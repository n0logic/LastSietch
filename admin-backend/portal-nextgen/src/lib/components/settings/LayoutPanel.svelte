<script>
  // Account preferences that change how a page is ARRANGED rather than how it
  // looks: the Storage grid/list pick and the map layer defaults saved per
  // character. Unlike Appearance next door, none of this is browser-local --
  // the prefs store owns one JSON document per scope on the server, so the
  // choice follows the player to any browser they sign in from.
  //
  // The map rows read the RAW scope documents (prefs.identity / prefs.scopes),
  // never getPref: getPref falls back to the identity doc, which would print
  // one identity-wide default under every character as if each had saved it.
  import { auth } from '$lib/auth.svelte.js';
  import { prefs, getPref, setPref } from '$lib/prefs.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';

  const MAP_KEYS = ['map_hidden', 'map_viewer'];
  const VIEWER_LABEL = { carved: 'Board', holo: 'Holo table' };

  function docFor(scope) {
    const doc = scope ? prefs.scopes[scope] : prefs.identity;
    return doc && typeof doc === 'object' ? doc : {};
  }

  function objOf(doc, key) {
    const v = doc[key];
    return v && typeof v === 'object' ? v : {};
  }

  // LayerTree writes `false` for a layer that is on, so the count is of truthy
  // values, not of keys.
  function hiddenCount(v) {
    return v && typeof v === 'object' ? Object.values(v).filter(Boolean).length : 0;
  }

  function rowsFor(scope, who) {
    const doc = docFor(scope);
    const hidden = objOf(doc, 'map_hidden');
    const viewer = objOf(doc, 'map_viewer');
    const keys = [...new Set([...Object.keys(hidden), ...Object.keys(viewer)])].sort();
    return keys.map((mapKey) => ({
      scope,
      who,
      mapKey,
      hidden: hiddenCount(hidden[mapKey]),
      viewer: VIEWER_LABEL[viewer[mapKey]] || (typeof viewer[mapKey] === 'string' ? viewer[mapKey] : ''),
    }));
  }

  const storageView = $derived(getPref('storage_view'));
  const rows = $derived([
    ...rowsFor('', 'All characters'),
    ...(auth.characters || []).flatMap((c) =>
      rowsFor(`char:${c.controller_id}`, c.char_name || `Character ${c.controller_id}`)),
  ]);

  function clearMap(scope, mapKey) {
    for (const key of MAP_KEYS) {
      const cur = docFor(scope)[key];
      if (!cur || typeof cur !== 'object' || !(mapKey in cur)) continue;
      const { [mapKey]: _drop, ...rest } = cur;
      setPref(key, Object.keys(rest).length ? rest : null, { scope });
    }
  }
</script>

<CarvedSlab>
  <p class="kicker mono">Layout | shelf and board</p>

  <div class="controls">
    <div class="field">
      <span class="lbl mono">Storage view</span>
      <div class="seg" role="group" aria-label="Storage view">
        <button
          class="seg-btn mono"
          class:on={storageView === 'grid'}
          type="button"
          aria-pressed={storageView === 'grid'}
          onclick={() => setPref('storage_view', 'grid')}
        >Grid</button>
        <button
          class="seg-btn mono"
          class:on={storageView === 'list'}
          type="button"
          aria-pressed={storageView === 'list'}
          onclick={() => setPref('storage_view', 'list')}
        >List</button>
      </div>
      <p class="explain">Saved on your account. It follows you to any browser.</p>
    </div>
  </div>

  <div class="maps">
    <span class="lbl mono">Map defaults</span>
    {#if rows.length === 0}
      <p class="explain">No map defaults saved yet. Save a layer preset on any map and it appears here.</p>
    {:else}
      <ul class="list">
        {#each rows as row (`${row.scope}|${row.mapKey}`)}
          <li class="entry">
            <span class="who">{row.who}</span>
            <span class="board mono">{row.mapKey}</span>
            <span class="detail">
              {row.hidden} {row.hidden === 1 ? 'layer' : 'layers'} hidden{row.viewer ? ` · ${row.viewer}` : ''}
            </span>
            <button
              class="clear mono"
              type="button"
              aria-label="Clear {row.mapKey} default for {row.who}"
              onclick={() => clearMap(row.scope, row.mapKey)}
            >Clear</button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</CarvedSlab>

<style>
  .controls { display: flex; flex-wrap: wrap; gap: var(--space-3); margin-top: var(--space-3); }
  .field { display: flex; flex-direction: column; gap: var(--space-1); min-width: 12rem; flex: 1 1 12rem; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .seg {
    align-self: start; display: inline-flex; overflow: hidden;
    border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--metal-0);
  }
  .seg-btn {
    background: transparent; border: 0; cursor: pointer;
    color: var(--text-muted); padding: var(--space-2) var(--space-3);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .seg-btn + .seg-btn { border-left: 1px solid var(--edge); }
  .seg-btn:hover { color: var(--text); }
  .seg-btn.on { color: var(--accent-bright); background: color-mix(in srgb, var(--accent) 12%, transparent); }

  .maps { margin-top: var(--space-4); display: flex; flex-direction: column; gap: var(--space-1); }
  .list { list-style: none; margin: var(--space-2) 0 0; padding: 0; display: flex; flex-direction: column; }
  .entry {
    display: grid; grid-template-columns: minmax(0, 1fr) 8rem minmax(0, 1fr) auto;
    gap: var(--space-3); align-items: baseline;
    padding: var(--space-2) 0; border-top: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  .entry:first-child { border-top: 0; }
  .who { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .board { font-size: var(--text-xs); color: var(--accent-text); }
  .detail { color: var(--text-muted); font-size: var(--text-xs); }
  .clear {
    justify-self: end; cursor: pointer;
    background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    color: var(--text-muted); padding: var(--space-1) var(--space-2);
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .clear:hover { color: var(--text); border-color: var(--accent); }
  .explain { margin: var(--space-2) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }

  @media (max-width: 640px) {
    .entry { grid-template-columns: 1fr; gap: var(--space-1); }
    .clear { justify-self: start; }
  }
</style>
