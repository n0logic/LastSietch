// Shared Sietch Traffic state. One $state proxy imported by the Desert hub and
// the maps hub so both read the SAME poll instead of each opening their own.
// Mutate the proxy's PROPERTIES in place (never reassign `traffic` itself), so
// the shared reference every component holds stays live.
//
// Public read, no session, no writes. The endpoint is served from a 60s server
// cache behind a rate limiter, so polling faster buys nothing and only spends
// the limiter's budget.
//
// The poll runs ONLY while the document is visible. A backgrounded tab that
// keeps ticking is the thing that turns one open portal tab into 1,440 reads a
// day for a board nobody is looking at; on return we fetch once immediately so
// the board is current rather than up to a minute stale.
//
// Null is never rendered as 0. A missing scalar stays null all the way to the
// board, which prints "not observed" instead of inventing an empty server.
import { api } from './api.js';

const POLL_MS = 60_000;

export const traffic = $state({
  status: 'idle',       // idle | loading | ready | error
  available: false,     // the feed answered with real rows
  stale: false,         // served past its freshness window
  readAt: null,         // server read stamp (ISO), never our clock
  battlegroup: null,    // {title, region} | null
  onlinePlayers: null,  // whole-battlegroup online count | null
  totals: null,         // {travel_requests, login_requests} | null
  rows: [],             // the 7 canonical instance rows
  instanced: null,      // {maps, warm, players, queue, rows:[...]} | null
});

// Monotonic request token: a slow in-flight read must never overwrite a newer
// one that already landed (a tab regaining focus fires an immediate refetch
// while the previous one is still open).
let loadSeq = 0;

// One timer and one listener for the whole app, however many components mount.
let subscribers = 0;
let timer = null;
let listening = false;

function visible() {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

/** Fetch once. Safe to call at any time; a failed read seals the board rather
 *  than blanking the last good rows. */
export async function loadTraffic() {
  const seq = ++loadSeq;
  if (traffic.status === 'idle') traffic.status = 'loading';
  try {
    const r = await api.server.traffic();
    if (seq !== loadSeq) return;
    traffic.available = r?.available === true;
    traffic.stale = r?.stale === true;
    traffic.readAt = r?.read_at ?? null;
    traffic.battlegroup = r?.battlegroup || null;
    traffic.onlinePlayers = r?.online_players ?? null;
    traffic.totals = r?.totals || null;
    traffic.rows = Array.isArray(r?.rows) ? r.rows : [];
    traffic.instanced = r?.instanced || null;
    traffic.status = 'ready';
  } catch (e) {
    if (seq !== loadSeq) return;
    traffic.status = 'error';
    traffic.available = false;
  }
}

function stopTimer() {
  if (timer != null) { clearInterval(timer); timer = null; }
}

function startTimer() {
  if (timer != null || subscribers === 0) return;
  timer = setInterval(() => { if (visible()) loadTraffic(); }, POLL_MS);
}

function onVisibility() {
  if (subscribers === 0) return;
  if (visible()) {
    loadTraffic();
    startTimer();
  } else {
    stopTimer();
  }
}

/** Subscribe a surface to the shared poll. Returns the unsubscribe function, so
 *  a caller can hand it straight back from $effect / onMount. The last
 *  subscriber to leave tears the timer and the listener down. */
export function watchTraffic() {
  subscribers += 1;
  if (!listening && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility);
    listening = true;
  }
  if (visible()) {
    loadTraffic();
    startTimer();
  }
  return () => {
    subscribers = Math.max(0, subscribers - 1);
    if (subscribers > 0) return;
    stopTimer();
    if (listening && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility);
      listening = false;
    }
  };
}
