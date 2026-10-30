// Amtal PvP board store. The public /portal/amtal/pvp read (kill boards, live kill
// streaks, streak bounties ended), on the same shape as amtal.svelte.js: ONE timer,
// polling only while the document is visible, and a monotonic token so a slow
// response can never overwrite a newer one.
//
// `enabled` mirrors LASTSIETCH_AMTAL_PVP_ENABLED. While it is off the section is not shown.
import { api } from './api.js';

// Two minutes, matched to the server cache: kills land in telemetry every 90 s.
const POLL_MS = 120_000;

export const pvp = $state({
  status: 'idle',        // idle | loading | ready | error
  enabled: false,
  available: false,      // false = the read failed upstream; never shown as an empty board
  weekLabel: null,
  streakStep: 5,
  streakSpice: 2500,
  season: [],            // [{name, kills, deaths, kd, best_streak, staff}]
  week: [],
  streaks: [],           // [{name, kills, bounty, since, staff}]
  collected: [],         // [{victim, victim_staff, collector, collector_staff, streak, bounty, at}]
  titles: null,          // {last_week_label, weekly: {title: name|null}, rules}
});

let seq = 0;
let timer = null;
let subscribers = 0;

export async function loadPvp() {
  const s = ++seq;
  if (pvp.status === 'idle') pvp.status = 'loading';
  try {
    const r = await api.amtal.pvp();
    if (s !== seq) return;
    pvp.enabled = r?.enabled === true;
    pvp.available = r?.available === true;
    if (pvp.available) {
      pvp.weekLabel = r.week_label ?? null;
      if (typeof r.streak_step === 'number') pvp.streakStep = r.streak_step;
      if (typeof r.streak_spice === 'number') pvp.streakSpice = r.streak_spice;
      pvp.season = Array.isArray(r.season) ? r.season : [];
      pvp.week = Array.isArray(r.week) ? r.week : [];
      pvp.streaks = Array.isArray(r.streaks) ? r.streaks : [];
      pvp.collected = Array.isArray(r.collected) ? r.collected : [];
      pvp.titles = r.titles && typeof r.titles === 'object' ? r.titles : null;
    }
    pvp.status = 'ready';
  } catch (e) {
    if (s !== seq) return;
    if (pvp.status !== 'ready') pvp.status = 'error';
  }
}

function tick() {
  if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
  loadPvp();
}

function onVisible() {
  if (document.visibilityState === 'visible') loadPvp();
}

export function subscribePvp() {
  subscribers += 1;
  if (subscribers === 1) {
    loadPvp();
    timer = setInterval(tick, POLL_MS);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisible);
    }
  }
  return unsubscribePvp;
}

export function unsubscribePvp() {
  subscribers = Math.max(0, subscribers - 1);
  if (subscribers === 0) {
    if (timer) { clearInterval(timer); timer = null; }
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisible);
    }
  }
}
