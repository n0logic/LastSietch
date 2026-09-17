// SVG live overlays: spice diamonds, sandworm glyphs + trail, sandstorm
// geometry. Draw fns take explicit state and return their hover-region
// registries; motion is delegated to reckon.js. No innerHTML anywhere; feed
// strings only ever cross the boundary as data.

import { sectorCenter } from './grid.js';

const SVGNS = 'http://www.w3.org/2000/svg';

const WORM_GLIDE_MS = 10000;   // = /live cadence (V1 glide duration)
const TRAIL_LEN = 3;
const TRAIL_OPACITY = [0.4, 0.25, 0.12];         // newest -> oldest
const STORM_STALE_MS = 12 * 60 * 1000;           // freeze extrapolation past this

export function isFiniteNum(v) {
  return typeof v === 'number' && isFinite(v);
}

// Storm heading yaw -> 8-point compass (screen dir = (cos yaw, sin yaw);
// +x=E, +y=S). Sign VERIFIED 2026-07-04 against a live sweep (measured drift
// bearing 34.47 deg vs heading_yaw 34.5; ops/storm-heading-verify/).
export function headingToCompass(yaw) {
  if (!isFiniteNum(yaw)) return '';
  const d = ((yaw % 360) + 360) % 360;
  return ['E', 'SE', 'S', 'SW', 'W', 'NW', 'N', 'NE'][Math.round(d / 45) % 8];
}

// Bearing between two normalized points -> 8-point compass (V1, verbatim).
export function compass(dx, dy) {
  let ang = Math.atan2(dx, -dy) * 180 / Math.PI;
  if (ang < 0) ang += 360;
  return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(ang / 45) % 8];
}

function diamond(parent, x, y, r, cls) {
  const p = document.createElementNS(SVGNS, 'path');
  p.setAttribute('d', 'M' + x + ' ' + (y - r) + 'L' + (x + r) + ' ' + y +
    'L' + x + ' ' + (y + r) + 'L' + (x - r) + ' ' + y + 'Z');
  p.setAttribute('class', cls);
  parent.appendChild(p);
  return p;
}

// ---- spice ----------------------------------------------------------------
// p: { hasSpice, grid, viewUnits, hidden, candidates, candidateCoords,
//      mediums, spice, dim }. Returns the hover-region registry.
export function drawSpice(svg, p) {
  const old = svg.querySelector('.map-spice-layer');
  if (old) svg.removeChild(old);
  const hover = [];
  if (!p.hasSpice) return hover;
  const layer = document.createElementNS(SVGNS, 'g');
  layer.setAttribute('class', 'map-spice-layer');
  const step = p.viewUnits / (p.grid ? p.grid.cols : 9);

  // Candidate Large sites: exact projected coord when the reader supplied one,
  // else the sector center so the layer never blanks between rotations.
  const coords = p.candidateCoords || {};
  if (!p.hidden['spice_large']) {
    (p.candidates || []).forEach(function (sec) {
      const xy = coords[sec];
      const c = (xy && xy.length === 2)
        ? { x: xy[0], y: xy[1] } : sectorCenter(p.grid, p.viewUnits, sec);
      if (!c) return;
      diamond(layer, c.x, c.y, step * 0.30, 'map-spice-candidate');
      hover.push({ x: c.x, y: c.y, r: step * 0.42,
        label: 'Candidate Large spice field, ' + sec });
    });
  }

  // Medium spice fields: full per-cycle set at exact coords, static from /data.
  if (!p.hidden['spice_medium']) {
    (p.mediums || []).forEach(function (md) {
      if (!md || md.length < 2) return;
      // md[3] = active/erupted (rotates among the static medium sites).
      const medActive = md[3] === true;
      diamond(layer, md[0], md[1], step * 0.20,
              medActive ? 'map-spice-medium-active' : 'map-spice-medium');
      hover.push({ x: md[0], y: md[1], r: step * 0.28,
        label: (medActive ? 'Active medium spice field' : 'Medium spice field')
          + (md[2] ? ', ' + md[2] : '') });
    });
  }

  const dims = (p.spice && p.spice.dimensions) || {};
  const info = dims[String(p.dim)];
  if (info && info.large_active && !p.hidden['spice_large']) {
    // Every surfaced Large (2-3 at once); fall back to the single ram_sector
    // pin when the reader did not supply the multi-field list (legacy).
    const blows = (info.ram_active_fields && info.ram_active_fields.length)
      ? info.ram_active_fields.map(function (f) {
          const fsec = (f.sector || '').toUpperCase();
          const fc = (f.nx != null && f.ny != null)
            ? { x: f.nx, y: f.ny }
            : (fsec ? sectorCenter(p.grid, p.viewUnits, fsec) : null);
          return { sec: fsec, c: fc };
        })
      : (function () {
          const sec = (info.ram_sector || '').toUpperCase();
          const c = (info.ram_nx != null && info.ram_ny != null)
            ? { x: info.ram_nx, y: info.ram_ny }
            : (sec ? sectorCenter(p.grid, p.viewUnits, sec) : null);
          return [{ sec: sec, c: c }];
        })();
    blows.forEach(function (b) {
      if (!b.c) return;
      diamond(layer, b.c.x, b.c.y, step * 0.38, 'map-spice-active');
      const pulse = document.createElementNS(SVGNS, 'circle');
      pulse.setAttribute('cx', b.c.x); pulse.setAttribute('cy', b.c.y);
      pulse.setAttribute('r', step * 0.5);
      pulse.setAttribute('class', 'map-spice-pulse');
      layer.insertBefore(pulse, layer.firstChild);
      hover.push({ x: b.c.x, y: b.c.y, r: step * 0.5,
        label: 'Active Large spice blow' + (b.sec ? ', ' + b.sec : '') });
    });
  }
  svg.appendChild(layer);
  return hover;
}

// ---- worms ------------------------------------------------------------------
// p: { hasSpice, show, grid, viewUnits, worms, els, reckoner, reduce }. els is
// the engine-owned per-worm record map (id -> {g, threat, u, x, y, trail}).
// reduce skips the SMIL wave animation (prefers-reduced-motion; CSS cannot
// disable SMIL, so it must not be appended at all). Returns the hover registry.
export function drawWorms(svg, p) {
  const existing = svg.querySelector('.map-worm-layer');
  const els = p.els;
  if (!p.hasSpice || !p.show) {
    if (existing) svg.removeChild(existing);
    Object.keys(els).forEach(function (id) {
      p.reckoner.remove('worm:' + id);
      delete els[id];
    });
    return [];
  }
  let layer = existing;
  if (!layer) {
    layer = document.createElementNS(SVGNS, 'g');
    layer.setAttribute('class', 'map-worm-layer');
    Object.keys(els).forEach(function (id) { delete els[id]; });
  }
  // Keep worms above the per-poll spice redraw (storm draws after this).
  svg.appendChild(layer);
  const hover = [];
  const step = p.viewUnits / (p.grid ? p.grid.cols : 9);
  const unit = step * 0.014;
  const seen = {};
  (p.worms || []).forEach(function (w) {
    if (w.nx == null || w.ny == null) return;
    const id = (w.id != null) ? ('id' + w.id) : ('p' + w.nx + '_' + w.ny);
    seen[id] = true;
    const threat = w.threat || (w.surfaced ? 'surfaced' : 'submerged');
    const age = w.age_s || 0;
    const op = age <= 60 ? 1 : age >= 360 ? 0.45 : 1 - (age - 60) / 545;
    let rec = els[id];
    let snap = false;
    let moved = false;
    if (!rec || rec.threat !== threat) {
      // New worm, or threat changed (wave SMIL + class depend on threat, so
      // rebuild). Snap to the reported position; no glide on first sight.
      const prevTrail = rec ? rec.trail : [];
      if (rec && rec.g && rec.g.parentNode) rec.g.parentNode.removeChild(rec.g);
      const g = wormGlyph(layer, w.nx, w.ny, unit, w, p.reduce);
      rec = els[id] = { g: g, threat: threat, u: unit,
                        x: w.nx, y: w.ny, trail: prevTrail };
      snap = true;
    } else {
      rec.u = unit;
      rec.g.setAttribute('opacity', op.toFixed(2));
      moved = w.nx !== rec.x || w.ny !== rec.y;
      if (moved) {
        rec.trail.unshift({ x: rec.x, y: rec.y });
        if (rec.trail.length > TRAIL_LEN) rec.trail.length = TRAIL_LEN;
        rec.x = w.nx;
        rec.y = w.ny;
      }
    }
    // Only feed the reckoner on a real change: re-registering an unchanged
    // target resets fromX=toX, which would snap a mid-glide worm on every
    // legend toggle or instance-switch redraw.
    if (snap || moved) {
      p.reckoner.lerpFix('worm:' + id, w.nx, w.ny, {
        intervalMs: WORM_GLIDE_MS,
        snap: snap,
        apply: makeWormApply(els, id),
      });
    }
    hover.push({ x: w.nx, y: w.ny, r: step * 0.12, label: wormLabel(w) });
  });
  // Drop worms no longer in the feed.
  Object.keys(els).forEach(function (id) {
    if (!seen[id]) {
      if (els[id].g && els[id].g.parentNode) els[id].g.parentNode.removeChild(els[id].g);
      p.reckoner.remove('worm:' + id);
      delete els[id];
    }
  });
  drawWormTrail(layer, els, step);
  return hover;
}

function makeWormApply(els, id) {
  return function (x, y) {
    const rec = els[id];
    if (!rec || !rec.g) return;
    rec.g.setAttribute('transform',
      'translate(' + x.toFixed(1) + ',' + y.toFixed(1) + ') scale(' + rec.u + ')');
  };
}

// Last TRAIL_LEN reported positions per worm as fading dots, under the glyphs.
function drawWormTrail(layer, els, step) {
  const old = layer.querySelector('.map-worm-trail-layer');
  if (old) layer.removeChild(old);
  const g = document.createElementNS(SVGNS, 'g');
  g.setAttribute('class', 'map-worm-trail-layer');
  Object.keys(els).forEach(function (id) {
    (els[id].trail || []).forEach(function (pt, i) {
      const dot = document.createElementNS(SVGNS, 'circle');
      dot.setAttribute('cx', pt.x); dot.setAttribute('cy', pt.y);
      dot.setAttribute('r', step * 0.02);
      dot.setAttribute('class', 'map-worm-trail');
      dot.setAttribute('opacity', TRAIL_OPACITY[i] || 0.1);
      g.appendChild(dot);
    });
  });
  layer.insertBefore(g, layer.firstChild);
}

function wormGlyph(parent, x, y, u, w, reduce) {
  const threat = w.threat || (w.surfaced ? 'surfaced' : 'submerged');
  const g = document.createElementNS(SVGNS, 'g');
  g.setAttribute('class', 'map-worm is-' + threat);
  g.setAttribute('transform', 'translate(' + x + ',' + y + ') scale(' + u + ')');
  const age = w.age_s || 0;
  const op = age <= 60 ? 1 : age >= 360 ? 0.45 : 1 - (age - 60) / 545;
  g.setAttribute('opacity', op.toFixed(2));
  const arrow = document.createElementNS(SVGNS, 'path');
  arrow.setAttribute('d', 'M-13 0 L-7 -4 L-8.5 0 L-7 4 Z');
  arrow.setAttribute('class', 'map-worm__arrow');
  g.appendChild(arrow);
  const wave = document.createElementNS(SVGNS, 'path');
  wave.setAttribute('class', 'map-worm__wave');
  const calm = threat === 'submerged';
  wave.setAttribute('d', wavePath(calm ? 0 : 1, 0));
  if (!calm && !reduce) {
    const amp = threat === 'breaching' ? 1.35 : threat === 'enraged' ? 1.1 : 0.85;
    const dur = threat === 'breaching' ? '0.45s' : threat === 'enraged' ? '0.6s' : '0.85s';
    const an = document.createElementNS(SVGNS, 'animate');
    an.setAttribute('attributeName', 'd');
    an.setAttribute('dur', dur);
    an.setAttribute('repeatCount', 'indefinite');
    an.setAttribute('calcMode', 'linear');
    an.setAttribute('values', [
      wavePath(amp, 0), wavePath(amp, 1), wavePath(amp, 2),
      wavePath(amp, 3), wavePath(amp, 0)
    ].join(';'));
    wave.appendChild(an);
  }
  g.appendChild(wave);
  parent.appendChild(g);
  return g;
}

function wavePath(amp, phase) {
  const heights = [0, -3, 2, -8, 9, -12, 10, -7, 4, -2, 0];
  const pts = [], n = heights.length, x0 = -5, dx = 1.7;
  for (let i = 0; i < n; i++) {
    const hv = heights[(i + (phase | 0)) % n];
    const yv = (i === 0 || i === n - 1) ? 0 : hv * amp;
    pts.push((x0 + i * dx).toFixed(1) + ' ' + yv.toFixed(1));
  }
  return 'M' + pts.join(' L');
}

// "Sandworm" in the functional readout (in-game term). No em dashes.
export function wormLabel(w) {
  let s = 'Sandworm';
  if (w.sector) s += ', sector ' + w.sector;
  const tag = { breaching: 'BREACHING', enraged: 'enraged',
                surfaced: 'roaming', submerged: 'submerged' }[w.threat];
  if (tag) s += ' (' + tag + ')';
  if (w.age_s != null && w.age_s > 120) s += ' · ' + Math.round(w.age_s / 60) + 'm ago';
  return s;
}

// ---- sandstorm --------------------------------------------------------------
// Consensus geometry: a MOVING storm whose primary signal is its projected
// CENTER + heading arrow; a translucent radius circle when the payload carries
// one; optional band dead-code. Absent all geometry: nothing drawn (the
// console carries the ETA). Center geometry rides in one translated group
// (.map-storm-track) so reckon.js can move circle + marker + arrow together
// between RAM fixes; is-extrapolated is toggled on it for amber styling.
// p: { hasStorms, hidden, sandstorm, dim, grid, viewUnits, reckoner }
export function drawStorm(svg, p) {
  const old = svg.querySelector('.map-storm-layer');
  if (old) svg.removeChild(old);
  if (!p.hasStorms || p.hidden['sandstorm']) { p.reckoner.remove('storm'); return; }
  // Backend re-keys each map's storms onto the instance dim, so this lookup
  // isolates one sietch: a Kulon storm cannot draw on the Habbanya tab.
  const info = ((p.sandstorm && p.sandstorm.dimensions) || {})[String(p.dim)];
  if (!info) { p.reckoner.remove('storm'); return; }
  const cx = info.center_nx, cy = info.center_ny;
  const hasCenter = isFiniteNum(cx) && isFiniteNum(cy);
  const sx = info.start_nx, sy = info.start_ny, ex = info.end_nx, ey = info.end_ny;
  const hasBand = isFiniteNum(sx) && isFiniteNum(sy) && isFiniteNum(ex) && isFiniteNum(ey);
  if (!hasCenter && !hasBand) { p.reckoner.remove('storm'); return; }
  const layer = document.createElementNS(SVGNS, 'g');
  layer.setAttribute('class', 'map-storm-layer');
  const step = p.viewUnits / (p.grid ? p.grid.cols : 9);
  const r = info.radius_nr;

  // Band/front: optional dead-code, drawn only if start+end ever arrive.
  if (hasBand) {
    const band = document.createElementNS(SVGNS, 'line');
    band.setAttribute('x1', sx); band.setAttribute('y1', sy);
    band.setAttribute('x2', ex); band.setAttribute('y2', ey);
    band.setAttribute('class', 'map-storm-band');
    layer.appendChild(band);
  }

  if (hasCenter) {
    const track = document.createElementNS(SVGNS, 'g');
    track.setAttribute('class', 'map-storm-track');
    track.setAttribute('transform', 'translate(' + cx + ',' + cy + ')');
    if (isFiniteNum(r) && r > 0) {
      const circle = document.createElementNS(SVGNS, 'circle');
      circle.setAttribute('cx', 0); circle.setAttribute('cy', 0);
      circle.setAttribute('r', r);
      circle.setAttribute('class', 'map-storm-circle');
      track.appendChild(circle);
    }
    const mark = document.createElementNS(SVGNS, 'circle');
    mark.setAttribute('cx', 0); mark.setAttribute('cy', 0);
    mark.setAttribute('r', step * 0.10);
    mark.setAttribute('class', 'map-storm-center');
    track.appendChild(mark);
    // Heading arrow from the storm yaw. UE yaw degrees; world +X -> screen
    // right, +Y -> screen down (DD cal flipY=false), so dir = (cos, sin).
    // Sign/axis VERIFIED 2026-07-04 against a live sweep (ops/storm-heading-verify/).
    const h = info.heading_yaw;
    if (isFiniteNum(h)) {
      const alen = (isFiniteNum(r) && r > 0) ? r : step * 0.6;
      const rad = h * Math.PI / 180;
      const arrow = document.createElementNS(SVGNS, 'line');
      arrow.setAttribute('x1', 0); arrow.setAttribute('y1', 0);
      arrow.setAttribute('x2', Math.cos(rad) * alen);
      arrow.setAttribute('y2', Math.sin(rad) * alen);
      arrow.setAttribute('class', 'map-storm-heading');
      track.appendChild(arrow);
    }
    layer.appendChild(track);

    const atMs = info.storm_scanned_utc
      ? new Date(info.storm_scanned_utc).getTime() : NaN;
    if (isFinite(atMs)) {
      p.reckoner.extrapolateFix('storm', cx, cy, isFiniteNum(h) ? h : null, {
        atMs: atMs,
        staleMs: STORM_STALE_MS,
        apply: function (x, y, estimated) {
          track.setAttribute('transform', 'translate(' + x + ',' + y + ')');
          track.classList.toggle('is-extrapolated', !!estimated);
        },
      });
    } else {
      // No scan timestamp: static placement only, nothing to extrapolate from.
      p.reckoner.remove('storm');
    }
  } else {
    p.reckoner.remove('storm');
  }
  svg.appendChild(layer);
}
