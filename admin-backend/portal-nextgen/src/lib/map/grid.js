// SVG survey grid + sector labels (DD) and the sector -> center math.

const SVGNS = 'http://www.w3.org/2000/svg';

function line(parent, x1, y1, x2, y2) {
  const l = document.createElementNS(SVGNS, 'line');
  l.setAttribute('x1', x1); l.setAttribute('y1', y1);
  l.setAttribute('x2', x2); l.setAttribute('y2', y2);
  parent.appendChild(l);
  return l;
}

function text(parent, x, y, s) {
  const t = document.createElementNS(SVGNS, 'text');
  t.setAttribute('x', x); t.setAttribute('y', y);
  t.setAttribute('class', 'map-grid-label');
  t.textContent = s;
  parent.appendChild(t);
  return t;
}

// Rebuilds only its own groups (overlay layers survive resize redraws) and
// inserts them first so overlays stay on top.
export function drawGrid(svg, grid, viewUnits) {
  svg.querySelectorAll(':scope > .map-grid, :scope > .map-grid-labels')
    .forEach(function (g) { svg.removeChild(g); });
  if (!grid) return;
  const step = viewUnits / grid.cols;
  const frag = document.createDocumentFragment();
  const gridG = document.createElementNS(SVGNS, 'g');
  gridG.setAttribute('class', 'map-grid');
  for (let i = 0; i <= grid.cols; i++) {
    const p = i * step;
    line(gridG, p, 0, p, viewUnits);
    line(gridG, 0, p, viewUnits, p);
  }
  frag.appendChild(gridG);
  const letters = grid.row_letters || 'ABCDEFGHI';
  const labelG = document.createElementNS(SVGNS, 'g');
  labelG.setAttribute('class', 'map-grid-labels');
  for (let r = 0; r < grid.rows; r++) {
    const letter = letters.charAt(grid.rows - 1 - r);
    for (let c = 0; c < grid.cols; c++) {
      text(labelG, c * step + step * 0.5, r * step + step * 0.5, letter + (c + 1));
    }
  }
  frag.appendChild(labelG);
  svg.insertBefore(frag, svg.firstChild);

  // No internal PvE/PvP split line: on our server PvE/PvP is per-INSTANCE
  // (the PvE|PvP tab), not a zone within one map.
}

// V1 sectorCenter math. Preserve exactly.
export function sectorCenter(grid, viewUnits, sector) {
  if (!grid || !sector) return null;
  const letters = grid.row_letters || 'ABCDEFGHI';
  const row = letters.indexOf(sector.charAt(0));
  const col = parseInt(sector.slice(1), 10);
  if (row < 0 || !col) return null;
  const step = viewUnits / grid.cols;
  const rowFromTop = (grid.rows - 1) - row;
  return { x: (col - 0.5) * step, y: (rowFromTop + 0.5) * step };
}
