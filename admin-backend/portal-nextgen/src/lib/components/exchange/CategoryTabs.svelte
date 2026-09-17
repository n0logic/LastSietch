<script>
  // Category filter tabs + a listing-kind sub-toggle. Categories come from the
  // overview `tabs` (server-defined keys); we NEVER invent category keys the backend
  // would reject, so with no tabs we show only "All" and lean on search. The kind
  // toggle narrows crafted items vs blueprints (schematics) - the values the search
  // handler accepts. All amber chrome; color is never the only signal (the active
  // tab also carries an underline + bold weight).
  import { exchange, runSearch } from '$lib/exchange.svelte.js';

  const KINDS = [
    { key: 'all', label: 'All' },
    { key: 'item', label: 'Items' },
    { key: 'schematic', label: 'Blueprints' },
  ];

  // Normalize overview tabs into {key,label}. Accepts strings or objects; always
  // prepends an "All" (empty category) tab.
  let cats = $derived.by(() => {
    const raw = Array.isArray(exchange.tabs) ? exchange.tabs : [];
    const mapped = raw.map((t) =>
      typeof t === 'string' ? { key: t, label: t } : { key: t?.key ?? t?.category ?? '', label: t?.label ?? t?.name ?? t?.key ?? '' }
    ).filter((t) => t.key !== '' && t.label);
    return [{ key: '', label: 'All' }, ...mapped];
  });

  function pickCat(key) { if (key !== exchange.browse.category) runSearch({ category: key }); }
  function pickKind(key) { if (key !== exchange.browse.kind) runSearch({ kind: key }); }
</script>

<div class="cats">
  <div class="tabs" role="tablist" aria-label="Category">
    {#each cats as c (c.key)}
      <button
        role="tab" aria-selected={exchange.browse.category === c.key}
        class:on={exchange.browse.category === c.key}
        onclick={() => pickCat(c.key)}
      >{c.label}</button>
    {/each}
  </div>

  <div class="kinds" role="tablist" aria-label="Listing kind">
    {#each KINDS as k (k.key)}
      <button
        role="tab" aria-selected={exchange.browse.kind === k.key}
        class:on={exchange.browse.kind === k.key}
        onclick={() => pickKind(k.key)}
      >{k.label}</button>
    {/each}
  </div>
</div>

<style>
  .cats { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
  .tabs {
    display: inline-flex; gap: var(--space-1); overflow-x: auto; scrollbar-width: none;
    max-width: 100%;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tabs button {
    flex: 0 0 auto; background: transparent; color: var(--text-muted); border: 0;
    border-bottom: 2px solid transparent;
    padding: var(--space-2) var(--space-3); font-size: var(--text-xs); cursor: pointer;
    font-family: var(--font-mono); letter-spacing: .12em; text-transform: uppercase; white-space: nowrap;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .tabs button:hover { color: var(--text); }
  .tabs button.on { color: var(--accent-bright); border-bottom-color: var(--accent); font-weight: 700; }

  .kinds {
    display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm);
    overflow: hidden; background: var(--metal-0); flex: 0 0 auto;
  }
  .kinds button {
    background: transparent; color: var(--text-muted); border: 0;
    padding: var(--space-1) var(--space-3); font-size: var(--text-xs); cursor: pointer;
    font-family: var(--font-mono); letter-spacing: .1em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .kinds button + button { border-left: 1px solid var(--edge); }
  .kinds button:hover { color: var(--text); }
  .kinds button.on {
    color: var(--accent-bright);
    background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 18%, transparent), transparent);
    box-shadow: inset 0 -2px 0 var(--accent); font-weight: 700;
  }
</style>
