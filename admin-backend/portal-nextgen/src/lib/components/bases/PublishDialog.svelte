<script>
  // Publish (or edit) one of the caller's OWN bases as a market listing. The
  // button used to be a zero-input write: the server stored an empty title and an
  // empty description, so every card in the gallery was named after nothing and
  // the only way to tell two bases apart was the thumbnail.
  //
  // Three things the player supplies here, and nothing else: a title, a
  // description, and up to three PURPOSE tags from the server's closed
  // vocabulary. The blueprint itself is never uploaded from this dialog; the
  // server re-exports it, and the structural tags and size band stay derived from
  // that re-export. Editing an existing listing is the same form over the same
  // endpoint: create_or_update updates in place on (account_id, game_bp_id) and
  // keeps the download count.
  //
  // The daily cap is shown BEFORE the write and gates the button, because a 429
  // arriving after the player has typed a description is a refusal they had no
  // way to see coming. A refusal that still happens renders the SERVER's own
  // message; this dialog never invents one.
  //
  // Chrome, focus trap, inert background and Escape come from Modal.
  import { onMount, untrack } from 'svelte';
  import { bases, publishListing, loadDetail } from '$lib/bases.svelte.js';
  import Modal from '$lib/components/Modal.svelte';
  import Notice from '$lib/components/Notice.svelte';

  let { target, onClose } = $props();

  const TITLE_MAX = 80;    // blueprint_market.TITLE_MAX
  const DESC_MAX = 400;    // blueprint_market.DESC_MAX
  const TAGS_MAX = 3;      // purpose tags per listing (plan 3.3)

  let publishedId = $derived(target?.published?.publish_id ?? null);
  let isEdit = $derived(publishedId != null);

  // Seeded ONCE. The dialog is mounted per open, so the form is the player's
  // draft from here on and must not be reset under them by a store refresh.
  const seed = untrack(() => target) || {};
  let title = $state(String(seed.published?.title || seed.name || '').slice(0, TITLE_MAX));
  let description = $state(String(seed.published?.description || '').slice(0, DESC_MAX));
  let tags = $state([...(seed.published?.user_tags || [])].slice(0, TAGS_MAX));

  // 'ready' once the form holds what is actually published. 'unknown' when this
  // is an edit and the published text could not be read: the player is told
  // plainly that saving replaces it, rather than being handed a blank box that
  // silently wipes a description they wrote.
  let prefill = $state('ready');
  let busy = $state(false);
  let refusal = $state('');

  let purposes = $derived(bases.tagCatalog?.purposes || []);
  let cap = $derived(Number(bases.caps?.publish_daily_cap) || 0);
  let usedToday = $derived(Number(bases.caps?.published_today) || 0);
  let atCap = $derived(cap > 0 && usedToday >= cap);
  let full = $derived(tags.length >= TAGS_MAX);
  let canSubmit = $derived(!busy && !atCap && title.trim().length > 0);

  // An edit needs the text that is live on the listing, and the overview row does
  // not always carry it. The detail card does.
  onMount(async () => {
    if (!isEdit || target?.published?.title != null) return;
    prefill = 'loading';
    try { await loadDetail(publishedId); } catch (e) { /* handled below */ }
    const d = bases.detail;
    if (d && String(d.publish_id) === String(publishedId)) {
      title = String(d.title || title).slice(0, TITLE_MAX);
      description = String(d.description || '').slice(0, DESC_MAX);
      tags = [...(d.user_tags || [])].slice(0, TAGS_MAX);
      prefill = 'ready';
    } else {
      prefill = 'unknown';
    }
  });

  function toggleTag(t) {
    if (tags.includes(t)) tags = tags.filter((x) => x !== t);
    else if (!full) tags = [...tags, t];
  }

  async function submit() {
    if (!canSubmit) return;
    // The store's notice is global and outlives the write that set it. Cleared
    // here so a refusal below renders THIS attempt's message and never an older
    // one that has nothing to do with the button just pressed.
    busy = true; refusal = ''; bases.notice = '';
    try {
      const r = await publishListing({
        game_bp_id: target.bp_id,
        title: title.trim(),
        description: description.trim(),
        tags,
      });
      // publishListing resolves true only when the server accepted the write
      // and never throws on a refusal; anything else keeps the modal (and the
      // typed text) open, with the store's notice carrying the server's message.
      if (r !== true) {
        refusal = bases.notice || 'The listing was refused. Nothing changed.';
        return;
      }
      onClose?.();
    } catch (e) {
      refusal = e?.data?.message || e?.data?.error || e?.message
        || 'The listing was refused. Nothing changed.';
    } finally { busy = false; }
  }
</script>

<Modal title={isEdit ? 'Edit listing' : 'Publish to the market'} size="lg" {onClose}>
  <p class="target">{target?.name || `Blueprint #${target?.bp_id}`}</p>

  {#if prefill === 'loading'}
    <p class="hint mono">reading the published text</p>
  {:else if prefill === 'unknown'}
    <Notice tone="warn" text="The text already on this listing could not be read. Saving now replaces the title and the description with what is in this form." />
  {/if}

  <label class="fld"><span>Title</span>
    <input type="text" bind:value={title} maxlength={TITLE_MAX} placeholder="Name this base for the market" />
    <span class="count mono">{title.length} of {TITLE_MAX}</span>
  </label>

  <label class="fld"><span>Description</span>
    <textarea bind:value={description} maxlength={DESC_MAX} rows="4" placeholder="What is it for, what is inside, anything a builder should know."></textarea>
    <span class="count mono">{description.length} of {DESC_MAX}</span>
  </label>

  {#if purposes.length}
    <fieldset class="fld tagset">
      <legend>Purpose <span class="count mono">{tags.length} of {TAGS_MAX}</span></legend>
      <div class="chips">
        {#each purposes as t (t)}
          <button
            class="chip" class:on={tags.includes(t)}
            type="button" aria-pressed={tags.includes(t)}
            disabled={full && !tags.includes(t)}
            onclick={() => toggleTag(t)}
          >{t}</button>
        {/each}
      </div>
      <p class="hint mono">Structural tags and the size band stay server-derived.</p>
    </fieldset>
  {/if}

  {#if cap > 0}
    <p class="cap mono" class:atcap={atCap}>{usedToday} of {cap} published today</p>
  {/if}

  <div class="row">
    <button class="btn primary" type="button" onclick={submit} disabled={!canSubmit}>
      {busy ? 'Saving' : (isEdit ? 'Save listing' : 'Publish')}
    </button>
    <button class="btn" type="button" onclick={() => onClose?.()}>Cancel</button>
  </div>

  {#if atCap}
    <Notice tone="warn" text="You have used today's publish allowance. Try again tomorrow." />
  {/if}
  {#if refusal}
    <Notice tone="error" text={refusal} />
  {/if}
</Modal>

<style>
  .target { margin: 0; font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; border: 0; padding: 0; margin: 0; }
  .fld input[type='text'], .fld textarea {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text); text-transform: none; letter-spacing: normal;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
  }
  .fld textarea { resize: vertical; min-height: 5rem; line-height: 1.45; }
  .fld input:focus-visible, .fld textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .count { align-self: flex-end; font-size: 10px; color: var(--text-muted); letter-spacing: .04em; }
  .tagset legend { padding: 0; display: flex; align-items: baseline; gap: var(--space-2); }
  .chips { display: flex; flex-wrap: wrap; gap: var(--space-1); margin-top: var(--space-1); }
  .chip {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .06em; text-transform: none;
    color: var(--text-muted); background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); cursor: pointer;
  }
  .chip:hover:not(:disabled) { color: var(--text); border-color: var(--edge-hi); }
  .chip.on { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .chip:disabled { opacity: .4; cursor: not-allowed; }
  .chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .hint { margin: var(--space-1) 0 0; font-size: 10px; color: var(--text-muted); text-transform: none; letter-spacing: .04em; }
  .cap { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .cap.atcap { color: var(--accent-text); }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
</style>
