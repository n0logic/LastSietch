// Seven Nights of the Sietch: the event page's shared state. One $state proxy for
// the route and every sietch component; mutate its properties, never reassign it.
//
// Contract: PLAN §8 (frozen 2026-10-24). GET /portal/sietch/state is the whole
// page in one payload; POST /portal/sietch/knock answers one Knock. The balance
// lives on the server only, so a Knock never adds Tokens locally: it shows the
// answer and REFETCHES the state, which is the only source of a number.
//
// Night math is UTC. One night = one UTC day, and DST ends inside night 7, so
// every countdown targets an absolute instant from the server (or the copy
// table), never "20:00 ET".
import { getJSON, sendCsrfJSON, api } from './api.js';
import { COPY } from './components/sietch/copy.js';

const PREVIEW = import.meta.env.VITE_REVIEW_PREVIEW === 'true';
// Client-side echo of the server's 30 s Knock limit, so the button can say why it
// is resting. The server is the authority; this only spares a wasted round trip.
export const KNOCK_COOLDOWN_MS = 30000;

export const sietch = $state({
  status: 'idle',          // idle | loading | ready | error | anon | unlinked
  data: null,              // the /portal/sietch/state payload
  knock: { busy: false, result: null, at: 0 },
  board: { status: 'idle', rows: [] },
  // The one order being watched after a bank/wallet purchase (POST answers
  // 'queued' and delivery runs in the background). timedOut: still in flight
  // after the watch window; the Hoard keeps the truth from then on.
  tracking: { id: null, timedOut: false },
  notice: '',
  noticeTone: 'info',
  // Preview only: fixtures pin a moment inside their night so every phase can be
  // seen today. Always 0 against the real API.
  clockOffset: 0,
});

let fixtures = null;
async function loadFixtures() {
  // Written as a literal comparison so the production build folds it and drops the
  // fixture chunk entirely (a const alias was not folded).
  if (import.meta.env.VITE_REVIEW_PREVIEW !== 'true') throw new Error('fixtures are preview-only');
  if (!fixtures) fixtures = await import('./components/sietch/fixtures.js');
  return fixtures;
}

function scenario() {
  try { return new URL(location.href).searchParams.get('sn') || 'night1'; } catch (e) { return 'night1'; }
}

export async function loadState() {
  if (sietch.status !== 'ready') sietch.status = 'loading';
  try {
    if (PREVIEW) {
      const f = await loadFixtures();
      sietch.data = f.fixtureState(scenario());
      sietch.clockOffset = sietch.data._preview_now ? Date.parse(sietch.data._preview_now) - Date.now() : 0;
    } else {
      sietch.data = await getJSON('/portal/sietch/state');
    }
    sietch.status = 'ready';
  } catch (e) {
    // 409 is the session with no linked game profile. The event gate being off is
    // NOT an error: /state still answers 200 with flags.earning false.
    sietch.status = e?.status === 401 ? 'anon' : e?.status === 409 ? 'unlinked' : 'error';
  }
}

export async function loadBoard() {
  sietch.board.status = 'loading';
  try {
    const r = PREVIEW ? (await loadFixtures()).FIXTURE_BOARD : await getJSON('/portal/sietch/board');
    sietch.board.rows = Array.isArray(r?.rows) ? r.rows : [];
    sietch.board.status = 'ready';
  } catch (e) {
    sietch.board.status = 'error';
  }
}

export async function knock(spotId = null) {
  if (sietch.knock.busy) return;
  sietch.knock.busy = true;
  try {
    let r;
    if (PREVIEW) {
      r = (await loadFixtures()).fixtureKnock(sietch.data?.night?.activity);
    } else {
      r = await sendCsrfJSON('POST', '/portal/sietch/knock', spotId ? { spot_id: spotId } : {});
    }
    sietch.knock.result = r && typeof r === 'object' ? r : { result: 'error' };
  } catch (e) {
    // 429 is the rate limit, 409 a session with no linked profile; anything else
    // is an outage, said plainly.
    sietch.knock.result = e?.status === 429 ? { result: 'cooldown' }
      : e?.status === 409 ? { result: 'unlinked', message: e?.data?.message }
      : { result: 'error' };
  } finally {
    sietch.knock.at = Date.now();
    sietch.knock.busy = false;
  }
  if (!PREVIEW && sietch.knock.result?.result === 'treat') await loadState();
}

/** One line of player copy for a Knock answer. Unknown results fall back to the
 *  plain miss line rather than inventing a claim. */
export function knockLine(r) {
  const k = COPY.knock;
  if (!r) return '';
  switch (r.result) {
    case 'treat': return r.next_clue ? k.riddle_hit : (sietch.data?.night?.activity === 'seek' ? k.found : k.treat);
    case 'miss':
      if (sietch.data?.night?.activity === 'riddles') return k.riddle_miss;
      return r.dist_text ? k.miss_distance.replace('{dist}', r.dist_text) : k.miss;
    case 'band': return k.bands[r.band] || k.miss;
    case 'stale': return k.stale;
    case 'closed': return k.closed;
    case 'ineligible': return k.ineligible;
    case 'amtal': return k.amtal;
    case 'offline': return k.offline;
    case 'cooldown': return k.cooldown;
    case 'done': return k.done;
    case 'unlinked': return r.message || 'Link a game character to Knock.';
    case 'error': return 'The Knock did not reach the desert. Try again in a moment.';
    default: return k.miss;
  }
}

/** Write one of the three event prefs through the existing prefs endpoint. The
 *  server validates ownership; a refusal leaves the old value and says so. */
export async function setSietchPref(key, value) {
  const map = { flair: 'sietch_flair', mapdot: 'sietch_mapdot', board_hidden: 'sietch_board_hidden' };
  const prior = sietch.data?.prefs?.[key];
  if (sietch.data?.prefs) sietch.data.prefs[key] = value;
  if (PREVIEW) return;
  try {
    await api.settings.prefs.set('', { [map[key]]: value });
    setNotice('Saved.', 'ok');
  } catch (e) {
    if (sietch.data?.prefs) sietch.data.prefs[key] = prior;
    setNotice('That could not be saved. Try again in a moment.', 'error');
  }
}

export function setNotice(text, tone = 'info') {
  sietch.notice = text || '';
  sietch.noticeTone = tone;
}

/** Where the event stands at `now`: before | night | after, plus the instant the
 *  next countdown should target. Server instants win; the copy table backs them up. */
export function phaseAt(now, night) {
  const start = Date.parse(COPY.event.starts_at);
  const end = Date.parse(COPY.event.ends_at);
  if (now < start) return { phase: 'before', target: COPY.event.starts_at };
  if (now >= end) return { phase: 'after', target: null };
  const ends = night?.ends_at || COPY.nights.find((c) => now >= Date.parse(c.starts_at) && now < Date.parse(c.ends_at))?.ends_at;
  return { phase: 'night', target: ends || null };
}

export function nightCopy(night) {
  if (!night) return null;
  const table = COPY.nights[(night.n || 0) - 1] || {};
  if (night.copy && typeof night.copy === 'object') return { ...table, ...night.copy };
  if (typeof night.copy === 'string') return { ...table, hook: night.copy };
  return table;
}

const TRACK_EVERY_MS = 2500;
const TRACK_FOR_MS = 30000;
const SETTLED = ['delivered', 'refunded', 'failed'];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function orderById(id) {
  return (sietch.data?.orders || []).find((o) => o.id === id) || null;
}

// Poll /state until the order settles or the window closes. A newer purchase
// replaces the watch, and the older loop then stops on its own.
async function trackOrder(id) {
  sietch.tracking = { id, timedOut: false };
  const until = Date.now() + TRACK_FOR_MS;
  while (Date.now() < until) {
    await sleep(TRACK_EVERY_MS);
    if (sietch.tracking.id !== id) return;
    if (PREVIEW) {
      const o = orderById(id);
      if (o) o.status = 'delivered';
    } else {
      await loadState();
    }
    if (SETTLED.includes(orderById(id)?.status)) return;
  }
  if (sietch.tracking.id === id) sietch.tracking.timedOut = true;
}

/** Buy one SKU. `idemKey` is minted by the CALLER once per intended purchase and
 *  reused on a retry, so a double tap or a lost response cannot buy twice. Not
 *  optimistic: the balance and the order come from the server's answer and a
 *  refetch, the Karum rule for writes whose outcome can be "in flight". */
export async function order({ sku, idemKey, characterId = null }) {
  try {
    let r;
    if (PREVIEW) {
      const item = sietch.data?.catalog?.find((c) => c.sku === sku);
      r = { order: { id: Date.now(), sku, name: item?.name, lane: item?.lane, price: item?.price,
        status: item?.lane === 'prefs' ? 'delivered' : 'queued', created_at: new Date().toISOString() },
        balance: (sietch.data?.balance || 0) - (item?.price || 0) };
      if (sietch.data) {
        sietch.data.balance = r.balance;
        sietch.data.orders = [r.order, ...(sietch.data.orders || [])];
      }
      if (r.order.status === 'queued') trackOrder(r.order.id);
      return { ok: true, order: r.order };
    }
    r = await sendCsrfJSON('POST', '/portal/sietch/order',
      { sku, idem_key: idemKey, ...(characterId ? { character_id: characterId } : {}) });
    await loadState();
    const placed = r?.order || null;
    if (placed && ['bank', 'wallet', 'spec', 'charxp'].includes(placed.lane) && !SETTLED.includes(placed.status)) {
      trackOrder(placed.id);
    }
    return { ok: true, order: placed };
  } catch (e) {
    // A timeout or 5xx may still have landed: refetch so the Hoard shows the truth.
    if (!e?.status || e.status >= 500) await loadState();
    return { error: e?.message || 'order_failed',
             message: e?.data?.message || (e?.status >= 500 || !e?.status
               ? 'We could not confirm that order. Check Your Hoard before buying again.'
               : 'That could not be bought.') };
  }
}
