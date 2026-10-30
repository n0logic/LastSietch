// World transform + gesture controller for the map engine. One transform
// (baseScale * zoom + pan) drives the CSS stage; canvas, SVG overlay and
// backdrop all live inside the stage so they stay in lockstep (V1 invariant).

// azimuth/tilt (degrees) drive only the holo camera: azimuth is the heading the
// camera faces, clockwise from north (0 = north up); tilt is its elevation above
// the table. The carved stage transform ignores both, and rotation gestures are
// only attached when a 3D viewer is active, so carved stays at azimuth 0.
export const TILT_MIN = 20;
export const TILT_MAX = 80;
export const ORBIT_DEFAULT = { azimuth: 0, tilt: 35 };

export function createView(viewUnits) {
  return { view: viewUnits, display: 0, zoom: 1, panX: 0, panY: 0,
           azimuth: ORBIT_DEFAULT.azimuth, tilt: ORBIT_DEFAULT.tilt };
}

function wrapDeg(a) {
  a = a % 360;
  return a < 0 ? a + 360 : a;
}

// Full 360 azimuth (wrapped to 0..360), tilt clamped to TILT_MIN..TILT_MAX.
export function setOrbit(v, azimuth, tilt) {
  v.azimuth = Number.isFinite(azimuth) ? wrapDeg(azimuth) : ORBIT_DEFAULT.azimuth;
  v.tilt = Number.isFinite(tilt) ? Math.max(TILT_MIN, Math.min(TILT_MAX, tilt)) : ORBIT_DEFAULT.tilt;
}

// Next 45 deg heading in direction dir (+1 clockwise, -1 counter): an
// off-grid heading snaps to the next multiple instead of staying off-grid.
export function stepHeading(azimuth, dir) {
  const k = azimuth / 45;
  const snapped = dir > 0 ? Math.floor(k + 1e-6) + 1 : Math.ceil(k - 1e-6) - 1;
  return wrapDeg(snapped * 45);
}

// Interpolate orbit a -> b at t (0..1), azimuth along the shorter way round.
export function orbitLerp(a, b, t) {
  let d = wrapDeg(b.azimuth - a.azimuth);
  if (d > 180) d -= 360;
  return { azimuth: wrapDeg(a.azimuth + d * t), tilt: a.tilt + (b.tilt - a.tilt) * t };
}

// A screen-space drag (dx, dy px) as a board-space pan delta under the current
// heading, so the map follows the finger at any rotation. Identity at 0.
export function rotateScreenDelta(v, dx, dy) {
  const r = (v.azimuth || 0) * Math.PI / 180;
  const c = Math.cos(r), s = Math.sin(r);
  return { dx: dx * c - dy * s, dy: dx * s + dy * c };
}

export function baseScale(v) {
  return v.display / v.view;
}

export function clampPan(v) {
  const max = 0, min = v.display * (1 - v.zoom);
  v.panX = Math.max(min, Math.min(max, v.panX));
  v.panY = Math.max(min, Math.min(max, v.panY));
}

export function setZoom(v, z, ox, oy) {
  z = Math.max(1, Math.min(8, z));
  if (ox != null) {
    const k = z / v.zoom;
    v.panX = ox - (ox - v.panX) * k;
    v.panY = oy - (oy - v.panY) * k;
  }
  v.zoom = z;
  clampPan(v);
}

export function panToNormalized(v, nx, ny) {
  const bs = baseScale(v);
  v.panX = v.display / 2 - nx * bs * v.zoom;
  v.panY = v.display / 2 - ny * bs * v.zoom;
  clampPan(v);
}

export function screenToNormalized(v, mx, my) {
  const bs = baseScale(v);
  return {
    nx: (mx - v.panX) / v.zoom / bs,
    ny: (my - v.panY) / v.zoom / bs,
  };
}

export function applyStageTransform(v, stage) {
  stage.style.transform =
    'translate(' + v.panX + 'px,' + v.panY + 'px) scale(' + v.zoom + ')';
}

// Normalized -> world coords via the map calibration (inverse of the backend
// normalize). Returns null when the map ships no calibration.
export function normalizedToWorld(nx, ny, cal, viewUnits) {
  if (!cal) return null;
  return {
    x: Math.round(nx / viewUnits * cal.spanX + cal.originX),
    y: Math.round(ny / viewUnits * cal.spanY + cal.originY),
  };
}

// Inverse of normalizedToWorld: world (x, y) -> normalized 0..viewUnits, using
// the map's calibration (same formula as the backend map_model.normalize, incl.
// flipY). Used to plot the live public player/vehicle positions. null on bad
// input so a malformed coord is skipped rather than drawn at the origin.
export function worldToNormalized(x, y, cal, viewUnits) {
  if (!cal || x == null || y == null) return null;
  let nx = (x - cal.originX) / cal.spanX * viewUnits;
  let ny = (y - cal.originY) / cal.spanY * viewUnits;
  if (cal.flipY) ny = viewUnits - ny;
  return { nx: nx, ny: ny };
}

// Pointer/wheel gestures: V1 drag + pinch + wheel-zoom, ported verbatim.
// handlers: onTransform() after any zoom/pan/orbit change, onInteract(bool) as a
// drag/pinch starts/ends, onHover(mx, my) on non-drag moves, onLeave(),
// onClick(mx, my), onStep(dir) for a keyboard 45 deg rotate step. All coords
// viewport-relative. Returns detach().
// opts.rotate (holo only) adds orbit gestures on the same view state:
//   mouse right-drag or Ctrl+drag: horizontal = azimuth, vertical = tilt;
//   two fingers: a twist rotates (pinch zoom + pan keep working alongside), a
//     parallel vertical drag with steady spacing tilts (exclusive, no pan);
//   Q / E: onStep(-1 / +1).
// One-finger drags pan in screen space under the current heading. Without
// opts.rotate the behavior is the verbatim V1 path.
const ORBIT_DEG_PER_PX = 0.4;
const TILT_DEG_PER_PX = 0.3;
const TWIST_START_DEG = 10;

function lineAngle(pts) {
  return Math.atan2(pts[1].y - pts[0].y, pts[1].x - pts[0].x) * 180 / Math.PI;
}

function angleDiff(a, b) {
  let d = (a - b) % 360;
  if (d > 180) d -= 360;
  if (d < -180) d += 360;
  return d;
}

export function attachGestures(viewport, v, handlers, opts) {
  const rotate = !!(opts && opts.rotate);
  const pointers = new Map();
  let drag = null, pinch = null, orbit = null;

  function onWheel(e) {
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    setZoom(v, v.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15),
            e.clientX - rect.left, e.clientY - rect.top);
    handlers.onTransform();
  }

  function onPointerDown(e) {
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    viewport.setPointerCapture(e.pointerId);
    viewport.classList.add('is-dragging');
    handlers.onInteract(true);
    if (pointers.size === 2) {
      const pts = Array.from(pointers.values());
      const rect = viewport.getBoundingClientRect();
      drag = null;
      orbit = null;
      pinch = {
        dist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1,
        zoom: v.zoom,
        midX: (pts[0].x + pts[1].x) / 2 - rect.left,
        midY: (pts[0].y + pts[1].y) / 2 - rect.top,
        panX: v.panX, panY: v.panY,
        angle: lineAngle(pts), azimuth: v.azimuth, tilt: v.tilt,
        mode: null, twistAt: null,
      };
    } else if (pointers.size === 1) {
      if (rotate && e.pointerType === 'mouse' && (e.button === 2 || e.ctrlKey)) {
        drag = null;
        orbit = { x: e.clientX, y: e.clientY, azimuth: v.azimuth, tilt: v.tilt };
      } else {
        drag = { x: e.clientX, y: e.clientY, px: v.panX, py: v.panY };
      }
    }
  }

  // Rotate-mode two-finger move. Decides once per gesture between an exclusive
  // tilt (parallel vertical drag, spacing and angle steady) and pinch (zoom +
  // pan, with twist rotation joining once the fingers turn past the threshold).
  function rotatePinchMove(pts, rect) {
    const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
    const midX = (pts[0].x + pts[1].x) / 2 - rect.left;
    const midY = (pts[0].y + pts[1].y) / 2 - rect.top;
    const ratio = dist / pinch.dist;
    const dMidX = midX - pinch.midX, dMidY = midY - pinch.midY;
    const dAng = angleDiff(lineAngle(pts), pinch.angle);
    if (!pinch.mode) {
      if (Math.abs(dMidY) > 18 && Math.abs(dMidY) > 2 * Math.abs(dMidX) &&
          Math.abs(ratio - 1) < 0.08 && Math.abs(dAng) < 8) {
        pinch.mode = 'tilt';
      } else if (Math.abs(dAng) > TWIST_START_DEG || Math.abs(ratio - 1) > 0.08 ||
                 Math.hypot(dMidX, dMidY) > 18) {
        pinch.mode = 'pinch';
      } else {
        return;
      }
    }
    if (pinch.mode === 'tilt') {
      setOrbit(v, v.azimuth, pinch.tilt + dMidY * TILT_DEG_PER_PX);
      handlers.onTransform();
      return;
    }
    if (pinch.twistAt == null && Math.abs(dAng) > TWIST_START_DEG) pinch.twistAt = dAng;
    if (pinch.twistAt != null) {
      // Fingers turning clockwise turn the map clockwise: heading goes down.
      setOrbit(v, pinch.azimuth - (dAng - pinch.twistAt), v.tilt);
    }
    const z = Math.max(1, Math.min(8, pinch.zoom * ratio));
    const wx = (pinch.midX - pinch.panX) / pinch.zoom;
    const wy = (pinch.midY - pinch.panY) / pinch.zoom;
    const moved = rotateScreenDelta(v, dMidX, dMidY);
    v.zoom = z;
    v.panX = pinch.midX - wx * z + moved.dx;
    v.panY = pinch.midY - wy * z + moved.dy;
    clampPan(v);
    handlers.onTransform();
  }

  function onPointerMove(e) {
    if (pointers.has(e.pointerId)) {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    }
    if (pinch && pointers.size >= 2 && rotate) {
      rotatePinchMove(Array.from(pointers.values()), viewport.getBoundingClientRect());
    } else if (orbit) {
      setOrbit(v, orbit.azimuth - (e.clientX - orbit.x) * ORBIT_DEG_PER_PX,
               orbit.tilt + (e.clientY - orbit.y) * TILT_DEG_PER_PX);
      handlers.onTransform();
    } else if (pinch && pointers.size >= 2) {
      const pts = Array.from(pointers.values());
      const rect = viewport.getBoundingClientRect();
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      const midX = (pts[0].x + pts[1].x) / 2 - rect.left;
      const midY = (pts[0].y + pts[1].y) / 2 - rect.top;
      const z = Math.max(1, Math.min(8, pinch.zoom * dist / pinch.dist));
      const wx = (pinch.midX - pinch.panX) / pinch.zoom;
      const wy = (pinch.midY - pinch.panY) / pinch.zoom;
      v.zoom = z;
      v.panX = midX - wx * z;
      v.panY = midY - wy * z;
      clampPan(v);
      handlers.onTransform();
    } else if (drag) {
      const d = rotateScreenDelta(v, e.clientX - drag.x, e.clientY - drag.y);
      v.panX = drag.px + d.dx;
      v.panY = drag.py + d.dy;
      clampPan(v);
      handlers.onTransform();
    } else {
      const rect = viewport.getBoundingClientRect();
      handlers.onHover(e.clientX - rect.left, e.clientY - rect.top);
    }
  }

  function endPointer(e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 1) {
      const p = pointers.values().next().value;
      drag = { x: p.x, y: p.y, px: v.panX, py: v.panY };
    } else if (pointers.size === 0) {
      drag = null;
      orbit = null;
      viewport.classList.remove('is-dragging');
      handlers.onInteract(false);
    }
  }

  function onLeave() {
    handlers.onLeave();
  }

  function onClick(e) {
    const rect = viewport.getBoundingClientRect();
    handlers.onClick(e.clientX - rect.left, e.clientY - rect.top);
  }

  // Right-drag orbits, so the board's context menu would only get in the way.
  function onContextMenu(e) {
    e.preventDefault();
  }

  function onKeyDown(e) {
    if (e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
    const k = e.key;
    if (k === 'q' || k === 'Q') handlers.onStep(-1);
    else if (k === 'e' || k === 'E') handlers.onStep(1);
  }

  viewport.addEventListener('wheel', onWheel, { passive: false });
  viewport.addEventListener('pointerdown', onPointerDown);
  viewport.addEventListener('pointermove', onPointerMove);
  viewport.addEventListener('pointerup', endPointer);
  viewport.addEventListener('pointercancel', endPointer);
  viewport.addEventListener('pointerleave', onLeave);
  viewport.addEventListener('click', onClick);
  if (rotate) {
    viewport.addEventListener('contextmenu', onContextMenu);
    window.addEventListener('keydown', onKeyDown);
  }

  return function detach() {
    if (rotate) {
      viewport.removeEventListener('contextmenu', onContextMenu);
      window.removeEventListener('keydown', onKeyDown);
    }
    viewport.removeEventListener('wheel', onWheel);
    viewport.removeEventListener('pointerdown', onPointerDown);
    viewport.removeEventListener('pointermove', onPointerMove);
    viewport.removeEventListener('pointerup', endPointer);
    viewport.removeEventListener('pointercancel', endPointer);
    viewport.removeEventListener('pointerleave', onLeave);
    viewport.removeEventListener('click', onClick);
  };
}
