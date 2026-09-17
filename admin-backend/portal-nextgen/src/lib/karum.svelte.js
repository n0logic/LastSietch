// Shared Karum state. One $state proxy imported by the route and every Karum
// component, so the board, your own listings and the open dialog stay in sync
// without prop-drilling. Mutate the proxy's PROPERTIES in place (never reassign
// `karum` itself) so the shared reference every component holds stays live.
//
// Reads are public for the board and session-gated for your own rows. Writes are
// CSRF + uuid-idempotent.
//
// ── WHY THIS STORE IS NOT OPTIMISTIC ──────────────────────────────────────────
// The bases store mutates locally, fires the write, and rolls back on a throw.
// That is right for a publish. It is WRONG here, because two of the three writes
// have outcomes that are neither success nor failure:
//
//   reconciling       we do not know whether the payment committed
//   paid_undelivered  it did commit, and the goods are still coming
//
// Both come back as HTTP 202. Rolling back on those would tell a player their
// purchase failed when their Solari has already moved, and re-enabling the Buy
// button would invite the double-click that the server-side idempotency exists to
// absorb. So every write here REFETCHES and reports what the server actually said.
// Slower by one round trip; honest in the cases that matter.
import { api, uuidv4 } from './api.js';

const PAGE = 24;

export const karum = $state({
  status: 'idle',          // idle | loading | ready | error
  linked: false,
  bank: null,              // banked Solari (null = unknown)
  online: null,            // true | false | null(undetermined)
  offlineOk: false,        // listing allowed. undetermined -> false (fail closed)
  caps: { max_price: 900000000, listings_per_day: 20, listed_today: 0 },
  flags: { karum_enabled: false, karum_wtb_enabled: false },
  characterName: '',

  board: { side: 'sell', q: '', template: '', sort: 'new', page: 1, rows: [], more: false, status: 'idle', grade: null },
  mine: [],                // your listings, any state
  purchases: [],           // what you have bought
  requests: [],            // active wanted board from the overview
  myRequests: [],          // wanted orders you posted
  fills: [],               // wanted orders you filled or are filling

  sellable: { items: [], containerId: null, backpackContainerId: null, hiddenNoCategory: 0, status: 'idle' },
  catalog: { items: [], status: 'idle', categoryFilterLive: false },

  notice: '',
  noticeTone: 'info',      // info | ok | warn | error
});

export function setNotice(text, tone = 'info') {
  karum.notice = text || '';
  karum.noticeTone = tone;
}

/** Friendly copy for a thrown write. sendCsrfJSON attaches the whole envelope to
 *  err.data, so prefer the server's own message: it is written for players and it
 *  is the only place that knows which of several refusals happened. */
function reason(err, fallback) {
  return err?.data?.message || fallback;
}

export async function loadOverview({ preserveBoard = false } = {}) {
  karum.status = 'loading';
  try {
    const r = await api.karum.overview();
    karum.linked = true;
    karum.bank = typeof r?.bank_solari === 'number' ? r.bank_solari : null;
    karum.online = typeof r?.online === 'boolean' ? r.online : null;
    karum.offlineOk = r?.offline_ok === true;
    karum.caps = {
      max_price: Number(r?.caps?.max_price) || 900000000,
      listings_per_day: Number(r?.caps?.listings_per_day) || 20,
      listed_today: Number(r?.caps?.listed_today) || 0,
    };
    karum.flags = {
      karum_enabled: r?.flags?.karum_enabled === true,
      karum_wtb_enabled: r?.flags?.karum_wtb_enabled === true,
    };
    karum.characterName = r?.character_name || '';
    karum.mine = Array.isArray(r?.my_listings) ? r.my_listings : [];
    karum.purchases = Array.isArray(r?.my_purchases) ? r.my_purchases : [];
    karum.requests = Array.isArray(r?.requests) ? r.requests : [];
    karum.myRequests = Array.isArray(r?.my_requests) ? r.my_requests : [];
    karum.fills = Array.isArray(r?.my_fills) ? r.my_fills : [];
    // The overview's first page doubles as the board's first page, so a signed-in
    // viewer does not pay for two requests to see the same rows.
    const overviewRows = karum.board.side === 'wanted' ? r?.requests : r?.listings;
    if (!preserveBoard && !karum.board.q && !karum.board.template && karum.board.sort === 'new' && karum.board.grade == null && Array.isArray(overviewRows)) {
      karum.board.rows = overviewRows;
      karum.board.page = 1;
      karum.board.more = overviewRows.length >= PAGE;
      karum.board.status = 'ready';
    }
    karum.status = 'ready';
    if (!preserveBoard && (karum.board.q || karum.board.template || karum.board.sort !== 'new' || karum.board.grade != null)) await loadBoard();
  } catch (e) {
    karum.status = 'error';
    karum.linked = false;
  }
}

/** The public board. Also the signed-out entry point. */
let boardSequence = 0;

export async function loadBoard(patch = {}) {
  const token = ++boardSequence;
  // Switching boards drops the old rows FIRST. The side flips synchronously while
  // the fetch is in flight, and the board renders whatever rows it holds through
  // the branch for the NEW side: a wanted order was drawn as a sale card with a
  // Buy button for as long as the search took (player report, 2026-09-04).
  if (patch.side !== undefined && patch.side !== karum.board.side) karum.board.rows = [];
  Object.assign(karum.board, patch, { page: 1 });
  // The grade filter only exists on the wanted board, and the server refuses it on the
  // sell board. Clearing it HERE rather than in the component means no caller can leave a
  // stale grade behind and turn every sell-board load into a 400.
  if (karum.board.side !== 'wanted') karum.board.grade = null;
  karum.board.status = 'loading';
  try {
    const r = await api.karum.search({
      q: karum.board.q, template: karum.board.template,
      sort: karum.board.sort, side: karum.board.side, page: 1,
      grade: karum.board.grade,
    });
    if (token !== boardSequence) return;
    karum.board.rows = Array.isArray(r?.rows) ? r.rows : [];
    karum.board.more = r?.more === true;
    karum.board.status = 'ready';
  } catch (e) {
    if (token !== boardSequence) return;
    karum.board.status = 'error';
  }
}

export async function loadMoreBoard() {
  if (karum.board.status === 'loading' || !karum.board.more) return;
  const next = karum.board.page + 1;
  const token = ++boardSequence;
  karum.board.status = 'loading';
  try {
    const r = await api.karum.search({
      q: karum.board.q, template: karum.board.template,
      sort: karum.board.sort, side: karum.board.side, page: next,
      grade: karum.board.grade,
    });
    if (token !== boardSequence) return;
    karum.board.rows = [...karum.board.rows, ...(Array.isArray(r?.rows) ? r.rows : [])];
    karum.board.page = next;
    karum.board.more = r?.more === true;
    karum.board.status = 'ready';
  } catch (e) {
    if (token !== boardSequence) return;
    karum.board.status = 'error';
  }
}

export async function loadSellable() {
  karum.sellable.status = 'loading';
  try {
    const r = await api.karum.sellable();
    karum.sellable.items = Array.isArray(r?.items) ? r.items : [];
    // Bank id, kept for display and as a fallback. 🔴 A WRITE must use the item's OWN
    // container_id: bank and backpack are different inventories, and sending the bank id
    // for a backpack item resolves to the bank and then fails item_not_found.
    karum.sellable.containerId = r?.container_id ?? null;
    karum.sellable.backpackContainerId = r?.backpack_container_id ?? null;
    // Tradeable items withheld because their template has no CHOAM Exchange category, so
    // the Karum could not hand them back. Surfaced rather than dropped silently: the
    // player can see the item in-game, and an unexplained absence reads as a bug.
    karum.sellable.hiddenNoCategory = Number(r?.hidden_no_category) || 0;
    karum.sellable.status = 'ready';
  } catch (e) {
    karum.sellable.items = [];
    karum.sellable.hiddenNoCategory = 0;
    karum.sellable.status = 'error';
  }
}

export async function loadCatalog() {
  if (karum.catalog.status === 'loading') return;
  karum.catalog.status = 'loading';
  try {
    const r = await api.karum.catalog();
    karum.catalog.items = Array.isArray(r?.items) ? r.items : [];
    karum.catalog.categoryFilterLive = r?.category_filter_live === true;
    karum.catalog.status = 'ready';
  } catch (e) {
    karum.catalog.items = [];
    karum.catalog.status = 'error';
  }
}

/** LIST. The only offline-gated leg. Returns a plain outcome the dialog renders:
 *  {ok} | {deferred} | {error}. The uuid is minted by the CALLER and reused across
 *  retries, so it is a parameter rather than something this function invents. */
export async function listItem({ itemId, containerId, price, template, uuid }) {
  try {
    const r = await api.karum.list({
      item_id: itemId, container_id: containerId, price,
      expected_template: template || '', uuid,
    });
    if (r?.status === 'deferred') {
      return { deferred: true, message: r?.message || 'The Karum is not open yet.' };
    }
    await Promise.all([loadOverview(), loadSellable()]);
    return { ok: true, listingId: r?.listing_id };
  } catch (e) {
    // A player_online refusal is routine and gets the server's own gate copy.
    return { error: e?.message || 'write_failed',
             message: reason(e, 'That could not be listed. Refresh and try again.') };
  }
}

/** BUY. Three shapes of answer, and the in-flight one is the point of this store.
 *  A 202 means the trade is mid-flight: `reconciling` (payment status unknown) or
 *  `paid_undelivered` (paid, goods coming). NEITHER is a failure and NEITHER should
 *  re-arm the button, because the server has already accepted this correlation_id
 *  and a second click would just replay it. */
export async function buyListing({ listingId, expectedPrice, uuid }) {
  try {
    const r = await api.karum.buy({
      listing_id: listingId, expected_price: expectedPrice, uuid,
    });
    if (r?.status === 'deferred') {
      return { deferred: true, message: r?.message || 'The Karum is not open yet.' };
    }
    await loadOverview();
    return { ok: true, bankAfter: r?.bank_after ?? null,
             collectAt: r?.collect_at || 'any CHOAM Exchange terminal',
             note: r?.note || '' };
  } catch (e) {
    const token = e?.message || 'write_failed';
    if (e?.status === 202 || token === 'reconciling' || token === 'paid_undelivered') {
      await loadOverview();
      return { inFlight: true, token, message: reason(e, 'We are confirming this trade.') };
    }
    if (token === 'listing_gone') await loadBoard();
    return { error: token, message: reason(e, 'That purchase could not be completed.') };
  }
}

export async function cancelListing({ listingId, uuid }) {
  try {
    const r = await api.karum.cancel({ listing_id: listingId, uuid });
    if (r?.status === 'deferred') {
      return { deferred: true, message: r?.message || 'The Karum is not open yet.' };
    }
    await loadOverview();
    return { ok: true, collectAt: r?.collect_at || 'any CHOAM Exchange terminal' };
  } catch (e) {
    await loadOverview();
    return { error: e?.message || 'write_failed',
             message: reason(e, 'That listing could not be pulled. Try again shortly.') };
  }
}

/** POST a wanted order. `qualityLevel` is null for a no-grade template (and for the
 *  legacy any-grade contract); 0 is Base and is a REAL request, so the null check below
 *  is `== null` and never a falsy test, which would silently drop every Base order. */
export async function postRequest({ templateId, stackSize, price, qualityLevel = null,
                                    qualityMode = 'exact', uuid }) {
  try {
    const r = await api.karum.requestPost({
      template_id: templateId, stack_size: stackSize, price, uuid,
      ...(qualityLevel == null
        ? {}
        : { quality_level: qualityLevel, quality_mode: qualityMode }),
    });
    if (r?.status === 'deferred') {
      return { deferred: true, message: r?.message || 'Wanted orders are not open yet.' };
    }
    await loadOverview();
    return { ok: true, request: r?.request || null };
  } catch (e) {
    return { error: e?.message || 'write_failed',
             message: reason(e, 'That wanted order could not be posted.') };
  }
}

export async function fillRequest({ requestId, itemId, containerId, expectedPrice,
                                    expectedTemplate, uuid }) {
  try {
    const r = await api.karum.requestFill({
      request_id: requestId, item_id: itemId, container_id: containerId,
      expected_price: expectedPrice, expected_template: expectedTemplate, uuid,
    });
    if (r?.status === 'deferred') {
      return { deferred: true, message: r?.message || 'Wanted orders are not open yet.' };
    }
    await Promise.all([loadOverview(), loadSellable()]);
    return { ok: true, earned: r?.earned ?? expectedPrice, note: r?.note || '' };
  } catch (e) {
    const token = e?.message || 'write_failed';
    if (e?.status === 202 || token === 'reconciling' || token === 'paid_undelivered') {
      await loadOverview();
      return { inFlight: true, token, message: reason(e, 'We are confirming this fill.') };
    }
    await Promise.all([loadOverview(), loadSellable()]);
    return { error: token, message: reason(e, 'That wanted order could not be filled.') };
  }
}

export async function cancelRequest({ requestId }) {
  try {
    await api.karum.requestCancel({ request_id: requestId });
    await loadOverview();
    return { ok: true };
  } catch (e) {
    await loadOverview();
    return { error: e?.message || 'write_failed',
             message: reason(e, 'That wanted order could not be cancelled.') };
  }
}

export async function loadAll(options = {}) {
  await loadOverview(options);
  if (karum.status === 'ready') await Promise.all([loadSellable(), loadCatalog()]);
}

export { uuidv4 };
