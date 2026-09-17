// World transform + gesture controller for the map engine. One transform
// (baseScale * zoom + pan) drives the CSS stage; canvas, SVG overlay and
// backdrop all live inside the stage so they stay in lockstep (V1 invariant).

export function createView(viewUnits) {
  return { view: viewUnits, display: 0, zoom: 1, panX: 0, panY: 0 };
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
// handlers: onTransform() after any zoom/pan change, onInteract(bool) as a
// drag/pinch starts/ends, onHover(mx, my) on non-drag moves, onLeave(),
// onClick(mx, my). All coords viewport-relative. Returns detach().
export function attachGestures(viewport, v, handlers) {
  const pointers = new Map();
  let drag = null, pinch = null;

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
      pinch = {
        dist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1,
        zoom: v.zoom,
        midX: (pts[0].x + pts[1].x) / 2 - rect.left,
        midY: (pts[0].y + pts[1].y) / 2 - rect.top,
        panX: v.panX, panY: v.panY,
      };
    } else if (pointers.size === 1) {
      drag = { x: e.clientX, y: e.clientY, px: v.panX, py: v.panY };
    }
  }

  function onPointerMove(e) {
    if (pointers.has(e.pointerId)) {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    }
    if (pinch && pointers.size >= 2) {
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
      v.panX = drag.px + (e.clientX - drag.x);
      v.panY = drag.py + (e.clientY - drag.y);
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

  viewport.addEventListener('wheel', onWheel, { passive: false });
  viewport.addEventListener('pointerdown', onPointerDown);
  viewport.addEventListener('pointermove', onPointerMove);
  viewport.addEventListener('pointerup', endPointer);
  viewport.addEventListener('pointercancel', endPointer);
  viewport.addEventListener('pointerleave', onLeave);
  viewport.addEventListener('click', onClick);

  return function detach() {
    viewport.removeEventListener('wheel', onWheel);
    viewport.removeEventListener('pointerdown', onPointerDown);
    viewport.removeEventListener('pointermove', onPointerMove);
    viewport.removeEventListener('pointerup', endPointer);
    viewport.removeEventListener('pointercancel', endPointer);
    viewport.removeEventListener('pointerleave', onLeave);
    viewport.removeEventListener('click', onClick);
  };
}
