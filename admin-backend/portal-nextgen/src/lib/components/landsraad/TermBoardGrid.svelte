<script>
  // Term board: the 25-house Landsraad layout as a 5x5 grid of tiles (in-game
  // board parity). Each tile shows the house crest/monogram, its winner (if
  // decided) or contested state, and the viewer-faction progress bar when the
  // server shipped a progress row (opposing-faction exact numbers never leave the
  // server). Clicking a tile opens its reward-ladder drawer. Keyboard-activatable.
  import { houseCrestUrl } from '$lib/icons.js';
  import { landsraad, selectTile } from '$lib/landsraad.svelte.js';

  let { tiles = [] } = $props();

  // Stable board order: honor board_index when present, else keep server order.
  let ordered = $derived(
    [...tiles].sort((a, b) => (Number(a?.board_index) || 0) - (Number(b?.board_index) || 0))
  );

  function onKey(e, tile) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectTile(tile); }
  }
  function myPct(tile) {
    const row = (tile?.factions || [])[0];
    return row ? Math.max(0, Math.min(100, Number(row.pct) || 0)) : null;
  }
</script>

<div class="board-wrap">
  <div class="grid" role="list">
    {#each ordered as tile (tile.name)}
      {@const crest = houseCrestUrl(tile.crest)}
      {@const pct = myPct(tile)}
      {@const won = tile.winner?.slug}
      <div
        class="tile" role="listitem"
        class:selected={landsraad.selectedTile === tile}
        class:decided={!!won}
        class:sysselraad={tile.sysselraad}
        data-faction={won || 'contested'}
      >
        <button class="hit" type="button" onclick={() => selectTile(tile)} onkeydown={(e) => onKey(e, tile)}
                aria-label={`${tile.name}${won ? `, held by House ${tile.winner?.name}` : ', contested'}, open rewards`}>
          <div class="crest-wrap">
            {#if crest}
              <img class="crest" src={crest} alt="" aria-hidden="true" loading="lazy" />
            {:else}
              <span class="monogram mono" aria-hidden="true">{tile.monogram || tile.short?.slice(0, 2) || '??'}</span>
            {/if}
          </div>
          <span class="short mono">{tile.short || tile.name}</span>
          {#if won}
            <span class="state {won}">House {tile.winner?.name}</span>
          {:else if pct != null}
            <span class="bar" aria-hidden="true"><span class="fill" style="width:{pct}%"></span></span>
          {:else}
            <span class="state contested">Contested</span>
          {/if}
        </button>
      </div>
    {/each}
  </div>
</div>

<style>
  .board-wrap { overflow-x: auto; }
  .grid {
    display: grid; grid-template-columns: repeat(5, minmax(84px, 1fr)); gap: var(--space-2);
    min-width: 460px;
  }
  .tile {
    position: relative; border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0); overflow: hidden;
    transition: border-color var(--motion-fast) var(--ease-out), transform var(--motion-fast) var(--ease-out);
  }
  .tile:hover { transform: translateY(-2px); border-color: var(--edge-hi); }
  .tile.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
  .tile[data-faction='atreides'] { border-color: color-mix(in srgb, var(--ls-ibad) 40%, var(--edge)); }
  .tile[data-faction='harkonnen'] { border-color: color-mix(in srgb, var(--ls-red) 40%, var(--edge)); }
  .tile.sysselraad::after {
    content: '★'; position: absolute; top: 4px; right: 6px; font-size: 10px; color: var(--accent-bright);
  }
  .hit {
    display: flex; flex-direction: column; align-items: center; gap: var(--space-1);
    width: 100%; padding: var(--space-3) var(--space-2); cursor: pointer;
    background: none; border: 0; color: inherit; text-align: center;
  }
  .hit:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  .crest-wrap { width: 40px; height: 40px; display: flex; align-items: center; justify-content: center; }
  .crest { max-width: 100%; max-height: 100%; object-fit: contain; }
  .monogram { font-size: var(--text-lg); font-weight: 700; color: var(--text-muted); }
  .short { font-size: var(--text-xs); color: var(--text); letter-spacing: .04em; overflow: hidden; text-overflow: ellipsis; max-width: 100%; white-space: nowrap; }
  .state { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; }
  .state.contested { color: var(--text-muted); }
  .state.atreides { color: var(--ls-ibad); }
  .state.harkonnen { color: var(--ls-red); }
  .bar { width: 80%; height: 5px; background: var(--metal-1); border-radius: 999px; overflow: hidden; }
  .fill { display: block; height: 100%; background: var(--accent); }
</style>
