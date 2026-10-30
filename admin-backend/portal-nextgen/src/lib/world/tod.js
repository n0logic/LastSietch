// Sand Table time of day: four lighting presets from the VIEWER's local clock, blended
// across each boundary. Pure (no three, no DOM) so the smoke can pin the mapping.

// Boundaries in local hours; each preset owns [from, to). Night wraps midnight.
export const SPANS = { dawn: [5, 8], day: [8, 17], dusk: [17, 20], night: [20, 5] };
export const ORDER = ['dawn', 'day', 'dusk', 'night'];
const HALF_BLEND = 0.25;          // +/- 15 min around a boundary = about 30 min of blend

const wrap = (h) => ((h % 24) + 24) % 24;
const smooth = (t) => t * t * (3 - 2 * t);

// Hours past a boundary, folded to -12..12
function around(h, b) { let d = wrap(h - b); if (d > 12) d -= 24; return d; }

// -> { dawn, day, dusk, night } weights summing to 1
export function todWeights(hour) {
  const h = wrap(hour);
  const w = { dawn: 0, day: 0, dusk: 0, night: 0 };
  const seq = ORDER;
  // the preset a given hour sits in
  let cur = 'night';
  for (const p of seq) { const [a, b] = SPANS[p]; if (a < b ? h >= a && h < b : h >= a || h < b) cur = p; }
  const i = seq.indexOf(cur);
  const [a, b] = SPANS[cur];
  const next = seq[(i + 1) % 4], prev = seq[(i + 3) % 4];
  const dEnd = around(h, b), dStart = around(h, a);     // negative before the boundary
  if (dEnd > -HALF_BLEND) { const t = smooth((dEnd + HALF_BLEND) / (2 * HALF_BLEND)); w[cur] = 1 - t; w[next] = t; }
  else if (dStart < HALF_BLEND) { const t = smooth((dStart + HALF_BLEND) / (2 * HALF_BLEND)); w[cur] = t; w[prev] = 1 - t; }
  else w[cur] = 1;
  return w;
}

export function todPreset(hour) {
  const w = todWeights(hour);
  return ORDER.reduce((best, p) => (w[p] > w[best] ? p : best), 'night');
}

// A fixed hour for each manual choice (Auto uses the real clock)
export const FIXED_HOUR = { dawn: 6.5, day: 12.5, dusk: 18.5, night: 23 };
export const MODES = ['auto', 'dawn', 'day', 'dusk', 'night'];
export function resolveHour(mode, override, now) {
  if (Number.isFinite(override)) return override;
  if (FIXED_HOUR[mode] != null) return FIXED_HOUR[mode];
  const d = now || new Date();
  return d.getHours() + d.getMinutes() / 60;
}

// Lighting per preset. az/el degrees of the key light (az 90 = from the east), colours
// linear-ish rgb 0..1. exposure multiplies the renderer exposure; fire scales braziers,
// stone glow and FX glows (fx-dev reads it through setTimeOfDay).
export const LOOK = {
  dawn:  { az: 95,  el: 9,  intensity: 3.1, color: [1.0, 0.62, 0.52], hemi: 0.2,  hemiSky: [0.78, 0.6, 0.62], amb: 0.12, ambColor: [0.66, 0.5, 0.52], exposure: 0.95, bg: [0.07, 0.04, 0.045], slit: [1.0, 0.55, 0.5], slitOp: 0.9,  shaft: 0.07,  fire: 1.0 },
  day:   { az: 200, el: 28, intensity: 3.6, color: [1.0, 0.9, 0.74],  hemi: 0.24, hemiSky: [0.8, 0.72, 0.6],   amb: 0.14, ambColor: [0.66, 0.56, 0.45], exposure: 1.0, bg: [0.045, 0.035, 0.03], slit: [1.0, 0.8, 0.52], slitOp: 0.95, shaft: 0.08,  fire: 0.8 },
  dusk:  { az: 265, el: 8,  intensity: 3.3, color: [1.0, 0.45, 0.36], hemi: 0.2,  hemiSky: [0.82, 0.5, 0.52],  amb: 0.11, ambColor: [0.62, 0.38, 0.42], exposure: 0.95, bg: [0.07, 0.03, 0.04],  slit: [1.0, 0.4, 0.45],  slitOp: 0.95, shaft: 0.085, fire: 1.15 },
  night: { az: 300, el: 30, intensity: 0.95, color: [0.45, 0.56, 0.95], hemi: 0.22, hemiSky: [0.4, 0.5, 0.82], amb: 0.2,  ambColor: [0.34, 0.42, 0.7],  exposure: 0.9, bg: [0.015, 0.02, 0.045], slit: [0.45, 0.6, 1.0], slitOp: 0.5, shaft: 0.03, fire: 1.9 },
};

const NUM = ['az', 'el', 'intensity', 'hemi', 'amb', 'exposure', 'slitOp', 'shaft', 'fire'];
const VEC = ['color', 'hemiSky', 'ambColor', 'bg', 'slit'];
export function blendLook(weights) {
  const out = { color: [0, 0, 0], hemiSky: [0, 0, 0], ambColor: [0, 0, 0], bg: [0, 0, 0], slit: [0, 0, 0] };
  NUM.forEach((k) => { out[k] = 0; });
  // blend the sun position on a unit circle so 95 -> 265 degrees never swings through 180 wrongly
  let sx = 0, sz = 0;
  for (const p of ORDER) {
    const w = weights[p] || 0, l = LOOK[p];
    if (!w) continue;
    NUM.forEach((k) => { if (k !== 'az') out[k] += l[k] * w; });
    VEC.forEach((k) => { for (let i = 0; i < 3; i++) out[k][i] += l[k][i] * w; });
    sx += Math.sin(l.az * Math.PI / 180) * w; sz += Math.cos(l.az * Math.PI / 180) * w;
  }
  out.az = (Math.atan2(sx, sz) * 180 / Math.PI + 360) % 360;
  out.night = weights.night || 0;
  return out;
}
