// Marker clustering for the dense layers (ore, salvage, flora, camps, hazards) of the
// holo table. THREE-free so the smoke can pin it. Points of interest (lodClass 0:
// caves, landmarks, stations, CHOAM, shipwrecks, vendors, hubs) are never passed in.
//
// A quadtree-aligned world grid: at level L the cell edge is view / 2^L, so every cell
// at L+1 sits inside one cell at L. The level follows the zoom in whole steps (with
// hysteresis), which means clusters only ever split as you zoom in, never reshuffle,
// and small zoom changes change nothing at all.

import { lodClass } from './icon-atlas.js';

// Only the dense layers cluster; every point of interest stays an individual icon
export function isClusterCat(cat) { return lodClass(cat) !== 0; }

// A marker is drawn (and so clusterable / countable) unless its type or category is hidden
export function markerVisible(m, catIndex, hidden) {
  const typeIdx = m[3];
  if (typeof typeIdx === 'number' && hidden[typeIdx]) return false;
  return hidden[catIndex[m[2]] || 'other'] !== true;
}

export const CELL_PX = 110;         // target on-screen cell edge, CSS px
export const CLUSTER_MIN = 3;       // same-type markers in one cell to form a cluster
export const CLUSTER_OFF_ZOOM = 6;  // at and past this zoom every marker is individual
export const LEVEL_MIN = 1, LEVEL_MAX = 9;
const HYST = 0.65;                  // a level change needs this much log2 zoom travel
// Clusters drawn at once per quality tier; the biggest win, the rest stay individual.
export const CLUSTER_CAP = { high: 1400, mid: 800, low: 300 };

// zoom + the viewport's short edge -> quadtree level (0 = clustering off)
export function clusterLevel(zoom, disp, prev) {
  if (!(zoom < CLUSTER_OFF_ZOOM)) return 0;
  const x = Math.log2(Math.max(1, disp) * Math.max(1, zoom) / CELL_PX);
  let L = Math.round(x);
  if (prev >= LEVEL_MIN && Math.abs(x - prev) <= HYST) L = prev;
  return Math.max(LEVEL_MIN, Math.min(LEVEL_MAX, L));
}

// items: [{ x, y, key, k }] in 0..view board units. Returns
//   { clusters: [{ x, y, count, key, ks, minX, maxX, minY, maxY }], singles: [k...] }
// Every item lands in exactly one place, so sum(counts) + singles.length === items.length.
export function buildClusters(items, level, view, tier) {
  const out = { clusters: [], singles: [] };
  if (!level) { for (let i = 0; i < items.length; i++) out.singles.push(items[i].k); return out; }
  const n = Math.pow(2, level), edge = view / n;
  const cells = new Map();
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const cx = Math.min(n - 1, Math.max(0, Math.floor(it.x / edge)));
    const cy = Math.min(n - 1, Math.max(0, Math.floor(it.y / edge)));
    const id = (cy * n + cx) + '|' + it.key;
    let c = cells.get(id);
    if (!c) cells.set(id, c = []);
    c.push(it);
  }
  const all = [];
  cells.forEach(function (arr) {
    if (arr.length < CLUSTER_MIN) { for (let i = 0; i < arr.length; i++) out.singles.push(arr[i].k); return; }
    let sx = 0, sy = 0, minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    const ks = [];
    for (let i = 0; i < arr.length; i++) {
      const it = arr[i];
      sx += it.x; sy += it.y; ks.push(it.k);
      if (it.x < minX) minX = it.x; if (it.x > maxX) maxX = it.x;
      if (it.y < minY) minY = it.y; if (it.y > maxY) maxY = it.y;
    }
    all.push({ x: sx / arr.length, y: sy / arr.length, count: arr.length, key: arr[0].key, ks: ks,
               minX: minX, maxX: maxX, minY: minY, maxY: maxY });
  });
  const cap = (CLUSTER_CAP[tier] || CLUSTER_CAP.mid);
  if (all.length > cap) {
    all.sort(function (a, b) { return b.count - a.count; });
    for (let i = cap; i < all.length; i++) for (let j = 0; j < all[i].ks.length; j++) out.singles.push(all[i].ks[j]);
    all.length = cap;
  }
  out.clusters = all;
  return out;
}

// Disc diameter in CSS px: log scale on the count, clamped.
export function discPx(count) {
  return Math.max(24, Math.min(48, 16 + 6 * Math.log2(Math.max(1, count))));
}

// Count -> badge glyph codes: 0-9 digits, 10 = 'k', 11 = '.'; 1234 -> "1.2k", 12000 -> "12k"
export function badgeGlyphs(count) {
  let s;
  if (count < 1000) s = String(count);
  else if (count < 10000) s = (Math.floor(count / 100) / 10).toFixed(1).replace(/\.0$/, '') + 'k';
  else s = Math.min(999, Math.round(count / 1000)) + 'k';
  const g = [];
  for (let i = 0; i < s.length; i++) g.push(s[i] === 'k' ? 10 : s[i] === '.' ? 11 : s.charCodeAt(i) - 48);
  return { text: s, glyphs: g };
}

// Camera zoom that frames a cluster's bounds: at least twice the current zoom (so the
// quadtree level steps up and the cluster really splits), more when the bounds are small.
export function zoomForBounds(c, zoom, view) {
  const span = Math.max(c.maxX - c.minX, c.maxY - c.minY, 30);
  const fit = (view * 0.6) / span;
  return Math.max(1, Math.min(8, Math.max(zoom * 2, fit)));
}
