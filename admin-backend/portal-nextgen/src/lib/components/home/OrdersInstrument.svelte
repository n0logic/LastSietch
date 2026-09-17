<script>
  // Home instrument: the player's own order book. Compact by default (one
  // number + glyph), expands in place to the full set.
  //
  // Each of the three counts is independently nullable, because each comes from
  // a loader that can fail on its own. A count that could not be read is ABSENT
  // from the panel; it is never printed as 0, which would tell the player they
  // have nothing outstanding when we simply do not know. All three missing is a
  // sealed panel, not an empty ledger.
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  let { marketOpen = null, marketFilled = null, karumOpen = null } = $props();

  let open = $state(false);

  function count(n) {
    return typeof n === 'number' && Number.isFinite(n) ? Math.floor(n) : null;
  }

  let facts = $derived(
    [
      { n: count(marketOpen), word: 'open' },
      { n: count(marketFilled), word: 'filled today' },
      { n: count(karumOpen), word: 'Karum open' },
    ].filter((f) => f.n !== null)
  );
  let primary = $derived(facts[0] ?? null);
  let rest = $derived(facts.slice(1));
</script>

{#if primary === null}
  <SealedPanel slab status="empty" action="none" emptyText="Your orders could not be read." />
{:else}
  <CarvedSlab sharp>
    <button class="face" type="button" aria-expanded={open} onclick={() => (open = !open)}>
      <span class="eyebrow mono">Orders</span>
      <span class="val mono">{primary.n.toLocaleString()}<small>{primary.word}</small></span>
      <svg class="glyph" viewBox="0 0 36 36" aria-hidden="true">
        <path d="M8 7h20v22H8z" />
        <path d="M13 14h10M13 19h10M13 24h6" />
      </svg>
      <span class="caret" aria-hidden="true"></span>
      {#if rest.length}
        <span class="sub mono">{rest.map((f) => `${f.n.toLocaleString()} ${f.word}`).join(' | ')}</span>
      {/if}
    </button>
    {#if open}
      <div class="body">
        {#each facts as f (f.word)}
          <p class="row"><span>{f.word}</span><b class="mono">{f.n.toLocaleString()}</b></p>
        {/each}
        <a class="cta mono" href="{base}/exchange">Open the exchange</a>
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
  .sub { grid-column: 1 / -1; font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .04em; }
  .glyph {
    grid-row: 1 / 3; grid-column: 2; align-self: center; width: 34px; height: 34px;
    fill: none; stroke: var(--accent); stroke-width: 1.6; stroke-linecap: round; opacity: .8;
  }
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
