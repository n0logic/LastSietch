// M3 stage arbiter. Decides which viewer renders the board and is the ONE place
// three enters the app (lazily, via ./index.js). This module imports no three
// itself, so it stays on the eager maps-route path with zero 3D weight.
//
// StageViewer interface (both the carved wrapper here and dev-2's holo viewer
// satisfy it; the route talks to whichever is active):
//   createXViewer(hostEls, opts) -> {
//     ready: Promise<void>,        // resolves when the first frame is drawable
//     applyScene(scene): void,     // engine onScene snapshot (engine.js emitScene)
//     setView(v): void,            // engine onViewChange payload
//     setTheme(mode): void,        // 'night' | 'day'; re-reads CSS tokens
//     projector: { project(nx,ny), unproject(mx,my) } | null,
//     resize(): void,
//     destroy(): void              // dispose GPU + listeners, idempotent
//   }
// hostEls for the holo viewer: { viewport, canvas } (the route's holo <canvas>
// sibling inside .map-viewport, plus the viewport to observe for sizing).

// The carved viewer is a thin no-op wrapper: the engine already draws the carved
// overlays straight into its own canvas/SVG, so applyScene/setView do nothing
// and it exposes no projector (engine falls back to the affine 2.5D transform).
// It exists so the route holds one uniform StageViewer for either path.
export function createCarvedViewer() {
  return {
    ready: Promise.resolve(),
    applyScene() {},
    setView() {},
    setTheme() {},
    projector: null,
    resize() {},
    destroy() {},
  };
}

// Live WebGL2 probe, cached. quality.webgl is webgl2-OR-webgl, so it is not
// enough: holo needs webgl2 specifically. Do not import three to test this.
let _webgl2 = null;
export function probeWebGL2() {
  if (_webgl2 !== null) return _webgl2;
  if (typeof document === 'undefined') return false;
  try {
    const c = document.createElement('canvas');
    _webgl2 = !!c.getContext('webgl2');
  } catch {
    _webgl2 = false;
  }
  return _webgl2;
}

// Resolve which viewer to show. Auto-mount holo only on the 'high' tier (which
// already excludes reduced-motion, save-data, coarse pointer, and weak devices)
// AND a passing WebGL2 probe. A user preference overrides: forcing 'carved'
// always works; forcing 'holo' is honored best-effort on any device that passes
// the probe (mid tier included, per owner Q2). No WebGL2 -> always carved.
export function resolveViewerChoice({ quality, preference }) {
  if (!probeWebGL2()) return 'carved';
  if (preference === 'holo') return 'holo';
  if (preference === 'carved') return 'carved';
  return quality && quality.tier === 'high' ? 'holo' : 'carved';
}

// Lazy-import + construct the holo viewer and await its first frame. three is
// pulled in here and nowhere else, so Rollup isolates three + holo into one
// lazy chunk. Any import or init failure resolves to null (the caller stays on
// the carved board); this is also the production failure path.
export async function mountHoloViewer(hostEls, opts) {
  try {
    const mod = await import('./index.js');
    const viewer = mod.createHoloViewer(hostEls, opts);
    await viewer.ready;
    return viewer;
  } catch (err) {
    console.warn('[map] holo viewer unavailable; staying on the carved board', err);
    return null;
  }
}
