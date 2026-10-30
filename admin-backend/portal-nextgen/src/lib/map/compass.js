// Holo heading compass: a small DOM control the engine mounts inside the map
// viewport while a 3D viewer is active (the carved board never rotates, so it
// never shows). The dial's needle points at north on screen; clicking the dial
// steps the heading 45 deg clockwise, the N button returns to north-up at the
// default tilt. Pointer events stop here so the engine's viewport gestures
// (drag capture, marker hit-test) never see a compass press. Styled inline
// from the house tokens because the engine, not the route, owns this node.

const BTN = [
  'width:38px', 'height:38px', 'display:grid', 'place-items:center', 'padding:0',
  'background:var(--panel)', 'color:var(--text)',
  'border:1px solid var(--edge)', 'border-radius:var(--radius-sm)',
  'cursor:pointer', 'line-height:1',
  'box-shadow:inset 0 1px 0 var(--metal-hi), 0 6px 14px rgba(0,0,0,.35)',
].join(';');

// Needle: amber north half, dim south half, inside a faint ring. Built with
// createElementNS (the engine never uses innerHTML).
function buildNeedle() {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '24');
  svg.setAttribute('height', '24');
  svg.setAttribute('aria-hidden', 'true');
  const parts = [
    ['circle', { cx: '12', cy: '12', r: '10.5', fill: 'none', stroke: 'currentColor', 'stroke-opacity': '.35' }],
    ['path', { d: 'M12 3 L15 12 L12 10.6 L9 12 Z', fill: 'var(--accent-bright, var(--accent))' }],
    ['path', { d: 'M12 21 L15 12 L12 13.4 L9 12 Z', fill: 'currentColor', 'fill-opacity': '.45' }],
  ];
  parts.forEach(function (p) {
    const el = document.createElementNS(NS, p[0]);
    Object.keys(p[1]).forEach(function (k) {
      // Paints go through style so var(--token) resolves.
      if (k === 'fill' || k === 'stroke') el.style.setProperty(k, p[1][k]);
      else el.setAttribute(k, p[1][k]);
    });
    svg.appendChild(el);
  });
  return svg;
}

function stop(e) {
  e.stopPropagation();
}

// createCompass(viewport, { onStep, onReset }) -> { update(azimuth), destroy() }
export function createCompass(viewport, handlers) {
  const root = document.createElement('div');
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', 'Rotate view');
  root.style.cssText = 'position:absolute;top:var(--space-3);right:var(--space-3);z-index:12;' +
    'display:flex;flex-direction:column;gap:var(--space-1)';

  const dial = document.createElement('button');
  dial.type = 'button';
  dial.style.cssText = BTN;
  const needle = buildNeedle();
  dial.appendChild(needle);

  const north = document.createElement('button');
  north.type = 'button';
  north.style.cssText = BTN + ';font:600 var(--text-sm) "JetBrains Mono", ui-monospace, monospace;letter-spacing:.06em';
  north.textContent = 'N';
  north.setAttribute('aria-label', 'Reset to north up');
  north.title = 'North up';

  [dial, north].forEach(function (b) {
    b.addEventListener('pointerdown', stop);
    b.addEventListener('pointerup', stop);
    b.addEventListener('dblclick', stop);
  });
  dial.addEventListener('click', function (e) { stop(e); handlers.onStep(1); });
  north.addEventListener('click', function (e) { stop(e); handlers.onReset(); });

  root.appendChild(dial);
  root.appendChild(north);
  viewport.appendChild(root);

  function update(azimuth) {
    const deg = Math.round(azimuth || 0) % 360;
    // North sits at screen angle -heading.
    needle.style.transform = 'rotate(' + (-(azimuth || 0)) + 'deg)';
    const label = 'Heading ' + deg + ' degrees. Rotate 45 degrees (Q / E)';
    dial.setAttribute('aria-label', label);
    dial.title = 'Heading ' + String(deg).padStart(3, '0') + '°';
  }
  update(0);

  function destroy() {
    if (root.parentNode) root.parentNode.removeChild(root);
  }

  return { update, destroy };
}
