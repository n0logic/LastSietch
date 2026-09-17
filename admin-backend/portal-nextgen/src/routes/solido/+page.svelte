<script>
  // Solido: the preview tool. Pick a blueprint, LOOK at it, then import it to
  // your own character.
  //
  // The page used to be an import form with no preview at all: paste something,
  // press Import, find out afterwards what you got. Now the source is resolved
  // and shown first, and Import is the last step rather than the only one.
  //
  // Three sources, and they are honest about what each one can show BEFORE the
  // import happens:
  //   * one of our own market listings  -> the real blueprint blob, so a full census;
  //   * a .json the player picked        -> parsed in the browser, so a full census;
  //   * a pasted third-party link/UUID   -> nothing. The CSP forbids fetching
  //     dune.layout.tools from here and we will not pretend otherwise, so the
  //     panel says the census is unavailable until it is imported.
  //
  // The write itself is ImportDialog on the V2 lane (/portal/bases/v2/import,
  // ruling 9.5): it owns the destination picker, the delivery gate and the
  // idempotency key, so this page never posts an import of its own. The V1 HTML
  // route stays live for Classic; nothing here touches it.
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { api } from '$lib/api.js';
  import { bases, loadAll, loadImports, openPreview } from '$lib/bases.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import ImportDialog from '$lib/components/bases/ImportDialog.svelte';
  import ThreeDPreview from '$lib/components/bases/ThreeDPreview.svelte';

  const gate = useAuthGate();

  // A memory guard, NOT a copy of the server's upload cap. The server owns the
  // real limit and refuses with its own message; this only stops the browser
  // reading something absurd into a string.
  const READ_MAX = 8 * 1024 * 1024;

  let sourceType = $state('listing');   // 'listing' | 'link' | 'file'
  let listingId = $state('');
  let link = $state('');
  let fileName = $state('');
  let blueprintText = $state('');
  let fileError = $state('');

  let previewPhase = $state('idle');    // idle | loading | ready | unavailable | error
  let previewNote = $state('');
  let census = $state(null);

  let importOpen = $state(false);

  let listings = $derived(bases.market.listings || []);
  let pickedListing = $derived(listings.find((l) => String(l.publish_id) === listingId) || null);
  let caps = $derived(bases.caps);

  let ready = $derived(
    sourceType === 'listing' ? !!pickedListing
      : sourceType === 'file' ? blueprintText.length > 0
        : link.trim().length > 0
  );

  // Counts straight off the blueprint object, the same three arrays the server
  // derives its card census from. Accepts the Solido envelope or the bare object.
  function censusOf(blob) {
    const b = blob && typeof blob.blueprint_data === 'object' ? blob.blueprint_data : blob;
    if (!b || typeof b !== 'object') return null;
    const instances = Array.isArray(b.instances) ? b.instances.length : 0;
    const placeables = Array.isArray(b.placeables) ? b.placeables.length : 0;
    const pentashields = Array.isArray(b.pentashields) ? b.pentashields.length : 0;
    return {
      pieces: instances + placeables + pentashields,
      structural: instances,
      placeables,
      pentashields,
    };
  }

  function resetPreview() {
    previewPhase = 'idle';
    previewNote = '';
    census = null;
  }

  function pickFile(e) {
    const f = e.target?.files?.[0] || null;
    fileError = '';
    fileName = '';
    blueprintText = '';
    resetPreview();
    if (!f) return;
    if (f.size > READ_MAX) {
      fileError = 'That file is too large to read here. Pick a Solido blueprint .json.';
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => { fileError = 'That file could not be read.'; };
    reader.onload = () => {
      blueprintText = String(reader.result || '');
      fileName = f.name;
    };
    reader.readAsText(f);
  }

  async function runPreview() {
    if (!ready) return;
    census = null;
    previewNote = '';
    if (sourceType === 'link') {
      previewPhase = 'unavailable';
      previewNote = 'A blueprint hosted somewhere else cannot be read from this page, so its census is unavailable until it is imported. The import itself still checks it.';
      return;
    }
    if (sourceType === 'file') {
      try {
        const parsed = JSON.parse(blueprintText);
        const c = censusOf(parsed);
        if (!c) throw new Error('shape');
        census = c;
        previewPhase = 'ready';
      } catch (e) {
        previewPhase = 'error';
        previewNote = 'That file is not a Solido blueprint. Export one from the game and try again.';
      }
      return;
    }
    previewPhase = 'loading';
    try {
      const blob = await api.bases.blueprint(pickedListing.publish_id);
      if (!blob || blob.available === false) {
        previewPhase = 'unavailable';
        previewNote = 'The blueprint file for that listing is no longer on disk, so it cannot be previewed or imported.';
        return;
      }
      census = censusOf(blob);
      previewPhase = census ? 'ready' : 'error';
      if (!census) previewNote = 'That listing could not be read as a blueprint.';
    } catch (e) {
      previewPhase = 'error';
      previewNote = 'That listing could not be read right now. Try again shortly.';
    }
  }

  // What ImportDialog is handed. A picked listing goes through the dialog's own
  // private lane; link and file are resolved here into the V2 body fragment the
  // dialog spreads: source_type plus the one field that matches it.
  let importSource = $derived(
    sourceType === 'link' ? { source_type: 'public', link: link.trim() }
      : sourceType === 'file' ? { source_type: 'upload', blueprint_text: blueprintText }
        : null
  );
  let importLabel = $derived(
    sourceType === 'listing' ? (pickedListing?.title || '')
      : sourceType === 'file' ? fileName : link.trim()
  );

  function fmt(n) { return (Number(n) || 0).toLocaleString('en-US'); }
  function when(iso) {
    if (!iso) return '';
    const t = Date.parse(String(iso).replace(' ', 'T'));
    return Number.isNaN(t) ? '' : new Date(t).toISOString().slice(0, 16).replace('T', ' ');
  }

  function onImported() {
    importOpen = false;
    loadImports();
  }

  let loaded = false;
  $effect(() => {
    if (gate.status === 'authed' && !loaded) {
      loaded = true;
      untrack(loadAll);      // sections + caps + the market rows the picker lists
      untrack(loadImports);
    }
  });

  // Changing the source invalidates whatever is on screen.
  $effect(() => { sourceType; untrack(resetPreview); });
</script>

<svelte:head>
  <title>Solido | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Solido blueprints"
    title="Solido import"
    sub="Preview a blueprint, then import it to your own character as a Solido tool, delivered to your CHOAM bank or your backpack. Pick one of our market listings, paste a dune.layout.tools link, or open a blueprint .json you already have."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in and link a character to import blueprints."
    />
  {:else if !bases.linked && bases.status === 'ready'}
    <SealedPanel
      status="empty" action="none" art="no-bases" slab={true} width="prose"
      emptyText="No linked character yet. Link one in Settings and this page can deliver a blueprint straight to it."
    />
  {:else}
    <div class="stack">
      <CarvedSlab>
        <p class="panel-kicker mono">1. Choose a blueprint</p>

        <div class="sources" role="radiogroup" aria-label="Blueprint source">
          <label>
            <input type="radio" bind:group={sourceType} value="listing" />
            One of our market listings
          </label>
          <label>
            <input type="radio" bind:group={sourceType} value="link" />
            Paste a link or UUID
          </label>
          <label>
            <input type="radio" bind:group={sourceType} value="file" />
            Open a .json
          </label>
        </div>

        {#if sourceType === 'listing'}
          <label class="field">
            Listing
            <select bind:value={listingId} onchange={resetPreview}>
              <option value="">Pick a listing</option>
              {#each listings as l (l.publish_id)}
                <option value={String(l.publish_id)}>{l.title || 'Untitled base'}</option>
              {/each}
            </select>
          </label>
          <p class="hint">
            The newest listings are offered here.
            <a href="{base}/bases">Browse the whole market</a> to find another one.
          </p>
        {:else if sourceType === 'link'}
          <label class="field">
            Link or UUID
            <input
              type="text" bind:value={link} oninput={resetPreview}
              placeholder="dune.layout.tools link or a bare UUID" autocomplete="off" />
          </label>
        {:else}
          <label class="field">
            Blueprint file
            <input type="file" accept="application/json,.json" onchange={pickFile} />
          </label>
          {#if fileName}<p class="hint">{fileName}</p>{/if}
          {#if fileError}<Notice tone="error" text={fileError} />{/if}
        {/if}

        <button class="btn" type="button" onclick={runPreview} disabled={!ready}>
          {previewPhase === 'loading' ? 'Reading' : 'Preview'}
        </button>
      </CarvedSlab>

      <CarvedSlab>
        <p class="panel-kicker mono">2. What you are about to import</p>

        {#if previewPhase === 'idle'}
          <SealedPanel
            status="empty" action="none" art="no-listings" width="prose"
            emptyText="Nothing chosen yet. Pick a source above and press Preview."
          />
        {:else if previewPhase === 'loading'}
          <SealedPanel status="loading" loadingText="reading the blueprint" />
        {:else if previewPhase === 'error'}
          <Notice tone="error" text={previewNote} />
        {:else}
          {#if importLabel}<p class="title">{importLabel}</p>{/if}
          {#if census}
            <dl class="census mono">
              <div><dt>pieces</dt><dd>{fmt(census.pieces)}</dd></div>
              <div><dt>structural</dt><dd>{fmt(census.structural)}</dd></div>
              <div><dt>placeables</dt><dd>{fmt(census.placeables)}</dd></div>
              <div><dt>pentashields</dt><dd>{fmt(census.pentashields)}</dd></div>
            </dl>
          {/if}
          {#if previewNote}<Notice tone="warn" text={previewNote} />{/if}
          {#if previewPhase === 'ready' && sourceType === 'listing' && pickedListing}
            <button
              class="btn ghost" type="button"
              onclick={() => openPreview({
                publishId: pickedListing.publish_id,
                title: pickedListing.title,
                thumbUrl: pickedListing.thumb_url,
              })}
            >Preview 3D</button>
          {/if}
        {/if}

        {#if caps.import_daily_cap > 0}
          <p class="cap mono">Imported today: {caps.imported_today} of {caps.import_daily_cap}</p>
        {/if}

        <button
          class="btn" type="button"
          disabled={!ready || previewPhase === 'idle' || previewPhase === 'loading'
                    || previewPhase === 'error' || !bases.flags.import_enabled}
          onclick={() => (importOpen = true)}
        >Import</button>
        {#if !bases.flags.import_enabled}
          <Notice tone="warn" text="Importing is switched off right now. It will come back." />
        {/if}
      </CarvedSlab>

      <CarvedSlab>
        <p class="panel-kicker mono">Recent imports</p>
        {#if bases.imports.status === 'loading'}
          <SealedPanel status="loading" loadingText="loading" />
        {:else if bases.imports.status === 'error'}
          <SealedPanel
            status="error"
            errorText="Your import history could not be read right now. Importing still works."
          />
        {:else if bases.imports.rows.length === 0}
          <SealedPanel
            status="empty" action="none" art="no-term" width="prose"
            emptyText="No imports yet. The ones you make show up here with where they were delivered."
          />
        {:else}
          <ul class="history">
            {#each bases.imports.rows as row, i (i)}
              <li>
                <span class="mono at">{when(row.at)}</span>
                <span class="what">{row.source_type} to {row.delivery}</span>
                <span class="res mono" data-res={row.result}>{row.result}</span>
                {#if row.message}<span class="msg">{row.message}</span>{/if}
              </li>
            {/each}
          </ul>
        {/if}
      </CarvedSlab>
    </div>
  {/if}
</div>

{#if importOpen}
  <ImportDialog
    listing={sourceType === 'listing' ? pickedListing : null}
    source={importSource}
    label={importLabel}
    onClose={onImported}
  />
{/if}
<ThreeDPreview />

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .stack { display: flex; flex-direction: column; gap: var(--space-4); }

  .panel-kicker { color: var(--accent); text-transform: uppercase; letter-spacing: .2em; font-size: 10px; margin: 0 0 var(--space-3); }

  .sources { display: flex; flex-wrap: wrap; gap: var(--space-1) var(--space-4); margin-bottom: var(--space-3); }
  .sources label, .field { font-size: var(--text-sm); }
  .field { display: block; max-width: 34rem; margin-bottom: var(--space-3); }
  .field input[type='text'], .field select {
    width: 100%; margin-top: var(--space-1); padding: var(--space-1) var(--space-2);
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .field input[type='file'] { display: block; margin-top: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); }
  .hint { margin: 0 0 var(--space-3); font-size: var(--text-xs); color: var(--text-muted); }
  .hint a { color: var(--accent-text); }

  .title { margin: 0 0 var(--space-2); font-size: var(--text-base); color: var(--text); overflow-wrap: anywhere; }
  .census { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); margin: 0 0 var(--space-3); font-size: var(--text-xs); }
  .census div { display: flex; flex-direction: column; gap: 2px; }
  .census dt { color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; font-size: 10px; }
  .census dd { margin: 0; color: var(--text); }

  .cap { margin: var(--space-3) 0 var(--space-2); font-size: var(--text-xs); color: var(--text-muted); }

  .history { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .history li { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-1) var(--space-3); font-size: var(--text-xs); }
  .at { color: var(--text-muted); }
  .what { color: var(--text); }
  .res { text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); }
  .res[data-res='ok'], .res[data-res='success'] { color: var(--ls-green); }
  .msg { flex: 1 1 100%; color: var(--text-muted); }

  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-2) var(--space-5); border-radius: var(--radius-sm);
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); cursor: pointer;
  }
  .btn.ghost { color: var(--text); background: var(--metal-1); border-color: var(--edge); }
  .btn:hover:not(:disabled) { filter: brightness(1.08); }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
