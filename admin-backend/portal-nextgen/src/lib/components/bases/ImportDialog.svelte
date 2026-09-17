<script>
  // Import a blueprint to one of the caller's linked characters. Two entry paths:
  //  - Public: paste a public Solido link / UUID.
  //  - Private: arrived from a market card "Save" (listing prefilled, link hidden).
  // The player picks a destination character and a delivery (CHOAM bank, or the
  // offline-gated backpack). The server re-validates ownership + the offline gate;
  // this dialog surfaces the server envelope honestly. Chrome, focus trap, inert
  // background and Escape all come from the shared Modal primitive.
  //
  // Two things are shown BEFORE the write (wave 6): the daily import cap, so the
  // player is not told "limit reached" by a 429 they had no way to see coming,
  // and what is actually about to arrive. For one of our listings the census is
  // the card's own server-derived counts. For a pasted third-party link there is
  // nothing to count: the CSP forbids fetching it and the server only parses it
  // during the import, so the dialog says exactly that rather than showing zeros
  // that read as an empty base.
  import { bases, importBlueprint } from '$lib/bases.svelte.js';
  import { uuidv4 } from '$lib/api.js';
  import Modal from '$lib/components/Modal.svelte';

  // `source` is a V2 body fragment the Solido page already resolved (an upload's
  // blueprint_text or a pasted public link) and `label` its human name; the
  // dialog then spreads it instead of asking for a link.
  let { listing = null, source = null, label = '', onClose } = $props();

  // Private mode when opened from a market card; else a public link paste.
  let privateMode = $derived(listing != null);
  let resolved = $derived(!privateMode && source != null);
  let link = $state('');
  let sections = $derived(bases.sections || []);
  let accountId = $state(null);
  let delivery = $state('bank');
  let busy = $state(false);
  let phase = $state('idle'); // idle | ok | err
  let message = $state('');

  // One linked character is the common case: it is the destination, and the
  // picker says so instead of offering a choice of one.
  let onlyOne = $derived(sections.length === 1);
  let selectedSection = $derived(sections.find((s) => s.account_id === accountId) || sections[0] || null);
  let selectedOnline = $derived(selectedSection?.online === true);
  // Sections arrive with the overview, which can land after this dialog mounts.
  $effect(() => { if (accountId == null && sections.length) accountId = sections[0].account_id; });
  // Backpack delivery needs the character offline; fall back to bank when online.
  $effect(() => { if (selectedOnline && delivery === 'backpack') delivery = 'bank'; });

  let importCap = $derived(Number(bases.caps?.import_daily_cap) || 0);
  let importedToday = $derived(Number(bases.caps?.imported_today) || 0);
  let atCap = $derived(importCap > 0 && importedToday >= importCap);

  // The card fields the market already carries. Nothing is derived client-side:
  // a count that is absent stays absent rather than rendering as 0.
  let census = $derived(privateMode ? [
    { label: 'pieces', value: listing.piece_count },
    { label: 'structural', value: listing.instance_count },
    { label: 'placeables', value: listing.placeable_count },
    { label: 'pentashields', value: listing.pentashield_count },
  ].filter((c) => c.value) : []);

  let canSubmit = $derived(!busy && !atCap && accountId != null && (privateMode || resolved || link.trim().length > 0));

  function fmt(n) { return (Number(n) || 0).toLocaleString(); }

  // Stable idempotency key for the current logical send. Minted once and reused
  // across retries so a retry after a lost response replays against the writer's
  // ledger instead of importing twice. Reset on real success or a changed target.
  let idem = '';
  let idemFor = '';
  // A different resolved source is a different logical send, whatever its length.
  $effect(() => { source; idem = ''; idemFor = ''; });

  async function submit() {
    if (!canSubmit) return;
    busy = true; phase = 'idle'; message = '';
    const target = privateMode ? `p:${listing.publish_id}:${accountId}:${delivery}`
      : resolved ? `s:${source.source_type}:${label}:${(source.blueprint_text || source.link || '').length}:${accountId}:${delivery}`
        : `l:${link.trim()}:${accountId}:${delivery}`;
    if (idem === '' || idemFor !== target) { idem = uuidv4(); idemFor = target; }
    try {
      // The resolved source is spread FIRST so the destination, delivery and
      // idempotency key the dialog owns can never be overridden by a source key.
      const body = { ...(resolved ? source : {}), account_id: accountId, delivery, client_uuid: idem };
      if (privateMode) { body.source_type = 'private'; body.publish_id = listing.publish_id; }
      else if (!resolved) { body.source_type = 'public'; body.link = link.trim(); }
      const r = await importBlueprint(body);
      if (r?.ok) {
        phase = 'ok';
        message = r.message || 'Imported. Check your CHOAM bank or backpack in-game.';
        idem = ''; idemFor = '';   // logical send done; a fresh send mints a new key
        setTimeout(() => onClose?.(), 1400);
      } else {
        phase = 'err';
        message = (r && (r.error || r.message)) || 'Import failed.';
      }
    } catch (e) {
      phase = 'err';
      // 409 = character online while asking for backpack delivery; 429 = daily cap.
      if (e?.status === 409 || e?.message === 'player_online') {
        message = 'Log out of the game to deliver to the backpack, or choose bank delivery.';
      } else if (e?.status === 429) {
        message = 'Daily import limit reached. Try again tomorrow.';
      } else {
        message = 'Import failed. Check the link and try again.';
      }
    } finally { busy = false; }
  }
</script>

<Modal title="Import blueprint" size="md" {onClose}>
  <p class="target">{privateMode ? (listing.title || 'Saved base') : (label || 'From a link')}</p>

  {#if privateMode}
    {#if census.length}
      <p class="census mono">
        {#each census as c (c.label)}<span class="cstat"><b>{fmt(c.value)}</b> {c.label}</span>{/each}
      </p>
    {/if}
  {:else if !(resolved && source.source_type === 'upload')}
    <p class="census mono unknown">Census unavailable until imported.</p>
  {/if}

  {#if !privateMode && !resolved}
    <label class="fld"><span>Public link or UUID</span>
      <input type="text" bind:value={link} placeholder="A blueprint link or UUID" aria-label="Public blueprint link or UUID" />
    </label>
  {/if}

  {#if onlyOne && selectedSection}
    <p class="fld one">
      <span>Deliver to</span>
      <span class="onename">{selectedSection.character_name}{selectedSection.online ? ' (online)' : ''}</span>
    </p>
  {:else}
    <label class="fld"><span>Deliver to</span>
      <select bind:value={accountId} aria-label="Destination character">
        {#each sections as s (s.account_id)}
          <option value={s.account_id}>{s.character_name}{s.online ? ' (online)' : ''}</option>
        {/each}
      </select>
    </label>
  {/if}

  <fieldset class="fld delivery">
    <legend>Delivery</legend>
    <label class="radio"><input type="radio" name="delivery" value="bank" bind:group={delivery} /> CHOAM bank</label>
    <label class="radio" class:disabled={selectedOnline}>
      <input type="radio" name="delivery" value="backpack" bind:group={delivery} disabled={selectedOnline} /> Backpack
      <span class="note mono">{selectedOnline ? '(log out to use the backpack)' : '(works while offline)'}</span>
    </label>
  </fieldset>

  {#if importCap > 0}
    <p class="cap mono" class:atcap={atCap}>{importedToday} of {importCap} imports today</p>
  {/if}

  <div class="row">
    <button class="btn primary" type="button" onclick={submit} disabled={!canSubmit}>{busy ? 'Importing' : 'Import'}</button>
    <button class="btn" type="button" onclick={() => onClose?.()}>Cancel</button>
  </div>

  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</Modal>

<style>
  .target { margin: 0; font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .census { display: flex; flex-wrap: wrap; gap: var(--space-1) var(--space-3); margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .census .cstat b { font-weight: 400; color: var(--text); }
  .census.unknown { color: var(--text-muted); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; border: 0; padding: 0; margin: 0; }
  .fld input[type='text'], .fld select {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text); text-transform: none; letter-spacing: normal;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
  }
  .fld input:focus-visible, .fld select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .onename { font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text); text-transform: none; letter-spacing: normal; }
  .delivery { gap: var(--space-2); }
  .delivery legend { padding: 0; }
  .radio { display: flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm); color: var(--text); text-transform: none; letter-spacing: normal; }
  .radio.disabled { color: var(--text-muted); }
  .radio .note { color: var(--text-muted); font-size: var(--text-xs); }
  .cap { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .cap.atcap { color: var(--accent-text); }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='err'] { color: var(--ls-red); }
</style>
