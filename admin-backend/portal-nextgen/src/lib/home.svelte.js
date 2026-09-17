// Home store. Every read the dashboard makes lives here, on the shape
// traffic.svelte.js already proved: ONE timer per read, polling only while the
// document is visible, one immediate refetch on return, and a monotonic token
// per read so a slow response can never overwrite a newer one.
//
// The reason is arithmetic. Home ran five setIntervals straight out of the
// route, none of them visibility-gated, so a portal tab left open behind a game
// window spent 8,640 live reads, 2,880 overviews, 1,440 standings and 720
// announcement reads a day on a page nobody was looking at. The same tab
// returning from the background then showed up to a minute of stale desert
// until the next tick, which is exactly when a player checks it.
//
// Null is never rendered as 0. A missing scalar stays null the whole way to the
// verdict, which reads it as ABSENT: a nullable signal that gets a `?? 0` turns
// "we could not read your rewards" into "you have nothing waiting", which is a
// fabricated statement about a player's own account.
import { api } from './api.js';
import { auth } from './auth.svelte.js';
import { mailbox } from './mailbox.svelte.js';
import {
  DEFAULT_MAP_KEY, deriveSubLines, deriveVerdict, dimForPartition, mapKeyFor,
  readSignals,
} from './homeVerdict.js';

// The Deep Desert is the public reading and the fallback whenever we cannot
// place the player's character on a map.
const DEFAULT_MAP = DEFAULT_MAP_KEY;

// One cadence per read. These are the cadences Home already ran at; the change
// is that they now stop while the tab is hidden.
const POLL_MS = {
  live: 10_000,
  overview: 30_000,
  standings: 60_000,
  announcement: 120_000,
  home: 60_000,
};

export const home = $state({
  status: 'idle',      // idle | loading | ready
  // The composition read's own state. Separate from `status`, which is the
  // desert: a personal panel that waits on charName being non-null waits
  // forever the moment /portal/home/v2 starts failing.
  homeStatus: 'loading', // loading | ready | failed
  // Raw payloads, one per read. The public cards render straight off these.
  raw: {
    data: null,        // /portal/maps/{key}/data: coriolis, instances, spice_mediums
    live: null,        // /portal/maps/{key}/live: spice, worms, sandstorm
    overview: null,    // /portal/server/overview: status, build, world, events
    standings: null,   // /portal/landsraad/standings
    announcement: null, // /portal/announcement
    home: null,        // /portal/home/v2 (linked only)
  },
  // Flat facts the verdict and the instruments read. Never raw payloads.
  signals: readSignals({}, { booting: true, mapKey: DEFAULT_MAP, dim: 0 }),
  verdict: { rank: 0, text: 'Reading the desert...', tone: 'idle', timerUtc: null },
  subLines: [],
  lastReadAt: null,    // our clock at the last live read, for the freshness line
});

// What the reads themselves cannot know: our clock, the selected instance, the
// board in play, and whether the first desert read has landed. Held outside the
// $state object because readSignals rebuilds `home.signals` wholesale.
const ctx = { booting: true, mapKey: DEFAULT_MAP, dim: 0, now: Date.now() };

/** Rebuild the flat facts, then the verdict and its supporting lines, from
 *  whatever has landed so far. Pure downstream of here: everything the ladder
 *  reads is a plain value and the three functions live in homeVerdict.js. */
function recompute() {
  ctx.now = Date.now();
  ctx.authed = auth.status === 'authed';
  ctx.linkCount = (auth.linked || []).length;
  ctx.instLabel = instanceLabel();
  ctx.unread = mailbox.status === 'ready' ? mailbox.unread : null;
  home.signals = readSignals(home.raw, ctx);
  const gate = {
    authed: auth.status === 'authed',
    anon: auth.status === 'anon',
    loading: auth.status === 'loading',
  };
  home.verdict = deriveVerdict(home.signals, gate);
  home.subLines = deriveSubLines(home.signals, gate, home.verdict);
}

/** Label of the selected instance on the board in play (PvE / Habbanya / ...). */
function instanceLabel() {
  const list = (home.raw.data && home.raw.data.instances) || null;
  if (!list) return ctx.dim === 0 ? 'PvE' : 'PvP';
  const hit = list.find((i) => i.dim === ctx.dim);
  return hit ? hit.label : null;
}

// Monotonic token per read. A tab regaining focus fires an immediate refetch
// while the previous request is still open; whichever lands second must not win
// on age alone.
const seq = { data: 0, live: 0, overview: 0, standings: 0, announcement: 0, home: 0 };

let subscribers = 0;
let homeInFlight = false;
let listening = false;
const timers = { live: null, overview: null, standings: null, announcement: null, home: null };

/** The player's stored Deep Desert instance preference. Read as a preference,
 *  not as a signal: an unset or unparseable key is the default instance. */
function storedDim() {
  try {
    const stored = Number(localStorage.getItem('ls-dim'));
    return Number.isFinite(stored) ? stored : 0;
  } catch (e) {
    return 0;
  }
}

function visible() {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

/** The map board Home is reading. Deep Desert unless the linked character is
 *  online somewhere we have a board for. */
function mapKey() {
  return ctx.mapKey || DEFAULT_MAP;
}

export async function loadData() {
  const s = ++seq.data;
  const key = mapKey();
  try {
    const r = await api.maps.data(key);
    if (s !== seq.data) return;
    home.raw.data = r;
    // The board's instance table is what turns a partition into a dimension, so
    // the placement can only resolve once this read has landed.
    applyPartitionDim();
    recompute();
  } catch (e) {}
}

export async function loadLive() {
  const s = ++seq.live;
  const key = mapKey();
  try {
    const r = await api.maps.live(key);
    if (s !== seq.live) return;
    home.raw.live = r;
    home.lastReadAt = Date.now();
  } catch (e) {
    if (s !== seq.live) return;
  }
  ctx.booting = false;
  home.status = 'ready';
  recompute();
}

export async function loadOverview() {
  const s = ++seq.overview;
  try {
    const r = await api.server.overview();
    if (s !== seq.overview) return;
    home.raw.overview = r;
    recompute();
  } catch (e) {}
}

export async function loadStandings() {
  const s = ++seq.standings;
  try {
    const r = await api.landsraad.standings();
    if (s !== seq.standings) return;
    home.raw.standings = r;
    recompute();
  } catch (e) {}
}

export async function loadAnnouncement() {
  const s = ++seq.announcement;
  try {
    const r = await api.server.announcement();
    if (s !== seq.announcement) return;
    home.raw.announcement = r;
    recompute();
  } catch (e) {}
}

/** The one composition read, session-gated and linked-only. Anonymous and
 *  unlinked callers never send it: it answers 401 for them and a 401 in the
 *  network log every 60s is noise, not a signal. */
export async function loadHomeOverview() {
  if (auth.status !== 'authed' || !(auth.linked || []).length) {
    home.raw.home = null;
    home.homeStatus = 'loading';
    return;
  }
  if (homeInFlight) return;
  homeInFlight = true;
  const s = ++seq.home;
  try {
    const r = await api.home.overview();
    if (s !== seq.home) return;
    home.raw.home = r;
    home.homeStatus = 'ready';
    followCharacter();
    recompute();
  } catch (e) {
    if (s !== seq.home) return;
    // A failed composition read seals the personal column; it never blanks the
    // public reading underneath it. The board goes back to the public Deep
    // Desert too: it was only ever on Hagga because this read placed the
    // character there, and we can no longer say that it does.
    home.raw.home = null;
    home.homeStatus = 'failed';
    setMapKey(DEFAULT_MAP);
    recompute();
  } finally {
    homeInFlight = false;
  }
}

/** Point the board at the map the linked character is standing on. Only while
 *  the character is ONLINE: a last-known map for somebody who logged out an hour
 *  ago is not where they are, and rank 3 would be reading a storm at a position
 *  nobody occupies. Anything without a live board of its own (the hub cities,
 *  the Overmap) falls back to the stored Deep Desert instance. */
function followCharacter() {
  const c = (home.raw.home && home.raw.home.character) || null;
  const key = c && c.online === true ? mapKeyFor(c.current_map) : null;
  setMapKey(key || DEFAULT_MAP);
  applyPartitionDim();
}

// The (board, partition) pair the placement was last applied for.
const applied = { mapKey: null, partition: null };

/** Resolve the instance from the character's partition, through the board's own
 *  instance table. Hagga's three sietches share one terrain and one friendly
 *  name, so the partition is the ONLY thing that says which of them a player is
 *  standing in; reading the sietch off 'Hagga Basin' would put a Kulon player's
 *  storm on the Habbanya sand. A null partition, or a board whose instances
 *  carry no partition at all (the Deep Desert), leaves the stored instance
 *  exactly where it was.
 *
 *  Applied ONCE per board and partition. The composition read repeats every 60
 *  seconds and re-applying it every time silently dragged the toggle back: a
 *  player reading Kulon while their character stands in Habbanya got one poll
 *  of their pick and then the placement again. */
function applyPartitionDim() {
  const c = (home.raw.home && home.raw.home.character) || null;
  if (!c || c.online !== true) return;
  const key = mapKey();
  const partition = c.current_partition != null ? c.current_partition : null;
  if (applied.mapKey === key && applied.partition === partition) return;
  const instances = (home.raw.data && home.raw.data.instances) || null;
  const dim = dimForPartition(instances, partition);
  if (dim == null) return;
  ctx.dim = dim;
  applied.mapKey = key;
  applied.partition = partition;
}

const READS = {
  live: loadLive,
  overview: loadOverview,
  standings: loadStandings,
  announcement: loadAnnouncement,
  home: loadHomeOverview,
};

function stopTimers() {
  for (const key of Object.keys(timers)) {
    if (timers[key] != null) { clearInterval(timers[key]); timers[key] = null; }
  }
}

function startTimers() {
  if (subscribers === 0) return;
  for (const key of Object.keys(READS)) {
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

/** Select the instance (dimension) the board reads. Persisted for the Deep
 *  Desert only: `ls-dim` is the player's DD PvE/PvP preference and a Hagga
 *  sietch pick must not overwrite it. */
export function setDim(d) {
  ctx.dim = d;
  recompute();
  if (mapKey() !== DEFAULT_MAP) return;
  try { localStorage.setItem('ls-dim', String(d)); } catch (e) {}
}

/** Point the board at a different map. Re-reads the board data and the live
 *  overlay once, immediately, rather than waiting out the next tick on a map
 *  the player has already left. */
export function setMapKey(key) {
  const next = key || DEFAULT_MAP;
  if (next === ctx.mapKey) return;
  ctx.mapKey = next;
  // Back on the public board, the player's own instance comes back with it. A
  // Hagga placement writes a dimension the Deep Desert does not have (Amtal is
  // dim 2), so a player who logs out of Amtal was left reading a board with no
  // instance selected and every desert card saying "no reading".
  if (next === DEFAULT_MAP) ctx.dim = storedDim();
  applied.mapKey = null;
  applied.partition = null;
  home.raw.data = null;
  home.raw.live = null;
  recompute();
  if (visible()) { loadData(); loadLive(); }
}

/** Subscribe a surface to the shared poll. Returns the unsubscribe function so
 *  a caller can hand it straight back from onMount. The last subscriber to
 *  leave tears down every timer and the visibility listener. */
export function subscribe() {
  subscribers += 1;
  if (subscribers === 1) {
    ctx.dim = storedDim();
    if (home.status === 'idle') home.status = 'loading';
    recompute();
    loadData();
  }
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
