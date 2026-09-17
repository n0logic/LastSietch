<script>
  // The Karum: the merchant quarter. Player-to-player trade at a fixed ask.
  //
  // TOP = the standing board (public: anyone can browse, only a linked player can
  // buy). Below it, for linked players, your own stall: what you have up, what you
  // have bought, and the sell form. Signed-out sees the board and the stall seals.
  //
  // The one thing this page has to teach, because the game will not: a collected
  // item appears in the CHOAM Exchange "Completed" tab marked CANCELED. That is the
  // only completion format the client renders. Every surface that hands over goods
  // repeats it, deliberately, because a player who is not told reads it as a bug.
  //
  // The other thing worth stating once, at the top: because escrow happens when a
  // seller LISTS rather than when a buyer pays, a paid-for item is already out of
  // the seller's hands, so no seller can take the money and keep the goods.
  //
  // That claim is about SELLERS and is deliberately no longer stated as "non-delivery
  // is impossible", which is what this said until 2026-07-27. It was false. An item
  // whose template has no CHOAM Exchange category could be escrowed and then handed to
  // nobody, because every leg that gives goods away builds an Exchange order and the
  // category can only be copied from a real one. Measured at ~10% of the templates
  // sitting in player banks, and it stranded a real item on the first live cancel. The
  // listing leg now refuses those outright, but the promise stays scoped to what is
  // actually guaranteed.
  import { untrack } from 'svelte';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { karum, loadAll, loadBoard } from '$lib/karum.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import KarumBoard from '$lib/components/karum/KarumBoard.svelte';
  import MyKarumListings from '$lib/components/karum/MyKarumListings.svelte';
  import KarumSellDialog from '$lib/components/karum/KarumSellDialog.svelte';
  import KarumRequestDialog from '$lib/components/karum/KarumRequestDialog.svelte';
  import KarumBuyDialog from '$lib/components/karum/KarumBuyDialog.svelte';
  import KarumFillDialog from '$lib/components/karum/KarumFillDialog.svelte';
  import KarumDetail from '$lib/components/karum/KarumDetail.svelte';

  const gate = useAuthGate();

  let linked = $derived(karum.status === 'ready' && gate.authed);
  let buyTarget = $state(null);
  let fillTarget = $state(null);
  let tradeForm = $state(null);
  let inspectTarget = $state(null);

  function openBuy(listing) { buyTarget = listing; }
  function closeBuy() { buyTarget = null; }
  function openFill(request) { fillTarget = request; }
  function closeFill() { fillTarget = null; }
  function toggleForm(name) { tradeForm = tradeForm === name ? null : name; }

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(() => loadAll({ preserveBoard: karum.board.status === 'ready' })); }
    // The board still loads for signed-out viewers; the stall seals.
    else if (status === 'anon' && !loaded) { loaded = true; untrack(loadBoard); }
  });
</script>

<svelte:head>
  <title>The Karum | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | player trade"
    title="The Karum"
    sub="A merchant quarter outside the CHOAM economy. List what you have or post what you need. Trades wait for another player, nothing expires, and the CHOAM bot cannot take them."
  />

  <div class="stack">
    <CarvedSlab sharp={true}>
      <!-- Buying needs a linked session and nothing else. It is a GIVE to the buyer, so
           unlike listing it is not offline-gated: you can buy while you are in game. -->
      <KarumBoard canBuy={linked} canFill={linked} onBuy={openBuy} onFill={openFill} onInspect={(row) => (inspectTarget = row)} />
    </CarvedSlab>

    {#if gate.loading}
      <p class="skeleton">loading</p>
    {:else if gate.anon}
      <SealedPanel
        status="empty" action="login" width="prose"
        emptyText="Sign in to buy, fill wanted orders, list from your CHOAM bank, or post what you need. Both sides must be linked because only linked accounts can be addressed."
      />
    {:else if karum.status === 'error'}
      <SealedPanel
        status="error" width="prose"
        errorText="Your stall could not be read right now. The board above is still current."
      />
    {:else}
      {#if karum.notice}
        <Notice tone={karum.noticeTone} text={karum.notice} />
      {/if}
      <CarvedSlab>
        <div class="stallhead">
          <div>
            <p class="banklabel mono">Banked Solari</p>
            <p class="bankval mono">{karum.bank == null ? '—' : karum.bank.toLocaleString()}</p>
          </div>
          <div class="actions">
            <button class="sell" type="button" onclick={() => toggleForm('sell')}
                    aria-expanded={tradeForm === 'sell'}>
              {tradeForm === 'sell' ? 'Close listing' : 'List an item'}
            </button>
            <button class="request" type="button" onclick={() => toggleForm('request')}
                    aria-expanded={tradeForm === 'request'}>
              {tradeForm === 'request' ? 'Close request' : 'Post a request'}
            </button>
          </div>
        </div>
        {#if tradeForm === 'sell'}
          <div class="sellwrap">
            <KarumSellDialog onClose={() => (tradeForm = null)} />
          </div>
        {:else if tradeForm === 'request'}
          <div class="sellwrap requestwrap">
            <KarumRequestDialog onClose={() => (tradeForm = null)} />
          </div>
        {/if}
        <MyKarumListings />
      </CarvedSlab>

      <p class="promise">
        For-sale goods enter escrow when listed. Wanted orders reserve no Solari when posted.
        When someone fills one, their item and the requester's payment commit together, then
        the existing claim lane delivers the item. No seller can take your Solari and keep the item.
      </p>
    {/if}
  </div>
</div>

{#if inspectTarget}
  <KarumDetail item={inspectTarget} canTrade={linked} onClose={() => (inspectTarget = null)} onTrade={(row) => { inspectTarget = null; if (row.request_id != null) openFill(row); else openBuy(row); }} />
{/if}
{#if buyTarget}
  <KarumBuyDialog listing={buyTarget} onClose={closeBuy} />
{/if}
{#if fillTarget}
  <KarumFillDialog request={fillTarget} onClose={closeFill} />
{/if}

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .stack { display: flex; flex-direction: column; gap: var(--space-4); }

  .stallhead { display: flex; align-items: flex-end; justify-content: space-between; gap: var(--space-4); flex-wrap: wrap; margin-bottom: var(--space-3); }
  .actions { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .banklabel { margin: 0; font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .bankval { margin: 0; font-size: var(--text-xl); color: var(--accent-bright); font-variant-numeric: tabular-nums; }
  .sell {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-4); cursor: pointer;
  }
  .sell:hover { filter: brightness(1.08); }
  .sell:active, .request:active { transform: translateY(1px); }
  .request {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent-bright); background: var(--metal-1); border: 1px solid var(--accent);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-4); cursor: pointer;
  }
  .request:hover { background: var(--metal-2); }
  .sellwrap {
    margin-bottom: var(--space-4); padding: var(--space-3);
    background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    max-width: 26rem;
  }
  .sellwrap.requestwrap { max-width: 38rem; }

  .promise { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.5; max-width: 68ch; }

  /* Stagger-rise the panels on enter (opacity/transform only). */
  .stack > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .stack > :global(*:nth-child(1)) { animation-delay: .04s; }
  .stack > :global(*:nth-child(2)) { animation-delay: .12s; }
  .stack > :global(*:nth-child(3)) { animation-delay: .18s; }
  @keyframes rise { to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) {
    .stack > :global(*) { opacity: 1; transform: none; animation: none; }
  }
  @media (max-width: 40rem) {
    .actions, .sell, .request { width: 100%; }
  }
</style>
