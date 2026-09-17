<script>
  // Home instrument: the Landsraad term. Compact by default (houses decided +
  // the leading crest), expands in place to the shipped LandsraadStanding rails.
  //
  // The rails, the leader emphasis and the term countdown all live in
  // LandsraadStanding already, so the expanded body renders that component
  // rather than a second copy of the same layout. The compact face therefore
  // does NOT repeat the countdown: one term clock on screen, not two.
  //
  // "Your contribution" only appears when the figure was actually read. A null
  // contribution is absent, never 0: a player who contributed nothing and a
  // player whose contribution could not be loaded are different statements.
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LandsraadStanding from '$lib/components/LandsraadStanding.svelte';

  let { standings = null, myContribution = null } = $props();

  const CREST = (c) => `/admin/static/img/factions/${c}.png`;

  let open = $state(false);
  let rails = $derived(Array.isArray(standings?.rails) ? standings.rails : []);
  let available = $derived(standings?.available === true && rails.length >= 2);
  let decided = $derived(typeof standings?.decided === 'number' ? standings.decided : null);
  let contested = $derived(typeof standings?.contested === 'number' ? standings.contested : null);
  let total = $derived(decided != null && contested != null ? decided + contested : null);
  // Leading house by score; a tie has no leader and so wears no crest.
  let leader = $derived.by(() => {
    if (rails.length < 2 || rails[0].score === rails[1].score) return null;
    return rails[0].score > rails[1].score ? rails[0] : rails[1];
  });
  let contribution = $derived(
    typeof myContribution === 'number' && Number.isFinite(myContribution)
      ? Math.floor(myContribution)
      : null
  );
</script>

{#if standings === null || standings === undefined}
  <SealedPanel slab status="empty" action="none" emptyText="The Landsraad standings could not be read." />
{:else}
  <CarvedSlab sharp>
    <button class="face" type="button" aria-expanded={open} onclick={() => (open = !open)}>
      <!-- "term", not a bare "Landsraad": the reused standing card below carries
           its own Landsraad kicker, and two identical labels a row apart read as
           a rendering bug. -->
      <span class="eyebrow mono">Landsraad term</span>
      {#if available && decided != null}
        <span class="val mono">{decided}{#if total != null}<small>of {total} decided</small>{/if}</span>
      {:else if available}
        <span class="val mono">{rails[0].score}<small>{rails[0].name}</small></span>
      {:else}
        <span class="val muted">No active term</span>
      {/if}
      {#if leader?.crest}
        <img class="glyph crest" src={CREST(leader.crest)} alt="" aria-hidden="true" loading="lazy" />
      {:else}
        <svg class="glyph" viewBox="0 0 36 36" aria-hidden="true">
          <path d="M18 6v24M9 12h18M11 12l-4 8h8zM25 12l-4 8h8z" />
        </svg>
      {/if}
      <span class="caret" aria-hidden="true"></span>
    </button>
    {#if open}
      <div class="body">
        <LandsraadStanding {standings} />
        {#if contribution !== null}
          <p class="row"><span>Your contribution</span><b class="mono">{contribution.toLocaleString()}</b></p>
        {/if}
        <a class="cta mono" href="{base}/landsraad">Open the Landsraad</a>
      </div>
    {/if}
  </CarvedSlab>
{/if}

<style>
  .face {
    width: 100%; display: grid; grid-template-columns: 1fr auto; gap: 2px var(--space-3);
    align-items: center; text-align: left; background: none; border: 0; padding: 0;
    color: inherit; cursor: pointer;
  }
  .eyebrow {
    grid-column: 1; font-size: var(--text-xs); text-transform: uppercase;
    letter-spacing: .18em; color: var(--accent);
  }
  .val {
    grid-column: 1; font-size: var(--text-2xl); font-weight: 700; line-height: 1;
    color: var(--text); overflow-wrap: anywhere;
  }
  .val small { font-size: var(--text-xs); font-weight: 400; color: var(--text-muted); margin-left: var(--space-2); }
  .val.muted { color: var(--text-muted); font-size: var(--text-lg); font-weight: 400; }
  .glyph {
    grid-row: 1 / 3; grid-column: 2; align-self: center; width: 34px; height: 34px;
    fill: none; stroke: var(--accent); stroke-width: 1.6; stroke-linecap: round; opacity: .8;
  }
  .glyph.crest { object-fit: contain; opacity: .95; filter: drop-shadow(0 0 4px rgba(0, 0, 0, .5)); }
  .caret {
    grid-column: 2; justify-self: end; width: 8px; height: 8px;
    border-right: 1.5px solid var(--text-muted); border-bottom: 1.5px solid var(--text-muted);
    transform: rotate(45deg); margin-top: var(--space-2);
    transition: transform var(--motion-fast) var(--ease-out);
  }
  .face[aria-expanded='true'] .caret { transform: rotate(225deg); }
  .face:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

  .body {
    margin-top: var(--space-3); padding-top: var(--space-3);
    border-top: 1px solid var(--edge); display: flex; flex-direction: column; gap: var(--space-2);
  }
  .row { margin: 0; display: flex; justify-content: space-between; gap: var(--space-3); font-size: var(--text-sm); color: var(--text-muted); }
  .row b { font-weight: 500; color: var(--text); }
  .cta {
    align-self: flex-start; text-decoration: none; color: var(--accent-text);
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
  }
  .cta:hover { color: var(--accent-bright); }

  /* Phone: the instruments row is two columns wide, which leaves a seven
     figure Solari number nowhere to go. The glyph drops, the caret moves up
     beside the eyebrow, and the figure takes the full card width one size
     down rather than breaking mid-number. */
  @media (max-width: 520px) {
    .glyph { display: none; }
    .val { grid-column: 1 / -1; font-size: var(--text-lg); }
    .caret { grid-row: 1; margin-top: 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .caret { transition: none; }
  }
</style>
