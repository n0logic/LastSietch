// Sand Table data layer (DOM-free, three-free, so the Guild chart, the 3D scene and
// the node smoke all share it). Everything positional comes from the overmap bake
// in static/world-data (overmap cm: X east, Y south). Nothing here derives a count.

// The four locations that are one of OUR maps. Everything else is an engraved marker
// with no live data. status maps[] names come from routers/dune.py MAP_DISPLAY_NAMES.
const STATUS_NAME = {
  hagga: 'Hagga Basin',
  'deep-desert': 'Deep Desert',
  arrakeen: 'Arrakeen',
  'harko-village': 'Harko Village',
};

export const KIND_LABEL = {
  region: 'Region', settlement: 'Settlement', story: 'Story map', landmark: 'Landmark', dungeon: 'Dungeon',
};

// Blueprint calibration of T_UI_GlobalMap_D (spec section 3), 4096 x 2048 reference.
export const ART_CAL = { cmPerUnit: 250, offX: 996.984, offY: 613.1, scale4096: 2.048 };
export function overmapToArt4096(xCm, yCm) {
  return [
    (xCm / ART_CAL.cmPerUnit + ART_CAL.offX) * ART_CAL.scale4096,
    (yCm / ART_CAL.cmPerUnit + ART_CAL.offY) * ART_CAL.scale4096,
  ];
}

// Asset URL with the world bakeStamp as the cache-bust key (the stamp changes with
// every re-bake, so a shipped file never keeps an old key).
export function worldUrl(base, name, stamp) {
  return `${base}/world-data/${name}${stamp ? `?v=${encodeURIComponent(stamp)}` : ''}`;
}

// Manifest first (no-cache, it carries the stamp), then the rest keyed by it.
export async function loadWorld(base, fetchImpl) {
  const f = fetchImpl || fetch;
  const man = await (await f(`${base}/world-data/world-manifest.json`, { cache: 'no-cache' })).json();
  const stamp = man.bakeStamp || '';
  const get = async (name) => {
    const r = await f(worldUrl(base, name, stamp));
    if (!r.ok) throw new Error(name + ' ' + r.status);
    return r.json();
  };
  const [places, contours, art, mini] = await Promise.all([
    get('overmap-places.json'), get('overmap-contours.json'), get('overmap-art.json'), get('overmap-miniatures.json'),
  ]);
  return { stamp, manifest: man, places, contours, art, mini, stones: buildStones(places, mini) };
}

// 23 locations -> stones. Hagga is ONE stone with 2 facets, Deep Desert one with 2
// (spec 5). Order = west to east so keyboard cycling is deterministic.
export function buildStones(places, mini) {
  const byKey = {};
  (places.stones || []).forEach((s) => { byKey[s.key] = s; });
  const miniLoc = (mini && mini.locations) || {};
  const stones = places.locations.map((l) => {
    const m = miniLoc[l.id] || {};
    const s = l.key ? byKey[l.key] : null;
    return {
      id: l.id,
      name: l.name,
      kind: l.kind,
      key: l.key || null,
      description: l.description || '',
      x: l.position.x,
      y: l.position.y,
      z: l.position.z || 0,
      facets: s ? s.facets.map((f) => ({ key: f.key, name: f.name, rule: f.rule })) : [],
      disc: m.disc || null,
      label: m.label || null,
      live: !!l.key,
    };
  });
  stones.sort((a, b) => a.x - b.x || a.y - b.y);
  return stones;
}

// Keyboard order: arrows cycle through this list (wraps).
export function cycle(stones, currentId, dir) {
  if (!stones.length) return null;
  const i = stones.findIndex((s) => s.id === currentId);
  if (i < 0) return (dir > 0 ? stones[0] : stones[stones.length - 1]).id;
  return stones[(i + (dir > 0 ? 1 : -1) + stones.length) % stones.length].id;
}

// duneStatus() -> 'up' | 'down' | 'on-demand' | 'blocked' | 'unknown' | null (no live data).
// Hubs spun down are "on-demand" ("Available on travel"), never "down"/"Offline".
export function stoneStatus(stone, status) {
  if (!stone.live) return null;
  if (!status || status.available === false || !Array.isArray(status.maps)) return 'unknown';
  const row = status.maps.find((m) => m && m.name === STATUS_NAME[stone.key]);
  if (!row) return 'unknown';
  if (row.blocked) return 'blocked';
  if (row.up) return 'up';
  return row.on_demand ? 'on-demand' : 'down';
}

// Location -> status row. ONLY the four locations with their own row in /api/dune/status
// maps[] (Hagga Basin incl. its two facets, Deep Desert, Arrakeen, Harko Village) carry
// live status; story maps and dungeons are instance pools whose rows are a collapsed total,
// so they stay engraved markers with a neutral gold ring: no glow, no glyph, no caption.
// The travel badge (inlay chevron + "On travel" caption) belongs to on-demand rows only.
export function travelBadge(status) {
  return status === 'on-demand' ? { glyph: true, caption: 'On travel' } : null;
}

export const STATUS_TEXT = {
  up: 'Online', down: 'Down', 'on-demand': 'Available on travel', blocked: 'Sealed', unknown: 'Status unavailable',
};

// /api/dune/bands -> enum-only. Anything that is not a known enum string is dropped;
// numbers, nested objects and unknown keys never become marks. No derivation, ever.
const BAND_MARKS = { quiet: 0, normal: 1, busy: 2, surging: 3 };
const AMTAL_MARKS = { quiet: 0, active: 1 };
const BAND_LABEL = { quiet: 'Quiet', normal: 'Normal', busy: 'Busy', surging: 'Surging', active: 'Active' };

export function bandKey(stoneKey, facetKey) {
  return facetKey ? `${stoneKey}.${facetKey}` : stoneKey;
}

// Returns { marks: 0..3, label } or null (draw nothing).
export function bandFor(bands, stoneKey, facetKey) {
  if (!bands || typeof bands !== 'object') return null;
  const src = bands.bands && typeof bands.bands === 'object' ? bands.bands : bands;
  const k = bandKey(stoneKey, facetKey);
  const v = src[k];
  if (typeof v !== 'string') return null;
  const table = k === 'hagga.amtal' ? AMTAL_MARKS : BAND_MARKS;
  if (!Object.prototype.hasOwnProperty.call(table, v)) return null;
  return { marks: table[v], label: BAND_LABEL[v] };
}

export function stoneBands(stone, bands) {
  if (!stone.live) return [];
  const targets = stone.facets.length ? stone.facets.map((f) => ({ facet: f.key, name: f.name })) : [{ facet: null, name: stone.name }];
  return targets.map((t) => ({ ...t, band: bandFor(bands, stone.key, t.facet) }));
}

export async function loadBands(fetchImpl) {
  try {
    const r = await (fetchImpl || fetch)('/api/dune/bands', { credentials: 'same-origin', headers: { accept: 'application/json' } });
    if (!r.ok) return null;
    const j = await r.json();
    return j && typeof j === 'object' ? j : null;
  } catch { return null; }
}

// Where a click goes. Hagga facets pick the instance; DD defaults to PvE.
export function mapHref(base, stone, facetKey) {
  if (!stone.live) return null;
  const f = facetKey || (stone.facets[0] && stone.facets[0].key);
  return `${base}/maps/${stone.key}${stone.facets.length && f ? `?inst=${f}` : ''}`;
}

// 3D vs the Guild chart. Reduced motion, low tier, no WebGL2 and phones get the chart.
// ?view=3d|chart (or the toggle) overrides everything except a missing WebGL2.
export function chooseView({ quality, webgl2, forced }) {
  if (!webgl2) return 'chart';
  if (forced === 'chart') return 'chart';
  if (forced === '3d') return '3d';
  if (!quality || quality.reducedMotion || quality.tier === 'low') return 'chart';
  if (quality.coarse) return 'chart';
  return '3d';
}

// Arrival glide: once per browser session; reduced motion never plays it.
const ARRIVAL_KEY = 'ls-world-arrived';
export function shouldArrive({ reducedMotion, storage }) {
  if (reducedMotion) return false;
  try { return !(storage && storage.getItem(ARRIVAL_KEY)); } catch { return true; }
}
export function markArrived(storage) {
  try { storage && storage.setItem(ARRIVAL_KEY, '1'); } catch { /* private mode */ }
}

// Hover/focus card model, shared by the chart and the 3D scene.
export function cardModel(stone, status, bands) {
  const st = stoneStatus(stone, status);
  return {
    id: stone.id,
    title: stone.name,
    kind: KIND_LABEL[stone.kind] || stone.kind,
    status: st,
    statusText: st ? STATUS_TEXT[st] : null,
    facets: stone.facets.map((f) => ({ ...f, band: bandFor(bands, stone.key, f.key) })),
    band: stone.live && !stone.facets.length ? bandFor(bands, stone.key, null) : null,
    description: stone.description,
    live: stone.live,
  };
}

// Viewer anchor: the stone matching the signed-in viewer's current map name, or null.
export function anchorStoneId(stones, currentMap) {
  if (!currentMap) return null;
  const s = stones.find((x) => x.live && STATUS_NAME[x.key] === currentMap);
  return s ? s.id : null;
}

// ---- Live storms for the table's sand-drift layer ----------------------------------------
// Same rule as the holo War-Table (2.26.1): only a live position draws. A storm with
// a fresh scanned location AND a centre is 'centered'; a recent spawn without a
// position no longer paints a timing-only 'sweeping' storm, and the relay's
// storm_state 'clear' draws nothing. Takes the /portal/maps/<key>/live payload and
// returns every centered dimension (Deep Desert PvE + PvP, or both Hagga
// sietches, share one stone), in dimension order; [] when there is nothing to draw.
// Stones that carry a live storm layer: stone id -> /portal/maps/<key>/live key.
export const STORM_STONES_MAP = { 'deep-desert': 'deep-desert', 'hagga-basin': 'hagga' };
const STORM_LOC_FRESH_MS = 20 * 60 * 1000;
export function stormsFromLive(live, nowMs) {
  const dims = (live && live.sandstorm && live.sandstorm.dimensions) || null;
  if (!dims || live.sandstorm.available === false) return [];
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const out = [];
  for (const dim of Object.keys(dims).sort()) {
    const info = dims[dim];
    if (!info || typeof info !== 'object' || info.storm_state === 'clear') continue;
    const scanned = info.storm_scanned_utc ? new Date(info.storm_scanned_utc).getTime() : NaN;
    if (num(info.center_nx) === null || num(info.center_ny) === null || !(now - scanned < STORM_LOC_FRESH_MS)) continue;
    out.push({ dim, state: 'centered', nx: num(info.center_nx), ny: num(info.center_ny), radius: num(info.radius_nr), headingDeg: num(info.heading_yaw),
      etaMs: info.next_eta_utc ? new Date(info.next_eta_utc).getTime() : null });
  }
  return out;
}
// The first centered storm, or null (single-storm callers).
export function stormFromLive(live, nowMs) {
  const s = stormsFromLive(live, nowMs)[0];
  if (!s) return null;
  delete s.dim;
  return s;
}
