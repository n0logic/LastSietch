<script>
  // Community blueprint market: sort tabs + a closed tag rail over the public
  // gallery, a responsive card grid, and a load-more paginator. Reads are public;
  // the "Save" action on each card (linked viewers only) bubbles up to open the
  // import dialog.
  //
  // The rail replaced a free-text tag box. The filter matches a fixed server
  // vocabulary (purposes, then the derived structural tags, then the four size
  // bands), so anything typed outside it returned an empty gallery that read as
  // "nobody has built this" rather than "that is not a tag". A closed rail can
  // only ask questions the market can answer.
  import { onMount } from 'svelte';
  import { bases, loadMarket, loadMoreMarket, loadTagCatalog, setTag, sweepMissingThumbnails } from '$lib/bases.svelte.js';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import ListingCard from './ListingCard.svelte';

  let { linked = false, onImport } = $props();

  const SORTS = [
    { key: 'new', label: 'Newest' },
    { key: 'popular', label: 'Popular' },
  ];

  let listings = $derived(bases.market.listings);
  let activeTag = $derived(bases.market.tag || '');
  // Purposes lead (what the base is FOR), then what it is made of, then how big.
  let rail = $derived([
    ...(bases.tagCatalog?.purposes || []),
    ...(bases.tagCatalog?.tags || []),
    ...(bases.tagCatalog?.size_bands || []),
  ]);

  onMount(loadTagCatalog);

  // Admin-only preview backfill. The server decides the role; this gate only
  // hides a button that would 404 for anyone else.
  const adminGate = useAuthGate({ role: 'admin' });
  let sweepLabel = $derived(
    bases.sweep.status === 'running'
      ? `Rendering ${bases.sweep.done + bases.sweep.failed} of ${bases.sweep.total}`
      : bases.sweep.status === 'done'
        ? `Previews rendered: ${bases.sweep.done}${bases.sweep.failed ? `, ${bases.sweep.failed} failed` : ''}`
        : bases.sweep.status === 'error' ? 'Preview sweep failed' : 'Render missing previews'
  );

  function setSort(sort) {
    if (sort === bases.market.sort) return;
    loadMarket({ sort });
  }
</script>

<div class="gallery">
  <div class="head">
    <p class="panel-kicker mono">Community market</p>
    <div class="tabs" role="tablist" aria-label="Sort blueprints">
      {#each SORTS as s (s.key)}
        <button
          class="tab" class:on={bases.market.sort === s.key}
          role="tab" aria-selected={bases.market.sort === s.key}
          type="button" onclick={() => setSort(s.key)}
        >{s.label}</button>
      {/each}
    </div>
    {#if adminGate.allowed}
      <button
        class="sweep mono" type="button" onclick={sweepMissingThumbnails}
        disabled={bases.sweep.status === 'running'}
        title="Render a preview for every listing that has none (admin)"
      >{sweepLabel}</button>
    {/if}
  </div>

  {#if rail.length}
    <div class="rail" role="group" aria-label="Filter blueprints by tag">
      <button
        class="rchip" class:on={activeTag === ''}
        type="button" aria-pressed={activeTag === ''} onclick={() => setTag('')}
      >All</button>
      {#each rail as t (t)}
        <button
          class="rchip" class:on={activeTag === t}
          type="button" aria-pressed={activeTag === t} onclick={() => setTag(t)}
        >{t}</button>
      {/each}
    </div>
  {/if}

  {#if bases.market.status === 'loading' && listings.length === 0}
    <div class="grid">
      {#each Array(6) as _, i (i)}<div class="card-skel skeleton"></div>{/each}
    </div>
  {:else if bases.market.status === 'error'}
    <SealedPanel
      status="error"
      errorText="The market could not be read right now. Try again shortly."
    />
  {:else if listings.length === 0}
    <SealedPanel
      status="empty" action="none" art="no-listings"
      emptyText="No blueprints match that filter yet."
    />
  {:else}
    <div class="grid">
      {#each listings as listing (listing.publish_id)}
        <ListingCard {listing} {linked} {onImport} />
      {/each}
    </div>
    {#if bases.market.more}
      <button class="more-btn" type="button" onclick={loadMoreMarket} disabled={bases.market.status === 'loading'}>
        {bases.market.status === 'loading' ? 'Loading' : 'Load more'}
      </button>
    {/if}
  {/if}
</div>

<style>
  .head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; margin-bottom: var(--space-3); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .tabs { display: inline-flex; gap: var(--space-1); }
  .sweep { font-size: 11px; color: var(--text-muted); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 2px var(--space-2); background: transparent; cursor: pointer; }
  .sweep:disabled { cursor: progress; opacity: .7; }
  .tab {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    color: var(--text-muted); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-3); cursor: pointer;
  }
  .tab.on { color: var(--accent-bright); border-color: var(--accent); }

  /* One line that scrolls sideways. Wrapping put four rows of chips above the
     grid on a phone and pushed every listing below the fold. */
  .rail {
    display: flex; gap: var(--space-1); margin-bottom: var(--space-4);
    overflow-x: auto; overscroll-behavior-x: contain; scrollbar-width: thin;
    padding-bottom: var(--space-1);
  }
  .rchip {
    flex: 0 0 auto;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .06em;
    color: var(--text-muted); background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); cursor: pointer;
    white-space: nowrap;
  }
  .rchip:hover { color: var(--text); border-color: var(--edge-hi); }
  .rchip.on { color: var(--accent-bright); border-color: var(--accent); }
  .rchip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--space-3); }
  .card-skel { aspect-ratio: 16 / 13; border-radius: var(--radius-sm); }
  .more-btn {
    margin: var(--space-4) auto 0; display: block;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-5); cursor: pointer;
  }
  .more-btn:hover:not(:disabled) { border-color: var(--accent); }
  .more-btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
