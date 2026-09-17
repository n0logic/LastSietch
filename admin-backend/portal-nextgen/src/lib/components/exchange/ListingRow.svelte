<script>
  // One market template row. Clicking the row (or Enter/Space) opens its price
  // ladder in the drawer. Shows the icon, name + best grade, the min-max price band,
  // the listing/quantity count, and the NPC / Player / Bot-pays badges. An inline
  // Sparkline sits at the end (calibrating until the history table accrues). A "..."
  // affordance opens the Add-price-alert popover (reused WatchAlert), which stops
  // propagation so it never also opens the ladder.
  //
  // All chrome is amber. Badges are TEXT (never color-only): NPC/Player/Bot read as
  // words, and the grade is a numeral, so the row survives color-blindness + day mode.
  import { openItem } from '$lib/exchange.svelte.js';
  import { clickOutside } from '$lib/actions/clickOutside.js';
  import { iconUrl } from '$lib/icons.js';
  import Sparkline from './Sparkline.svelte';
  import WatchAlert from '$lib/components/storage/WatchAlert.svelte';

  let { row, selected = false } = $props();

  let open = $state(false); // watch popover

  let name = $derived(row?.name || row?.template_id || 'Item');
  let icon = $derived(iconUrl(row?.icon));
  let minP = $derived(Number(row?.min_price) || 0);
  let maxP = $derived(Number(row?.max_price) || 0);
  let count = $derived(Number(row?.listing_count) || 0);
  let qty = $derived(Number(row?.total_qty) || 0);
  let grade = $derived(row?.max_quality != null ? Number(row.max_quality) : null);
  let botDisplay = $derived(row?.bot_buy_display || '');

  // WatchAlert expects {template, name}; the browse row uses template_id.
  let watchItem = $derived({ template: row?.template_id, name });

  function activate() { openItem(row); }
  function onKey(e) {
    // Only the row itself opens the ladder; a key on the "..." button (or anything
    // nested) must not bubble up and ALSO open the drawer.
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
  }
  function toggleWatch(e) { e.stopPropagation(); open = !open; }
</script>

<div
  class="row" class:selected role="button" tabindex="0"
  onclick={activate} onkeydown={onKey}
  aria-label="{name}, open price ladder"
>
  <img class="icon" src={icon} alt="" aria-hidden="true" loading="lazy" />

  <div class="id">
    <span class="name" title={name}>{name}</span>
    <div class="badges">
      {#if row?.has_player}<span class="badge player">Player</span>{/if}
      {#if row?.has_npc}<span class="badge npc">NPC</span>{/if}
      {#if botDisplay}<span class="badge bot" title={botDisplay}>{botDisplay}</span>{/if}
      {#if grade != null}<span class="badge grade mono">G{grade}</span>{/if}
    </div>
  </div>

  <div class="price">
    <span class="band mono">
      {minP.toLocaleString()}{#if maxP && maxP !== minP}<span class="dash">&ndash;</span>{maxP.toLocaleString()}{/if}
    </span>
    <span class="count mono">{count.toLocaleString()} listing{count === 1 ? '' : 's'}{#if qty}<span class="sep">&middot;</span>{qty.toLocaleString()} qty{/if}</span>
  </div>

  <div class="spark"><Sparkline points={[]} label="{name} price history" /></div>

  <div class="actions" use:clickOutside={() => { if (open) open = false; }}>
    <button class="more mono" type="button" aria-label="Price alert for {name}" aria-expanded={open} onclick={toggleWatch}>&hellip;</button>
    {#if open}
      <!-- Container only stops the row-open click from firing; the alert form
           inside is fully keyboard-reachable on its own. -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="pop" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
        <WatchAlert item={watchItem} onDone={() => (open = false)} />
      </div>
    {/if}
  </div>
</div>

<style>
  .row {
    display: grid; align-items: center; gap: var(--space-3);
    grid-template-columns: 34px minmax(0, 1.6fr) minmax(0, 1fr) auto auto;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi);
    cursor: pointer; transition: border-color var(--motion-fast) var(--ease-out), transform var(--motion-fast) var(--ease-out);
  }
  .row:hover { border-color: var(--accent); transform: translateY(-1px); }
  .row.selected { border-color: var(--accent); box-shadow: inset 0 1px 0 var(--metal-hi), 0 0 0 1px var(--accent-glow); }
  .icon { width: 34px; height: 34px; object-fit: contain; }

  .id { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  .name { font-size: var(--text-sm); color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .badge {
    font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
    padding: 1px 5px; border-radius: 2px; border: 1px solid var(--edge);
    color: var(--text-muted); white-space: nowrap;
  }
  .badge.player { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 45%, var(--edge)); }
  .badge.npc { color: var(--text-muted); }
  .badge.bot { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 45%, var(--edge)); text-transform: none; letter-spacing: normal; }
  .badge.grade { color: var(--text); border-color: var(--edge-hi); }

  .price { min-width: 0; display: flex; flex-direction: column; gap: 2px; text-align: right; }
  .band { font-size: var(--text-sm); font-variant-numeric: tabular-nums; color: var(--accent-text); }
  .band .dash { color: var(--text-muted); margin: 0 2px; }
  .count { font-size: var(--text-xs); color: var(--text-muted); }
  .count .sep { margin: 0 4px; }

  .spark { display: grid; place-items: center; min-width: 96px; }

  .actions { position: relative; }
  .more {
    width: 26px; height: 26px; display: grid; place-items: center; line-height: 1;
    color: var(--text-muted); background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); cursor: pointer;
  }
  .more:hover { color: var(--accent-text); border-color: var(--accent); }
  .pop {
    position: absolute; z-index: 6; top: 100%; right: 0; margin-top: 4px; min-width: 13rem;
    padding: var(--space-2); background: var(--metal-1); border: 1px solid var(--edge-hi);
    border-radius: var(--radius-sm); box-shadow: var(--shadow-overlay); cursor: default;
  }

  @media (max-width: 720px) {
    .row { grid-template-columns: 30px minmax(0, 1fr) auto auto; }
    .spark { display: none; }
  }
</style>
