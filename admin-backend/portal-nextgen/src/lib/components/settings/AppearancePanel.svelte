<script>
  // Day/night and house colours, moved off the topbar. The keys are the shipped
  // ones: `ls-theme` and `ls-house` are what the no-FOUC boot script in
  // app.html reads before first paint, so renaming either here would leave the
  // player's choice applied on this page and gone on the next load. The
  // attributes go on <html> for the same reason app.html sets them there.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import { motion, loadMotion, setMotion } from '$lib/ambient.svelte.js';

  const HOUSES = ['none', 'atreides', 'harkonnen', 'corrino', 'fremen'];

  let theme = $state('night');
  let house = $state('none');

  onMount(() => {
    loadMotion();
    const el = document.documentElement;
    theme = el.getAttribute('data-theme') || 'night';
    house = el.getAttribute('data-house') || 'none';
  });

  function applyTheme(t) {
    theme = t;
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('ls-theme', t); } catch (e) {}
  }
  function applyHouse(h) {
    house = h;
    document.documentElement.setAttribute('data-house', h);
    try { localStorage.setItem('ls-house', h); } catch (e) {}
  }
</script>

<CarvedSlab>
  <p class="kicker mono">Appearance | lamp and banner</p>

  <div class="controls">
    <div class="field">
      <span class="lbl mono">Mode</span>
      <button
        class="mode"
        type="button"
        onclick={() => applyTheme(theme === 'night' ? 'day' : 'night')}
        aria-label="Toggle day/night mode"
      >{theme === 'night' ? 'Night' : 'Day'}</button>
    </div>
    <label class="field">
      <span class="lbl mono">House colours</span>
      <select bind:value={house} onchange={() => applyHouse(house)} aria-label="House colors">
        {#each HOUSES as h}
          <option value={h}>{h === 'none' ? 'Last Sietch' : h.charAt(0).toUpperCase() + h.slice(1)}</option>
        {/each}
      </select>
    </label>
  </div>

  <label class="field motion-field">
    <span class="lbl mono">Ambient motion</span>
    <select value={motion.preference} onchange={(event) => setMotion(event.target.value)}>
      <option value="auto">Automatic: still images on phones</option>
      <option value="still">Still images</option>
      <option value="full">Ambient video</option>
    </select>
  </label>
  <p class="explain">Reduced motion and data saver always use still images.</p>

  <p class="explain">Both are kept in this browser only. Sign in from another device and it starts on the sietch default.</p>
</CarvedSlab>

<style>
  .controls { display: flex; flex-wrap: wrap; gap: var(--space-3); margin-top: var(--space-3); }
  .motion-field { margin-top: var(--space-4); }
  .field { display: flex; flex-direction: column; gap: var(--space-1); min-width: 10rem; flex: 1 1 10rem; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .mode, .field select {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
  }
  .mode { cursor: pointer; text-align: left; }
  .mode:hover { border-color: var(--accent); }
  .explain { margin: var(--space-3) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
</style>
