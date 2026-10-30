// Stone layout (pure, no three): the display radius of every disc and the overlap solver.
// Positions are the game's and never move; only radii shrink.

const CM = 1e-4;                       // overmap cm -> scene units (1 unit = 100 m)
export const DISC = { scale: 2.0, min: 0.34, max: 0.95, floor: 0.09, gap: 0.04 };

// The game's travel volumes range from 12 m to 135 m across, far too uneven for one table:
// scale them, then clamp so every miniature reads.
export function baseRadius(s) {
  const cm = (s.disc && s.disc.radiusCm) || 1250;
  return Math.max(DISC.min, Math.min(DISC.max, (cm / 10000) * DISC.scale));
}

export function discCentre(s) {
  return { x: (s.disc ? s.disc.centerCm[0] : s.x) * CM, z: (s.disc ? s.disc.centerCm[1] : s.y) * CM };
}

// -> { [id]: radius }. Any two discs that still intersect shrink together in proportion to
// their size until the gap is clear or both hit the floor. Deterministic, order independent.
export function solveRadii(stones, opts) {
  const o = { ...DISC, ...(opts || {}) };
  const items = stones.map((s) => ({ id: s.id, ...discCentre(s), r: baseRadius(s) }));
  for (let pass = 0; pass < 80; pass++) {
    let moved = false;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i], b = items[j];
        const d = Math.hypot(a.x - b.x, a.z - b.z);
        const need = a.r + b.r + o.gap;
        if (d >= need) continue;
        const room = Math.max(0, d - o.gap);
        const k = room / (a.r + b.r);
        const ra = Math.max(o.floor, a.r * k), rb = Math.max(o.floor, b.r * k);
        if (ra < a.r - 1e-9 || rb < b.r - 1e-9) { a.r = Math.min(a.r, ra); b.r = Math.min(b.r, rb); moved = true; }
      }
    }
    if (!moved) break;
  }
  return Object.fromEntries(items.map((it) => [it.id, it.r]));
}

// Pairs that still overlap after solving (used by the smoke and as a runtime warning)
export function overlaps(stones, radii, gap) {
  const out = [];
  for (let i = 0; i < stones.length; i++) for (let j = i + 1; j < stones.length; j++) {
    const a = stones[i], b = stones[j], ca = discCentre(a), cb = discCentre(b);
    if (Math.hypot(ca.x - cb.x, ca.z - cb.z) < radii[a.id] + radii[b.id] + (gap == null ? 0 : gap) - 1e-9) out.push([a.id, b.id]);
  }
  return out;
}
