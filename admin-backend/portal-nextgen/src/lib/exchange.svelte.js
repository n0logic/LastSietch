// Shared Exchange (CHOAM market) state. One $state proxy imported by the route +
// every exchange component so the Solari bank, the browse list, the open price
// ladder, the watches, the flips, and my-orders stay in sync without prop-
// drilling. Mutate the proxy's PROPERTIES in place (never reassign `exchange`),
// so the shared reference every component holds stays live.
//
// Reads are never gated (browsing always works). BUY is optimistic + ONLINE-SAFE:
// decrement the bank, fire the CSRF write, reconcile the ladder + bank from server
// truth, and roll back on any throw. SELL is OFFLINE-gated server-side (the server
// is authoritative); `offlineOk` here is a UI courtesy only and defaults closed.
// The market v2 overview does NOT expose the player's online status, so the sell
// gate is enforced by the server and surfaced from its refusal token, never faked.
import { api, uuidv4 } from './api.js';

export const exchange = $state({
  status: 'idle',        // idle | loading | ready | error
  csrf: '',              // echoed for reference (writes read the cookie directly)

  bank: { solari: 0 },   // owned Solari. AMBER at rest; only the DRAIN during a
                         // settling buy earns the Ibad-blue cue (a live spend).
  buying: false,         // a buy is settling right now (drives the drain cue)

  tabs: [],              // category tab defs from overview ([{key,label}] | strings)
  browse: {
    q: '', category: '', kind: 'all', sort: 'active',
    page: 0, rows: [], more: false, status: 'idle',
  },
  selected: {            // the open price ladder (PriceLadderDrawer)
    tpl: null, name: '', icon: '',
    ladder: [], bot_tiers: [], history: null, status: 'idle',
  },
  watches: [],           // [{template_id, name, icon, max_price, min_price?}]
  alerts: [],            // FIRED alerts, newest first [{id, template_id, name, icon,
                         // threshold_price, match_price, created_at, seen}]
  // Two different numbers that were previously conflated into one "alertCount":
  // armedCount is how many watches you have SET, firedCount is how many of them
  // have actually TRIPPED and you have not looked at yet. Only firedCount is a
  // notification.
  armedCount: 0,
  firedCount: 0,
  orders: {              // my listings, three buckets + the segmented control tab
    tab: 'active', active: [], completed: [], history: [], status: 'idle',
  },
  botLimits: { scopes: [], updated_at: '', window_days: 7, status: 'idle' }, // weekly bot buy/sell budget tracker
  flips: { rows: [], status: 'idle' }, // min ask < bot cap (the arbitrage board)
  pulse: { tracked: 0, syncedAt: 0 },  // tracked count + last-live-sync epoch (ms)
  offlineOk: false,      // sell-only courtesy gate (server authoritative; closed)
  notice: '',
  noticeTone: 'info',    // info | ok | warn | error
});

function setNotice(text, tone = 'info') {
  exchange.notice = text || '';
  exchange.noticeTone = tone;
}

/** Note the moment a genuinely live read landed (drives the Ibad heartbeat). */
function markSynced() { exchange.pulse.syncedAt = Date.now(); }

/** Boot: pull the overview (bank + watches + alert count + category tabs), then
 *  fan out the browse list, the flip board, and my-orders. */
export async function loadOverview() {
  exchange.status = 'loading';
  try {
    const r = await api.market.overview();
    exchange.bank.solari = Number(r?.bank_solari) || 0;
    exchange.watches = Array.isArray(r?.watches) ? r.watches : [];
    exchange.alerts = Array.isArray(r?.alerts) ? r.alerts : [];
    exchange.armedCount = exchange.watches.length;
    // A real 0 is a valid count, so null/undefined-check rather than falsy-coerce.
    exchange.firedCount = r?.alert_count != null ? Number(r.alert_count) : 0;
    exchange.tabs = Array.isArray(r?.tabs) ? r.tabs : [];
    exchange.csrf = r?.csrf || '';
    exchange.pulse.tracked = exchange.watches.length;
    markSynced();
    exchange.status = 'ready';
    runSearch();
    loadFlips();
    loadOrders();
    loadBotLimits();
  } catch (e) {
    exchange.status = 'error';
  }
}

/** Bot weekly buy/sell budget tracker (per category + item overrides). Read-only,
 *  best-effort — a miss just leaves the strip hidden. */
export async function loadBotLimits() {
  exchange.botLimits.status = 'loading';
  try {
    const r = await api.market.botLimits();
    exchange.botLimits.scopes = (r && r.available && Array.isArray(r.scopes)) ? r.scopes : [];
    exchange.botLimits.updated_at = r?.updated_at || '';
    exchange.botLimits.window_days = Number(r?.window_days) || 7;
    exchange.botLimits.status = 'ready';
  } catch (e) {
    exchange.botLimits.status = 'error';
  }
}

// Monotonic request tokens so a slow response can never overwrite a newer one.
let searchSeq = 0;
let itemSeq = 0;

/** Replace-search: patch the browse filters, reset to page 0, refetch page 0. */
export async function runSearch(patch = {}) {
  Object.assign(exchange.browse, patch);
  if ('q' in patch || 'category' in patch || 'kind' in patch || 'sort' in patch) {
    exchange.browse.page = 0;
  }
  const seq = ++searchSeq;
  exchange.browse.status = 'loading';
  try {
    const { q, category, kind, sort } = exchange.browse;
    const r = await api.market.search({ q, category, kind, sort, page: 0 });
    if (seq !== searchSeq) return; // stale response, a newer search won
    exchange.browse.rows = Array.isArray(r?.rows) ? r.rows : [];
    exchange.browse.more = r?.more === true;
    exchange.browse.page = Number(r?.page) || 0;
    exchange.browse.status = 'ready';
    markSynced();
  } catch (e) {
    if (seq !== searchSeq) return;
    exchange.browse.rows = [];
    exchange.browse.more = false;
    exchange.browse.status = 'error';
  }
}

/** Append the next page (keeps existing rows). No-op while already loading. */
export async function loadMore() {
  if (!exchange.browse.more || exchange.browse.status === 'loading') return;
  const seq = ++searchSeq;
  const nextPage = (exchange.browse.page || 0) + 1;
  exchange.browse.status = 'loading';
  try {
    const { q, category, kind, sort } = exchange.browse;
    const r = await api.market.search({ q, category, kind, sort, page: nextPage });
    if (seq !== searchSeq) return;
    exchange.browse.page = nextPage;
    exchange.browse.rows = [...exchange.browse.rows, ...(Array.isArray(r?.rows) ? r.rows : [])];
    exchange.browse.more = r?.more === true;
    exchange.browse.status = 'ready';
  } catch (e) {
    if (seq !== searchSeq) return;
    exchange.browse.status = 'ready'; // keep what we already have
  }
}

/** Open a template's price ladder (drawer). Accepts a search row or a bare tpl. */
export async function openItem(row) {
  const tpl = typeof row === 'string' ? row : row?.template_id;
  if (!tpl) return;
  exchange.selected.tpl = tpl;
  exchange.selected.name = (typeof row === 'object' && row?.name) || exchange.selected.name;
  exchange.selected.icon = (typeof row === 'object' && row?.icon) || '';
  exchange.selected.ladder = [];
  exchange.selected.bot_tiers = [];
  exchange.selected.history = null;
  exchange.selected.status = 'loading';
  const seq = ++itemSeq;
  try {
    const r = await api.market.item(tpl);
    if (seq !== itemSeq || exchange.selected.tpl !== tpl) return;
    exchange.selected.name = r?.name || exchange.selected.name;
    exchange.selected.icon = r?.icon || exchange.selected.icon;
    exchange.selected.ladder = Array.isArray(r?.ladder) ? r.ladder : [];
    exchange.selected.bot_tiers = Array.isArray(r?.bot_tiers) ? r.bot_tiers : [];
    if (typeof r?.bank_solari === 'number') exchange.bank.solari = r.bank_solari;
    exchange.selected.status = 'ready';
    markSynced();
  } catch (e) {
    if (seq !== itemSeq) return;
    exchange.selected.status = 'error';
  }
  loadHistory(tpl); // independent; may come back calibrating
}

export function closeItem() {
  exchange.selected.tpl = null;
  exchange.selected.ladder = [];
  exchange.selected.bot_tiers = [];
  exchange.selected.history = null;
  exchange.selected.status = 'idle';
}

/** Price history for the sparkline. Empty/absent -> a calibrating placeholder
 *  (the capture table only starts accruing at launch, so this is the norm now). */
async function loadHistory(tpl) {
  try {
    const r = await api.market.history(tpl);
    if (exchange.selected.tpl !== tpl) return;
    const points = Array.isArray(r?.points) ? r.points : [];
    exchange.selected.history = {
      points,
      low_7d: r?.low_7d ?? null,
      calibrating: r?.calibrating === true || points.length === 0,
    };
  } catch (e) {
    if (exchange.selected.tpl !== tpl) return;
    exchange.selected.history = { points: [], low_7d: null, calibrating: true };
  }
}

/** Refetch the bank + watch context after a write settles. */
async function refreshBank() {
  try {
    const r = await api.market.overview();
    if (typeof r?.bank_solari === 'number') exchange.bank.solari = r.bank_solari;
    if (Array.isArray(r?.watches)) exchange.watches = r.watches;
    if (Array.isArray(r?.alerts)) exchange.alerts = r.alerts;
    exchange.armedCount = exchange.watches.length;
    exchange.firedCount = r?.alert_count != null ? Number(r.alert_count) : 0;
    exchange.pulse.tracked = exchange.watches.length;
    markSynced();
  } catch (e) { /* keep the optimistic view; a later load reconciles */ }
}

/** Buy `count` off one ladder rung. Optimistically drains the bank (the ONE place
 *  the Solari gauge earns its Ibad-blue drain cue), then reconciles ladder + bank.
 *  ONLINE-SAFE: never gated. Rolls the balance back on any throw.
 *
 *  `uuid` is the idempotency key. The caller mints it ONCE per logical buy attempt
 *  and passes the SAME value on retry so an ambiguous timeout (server committed,
 *  client saw a timeout) never double-charges + double-delivers. We only mint a
 *  fallback here for callers that do not manage their own key. */
export async function buy(order, count = 1, uuid) {
  if (exchange.buying) return false;
  const n = Math.max(1, Math.floor(Number(count)) || 1);
  const unit = Number(order?.price) || 0;
  const cost = unit * n;
  if (cost > (exchange.bank.solari || 0)) {
    setNotice('Not enough Solari on account for that buy.', 'warn');
    return false;
  }
  const key = uuid || uuidv4();
  const prev = exchange.bank.solari;
  exchange.buying = true;
  exchange.bank.solari = Math.max(0, prev - cost); // optimistic drain
  try {
    const r = await api.market.buy({
      order_id: order.order_id, revision: order.revision, count: n, uuid: key,
    });
    if (typeof r?.bank_after === 'number') exchange.bank.solari = r.bank_after;
    const got = typeof r?.delivered === 'number' ? r.delivered : n;
    setNotice(`Bought ${got.toLocaleString()} for ${cost.toLocaleString()} Solari.`, 'ok');
    if (exchange.selected.tpl) await openItem({ template_id: exchange.selected.tpl, name: exchange.selected.name, icon: exchange.selected.icon });
    await refreshBank();
    return true;
  } catch (e) {
    exchange.bank.solari = prev; // rollback
    setNotice(friendly(e, 'The buy did not go through. Nothing changed.'), 'error');
    return false;
  } finally {
    exchange.buying = false;
  }
}

/** Load my listings (three buckets). No expiry data exists -> no countdown. */
export async function loadOrders() {
  exchange.orders.status = 'loading';
  try {
    const r = await api.market.myOrders();
    exchange.orders.active = Array.isArray(r?.active) ? r.active : [];
    exchange.orders.completed = Array.isArray(r?.completed) ? r.completed : [];
    exchange.orders.history = Array.isArray(r?.history) ? r.history : [];
    exchange.orders.status = 'ready';
  } catch (e) {
    exchange.orders.status = 'error';
  }
}

export function setOrdersTab(tab) { exchange.orders.tab = tab; }

/** Cancel one of my active listings (online-safe). Refetches on success. */
export async function cancelOrder(order) {
  try {
    await api.market.cancelOrder({ order_id: order.order_id, revision: order.revision, uuid: uuidv4() });
    setNotice('Listing cancelled.', 'ok');
    await loadOrders();
    return true;
  } catch (e) {
    setNotice(friendly(e, 'Could not cancel that listing.'), 'error');
    return false;
  }
}

/** Renew (relist) one of my listings at a fresh price + duration (online-safe). */
export async function relistOrder(order, price, durationDays) {
  const p = Math.floor(Number(price)) || 0;
  if (p <= 0) { setNotice('Enter a relist price.', 'warn'); return false; }
  try {
    await api.market.relistOrder({
      order_id: order.order_id, revision: order.revision,
      price: p, duration_days: durationDays, uuid: uuidv4(),
    });
    setNotice('Listing renewed.', 'ok');
    await loadOrders();
    return true;
  } catch (e) {
    setNotice(friendly(e, 'Could not renew that listing.'), 'error');
    return false;
  }
}

/** The Bot-Floor flip board: cheapest player ask below the bot cap. */
export async function loadFlips() {
  exchange.flips.status = 'loading';
  try {
    const r = await api.market.flips();
    exchange.flips.rows = Array.isArray(r?.rows) ? r.rows : [];
    exchange.flips.status = 'ready';
    markSynced();
  } catch (e) {
    exchange.flips.status = 'error';
  }
}

/** Remove a price alert (optimistic; rolls the row back on a throw). */
export async function removeWatch(w) {
  const prev = exchange.watches;
  exchange.watches = exchange.watches.filter((x) => x.template_id !== w.template_id);
  exchange.pulse.tracked = exchange.watches.length;
  try {
    await api.market.watchRemove({ template_id: w.template_id });
    setNotice('Alert removed.', 'ok');
    return true;
  } catch (e) {
    exchange.watches = prev; // rollback
    exchange.pulse.tracked = exchange.watches.length;
    setNotice('Could not remove that alert.', 'error');
    return false;
  }
}

/** Map a server error token to friendly copy (fail-closed callers surface this). */
function friendly(e, fallback) {
  const t = (e && e.message) || '';
  if (t === 'player_online') return 'Log out of the game first. Nothing changed.';
  if (t === 'rate_limited') return 'Slow down a moment, then try again.';
  if (t === 'insufficient_funds') return 'Not enough Solari on account. Nothing changed.';
  if (t === 'order_gone' || t === 'not_found' || t === 'order_not_found') return 'That listing is no longer available.';
  if (t === 'category_blocked') return 'That item cannot be traded here.';
  return fallback;
}

/** Badge-only refresh for the topbar. Hits the COUNT route, never the overview:
 *  the overview resolves the buyer controller + bank through the relay against the
 *  live game DB, and this runs on every hard load of every portal page. That is
 *  what makes it a real mirror of refreshUnread() rather than one in name only.
 *  Never throws; any failure degrades to 0 rather than a fabricated badge. */
export async function refreshAlerts() {
  try {
    const r = await api.market.alertsCount();
    exchange.firedCount = r?.alert_count != null ? Number(r.alert_count) : 0;
  } catch (e) {
    exchange.firedCount = 0;
  }
}

/** Mark the NAMED alerts seen and take the fresh unseen count from the server.
 *  Its own CSRF-gated write on purpose: the overview read is polled, so clearing
 *  there would dismiss an alert the player never saw. Only the ids the caller
 *  actually rendered are sent, and the badge is set from the server's remaining
 *  count rather than assumed to be 0 — alerts outside the rendered slice are
 *  still unseen. Best-effort: on a throw the badge stays up and a later visit
 *  retries. */
export async function markAlertsSeen(ids) {
  const wanted = (Array.isArray(ids) ? ids : []).filter((id) => id != null);
  if (wanted.length === 0) return;
  try {
    const r = await api.market.alertsSeen(wanted);
    exchange.firedCount = r?.alert_count != null ? Number(r.alert_count) : exchange.firedCount;
    const done = new Set(wanted);
    exchange.alerts = exchange.alerts.map((a) => (done.has(a.id) ? { ...a, seen: true } : a));
  } catch (e) { /* leave the badge up; a later visit retries */ }
}

/** Load everything (called once auth resolves to authed). */
export function loadAll() {
  loadOverview();
}
