<script>
  // Legend rebuilt as a category -> type tree: icon chips, counts, search filter,
  // All/None/Default quick picks, preset save/load. Keeps V1's localStorage key
  // map_preset_{key} (JSON of the hidden map) so player presets survive the
  // migration. `legend` = the engine's onLegend model; `hidden` is owned by the
  // route (chrome owns persistence); every change emits a FRESH object.
  // `defaults` = the board's opening hidden-map (the route seeds it: medium spice
  // fields off on the Deep Desert), so "Default" means "as first opened", not
  // "everything on"; that is what All is for.
  // Wave 10b: a linked player's preset is saved on the ACCOUNT under map_hidden,
  // in the selected character's scope (identity scope when no character is
  // known), so it follows them to any browser. map_preset_{key} stays exactly
  // where it is: the anonymous path, and the source of the one-time adoption.
  import { untrack } from 'svelte';
  import { auth } from '$lib/auth.svelte.js';
  import { prefs, charScope, getPref, setPref } from '$lib/prefs.svelte.js';

  let { legend = [], hidden = {}, mapKey = '', defaults = {}, onchange, emptyText = 'Reading the layer index...', note = '' } = $props();

  const presetKey = $derived(`map_preset_${mapKey}`);

  // The server refuses a WHOLE write that carries one key outside its
  // whitelist, so only ids that match it travel; anything else stays in the
  // browser-local preset rather than costing the player the entire save. The
  // value whitelist is layerId -> true ONLY, and a layer toggled off and back
  // on leaves `false` behind, so those entries are dropped as well: a missing
  // id reads as visible everywhere (typeHidden). An empty object is still a
  // saved preset, meaning "everything visible", and is stored as one.
  const MAP_KEY_RE = /^[a-z0-9_-]{1,32}$/;
  const LAYER_ID_RE = /^[a-zA-Z0-9_.:-]{1,64}$/;
  const MAX_LAYERS = 200;
  // Server caps for one scope doc: 24 maps under map_hidden, 8192 bytes for the
  // whole doc. Over either, EVERY later write to that scope is refused, so the
  // oldest boards (object insertion order) are dropped here first. Headroom of
  // 192 bytes covers the keys this component does not own.
  const MAX_MAPS = 24;
  const MAX_DOC_BYTES = 8000;

  function cleanHidden(map) {
    const out = {};
    for (const [id, off] of Object.entries(map || {})) {
      if (off !== true || !LAYER_ID_RE.test(id)) continue;
      if (Object.keys(out).length >= MAX_LAYERS) break;
      out[id] = true;
    }
    return out;
  }

  function scopePreset() {
    if (auth.status !== 'authed') return null;
    const saved = getPref('map_hidden', { scope: charScope() })?.[mapKey];
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : null;
  }

  function docBytes(scope, mapHidden) {
    const doc = { ...(scope ? prefs.scopes[scope] : prefs.identity), map_hidden: mapHidden };
    try {
      const json = JSON.stringify(doc);
      return typeof TextEncoder === 'function' ? new TextEncoder().encode(json).length : json.length;
    } catch (e) { return 0; }
  }

  function fitToCaps(maps, scope) {
    const current = maps[mapKey];
    const older = Object.keys(maps).filter((k) => k !== mapKey);
    // The board just saved is written back LAST, so it is the freshest entry
    // and survives every trim; the oldest boards leave first.
    const build = () => {
      const out = {};
      for (const k of older) out[k] = maps[k];
      out[mapKey] = current;
      return out;
    };
    while (older.length > MAX_MAPS - 1) older.shift();
    let next = build();
    while (older.length && docBytes(scope, next) > MAX_DOC_BYTES) {
      older.shift();
      next = build();
    }
    return next;
  }

  function writeScopePreset(map) {
    if (auth.status !== 'authed' || !MAP_KEY_RE.test(mapKey)) return;
    const scope = charScope();
    const next = fitToCaps({ ...getPref('map_hidden', { scope }), [mapKey]: cleanHidden(map) }, scope);
    setPref('map_hidden', next, { scope });
  }

  let hasPreset = $state(false);
  $effect(() => {
    const onAccount = !!scopePreset();
    try { hasPreset = onAccount || !!localStorage.getItem(presetKey); } catch (e) { hasPreset = onAccount; }
  });

  // One-time adoption: a preset saved in this browser before the account had
  // one becomes this scope's default on the first load that can see both. The
  // local key is left alone (an anonymous visitor still reads it).
  let adopted = null;
  $effect(() => {
    if (auth.status !== 'authed' || prefs.status !== 'ready') return;
    const k = mapKey;
    if (!k || adopted === k) return;
    untrack(() => {
      adopted = k;
      if (scopePreset()) return;
      let local = null;
      try { local = JSON.parse(localStorage.getItem(presetKey) || 'null'); } catch (e) {}
      if (!local || typeof local !== 'object' || Array.isArray(local)) return;
      writeScopePreset(local);
    });
  });

  let search = $state('');
  let collapsed = $state({});

  function typeHidden(idx) { return !!hidden[idx]; }
  function catOff(cat) {
    const types = cat.types || [];
    return types.length > 0 && types.every((t) => typeHidden(t.idx));
  }

  function toggleType(idx) {
    onchange?.({ ...hidden, [idx]: !hidden[idx] });
  }
  function toggleCat(cat) {
    const types = cat.types || [];
    const allHidden = types.every((t) => typeHidden(t.idx));
    const next = { ...hidden };
    for (const t of types) next[t.idx] = !allHidden;
    onchange?.(next);
  }
  function toggleCollapse(key) {
    collapsed = { ...collapsed, [key]: !collapsed[key] };
  }

  function allOn() {
    const next = { ...hidden };
    for (const cat of legend) for (const t of cat.types || []) next[t.idx] = false;
    onchange?.(next);
  }
  function noneOn() {
    const next = { ...hidden };
    for (const cat of legend) for (const t of cat.types || []) next[t.idx] = true;
    onchange?.(next);
  }
  function restoreDefaults() { onchange?.({ ...defaults }); }

  function savePreset() {
    try {
      localStorage.setItem(presetKey, JSON.stringify(hidden));
      hasPreset = true;
    } catch (e) {}
    writeScopePreset(hidden);
  }
  function loadPreset() {
    const onAccount = scopePreset();
    if (onAccount) { onchange?.({ ...onAccount }); return; }
    try {
      const saved = localStorage.getItem(presetKey);
      if (saved) onchange?.(JSON.parse(saved));
    } catch (e) {}
  }

  // Search filters the DISPLAYED tree only (marker visibility is the toggles').
  let needle = $derived(search.trim().toLowerCase());
  function typeMatches(t) {
    return !needle || (t.label || '').toLowerCase().includes(needle);
  }
  function catVisible(cat) {
    return !needle || (cat.types || []).some(typeMatches);
  }
</script>

<div class="tree">
  <input
    type="search"
    class="search mono"
    placeholder="Filter layers..."
    aria-label="Filter map layers"
    autocomplete="off"
    bind:value={search}
  />

  <div class="picks" role="group" aria-label="Layer quick picks">
    <button onclick={allOn}>All</button>
    <button onclick={noneOn}>None</button>
    <button onclick={restoreDefaults}>Default</button>
    <span class="picks-gap" aria-hidden="true"></span>
    <button onclick={savePreset}>Save</button>
    {#if hasPreset}<button onclick={loadPreset}>Load</button>{/if}
  </div>

  <div class="cats" role="list" aria-label="Map layers">
    {#each legend as cat (cat.key)}
      {#if catVisible(cat)}
        <div class="cat" class:off={catOff(cat)} role="listitem">
          <div class="cat-row">
            <button
              class="expand mono"
              aria-label={collapsed[cat.key] ? `Expand ${cat.label}` : `Collapse ${cat.label}`}
              aria-expanded={!collapsed[cat.key]}
              onclick={() => toggleCollapse(cat.key)}
            >{collapsed[cat.key] ? '▸' : '▾'}</button>
            <button
              class="cat-btn"
              aria-pressed={!catOff(cat)}
              onclick={() => toggleCat(cat)}
            >
              <span class="swatch" style:background={cat.color}></span>
              <span class="cat-label">{cat.label}</span>
              <span class="count mono">{(cat.count || 0).toLocaleString()}</span>
            </button>
          </div>
          {#if !collapsed[cat.key]}
            <div class="types">
              {#each cat.types || [] as t (t.idx)}
                {#if typeMatches(t)}
                  <button
                    class="type"
                    class:off={typeHidden(t.idx)}
                    aria-pressed={!typeHidden(t.idx)}
                    title={t.title || undefined}
                    onclick={() => toggleType(t.idx)}
                  >
                    {#if t.icon}
                      <img class="type-icon" src="/admin/static/img/dune-icons/{t.icon}.png" alt="" loading="lazy" />
                    {:else}
                      <span class="swatch swatch-sm" style:background={cat.color}></span>
                    {/if}
                    <span class="type-label">{t.label}</span>
                    <span class="count mono">{(t.count || 0).toLocaleString()}</span>
                  </button>
                {/if}
              {/each}
            </div>
          {/if}
        </div>
      {/if}
    {/each}
    {#if !legend.length}
      <p class="empty mono">{emptyText}</p>
    {/if}
  </div>

  {#if note}
    <p class="note mono">{note}</p>
  {/if}
</div>

<style>
  .tree { display: flex; flex-direction: column; gap: var(--space-3); min-height: 0; }

  .search {
    width: 100%; background: var(--metal-0); color: var(--text);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); font-size: var(--text-sm);
  }
  .search::placeholder { color: var(--text-muted); }
  .search:focus { border-color: var(--accent); outline: none; }

  .picks { display: flex; gap: var(--space-1); align-items: center; }
  .picks-gap { flex: 1; }
  .picks button {
    background: var(--bg-elevated); color: var(--text-muted);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); cursor: pointer;
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .picks button:hover { color: var(--text); border-color: var(--edge-hi); }

  .cats { overflow-y: auto; min-height: 0; scrollbar-width: thin; }

  .cat { border-bottom: 1px solid var(--border-subtle); padding: var(--space-1) 0; }
  .cat-row { display: flex; align-items: stretch; gap: var(--space-1); }
  .expand {
    flex: none; width: 24px; background: transparent; border: 0;
    color: var(--text-muted); cursor: pointer; font-size: var(--text-sm);
  }
  .expand:hover { color: var(--text); }
  .cat-btn {
    flex: 1; display: flex; align-items: center; gap: var(--space-2);
    background: transparent; border: 0; cursor: pointer; text-align: left;
    color: var(--text); padding: var(--space-1) var(--space-1);
    font-family: var(--font-sans); font-size: var(--text-sm); font-weight: 500;
    border-radius: var(--radius-sm);
    transition: background var(--motion-fast) var(--ease-out);
  }
  .cat-btn:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .cat.off .cat-btn { color: var(--text-muted); }
  .cat.off .swatch { opacity: .3; }

  .swatch {
    flex: none; width: 12px; height: 12px; border-radius: 3px;
    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .4);
  }
  .swatch-sm { width: 10px; height: 10px; }
  .cat-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .count { flex: none; color: var(--text-muted); font-size: var(--text-xs); }

  .types { display: flex; flex-direction: column; padding-left: 24px; }
  .type {
    display: flex; align-items: center; gap: var(--space-2);
    background: transparent; border: 0; cursor: pointer; text-align: left;
    color: var(--text-muted); padding: var(--space-1) var(--space-1);
    font-size: var(--text-xs); font-family: var(--font-sans);
    border-radius: var(--radius-sm);
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .type:hover { color: var(--text); background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .type.off { opacity: .45; }
  .type.off .type-label { text-decoration: line-through; text-decoration-color: var(--text-muted); }
  .type-icon { flex: none; width: 16px; height: 16px; object-fit: contain; }
  .type-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .empty { color: var(--text-muted); font-size: var(--text-xs); letter-spacing: .08em; }
  .note {
    margin: 0; padding-top: var(--space-2);
    border-top: 1px solid var(--border-subtle);
    color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .06em; opacity: .8;
  }
</style>
