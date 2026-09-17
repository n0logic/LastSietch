<script>
  // Faction rail: one great house's board summary (Atreides left, Harkonnen
  // right). Shows the crest, decided-tile score, and the top contributing guilds.
  // Ibad blue for Atreides accents, red for Harkonnen, per the board convention.
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import { factionCrestUrl } from '$lib/icons.js';

  let { rail = null } = $props();

  let crest = $derived(rail ? factionCrestUrl(rail.crest || rail.slug) : null);
  let guilds = $derived(Array.isArray(rail?.top_guilds) ? rail.top_guilds : []);
</script>

{#if rail}
  <div class="rail" data-faction={rail.slug}>
    <div class="head">
      {#if crest}<img class="crest" src={crest} alt="" aria-hidden="true" loading="lazy" />{/if}
      <div class="titles">
        <span class="name">{rail.name}</span>
        <span class="score mono">{rail.score ?? 0} <span class="score-lbl">tiles</span></span>
      </div>
    </div>
    {#if guilds.length}
      <ul class="guilds" role="list">
        {#each guilds as g, i (i)}
          <li class="guild">
            <span class="gname" title={g.guild}>{g.guild}</span>
            <span class="gamt mono">{g.amount_display ?? g.amount ?? 0}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <SealedPanel
        status="empty" action="none" art="no-term"
        emptyText="No contributions yet this term."
      />
    {/if}
  </div>
{/if}

<style>
  .rail { display: flex; flex-direction: column; gap: var(--space-3); }
  .head { display: flex; align-items: center; gap: var(--space-3); }
  .crest { width: 40px; height: 40px; object-fit: contain; flex: 0 0 auto; }
  .titles { display: flex; flex-direction: column; }
  .name { font-family: var(--font-display); font-size: var(--text-lg); text-transform: uppercase; letter-spacing: .06em; color: var(--text); }
  .score { font-size: var(--text-xl); font-weight: 700; }
  .score-lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; }
  [data-faction='atreides'] .score { color: var(--ls-ibad); }
  [data-faction='harkonnen'] .score { color: var(--ls-red); }
  .guilds { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .guild {
    display: flex; justify-content: space-between; gap: var(--space-3);
    font-size: var(--text-sm); padding: var(--space-1) var(--space-2);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .gname { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gamt { color: var(--text-muted); flex: 0 0 auto; }
</style>
