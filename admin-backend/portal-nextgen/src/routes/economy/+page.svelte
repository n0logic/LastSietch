<script>
  // Economy hub: the landing page the "Economy" nav label points at. It is the
  // trader's own console, not a third market. Everything that BROWSES a market
  // stays on /exchange and /karum; what lives here is the state that belongs to
  // you across both of them: the bank, your watches, your fired alerts and your
  // open listings.
  //
  // The gate splits on whose number it is, not on how the panel looks (owner
  // ruling 2026-09-03). MarketPulseStrip reads the market at large, so it is
  // public and gives the page a shape before auth resolves. MarketStatBar reads
  // YOUR balance, YOUR watches and YOUR listings: rendered for a signed-out
  // viewer it says "0 Solari" about somebody with no account attached, which is
  // a fabricated statement, not an empty one. It sits under the gate with the
  // four panels, and anon gets one honest sealed panel instead of five empty ones.
  //
  // The fired-alerts count is the layout's, not a second read: the shell already
  // calls refreshAlerts() once the viewer is known, and a second count here would
  // be a different number the moment either one went stale.
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { exchange, loadAll as loadExchange } from '$lib/exchange.svelte.js';
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

  // ChoamBankCard reads the storage store (the bank is a storage read, not a
  // market one), so both stores boot here or the card renders an empty console.
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

  let firedAlerts = $derived(gate.authed ? (exchange.firedCount || 0) : 0);
</script>

<svelte:head>
  <title>Economy | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | trade"
    title="Economy"
    sub="Your side of both markets in one place: the CHOAM bank, the prices you are watching, the alerts that fired, and the listings you have standing."
  />

  <div class="strip">
    <MarketPulseStrip />
  </div>

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
        <CarvedSlab><ChoamBankCard /></CarvedSlab>
        <CarvedSlab sharp={true}><WatchlistCard /></CarvedSlab>
      </div>
      <div class="col">
        <CarvedSlab sharp={true}><AlertsFeed /></CarvedSlab>
        <CarvedSlab sharp={true}><MyOrdersTabs /></CarvedSlab>
      </div>
    </div>
  {/if}

  <h2 class="section-label mono">The two markets</h2>
  <div class="ways">
    <a class="way" href={`${base}/exchange`}>
      <span class="way-name">The Exchange</span>
      <span class="way-sub">CHOAM prices, the ladder, and the Bot-Floor flips.</span>
      {#if firedAlerts > 0}
        <span class="way-badge mono" aria-label="{firedAlerts} price alerts fired">{firedAlerts > 99 ? '99+' : firedAlerts}</span>
      {/if}
    </a>
    <a class="way" href={`${base}/karum`}>
      <span class="way-name">The Karum</span>
      <span class="way-sub">Player to player, at a fixed ask, with wanted orders.</span>
    </a>
  </div>
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .strip { display: flex; flex-direction: column; gap: var(--space-3); margin-bottom: var(--space-4); }
  .stats { margin-bottom: var(--space-4); }

  .grid {
    display: grid; gap: var(--space-4);
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    align-items: start;
  }
  .col { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }

  .section-label {
    margin: var(--space-7) 0 var(--space-3); font-size: var(--text-xs);
    letter-spacing: .28em; text-transform: uppercase; color: var(--text-muted); opacity: .8;
  }
  .ways { display: grid; gap: var(--space-3); grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
  .way {
    position: relative; display: flex; flex-direction: column; gap: var(--space-1);
    padding: var(--space-3) var(--space-4);
    background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    text-decoration: none; color: var(--text);
    transition: border-color var(--motion-fast) var(--ease-out);
  }
  .way:hover { border-color: var(--accent); }
  .way-name { font-family: var(--font-display); letter-spacing: .06em; text-transform: uppercase; }
  .way-sub { color: var(--text-muted); font-size: var(--text-sm); line-height: 1.4; }
  /* A count of alerts that have actually fired, so it earns Ibad like the nav badge. */
  .way-badge {
    position: absolute; top: var(--space-3); right: var(--space-3);
    min-width: 1.05rem; height: 1.05rem; padding: 0 3px;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; line-height: 1; color: var(--bg-deep);
    background: var(--ls-ibad); border-radius: 999px;
  }
</style>
