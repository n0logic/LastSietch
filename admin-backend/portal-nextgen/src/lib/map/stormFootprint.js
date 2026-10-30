// Sandstorm footprint + motion (reader v2). DOM- and three-free so the holo, the 2D
// board, the hover and the smoke test share one model.
//
// The relay sends, per storm world, the zone ellipses in board space
// (zones[{name, damage, nx, ny, ax_nr, ay_nr, yaw}]: semi-axis ax along the heading,
// ay across it) plus the straight route (start_nx/ny -> end_nx/ny) and the lifetime.
// Live probe 2026-10-07: a storm holds STORM_HOLD_S at spawn, then moves along the
// route at a constant speed, lifetime_total = |route| / speed + hold. So between
// polls the footprint is placed exactly; no velocity estimate from fix pairs.
// Heading convention = the verified heading arrow: dir (cos yaw, sin yaw), +x east,
// +y south in board space.

export const STORM_HOLD_S = 10;
export const STORM_STALE_MS = 12 * 60 * 1000;   // stop gliding this long after a scan
// The glide follows the game's own route and speed (exact, probe-verified), so it is
// not an estimate. It only counts as one once polls have been missing this long.
export const STORM_EST_MS = 2 * 60 * 1000;

function num(v) { return typeof v === 'number' && isFinite(v); }

// fp or null. Null when there is nothing to draw as a footprint (no zones: the
// caller falls back to the v1 circle), or the reader says clear.
export function stormFootprint(info) {
  if (!info || info.storm_state === 'clear') return null;
  if (!num(info.center_nx) || !num(info.center_ny) || !Array.isArray(info.zones)) return null;
  const cx = info.center_nx, cy = info.center_ny;
  const seen = new Set();
  const zones = [];
  for (const z of info.zones) {
    if (!z || !num(z.nx) || !num(z.ny) || !num(z.ax_nr) || !num(z.ay_nr) || z.ax_nr <= 0 || z.ay_nr <= 0) continue;
    const key = [z.nx, z.ny, z.ax_nr, z.ay_nr].map((v) => v.toFixed(1)).join(',');
    if (seen.has(key)) continue;   // BuildUp and GroundLayer share one ellipse
    seen.add(key);
    zones.push({ name: z.name || 'zone', damage: z.damage !== false, dx: z.nx - cx, dy: z.ny - cy,
      ax: z.ax_nr, ay: z.ay_nr, yaw: num(z.yaw) ? z.yaw : (num(info.heading_yaw) ? info.heading_yaw : 0) });
  }
  if (!zones.length) return null;
  // Outer rims first, damage last: draw order and hover both read better.
  zones.sort((a, b) => (a.damage === b.damage ? b.ax * b.ay - a.ax * a.ay : a.damage ? 1 : -1));
  const yaw = num(info.heading_yaw) ? info.heading_yaw : zones[0].yaw;
  const atMs = info.storm_scanned_utc ? Date.parse(info.storm_scanned_utc) : NaN;
  let route = null, motion = null;
  if (num(info.start_nx) && num(info.start_ny) && num(info.end_nx) && num(info.end_ny)) {
    route = { sx: info.start_nx, sy: info.start_ny, ex: info.end_nx, ey: info.end_ny };
    const L = Math.hypot(route.ex - route.sx, route.ey - route.sy);
    const total = info.lifetime_total, left = info.lifetime_remaining;
    if (L > 0 && num(total) && num(left) && total > STORM_HOLD_S + 1) {
      const travel = total - STORM_HOLD_S;
      motion = { dirX: (route.ex - route.sx) / L, dirY: (route.ey - route.sy) / L,
        speed: L / travel, elapsed0: total - left, travel: travel };
    }
  }
  let endsMs = info.ends_utc ? Date.parse(info.ends_utc) : NaN;
  if (!isFinite(endsMs) && isFinite(atMs) && num(info.lifetime_remaining)) endsMs = atMs + info.lifetime_remaining * 1000;
  return { cx, cy, yaw, zones, route, motion, atMs, endsMs };
}

// Seconds of travel along the route at elapsed storm time e (hold, then clamp at the end).
function travelled(m, e) {
  return Math.min(m.travel, Math.max(0, e - STORM_HOLD_S));
}

// Footprint centre at wall time. Anchored on the scanned centre so a new poll never
// jumps; moves at the route speed; clamps at the route end; freezes STORM_STALE_MS
// after the scan. Reduced motion: the latest known position, static.
export function positionAt(fp, wallMs, reduce) {
  if (!fp) return null;
  if (reduce || !fp.motion || !isFinite(fp.atMs)) return { x: fp.cx, y: fp.cy, estimated: false };
  const dt = Math.min(STORM_STALE_MS, Math.max(0, wallMs - fp.atMs)) / 1000;
  const m = fp.motion;
  const s = travelled(m, m.elapsed0 + dt) - travelled(m, m.elapsed0);
  return { x: fp.cx + m.dirX * m.speed * s, y: fp.cy + m.dirY * m.speed * s,
    estimated: dt * 1000 > STORM_EST_MS };
}

export function isOver(fp, wallMs) {
  return !!fp && isFinite(fp.endsMs) && wallMs >= fp.endsMs;
}

export function clearsInSec(fp, wallMs) {
  if (!fp || !isFinite(fp.endsMs)) return null;
  return Math.max(0, Math.round((fp.endsMs - wallMs) / 1000));
}

export function fmtClears(sec) {
  const s = Math.max(0, Math.round(sec));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

export function stormLabel(fp, wallMs) {
  const s = clearsInSec(fp, wallMs);
  return s == null ? 'Sandstorm' : 'Sandstorm, clears in ' + fmtClears(s);
}

// Board point inside any zone of the footprint at its current position.
export function hitFootprint(fp, x, y, wallMs, reduce) {
  const p = positionAt(fp, wallMs, reduce);
  if (!p || isOver(fp, wallMs)) return false;
  for (const z of fp.zones) {
    const rad = z.yaw * Math.PI / 180, c = Math.cos(rad), s = Math.sin(rad);
    const rx = x - (p.x + z.dx), ry = y - (p.y + z.dy);
    const u = (rx * c + ry * s) / z.ax, v = (-rx * s + ry * c) / z.ay;
    if (u * u + v * v <= 1) return true;
  }
  return false;
}
