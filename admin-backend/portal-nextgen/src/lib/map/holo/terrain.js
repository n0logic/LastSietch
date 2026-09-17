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
const REAL_SEG_MAX = 383;
const REAL_SEG_LOW = 95;

// Real-terrain relief exaggeration ON TOP of the baked heightScale. The baked
// rasters peak around 16 board units where the seeded relief spans +-38, so the
// real desert reads flat next to the placeholder; 2x lands the peaks in the same
// visual band. Owner-tunable per load via ?holoRelief=X (opts.reliefBoost).
// Seeded path never sees this.
const RELIEF_BOOST = 2.0;

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

// createTerrain(THREE, opts) -> { mesh, heightAt, dispose, applyLayout }.
// opts: { view, material, reducedMotion?, layoutBase? }.
//   view       normalized world extent (1000).
//   material   shared amber holo material (owned + disposed by index).
//   reducedMotion  low-density real mesh + no assumptions of animation.
//   layoutBase base URL for the baked artifacts (e.g. '/terrain/');
//              index.js supplies it from $app/paths. Defaults sensibly so the
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
  let currentLayoutId = null;  // the layout currently rendered (null = seeded)

  // Build a real-terrain geometry from a decoded raster + meta. Sets Y per vert
  // from the raster and drops faces whose centre cell is out of coverage so the
  // desert floats as a shaped landmass.
  function buildRealGeo(sampler, covered, W, H) {
    const seg = reducedMotion ? REAL_SEG_LOW : Math.min(Math.max(W, H) - 1, REAL_SEG_MAX);
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
    if (currentLayoutId === null) return;
    activeHeight = seededHeight;
    currentLayoutId = null;
    swapGeo(buildSeededGeo());
  }

  // applyLayout(layout) -> Promise<boolean> (true if the rendered relief changed).
  // layout = { id, confidence, source } from the /data payload. Absent / fallback
  // / same-id are no-ops that leave the current relief untouched.
  async function applyLayout(layout) {
    if (destroyed) return false;
    const id = layout && Number.isInteger(layout.id) ? layout.id : null;
    const usable = id !== null && layout.source !== 'fallback';
    if (!usable) {
      if (currentLayoutId !== null) { revertToSeeded(); return true; }
      return false;
    }
    if (id === currentLayoutId) return false;

    const token = ++loadToken;
    try {
      const built = await loadLayoutRaster(layoutBase, id, reliefBoost);
      if (destroyed || token !== loadToken) { return false; }
      const { sampler, covered, W, H } = built;
      swapGeo(buildRealGeo(sampler, covered, W, H));
      activeHeight = sampler;
      currentLayoutId = id;
      return true;
    } catch (err) {
      // Any failure keeps the current relief (seeded on first load). Never throws
      // into the render loop; the board just stays on the fallback it already has.
      if (typeof console !== 'undefined') {
        console.warn('[holo] layout ' + id + ' load failed; keeping fallback relief', err);
      }
      return false;
    }
  }

  function dispose() {
    destroyed = true;
    if (geo) geo.dispose();
    // material is owned + disposed by index (shared amber material).
  }

  return { mesh, heightAt, dispose, applyLayout };
}

// --- raster loading (plain fetch + DataView, no GLTFLoader) ------------------

function normalizeBase(b) {
  if (!b) return '/terrain/';
  return b.charAt(b.length - 1) === '/' ? b : b + '/';
}

// Fetch the manifest, resolve the one layout's bin+meta (versioned by bakeStamp),
// decode the little-endian Uint16 heightfield + optional coverage mask, and
// return a world-Y sampler over 0..view plus the coverage grid.
async function loadLayoutRaster(base, id, reliefBoost) {
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

  const zMin = num(meta.zMinCm, 0);
  const zMax = num(meta.zMaxCm, 65535);
  const floor = num(meta.floorCm, zMin);
  const heightScale = num(meta.heightScale, 1) * (num(reliefBoost, 1) || 1);
  const span = (meta.worldSpan && meta.worldSpan[0]) || 2438400;
  const view = 1000;
  const cmPerBoard = span / view;           // horizontal cm per board unit
  const zRange = zMax - zMin;

  // heightAt(nx, ny): bilinear-sample the raster, reconstruct world Z (cm), then
  // to board units at the same scale as X/Z, exaggerated by the baked heightScale
  // times the runtime reliefBoost.
  function sampler(nx, ny) {
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
  }

  return { sampler, covered, W, H };
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
