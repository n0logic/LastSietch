// Seven Nights of the Sietch: tonight's public porch and stall doors on the board.
// Pins come ONLY from the session-gated /portal/sietch/state (night.spots_public
// rows carrying {map, nx, ny}); the public names-only route is never read for
// pins (owner ruling 2026-10-24). Only porch and stall are ever drawn: never
// haunt, seek or clue, even if a row of that kind arrives with a position. A row
// without nx is not drawn. Labels cross as data.
//
// No imports: the filter is pure so the CI test can run it under node.

const SVGNS = 'http://www.w3.org/2000/svg';
const LANTERN_SRC = '/img/sietch-nights/lantern-marker.svg?v=20261024a';
export const PIN_KINDS = Object.freeze({ porch: 'Porch', stall: 'Stall landmark' });

// rows: night.spots_public from /state. Returns the rows to pin on this board.
export function doorsToDraw(rows, mapKey, part) {
  if (!Array.isArray(rows)) return [];
  return rows.filter(function (s) {
    if (!s || !Object.prototype.hasOwnProperty.call(PIN_KINDS, s.kind)) return false;
    if (typeof s.nx !== 'number' || typeof s.ny !== 'number') return false;
    return s.map === mapKey;
  });
}

// p: { list, mapKey, part, viewUnits }. Returns the hover registry.
export function drawSietchSpots(svg, p) {
  const old = svg.querySelector('.map-sn-layer');
  if (old) svg.removeChild(old);
  const hover = [];
  const list = doorsToDraw(p.list, p.mapKey, p.part);
  if (!list.length) return hover;
  const layer = document.createElementNS(SVGNS, 'g');
  layer.setAttribute('class', 'map-sn-layer');
  const unit = p.viewUnits / 1000;
  const k = (10 * unit * 3.2) / 40;
  list.forEach(function (s) {
    // A dashed spice ring marks a door, so it never reads as the player's own
    // Lantern-skinned dot (which wears the live Ibad ring instead).
    const ring = document.createElementNS(SVGNS, 'circle');
    ring.setAttribute('cx', s.nx); ring.setAttribute('cy', s.ny);
    ring.setAttribute('r', 15 * unit);
    ring.setAttribute('class', 'map-sn-door__ring');
    layer.appendChild(ring);
    const img = document.createElementNS(SVGNS, 'image');
    img.setAttribute('href', LANTERN_SRC);
    img.setAttribute('x', s.nx - 16 * k); img.setAttribute('y', s.ny - 22 * k);
    img.setAttribute('width', 32 * k); img.setAttribute('height', 40 * k);
    img.setAttribute('class', 'map-sn-door');
    layer.appendChild(img);
    hover.push({ x: s.nx, y: s.ny, r: 14 * unit,
      label: (s.name || PIN_KINDS[s.kind]) + (s.done ? ' (Seven Nights, knocked)' : ' (Seven Nights, tonight)') });
  });
  svg.appendChild(layer);
  return hover;
}
