<script>
  // Day/night and house colours, moved off the topbar. The keys are the shipped
  // ones: `ls-theme` and `ls-house` are what the no-FOUC boot script in
  // app.html reads before first paint, so renaming either here would leave the
  // player's choice applied on this page and gone on the next load. The
  // attributes go on <html> for the same reason app.html sets them there.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import { motion, loadMotion, setMotion } from '$lib/ambient.svelte.js';
  import { setPref } from '$lib/prefs.svelte.js';
  import { readingFontOn, applyReadingFont, readingSize, applyReadingSize } from '$lib/readingFont.js';
  import CosmeticsPicker from '$lib/components/settings/CosmeticsPicker.svelte';

  const HOUSES = ['none', 'atreides', 'harkonnen', 'corrino', 'fremen'];

  let theme = $state('night');
  let house = $state('none');
  let readingFont = $state(false);
  let readingSz = $state('m');

  const SIZES = [
    { value: 's', label: 'Small' },
    { value: 'm', label: 'Medium' },
    { value: 'l', label: 'Large' },
  ];

  onMount(() => {
    loadMotion();
    const el = document.documentElement;
    theme = el.getAttribute('data-theme') || 'night';
    house = el.getAttribute('data-house') || 'none';
    readingFont = readingFontOn();
    readingSz = readingSize();
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
  // Device first (no reload, no flash next time), then the account copy.
  function toggleReadingFont() {
    readingFont = !readingFont;
    applyReadingFont(readingFont);
    setPref('reading_font', readingFont);
  }
  function pickReadingSize(size) {
    readingSz = size;
    applyReadingSize(size);
    setPref('reading_size', size);
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

  <div class="field reading-field">
    <span class="lbl mono">Reading font</span>
    <button
      class="mode switch"
      type="button"
      role="switch"
      aria-checked={readingFont}
      onclick={toggleReadingFont}
    >Easier reading font <span class="state mono" aria-hidden="true">{readingFont ? 'On' : 'Off'}</span></button>
  </div>
  {#if readingFont}
    <fieldset class="field size-field">
      <legend class="lbl mono">Reading size</legend>
      <div class="seg">
        {#each SIZES as sz}
          <label class="seg-opt" class:on={readingSz === sz.value}>
            <input
              type="radio"
              name="reading-size"
              value={sz.value}
              checked={readingSz === sz.value}
              onchange={() => pickReadingSize(sz.value)}
            />{sz.label}
          </label>
        {/each}
      </div>
    </fieldset>
  {/if}
  <p class="explain">Uses OpenDyslexic for body text, in Small, Medium or Large. Headings and numbers stay the same. Saved on this device and on your account.</p>

  <p class="explain">Mode, house colours and motion are kept in this browser only. Sign in from another device and they start on the sietch default.</p>

  <CosmeticsPicker />
</CarvedSlab>

<style>
  .controls { display: flex; flex-wrap: wrap; gap: var(--space-3); margin-top: var(--space-3); }
  .motion-field, .reading-field { margin-top: var(--space-4); }
  .field { display: flex; flex-direction: column; gap: var(--space-1); min-width: 10rem; flex: 1 1 10rem; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .mode, .field select {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
  }
  /* The wide reading font makes the longest option wider than a phone; the select
     truncates instead of pushing the card off screen. */
  .field select { width: 100%; }
  .mode { cursor: pointer; text-align: left; }
  .mode:hover { border-color: var(--accent); }
  .switch { display: flex; justify-content: space-between; align-items: center; gap: var(--space-2); }
  .state { font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; color: var(--text-muted); }
  .switch[aria-checked='true'] { border-color: var(--accent); }
  .switch[aria-checked='true'] .state { color: var(--accent-bright); }
  .size-field { margin: var(--space-3) 0 0; padding: 0; border: 0; }
  .size-field .lbl { padding: 0; margin-bottom: var(--space-1); }
  .seg { display: flex; border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; }
  .seg-opt {
    flex: 1 1 0; position: relative; text-align: center; cursor: pointer;
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text-muted);
    background: var(--metal-1); padding: var(--space-1) var(--space-2);
  }
  .seg-opt + .seg-opt { border-left: 1px solid var(--edge); }
  .seg-opt input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
  .seg-opt.on { color: var(--accent-bright); box-shadow: inset 0 0 0 1px var(--accent); }
  .seg-opt:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
  .explain { margin: var(--space-3) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
</style>
