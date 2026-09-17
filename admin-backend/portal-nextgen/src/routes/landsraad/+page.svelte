<script>
  // Landsraad: the great-house board + the player's own house rewards, re-skinned
  // in the carved-sietch language. TOP = a term ribbon with the live countdown to
  // the term end. Then the 25-house term board (5x5) flanked by the two great-
  // house rails, and below it the player's rewards summary + the 25-house own-
  // rewards board. Clicking a board tile opens its reward-ladder drawer.
  //
  // READ-ONLY (contract MODULE 2): account-scoped, no writes, no CSRF. The board
  // and rewards halves render independently (either may be null). Reads are never
  // gated; signed-out seals to a Connect-Discord panel.
  import { untrack } from 'svelte';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { landsraad, loadAll } from '$lib/landsraad.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import TermBoardGrid from '$lib/components/landsraad/TermBoardGrid.svelte';
  import FactionRail from '$lib/components/landsraad/FactionRail.svelte';
  import TileDrawer from '$lib/components/landsraad/TileDrawer.svelte';
  import RewardsSummary from '$lib/components/landsraad/RewardsSummary.svelte';
  import HouseRewardsGrid from '$lib/components/landsraad/HouseRewardsGrid.svelte';

  const gate = useAuthGate();

  let board = $derived(landsraad.board);
  let rewards = $derived(landsraad.rewards);
  let term = $derived(board?.term || null);
  let rails = $derived(Array.isArray(board?.rails) ? board.rails : []);
  let tiles = $derived(Array.isArray(board?.tiles) ? board.tiles : []);
  let rewardHouses = $derived(Array.isArray(rewards?.board) ? rewards.board : []);

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadAll); }
    else if (status === 'anon') { loaded = false; }
  });
</script>

<svelte:head>
  <title>Landsraad | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Landsraad council"
    title="Landsraad"
    sub="The great-house contest for the twenty-five council seats this term, and the house rewards waiting for you to collect. Open a tile to see its reward ladder."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see the Landsraad board and your house rewards."
    />
  {:else if landsraad.status === 'error'}
    <SealedPanel
      status="error"
      errorText="The Landsraad could not be read right now. Try again in a moment."
    />
  {:else}
    <div class="stack">
      <!-- Term ribbon -->
      {#if term}
        <CarvedSlab>
          <div class="ribbon">
            <div class="rib-term">
              <span class="rib-cap mono">Current term</span>
              <span class="rib-num mono">#{term.term_id ?? '—'}{#if term.test_term}<span class="rib-test"> test</span>{/if}</span>
            </div>
            {#if board?.decided != null}
              <div class="rib-seats">
                <span class="rib-cap mono">Seats decided</span>
                <span class="rib-val mono">{board.decided} / {board.decided + board.contested}</span>
              </div>
            {/if}
            {#if term.end_utc}
              <div class="rib-cd">
                <span class="rib-cap mono">Term ends in</span>
                <LiveCountdown target={term.end_utc} />
              </div>
            {/if}
          </div>
        </CarvedSlab>
      {/if}

      <!-- Board + faction rails -->
      {#if board && tiles.length}
        <div class="board-row">
          {#if rails[0]}<CarvedSlab><FactionRail rail={rails[0]} /></CarvedSlab>{/if}
          <CarvedSlab sharp={true}><TermBoardGrid {tiles} /></CarvedSlab>
          {#if rails[1]}<CarvedSlab><FactionRail rail={rails[1]} /></CarvedSlab>{/if}
        </div>
      {:else}
        <SealedPanel
          slab={true} status="empty" action="none" art="no-term"
          emptyText="No active Landsraad term right now. Your house rewards are still shown below."
        />
      {/if}

      <!-- Your rewards -->
      {#if landsraad.rewardsError}
        <SealedPanel
          slab={true} status="empty" action="none"
          emptyText="Your house rewards could not be read right now. The board above is still current."
        />
      {:else if rewards}
        <CarvedSlab><RewardsSummary summary={rewards.summary} /></CarvedSlab>
        {#if rewardHouses.length}
          <CarvedSlab sharp={true}><HouseRewardsGrid houses={rewardHouses} /></CarvedSlab>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<TileDrawer />

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .stack { display: flex; flex-direction: column; gap: var(--space-4); }

  .ribbon { display: flex; align-items: center; gap: var(--space-6); flex-wrap: wrap; }
  .rib-term, .rib-seats, .rib-cd { display: flex; flex-direction: column; gap: var(--space-1); }
  .rib-cap { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .rib-num { font-size: var(--text-2xl); font-weight: 700; color: var(--accent-text); line-height: 1; }
  .rib-test { font-size: var(--text-sm); color: var(--text-muted); }
  .rib-val { font-size: var(--text-lg); color: var(--text); }
  .rib-cd { margin-left: auto; }

  .board-row {
    display: grid; gap: var(--space-4);
    grid-template-columns: minmax(0, 0.8fr) minmax(0, 2fr) minmax(0, 0.8fr);
    align-items: start;
  }

  /* Stagger-rise the panels on enter (opacity/transform only). */
  .stack > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .stack > :global(*:nth-child(1)) { animation-delay: .04s; }
  .stack > :global(*:nth-child(2)) { animation-delay: .10s; }
  .stack > :global(*:nth-child(3)) { animation-delay: .16s; }
  .stack > :global(*:nth-child(4)) { animation-delay: .22s; }
  @keyframes rise { to { opacity: 1; transform: none; } }

  @media (max-width: 900px) {
    .board-row { grid-template-columns: 1fr; }
  }
  @media (prefers-reduced-motion: reduce) {
    .stack > :global(*) { opacity: 1; transform: none; animation: none; }
  }
</style>
