// Per-player me-layer (V1 drawMe port): own position pin, base totems, owned
// vehicles. The self pin is the one moving entity: it lerps between /me fixes
// via reckon.js (maxFactor 1.5, positions are save-tick bound) and renders
// is-estimated (amber) mid-glide; only a real fresh fix earns Ibad. Bases and
// vehicles are static per fix. No innerHTML; labels cross as data.

import { compass } from './overlays.js';
import { ensureIconDefs, iconUse, tokenIcon } from './icons.js';

const SVGNS = 'http://www.w3.org/2000/svg';
const SELF_ID = 'me:self';

function dimVisible(dim, selectedDim) {
  if (selectedDim === null || selectedDim === undefined) return true;
  if (dim === null || dim === undefined) return true;
  return Number(dim) === Number(selectedDim);
}

// A marker is visible on the selected instance when its partition matches (the
// authoritative sietch discriminator, e.g. Hagga 1=Habbanya / 32=Kulon /
// 33=Amtal); when the instance declares no partition we fall back to the
// dimension (DD 0/1).
function instVisible(entity, selDim, selPart) {
  if (selPart !== null && selPart !== undefined) {
    if (entity.part === null || entity.part === undefined) return true;
    return Number(entity.part) === Number(selPart);
  }
  return dimVisible(entity.dim, selDim);
}

// p: { authed, me, show: {self,bases,vehicles}, dim, viewUnits, els,
//      reckoner, intervalMs, fresh }. els is the engine-owned record map (self
//      pin element survives redraws so mid-glide position is never lost).
// fresh = the /me feed freshness (chrome's meFresh clock, pushed through
// engine.setMeFresh): stale drops the pin to is-stale (amber, breathe killed
// by chrome CSS) at its last-known position, board and chip must agree.
// Returns the hover-region registry.
export function drawMe(svg, p) {
  ensureIconDefs(svg);
  const old = svg.querySelector('.map-me-layer');
  if (old) svg.removeChild(old);
  const els = p.els;
  const hover = [];
  const me = p.me;
  if (!p.authed || !me) {
    dropSelf(els, p.reckoner);
    return hover;
  }
  const layer = document.createElementNS(SVGNS, 'g');
  layer.setAttribute('class', 'map-me-layer');
  const unit = p.viewUnits / 1000;

  if (p.show.bases) {
    (me.bases || []).forEach(function (b) {
      if (!instVisible(b, p.dim, p.part)) return;
      meHouse(layer, b.nx, b.ny, 13 * unit, b.kind === 'outpost');
      hover.push({ x: b.nx, y: b.ny, r: 14 * unit,
        label: (b.name ? b.name + ', ' : '') +
               (b.kind === 'outpost' ? 'your outpost' : 'your base') });
    });
  }
  if (p.show.vehicles) {
    (me.vehicles || []).forEach(function (v) {
      if (!instVisible(v, p.dim, p.part)) return;
      meVehicle(layer, v.nx, v.ny, 20 * unit, v.icon);
      hover.push({ x: v.nx, y: v.ny, r: 12 * unit,
        label: (v.name || 'Vehicle') + ' (yours)' });
    });
  }

  const s = me.self;
  if (p.show.self && s && instVisible(s, p.dim, p.part)) {
    const online = !!s.online;
    let rec = els.self;
    let snap = false;
    let moved = false;
    if (!rec || rec.online !== online || rec.unit !== unit) {
      // First sight, presence flip, or map-scale change: rebuild + snap
      // (no glide on first sight, V1 semantics).
      if (rec && rec.g && rec.g.parentNode) rec.g.parentNode.removeChild(rec.g);
      const g = mePin(layer, 15 * unit, online);
      g.setAttribute('transform', 'translate(' + s.nx + ',' + s.ny + ')');
      rec = els.self = { g: g, online: online, unit: unit, x: s.nx, y: s.ny };
      snap = true;
    } else {
      // Keep the live node (and its mid-glide transform): move it into the
      // fresh layer instead of rebuilding.
      layer.appendChild(rec.g);
      moved = s.nx !== rec.x || s.ny !== rec.y;
      rec.x = s.nx; rec.y = s.ny;
    }
    // Ibad-only-for-live: a dead /me feed degrades the pin honestly.
    rec.g.classList.toggle('is-stale', p.fresh === false);
    if (snap || moved) {
      p.reckoner.lerpFix(SELF_ID, s.nx, s.ny, {
        intervalMs: p.intervalMs,
        maxFactor: 1.5,          // never extrapolate past 1.5x the fix interval
        snap: snap,
        apply: makeSelfApply(els),
      });
    }
    hover.push({ x: s.nx, y: s.ny - 13 * unit, r: 16 * unit,
      label: online ? 'You are here' : 'You, last known position (offline)' });
  } else {
    dropSelf(els, p.reckoner);
  }
  svg.appendChild(layer);
  return hover;
}

function dropSelf(els, reckoner) {
  if (els.self) {
    if (els.self.g && els.self.g.parentNode) {
      els.self.g.parentNode.removeChild(els.self.g);
    }
    delete els.self;
  }
  reckoner.remove(SELF_ID);
}

function makeSelfApply(els) {
  return function (x, y, estimated) {
    const rec = els.self;
    if (!rec || !rec.g) return;
    rec.g.setAttribute('transform', 'translate(' + x + ',' + y + ')');
    // Estimates are amber; only a real fresh fix earns Ibad (chrome CSS).
    rec.g.classList.toggle('is-estimated', !!estimated);
  };
}

// V1 mePin drawn at local (0,0) so the group transform carries the position.
function mePin(parent, r, online) {
  const g = document.createElementNS(SVGNS, 'g');
  g.setAttribute('class', 'map-me-pin' + (online ? ' is-online' : ' is-offline'));
  if (online) {
    // Breathing ring: genuinely live, CSS-animated (map-me-pulse).
    const pulse = document.createElementNS(SVGNS, 'circle');
    pulse.setAttribute('cx', 0); pulse.setAttribute('cy', 0);
    pulse.setAttribute('r', r * 0.95);
    pulse.setAttribute('class', 'map-me-pulse');
    g.appendChild(pulse);
  }
  const top = -r * 1.9;
  const p = document.createElementNS(SVGNS, 'path');
  p.setAttribute('d', 'M0 0' +
    'C' + (-r) + ' ' + (top + r * 0.6) + ' ' + (-r) + ' ' + top + ' 0 ' + top +
    'C' + r + ' ' + top + ' ' + r + ' ' + (top + r * 0.6) + ' 0 0Z');
  p.setAttribute('class', 'map-me-pin__body');
  g.appendChild(p);
  const dot = document.createElementNS(SVGNS, 'circle');
  dot.setAttribute('cx', 0); dot.setAttribute('cy', top + r * 0.55);
  dot.setAttribute('r', r * 0.34);
  dot.setAttribute('class', 'map-me-pin__dot');
  g.appendChild(dot);
  parent.appendChild(g);
  return g;
}

// Your holdings: the amber keep sprite, an outpost at three quarters. The
// hover radius in drawMe is unchanged; the polygon stays as the fallback.
function meHouse(parent, x, y, r, small) {
  const g = document.createElementNS(SVGNS, 'g');
  g.setAttribute('class', 'map-me-base' + (small ? ' is-outpost' : ''));
  const w = r * (small ? 0.7 : 0.9);
  if (iconUse(g, 'base-mine', x, y, w * 2.2, 'map-me-base__icon map-icon')) {
    parent.appendChild(g);
    return;
  }
  const p = document.createElementNS(SVGNS, 'path');
  p.setAttribute('d',
    'M' + (x - w) + ' ' + (y - w * 0.1) +
    'L' + x + ' ' + (y - w) +
    'L' + (x + w) + ' ' + (y - w * 0.1) +
    'L' + (x + w) + ' ' + (y + w) +
    'L' + (x - w) + ' ' + (y + w) + 'Z');
  p.setAttribute('class', 'map-me-base__body');
  g.appendChild(p);
  parent.appendChild(g);
}

// Your vehicles: the sprite for the class the container token names; the
// cargo container (and any token without a sprite) keeps its PNG.
function meVehicle(parent, x, y, r, icon) {
  const name = tokenIcon(icon);
  if (name && iconUse(parent, name, x, y, r, 'map-me-vehicle__icon map-icon')) return;
  if (icon) {
    const img = document.createElementNS(SVGNS, 'image');
    // icon is a server-generated token today; encode anyway (free hardening).
    img.setAttribute('href',
      '/admin/static/img/containers/' + encodeURIComponent(icon) + '.png');
    img.setAttribute('x', x - r / 2); img.setAttribute('y', y - r / 2);
    img.setAttribute('width', r); img.setAttribute('height', r);
    img.setAttribute('class', 'map-me-vehicle__icon');
    parent.appendChild(img);
  } else {
    const c = document.createElementNS(SVGNS, 'circle');
    c.setAttribute('cx', x); c.setAttribute('cy', y); c.setAttribute('r', r * 0.4);
    c.setAttribute('class', 'map-me-vehicle__dot');
    parent.appendChild(c);
  }
}

// ---- worm proximity (V1 updateProximity math, minus all DOM/copy) ----------
// self: {nx, ny} or null. worms: current-dim worm list. step: sector size in
// view units. Returns null when no readout applies, else
// { tier: 'danger'|'warn'|'caution'|'clear', distSectors, dir, sector,
//   closing } for chrome to word (chrome owns copy; no em dashes).
export function wormProximity(self, worms, step) {
  if (!self || self.nx == null || !worms || !worms.length || !step) return null;
  let nearest = null, bestD = Infinity;
  worms.forEach(function (w) {
    if (w.nx == null) return;
    const dx = w.nx - self.nx, dy = w.ny - self.ny, d = dx * dx + dy * dy;
    if (d < bestD) { bestD = d; nearest = w; }
  });
  if (!nearest) return null;
  const distSec = Math.sqrt(bestD) / step;
  const dir = compass(nearest.nx - self.nx, nearest.ny - self.ny);
  const tier = distSec <= 0.65 ? 'danger'
    : distSec <= 1.5 ? 'warn'
    : distSec <= 3 ? 'caution' : 'clear';
  return {
    tier: tier,
    distSectors: Math.round(distSec * 10) / 10,
    dir: dir,
    sector: nearest.sector || null,
    closing: nearest.threat === 'breaching' || nearest.threat === 'enraged',
  };
}
