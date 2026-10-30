// The Sand Table viewer: the ONLY module under sandtable/ that imports three, so
// Rollup isolates three + the GLTF/meshopt loaders into one lazy chunk (same pattern as
// holo/stage-adapter.js). Hybrid look: the in-game overland's terraced relief with the
// game's own miniatures on mauve discs, set inside a carved sietch-chamber rim, with our
// live layer (status glow, band marks, viewer pin, rim countdown) on top.

import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { createStage } from '../holo/stage.js';
import { createCompass } from '../compass.js';
import { createView, setOrbit, stepHeading, attachGestures, panToNormalized, ORBIT_DEFAULT } from '../transform.js';
import { worldUrl, travelBadge, STORM_STONES_MAP } from '../../world/data.js';
import { todPreset } from '../../world/tod.js';
import { createTableCamera } from './camera.js';
import { createEnv, createGlowTexture } from './env.js';
import { createStones } from './stones.js';
import { solveRadii } from '../../world/layout.js';
import { createFx } from './fx.js';

const CM = 1e-4;                  // overmap cm -> scene units (1 unit = 100 m)
const EXAG = 2.6;                 // vertical exaggeration of the terraced relief
const DEFAULT_ORBIT = { azimuth: 0, tilt: 38 };
const MARGIN = 1.45;       // default frame = bounding box of the locations x this

const ease = (t) => 1 - Math.pow(1 - t, 3);

export function createSandTable(host, opts) {
  const { world, base } = opts;
  const reduced = !!opts.reducedMotion;
  const hi = !!opts.hi;
  const exag = opts.exag || EXAG;

  if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
  const canvas = document.createElement('canvas');
  canvas.setAttribute('aria-hidden', 'true');
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;touch-action:none;display:block';
  host.appendChild(canvas);
  const labelLayer = document.createElement('div');
  labelLayer.style.cssText = 'position:absolute;inset:0;pointer-events:none;overflow:hidden';
  host.appendChild(labelLayer);

  const stage = createStage(THREE, { canvas, viewport: host, onContextLost: opts.onContextLost || function () {} });
  const renderer = stage.renderer;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  const scene = stage.scene;
  scene.background = new THREE.Color(0x090604);
  scene.fog = new THREE.FogExp2(0x0a0705, 0.0075);

  let destroyed = false;
  let ctl = null;

  // ---- table frame: centre + radius from the game's own contours and stones -----------
  let minx = Infinity, maxx = -Infinity, minz = Infinity, maxz = -Infinity;
  for (const l of world.contours.lines) {
    for (let i = 0; i < l.length; i += 2) {
      const x = l[i] * CM, z = l[i + 1] * CM;
      if (x < minx) minx = x; if (x > maxx) maxx = x; if (z < minz) minz = z; if (z > maxz) maxz = z;
    }
  }
  world.stones.forEach((s) => {
    minx = Math.min(minx, s.x * CM); maxx = Math.max(maxx, s.x * CM); minz = Math.min(minz, s.y * CM); maxz = Math.max(maxz, s.y * CM);
  });
  const cx = (minx + maxx) / 2, cz = (minz + maxz) / 2;
  let rr = 0;
  for (const l of world.contours.lines) {
    for (let i = 0; i < l.length; i += 2) rr = Math.max(rr, Math.hypot(l[i] * CM - cx, l[i + 1] * CM - cz));
  }
  world.stones.forEach((s) => { rr = Math.max(rr, Math.hypot(s.x * CM - cx, s.y * CM - cz) + 1.2); });
  const R = rr * 1.035;

  const gl = createGlowTexture(THREE);
  const env = createEnv(THREE, { cx, cz, R, hi });
  scene.add(env.group);

  const tcam = createTableCamera(THREE, { cx, cz, R, el: host });
  stage.setCamera(tcam);
  const view = createView(1000);
  setOrbit(view, DEFAULT_ORBIT.azimuth, DEFAULT_ORBIT.tilt);

  // ---- state shared with the shaders --------------------------------------------------
  const U = {
    uArt: { value: null },
    uArtRect: { value: new THREE.Vector4() },
    uTint: { value: 0.55 },
    uFocus: { value: new THREE.Vector4(0, 0, 1.6, 0) },
    uHmax: { value: 1 },
    uGlow: { value: new THREE.Color(0.5, 0.28, 0.08) },
    uDetail: { value: null },
    uDetailK: { value: 0 },
    uDetailScale: { value: 1 },
  };
  function setRect(ar) { U.uArtRect.value.set(ar.x0 * CM, ar.y0 * CM, (ar.x1 - ar.x0) * CM, (ar.y1 - ar.y0) * CM); }
  setRect(world.art.worldRectCm);

  function sandMaterial(bed) {
    const m = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.96, metalness: 0, flatShading: !bed });
    m.onBeforeCompile = (sh) => {
      Object.assign(sh.uniforms, U);
      sh.uniforms.uBed = { value: bed ? 1 : 0 };
      sh.vertexShader = sh.vertexShader
        .replace('#include <common>', '#include <common>\nvarying vec3 vWP;')
        .replace('#include <begin_vertex>', '#include <begin_vertex>\nvWP = (modelMatrix * vec4(transformed, 1.0)).xyz;');
      sh.fragmentShader = sh.fragmentShader
        .replace('#include <common>', `#include <common>
          varying vec3 vWP; uniform sampler2D uArt; uniform vec4 uArtRect; uniform float uTint; uniform vec4 uFocus;
          uniform float uHmax; uniform vec3 uGlow; uniform float uBed;
          uniform sampler2D uDetail; uniform float uDetailK; uniform float uDetailScale;
          float hash21(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }`)
        .replace('#include <color_fragment>', `#include <color_fragment>
          vec2 auv = vec2((vWP.x - uArtRect.x) / uArtRect.z, 1.0 - (vWP.z - uArtRect.y) / uArtRect.w);
          vec3 art = texture2D(uArt, auv).rgb;
          float ht = clamp(vWP.y / uHmax, 0.0, 1.0);
          vec3 sand = mix(vec3(0.50, 0.31, 0.16), vec3(0.74, 0.53, 0.30), ht);
          float grain = hash21(floor(vWP.xz * 90.0)) * 0.10 - 0.05;
          sand *= 1.0 + grain;
          float dt = texture2D(uDetail, vWP.xz * uDetailScale).g;
          sand *= 1.0 + (dt - 0.5) * uDetailK;
          vec3 fn = normalize(cross(dFdx(vWP), dFdy(vWP)));
          float riser = smoothstep(0.35, 0.9, 1.0 - abs(fn.y)) * (1.0 - uBed);
          vec3 col = mix(sand, sand * 0.6 + art * 0.62, uTint + uBed * 0.45);
          col = mix(col, vec3(0.9, 0.72, 0.47), riser * 0.30);
          // the polar cap and salt flats are white-grey in the art: let them read white, not sand-tinted
          float aMax = max(art.r, max(art.g, art.b)), aMin = min(art.r, min(art.g, art.b));
          float whiteness = smoothstep(0.34, 0.16, (aMax - aMin) / max(aMax, 0.001)) * smoothstep(0.2, 0.4, aMax);
          col = mix(col, art * 1.9, whiteness * 0.92 * step(0.5, uTint + uBed));
          col += uGlow * smoothstep(uFocus.z, 0.0, length(vWP.xz - uFocus.xy)) * uFocus.w;
          diffuseColor.rgb = col;`);
    };
    return m;
  }

  const gltf = new GLTFLoader();
  gltf.setMeshoptDecoder(MeshoptDecoder);
  const url = (n) => worldUrl(base, n, world.stamp);

  // heightfield of the (exaggerated, scene-unit) relief: stone placement, contour drape, picks
  const CELL = 0.06;
  let gx0 = cx - R, gz0 = cz - R, gw = Math.ceil((2 * R) / CELL) + 2;
  let grid = new Float32Array(gw * gw);
  function heightAt(x, z) {
    const fx = (x - gx0) / CELL, fz = (z - gz0) / CELL;
    const ix = Math.floor(fx), iz = Math.floor(fz);
    if (ix < 0 || iz < 0 || ix >= gw - 1 || iz >= gw - 1) return 0;
    const tx = fx - ix, tz = fz - iz;
    const a = grid[iz * gw + ix], b = grid[iz * gw + ix + 1], c = grid[(iz + 1) * gw + ix], d = grid[(iz + 1) * gw + ix + 1];
    return (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz;
  }

  let terrain = null, bed = null, contourLines = null, stones = null, fx = null, minis = null;
  let artTex = null, envTex = null;
  const anim = { cur: null };
  let dirty = true;
  let selectedId = null, hoverId = null;
  let hourCache = 16, lastEnvTick = -99;

  function syncFx() { if (fx) fx.setScale(host.clientHeight || 800, tcam.camera.fov); }
  const ready = (async () => {
    const [terr, mini, fxData, hires] = await Promise.all([
      gltf.loadAsync(url('overmap-terrace.glb')),
      gltf.loadAsync(url('overmap-miniatures.glb')),
      fetch(url('overmap-locations-fx.json')).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      fetch(url('overmap-hires.json')).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    let art;
    if (hires && hires.worldRectCm) {
      const file = hi || !hires.lowTier ? hires.image : hires.lowTier.image;
      try { art = await new THREE.TextureLoader().loadAsync(url(file)); setRect(hires.worldRectCm); } catch { art = null; }
    }
    if (!art) { art = await new THREE.TextureLoader().loadAsync(url('overmap-art-2048.webp')); setRect(world.art.worldRectCm); }
    if (destroyed) return;
    art.colorSpace = THREE.SRGBColorSpace; art.anisotropy = hi ? 8 : 4;
    artTex = art; U.uArt.value = art;

    // ---- relief: dequantize to float, place in scene units, clip to the table disc ----
    let tmesh = null;
    terr.scene.traverse((o) => { if (o.isMesh && !tmesh) tmesh = o; });
    tmesh.updateWorldMatrix(true, false);
    const src = tmesh.geometry;
    const pa = src.attributes.position, n = pa.count;
    const m = tmesh.matrixWorld.elements;
    const pos = new Float32Array(n * 3);
    let ymin = Infinity, ymax = -Infinity;
    for (let i = 0; i < n; i++) {
      const x = pa.getX(i), y = pa.getY(i), z = pa.getZ(i);
      const wx = m[0] * x + m[4] * y + m[8] * z + m[12];
      const wy = m[1] * x + m[5] * y + m[9] * z + m[13];
      const wz = m[2] * x + m[6] * y + m[10] * z + m[14];
      pos[i * 3] = wx * 0.01; pos[i * 3 + 1] = wy; pos[i * 3 + 2] = wz * 0.01;
      if (wy < ymin) ymin = wy; if (wy > ymax) ymax = wy;
    }
    // ground level: tiles disagree by about a metre on the flat sand, which shows as straight
    // seams and slabs once exaggerated; snap everything within a metre of the floor to one plane
    const k = 0.01 * exag;
    for (let i = 0; i < n; i++) { const y = pos[i * 3 + 1]; pos[i * 3 + 1] = (y <= 1.0 ? 0 : y) * k; }
    U.uHmax.value = (ymax - 0) * k * 0.8;
    const idx = src.index.array;
    const keep = new Uint32Array(idx.length);
    let kc = 0;
    const R2 = R * R;
    for (let t = 0; t < idx.length; t += 3) {
      const a = idx[t] * 3, b = idx[t + 1] * 3, c = idx[t + 2] * 3;
      const mx = (pos[a] + pos[b] + pos[c]) / 3 - cx, mz = (pos[a + 2] + pos[b + 2] + pos[c + 2]) / 3 - cz;
      if (mx * mx + mz * mz < R2) { keep[kc++] = idx[t]; keep[kc++] = idx[t + 1]; keep[kc++] = idx[t + 2]; }
    }
    // nothing may reach the rim: pull edge vertices inside the inner wall and fade them to the bed
    const used = new Uint8Array(n);
    for (let t = 0; t < kc; t++) used[keep[t]] = 1;
    for (let v = 0; v < n; v++) {
      if (!used[v]) continue;
      const dx = pos[v * 3] - cx, dz = pos[v * 3 + 2] - cz, d = Math.hypot(dx, dz);
      if (d > R * 0.9) {
        const t = Math.min(1, (d - R * 0.9) / (R * 0.075));
        const fade = 1 - t * t * (3 - 2 * t);
        pos[v * 3 + 1] *= fade;
        if (d > R * 0.985) { const f = (R * 0.985) / d; pos[v * 3] = cx + dx * f; pos[v * 3 + 2] = cz + dz * f; }
      }
    }
    // level a pad under every stone so the disc sits flush like the game's plate
    const radii = solveRadii(world.stones);
    const pads = world.stones.map((s) => {
      const r = radii[s.id];
      const px = (s.disc ? s.disc.centerCm[0] : s.x) * CM, pz = (s.disc ? s.disc.centerCm[1] : s.y) * CM;
      let sum = 0, cnt = 0;
      for (let v = 0; v < n; v++) {
        if (!used[v]) continue;
        const dx = pos[v * 3] - px, dz = pos[v * 3 + 2] - pz;
        if (dx * dx + dz * dz < r * r) { sum += pos[v * 3 + 1]; cnt++; }
      }
      return { px, pz, r, y: cnt ? sum / cnt : 0 };
    });
    for (let v = 0; v < n; v++) {
      if (!used[v]) continue;
      for (const pd of pads) {
        const d = Math.hypot(pos[v * 3] - pd.px, pos[v * 3 + 2] - pd.pz);
        if (d < pd.r * 1.5) {
          const t = d < pd.r * 1.02 ? 1 : 1 - (d - pd.r * 1.02) / (pd.r * 0.48);
          pos[v * 3 + 1] += (pd.y - 0.03 - pos[v * 3 + 1]) * (t * t * (3 - 2 * t));
        }
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setIndex(new THREE.BufferAttribute(keep.slice(0, kc), 1));
    geo.computeVertexNormals();
    geo.computeBoundingSphere();
    terrain = new THREE.Mesh(geo, sandMaterial(false));
    terrain.castShadow = true; terrain.receiveShadow = true;
    terrain.userData.stats = { tris: kc / 3, ymin, ymax, exag };
    src.dispose();

    // heightfield (max per cell, then fill holes from neighbours)
    grid.fill(-1e9);
    for (let i = 0; i < n; i++) {
      const ix = Math.round((pos[i * 3] - gx0) / CELL), iz = Math.round((pos[i * 3 + 2] - gz0) / CELL);
      if (ix < 0 || iz < 0 || ix >= gw || iz >= gw) continue;
      const j = iz * gw + ix;
      if (pos[i * 3 + 1] > grid[j]) grid[j] = pos[i * 3 + 1];
    }
    for (let pass = 0; pass < 4; pass++) {
      const g2 = grid.slice();
      for (let z = 1; z < gw - 1; z++) for (let x = 1; x < gw - 1; x++) {
        const j = z * gw + x;
        if (g2[j] > -1e8) continue;
        let s = 0, c = 0;
        for (const q of [j - 1, j + 1, j - gw, j + gw]) if (g2[q] > -1e8) { s += g2[q]; c++; }
        if (c) grid[j] = s / c;
      }
    }
    for (let j = 0; j < grid.length; j++) if (grid[j] < -1e8) grid[j] = 0;

    // bed: the island and anything the relief lacks is drawn from the art alone
    bed = new THREE.Mesh(new THREE.CircleGeometry(R, 128), sandMaterial(true));
    bed.rotation.x = -Math.PI / 2; bed.position.set(cx, -0.02, cz); bed.receiveShadow = true;

    // ---- engraved contours, draped on the relief ----------------------------------------
    const seg = [];
    for (const l of world.contours.lines) {
      for (let i = 2; i < l.length; i += 2) {
        const x0 = l[i - 2] * CM, z0 = l[i - 1] * CM, x1 = l[i] * CM, z1 = l[i + 1] * CM;
        const len = Math.hypot(x1 - x0, z1 - z0);
        const steps = Math.max(1, Math.ceil(len / 0.07));
        let px = x0, pz = z0;
        for (let s = 1; s <= steps; s++) {
          const qx = x0 + ((x1 - x0) * s) / steps, qz = z0 + ((z1 - z0) * s) / steps;
          if ((px - cx) ** 2 + (pz - cz) ** 2 < R2 && (qx - cx) ** 2 + (qz - cz) ** 2 < R2) {
            seg.push(px, heightAt(px, pz) + 0.035, pz, qx, heightAt(qx, qz) + 0.035, qz);
          }
          px = qx; pz = qz;
        }
      }
    }
    const cg = new THREE.BufferGeometry();
    cg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(seg), 3));
    contourLines = new THREE.LineSegments(cg, new THREE.ShaderMaterial({
      transparent: true, depthWrite: false,
      uniforms: { uFocus: U.uFocus, uColor: { value: new THREE.Color(0xf0c070) } },
      vertexShader: 'varying vec3 vP; void main(){ vP = (modelMatrix*vec4(position,1.0)).xyz; gl_Position = projectionMatrix*viewMatrix*vec4(vP,1.0); }',
      fragmentShader: 'uniform vec4 uFocus; uniform vec3 uColor; varying vec3 vP; void main(){ float f = smoothstep(uFocus.z*1.6, 0.0, length(vP.xz-uFocus.xy))*uFocus.w; gl_FragColor = vec4(uColor, 0.16 + 0.5*f); }',
    }));
    contourLines.frustumCulled = false;

    // ---- miniatures ----------------------------------------------------------------------
    minis = mini.scene;
    const pm = new THREE.PMREMGenerator(renderer);
    envTex = pm.fromScene(new RoomEnvironment(), 0.04).texture;
    pm.dispose();
    stones = createStones(THREE, {
      world, heightAt, glowTex: gl, hi, skirtMat: sandMaterial(false), radii, envTex,
      takeMini: (id) => { const o = minis.getObjectByName('loc_' + id); if (o && o.parent) o.parent.remove(o); return o || null; },
    });
    stones.tuneMaterials(stones.group);
    stones.group.traverse((o) => {
      if (o.isMesh && o.material && !Array.isArray(o.material) && o.material.isMeshStandardMaterial) o.material.envMapIntensity = 0.55;
      if (o.isMesh && o.material && o.material.isMeshStandardMaterial && o.material.map && !o.material.envMap) o.material.envMap = envTex;
    });

    // ---- FX ------------------------------------------------------------------------------
    fx = createFx(THREE, { reducedMotion: reduced, glowTex: gl, url: (n) => url(n), hi, heightAt, getHour: opts.getHour });
    if (fxData) {
      fx.build(fxData, (id) => {
        const e = stones.byId.get(id);
        return e ? { x: e.anchor.x, y: e.anchor.y, z: e.anchor.z, s: e.ms, fxScale: 1 } : null;
      });
    }
    if (fx.buildExtras) fx.buildExtras({ fxData, stones, mini: world.mini, disc: stones.byId.get('deep-desert'), stormStones: Object.keys(STORM_STONES_MAP) });
    const lines = (opts.world.mini && opts.world.mini.travelLines) || [];
    fx.loadTextures && fx.loadTextures().then((t) => {
      if (destroyed || !t) return;
      if (t.rim) { t.rim.repeat.set(10, 1.5); env.rimMat.map = t.rim; env.rimMat.bumpMap = t.rim; env.rimMat.needsUpdate = true; }
      if (t.sand) { U.uDetail.value = t.sand; U.uDetailScale.value = 50 / (2 * R); U.uDetailK.value = 0.55; }
    }).catch(() => {});
    fx.travel(lines, (p) => [p[0] * CM, heightAt(p[0] * CM, p[1] * CM) + 0.18 + p[2] * CM * 8, p[1] * CM]);

    scene.add(terrain, bed, contourLines, stones.group, fx.group);
    scene.traverse((o) => { if (o.isMesh && o !== terrain) o.frustumCulled = true; });

    // initial state from opts
    if (opts.initial) {
      stones.setStatus(opts.initial.status || {});
      stones.setBands(opts.initial.bands || {});
      stones.setAnchor(opts.initial.anchor || null);
    }
    { const f = framing(); sizeView(); view.zoom = f.zoom; panToNormalized(view, f.nx, f.ny); apply(); }
    if (opts.initialStorms && fx.setStorm) for (const [id, st] of Object.entries(opts.initialStorms)) fx.setStorm(st, id);
    syncFx();
    buildLabels();
    applyHour();
    drawCountdown();
    host.dispatchEvent(new CustomEvent('sandtable-ready'));
  })();

  // ---- labels (DOM, projected each frame the view changes) -----------------------------
  const labels = new Map();
  function buildLabels() {
    stones.byId.forEach((e, id) => {
      const s = e.s;
      const d = document.createElement('div');
      const gold = s.label && s.label.style === 'gold3d';
      d.className = 'st-label ' + (gold ? 'st-gold' : 'st-cap');
      d.textContent = gold ? s.name.toUpperCase() : s.name;
      d.style.cssText = 'position:absolute;left:0;top:0;white-space:nowrap;will-change:transform;opacity:0';
      labelLayer.appendChild(d);
      const sub = document.createElement('span');
      sub.style.cssText = 'display:none;margin-top:1px;font:600 11px/1 "Saira Condensed",sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#f0d9a8;text-shadow:0 1px 3px #000';
      d.style.display = 'flex'; d.style.flexDirection = 'column'; d.style.alignItems = 'center';
      d.appendChild(sub);
      labels.set(id, { el: d, sub, gold, x: -1, y: -1, vis: false, cap: '' });
    });
  }
  // Labels: regions and settlements always show; everything else yields to anything already
  // placed (label-label) and to any other stone's disc (label-disc), by priority.
  let labelOrder = null;
  function updateLabels() {
    if (!stones) return;
    if (!labelOrder) {
      const pri = (e) => (e.s.label && e.s.label.style === 'gold3d' ? 0 : e.s.live ? 1 : 2);
      labelOrder = Array.from(labels.keys()).sort((a, b) => pri(stones.byId.get(a)) - pri(stones.byId.get(b)));
    }
    const dist = tcam.camera.position.distanceTo(tcam.target);
    const placed = [];
    const discs = [];
    stones.byId.forEach((e, id) => {
      const c = tcam.project(e.cxu, e.plateY, e.czu), edge = tcam.project(e.cxu + e.r, e.plateY, e.czu);
      discs.push({ id, x: c.x, y: c.y, r: Math.hypot(edge.x - c.x, edge.y - c.y) * 1.05 });
    });
    for (const id of labelOrder) {
      const l = labels.get(id), e = stones.byId.get(id);
      const p = tcam.project(e.cxu, e.plateY + 0.04, e.czu + e.r * 1.25);
      const badge = travelBadge(e.status);
      const cap = badge ? badge.caption : '';
      if (cap !== l.cap) { l.cap = cap; l.sub.textContent = cap; l.sub.style.display = cap ? 'block' : 'none'; l.w = 0; }
      const forced = l.gold || e.sel || e.hover;
      let show = p.visible && (forced || dist < R * 1.5 || !!cap);
      if (show) {
        if (!l.w) { l.w = l.el.offsetWidth || 80; l.h = l.el.offsetHeight || 16; }
        const box = { x0: p.x - l.w / 2, x1: p.x + l.w / 2, y0: p.y, y1: p.y + l.h };
        if (!forced) {
          for (const q of placed) if (box.x0 < q.x1 && box.x1 > q.x0 && box.y0 < q.y1 && box.y1 > q.y0) { show = false; break; }
          if (show) for (const d of discs) {
            if (d.id === id) continue;
            const nx = Math.max(box.x0, Math.min(d.x, box.x1)), ny = Math.max(box.y0, Math.min(d.y, box.y1));
            if (Math.hypot(nx - d.x, ny - d.y) < d.r) { show = false; break; }
          }
        }
        if (show) placed.push(box);
      }
      if (show !== l.vis) { l.vis = show; l.el.style.opacity = show ? '1' : '0'; }
      if (show && (Math.abs(p.x - l.x) > 0.4 || Math.abs(p.y - l.y) > 0.4)) {
        l.x = p.x; l.y = p.y;
        l.el.style.transform = `translate(${p.x.toFixed(1)}px,${p.y.toFixed(1)}px) translate(-50%,0)`;
      }
    }
  }

  // ---- time of day + rim countdown -------------------------------------------------------
  function applyHour() {
    hourCache = opts.getHour ? opts.getHour() : 12.5;
    const look = env.setSun(hourCache);
    renderer.toneMappingExposure = look.exposure;
    scene.background.copy(env.bgCol); scene.fog.color.copy(env.bgCol);
    stones && stones.setNight(look.night);
    if (fx && fx.setTimeOfDay) fx.setTimeOfDay(todPreset(hourCache), look.night, look.fire);
  }
  function drawCountdown() { env.drawPlate(opts.getCountdown ? opts.getCountdown() : ''); }

  // Default frame: the bounding box of the locations plus a margin, so it follows the data
  // when Funcom adds places. The rest of the overland stays reachable by pan and zoom.
  function framing() {
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    world.stones.forEach((s) => { x0 = Math.min(x0, s.x * CM); x1 = Math.max(x1, s.x * CM); z0 = Math.min(z0, s.y * CM); z1 = Math.max(z1, s.y * CM); });
    const nn = tcam.normalized((x0 + x1) / 2, (z0 + z1) / 2);
    const ext = Math.max((x1 - x0) * 1.15, (z1 - z0)) * MARGIN;
    return { nx: nn.nx, ny: nn.ny, zoom: Math.max(1, Math.min(3, (2 * R) / ext)) };
  }
  // ---- camera driving ---------------------------------------------------------------------
  function sizeView() { view.display = Math.min(host.clientWidth || 1, host.clientHeight || 1); }
  function apply() { sizeView(); tcam.setView(view); dirty = true; syncFx(); }
  function centre() {
    const d = view.display || 1;
    return { nx: ((0.5 - view.panX / d) / view.zoom) * 1000, ny: ((0.5 - view.panY / d) / view.zoom) * 1000 };
  }
  let dolly = 1;
  function tween(to, ms, done) {
    sizeView();
    const c = centre();
    anim.cur = {
      t0: performance.now(), dur: reduced ? 0 : ms, done,
      from: { az: view.azimuth, tilt: view.tilt, zoom: view.zoom, nx: c.nx, ny: c.ny, dolly },
      to,
    };
    if (reduced) stepAnim(performance.now() + 1);
  }
  function stepAnim(now) {
    const a = anim.cur; if (!a) return;
    const t = a.dur ? Math.min(1, (now - a.t0) / a.dur) : 1, e = ease(t);
    const f = a.from, to = a.to;
    let da = (to.az != null ? to.az : f.az) - f.az;
    da = ((da + 540) % 360) - 180;
    view.azimuth = (f.az + da * e + 360) % 360;
    view.tilt = f.tilt + ((to.tilt != null ? to.tilt : f.tilt) - f.tilt) * e;
    view.zoom = f.zoom + ((to.zoom != null ? to.zoom : f.zoom) - f.zoom) * e;
    dolly = f.dolly + ((to.dolly != null ? to.dolly : f.dolly) - f.dolly) * e;
    tcam.setDolly(dolly);
    sizeView();
    panToNormalized(view, f.nx + ((to.nx != null ? to.nx : f.nx) - f.nx) * e, f.ny + ((to.ny != null ? to.ny : f.ny) - f.ny) * e);
    apply();
    if (t >= 1) { anim.cur = null; if (a.done) a.done(); }
  }

  const compass = createCompass(host, {
    onStep: (dir) => tween({ az: stepHeading(view.azimuth, dir) }, 420),
    onReset: () => tween({ az: 0, tilt: DEFAULT_ORBIT.tilt, ...framing(), dolly: 1 }, 600),
  });
  compass.update(0);

  let hoverRay = null;
  const detach = attachGestures(host, view, {
    onTransform: () => { anim.cur = null; apply(); compass.update(view.azimuth); },
    onInteract: () => { anim.cur = null; },
    onHover: (mx, my) => pickAt(mx, my, false),
    onLeave: () => setHover(null),
    onClick: (mx, my) => { if (moved) return; const id = pick(mx, my); if (id) opts.onActivate && opts.onActivate(id); },
    onStep: (dir) => tween({ az: stepHeading(view.azimuth, dir) }, 420),
  }, { rotate: true });

  let downAt = null, moved = false;
  host.addEventListener('pointerdown', (e) => { downAt = { x: e.clientX, y: e.clientY }; moved = false; }, true);
  host.addEventListener('pointermove', (e) => { if (downAt && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > 6) moved = true; }, true);
  host.addEventListener('pointerup', () => { downAt = null; }, true);
  host.addEventListener('contextmenu', (e) => e.preventDefault());

  function pick(mx, my) {
    if (!stones) return null;
    hoverRay = tcam.ndcRay(mx, my);
    const hits = hoverRay.intersectObjects(stones.hitMeshes, false);
    return hits.length ? hits[0].object.userData.id : null;
  }
  function anchorOf(id) {
    const e = stones.byId.get(id);
    const p = tcam.project(e.cxu, e.plateY + 0.35, e.czu);
    return { x: p.x, y: p.y };
  }
  function setHover(id) {
    if (id === hoverId) return;
    hoverId = id;
    stones && stones.setHover(id);
    host.style.cursor = id && stones.byId.get(id).s.live ? 'pointer' : id ? 'help' : '';
    const sel = id || selectedId;
    setFocus(sel);
    opts.onHover && opts.onHover(id, id ? anchorOf(id) : null);
  }
  function pickAt(mx, my) { setHover(pick(mx, my)); }
  function setFocus(id) {
    if (id && stones) { const e = stones.byId.get(id); U.uFocus.value.set(e.cxu, e.czu, Math.max(1.4, e.r * 2.6), 1); }
    else U.uFocus.value.w = 0;
  }

  // ---- public controller --------------------------------------------------------------------
  const first = performance.now();
  const frame = (sec) => {
    if (!stones) return;
    const now = performance.now();
    if (anim.cur) stepAnim(now);
    stones.tick(sec, reduced);
    env.tick(sec, reduced);
    fx.tick(sec);
    if (sec - lastEnvTick > 20) { lastEnvTick = sec; applyHour(); drawCountdown(); }
    updateLabels();
    if (hoverId) opts.onHover && opts.onHover(hoverId, anchorOf(hoverId));
    void first; void dirty;
  };
  const started = ready.then(() => stage.start(frame));

  ctl = {
    ready: ready.then(() => started),
    setStatus(map) { stones && stones.setStatus(map); opts.initial = Object.assign(opts.initial || {}, { status: map }); },
    setBands(map) { stones && stones.setBands(map); opts.initial = Object.assign(opts.initial || {}, { bands: map }); },
    setAnchor(id) { stones && stones.setAnchor(id); opts.initial = Object.assign(opts.initial || {}, { anchor: id }); },
    setSelected(id, fly) {
      selectedId = id;
      stones && stones.setSelected(id);
      setFocus(id || hoverId);
      if (id && fly && stones) {
        const e = stones.byId.get(id);
        const nn = tcam.normalized(e.cxu, e.czu);
        tween({ nx: nn.nx, ny: nn.ny, zoom: Math.max(view.zoom, 2.2) }, 520);
      }
    },
    lookAtStone(id, zoom, tilt) { const e = stones && stones.byId.get(id); if (e) ctl.lookAtCm(e.s.x, e.s.y, zoom, tilt); },
    focusPoint(x, z, zoom, tilt) { const nn = tcam.normalized(x, z); tween({ nx: nn.nx, ny: nn.ny, zoom: zoom || 6, tilt: tilt || 30 }, 700); },
    setStorm(st, id = 'deep-desert') { if (fx && fx.setStorm) fx.setStorm(st || null, id); (opts.initialStorms ||= {})[id] = st || null; },
    refreshLight: () => applyHour(),
    // jump the camera to an overmap point (cm): QA and deep links
    lookAtCm(xcm, ycm, zoom, tilt) { anim.cur = null; setOrbit(view, 0, tilt || DEFAULT_ORBIT.tilt); view.zoom = zoom || 2; const nn = tcam.normalized(xcm * CM, ycm * CM); panToNormalized(view, nn.nx, nn.ny); apply(); compass.update(0); },
    anchorOf: (id) => (stones && stones.byId.has(id) ? anchorOf(id) : null),
    // screen position of the disc's own centre (what a pointer aimed at the stone must hit)
    plateScreen: (id) => { const e = stones && stones.byId.get(id); if (!e) return null; const p = tcam.project(e.cxu, e.plateY, e.czu); return { x: p.x, y: p.y }; },
    screenOf: (id) => (stones && stones.byId.has(id) ? anchorOf(id) : null),
    // camera descends onto the stone, then done() (the caller navigates)
    descend(id, done) {
      const e = stones && stones.byId.get(id);
      if (!e) { done && done(); return; }
      const nn = tcam.normalized(e.cxu, e.czu);
      tween({ nx: nn.nx, ny: nn.ny, zoom: 6, tilt: 30 }, 950, done);
    },
    overview() { tween({ az: 0, tilt: DEFAULT_ORBIT.tilt, ...framing(), dolly: 1 }, 700); },
    arrive() {
      setOrbit(view, -28, 58); view.zoom = 1; dolly = 1.55; tcam.setDolly(dolly); panToNormalized(view, 500, 500); apply();
      tween({ az: 0, tilt: DEFAULT_ORBIT.tilt, ...framing(), dolly: 1 }, 2600);
    },
    setOrbitNow(az, tilt, zoom) { anim.cur = null; setOrbit(view, az, tilt); if (zoom) view.zoom = zoom; const f = framing(); panToNormalized(view, f.nx, f.ny); apply(); compass.update(view.azimuth); },
    fxInfo: () => (fx && fx.info ? fx.info() : null),
    stats: () => (terrain ? terrain.userData.stats : null),
    resize() { stage.resize(); apply(); },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      detach(); compass.destroy();
      stage.dispose();
      [terrain, bed, contourLines].forEach((o) => { if (o) { o.geometry.dispose(); o.material.dispose(); } });
      stones && stones.dispose(); fx && fx.dispose(); env.dispose();
      artTex && artTex.dispose(); envTex && envTex.dispose(); gl.dispose();
      if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
      if (labelLayer.parentNode) labelLayer.parentNode.removeChild(labelLayer);
    },
  };
  host.__sandtable = ctl;
  apply();
  void ORBIT_DEFAULT;
  return ctl;
}

export default createSandTable;
