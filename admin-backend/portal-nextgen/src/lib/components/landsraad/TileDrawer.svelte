<script>
  // Term-tile reward panel, opened from a board tile. Shows the house crest, the
  // decided/contested state, the viewer's own contribution + progress
  // (server-scoped; opposing numbers never arrive), and the full threshold ->
  // reward ladder with a "reached" mark and inline swatch chips for
  // placeable-dye rewards. Read-only.
  //
  // Was a right-hand slide-in drawer carrying its own scrim, focus trap and
  // Escape key; all three now come from the shared Modal primitive, so this is a
  // centred dialog. Modal owns its panel geometry, so the drawer presentation
  // could not be carried across from the call site.
  import { landsraad, closeTile } from '$lib/landsraad.svelte.js';
  import { houseCrestUrl } from '$lib/icons.js';
  import Modal from '$lib/components/Modal.svelte';
  import SwatchChips from './SwatchChips.svelte';

  let tile = $derived(landsraad.selectedTile);
  let open = $derived(tile != null);
  let ladder = $derived(Array.isArray(tile?.rewards) ? tile.rewards : []);
  let crest = $derived(tile ? houseCrestUrl(tile.crest) : null);
  let myRow = $derived((tile?.factions || [])[0] || null);
</script>

{#if open && tile}
  <Modal title={tile.name} size="md" onClose={closeTile}>
    <div class="ident">
      {#if crest}<img class="dicon" src={crest} alt="" aria-hidden="true" />{/if}
      <p class="kicker mono">Landsraad term reward</p>
    </div>

    <div class="state-row">
      {#if tile.winner?.slug}
        <span class="chip {tile.winner.slug}">Held by House {tile.winner.name}</span>
      {:else}
        <span class="chip contested">Contested</span>
      {/if}
      {#if tile.sysselraad}<span class="chip star">Sysselraad</span>{/if}
    </div>

    {#if tile.rep_location}
      <div class="rep">
        <span class="rep-lbl mono">Rep location</span>
        <span class="rep-val">{tile.rep_location}</span>
      </div>
    {/if}

    {#if tile.my_contribution != null || myRow}
      <div class="contrib">
        {#if tile.my_contribution != null}
          <div class="c-line"><span class="c-lbl mono">Your contribution</span><span class="c-val mono">{tile.my_contribution_display ?? tile.my_contribution}</span></div>
        {/if}
        {#if myRow}
          <div class="c-line"><span class="c-lbl mono">{myRow.name}</span><span class="c-val mono">{myRow.amount_display ?? myRow.amount} / {tile.goal_display ?? tile.goal}</span></div>
          <div class="bar"><span class="fill" style="width:{Math.max(0, Math.min(100, Number(myRow.pct) || 0))}%"></span></div>
        {/if}
      </div>
    {/if}

    <div class="ladder">
      {#if ladder.length === 0}
        <p class="hint">No reward ladder for this house this term.</p>
      {:else}
        <ul class="rungs" role="list">
          {#each ladder as r, i (i)}
            <li class="rung" class:reached={r.reached}>
              <span class="thr mono">{r.threshold_display ?? r.threshold}</span>
              <span class="rwd">
                <span class="rname">{r.amount_display ?? r.amount} {r.name}</span>
                {#if r.swatch}<SwatchChips swatch={r.swatch} />{/if}
              </span>
              {#if r.reached}<span class="mark" title="Reached" aria-label="Reached">✓</span>{/if}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </Modal>
{/if}

<style>
  .ident { display: flex; align-items: center; gap: var(--space-3); }
  .dicon { width: 44px; height: 44px; object-fit: contain; flex: 0 0 auto; }

  .state-row { display: flex; gap: var(--space-2); flex-wrap: wrap; }
  .chip { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .08em; border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 2px var(--space-2); color: var(--text-muted); }
  .chip.atreides { color: var(--ls-ibad); border-color: color-mix(in srgb, var(--ls-ibad) 40%, var(--edge)); }
  .chip.harkonnen { color: var(--ls-red); border-color: color-mix(in srgb, var(--ls-red) 40%, var(--edge)); }
  .chip.star { color: var(--accent-bright); }

  .rep { display: flex; flex-direction: column; gap: 2px; padding-bottom: var(--space-3); border-bottom: 1px solid var(--edge); }
  .rep-lbl { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); }
  .rep-val { font-size: var(--text-sm); color: var(--text); line-height: 1.45; }

  .contrib { display: flex; flex-direction: column; gap: var(--space-2); background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-3); }
  .c-line { display: flex; justify-content: space-between; gap: var(--space-3); font-size: var(--text-sm); }
  .c-lbl { color: var(--text-muted); }
  .c-val { color: var(--text); }
  .bar { height: 6px; background: var(--metal-1); border-radius: 999px; overflow: hidden; }
  .fill { display: block; height: 100%; background: var(--accent); }

  .ladder { flex: 1 1 auto; }
  .hint { color: var(--text-muted); font-size: var(--text-sm); }
  .rungs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .rung {
    display: flex; align-items: center; gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .rung.reached { border-color: color-mix(in srgb, var(--ls-green) 40%, var(--edge)); }
  .thr { flex: 0 0 auto; font-size: var(--text-xs); color: var(--text-muted); min-width: 4.5ch; text-align: right; }
  .rwd { flex: 1 1 auto; display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .rname { font-size: var(--text-sm); color: var(--text); }
  .mark { flex: 0 0 auto; color: var(--ls-green); font-weight: 700; }
</style>
