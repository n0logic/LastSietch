// Map icon sprites, shared by the crowd layer (players.js) and the me-layer
// (me.js). Each icon is a 96 px WebP token served off the admin static tree
// (the same origin trick meVehicle used for the container PNGs). A flapping
// ornithopter is a 3-frame strip stacked vertically; the strip lives ONCE in a
// <symbol> and every marker is a <use>, so one SMIL timeline flaps every
// instance and the crowd never carries per-marker animation state. SMIL, not a
// CSS transform: a <use> clone repaints a SMIL-driven attribute, while a CSS
// animation inside a <symbol> was not seen to move the clones (headless
// capture, 2026-09-04). Reduced motion never gets the <animate> at all, the
// same gate the worm wave uses, because SMIL does not read CSS.
//
// PII rule: an icon is chosen off `t` / `st` (vehicle class and subtype), which
// are world ids the public feed already carries. Nothing here reads a name.

const SVGNS = 'http://www.w3.org/2000/svg';
const XLINK = 'http://www.w3.org/1999/xlink';

export const ICON_BASE = '/admin/static/img/v2/map-icons/v1/';
export const FRAME = 96;
export const FLAP_DUR = '0.45s';

function reducedMotion() {
  return !!(typeof window !== 'undefined' && window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

// "0;-96;-192;0": one stop per frame, then back to the first.
export function flapValues(frames) {
  const v = [];
  for (let i = 0; i < frames; i++) v.push(String(-i * FRAME));
  v.push('0');
  return v.join(';');
}

// name -> frame count. Mirrors admin-backend/static/img/v2/map-icons/v1/manifest.json.
export const ICONS = {
  'orni-light': 3,
  'orni-medium': 3,
  'orni-transport': 3,
  sandbike: 1,
  buggy: 1,
  sandcrawler: 1,
  'base-active': 1,
  'base-quiet': 1,
  'base-dormant': 1,
  'base-abandoned': 1,
  'base-orphaned': 1,
  'base-stored': 1,
  'base-mine': 1,
  'player-anon': 1,
  'player-amber': 1,
};

const ORNI_SUBTYPES = { light: 'orni-light', medium: 'orni-medium', transport: 'orni-transport' };
const GROUND = { sandbike: 'sandbike', buggy: 'buggy', sandcrawler: 'sandcrawler' };

// Container-icon tokens (routers/portal.py _container_icon) -> sprite names.
// vehicle-container and anything unknown return null so the caller keeps the
// PNG path it already had.
const TOKENS = {
  'vehicle-scout': 'orni-light',
  'vehicle-assault': 'orni-medium',
  'vehicle-carrier': 'orni-transport',
  'vehicle-sandbike': 'sandbike',
  'vehicle-buggy': 'buggy',
  'vehicle-sandcrawler': 'sandcrawler',
};

// Public vehicle feed row: t = class bucket, st = subtype (ornithopter tier).
export function vehicleIcon(t, st) {
  if (t === 'ornithopter') return ORNI_SUBTYPES[st] || 'orni-light';
  return GROUND[t] || null;
}

export function tokenIcon(token) {
  return TOKENS[token] || null;
}

function setHref(el, value) {
  el.setAttribute('href', value);
  el.setAttributeNS(XLINK, 'xlink:href', value);
}

// One <defs> per svg, appended once. Symbols are drawn on a 96 grid; a 3-frame
// strip sits inside a nested <svg> (overflow hidden by default) and a discrete
// SMIL <animate> on the image's y slides the strip by whole frames.
export function ensureIconDefs(svg) {
  if (!svg || svg.querySelector('.map-icon-defs')) return;
  const defs = document.createElementNS(SVGNS, 'defs');
  defs.setAttribute('class', 'map-icon-defs');
  Object.keys(ICONS).forEach(function (name) {
    const frames = ICONS[name];
    const sym = document.createElementNS(SVGNS, 'symbol');
    sym.setAttribute('id', 'mi-' + name);
    sym.setAttribute('viewBox', '0 0 ' + FRAME + ' ' + FRAME);
    const img = document.createElementNS(SVGNS, 'image');
    setHref(img, ICON_BASE + name + '.webp');
    img.setAttribute('width', FRAME);
    img.setAttribute('height', FRAME * frames);
    if (frames > 1) {
      const clip = document.createElementNS(SVGNS, 'svg');
      clip.setAttribute('width', FRAME);
      clip.setAttribute('height', FRAME);
      clip.setAttribute('viewBox', '0 0 ' + FRAME + ' ' + FRAME);
      img.setAttribute('class', 'map-icon-flap');
      if (!reducedMotion()) {
        const an = document.createElementNS(SVGNS, 'animate');
        an.setAttribute('attributeName', 'y');
        an.setAttribute('values', flapValues(frames));
        an.setAttribute('calcMode', 'discrete');
        an.setAttribute('dur', FLAP_DUR);
        an.setAttribute('repeatCount', 'indefinite');
        img.appendChild(an);
      }
      clip.appendChild(img);
      sym.appendChild(clip);
    } else {
      sym.appendChild(img);
    }
    defs.appendChild(sym);
  });
  svg.insertBefore(defs, svg.firstChild);
}

// Appends a <use> of the named icon centred on (x, y) in a box px square.
// Returns null for an unknown name so the caller can fall back to its dot.
export function iconUse(parent, name, x, y, box, cls) {
  if (!ICONS[name]) return null;
  const u = document.createElementNS(SVGNS, 'use');
  setHref(u, '#mi-' + name);
  u.setAttribute('x', x - box / 2);
  u.setAttribute('y', y - box / 2);
  u.setAttribute('width', box);
  u.setAttribute('height', box);
  if (cls) u.setAttribute('class', cls);
  parent.appendChild(u);
  return u;
}
