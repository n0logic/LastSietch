// Map engine facade. Owns canvas/SVG internals inside the host elements the
// Svelte chrome hands it; everything textual crosses the boundary as plain
// data through opts.on callbacks (no innerHTML, no DOM outside hostEls).
// Chrome -> engine: the returned API. Engine -> chrome: callbacks. Presence
// of localStorage presets, ls-dim and all copy is chrome's business.

import {
  createView, baseScale, setZoom, panToNormalized, screenToNormalized,
  applyStageTransform, normalizedToWorld, attachGestures,
} from './transform.js';
import {
  supersample, createIconCache, drawCanvas, nearestMarker,
  computeClusters, clusterAt, CLUSTER_ZOOM_MAX, CLUSTER_RING_PX,
} from './markers.js';
import { drawGrid, sectorCenter } from './grid.js';
import { drawSpice, drawWorms, drawStorm } from './overlays.js';
import { drawMe, wormProximity } from './me.js';
import { drawOtherPlayers, countOtherPlayers } from './players.js';
import { drawWaypoints } from './waypoints.js';
import { createWormAudio } from './audio.js';
import { createReckoner, prefersReducedMotion } from './reckon.js';
import {
  createPollers, deriveConsole, ME_INTERVAL_MS, ME_STALE_MS,
} from './pollers.js';

const CLICK_HIT_PX = 18;   // V1 popup hit radius (screen px)
const HOVER_HIT_PX = 14;   // V1 tooltip hit radius

// hostEls: { viewport, stage, backdrop, canvas, svg }
// opts: { key, quality?, projector?, on: { onStatus, onLegend, onCounts,
//         onConsole, onHover, onSelect, onViewChange, onScene, onMe,
//         onProximity } }
// opts.projector (M3): a 3D viewer supplies { project(nx,ny), unproject(mx,my) }
// so hit-testing stays engine-side but converts screen<->normalized through the
// holo camera instead of the affine 2.5D transform. Absent = today's behavior.
// onScene (M3): a plain spatial snapshot emitted on every spatial change; the
// carved path draws overlays into the SVG directly, a holo viewer renders from
// this snapshot. No new derivation; reuses values the engine already holds.
// onSelect payloads are kind-discriminated: 'marker' (M1 shape) | 'waypoint'
// (marker-compatible fields + waypointId/note/nx/ny).
// onMe: raw /me relay {authenticated, available, self, bases, vehicles}
// plus asOfMs (receipt stamp; chrome degrades freshness past ~2.5x 10s).
// onProximity: wormProximity model or null (chrome words the WarnStrip copy).
export function createMapEngine(hostEls, opts) {
  const viewport = hostEls.viewport, stage = hostEls.stage,
        backdrop = hostEls.backdrop, canvas = hostEls.canvas, svg = hostEls.svg;
  const ctx = canvas.getContext('2d');
  const on = (opts && opts.on) || {};
  const ss = supersample();

  let meta = { key: opts.key, view: 1000 };
  const state = {
    ready: false,
    destroyed: false,
    legend: [], markers: [], typeIndex: [], typeIcons: [],
    catIndex: [], catColors: {},
    // Hidden map: numeric type idx OR string overlay key
    // ('spice_large' | 'spice_medium' | 'sandstorm' | 'worms') -> true.
    hidden: {},
    instance: null,
    spice: null, worms: null, sandstorm: null,
    // Transport receipt stamps: a dead poller fails silently, so the console
    // model carries asOfMs and chrome degrades Ibad to dim amber on staleness.
    spiceAtMs: null, wormsAtMs: null, playersAtMs: null,
    players: null, total: 0,
    spiceHover: [], wormHover: [], wormEls: {},
    // M2 personal layer. meAtMs is the /me receipt stamp (same staleness rule
    // as spice/worms); meEls keeps the self pin node alive across redraws so
    // a mid-glide position survives. Waypoints are chrome-fed (CRUD is
    // chrome's) via applyFix('waypoints', list).
    me: null, meAuthed: false, meAtMs: null, meHover: [], meEls: {},
    // /me feed freshness: pushed in by chrome's meFresh clock (setMeFresh)
    // so board pin and chip degrade in lockstep; a new fix self-heals to true.
    meFresh: true,
    meShow: { self: true, bases: true, vehicles: true },
    // Live OTHER players + world vehicles (Hagga only; public positions feed).
    // Filtered to the selected sietch by partition at draw time. otherCount is
    // the count for the SELECTED sietch (for the chrome readout).
    otherPlayers: [], otherVehicles: [], othersAtMs: null, otherCount: 0,
    showOthers: { players: true, vehicles: true },
    waypoints: [], wpHover: [],
    // Ore hotspots: zoom-independent, recomputed on data/hidden change;
    // shown only below CLUSTER_ZOOM_MAX (clustersShown tracks the crossing).
    clusters: [], clusterMembers: null, clustersShown: false,
    interacting: false,
  };
  const view = createView(1000);

  function cb(name, payload) {
    if (on[name]) on[name](payload);
  }

  // Contract addendum: 'ready' carries {name, instances, has_spice, has_storms,
  // view, key}
  // so the chrome can render the title + InstrumentTabs without a second fetch;
  // 'error' may carry a short message; 'loading' carries nothing.
  function emitStatus(status, detail) {
    if (on.onStatus) on.onStatus(status, detail);
  }

  const reduceMotion = (opts.quality && opts.quality.reducedMotion != null)
    ? !!opts.quality.reducedMotion : prefersReducedMotion();
  const reckoner = createReckoner({ reducedMotion: reduceMotion });
  const icons = createIconCache(redrawMarkers);
  const pollers = createPollers(opts.key, applyFix, currentDim);
  const wormAudio = createWormAudio();

  // ---- draw ---------------------------------------------------------------
  function redrawMarkers() {
    if (!state.ready || state.destroyed) return;
    const clustered = state.clustersShown && state.clusters.length > 0;
    drawCanvas(ctx, canvas, {
      markers: state.markers, catIndex: state.catIndex,
      catColors: state.catColors, typeIcons: state.typeIcons,
      hidden: state.hidden, hasGrid: !!meta.grid,
      scale: baseScale(view) * ss, ss: ss, icons: icons,
      clusters: clustered ? state.clusters : null,
      clusterMembers: clustered ? state.clusterMembers : null,
    });
  }

  function refreshClusters() {
    const r = computeClusters(state.markers, state.catIndex, state.hidden, view.view);
    state.clusters = r.clusters;
    state.clusterMembers = r.members;
  }

  function currentDim() {
    return state.instance ? state.instance.dim : null;
  }

  // The selected instance's partition (Hagga 1=Habbanya / 32=Kulon / 33=Amtal),
  // else null (DD/hubs discriminate by dim). The authoritative sietch split for
  // the me layer and the live other-players layer.
  function currentPart() {
    return state.instance && state.instance.part != null
      ? state.instance.part : null;
  }

  function redrawSpice() {
    state.spiceHover = drawSpice(svg, {
      hasSpice: !!meta.has_spice, grid: meta.grid, viewUnits: view.view,
      hidden: state.hidden, candidates: meta.spice_candidates,
      candidateCoords: meta.spice_candidate_coords, mediums: meta.spice_mediums,
      spice: state.spice, dim: currentDim(),
    });
  }

  function redrawWorms() {
    state.wormHover = drawWorms(svg, {
      hasSpice: !!meta.has_spice, show: !state.hidden['worms'],
      grid: meta.grid, viewUnits: view.view,
      worms: wormsForInstance(), els: state.wormEls, reckoner: reckoner,
      reduce: reduceMotion,
    });
  }

  function redrawStorm() {
    drawStorm(svg, {
      hasStorms: !!meta.has_storms, hidden: state.hidden,
      sandstorm: state.sandstorm, dim: currentDim(),
      grid: meta.grid, viewUnits: view.view, reckoner: reckoner,
    });
  }

  function wormsForInstance() {
    return wormsForDim(currentDim());
  }

  function wormsForDim(dim) {
    if (!state.worms || !state.worms.dimensions) return [];
    const info = state.worms.dimensions[String(dim)];
    return (info && info.worms) || [];
  }

  function redrawMe() {
    state.meHover = drawMe(svg, {
      authed: state.meAuthed, me: state.me, show: state.meShow,
      dim: currentDim(), part: currentPart(), viewUnits: view.view,
      els: state.meEls, reckoner: reckoner, intervalMs: ME_INTERVAL_MS,
      fresh: state.meFresh,
    });
  }

  function redrawWaypoints() {
    state.wpHover = drawWaypoints(svg, {
      list: state.waypoints, viewUnits: view.view,
    });
  }

  // Live other-player layer (carved SVG path). Only maps that expose the feed
  // (Hagga) ever carry rows; the draw is filtered to the selected sietch by
  // partition and returns the drawn count for the readout. Recomputes the
  // selected-sietch count even when a sub-layer is toggled off.
  function redrawOthers() {
    const part = currentPart();
    drawOtherPlayers(svg, {
      players: state.otherPlayers, vehicles: state.otherVehicles,
      cal: meta.cal, viewUnits: view.view, part: part, mapKey: meta.key,
      showPlayers: state.showOthers.players,
      showVehicles: state.showOthers.vehicles,
    });
    state.otherCount = countOtherPlayers(state.otherPlayers, part, meta.key);
  }

  // Danger = any enraged/breaching worm in the VIEWED dim (V1 banner rule).
  // The audio module handles the transition edge + gesture gating itself.
  function updateWormAudio() {
    const danger = wormsForInstance().some(function (w) {
      return w.threat === 'enraged' || w.threat === 'breaching';
    });
    wormAudio.notifyDanger(danger);
  }

  // Worm proximity readout relative to the player's own position, scoped to
  // the SELF's dim (V1 semantics: the warning tracks the player, not the tab).
  function emitProximity() {
    const self = (state.meAuthed && state.me && state.me.self) || null;
    const step = view.view / (meta.grid ? meta.grid.cols : 9);
    cb('onProximity',
       self ? wormProximity(self, wormsForDim(self.dim), step) : null);
  }

  // Raw /me shape relayed (chrome derives MeChips counts and RescueButton
  // gates from self/bases itself, incl. bases-by-dim), plus the receipt stamp
  // for freshness degradation. Contract agreed with dev-2 (task #13).
  function emitMe() {
    const me = state.me;
    cb('onMe', {
      authenticated: state.meAuthed,
      available: !!(me && me.available),
      self: (me && me.self) || null,
      bases: (me && me.bases) || [],
      vehicles: (me && me.vehicles) || [],
      asOfMs: state.meAtMs,
    });
  }

  // The chrome sizes the viewport (mobile-first layout); the engine sizes the
  // square stage/canvas inside it. V1 consulted the desktop sidebar here; that
  // concern moved out with the chrome.
  function layout() {
    if (!state.ready || state.destroyed) return;
    const w = viewport.clientWidth || 0;
    const h = viewport.clientHeight || w;
    const size = Math.min(w, h || w);
    view.display = size;
    stage.style.width = size + 'px';
    stage.style.height = size + 'px';
    canvas.style.width = size + 'px';
    canvas.style.height = size + 'px';
    canvas.width = Math.round(size * ss);
    canvas.height = Math.round(size * ss);
    drawGrid(svg, meta.grid, view.view);
    redrawMarkers();
    pushTransform();
  }

  function pushTransform() {
    applyStageTransform(view, stage);
    // Cluster visibility is the one zoom-dependent canvas state: redraw only
    // when the threshold is crossed, not on every pan/zoom frame.
    const show = view.zoom < CLUSTER_ZOOM_MAX;
    if (show !== state.clustersShown) {
      state.clustersShown = show;
      redrawMarkers();
    }
    cb('onViewChange', { zoom: view.zoom, panX: view.panX, panY: view.panY,
                         interacting: state.interacting });
  }

  function setupBackdrop() {
    const b = meta.backdrop || { type: 'sand' };
    if (b.type === 'image' && b.src) {
      backdrop.style.backgroundImage = "url('" + b.src + "')";
      backdrop.style.backgroundSize = '100% 100%';
      backdrop.classList.add('map-backdrop--image');
    } else {
      backdrop.classList.add('map-backdrop--sand');
    }
  }

  // ---- data-out models ----------------------------------------------------
  function legendModel() {
    const mediums = meta.spice_mediums || [];
    const medActive = mediums.filter(function (m) { return m && m[3] === true; }).length;
    // Medium-count enrichment (V1 setMediumLegendCount, as data).
    const medTitle = medActive
      ? medActive.toLocaleString() + ' of ' + mediums.length.toLocaleString() +
        ' medium fields erupted now'
      : mediums.length.toLocaleString() + ' medium spice fields';
    return (state.legend || []).map(function (cat) {
      const types = (cat.types || []).map(function (t) {
        const row = { idx: t.idx, label: t.label, icon: t.icon || null,
                      count: t.count || 0 };
        if (t.idx === 'spice_medium') {
          row.count = mediums.length;
          row.title = medTitle;
        }
        return row;
      });
      return {
        key: cat.key, label: cat.label, color: cat.color,
        count: cat.key === 'spice' ? mediums.length : (cat.count || 0),
        types: types,
      };
    });
  }

  function emitCounts() {
    const pl = state.players;
    cb('onCounts', {
      total: state.total,
      playersAvailable: !!(pl && pl.available),
      mapPlayers: (pl && pl.map_players) || 0,
      serverPlayers: (pl && pl.server_players) || 0,
      // Receipt stamp (null before first /players): chrome degrades the
      // counter honestly past ~2.5x the 45s cadence, same rule as asOfMs.
      playersAsOfMs: state.playersAtMs,
      // Live others in the SELECTED sietch (Hagga only; null feed = unavailable).
      othersAvailable: state.othersAtMs != null,
      otherPlayers: state.otherCount,
      othersAsOfMs: state.othersAtMs,
    });
  }

  function emitConsole() {
    cb('onConsole', deriveConsole({
      meta: meta, spice: state.spice, sandstorm: state.sandstorm,
      worms: state.worms, instance: state.instance,
      spiceAtMs: state.spiceAtMs, wormsAtMs: state.wormsAtMs,
    }));
  }

  // Candidate Large sites resolved to coords, mirroring the carved drawSpice
  // fallback: exact projected coord when the reader supplied one, else the
  // sector center so a coord-less candidate never blanks. Keeping this in the
  // engine gives both viewers the identical candidate set (sector -> [x, y]).
  function resolvedCandidates() {
    const coords = meta.spice_candidate_coords || {};
    const out = {};
    (meta.spice_candidates || []).forEach(function (sec) {
      const xy = coords[sec];
      if (xy && xy.length === 2) { out[sec] = xy; return; }
      const c = sectorCenter(meta.grid, view.view, sec);
      if (c) out[sec] = [c.x, c.y];
    });
    return out;
  }

  // M3 spatial snapshot for a 3D viewer. Plain models the engine already holds
  // (no re-derivation, no re-fetch); the holo viewer renders from this. Fired on
  // every spatial change so both viewers stay fed by the one ingest path.
  function emitScene() {
    if (!on.onScene || !state.ready) return;
    on.onScene({
      ready: state.ready, view: view.view,
      grid: meta.grid, cal: meta.cal, backdrop: meta.backdrop,
      layout: meta.layout,
      dim: currentDim(), part: currentPart(),
      hidden: state.hidden,
      markers: state.markers, catIndex: state.catIndex, catColors: state.catColors,
      typeIcons: state.typeIcons, typeIndex: state.typeIndex,
      legend: state.legend,
      spiceActive: state.spice,
      candidates: resolvedCandidates(),
      mediums: meta.spice_mediums,
      worms: wormsForInstance(),
      sandstorm: state.sandstorm,
      me: state.me, meAuthed: state.meAuthed, meFresh: state.meFresh, meShow: state.meShow,
      otherPlayers: state.otherPlayers, otherVehicles: state.otherVehicles,
      showOthers: state.showOthers,
      waypoints: state.waypoints,
    });
  }

  // ---- ingestion (THE single entry point; SSE swaps in here later) ---------
  function applyFix(kind, payload) {
    if (state.destroyed || !payload) return;
    if (kind === 'data') { ingestData(payload); return; }
    if (!state.ready) return;
    if (kind === 'live') {
      state.spice = payload.spice || { dimensions: {} };
      state.worms = payload.worms || { dimensions: {} };
      state.sandstorm = payload.sandstorm || { dimensions: {} };
      state.spiceAtMs = state.wormsAtMs = Date.now();
      redrawSpice();
      redrawWorms();
      redrawStorm();
      emitConsole();
      updateWormAudio();
      emitProximity();
      emitScene();
    } else if (kind === 'spice') {          // M-LIVE split feeds
      state.spice = payload;
      state.spiceAtMs = Date.now();
      redrawSpice();
      emitConsole();
      emitScene();
    } else if (kind === 'worms') {
      state.worms = payload;
      state.wormsAtMs = Date.now();
      redrawWorms();
      emitConsole();
      updateWormAudio();
      emitProximity();
      emitScene();
    } else if (kind === 'sandstorm') {
      state.sandstorm = payload;
      redrawStorm();
      emitConsole();
      emitScene();
    } else if (kind === 'players') {
      state.players = payload;
      state.playersAtMs = Date.now();
      emitCounts();
    } else if (kind === 'positions') {
      state.otherPlayers = (payload && payload.players) || [];
      state.othersAtMs = Date.now();
      redrawOthers();
      emitCounts();
      emitScene();
    } else if (kind === 'worldVehicles') {
      state.otherVehicles = (payload && payload.vehicles) || [];
      redrawOthers();
      emitScene();
    } else if (kind === 'me') {
      // /portal/maps/{key}/me response. authenticated:false (401 mid-run or
      // signed out) clears the layer honestly instead of freezing it.
      state.meAuthed = !!payload.authenticated;
      state.me = !state.meAuthed ? null
        : (payload.available ? payload
           : { available: false, self: null, bases: [], vehicles: [] });
      state.meAtMs = state.meAuthed ? Date.now() : null;
      state.meFresh = true;   // a fix just arrived: fresh by definition
      redrawMe();
      emitMe();
      emitProximity();
      emitScene();
    } else if (kind === 'me-quiet') {
      // /me poll failed (non-401): no new receipt arrived, so re-evaluate
      // freshness engine-side — a dead feed produces no fixes and would
      // otherwise never trigger a redraw (reviewer #21 root cause). Converges
      // on the same is-stale rendering as chrome-pushed setMeFresh; either
      // signal may fire first, both are idempotent.
      if (state.meFresh && state.meAtMs &&
          Date.now() - state.meAtMs >= ME_STALE_MS) {
        state.meFresh = false;
        redrawMe();
        emitScene();   // freshness flip: holo self-pip stops breathing in step
      }
    } else if (kind === 'waypoints') {
      // Chrome-owned CRUD feeds the full current list (optimistic updates
      // included); the engine only renders and hit-tests.
      state.waypoints = Array.isArray(payload) ? payload : [];
      redrawWaypoints();
      emitScene();
    }
  }

  function ingestData(d) {
    meta = {
      key: d.map, name: d.name || '', view: d.view || 1000, cal: d.cal, grid: d.grid,
      backdrop: d.backdrop, instances: d.instances || [],
      layout: d.layout || null,
      has_spice: !!d.has_spice,
      // Separate axis from spice: Hagga storms, Hagga has no spice.
      has_storms: !!d.has_storms,
      spice_candidates: d.spice_candidates || [],
      // Part A/B static-per-cycle spice data (from /data, not /live). Empty
      // until the reader feeds them; degrades to sector-center plotting.
      spice_mediums: d.spice_mediums || [],
      spice_candidate_coords: d.spice_candidate_coords || {},
      coriolis: d.coriolis || null,
    };
    view.view = meta.view;
    state.instance = meta.instances[0] || null;
    state.legend = d.legend || [];
    state.markers = d.markers || [];
    state.typeIndex = d.type_index || [];
    state.typeIcons = d.type_icons || [];
    state.catIndex = d.cat_index || [];
    state.catColors = d.cat_colors || {};
    state.total = d.total || 0;
    state.ready = true;
    refreshClusters();
    state.clustersShown = view.zoom < CLUSTER_ZOOM_MAX;
    svg.setAttribute('viewBox', '0 0 ' + view.view + ' ' + view.view);
    setupBackdrop();
    layout();
    // Static candidates/mediums render before the first /live arrives.
    redrawSpice();
    redrawStorm();
    cb('onLegend', legendModel());
    emitCounts();
    emitConsole();
    emitScene();
    emitStatus('ready', {
      name: meta.name, instances: meta.instances,
      has_spice: meta.has_spice, has_storms: meta.has_storms,
      view: meta.view, key: meta.key,
    });
    // /live carries spice + worms + sandstorm: poll when the map has EITHER.
    pollers.start(!!meta.has_spice || !!meta.has_storms);
  }

  // ---- hit tests ------------------------------------------------------------
  function hitCluster(nx, ny) {
    if (!state.clustersShown || !state.clusters.length) return null;
    // Ring is fixed CSS px scaled by zoom on screen -> zoom-independent
    // normalized radius.
    return clusterAt(state.clusters, nx, ny, CLUSTER_RING_PX / baseScale(view));
  }

  function clusterLabel(c) {
    const typeName = state.typeIndex[c.typeIdx] || 'Ore';
    return typeName + ' cluster (' + c.count + ')';
  }

  // While clustered, member dots are not drawn, so they must not be
  // hit-testable either; excluded in-loop so a hidden member cannot shadow
  // a visible marker slightly farther away.
  function hitExclude() {
    return state.clustersShown ? state.clusterMembers : null;
  }

  // M3 coordinate seam: with a projector (holo) the screen<->normalized mapping
  // is the 3D camera's ground-plane raycast; without one it is the affine 2.5D
  // transform (carved, unchanged). unproject may return null off the board.
  function toNorm(mx, my) {
    return opts.projector ? opts.projector.unproject(mx, my)
                          : screenToNormalized(view, mx, my);
  }
  // Popup anchor: project the selected point's world coords back to the screen
  // (holo), else fall back to the pointer position (carved, unchanged).
  function toScreen(nx, ny, fallbackX, fallbackY) {
    if (opts.projector) {
      const p = opts.projector.project(nx, ny);
      if (p) return { x: p.screenX, y: p.screenY };
    }
    return { x: fallbackX, y: fallbackY };
  }

  function hoverAt(mx, my) {
    if (!state.ready) return;
    const n = toNorm(mx, my);
    if (!n) { cb('onHover', null); return; }   // off-board (projector only)
    // Priority: me > waypoints > worms > spice > clusters > markers (V1 order).
    const regions = state.meHover.concat(state.wpHover, state.wormHover,
                                         state.spiceHover);
    for (let i = 0; i < regions.length; i++) {
      const h = regions[i];
      const dx = h.x - n.nx, dy = h.y - n.ny;
      if (dx * dx + dy * dy <= h.r * h.r) {
        cb('onHover', { label: h.label, screenX: mx, screenY: my });
        return;
      }
    }
    const cl = hitCluster(n.nx, n.ny);
    if (cl) {
      cb('onHover', { label: clusterLabel(cl), screenX: mx, screenY: my });
      return;
    }
    const maxDist = HOVER_HIT_PX / view.zoom / baseScale(view);
    const best = nearestMarker(state.markers, state.catIndex, state.hidden,
                               n.nx, n.ny, maxDist, hitExclude());
    if (best) {
      cb('onHover', { label: best[4] || state.typeIndex[best[3]] || '',
                      screenX: mx, screenY: my });
    } else {
      cb('onHover', null);
    }
  }

  function clickAt(mx, my) {
    if (!state.ready) return;
    const n = toNorm(mx, my);
    if (!n) { cb('onSelect', null); return; }   // off-board (projector only)
    // Waypoint pins select first (pin detail popup; chrome renders actions).
    for (let i = 0; i < state.wpHover.length; i++) {
      const wp = state.wpHover[i];
      const wdx = wp.x - n.nx, wdy = wp.y - n.ny;
      if (wdx * wdx + wdy * wdy <= wp.r * wp.r) {
        cb('onSelect', waypointDetail(wp, mx, my));
        return;
      }
    }
    // Hotspot click: zoom one step centered on the cluster (its dots take
    // over past the threshold). No popup for aggregates.
    const cl = hitCluster(n.nx, n.ny);
    if (cl) {
      setZoom(view, view.zoom * 1.5);
      panToNormalized(view, cl.x, cl.y);
      pushTransform();
      return;
    }
    const maxDist = CLICK_HIT_PX / view.zoom / baseScale(view);
    const best = nearestMarker(state.markers, state.catIndex, state.hidden,
                               n.nx, n.ny, maxDist, hitExclude());
    cb('onSelect', best ? selectDetail(best, mx, my) : null);
  }

  function selectDetail(m, mx, my) {
    const cat = state.catIndex[m[2]] || 'other';
    const typeName = state.typeIndex[m[3]] || '';
    let catLabel = '';
    (state.legend || []).forEach(function (leg) {
      if (leg.key === cat) catLabel = leg.label;
    });
    const world = normalizedToWorld(m[0], m[1], meta.cal, view.view);
    const coordStr = world
      ? world.x + ', ' + world.y
      : m[0].toFixed(0) + ', ' + m[1].toFixed(0);
    const anchor = toScreen(m[0], m[1], mx, my);
    return {
      kind: 'marker',
      // Named POIs carry a 5th element; fall back to the type label.
      name: m[4] || typeName,
      catLabel: catLabel,
      // Secondary type label only for named POIs (V1 popup semantics).
      typeLabel: (m[4] && typeName) ? typeName : '',
      color: state.catColors[cat] || '',
      coordStr: coordStr,
      screenX: anchor.x, screenY: anchor.y,
    };
  }

  // Waypoint pin detail: marker-compatible shape (MapPopup renders it as-is,
  // per the chrome contract with dev-2) + kind/waypointId/nx/ny extras so the
  // popup can offer delete/pan.
  function waypointDetail(wp, mx, my) {
    const world = normalizedToWorld(wp.nx, wp.ny, meta.cal, view.view);
    const anchor = toScreen(wp.nx, wp.ny, mx, my);
    return {
      kind: 'waypoint',
      name: wp.note || 'Waypoint',
      catLabel: 'Waypoint',
      typeLabel: '',
      color: '',
      coordStr: world
        ? world.x + ', ' + world.y
        : Math.round(wp.nx) + ', ' + Math.round(wp.ny),
      screenX: anchor.x, screenY: anchor.y,
      waypointId: wp.id,
      note: wp.note || '',
      nx: wp.nx, ny: wp.ny,
    };
  }

  // ---- wiring ---------------------------------------------------------------
  const detachGestures = attachGestures(viewport, view, {
    onTransform: pushTransform,
    onInteract: function (active) {
      state.interacting = active;
      cb('onViewChange', { zoom: view.zoom, panX: view.panX, panY: view.panY,
                           interacting: active });
    },
    onHover: hoverAt,
    onLeave: function () { cb('onHover', null); },
    onClick: clickAt,
  });

  function onWindowResize() { layout(); }
  window.addEventListener('resize', onWindowResize);

  // ---- public API -----------------------------------------------------------
  return {
    start: function () {
      emitStatus('loading');
      pollers.loadData().catch(function (err) {
        if (!state.destroyed) emitStatus('error', (err && err.message) || 'Map data unavailable.');
      });
    },
    applyFix: applyFix,
    setInstance: function (instanceKey) {
      const next = (meta.instances || []).filter(function (i) {
        return i.key === instanceKey;
      })[0];
      if (!next || next === state.instance) return;
      state.instance = next;
      reckoner.remove('storm');   // the other dim's track must not carry over
      redrawSpice();
      redrawWorms();
      redrawStorm();
      redrawMe();                 // me markers are dim/part-filtered
      redrawOthers();             // other players re-filter to the new sietch
      pollers.refreshPlayers();   // per-dim count: refetch for the new instance
      emitCounts();               // selected-sietch other-player count changes
      emitConsole();
      updateWormAudio();          // switching INTO a dim in danger pings
      emitScene();                // dim change: holo re-filters overlays
    },
    setHidden: function (hiddenMap) {
      state.hidden = hiddenMap || {};
      refreshClusters();   // clusters respect legend state
      redrawMarkers();
      redrawSpice();
      redrawWorms();
      redrawStorm();
      emitScene();
    },
    zoomIn: function () {
      setZoom(view, view.zoom * 1.5, view.display / 2, view.display / 2);
      pushTransform();
    },
    zoomOut: function () {
      setZoom(view, view.zoom / 1.5, view.display / 2, view.display / 2);
      pushTransform();
    },
    resetView: function () {
      view.zoom = 1; view.panX = 0; view.panY = 0;
      pushTransform();
    },
    panTo: function (nx, ny) {
      panToNormalized(view, nx, ny);
      pushTransform();
    },
    // ---- M2 personal layer ------------------------------------------------
    // Chrome signals auth: true starts the 10s /me poller, false stops it and
    // clears the layer. Chrome calls this after /portal/me resolves (the
    // poller must never run anonymously).
    setMeActive: function (active) {
      if (state.destroyed) return;   // same late-callback hazard as setStreamActive
      if (active) {
        pollers.startMe();
      } else {
        pollers.stopMe();
        applyFix('me', { authenticated: false });
      }
    },
    // /me feed freshness, pushed from chrome's meFresh clock (asOfMs vs the
    // shared 25s threshold) so board self-pip and chip degrade in lockstep:
    // stale = amber, breathe killed, last-known position held. The engine
    // deliberately runs no clock of its own; a new fix self-heals to fresh.
    setMeFresh: function (fresh) {
      if (state.destroyed) return;
      if (state.meFresh === !!fresh) return;
      state.meFresh = !!fresh;
      if (state.ready) { redrawMe(); emitScene(); }
    },
    // MeChips toggles: { self, bases, vehicles } booleans.
    setMeShow: function (show) {
      state.meShow = {
        self: !show || show.self !== false,
        bases: !show || show.bases !== false,
        vehicles: !show || show.vehicles !== false,
      };
      if (state.ready) redrawMe();
    },
    // Live other-player layer toggles: { players, vehicles } booleans (Hagga).
    setShowOthers: function (show) {
      state.showOthers = {
        players: !show || show.players !== false,
        vehicles: !show || show.vehicles !== false,
      };
      if (state.ready) { redrawOthers(); emitScene(); }
    },
    // Worm audio cue (M2 parity gate). MUST be called from a user gesture so
    // the AudioContext unlocks; muted default.
    setAudioEnabled: function (on) {
      wormAudio.setEnabled(on);
    },
    // Screen (viewport-relative) -> normalized coords for waypoint creation.
    // Chrome owns the POST; returns null when the point is off the board.
    addWaypointAt: function (mx, my) {
      if (!state.ready) return null;
      const n = toNorm(mx, my);   // projector-aware, same as hoverAt/clickAt
      if (!n) return null;        // off-board (projector only)
      if (n.nx < 0 || n.ny < 0 || n.nx > view.view || n.ny > view.view) return null;
      return { nx: n.nx, ny: n.ny };
    },
    // M-LIVE transport swap: true pauses the public live/players pollers
    // while the SSE stream (stream.js) feeds applyFix; false resumes them.
    // The me poller is session-gated and unaffected.
    setStreamActive: function (active) {
      // Self-guard: a late SSE status callback must not restart poll timers
      // on a torn-down engine (chrome guards the trigger side; belt-and-braces).
      if (state.destroyed) return;
      if (active) pollers.pause();
      else pollers.resume();
    },
    resize: layout,
    destroy: function () {
      state.destroyed = true;
      pollers.stop();
      reckoner.destroy();
      detachGestures();
      window.removeEventListener('resize', onWindowResize);
      icons.destroy();
      wormAudio.destroy();
    },
  };
}
