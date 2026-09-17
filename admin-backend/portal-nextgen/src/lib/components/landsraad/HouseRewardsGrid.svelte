<script>
  // House rewards grid: the 25-house own-rewards board (rewards half of the
  // overview). Every house gets a tile; houses with pending rewards are lit with
  // a count badge and expand to show their item lines (swatch chips inline on
  // placeable-dye rewards). Empty houses render dimmed (faithful to the in-game
  // board). Read-only; clicking a house with rewards toggles its detail.
  import { houseCrestUrl } from '$lib/icons.js';
  import { landsraad, selectHouse } from '$lib/landsraad.svelte.js';
  import SwatchChips from './SwatchChips.svelte';

  let { houses = [] } = $props();

  function onKey(e, h) {
    if (!h.has_rewards) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectHouse(h); }
  }

  // Per-house swatch state: waiting (pending now) beats collected (fully
  // withdrawn at some point, all-time history; the reader keeps no time
  // window) beats none (this house never had one). A house can carry both a
  // pending flavor and a collected one at once (e.g. Placeables collected,
  // Stillsuit still waiting) since each flavor is its own row; waiting wins
  // the tile-level state because it's the actionable fact.
  function isSwatchName(name) {
    return typeof name === 'string' && name.toLowerCase().includes('swatch');
  }
  function swatchInfo(h) {
    const pending = (h.items || []).filter((it) => isSwatchName(it.name));
    if (pending.length) return { state: 'waiting', count: pending.length };
    const collected = (h.claimed || []).filter((c) => isSwatchName(c.name));
    if (collected.length) return { state: 'collected', count: collected.length };
    return { state: 'none', count: 0 };
  }
  function swatchLabel(sw) {
    const noun = sw.count > 1 ? 'Swatches' : 'Swatch';
    const verb = sw.state === 'waiting' ? 'waiting' : 'collected';
    return sw.count > 1 ? `${sw.count} ${noun} ${verb}` : `${noun} ${verb}`;
  }
</script>

<div class="hgrid-wrap">
  <p class="panel-kicker mono">House rewards board</p>
  <div class="hgrid" role="list">
    {#each houses as h (h.raw)}
      {@const crest = houseCrestUrl(h.crest)}
      {@const open = landsraad.selectedHouse === h}
      {@const sw = swatchInfo(h)}
      <div class="house" role="listitem" class:has={h.has_rewards} class:open>
        <button
          class="hbtn" type="button"
          disabled={!h.has_rewards}
          aria-expanded={h.has_rewards ? open : undefined}
          onclick={() => h.has_rewards && selectHouse(h)}
          onkeydown={(e) => onKey(e, h)}
          aria-label={(h.has_rewards ? `${h.name}, ${h.reward_count} reward lines, toggle detail` : `${h.name}, no rewards`) + (sw.state !== 'none' ? `, ${swatchLabel(sw).toLowerCase()}` : '')}
        >
          <div class="crest-wrap">
            {#if crest}
              <img class="crest" src={crest} alt="" aria-hidden="true" loading="lazy" />
            {:else}
              <span class="monogram mono" aria-hidden="true">{h.monogram || h.short?.slice(0, 2) || '??'}</span>
            {/if}
          </div>
          <span class="hname">{h.short || h.name}</span>
          {#if h.has_rewards}
            <span class="badge mono">{h.reward_count}</span>
          {/if}
        </button>

        {#if sw.state !== 'none'}
          <div class="swatch-tag {sw.state}">
            <span class="g" aria-hidden="true">{sw.state === 'waiting' ? '!' : '✓'}</span>
            <span class="t">{swatchLabel(sw)}</span>
          </div>
        {/if}

        {#if open && h.has_rewards}
          <div class="detail">
            {#if h.rep_location}
              <p class="d-rep">
                <span class="d-rep-lbl mono">Rep location</span>
                <span class="d-rep-val">{h.rep_location}</span>
              </p>
            {/if}
            {#if h.solari_display}
              <p class="d-solari mono">{h.solari_display} Solari</p>
            {/if}
            <ul class="items" role="list">
              {#each h.items as it, i (i)}
                <li class="item">
                  <span class="i-name">{it.amount ? `${Number(it.amount).toLocaleString()} ` : ''}{it.name}</span>
                  {#if it.swatch}<SwatchChips swatch={it.swatch} />{/if}
                </li>
              {/each}
            </ul>
          </div>
        {/if}
      </div>
    {/each}
  </div>
</div>

<style>
  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .hgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: var(--space-2); align-items: start; }
  .house { border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--metal-0); overflow: hidden; }
  .house:not(.has) { opacity: .5; }
  .house.has:hover { border-color: var(--edge-hi); }
  .house.open { border-color: var(--accent); grid-column: 1 / -1; }
  .hbtn {
    display: flex; align-items: center; gap: var(--space-2); width: 100%;
    padding: var(--space-2) var(--space-3); background: none; border: 0; color: inherit; text-align: left;
    cursor: pointer;
  }
  .hbtn:disabled { cursor: default; }
  .hbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  .crest-wrap { width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }
  .crest { max-width: 100%; max-height: 100%; object-fit: contain; }
  .monogram { font-size: var(--text-sm); font-weight: 700; color: var(--text-muted); }
  .hname { flex: 1 1 auto; font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge { flex: 0 0 auto; font-size: var(--text-xs); color: var(--bg-deep); background: var(--accent); border-radius: 999px; padding: 0 var(--space-2); min-width: 1.4rem; text-align: center; }
  .swatch-tag {
    display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
    padding: 0 var(--space-3) var(--space-2);
    font-size: 10px; letter-spacing: .04em; text-transform: uppercase;
  }
  .swatch-tag .g { font-size: var(--text-xs); line-height: 1; }
  .swatch-tag .t { color: var(--text-muted); }
  /* Waiting: a swatch is sitting at the rep right now (actionable, matches the
     reward-count badge's accent). Collected: fully withdrawn at some point
     (history, not live data -- never Ibad blue). Distinguished by glyph + label
     + color together, never color alone. */
  .swatch-tag.waiting .g, .swatch-tag.waiting .t { color: var(--accent-text); font-weight: 700; }
  .swatch-tag.collected .g, .swatch-tag.collected .t { color: var(--ls-green); }
  .detail { padding: 0 var(--space-3) var(--space-3); border-top: 1px solid var(--edge); }
  .d-rep { margin: var(--space-2) 0 0; display: flex; flex-direction: column; gap: 2px; }
  .d-rep-lbl { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); }
  .d-rep-val { font-size: var(--text-sm); color: var(--text); line-height: 1.45; }
  .d-solari { margin: var(--space-2) 0; font-size: var(--text-sm); color: var(--accent-text); }
  .items { list-style: none; margin: var(--space-2) 0 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .item { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; font-size: var(--text-sm); color: var(--text); }
</style>
