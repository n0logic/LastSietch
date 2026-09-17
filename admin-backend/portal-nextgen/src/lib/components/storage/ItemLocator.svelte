<script>
  // Cross-container item locator, pinned above the right grid. Type a name; the
  // server returns matches grouped by item, each carrying the containers that hold
  // it (with per-container quantities). Clicking a container chip opens it in the
  // right panel and flags every cell of that template so the grid pulses them.
  // Read-only (never offline-gated). Debounced so a fast typist does not spam it.
  import { storage, runSearch, jumpToContainer } from '$lib/storage.svelte.js';
  import { iconUrl } from '$lib/icons.js';

  let q = $state('');
  let timer = null;

  function onInput(e) {
    q = e.target.value;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => runSearch(q), 220);
  }
  function clear() {
    q = '';
    if (timer) clearTimeout(timer);
    runSearch('');
  }
</script>

<div class="locator">
  <div class="bar">
    <input
      type="search" value={q} oninput={onInput}
      placeholder="Find an item across all containers"
      aria-label="Search items across containers"
    />
    {#if q}<button class="clear" type="button" onclick={clear} aria-label="Clear search">&times;</button>{/if}
  </div>

  {#if storage.search.status === 'loading'}
    <p class="hint mono">searching&hellip;</p>
  {:else if storage.search.status === 'ready' && storage.search.hits.length === 0 && q}
    <p class="hint">No container holds that.</p>
  {:else if storage.search.hits.length > 0}
    <ul class="hits">
      {#each storage.search.hits as hit (hit.template)}
        <li class="hit">
          <div class="hit-head">
            {#if hit.icon}<img class="hit-icon" src={iconUrl(hit.icon)} alt="" aria-hidden="true" loading="lazy" />{/if}
            <span class="hit-name">{hit.name || hit.template}</span>
            <span class="hit-qty mono">&times;{(hit.total_qty ?? 0).toLocaleString()}</span>
          </div>
          <div class="hit-locs">
            {#each hit.containers ?? [] as loc (loc.container_id)}
              <button class="loc" type="button" onclick={() => jumpToContainer(loc.container_id, hit.template)}>
                {loc.container_name || loc.container_type || loc.container_id}
                <span class="loc-qty mono">{(loc.qty ?? 0).toLocaleString()}</span>
              </button>
            {/each}
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .locator { display: flex; flex-direction: column; gap: var(--space-2); }
  .bar { position: relative; display: flex; }
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
  .hint { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .hits { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); max-height: 15rem; overflow-y: auto; }
  .hit { display: flex; flex-direction: column; gap: var(--space-1); }
  .hit-head { display: flex; align-items: center; gap: var(--space-2); }
  .hit-icon { width: 22px; height: 22px; object-fit: contain; }
  .hit-name { font-size: var(--text-sm); color: var(--text); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hit-qty { font-size: var(--text-xs); color: var(--text-muted); }
  .hit-locs { display: flex; flex-wrap: wrap; gap: var(--space-1); padding-left: calc(22px + var(--space-2)); }
  .loc {
    display: inline-flex; align-items: baseline; gap: var(--space-1);
    font-size: var(--text-xs); color: var(--text-muted);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: 2px var(--space-2); cursor: pointer;
  }
  .loc:hover { border-color: var(--accent); color: var(--accent-text); }
  .loc-qty { color: var(--text); }
</style>
