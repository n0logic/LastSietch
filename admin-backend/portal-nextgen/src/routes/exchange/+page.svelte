<script>
  // Exchange: the CHOAM market re-skinned in the carved-sietch / Solari V2 language.
  // TOP = the live pulse strip + the three HUD stat slabs (Solari gauge drains on
  // buy). LEFT = browse (search + category/kind tabs + the listing table). RIGHT =
  // the Bot-Floor flip board, your price alerts, and your listings. Clicking a
  // listing opens the spice-glass price ladder drawer with the inline buy panel.
  //
  // Reads are never gated (browsing always works). Buy is ONLINE-SAFE + optimistic;
  // selling is offline-gated server-side and lives on the Storage surface. Signed-out
  // seals to an honest Connect-Discord panel.
  import { untrack } from 'svelte';
  import { page } from '$app/state';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { exchange, loadAll, loadMore, openItem } from '$lib/exchange.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import MarketPulseStrip from '$lib/components/exchange/MarketPulseStrip.svelte';
  import MarketStatBar from '$lib/components/exchange/MarketStatBar.svelte';
  import BotLimits from '$lib/components/exchange/BotLimits.svelte';
  import BrowseSearch from '$lib/components/exchange/BrowseSearch.svelte';
  import CategoryTabs from '$lib/components/exchange/CategoryTabs.svelte';
  import ListingRow from '$lib/components/exchange/ListingRow.svelte';
  import PriceLadderDrawer from '$lib/components/exchange/PriceLadderDrawer.svelte';
  import FlipBoard from '$lib/components/exchange/FlipBoard.svelte';
  import AlertsFeed from '$lib/components/exchange/AlertsFeed.svelte';
  import WatchlistCard from '$lib/components/exchange/WatchlistCard.svelte';
  import MyOrdersTabs from '$lib/components/exchange/MyOrdersTabs.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';

  const gate = useAuthGate();

  let rows = $derived(exchange.browse.rows);
  let selectedTpl = $derived(exchange.selected.tpl);

  let loaded = false;
  let openedQuery = '';
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadAll); }
    else if (status === 'anon') { loaded = false; }
  });
  $effect(() => {
    const template = page.url.searchParams.get('tpl') || '';
    if (gate.authed && template && template !== openedQuery) {
      openedQuery = template;
      untrack(() => openItem(template));
    }
    if (!template) openedQuery = '';
  });
</script>

<svelte:head>
  <title>Exchange | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | CHOAM Exchange"
    title="Exchange"
    sub="Every open listing on the sietch market, the price ladder behind each item, and the Bot-Floor flips where a player ask sits under what CHOAM will pay. Watch a price, buy off the ladder, and manage your own listings."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to browse the CHOAM Exchange, buy off the ladder, and track your listings."
    />
  {:else if exchange.status === 'error'}
    <SealedPanel
      status="error"
      errorText="The Exchange could not be read right now. Try again in a moment."
    />
  {:else}
    {#if exchange.notice}
      <Notice tone={exchange.noticeTone} text={exchange.notice} />
    {/if}

    <div class="stack">
      <div><MarketPulseStrip /></div>
      <div><MarketStatBar /></div>
      <div><BotLimits /></div>
    </div>

    <div class="grid">
      <!-- LEFT: browse -->
      <div class="col left">
        <CarvedSlab sharp={true}>
          <div class="browse-head">
            <BrowseSearch />
            <CategoryTabs />
          </div>

          {#if exchange.browse.status === 'loading' && rows.length === 0}
            <div class="rows">
              {#each Array(8) as _, i (i)}<div class="row-skel skeleton"></div>{/each}
            </div>
          {:else if exchange.browse.status === 'error'}
            <SealedPanel
              status="error"
              errorText="The listings could not be read. Try another search."
            />
          {:else if rows.length === 0}
            <SealedPanel
              status="empty" action="none" art="nothing-found"
              emptyText="No listings match that. Try a different name or category."
            />
          {:else}
            <div class="rows" role="list">
              {#each rows as row (row.template_id)}
                <ListingRow {row} selected={selectedTpl === row.template_id} />
              {/each}
            </div>
            {#if exchange.browse.more}
              <button class="more-btn" type="button" onclick={loadMore} disabled={exchange.browse.status === 'loading'}>
                {exchange.browse.status === 'loading' ? 'Loading' : 'Load more'}
              </button>
            {/if}
          {/if}
        </CarvedSlab>
      </div>

      <!-- RIGHT: flips + alerts + my listings -->
      <div class="col right">
        <CarvedSlab sharp={true}><FlipBoard /></CarvedSlab>
        <CarvedSlab sharp={true}><AlertsFeed /></CarvedSlab>
        <CarvedSlab sharp={true}><WatchlistCard /></CarvedSlab>
        <CarvedSlab sharp={true}><MyOrdersTabs /></CarvedSlab>
      </div>
    </div>
  {/if}
</div>

<PriceLadderDrawer />

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .stack { margin-top: var(--space-4); display: flex; flex-direction: column; gap: var(--space-3); }

  .grid {
    margin-top: var(--space-4);
    display: grid; gap: var(--space-4);
    grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
    align-items: start;
  }
  .col { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }

  .browse-head { display: flex; flex-direction: column; gap: var(--space-3); margin-bottom: var(--space-4); }
  .rows { display: flex; flex-direction: column; gap: var(--space-1); }
  .row-skel { height: 52px; width: 100%; border-radius: var(--radius-sm); }
  .more-btn {
    margin-top: var(--space-3); align-self: center;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-5); cursor: pointer;
  }
  .more-btn:hover:not(:disabled) { border-color: var(--accent); }
  .more-btn:disabled { opacity: .5; cursor: not-allowed; }

  /* Stagger-rise the panels on enter (opacity/transform only). */
  .stack > :global(*), .col > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .stack > :global(*:nth-child(1)) { animation-delay: .02s; }
  .stack > :global(*:nth-child(2)) { animation-delay: .08s; }
  .col.left > :global(*) { animation-delay: .12s; }
  .col.right > :global(*:nth-child(1)) { animation-delay: .14s; }
  .col.right > :global(*:nth-child(2)) { animation-delay: .20s; }
  .col.right > :global(*:nth-child(3)) { animation-delay: .26s; }
  @keyframes rise { to { opacity: 1; transform: none; } }

  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
  }
  @media (prefers-reduced-motion: reduce) {
    .stack > :global(*), .col > :global(*) { opacity: 1; transform: none; animation: none; }
  }
</style>
