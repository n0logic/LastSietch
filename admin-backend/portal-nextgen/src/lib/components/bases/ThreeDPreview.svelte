<script>
  // 3D blueprint preview modal. Reuses the FULL V1 three.js engine
  // (/admin/static/js/portal-solido-viewer.js) rather than a bundled box viewer,
  // so V2 gets real GLB meshes, Clay/Textured materials, the first-person walk,
  // piece breakdown, and piece selection at parity with the V1 detail page. The
  // engine + its vendored three + catalog + glb-manifest + 1055 piece GLBs are all
  // same-origin under /admin/static (CSP script-src/connect-src 'self'), so this
  // loads with no CSP change and no new assets. Only the chrome (toolbar, panels,
  // walk HUD) is reimplemented here in Svelte; the heavy rendering is all V1.
  //
  // The engine mounts synchronously in Boxes mode (instant), then upgrades to Real
  // Meshes once the manifest resolves and confirms this base has matching GLBs.
  //
  // Chrome, focus trap, inert background, scrim and the X come from the shared Modal
  // primitive. `size="stage"` is a size Modal does not style, so the viewport-sized
  // panel below owns its own dimensions without fighting a shipped rule.
  //
  // Phone sizing (wave 6): the stage is a fraction of the viewport with a hard
  // cap, not the whole screen. At 92dvh the canvas swallowed the phone, the piece
  // breakdown and the exit control landed off the visible area, and a player with
  // one hand on the device had nothing to grab. Orbit on touch is OrbitControls'
  // own one-finger drag; it only works while the canvas keeps `touch-action: none`,
  // or the browser scrolls the modal instead of rotating the base.
  //
  // Data Saver: this pulls a vendored three.js, a piece catalog and up to 1055
  // GLBs. On a metered connection that is not a cost to spend without asking, so
  // the viewer does not init at all: the listing thumbnail stands in and a tap
  // arms the load. The signal comes from the one app-wide detector, the same
  // `detectQuality()` SealedPanel reads; a second ad-hoc probe here would be a
  // second thing to keep true.
  import { tick } from 'svelte';
  import { api } from '$lib/api.js';
  // The engine + catalog URLs live in the store (one ?v= pin for the preview and
  // the publish-time thumbnail, so both hit the same cached module).
  import { bases, closePreview, VIEWER_URL, CATALOG_URL } from '$lib/bases.svelte.js';
  import { detectQuality } from '$lib/quality.js';
  import Modal from '$lib/components/Modal.svelte';
  let _catalogPromise = null;
  function loadCatalog() {
    if (!_catalogPromise) {
      _catalogPromise = fetch(CATALOG_URL, { credentials: 'same-origin' }).then((r) => {
        if (!r.ok) throw new Error('catalog ' + r.status);
        return r.json();
      });
    }
    return _catalogPromise;
  }

  let target = $derived(bases.preview);
  let open = $derived(target != null);

  const quality = detectQuality();
  // The preview target the player explicitly armed. Keyed to the target rather
  // than a bare boolean so opening a DIFFERENT base asks again instead of
  // inheriting the last consent.
  let armedFor = $state(null);
  let saverHold = $derived(quality.saveData && armedFor !== target);
  function loadNow() { armedFor = target; }

  let canvasEl = $state(null);
  let stageEl = $state(null);

  let phase = $state('idle');       // idle | loading | saver | ready | error
  let note = $state('');
  let handle = null;
  let manifest = null;

  // Chrome state.
  let mode = $state('boxes');       // boxes | real
  let textured = $state(false);     // clay (false) | textured (true)
  let switching = $state(false);
  let hasReal = $state(false);      // this base has GLB-backed pieces
  let realWarn = $state(false);     // large-base perf warning
  let breakdown = $state([]);       // [{label, count}]
  let counts = $state({ structural: 0, placeables: 0, pentashields: 0 });
  let selected = $state(null);      // clicked piece info | null
  let walkActive = $state(false);
  let walkPaused = $state(false);
  let isTouch = $state(false);
  let shotFailed = $state(false);   // the stand-in thumbnail 404'd (never rendered)
  let fsOn = $state(false);
  let cssFs = $state(false);        // CSS fullscreen fallback (iOS Safari)

  function fmt(n) { return (Number(n) || 0).toLocaleString(); }

  // (Re)mount the engine whenever the open target changes.
  $effect(() => {
    const t = target;
    if (!t) { teardown(); return; }
    resetChrome();
    // Nothing is fetched until the player asks: no engine, no catalog, no GLBs.
    if (saverHold) { phase = 'saver'; note = ''; return; }
    phase = 'loading'; note = '';
    let cancelled = false;
    (async () => {
      const [blob, cat, mod] = await Promise.all([
        t.publishId != null ? api.bases.blueprint(t.publishId) : api.bases.ownBlueprint(t.bpId),
        loadCatalog(),
        import(/* @vite-ignore */ VIEWER_URL),
      ]);
      if (cancelled) return;
      if (!blob || blob.available === false) { phase = 'error'; note = '3D preview is unavailable for this base.'; return; }
      await tick(); // canvas in the DOM
      if (cancelled || !canvasEl) return;

      handle = mod.mount(canvasEl, blob, cat, {
        onSelect: (info) => { selected = info; },
        onWalkState: (on) => { walkActive = on; if (!on) walkPaused = false; },
        onWalkPause: (on) => { walkPaused = on; },
        onWalkRequestFs: () => toggleFs(),
      });
      isTouch = !!handle.isTouch;
      const s = handle.stats || {};
      breakdown = (handle.getBreakdown && handle.getBreakdown()) || s.breakdown || [];
      counts = {
        structural: (blob.instances || []).length,
        placeables: (blob.placeables || []).length,
        pentashields: (blob.pentashields || []).length,
      };
      const bits = [`${fmt(s.rendered_pieces)} pieces`];
      if (s.capped) bits.push('large, simplified');
      if (s.pentashields_skipped) bits.push(`${s.pentashields_skipped} Pentashields not shown`);
      note = bits.join(' · ');
      phase = 'ready';

      // Upgrade to Real Meshes once the manifest confirms matching GLBs.
      if (handle.getManifest) {
        handle.getManifest().then((m) => {
          if (cancelled) return;
          manifest = m;
          if (manifestOverlap(m, blob) > 0) {
            hasReal = true;
            realWarn = !!handle.realMeshWarning;
            setMode('real').then(() => setMat(true));   // owner ruling 2026-09-03: real meshes, textured; boxes and clay one click away
          }
        }).catch(() => { /* no manifest, boxes only */ });
      }
    })().catch(() => { if (!cancelled) { phase = 'error'; note = '3D preview could not be loaded.'; } });
    return () => { cancelled = true; teardown(); };
  });

  function manifestOverlap(m, blob) {
    if (!m || !m.pieces) return 0;
    const used = new Set();
    for (const it of (blob.instances || [])) if (it.building_type) used.add(it.building_type);
    for (const pl of (blob.placeables || [])) if (pl.building_type) used.add(pl.building_type);
    let n = 0;
    for (const k in m.pieces) if (used.has(k)) n++;
    return n;
  }

  function resetChrome() {
    mode = 'boxes'; textured = false; switching = false; hasReal = false;
    realWarn = false; breakdown = []; counts = { structural: 0, placeables: 0, pentashields: 0 };
    selected = null; walkActive = false; walkPaused = false; manifest = null;
    shotFailed = false;
  }

  async function setMode(m) {
    if (switching || !handle || m === mode) return;
    if (m === 'real' && !hasReal) return;
    switching = true;
    try { await handle.switchMode(m, manifest); mode = m; if (m === 'boxes') selected = null; }
    catch (e) { mode = 'boxes'; }
    finally { switching = false; }
  }

  async function setMat(t) {
    if (switching || !handle || t === textured) return;
    switching = true;
    try { await handle.setTextured(t); textured = t; }
    catch (e) { /* keep prior */ }
    finally { switching = false; }
  }

  function startWalk() { try { handle?.startWalk?.(); } catch (e) {} }
  function stopWalk() { try { handle?.stopWalk?.(); } catch (e) {} }
  function resumeWalk() { try { handle?.resumeWalk?.(); } catch (e) {} }

  function toggleFs() {
    const el = stageEl;
    if (!el) return;
    if (document.fullscreenElement === el) { document.exitFullscreen?.(); return; }
    if (cssFs) { cssFs = false; return; }
    if (el.requestFullscreen) { el.requestFullscreen().catch(() => { cssFs = true; }); }
    else { cssFs = true; }
  }
  function onFsChange() { fsOn = document.fullscreenElement === stageEl; }

  // Touch joystick: pointer-captured drag → normalized (x, y), y+ = forward.
  let joyEl = $state(null);
  let nubT = $state('');
  let joyPid = null, joyCx = 0, joyCy = 0;
  const JOY_R = 40;
  function joyDown(e) {
    joyPid = e.pointerId;
    try { joyEl.setPointerCapture(joyPid); } catch (err) {}
    const r = joyEl.getBoundingClientRect();
    joyCx = r.left + r.width / 2; joyCy = r.top + r.height / 2;
    joyMove(e); e.preventDefault();
  }
  function joyMove(e) {
    if (e.pointerId !== joyPid) return;
    const dx = e.clientX - joyCx, dy = e.clientY - joyCy;
    const d = Math.hypot(dx, dy) || 1, c = Math.min(1, d / JOY_R);
    const nx = (dx / d) * c, ny = (dy / d) * c;
    nubT = `translate(${nx * JOY_R}px, ${ny * JOY_R}px)`;
    handle?.setWalkJoystick?.(nx, -ny);
  }
  function joyEnd(e) {
    if (e.pointerId !== joyPid) return;
    joyPid = null; nubT = '';
    handle?.setWalkJoystick?.(0, 0);
  }

  function teardown() {
    if (handle) { try { handle.dispose(); } catch (e) {} handle = null; }
    if (document.fullscreenElement === stageEl) { try { document.exitFullscreen?.(); } catch (e) {} }
    cssFs = false; fsOn = false; walkActive = false; walkPaused = false;
  }

  $effect(() => {
    if (!open) return;
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  });

  // Escape is claimed HERE, in the capture phase, because this dialog's Escape is
  // conditional: the engine owns it during a walk, and it leaves fullscreen before it
  // closes. Modal's own Escape would otherwise close the dialog outright, and it is
  // reached first (the panel is an ancestor of whatever holds focus).
  function onEscCapture(e) {
    if (e.key !== 'Escape') return;
    e.stopPropagation();                    // Modal's panel handler must never see it
    if (walkActive) { stopWalk(); return; } // the walk's own exit, not the dialog's
    if (fsOn || cssFs) { toggleFs(); return; }
    closePreview();
  }

  let selCoords = $derived(selected
    ? [selected.x, selected.y, selected.z].map((n) => (Math.round(Number(n) * 10) / 10).toLocaleString()).join(', ')
    : '');
</script>

<svelte:window onkeydowncapture={open ? onEscCapture : undefined} />

{#if open && target}
  <Modal size="stage" title="3D preview" onClose={closePreview}>
    <header class="dhead">
      <div class="dtitle">
        <h2>{target.title || 'Base'}</h2>
        {#if phase === 'ready'}
          <p class="submeta mono">
            {fmt(counts.structural)} structural · {fmt(counts.placeables)} placeables{#if counts.pentashields} · {fmt(counts.pentashields)} Pentashields{/if}
          </p>
        {/if}
      </div>
    </header>

    {#if phase === 'ready' && hasReal}
      <div class="toolbar">
        <div class="seg" role="group" aria-label="Render mode">
          <button class="segbtn" class:on={mode === 'boxes'} type="button" disabled={switching} aria-pressed={mode === 'boxes'} onclick={() => setMode('boxes')}>Boxes</button>
          <button class="segbtn" class:on={mode === 'real'} type="button" disabled={switching} aria-pressed={mode === 'real'} onclick={() => setMode('real')}>{switching && mode !== 'real' ? 'Loading' : 'Real meshes'}</button>
        </div>
        {#if mode === 'real'}
          <div class="seg" role="group" aria-label="Material">
            <button class="segbtn" class:on={!textured} type="button" disabled={switching} aria-pressed={!textured} onclick={() => setMat(false)}>Clay</button>
            <button class="segbtn" class:on={textured} type="button" disabled={switching} aria-pressed={textured} onclick={() => setMat(true)}>Textured</button>
          </div>
          <button class="walkbtn" type="button" onclick={startWalk}>🚶 Walk inside</button>
        {/if}
        {#if realWarn}<span class="warn mono">Large base. Real meshes may be slow on mobile</span>{/if}
      </div>
    {/if}

    <div class="stage" class:cssfs={cssFs} bind:this={stageEl}>
      <canvas bind:this={canvasEl} class="canvas" class:hidden={phase !== 'ready'}></canvas>

      {#if phase === 'saver'}
        <div class="saver">
          {#if target.thumbUrl && !shotFailed}
            <img class="saver-shot" src={target.thumbUrl} alt="" decoding="async" onerror={() => (shotFailed = true)} />
          {/if}
          <div class="saver-ask">
            <p class="saver-copy">Data Saver is on. The 3D viewer downloads the piece meshes for this base, so it waits for you.</p>
            <button class="saver-btn" type="button" onclick={loadNow}>Load 3D preview</button>
          </div>
        </div>
      {:else if phase === 'loading'}
        <p class="overlay loading mono">loading 3D</p>
      {:else if phase === 'error'}
        <p class="overlay">{note}</p>
      {/if}

      {#if phase === 'ready' && breakdown.length && !walkActive}
        <div class="breakdown">
          <p class="bd-head mono">Piece breakdown</p>
          <ul class="bd-list">
            {#each breakdown.slice(0, 10) as row (row.label)}
              <li class="bd-row"><span class="bd-label">{row.label}</span><span class="bd-count mono">×{fmt(row.count)}</span></li>
            {/each}
          </ul>
        </div>
      {/if}

      {#if phase === 'ready' && !walkActive}
        <p class="hint mono">{isTouch ? 'Drag to orbit · Pinch to zoom' : 'Drag to rotate · Right-drag to pan · Scroll to zoom'}{#if mode === 'real'} · Tap a piece to select it{/if}</p>
      {/if}

      {#if phase === 'ready' && mode === 'real' && selected && !walkActive}
        <div class="selbar">
          <span class="sel-cat mono">{(selected.category || 'Piece').toUpperCase()}</span>
          <span class="sel-name">{selected.building_type || ''}</span>
          <span class="sel-meta mono">↻ {selected.rotation || 0}° · {selCoords}</span>
          <button class="sel-walk" type="button" onclick={startWalk}>Walk from here</button>
        </div>
      {/if}

      {#if walkActive}
        <div class="walkhud">
          <button class="hud-btn hud-exit" type="button" onclick={stopWalk}>✕ Exit tour</button>
          <button class="hud-btn hud-fs" type="button" onclick={toggleFs}>⛶ {fsOn || cssFs ? 'Exit fullscreen' : 'Fullscreen'}</button>
          {#if !walkPaused}<div class="cross" aria-hidden="true"></div>{/if}
          <p class="walk-hint mono">{isTouch ? 'Left pad to move · drag to look' : 'WASD to move · mouse to look · Shift to run · F for fullscreen · Esc to exit'}</p>
          {#if isTouch}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="joy" bind:this={joyEl} onpointerdown={joyDown} onpointermove={joyMove} onpointerup={joyEnd} onpointercancel={joyEnd}>
              <div class="nub" style:transform={nubT}></div>
            </div>
          {/if}
          {#if walkPaused}
            <div class="pause">
              <p class="pause-title">Tour paused</p>
              <div class="pause-row">
                <button class="hud-btn" type="button" onclick={resumeWalk}>▶ Resume walking</button>
                <button class="hud-btn" type="button" onclick={stopWalk}>Back to orbit view</button>
              </div>
            </div>
          {/if}
        </div>
      {/if}
    </div>

    <footer class="dfoot">
      {#if phase === 'ready' && note}<span class="note mono">{note}</span>{/if}
    </footer>
  </Modal>
{/if}

<style>
  /* The stage panel is viewport-sized, unlike Modal's three content widths. The
     dialog is content-tall and capped, so the header, the toolbar and the footer
     stay on screen next to the canvas instead of being pushed off a phone. */
  :global(.modal.size-stage) {
    width: min(1100px, 96vw); height: auto; max-height: 92dvh;
    display: flex; flex-direction: column;
  }
  :global(.modal.size-stage .mbody) { flex: 1 1 auto; min-height: 0; }

  .dhead { display: flex; align-items: flex-start; gap: var(--space-3); }
  .dtitle { flex: 1 1 auto; min-width: 0; }
  .dtitle h2 { margin: 0; font-size: var(--text-xl); text-transform: uppercase; letter-spacing: .04em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .submeta { margin: 2px 0 0; color: var(--text-muted); font-size: var(--text-xs); }

  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
  .seg { display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; }
  .segbtn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .08em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3); background: var(--metal-0); color: var(--text-muted);
    border: 0; border-right: 1px solid var(--edge); cursor: pointer; min-width: 5.5rem;
  }
  .seg .segbtn:last-child { border-right: 0; }
  .segbtn.on { background: var(--accent); color: var(--bg-deep); }
  .segbtn:disabled { opacity: .6; cursor: default; }
  .walkbtn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .08em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3); border-radius: var(--radius-sm);
    background: var(--metal-1); color: var(--text); border: 1px solid var(--edge); cursor: pointer;
  }
  .walkbtn:hover { border-color: var(--accent); }
  .warn { color: var(--accent-text); font-size: var(--text-xs); }

  /* A fraction of the viewport with a hard ceiling: big on a desktop, never the
     whole phone. min-height keeps it usable on a short landscape phone. */
  .stage {
    position: relative; flex: 0 0 auto;
    height: min(58dvh, 560px); min-height: 220px;
    background: var(--bg-deep); border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden;
  }
  .stage.cssfs { position: fixed; inset: 0; z-index: 80; height: auto; border-radius: 0; }
  .stage:fullscreen { height: 100%; }
  /* touch-action: none is what hands a one-finger drag to OrbitControls instead
     of scrolling the dialog under it. */
  .canvas { width: 100%; height: 100%; display: block; touch-action: none; }
  .canvas.hidden { visibility: hidden; }
  .overlay { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: var(--text-sm); }
  .overlay.loading { animation: stage-breathe 1.6s var(--ease-out) infinite; }
  @keyframes stage-breathe { 0%, 100% { opacity: .55; } 50% { opacity: 1; } }

  /* The orbit camera only moves while a pointer is down and the V1 engine has no
     autorotate, so there is no idle spin to stop here. What a reduced-motion
     reader gets is the chrome motion this component owns, held still. */
  @media (prefers-reduced-motion: reduce) {
    .overlay.loading { animation: none; opacity: 1; }
  }

  .saver { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }
  .saver-shot { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: .5; }
  .saver-ask { position: relative; display: flex; flex-direction: column; align-items: center; gap: var(--space-3); max-width: 34ch; padding: var(--space-4); text-align: center; }
  .saver-copy { margin: 0; font-size: var(--text-sm); color: var(--text); text-shadow: 0 1px 3px var(--bg-deep); }
  .saver-btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-2) var(--space-4); border-radius: var(--radius-sm);
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); cursor: pointer;
  }
  .saver-btn:hover { filter: brightness(1.08); }
  .saver-btn:focus-visible { outline: 2px solid var(--accent-bright); outline-offset: 2px; }

  .breakdown { position: absolute; top: var(--space-2); right: var(--space-2); max-width: 46%; background: color-mix(in srgb, var(--panel) 88%, transparent); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); backdrop-filter: blur(3px); }
  .bd-head { margin: 0 0 var(--space-1); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .14em; color: var(--accent); }
  .bd-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .bd-row { display: flex; justify-content: space-between; gap: var(--space-3); font-size: var(--text-xs); }
  .bd-label { color: var(--text); }
  .bd-count { color: var(--accent-text); }

  .hint { position: absolute; right: var(--space-3); bottom: var(--space-2); margin: 0; font-size: var(--text-xs); color: var(--text-muted); background: color-mix(in srgb, var(--bg-deep) 70%, transparent); padding: 2px var(--space-2); border-radius: var(--radius-sm); }

  .selbar { position: absolute; left: var(--space-2); bottom: var(--space-2); display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); max-width: 60%; background: color-mix(in srgb, var(--panel) 90%, transparent); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); }
  .sel-cat { font-size: 10px; letter-spacing: .12em; color: var(--bg-deep); background: var(--accent); border-radius: var(--radius-sm); padding: 1px var(--space-1); }
  .sel-name { font-size: var(--text-xs); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sel-meta { font-size: 10px; color: var(--text-muted); }
  .sel-walk { font-family: var(--font-mono); font-size: 10px; text-transform: uppercase; letter-spacing: .08em; padding: 1px var(--space-2); border-radius: var(--radius-sm); background: var(--metal-1); color: var(--text); border: 1px solid var(--edge); cursor: pointer; }
  .sel-walk:hover { border-color: var(--accent); }

  .walkhud { position: absolute; inset: 0; pointer-events: none; }
  .walkhud .hud-btn, .walkhud .joy, .walkhud .pause { pointer-events: auto; }
  .hud-btn { position: absolute; font-family: var(--font-mono); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .08em; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--panel) 90%, transparent); color: var(--text); border: 1px solid var(--edge); cursor: pointer; }
  .hud-btn:hover { border-color: var(--accent); }
  .hud-exit { top: var(--space-2); left: var(--space-2); }
  .hud-fs { top: var(--space-2); right: var(--space-2); }
  .cross { position: absolute; top: 50%; left: 50%; width: 14px; height: 14px; transform: translate(-50%, -50%); }
  .cross::before, .cross::after { content: ''; position: absolute; background: rgba(255,255,255,.7); }
  .cross::before { left: 50%; top: 0; width: 2px; height: 100%; transform: translateX(-50%); }
  .cross::after { top: 50%; left: 0; height: 2px; width: 100%; transform: translateY(-50%); }
  .walk-hint { position: absolute; left: 50%; bottom: var(--space-3); transform: translateX(-50%); margin: 0; font-size: var(--text-xs); color: var(--text-muted); background: color-mix(in srgb, var(--bg-deep) 70%, transparent); padding: 2px var(--space-2); border-radius: var(--radius-sm); white-space: nowrap; }
  .joy { position: absolute; left: var(--space-4); bottom: var(--space-4); width: 96px; height: 96px; border-radius: 50%; background: color-mix(in srgb, var(--panel) 60%, transparent); border: 1px solid var(--edge); touch-action: none; }
  .nub { position: absolute; top: 50%; left: 50%; width: 40px; height: 40px; margin: -20px 0 0 -20px; border-radius: 50%; background: color-mix(in srgb, var(--accent) 70%, transparent); }
  .pause { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-3); background: rgba(0,0,0,.55); }
  .pause-title { margin: 0; font-size: var(--text-xl); text-transform: uppercase; letter-spacing: .1em; color: var(--text); }
  .pause-row { position: static; display: flex; gap: var(--space-2); }
  .pause .hud-btn { position: static; }

  .dfoot { display: flex; justify-content: space-between; gap: var(--space-3); font-size: var(--text-xs); color: var(--text-muted); min-height: 1rem; }
  .note { color: var(--accent-text); }
</style>
