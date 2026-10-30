// Contract fixtures for the Seven Nights page (PLAN §8, frozen 2026-10-24). Used
// ONLY by the local review preview (LASTSIETCH_PREVIEW=1); the store imports
// this module behind import.meta.env.VITE_REVIEW_PREVIEW, which the production
// build defines as 'false', so none of it reaches the shipped bundle.
// Pick a scenario with ?sn=<name> on the /sietch URL.
import { COPY } from './copy.js';

const iso = (ms) => new Date(ms).toISOString();

function nightAt(n) {
  const c = COPY.nights[n - 1];
  return {
    n: c.n, key: c.key, starts_at: c.starts_at, ends_at: c.ends_at, activity: c.activity,
    copy: c,
    spots_public: n === 1
      ? [{ id: 'n1-a', name: 'The Eastern Porch', kind: 'porch', done: true },
         { id: 'n1-b', name: 'The Waystation Door', kind: 'porch', done: false },
         { id: 'n1-c', name: 'The Old Well Gate', kind: 'porch', done: false }]
      : n === 4 ? [{ id: 'n4-a', name: 'The Stall Arch', kind: 'stall', done: false }]
      : n === 6 ? [{ id: 'n6-a', name: 'The Hollow Dunes', kind: 'haunt', done: false }]
      : [],
    clue_current: n === 2 ? { seq: 2, of: 3, riddle: 'I hold water but I am not a cup. Travelers kneel at me at dusk.' } : null,
  };
}

function calendar(tonight) {
  return COPY.nights.map((c) => ({
    n: c.n, key: c.key,
    state: c.n < tonight ? 'past' : c.n === tonight ? 'tonight' : 'upcoming',
    earned: c.n < tonight ? [5, 7, 4, 9, 6, 8, 0][c.n - 1] : c.n === tonight ? 3 : 0,
  }));
}

const CATALOG = [
  { sku: 'sn-flair-nightwalker', name: 'Night Walker', kind: 'flair', lane: 'prefs', pref_key: 'nightwalker', price: 5, limit_left: 0, owned: true, disabled: false, blurb: 'A badge for your Hoard, the leaderboard and your profile card.' },
  { sku: 'sn-mapdot-lantern', name: 'Lantern', kind: 'mapdot', lane: 'prefs', pref_key: 'lantern', price: 8, limit_left: 1, owned: false, disabled: true, opens_at: '2026-10-30T00:00:00Z', blurb: 'A lantern marker for your own dot on portal maps.' },
  { sku: 'sn-contraband-swatch', name: 'Contraband Swatch', kind: 'swatch', lane: 'bank', price: 10, limit_left: 1, owned: false, disabled: false, applies_to: ['garments', 'holsters', 'vehicles'], blurb: 'A dye swatch smuggled past the Sardaukar.' },
  { sku: 'sn-role-survivor', name: 'Seven Nights Survivor', kind: 'role', lane: 'role', price: 12, limit_left: 1, owned: false, disabled: false, blurb: 'A permanent Discord role.' },
  { sku: 'sn-solari-purse', name: 'Solari Purse', kind: 'solari', lane: 'wallet', price: 10, limit_left: 2, owned: false, disabled: false, blurb: '25,000 Solari to your wallet.' },
  { sku: 'sn-healkit', name: 'Healkit Mk6 Bundle', kind: 'consumable', lane: 'bank', price: 4, limit_left: 3, owned: false, disabled: false, blurb: 'Five top-grade healkits.' },
  { sku: 'sn-iodine', name: 'Iodine Pills', kind: 'consumable', lane: 'bank', price: 4, limit_left: 3, owned: false, disabled: false, blurb: 'Five pills for the irradiated wrecks.' },
  { sku: 'sn-spec-combat', name: 'Combat Training Writ', kind: 'spec', lane: 'spec', price: 15, limit_left: 1, owned: false, disabled: false, applies_to: ['Combat specialization'], blurb: 'Drills from the old war-masters, written down for the night. +1,500 Combat specialization XP, up to the track cap. Delivered when you next log out.' },
];

function base(tonight, over = {}) {
  const now = Date.now();
  return {
    night: tonight ? nightAt(tonight) : null,
    progress: {
      visit: { used: 2, cap: 2 },
      knock: { used: 1, cap: 3 },
      landsraad: { used: 0, cap: tonight === 3 ? 4 : 2 },
      activity: { used: 0, cap: tonight === 2 ? 3 : 1 },
    },
    eligible: true,
    balance: 21,
    earned_total: 26,
    calendar: calendar(tonight || 0),
    catalog: CATALOG,
    orders: [
      { id: 11, sku: 'sn-flair-nightwalker', name: 'Night Walker', lane: 'prefs', price: 5, status: 'delivered', created_at: iso(now - 3600e3) },
    ],
    ledger: [
      { id: 6, delta: 1, kind: 'earn', source: 'knock', night: '2026-10-26', created_at: iso(now - 600e3) },
      { id: 5, delta: 2, kind: 'earn', source: 'visit', night: '2026-10-26', created_at: iso(now - 1800e3) },
      { id: 4, delta: -5, kind: 'order', order_id: 11, created_at: iso(now - 3600e3) },
      { id: 3, delta: 25, kind: 'admin_adjust', reason: 'Costume contest', created_at: iso(now - 86400e3) },
      { id: 2, delta: 1, kind: 'earn', source: 'landsraad', night: '2026-10-25', created_at: iso(now - 90000e3) },
    ],
    prefs: { flair: 'nightwalker', mapdot: null, board_hidden: false },
    flags: { earning: true, shop: false },
    _preview_now: tonight ? iso(Date.parse(COPY.nights[tonight - 1].starts_at) + 3 * 3600e3) : null,
    ...over,
  };
}

const SCENARIOS = {
  night1: () => base(1),
  night2: () => base(2),
  night3: () => base(3),
  night4: () => base(4, { flags: { earning: true, shop: true } }),
  night5: () => base(5, { flags: { earning: true, shop: true } }),
  night7: () => base(7, { flags: { earning: true, shop: true } }),
  prelaunch: () => base(0, { night: null, balance: 0, earned_total: 0, ledger: [], orders: [] }),
  ineligible: () => base(1, { eligible: false, balance: 0, earned_total: 0, ledger: [], orders: [] }),
  // Gate off: the server sends no night and no progress, still a 200.
  closed: () => base(1, { night: null, progress: null, flags: { earning: false, shop: false } }),
  empty: () => base(1, { balance: 0, earned_total: 0, ledger: [], orders: [], prefs: { flair: null, mapdot: null, board_hidden: false } }),
  after: () => base(0, { night: null, flags: { earning: false, shop: true }, _preview_now: '2026-11-02T12:00:00Z' }),
};

export function fixtureState(name) {
  if (name === 'unlinked') {
    const err = new Error('GET /portal/sietch/state -> 409');
    err.status = 409;
    throw err;
  }
  return (SCENARIOS[name] || SCENARIOS.night1)();
}

// A Knock in the preview walks through every result the contract can return, so
// each state can be seen without a game client.
const KNOCKS = {
  porches: [{ result: 'treat', award: 1 }, { result: 'miss', dist_text: '1.2 km' }, { result: 'stale' }, { result: 'amtal' }],
  riddles: [{ result: 'miss' }, { result: 'treat', award: 2, next_clue: { seq: 3, of: 3, riddle: 'Where the sand sings at night, I wait in a ring of stone.' } }],
  seek: [{ result: 'band', band: 'cold' }, { result: 'band', band: 'cool' }, { result: 'band', band: 'warm' }, { result: 'band', band: 'scorching' }, { result: 'treat', award: 10 }],
};
let knockStep = 0;

export function fixtureKnock(activity) {
  const list = KNOCKS[activity] || KNOCKS.porches;
  const r = list[knockStep % list.length];
  knockStep += 1;
  return r;
}

export const FIXTURE_BOARD = {
  rows: [
    { rank: 1, name: 'Player One', earned: 41, flair: 'nightwalker' },
    { rank: 2, name: 'A wanderer', earned: 38, flair: null },
    { rank: 3, name: 'Player Two', earned: 33, flair: null },
    { rank: 4, name: 'Player Three', earned: 29, flair: 'nightwalker' },
    { rank: 5, name: 'Player Four', earned: 22, flair: null },
  ],
};
