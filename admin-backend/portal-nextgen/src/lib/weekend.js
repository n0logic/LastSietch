// The weekend rotation, for the This weekend / Next weekend card (player portal
// plan B2) and the Help "Weekend events" entry.
//
// PLAIN DATA AND PURE FUNCTIONS. No Svelte, no store, no fetch: the card hands in
// the published events and the clock, and the suite imports this module under
// node and runs it for real.
//
// The rotation is the owner ruling of 2026-10-23
// (the design notes (not published)): every
// weekend, Friday 00:00 to Sunday 24:00 America/New_York, picked by the Friday's
// ordinal in its month. A fifth Friday is the owner's pick per month, so it has
// no planned effect here.
//
// The effect values are the ones ops/weekend-events/event-effect-worker.py sets
// (LIVE and RESTART), against the values of record in
// ops/game-config/values-of-record.json. Every meaning line says what changes
// against the normal rate and nothing about WHERE: scope is the event's, never
// assumed (an effect can be scoped to Amtal).

export const ET = 'America/New_York';

export const EFFECTS = {
  xp: {
    name: 'Double XP weekend',
    chip: '2x character XP',
    meaning: 'Character XP is doubled: twice the normal 1x rate.',
    restart: true,
  },
  spec_xp: {
    name: 'Double Landsraad weekend',
    chip: '2x spec XP from Landsraad',
    meaning: 'Landsraad mission rewards give twice the normal specialization XP.',
    restart: false,
  },
  crafting: {
    name: 'Crafting weekend',
    chip: 'Crafting 20% off',
    meaning: 'Crafting costs 20% less material than normal.',
    restart: true,
  },
  harvest: {
    name: 'Harvest weekend',
    chip: '2x harvest',
    meaning: 'Cutteray and vehicle mining yield is doubled on top of each map\'s normal rate. Hand gathering is unchanged.',
    restart: false,
  },
};

// Friday ordinal in the month -> effect. 5 is open.
export const ROTATION = { 1: 'xp', 2: 'spec_xp', 3: 'crafting', 4: 'harvest', 5: null };

// effect_state as the runner writes it (ops/weekend-events/README.md, States).
// Only `running` is live. A missed or failed effect never reads as running.
export const STATE_LABELS = {
  armed: 'Set to start',
  running: 'Live now',
  done: 'Finished',
  failed: 'Not confirmed live',
  missed: 'Did not start',
};

/** Wall-clock parts of an instant in a time zone. */
function partsIn(ms, timeZone) {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric', weekday: 'short',
  });
  const out = {};
  for (const p of fmt.formatToParts(ms)) out[p.type] = p.value;
  return {
    y: Number(out.year), m: Number(out.month), d: Number(out.day),
    h: Number(out.hour), min: Number(out.minute), s: Number(out.second),
    weekday: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(out.weekday),
  };
}

/** The UTC instant of 00:00 Eastern on a calendar date. DST in the US changes
 *  at 02:00, so the offset read at noon is the offset at midnight. */
export function etMidnight(y, m, d) {
  const noon = Date.UTC(y, m - 1, d, 12);
  const p = partsIn(noon, ET);
  const offset = Date.UTC(p.y, p.m - 1, p.d, p.h, p.min, p.s) - noon;
  return Date.UTC(y, m - 1, d) - offset;
}

function addDays(date, n) {
  const t = new Date(Date.UTC(date.y, date.m - 1, date.d + n));
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

/** One weekend window from its Friday's calendar date (Eastern). */
export function weekendOf(friday) {
  const monday = addDays(friday, 3);
  const ordinal = Math.ceil(friday.d / 7);
  return {
    friday,
    ordinal,
    effect: ROTATION[ordinal] ?? null,
    startMs: etMidnight(friday.y, friday.m, friday.d),
    endMs: etMidnight(monday.y, monday.m, monday.d),
  };
}

/** This weekend (the one running, or the next one from Monday to Thursday) and
 *  the one after it. */
export function weekendSlots(nowMs) {
  const p = partsIn(nowMs, ET);
  const today = { y: p.y, m: p.m, d: p.d };
  // Fri 5 -> 0, Sat 6 -> -1, Sun 0 -> -2; Mon..Thu forward to Friday.
  const shift = p.weekday === 0 ? -2 : p.weekday === 6 ? -1 : 5 - p.weekday;
  const thisFriday = addDays(today, shift);
  return [weekendOf(thisFriday), weekendOf(addDays(thisFriday, 7))];
}

/** Every weekend whose Friday falls in the Eastern month of `nowMs`. */
export function monthWeekends(nowMs) {
  const p = partsIn(nowMs, ET);
  let first = { y: p.y, m: p.m, d: 1 };
  const firstWeekday = new Date(Date.UTC(p.y, p.m - 1, 1)).getUTCDay();
  first = addDays(first, (5 - firstWeekday + 7) % 7);
  const out = [];
  for (let f = first; f.m === p.m; f = addDays(f, 7)) out.push(weekendOf(f));
  return out;
}

export function parseUtc(s) {
  if (typeof s !== 'string' || !s.trim()) return null;
  let v = s.trim().replace(' ', 'T');
  if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : t;
}

// Title fallback, for an event that carries no effect column (the backend before
// Stage 3) or says `none` because staff run that weekend by hand. Each rule needs
// a "double", "2x" or "weekend" word, so a Landsraad term notice or a Great
// Harvest gathering never reads as a multiplier. Landsraad is tested before plain
// XP so "Double Landsraad" never reads as 2x character XP.
const TITLE_RULES = [
  [/(?:double|2x)\s+landsraad|landsraad\s+weekend|spec(?:ialization)?\s+xp/i, 'spec_xp'],
  [/craft(?:ing)?\s+weekend/i, 'crafting'],
  [/(?:double|2x)\s+harvest|harvest\s+weekend/i, 'harvest'],
  [/(?:double|2x)\s*xp|\bxp\s+weekend/i, 'xp'],
];

/** What this event does to the game: { effect, scope, state, source } or null.
 *  `state` is the runner's word and only comes from the column; a title-derived
 *  effect has no state, because nothing measured it. */
export function effectOf(event) {
  if (!event || typeof event !== 'object') return null;
  const col = event.effect;
  if (typeof col === 'string' && EFFECTS[col]) {
    return {
      effect: col,
      scope: event.effect_scope === 'fleet' || event.effect_scope === 'amtal' ? event.effect_scope : null,
      state: STATE_LABELS[event.effect_state] ? event.effect_state : null,
      source: 'column',
    };
  }
  if (col != null && col !== 'none') return null;
  const title = String(event.title || '');
  for (const [re, effect] of TITLE_RULES) {
    if (re.test(title)) return { effect, scope: null, state: null, source: 'title' };
  }
  return null;
}

/** Where the effect reaches, in words, or ''. Only a narrowed scope is worded:
 *  copy rule Q7 (player portal plan) bans an all-maps claim, so a fleet event
 *  says nothing about where and its meaning line carries what changes. */
export function scopeText(scope) {
  if (scope === 'amtal') return 'On Amtal only.';
  return '';
}

/** The published event that IS this weekend: overlapping its window and carrying
 *  an effect (column first, then title). Cancelled rows never count. */
export function eventFor(slot, events) {
  const rows = (Array.isArray(events) ? events : []).filter((ev) => {
    if (!ev || ev.status === 'cancelled') return false;
    const s = parseUtc(ev.starts_utc);
    if (s == null) return false;
    const e = parseUtc(ev.ends_utc) ?? s;
    return s < slot.endMs && e > slot.startMs && effectOf(ev) != null;
  });
  rows.sort((a, b) => (effectOf(a).source === 'column' ? 0 : 1) - (effectOf(b).source === 'column' ? 0 : 1));
  return rows[0] || null;
}

/** Everything the card renders for one slot. A published event wins over the
 *  rotation; with none, the rotation's plan shows as planned, never as confirmed. */
export function describeSlot(slot, events, nowMs) {
  const event = eventFor(slot, events);
  const eff = event ? effectOf(event) : null;
  const effect = eff ? eff.effect : slot.effect;
  const startMs = event ? parseUtc(event.starts_utc) : slot.startMs;
  const endMs = event ? (parseUtc(event.ends_utc) ?? slot.endMs) : slot.endMs;
  return {
    event,
    effect,
    info: effect ? EFFECTS[effect] : null,
    planned: !event,
    scope: eff ? eff.scope : null,
    state: eff ? eff.state : null,
    startMs,
    endMs,
    open: nowMs >= startMs && nowMs < endMs,
    restart: effect ? EFFECTS[effect].restart : false,
    title: event ? String(event.title || '') : (effect ? EFFECTS[effect].name : 'Open weekend'),
  };
}

/** The effect's state in words, or '' when nothing measured it. An armed effect
 *  inside its window has not been read back yet, so it says so. */
export function stateText(state, open) {
  if (state === 'armed' && open) return 'Not live yet';
  return STATE_LABELS[state] || '';
}

/** "Fri, Nov 6" in Eastern, for the month strip. */
export function dayText(ms) {
  if (!Number.isFinite(ms)) return '';
  return new Intl.DateTimeFormat('en-US', { timeZone: ET, weekday: 'short', month: 'short', day: 'numeric' }).format(ms);
}

/** "Fri, Nov 6, 12:00 AM EST" in a zone. */
export function timeText(ms, timeZone) {
  if (!Number.isFinite(ms)) return '';
  return new Intl.DateTimeFormat('en-US', {
    timeZone, weekday: 'short', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
  }).format(ms);
}

/** The ET line, plus the viewer's reading when their zone shows a different
 *  wall clock. */
export function timePair(ms, localZone) {
  const et = timeText(ms, ET);
  if (!localZone || !Number.isFinite(ms)) return { et, local: '' };
  const a = partsIn(ms, ET);
  const b = partsIn(ms, localZone);
  const same = a.y === b.y && a.m === b.m && a.d === b.d && a.h === b.h && a.min === b.min;
  return { et, local: same ? '' : timeText(ms, localZone) };
}
