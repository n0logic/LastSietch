<script>
  // The browse controls above the listing table: a debounced name search + a sort
  // select. Read-only (never gated). Both feed the shared store's runSearch, which
  // resets to page 0 and guards stale responses. Pure chrome, so amber throughout.
  import { onDestroy } from 'svelte';
  import { exchange, runSearch } from '$lib/exchange.svelte.js';

  // Sort keys the /portal/market/v2/search handler actually accepts.
  const SORTS = [
    { key: 'active', label: 'Most listed' },
    { key: 'price', label: 'Cheapest' },
    { key: 'expensive', label: 'Most expensive' },
    { key: 'name', label: 'A-Z' },
  ];

  let q = $state(exchange.browse.q);
  let timer = null;

  function onInput(e) {
    q = e.target.value;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => runSearch({ q: q.trim() }), 240);
  }

  onDestroy(() => { if (timer) clearTimeout(timer); });
  function clear() {
    q = '';
    if (timer) clearTimeout(timer);
    runSearch({ q: '' });
  }
  function onSort(e) { runSearch({ sort: e.target.value }); }
</script>

<div class="browse">
  <div class="bar">
    <input
      type="search" value={q} oninput={onInput}
      placeholder="Search the CHOAM Exchange"
      aria-label="Search market listings by name"
    />
    {#if q}<button class="clear" type="button" onclick={clear} aria-label="Clear search">&times;</button>{/if}
  </div>
  <label class="sort">
    <span class="mono">Sort</span>
    <select value={exchange.browse.sort} onchange={onSort} aria-label="Sort listings">
      {#each SORTS as s (s.key)}<option value={s.key}>{s.label}</option>{/each}
    </select>
  </label>
</div>

<style>
  .browse { display: flex; align-items: flex-end; gap: var(--space-3); flex-wrap: wrap; }
  .bar { position: relative; flex: 1; min-width: 12rem; display: flex; }
  .bar input {
    flex: 1; font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-6) var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .bar input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .clear {
    position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
    width: 20px; height: 20px; display: grid; place-items: center; line-height: 1;
    color: var(--text-muted); background: transparent; border: 0; cursor: pointer; font-size: var(--text-lg);
  }
  .clear:hover { color: var(--accent-text); }
  .sort { display: inline-flex; flex-direction: column; gap: var(--space-1); }
  .sort span { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em; color: var(--text-muted); }
  .sort select {
    font-family: var(--font-mono); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3);
  }
  .sort select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
