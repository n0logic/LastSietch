// Shared Login-Rewards state. One $state proxy imported by the route + every
// rewards component so the streak, the daily calendar, and the weekly/monthly
// tracks stay in sync without prop-drilling. Mutate the proxy's PROPERTIES in
// place (never reassign `rewards` itself), so the shared reference every
// component holds stays live.
//
// The overview read is never gated (browsing always works). A claim is optimistic:
// flip the affected cell locally, fire the CSRF write, and on any throw
// ({ok:false} / non-2xx) revert to the pre-write snapshot. The system ships DARK
// behind LASTSIETCH_REWARD_ENABLED; a {ok:true, status:'deferred'} response is rendered
// as "not yet enabled" and reverts, NEVER as a success. On success (status
// 'applied' OR 'replay') the overview is refetched to reconcile server truth.
import { api } from './api.js';

export const rewards = $state({
  status: 'idle',        // idle | loading | ready | error
  enabled: false,        // LASTSIETCH_REWARD_ENABLED mirror (server authoritative)
  serverNowUtc: null,    // server clock (UTC) at overview time
  nextClaimUtc: null,    // next claim reset instant (absolute)

  streak: {
    current: 0,          // consecutive login days (total; big number)
    best: 0,             // best streak on record
    logged_today: false, // player logged in today (streak counts)
    milestone_next: 7,   // day count of the next milestone
    cycle_day: 0,        // position in the repeating 7-day ramp cycle (gauge)
  },

  // daily.today = { date, cycle_day, amount (ACCUMULATED claimable), claimed, claimable }
  // ramp = [7 ints]; milestones = [3,7]; cycle = 7 grid cells for the current cycle:
  //   { cycle_day, amount, state, is_today, milestone }; state in
  //   { claimed, claimable, pending, upcoming }. weeks = rows in the month grid.
  // calendar = 28 REAL dated cells for the current period (Monday-aligned,
  //   index 0 = period start): { date, logged, claimed, is_today, is_future,
  //   amount, ramp_day, milestone }; amount/ramp_day are null for future days
  //   and for past days with no login. Sent alongside `cycle`, not replacing
  //   it; falls back to [] on an older backend so `cycle` keeps rendering.
  daily: { today: null, ramp: [], milestones: [], cycleLen: 7, weeks: 4,
           cycle: [], calendar: [], claimableTotal: 0, claimableDays: 0 },

  // weekly = single card object (NOT an array):
  //   { week_key, template_id, name, icon, tier, rarity, quality_level,
  //     requirement, unlocked, claimed, claimable }
  weekly: null,

  // monthly = the same flattened card shape as `weekly` once the overview lands.
  // The monthly claim has been LIVE since 2026-08-24; { status: 'coming_soon' } is
  // only the pre-load placeholder (and what a backend older than that sends).
  monthly: { status: 'coming_soon' },

  claiming: false,       // a claim write is in flight
  notice: '',            // transient result line (claim outcomes)
  noticeTone: 'info',    // info | ok | warn | error

  // Transient monthly-reveal dialog. Set ONLY by a successful monthly claim below,
  // never by MonthlyTrack (which shows the gate and never claims on its own). The
  // card is a snapshot taken before the write, because a successful claim
  // reconciles and replaces `monthly` with the server's post-claim row.
  reveal: { open: false, reward: null },
});

/** Close the monthly reveal (also the reset used on boot). */
export function closeReveal() {
  rewards.reveal = { open: false, reward: null };
}

function setNotice(text, tone = 'info') {
  rewards.notice = text || '';
  rewards.noticeTone = tone;
}

function applyOverview(r) {
  rewards.enabled = r?.enabled === true;
  rewards.serverNowUtc = r?.server_now_utc || null;
  rewards.nextClaimUtc = r?.next_claim_utc || null;

  const s = r?.streak || {};
  rewards.streak = {
    current: Number(s.current) || 0,
    best: Number(s.best) || 0,
    logged_today: s.logged_today === true,
    milestone_next: Number(s.milestone_next) || 7,
    cycle_day: Number(s.cycle_day) || 0,
  };

  const d = r?.daily || {};
  rewards.daily = {
    today: d.today || null,
    ramp: Array.isArray(d.ramp) ? d.ramp : [],
    milestones: Array.isArray(d.milestones) ? d.milestones : [],
    cycleLen: Number(d.cycle_len) || 7,
    weeks: Number(d.weeks) || 4,
    cycle: Array.isArray(d.cycle) ? d.cycle : [],
    calendar: Array.isArray(d.calendar) ? d.calendar : [],
    claimableTotal: Number(d.claimable_total) || 0,
    claimableDays: Number(d.claimable_days) || 0,
  };

  rewards.weekly = r?.weekly && typeof r.weekly === 'object' ? r.weekly : null;
  rewards.monthly = r?.monthly && typeof r.monthly === 'object'
    ? r.monthly
    : { status: 'coming_soon' };
}

/** Boot: pull the overview (enabled flag + streak + daily/weekly/monthly). */
export async function loadOverview() {
  rewards.status = 'loading';
  try {
    applyOverview(await api.rewards.overview());
    rewards.status = 'ready';
  } catch (e) {
    rewards.status = 'error';
  }
}

/** Quiet refetch after a write so the UI reconciles with server truth (streak
 *  bump, next-claim clock, cell states the optimistic path only approximated).
 *  Keeps the optimistic view on failure; a later load fixes it. */
async function reconcile() {
  try { applyOverview(await api.rewards.overview()); } catch (e) { /* keep view */ }
}

/** Flip every claimable cycle cell to `state` (the accumulate claim sweeps the whole
 *  pool at once), returning the [cell, prevState] pairs so a failed claim can revert. */
function flipClaimableCells(state) {
  const flips = [];
  const arr = rewards.daily.cycle;
  if (Array.isArray(arr)) {
    for (const c of arr) {
      if (c.state === 'claimable') { flips.push([c, c.state]); c.state = state; }
    }
  }
  return flips;
}
function restoreCells(flips) { for (const [c, s] of flips) c.state = s; }

/** Shared claim core. `revert` undoes the optimistic mutation. Returns the parsed
 *  success response (truthy) or false. Deferred (DARK) and thrown errors both
 *  revert; 'applied' and 'replay' are both treated as success. */
async function runClaim(kind, revert) {
  rewards.claiming = true;
  try {
    const r = await api.rewards.claim({ reward_kind: kind });
    if (r?.status === 'deferred') {
      revert();
      setNotice(r?.message || 'Login rewards are not enabled yet. Nothing claimed.', 'warn');
      return false;
    }
    await reconcile();
    return r; // status applied | replay
  } catch (e) {
    revert();
    setNotice(friendly(e, 'The claim did not go through. Nothing claimed.'), 'error');
    return false;
  } finally {
    rewards.claiming = false;
  }
}

/** Claim today's daily Solari (reward_kind daily_solari). Online-safe (no offline
 *  gate). Optimistically flips today + its calendar cells to claimed. */
export async function claimDaily() {
  if (rewards.claiming) return false;
  const today = rewards.daily.today;
  // Guard on the POOL, not just today's rung: the backend releases already-earned days
  // whether or not today has landed, so refusing here would re-strand exactly the
  // Solari that change exists to free.
  const pool = Number(rewards.daily?.claimableTotal) || 0;
  if (!today?.claimable && pool <= 0) return false;

  const prev = { claimed: today.claimed, claimable: today.claimable,
                 total: rewards.daily.claimableTotal };
  const flips = flipClaimableCells('claimed');
  today.claimed = true;
  today.claimable = false;
  const amtGuess = Number(today.amount) || 0;
  rewards.daily.claimableTotal = 0;

  const r = await runClaim('daily_solari', () => {
    today.claimed = prev.claimed;
    today.claimable = prev.claimable;
    rewards.daily.claimableTotal = prev.total;
    restoreCells(flips);
  });
  if (r) {
    const amt = Number(r.amount) || amtGuess;
    setNotice(amt > 0 ? `Claimed ${amt.toLocaleString()} Solari.` : 'Reward claimed.', 'ok');
    return true;
  }
  return false;
}

/** Claim the weekly item (reward_kind weekly_item). Unlocks at a 7-day streak;
 *  a 'locked' refusal surfaces the streak requirement. */
export async function claimWeekly() {
  if (rewards.claiming) return false;
  const w = rewards.weekly;
  if (!w?.claimable) return false;

  const prev = { claimed: w.claimed, claimable: w.claimable };
  const name = w.name;
  w.claimed = true;
  w.claimable = false;

  const r = await runClaim('weekly_item', () => {
    w.claimed = prev.claimed;
    w.claimable = prev.claimable;
  });
  if (r) {
    setNotice(name ? `Claimed ${name}.` : 'Weekly reward claimed.', 'ok');
    return true;
  }
  return false;
}

/** Claim the monthly pre-augmented weapon (reward_kind monthly_augment). Unlocks on a
 *  day COUNT within the 28-day period, not a streak, so a missed day never resets it. */
export async function claimMonthly() {
  if (rewards.claiming) return false;
  const m = rewards.monthly;
  if (!m?.claimable) return false;

  const prev = { claimed: m.claimed, claimable: m.claimable };
  const name = m.name;
  const card = m.template_id ? {
    template_id: m.template_id,
    name: m.name,
    icon: m.icon,
    grade: m.quality_level,
    rarity: m.rarity,
    type: m.type || 'weapon',
  } : null;
  m.claimed = true;
  m.claimable = false;

  const r = await runClaim('monthly_augment', () => {
    m.claimed = prev.claimed;
    m.claimable = prev.claimable;
  });
  if (r) {
    setNotice(name ? `Claimed ${name}.` : 'Monthly reward claimed.', 'ok');
    rewards.reveal = { open: card !== null, reward: card };
    return true;
  }
  return false;
}

/** Claim everything outstanding in one press: the accumulated daily Solari, then the
 *  weekly weapon, then the monthly augmented weapon.
 *
 *  Deliberately three sequential calls to the existing per-kind endpoint rather than one
 *  "claim all" route. Each call re-derives its own eligibility server-side and carries its
 *  own deterministic idempotency key, so the client cannot talk its way into a reward it
 *  is not owed, and a tier that refuses (a full CHOAM bank fails the item mints while
 *  Solari still lands) never blocks the others. Runs in reward order so the cheapest,
 *  most reliable tier is banked first.
 *
 *  Each step reconciles with the server before the next begins, so `claimable` is always
 *  read fresh rather than from the snapshot taken when the button was pressed. */
export async function claimAll() {
  if (rewards.claiming) return false;
  const done = [];
  const skipped = [];

  for (const [label, claimable, fn] of [
    // Pool-aware: unclaimed days already earned are collectible even before today's
    // own rung lands, matching the backend's pool-first gate.
    ['daily', () => rewards.daily?.today?.claimable === true
       || (Number(rewards.daily?.claimableTotal) || 0) > 0, claimDaily],
    ['weekly', () => rewards.weekly?.claimable === true, claimWeekly],
    ['monthly', () => rewards.monthly?.claimable === true, claimMonthly],
  ]) {
    if (!claimable()) continue;
    // Each helper sets its own notice; the summary below replaces it once every tier
    // has run, so a multi-tier claim ends on one line instead of three flickering ones.
    const ok = await fn();
    (ok ? done : skipped).push(label);
  }

  if (!done.length && !skipped.length) {
    setNotice('Nothing to claim right now.', 'info');
    return false;
  }
  if (done.length > 1 || (done.length && skipped.length)) {
    const parts = [`Claimed ${done.join(' + ')}.`];
    if (skipped.length) parts.push(`${skipped.join(' and ')} did not go through.`);
    setNotice(parts.join(' '), skipped.length ? 'warn' : 'ok');
  }
  return done.length > 0;
}

/** Map a claim error code to friendly copy (the backend's error vocabulary).
 *
 * Prefers the SERVER's own sentence when it sent one. `e.message` is the TOKEN;
 * `e.data.message` is the human sentence the endpoint built, which is often far
 * more specific than anything keyed off a token can be. Same pattern as
 * karum.svelte.js:59 and augments.svelte.js, and for the same reason: a token
 * map can only ever say the most general true thing.
 *
 * 🔴 `unavailable` is why this matters. It is a 502 from THREE distinct service
 * failures -- a login-days fetch that threw, a relay that returned nothing, and
 * a writer refusal nothing classified. It does NOT mean "you have nothing to
 * claim". It said exactly that until 2026-08-24, while every monthly claim was
 * failing on a relay field drop, so the one message a stuck player saw was the
 * one thing that was definitely false. */
function friendly(e, fallback) {
  const t = (e && e.message) || '';
  const server = (e && e.data && typeof e.data.message === 'string') ? e.data.message.trim() : '';
  if (server) return server;
  const map = {
    not_logged_in: 'Sign in first, then claim.',
    already_claimed: 'You already claimed this. Come back after the next reset.',
    locked: 'Reach a 7-day streak to unlock this reward.',
    bank_full: 'Your CHOAM bank is full. Free up some space in-game, then claim again.',
    bank_unopened: 'Open your CHOAM bank in-game once so it exists, then claim.',
    unavailable: 'The rewards service did not respond. Nothing was claimed - try again shortly.',
    csrf: 'Your session expired. Refresh the page and try again.',
    rate_limited: 'Slow down a moment, then try again.',
  };
  return map[t] || fallback;
}

/** Load everything (called once auth resolves to authed). */
export function loadAll() {
  // A boot is a reset (sign-in, account switch): never reopen a stale reveal.
  closeReveal();
  loadOverview();
}
