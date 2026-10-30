// The three.js half of the Character page's 3D gear stage. Dynamic-imported by
// GearStage.svelte only when the player opens the stage, so three never touches
// the page load. Colour rules live in $lib/gearColor.js; this file only draws.
//
// Each garment material gets a MeshStandardMaterial patched in onBeforeCompile:
// the layer index is read from the material's index map (nearest filter, at
// UV1) and the base colour becomes uLayer[index]. The baked meshes carry no
// albedo, so there is nothing to multiply it with. Materials without verified
// layer data fall back to one flat tint.
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { clone as cloneSkinned } from 'three/examples/jsm/utils/SkeletonUtils.js';
import { colourMode, layerUniforms, reprTint, swatchDyes, NEUTRAL } from '$lib/gearColor.js';

const GLB_BASE = '/portal/gear/glb/';
const IDMAP_BASE = '/portal/gear/idmap/';

const _glb = new Map();
const _idmaps = new Map();

function loadGlb(file, hash) {
  const url = GLB_BASE + file + (hash ? '?v=' + hash : '');
  if (!_glb.has(url)) {
    const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
    _glb.set(url, loader.loadAsync(url).then((g) => g.scene));
  }
  return _glb.get(url);
}

function loadIdmap(name) {
  if (!_idmaps.has(name)) {
    _idmaps.set(name, new THREE.TextureLoader().loadAsync(IDMAP_BASE + name + '.png').then((t) => {
      // Raw layer indices, not colour: no filtering, no colour conversion, and
      // glTF UVs put v=0 at the top row.
      t.magFilter = THREE.NearestFilter;
      t.minFilter = THREE.NearestFilter;
      t.generateMipmaps = false;
      t.flipY = false;
      t.colorSpace = THREE.NoColorSpace;
      t.wrapS = t.wrapT = THREE.RepeatWrapping;
      t.needsUpdate = true;
      return t;
    }));
  }
  return _idmaps.get(name);
}

function layeredMaterial(idmap, uniforms) {
  const mat = new THREE.MeshStandardMaterial({ roughness: 0.82, metalness: 0.0 });
  const shared = { uLayer: { value: uniforms }, uIdMap: { value: idmap } };
  mat.userData.layer = shared;
  mat.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, shared);
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\n#ifndef USE_UV1\nattribute vec2 uv1;\n#endif\nvarying vec2 vLsUv1;')
      .replace('#include <uv_vertex>', '#include <uv_vertex>\nvLsUv1 = uv1;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', '#include <common>\nuniform vec3 uLayer[12];\nuniform sampler2D uIdMap;\nvarying vec2 vLsUv1;')
      .replace('#include <color_fragment>', [
        '#include <color_fragment>',
        'int lsIdx = int(clamp(floor(texture2D(uIdMap, fract(vLsUv1)).r * 255.0 + 0.5), 0.0, 11.0));',
        'vec3 lsCol = uLayer[0];',
        'for (int i = 0; i < 12; i++) { if (i == lsIdx) lsCol = uLayer[i]; }',
        'diffuseColor.rgb *= lsCol;',
      ].join('\n'));
  };
  mat.customProgramCacheKey = () => 'ls-layered-v1';
  return mat;
}

function flatMaterial(rgb) {
  const c = rgb || NEUTRAL;
  return new THREE.MeshStandardMaterial({ color: new THREE.Color(c[0], c[1], c[2]), roughness: 0.82, metalness: 0.0 });
}

/** Mount the stage. plan = gearColor.stagePlan(); data = {layers, lut};
 *  opts = {reducedMotion}. Returns {setDyed(bool), dispose()}. */
export async function mountStage(canvas, plan, data, opts = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xf2e6cc, 0x2a2012, 1.4));
  const key = new THREE.DirectionalLight(0xffe6b0, 1.6);
  key.position.set(1.2, 2, 1.6);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xa0c4ff, 0.5);
  fill.position.set(-1.5, 0.6, -0.8);
  scene.add(fill);
  const front = new THREE.DirectionalLight(0xfff4e0, 0.9);
  front.position.set(0.2, 0.5, 2.6);
  scene.add(front);

  const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 50);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = !opts.reducedMotion;
  controls.enablePan = false;
  controls.autoRotate = !opts.reducedMotion;
  controls.autoRotateSpeed = 1.2;

  const figure = new THREE.Group();
  scene.add(figure);
  // Every layered material, so the dye toggle can refill its uniforms in place.
  const layered = [];

  const scenes = await Promise.all(plan.map((p) => loadGlb(p.file, p.hash)));
  for (let i = 0; i < plan.length; i++) {
    const p = plan[i];
    // A skinned clone with its own bones and materials, so the cache stays clean.
    const node = cloneSkinned(scenes[i]);
    const made = new Map();
    const pending = [];
    node.traverse((o) => {
      if (!o.isMesh) return;
      const name = o.material && o.material.name;
      o.visible = p.materials.includes(name);
      if (!o.visible) return;
      if (!made.has(name)) {
        const swatch = p.swatches[name] || '';
        const how = colourMode(name, data.layers);
        if (how.mode === 'layered') {
          const mat = layeredMaterial(null, layerUniforms(how.entry, swatchDyes(data.lut, swatch)));
          layered.push({ mat, entry: how.entry, swatch });
          pending.push(loadIdmap(how.idmap).then((t) => { mat.userData.layer.uIdMap.value = t; }));
          made.set(name, mat);
        } else {
          made.set(name, flatMaterial(reprTint(data.lut, swatch)));
        }
      }
      o.material = made.get(name);
      o.frustumCulled = false;
    });
    await Promise.all(pending);
    figure.add(node);
  }

  const box = new THREE.Box3().setFromObject(figure);
  const size = box.getSize(new THREE.Vector3());
  const centre = box.getCenter(new THREE.Vector3());
  figure.position.sub(centre);
  const radius = Math.max(size.x, size.y, size.z) * 0.5 || 1;
  camera.near = radius * 0.01;
  camera.far = radius * 40;
  camera.position.set(0, radius * 0.15, radius * 3.4);
  controls.target.set(0, 0, 0);
  controls.minDistance = radius * 1.2;
  controls.maxDistance = radius * 6;
  controls.update();

  function resize() {
    const w = canvas.clientWidth || 320, h = canvas.clientHeight || 400;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(canvas);

  let raf = 0, running = true;
  function tick() {
    if (!running) return;
    controls.update();
    renderer.render(scene, camera);
    raf = requestAnimationFrame(tick);
  }
  tick();

  return {
    setDyed(on) {
      for (const l of layered) {
        l.mat.userData.layer.uLayer.value = layerUniforms(l.entry, on ? swatchDyes(data.lut, l.swatch) : null);
      }
    },
    dispose() {
      running = false;
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      figure.traverse((o) => { if (o.isMesh && o.material) o.material.dispose(); });
      renderer.dispose();
    },
  };
}
