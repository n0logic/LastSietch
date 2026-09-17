// Events store. Every read the Events page makes lives here, on the shape
// traffic.svelte.js proved and home.svelte.js repeated: ONE timer per read,
// polling only while the document is visible, one immediate refetch on return,
// and a monotonic token per read so a slow response can never overwrite a newer
// one that already landed.
//
// The board is PUBLIC. An anonymous visitor reads upcoming and past exactly as a
// linked player does; the only thing a session buys is the reminder toggle. So
// `mine` is the one read that is gated, and it is never SENT for an anonymous or
// unlinked viewer: that endpoint answers 401 for them, and a 401 in the network
// log every two minutes is noise rather than a signal.
//
// `mine` is null until a read succeeds, and back to null the moment one fails.
// It is never [] on a failure. An empty array is a statement about the player
// ("you have no reminders set"); null is a statement about us ("we could not
// read them"). The toggle renders ABSENT on null rather than showing an off
// switch that would silently be a lie about the player's own account.
//
// Only `upcoming` polls. A past event cannot change while you watch it, so the
// past window is read once per subscribe and once more whenever the tab comes
// back, which is the moment its 90 day edge may have moved.
import { api } from './api.js';
import { auth } from './auth.svelte.js';

// The four kinds the backend CHECK constraint allows, as the WIRE values and
// nothing else. The composer owns the words a player reads: it maps each value
// through its own label table, so a label typed here would be a second copy of
// the same vocabulary and the two would drift.
export const EVENT_KINDS = [
  'community',
  'faction_war',
  'maintenance',
  'other',
];

// The banner plates shipped with the app, as BARE SLUGS. A slug is not a path:
// the URL is built once, inside the banner component, with its own version key.
// Nothing here ever carries a directory, an extension or a query.
export const EVENT_BANNERS = [
  'fremkit-launch',
  'landsraad-term',
  'maintenance-window',
  'monthly-reward',
  'sietch-almanac',
  'sietch-gathering',
  'spoils-of-kanly',
  'storm-season',
  'war-of-assassins',
];

// Two minutes. The list is served from a 60s server cache behind a rate limiter,
// so polling faster buys nothing and only spends the limiter's budget.
const POLL_MS = { upcoming: 120_000 };

export const events = $state({
  status: 'idle',    // idle | loading | ready | error
  upcoming: [],      // soonest first, published only
  past: [],          // newest first, 90 day window
  mine: null,        // reminded event ids, or null (anonymous, or a failed read)
  detail: { status: 'idle', id: '', event: null }, // the deep link's own read
  lastReadAt: null,  // our clock at the last successful list read
});

// The reminder write lane. Deliberately NOT on `events`: that object is the
// pinned read shape, and a transient busy flag is not a reading of the board.
export const eventRemind = $state({
  busyId: null,      // the event id whose toggle is in flight
  notice: '',        // refusal copy from the last failed write
  noticeTone: 'info',
});

// The admin lane: the composer's own state plus the admin list, which is the
// only read that can see drafts. Never populated for a player without the role,
// because the endpoint answers 404 to them.
export const eventAdmin = $state({
  open: false,       // composer open
  mode: 'create',    // create | edit
  target: null,      // the event being edited, or null
  status: 'idle',    // idle | loading | ready | error
  rows: null,        // admin list rows, null until a read succeeds
  busy: false,       // a create/update/cancel is in flight
  error: '',         // refusal copy for the composer
});

// Monotonic token per read. A tab regaining focus fires an immediate refetch
// while the previous request is still open; whichever lands second must not win
// on age alone.
const seq = { upcoming: 0, past: 0, mine: 0, detail: 0, admin: 0 };

let subscribers = 0;
let listening = false;
const timers = { upcoming: null };

function visible() {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

/** The rows out of a list envelope. The endpoint answers {ok, events:[...]}; a
 *  bare array is accepted too, so a shape drift reads as an empty board rather
 *  than a thrown page. */
function rowsOf(r) {
  if (Array.isArray(r)) return r;
  if (r && Array.isArray(r.events)) return r.events;
  return [];
}

/** The single event out of a detail envelope, or null. */
function eventOf(r) {
  if (!r || typeof r !== 'object') return null;
  if (r.event && typeof r.event === 'object') return r.event;
  return r.id != null ? r : null;
}

/** A linked session is the only one the reminder reads and writes are for. */
function linked() {
  return auth.status === 'authed' && (auth.linked || []).length > 0;
}

export async function loadUpcoming() {
  const s = ++seq.upcoming;
  if (events.status === 'idle') events.status = 'loading';
  try {
    const r = await api.events.list('upcoming');
    if (s !== seq.upcoming) return;
    events.upcoming = rowsOf(r);
    events.lastReadAt = Date.now();
    events.status = 'ready';
  } catch (e) {
    if (s !== seq.upcoming) return;
    // A failed read seals the board rather than blanking rows that are still
    // true. Only a board that never read at all goes to error.
    if (events.status !== 'ready') events.status = 'error';
  }
}

export async function loadPast() {
  const s = ++seq.past;
  try {
    const r = await api.events.list('past');
    if (s !== seq.past) return;
    events.past = rowsOf(r);
  } catch (e) {}
}

/** The player's own reminder ids. Anonymous and unlinked callers never send it. */
export async function loadMine() {
  if (!linked()) {
    events.mine = null;
    return;
  }
  const s = ++seq.mine;
  try {
    const r = await api.events.mine();
    if (s !== seq.mine) return;
    events.mine = Array.isArray(r && r.reminders) ? r.reminders : null;
  } catch (e) {
    if (s !== seq.mine) return;
    events.mine = null;
  }
}

/** Has this player set a reminder on `id`? true | false | null, where null means
 *  we cannot say, and the toggle renders absent rather than guessing. */
export function remindedFor(id) {
  if (!Array.isArray(events.mine)) return null;
  const want = String(id);
  return events.mine.some((x) => String(x) === want);
}

/** One event, for the deep link. A 404 is `missing`, which is what the route
 *  seals on; every other failure is `error`, which invites a retry. */
export async function loadDetail(id) {
  const want = String(id);
  const s = ++seq.detail;
  events.detail.id = want;
  events.detail.status = 'loading';
  events.detail.event = null;
  try {
    const r = await api.events.detail(want);
    if (s !== seq.detail) return;
    const ev = eventOf(r);
    events.detail.event = ev;
    events.detail.status = ev ? 'ready' : 'missing';
  } catch (e) {
    if (s !== seq.detail) return;
    events.detail.status = e && e.status === 404 ? 'missing' : 'error';
  }
}

/** Toggle the reminder on one event, optimistically.
 *
 *  The optimistic half is the point: the toggle is the only control on the page
 *  and a 400ms wait on a round trip reads as a dead button, so the switch moves
 *  first. The rollback is what makes that honest. Every refusal path throws
 *  (sendCsrfJSON fails closed on a non-2xx AND on {ok:false}), and the catch puts
 *  the list back exactly as it was, because a toggle that stays on after the
 *  server refused is a promise of a notification nobody will send. */
export async function toggleRemind(eventId) {
  const id = eventId != null ? String(eventId) : '';
  if (!id) return;
  const before = events.mine;
  // Null means we never read the reminders (anonymous, or the read failed), and
  // there is nothing to toggle from.
  if (!Array.isArray(before)) return;
  if (eventRemind.busyId != null) return;
  const on = before.some((x) => String(x) === id);
  eventRemind.busyId = id;
  eventRemind.notice = '';
  events.mine = on ? before.filter((x) => String(x) !== id) : [...before, id];
  try {
    if (on) await api.events.unremind(id);
    else await api.events.remind(id);
  } catch (e) {
    events.mine = before;
    eventRemind.notice = friendly(
      e, on ? 'That reminder could not be dropped. Nothing changed.'
            : 'That reminder did not go through. Nothing changed.');
    eventRemind.noticeTone = 'error';
  } finally {
    eventRemind.busyId = null;
  }
}

/** The admin list, drafts included. Called ONLY from behind the role gate: for
 *  anyone else the endpoint is a 404, and asking is a request we know the answer
 *  to. The gate hides controls; the server decides the role. */
export async function loadAdminList() {
  const s = ++seq.admin;
  eventAdmin.status = 'loading';
  try {
    const r = await api.events.adminList();
    if (s !== seq.admin) return;
    eventAdmin.rows = rowsOf(r);
    eventAdmin.status = 'ready';
  } catch (e) {
    if (s !== seq.admin) return;
    eventAdmin.rows = null;
    eventAdmin.status = 'error';
  }
}

export function openComposer(target) {
  eventAdmin.target = target || null;
  eventAdmin.mode = target ? 'edit' : 'create';
  eventAdmin.error = '';
  eventAdmin.open = true;
}

export function closeComposer() {
  eventAdmin.open = false;
  eventAdmin.target = null;
  eventAdmin.error = '';
}

/** Create or update, decided by the mode the composer was opened in. */
export async function submitComposer(body) {
  if (eventAdmin.busy) return false;
  eventAdmin.busy = true;
  eventAdmin.error = '';
  const target = eventAdmin.target;
  try {
    if (eventAdmin.mode === 'edit' && target && target.id != null) {
      await api.events.adminUpdate(String(target.id), body);
    } else {
      await api.events.adminCreate(body);
    }
    closeComposer();
    await refreshAfterWrite();
    return true;
  } catch (e) {
    eventAdmin.error = friendly(e, 'That did not save. Nothing changed.');
    return false;
  } finally {
    eventAdmin.busy = false;
  }
}

/** Cancel the event the composer is open on. The composer calls this with the
 *  typed token and the reason, positionally, and the id comes from the composer's
 *  own target rather than from the form: a cancellation mails every player who
 *  set a reminder and cannot be taken back. */
export async function cancelEvent(confirm, reason) {
  if (eventAdmin.busy) return false;
  const target = eventAdmin.target;
  const id = target && target.id != null ? String(target.id) : '';
  if (!id) return false;
  eventAdmin.busy = true;
  eventAdmin.error = '';
  try {
    await api.events.adminCancel(id, String(confirm || ''), String(reason || ''));
    closeComposer();
    await refreshAfterWrite();
    return true;
  } catch (e) {
    eventAdmin.error = friendly(e, 'That cancellation did not go through. Nothing changed.');
    return false;
  } finally {
    eventAdmin.busy = false;
  }
}

/** After any admin write, re-read what the write could have changed. The public
 *  list is edge cached for a minute, so this can still show the old row; the
 *  admin list is not, and it is the one an admin is reading. */
async function refreshAfterWrite() {
  await loadAdminList();
  loadUpcoming();
  loadPast();
}

/** Map a refusal token to friendly copy.
 *
 *  Prefers the SERVER's own sentence when it sent one: `e.message` is the TOKEN,
 *  `e.data.message` is the human sentence the endpoint built, which is often far
 *  more specific than anything keyed off a token can be. Same shape as
 *  rewards.svelte.js and karum.svelte.js, and for the same reason: a token map
 *  can only ever say the most general true thing. */
function friendly(e, fallback) {
  const t = (e && e.message) || '';
  const server = (e && e.data && typeof e.data.message === 'string') ? e.data.message.trim() : '';
  if (server) return server;
  const map = {
    unauthenticated: 'Sign in and link a character first, then set a reminder.',
    csrf: 'Your session expired. Refresh the page and try again.',
    not_found: 'That event is no longer on the board.',
    event_started: 'That event has already started, so a reminder would arrive too late.',
    event_reminder_cap: 'This event is holding all the reminders it can. Nothing changed.',
    player_reminder_cap: 'You are holding as many reminders as the sietch allows. Drop one, then set this.',
    reminder_throttled: 'Too many reminder changes in the last hour. Wait a while, then try again.',
    confirm_required: 'Type CANCEL to confirm. Nothing changed.',
    rate_limited: 'Slow down a moment, then try again.',
  };
  return map[t] || fallback;
}

const READS = {
  upcoming: loadUpcoming,
  past: loadPast,
  mine: loadMine,
};

function stopTimers() {
  for (const key of Object.keys(timers)) {
    if (timers[key] != null) { clearInterval(timers[key]); timers[key] = null; }
  }
}

function startTimers() {
  if (subscribers === 0) return;
  for (const key of Object.keys(POLL_MS)) {
    if (timers[key] != null) continue;
    timers[key] = setInterval(() => { if (visible()) READS[key](); }, POLL_MS[key]);
  }
}

function readAll() {
  for (const key of Object.keys(READS)) READS[key]();
}

function onVisibility() {
  if (subscribers === 0) return;
  if (visible()) {
    readAll();
    startTimers();
  } else {
    stopTimers();
  }
}

/** Subscribe a surface to the shared poll. Returns the unsubscribe function so a
 *  caller can hand it straight back from onMount. The last subscriber to leave
 *  tears down the timer and the visibility listener. */
export function subscribe() {
  subscribers += 1;
  if (subscribers === 1 && events.status === 'idle') events.status = 'loading';
  if (!listening && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility);
    listening = true;
  }
  if (visible()) {
    readAll();
    startTimers();
  }
  return unsubscribe;
}

/** Drop one subscriber. Idempotent, and safe to call when none are left. */
export function unsubscribe() {
  subscribers = Math.max(0, subscribers - 1);
  if (subscribers > 0) return;
  stopTimers();
  if (listening && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', onVisibility);
    listening = false;
  }
}
