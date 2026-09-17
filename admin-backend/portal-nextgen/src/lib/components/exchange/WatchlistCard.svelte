<script>
  // Your armed price WATCHES (the targets you set). What has actually fired lives
  // in AlertsFeed; conflating the two is the mislabel this card used to carry.
  // Each row shows the item + the target price you set; a
  // watch whose live market min has crossed AT/BELOW that target is lit Ibad-blue (a
  // value moving now = the alert firing), otherwise amber. Adding new alerts happens
  // from a ListingRow's "..." (reused WatchAlert); this card lists + removes them.
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import { exchange, removeWatch } from '$lib/exchange.svelte.js';
  import { iconUrl } from '$lib/icons.js';

  const STALE_MS = 60000;

  let watches = $derived(exchange.watches || []);

  // Same stale clock as the pulse heartbeat: once the last live sync is > 60s old,
  // a "crossed" alert is no longer verifiably live and must not light Ibad.
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 1000);
    return () => clearInterval(id);
  });
  let stale = $derived(!exchange.pulse.syncedAt || (now - exchange.pulse.syncedAt) > STALE_MS);

  function crossed(w) {
    if (stale) return false;
    const min = Number(w?.min_price);
    const tgt = Number(w?.max_price);
    return Number.isFinite(min) && Number.isFinite(tgt) && min > 0 && min <= tgt;
  }
</script>

<div class="watchcard">
  <p class="kicker mono">Price watches</p>
  {#if watches.length === 0}
    <SealedPanel
      status="empty" action="none" art="no-alerts"
      emptyText="No watches yet. Open an item and use the … menu to watch a price."
    />
  {:else}
    <ul class="rows">
      {#each watches as w (w.template_id)}
        {@const fired = crossed(w)}
        <li class="row" class:fired>
          <img class="icon" src={iconUrl(w.icon)} alt="" aria-hidden="true" loading="lazy" />
          <span class="name" title={w.name}>{w.name || w.template_id}</span>
          <span class="tgt mono">
            {#if fired}<span class="fire mono">now</span>{/if}
            &le;{Number(w.max_price).toLocaleString()}
          </span>
          <button class="rm" type="button" onclick={() => removeWatch(w)} aria-label="Remove price watch for {w.name || w.template_id}">&times;</button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .watchcard { display: flex; flex-direction: column; gap: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .2em; font-size: var(--text-xs); }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .row {
    display: grid; align-items: center; gap: var(--space-2);
    grid-template-columns: 26px minmax(0, 1fr) auto auto;
    padding: var(--space-1) var(--space-2);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--metal-0);
  }
  .icon { width: 26px; height: 26px; object-fit: contain; }
  .name { min-width: 0; font-size: var(--text-sm); color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tgt { font-size: var(--text-xs); color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; display: inline-flex; align-items: center; gap: 4px; }
  /* Fired alert: the live crossing is the moment that earns Ibad. */
  .row.fired { border-color: color-mix(in srgb, var(--ls-ibad) 45%, var(--edge)); }
  .row.fired .tgt { color: var(--ls-ibad); }
  .fire { font-size: 9px; text-transform: uppercase; letter-spacing: .1em; color: var(--bg-deep); background: var(--ls-ibad); border-radius: 2px; padding: 0 3px; }
  .rm { width: 22px; height: 22px; display: grid; place-items: center; line-height: 1; color: var(--text-muted); background: transparent; border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer; font-size: var(--text-base); }
  .rm:hover { color: var(--ls-red); border-color: var(--ls-red); }
</style>
