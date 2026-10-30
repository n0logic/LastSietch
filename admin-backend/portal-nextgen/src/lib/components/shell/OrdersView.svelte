<script>
  // Trade > My orders (P3): the trader's own console that /economy carried, now a
  // tab beside the two markets. The same panels and the same gate split as that
  // page: the market strip is public, everything that reads YOUR numbers sits
  // under the login gate. The two-markets link row is gone; the tabs above are it.
  import { untrack } from 'svelte';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { loadAll as loadExchange } from '$lib/exchange.svelte.js';
  import { loadAll as loadStorage } from '$lib/storage.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import MarketPulseStrip from '$lib/components/exchange/MarketPulseStrip.svelte';
  import MarketStatBar from '$lib/components/exchange/MarketStatBar.svelte';
  import WatchlistCard from '$lib/components/exchange/WatchlistCard.svelte';
  import AlertsFeed from '$lib/components/exchange/AlertsFeed.svelte';
  import MyOrdersTabs from '$lib/components/exchange/MyOrdersTabs.svelte';
  import ChoamBankCard from '$lib/components/storage/ChoamBankCard.svelte';

  const gate = useAuthGate();

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) {
      loaded = true;
      untrack(loadExchange);
      untrack(loadStorage);
    } else if (status === 'anon') {
      loaded = false;
    }
  });
</script>

<svelte:head>
  <title>My orders | Trade | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | trade"
    title="My orders"
    sub="Your side of both markets: the CHOAM bank, the prices you are watching, the alerts that fired, and the listings you have standing."
  />

  <div class="strip"><MarketPulseStrip /></div>

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see your CHOAM bank, your price watches and your open listings. Browsing both markets works signed out."
    />
  {:else}
    <div class="stats"><MarketStatBar /></div>
    <div class="grid">
      <div class="col">
        <CarvedSlab sharp={true}><MyOrdersTabs /></CarvedSlab>
        <CarvedSlab sharp={true}><AlertsFeed /></CarvedSlab>
      </div>
      <div class="col">
        <CarvedSlab><ChoamBankCard /></CarvedSlab>
        <CarvedSlab sharp={true}><WatchlistCard /></CarvedSlab>
      </div>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .strip { display: flex; flex-direction: column; gap: var(--space-3); margin-bottom: var(--space-4); }
  .stats { margin-bottom: var(--space-4); }
  .grid { display: grid; gap: var(--space-4); grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); align-items: start; }
  .col { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
</style>
