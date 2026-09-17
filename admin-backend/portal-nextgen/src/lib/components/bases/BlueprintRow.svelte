<script>
  // One of the caller's OWN blueprint rows inside a character section. Shows the
  // base name + piece count and the row actions: Preview 3D, Rename (offline-
  // gated inline editor), Download (V1 binary endpoint), and Publish/Unpublish to
  // the community market (optimistic via the store). Published rows show the
  // listing's download count.
  //
  // Publish is no longer a one-click write. It opens PublishDialog through the
  // store so the player supplies the title, the description and the purpose tags
  // the market filters on, and sees the daily cap BEFORE the write. A published
  // row gets Edit, which is the same modal over the same endpoint: create_or_update
  // updates in place on (account_id, game_bp_id) and keeps the download count.
  import { bases, rename, unpublish, openPreview, openPublish } from '$lib/bases.svelte.js';

  let { bp, accountId, nameMax = 40 } = $props();

  let editing = $state(false);
  let draft = $state('');
  let busy = $state(false);

  let published = $derived(bp.published || null);
  let name = $derived(bp.name || `Blueprint #${bp.bp_id}`);
  // Publish leaves the row busy while the gallery thumbnail renders offscreen.
  let rendering = $derived(bases.thumbing === bp.bp_id);
  // The listing's own gallery plate, which the Data Saver path in ThreeDPreview
  // shows instead of downloading the meshes. Null until the row is published.
  let thumbUrl = $derived(published?.publish_id != null
    ? `/portal/solido/${published.publish_id}/thumb.png`
    : null);

  function fmt(n) { const v = Number(n) || 0; return v.toLocaleString(); }

  function preview() { openPreview({ bpId: bp.bp_id, title: name, thumbUrl }); }

  function startRename() { draft = bp.name || ''; editing = true; }
  function cancelRename() { editing = false; }
  async function commitRename() {
    if (busy) return;
    busy = true;
    const ok = await rename(bp.bp_id, draft);
    busy = false;
    if (ok) editing = false;
  }
  function onRenameKey(e) {
    if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
    else if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
  }

  async function onUnpublish() { if (busy) return; busy = true; await unpublish(bp.bp_id, published?.publish_id); busy = false; }
</script>

<li class="row">
  <div class="id">
    {#if editing}
      <!-- svelte-ignore a11y_autofocus -->
      <input
        class="rename" type="text" bind:value={draft} maxlength={nameMax}
        aria-label="New base name" placeholder="Base name"
        onkeydown={onRenameKey} autofocus
      />
      <div class="rename-actions">
        <button class="mini primary" type="button" onclick={commitRename} disabled={busy}>{busy ? 'Saving' : 'Save'}</button>
        <button class="mini" type="button" onclick={cancelRename} disabled={busy}>Cancel</button>
      </div>
    {:else}
      <span class="name" title={name}>{name}</span>
      <span class="meta mono">
        {fmt(bp.piece_count)} pieces
        {#if rendering}<span class="pub">· rendering preview…</span>
        {:else if published}<span class="pub">· published · &darr; {fmt(published.download_count)}</span>{/if}
      </span>
    {/if}
  </div>

  {#if !editing}
    <div class="actions">
      <button class="act" type="button" onclick={preview}>Preview 3D</button>
      <button class="act" type="button" onclick={startRename}>Rename</button>
      <a class="act" href={`/portal/my-bases/${bp.bp_id}/export`} download={`${name}.json`}>Download</a>
      {#if published}
        <button class="act" type="button" onclick={() => openPublish(bp)}>Edit listing</button>
        <button class="act warn" type="button" onclick={onUnpublish} disabled={busy}>Unpublish</button>
      {:else if bases.flags.publish_enabled}
        <button class="act primary" type="button" onclick={() => openPublish(bp)}>Publish</button>
      {/if}
    </div>
  {/if}
</li>

<style>
  .row {
    display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap;
    padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .id { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 12rem; }
  .name { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: var(--text-xs); color: var(--text-muted); }
  .pub { color: var(--ls-green); }
  .rename {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2);
  }
  .rename:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .rename-actions { display: flex; gap: var(--space-1); margin-top: var(--space-1); }
  .mini { font-family: var(--font-mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; padding: 2px var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .mini.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .actions { display: flex; gap: var(--space-1); flex-wrap: wrap; }
  .act {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .08em; text-transform: uppercase;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); cursor: pointer; text-decoration: none;
  }
  .act:hover:not(:disabled) { border-color: var(--accent); }
  .act.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .act.warn { color: var(--ls-red); }
  .act:disabled { opacity: .5; cursor: not-allowed; }
</style>
