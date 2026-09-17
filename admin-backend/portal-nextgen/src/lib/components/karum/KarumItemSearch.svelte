<script>
  // Shared searchable picker. Two callers with deliberately different option shapes:
  //   KarumRequestDialog - the 1,662-entry tradeable CATALOGUE (carries tier/gradeable)
  //   KarumFillDialog    - the caller's own matching bank stacks (no tier)
  // The tier filter therefore renders only when the options actually carry tiers, which
  // is what keeps one component honest for both callers instead of forking it.
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  let {
    options = [], selected = null, onSelect, label = 'Item',
    placeholder = 'Search by item name', disabled = false, id = 'karum-item-search',
  } = $props();

  // 1,662 catalogue entries behind a 12-row cap meant a specific T6 was only reachable by
  // typing its exact name. The cap stays (a 600-row listbox helps nobody) but it is now
  // paired with a tier filter and a visible count, so a truncated result set never reads
  // as "that item does not exist".
  const LIMIT = 20;

  let query = $state('');
  let tier = $state(null);
  let open = $state(false);

  let current = $derived(options.find((option) => option.key === selected) || null);
  let tiers = $derived([...new Set(
    options.map((option) => option.tier).filter((t) => t != null)
  )].sort((a, b) => b - a));
  let scoped = $derived(tier == null ? options : options.filter((o) => o.tier === tier));
  let hits = $derived.by(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return scoped;
    return scoped.filter((option) => {
      const text = `${option.name || ''} ${option.template || ''} ${option.category || ''}`.toLowerCase();
      return text.includes(needle);
    });
  });
  let filtered = $derived(hits.slice(0, LIMIT));
  let hidden = $derived(Math.max(0, hits.length - filtered.length));

  function choose(option) {
    query = option.name || option.template || '';
    open = false;
    onSelect?.(option.key);
  }

  function toggleTier(value) {
    tier = tier === value ? null : value;
    open = true;
  }
</script>

<div class="picker">
  <label class="label" for={id}>{label}</label>
  <input
    {id} class="input" type="search" role="combobox" aria-autocomplete="list" autocomplete="off"
    {placeholder} {disabled} value={query}
    onfocus={() => (open = true)}
    oninput={(event) => { query = event.currentTarget.value; open = true; }}
    aria-expanded={open && !disabled} aria-controls={`${id}-results`}
  />

  {#if tiers.length > 1}
    <div class="tiers" role="group" aria-label="Filter by tier">
      <button class="tier" class:on={tier == null} type="button" disabled={disabled}
              aria-pressed={tier == null} onclick={() => toggleTier(null)}>All</button>
      {#each tiers as value (value)}
        <button class="tier" class:on={tier === value} type="button" disabled={disabled}
                aria-pressed={tier === value} onclick={() => toggleTier(value)}>T{value}</button>
      {/each}
    </div>
  {/if}

  {#if current}
    <div class="selected" aria-live="polite">
      <span class="selected-name">{current.name}</span>
      <span class="selected-detail mono">{current.detail || current.template}</span>
      <button class="clear" type="button" onclick={() => { query = ''; onSelect?.(null); open = true; }}
              disabled={disabled}>Change</button>
    </div>
  {/if}

  {#if open && !disabled}
    <div id={`${id}-results`} class="results" role="listbox" aria-label={`${label} results`}>
      {#if filtered.length === 0}
        <SealedPanel
          status="empty" action="none" art="nothing-found"
          emptyText="No matching item."
        />
      {:else}
        {#each filtered as option (option.key)}
          <button class="result" class:on={option.key === selected} type="button"
                  role="option" aria-selected={option.key === selected}
                  onmousedown={(event) => event.preventDefault()} onclick={() => choose(option)}>
            <span class="result-name">{option.name || option.template}</span>
            <span class="result-meta mono">
              {option.detail || option.template}
              {#if option.category}<span>&middot; {option.category}</span>{/if}
            </span>
          </button>
        {/each}
        {#if hidden > 0}
          <p class="more mono" aria-live="polite">
            {hidden.toLocaleString()} more match{hidden === 1 ? '' : 'es'}. Keep typing{tiers.length > 1 ? ', or pick a tier' : ''} to narrow it down.
          </p>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<style>
  .picker { position: relative; display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .label { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .input {
    width: 100%; box-sizing: border-box; font-family: var(--font-sans); font-size: var(--text-sm);
    color: var(--text); background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-2);
  }
  .input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .input:disabled { opacity: .5; }
  .tiers { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .tier {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
    padding: 2px var(--space-2); border-radius: var(--radius-sm); cursor: pointer;
    color: var(--text-muted); background: var(--metal-0); border: 1px solid var(--edge);
  }
  .tier:hover:not(:disabled) { border-color: var(--edge-hi); color: var(--text); }
  .tier.on { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .tier:disabled { opacity: .5; cursor: not-allowed; }
  .selected {
    display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center;
    gap: var(--space-2); padding: var(--space-1) var(--space-2);
    background: var(--bg-deep); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .selected-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--text-xs); color: var(--text); }
  .selected-detail, .result-meta { font-size: 10px; color: var(--text-muted); }
  .clear { font: inherit; font-size: 10px; color: var(--accent-text); background: none; border: 0; cursor: pointer; text-transform: uppercase; letter-spacing: .06em; }
  .results {
    position: absolute; z-index: 2; top: calc(100% + var(--space-1)); left: 0; right: 0;
    max-height: 18rem; overflow: auto; background: var(--metal-0);
    border: 1px solid var(--edge-hi); border-radius: var(--radius-sm);
    box-shadow: 0 16px 34px -22px var(--shadow-cast);
  }
  .result {
    width: 100%; display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
    padding: var(--space-2); color: var(--text); background: transparent; border: 0;
    border-bottom: 1px solid var(--edge); text-align: left; cursor: pointer;
  }
  .result:last-child { border-bottom: 0; }
  .result:hover, .result.on { background: var(--metal-1); }
  .result:active { transform: translateY(1px); }
  .result-name { font-size: var(--text-sm); }
  .more {
    margin: 0; padding: var(--space-3); color: var(--text-muted);
    border-top: 1px solid var(--edge); background: var(--bg-deep);
    font-size: 10px; line-height: 1.4;
  }
  @media (max-width: 40rem) {
    .selected { grid-template-columns: minmax(0, 1fr) auto; }
    .selected-detail { grid-column: 1 / -1; }
  }
</style>
