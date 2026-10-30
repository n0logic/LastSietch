// War-Table time of day. THREE-free so the smoke can pin it. Same presets, same
// viewer-clock logic, same 30-minute blends and same ?hour override as the Sand Table
// (world/tod.js); the toggle choice lives under the Sand Table's own storage key so the
// two stay in step.
import { todWeights, todPreset, resolveHour, blendLook, MODES, ORDER } from '../../world/tod.js';

export { MODES, todPreset, resolveHour };
export const TOD_KEY = 'ls-world-tod';
export const RELIGHT_MS = 20000;   // Auto re-evaluates the viewer clock this often

export function readMode() {
  try { const m = localStorage.getItem(TOD_KEY); if (MODES.includes(m)) return m; } catch { /* no storage */ }
  return 'auto';
}
export function saveMode(m) {
  try { localStorage.setItem(TOD_KEY, m); } catch { /* private mode */ }
}

// ?hour=N pins the light (review / screenshots); NaN when absent
export function hourOverride(search) {
  try {
    const q = new URLSearchParams(search || '');
    return q.has('hour') ? Number(q.get('hour')) : NaN;
  } catch { return NaN; }
}

// Key light direction (unit, pointing AT the sun) in holo world space: +x east,
// -z north (board ny grows south). tod az 90 = from the east, 270 = from the west.
export function sunDir(az, el) {
  const a = az * Math.PI / 180, e = el * Math.PI / 180;
  return [Math.sin(a) * Math.cos(e), Math.sin(e), -Math.cos(a) * Math.cos(e)];
}

const norm = (c) => { const m = Math.max(c[0], c[1], c[2], 1e-4); return [c[0] / m, c[1] / m, c[2] / m]; };
const scale = (c, k) => [c[0] * k, c[1] * k, c[2] * k];
const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];

// Weights -> shader-ready drape lighting. Day lands on the old fixed look (ambient 0.35,
// key 0.75, both x0.8); night is a dim cool moonlight; dawn/dusk are a low warm sun.
export function holoLight(weights) {
  const L = blendLook(weights);
  const night = L.night;
  // a low sun leaves flat ground nearly unlit, so dawn and dusk carry extra fill
  const tw = (weights.dawn || 0) + (weights.dusk || 0);
  const sun = norm(L.color), amb = norm(L.ambColor), hemi = norm(L.hemiSky);
  return {
    night,
    sunDir: sunDir(L.az, L.el),
    sun: scale(sun, 0.94 * (L.intensity / 3.6) * (1 + 0.5 * tw + 0.9 * night)),
    ambient: scale(mix([1, 1, 1], amb, 0.55), 0.3 * (1 - 0.25 * night) * (1 + 1.1 * tw)),
    hemi: scale(hemi, L.hemi * 0.25),
    exposure: L.exposure,
    // hologram branch: a faint tint shift only
    tint: mix([1, 1, 1], mix(sun, [0.62, 0.78, 1.0], night), 0.14),
    look: L,
  };
}

// Mode + hour -> { preset, weights, light }
export function lightFor(mode, override, now) {
  const hour = resolveHour(mode, override, now);
  const weights = todWeights(hour);
  return { hour, preset: todPreset(hour), weights, light: holoLight(weights) };
}

// Weights eased between two sets (the toggle transition); both sum to 1 so the blend does too.
export function lerpWeights(a, b, t) {
  const out = {};
  ORDER.forEach((p) => { out[p] = (a[p] || 0) + ((b[p] || 0) - (a[p] || 0)) * t; });
  return out;
}
