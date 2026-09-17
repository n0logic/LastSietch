// Static gear stat/description catalogue (admin-backend/static/data/gear-stats.json,
// 858 entries keyed by template_id: RE-extracted from the game paks, generated
// 2026-06-17). Fetched once and cached in memory for the session.
//
// IMPORTANT: every numeric stat in this file is BASE (unscaled by the item's
// in-game quality/grade). The runtime scaling curve is not persisted anywhere
// we can read (see GHIDRA-GRADE-SCALING-2026-06-17) - a T3 item can show
// 2-3x its base Damage on screen. Callers MUST label these as base values,
// never present them as the live in-game number.
import { getJSON } from '$lib/api.js';

const URL = '/admin/static/data/gear-stats.json';
let pending = null;

function load() {
  if (!pending) {
    pending = getJSON(URL).then((d) => (d && d.gear) || {}).catch(() => ({}));
  }
  return pending;
}

/** Static stat/description entry for one template_id: {type, rarity,
 *  description, stats:{}, units:{}}, or null when the item has no
 *  catalogued row (most live template_ids do not - that is routine, not
 *  an error, since gear-stats.json covers 858 of several thousand items). */
export async function gearStatsFor(templateId) {
  if (!templateId) return null;
  const gear = await load();
  return gear[templateId] || null;
}
