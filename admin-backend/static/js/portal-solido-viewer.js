/* portal-solido-viewer.js — three.js blueprint viewer (ES module).
 *
 * Dynamic-import()ed ONLY when the user opens the 3D view, so three.js never
 * loads on any other portal page.
 *
 * Renders a Dune base blueprint as either:
 *   - Box massing (original behaviour, always available)
 *   - Real GLB meshes, GPU-instanced per piece type, with box fallback per
 *     unmatched piece. Real mode has a Clay material (default, icehunter-style
 *     studio look) and a Textured material (the GLB's own baked PBR maps).
 *
 * GLB loading strategy:
 *   - Fetches /admin/static/data/glb-manifest.json once; memoised in module.
 *   - Per unique piece_id with a GLB: one GLTFLoader.load(), processed once into
 *     world-baked geometry + a merged clay geometry (cached per URL).
 *   - All placements of a piece type render as ONE InstancedMesh (× sub-mesh in
 *     textured mode), so a 2500-piece base is a few dozen draw calls — no piece-
 *     count cap; only a mobile perf warning above REAL_MESH_WARN.
 *   - GLB meshes are authored in metres at the real pivot, so instances render at
 *     scale 1 at the placement position (consistent with the box positions).
 *   - Skeletal meshes (SkinnedMesh) are rendered at bind pose — no animation.
 *   - Click a piece to select it (raycast → placement); breakdown by category.
 *
 * Coordinate model: UE left-handed Z-up → three.js right-handed Y-up.
 *   three.pos = (-UE.y, UE.z, UE.x)
 *   Yaw about UE-Z → three-Y, sign calibrated below.
 *
 * Contract:  mount(canvasEl, blueprintData, catalog, opts?) → handle
 *   opts.onSelect(info|null) — called on piece click with {building_type,
 *                              category, rotation, x, y, z} or null on deselect.
 *   opts.onWalkState(bool)   — walkthrough entered/exited (drive the HUD).
 *   opts.onWalkRequestFs()   — user pressed F during a walk (toggle fullscreen).
 *   handle: { dispose, switchMode('boxes'|'real', manifest), setTextured(bool),
 *             startWalk(), stopWalk(), walkActive(), setWalkJoystick(x, y),
 *             isTouch, getManifest(), getBreakdown(), clearSelection(), stats }
 */
import * as THREE from './vendor/three/three.module.min.js';
import { OrbitControls } from './vendor/three/OrbitControls.js';
import { GLTFLoader } from './vendor/three/GLTFLoader.js';
import { mergeGeometries } from './vendor/three/BufferGeometryUtils.js';

const SCALE = 1 / 100;           // UE cm → viewer metres (positions). GLB meshes
                                 // are already exported in metres, so instanced
                                 // pieces render at scale 1 in the same space.
const YAW_SIGN = -1;              // calibrated UE→three yaw sign
const PIECE_CAP = 8000;           // hard cap on rendered pieces (box mode)
// Real meshes are GPU-instanced (one InstancedMesh per piece type), so piece
// COUNT is cheap — only the number of unique types matters. We warn on mobile
// for very large bases but no longer fall back to boxes by piece count.
const REAL_MESH_WARN = 4000;
const DEG = Math.PI / 180;

// Shared clay material (icehunter-style studio look). One instance, reused
// across every piece in clay mode so the whole base is a handful of draw calls.
// Light NEUTRAL grey — the studio rig below is white/neutral; any warm tint in
// lights or env shows up directly on this surface (the v1 warm rig read orange).
const CLAY_MATERIAL = new THREE.MeshStandardMaterial({
  color: 0xd6d7d9, roughness: 0.85, metalness: 0.0,
});
const _UP = new THREE.Vector3(0, 1, 0);
const _qMesh = new THREE.Quaternion();

// Placement orientation. Instances carry yaw only; placeables carry a full UE
// FRotator stored as [rx, ry, rz] = (Pitch, Yaw, Roll) in degrees — wall-mounted
// pieces (banners, signs, lights) need the pitch/roll terms, not just yaw.
//
// Rather than hand-compose per-axis signs (error-prone — UE pitch is inverted,
// the world swap is a reflection), build UE's EXACT FRotator quaternion and map
// it through the same coordinate reflection P the positions use
// (three.pos = (-UE.y, UE.z, UE.x), det P = -1). For a reflection,
// conjugation sends R(n,θ) → R(P·n, -θ); on the quaternion that is
//   q_three = ( q_ue.y, -q_ue.z, -q_ue.x, q_ue.w ).
// This reproduces the proven yaw-only path exactly (yaw → -yaw about three-Y =
// YAW_SIGN -1) and is correct for pitch+roll by construction.
function ueRotatorToThreeQuat(pitchDeg, yawDeg, rollDeg, out) {
  const hp = pitchDeg * DEG * 0.5, hy = yawDeg * DEG * 0.5, hr = rollDeg * DEG * 0.5;
  const sp = Math.sin(hp), cp = Math.cos(hp);
  const sy = Math.sin(hy), cy = Math.cos(hy);
  const sr = Math.sin(hr), cr = Math.cos(hr);
  // UE FRotator::Quaternion() (verbatim from Unreal source), UE LH space:
  const ux =  cr * sp * sy - sr * cp * cy;
  const uy = -cr * sp * cy - sr * cp * sy;
  const uz =  cr * cp * sy - sr * sp * cy;
  const uw =  cr * cp * cy + sr * sp * sy;
  return out.set(uy, -uz, -ux, uw);   // reflect into three-space
}

// baseYaw = GLB mesh-basis yaw (exporter axis-swap correction), applied innermost
// in three-space (mesh local → world). 0 for box mode (boxes have no mesh basis).
function placementQuat(pc, baseYaw) {
  const q = ueRotatorToThreeQuat(pc.rx || 0, pc.yaw || 0, pc.rz || 0,
                                 new THREE.Quaternion());
  if (baseYaw) q.multiply(_qMesh.setFromAxisAngle(_UP, baseYaw));
  return q;
}

// Module-level caches so repeated mount/unmount on the same page reuses work.
let _manifestPromise = null;
const _glbCache = new Map();      // url → Promise<{parts, clayGeometry}>
let _tintsPromise = null;         // material.name → [r,g,b] linear (textured mode)
let _offsetsPromise = null;       // building_type → baked component transform

function lc(s) { return (s || '').toLowerCase(); }

// ---- material tints (textured mode) -----------------------------------------
// The baked GLB albedos for ML_* layered materials are the engine's dev checker
// card (flat grey) — Funcom's real surface color lives in the material-instance
// layer stack, resolved offline into material-tints.json (linear RGB keyed by
// GLB material.name; see docs/dune-research/PIECE-TINT-EXTRACTION-2026-06-12.md).
// Fetched lazily on the first textured render only.
function fetchTints() {
  if (!_tintsPromise) {
    _tintsPromise = fetch('/admin/static/data/material-tints.json',
      { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return (d && d.materials) || null; })
      .catch(function () { return null; });
  }
  return _tintsPromise;
}

// Placeable actor blueprints bake a transform into their mesh COMPONENT
// (RelativeRotation/Location/Scale) which the game composes at render time —
// wall banners/art carry Yaw -90 + a push-off-the-wall, couches lateral
// offsets, corner pieces Yaw -45. Without it pieces render turned 90°,
// floating, or stacked. Resolved offline from the BP CDOs; see
// docs/dune-research/PLACEABLE-OFFSETS-2026-06-12.md.
function fetchOffsets() {
  if (!_offsetsPromise) {
    _offsetsPromise = fetch('/admin/static/data/placeable-mesh-offsets.json',
      { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return (d && d.pieces) || null; })
      .catch(function () { return null; });
  }
  return _offsetsPromise;
}

// Mutates a GLB material in place, once (materials are shared via _glbCache).
// Entry shape (v2 sidecar): { t: [r,g,b] base tint, e: [r,g,b] emissive }.
// ML_*: the baked albedo IS the grey dev card — drop it and shade with the
// resolved tint (normal/roughness maps stay). Others: only color untextured
// materials; a real baked texture already carries its own color. Emissive
// makes lamps glow and gives railings their blue accent strips back.
function tintMaterial(mat, tints) {
  if (!mat || mat.__lsTinted !== undefined) return;
  const entry = tints && tints[mat.name];
  if (!entry) { mat.__lsTinted = false; return; }
  mat.__lsTinted = true;
  const t = entry.t;
  if (t) {
    if (/^ML_/i.test(mat.name)) {
      mat.map = null;
      mat.color.setRGB(t[0], t[1], t[2]);
    } else if (!mat.map) {
      mat.color.setRGB(t[0], t[1], t[2]);
    }
  }
  const e = entry.e;
  if (e && mat.emissive) {
    mat.emissive.setRGB(e[0], e[1], e[2]);
    mat.emissiveIntensity = 0.85;
  }
  mat.needsUpdate = true;
}

// ---- manifest ---------------------------------------------------------------
function fetchManifest() {
  if (!_manifestPromise) {
    _manifestPromise = fetch('/admin/static/data/glb-manifest.json',
      { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  return _manifestPromise;
}

// ---- piece resolver (box dims + faction colour) ----------------------------
function buildResolver(catalog) {
  const grid = catalog.grid || { foundation_size: 512, floor_height: 384 };
  const FOUND = grid.foundation_size || 512;
  const FLOOR = grid.floor_height || 384;
  const patterns = catalog.patterns || [];
  const categories = catalog.categories || {};
  const factions = catalog.factions || {};
  const defCat = categories.default || { size: { x: 0.6, y: 0.6, z: 0.6 }, shade: 0.9 };
  const defColor = (factions.neutral && factions.neutral.color) || '#9c8a68';

  function categoryOf(bt) {
    const name = lc(bt);
    for (const [needle, cat] of patterns) {
      if (name.includes(needle)) return categories[cat] ? cat : 'default';
    }
    return 'default';
  }
  function factionColorOf(bt) {
    const name = lc(bt);
    for (const key of Object.keys(factions)) {
      const f = factions[key];
      for (const p of (f.prefixes || [])) {
        if (name.startsWith(p)) return f.color;
      }
    }
    return defColor;
  }
  function shadeColor(hex, mult) {
    const c = new THREE.Color(hex);
    c.multiplyScalar(mult);
    c.r = Math.min(1, c.r); c.g = Math.min(1, c.g); c.b = Math.min(1, c.b);
    return c;
  }

  const cache = new Map();
  function resolve(bt) {
    let hit = cache.get(bt);
    if (hit) return hit;
    const catKey = categoryOf(bt);
    const cat = categories[catKey] || defCat;
    const size = cat.size || defCat.size;
    const dims = {
      w: Math.max(0.02, (size.y * FOUND) * SCALE),
      h: Math.max(0.02, (size.z * FLOOR) * SCALE),
      d: Math.max(0.02, (size.x * FOUND) * SCALE),
    };
    const color = shadeColor(factionColorOf(bt), cat.shade || 1);
    hit = { dims, colorKey: color.getHexString(), color };
    cache.set(bt, hit);
    return hit;
  }
  resolve.categoryOf = categoryOf;   // exposed for the piece-breakdown panel
  return resolve;
}

// Human-friendly labels for the catalog category keys (breakdown panel).
const CATEGORY_LABELS = {
  foundation: 'Foundation', foundation_wedge: 'Foundation', floor: 'Floor',
  wall: 'Wall', wall_half: 'Wall', door: 'Door', roof: 'Rooftop', ramp: 'Ramp',
  stairs: 'Stairs', railing: 'Railing', pillar: 'Pillar', corner: 'Corner',
  prop: 'Decoration', prop_large: 'Decoration', default: 'Other',
};

// ---- GLB loading + processing ----------------------------------------------
// Loads a piece GLB once and pre-processes it for instancing:
//   - parts:        [{ geometry (world-baked), material }] preserving the GLB's
//                   own PBR materials (used by Textured mode).
//   - clayGeometry: all parts merged into one geometry for the single-material
//                   clay InstancedMesh (used by Clay mode). null if merge fails.
// The GLB meshes are authored in metres at the piece's real pivot, so we bake
// each node's world matrix into the geometry and render instances at scale 1.
// Cached per URL; subsequent mounts reuse the processed result.
function loadGlbParts(url) {
  if (_glbCache.has(url)) return _glbCache.get(url);
  const p = new Promise(function (resolve, reject) {
    new GLTFLoader().load(url, function (gltf) {
      const root = gltf.scene;
      root.updateMatrixWorld(true);
      const parts = [];
      root.traverse(function (node) {
        if ((node.isMesh || node.isSkinnedMesh) && node.geometry) {
          const geom = node.geometry.clone();
          geom.applyMatrix4(node.matrixWorld);     // bake transform → world space
          // Instancing needs a plain (non-interleaved, no-group) geometry per
          // material; GLTFLoader meshes are already one-material-per-mesh.
          geom.clearGroups();
          // UE bakes utility masks (not display colors) into vertex colors;
          // the exported COLOR_0 is near-black, and GLTFLoader multiplies the
          // albedo by it → everything renders black in textured mode. Strip it.
          if (geom.getAttribute('color')) geom.deleteAttribute('color');
          const mat = Array.isArray(node.material) ? node.material[0] : node.material;
          if (mat && mat.vertexColors) { mat.vertexColors = false; mat.needsUpdate = true; }
          parts.push({ geometry: geom, material: mat });
        }
      });
      let clayGeometry = null;
      if (parts.length === 1) {
        clayGeometry = parts[0].geometry;
      } else if (parts.length > 1) {
        try {
          clayGeometry = mergeGeometries(parts.map(function (p) { return p.geometry; }), false);
        } catch (e) { clayGeometry = null; }   // attribute mismatch → per-part clay
      }
      resolve({ parts: parts, clayGeometry: clayGeometry });
    }, undefined, function (err) { reject(err); });
  });
  _glbCache.set(url, p);
  return p;
}

// ---- environment map --------------------------------------------------------
// Real GLB pieces use MeshStandardMaterial (metal/rough PBR). Without an
// environment to reflect they render flat and dead. We synthesise a cheap
// sky/ground gradient equirect, run it through PMREM, and use it as the scene
// environment so every piece gets soft image-based ambient (the single biggest
// "looks 3D, not flat" win). Built once per renderer; cached on the renderer.
function makeEnvironment(renderer) {
  if (renderer.__envMap) return renderer.__envMap;
  const W = 16, H = 64;
  const data = new Uint8Array(W * H * 4);
  // NEUTRAL studio gradient (white-grey dome → grey horizon → dark floor).
  // Must stay colourless: the clay material shows any env tint directly.
  const top = [0.62, 0.63, 0.66], mid = [0.38, 0.39, 0.41], bot = [0.10, 0.10, 0.11];
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

// ---- scene setup (shared between modes) -------------------------------------
// NEUTRAL white studio rig (icehunter look): high white ambient so interior /
// away-facing faces never crush to black, white key for form, cool-grey bg.
// Colour comes from the textured GLBs in Textured mode, never from the lights.
function buildScene(center, bbox, sizeVec) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#0d0f12');

  const radius = Math.max(sizeVec.x, sizeVec.y, sizeVec.z, 1);

  scene.add(new THREE.HemisphereLight('#ffffff', '#54565a', 1.15));
  const key = new THREE.DirectionalLight('#ffffff', 1.1);
  key.position.set(radius, radius * 2, radius * 0.6);
  scene.add(key);
  // Fill from the opposite side so unlit faces stay readable.
  const fill = new THREE.DirectionalLight('#dfe4ea', 0.45);
  fill.position.set(-radius, radius * 0.8, -radius * 0.6);
  scene.add(fill);
  // Low rim/back light to separate silhouettes from the dark background.
  const rim = new THREE.DirectionalLight('#ffffff', 0.3);
  rim.position.set(-radius * 0.4, radius * 0.5, radius * 1.5);
  scene.add(rim);

  const gridSpan = Math.ceil(Math.max(sizeVec.x, sizeVec.z) * 1.4) || 20;
  const grid = new THREE.GridHelper(
    gridSpan,
    Math.min(80, Math.max(8, Math.round(gridSpan / 5))),
    '#2e3136', '#1c1e22');
  grid.position.set(center.x, bbox.isEmpty() ? 0 : bbox.min.y, center.z);
  scene.add(grid);

  return { scene, radius };
}

// ---- box rendering (original, always available) ----------------------------
function renderBoxes(scene, used, resolve) {
  const buckets = new Map();
  const s = new THREE.Vector3();

  for (const pc of used) {
    const r = resolve(pc.bt);
    const px = -(pc.y) * SCALE;
    const py = (pc.z) * SCALE;
    const pz = (pc.x) * SCALE;
    if (!isFinite(px) || !isFinite(py) || !isFinite(pz)) continue;
    let bk = buckets.get(r.colorKey);
    if (!bk) { bk = { color: r.color, mats: [] }; buckets.set(r.colorKey, bk); }
    s.set(r.dims.w, r.dims.h, r.dims.d);
    bk.mats.push(new THREE.Matrix4().compose(
      new THREE.Vector3(px, py, pz), placementQuat(pc, 0), s));
  }

  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  const meshes = [];
  const materials = [];
  for (const bk of buckets.values()) {
    const mat = new THREE.MeshLambertMaterial({ color: bk.color });
    materials.push(mat);
    const im = new THREE.InstancedMesh(unitBox, mat, bk.mats.length);
    bk.mats.forEach(function (m, i) { im.setMatrixAt(i, m); });
    im.instanceMatrix.needsUpdate = true;
    scene.add(im);
    meshes.push(im);
  }

  return {
    dispose: function () {
      // Remove from the scene graph too — dispose() alone frees GPU buffers
      // but leaves the meshes rendering (the "boxes overlay the real meshes
      // after toggling" bug).
      meshes.forEach(function (m) { scene.remove(m); m.dispose && m.dispose(); });
      materials.forEach(function (m) { m.dispose(); });
      unitBox.dispose();
    },
    color_buckets: buckets.size,
  };
}

// ---- manifest piece resolution (exact + normalized + alias) -----------------
// Blueprint building_type strings mostly match manifest keys exactly, but real
// player blueprints throw near-misses (case drift, trailing variant suffixes
// like _01/_A, Door vs Door_Frame). We index the manifest once and try, in
// order: exact → case-insensitive → suffix-stripped → "contains" stem match.
let _pieceIndex = null;
function buildPieceIndex(piecesMap) {
  if (_pieceIndex && _pieceIndex.src === piecesMap) return _pieceIndex;
  const ci = new Map();        // lowercased key → canonical key
  const stem = new Map();      // normalized stem → canonical key (first wins)
  function norm(s) {
    return (s || '').toLowerCase()
      .replace(/_(0?\d|[a-f]|frame|var\d*|variant\d*)$/i, '')
      .replace(/[^a-z0-9]/g, '');
  }
  for (const k of Object.keys(piecesMap)) {
    if (!piecesMap[k] || !piecesMap[k].file) continue;
    ci.set(k.toLowerCase(), k);
    const n = norm(k);
    if (n && !stem.has(n)) stem.set(n, k);
  }
  _pieceIndex = { src: piecesMap, ci, stem, norm };
  return _pieceIndex;
}
function resolvePieceEntry(piecesMap, bt) {
  let e = piecesMap[bt];
  if (e && e.file) return e;
  const idx = buildPieceIndex(piecesMap);
  let k = idx.ci.get((bt || '').toLowerCase());
  if (k) return piecesMap[k];
  k = idx.stem.get(idx.norm(bt));
  if (k) return piecesMap[k];
  return null;
}

// ---- GLB rendering (real mesh mode, GPU-instanced) -------------------------
// One InstancedMesh per piece type (× sub-mesh in textured mode), so a
// 2500-piece base is a few dozen draw calls and renders smoothly. Pieces
// without a GLB entry fall back to box massing. `textured` picks the GLB's own
// PBR materials; otherwise the shared clay material is used.
// Returns { dispose, info, pick } where pick.intersect(raycaster) → placement.
function renderGlbs(scene, used, resolve, manifest, textured, tints, offsets) {
  const piecesMap = (manifest && manifest.pieces) || {};
  const base = '/admin/static/glb/pieces/';
  const ONE = new THREE.Vector3(1, 1, 1);

  // Group placements by piece type so each GLB loads exactly once.
  const byId = new Map();
  for (const pc of used) {
    let arr = byId.get(pc.bt);
    if (!arr) { arr = []; byId.set(pc.bt, arr); }
    arr.push(pc);
  }

  const boxFallback = [];        // placements with no GLB → boxes
  const unmatchedTypes = [];     // distinct types with no GLB (diagnostic)
  const loadFailures = [];       // distinct types whose GLB failed to load
  const instancedMeshes = [];    // every InstancedMesh added (dispose + raycast)
  const pickMap = new Map();     // InstancedMesh → ordered placements[]
  let placedInstances = 0;       // distinct real-mesh placements (not ×sub-mesh)

  const glbTasks = [];
  for (const [bt, pcs] of byId) {
    const entry = resolvePieceEntry(piecesMap, bt);
    if (entry && entry.file) glbTasks.push({ bt: bt, pcs: pcs, url: base + entry.file });
    else { unmatchedTypes.push(bt); for (const pc of pcs) boxFallback.push(pc); }
  }

  // GLB meshes are exported UE→glTF as (x, z, y) axis swap, but the viewer's
  // world permutation is (-y, z, x). Net effect: every mesh needs a constant
  // -90° yaw pre-rotation under the placement yaw, or pieces render turned 90°
  // (walls perpendicular to their wall line, roof wedges sloping tangentially).
  // If a future export changes axes and everything faces backwards, flip to
  // +Math.PI / 2.
  const MESH_BASE_YAW = -Math.PI / 2;

  function placeMatrix(pc) {
    const px = -(pc.y) * SCALE, py = (pc.z) * SCALE, pz = (pc.x) * SCALE;
    if (!isFinite(px) || !isFinite(py) || !isFinite(pz)) return null;
    const pos = new THREE.Vector3(px, py, pz);
    // world = actor ∘ component ∘ mesh-basis. The component transform is the
    // actor BP's baked mesh offset (placeable decor); identity when absent.
    const q = placementQuat(pc, 0);
    let scl = ONE;
    const off = offsets && offsets[pc.bt];
    if (off) {
      if (off.loc) {
        // UE cm → three metres under the same (-y, z, x) reflection as positions,
        // rotated into world by the actor orientation.
        const lo = new THREE.Vector3(-off.loc[1] * SCALE, off.loc[2] * SCALE, off.loc[0] * SCALE);
        lo.applyQuaternion(q);
        pos.add(lo);
      }
      if (off.rot && (off.rot[0] || off.rot[1] || off.rot[2])) {
        q.multiply(ueRotatorToThreeQuat(off.rot[0], off.rot[1], off.rot[2],
                                        new THREE.Quaternion()));
      }
      if (off.scale) {
        scl = new THREE.Vector3(off.scale[1], off.scale[2], off.scale[0]);
      }
    }
    q.multiply(_qMesh.setFromAxisAngle(_UP, MESH_BASE_YAW));
    return new THREE.Matrix4().compose(pos, q, scl);
  }

  const loadPromises = glbTasks.map(function (task) {
    return loadGlbParts(task.url).then(function (processed) {
      // Build ordered placements + matrices (instanceId i ↔ placements[i]).
      const placements = [], matrices = [];
      for (const pc of task.pcs) {
        const m = placeMatrix(pc);
        if (m) { placements.push(pc); matrices.push(m); }
      }
      if (!placements.length) return;

      // Choose the geometry/material primitives for this mode.
      let prims;
      if (textured && processed.parts.length) {
        prims = processed.parts.map(function (p) {
          tintMaterial(p.material, tints);
          return { geometry: p.geometry, material: p.material };
        });
      } else if (processed.clayGeometry) {
        prims = [{ geometry: processed.clayGeometry, material: CLAY_MATERIAL }];
      } else {
        prims = processed.parts.map(function (p) { return { geometry: p.geometry, material: CLAY_MATERIAL }; });
      }
      if (!prims.length) { for (const pc of task.pcs) boxFallback.push(pc); return; }
      placedInstances += placements.length;

      prims.forEach(function (pr) {
        const im = new THREE.InstancedMesh(pr.geometry, pr.material, placements.length);
        // Instance matrices spread pieces far from the base geometry origin, so
        // the auto bounding sphere would mis-cull the whole batch. Disable it.
        im.frustumCulled = false;
        for (let i = 0; i < matrices.length; i++) im.setMatrixAt(i, matrices[i]);
        im.instanceMatrix.needsUpdate = true;
        im.userData.btType = task.bt;
        scene.add(im);
        instancedMeshes.push(im);
        pickMap.set(im, placements);   // every sub-mesh shares the placement order
      });
    }).catch(function (err) {
      loadFailures.push(task.bt);
      if (window.console) console.warn('[solido] GLB load failed:', task.bt, err && err.message);
      for (const pc of task.pcs) boxFallback.push(pc);
    });
  });

  return Promise.all(loadPromises).then(function () {
    // Box-fill everything that had no GLB or failed to load (rendered last so
    // load failures discovered mid-flight are still covered).
    const boxResult = renderBoxes(scene, boxFallback, resolve);
    // Diagnostic for closing coverage gaps: which piece types fell back, and why.
    if (window.console && (unmatchedTypes.length || loadFailures.length)) {
      console.info('[solido] real-mesh fallbacks',
        { no_manifest_entry: unmatchedTypes, glb_load_failed: loadFailures });
    }

    const _ray = new THREE.Vector3();
    const pick = {
      intersect: function (raycaster) {
        if (!instancedMeshes.length) return null;
        const hits = raycaster.intersectObjects(instancedMeshes, false);
        for (const h of hits) {
          const list = pickMap.get(h.object);
          if (list && h.instanceId != null && list[h.instanceId]) {
            return { placement: list[h.instanceId], object: h.object, instanceId: h.instanceId, point: h.point };
          }
        }
        return null;
      },
      meshes: function () { return instancedMeshes; },   // walkthrough collision set
    };

    return {
      dispose: function () {
        boxResult.dispose();
        instancedMeshes.forEach(function (im) { scene.remove(im); im.dispose(); });
        // NOTE: geometries + GLB materials live in _glbCache and are reused by
        // later mode switches / remounts, so we deliberately do NOT dispose them
        // here. The shared CLAY_MATERIAL is module-level and never disposed.
      },
      info: {
        glb_types: glbTasks.length,
        glb_instances: placedInstances,
        box_fallback: boxFallback.length,
        unmatched_types: unmatchedTypes,
      },
      pick: pick,
    };
  });
}

// ---- public mount -----------------------------------------------------------
export function mount(canvasEl, blueprintData, catalog, opts) {
  opts = opts || {};
  const resolve = buildResolver(catalog);
  const instances = blueprintData.instances || [];
  const placeables = blueprintData.placeables || [];
  const pentashields = blueprintData.pentashields || [];

  const placements = [];
  for (const it of instances) {
    placements.push({ bt: it.building_type, x: it.x, y: it.y, z: it.z, yaw: it.rotation || 0 });
  }
  for (const pl of placeables) {
    placements.push({ bt: pl.building_type, x: pl.x, y: pl.y, z: pl.z,
                      yaw: pl.ry || 0, rx: pl.rx || 0, rz: pl.rz || 0 });
  }

  const total = placements.length;
  const capped = total > PIECE_CAP;
  const used = capped ? placements.slice(0, PIECE_CAP) : placements;
  const realMeshWarning = total > REAL_MESH_WARN;

  // ---- piece breakdown by category (for the side panel) ----
  // Counts placements per catalog category, merged by display LABEL (wall +
  // wall_half are both "Wall", etc.), sorted by count desc.
  const _catCounts = new Map();
  for (const pc of placements) {
    const key = resolve.categoryOf ? resolve.categoryOf(pc.bt) : 'default';
    const label = CATEGORY_LABELS[key] || key;
    _catCounts.set(label, (_catCounts.get(label) || 0) + 1);
  }
  const breakdown = Array.from(_catCounts.entries())
    .map(function (e) { return { label: e[0], count: e[1] }; })
    .sort(function (a, b) { return b.count - a.count; });

  // Pre-compute bounding box from positions (same for both modes).
  const bbox = new THREE.Box3().makeEmpty();
  for (const pc of used) {
    const px = -(pc.y) * SCALE;
    const py = (pc.z) * SCALE;
    const pz = (pc.x) * SCALE;
    if (isFinite(px) && isFinite(py) && isFinite(pz)) {
      bbox.expandByPoint(new THREE.Vector3(px, py, pz));
    }
  }
  const center = new THREE.Vector3();
  const sizeVec = new THREE.Vector3();
  if (!bbox.isEmpty()) { bbox.getCenter(center); bbox.getSize(sizeVec); }
  else { sizeVec.set(10, 10, 10); }

  const { scene, radius } = buildScene(center, bbox, sizeVec);

  const renderer = new THREE.WebGLRenderer({ canvas: canvasEl, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  // Filmic tone mapping + sRGB output so the baked PBR textures read with real
  // contrast and depth instead of the washed-out / flat look of raw linear.
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  // Image-based ambient for the real-mesh PBR materials.
  scene.environment = makeEnvironment(renderer);

  const camera = new THREE.PerspectiveCamera(55, 1, 0.1, radius * 50);
  camera.position.set(
    center.x + radius * 1.1,
    center.y + radius * 0.9,
    center.z + radius * 1.1);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.target.copy(center);
  controls.maxDistance = radius * 12;
  controls.update();

  function resize() {
    const w = canvasEl.clientWidth || 640;
    const h = canvasEl.clientHeight || 420;
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
    if (walk) walkUpdate(); else controls.update();
    renderer.render(scene, camera);
    raf = requestAnimationFrame(tick);
  }

  // Track current content render so we can swap modes.
  let contentDispose = null;
  let currentMode = 'boxes';
  let textured = false;          // clay (false) vs textured (true) in real mode
  let currentPick = null;        // pick interface from the active real render
  let lastManifest = null;       // remembered for clay/textured re-render

  // ---- walkthrough (first-person base tour) ---------------------------------
  // Real-mesh mode only: the instanced meshes double as the collision set.
  // World units are true metres (GLBs authored in metres, positions cm→m), so
  // eye height / speeds are physical. Desktop = pointer lock + WASD; touch =
  // drag-to-look on the canvas + an external joystick fed via setWalkJoystick.
  const WALK_EYE = 1.7, WALK_SPEED = 4.0, WALK_RUN = 7.0, WALK_STEP_MAX = 0.8;
  const IS_TOUCH = ('ontouchstart' in window) ||
    (window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  let walk = null;               // { yaw, pitch, pos, keys, joy, floorTarget, last }
  let _lastHitPoint = null;      // viewer-space point of the last selected piece
  const _savedCam = { pos: new THREE.Vector3(), quat: new THREE.Quaternion(),
                      target: new THREE.Vector3(), fov: camera.fov };
  const _walkRay = new THREE.Raycaster();
  const _DOWN = new THREE.Vector3(0, -1, 0);
  const _walkEuler = new THREE.Euler(0, 0, 0, 'YXZ');

  function walkMeshes() {
    return (currentPick && currentPick.meshes) ? currentPick.meshes() : [];
  }
  function floorYAt(x, z, fromY) {
    _walkRay.set(new THREE.Vector3(x, fromY, z), _DOWN);
    _walkRay.far = 60;
    const hits = _walkRay.intersectObjects(walkMeshes(), false);
    return hits.length ? hits[0].point.y : null;
  }
  function walkBlocked(from, dirX, dirZ, dist) {
    const d = Math.hypot(dirX, dirZ);
    if (d < 1e-6) return false;
    _walkRay.set(from, new THREE.Vector3(dirX / d, 0, dirZ / d));
    _walkRay.far = dist + 0.45;          // body-radius gap from walls
    return _walkRay.intersectObjects(walkMeshes(), false).length > 0;
  }
  function applyWalkCamera() {
    camera.position.copy(walk.pos);
    _walkEuler.set(walk.pitch, walk.yaw, 0);
    camera.quaternion.setFromEuler(_walkEuler);
  }

  function onWalkKey(ev) {
    if (!walk) return;
    const down = ev.type === 'keydown';
    switch (ev.code) {
      case 'KeyW': case 'ArrowUp': walk.keys.f = down; break;
      case 'KeyS': case 'ArrowDown': walk.keys.b = down; break;
      case 'KeyA': case 'ArrowLeft': walk.keys.l = down; break;
      case 'KeyD': case 'ArrowRight': walk.keys.r = down; break;
      case 'ShiftLeft': case 'ShiftRight': walk.keys.run = down; break;
      case 'KeyF':
        if (down && opts.onWalkRequestFs) opts.onWalkRequestFs();
        break;
      case 'Escape': if (down) stopWalk(); return;
      default: return;
    }
    ev.preventDefault();
  }
  function onWalkMouse(ev) {
    if (!walk || document.pointerLockElement !== renderer.domElement) return;
    walk.yaw -= (ev.movementX || 0) * 0.0024;
    walk.pitch = Math.max(-1.45, Math.min(1.45, walk.pitch - (ev.movementY || 0) * 0.0024));
  }
  let _fsChangeAt = 0;
  function onWalkFsChange() { _fsChangeAt = performance.now(); }
  function setWalkPaused(on) {
    if (!walk || walk.paused === on) return;
    walk.paused = on;
    walk.keys = {};
    if (!on) walk.last = performance.now();   // don't integrate the paused gap
    if (opts.onWalkPause) opts.onWalkPause(on);
  }
  function onLockChange() {
    if (!walk || IS_TOUCH) return;
    if (document.pointerLockElement === renderer.domElement) {
      setWalkPaused(false);                   // (re)locked — resume
      return;
    }
    // Fullscreen transitions (F key) drop pointer lock as a side effect; give
    // those a one-shot re-lock instead of treating them as a pause.
    if (performance.now() - _fsChangeAt < 1000) {
      try {
        const p = renderer.domElement.requestPointerLock();
        if (p && p.catch) p.catch(function () { setWalkPaused(true); });
        return;
      } catch (e) { /* fall through to pause */ }
    }
    // Lock lost (Esc, click on another window/monitor, alt-tab): PAUSE, never
    // hard-exit — exiting restores the orbit camera and players lose their spot
    // (the 2026-06-12 "view resets" report). Resume = click the view; exit =
    // Esc again (keydown reaches us while unlocked) or the HUD Exit button.
    setWalkPaused(true);
  }
  function onWalkResumeClick() {
    if (!walk || !walk.paused || IS_TOUCH) return;
    try { renderer.domElement.requestPointerLock(); } catch (e) {}
  }
  let _lookId = null, _lookX = 0, _lookY = 0;
  function onWalkTouchStart(ev) {
    if (!walk || _lookId != null) return;
    const t = ev.changedTouches[0];
    _lookId = t.identifier; _lookX = t.clientX; _lookY = t.clientY;
  }
  function onWalkTouchMove(ev) {
    if (!walk || _lookId == null) return;
    for (let i = 0; i < ev.changedTouches.length; i++) {
      const t = ev.changedTouches[i];
      if (t.identifier !== _lookId) continue;
      walk.yaw -= (t.clientX - _lookX) * 0.005;
      walk.pitch = Math.max(-1.45, Math.min(1.45, walk.pitch - (t.clientY - _lookY) * 0.005));
      _lookX = t.clientX; _lookY = t.clientY;
      ev.preventDefault();
    }
  }
  function onWalkTouchEnd(ev) {
    for (let i = 0; i < ev.changedTouches.length; i++) {
      if (ev.changedTouches[i].identifier === _lookId) _lookId = null;
    }
  }
  function bindWalkInput() {
    window.addEventListener('keydown', onWalkKey);
    window.addEventListener('keyup', onWalkKey);
    document.addEventListener('mousemove', onWalkMouse);
    document.addEventListener('pointerlockchange', onLockChange);
    document.addEventListener('fullscreenchange', onWalkFsChange);
    renderer.domElement.addEventListener('pointerdown', onWalkResumeClick);
    renderer.domElement.addEventListener('touchstart', onWalkTouchStart, { passive: false });
    renderer.domElement.addEventListener('touchmove', onWalkTouchMove, { passive: false });
    renderer.domElement.addEventListener('touchend', onWalkTouchEnd);
  }
  function unbindWalkInput() {
    window.removeEventListener('keydown', onWalkKey);
    window.removeEventListener('keyup', onWalkKey);
    document.removeEventListener('mousemove', onWalkMouse);
    document.removeEventListener('pointerlockchange', onLockChange);
    document.removeEventListener('fullscreenchange', onWalkFsChange);
    renderer.domElement.removeEventListener('pointerdown', onWalkResumeClick);
    renderer.domElement.removeEventListener('touchstart', onWalkTouchStart);
    renderer.domElement.removeEventListener('touchmove', onWalkTouchMove);
    renderer.domElement.removeEventListener('touchend', onWalkTouchEnd);
  }

  function startWalk() {
    if (walk || currentMode !== 'real' || !walkMeshes().length) return false;
    const at = _lastHitPoint || center;
    const fy = floorYAt(at.x, at.z, at.y + 3);
    const eye = new THREE.Vector3(at.x, (fy != null ? fy : at.y) + WALK_EYE, at.z);
    _savedCam.pos.copy(camera.position);
    _savedCam.quat.copy(camera.quaternion);
    _savedCam.target.copy(controls.target);
    _savedCam.fov = camera.fov;
    controls.enabled = false;
    camera.fov = 70; camera.updateProjectionMatrix();
    clearSelection();
    // Face the base centre so the first frame is the structure, not a wall.
    const yaw = Math.atan2(-(center.x - eye.x), -(center.z - eye.z));
    walk = { yaw: yaw, pitch: 0, pos: eye, keys: {}, joy: { x: 0, y: 0 },
             floorTarget: null, paused: false, last: performance.now() };
    applyWalkCamera();
    bindWalkInput();
    if (!IS_TOUCH && renderer.domElement.requestPointerLock) {
      try { renderer.domElement.requestPointerLock(); } catch (e) {}
    }
    if (opts.onWalkState) opts.onWalkState(true);
    return true;
  }
  function stopWalk() {
    if (!walk) return;
    walk = null;
    unbindWalkInput();
    if (document.pointerLockElement === renderer.domElement && document.exitPointerLock) {
      document.exitPointerLock();
    }
    camera.position.copy(_savedCam.pos);
    camera.quaternion.copy(_savedCam.quat);
    camera.fov = _savedCam.fov; camera.updateProjectionMatrix();
    controls.target.copy(_savedCam.target);
    controls.enabled = true;
    controls.update();
    if (opts.onWalkState) opts.onWalkState(false);
  }

  function walkUpdate() {
    const now = performance.now();
    if (walk.paused) { walk.last = now; return; }
    const dt = Math.min(0.05, (now - walk.last) / 1000);
    walk.last = now;
    const k = walk.keys;
    let ix = (k.r ? 1 : 0) - (k.l ? 1 : 0) + walk.joy.x;
    let iz = (k.f ? 1 : 0) - (k.b ? 1 : 0) + walk.joy.y;
    const mag = Math.hypot(ix, iz);
    if (mag > 1e-3) {
      if (mag > 1) { ix /= mag; iz /= mag; }
      const step = (k.run ? WALK_RUN : WALK_SPEED) * dt;
      const sy = Math.sin(walk.yaw), cy = Math.cos(walk.yaw);
      // camera forward = (-sin yaw, 0, -cos yaw); right = (cos yaw, 0, -sin yaw)
      walkMove((-sy * iz + cy * ix) * step, (-cy * iz - sy * ix) * step);
    }
    // Smooth vertical toward the current floor (handles stairs + ramps).
    const p = walk.pos;
    if (walk.floorTarget == null) {
      const fy = floorYAt(p.x, p.z, p.y + 0.4);
      if (fy != null) walk.floorTarget = fy + WALK_EYE;
    }
    if (walk.floorTarget != null) {
      p.y += (walk.floorTarget - p.y) * Math.min(1, dt * 12);
    }
    applyWalkCamera();
  }
  function walkMove(dx, dz) {
    const p = walk.pos;
    const chest = new THREE.Vector3(p.x, p.y - 0.55, p.z);
    function attempt(mx, mz) {
      const d = Math.hypot(mx, mz);
      if (d < 1e-6) return false;
      if (walkBlocked(chest, mx, mz, d)) return false;
      const fy = floorYAt(p.x + mx, p.z + mz, p.y + 0.4);
      if (fy == null) return false;                          // base edge — stay on
      const eyeTarget = fy + WALK_EYE;
      if (eyeTarget - p.y > WALK_STEP_MAX) return false;     // too tall to step up
      p.x += mx; p.z += mz;
      walk.floorTarget = eyeTarget;
      return true;
    }
    // Full move, else slide along the wall axis-by-axis.
    if (!attempt(dx, dz)) { if (!attempt(dx, 0)) attempt(0, dz); }
  }

  tick();   // start the render loop (after walk state exists — tick reads it)

  // Initial box render (synchronous, no loading required).
  const boxResult = renderBoxes(scene, used, resolve);
  contentDispose = boxResult.dispose;

  // ---- selection highlight (yellow wireframe box around the picked piece) ----
  let selectionBox = null;
  const _tmpMat = new THREE.Matrix4();
  function clearSelection() {
    if (selectionBox) { scene.remove(selectionBox); selectionBox = null; }
  }
  function highlightInstance(object, instanceId) {
    clearSelection();
    object.getMatrixAt(instanceId, _tmpMat);
    // Bounding box of the piece geometry, transformed by the instance matrix.
    if (!object.geometry.boundingBox) object.geometry.computeBoundingBox();
    const box = object.geometry.boundingBox.clone().applyMatrix4(_tmpMat);
    const helper = new THREE.Box3Helper(box, new THREE.Color('#ffd24a'));
    if (helper.material) { helper.material.depthTest = false; helper.material.transparent = true; }
    helper.renderOrder = 999;
    scene.add(helper);
    selectionBox = helper;
  }

  // ---- click-to-select (raycast the instanced real meshes) ----
  const _ray = new THREE.Raycaster();
  const _ndc = new THREE.Vector2();
  let _downX = 0, _downY = 0;
  function onPointerDown(ev) { _downX = ev.clientX; _downY = ev.clientY; }
  function onPointerUp(ev) {
    if (walk) return;            // walkthrough owns the pointer
    // Ignore drags (orbit) — only treat near-stationary clicks as a pick.
    if (Math.abs(ev.clientX - _downX) > 4 || Math.abs(ev.clientY - _downY) > 4) return;
    if (currentMode !== 'real' || !currentPick) return;
    const rect = renderer.domElement.getBoundingClientRect();
    _ndc.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    _ndc.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    _ray.setFromCamera(_ndc, camera);
    const hit = currentPick.intersect(_ray);
    if (hit) {
      _lastHitPoint = hit.point.clone();   // walkthrough spawn point
      highlightInstance(hit.object, hit.instanceId);
      if (opts.onSelect) opts.onSelect(describePlacement(hit.placement));
    } else {
      clearSelection();
      if (opts.onSelect) opts.onSelect(null);
    }
  }
  renderer.domElement.addEventListener('pointerdown', onPointerDown);
  renderer.domElement.addEventListener('pointerup', onPointerUp);

  function describePlacement(pc) {
    const r1 = function (n) { return Math.round((n || 0) * 100) / 100; };
    return {
      building_type: pc.bt,
      category: CATEGORY_LABELS[resolve.categoryOf ? resolve.categoryOf(pc.bt) : 'default'] || 'Other',
      rotation: r1(pc.yaw),
      pitch: r1(pc.rx), roll: r1(pc.rz),
      x: pc.x, y: pc.y, z: pc.z,
    };
  }

  // ---- mode toggle ----
  // newMode: 'boxes' | 'real'. In 'real' mode the `textured` flag picks clay or
  // the GLB's own PBR materials. Returns a Promise that resolves when done.
  function renderReal(manifest) {
    // Textured mode needs the tint sidecar first (one cheap fetch, memoized);
    // every real render needs the placeable component offsets.
    const tintsReady = textured ? fetchTints() : Promise.resolve(null);
    return Promise.all([tintsReady, fetchOffsets()]).then(function (res) {
      return renderGlbs(scene, used, resolve, manifest, textured, res[0], res[1]);
    }).then(function (result) {
      contentDispose = result.dispose;
      currentPick = result.pick;
      currentMode = 'real';
      return { mode: 'real', textured: textured, glb_types: result.info.glb_types,
               glb_instances: result.info.glb_instances, box_fallback: result.info.box_fallback,
               unmatched_types: result.info.unmatched_types };
    });
  }
  function switchMode(newMode, manifest) {
    if (manifest) lastManifest = manifest;
    if (newMode === currentMode && newMode !== 'real') {
      return Promise.resolve({ mode: currentMode });
    }
    stopWalk();
    clearSelection();
    if (contentDispose) { contentDispose(); contentDispose = null; }
    currentPick = null;
    if (newMode === 'real') return renderReal(manifest || lastManifest);
    const br = renderBoxes(scene, used, resolve);
    contentDispose = br.dispose;
    currentMode = 'boxes';
    return Promise.resolve({ mode: 'boxes' });
  }

  // Toggle clay/textured; re-renders in place if currently in real mode.
  function setTextured(on) {
    on = !!on;
    if (on === textured) return Promise.resolve({ textured: textured });
    textured = on;
    if (currentMode !== 'real') return Promise.resolve({ textured: textured });
    stopWalk();
    clearSelection();
    if (contentDispose) { contentDispose(); contentDispose = null; }
    currentPick = null;
    return renderReal(lastManifest);
  }

  function dispose() {
    stopWalk();
    running = false;
    if (raf) cancelAnimationFrame(raf);
    if (ro) ro.disconnect(); else window.removeEventListener('resize', resize);
    renderer.domElement.removeEventListener('pointerdown', onPointerDown);
    renderer.domElement.removeEventListener('pointerup', onPointerUp);
    clearSelection();
    controls.dispose();
    if (contentDispose) contentDispose();
    renderer.dispose();
  }

  // Fetch the manifest for callers that want it (the toggle UI needs it).
  const manifestReady = fetchManifest();

  // One-shot PNG capture at the given size (for publish-time market thumbs).
  // Renders explicitly so toDataURL works without preserveDrawingBuffer, then
  // restores the live canvas size.
  function snapshot(w, h) {
    renderer.setSize(w || 800, h || 500, false);
    camera.aspect = (w || 800) / (h || 500);
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);
    const url = renderer.domElement.toDataURL('image/png');
    resize();
    return url;
  }

  return {
    dispose,
    switchMode,
    setTextured,
    snapshot,
    clearSelection,
    startWalk,
    stopWalk,
    resumeWalk: onWalkResumeClick,
    walkActive: function () { return !!walk; },
    setWalkJoystick: function (x, y) { if (walk) { walk.joy.x = x; walk.joy.y = y; } },
    isTouch: IS_TOUCH,
    getManifest: function () { return manifestReady; },
    getBreakdown: function () { return breakdown; },
    realMeshWarning,
    stats: {
      total_pieces: total,
      rendered_pieces: used.length,
      capped,
      color_buckets: boxResult.color_buckets,
      pentashields_skipped: pentashields.length,
      breakdown: breakdown,
    },
  };
}
