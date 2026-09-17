// Shared Character state. One $state proxy imported by the route + every
// character component so vitals, faction standing, specializations, journey,
// the Landsraad teaser, and the equipped list stay in sync without prop-
// drilling. Mutate the proxy's PROPERTIES in place (never reassign `character`
// itself), so the shared reference every component holds stays live.
//
// Read-only surface: the overview is LIVE and never gated. The selected
// character is resolved server-side from the selection cookie, so switching the
// active character in the topbar reloads the app and this store re-fetches
// against the new selection. Sections that could not resolve for a non-default
// selected controller carry ctrl_scoped:false; the UI shows a subtle "reflects
// last logout" note there rather than fabricating a scoped value.
import { api } from './api.js';

export const character = $state({
  status: 'idle',        // idle | loading | ready | error
  characterName: '',     // active character name (echoed for headers)

  // Frozen overview shape (contract MODULE 1). Sections default to null and each
  // card renders independently, so a partial payload never blanks the page.
  header: null,          // { name, level, online, current_map, faction, faction_crest, last_online }
  vitals: null,          // { intel, xp, unspent_sp, bank_solari, pocket_solari, scrip }
  factionRep: null,      // { faction, crest, rank, rank_name, standing, pct, at_max, next_rank, to_next } | null
  specializations: null, // { tracks[], level_cap, keystones_owned, keystones_total, ctrl_scoped } | null
  journey: null,         // { arcs_completed, arc_names[], poi_total, big_moments_count, codex_count } | null
  landsraadTeaser: null, // { total_lines, total_solari, schematic_lines } | null
  equipped: null,        // { items[], ctrl_scoped } | null
});

/** Boot: pull the whole character overview in one request. */
export async function loadOverview() {
  character.status = 'loading';
  try {
    const r = await api.character.overview();
    character.header = r?.character || null;
    character.vitals = r?.vitals || null;
    character.factionRep = r?.faction_rep || null;
    character.specializations = r?.specializations || null;
    character.journey = r?.journey || null;
    character.landsraadTeaser = r?.landsraad_teaser || null;
    character.equipped = r?.equipped || null;
    character.characterName = r?.character_name || r?.character?.name || '';
    character.status = 'ready';
  } catch (e) {
    character.status = 'error';
    character.header = null;
    character.vitals = null;
    character.factionRep = null;
    character.specializations = null;
    character.journey = null;
    character.landsraadTeaser = null;
    character.equipped = null;
  }
}

/** Load everything (called once auth resolves to authed). */
export function loadAll() {
  loadOverview();
}
