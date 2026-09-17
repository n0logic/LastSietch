<script>
  // One community-market blueprint, deep-linkable at /bases/<id>. This
  // is the page V1 had and V2 shipped without: the gallery card was the only view
  // of a listing, so nothing could be linked, shared or downloaded.
  //
  // Fully PUBLIC. A signed-out visitor reads the whole card and can download the
  // .json; only Import is gated, because importing writes to a character.
  //
  // Two things are deliberate here:
  //   * The id is validated against a plain-integer pattern BEFORE any request.
  //     A junk or enormous id never becomes a lookup, and never reaches copy.
  //   * A missing listing seals to one panel that says nothing about WHICH id was
  //     asked for. Removed, hidden and never-existed are one answer, because
  //     telling them apart is exactly what a probe wants.
  //
  // The description is player-authored and is rendered as TEXT. It is never
  // {@html}: a market listing is not a place to let a stranger write markup into
  // another player's page.
  import { untrack } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { api } from '$lib/api.js';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import {
    bases, loadDetail, loadTagCatalog, loadOverview, openPreview,
  } from '$lib/bases.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import ImportDialog from '$lib/components/bases/ImportDialog.svelte';
  import ThreeDPreview from '$lib/components/bases/ThreeDPreview.svelte';

  const gate = useAuthGate();

  // publish_id only: a plain positive integer, bounded so a giant string is a
  // client-side refusal rather than a request the backend has to reject.
  let id = $derived(
    /^[1-9][0-9]{0,11}$/.test(String($page.params.id || '')) ? String($page.params.id) : ''
  );

  // `bases.detail` IS the card dict plus a status, so a ready listing is read
  // straight off it. The store outlives the route, so a listing whose publish_id
  // is not the one in the URL is the PREVIOUS page's, still loaded: it reads as
  // loading rather than flashing the wrong base for a frame.
  let stale = $derived(
    bases.detail.status === 'ready' && String(bases.detail.publish_id ?? '') !== id
  );
  let phase = $derived(!id ? 'missing' : stale ? 'loading' : bases.detail.status);
  let listing = $derived(phase === 'ready' ? bases.detail : null);

  let importOpen = $state(false);
  let copied = $state('');
  let copyTimer;

  let canImport = $derived(gate.authed && bases.linked && bases.flags.import_enabled);

  // The full census: every count the payload supplies, zeros included. A card
  // hides a zero as noise; a detail page saying "0 pentashields" is an answer.
  let census = $derived(listing ? [
    { label: 'pieces', value: listing.piece_count },
    { label: 'structural', value: listing.instance_count },
    { label: 'placeables', value: listing.placeable_count },
    { label: 'pentashields', value: listing.pentashield_count },
  ].filter((c) => c.value != null) : []);

  // The size band is one of the derived tags; which strings ARE size bands is
  // the server's vocabulary, never a list retyped here.
  let sizeBand = $derived(
    (listing?.tags || []).find((t) => (bases.tagCatalog.size_bands || []).includes(t)) || ''
  );
  // 'neutral' is the backend's stand-in for "unset", so it earns no chip.
  let faction = $derived(
    listing?.faction && listing.faction !== 'neutral' ? listing.faction : ''
  );

  function fmt(n) {
    const v = Number(n) || 0;
    return v.toLocaleString('en-US');
  }
  function when(iso) {
    if (!iso) return '';
    const t = Date.parse(String(iso).replace(' ', 'T'));
    return Number.isNaN(t) ? '' : new Date(t).toISOString().slice(0, 10);
  }

  function preview() {
    if (!listing) return;
    openPreview({
      publishId: listing.publish_id, title: listing.title, thumbUrl: listing.thumb_url,
    });
  }

  async function copyShare() {
    const url = listing?.share_url || '';
    if (!url) return;
    clearTimeout(copyTimer);
    try {
      await navigator.clipboard.writeText(url);
      copied = 'ok';
    } catch (e) {
      // Clipboard access is denied outright in some browsers and in every
      // insecure context. The link is on screen either way, so say so.
      copied = 'err';
    }
    copyTimer = setTimeout(() => { copied = ''; }, 2600);
  }

  let loadedId = '';
  $effect(() => {
    const want = id;
    if (!want || want === loadedId) return;
    loadedId = want;
    untrack(() => { loadDetail(want); loadTagCatalog(); });
  });

  // Import needs the caller's linked characters, which only the overview carries.
  // Guarded on `idle` so arriving from the gallery does not refetch it.
  let booted = false;
  $effect(() => {
    if (gate.status === 'authed' && !booted && bases.status === 'idle') {
      booted = true;
      untrack(loadOverview);
    }
  });

  $effect(() => () => clearTimeout(copyTimer));
</script>

<svelte:head>
  <title>{listing?.title ? `${listing.title} | Last Sietch` : 'Blueprint | Last Sietch'}</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Solido blueprints"
    title={listing?.title || 'Blueprint'}
  >
    <a class="back mono" href="{base}/bases">Back to the market</a>
  </PageHeader>

  {#if phase === 'loading' || phase === 'idle'}
    <SealedPanel status="loading" loadingText="loading" slab={true} />
  {:else if phase === 'missing'}
    <SealedPanel
      status="empty" action="none" art="no-listings" slab={true} width="prose"
      emptyText="That blueprint is not on the market. It may have been taken down by its author, or the link may be out of date. The market is still there."
    />
  {:else if phase === 'error'}
    <SealedPanel
      status="error" slab={true} width="prose"
      errorText="That blueprint could not be read right now. Try again shortly."
    />
  {:else if listing}
    <CarvedSlab>
      <div class="layout">
        <figure class="shot" class:empty={!listing.thumb_url}>
          {#if listing.thumb_url}
            <img src={listing.thumb_url} alt="" loading="lazy" decoding="async" />
          {:else}
            <span class="noart mono">no preview</span>
          {/if}
          {#if listing.has_paid_pieces}<span class="mtx mono">MTX</span>{/if}
        </figure>

        <div class="facts">
          <p class="byline mono">
            <span>by {listing.author_name || 'a Fremen builder'}</span>
            <span class="dl" title="Downloads">{fmt(listing.download_count)} downloads</span>
            {#if when(listing.created_at)}
              <time datetime={listing.created_at}>{when(listing.created_at)}</time>
            {/if}
          </p>

          {#if listing.description}
            <p class="desc">{listing.description}</p>
          {/if}

          <dl class="census mono">
            {#each census as c (c.label)}
              <div><dt>{c.label}</dt><dd>{fmt(c.value)}</dd></div>
            {/each}
            {#if sizeBand}<div><dt>size</dt><dd>{sizeBand}</dd></div>{/if}
            {#if faction}<div><dt>faction</dt><dd class="cap">{faction}</dd></div>{/if}
          </dl>

          {#if listing.tags?.length}
            <div class="tags">
              {#each listing.tags as t, i (i)}
                <a class="chip" href="{base}/bases?tag={encodeURIComponent(t)}">{t}</a>
              {/each}
            </div>
          {/if}

          <div class="actions">
            <button class="btn" type="button" onclick={preview}>Preview 3D</button>
            <a class="btn ghost" href={api.bases.download(id)} download>Download .json</a>
            {#if canImport}
              <button class="btn ghost" type="button" onclick={() => (importOpen = true)}>
                Save to character
              </button>
            {/if}
          </div>

          {#if listing.share_url}
            <div class="share">
              <p class="panel-kicker mono">Share this blueprint</p>
              <div class="sharerow">
                <code class="url">{listing.share_url}</code>
                <button class="btn ghost" type="button" onclick={copyShare}>Copy link</button>
              </div>
              {#if copied === 'ok'}
                <Notice tone="ok" text="Link copied." />
              {:else if copied === 'err'}
                <Notice tone="warn" text="This browser would not let us copy for you. Select the link above and copy it by hand." />
              {/if}
            </div>
          {/if}
        </div>
      </div>
    </CarvedSlab>
  {/if}
</div>

{#if importOpen && listing}
  <ImportDialog {listing} onClose={() => (importOpen = false)} />
{/if}
<ThreeDPreview />

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .back {
    display: inline-block; font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; color: var(--text-muted); text-decoration: none;
  }
  .back:hover { color: var(--accent-text); }

  .layout { display: grid; gap: var(--space-5); grid-template-columns: 1fr; }
  @media (min-width: 860px) { .layout { grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); } }

  .shot {
    position: relative; margin: 0; aspect-ratio: 16 / 10; overflow: hidden;
    background: var(--bg-deep); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    display: flex; align-items: center; justify-content: center;
  }
  .shot img { width: 100%; height: 100%; object-fit: cover; }
  .noart { color: var(--text-muted); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em; }
  .mtx {
    position: absolute; top: 8px; right: 8px; font-size: 10px; color: var(--bg-deep);
    background: var(--accent-bright); border-radius: var(--radius-sm); padding: 1px var(--space-1);
  }

  .facts { display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
  .byline { display: flex; flex-wrap: wrap; gap: var(--space-1) var(--space-3); font-size: var(--text-xs); color: var(--text-muted); margin: 0; }
  .desc { margin: 0; font-size: var(--text-sm); line-height: 1.55; color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }

  .census { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); margin: 0; font-size: var(--text-xs); }
  .census div { display: flex; flex-direction: column; gap: 2px; }
  .census dt { color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; }
  .census dd { margin: 0; color: var(--text); }
  .census dd.cap { text-transform: capitalize; }

  .tags { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .chip {
    font-size: var(--text-xs); color: var(--text-muted); text-decoration: none;
    border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 2px var(--space-2);
  }
  .chip:hover { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 50%, var(--edge)); }

  .actions { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-1); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-2) var(--space-4); border-radius: var(--radius-sm);
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
    cursor: pointer; text-decoration: none;
  }
  .btn.ghost { color: var(--text); background: var(--metal-1); border-color: var(--edge); }
  .btn:hover { filter: brightness(1.08); }
  .btn.ghost:hover { border-color: var(--accent); filter: none; }

  .share { margin-top: var(--space-2); }
  .panel-kicker { color: var(--accent); text-transform: uppercase; letter-spacing: .2em; font-size: 10px; margin: 0 0 var(--space-1); }
  .sharerow { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
  .url {
    flex: 1 1 16rem; min-width: 0; overflow-x: auto; white-space: nowrap;
    font-family: var(--font-mono); font-size: var(--text-xs); color: var(--text-muted);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2);
  }
</style>
