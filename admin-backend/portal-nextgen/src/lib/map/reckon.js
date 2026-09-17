// Shared dead-reckoning: ONE rAF loop drives every moving map entity, id-keyed.
// Policies:
//   lerp         glide from the previous fix to the new one over intervalMs,
//                optionally coasting to maxFactor * intervalMs, then hold.
//                (worms: maxFactor 1 = V1 glide; players in M2 pass 1.5,
//                never extrapolate past that: positions are save-tick bound)
//   extrapolate  advance from the last fix at the velocity implied by the last
//                two distinct fixes (fix-pair displacement is the direction
//                source per team-lead ruling: measured beats the provisional
//                heading sign), snap-correct on each real fix, FREEZE once the
//                fix is older than staleMs. (storm center)
// apply(x, y, estimated) receives every rendered position; estimated=true for
// anything that is not a real fix, so chrome can render it amber, not Ibad.
// reducedMotion: no rAF loop, positions jump on fix arrival (V1 behavior).

export function nowMs() {
  return (typeof performance !== 'undefined' && performance.now)
    ? performance.now() : Date.now();
}

export function prefersReducedMotion() {
  return !!(typeof window !== 'undefined' && window.matchMedia &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

// Displacement below this (normalized units; ~0.5% of a DD sector) is treated
// as RAM center jitter between coarse reads, not motion. Tune against live sweeps.
const DISPLACEMENT_EPSILON = 0.5;

export function createReckoner(opts) {
  const reduce = (opts && opts.reducedMotion != null)
    ? !!opts.reducedMotion : prefersReducedMotion();
  const entities = {};
  let raf = null;

  function schedule() {
    if (!reduce && raf == null) raf = requestAnimationFrame(tick);
  }

  function tick() {
    raf = null;
    const now = nowMs();
    const wall = Date.now();
    let moving = false;
    Object.keys(entities).forEach(function (id) {
      const e = entities[id];
      if (e.done) return;
      if (e.mode === 'lerp') {
        let f = (now - e.t0) / e.intervalMs;
        if (f < 0) f = 0;
        if (f >= e.maxFactor) { f = e.maxFactor; e.done = true; }
        else moving = true;
        e.apply(e.fromX + (e.toX - e.fromX) * f,
                e.fromY + (e.toY - e.fromY) * f,
                f !== 1);
      } else {
        const hasVel = !!(e.vx || e.vy);
        let dt = wall - e.atMs;
        if (dt < 0) dt = 0;
        if (!hasVel) {
          e.done = true;
          e.apply(e.x, e.y, false);
        } else if (dt >= e.staleMs) {
          // Fix went stale: freeze at the extrapolation limit.
          e.done = true;
          e.apply(e.x + e.vx * e.staleMs, e.y + e.vy * e.staleMs, true);
        } else {
          moving = true;
          e.apply(e.x + e.vx * dt, e.y + e.vy * dt, dt > 0);
        }
      }
    });
    if (moving) raf = requestAnimationFrame(tick);
  }

  // cfg: { intervalMs, maxFactor?, apply, snap? }. snap forces a jump (used on
  // first sight and worm glyph rebuilds, matching V1's no-glide-on-first-sight).
  function lerpFix(id, x, y, cfg) {
    let e = entities[id];
    if (!e || e.mode !== 'lerp' || cfg.snap) {
      e = entities[id] = {
        mode: 'lerp', intervalMs: cfg.intervalMs,
        maxFactor: cfg.maxFactor || 1, apply: cfg.apply,
        fromX: x, fromY: y, toX: x, toY: y, t0: nowMs(), done: true,
      };
      e.apply(x, y, false);
      return;
    }
    e.apply = cfg.apply;
    e.intervalMs = cfg.intervalMs;
    e.maxFactor = cfg.maxFactor || 1;
    // V1: glide from the previous TARGET (not the rendered position).
    e.fromX = e.toX; e.fromY = e.toY;
    e.toX = x; e.toY = y;
    e.t0 = nowMs();
    if (reduce) { e.done = true; e.apply(x, y, false); return; }
    e.done = false;
    schedule();
  }

  // cfg: { atMs, staleMs, apply }. atMs is the fix's own timestamp (source scan
  // time) so a payload re-delivered by a faster poll is deduped, not treated as
  // a new fix.
  function extrapolateFix(id, x, y, headingDeg, cfg) {
    const e = entities[id];
    if (e && e.mode === 'extrapolate' && e.atMs === cfg.atMs) {
      // Same fix re-delivered: retarget apply (the SVG node may have been
      // rebuilt) and keep extrapolating from the existing anchor.
      e.apply = cfg.apply;
      if (reduce) { e.apply(e.x, e.y, false); return; }
      e.done = false;
      schedule();
      return;
    }
    let vx = 0, vy = 0;
    if (e && e.mode === 'extrapolate' && cfg.atMs > e.atMs) {
      // Ruling (team-lead + architect): direction from the fix-pair
      // displacement. The heading sign was VERIFIED 2026-07-04 (drift 34.47
      // deg vs yaw 34.5), but measured displacement still beats a single
      // scalar heading, so it stays primary. Speed magnitude always comes
      // from the fix pair.
      const dtf = cfg.atMs - e.atMs;
      const dx = x - e.x, dy = y - e.y;
      const dist = Math.hypot(dx, dy);
      if (dist >= DISPLACEMENT_EPSILON) {
        vx = dx / dtf;
        vy = dy / dtf;
      } else if (headingDeg != null && isFinite(headingDeg)) {
        // Sub-epsilon displacement = jitter, not motion: take the direction
        // from heading so any residual drift at least points plausibly.
        const rad = headingDeg * Math.PI / 180;
        vx = Math.cos(rad) * (dist / dtf);
        vy = Math.sin(rad) * (dist / dtf);
      }
      // else: hold position (vx=vy=0) until a real vector exists.
    }
    const next = entities[id] = {
      mode: 'extrapolate', x: x, y: y, atMs: cfg.atMs, staleMs: cfg.staleMs,
      vx: vx, vy: vy, apply: cfg.apply, done: false,
    };
    next.apply(x, y, false);   // snap-correct on every real fix
    if (reduce) { next.done = true; return; }
    schedule();
  }

  function remove(id) {
    delete entities[id];
  }

  function clear() {
    Object.keys(entities).forEach(remove);
  }

  function destroy() {
    clear();
    if (raf != null) { cancelAnimationFrame(raf); raf = null; }
  }

  return { lerpFix, extrapolateFix, remove, clear, destroy };
}
