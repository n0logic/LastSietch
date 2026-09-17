// Waypoint pins (V1 drawWaypoints port). The engine only renders and
// hit-tests; chrome owns the CRUD fetches and feeds the current list through
// applyFix('waypoints', list). wp: { id, nx, ny, note }.

const SVGNS = 'http://www.w3.org/2000/svg';

// p: { list, viewUnits }. Returns the hover/select registry; each region
// carries the waypoint fields so the engine can emit a select detail.
export function drawWaypoints(svg, p) {
  const old = svg.querySelector('.map-wp-layer');
  if (old) svg.removeChild(old);
  const hover = [];
  const list = p.list || [];
  if (!list.length) return hover;
  const layer = document.createElementNS(SVGNS, 'g');
  layer.setAttribute('class', 'map-wp-layer');
  const unit = p.viewUnits / 1000;
  list.forEach(function (wp) {
    const x = wp.nx, y = wp.ny;
    if (x == null || y == null) return;
    const r = 9 * unit;
    const pin = document.createElementNS(SVGNS, 'path');
    pin.setAttribute('d', 'M' + x + ' ' + (y - r * 1.6) +
      'C' + (x - r) + ' ' + (y - r * 0.4) + ' ' + (x - r) + ' ' + (y + r * 0.1) + ' ' + x + ' ' + (y + r) +
      'C' + (x + r) + ' ' + (y + r * 0.1) + ' ' + (x + r) + ' ' + (y - r * 0.4) + ' ' + x + ' ' + (y - r * 1.6) + 'Z');
    pin.setAttribute('class', 'map-wp-pin');
    layer.appendChild(pin);
    const dot = document.createElementNS(SVGNS, 'circle');
    dot.setAttribute('cx', x); dot.setAttribute('cy', y - r * 0.6);
    dot.setAttribute('r', r * 0.32);
    dot.setAttribute('class', 'map-wp-pin__dot');
    layer.appendChild(dot);
    hover.push({
      x: x, y: y - r * 0.6, r: r * 1.4,
      label: (wp.note || 'Waypoint') + ' (' + Math.round(x) + ', ' + Math.round(y) + ')',
      id: wp.id, nx: x, ny: y, note: wp.note || '',
    });
  });
  svg.appendChild(layer);
  return hover;
}
