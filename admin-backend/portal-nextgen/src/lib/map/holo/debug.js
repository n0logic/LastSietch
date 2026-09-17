// Debug URL overrides for the holo war-table. Three-free on purpose: the maps
// route imports this eagerly (for the on-screen chip) and index.js imports it
// inside the lazy holo chunk; neither pulls three.

// ?holoLayout=N (0-11) forces the holo to render baked layout N, bypassing the
// backend matcher, so we can compare each real DD terrain to the in-game map.
// Returns null when absent or out of range (normal path). Guarded for SSR.
export function forcedLayout() {
  if (typeof window === 'undefined') return null;
  try {
    // Regex the raw URL so a malformed query still works: the maps route already
    // carries ?inst=pve, so a hand-appended ?holoLayout=2 becomes ?inst=pve?holoLayout=2
    // (double ?) which URLSearchParams would fold into the inst value. Match by
    // name regardless of the ? / & delimiter.
    var m = /[?&]holoLayout=(\d+)/.exec(window.location.href);
    if (!m) return null;
    var n = parseInt(m[1], 10);
    return (Number.isInteger(n) && n >= 0 && n <= 11) ? n : null;
  } catch (e) {
    return null;
  }
}

// ?holoRelief=X (0.5-8, float) overrides the real-terrain relief exaggeration
// so the owner can tune how dramatically the baked desert reads. Applies only
// to the REAL layout path; the seeded relief never sees it. Null = default.
export function forcedRelief() {
  if (typeof window === 'undefined') return null;
  try {
    var m = /[?&]holoRelief=(\d+(?:\.\d+)?)/.exec(window.location.href);
    if (!m) return null;
    var x = parseFloat(m[1]);
    return (isFinite(x) && x >= 0.5 && x <= 8) ? x : null;
  } catch (e) {
    return null;
  }
}
