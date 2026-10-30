<script>
  // 3D gear stage: the worn garments on a turntable, in their in-game colours.
  // Redesign only (the route renders it behind the shell switch). Nothing heavy
  // loads until the player opens it: three.js, the meshes, the layer data and
  // the dye table are all fetched on that click. Meshes and layer data are served
  // to linked sessions only. Weapons are never shown.
  //
  // The body comes from equipped.body_type (1 = male, 2 = female, inferred);
  // absent means the female body, which most baked sets have.
  import { onDestroy, tick } from 'svelte';
  import { getJSON } from '$lib/api.js';
  import { bodyFor, stagePlan } from '$lib/gearColor.js';

  let { equipped = null } = $props();

  // Same key as the Classic viewer: the table has not changed since it shipped.
  const LUT_URL = '/admin/static/data/swatch-lut.json?v=20261030g';

  let worn = $derived(Array.isArray(equipped?.items) ? equipped.items.filter((it) => it.source !== 'hotbar') : []);
  let bodyType = $derived(equipped?.body_type ?? null);

  let status = $state('closed'); // closed | loading | ready | empty | error
  let dyed = $state(true);
  let shown = $state(0);
  let canvas = $state(null);
  let handle = null;

  function reducedMotion() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; }
  }

  async function open() {
    status = 'loading';
    try {
      const [manifest, layers, lut, mod] = await Promise.all([
        getJSON('/portal/gear/glb/manifest.json'),
        getJSON('/portal/gear/layers'),
        getJSON(LUT_URL).catch(() => null),
        import('./gearStage.js'),
      ]);
      const plan = stagePlan(worn, manifest, bodyType);
      shown = plan.reduce((n, p) => n + p.materials.length, 0);
      if (!plan.length) { status = 'empty'; return; }
      status = 'ready';
      await tick();
      handle = await mod.mountStage(canvas, plan, { layers, lut }, { reducedMotion: reducedMotion() });
      handle.setDyed(dyed);
    } catch (e) {
      status = 'error';
    }
  }

  function toggleDyes() {
    dyed = !dyed;
    handle?.setDyed(dyed);
  }

  onDestroy(() => { handle?.dispose(); handle = null; });
</script>

<div class="stage">
  <div class="head">
    <p class="panel-kicker mono">Gear stage</p>
    {#if status === 'ready'}
      <button class="chip" type="button" aria-pressed={dyed} onclick={toggleDyes}>{dyed ? 'Dyes on' : 'Undyed'}</button>
    {/if}
  </div>

  {#if worn.length === 0}
    <p class="hollow">Nothing worn to show.</p>
  {:else if status === 'closed'}
    <p class="lede">See your worn armour in 3D, in the colours your dyes give it in game.</p>
    <button class="open" type="button" onclick={open}>Open 3D view</button>
  {:else if status === 'empty'}
    <p class="hollow">None of your worn pieces has a 3D model yet.</p>
  {:else if status === 'error'}
    <p class="hollow">The 3D view could not load right now. Try again in a moment.</p>
  {:else}
    <div class="frame" aria-busy={status === 'loading'}>
      <canvas bind:this={canvas} class="canvas" aria-label="Your worn gear in 3D. Drag to turn it."></canvas>
      {#if status === 'loading'}<p class="loading mono">loading</p>{/if}
    </div>
    <p class="note">
      {shown} {shown === 1 ? 'piece' : 'pieces'} on the {bodyFor(bodyType)} body{#if bodyType == null}, the default until your body type is read{/if}. Drag to turn.
    </p>
  {/if}
</div>

<style>
  .stage { display: flex; flex-direction: column; gap: var(--space-2); }
  .head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .lede, .hollow, .note { margin: 0; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; }
  .note { font-size: var(--text-xs); }
  .open, .chip {
    align-self: flex-start; background: transparent; color: var(--accent-text);
    border: 1px solid var(--border-strong, var(--accent)); border-radius: var(--radius-sm, 4px);
    font: inherit; font-size: var(--text-sm); padding: var(--space-2) var(--space-3); cursor: pointer;
    min-height: 44px;
  }
  .chip { font-size: var(--text-xs); min-height: 36px; padding: var(--space-1) var(--space-3); text-transform: uppercase; letter-spacing: .08em; }
  .open:hover, .chip:hover { border-color: var(--accent); color: var(--accent-bright, var(--accent)); }
  .open:focus-visible, .chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .frame {
    position: relative; width: 100%; aspect-ratio: 3 / 4; max-height: 70vh;
    background: radial-gradient(ellipse at 50% 35%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 70%);
    border-radius: var(--radius-sm, 4px); overflow: hidden;
  }
  .canvas { display: block; width: 100%; height: 100%; }
  @media (max-width: 600px) { .frame { aspect-ratio: 4 / 5; max-height: 60vh; } }
  .loading { position: absolute; inset: 0; display: grid; place-items: center; margin: 0; color: var(--text-muted); font-size: var(--text-xs); letter-spacing: .2em; text-transform: uppercase; }
</style>
