<script>
  // M1 board page: the map engine renders inside the five host elements below;
  // everything the player reads or touches around it (console, layers, tabs,
  // popup, tooltip, zoom cluster, 2.5D planning-table treatment) is chrome here.
  // Mobile-first: full-bleed viewport w/ pinned ticker + layers FAB -> MapSheet;
  // desktop: console above, board left, 300px carved sidebar right.
  import { onMount, untrack, tick } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { replaceState } from '$app/navigation';
  import { detectQuality } from '$lib/quality.js';
  import { api } from '$lib/api.js';
  import { auth } from '$lib/auth.svelte.js';
  import { prefs, charScope, getPref, setPref } from '$lib/prefs.svelte.js';
  import SpiceHudConsole from '$lib/components/map/SpiceHudConsole.svelte';
  import ViewerToggle from '$lib/components/map/ViewerToggle.svelte';
  import LayerTree from '$lib/components/map/LayerTree.svelte';
  import InstrumentTabs from '$lib/components/map/InstrumentTabs.svelte';
  import MapPopup from '$lib/components/map/MapPopup.svelte';
  import MapSheet from '$lib/components/map/MapSheet.svelte';
  import MeChips from '$lib/components/map/MeChips.svelte';
  import OthersChips from '$lib/components/map/OthersChips.svelte';
  import WaypointPanel from '$lib/components/map/WaypointPanel.svelte';
  import RescueButton from '$lib/components/map/RescueButton.svelte';
  import { createMapEngine } from '$lib/map/engine.js';
  import { liveStream } from '$lib/map/stream.js';
  import { CLUSTER_ZOOM_MAX } from '$lib/map/markers.js';
  import {
    createCarvedViewer, probeWebGL2, resolveViewerChoice, mountHoloViewer,
  } from '$lib/map/holo/stage-adapter.js';
  import { forcedLayout, forcedRelief } from '$lib/map/holo/debug.js';

  let key = $derived($page.params.key);

  // Engine host elements (contract: engine owns everything inside these five).
  let viewportEl = $state(null);
  let stageEl = $state(null);
  let backdropEl = $state(null);
  let canvasEl = $state(null);
  let svgEl = $state(null);

  // Engine -> chrome state (the seven contract callbacks land here).
  let status = $state('loading');
  let meta = $state(null);
  let legend = $state([]);
  let counts = $state(null);
  let consoleModel = $state(null);
  let hover = $state(null);
  let selectDetail = $state(null);
  let interacting = $state(false);
  let zoom = $state(1);
  let pan = $state({ x: 0, y: 0 });
  let now = $state(Date.now());

  // Chrome-owned state + persistence.
  let hidden = $state({});
  // Layer visibility persists per board under map_layers_{key}; a first visit
  // opens on the board's defaults. Deep Desert opens with the medium spice
  // fields OFF (owner ask 2026-09-02); everything else stays on. Classic V1
  // reads the same key, so a choice made in either companion carries over.
  // Seeded from the legend (data-driven), never from the map key.
  let layersInit = false;
  const layersKey = (k) => `map_layers_${k}`;
  function seedLayers(model) {
    const hasMedium = (model || []).some((cat) => (cat.types || []).some((t) => t.idx === 'spice_medium'));
    return hasMedium ? { spice_medium: true } : {};
  }
  let layerDefaults = $derived(seedLayers(legend));
  function readLayers(k) {
    try {
      const saved = JSON.parse(localStorage.getItem(layersKey(k)) || 'null');
      return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : null;
    } catch (e) { return null; }
  }
  function writeLayers(k, next) {
    try { localStorage.setItem(layersKey(k), JSON.stringify(next)); } catch (e) {}
  }
  // Wave 10b: a default saved on the ACCOUNT (the selected character's scope,
  // identity scope when no character is known) beats the board's opening
  // defaults; the browser key above stays the anonymous path and the fallback
  // while the prefs doc is still loading. Latches per board so a doc that
  // arrives late never overwrites picks the player has already made.
  const MAP_KEY_RE = /^[a-z0-9_-]{1,32}$/;
  let prefsApplied = false;
  // A pick made during the load window outranks the doc that arrives after it:
  // the player is looking at the board they just set.
  let userTouched = false;
  function prefHidden(k) {
    if (auth.status !== 'authed') return null;
    const saved = getPref('map_hidden', { scope: charScope() })?.[k];
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? { ...saved } : null;
  }
  function prefViewer(k) {
    if (auth.status !== 'authed') return null;
    const v = getPref('map_viewer', { scope: charScope() })?.[k];
    return v === 'holo' || v === 'carved' ? v : null;
  }
  let dim = $state(null);
  let sheetOpen = $state(false);
  let sidebarOpen = $state(true);
  let topbarH = $state(96);
  // Latches true on first board engagement (hover or an engine interaction) and
  // never resets: the 2.5D tilt plays once on load, eases flat when the player
  // first touches the board, and never leans back when the pointer leaves.
  let engaged = $state(false);
  // Reactive shallow ref (methods stay unproxied): the me/audio/stream effects
  // must re-fire when the engine instance itself appears or is torn down, not
  // only when `status` changes -- depending on status alone left a persisted
  // audio-on state unarmed after reload (QA #17, task #19).
  let engine = $state.raw(null);

  // ---- M3 War-Table holo viewer -----------------------------------------
  // The carved board always paints first (LCP stays on the zero-WebGL path);
  // a Three.js holo viewer lazy-swaps in on high-tier + WebGL2, or on an
  // explicit user force. The engine is the sole data owner either way; the
  // viewer only renders from onScene snapshots and supplies a hit-test
  // projector. The carved wrapper is a no-op StageViewer so the engine effect
  // holds one uniform `active` viewer for both paths.
  const carvedViewer = createCarvedViewer();
  const canForceHolo = probeWebGL2();      // toggle enables holo only if this passes
  let viewerPref = $state(readViewerPref()); // 'holo' | 'carved' | null (account, else device)
  let holoCanvasEl = $state(null);         // the WebGL <canvas> sibling (holo only)
  let holoViewer = $state.raw(null);       // the mounted holo StageViewer, else null
  let holoActive = $state(false);          // holo currently owns the visuals
  let holoMounting = $state(false);        // canvas rendered, mount attempt in flight
  let holoUnavailable = $state(false);     // session flag after context loss (no thrash)
  let viewerRev = $state(0);               // bump to recreate the engine with/without projector
  let idleHandle = null;
  let prevKey = null;

  let holoAvailable = $derived(canForceHolo && !holoUnavailable);
  let activeViewerKind = $derived(holoActive ? 'holo' : 'carved');

  // Debug-override chip: when ?holoLayout=N forces a baked terrain, say so on
  // screen so A/B runs across the 12 layouts stay legible. SSR-safe (both
  // helpers return null without window) and invisible on the normal path.
  const dbgLayout = forcedLayout();
  const dbgRelief = forcedRelief();

  function readViewerPref() {
    const onAccount = prefViewer(key);
    if (onAccount) return onAccount;
    if (typeof localStorage === 'undefined') return null;
    try {
      const v = localStorage.getItem('ls-map-viewer');
      return v === 'holo' || v === 'carved' ? v : null;
    } catch (e) { return null; }
  }

  function currentThemeMode() {
    if (typeof document === 'undefined') return 'night';
    return document.documentElement.getAttribute('data-theme') === 'day' ? 'day' : 'night';
  }

  function setViewerPref(pref) {
    if (pref !== 'holo' && pref !== 'carved') return;
    if (pref === 'holo' && !holoAvailable) return;   // WebGL2 absent: option is disabled
    userTouched = true;
    viewerPref = pref;
    try { localStorage.setItem('ls-map-viewer', pref); } catch (e) {}
    if (auth.status === 'authed' && MAP_KEY_RE.test(key)) {
      const scope = charScope();
      setPref('map_viewer', { ...getPref('map_viewer', { scope }), [key]: pref }, { scope });
    }
    // The reconcile effect below reacts to viewerPref and mounts/tears down.
  }

  function scheduleHoloMount() {
    if (holoMounting || holoViewer) return;
    holoMounting = true;   // renders the holo <canvas> so mountHolo can bind it
    const run = () => { idleHandle = null; mountHolo(); };
    idleHandle = (typeof requestIdleCallback === 'function')
      ? requestIdleCallback(run) : setTimeout(run, 0);
  }

  async function mountHolo() {
    if (holoViewer || holoUnavailable) { holoMounting = false; return; }
    await tick();   // let the {#if} render and bind holoCanvasEl
    if (!holoCanvasEl || !viewportEl) { holoMounting = false; return; }
    const viewer = await mountHoloViewer(
      { viewport: viewportEl, canvas: holoCanvasEl },
      { quality, onContextLost: onHoloContextLost }
    );
    // A carved override (or context loss) may have arrived mid-import: honor it.
    if (!viewer) { holoMounting = false; return; }
    if (holoUnavailable || viewerPref === 'carved') {
      viewer.destroy(); holoMounting = false; return;
    }
    holoViewer = viewer;
    holoActive = true;
    holoMounting = false;
    viewer.setTheme(currentThemeMode());
    // Recreate the engine so opts.projector is present at construction (the
    // engine is keyed on `key` + `viewerRev`; recreation just re-fetches /data).
    viewerRev += 1;
  }

  function teardownHolo() {
    if (idleHandle != null) {
      if (typeof cancelIdleCallback === 'function') cancelIdleCallback(idleHandle);
      else clearTimeout(idleHandle);
      idleHandle = null;
    }
    const v = holoViewer;
    holoViewer = null;
    holoActive = false;
    holoMounting = false;
    if (v) {
      v.destroy();
      viewerRev += 1;   // recreate the engine without a projector
      setTimeout(() => engine?.resize(), 0);   // the carved stage re-measures
    }
  }

  function onHoloContextLost() {
    if (holoUnavailable) return;
    holoUnavailable = true;   // do not thrash back to holo this session
    console.warn('[map] holo WebGL context lost; falling back to the carved board');
    teardownHolo();
  }

  // M2 personal layer (auth-gated: anon keeps the exact M1 chrome).
  let authed = $derived(auth.status === 'authed');
  let me = $state(null);          // engine onMe model: {available, online, counts, asOfMs}
  let meShow = $state({ self: true, bases: true, vehicles: true });
  let waypoints = $state([]);
  let wpError = $state('');
  let audioOn = $state(false);    // worm audio cue, muted default
  let dropPulse = $state(null);   // {x, y} transient long-press/alt-click feedback
  let dropPulseTimer;

  // Quality gate for the 2.5D treatment: high = tilt + glow, mid = tilt only,
  // low / reduced-motion / save-data / touch devices = flat board (V1 behavior).
  // Device capability lives here (not a CSS pointer query: headless/kiosk
  // browsers report pointer:none); CSS gates on width only.
  const quality = detectQuality();
  const tiltTier =
    quality.reducedMotion || quality.saveData || quality.coarse || quality.tier === 'low'
      ? 'flat'
      : quality.tier;

  let instances = $derived(meta?.instances ?? []);
  let instLabel = $derived(instances.find((i) => i.dim === dim)?.label ?? '');
  let boardName = $derived(meta?.name ?? '');

  function initDim(m) {
    const list = m?.instances ?? [];
    if (list.length < 2) { dim = list[0]?.dim ?? null; return; }
    const q = $page.url.searchParams.get('inst');
    let target = list.find((i) => i.key === q);
    if (!target) {
      let saved = 0;
      try { saved = Number(localStorage.getItem('ls-dim')) || 0; } catch (e) {}
      target = list.find((i) => i.dim === saved) ?? list[0];
    }
    dim = target.dim;
    if (target.key !== list[0].key) engine?.setInstance(target.key);
  }

  function setDim(d) {
    if (d === dim) return;
    dim = d;
    try { localStorage.setItem('ls-dim', String(d)); } catch (e) {}
    const inst = instances.find((i) => i.dim === d);
    if (inst) {
      engine?.setInstance(inst.key);
      // Keep ?inst= in sync so refresh/share lands on the same instance
      // (replaceState: no navigation, no history spam).
      try {
        const url = new URL(window.location.href);
        url.searchParams.set('inst', inst.key);
        replaceState(url, {});
      } catch (e) {}
    }
  }

  function onLayersChange(next) {
    userTouched = true;
    hidden = next;
    engine?.setHidden(next);
    writeLayers(key, next);
  }

  function toggleSidebar() {
    sidebarOpen = !sidebarOpen;
    try { localStorage.setItem('ls-map-sidebar', sidebarOpen ? 'open' : 'closed'); } catch (e) {}
    // Let the width transition settle before the engine re-measures.
    setTimeout(() => engine?.resize(), 300);
  }

  // Engine lifecycle: recreated whenever the board key (and therefore the host
  // elements inside the {#key} block) changes.
  $effect(() => {
    const k = key;
    const rev = viewerRev;   // recreate when the holo viewer mounts/tears down
    const els = { viewport: viewportEl, stage: stageEl, backdrop: backdropEl, canvas: canvasEl, svg: svgEl };
    if (!k || !els.viewport || !els.stage || !els.backdrop || !els.canvas || !els.svg) return;

    status = 'loading';
    meta = null; legend = []; counts = null; consoleModel = null;
    hover = null; selectDetail = null; hidden = {}; dim = null;
    layersInit = false;
    prefsApplied = false;
    userTouched = false;
    me = null; waypoints = []; wpError = '';

    // The active StageViewer for THIS engine incarnation: holo when swapped in,
    // else the carved no-op wrapper. Read untracked so only `key`/`viewerRev`
    // drive recreation. Its projector is wired into the engine at construction.
    const active = untrack(() => (holoActive && holoViewer ? holoViewer : carvedViewer));

    const eng = createMapEngine(els, {
      key: k,
      quality,
      projector: active.projector,
      on: {
        onStatus: (s, payload) => {
          status = s;
          // 'ready' detail is the flat {name, instances, has_spice, view, key}
          // object (contract v1 said {meta:{...}}; flat agreed with dev-1).
          const m = payload?.meta ?? payload;
          if (s === 'ready' && m) { meta = m; initDim(m); }
        },
        onLegend: (model) => {
          legend = model ?? [];
          // First legend of this incarnation: apply the account default, else
          // this browser's saved choice, else the board's defaults. Later
          // legend refreshes (medium counts) leave the player's toggles alone.
          if (!layersInit && legend.length && engine === eng) {
            layersInit = true;
            const next = prefHidden(k) ?? readLayers(k) ?? seedLayers(legend);
            hidden = next;
            eng.setHidden(next);
          }
        },
        onCounts: (c) => { counts = c; },
        onConsole: (m) => { consoleModel = m; },
        onHover: (h) => { hover = h; },
        // Waypoint pins arrive kind-discriminated; adapt them to the popup's
        // marker shape (amber swatch, note as the name).
        onSelect: (d) => {
          selectDetail = d?.kind === 'waypoint'
            ? { name: d.note || 'Waypoint', catLabel: 'Waypoint', typeLabel: '',
                color: 'var(--accent)', coordStr: d.coordStr,
                screenX: d.screenX, screenY: d.screenY }
            : d;
        },
        onViewChange: (v) => {
          interacting = !!v?.interacting;
          zoom = v?.zoom ?? 1;
          pan = { x: v?.panX ?? 0, y: v?.panY ?? 0 };
          active.setView(v);
        },
        // M3: the spatial snapshot the active viewer renders from. Wired ONLY
        // when holo is active, so carved-only users (mobile/mid/low) never pay
        // the snapshot build (engine emitScene early-returns with no onScene).
        // Holo activation recreates the engine via viewerRev, so this is
        // re-evaluated with active=holo at that point.
        onScene: active === carvedViewer ? undefined : (scene) => active.applyScene(scene),
        // M2 contract (task #12, agreed with dev-1): the engine relays the raw
        // /me shape {authenticated, available, self, bases, vehicles, asOfMs}
        // off each poll; chrome derives chip counts and rescue gates itself.
        onMe: (model) => { me = model?.authenticated ? model : null; },
      },
    });
    engine = eng;
    eng.start();
    return () => { eng.destroy(); if (engine === eng) engine = null; };
  });

  // The prefs doc can still be loading when the board mounts (the first legend
  // then lands on the browser fallback), so the account default is applied once
  // more the moment it becomes readable. prefsApplied latches until the board
  // changes: a later status flip never overwrites the player's live picks, and
  // a pick made DURING the load window (userTouched) keeps the apply from
  // landing at all -- the latch still closes, so the doc never arrives twice.
  $effect(() => {
    const ready = prefs.status === 'ready';
    const eng = engine;
    if (!ready || !eng) return;
    const k = key;
    untrack(() => {
      if (prefsApplied) return;
      prefsApplied = true;
      if (userTouched) return;
      const saved = prefHidden(k);
      if (saved) {
        hidden = saved;
        eng.setHidden(saved);
      }
      const viewer = prefViewer(k);
      if (viewer) viewerPref = viewer;
    });
  });

  // ---- M3 holo lifecycle -------------------------------------------------
  // Board (key) change: the {#key} block rebuilds the DOM (and rebinds the holo
  // canvas), so drop any holo bound to the old canvas and re-allow holo for the
  // fresh board. The reconcile effect below then re-decides for the new board.
  $effect(() => {
    const k = key;
    untrack(() => {
      if (prevKey !== null && prevKey !== k && (holoViewer || holoMounting)) {
        teardownHolo();
      }
      if (prevKey !== null && prevKey !== k) holoUnavailable = false;
      prevKey = k;
    });
  });

  // Resolve the viewer whenever the board is ready or the user's preference
  // changes. Auto-mounts holo on high-tier + WebGL2 (idle-deferred), swaps to
  // carved on an explicit override, and never re-attempts after a context loss.
  $effect(() => {
    if (status !== 'ready') return;
    const pref = viewerPref;   // track
    const eng = engine;        // track
    if (!eng) return;
    untrack(() => {
      const choice = holoUnavailable
        ? 'carved' : resolveViewerChoice({ quality, preference: pref });
      if (choice === 'holo' && !holoViewer && !holoMounting) scheduleHoloMount();
      else if (choice === 'carved' && (holoViewer || holoMounting)) teardownHolo();
    });
  });

  // ---- M2 personal layer -------------------------------------------------
  // All engine calls below are the task #12 surface, invoked via optional
  // chaining: while the engine extension is in flight the chrome degrades to
  // list-only behavior instead of breaking (integration pass reconciles).

  // Authed + board ready: enable the engine's 10s /me poller, feed it the
  // persisted layer visibility, and load this board's waypoints. Anon (or
  // sign-out): everything tears back down to the exact M1 chrome.
  $effect(() => {
    // Track BOTH gates: status alone can reach 'ready' around the engine
    // assignment, and an effect that read `engine` only inside untrack()
    // would no-op forever (the audio flavor of this bit QA in #17).
    const eng = engine;
    if (status !== 'ready' || !authed || !eng) return;
    const k = key;
    untrack(() => {
      eng.setMeActive?.(true);
      eng.setMeShow?.({ ...meShow });
      loadWaypoints(k);
    });
    return () => {
      eng.setMeActive?.(false);
      me = null;
    };
  });

  // Worm audio cue is public chrome (not auth-gated), armed whenever a new
  // engine instance reports ready -- including a reload that lands with the
  // persisted ls-worm-audio=1, which must arm the engine without a manual
  // toggle. Muted default; persisted.
  $effect(() => {
    const eng = engine;
    if (status !== 'ready' || !eng) return;
    untrack(() => eng.setAudioEnabled?.(audioOn));
  });

  // Live other-player layer visibility (public chrome, Hagga only). Pushed
  // whenever a fresh engine reports ready so a reload honors the persisted
  // choice; the engine no-ops the toggles on maps with no feed.
  $effect(() => {
    const eng = engine;
    if (status !== 'ready' || !eng) return;
    untrack(() => eng.setShowOthers?.({ ...showOthers }));
  });

  // Board self-pip freshness: push the chrome's meFresh clock through the
  // engine so the board pin and the chip degrade in lockstep (Ibad only
  // while the /me feed is provably alive). The engine self-heals to fresh
  // on every /me fix, so pushing true again is idempotent.
  $effect(() => {
    const eng = engine;
    if (status !== 'ready' || !eng) return;
    const fresh = meFresh;
    untrack(() => eng.setMeFresh?.(fresh));
  });

  // M-LIVE transport: the SSE client feeds the engine's single applyFix entry;
  // 'up' pauses the public pollers, 'down' (or unsupported: liveStream returns
  // null) resumes them, so polling stays the transparent fallback. Public data
  // only; runs for anon and authed alike. The `engine === eng` guard keeps the
  // teardown from resuming pollers on an already-destroyed engine instance.
  $effect(() => {
    if (status !== 'ready') return;
    const eng = engine;
    if (!eng) return;
    const stream = liveStream(key, (kind, payload) => eng.applyFix(kind, payload), {
      onStatus: (s) => { if (engine === eng) eng.setStreamActive?.(s === 'up'); },
    });
    if (!stream) return;
    return () => {
      stream.close();
      if (engine === eng) eng.setStreamActive?.(false);
    };
  });

  function setAudio(on) {
    audioOn = on;
    try { localStorage.setItem('ls-worm-audio', on ? '1' : '0'); } catch (e) {}
    engine?.setAudioEnabled?.(on);
  }

  function onMeShowChange(next) {
    meShow = next;
    try { localStorage.setItem('ls-me-show', JSON.stringify(next)); } catch (e) {}
    engine?.setMeShow?.({ ...next });
  }

  // Live other-player layer (Hagga only; public data, not auth-gated). Default
  // ON so players see the basin populated like the public map does.
  let showOthers = $state({ players: true, vehicles: true });
  function onShowOthersChange(next) {
    showOthers = next;
    try { localStorage.setItem('ls-map-others', JSON.stringify(next)); } catch (e) {}
    engine?.setShowOthers?.({ ...next });
  }
  let isHagga = $derived(meta?.key === 'hagga');
  let othersCount = $derived(counts?.othersAvailable ? (counts.otherPlayers || 0) : null);
  // "Here" count: per-sietch live others on Hagga (the /players total spans both
  // sietches), else the whole-map total.
  let hereCount = $derived(
    isHagga && othersCount != null ? othersCount : (counts?.mapPlayers ?? 0)
  );

  let meCounts = $derived({
    self: me?.available && me.self ? 1 : 0,
    bases: me?.available ? (me.bases || []).length : 0,
    vehicles: me?.available ? (me.vehicles || []).length : 0,
  });
  // Ibad freshness rule (same as the console feeds): the breathing live pip is
  // only honest while /me receipts keep arriving. 2.5x the 10s cadence; a
  // silently-dead feed (non-401 failures are swallowed) degrades to dim amber.
  const ME_STALE_MS = 25_000;
  let meFresh = $derived(me?.asOfMs != null && now - me.asOfMs <= ME_STALE_MS);
  let meStale = $derived(!!(me?.available && me.self) && !meFresh);
  let meOnline = $derived(!!(me?.available && me.self?.online) && meFresh);

  // Rescue gates the chrome can see, mirrored as honest disabled states.
  // The server stays authoritative; unknown states (no /me yet, feed down)
  // leave the button usable and let the server answer.
  let rescueGate = $derived.by(() => {
    if (key === 'deep-desert') {
      return { reason: 'Self-rescue is not available in the Deep Desert.' };
    }
    if (me?.available) {
      if (!me.self) {
        return { reason: 'We cannot see your character on this board right now.' };
      }
      if (!me.self.online) {
        return { reason: 'You need to be logged in to the game to be rescued.' };
      }
      const sameDim = (me.bases || []).some((b) => b.dim == null || me.self.dim == null
        || Number(b.dim) === Number(me.self.dim));
      if (!sameDim) {
        return { reason: 'No base totem in reach. Rescue can only send you to your own base.' };
      }
    }
    return null;
  });

  // ---- waypoints: CRUD against the V1 endpoints, optimistic w/ rollback ---
  const WP_MAX = 50; // mirrors the backend cap

  function syncPins() {
    engine?.applyFix?.('waypoints', $state.snapshot(waypoints));
  }

  async function loadWaypoints(k) {
    try {
      const d = await api.maps.waypoints.list(k);
      if (!d?.authenticated || k !== key) return;
      waypoints = (d.waypoints || []).map((w) => ({ ...w, pending: false }));
      wpError = '';
      syncPins();
    } catch (e) {
      // 401 = anon (cookie is origin-scoped in dev); leave the M1 chrome as is.
    }
  }

  async function addWaypoint(nx, ny) {
    if (waypoints.length >= WP_MAX) {
      wpError = `Waypoint limit (${WP_MAX}) reached for this map.`;
      return;
    }
    const temp = { id: -Date.now(), nx, ny, note: '', pending: true };
    waypoints = [...waypoints, temp];
    syncPins();
    try {
      const d = await api.maps.waypoints.add(key, { nx, ny, note: '' });
      waypoints = waypoints.map((w) => (w.id === temp.id ? { ...d.waypoint, pending: false } : w));
      wpError = '';
    } catch (e) {
      waypoints = waypoints.filter((w) => w.id !== temp.id);
      wpError = e?.message || 'Could not save the waypoint. Try again.';
    }
    syncPins();
  }

  async function deleteWaypoint(wp) {
    const before = waypoints;
    waypoints = waypoints.filter((w) => w.id !== wp.id);
    syncPins();
    try {
      await api.maps.waypoints.delete(key, wp.id);
      wpError = '';
    } catch (e) {
      waypoints = before;
      wpError = e?.message || 'Could not delete the waypoint. Try again.';
      syncPins();
    }
  }

  // Rename via the frozen PATCH {note} contract: id and creation order are
  // preserved. Optimistic note swap with rollback on failure.
  async function renameWaypoint(wp, note) {
    const oldNote = wp.note;
    waypoints = waypoints.map((w) => (w.id === wp.id ? { ...w, note, pending: true } : w));
    try {
      const d = await api.maps.waypoints.update(key, wp.id, note);
      waypoints = waypoints.map((w) => (w.id === wp.id ? { ...d.waypoint, pending: false } : w));
      wpError = '';
    } catch (e) {
      waypoints = waypoints.map((w) => (w.id === wp.id ? { ...w, note: oldNote, pending: false } : w));
      wpError = e?.message || 'Could not rename the waypoint. Try again.';
    }
    syncPins();
  }

  function panToWaypoint(wp) {
    engine?.panTo(wp.nx, wp.ny);
  }

  // ---- pin drop: Alt+click desktop, ~350ms long-press mobile --------------
  // Chrome owns gesture detection with CAPTURE listeners on the viewport so a
  // pin drop stops the event before the engine's bubble-phase click handler
  // can turn it into a marker popup (agreed split: engine only converts
  // coordinates via addWaypointAt).
  const LONG_PRESS_MS = 350;
  const MOVE_CANCEL_PX = 10;

  function screenToPin(mx, my) {
    const p = engine?.addWaypointAt?.(mx, my);
    if (p) return p;
    // Fallback until the task #12 engine surface lands: invert the one shared
    // transform from the chrome-tracked view state.
    const display = viewportEl?.clientWidth || 0;
    const viewUnits = meta?.view ?? 1000;
    if (!display || !meta) return null;
    const bs = display / viewUnits;
    const clampN = (v) => Math.max(0, Math.min(viewUnits, v));
    return {
      nx: clampN((mx - pan.x) / zoom / bs),
      ny: clampN((my - pan.y) / zoom / bs),
    };
  }

  function dropPinAt(mx, my) {
    const p = screenToPin(mx, my);
    if (!p) return;
    dropPulse = { x: mx, y: my };
    clearTimeout(dropPulseTimer);
    dropPulseTimer = setTimeout(() => { dropPulse = null; }, 600);
    addWaypoint(Math.round(p.nx * 10) / 10, Math.round(p.ny * 10) / 10);
  }

  $effect(() => {
    const el = viewportEl;
    if (!el || !authed) return;

    let pressTimer = null;
    let downAt = null;      // {x, y} client coords at pointerdown
    let suppressClick = false;

    const rel = (e) => {
      const rect = el.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    };
    const cancelPress = () => {
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
    };

    const onDown = (e) => {
      downAt = { x: e.clientX, y: e.clientY };
      cancelPress();
      // Long-press drop is the coarse-pointer path only; a mouse has Alt+click.
      if (e.pointerType !== 'mouse' && e.isPrimary) {
        const at = rel(e);
        pressTimer = setTimeout(() => {
          pressTimer = null;
          suppressClick = true;
          try { navigator.vibrate?.(35); } catch (err) {}
          dropPinAt(at.x, at.y);
        }, LONG_PRESS_MS);
      }
    };
    const onMove = (e) => {
      if (!pressTimer || !downAt) return;
      if (Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > MOVE_CANCEL_PX) cancelPress();
    };
    const onUp = () => cancelPress();
    const onClick = (e) => {
      if (suppressClick) {
        // The click trailing a long-press drop must not reach the engine.
        suppressClick = false;
        e.stopPropagation();
        e.preventDefault();
        return;
      }
      if (!e.altKey) return;
      // Ignore Alt+click at the end of a drag (V1 recorded the down position).
      if (downAt && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) > 6) return;
      e.stopPropagation();
      const at = rel(e);
      dropPinAt(at.x, at.y);
    };

    el.addEventListener('pointerdown', onDown, true);
    el.addEventListener('pointermove', onMove, true);
    el.addEventListener('pointerup', onUp, true);
    el.addEventListener('pointercancel', onUp, true);
    el.addEventListener('click', onClick, true);
    return () => {
      cancelPress();
      el.removeEventListener('pointerdown', onDown, true);
      el.removeEventListener('pointermove', onMove, true);
      el.removeEventListener('pointerup', onUp, true);
      el.removeEventListener('pointercancel', onUp, true);
      el.removeEventListener('click', onClick, true);
    };
  });

  $effect(() => () => clearTimeout(dropPulseTimer));

  onMount(() => {
    try { sidebarOpen = localStorage.getItem('ls-map-sidebar') !== 'closed'; } catch (e) {}
    try {
      const saved = JSON.parse(localStorage.getItem('ls-me-show') || 'null');
      if (saved && typeof saved === 'object') {
        meShow = { self: saved.self !== false, bases: saved.bases !== false, vehicles: saved.vehicles !== false };
      }
    } catch (e) {}
    try {
      const saved = JSON.parse(localStorage.getItem('ls-map-others') || 'null');
      if (saved && typeof saved === 'object') {
        showOthers = { players: saved.players !== false, vehicles: saved.vehicles !== false };
      }
    } catch (e) {}
    try { audioOn = localStorage.getItem('ls-worm-audio') === '1'; } catch (e) {}
    const measure = () => {
      const tb = document.querySelector('.topbar');
      topbarH = tb?.offsetHeight || 96;
      engine?.resize();
    };
    measure();
    window.addEventListener('resize', measure);
    const topbarObserver = new ResizeObserver(measure);
    const topbar = document.querySelector('.topbar');
    if (topbar) topbarObserver.observe(topbar);
    // Ticks the player-counter staleness gate (45s cadence, judged at 2.5x).
    const ticker = setInterval(() => { now = Date.now(); }, 10_000);
    // M3 theme bridge: the holo material re-reads CSS tokens when the surface
    // flips night<->day OR the house changes (data-house drives --accent per
    // app.css). The carved path is pure CSS and needs no signal.
    const themeObserver = new MutationObserver(() => holoViewer?.setTheme(currentThemeMode()));
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-house'] });
    return () => {
      window.removeEventListener('resize', measure);
      topbarObserver.disconnect();
      clearInterval(ticker);
      themeObserver.disconnect();
      teardownHolo();   // dispose GPU + listeners on route leave (idempotent)
    };
  });

  // Player counter is live only while /players receipts keep arriving.
  const PLAYERS_STALE_MS = 112_000; // 2.5x the 45s cadence
  let playersLive = $derived(
    counts?.playersAsOfMs != null && now - counts.playersAsOfMs <= PLAYERS_STALE_MS
  );

  // Mega-cluster affordance: below the engine's cluster threshold, dense ore
  // renders as hotspots; the legend note explains the count mismatch. Only
  // shown for boards that actually have an ore category.
  let clusterNote = $derived(
    zoom < CLUSTER_ZOOM_MAX && legend.some((c) => c.key === 'ore')
      ? 'Dense ore shows as hotspots. Zoom in for single nodes.'
      : ''
  );

  // The board eases flat on first engagement and stays flat for the session, so
  // the tilt is a one-time arrival flourish, never a lean-back on pointer-leave.
  // An engine interaction (drag/zoom) latches it too, in case that beats the hover.
  $effect(() => { if (interacting) engaged = true; });
  // Holo owns its own 3D lean, so the carved CSS tilt must stay flat under it
  // (the holo canvas lives inside .tilt-plane; a rotateX would double the pitch).
  let flat = $derived(tiltTier === 'flat' || engaged || holoActive);
</script>

<svelte:head>
  <title>{boardName || 'Map'} | Last Sietch</title>
</svelte:head>

{#key key}
  <div class="board-shell tier-{tiltTier}" style:--topbar-h={`${topbarH}px`}>
    <header class="board-head">
      <div class="board-title">
        <a class="crumb mono" href="{base}/maps">&larr; Maps</a>
        <h1>{boardName || (status === 'error' ? 'Sealed board' : 'Reading...')}</h1>
        <InstrumentTabs {instances} {dim} onchange={setDim} />
      </div>
      <div class="head-right">
        {#if counts?.playersAvailable}
          <p class="players mono" class:stale={!playersLive}
             title={playersLive ? 'Players on this map / online server-wide' : 'Last read; player feed is quiet'}>
            <span class="players-here">{hereCount}</span> here | {counts.serverPlayers} online{#if !playersLive} | last read{/if}
          </p>
        {/if}
        {#if holoAvailable}
          <ViewerToggle viewerKind={activeViewerKind} {holoAvailable} onchange={setViewerPref} />
        {/if}
      </div>
    </header>

    {#if consoleModel && meta?.has_spice}
      <div class="console-row">
        <SpiceHudConsole model={consoleModel} {instLabel} {audioOn} onaudiotoggle={setAudio} />
      </div>
    {/if}

    <div class="board-main" class:sidebar-closed={!sidebarOpen}>
      <!-- Engagement latches on the FRAME (not the viewport): the first pointer
           enter flattens the plane for the session. Because it never leans back,
           an in-frame control can never have a re-tilt move it under a click. -->
      <div
        class="board-frame"
        class:flat
        role="presentation"
        onpointerenter={() => { engaged = true; }}
      >
        <div class="tilt-plane">
          <div
            class="map-viewport"
            bind:this={viewportEl}
            role="application"
            aria-label="{boardName || 'Map'} board: drag to pan, pinch or buttons to zoom"
          >
            <div class="map-stage" class:holo-active={holoActive} bind:this={stageEl}>
              <div class="map-backdrop" bind:this={backdropEl}></div>
              <canvas class="map-canvas" bind:this={canvasEl}></canvas>
              <svg class="map-overlay" bind:this={svgEl} xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none"></svg>
            </div>

            <!-- M3: the WebGL holo canvas owns the visuals while mounted; the
                 carved .map-stage above is display:none but keeps ticking. It
                 is presentational (the carved board keeps the a11y semantics). -->
            {#if holoMounting || holoActive}
              <canvas class="holo-canvas" class:live={holoActive} bind:this={holoCanvasEl} aria-hidden="true"></canvas>
            {/if}

            {#if holoActive && dbgLayout != null}
              <div class="layout-chip mono" role="status">
                Layout {dbgLayout} forced{dbgRelief != null ? ` | relief ${dbgRelief}x` : ''}
              </div>
            {/if}

            {#if hover}
              <div class="tooltip mono" style:left={`${hover.screenX}px`} style:top={`${hover.screenY}px`}>
                {hover.label}
              </div>
            {/if}
            {#if dropPulse}
              <div class="drop-pulse" aria-hidden="true"
                   style:left={`${dropPulse.x}px`} style:top={`${dropPulse.y}px`}></div>
            {/if}
            <MapPopup detail={selectDetail} onclose={() => { selectDetail = null; }} />

            {#if status === 'loading'}
              <div class="veil" role="status">
                <p class="veil-line mono">Reading the desert...</p>
              </div>
            {:else if status === 'error'}
              <div class="veil" role="alert">
                <p class="veil-line mono">The board is sealed. Map data did not answer.</p>
                <button class="retry mono" onclick={() => engine?.start()}>Try again</button>
              </div>
            {/if}
          </div>

        </div>

        <!-- Outside .tilt-plane: the cluster must never ride the tilt
             transition (buttons moving under a mid-flight click retarget it). -->
        <div class="zoom-cluster" role="group" aria-label="Zoom">
          <button onclick={() => engine?.zoomIn()} aria-label="Zoom in">+</button>
          <button onclick={() => engine?.resetView()} aria-label="Reset view">&#x2302;</button>
          <button onclick={() => engine?.zoomOut()} aria-label="Zoom out">&minus;</button>
        </div>

        <!-- Also outside .tilt-plane (M1 lesson 2): the emergency control
             anchors to the frame so it never moves under a mid-flight click. -->
        {#if authed}
          <div class="rescue-anchor">
            <RescueButton gate={rescueGate} />
          </div>
        {/if}
      </div>

      <aside class="sidebar" aria-label="Map layers and waypoints" class:closed={!sidebarOpen}>
        <button class="sidebar-toggle mono" onclick={toggleSidebar} aria-expanded={sidebarOpen}>
          {sidebarOpen ? 'Layers ▸' : '◂'}
        </button>
        {#if sidebarOpen}
          <div class="sidebar-body">
            {#if authed}
              <div class="me-row">
                <MeChips counts={meCounts} show={meShow} online={meOnline} stale={meStale} onchange={onMeShowChange} />
              </div>
            {/if}
            {#if isHagga}
              <div class="me-row">
                <OthersChips count={othersCount} show={showOthers} onchange={onShowOthersChange} />
              </div>
            {/if}
            <LayerTree {legend} {hidden} defaults={layerDefaults} mapKey={key} onchange={onLayersChange}
              note={clusterNote}
              emptyText={status === 'error' ? 'No layers to read.' : 'Reading the layer index...'} />
            {#if authed}
              <WaypointPanel {waypoints} coarse={quality.coarse} error={wpError}
                onpan={panToWaypoint} onrename={renameWaypoint} ondelete={deleteWaypoint} />
            {/if}
          </div>
        {/if}
      </aside>
    </div>

    {#if counts?.total}
      <p class="fineprint mono">
        {counts.total.toLocaleString()} points on the board. Ibad blue marks streaming values.
      </p>
    {/if}

    <button class="fab mono" onclick={() => { sheetOpen = true; }} aria-label="Open map layers">
      Layers
    </button>
    <MapSheet open={sheetOpen} label="Map layers" onclose={() => { sheetOpen = false; }}>
      {#if authed}
        <div class="me-row">
          <MeChips counts={meCounts} show={meShow} online={meOnline} stale={meStale} onchange={onMeShowChange} />
        </div>
      {/if}
      {#if isHagga}
        <div class="me-row">
          <OthersChips count={othersCount} show={showOthers} onchange={onShowOthersChange} />
        </div>
      {/if}
      <LayerTree {legend} {hidden} defaults={layerDefaults} mapKey={key} onchange={onLayersChange}
        note={clusterNote}
        emptyText={status === 'error' ? 'No layers to read.' : 'Reading the layer index...'} />
      {#if authed}
        <WaypointPanel {waypoints} coarse={quality.coarse} error={wpError}
          onpan={panToWaypoint} onrename={renameWaypoint} ondelete={deleteWaypoint} />
      {/if}
    </MapSheet>
  </div>
{/key}

<style>
  /* ---- shell: mobile-first full-bleed column ------------------------------ */
  .board-shell {
    display: flex; flex-direction: column;
    height: calc(100dvh - var(--topbar-h, 96px));
    padding: var(--space-2) var(--space-2) 0;
    gap: var(--space-2);
  }

  .board-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-3); flex-wrap: wrap;
  }
  .board-title { display: flex; align-items: center; gap: var(--space-3); min-width: 0; }
  .head-right { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
  .crumb {
    flex: none; text-decoration: none; color: var(--text-muted);
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
  }
  .crumb:hover { color: var(--accent-bright); }
  .board-title h1 {
    font-size: var(--text-xl); letter-spacing: .04em; text-transform: uppercase;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .players {
    margin: 0; color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2);
  }
  .players-here { color: var(--ls-ibad); font-weight: 700; }
  .players.stale .players-here { color: var(--accent-soft); }

  .console-row { flex: none; }

  /* ---- board + sidebar ---------------------------------------------------- */
  .board-main {
    flex: 1; min-height: 0; display: flex; gap: var(--space-3);
  }

  /* The carved planning table: inset bezel + etched corner brackets. A custom
     frame rather than CarvedSlab (its hover lift would fight the tilt + the
     engine's pointer math). */
  .board-frame {
    position: relative; flex: 1; min-width: 0; min-height: 0;
    perspective: 1200px;
  }
  .tilt-plane {
    position: relative; height: 100%;
    /* Size container so the viewport can square itself to min(w,h): the engine
       anchors its stage to the viewport's top-left (pointer-math invariant),
       so the VIEWPORT is what gets squared + centered, exactly like V1. */
    container-type: size;
    display: flex; align-items: center; justify-content: center;
    background: var(--panel);
    border: 1px solid var(--edge);
    border-radius: var(--radius-sm);
    padding: var(--space-2);
    box-shadow:
      inset 0 1px 0 var(--metal-hi),
      inset 0 0 0 1px rgba(0, 0, 0, .45),
      0 30px 60px -30px var(--shadow-cast);
    transition: transform var(--motion-slow) var(--ease-out), box-shadow var(--motion-slow) var(--ease-out);
    transform-origin: 50% 100%;
  }
  /* Etched corner brackets (chrome = always amber). */
  .tilt-plane::before, .tilt-plane::after {
    content: ''; position: absolute; width: 13px; height: 13px;
    border: 1.5px solid var(--edge-hi); opacity: .7; pointer-events: none; z-index: 5;
  }
  .tilt-plane::before { top: 5px; left: 5px; border-right: 0; border-bottom: 0; }
  .tilt-plane::after { bottom: 5px; right: 5px; border-left: 0; border-top: 0; }

  /* 2.5D: resting tilt on capable desktops, easing flat on engagement. */
  @media (min-width: 900px) {
    .tier-high .tilt-plane, .tier-mid .tilt-plane { transform: rotateX(14deg); }
    .tier-high .board-frame.flat .tilt-plane,
    .tier-mid .board-frame.flat .tilt-plane { transform: rotateX(0deg); }
    /* Far-edge amber rim light: reads as the light source raking the table. */
    .tier-high .tilt-plane {
      box-shadow:
        inset 0 1px 0 var(--metal-hi),
        inset 0 14px 24px -18px var(--accent-glow),
        inset 0 0 0 1px rgba(0, 0, 0, .45),
        0 30px 60px -30px var(--shadow-cast);
    }
  }

  .map-viewport {
    position: relative; flex: none;
    width: min(100cqw, 100cqh); height: min(100cqw, 100cqh);
    overflow: hidden; border-radius: 2px;
    background: var(--metal-0);
    cursor: grab;
    touch-action: none;
  }
  .map-viewport:global(.is-dragging) { cursor: grabbing; }
  .map-stage { position: absolute; top: 0; left: 0; transform-origin: 0 0; }
  /* M3: the carved stage is hidden (not unmounted) while holo owns the board;
     the engine keeps ticking it so a swap back is instant. */
  .map-stage.holo-active { display: none; }
  .map-backdrop { position: absolute; inset: 0; }
  .map-canvas, .map-overlay { position: absolute; inset: 0; width: 100%; height: 100%; }
  .map-overlay { pointer-events: none; }

  /* M3 holo canvas: fills the viewport, under the chrome overlays (tooltip,
     popup, veil) but the engine's gesture layer stays on .map-viewport so
     hit-testing is unchanged (the engine converts via opts.projector). */
  .holo-canvas {
    position: absolute; inset: 0; width: 100%; height: 100%;
    z-index: 1; pointer-events: none; display: block;
  }

  /* Debug-forced layout chip: only rendered when ?holoLayout=N is in the URL,
     so it never appears for players on the normal path. */
  .layout-chip {
    position: absolute; z-index: 11; left: var(--space-2); bottom: var(--space-2);
    pointer-events: none;
    background: color-mix(in srgb, var(--bg-deep) 78%, transparent);
    color: var(--accent-bright, var(--accent));
    border: 1px solid color-mix(in srgb, var(--accent) 46%, transparent);
    border-radius: var(--radius-sm);
    padding: 2px var(--space-2); font-size: var(--text-xs);
    letter-spacing: .14em; text-transform: uppercase; white-space: nowrap;
  }

  .tooltip {
    position: absolute; z-index: 11; pointer-events: none;
    transform: translate(-50%, calc(-100% - 10px));
    background: var(--metal-0); color: var(--text);
    border: 1px solid var(--edge-hi); border-radius: var(--radius-sm);
    padding: 2px var(--space-2); font-size: var(--text-xs);
    white-space: nowrap; box-shadow: 0 6px 18px rgba(0, 0, 0, .4);
  }

  .veil {
    position: absolute; inset: 0; z-index: 10;
    display: grid; place-content: center; gap: var(--space-3);
    background: color-mix(in srgb, var(--bg-deep) 62%, transparent);
    text-align: center;
  }
  .veil-line {
    margin: 0; color: var(--text-muted); font-size: var(--text-sm);
    letter-spacing: .18em; text-transform: uppercase;
  }
  .retry {
    justify-self: center;
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-4); cursor: pointer;
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
  }
  .retry:hover { border-color: var(--accent); }

  .zoom-cluster {
    position: absolute; right: var(--space-3); bottom: var(--space-3); z-index: 12;
    display: flex; flex-direction: column; gap: var(--space-1);
  }
  .zoom-cluster button {
    width: 38px; height: 38px; display: grid; place-items: center;
    background: var(--panel); color: var(--text);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    font-size: var(--text-lg); line-height: 1; cursor: pointer;
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 6px 14px rgba(0, 0, 0, .35);
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .zoom-cluster button:hover { border-color: var(--accent); color: var(--accent-bright); }

  /* Rescue control: frame-anchored opposite the zoom cluster. */
  .rescue-anchor {
    position: absolute; left: var(--space-3); bottom: var(--space-3); z-index: 12;
  }

  /* Pin-drop feedback: a haptic-style ring where the press landed. */
  .drop-pulse {
    position: absolute; z-index: 11; pointer-events: none;
    width: 14px; height: 14px; border-radius: 50%;
    transform: translate(-50%, -50%);
    border: 2px solid var(--accent);
    box-shadow: 0 0 10px var(--accent-glow);
    animation: drop-pulse .6s var(--ease-out) forwards;
  }
  @keyframes drop-pulse {
    from { opacity: 1; scale: .5; }
    to { opacity: 0; scale: 2.4; }
  }
  @media (prefers-reduced-motion: reduce) {
    .drop-pulse { animation: none; opacity: 0; }
  }

  .me-row { margin-bottom: var(--space-3); }

  /* ---- sidebar (desktop) --------------------------------------------------- */
  .sidebar { display: none; }

  .fineprint {
    display: none; flex: none; margin: 0; padding: var(--space-1) 0 var(--space-2);
    color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .06em; opacity: .75;
  }
  @media (min-width: 900px) { .fineprint { display: block; } }

  /* ---- mobile FAB + sheet --------------------------------------------------- */
  .fab {
    position: fixed; right: var(--space-4); z-index: 29;
    bottom: calc(var(--space-4) + env(safe-area-inset-bottom, 0px));
    background: var(--accent); color: var(--bg-deep);
    border: 1px solid var(--accent); border-radius: var(--radius-xl);
    padding: var(--space-3) var(--space-5);
    font-size: var(--text-sm); font-weight: 700;
    letter-spacing: .12em; text-transform: uppercase; cursor: pointer;
    box-shadow: 0 0 14px var(--accent-glow), 0 10px 24px rgba(0, 0, 0, .4);
  }

  /* ---- desktop layout ------------------------------------------------------- */
  @media (min-width: 900px) {
    .board-shell {
      max-width: 1400px; margin: 0 auto; width: 100%;
      height: auto; min-height: calc(100dvh - var(--topbar-h, 96px));
      padding: var(--space-5) var(--space-4) var(--space-4);
      gap: var(--space-4);
    }
    .board-main {
      /* Explicit height (not flex-grow): the squared viewport derives from
         container-size units, so the chain needs a definite height. */
      flex: none;
      height: clamp(460px, calc(100dvh - var(--topbar-h, 96px) - 250px), 900px);
    }
    .board-title h1 { font-size: var(--text-2xl); }

    .sidebar {
      display: flex; flex-direction: column; flex: none;
      width: 300px; min-height: 0;
      background: var(--panel);
      border: 1px solid var(--edge); border-radius: var(--radius-sm);
      box-shadow: inset 0 1px 0 var(--metal-hi), 0 18px 44px -28px var(--shadow-cast);
      padding: var(--space-3);
      transition: width var(--motion-mid) var(--ease-out);
    }
    .sidebar.closed { width: 44px; padding: var(--space-3) var(--space-1); }
    .sidebar-toggle {
      flex: none; align-self: stretch; margin-bottom: var(--space-2);
      background: transparent; color: var(--text-muted); border: 0; cursor: pointer;
      font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
      text-align: left; padding: var(--space-1);
    }
    .sidebar.closed .sidebar-toggle { text-align: center; padding: var(--space-1) 0; }
    .sidebar-toggle:hover { color: var(--text); }
    .sidebar-body { flex: 1; min-height: 0; display: flex; flex-direction: column; }

    .fab { display: none; }
  }
  @media (max-width: 899px) {
    .console-row :global(.slab) { padding: var(--space-3) var(--space-4); }
  }
</style>
