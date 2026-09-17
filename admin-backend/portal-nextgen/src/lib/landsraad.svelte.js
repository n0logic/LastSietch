// Shared Landsraad state. One $state proxy imported by the route + every
// landsraad component so the term board, the faction rails, the open tile
// drawer, the rewards summary, and the house-rewards grid stay in sync without
// prop-drilling. Mutate the proxy's PROPERTIES in place (never reassign
// `landsraad` itself), so the shared reference every component holds stays live.
//
// READ-ONLY surface (contract MODULE 2): account-scoped, no CSRF, no writes, no
// optimistic mutation. The character switcher does NOT re-key this. The board
// (term-global great-house contest) and the rewards (the player's own pending
// house rewards) render independently: either half may be null and the other
// still shows. Reads are never gated; signed-out seals to a Connect-Discord panel.
import { api } from './api.js';

export const landsraad = $state({
  status: 'idle',        // idle | loading | ready | error
  board: null,           // _load_landsraad_board output (tiles/rails/term) | null
  rewards: null,         // _load_landsraad output (summary/houses/board) | null
  rewardsError: false,   // rewards half failed while the board still resolved
  activeChar: '',        // active character name (server-resolved, echoed)

  selectedTile: null,    // open TileDrawer target (a board tile) | null
  selectedHouse: null,   // open HouseRewardsGrid detail (a rewards house) | null
});

// Monotonic request token so a slow in-flight load can never overwrite a newer
// one (e.g. a rapid re-auth or a manual refresh landing out of order).
let loadSeq = 0;

/** Boot: pull the combined board + rewards overview in one request. */
export async function loadAll() {
  const seq = ++loadSeq;
  landsraad.status = 'loading';
  try {
    const r = await api.landsraad.overview();
    if (seq !== loadSeq) return; // stale response, a newer load won
    landsraad.board = r?.board || null;
    landsraad.rewards = r?.rewards || null;
    landsraad.rewardsError = r?.rewards_error === true;
    landsraad.activeChar = r?.active_character_name || '';
    landsraad.status = 'ready';
  } catch (e) {
    if (seq !== loadSeq) return;
    landsraad.status = 'error';
    landsraad.board = null;
    landsraad.rewards = null;
  }
}

/** Open a board tile's reward-ladder drawer (or close if the same tile). */
export function selectTile(tile) {
  landsraad.selectedTile =
    tile && landsraad.selectedTile !== tile ? tile : null;
}
export function closeTile() { landsraad.selectedTile = null; }

/** Open a rewards house's detail (or close if the same house). */
export function selectHouse(house) {
  landsraad.selectedHouse =
    house && landsraad.selectedHouse !== house ? house : null;
}
