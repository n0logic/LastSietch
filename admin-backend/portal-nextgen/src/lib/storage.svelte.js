// Shared Storage state. One $state proxy imported by the route + every storage
// component so the bank, the container rail, and the item grid stay in sync
// without prop-drilling. Mutate the proxy's PROPERTIES in place (never reassign
// `storage` itself), so the shared reference every component holds stays live.
//
// Reads are never gated (browsing always works). Writes are optimistic:
// mutate locally, fire the CSRF write, and on any throw ({ok:false} / non-2xx)
// revert to the pre-write snapshot. `offlineOk` mirrors the server offline gate
// (undetermined = LOCKED, fail-closed); the server hard-gates regardless, so the
// UI lock is a courtesy, not the security boundary.
import { api, uuidv4 } from './api.js';

export const storage = $state({
  status: 'idle',        // idle | loading | ready | error
  online: null,          // player online status: true | false | null(undetermined)
  offlineOk: false,      // writes allowed (offline confirmed). undetermined -> false
  withdrawCap: 100000,   // _STORAGE_WITHDRAW_CAP mirror

  bank: null,            // { solari, inv_id, mic, miv, items[] }
  backpack: null,        // selected character: { inv_id, mic, miv, items[] }
  containers: [],        // [{ id, name, type, location, item_count, mic, miv, cached, glb? }]

  selectedId: null,      // currently opened container id
  partsRefreshToken: 0,  // bumped after a durability write so the vehicle parts panel re-fetches
  items: [],             // items of the selected container (SPARSE position_index)
  itemsMic: 0,
  itemsMiv: 0,
  itemsStatus: 'idle',   // idle | loading | ready | error

  search: { q: '', hits: [], status: 'idle' }, // cross-container locator
  highlight: null,       // { containerId, itemId } jump-to target from the locator
  moveEnabled: false,    // LASTSIETCH_STORAGE_MOVE_ENABLED mirror (server is authoritative)
  pawnMoveEnabled: false,// Bank and Backpack lane, separately gated and server authoritative
  marketBackpackEnabled: false, // Exchange listings sourced from the backpack (own kill switch)
  transferEnabled: false,// LASTSIETCH_ITEM_TRANSFER_ENABLED mirror (Tier 5 DARK when false)
  transferDailyCap: 30,  // caps.transfer_daily_cap mirror (writer XFER_MAX_PER_DAY)
  transferDailyUsed: 0,  // caps.transfer_daily_used mirror, identity-collapsed
  transferPairCap: 15,   // caps.transfer_pair_cap mirror (writer XFER_MAX_PER_PAIR_PER_DAY)
  transfers: [],         // both-directions history [{t, direction, item, grade, counterparty, status}]
  transfersStatus: 'idle', // idle | loading | ready | empty | error
  notice: '',            // transient result line (write outcomes)
  noticeTone: 'info',    // info | ok | warn | error
  noticeSeq: 0,          // bumps per notice so an identical line re-announces/replays
});

/** Loose id compare: rail container ids and the bank inv_id cross number/string
 *  boundaries (drag payload -> JSON -> back), so never compare them with ===. */
function sameId(a, b) {
  return a != null && b != null && String(a) === String(b);
}

function bankInvId() {
  return storage.bank?.inv_id ?? null;
}

function backpackInvId() {
  return storage.backpack?.inv_id ?? null;
}

function isBankId(id) {
  return sameId(id, bankInvId());
}

function isBackpackId(id) {
  return sameId(id, backpackInvId());
}

function pawnStorageKind(id) {
  if (isBankId(id)) return 'bank';
  if (isBackpackId(id)) return 'backpack';
  return '';
}

function containerName(id) {
  if (isBankId(id)) return 'the bank';
  if (isBackpackId(id)) return 'your backpack';
  const c = storage.containers.find((x) => sameId(x.id, id));
  return c?.name || '';
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Selected container object (or null). */
export function selectedContainer() {
  // sameId, not ===: container ids cross the number/string boundary through the
  // drag payload + click handlers, so a strict compare silently returns null and
  // the panel claims nothing is selected while its grid is full.
  return storage.containers.find((c) => sameId(c.id, storage.selectedId)) || null;
}

let noticeTimer = null;

/** Set the transient write-outcome line. `autoDismissMs` > 0 clears it again on a
 *  timer: success ('ok') lines are transient confirmations, while warn/error lines
 *  stay put until the next write so the player can read the refusal. */
function setNotice(text, tone = 'info', autoDismissMs = 0) {
  if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
  storage.notice = text || '';
  storage.noticeTone = tone;
  storage.noticeSeq += 1;
  if (text && autoDismissMs > 0) {
    noticeTimer = setTimeout(() => {
      noticeTimer = null;
      storage.notice = '';
    }, autoDismissMs);
  }
}

const OK_NOTICE_MS = 2500;
const MOVE_INTENT_TTL_MS = 60000;
const moveIntentKeys = new Map();

function moveIntentKey(itemId, srcStorage, dstStorage, expectedTemplate) {
  return `${itemId}:${srcStorage}:${dstStorage}:${expectedTemplate || ''}`;
}

function moveIntentUuid(key) {
  const now = Date.now();
  const prior = moveIntentKeys.get(key);
  if (prior && prior.expiresAt > now) return prior.uuid;
  const uuid = uuidv4();
  moveIntentKeys.set(key, { uuid, expiresAt: now + MOVE_INTENT_TTL_MS });
  return uuid;
}

/** Boot: pull the overview (gate + bank + containers + caps) in one request. */
export async function loadOverview() {
  storage.status = 'loading';
  try {
    const r = await api.storage.overview();
    storage.online = typeof r?.online === 'boolean' ? r.online : null;
    storage.offlineOk = r?.offline_ok === true;
    storage.withdrawCap = Number(r?.caps?.withdraw_cap) || 100000;
    storage.moveEnabled = r?.flags?.move_enabled === true;
    storage.pawnMoveEnabled = r?.flags?.pawn_move_enabled === true;
    storage.marketBackpackEnabled = r?.flags?.market_sell_backpack_enabled === true;
    storage.transferEnabled = r?.flags?.transfer_enabled === true;
    storage.transferDailyCap = Number(r?.caps?.transfer_daily_cap) || 30;
    storage.transferDailyUsed = Number(r?.caps?.transfer_daily_used) || 0;
    storage.transferPairCap = Number(r?.caps?.transfer_pair_cap) || 15;
    storage.bank = r?.bank || null;
    storage.backpack = r?.backpack || null;
    storage.containers = Array.isArray(r?.containers) ? r.containers : [];
    // Bank is already visible on the left, so open Backpack on the right when it
    // exists. Also replace a stale selected pawn inventory after a character switch.
    const selectedStillVisible = storage.containers.some((c) => sameId(c.id, storage.selectedId));
    if ((!selectedStillVisible || storage.selectedId == null) && storage.containers.length > 0) {
      const initial = storage.containers.find((c) => c.is_backpack) || storage.containers[0];
      selectContainer(initial.id);
    }
    storage.status = 'ready';
  } catch (e) {
    storage.status = 'error';
    storage.bank = null;
    storage.backpack = null;
    storage.containers = [];
  }
}

/** Select a container and (lazily) load its item grid. */
export async function selectContainer(id) {
  storage.selectedId = id;
  storage.itemsStatus = 'loading';
  storage.items = [];
  try {
    const r = await api.storage.containerItems(id);
    // Guard against a stale response if the user clicked away mid-flight.
    if (storage.selectedId !== id) return;
    storage.items = Array.isArray(r?.items) ? r.items : [];
    storage.itemsMic = Number(r?.mic) || 0;
    storage.itemsMiv = Number(r?.miv) || 0;
    storage.itemsStatus = 'ready';
  } catch (e) {
    if (storage.selectedId !== id) return;
    storage.items = [];
    storage.itemsStatus = 'error';
  }
}

/** Cross-container locator search. Empty query clears the hit list. */
export async function runSearch(q) {
  storage.search.q = q;
  if (!q || !q.trim()) {
    storage.search.hits = [];
    storage.search.status = 'idle';
    return;
  }
  storage.search.status = 'loading';
  try {
    const r = await api.storage.search(q.trim());
    storage.search.hits = Array.isArray(r?.hits) ? r.hits : [];
    storage.search.status = 'ready';
  } catch (e) {
    storage.search.hits = [];
    storage.search.status = 'error';
  }
}

/** Jump the right panel to a container the locator surfaced and flag the matching
 *  template so the grid pulses every cell holding it (search groups by template,
 *  not by a single item row, so we highlight by template). */
export async function jumpToContainer(containerId, template) {
  storage.highlight = { containerId, template };
  if (storage.selectedId !== containerId) await selectContainer(containerId);
}

/** Refetch the overview (gate + bank grid + container rail counts). Keeps the
 *  optimistic view on any throw; a later load fixes it. Exported so a write that
 *  moves the bank balance from OUTSIDE this store (e.g. a Solari gift, which
 *  posts through the guild/gift relay path, not storageWithdraw/Deposit) can
 *  still bring ChoamBankCard's balance back in sync without a full reload. */
export async function refreshOverview() {
  try {
    const r = await api.storage.overview();
    storage.bank = r?.bank || storage.bank;
    storage.backpack = r?.backpack || storage.backpack;
    storage.containers = Array.isArray(r?.containers) ? r.containers : storage.containers;
    storage.online = typeof r?.online === 'boolean' ? r.online : storage.online;
    storage.offlineOk = r?.offline_ok === true;
    storage.pawnMoveEnabled = r?.flags?.pawn_move_enabled === true;
    storage.transferEnabled = r?.flags?.transfer_enabled === true;
    storage.transferDailyUsed = Number(r?.caps?.transfer_daily_used) || 0;
    return true;
  } catch (e) {
    return false;
  }
}

/** Fetch ONE container's grid and return its items. Unlike selectContainer this
 *  never blanks the grid first (the old rows stay up until the new ones land, so
 *  a post-write refetch does not flash empty), and it only writes into the open-
 *  grid state while that container is still the selected one. */
async function fetchContainerItems(id) {
  try {
    const r = await api.storage.containerItems(id);
    const items = Array.isArray(r?.items) ? r.items : [];
    if (sameId(storage.selectedId, id)) {
      storage.items = items;
      storage.itemsMic = Number(r?.mic) || 0;
      storage.itemsMiv = Number(r?.miv) || 0;
      storage.itemsStatus = 'ready';
    }
    return items;
  } catch (e) {
    if (sameId(storage.selectedId, id)) storage.itemsStatus = 'error';
    return null;
  }
}

/** Refetch the bank + the open container after a write so the UI reconciles with
 *  server truth (positions/merges the optimistic path only approximated). */
async function reconcile() {
  await refreshOverview();
  if (storage.selectedId != null) await fetchContainerItems(storage.selectedId);
}

// Container reads are mirror-first server-side (mirror snapshot, cached relay
// fallback), so the grid can still answer with a PRE-move snapshot immediately
// after the writer commits. Re-pull both ends until the moved row actually shows
// up in the destination, backing off between passes. First pass is immediate, so
// a fresh read model costs nothing extra.
const MOVE_SETTLE_DELAYS = [0, 400, 900, 1600];

/** True once the destination read reports the moved item. The bank destination is
 *  served by the overview envelope (inv30 grid), not the container-items route. */
function movedItemLanded(itemId, dstContainerId, probed) {
  if (isBankId(dstContainerId)) {
    const bi = storage.bank?.items;
    return Array.isArray(bi) && bi.some((it) => it.item_id === itemId);
  }
  if (isBackpackId(dstContainerId)) {
    const pi = storage.backpack?.items;
    if (Array.isArray(pi) && pi.some((it) => it.item_id === itemId)) return true;
  }
  const hit = probed.find(([id]) => sameId(id, dstContainerId));
  return !!(hit && Array.isArray(hit[1]) && hit[1].some((it) => it.item_id === itemId));
}

/** Reconcile BOTH ends of a move: overview (bank grid + rail counts) plus the
 *  source and destination item grids. Returns false when the destination never
 *  reported the item within the settle window (read model lagging the write). */
async function settleMove(itemId, srcContainerId, dstContainerId) {
  for (let i = 0; i < MOVE_SETTLE_DELAYS.length; i++) {
    if (MOVE_SETTLE_DELAYS[i]) await sleep(MOVE_SETTLE_DELAYS[i]);
    await refreshOverview();
    const ids = [dstContainerId, srcContainerId].filter(
      (id, idx, arr) => id != null && !isBankId(id) && arr.indexOf(id) === idx
    );
    const probed = await Promise.all(
      ids.map((id) => fetchContainerItems(id).then((items) => [id, items]))
    );
    if (movedItemLanded(itemId, dstContainerId, probed)) return true;
  }
  return false;
}

/** Withdraw Solari from the bank credit into a coin stack. Optimistic on the
 *  bank balance; reconciles the coin stack + positions from the server. */
export async function withdraw(amount) {
  const n = Math.floor(Number(amount)) || 0;
  if (n <= 0) { setNotice('Enter a positive amount.', 'warn'); return false; }
  if (n > storage.withdrawCap) {
    setNotice(`Over the ${storage.withdrawCap.toLocaleString()} Solari cap for one transfer.`, 'warn');
    return false;
  }
  if (!storage.offlineOk) { setNotice('Log out of the game to move Solari.', 'warn'); return false; }
  const prev = storage.bank ? storage.bank.solari : 0;
  if (storage.bank) storage.bank.solari = Math.max(0, prev - n);
  try {
    const r = await api.storage.withdraw({ amount: n, uuid: uuidv4() });
    if (storage.bank && typeof r?.bank_after === 'number') storage.bank.solari = r.bank_after;
    setNotice(`Withdrew ${n.toLocaleString()} Solari.`, 'ok', OK_NOTICE_MS);
    await reconcile();
    return true;
  } catch (e) {
    if (storage.bank) storage.bank.solari = prev; // rollback
    setNotice(friendly(e, 'The withdrawal did not go through.'), 'error');
    return false;
  }
}

/** Deposit coins into the bank credit. mode 'sweep' banks every owned coin;
 *  'amount' banks a specific total. Optimistic add to the balance. */
export async function deposit(mode, amount) {
  if (!storage.offlineOk) { setNotice('Log out of the game to move Solari.', 'warn'); return false; }
  const n = mode === 'amount' ? (Math.floor(Number(amount)) || 0) : 0;
  if (mode === 'amount' && n <= 0) { setNotice('Enter a positive amount.', 'warn'); return false; }
  const prev = storage.bank ? storage.bank.solari : 0;
  if (mode === 'amount' && storage.bank) storage.bank.solari = prev + n;
  try {
    const body = mode === 'amount'
      ? { mode: 'amount', amount: n, uuid: uuidv4() }
      : { mode: 'sweep', uuid: uuidv4() };
    const r = await api.storage.deposit(body);
    if (storage.bank && typeof r?.bank_after === 'number') storage.bank.solari = r.bank_after;
    const swept = typeof r?.swept_total === 'number' ? r.swept_total : n;
    setNotice(`Deposited ${swept.toLocaleString()} Solari.`, 'ok', OK_NOTICE_MS);
    await reconcile();
    return true;
  } catch (e) {
    if (storage.bank) storage.bank.solari = prev; // rollback
    setNotice(friendly(e, 'The deposit did not go through.'), 'error');
    return false;
  }
}

/** Move a whole item stack into a destination container (own-account only).
 *  Optimistically removes the item from the current grid, then reconciles both
 *  inventories from the server. Fail-closed: any throw reverts the grid. */
export async function moveItem(itemId, dstContainerId, srcContainerId, expectedTemplate) {
  if (!storage.offlineOk) { setNotice('Log out of the game to reorganise storage.', 'warn'); return false; }
  if (!storage.pawnMoveEnabled) { setNotice('Bank and Backpack transfers are not enabled yet.', 'warn'); return false; }
  if (sameId(dstContainerId, srcContainerId)) return false; // no-op self-drop
  const srcStorage = pawnStorageKind(srcContainerId);
  const dstStorage = pawnStorageKind(dstContainerId);
  if (!srcStorage || !dstStorage || srcStorage === dstStorage) {
    setNotice('Only Bank and Backpack can exchange items.', 'warn');
    return false;
  }
  // Optimistically remove from whichever grid currently holds the item (the open
  // container grid or the bank inv30 grid); settleMove() refetches both ends after.
  const bankItems = storage.bank && Array.isArray(storage.bank.items) ? storage.bank.items : null;
  const ci = storage.items.findIndex((it) => it.item_id === itemId);
  const bi = bankItems ? bankItems.findIndex((it) => it.item_id === itemId) : -1;
  let snapshot = null, from = null;
  if (ci >= 0) { snapshot = storage.items[ci]; from = 'container'; storage.items.splice(ci, 1); }
  else if (bi >= 0) { snapshot = bankItems[bi]; from = 'bank'; bankItems.splice(bi, 1); }
  const intentKey = moveIntentKey(itemId, srcStorage, dstStorage, expectedTemplate);
  try {
    const body = {
      item_id: itemId,
      expected_source: srcStorage,
      destination: dstStorage,
      uuid: moveIntentUuid(intentKey),
    };
    if (expectedTemplate) body.expected_template = expectedTemplate;
    await api.storage.pawnMove(body);
    const where = containerName(dstContainerId);
    setNotice(where ? `Moved to ${where}.` : 'Moved.', 'ok', OK_NOTICE_MS);
    // Show the landing. A drop onto a rail tile targets a container that is not
    // open, so without this the item vanishes from the source grid and is never
    // seen again until the player hunts for it. The bank is exempt: its grid is
    // always on screen, so switching the right panel to it would be noise.
    if (!isBankId(dstContainerId) && !sameId(storage.selectedId, dstContainerId)) {
      storage.selectedId = dstContainerId;
      storage.items = [];
      storage.itemsStatus = 'loading'; // settleMove's first pass fills it
    }
    const landed = await settleMove(itemId, srcContainerId, dstContainerId);
    if (!landed) {
      setNotice(
        `${where ? `Moved to ${where}.` : 'Moved.'} The grid is still catching up.`,
        'ok', OK_NOTICE_MS
      );
    }
    moveIntentKeys.delete(intentKey);
    return true;
  } catch (e) {
    // rollback into the array we pulled it from
    if (snapshot && from === 'container') storage.items.splice(ci, 0, snapshot);
    else if (snapshot && from === 'bank' && bankItems) bankItems.splice(bi, 0, snapshot);
    // pawn_move_disabled + player_online are honest expected refusals,
    // not failures: surface them as a warn, not an error toast.
    const token = (e && e.message) || '';
    const soft = token === 'pawn_move_disabled' || token === 'player_online';
    setNotice(moveError(e), soft ? 'warn' : 'error');
    return false;
  }
}

/** Map a MOVE error token to friendly copy (the writer's dst-gate vocabulary). */
function moveError(e) {
  const t = (e && e.message) || '';
  const map = {
    pawn_move_disabled: 'Bank and Backpack transfers are not enabled yet. Nothing moved.',
    player_online: 'Log out of the game to reorganise storage. Nothing moved.',
    dst_no_slots: 'That container holds no stack slots.',
    dst_full_slots: 'That container is full.',
    dst_full_volume: 'That container has no room for this stack.',
    no_bank: 'This character has no CHOAM bank available.',
    no_backpack: 'This character has no backpack available.',
    idempotency_conflict: 'That move request was already used for something else.',
    not_owner: 'That item is not in this character’s Bank or Backpack.',
    item_not_found: 'That item is no longer there.',
    move_failed: 'The move did not complete. Nothing moved.',
  };
  return map[t] || 'The move did not complete. Nothing moved.';
}

// One idempotency key per LOGICAL transfer, kept per TARGET (the item plus the
// recipient it is being sent to) and held here rather than inside the dialog:
// the send popover can be closed and reopened mid-flight, and a key that died
// with the component would turn the retry into a SECOND transfer. Re-minted
// only once the server has ANSWERED -- applied, replay, deferred and every
// refusal are all definitive -- and NEVER after a timeout or a dropped
// connection, where the write may still be in flight. sendCsrfJSON sets
// `e.status` only when a response actually came back, so that is the test for
// "the server answered"; a network throw carries none.
//
// A Map rather than the one slot this used to be: with a single slot a second
// send to a different target overwrote the first one's key, so retrying the
// first minted a fresh key the writer's ledger could not replay. It could not
// double-send (the writer re-homes a single row and re-verifies ownership), but
// it burned another daily and pair cap slot for the 180 s replay window.
//
// NO TTL here, unlike moveIntentKeys above. This key exists precisely to
// survive an unanswered write, and a timeout has no upper bound the client can
// know, so an expiry would re-mint in exactly the case the key is for. The map
// is bounded instead: at TRANSFER_INTENT_MAX the oldest insertion is evicted.
const TRANSFER_INTENT_MAX = 20;
const transferIntentKeys = new Map();

function transferIntentUuid(target) {
  const prior = transferIntentKeys.get(target);
  if (prior) return prior;
  if (transferIntentKeys.size >= TRANSFER_INTENT_MAX) {
    // A Map iterates in insertion order, so the first key is the oldest.
    transferIntentKeys.delete(transferIntentKeys.keys().next().value);
  }
  const uuid = uuidv4();
  transferIntentKeys.set(target, uuid);
  return uuid;
}

function endTransferIntent(target) {
  transferIntentKeys.delete(target);
}

/** Wave 7 refusal copy. Every other token keeps the server's own line
 *  (_TRANSFER_ERROR_TEXT), which is the text players already read. */
const TRANSFER_REFUSAL_TEXT = {
  bad_code: 'A code is eight characters, A to Z and 2 to 9. Nothing was moved.',
  code_unknown: 'No player carries that code now. It may have been rotated. Nothing was moved.',
  lookup_throttled: 'Too many code lookups just now. Wait a moment, then try again.',
  linked_alt: 'That code belongs to an account linked to yours. Nothing was moved.',
  self_transfer: 'That is your own code. Nothing was moved.',
  recipient_unlinked: 'No player by that name has opened a bank.',
};

function transferError(e) {
  const token = (e && e.message) || '';
  return TRANSFER_REFUSAL_TEXT[token]
    || e?.data?.message
    || 'The transfer did not complete. Nothing was moved.';
}

/** Send ONE whole stack out of the sender's own bank to another player. The
 *  recipient is EXACTLY ONE of `code` (an identity code, re-resolved server-side
 *  on the write) or `charName`; neither ever appears in a URL. Returns
 *  {phase, message} with phase 'sent' | 'deferred' | 'failed'. 'deferred' means
 *  a flag is off and NOTHING moved, so a caller must never render it as a send. */
export async function sendTransfer({ itemId, template = '', code = '', charName = '' }) {
  const target = `${itemId}:${code || charName}`;
  const body = { item_id: itemId, uuid: transferIntentUuid(target) };
  if (code) body.recipient_code = code;
  else body.recipient_char_name = charName;
  if (template) body.expected_template = template;
  try {
    const r = await api.storage.transfer(body);
    endTransferIntent(target);
    const status = r?.status || (r?.ok ? 'applied' : 'failed');
    if (status === 'deferred') {
      return { phase: 'deferred',
               message: r?.message || 'Item transfer is paused right now. Nothing was moved.' };
    }
    if (status === 'applied' || status === 'replay') {
      await refreshOverview();
      return { phase: 'sent', message: r?.message || '' };
    }
    return { phase: 'failed', message: r?.message || 'The transfer did not complete.' };
  } catch (e) {
    // A refusal IS an answer: end the logical send so the next submit is a new
    // one. A timeout is not, so the key survives and the retry replays.
    if (typeof e?.status === 'number') endTransferIntent(target);
    return { phase: 'failed', message: transferError(e) };
  }
}

/** Both-directions transfer history for every account on this session. */
export async function loadTransfers(limit = 50) {
  storage.transfersStatus = 'loading';
  try {
    const r = await api.storage.transfers({ limit });
    storage.transfers = Array.isArray(r?.rows) ? r.rows : [];
    storage.transfersStatus = storage.transfers.length ? 'ready' : 'empty';
  } catch (e) {
    storage.transfers = [];
    storage.transfersStatus = 'error';
  }
}

function friendly(e, fallback) {
  const t = (e && e.message) || '';
  if (t === 'player_online') return 'Log out of the game first. Nothing changed.';
  if (t === 'rate_limited') return 'Slow down a moment, then try again.';
  return fallback;
}

/** Load everything (called once auth resolves to authed). */
export function loadAll() {
  loadOverview();
}
