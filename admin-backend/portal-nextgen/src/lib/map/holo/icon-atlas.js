// Marker icon atlas + level-of-detail for the holo table. THREE-free so the
// smoke script can import it. The atlas is built by ops/map-icon-atlas from the
// same type_icons the 2D board resolves (the game's own T_UI_IconMap* art).
import atlas from './atlas.js';

export const ATLAS_URL = atlas.image;
export const FALLBACK_RECT = atlas.fallback;

// Resource layers are thousands strong; they swap dot -> icon with zoom.
// Everything else (caves, landmarks, stations, hubs, vendors, hazards) is a
// point of interest and shows its icon at every zoom.
const DENSE = { ore: true, salvage: true, flora: true };
export function isDenseCat(cat) { return DENSE[cat] === true; }
// Camps and hazard zones are hundreds strong: thinned too, but they start
// showing icons earlier than the resource nodes.
const MID = { enemy: true, hazard: true };
export function isMidCat(cat) { return MID[cat] === true; }
// LOD class carried per point: 0 = POI (always icon), 1 = resource, 2 = camp/hazard.
export function lodClass(cat) { return DENSE[cat] ? 1 : MID[cat] ? 2 : 0; }

// Atlas rect [u0, v0, u1, v1] for a type icon basename, or null when the type
// has no in-game icon (or the atlas does not know it): caller draws the dot.
export function rectFor(icon) {
  return (icon && atlas.rects[icon]) || null;
}

// Zoom ramp for dense layers: below ICON_ZOOM_START every node is a dot; the
// share of nodes drawn as icons then climbs linearly to all of them at
// ICON_ZOOM_FULL. Nodes are ranked once (stable hash) so the same ones turn
// into icons first and nothing shimmers while the user zooms.
export const ICON_ZOOM_START = 2.2;
export const ICON_ZOOM_FULL = 5.5;
// Icons on screen at once for the dense layers, per quality tier.
export const ICON_BUDGET = { high: 3500, mid: 1500, low: 400 };

export const MID_ZOOM_START = 1.4;
export const MID_ZOOM_FULL = 3.4;

export function iconFraction(zoom, tier, denseCount, start, full) {
  const s0 = start == null ? ICON_ZOOM_START : start;
  const t = (zoom - s0) / ((full == null ? ICON_ZOOM_FULL : full) - s0);
  const ramp = Math.max(0, Math.min(1, t));
  if (!denseCount) return ramp;
  // The budget caps the whole-board count; zoomed in, far fewer are in view,
  // so the cap loosens by the zoom area factor.
  const budget = (ICON_BUDGET[tier] || ICON_BUDGET.mid) * Math.max(1, zoom * zoom);
  return Math.min(ramp, budget / denseCount);
}

// Stable rank in [0, 1) per marker index (integer hash, no RNG state). Rare
// emphasised ore (Titanium/Stravidium) is given rank 0 by the caller so it
// turns into an icon first.
export function rankFor(i) {
  let h = (i + 1) | 0;
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  h = h ^ (h >>> 16);
  return (h >>> 0) / 4294967296;
}

// Draw order: later = on top. Dense resources first, POIs last.
const ORDER = ['flora', 'salvage', 'ore', 'hazard', 'enemy', 'other', 'poi',
               'district', 'vendor', 'service'];
export function drawRank(cat) {
  const i = ORDER.indexOf(cat);
  return i < 0 ? ORDER.indexOf('other') : i;
}
