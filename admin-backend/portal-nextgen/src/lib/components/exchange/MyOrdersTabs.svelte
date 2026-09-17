<script>
  // My listings, three buckets behind a segmented control: Active / Completed /
  // History. Active rows can Cancel + Relist; the others are read-only. NO expiry
  // countdown exists in the data, so none is shown (an honest cut, per contract).
  import { exchange, setOrdersTab } from '$lib/exchange.svelte.js';
  import OrderRow from './OrderRow.svelte';

  const TABS = [
    { key: 'active', label: 'Active' },
    { key: 'completed', label: 'Completed' },
    { key: 'history', label: 'History' },
  ];

  let tab = $derived(exchange.orders.tab);
  let list = $derived(exchange.orders[tab] || []);
  let status = $derived(exchange.orders.status);
  function count(key) { return (exchange.orders[key] || []).length; }
</script>

<div class="orders">
  <div class="head">
    <p class="kicker mono">My listings</p>
    <div class="seg" role="tablist" aria-label="My listings filter">
      {#each TABS as t (t.key)}
        <button
          role="tab" aria-selected={tab === t.key}
          class:on={tab === t.key}
          onclick={() => setOrdersTab(t.key)}
        >{t.label}<span class="c mono">{count(t.key)}</span></button>
      {/each}
    </div>
  </div>

  {#if status === 'loading'}
    <p class="hint mono">reading your listings&hellip;</p>
  {:else if status === 'error'}
    <p class="hint">Could not read your listings.</p>
  {:else if list.length === 0}
    <p class="hint">
      {#if tab === 'active'}You have nothing listed right now. List an item from Storage.
      {:else if tab === 'completed'}No completed sales yet.
      {:else}No closed listings yet.{/if}
    </p>
  {:else}
    <div class="rows">
      {#each list as o (o.order_id)}
        <OrderRow order={o} bucket={tab} />
      {/each}
    </div>
  {/if}
</div>

<style>
  .orders { display: flex; flex-direction: column; gap: var(--space-3); }
  .head { display: flex; flex-direction: column; gap: var(--space-2); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .2em; font-size: var(--text-xs); }
  .seg { display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; background: var(--metal-0); align-self: flex-start; }
  .seg button {
    display: inline-flex; align-items: center; gap: var(--space-1);
    background: transparent; color: var(--text-muted); border: 0;
    padding: var(--space-1) var(--space-3); font-size: var(--text-xs); cursor: pointer;
    font-family: var(--font-mono); letter-spacing: .1em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .seg button + button { border-left: 1px solid var(--edge); }
  .seg button .c { font-size: 10px; color: var(--text-muted); }
  .seg button:hover { color: var(--text); }
  .seg button.on { color: var(--accent-bright); background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 18%, transparent), transparent); box-shadow: inset 0 -2px 0 var(--accent); font-weight: 700; }
  .seg button.on .c { color: var(--accent-text); }
  .hint { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; }
  .rows { display: flex; flex-direction: column; gap: var(--space-1); max-height: 24rem; overflow-y: auto; }
</style>
