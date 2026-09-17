<script>
  // Bases: the Solido blueprint domain re-skinned in the carved-sietch language.
  // TOP = the community market gallery (public, sort + tag filter, card grid with
  // a 3D box preview and a Save-to-character action). Below it, for linked
  // players, "My bases" lists every linked character's blueprints with rename,
  // download, publish/unpublish, and a 3D preview. A publish-cap line surfaces the
  // rolling daily limit. The 3D preview + import open as focus-trapped modals.
  //
  // Scope = all-linked accounts (V1 parity). Reads are public/session + never
  // gated; writes are CSRF + uuid-idempotent (optimistic w/ rollback in the store).
  // Signed-out still sees the public market; the My Bases half seals to a panel.
  import { untrack } from 'svelte';
  import { page } from '$app/stores';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { bases, loadAll, loadMarket, loadTagCatalog, closePublish } from '$lib/bases.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import MarketGallery from '$lib/components/bases/MarketGallery.svelte';
  import MyBasesList from '$lib/components/bases/MyBasesList.svelte';
  import ImportDialog from '$lib/components/bases/ImportDialog.svelte';
  import PublishDialog from '$lib/components/bases/PublishDialog.svelte';
  import ThreeDPreview from '$lib/components/bases/ThreeDPreview.svelte';

  const gate = useAuthGate();

  // ?tag= deep link, both directions. Read once here BEFORE the first market
  // read, so the gallery opens on the filtered page instead of loading
  // everything and then reloading; written back by setTag on every chip click.
  // The URL is the truth at both ends, which is why arriving with no ?tag= also
  // clears a filter the module store kept from an earlier visit.
  const deepTag = ($page.url.searchParams.get('tag') || '').trim().slice(0, 40);
  bases.market.tag = deepTag;

  let linked = $derived(bases.linked && gate.authed);
  let cap = $derived(bases.caps);

  // Import dialog: null (closed) | { listing } (private, from a Save) | {} (public).
  let importTarget = $state(null);
  function openImport(listing) { importTarget = { listing: listing || null }; }
  function closeImport() { importTarget = null; }

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status !== 'authed' && status !== 'anon') return;
    if (loaded) return;
    loaded = true;
    // The public market still loads for signed-out viewers (My Bases seals), and
    // so does the filter vocabulary the chip rail is built from.
    untrack(status === 'authed' ? loadAll : loadMarket);
    untrack(loadTagCatalog);
  });
</script>

<svelte:head>
  <title>Bases | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Solido blueprints"
    title="Bases"
    sub="Browse the community blueprint market, open a listing for its full census, preview it in 3D, download the .json, or save it straight to your character. Signed in, manage your own bases: rename them, and publish one to the market with a title, a description and up to three purpose tags."
  />

  <div class="stack">
    <!-- Community market (public) -->
    <CarvedSlab sharp={true}>
      <MarketGallery linked={linked} onImport={openImport} />
    </CarvedSlab>

    {#if gate.loading}
      <p class="skeleton">loading</p>
    {:else if gate.anon}
      <SealedPanel
        status="empty" action="login"
        emptyText="Sign in to manage your own bases: rename, download, and publish them."
      />
    {:else if bases.status === 'error'}
      <SealedPanel
        status="error"
        errorText="Your bases could not be read right now. The market above is still current."
      />
    {:else}
      {#if bases.notice}
        <Notice tone={bases.noticeTone} text={bases.notice} />
      {/if}
      <CarvedSlab>
        {#if cap.publish_daily_cap > 0}
          <p class="cap mono">Published today: {cap.published_today} of {cap.publish_daily_cap}</p>
        {/if}
        <MyBasesList sections={bases.sections} nameMax={cap.rename_name_max} />
      </CarvedSlab>
    {/if}
  </div>
</div>

{#if importTarget}
  <ImportDialog listing={importTarget.listing} onClose={closeImport} />
{/if}
{#if bases.publishTarget}
  <PublishDialog target={bases.publishTarget} onClose={closePublish} />
{/if}
<ThreeDPreview />

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .stack { display: flex; flex-direction: column; gap: var(--space-4); }
  .cap { margin: 0 0 var(--space-3); font-size: var(--text-xs); color: var(--text-muted); }

  /* Stagger-rise the panels on enter (opacity/transform only). */
  .stack > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .stack > :global(*:nth-child(1)) { animation-delay: .04s; }
  .stack > :global(*:nth-child(2)) { animation-delay: .12s; }
  @keyframes rise { to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) {
    .stack > :global(*) { opacity: 1; transform: none; animation: none; }
  }
</style>
