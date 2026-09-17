// Client helpers for the per-item augment reroll/swap action. The backend
// writer (POST /portal/character/augment) does not exist yet - this ships
// DARK behind `augments_enabled` on the character/equipped payload. When that
// flag is false, callers must render NO entry point at all (house rule: a
// disabled feature renders an honest "not available", never a fake success -
// so here that means not offering the door in the first place).
//
// CONTRACT ASSUMPTION (flag for the backend dev): `augments_enabled`,
// `player_online`, and `owned_augments` are assumed to arrive nested inside
// the existing `equipped` object on GET /portal/character/v2 (alongside
// items/ctrl_scoped/equipped_count/hotbar_count/augmented_count), not as new
// top-level overview keys. That lets every component here read them off the
// `equipped` prop that already flows through EquippedList, without editing
// character.svelte.js (outside this task's granted file scope). If the
// backend instead puts them at the top level, character.svelte.js needs three
// lines added to pass them through - equipped is already prop-drilled
// everywhere this module needs it. CONFIRMED correct by the team lead.
import { sendCsrfJSON, readCookie, CSRF_COOKIE } from './api.js';

const ENDPOINT = '/portal/character/augment';

// Real error tokens the writer is documented to emit. Anything not in this
// map still gets an honest sentence via the default branch, never a generic
// "something went wrong".
const ERRORS = {
  player_online: 'Log out of the game to change augments. Nothing was changed.',
  not_owner: 'That item is not yours to modify.',
  item_not_found: 'That item is no longer there.',
  // Pawn-side only: the writer consumes from backpack/worn/hotbar/CHOAM bank,
  // so this almost always means the augment is in a container, not that it is
  // gone. Text must match _AUGMENT_ERROR_TEXT in portal.py.
  augment_not_owned:
    'That augment has to be on your character to be used \u2014 in your backpack, on your hotbar, or in your CHOAM bank. One left in a container, chest or vehicle cannot be reached. Nothing was changed.',
  incompatible_augment: "That augment does not fit this item's type. Nothing was changed.",
  unknown_item_tags: "This item's type could not be verified right now. Nothing was changed.",
  too_many_augments: 'This item already carries its maximum number of augment slots.',
  augment_disabled: 'Augment rerolling and swapping is not enabled right now.',
  augment_count_shrank:
    "That change would have removed one of this item's augments, so nothing was changed. Please reopen the item and try again.",
  duplicate_augment:
    "This item already has that augment in another slot. An item cannot carry the same augment twice \u2014 pick a different augment, or change the other slot instead.",
  // Nothing the player is carrying rolls higher than what is already fitted, so
  // the writer refused rather than spending a rare augment for nothing. Means
  // EXACTLY that and nothing else: a copy that is higher somewhere and lower
  // somewhere else is allowed, not refused. Does not imply they own nothing --
  // they may hold several that simply do not beat what is installed. Says
  // neither "upgrade" nor "stat" on purpose: roll positions are the game's asset
  // order and do not map to catalogued effects, which is the same reason a
  // multi-effect augment never gets a resolved label. Must stay byte-identical
  // to _AUGMENT_ERROR_TEXT in portal.py.
  no_improvement:
    "None of the copies you're carrying rolls higher than the one already fitted, anywhere. Nothing was changed and nothing was destroyed.",
  // Half-deploy: the game-host writer predates the transplant roll mode while the
  // portal is already dispatching it. Fails closed -- nothing is consumed -- but
  // unmapped it read as "The change did not go through", which blames the player
  // for our deploy ordering. Text must match _AUGMENT_ERROR_TEXT in portal.py.
  bad_roll_mode:
    'Augment changes are briefly unavailable while the server finishes updating. Nothing was changed. Please try again shortly.',
  augment_roll_data_missing:
    'That augment has no stat data we can read, so it was not installed. Nothing was changed.',
  augment_arrays_desynced:
    "This item's augment data is inconsistent, so nothing was changed. Please report this.",
  write_failed: 'The change did not go through. Nothing was modified.',
};

/** Player-facing copy for a failed augment write.
 *
 *  🔴 Prefer the SERVER's own `message` when the envelope carried one -- same
 *  reasoning as karum.svelte.js's reason(): it is written for players and it is
 *  the only place that knows WHICH of several refusals happened. `bad_request`
 *  alone covers four distinct cases in portal.py (a malformed body, a swap that
 *  changes more than one slot, a stale augment list, and -- with the targeted
 *  upgrade flag OFF -- a same-name/same-grade pick), so one client string for
 *  that token would be wrong three times out of four. This matters more since
 *  the picker stopped greying out same-name/same-grade rows: with the flag off
 *  that pick now reaches the server and comes back as `bad_request`, and without
 *  this the player would be told "the change did not go through" instead of the
 *  accurate sentence the server already wrote.
 *
 *  The map above stays the fallback, for a refusal that carried a token but no
 *  message. Every token the writer can emit still belongs in it. */
export function augmentErrorMessage(token, serverMessage) {
  if (typeof serverMessage === 'string' && serverMessage.trim()) return serverMessage;
  return ERRORS[token] || 'The change did not go through. Nothing was modified.';
}

/** Collapse CONSECUTIVE identical roll values into {value, count} runs.
 *
 *  The number of stat rolls is a property of the augment, not the item or its
 *  grade, and it varies a lot: measured across 77 augments the spread is 1 roll
 *  (13 of them) through 7 (Shotgun5, SpitdartRifle7), with VULCAN GAU-92
 *  Expander carrying 6. A perfect-rolled 6-roll augment therefore rendered six
 *  identical "PERFECT 100%" chips in a row, which reads as noise rather than as
 *  information.
 *
 *  Grouping is CONSECUTIVE, not global, so a run is only ever collapsed with its
 *  own neighbours: [1, .5, 1] stays three chips in their original order rather
 *  than being reordered into "PERFECT x2, 50%". Roll order is positional in the
 *  game data (each index maps to a stat we do not surface), so preserving it
 *  costs nothing and keeps the display honest if we ever do name them.
 *
 *  Returns [] for a non-array, so callers can pass an unchecked field. */
export function groupRolls(rolls) {
  if (!Array.isArray(rolls)) return [];
  const out = [];
  for (const raw of rolls) {
    const value = Math.max(0, Math.min(1, Number(raw) || 0));
    const last = out[out.length - 1];
    if (last && last.value === value) last.count += 1;
    else out.push({ value, count: 1 });
  }
  return out;
}

// Clothing carries 2 augment slots, weapons carry 3 (verified fact). The
// item's `category` is the only signal we have for which family it is; only
// "Weapons" is a confirmed value, so everything else defaults to the lower
// clothing cap - safe because augmented gear is only ever one of these two
// families to begin with.
export function slotCap(item) {
  return item?.category === 'Weapons' ? 3 : 2;
}

// WIRE FORMAT (revised 2026-08-01, per the team lead - three times, this is
// the settled version): TWO SEPARATE fields do two different jobs. Conflating
// them cost a real feature and produced a false disclosure to players once
// already - do not re-merge them.
//
// `augments` = the FULL resulting name list for the item, in slot order. This
// is WHAT THE ITEM ENDS UP WITH. dune-augment.py REPLACES the entire block
// from exactly this list (`v_stats := v_stats || <new block>` - no merge), so
// a partial list PERMANENTLY DESTROYS every augment left off it. Always
// complete, built in one place (targetAugments() in the dialog), never
// assembled ad hoc per call site.
//
// `reroll_only` = which of those slots get FRESH rolls; every slot named here
// draws new numbers, every slot NOT named here keeps its EXACT current rolls
// (proven live: byte-for-byte identical), mirroring the writer's own
// `--reroll-only <ids>` flag. This applies to BOTH modes, not just reroll:
//
//   reroll everything on [A, B, C]              augments:[A,B,C]  reroll_only: omitted
//   reroll only B on [A, B, C]                  augments:[A,B,C]  reroll_only: [B]
//   swap B for an owned augment D on [A, B, C]  augments:[A,D,C]  reroll_only: [D]
//
// The swap case matters: naming ONLY the incoming augment in `reroll_only` is
// what keeps A and C's rolls (and grades) completely untouched. Omitting
// `reroll_only` on a swap would be wrong just like omitting it on a whole-
// item reroll would be right - the field means the same thing in both modes.
//
// `grade` is ONLY meaningful for a newly swapped-in augment (the writer runs
// with --preserve-grades, so every already-installed augment - including
// every slot touched by a reroll, whole-item or single-slot - keeps its own
// grade regardless of what this field says). Omit it entirely on a reroll;
// set it to the incoming owned augment's grade on a swap.
//
// `swap_slot` / `swap_grade` carry the two things `augments` structurally
// cannot. An augment's identity is (name, GRADE) and this list holds only
// names, so swapping a Damage1 G3 for a Damage1 G5 produces a byte-identical
// `augments`:
//
//   swap B(G3) for an owned B(G5) on [A, B, C]  augments:[A,B,C]  swap_slot: 1
//                                               reroll_only: [B]  swap_grade: 5
//
// Without swap_slot the server has no way to work out which slot changed (and
// refuses, which is how this surfaced: "It won't do a swap if you're using the
// same augment, regardless of grade"). Without swap_grade it cannot tell which
// of two owned copies is being spent, and the writer consumes on (name, grade).
// Omit both on a reroll, which changes neither.
export async function submitAugmentAction({ itemId, mode, augments, rerollOnly, grade,
                                            swapSlot, swapGrade, idempotencyKey }) {
  const body = {
    item_id: itemId,
    mode,
    augments,
    idempotency_key: idempotencyKey,
    csrf_token: readCookie(CSRF_COOKIE),
  };
  if (Array.isArray(rerollOnly)) body.reroll_only = rerollOnly;
  if (grade != null) body.grade = grade;
  if (Number.isInteger(swapSlot)) body.swap_slot = swapSlot;
  if (swapGrade != null) body.swap_grade = swapGrade;
  return sendCsrfJSON('POST', ENDPOINT, body);
}
