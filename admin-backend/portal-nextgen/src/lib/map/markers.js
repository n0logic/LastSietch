// Canvas marker renderer + lazy icon cache + nearest-marker hit tests
// (V1 drawCanvas, ported).

const ICON_BASE = '/admin/static/img/dune-icons/';

// V1 per-category dot radii. Preserve exactly.
export const RADIUS = { spice: 0, poi: 3.6, enemy: 3.2, ore: 2.4, salvage: 2.2,
                        hazard: 3.0, flora: 2.0, other: 2.2 };

// Hub categories (district/vendor/service) append last = drawn on top.
// "spice" stays out: it has its own SVG overlay pass.
const DRAW_ORDER = ['flora', 'salvage', 'ore', 'hazard', 'enemy', 'poi', 'other',
                    'district', 'vendor', 'service'];

// Canvas supersampling factor (V1 SS).
export function supersample() {
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  return Math.min(2, dpr) * 1.5;
}

// Lazy icon cache. onLoad fires when a new icon finishes decoding so the
// caller can redraw; destroy() turns late loads into no-ops (route unmount).
export function createIconCache(onLoad) {
  const cache = {};
  const loading = {};
  let dead = false;
  return {
    get(name) {
      const img = cache[name];
      return (img && img.complete && img.naturalWidth) ? img : null;
    },
    load(name) {
      if (dead || loading[name]) return;
      loading[name] = true;
      const img = new Image();
      img.onload = function () { if (!dead) { cache[name] = img; onLoad(); } };
      img.onerror = function () { cache[name] = false; };  // sentinel: skip retries
      img.src = ICON_BASE + name + '.png';
    },
    destroy() { dead = true; },
  };
}

// A marker is hidden by its numeric type idx first, then by an explicit legacy
// category key set true (preserves V1 legend key semantics).
export function markerHidden(m, catIndex, hidden) {
  const typeIdx = m[3];
  if (typeof typeIdx === 'number' && hidden[typeIdx]) return true;
  return hidden[catIndex[m[2]] || 'other'] === true;
}

// p: { markers, catIndex, catColors, typeIcons, hidden, hasGrid, scale, ss,
//      icons, clusters?, clusterMembers? }
// scale = baseScale * ss maps normalized coords to canvas pixels. When
// clusters is set (low zoom), clusterMembers markers are skipped and each
// cluster renders as one hotspot instead.
export function drawCanvas(ctx, canvas, p) {
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (p.hasGrid) {
    const bandTop = (7 / 9) * canvas.height;
    const grad = ctx.createLinearGradient(0, bandTop, 0, canvas.height);
    grad.addColorStop(0, 'rgba(58,40,24,0)');
    grad.addColorStop(0.55, 'rgba(52,35,20,0.55)');
    grad.addColorStop(1, 'rgba(38,26,15,0.92)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, bandTop, canvas.width, canvas.height - bandTop);
  }

  const byCat = {};
  p.markers.forEach(function (m) {
    if (p.clusterMembers && p.clusterMembers.has(m)) return;
    if (markerHidden(m, p.catIndex, p.hidden)) return;
    const cat = p.catIndex[m[2]] || 'other';
    (byCat[cat] || (byCat[cat] = [])).push(m);
  });

  ctx.lineWidth = Math.max(0.6, 0.5 * p.ss);
  ctx.strokeStyle = 'rgba(20,12,4,0.55)';
  DRAW_ORDER.forEach(function (cat) {
    const arr = byCat[cat];
    if (!arr) return;
    ctx.fillStyle = p.catColors[cat] || '#888';
    const r = Math.max(1.4 * p.ss, (RADIUS[cat] || 1.5) * p.ss);
    for (let i = 0; i < arr.length; i++) {
      const m = arr[i];
      const typeIdx = m[3];
      const iconName = (typeof typeIdx === 'number') ? p.typeIcons[typeIdx] : null;
      const img = iconName ? p.icons.get(iconName) : null;
      if (img) {
        const iconR = r * 1.4;
        ctx.drawImage(img, m[0] * p.scale - iconR, m[1] * p.scale - iconR,
                      iconR * 2, iconR * 2);
      } else {
        ctx.beginPath();
        ctx.arc(m[0] * p.scale, m[1] * p.scale, r, 0, 6.2832);
        ctx.fill();
        ctx.stroke();
        if (iconName) p.icons.load(iconName);
      }
    }
  });

  if (p.clusters && p.clusters.length) drawClusterHotspots(ctx, p);
}

// ---- mega-cluster ore aggregation (M1 stretch) -----------------------------
// Below CLUSTER_ZOOM_MAX, dense same-type ore buckets collapse into one
// hotspot marker (ring + count badge + type icon); individual dots return
// above the threshold. Clusters are recomputed only when markers or hidden
// state change (they are zoom-independent; only their visibility is not).

export const CLUSTER_ZOOM_MAX = 1.5;   // hotspots render below this zoom
export const CLUSTER_RING_PX = 11;     // hotspot ring radius, CSS px at zoom 1
const CLUSTER_BUCKETS = 18;            // bucket edge = view/18 (~half a DD sector)
const CLUSTER_MIN = 8;                 // same-type nodes per bucket to collapse

// Grid-bucket density clustering over visible ore markers. Returns
// { clusters: [{x, y, catIdx, typeIdx, count}], members: Set<marker> };
// members are the markers a cluster replaces (skipped by drawCanvas).
export function computeClusters(markers, catIndex, hidden, viewUnits) {
  const step = viewUnits / CLUSTER_BUCKETS;
  const buckets = {};
  for (let i = 0; i < markers.length; i++) {
    const m = markers[i];
    if (typeof m[3] !== 'number') continue;
    if ((catIndex[m[2]] || 'other') !== 'ore') continue;
    if (markerHidden(m, catIndex, hidden)) continue;
    const key = Math.floor(m[0] / step) + ':' + Math.floor(m[1] / step) + ':' + m[3];
    (buckets[key] || (buckets[key] = [])).push(m);
  }
  const clusters = [];
  const members = new Set();
  Object.keys(buckets).forEach(function (key) {
    const arr = buckets[key];
    if (arr.length < CLUSTER_MIN) return;
    let sx = 0, sy = 0;
    arr.forEach(function (m) { sx += m[0]; sy += m[1]; members.add(m); });
    clusters.push({
      x: sx / arr.length, y: sy / arr.length,
      catIdx: arr[0][2], typeIdx: arr[0][3], count: arr.length,
    });
  });
  return { clusters: clusters, members: members };
}

// Hotspot pass, drawn after the ordered category pass so rings sit on top.
function drawClusterHotspots(ctx, p) {
  const color = p.catColors.ore || '#888';
  p.clusters.forEach(function (c) {
    const x = c.x * p.scale, y = c.y * p.scale;
    const r = CLUSTER_RING_PX * p.ss;
    ctx.globalAlpha = 0.22;
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.lineWidth = 1.6 * p.ss;
    ctx.strokeStyle = color;
    ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.stroke();
    const iconName = p.typeIcons[c.typeIdx];
    const img = iconName ? p.icons.get(iconName) : null;
    if (img) {
      const ir = r * 0.62;
      ctx.drawImage(img, x - ir, y - ir, ir * 2, ir * 2);
    } else {
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(x, y, r * 0.35, 0, 6.2832); ctx.fill();
      if (iconName) p.icons.load(iconName);
    }
    // Count badge, top-right of the ring. Dark disc matches the canvas's
    // existing band-gradient tones.
    const label = String(c.count);
    const fs = 7.5 * p.ss;
    ctx.font = '600 ' + fs + 'px "JetBrains Mono", monospace';
    const br = Math.max(fs * 0.72, ctx.measureText(label).width * 0.62 + fs * 0.25);
    const bx = x + r * 0.8, by = y - r * 0.8;
    ctx.fillStyle = 'rgba(38,26,15,0.92)';
    ctx.beginPath(); ctx.arc(bx, by, br, 0, 6.2832); ctx.fill();
    ctx.lineWidth = 0.8 * p.ss;
    ctx.beginPath(); ctx.arc(bx, by, br, 0, 6.2832); ctx.stroke();
    ctx.fillStyle = color;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, bx, by);
    ctx.textAlign = 'start';
    ctx.textBaseline = 'alphabetic';
  });
}

// Nearest cluster whose ring contains (nx, ny), or null. The ring is a fixed
// CSS-px size scaled by zoom on screen, so its normalized radius is
// CLUSTER_RING_PX / baseScale, zoom-independent.
export function clusterAt(clusters, nx, ny, ringNorm) {
  let best = null, bestD = ringNorm * ringNorm;
  for (let i = 0; i < clusters.length; i++) {
    const c = clusters[i];
    const dx = c.x - nx, dy = c.y - ny, d = dx * dx + dy * dy;
    if (d <= bestD) { bestD = d; best = c; }
  }
  return best;
}

// Nearest visible marker within maxDist (normalized units), or null.
// exclude (optional Set): markers absorbed into a cluster hotspot; skipped
// in-loop so a hidden member cannot shadow a visible marker farther away.
export function nearestMarker(markers, catIndex, hidden, nx, ny, maxDist, exclude) {
  let best = null, bestD = maxDist * maxDist;
  for (let i = 0; i < markers.length; i++) {
    const m = markers[i];
    if (exclude && exclude.has(m)) continue;
    if (markerHidden(m, catIndex, hidden)) continue;
    const dx = m[0] - nx, dy = m[1] - ny, d = dx * dx + dy * dy;
    if (d < bestD) { bestD = d; best = m; }
  }
  return best;
}
