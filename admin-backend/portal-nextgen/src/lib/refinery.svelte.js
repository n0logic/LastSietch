// Ingot Refinery store. One CHOAM service: base refined ingots plus Spice
// Melange in, the matching Spice-infused metal dust out, all of it while the
// player is logged OUT of the game.
//
// The read is shaped like home.svelte.js's: ONE timer, polling only while the
// document is visible, one immediate refetch on return, and a monotonic token
// so a slow response can never overwrite a newer one. The catalog carries
// holdings summed across bank + backpack + toolbar, which the server does in one
// pass; a tab left open behind a game window must not keep paying for that read.
//
// DARK BY DEFAULT. `enabled` false is the honest "not open yet" state and it is
// QUIET: a sealed panel, never a toast and never a success. `recipe.enabled`
// false is the same fact for one tier. Both can also arrive as a REFUSAL on the
// write (the flag can flip between the read and the confirm), and they are
// folded straight back into this state rather than announced.
//
// Nothing here resolves an account, a controller or an inventory id: the server
// owns all three. The client sends what tier it wants, how many batches, and the
// rate it was SHOWN, so a rate retuned while the page sat open is refused rather
// than silently applied.
import { api, uuidv4 } from './api.js';
import { auth } from './auth.svelte.js';

// The catalog's own cadence. The rate table is cached 60 s server-side, and the
// holdings only move when the player does something in game, so a faster poll
// would buy nothing and cost a relay round trip per tick.
const POLL_MS = 60_000;

// Ruled ceiling per request (plan section 7, question 6). Also arrives in the
// catalog's caps; this is the fallback when the read has not landed yet.
export const MAX_BATCHES = 50;

export const refinery = $state({
  status: 'idle',        // idle | loading | ready | error
  enabled: false,        // the refinery flag, as the server reports it
  // Whether the game host actually READ the player's stock. While the service is
  // dark it opens no DB session at all, so every holding is ABSENT rather than
  // zero. False is the fail-closed default: no read, no figures.
  holdingsRead: false,
  // WHY the stock could not be read, when the host said something more specific
  // than "not read": 'no_bank' | 'no_pawn_storage' | null. Absent stays null, and
  // null is not an error, it is simply no further detail.
  pawnRefusal: null,
  online: null,          // true | false | null (undetermined)
  offlineOk: false,      // refining allowed. undetermined -> false (fail closed)
  characterName: '',
  bank: null,            // {inv_id, mic, used_slots, free_slots}
  sources: null,         // {bank, backpack, toolbar: {inv_id, reachable}}
  recipes: [],           // six tiers, server order
  caps: { maxBatchesPerRequest: MAX_BATCHES, windowDays: 7 },
  rateVersion: null,

  busy: '',              // output_template being written, '' when idle
  notice: '',
  noticeTone: 'info',    // info | ok | warn | error
  noticeFor: '',         // which tier the notice belongs to
  result: null,          // last APPLIED exchange envelope (carries output_template)
});

/** Attribute a write outcome to the tier that produced it. A refinery-wide
 *  notice would sit above six rows and name none of them. */
export function setNotice(text, tone = 'info', outputTemplate = '') {
  refinery.notice = text || '';
  refinery.noticeTone = tone;
  refinery.noticeFor = outputTemplate;
}

export function clearNotice() {
  setNotice('', 'info', '');
}

// Player-facing copy per refusal token. The SERVER's own `message` wins whenever
// the envelope carried one: four of these sentences interpolate a material name,
// a held count or a shortfall, and only the server knows those numbers. This map
// is the fallback for a token that arrived bare, in a form that states nothing it
// cannot know.
//
// The keys are the PLAN's copy table, which is the client-facing contract; the
// portal route maps the writer's own vocabulary onto it, so the writer's raw
// tokens do not reach here. Four of them (insufficient_ingots,
// insufficient_melange, no_backpack, no_space) are kept anyway as a backstop, so
// a token that slips through a half-mapped deploy still gets its real sentence
// instead of the generic one. Tokens the route cannot emit at all are NOT kept:
// copy for an impossible refusal reads as documentation that the refusal exists.
// Dropped 2026-09-04 on the lead's ruling: bank_full_volume, bad_tier,
// bad_quantity.
const ERRORS = {
  bad_request: 'That refining request was malformed.',
  unknown_recipe: 'We do not refine that ingot. Refresh and try again.',
  rate_changed:
    'The refining rate changed while this page was open. Refresh to see the new rate. Nothing was taken.',
  refinery_disabled: 'The Ingot Refinery is not open yet. Nothing was taken and nothing was made.',
  recipe_disabled: 'That tier is not open yet. The other tiers still refine.',
  player_online:
    'Log out of the game first, then refine. The refinery only works on storage nobody is holding open.',
  rate_limited: 'One batch at a time, please. Wait a moment and retry.',
  weekly_cap:
    'You have refined all of that dust you can this week. Your allowance for it frees up as your last batches age past seven days. The other tiers are unaffected.',
  no_bank:
    "We could not find this character's CHOAM bank. Open the bank in-game once, then retry.",
  no_pawn_storage:
    "We could not read this character's backpack and toolbar. Refresh and try again.",
  no_backpack:
    "We could not read this character's backpack and toolbar. Refresh and try again.",
  insufficient_input:
    'Not enough of that ingot for this batch across your bank, backpack and toolbar. Nothing was taken.',
  insufficient_ingots:
    'Not enough of that ingot for this batch across your bank, backpack and toolbar. Nothing was taken.',
  insufficient_spice:
    'Not enough Spice Melange for this batch across your bank, backpack and toolbar. Nothing was taken.',
  insufficient_melange:
    'Not enough Spice Melange for this batch across your bank, backpack and toolbar. Nothing was taken.',
  bank_full:
    'Your CHOAM bank has no room for the dust. Free a slot and try again. Nothing was taken.',
  no_space:
    'Your CHOAM bank has no room for the dust. Free a slot and try again. Nothing was taken.',
  idempotency_conflict: 'That refining request was already used for a different order.',
  unresolved: 'We could not verify your in-game character right now. Please try again in a moment.',
  unavailable: 'The refinery is unavailable right now. Please try again shortly.',
  write_failed: 'The refining could not be completed. Nothing was taken. Please try again.',
};

// The two tokens that are a STATE, not an event. Both mean the door is shut;
// neither means the player did anything wrong, so they fold back into the store
// and render as the quiet panel rather than as a write outcome.
const QUIET = new Set(['refinery_disabled', 'recipe_disabled']);

// Which refusals INTERRUPT. Notice renders role="alert" for the error tone and a
// polite role="status" for every other one, so this is an accessibility decision
// as much as a colour: a refusal the player can act on (log out, free a slot,
// bring more melange, wait, retry) is a status, not an alarm. Only a refusal
// that means something on OUR side is broken gets the error tone.
//
// write_failed is deliberately NOT in here. It is one of the three soft 200s
// (with refinery_disabled and recipe_disabled): nothing was taken, and the row
// still has its stepper and its Refine button, so the retry is already offered.
const ERROR_TONE = new Set([
  'bad_request', 'unknown_recipe', 'idempotency_conflict', 'unresolved', 'unavailable',
]);

/** Notice tone for a refusal token. */
export function toneFor(token) {
  return ERROR_TONE.has(token) ? 'error' : 'warn';
}

/** Token + player-facing sentence for a thrown write. sendCsrfJSON hangs the
 *  whole envelope on err.data, which is where both live. */
export function refineryError(err) {
  const data = (err && err.data) || null;
  const token = (data && data.error) || (err && err.message) || 'write_failed';
  const served = data && typeof data.message === 'string' ? data.message.trim() : '';
  return {
    token,
    message: served || ERRORS[token] || ERRORS.write_failed,
    quiet: QUIET.has(token),
  };
}

/** The sentence a tier row shows in place of its holdings when the stock was not
 *  read. Two of the three cases are things the player can act on, and the copy
 *  for both already exists in ERRORS above: the same refusal can arrive as a
 *  WRITE failure, and the plan gives one sentence per token, so reusing it is
 *  what stops the read path and the write path drifting into two wordings of the
 *  same fact. Anything unrecognised, null included, gets the generic line: we
 *  know the stock is unread and nothing more, and inventing a cause would be the
 *  same class of fabrication as inventing a zero. */
export function unreadReason() {
  const t = refinery.pawnRefusal;
  if (t === 'no_bank' || t === 'no_pawn_storage') return ERRORS[t];
  return 'We could not read your stock right now. Refresh in a moment.';
}

/** How many batches this tier can actually take right now. The catalog already
 *  computes it server-side under the same arithmetic; this is the floor the
 *  stepper clamps to, and it stays advisory either way because the writer
 *  recomputes everything under locks. */
export function maxBatchesFor(recipe) {
  // No read, no ceiling. Returning 0 here is what disables the stepper and the
  // confirm; it is never PRINTED as an affordable count (the row hides that line
  // entirely when the stock is unread) because 0 would be a claim we cannot make.
  if (!refinery.holdingsRead) return 0;
  const cap = Number(refinery.caps.maxBatchesPerRequest) || MAX_BATCHES;
  const afford = Number(recipe?.max_batches_affordable);
  if (!Number.isFinite(afford) || afford <= 0) return 0;
  return Math.max(0, Math.min(cap, Math.floor(afford)));
}

/** The rate line a tier reads out, and the exact rate the confirm echoes back.
 *  Built once so the sentence the player agreed to and the fields the server
 *  checks cannot drift apart. */
export function rateOf(recipe) {
  return {
    input_per_batch: Number(recipe?.input_per_batch) || 0,
    spice_per_batch: Number(recipe?.spice_per_batch) || 0,
    output_per_batch: Number(recipe?.output_per_batch) || 0,
  };
}

function num(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

/** A figure we may not have. Anything non-numeric stays NULL rather than becoming
 *  0, because a stock we could not read rendered as zero is a fabricated
 *  statement about the player's own account. Same rule home.svelte.js's header
 *  sets out, and the reason no read value in this file ever gets `?? 0`.
 *
 *  This also fails VISIBLY rather than quietly if the wire shape changes: an
 *  object arriving where a scalar was promised becomes null and the row says it
 *  could not read, instead of printing a confident 0 on all six tiers. That is
 *  exactly how lane B's held_output object bug would have surfaced. */
function numOrNull(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Normalise one per-source holdings block. `read` is the catalog's holdings_read:
 *  when it is false the host opened no DB session, so these are ABSENT and every
 *  source stays null. A genuinely unreachable toolbar on a real read is still 0,
 *  which is a different fact and keeps its 0. */
function holdings(h, read) {
  if (!read) return { bank: null, backpack: null, toolbar: null, total: null };
  return {
    bank: numOrNull(h?.bank),
    backpack: numOrNull(h?.backpack),
    toolbar: numOrNull(h?.toolbar),
    total: numOrNull(h?.total),
  };
}

function normaliseRecipe(r, read) {
  return {
    tier: num(r?.tier),
    output_template: r?.output_template || '',
    output_name: r?.output_name || '',
    output_icon: r?.output_icon || '',
    input_template: r?.input_template || '',
    input_name: r?.input_name || '',
    input_icon: r?.input_icon || '',
    spice_template: r?.spice_template || '',
    spice_name: r?.spice_name || 'Spice Melange',
    spice_icon: r?.spice_icon || '',
    input_per_batch: num(r?.input_per_batch),
    spice_per_batch: num(r?.spice_per_batch),
    output_per_batch: num(r?.output_per_batch),
    max_stack: num(r?.max_stack),
    held_input: holdings(r?.held_input, read),
    held_spice: holdings(r?.held_spice, read),
    held_output: read ? numOrNull(r?.held_output) : null,
    max_batches_affordable: read ? numOrNull(r?.max_batches_affordable) : null,
    weekly_dust_cap: num(r?.weekly_dust_cap),
    weekly_dust_used: num(r?.weekly_dust_used),
    weekly_dust_left: num(r?.weekly_dust_left),
    enabled: r?.enabled === true,
  };
}

// Monotonic token. A tab regaining focus fires an immediate refetch while the
// previous request is still open; whichever lands second must not win on age.
let seq = 0;

/** The one read. Session-gated and linked-only: it answers 401 for anyone else,
 *  and a 401 per minute in the network log is noise rather than a signal. */
export async function loadCatalog() {
  if (auth.status !== 'authed') {
    refinery.status = 'idle';
    return;
  }
  const s = ++seq;
  if (refinery.status === 'idle') refinery.status = 'loading';
  try {
    const r = await api.refinery.catalog();
    if (s !== seq) return;
    refinery.enabled = r?.enabled === true;
    refinery.online = typeof r?.online === 'boolean' ? r.online : null;
    refinery.offlineOk = r?.offline_ok === true;
    refinery.characterName = r?.character_name || '';
    // Set BEFORE the recipes: every held figure below is normalised through it.
    refinery.holdingsRead = r?.holdings_read === true;
    // Stored RAW, not validated against a token list: an unrecognised value is
    // handled where the sentence is chosen, which falls back to the generic line.
    refinery.pawnRefusal = r?.pawn_refusal ?? null;
    // The bank block carries the output room (free_slots), so it is a holding
    // like any other and goes when the read did not happen.
    refinery.bank = refinery.holdingsRead ? (r?.bank || null) : null;
    refinery.sources = refinery.holdingsRead ? (r?.sources || null) : null;
    refinery.recipes = Array.isArray(r?.recipes)
      ? r.recipes.map((x) => normaliseRecipe(x, refinery.holdingsRead))
      : [];
    refinery.caps = {
      maxBatchesPerRequest: num(r?.caps?.max_batches_per_request) || MAX_BATCHES,
      windowDays: num(r?.caps?.window_days) || 7,
    };
    refinery.rateVersion = r?.rate_version ?? null;
    refinery.status = 'ready';
  } catch (e) {
    if (s !== seq) return;
    // A failed read SEALS the panel. It does not render as a closed refinery:
    // "not open yet" is a statement about the service, and we do not know that.
    refinery.status = 'error';
  }
}

// One idempotency key per LOGICAL refine, held here rather than in the row so it
// outlives a re-render or a closed dialog. Keyed on the tier AND the batch count
// so changing the number is a different order, not a retry of the old one.
//
// REVIEWER M3, 2026-09-04. "The server answered" is NOT the test for retiring the
// key, and using it was a real double-refine hole. write_failed (200),
// unresolved (502), unavailable (503) and writer_no_output all ANSWER, and all
// four are exactly the outcomes where the writer may already have committed:
// the relay timed out, the response was lost, the envelope never came back. The
// old `e.status != null` test retired the key on every one of them, so the retry
// minted a SECOND uuid and refined again, spending the ingots twice.
//
// The test is now "does this outcome PROVE nothing was taken". Only then may a
// retry be a new order. Everything else keeps the key so the retry REPLAYS
// against the writer's ledger and can only ever apply once.
//
// Fails closed by construction: an unrecognised token is not in the set, so it
// keeps the key. That covers the network throw with no token at all, and it
// covers the auth refusals, which arrive as FastAPI detail strings ("Invalid
// CSRF token") rather than as tokens, so they never match by name. Keeping a key
// we did not need to keep costs one replay; retiring one we needed costs the
// player their ingots.
const NOTHING_TAKEN = new Set([
  // Gate refusals: evaluated before any DB session opens.
  'refinery_disabled', 'recipe_disabled', 'unknown_recipe', 'rate_changed',
  'bad_request', 'player_online', 'rate_limited', 'weekly_cap',
  // Pre-write resolution and stock checks: refused before the transaction.
  'no_bank', 'no_pawn_storage', 'insufficient_input', 'insufficient_spice',
  // Output-room refusals, both pre-write. bank_full_volume was here too, on the
  // reasoning that a spare name costs nothing in a membership test; dropped on
  // the reviewer's round 2, because plan 7b says that spelling exists nowhere and
  // a token nothing can emit reads as evidence that something does.
  'bank_full', 'bank_volume',
  // The writer proved this key belongs to a DIFFERENT order, so this one never ran.
  'idempotency_conflict',
  // Named by the ruling. They arrive as detail strings, not tokens, so in practice
  // they fall through to the fail-closed keep; listed so the intent is on record.
  'csrf', 'unauthenticated',
]);

const INTENT_MAX = 12;
const intents = new Map();

/** Mint, or recover, the intent for one logical order.
 *
 *  The intent holds the WHOLE ORDER, not a bare uuid: the key AND the terms it
 *  was built against. That is what makes a retry a retry.
 *
 *  M3 and L5 compose badly without this. M3 keeps the uuid across write_failed,
 *  unavailable, unresolved and writer_no_output so a retry REPLAYS rather than
 *  refining twice. L5 makes the body echo the rate. But the body used to be
 *  rebuilt from the LIVE store on every submit, and the catalog poll runs every
 *  60 s, so a rate retune landing between a failed write and its retry sent the
 *  ORIGINAL uuid with the NEW terms. The writer's replay row raises
 *  idempotency_conflict on any changed parameter, so the player got "already used
 *  for a different order" when what actually happened is the rate moved under
 *  them. Nothing was ever double spent, but that is the wrong sentence to hand
 *  someone who is already unsure whether their ingots are gone.
 *
 *  Frozen terms make both outcomes honest instead. Same uuid and same terms
 *  replays cleanly and republishes the original result; if the table really did
 *  move and the original never committed, the writer's own rate check answers
 *  rate_changed, which says exactly what happened.
 *
 *  A NEW order always takes the live store, because it gets a new key: the key
 *  carries the tier and the batch count, and a retired intent mints afresh. */
function intentFor(key, terms) {
  const prior = intents.get(key);
  if (prior) return prior;
  if (intents.size >= INTENT_MAX) {
    // A Map iterates in insertion order, so the first key is the oldest.
    intents.delete(intents.keys().next().value);
  }
  const order = { uuid: uuidv4(), terms };
  intents.set(key, order);
  return order;
}

/** Refine `batches` of one tier. Echoes back the whole rate the row was showing
 *  so the server can refuse a retune rather than apply it silently. Returns true
 *  only when the dust was actually minted. */
export async function submitExchange(recipe, batches) {
  const n = Math.floor(Number(batches) || 0);
  const ceiling = maxBatchesFor(recipe);
  if (!recipe?.output_template || n < 1 || n > ceiling) return false;
  if (refinery.busy) return false;

  const key = `${recipe.output_template}:${n}`;
  const rate = rateOf(recipe);
  // Captured ONCE, at mint time. Every later attempt on this key sends these,
  // never whatever the poll has since put in the store. See intentFor.
  const order = intentFor(key, {
    expected_input_template: recipe.input_template,
    expected_spice_template: recipe.spice_template,
    expected_input_per_batch: rate.input_per_batch,
    expected_spice_per_batch: rate.spice_per_batch,
    expected_output_per_batch: rate.output_per_batch,
    expected_rate_version: refinery.rateVersion,
  });
  refinery.busy = recipe.output_template;
  clearNotice();
  try {
    const r = await api.refinery.exchange({
      // WHICH order: the tier and the size. These are the intent key itself, so
      // they cannot drift from it.
      output_template: recipe.output_template,
      batches: n,
      // ON WHAT TERMS: the five figures the row PRINTED plus, per reviewer L5,
      // the rate VERSION, which covers what the table carries but the row does
      // not print (max_stack, the weekly cap, the window). A retune touching only
      // those is invisible to a per-batch comparison, so without the version a
      // page that sat open would spend against the old terms.
      //
      // Read from the INTENT, never from the live store, so a retry echoes what
      // the order was built against. That is the whole point of freezing them.
      ...order.terms,
      uuid: order.uuid,
    });
    intents.delete(key);
    refinery.result = r || null;
    setNotice(r?.message || '', 'ok', recipe.output_template);
    // The write moved holdings, the weekly counter and the bank's free slots.
    // Re-read rather than patching six numbers from a response shaped for a
    // different job.
    loadCatalog();
    return true;
  } catch (e) {
    const { token, message, quiet } = refineryError(e);
    // Retire ONLY on an outcome that proves the writer did not commit. Anything
    // else, answered or not, keeps the key so the retry replays. See NOTHING_TAKEN.
    if (NOTHING_TAKEN.has(token)) intents.delete(key);
    if (quiet) {
      // Not a write outcome: the door shut between the read and the confirm.
      // Fold it back into the state the panel already renders honestly.
      if (token === 'refinery_disabled') refinery.enabled = false;
      else {
        const hit = refinery.recipes.find((x) => x.output_template === recipe.output_template);
        if (hit) hit.enabled = false;
      }
      clearNotice();
      return false;
    }
    setNotice(message, toneFor(token), recipe.output_template);
    if (token === 'rate_changed') loadCatalog();
    return false;
  } finally {
    refinery.busy = '';
  }
}

/** Drop the last result so a row goes back to its stepper. */
export function clearResult() {
  refinery.result = null;
}

// ---- The shared visibility-gated poll -------------------------------------
let subscribers = 0;
let listening = false;
let timer = null;

function visible() {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

function stopTimer() {
  if (timer != null) { clearInterval(timer); timer = null; }
}

function startTimer() {
  if (subscribers === 0 || timer != null) return;
  timer = setInterval(() => { if (visible()) loadCatalog(); }, POLL_MS);
}

function onVisibility() {
  if (subscribers === 0) return;
  if (visible()) { loadCatalog(); startTimer(); }
  else stopTimer();
}

/** Subscribe a surface to the catalog poll. Returns the unsubscribe function so
 *  a caller can hand it straight back from onMount. The last subscriber to leave
 *  tears down the timer and the visibility listener. */
export function subscribe() {
  subscribers += 1;
  if (!listening && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility);
    listening = true;
  }
  if (visible()) { loadCatalog(); startTimer(); }
  return unsubscribe;
}

/** Drop one subscriber. Idempotent, and safe to call when none are left. */
export function unsubscribe() {
  subscribers = Math.max(0, subscribers - 1);
  if (subscribers > 0) return;
  stopTimer();
  if (listening && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', onVisibility);
    listening = false;
  }
}
