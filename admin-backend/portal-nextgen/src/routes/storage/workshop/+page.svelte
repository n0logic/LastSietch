<script>
  // Storage > Workshop: the two offline CHOAM bench services on one page.
  //
  //   Augments        the reroll/swap roll-up that used to live at
  //                   /storage/augments, mounted here unchanged. That route is
  //                   now a redirect into this tab, so every link that ever
  //                   pointed at it still lands somewhere true.
  //   Ingot Refinery  ingots plus Spice Melange into Spice-infused dust.
  //
  // The tab is in the URL (?tab=), not just in local state, so a refresh, a
  // bookmark and the redirect from /storage/augments all open the same panel.
  // Anything that is not a known tab falls back to Augments rather than
  // rendering nothing: an unknown query string is a typo, not an empty page.
  //
  // The strip is plain links with no click handler. Same route, different query,
  // so the client router swaps the panel without re-running anything, and the
  // tab is reachable by middle click, by keyboard and before hydration. Back
  // returns to the tab you came from, which is what a tab strip should do.
  //
  // Each tab keeps its OWN gate branches. They read different things (Augments
  // needs the storage overview, the refinery needs its own catalog), so one
  // shared "loading" would seal a panel that had already answered.
  import { untrack } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { storage, loadAll } from '$lib/storage.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import AugmentedItemsPanel from '$lib/components/storage/AugmentedItemsPanel.svelte';
  import RefineryPanel from '$lib/components/refinery/RefineryPanel.svelte';

  const TABS = [
    { key: 'augments', label: 'Augments' },
    { key: 'refinery', label: 'Ingot Refinery' },
  ];

  const gate = useAuthGate();

  let tab = $derived(
    TABS.some((t) => t.key === $page.url.searchParams.get('tab'))
      ? $page.url.searchParams.get('tab')
      : 'augments'
  );

  // The Augments panel reads the shared storage store, so this page boots it the
  // same way /storage and the old /storage/augments do: a direct link or a cold
  // refresh onto either tab still works.
  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadAll); }
    else if (status === 'anon') { loaded = false; }
  });
</script>

<svelte:head>
  <title>Workshop | Last Sietch</title>
</svelte:head>

<div class="page">
  <a class="crumb mono" href="{base}/storage">&larr; Storage</a>
  <PageHeader
    kicker="Last Sietch | CHOAM vault"
    title="Workshop"
    sub="The two bench services CHOAM runs for you while you are logged out: reroll and swap the augments on your gear, and refine base ingots into Spice-infused dust."
  />

  <nav class="tabs" aria-label="Workshop services">
    {#each TABS as t (t.key)}
      <a
        class="tab mono"
        class:on={tab === t.key}
        href="{base}/storage/workshop?tab={t.key}"
        aria-current={tab === t.key ? 'page' : undefined}
      >{t.label}</a>
    {/each}
  </nav>

  {#if tab === 'refinery'}
    <RefineryPanel />
  {:else if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see your augmented gear."
    />
  {:else if storage.status === 'error'}
    <SealedPanel
      status="error"
      errorText="Your storage could not be read right now. Try again in a moment."
    />
  {:else}
    <CarvedSlab sharp={true}>
      <AugmentedItemsPanel />
    </CarvedSlab>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .crumb {
    display: inline-block; margin-bottom: var(--space-2);
    color: var(--text-muted); font-size: var(--text-xs); text-decoration: none;
  }
  .crumb:hover { color: var(--accent-text); }

  .tabs { display: flex; gap: var(--space-2); margin-bottom: var(--space-4); flex-wrap: wrap; }
  .tab {
    text-decoration: none; font-size: var(--text-xs);
    text-transform: uppercase; letter-spacing: .1em;
    color: var(--text-muted); background: var(--metal-0);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-4);
  }
  .tab:hover { border-color: var(--accent); color: var(--accent-text); }
  .tab.on { color: var(--accent); border-color: var(--accent); background: var(--panel); }
</style>
