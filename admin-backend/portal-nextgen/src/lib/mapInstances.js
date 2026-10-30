// Map instances, as GET /portal/features serves them (`instances`: the OPEN ones,
// in config order, from the server's map-instance config). Pure: no I/O, so node
// can run every rule here. The read itself lives in mapInstances.svelte.js.
//
// The portal holds no instance names, partitions or dims of its own. An empty
// list (unread, or a failed read) draws no instance cards rather than guessing.

const KEY_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

// Game-defined map names (one per map_model.MAPS key), not server config. The
// key order is the order the map boards list them in.
export const MAP_NAMES = { 'deep-desert': 'Deep Desert', hagga: 'Hagga Basin' };

function intOrNull(v) {
  return Number.isInteger(v) ? v : null;
}

/** The features payload's `instances`, with anything malformed dropped. */
export function parseInstances(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const r of raw) {
    if (!r || typeof r !== 'object') continue;
    if (typeof r.key !== 'string' || !KEY_RE.test(r.key)) continue;
    if (typeof r.map !== 'string' || !KEY_RE.test(r.map)) continue;
    if (typeof r.name !== 'string' || !r.name) continue;
    out.push({
      key: r.key,
      name: r.name,
      map: r.map,
      inst: typeof r.inst === 'string' && KEY_RE.test(r.inst) ? r.inst : r.key,
      partition: intOrNull(r.partition),
      dim: intOrNull(r.dim),
      mode: typeof r.mode === 'string' && r.mode ? r.mode : (r.pvp === true ? 'PvP' : 'PvE'),
      pvp: r.pvp === true,
      tracked: r.tracked !== false,
    });
  }
  return out;
}

export function instancesOn(instances, mapKey) {
  return (instances || []).filter((i) => i.map === mapKey);
}

/** The instance on `mapKey` at `dim`, else that map's first instance, else null. */
export function instanceFor(instances, mapKey, dim) {
  const on = instancesOn(instances, mapKey);
  return on.find((i) => i.dim === dim) || on[0] || null;
}

export function byKey(instances, key) {
  return (instances || []).find((i) => i.key === key) || null;
}

/** The sietches: partitioned instances (one shared terrain, split by partition). */
export function sietches(instances) {
  return (instances || []).filter((i) => i.partition != null);
}

/** "map.inst" keys of instances kept off public counts and positions. */
export function untrackedBandKeys(instances) {
  return new Set((instances || []).filter((i) => !i.tracked).map((i) => `${i.map}.${i.inst}`));
}

/** True when a map carries more than one instance under the same name, so a card
 *  has to say which (Deep Desert · PvE). */
export function sharesName(instances, inst) {
  return instancesOn(instances, inst.map).filter((i) => i.name === inst.name).length > 1;
}

/** Mode line with the untracked note, e.g. "Full PvP · untracked". */
export function modeLine(inst) {
  return inst.tracked ? inst.mode : `${inst.mode} · untracked`;
}

export function boardHref(inst) {
  return `/maps/${inst.map}?inst=${inst.inst}`;
}

/** Instances in board order: by map (MAP_NAMES order, unknown maps last), then
 *  config order within a map. */
export function boardOrder(instances) {
  const order = Object.keys(MAP_NAMES);
  const rank = (i) => (order.includes(i.map) ? order.indexOf(i.map) : order.length);
  return (instances || []).map((inst, n) => ({ inst, n }))
    .sort((a, b) => rank(a.inst) - rank(b.inst) || a.n - b.n)
    .map((x) => x.inst);
}
