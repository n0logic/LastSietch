/* portal-gear-viewer.js — turntable GLB viewer for equipped gear (ES module).
 *
 * Dynamic-import()ed from portal-character.js only when the user clicks "View
 * in 3D" on a gear card. three.js + GLTFLoader are shared with the solido
 * viewer via the same vendor files.
 *
 * Contract:  mountGear(canvasEl, templateId) → { setModel(templateId), dispose }
 *   The renderer/scene/camera/controls/lights/env/RAF are created ONCE and
 *   persist; setModel() swaps the model node and re-fits the camera without
 *   tearing the viewer down. dispose() releases everything.
 *
 * Lookup chain for each templateId:
 *   1. Fetch /admin/static/data/glb-manifest.json (module-memoised).
 *   2. If manifest.gear[templateId] has a file entry → load GLB from
 *      /admin/static/glb/gear/<file>.
 *   3. Otherwise → reject (caller shows icon fallback).
 *
 * Render:
 *   - Bind-pose for skeletal meshes (SkinnedMesh renders fine without animation).
 *   - Slow auto-rotate turntable (OrbitControls still lets user drag).
 *   - Hemisphere + directional key light, Dune ambient palette.
 *   - Fits the loaded mesh into the canvas frustum automatically.
 */
import * as THREE from './vendor/three/three.module.min.js';
import { OrbitControls } from './vendor/three/OrbitControls.js';
import { GLTFLoader } from './vendor/three/GLTFLoader.js';

// Cache-bust the JSON data sidecars. The portal service worker uses
// stale-while-revalidate for /admin/static/, so an UN-queried data URL gets the
// previously-cached copy served first (the 2026-06-22 "new swatch-lut not picked
// up" bug). A ?v= bump gives each deploy a distinct URL the SW has never cached,
// so the new data lands immediately. Bump on any *-tints/swatch-lut/manifest change.
const DATA_V = '20260622d';

let _manifestPromise = null;
let _tintsPromise = null;
let _swatchLutPromise = null;
const _glbCache = new Map();

function fetchManifest() {
  if (!_manifestPromise) {
    _manifestPromise = fetch('/admin/static/data/glb-manifest.json?v=' + DATA_V,
      { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  return _manifestPromise;
}

// Material tint sidecar (same file the solido viewer uses): the baked albedos
// for layered ML_* materials are the engine's grey dev card; real color comes
// from the offline layer-stack resolution. Entry: { t: base tint, e: emissive },
// both linear RGB, keyed by GLB material.name.
function fetchTints() {
  if (!_tintsPromise) {
    _tintsPromise = fetch('/admin/static/data/material-tints.json?v=' + DATA_V,
      { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return (d && d.materials) || null; })
      .catch(function () { return null; });
  }
  return _tintsPromise;
}

// Per-player DYE sidecar: maps the equipped piece's SwatchId (from the equipped
// feed's FCustomizationStats.SwatchId) to a colour, so the same GLB can render
// in whatever dye the player chose. Soft-fails to {} (the file may not exist yet
// — 404 is expected in dev until the swatch extraction lands). Memoised.
function fetchSwatchLut() {
  if (!_swatchLutPromise) {
    _swatchLutPromise = fetch('/admin/static/data/swatch-lut.json?v=' + DATA_V,
      { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return d || {}; })
      .catch(function () { return {}; });
  }
  return _swatchLutPromise;
}

// Resolve a SwatchId to a linear-RGB dye colour, or null when there's no entry.
// LUT SHAPE (confirmed, dye-researcher 2026-06-22): swatch-lut.json is
//   { _meta:{...}, swatches: { "<SwatchId>": { repr_rgb:[r,g,b] (linear 0..1),
//                                              rgb:[[..]x5], primary_rgb, hex, ... } } }
// where SwatchId == the equipped item's FCustomizationStats.SwatchId == the
// asset m_Identifier. We use repr_rgb = the single-tint representative the build
// picks (most-chromatic dye layer, luminance-floored) because layer-0/"primary"
// is the near-black armor base. Per-layer fidelity (the 5-entry rgb[]) is a
// future v2. This is the ONE place to change — callers only ever see [r,g,b]|null.
function dyeRgbFor(swatchId, lut) {
  if (!swatchId || !lut || !lut.swatches) return null;
  const entry = lut.swatches[swatchId];
  const rgb = entry && (entry.repr_rgb || entry.primary_rgb);
  return (Array.isArray(rgb) && rgb.length === 3) ? rgb : null;
}

function tintMaterial(mat, tints, dyeRgb) {
  if (!mat || mat.__lsTinted !== undefined) return;
  mat.__lsTinted = true;
  const entry = tints && tints[mat.name];
  const isLayered = /^ML_/i.test(mat.name);
  // Which materials carry visible colour from the offline layer stack: the
  // layered ML_* masters (whose baked albedo is the engine grey dev card) and
  // untextured albedos. Those are exactly the surfaces a dye should recolour.
  const colourable = isLayered || !mat.map;

  // DYE takes PRECEDENCE over the material-name tint and applies even when the
  // material has no tints entry (gear material-tints coverage is partial; the
  // dye IS the colour). REPLACE rather than multiply — the baked albedo is the
  // grey dev card, so multiplying would crush the dye toward black. Undyed
  // pieces (DEFAULT_SWATCH / empty SwatchId) get dyeRgb=null and fall through to
  // the material-name tint below.
  if (dyeRgb && colourable) {
    if (isLayered) mat.map = null;
    mat.color.setRGB(dyeRgb[0], dyeRgb[1], dyeRgb[2]);
  } else if (entry && entry.t) {
    const t = entry.t;
    if (isLayered) { mat.map = null; mat.color.setRGB(t[0], t[1], t[2]); }
    else if (!mat.map) { mat.color.setRGB(t[0], t[1], t[2]); }
  }

  if (entry && entry.e && mat.emissive) {
    mat.emissive.setRGB(entry.e[0], entry.e[1], entry.e[2]);
    mat.emissiveIntensity = 0.85;
  }
  mat.needsUpdate = true;
}

function applyTints(root, tints, dyeRgb) {
  if (!tints && !dyeRgb) return;   // dye applies even when material-tints is absent
  root.traverse(function (node) {
    if (!node.material) return;
    const mats = Array.isArray(node.material) ? node.material : [node.material];
    mats.forEach(function (m) { tintMaterial(m, tints, dyeRgb); });
  });
}

// Bring a full-body skeletal garment from its A/T bind pose to a relaxed
// arms-at-sides stance. The extracted SkinnedMeshes load in bind pose (no
// animation clip), so the upper arms jut straight out; we rotate the upperarm
// bones to point down + slightly outward. The rotation is computed in WORLD
// space (from the current elbow direction toward a target down vector) so it is
// independent of each bone's local-axis convention. SkinnedMesh re-skins from
// the bone matrices every frame, so a one-time set sticks. Idempotent
// (root.__lsPosed guard); a no-op for meshes with no upperarm bones (weapons).
function poseArmsToSides(root) {
  if (!root || root.__lsPosed) return;
  root.__lsPosed = true;
  const bones = {};
  root.traverse(function (n) { if (n.name && !bones[n.name]) bones[n.name] = n; });
  if (!bones.upperarm_l && !bones.upperarm_r) return;
  root.updateWorldMatrix(true, true);
  // [upperarm, elbow, outwardX] — character-left arm swings toward +X, right -X.
  // Target = mostly down, a little out (clears the hips) and a touch forward.
  [['upperarm_l', 'lowerarm_l', 0.22],
   ['upperarm_r', 'lowerarm_r', -0.22]].forEach(function (spec) {
    const up = bones[spec[0]], lo = bones[spec[1]];
    if (!up) return;
    up.updateWorldMatrix(true, false);
    const pUp = new THREE.Vector3().setFromMatrixPosition(up.matrixWorld);
    let cur;
    if (lo) {
      lo.updateWorldMatrix(true, false);
      cur = new THREE.Vector3().setFromMatrixPosition(lo.matrixWorld).sub(pUp).normalize();
    } else {
      cur = new THREE.Vector3(0, 1, 0)
        .applyQuaternion(up.getWorldQuaternion(new THREE.Quaternion()));
    }
    const tgt = new THREE.Vector3(spec[2], -1, 0.05).normalize();
    const delta = new THREE.Quaternion().setFromUnitVectors(cur, tgt);
    const world = up.getWorldQuaternion(new THREE.Quaternion());
    const newWorld = delta.multiply(world);             // world-space pre-rotate
    const parentWorld = up.parent
      ? up.parent.getWorldQuaternion(new THREE.Quaternion())
      : new THREE.Quaternion();
    up.quaternion.copy(parentWorld.invert().multiply(newWorld));
    up.updateMatrixWorld(true);
  });
}

function loadGlb(url) {
  if (_glbCache.has(url)) return _glbCache.get(url);
  const p = new Promise(function (resolve, reject) {
    new GLTFLoader().load(url, function (g) { resolve(g.scene); }, undefined, reject);
  });
  _glbCache.set(url, p);
  return p;
}

// Resolve a templateId to a tinted, cached GLB scene. Rejects when the item
// has no GLB file (caller shows the icon fallback). Tinting is idempotent
// (mat.__lsTinted guard), so applying it to a cached scene on re-show is safe.
// Optional swatchId tints the piece by the player's chosen dye (no-op when
// absent or when the dye LUT has no matching entry).
function resolveModel(templateId, swatchId) {
  return fetchManifest().then(function (manifest) {
    const gearMap = (manifest && manifest.gear) || {};
    const entry = gearMap[templateId];
    if (!entry || !entry.file) {
      return Promise.reject(new Error('no GLB for ' + templateId));
    }
    const url = '/admin/static/glb/gear/' + entry.file;
    return Promise.all([loadGlb(url), fetchTints(), fetchSwatchLut()]);
  }).then(function (res) {
    applyTints(res[0], res[1], dyeRgbFor(swatchId, res[2]));
    return res[0];
  });
}

export function mountGear(canvasEl, templateId, swatchId) {
  return resolveModel(templateId, swatchId).then(function (gltfScene) {
    return _mount(canvasEl, gltfScene);
  });
}

// Neutral studio environment (PMREM) so the gear's PBR materials get soft
// image-based ambient instead of rendering flat. Built once per renderer.
function makeGearEnvironment(renderer) {
  if (renderer.__envMap) return renderer.__envMap;
  const W = 16, H = 64;
  const data = new Uint8Array(W * H * 4);
  // Brighter floor than a true studio: the gear meshes ship GREY DEV-CARD
  // albedos (no real texture), so all colour comes from the muted tint sidecar.
  // A dark env floor crushed those mid-grey tints to near-black under ACES; lift
  // the whole gradient so the resolved tints actually read.
  const top = [0.72, 0.73, 0.76], mid = [0.54, 0.54, 0.57], bot = [0.24, 0.24, 0.27];
  function lerp(a, b, t) { return a + (b - a) * t; }
  for (let y = 0; y < H; y++) {
    const t = y / (H - 1);
    let r, g, b;
    if (t < 0.5) { const k = t / 0.5; r = lerp(top[0], mid[0], k); g = lerp(top[1], mid[1], k); b = lerp(top[2], mid[2], k); }
    else { const k = (t - 0.5) / 0.5; r = lerp(mid[0], bot[0], k); g = lerp(mid[1], bot[1], k); b = lerp(mid[2], bot[2], k); }
    for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4;
      data[i] = Math.round(r * 255); data[i + 1] = Math.round(g * 255);
      data[i + 2] = Math.round(b * 255); data[i + 3] = 255;
    }
  }
  const tex = new THREE.DataTexture(data, W, H, THREE.RGBAFormat);
  tex.mapping = THREE.EquirectangularReflectionMapping;
  tex.needsUpdate = true;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const env = pmrem.fromEquirectangular(tex).texture;
  pmrem.dispose();
  tex.dispose();
  renderer.__envMap = env;
  return env;
}

// Persistent harness: renderer + scene + camera + controls + lights + env +
// RAF are built once. The model lives in a pivot Group we own (`pivot`); on
// swap we replace the cached scene parented under it — never mutating the
// cached scene's own transform — and re-fit the camera/controls.
function _mount(canvasEl, gltfScene) {
  // Reference scale for the light rig. Lights are positioned relative to this
  // so the rig stays fixed across model swaps; per-model framing is handled by
  // the camera/controls in fitModel().
  const REF = 1;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#0c0a07');

  // Lights: warm key + cool fill + rim, balanced against the env ambient.
  scene.add(new THREE.HemisphereLight('#cdb98a', '#2a2012', 1.05));
  const key = new THREE.DirectionalLight('#ffe6b0', 1.7);
  key.position.set(REF * 1.2, REF * 2, REF * 0.8);
  scene.add(key);
  const fill = new THREE.DirectionalLight('#a0c4ff', 0.55);
  fill.position.set(-REF, REF * 0.5, -REF * 0.5);
  scene.add(fill);
  const rim = new THREE.DirectionalLight('#fff1d6', 0.6);
  rim.position.set(-REF * 0.5, REF * 0.8, -REF * 1.4);
  scene.add(rim);
  // Soft front fill from the viewer's side, low and broad, so the camera-facing
  // surfaces (face, chest, legs) are not left in shadow by the high key light —
  // this is what left the dark garments reading as a near-black silhouette.
  const front = new THREE.DirectionalLight('#fff4e0', 0.85);
  front.position.set(REF * 0.2, REF * 0.4, REF * 2.6);
  scene.add(front);

  const renderer = new THREE.WebGLRenderer({ canvas: canvasEl, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.35;
  scene.environment = makeGearEnvironment(renderer);

  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 50);
  camera.position.set(0, 0.3, 2.8);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.07;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 1.8;
  controls.target.set(0, 0, 0);
  controls.update();

  // Owned pivot Group: we offset THIS (not the cached scene) to centre the
  // model, so cached scenes stay pristine for instant re-shows elsewhere.
  let pivot = null;       // current pivot Group in the scene
  let modelNode = null;   // the cached gltfScene currently parented under pivot

  function clearModel() {
    if (pivot) { scene.remove(pivot); }
    if (pivot && modelNode && modelNode.parent === pivot) {
      pivot.remove(modelNode);  // detach cached scene without disposing it
    }
    pivot = null;
    modelNode = null;
  }

  // Frame `node` (a cached gltfScene): recompute bounds, offset our pivot to
  // recentre, and re-fit camera position + controls target/limits.
  function fitModel(node) {
    const box = new THREE.Box3().setFromObject(node);
    const size = new THREE.Vector3();
    const centre = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(centre);
    const radius = Math.max(size.x, size.y, size.z) * 0.5 || 1;

    const g = new THREE.Group();
    g.add(node);
    g.position.sub(centre);   // recentre via the pivot, not the cached scene
    scene.add(g);
    pivot = g;
    modelNode = node;

    camera.near = Math.max(radius * 0.01, 0.001);
    camera.far = radius * 50;
    camera.position.set(0, radius * 0.3, radius * 2.8);
    camera.updateProjectionMatrix();

    controls.target.set(0, 0, 0);
    controls.minDistance = radius * 0.8;
    controls.maxDistance = radius * 6;
    controls.update();
  }

  function setModel(templateId, swatchId) {
    return resolveModel(templateId, swatchId).then(function (node) {
      clearModel();
      fitModel(node);
    });
  }

  // First model is already resolved by the caller (mountGear) — frame it now.
  fitModel(gltfScene);

  let _lastW = 0, _lastH = 0;
  function resize() {
    const w = canvasEl.clientWidth || 400;
    const h = canvasEl.clientHeight || 400;
    if (w === _lastW && h === _lastH) return;  // guard against resize feedback
    _lastW = w; _lastH = h;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  const ro = ('ResizeObserver' in window) ? new ResizeObserver(resize) : null;
  if (ro) ro.observe(canvasEl); else window.addEventListener('resize', resize);

  let raf = 0;
  let running = true;
  function tick() {
    if (!running) return;
    controls.update();
    renderer.render(scene, camera);
    raf = requestAnimationFrame(tick);
  }
  tick();

  return {
    // Swap the displayed model in-place. Resolves when the new model is framed;
    // rejects (without touching the current model) when the item has no GLB.
    setModel: setModel,
    // Detach the current model from the scene without disposing it (keeps the
    // persistent viewer alive + the _glbCache intact). Used when the caller
    // shows a no-model placeholder.
    clearModel: clearModel,
    dispose: function () {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
      controls.dispose();
      // Detach the current model from the scene but do NOT dispose its
      // geometries/materials: the gltfScene belongs to the module-level
      // _glbCache, which is shared with the legacy per-card viewer and with a
      // later remount of this stage. Disposing it would corrupt that cache.
      // The renderer.dispose() below releases this canvas's GPU context; cached
      // scenes stay valid and re-show instantly on the next mount.
      clearModel();
      renderer.dispose();
    },
  };
}
