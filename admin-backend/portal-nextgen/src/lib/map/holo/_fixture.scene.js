// Static fixture snapshot (contract 4.6). Lets dev-2 render and eyeball the
// holo viewer with no engine and no route. dev-1 never imports this. Shape is
// the onScene payload frozen in contract 4.1.

export const fixtureScene = {
  ready: 1,
  view: 1000,
  grid: { cols: 9, rows: 9 },
  cal: { originX: 0, originY: 0, spanX: 9, spanY: 9 },
  backdrop: { type: 'sand' },
  dim: 1,
  hidden: {},

  // marker = [nx, ny, catId, typeIdx]; catId indexes catIndex -> category key.
  markers: [
    [180, 240, 0, 10], [205, 262, 0, 11], [168, 300, 0, 10],
    [220, 210, 0, 12], [520, 480, 1, 5], [548, 505, 1, 6],
    [760, 250, 2, 7], [735, 288, 2, 7], [640, 700, 3, 3],
    [610, 735, 3, 3], [420, 560, 4, 2], [300, 640, 5, 8],
    [880, 620, 1, 5], [120, 820, 2, 7], [815, 815, 4, 2],
  ],
  catIndex: { 0: 'ore', 1: 'poi', 2: 'enemy', 3: 'salvage', 4: 'hazard', 5: 'flora' },
  catColors: {
    ore: '#c9a24a', poi: '#4ab0c9', enemy: '#f04040', salvage: '#b8863c',
    hazard: '#d46a1c', flora: '#7ab04a', other: '#9a8a6a', spice: '#a24bd4',
  },
  typeIcons: [],
  // typeIndex[typeIdx] = type NAME. Ore markers here use typeIdx 10/11/12;
  // 10/11 are Titanium/Stravidium variants so they get ore emphasis.
  typeIndex: ['', '', 'Aluminum', 'Salvage', '', 'Cave', 'Cave', 'EnemyCamp',
    'Flora', '', 'TitaniumOre', 'StravidiumMass', 'IronOre'],
  legend: [{ key: 'spice', label: 'Spice fields', color: '#a24bd4' }],

  spiceActive: {
    dimensions: {
      1: {
        large_active: true,
        ram_active_fields: [
          { sector: 'E5', nx: 520, ny: 470 },
          { sector: 'C7', nx: 300, ny: 690 },
        ],
      },
    },
  },
  candidates: { B2: [210, 190], G7: [720, 660], D4: [400, 400] },
  mediums: [
    [300, 320, 'C3', true], [650, 430, 'F4', false], [500, 800, 'E8', false],
  ],

  worms: [
    { id: 1, nx: 480, ny: 520, threat: 'surfaced', age_s: 25, sector: 'E5' },
    { id: 2, nx: 720, ny: 250, threat: 'enraged', age_s: 90, sector: 'G3' },
    { id: 3, nx: 250, ny: 700, threat: 'breaching', age_s: 15, sector: 'C7' },
  ],

  sandstorm: {
    dimensions: {
      1: {
        storm_sector: 'E8', storm_scanned_utc: '2026-07-02T00:00:00Z',
        center_nx: 788.1, center_ny: 554.8, radius_nr: 184.5, heading_yaw: 36.8, stage: 2,
      },
    },
  },

  me: {
    available: true,
    self: { nx: 450, ny: 450, dim: 1, online: true },
    bases: [{ nx: 400, ny: 600, dim: 1, kind: 'base', name: 'Home' }],
    vehicles: [],
  },
  meAuthed: true,
  meFresh: true,
  meShow: { self: true, bases: true, vehicles: true },

  waypoints: [{ id: 'wp1', nx: 600, ny: 600, note: 'Cache' }],
};

export default fixtureScene;
