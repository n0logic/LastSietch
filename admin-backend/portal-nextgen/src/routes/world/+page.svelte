<script>
  // /world: the Sand Table. The game's overland as a carved relief on a sietch table,
  // one stone per destination; click descends into that map's War-Table. 3D is a lazy
  // chunk; reduced motion, low tier, phones and no-WebGL2 get the Guild chart (same
  // stones, same states, same cards). Live layer: status from /api/dune/status, band
  // marks ONLY from the enum-only /api/dune/bands (absent or failing = no marks).
  import { onMount, onDestroy, untrack } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { api } from '$lib/api.js';
  import { auth } from '$lib/auth.svelte.js';
  import { detectQuality } from '$lib/quality.js';
  import { probeWebGL2 } from '$lib/map/holo/stage-adapter.js';
  import { serverTime, subscribeServerTime } from '$lib/server-time.svelte.js';
  import { resolveHour, MODES } from '$lib/world/tod.js';
  import * as worldData from '$lib/world/data.js';
  import { mapInstances, loadMapInstances } from '$lib/mapInstances.svelte.js';
  import { untrackedBandKeys } from '$lib/mapInstances.js';
  import GuildChart from '$lib/world/GuildChart.svelte';
  import InstrumentCard from '$lib/world/InstrumentCard.svelte';
  import {
    loadWorld, loadBands, chooseView, stoneStatus, stoneBands, cardModel, cycle, mapHref,
    shouldArrive, markArrived, anchorStoneId, STATUS_TEXT,
  } from '$lib/world/data.js';

  let world = $state(null);
  let loadError = $state(false);
  let status = $state(null);
  let bands = $state(null);
  let untracked = $derived(untrackedBandKeys(mapInstances.list));
  let anchorId = $state(null);
  let selectedId = $state(null);
  let mode = $state(null);            // '3d' | 'chart' once chosen
  let notice = $state('');
  let quality = null;
  let forced = null;
  let hostEl = $state(null);
  let ctl = null;
  let hover = $state({ id: null, anchor: null });
  let bounds = $state({ w: 900, h: 600 });
  let chooser = $state(null);         // stone awaiting a facet pick
  let leaving = $state(false);
  let sceneReady = $state(false);
  let stageEl = $state(null);

  let reduced = false;

  async function pollStatus() {
    try { status = await api.server.duneStatus(); } catch { status = null; }
  }
  // Live storms over Deep Desert and Hagga (signed-in only; any failure means no storm on that stone)
  async function pollStorm() {
    const stones = Object.entries(worldData.STORM_STONES_MAP);
    if (!ctl || !sceneReady || auth.status !== 'authed') { if (ctl && sceneReady) stones.forEach(([id]) => ctl.setStorm(null, id)); return; }
    await Promise.all(stones.map(async ([id, key]) => {
      try { ctl.setStorm(worldData.stormsFromLive(await api.maps.live(key), Date.now()), id); } catch { ctl.setStorm(null, id); }
    }));
  }
  async function pollBands() { bands = await loadBands(); }

  async function loadAll() {
    loadError = false;
    try { world = await loadWorld(base); } catch (e) { world = null; loadError = true; }
  }

  async function findAnchor() {
    if (auth.status !== 'authed') return;
    try {
      const h = await api.home.overview();
      const c = h && h.character;
      if (c && world) anchorId = anchorStoneId(world.stones, c.current_map);
    } catch { /* signed in without a linked character, or the read failed: no marker */ }
  }

  onMount(() => {
    try { const m = localStorage.getItem('ls-world-tod'); if (MODES.includes(m)) tod = m; } catch { /* no storage */ }
    quality = detectQuality();
    reduced = quality.reducedMotion;
    const q = $page.url.searchParams.get('view');
    forced = q === '3d' || q === 'chart' ? q : null;
    mode = chooseView({ quality, webgl2: probeWebGL2(), forced });
    loadMapInstances();
    pollStatus(); pollBands();
    const t1 = setInterval(pollStatus, 45_000);
    const t2 = setInterval(() => { pollBands(); pollStorm(); }, 60_000);
    const unTime = subscribeServerTime();
    loadAll();
    return () => { clearInterval(t1); clearInterval(t2); unTime(); };
  });
  onDestroy(() => { if (ctl) { ctl.destroy(); ctl = null; } });

  $effect(() => { if (world && auth.status === 'authed') untrack(findAnchor); });

  // ?stone=<id>: deep link from the Reach
  $effect(() => {
    const id = $page.url.searchParams.get('stone');
    if (world && id && world.stones.some((s) => s.id === id)) untrack(() => select(id, true));
  });

  // ---- derived live state -----------------------------------------------------------
  let statusMap = $derived(world ? Object.fromEntries(world.stones.map((s) => [s.id, stoneStatus(s, status)])) : {});
  let bandMap = $derived(world ? Object.fromEntries(world.stones.map((s) => [s.id, stoneBands(s, bands, untracked)])) : {});
  let focusId = $derived(hover.id || selectedId);
  let focusStone = $derived(world && focusId ? world.stones.find((s) => s.id === focusId) : null);
  let model = $derived(focusStone ? cardModel(focusStone, status, bands, untracked) : null);
  let anchorStone = $derived(world && anchorId ? world.stones.find((s) => s.id === anchorId) : null);

  $effect(() => { if (ctl && sceneReady) ctl.setStatus(statusMap); });
  $effect(() => { if (ctl && sceneReady) ctl.setBands(bandMap); });
  $effect(() => { if (ctl && sceneReady) ctl.setAnchor(anchorId); });

  // Eastern-time hour of the server clock, drives the sun leak
  // ?hour=14 pins the key light (review / screenshots); otherwise the server clock
  // The viewer's local clock picks the lighting (dawn / day / dusk / night); ?hour=N pins it
  // for screenshots, and the Light toggle (remembered) forces a preset.
  let tod = $state('auto');
  function lightHour() {
    const q = $page.url.searchParams;
    return resolveHour(tod, q.has('hour') ? Number(q.get('hour')) : NaN);
  }
  function setTod(m) {
    tod = m;
    try { localStorage.setItem('ls-world-tod', m); } catch { /* private mode */ }
    if (ctl && sceneReady) ctl.refreshLight();
  }
  function rimCountdown() {
    const left = serverTime.nextResetMs - serverTime.nowMs;
    if (!Number.isFinite(left) || left <= 0) return '';
    const mins = Math.floor(left / 60000), d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
    return 'Coriolis reset in ' + (d ? `${d}d ${h}h` : `${h}h ${m}m`);
  }

  // ---- 3D mount -----------------------------------------------------------------------
  let mounting = false;
  $effect(() => {
    if (mode === '3d' && world && hostEl && !ctl && !mounting) untrack(mount3d);
  });

  async function mount3d() {
    mounting = true;
    try {
      const mod = await import('$lib/map/sandtable/index.js');
      const c = mod.createSandTable(hostEl, {
        world, base, reducedMotion: reduced, exag: Number($page.url.searchParams.get('exag')) || undefined, hi: quality.tier === 'high',
        getHour: lightHour, getCountdown: rimCountdown,
        initial: { status: statusMap, bands: bandMap, anchor: anchorId },
        onHover: (id, anchor) => { hover = { id, anchor }; },
        onActivate: (id) => activate(id),
        onContextLost: () => fallback('The 3D table lost its graphics context. Showing the Guild chart.'),
      });
      ctl = c;
      await Promise.race([c.ready, new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 30000))]);
      sceneReady = true;
      pollStorm();
      bounds = { w: hostEl.clientWidth, h: hostEl.clientHeight };
      if (shouldArrive({ reducedMotion: reduced, storage: sessionStorage })) { c.arrive(); markArrived(sessionStorage); }
    } catch (e) {
      console.warn('[world] 3D table unavailable', e);
      fallback('The 3D table could not start here. Showing the Guild chart.');
    } finally { mounting = false; }
  }
  function fallback(msg) {
    if (ctl) { try { ctl.destroy(); } catch { /* already gone */ } ctl = null; }
    sceneReady = false; hover = { id: null, anchor: null };
    notice = msg; mode = 'chart';
  }
  function setMode(m) {
    if (m === mode) return;
    notice = '';
    if (ctl) { ctl.destroy(); ctl = null; sceneReady = false; }
    hover = { id: null, anchor: null };
    mode = m;
  }

  // ---- selection / open ---------------------------------------------------------------
  function select(id, fly) {
    selectedId = id;
    if (ctl) ctl.setSelected(id, fly);
  }
  // Click or Enter on a stone. Live stones open their War-Table (facets choose the
  // instance); engraved markers just pin their card.
  function activate(id) {
    const s = world.stones.find((x) => x.id === id);
    if (!s) return;
    select(id, false);
    if (!s.live) return;
    openStone(s, null);
  }
  function openStone(s, facet) {
    if (s.facets.length > 1 && !facet) { chooser = s; return; }
    chooser = null;
    const href = mapHref(base, s, facet);
    if (!href) return;
    const go = () => { leaving = true; setTimeout(() => goto(href), reduced || mode === 'chart' ? 0 : 260); };
    if (ctl && !reduced) ctl.descend(s.id, go); else goto(href);
  }

  function onKey(e) {
    if (!world || mode !== '3d') return;
    if (chooser) { if (e.key === 'Escape') { e.preventDefault(); chooser = null; } return; }
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      select(cycle(world.stones, selectedId, e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1), true);
    } else if (e.key === 'Enter' && selectedId) {
      e.preventDefault(); activate(selectedId);
    } else if (e.key === 'Escape') {
      e.preventDefault(); select(null, false); hover = { id: null, anchor: null }; if (ctl) ctl.overview();
    }
  }

  let announce = $derived(model ? `${model.title}. ${model.kind}.${model.statusText ? ' ' + model.statusText + '.' : ''}` : '');
  let selAnchor = $derived(ctl && sceneReady && selectedId && !hover.id ? ctl.anchorOf(selectedId) : null);
  let cardAnchor = $derived(hover.anchor || selAnchor);
</script>

<svelte:head><title>Sand Table | Last Sietch</title></svelte:head>

<div class="page">
  <header class="head">
    <div>
      <p class="kicker mono">Last Sietch | the world</p>
      <h1>Sand Table</h1>
    </div>
    <p class="sub">The overland of Arrakis as a carved relief. Every stone is a place you can travel to. Pick one to descend into its War-Table.</p>
  </header>

  <div class="bar">
    {#if anchorStone}<p class="you mono">You are on {anchorStone.name}.</p>{:else}<span></span>{/if}
    {#if mode === '3d'}
      <div class="seg small" role="group" aria-label="Light">
        {#each MODES as m}<button type="button" class:on={tod === m} aria-pressed={tod === m} onclick={() => setTod(m)}>{m === 'auto' ? 'Auto light' : m}</button>{/each}
      </div>
    {/if}
    <div class="seg" role="group" aria-label="View">
      <button type="button" class:on={mode === '3d'} aria-pressed={mode === '3d'} onclick={() => setMode('3d')}>3D table</button>
      <button type="button" class:on={mode === 'chart'} aria-pressed={mode === 'chart'} onclick={() => setMode('chart')}>Guild chart</button>
    </div>
  </div>
  {#if notice}<p class="notice" role="status">{notice}</p>{/if}

  <div class="stage-wrap" class:leaving bind:this={stageEl}>
    {#if loadError}
      <div class="state" role="alert">
        <p>The world data did not load.</p>
        <button type="button" class="btn" onclick={loadAll}>Try again</button>
      </div>
    {:else if !world || !mode}
      <div class="state" aria-busy="true"><p class="mono">Unrolling the table</p></div>
    {:else if mode === 'chart'}
      <GuildChart {world} {status} {bands} {anchorId} bind:selectedId onopen={(s, f) => openStone(s, f)} />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <div class="table" role="group" tabindex="0" aria-label="Sand Table, 3D view. Arrow keys move between locations, Enter opens a map, Escape returns to the full table." onkeydown={onKey}>
        <div class="host" bind:this={hostEl}></div>
        {#if !sceneReady}<div class="state over" aria-busy="true"><p class="mono">Carving the relief</p></div>{/if}
        {#if sceneReady}<InstrumentCard {model} anchor={cardAnchor} {bounds} onopen={() => {}} />{/if}
        {#if chooser}
          <div class="chooser" role="dialog" aria-label="Choose where to open {chooser.name}">
            <p class="mono">Open {chooser.name}</p>
            <div class="opts">
              {#each chooser.facets as f (f.key)}
                <button type="button" class="btn" onclick={() => openStone(chooser, f.key)}>
                  <b>{f.name}</b><span>{f.rule}</span>
                </button>
              {/each}
            </div>
            <button type="button" class="btn ghost" onclick={() => (chooser = null)}>Cancel</button>
          </div>
        {/if}
        <p class="sr" aria-live="polite">{announce}</p>
        <p class="hint mono">Drag to move. Right-drag, or Q and E, to turn. Scroll to zoom.</p>
      </div>
    {/if}
    {#if mode === 'chart' && chooser}
      <div class="chooser" role="dialog" aria-label="Choose where to open {chooser.name}">
        <p class="mono">Open {chooser.name}</p>
        <div class="opts">
          {#each chooser.facets as f (f.key)}
            <button type="button" class="btn" onclick={() => openStone(chooser, f.key)}><b>{f.name}</b><span>{f.rule}</span></button>
          {/each}
        </div>
        <button type="button" class="btn ghost" onclick={() => (chooser = null)}>Cancel</button>
      </div>
    {/if}
    <div class="veil" aria-hidden="true"></div>
  </div>

  {#if world}
    <p class="legend mono">
      <span><i class="d up"></i>Online</span><span><i class="d down"></i>Down</span><span><i class="d od"></i>Available on travel</span>
      {#if bands}<span><i class="d mk"></i>Marks show how busy a map is compared with its usual</span>{/if}
    </p>
  {/if}
</div>

<style>
  .page { max-width: 1280px; margin: 0 auto; padding: 0 var(--space-4) var(--space-6); }
  .bar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: var(--space-3); margin-bottom: var(--space-3); }
  .seg { display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; }
  .seg button { background: transparent; color: var(--text-muted); border: 0; padding: 8px 14px; min-height: 40px; font: inherit; letter-spacing: .06em; text-transform: uppercase; font-size: var(--text-sm); cursor: pointer; }
  .seg.small button { padding: 6px 10px; min-height: 36px; font-size: var(--text-xs); }
  .seg button.on { background: var(--accent-soft); color: var(--text); }
  .seg button:focus-visible { outline: 2px solid var(--accent-bright); outline-offset: -2px; }
  .notice { margin: 0 0 var(--space-3); padding: 8px 12px; border: 1px solid var(--edge); color: var(--text-muted); font-size: var(--text-sm); }
  .head { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: var(--space-2) var(--space-5); margin: var(--space-3) 0 var(--space-3); }
  .kicker { color: var(--accent); text-transform: uppercase; letter-spacing: .34em; font-size: var(--text-xs); margin: 0 0 4px; }
  h1 { margin: 0; font-size: clamp(1.7rem, 3.6vw, 2.4rem); line-height: 1; text-transform: uppercase; letter-spacing: .01em; }
  .sub { margin: 0; color: var(--text-muted); max-width: 52ch; font-size: var(--text-sm); }
  .you { margin: 0; font-size: var(--text-xs); color: var(--ls-ibad); letter-spacing: .12em; text-transform: uppercase; }
  .stage-wrap { position: relative; height: clamp(440px, calc(100dvh - 300px), 860px); background: #080605; border: 1px solid var(--edge); }
  .table { position: absolute; inset: 0; outline: none; }
  .table:focus-visible { box-shadow: inset 0 0 0 2px var(--accent-bright); }
  .host { position: absolute; inset: 0; overflow: hidden; }
  .state { position: absolute; inset: 0; display: grid; place-content: center; gap: var(--space-3); text-align: center; color: var(--text-muted); }
  .state.over { pointer-events: none; background: radial-gradient(ellipse at center, rgba(12,9,6,.6), rgba(8,6,5,.92)); z-index: 5; }
  .state p { margin: 0; letter-spacing: .24em; text-transform: uppercase; font-size: var(--text-sm); }
  .btn { background: var(--panel); color: var(--text); border: 1px solid var(--edge-hi); border-radius: var(--radius-sm); padding: 10px 16px; min-height: 44px; font: inherit; cursor: pointer; }
  .btn:hover { border-color: var(--accent); }
  .btn:focus-visible { outline: 2px solid var(--accent-bright); outline-offset: 2px; }
  .btn.ghost { background: transparent; color: var(--text-muted); }
  .btn b { display: block; letter-spacing: .12em; text-transform: uppercase; color: var(--accent-bright); }
  .btn span { font-size: var(--text-xs); color: var(--text-muted); }
  .chooser { position: absolute; left: 50%; bottom: var(--space-5); transform: translateX(-50%); z-index: 30; width: min(560px, calc(100% - 24px)); padding: var(--space-3) var(--space-4); background: rgba(14,11,8,.96); border: 1px solid var(--edge-hi); box-shadow: 0 14px 40px rgba(0,0,0,.6); display: grid; gap: var(--space-3); }
  .chooser p { margin: 0; text-transform: uppercase; letter-spacing: .2em; font-size: var(--text-xs); color: var(--accent); }
  .opts { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: var(--space-2); }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .hint { position: absolute; left: 12px; bottom: 8px; margin: 0; font-size: 11px; letter-spacing: .08em; color: rgba(230,205,160,.6); pointer-events: none; text-shadow: 0 1px 3px #000; }
  .veil { position: absolute; inset: 0; background: #060403; opacity: 0; pointer-events: none; transition: opacity .25s ease; }
  .leaving .veil { opacity: 1; }
  .legend { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); margin: var(--space-3) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .d { width: 10px; height: 10px; border-radius: 50%; display: inline-block; border: 2px solid; }
  .d.up { border-color: var(--accent-bright); background: var(--accent-glow); }
  .d.down { border-color: #3a352e; background: #1d1812; }
  .d.od { border-color: #8c6a2f; border-style: dashed; }
  .d.mk { border-color: var(--accent-bright); background: var(--accent-bright); width: 7px; height: 7px; }
  @media (prefers-reduced-motion: reduce) { .veil { transition: none; } }
  @media (max-width: 640px) { .stage-wrap { height: clamp(420px, 70dvh, 640px); } .hint { display: none; } }

  /* DOM labels the scene creates inside the host (scoped styles can't reach them) */
  :global(.st-label) { font-family: 'Saira Condensed', sans-serif; pointer-events: none; transition: opacity .15s; }
  :global(.st-gold) {
    font-size: clamp(13px, 1.35vw, 20px); font-weight: 700; letter-spacing: .2em;
    background: linear-gradient(180deg, #fff0b8 0%, #f1b84a 48%, #9a5d12 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
    filter: drop-shadow(0 2px 0 #4a2606) drop-shadow(0 4px 6px rgba(0,0,0,.7));
    font-style: italic;
  }
  :global(.st-cap) { font-size: 12.5px; letter-spacing: .08em; color: #dac0a0; text-shadow: 0 1px 3px #000, 0 0 6px #000; }
</style>
