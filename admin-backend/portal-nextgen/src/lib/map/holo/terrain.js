// Dune War-Table relief mesh (contract 4.4 + M4 real terrain).
//
// Two paths share one interface:
//   - SEEDED (default, unchanged): a value-noise heightfield, deterministic per
//     constant seed so every dev and CI sees identical geometry. Used when no
//     layout artifact is available, when the backend match is source==='fallback',
//     or when a layout fetch fails. This path is BYTE-IDENTICAL to the pre-M4
//     build; do not touch its constants or build loop.
//   - REAL (M4): when the backend identifies the active Coriolis layout, fetch
//     that one baked 16-bit heightfield raster (layout_<id>.bin + .json, versioned
//     by the manifest bakeStamp), rebuild the mesh from it, apply the coverage
//     mask so out-of-bounds cells drop out (the desert reads as a floating
//     landmass), and swap heightAt to sample the real raster so overlay markers
//     stay flush.
//   - HAGGA (2.26.0): the same REAL path fed from the static basin bake in its
//     own directory (/terrain/hagga/, id 0) at true vertical scale, plus an
//     optional normal map and the map-art drape (applied by index/material).
//     terrainSourceFor() picks the source per map key; the raster cache keys on
//     base + id so DD layout 0 and Hagga layout 0 never collide.
//
// The 0..1000 normalized world maps 1:1 onto the plane's X/Z extent so overlays
// project straight onto it. THREE is injected; this module never imports 'three'
// (index.js is the sole importer, so Rollup keeps three out of everything else).

const SEED = 1387;
const GRID = 95;          // 95 segments -> 96x96 verts, ~9.2k, one draw call
const OCTAVES = 4;
const FREQ = 4.5;         // base noise cells across the map
const AMP_FRAC = 0.038;   // height amplitude as a fraction of extent

// Real-terrain mesh density: the high tier follows the raster grid (384 -> 383
// segments, 147k verts, still one draw) so every baked cell gets its own vertex;
// at 191 a vertex sat every 127 m and the 100-200 m rock islands folded into the
// raster (bake_islands.py --holo) fell between vertices. Capped at REAL_SEG_MAX.
// Seeded 95 on reduced-motion/low. heightAt precision comes from the baked raster
// regardless of mesh density.
// The 1024 rasters (Hagga, DD 2.26.0) may go to REAL_SEG_HI on the high tier
// (512^2 verts, still one draw; Uint32 indices).
const REAL_SEG_MAX = 383;
const REAL_SEG_HI = 511;
const REAL_SEG_LOW = 95;

// Real-terrain relief exaggeration ON TOP of the baked heightScale. DD 2.26.0 is
// baked at true scale (heightScale 1.0) and draped like Hagga. True scale keeps
// slopes true, but the 24.4 km board is framed ~3x wider than Hagga's 8.1 km, so
// the same slopes read ~3x flatter on screen; 2x makes the Shield Wall read as a
// cliff and the dunes as relief (1.0 and 1.5 compared on the drape, 2026-10-06).
// Owner-tunable per load via ?holoRelief=X (opts.reliefBoost).
// Seeded path never sees this.
const RELIEF_BOOST = 2.0;
// Hagga is baked at true scale (heightScale 1.0 over an 8.1 km span, peaks
// ~80 board units), already in DD's boosted visual band, so no extra boost.
const HAGGA_RELIEF_BOOST = 1.0;

function hash(ix, iy) {
  let h = (ix | 0) * 374761393 + (iy | 0) * 668265263 + SEED * 362437;
  h = (h ^ (h >>> 13)) >>> 0;
  h = (h * 1274126177) >>> 0;
  h = (h ^ (h >>> 16)) >>> 0;
  return h / 4294967295;
}

function smooth(t) {
  return t * t * (3 - 2 * t);
}

function valueNoise(x, y) {
  const ix = Math.floor(x), iy = Math.floor(y);
  const fx = x - ix, fy = y - iy;
  const a = hash(ix, iy), b = hash(ix + 1, iy);
  const c = hash(ix, iy + 1), d = hash(ix + 1, iy + 1);
  const u = smooth(fx), v = smooth(fy);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}

function fbm(x, y) {
  let sum = 0, amp = 0.5, freq = 1, norm = 0;
  for (let o = 0; o < OCTAVES; o++) {
    sum += amp * valueNoise(x * freq, y * freq);
    norm += amp;
    amp *= 0.5;
    freq *= 2;
  }
  return sum / norm;   // 0..1
}

// terrainSourceFor(scene, opts) -> source | null (null = stay on seeded relief).
// opts: { base, forcedLayout?, forcedRelief?, tier? }. base is the SvelteKit
// $app/paths base ('' at the root). forcedLayout is already DD-gated by
// debug.js; it is gated again here so the rule holds for any caller.
//   hagga        static basin bake at <base>/terrain/hagga/, layout id 0
//   deep-desert  scene.layout (or ?holoLayout) from <base>/terrain/; drapes the
//                bake's own map art (meta.drape) like Hagga, never the 2D backdrop;
//                a bake without meta.drape keeps the hologram. 511 seg on high
//   anything else  null
// Maps with a static true-scale bake in <base>/terrain/<key>/ (layout id 0) and a
// draped backdrop. smallVariant: the backdrop ships a -2048 sibling for weak tiers
// (Hagga's 8192 art); the hub renders are 4096 and are used as-is.
const STATIC_BAKES = {
  'hagga': { smallVariant: true },          // ops/hagga-terrain-bake
  'arrakeen': { smallVariant: false },      // ops/hub-terrain-bake (game cartography diorama)
  'harko-village': { smallVariant: false },
};

export function terrainSourceFor(scene, opts) {
  opts = opts || {};
  const key = scene && scene.key;
  const root = (opts.base || '') + '/terrain/';
  const relief = num(opts.forcedRelief, null);
  if (Object.prototype.hasOwnProperty.call(STATIC_BAKES, key)) {
    return {
      key: key,
      base: root + key + '/',
      layout: { id: 0, source: 'static' },
      reliefBoost: relief != null ? relief : HAGGA_RELIEF_BOOST,
      segMax: opts.tier === 'high' ? REAL_SEG_HI : REAL_SEG_MAX,
      lowDensity: opts.tier === 'low',
      drape: true,
      smallVariant: STATIC_BAKES[key].smallVariant,
    };
  }
  if (key === 'deep-desert') {
    const forced = Number.isInteger(opts.forcedLayout) ? opts.forcedLayout : null;
    const layout = forced != null ? { id: forced, source: 'debug' } : scene.layout;
    if (!layout) return null;
    return {
      key: key,
      base: root,
      layout: layout,
      reliefBoost: relief != null ? relief : RELIEF_BOOST,
      segMax: opts.tier === 'high' ? REAL_SEG_HI : REAL_SEG_MAX,
      lowDensity: opts.tier === 'low',
      drape: true,
      bakeDrapeOnly: true,   // the DD backdrop is the flat 2D islands image: never drape it
    };
  }
  return null;
}

// surfaceFor(src, active, backdrop, small) -> { normalUrl, drapeUrl }.
// active is terrain.active() (the bake actually rendered, null while seeded).
// The normal map only when the rendered meta names one; the drape only for a
// drape source with an image backdrop, and only once the real relief is up
// (the seeded fallback keeps the plain hologram). small picks the backdrop's
// -2048 variant (touch / weak GPUs; the 8192 original is ~256 MB decoded).
export function surfaceFor(src, active, backdrop, small) {
  const out = { normalUrl: null, drapeUrl: null, drapeGain: 1 };
  if (!src || !active) return out;
  const meta = active.meta || {};
  if (typeof meta.drapeGain === 'number' && isFinite(meta.drapeGain) && meta.drapeGain > 0) out.drapeGain = meta.drapeGain;
  if (typeof meta.normalMap === 'string' && meta.normalMap) {
    out.normalUrl = active.base + meta.normalMap + active.v;
  }
  if (src.drape && typeof meta.drape === 'string' && meta.drape) {
    // the bake ships its own drape (hub lit renders): same framing as the bake raster
    // weak tiers take the bake's small sibling when it ships one (DD: layout_N_drape-2048.webp)
    const name = small && typeof meta.drapeSmall === 'string' && meta.drapeSmall ? meta.drapeSmall : meta.drape;
    out.drapeUrl = active.base + name + active.v;
  } else if (src.drape && !src.bakeDrapeOnly && backdrop && backdrop.type === 'image' &&
             typeof backdrop.src === 'string' && backdrop.src) {
    out.drapeUrl = small && src.smallVariant !== false
      ? backdrop.src.replace(/\.webp(\?|$)/, '-2048.webp$1') : backdrop.src;
  }
  return out;
}

// createTerrain(THREE, opts) -> { mesh, heightAt, dispose, applyLayout, active }.
// opts: { view, material, reducedMotion?, layoutBase?, reliefBoost? }.
//   view       normalized world extent (1000).
//   material   shared amber holo material (owned + disposed by index).
//   reducedMotion  low-density real mesh + no assumptions of animation.
//   layoutBase default base URL for the baked artifacts (e.g. '/terrain/') when
//              applyLayout is called without a source. Defaults sensibly so the
//              module is testable standalone.
// mesh is an InstancedMesh(count 1) because the tree-shaken import list omits
// plain Mesh; a single instance at identity is one draw call, same result.
export function createTerrain(THREE, opts) {
  const view = opts.view || 1000;
  const amp = view * AMP_FRAC;
  const reducedMotion = !!opts.reducedMotion;
  const layoutBase = normalizeBase(opts.layoutBase);
  const reliefBoost = (typeof opts.reliefBoost === 'number' && isFinite(opts.reliefBoost))
    ? opts.reliefBoost : RELIEF_BOOST;

  // Seeded continuous sampler. Kept as a standalone fn so heightAt can delegate
  // to it (default) or to the raster sampler (after a real layout loads), while
  // overlays keep the one stable heightAt reference.
  function seededHeight(nx, ny) {
    const u = nx / view, v = ny / view;
    return (fbm(u * FREQ, v * FREQ) - 0.5) * 2 * amp;
  }

  // The active sampler; overlays3d captured heightAt at construction, so it must
  // stay one function that internally dispatches to whichever sampler is live.
  let activeHeight = seededHeight;
  function heightAt(nx, ny) {
    return activeHeight(nx, ny);
  }

  // Seeded geometry build (verbatim pre-M4 behavior). Returns a BufferGeometry.
  function buildSeededGeo() {
    const geo = new THREE.PlaneGeometry(view, view, GRID, GRID);
    geo.rotateX(-Math.PI / 2);   // lay flat: X east, Z south, Y up
    const pos = geo.attributes.position;
    const half = view / 2;
    for (let i = 0; i < pos.count; i++) {
      const nx = pos.getX(i) + half;
      const ny = pos.getZ(i) + half;
      pos.setY(i, seededHeight(nx, ny));
    }
    pos.needsUpdate = true;
    geo.computeVertexNormals();
    return geo;
  }

  let geo = buildSeededGeo();
  const mesh = new THREE.InstancedMesh(geo, opts.material, 1);
  mesh.setMatrixAt(0, new THREE.Matrix4());   // identity; default is zeroed
  mesh.instanceMatrix.needsUpdate = true;
  mesh.frustumCulled = false;
  mesh.renderOrder = 0;

  let destroyed = false;
  let loadToken = 0;           // stale-guard for out-of-order async loads
  let currentKey = null;       // base + id currently rendered (null = seeded)
  let pendingKey = null;       // base + id in flight (repeat scenes don't restart it)
  let current = null;          // { key, base, meta, v } of the rendered bake
  const failed = new Set();    // keys that failed once: warn once, no retry storm

  // Build a real-terrain geometry from a decoded raster + meta. Sets Y per vert
  // from the raster and drops faces whose centre cell is out of coverage so the
  // desert floats as a shaped landmass.
  function buildRealGeo(sampler, covered, W, H, segMax, lowDensity) {
    const seg = (reducedMotion || lowDensity) ? REAL_SEG_LOW : Math.min(Math.max(W, H) - 1, segMax);
    const g = new THREE.PlaneGeometry(view, view, seg, seg);
    g.rotateX(-Math.PI / 2);
    const pos = g.attributes.position;
    const half = view / 2;
    for (let i = 0; i < pos.count; i++) {
      const nx = pos.getX(i) + half;
      const ny = pos.getZ(i) + half;
      pos.setY(i, sampler(nx, ny));
    }
    pos.needsUpdate = true;

    // Faded-void edges: keep only faces whose centroid falls on a covered cell.
    if (covered) {
      const src = g.getIndex();
      if (src) {
        const idx = src.array;
        const kept = [];
        for (let t = 0; t < idx.length; t += 3) {
          const a = idx[t], b = idx[t + 1], c = idx[t + 2];
          const cx = (pos.getX(a) + pos.getX(b) + pos.getX(c)) / 3 + half;
          const cy = (pos.getZ(a) + pos.getZ(b) + pos.getZ(c)) / 3 + half;
          if (coverageAt(covered, W, H, cx, cy)) {
            kept.push(a, b, c);
          }
        }
        const Ctor = pos.count > 65535 ? Uint32Array : Uint16Array;
        g.setIndex(new THREE.BufferAttribute(new Ctor(kept), 1));
      }
    }
    g.computeVertexNormals();
    return g;
  }

  function swapGeo(next) {
    const old = geo;
    geo = next;
    mesh.geometry = next;
    if (old) old.dispose();
  }

  // Revert to the seeded relief (fresh build is byte-identical to construction).
  function revertToSeeded() {
    if (currentKey === null) return;
    activeHeight = seededHeight;
    currentKey = null;
    current = null;
    swapGeo(buildSeededGeo());
  }

  // applyLayout(layout, src?) -> Promise<boolean> (true if the rendered relief changed).
  // layout = { id, confidence, source } from the /data payload (or a static
  // source's fixed layout). src = terrainSourceFor() output: { base,
  // reliefBoost, segMax, lowDensity }; absent = the createTerrain defaults (DD).
  // Absent / fallback / same-key / in-flight / already-failed are no-ops that
  // leave the current relief untouched.
  async function applyLayout(layout, src) {
    if (destroyed) return false;
    const id = layout && Number.isInteger(layout.id) ? layout.id : null;
    const usable = id !== null && layout.source !== 'fallback';
    if (!usable) {
      if (currentKey !== null) { revertToSeeded(); return true; }
      return false;
    }
    const base = src && src.base ? normalizeBase(src.base) : layoutBase;
    const boost = src && typeof src.reliefBoost === 'number' ? src.reliefBoost : reliefBoost;
    const segMax = src && src.segMax ? src.segMax : REAL_SEG_MAX;
    const lowDensity = !!(src && src.lowDensity);
    const key = base + '#' + id;
    if (key === currentKey || key === pendingKey || failed.has(key)) return false;

    const token = ++loadToken;
    pendingKey = key;
    try {
      const decoded = await loadLayoutRaster(base, id);
      if (destroyed || token !== loadToken) { return false; }
      const sampler = makeSampler(decoded, boost);
      const { covered, W, H } = decoded;
      const nextGeo = buildRealGeo(sampler, covered, W, H, segMax, lowDensity);
      swapGeo(nextGeo);
      // Overlays and the hit-test sample the RENDERED surface, not the raster:
      // between vertices the mesh is flat triangles, and on the 95-seg tier the
      // raster sits up to ~9 units (p99) off it, so anchors would float or sink.
      activeHeight = meshSampler(nextGeo, view);
      currentKey = key;
      // relief = total vertical exaggeration (baked heightScale x boost), for
      // scaling the true-scale normal map's slopes to match the mesh.
      current = {
        key: key, base: base, meta: decoded.meta, v: decoded.v,
        relief: num(decoded.meta.heightScale, 1) * boost,
      };
      return true;
    } catch (err) {
      // Any failure keeps the current relief (seeded on first load). Never throws
      // into the render loop; the board just stays on the fallback it already has.
      if (token === loadToken) failed.add(key);
      if (typeof console !== 'undefined') {
        console.warn('[holo] layout ' + key + ' load failed; keeping fallback relief', err);
      }
      return false;
    } finally {
      if (token === loadToken) pendingKey = null;
    }
  }

  // The bake currently rendered ({ key, base, meta, v, relief }) or null.
  function active() {
    return current;
  }

  function dispose() {
    destroyed = true;
    if (geo) geo.dispose();
    // material is owned + disposed by index (shared amber material).
  }

  return { mesh, heightAt, dispose, applyLayout, active };
}

// --- raster loading (plain fetch + DataView, no GLTFLoader) ------------------

function normalizeBase(b) {
  if (!b) return '/terrain/';
  return b.charAt(b.length - 1) === '/' ? b : b + '/';
}

// Decoded rasters keyed by base + id, shared across viewer instances so a map
// switch back does not refetch or re-decode. Base is part of the key: DD
// /terrain/ layout 0 and Hagga /terrain/hagga/ layout 0 are different bakes.
// Failed loads are evicted so a later viewer can retry.
const rasterCache = new Map();

function loadLayoutRaster(base, id) {
  const key = base + '#' + id;
  let p = rasterCache.get(key);
  if (!p) {
    p = fetchLayoutRaster(base, id);
    rasterCache.set(key, p);
    p.catch(function () { if (rasterCache.get(key) === p) rasterCache.delete(key); });
  }
  return p;
}

// Fetch the manifest, resolve the one layout's bin+meta (versioned by bakeStamp),
// decode the little-endian Uint16 heightfield + optional coverage mask.
async function fetchLayoutRaster(base, id) {
  const manRes = await fetch(base + 'manifest.json', { cache: 'no-cache' });
  if (!manRes.ok) throw new Error('manifest ' + manRes.status);
  const man = await manRes.json();
  const entry = (man.layouts || []).find((l) => l.id === id);
  if (!entry) throw new Error('layout ' + id + ' not in manifest');
  const v = man.bakeStamp ? '?v=' + encodeURIComponent(man.bakeStamp) : '';
  const binName = entry.bin || ('layout_' + id + '.bin');
  const metaName = entry.meta || ('layout_' + id + '.json');

  const [binRes, metaRes] = await Promise.all([
    fetch(base + binName + v),
    fetch(base + metaName + v),
  ]);
  if (!binRes.ok) throw new Error('bin ' + binRes.status);
  if (!metaRes.ok) throw new Error('meta ' + metaRes.status);
  const meta = await metaRes.json();
  const buf = await binRes.arrayBuffer();

  const grid = meta.grid || [384, 384];
  const W = grid[0], H = grid[1];
  if (buf.byteLength < W * H * 2) {
    throw new Error('bin short: ' + buf.byteLength + ' < ' + (W * H * 2));
  }
  // Decode as little-endian regardless of host endianness (contract-frozen).
  const dv = new DataView(buf);
  const raster = new Uint16Array(W * H);
  for (let i = 0; i < W * H; i++) raster[i] = dv.getUint16(i * 2, true);

  const covered = await loadCoverage(base, meta, v, W, H);
  return { raster, covered, W, H, meta, v };
}

// heightAt(nx, ny) on the rendered relief: the vertex heights of a rotated
// PlaneGeometry(view, view, seg, seg) interpolated over the same two triangles
// per cell that PlaneGeometry indexes (a,b,d) and (b,c,d), so a point placed at
// heightAt lies exactly on the drawn surface. Reads Y before the coverage cull
// (which drops faces, never vertices).
function meshSampler(geo, view) {
  const pos = geo.attributes.position;
  const seg = Math.round(Math.sqrt(pos.count)) - 1;
  const step = view / seg;
  const row = seg + 1;
  const ys = new Float32Array(pos.count);
  for (let i = 0; i < pos.count; i++) ys[i] = pos.getY(i);
  return function (nx, ny) {
    const gx = clamp(nx / step, 0, seg), gy = clamp(ny / step, 0, seg);
    const ix = Math.min(Math.floor(gx), seg - 1), iy = Math.min(Math.floor(gy), seg - 1);
    const fx = gx - ix, fy = gy - iy;
    const a = ys[iy * row + ix], d = ys[iy * row + ix + 1];
    const b = ys[(iy + 1) * row + ix], c = ys[(iy + 1) * row + ix + 1];
    if (fx + fy <= 1) return a + (d - a) * fx + (b - a) * fy;
    return c + (b - c) * (1 - fx) + (d - c) * (1 - fy);
  };
}

// heightAt(nx, ny) over 0..view for a decoded raster: bilinear-sample, rebuild
// world Z (cm), then to board units at the same scale as X/Z (worldSpan[0] cm
// across 1000 units), exaggerated by the baked heightScale times reliefBoost.
function makeSampler(decoded, reliefBoost) {
  const { raster, W, H, meta } = decoded;
  const zMin = num(meta.zMinCm, 0);
  const zMax = num(meta.zMaxCm, 65535);
  const floor = num(meta.floorCm, zMin);
  const heightScale = num(meta.heightScale, 1) * (num(reliefBoost, 1) || 1);
  const span = (meta.worldSpan && meta.worldSpan[0]) || 2438400;
  const view = 1000;
  const cmPerBoard = span / view;           // horizontal cm per board unit
  const zRange = zMax - zMin;

  return function sampler(nx, ny) {
    const gx = clamp((nx / view) * (W - 1), 0, W - 1);
    const gy = clamp((ny / view) * (H - 1), 0, H - 1);
    const x0 = Math.floor(gx), y0 = Math.floor(gy);
    const x1 = Math.min(x0 + 1, W - 1), y1 = Math.min(y0 + 1, H - 1);
    const fx = gx - x0, fy = gy - y0;
    const h00 = raster[y0 * W + x0], h10 = raster[y0 * W + x1];
    const h01 = raster[y1 * W + x0], h11 = raster[y1 * W + x1];
    const top = h00 + (h10 - h00) * fx;
    const bot = h01 + (h11 - h01) * fx;
    const u16 = top + (bot - top) * fy;
    const zc = zMin + (u16 / 65535) * zRange;
    return ((zc - floor) / cmPerBoard) * heightScale;
  };
}

// Coverage mask: meta.coverage is a filename (1-bit floating-landmass mask baked
// beside the layout). Auto-detects a byte-per-cell mask or a bit-packed mask by
// length. Absent/failed coverage -> null (no cells dropped; solid square).
async function loadCoverage(base, meta, v, W, H) {
  const name = meta.coverage;
  if (!name || typeof name !== 'string') return null;
  try {
    const res = await fetch(base + name + v);
    if (!res.ok) return null;
    const bytes = new Uint8Array(await res.arrayBuffer());
    const cells = W * H;
    const covered = new Uint8Array(cells);
    if (bytes.length >= cells) {
      for (let i = 0; i < cells; i++) covered[i] = bytes[i] ? 1 : 0;
    } else if (bytes.length >= Math.ceil(cells / 8)) {
      for (let i = 0; i < cells; i++) {
        covered[i] = (bytes[i >> 3] >> (i & 7)) & 1;
      }
    } else {
      return null;   // unrecognized size; treat as fully covered
    }
    return covered;
  } catch {
    return null;
  }
}

function coverageAt(covered, W, H, nx, ny) {
  const col = clamp(Math.round((nx / 1000) * (W - 1)), 0, W - 1);
  const row = clamp(Math.round((ny / 1000) * (H - 1)), 0, H - 1);
  return covered[row * W + col] === 1;
}

function clamp(x, lo, hi) {
  return x < lo ? lo : x > hi ? hi : x;
}

function num(v, dflt) {
  return typeof v === 'number' && isFinite(v) ? v : dflt;
}
