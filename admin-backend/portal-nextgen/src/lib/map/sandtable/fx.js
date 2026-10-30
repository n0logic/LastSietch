import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

// Cheap re-creations of the game's per-location overland FX
// (overmap-locations-fx.json recipes): additive point particles with a vertex-shader
// loop, flickering glows, arc flashes, searchlight cones, the pink erythrite glow and
// the travel-pointer lights that flow along the four BP_Pointers splines. One shared
// ShaderMaterial family, no per-frame JS allocation. Reduced motion freezes all of it.
// THREE is injected.

const VERT = `
  uniform float uTime; uniform float uScale; uniform float uSize; uniform vec3 uBox; uniform float uRise; uniform float uSway; uniform float uNear;
  attribute vec4 aSeed;
  varying float vA;
  void main() {
    float life = fract(aSeed.w + uTime * (0.05 + aSeed.z * 0.1));
    vec3 p = (aSeed.xyz - 0.5) * uBox;
    p.y = life * uRise * uBox.y;
    p.x += sin(uTime * 0.6 + aSeed.x * 20.0) * uSway * uBox.x * 0.15;
    p.z += cos(uTime * 0.5 + aSeed.y * 20.0) * uSway * uBox.z * 0.15;
    vA = smoothstep(0.0, 0.15, life) * (1.0 - smoothstep(0.55, 1.0, life));
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vA *= smoothstep(uNear * 0.4, uNear, -mv.z);
    gl_PointSize = uSize * (0.6 + aSeed.z) * uScale / -mv.z;
    gl_Position = projectionMatrix * mv;
  }
`;
const FRAG = `
  uniform vec3 uColor; uniform float uAlpha;
  varying float vA;
  void main() {
    vec2 d = gl_PointCoord - 0.5;
    float r = length(d) * 2.0;
    float a = smoothstep(1.0, 0.0, r);
    gl_FragColor = vec4(uColor, a * a * vA * uAlpha);
  }
`;

// Tier caps: the low tier keeps the cheap glows but drops the haze and the storm's blown
// sand points, halves the particle systems and uses a two-octave drift shader.
export const FX_CAPS = {
  hi:  { particleMul: 1,   haze: 160, stormPoints: 220, octaves: 4, ornithopters: true },
  low: { particleMul: 0.5, haze: 0,   stormPoints: 0,   octaves: 2, ornithopters: false },
};
// A storm only draws for these states (live data: centered / sweeping). 'eta' = not here yet.
export const STORM_DRAWN = { sweeping: 1, centered: 1 };


// Soft dark plane (the game's M_Darkness: unlit translucent black, edge falloff).
const DARK_FRAG = `
  uniform float uOpacity; uniform float uFall; varying vec2 vUv;
  void main() {
    vec2 d = abs(vUv - 0.5) * 2.0;
    float e = 1.0 - smoothstep(1.0 - uFall, 1.0, max(d.x, d.y));
    gl_FragColor = vec4(0.0, 0.0, 0.0, e * uOpacity);
  }
`;
// the veil's vertical falloff: dark at the foot, clear at the top, so it reads from an iso view too
const VEIL_FRAG = `
  uniform float uOpacity; varying vec2 vUv;
  void main() { float a = pow(1.0 - vUv.y, 1.6); gl_FragColor = vec4(0.0, 0.0, 0.0, a * uOpacity); }
`;
const PLANE_VERT = `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

// Sand drift over a stone: scrolling streaky fbm along each storm's heading, masked by the
// stone disc and by that storm's blob centre/radius (uv space 0..1 over the disc's bounding
// square). Up to STORM_MAX storms share one stone (DD PvE + PvP, the three Hagga sietches).
export const STORM_MAX = 3;
const STORM_FRAG = `
  uniform float uTime; uniform float uOct; uniform float uPower; uniform int uN;
  uniform vec2 uDir[${STORM_MAX}]; uniform vec3 uBlob[${STORM_MAX}]; uniform vec3 uColor;
  varying vec2 vUv;
  float h(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float n(vec2 p) {
    vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
    return mix(mix(h(i), h(i + vec2(1.0, 0.0)), f.x), mix(h(i + vec2(0.0, 1.0)), h(i + vec2(1.0, 1.0)), f.x), f.y);
  }
  float drift(vec2 c, vec2 dir, float seed) {
    vec2 q = vec2(dot(c, dir), dot(c, vec2(-dir.y, dir.x)));
    float t = uTime * 0.35;
    float a = 0.0, amp = 0.5, fr = 1.0;
    for (int i = 0; i < 4; i++) {
      if (float(i) >= uOct) break;
      // streaks: stretch along the wind, scroll with it
      a += amp * n(vec2((q.x - t * fr) * 3.0 * fr, q.y * 14.0 * fr + float(i) * 7.0 + seed));
      amp *= 0.5; fr *= 2.1;
    }
    return smoothstep(0.3, 0.7, a);
  }
  void main() {
    vec2 c = vUv - 0.5;
    float disc = 1.0 - smoothstep(0.38, 0.5, length(c));
    float alpha = 0.0;
    for (int k = 0; k < ${STORM_MAX}; k++) {
      if (k >= uN) break;
      float blob = 1.0 - smoothstep(uBlob[k].z * 0.55, uBlob[k].z, length(vUv - uBlob[k].xy));
      if (blob > 0.0) alpha = max(alpha, blob * drift(c, uDir[k], float(k) * 13.0));
    }
    gl_FragColor = vec4(uColor, disc * alpha * uPower * 1.15);
  }
`;

// Tiny non-indexed geometry merge (position + optional color), so each prop is one draw call.
function mergeGeos(THREE, geos) {
  const parts = geos.map((g) => (g.index ? g.toNonIndexed() : g));
  const n = parts.reduce((a, g) => a + g.attributes.position.count, 0);
  const pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
  let o = 0, hasCol = false;
  parts.forEach((g) => {
    pos.set(g.attributes.position.array, o * 3);
    if (g.attributes.color) { col.set(g.attributes.color.array, o * 3); hasCol = true; } else col.fill(1, o * 3, (o + g.attributes.position.count) * 3);
    o += g.attributes.position.count;
  });
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  if (hasCol) out.setAttribute('color', new THREE.BufferAttribute(col, 3));
  out.computeVertexNormals();
  return out;
}

export function createFx(THREE, opts) {
  const reduced = !!opts.reducedMotion;
  const group = new THREE.Group();
  const systems = [];
  const flickers = [];
  const arcs = [];
  const pulses = [];
  const lights = [];
  const travelCurves = [];
  const pxScale = { value: 900 };      // viewport px per world unit at distance 1
  const disposables = [];

  // box = [x,y,z] in scene units; origin = base centre; rise 0..1 of box height
  function particles(origin, o) {
    const n = Math.max(4, Math.round((o.count || 80) * (o.fixed ? 1 : caps.particleMul)));
    const geo = new THREE.BufferGeometry();
    const seed = new Float32Array(n * 4), pos = new Float32Array(n * 3);
    for (let i = 0; i < n * 4; i++) seed[i] = Math.random();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('aSeed', new THREE.BufferAttribute(seed, 4));
    const mat = new THREE.ShaderMaterial({
      vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      uniforms: {
        uTime: { value: 0 }, uScale: pxScale, uSize: { value: o.size || 14 }, uBox: { value: new THREE.Vector3(...o.box) },
        uNear: { value: o.near || 0 }, uRise: { value: o.rise == null ? 1 : o.rise }, uSway: { value: o.sway == null ? 1 : o.sway },
        uColor: { value: new THREE.Color(o.color) }, uAlpha: { value: o.alpha == null ? 0.8 : o.alpha },
      },
    });
    const pts = new THREE.Points(geo, mat);
    pts.frustumCulled = false;
    pts.position.set(origin[0], origin[1], origin[2]);
    group.add(pts);
    systems.push(mat);
    disposables.push(geo, mat);
    return pts;
  }

  function glow(origin, color, scale, opts2) {
    const mat = new THREE.SpriteMaterial({ map: opts.glowTex, color, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0.4, toneMapped: false });
    const sp = new THREE.Sprite(mat);
    sp.scale.set(scale, scale, 1);
    sp.position.set(origin[0], origin[1], origin[2]);
    group.add(sp);
    disposables.push(mat);
    if (opts2 && opts2.flicker) flickers.push({ mat, base: mat.opacity, ph: Math.random() * 10, amp: opts2.flicker });
    if (opts2 && opts2.pulse) pulses.push({ mat, base: mat.opacity, speed: opts2.pulse, ph: Math.random() * 6 });
    return sp;
  }

  // Build every location's effects. `at(id)` -> { x, y, z, s } : the miniature root
  // (scene units) and its uniform scale, so offsets (overmap cm) land where the game put them.
  function build(fxData, at) {
    const locs = fxData.locations || {};
    const P = (id, off, lift) => {
      const a = at(id); if (!a) return null;
      const k = 1e-4 * a.s * a.fxScale;   // cm -> scene units, matching the miniature
      return [a.x + off[0] * k, a.y + (off[2] + (lift || 0)) * k, a.z + off[1] * k, k];
    };
    for (const [id, list] of Object.entries(locs)) {
      for (const e of list) {
        const off = e.offsetCm;
        if (!off) continue;
        const p = P(id, off); if (!p) continue;
        const k = p[3];
        if (e.asset === 'NS_VFX_Overland_Radiation') {
          const blue = id === 'ruins-of-old-carthag';
          const b = (e.params && e.params.BoxSize && e.params.BoxSize[0]) || 700;
          particles([p[0], p[1] - b * 0.4 * k, p[2]], { box: [b * k, b * k * 0.9, b * k], count: 70, size: 150 * k, rise: 1, sway: 1,
            color: blue ? 0x58b8ff : 0x7dff5a, alpha: blue ? 0.9 : 0.55 });
          glow([p[0], p[1], p[2]], blue ? 0x3a9bff : 0x69e04a, b * k * 1.2, { pulse: 0.7 });
        } else if (e.asset === 'NS_VFX_Overland_Fire') {
          particles([p[0], p[1], p[2]], { box: [300 * k, 520 * k, 300 * k], count: 36, size: 70 * k, rise: 1, sway: 0.5, color: 0xff8a2a, alpha: 0.95 });
          glow([p[0], p[1] + 70 * k, p[2]], 0xff7a1e, 420 * k, { flicker: 0.45 });
        } else if (e.asset === 'NS_VFX_Overland_Electric') {
          arcs.push({ origin: [p[0], p[1] + 60 * k, p[2]], r: 420 * k });
          glow([p[0], p[1] + 60 * k, p[2]], 0x6ac4ff, 520 * k, { flicker: 0.6 });
        } else if (e.asset === 'NS_VFX_Overmap_PoisonSpores') {
          particles([p[0], p[1] - 120 * k, p[2]], { box: [620 * k, 520 * k, 620 * k], count: 60, size: 80 * k, rise: 0.8, sway: 1, color: 0x8cff3a, alpha: 0.7 });
        } else if (e.asset === 'BP_Overland_ToxicPool') {
          glow([p[0], p[1] - 100 * k, p[2]], 0x7bff35, 700 * k, { pulse: 0.9 });
        } else if (e.asset === 'BP_OverlandMap_FlickeringLight' || (e.kind === 'light' && /Flicker/.test(JSON.stringify(e)))) {
          glow([p[0], p[1], p[2]], 0xffa04a, 560 * k, { flicker: 0.7 });
        } else if (e.asset === 'NS_VFX_Overland_WreckOfHephaestus_Wind') {
          particles([p[0], p[1], p[2]], { box: [2400 * k, 500 * k, 2400 * k], count: 40, size: 50 * k, rise: 0.4, sway: 2, color: 0xc89264, alpha: 0.45 });
        } else if (e.asset === 'SM_LightCone') {
          const cone = new THREE.Mesh(new THREE.ConeGeometry(130 * k, 900 * k, 14, 1, true),
            new THREE.MeshBasicMaterial({ color: 0xfff0c8, transparent: true, opacity: 0.16, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }));
          cone.position.set(p[0], p[1] + 450 * k, p[2]);
          group.add(cone);
          disposables.push(cone.geometry, cone.material);
        } else if (e.asset === 'BP_VFX_Overland_SpiceBlooms') {
          particles([p[0], p[1], p[2]], { box: [700 * k, 1200 * k, 700 * k], count: 40, size: 160 * k, rise: 1, sway: 1.2, color: 0xe8905a, alpha: 0.6 });
        }
      }
    }
  }

  // Travel pointers: a few bright points flowing along each game spline.
  function travel(lines, toScene) {
    lines.forEach((ln) => {
      const pts = ln.pointsCm.map(toScene);
      const curve = new THREE.CatmullRomCurve3(pts.map((p) => new THREE.Vector3(p[0], p[1], p[2])));
      const n = 5;
      const pos = new Float32Array(n * 3);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      const mat = new THREE.PointsMaterial({ color: 0xffd9a0, size: 0.26, map: opts.glowTex, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0.9, toneMapped: false });
      const p = new THREE.Points(geo, mat);
      p.frustumCulled = false;
      group.add(p);
      disposables.push(geo, mat);
      const tube = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(48)),
        new THREE.LineBasicMaterial({ color: 0xe8b86a, transparent: true, opacity: 0.16, depthWrite: false }));
      group.add(tube);
      disposables.push(tube.geometry, tube.material);
      travelCurves.push({ curve });
      systems.push({ travel: { curve, pos, geo, n, speed: 0.03 + Math.random() * 0.02, ph: Math.random() } });
    });
    if (pendingVehicles) { pendingVehicles(); pendingVehicles = null; }
  }

  const arcGeo = new THREE.BufferGeometry();
  const arcPos = new Float32Array(6 * 3 * 4);
  arcGeo.setAttribute('position', new THREE.BufferAttribute(arcPos, 3));
  const arcLine = new THREE.LineSegments(arcGeo, new THREE.LineBasicMaterial({ color: 0xbfe6ff, transparent: true, opacity: 0.0, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false }));
  arcLine.frustumCulled = false;
  group.add(arcLine);
  disposables.push(arcGeo, arcLine.material);
  let nextArc = 0;
  const tmp = new THREE.Vector3();

  function tick(sec) {
    if (reduced) return;
    swayTime.value = sec;
    vehicleLights(sec);
    if (stormOn) storms.forEach((st) => { if (st.on) st.mat.uniforms.uTime.value = sec; });
    for (let i = 0; i < vehicles.length; i++) placeVehicle(vehicles[i], sec);
    flickers.forEach((f) => { f.mat.opacity = fireMul * f.base * (1 - f.amp + f.amp * (0.5 + 0.5 * Math.sin(sec * 17 + f.ph) * Math.sin(sec * 5.3 + f.ph * 1.7))); });
    pulses.forEach((f) => { f.mat.opacity = fireMul * f.base * (0.65 + 0.35 * Math.sin(sec * f.speed * 2 + f.ph)); });
    for (const s of systems) {
      if (s.travel) {
        const t = s.travel;
        for (let i = 0; i < t.n; i++) {
          const u = (sec * t.speed + t.ph + i / t.n) % 1;
          t.curve.getPoint(u, tmp);
          t.pos[i * 3] = tmp.x; t.pos[i * 3 + 1] = tmp.y + 0.06; t.pos[i * 3 + 2] = tmp.z;
        }
        t.geo.attributes.position.needsUpdate = true;
      } else if (s.uniforms) s.uniforms.uTime.value = sec;
    }
    if (arcs.length) {
      if (sec > nextArc) {
        nextArc = sec + 0.16 + Math.random() * 0.1;
        const a = arcs[(Math.random() * arcs.length) | 0];
        for (let seg = 0; seg < 6; seg++) {
          const j = seg * 12;
          const ax = a.origin[0] + (Math.random() - 0.5) * a.r, az = a.origin[2] + (Math.random() - 0.5) * a.r;
          const ay = a.origin[1] + Math.random() * a.r * 0.5;
          arcPos.set([ax, ay, az, ax + (Math.random() - 0.5) * a.r, ay + (Math.random() - 0.5) * a.r * 0.6, az + (Math.random() - 0.5) * a.r], j);
          arcPos.set([ax, ay, az, ax, ay, az], j + 6);
        }
        arcGeo.attributes.position.needsUpdate = true;
        arcLine.material.opacity = Math.random() > 0.35 ? 0.9 : 0.0;
      } else arcLine.material.opacity *= 0.82;
    }
  }


  // ---- extras: the pieces that need more than a location recipe -------------------------
  const hi = opts.hi !== false;
  const caps = hi ? FX_CAPS.hi : FX_CAPS.low;
  const vehicles = [];
  const swayMats = [];
  const tmpV = new THREE.Vector3(), tmpV2 = new THREE.Vector3();
  let pendingVehicles = null;
  const storms = new Map(); // stone id -> { mesh, mat, pts, e, on }

  function darkness(fxData, at) {
    const locs = fxData.locations || {};
    for (const [id, list] of Object.entries(locs)) {
      for (const e of list) {
        if (!(e.materials || []).includes('M_Darkness') || !e.offsetCm) continue;
        const a = at(id); if (!a) continue;
        const k = 1e-4 * a.s * a.fxScale;
        const sc = e.scale || [1, 1, 1];
        const mat = new THREE.ShaderMaterial({
          vertexShader: PLANE_VERT, fragmentShader: DARK_FRAG, transparent: true, depthWrite: false,
          uniforms: { uOpacity: { value: 0.92 }, uFall: { value: 0.6 } },
        });
        const m = new THREE.Mesh(new THREE.PlaneGeometry(100 * sc[0] * k, 100 * sc[1] * k), mat);
        m.rotation.x = -Math.PI / 2;
        // over the miniature (the game's plane sits above the pass floor), so it veils the low ground
        m.position.set(a.x + e.offsetCm[0] * k, a.y + (e.offsetCm[2] + 260) * k, a.z + e.offsetCm[1] * k);
        m.renderOrder = 4;
        m.userData.fx = 'darkness:' + id;
        // soft vertical curtain around the footprint (open cylinder squashed to the plane's ellipse)
        const vmat = new THREE.ShaderMaterial({ vertexShader: PLANE_VERT, fragmentShader: VEIL_FRAG, transparent: true, depthWrite: false, side: THREE.DoubleSide, uniforms: { uOpacity: { value: 0.75 } } });
        const cyl = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.5, 700 * k, 24, 1, true), vmat);
        cyl.scale.set(100 * sc[0] * k * 0.9, 1, 100 * sc[1] * k * 0.9);
        cyl.position.set(m.position.x, a.y + e.offsetCm[2] * k + 350 * k, m.position.z);
        cyl.renderOrder = 4;
        cyl.userData.fx = 'darkness-veil:' + id;
        group.add(cyl);
        disposables.push(cyl.geometry, vmat);
        group.add(m);
        disposables.push(m.geometry, mat);
      }
    }
  }

  // BP_Overland_PrimroseField: Row x Columns grid at Distance cm, instances from the bake.
  function primrose(fxData, at) {
    const fields = [];
    for (const [id, list] of Object.entries(fxData.locations || {})) {
      const a = at(id); if (!a) continue;
      list.forEach((e) => { if (e.asset === 'BP_Overland_PrimroseField' && e.offsetCm) fields.push({ a, e }); });
    }
    if (!fields.length) return;
    // one merged flower: 3 sided stem + 6 sided head disc, unit height
    const stem = new THREE.CylinderGeometry(0.035, 0.05, 0.7, 3).translate(0, 0.35, 0);
    const head = new THREE.CylinderGeometry(0.22, 0.04, 0.12, 6).translate(0, 0.74, 0);
    const col = (g, c) => { const n = g.attributes.position.count, arr = new Float32Array(n * 3), cc = new THREE.Color(c); for (let i = 0; i < n; i++) cc.toArray(arr, i * 3); g.setAttribute('color', new THREE.BufferAttribute(arr, 3)); return g; };
    const merged = mergeGeos(THREE, [col(stem, 0x5f8a3a), col(head, 0xf3d96b)]);
    const mat = new THREE.MeshBasicMaterial({ vertexColors: true, toneMapped: false });
    mat.onBeforeCompile = (sh) => {
      sh.uniforms.uTime = swayTime;
      sh.vertexShader = 'uniform float uTime;\n' + sh.vertexShader.replace('#include <begin_vertex>',
        `#include <begin_vertex>
         float sw = position.y * position.y;
         transformed.x += sin(uTime * 1.6 + instanceMatrix[3].x * 40.0 + instanceMatrix[3].z * 31.0) * 0.12 * sw;
         transformed.z += cos(uTime * 1.3 + instanceMatrix[3].z * 37.0) * 0.1 * sw;`);
    };
    const total = fields.reduce((n, f) => n + ((f.e.params && f.e.params.instances) || 30), 0);
    const mesh = new THREE.InstancedMesh(merged, mat, total);
    mesh.frustumCulled = false;
    const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), sc = new THREE.Vector3(), ps = new THREE.Vector3(), eul = new THREE.Euler();
    let idx = 0;
    // deterministic jitter so the field never reshuffles between loads
    let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    fields.forEach(({ a, e }) => {
      const pr = e.params || {}, rows = pr.Row || 3, cols = pr.Columns || 8, dist = pr.Distance || 124;
      const n = pr.instances || rows * cols;
      const k = 1e-4 * a.s * a.fxScale;
      for (let i = 0; i < n; i++) {
        const c = i % cols, r = ((i / cols) | 0) % rows;
        const jx = (rnd() - 0.5) * dist * 0.7, jy = (rnd() - 0.5) * dist * 0.7;
        const gx = (c - (cols - 1) / 2) * dist + jx, gy = (r - (rows - 1) / 2) * dist * 1.1 + jy + (i >= rows * cols ? dist * 0.5 : 0);
        const x = a.x + (e.offsetCm[0] + gx) * k, z = a.z + (e.offsetCm[1] + gy) * k;
        const y = opts.heightAt ? opts.heightAt(x, z) : a.y + e.offsetCm[2] * k;
        const h = (0.6 + rnd() * 0.8) * 110 * k * 3.4;   // ~120-260 cm, a touch exaggerated to read at table scale
        q.setFromEuler(eul.set(0, rnd() * 6.28, 0));
        m4.compose(ps.set(x, y, z), q, sc.set(h, h, h));
        mesh.setMatrixAt(idx++, m4);
      }
    });
    // a soft pale patch under each field so it can be found from a distance
    fields.forEach(({ a, e }) => {
      const k = 1e-4 * a.s * a.fxScale, pr = e.params || {};
      const w = ((pr.Columns || 8) * (pr.Distance || 124) + 200) * k;
      glow([a.x + e.offsetCm[0] * k, (opts.heightAt ? opts.heightAt(a.x + e.offsetCm[0] * k, a.z + e.offsetCm[1] * k) : a.y) + 0.01, a.z + e.offsetCm[1] * k], 0xffe9a0, w * 1.3, { pulse: 0.3 });
    });
    mesh.count = idx;
    mesh.instanceMatrix.needsUpdate = true;
    mesh.userData.fx = 'primrose';
    group.add(mesh);
    disposables.push(merged, mat);
  }
  const swayTime = { value: 0 };

  // The game's own overland vehicle meshes (SM_OverlandMap_TinySandbike with its albedo,
  // SM_VFX_Env_Atre_Ships_DistanceOrnithopter), metres, y up. They ride the BP_Pointers travel
  // splines out and back at miniature size (smaller than the smallest stone). Lit by the scene,
  // casting a tiny shadow; at night small navigation lights blink instead.
  const VEH = {
    sandbike: { file: 'overmap-sandbike.glb', len: 0.085, nativeLen: 3.25, yaw: Math.PI / 2, speed: 0.011, lift: 0.012,
      lights: [[1.5, 0.9, 0.0, 0xfff0c8, 0.0], [-1.5, 0.9, 0.0, 0xff5a3a, 0.5]] },
    ornithopter: { file: 'overmap-ornithopter.glb', len: 0.15, nativeLen: 19, yaw: 0, speed: 0.007, lift: 0.1,
      lights: [[0, 2.5, 9.0, 0xff4040, 0.0], [0, 2.5, -9.0, 0x40ff70, 0.5], [-7.0, 3.0, 0, 0xffffff, 0.25]] },
  };
  const models = {};      // kind -> Promise<THREE.Object3D|null>, loaded once
  let gltfLoader = null;
  function loadModel(kind) {
    if (models[kind]) return models[kind];
    if (!opts.url) return (models[kind] = Promise.resolve(null));
    if (!gltfLoader) { gltfLoader = new GLTFLoader(); gltfLoader.setMeshoptDecoder(MeshoptDecoder); }
    models[kind] = gltfLoader.loadAsync(opts.url(VEH[kind].file)).then((g) => {
      g.scene.traverse((o) => {
        if (!o.isMesh) return;
        o.castShadow = true;
        const m = o.material;
        const mat = new THREE.MeshStandardMaterial({ map: m.map || null, vertexColors: !m.map && !!o.geometry.attributes.color, roughness: 0.75, metalness: 0.1 });
        // the game's ornithopter is vertex-painted for its (unlit, 2.5x brightness) far-sky material;
        // here it takes the table light as a plain sand-tan hull
        if (kind === 'ornithopter') { mat.vertexColors = false; mat.color.set(0xc9b896); mat.emissive.set(0x3a3020); }
        if (m.map) { m.map.colorSpace = THREE.SRGBColorSpace; m.map.anisotropy = 2; }
        o.material = mat;
        disposables.push(mat, o.geometry);
      });
      return g.scene;
    }).catch(() => null);
    return models[kind];
  }
  // The game's own BP_MeshFollowingSpline paths (overmap-vehicles.json): actor-space Hermite splines,
  // travelled once per LoopTime. Sampled to a polyline in scene units.
  function gamePath(a) {
    const yaw = (a.yawDeg * Math.PI) / 180, cy = Math.cos(yaw), sy = Math.sin(yaw), P = a.points, pts = [];
    const world = (q) => {   // actor space -> scene: rotate by the actor yaw, then rootCm; cm -> scene units
      const x = q[0] + a.splineOffsetCm[0], y = q[1] + a.splineOffsetCm[1];
      return [(a.rootCm[0] + x * cy - y * sy) * 1e-4, (q[2] + a.splineOffsetCm[2]) * 1e-4, (a.rootCm[1] + x * sy + y * cy) * 1e-4];
    };
    for (let i = 0; i < P.length - 1; i++) {
      const p0 = P[i].p, p1 = P[i + 1].p, m0 = P[i].leave, m1 = P[i + 1].arrive;
      for (let k = 0; k < 12; k++) {
        const t = k / 12, t2 = t * t, t3 = t2 * t;
        const h00 = 2 * t3 - 3 * t2 + 1, h10 = t3 - 2 * t2 + t, h01 = -2 * t3 + 3 * t2, h11 = t3 - t2;
        pts.push(world([0, 1, 2].map((c) => h00 * p0[c] + h10 * m0[c] + h01 * p1[c] + h11 * m1[c])));
      }
    }
    pts.push(world(P[P.length - 1].p));
    const cum = [0];
    for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][2] - pts[i - 1][2]));
    return { pts, cum, len: cum[cum.length - 1] };
  }
  function pathAt(path, u, out) {   // u 0..1 by distance
    const d = u * path.len;
    let i = 1; while (i < path.cum.length - 1 && path.cum[i] < d) i++;
    const f = (d - path.cum[i - 1]) / Math.max(1e-9, path.cum[i] - path.cum[i - 1]);
    const a = path.pts[i - 1], b = path.pts[i];
    out.set(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f);
    return out;
  }
  function gameVehicles(data) {
    (data.actors || []).forEach((a, i) => {
      if (a.kind === 'ornithopter' && !caps.ornithopters) return;
      const path = gamePath(a);
      if (!(path.len > 0)) return;
      addVehicle(a.kind, i, { path, loop: a.loopSec || 10 });
    });
    vehicles.forEach((v) => placeVehicle(v, 0));
    vehicleLights(0);
  }
  function addVehicle(kind, i, mv) {
    const cfg = VEH[kind];
    const mesh = new THREE.Group();
    mesh.userData.fx = kind;
    const inner = new THREE.Group();
    const sc = cfg.len / cfg.nativeLen;
    inner.scale.setScalar(sc);
    inner.rotation.y = cfg.yaw;
    mesh.add(inner);
    // navigation lights (night only): [x, y, z, colour, blink phase] in model metres
    const navs = cfg.lights.map((l) => {
      const mat = new THREE.SpriteMaterial({ map: opts.glowTex || null, color: l[3], transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0, toneMapped: false });
      const sp = new THREE.Sprite(mat);
      sp.scale.setScalar(0.028);
      sp.position.set(l[0] * sc, l[1] * sc, l[2] * sc).applyAxisAngle(new THREE.Vector3(0, 1, 0), cfg.yaw);
      mesh.add(sp);
      disposables.push(mat);
      return { mat, ph: l[4] };
    });
    group.add(mesh);
    vehicles.push(Object.assign({ mesh, inner, navs, kind, speed: cfg.speed, ph: (i * 0.37) % 1, lift: cfg.lift }, mv));
    loadModel(kind).then((m) => { if (m) inner.add(m.clone(true)); });
  }
  function vehicles_(fxData, mini, toScene, curves, stoneAt) {
    const list = [];
    for (const [id, arr] of Object.entries(fxData.locations || {})) {
      arr.forEach((e) => {
        if (/Ornithopter/i.test(e.asset || '')) list.push({ kind: 'ornithopter', id, e });
        else if (/Sandbike/i.test(e.asset || '')) list.push({ kind: 'sandbike', id, e });
      });
    }
    list.forEach((v, i) => {
      if (v.kind === 'ornithopter' && !caps.ornithopters) return;
      const at = stoneAt(v.id); if (!at) return;
      const pos = [at.x + v.e.offsetCm[0] * 1e-4 * at.s, 0, at.z + v.e.offsetCm[1] * 1e-4 * at.s];
      // nearest travel spline to the game's placement
      let best = null, bd = Infinity;
      curves.forEach((c) => c.curve.getPoints(24).forEach((p) => { const d = (p.x - pos[0]) ** 2 + (p.z - pos[2]) ** 2; if (d < bd) { bd = d; best = c; } }));
      if (!best) return;
      addVehicle(v.kind, i, { curve: best.curve });
    });
    vehicles.forEach((v) => placeVehicle(v, 0));
    vehicleLights(0);
  }
  function placeVehicle(v, sec) {
    if (v.path) {   // the game's spline: once per LoopTime, shrinking in and out at the ends
      const u = ((sec / v.loop) + v.ph) % 1;
      pathAt(v.path, u, tmpV); pathAt(v.path, Math.min(1, u + 0.01), tmpV2);
      const g0 = (opts.heightAt ? opts.heightAt(tmpV.x, tmpV.z) : 0) + v.lift + Math.max(0, tmpV.y) * 3;
      const g1 = (opts.heightAt ? opts.heightAt(tmpV2.x, tmpV2.z) : 0) + v.lift + Math.max(0, tmpV2.y) * 3;
      v.mesh.position.set(tmpV.x, g0, tmpV.z);
      if (tmpV2.x !== tmpV.x || tmpV2.z !== tmpV.z) v.mesh.lookAt(tmpV2.x, g1, tmpV2.z);
      v.mesh.scale.setScalar(Math.min(1, u / 0.06, (1 - u) / 0.06));
      if (v.kind === 'ornithopter') v.mesh.rotateZ(Math.sin(sec * 0.8 + v.ph * 6) * 0.1);
      return;
    }
    // fallback (no vehicle data): triangle wave 0..1..0 along the nearest travel spline
    const w = (sec * v.speed + v.ph) % 2, u = w < 1 ? w : 2 - w;
    v.curve.getPoint(u, tmpV);
    const du = w < 1 ? 0.01 : -0.01;
    v.curve.getPoint(Math.min(1, Math.max(0, u + du)), tmpV2);
    // both follow the relief (the spline itself floats above it for the pointer lights)
    const ground = (x, z) => (opts.heightAt ? opts.heightAt(x, z) : tmpV.y) + v.lift;
    const bob = v.kind === 'ornithopter' ? Math.sin(sec * 0.9 + v.ph * 7) * 0.012 : 0;
    const y = ground(tmpV.x, tmpV.z) + bob;
    v.mesh.position.set(tmpV.x, y, tmpV.z);
    v.mesh.lookAt(tmpV2.x, ground(tmpV2.x, tmpV2.z) + bob, tmpV2.z);
    // gentle bank into the (fake) wing beat
    if (v.kind === 'ornithopter') v.mesh.rotateZ(Math.sin(sec * 0.8 + v.ph * 6) * 0.1);
  }
  // night: small blinking navigation lights; reduced motion shows them steady
  let night = 0, fireMul = 1;
  function vehicleLights(sec) {
    for (let i = 0; i < vehicles.length; i++) {
      const navs = vehicles[i].navs;
      for (let j = 0; j < navs.length; j++) {
        const n = navs[j];
        const blink = reduced ? 1 : (Math.sin(sec * 2.4 + n.ph * 6.28) > 0.55 ? 1 : 0.25);
        n.mat.opacity = night * 0.9 * blink;
      }
    }
  }
  function setTimeOfDay(preset, nightW, fire) {
    night = Math.max(0, Math.min(1, nightW || 0));
    fireMul = Number.isFinite(fire) ? fire : 1;
    if (reduced) {   // no tick runs: apply the static look now
      flickers.forEach((f) => { f.mat.opacity = f.base * fireMul; });
      pulses.forEach((f) => { f.mat.opacity = f.base * fireMul; });
      vehicleLights(0);
    }
  }

  // Ambient dust haze: sparse slow motes over the whole table. High tier only.
  function haze(bounds) {
    if (!caps.haze || !bounds) return;
    const sx = bounds.max.x - bounds.min.x, sz = bounds.max.z - bounds.min.z;
    const p = particles([(bounds.min.x + bounds.max.x) / 2, bounds.min.y + 0.5, (bounds.min.z + bounds.max.z) / 2],
      { box: [sx, 18, sz], count: caps.haze, size: 0.045, rise: 0.35, sway: 3, color: 0xd9b27a, alpha: 0.16, near: 22, fixed: true });
    p.userData.fx = 'haze';
    p.material.blending = THREE.NormalBlending;
  }

  // Storm layer over one live stone. `e` = stones.byId entry (cxu, czu, r, plateY).
  function buildStorm(id, e) {
    const R = e.r * 1.1;
    const mat = new THREE.ShaderMaterial({
      vertexShader: PLANE_VERT, fragmentShader: STORM_FRAG, transparent: true, depthWrite: false,
      uniforms: {
        uTime: { value: 0 }, uOct: { value: caps.octaves }, uPower: { value: 0 }, uN: { value: 0 },
        uDir: { value: Array.from({ length: STORM_MAX }, () => new THREE.Vector2(1, 0)) },
        uBlob: { value: Array.from({ length: STORM_MAX }, () => new THREE.Vector3(0.5, 0.5, 0.9)) },
        uColor: { value: new THREE.Color(0xf0d4a0) },
      },
    });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(R * 2, R * 2), mat);
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set(e.cxu, e.plateY + 0.32 + e.r * 0.12, e.czu);
    mesh.renderOrder = 5;
    mesh.visible = false;
    mesh.userData.fx = 'storm';
    group.add(mesh);
    disposables.push(mesh.geometry, mat);
    const st = { mesh, mat, e, pts: null, on: false };
    if (caps.stormPoints) {
      const pts = particles([e.cxu, e.plateY + 0.2, e.czu], { box: [R * 1.6, e.r * 0.5, R * 1.6], count: caps.stormPoints, size: Math.max(0.1, e.r * 0.025), rise: 0.5, sway: 4, color: 0xf0d4a0, alpha: 0.7, near: 3, fixed: true });
      pts.visible = false; pts.userData.fx = 'storm-sand';
      pts.material.blending = THREE.NormalBlending;
      st.pts = pts;
    }
    storms.set(id, st);
  }

  // list = stormsFromLive() output (or one stormFromLive() storm, or null) for one stone,
  // default the Deep Desert. The hook being absent (never called) = no storm.
  let stormOn = false;
  function setStorm(list, id = 'deep-desert') {
    const st = storms.get(id);
    if (!st) return;
    const drawn = (Array.isArray(list) ? list : list ? [list] : []).filter((s) => s && STORM_DRAWN[s.state]).slice(0, STORM_MAX);
    st.on = drawn.length > 0;
    stormOn = [...storms.values()].some((x) => x.on);
    st.mesh.visible = st.on;
    if (st.pts) st.pts.visible = st.on;
    const u = st.mat.uniforms;
    u.uN.value = drawn.length;
    if (!st.on) { u.uPower.value = 0; return; }
    drawn.forEach((s, k) => {
      const rad = ((s.headingDeg == null ? 0 : s.headingDeg) * Math.PI) / 180;
      u.uDir.value[k].set(Math.cos(rad), Math.sin(rad));    // +x east, +y south, same as the maps page
      // nx/ny are the 0..900 map frame; blob centre lands inside the stone square
      const has = Number.isFinite(s.nx) && Number.isFinite(s.ny);
      const cx = has ? Math.min(0.95, Math.max(0.05, s.nx / 900)) : 0.5, cy = has ? Math.min(0.95, Math.max(0.05, s.ny / 900)) : 0.5;
      const rr = Number.isFinite(s.radius) ? Math.min(1.1, Math.max(0.3, (s.radius / 900) * 2.4)) : 0.9;
      u.uBlob.value[k].set(0.5 + (cx - 0.5) * 0.8, 0.5 + (cy - 0.5) * 0.8, rr * 0.55);
    });
    u.uPower.value = drawn.some((s) => s.state === 'sweeping') ? 1 : 0.85;
    // the blown sand points follow the first storm's heading
    if (st.pts) { st.pts.rotation.y = -((drawn[0].headingDeg || 0) * Math.PI) / 180; }
    if (reduced) { u.uTime.value = 1.7; }
  }

  // Everything beyond the per-location recipes. stones = createStones() result; world = { mini }.
  function buildExtras(o) {
    const fxData = o.fxData || {};
    const stoneAt = (id) => { const e = o.stones && o.stones.byId.get(id); return e ? { x: e.anchor.x, z: e.anchor.z, s: e.ms * 1e-0 } : null; };
    const stoneEntry = (id) => { const e = o.stones && o.stones.byId.get(id); return e ? { x: e.anchor.x, z: e.anchor.z, s: e.ms } : null; };
    const at = (id) => { const e = o.stones && o.stones.byId.get(id); return e ? { x: e.anchor.x, y: e.anchor.y, z: e.anchor.z, s: e.ms, fxScale: 1 } : null; };
    darkness(fxData, at);
    primrose(fxData, at);
    // vehicles need the travel splines, which travel() builds after this
    if (o.vehicleData) gameVehicles(o.vehicleData);
    else if (opts.url) fetch(opts.url('overmap-vehicles.json')).then((r) => (r.ok ? r.json() : null)).then((d) => { if (d && d.actors) gameVehicles(d); else if (travelCurves.length) vehicles_(fxData, o.mini, null, travelCurves, stoneEntry); }).catch(() => {});
    else pendingVehicles = () => vehicles_(fxData, o.mini, null, travelCurves, (id) => { const e = o.stones && o.stones.byId.get(id); return e ? { x: e.anchor.x, z: e.anchor.z, s: e.ms } : null; });
    void stoneAt;
    if (pendingVehicles && travelCurves.length) { pendingVehicles(); pendingVehicles = null; }
    let bounds = o.bounds;
    if (!bounds && o.stones) {
      bounds = new THREE.Box3();
      o.stones.byId.forEach((e) => bounds.expandByPoint(tmpV.set(e.cxu, e.plateY, e.czu)));
      bounds.expandByScalar(4);
    }
    haze(bounds);
    for (const id of o.stormStones || ['deep-desert']) {
      const e = o.stones && o.stones.byId.get(id);
      if (e) buildStorm(id, e);
    }
  }

  // Seamless material textures (rim stone, sand-bed detail), loaded on demand by the scene.
  async function loadTextures() {
    const loader = new THREE.TextureLoader();
    const get = async (name, srgb) => {
      const t = await loader.loadAsync(opts.url ? opts.url('fx/' + name) : 'fx/' + name);
      t.wrapS = t.wrapT = THREE.RepeatWrapping; t.anisotropy = 4;
      if (srgb) t.colorSpace = THREE.SRGBColorSpace;
      disposables.push(t);
      return t;
    };
    const [rim, sand] = await Promise.all([get('rim.webp', true).catch(() => null), get('sand-detail.webp', false).catch(() => null)]);
    return { rim, sand };
  }

  // Presence report for the smoke checks: what got built, and whether it is animated.
  function info() {
    const names = {};
    group.traverse((o) => { if (o.userData && o.userData.fx) names[o.userData.fx] = (names[o.userData.fx] || 0) + 1; });
    return { names, reduced, hi, caps, stormOn, vehicles: vehicles.map((v) => v.kind), primroseInstances: (() => { let n = 0; group.traverse((o) => { if (o.isInstancedMesh) n += o.count; }); return n; })(), glows: flickers.length + pulses.length, systems: systems.length,
      vehiclePos: Object.fromEntries(vehicles.map((v) => [v.kind, [v.mesh.position.x, v.mesh.position.y, v.mesh.position.z]])) };
  }
  function setScale(heightPx, fovDeg) { pxScale.value = heightPx / (2 * Math.tan(fovDeg * Math.PI / 360)); }
  function dispose() { disposables.forEach((d) => d.dispose && d.dispose()); }
  return { group, build, buildExtras, setStorm, setTimeOfDay, info, loadTextures, travel, tick, dispose, setScale, lights, caps, get stormOn() { return stormOn; } };
}
