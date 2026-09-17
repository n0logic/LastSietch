<script>
  // The operator's create / edit / cancel form for one event, inside the shared
  // Modal (focus trap, inert background, Escape, portal to body all come from
  // there). The ADMIN GATE IS THE ROUTE'S, not this component's: nothing here
  // decides who may write, and nothing here writes. Every field goes back out
  // through `onSubmit` and `onCancelEvent`; there is no api import and no fetch.
  //
  // Times: the operator types in their own clock (datetime-local is the only
  // input that behaves like a wall clock), and this converts to the one wire
  // format the whole wave uses, "YYYY-MM-DD HH:MM:SSZ". An edit converts back the
  // other way so the boxes show what the operator would have typed. UTC never
  // reaches the input and local time never reaches the payload.
  //
  // The banner is picked from the `banners` prop by SLUG. No free text: a typed
  // path is what the render-side refusal in EventBanner exists to catch, and this
  // form should never be the thing that produces one. The preview above the
  // picker is the real component, so what the operator sees is what publishes.
  //
  // Cancelling is destructive and irreversible (everyone holding a reminder is
  // notified), so it is a separate section behind a typed CANCEL, never the
  // primary button's second meaning.
  import { untrack } from 'svelte';
  import Modal from '$lib/components/Modal.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import EventBanner from './EventBanner.svelte';

  let {
    open = false,
    mode = 'create',       // 'create' | 'edit'
    event = null,
    kinds = [],
    banners = [],
    busy = false,
    error = '',
    onSubmit,
    onCancelEvent,
    onClose,
  } = $props();

  // Mirrors portal_events column limits (plan section 4).
  const TITLE_MAX = 200;
  const DESC_MAX = 4000;
  const FIELD_MAX = 120;
  const REASON_MAX = 200;
  const CONFIRM_WORD = 'CANCEL';

  const KIND_LABELS = {
    community: 'Community',
    faction_war: 'Faction war',
    maintenance: 'Maintenance',
    other: 'Other',
  };

  let title = $state('');
  let kind = $state('');
  let banner = $state('');
  let startsLocal = $state('');
  let endsLocal = $state('');
  let mapName = $state('');
  let host = $state('');
  let description = $state('');
  let publish = $state(false);
  let confirmText = $state('');
  let cancelReason = $state('');

  let kindOptions = $derived(
    (Array.isArray(kinds) ? kinds : []).map((k) => ({ value: k, label: KIND_LABELS[k] || k }))
  );
  let bannerOptions = $derived(
    (Array.isArray(banners) ? banners : []).filter((b) => typeof b === 'string' && b)
  );

  function parseUtc(s) {
    if (typeof s !== 'string' || !s.trim()) return null;
    let v = s.trim().replace(' ', 'T');
    if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }

  const pad = (n) => String(n).padStart(2, '0');

  // UTC wire stamp -> the value a datetime-local input wants (viewer local).
  function toLocalInput(utc) {
    const t = parseUtc(utc);
    if (t == null) return '';
    const d = new Date(t);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
           `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // datetime-local (viewer local) -> the UTC wire stamp. Null for an empty box:
  // an unset end time is a field the event does not have, not midnight.
  function toUtcStamp(local) {
    if (!local) return null;
    const t = Date.parse(local);
    if (Number.isNaN(t)) return null;
    const d = new Date(t);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
           `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`;
  }

  function seedFrom(ev) {
    title = String(ev?.title || '').slice(0, TITLE_MAX);
    kind = String(ev?.kind || '') || (Array.isArray(kinds) && kinds[0]) || '';
    banner = String(ev?.banner || '');
    startsLocal = toLocalInput(ev?.starts_utc);
    endsLocal = toLocalInput(ev?.ends_utc);
    mapName = String(ev?.map || '').slice(0, FIELD_MAX);
    host = String(ev?.host || '').slice(0, FIELD_MAX);
    description = String(ev?.description || '').slice(0, DESC_MAX);
    publish = ev?.status === 'published';
    confirmText = '';
    cancelReason = '';
  }

  // Seeded once per open, and re-seeded when the dialog is pointed at a different
  // event. From there the form is the operator's draft and must not be reset
  // under them by a poll landing a fresh copy of the same row.
  let seeded = '';
  $effect(() => {
    if (!open) { seeded = ''; return; }
    const key = `${mode}:${event?.id ?? 'new'}`;
    if (key === seeded) return;
    seeded = key;
    const ev = untrack(() => (mode === 'edit' ? event : null));
    untrack(() => seedFrom(ev));
  });

  let titleOver = $derived(title.length > TITLE_MAX);
  let descOver = $derived(description.length > DESC_MAX);
  let canSubmit = $derived(
    !busy && title.trim().length > 0 && kind !== '' && startsLocal !== '' &&
    !titleOver && !descOver
  );
  let canCancel = $derived(!busy && confirmText === CONFIRM_WORD);

  function submit() {
    if (!canSubmit) return;
    onSubmit?.({
      title: title.trim().slice(0, TITLE_MAX),
      kind,
      banner: banner || null,
      starts_utc: toUtcStamp(startsLocal),
      ends_utc: toUtcStamp(endsLocal),
      map: mapName.trim().slice(0, FIELD_MAX) || null,
      host: host.trim().slice(0, FIELD_MAX) || null,
      description: description.trim().slice(0, DESC_MAX) || null,
      status: publish ? 'published' : 'draft',
    });
  }

  function cancelEvent() {
    if (!canCancel) return;
    onCancelEvent?.(confirmText, cancelReason.trim().slice(0, REASON_MAX));
  }
</script>

{#if open}
  <Modal
    open={true}
    title={mode === 'edit' ? 'Edit event' : 'Call an event'}
    size="lg"
    onClose={() => onClose?.()}
  >
    <label class="fld"><span>Title</span>
      <input type="text" bind:value={title} maxlength={TITLE_MAX} placeholder="Name the gathering" />
      <span class="count mono" class:over={titleOver}>{title.length} of {TITLE_MAX}</span>
    </label>

    {#if kindOptions.length}
      <label class="fld"><span>Kind</span>
        <select bind:value={kind}>
          {#each kindOptions as k (k.value)}<option value={k.value}>{k.label}</option>{/each}
        </select>
      </label>
    {/if}

    <div class="fld"><span>Banner</span>
      <div class="preview">
        <EventBanner {banner} {title} />
      </div>
      <fieldset class="picker">
        <legend class="sr">Banner plate</legend>
        <label class="bopt" class:sel={banner === ''}>
          <input type="radio" name="event-banner" value="" bind:group={banner} />
          <span class="bnone mono">No banner</span>
        </label>
        {#each bannerOptions as b (b)}
          <label class="bopt" class:sel={banner === b}>
            <input type="radio" name="event-banner" value={b} bind:group={banner} />
            <EventBanner banner={b} />
            <span class="bname mono">{b}</span>
          </label>
        {/each}
      </fieldset>
    </div>

    <div class="pair">
      <label class="fld"><span>Starts (your clock)</span>
        <input type="datetime-local" bind:value={startsLocal} />
        <span class="count mono">{toUtcStamp(startsLocal) || 'not set'}</span>
      </label>
      <label class="fld"><span>Ends (optional)</span>
        <input type="datetime-local" bind:value={endsLocal} />
        <span class="count mono">{toUtcStamp(endsLocal) || 'not set'}</span>
      </label>
    </div>

    <div class="pair">
      <label class="fld"><span>Map</span>
        <input type="text" bind:value={mapName} maxlength={FIELD_MAX} placeholder="Hagga Basin, Deep Desert" />
      </label>
      <label class="fld"><span>Host handle</span>
        <input type="text" bind:value={host} maxlength={FIELD_MAX} placeholder="Who is running it" />
      </label>
    </div>

    <label class="fld"><span>Description</span>
      <textarea bind:value={description} maxlength={DESC_MAX} rows="6" placeholder="What is happening, where to gather, what to bring."></textarea>
      <span class="count mono" class:over={descOver}>{description.length} of {DESC_MAX}</span>
    </label>

    <label class="check">
      <input type="checkbox" bind:checked={publish} />
      <span>Publish now (unchecked stays a draft, invisible to players)</span>
    </label>
    <p class="hint mono">Discord cross-post is not configured yet, so publishing posts nothing to Discord.</p>

    <div class="row">
      <button class="btn primary" type="button" onclick={submit} disabled={!canSubmit}>
        {busy ? 'Saving' : mode === 'edit' ? 'Save event' : 'Create event'}
      </button>
      <button class="btn" type="button" onclick={() => onClose?.()} disabled={busy}>Close</button>
    </div>

    {#if error}
      <Notice tone="error" text={error} />
    {/if}

    {#if mode === 'edit' && event}
      <section class="danger">
        <p class="kicker mono">Cancel this event</p>
        <p class="warn">Everyone holding a reminder is notified straight away, and the event stays on the almanac marked cancelled. This cannot be undone.</p>
        <label class="fld"><span>Type {CONFIRM_WORD} to confirm</span>
          <input type="text" bind:value={confirmText} autocomplete="off" spellcheck="false" />
        </label>
        <label class="fld"><span>Reason (shown to players)</span>
          <input type="text" bind:value={cancelReason} maxlength={REASON_MAX} placeholder="Why it is off" />
        </label>
        <button class="btn danger-btn" type="button" onclick={cancelEvent} disabled={!canCancel}>
          Cancel event
        </button>
      </section>
    {/if}
  </Modal>
{/if}

<style>
  .fld {
    display: flex; flex-direction: column; gap: var(--space-1);
    font-size: var(--text-xs); color: var(--text-muted);
    text-transform: uppercase; letter-spacing: .06em;
    border: 0; padding: 0; margin: 0;
  }
  .fld input[type='text'], .fld input[type='datetime-local'], .fld select, .fld textarea {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    text-transform: none; letter-spacing: normal;
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
  }
  .fld textarea { resize: vertical; min-height: 7rem; line-height: 1.5; }
  .fld input:focus-visible, .fld select:focus-visible, .fld textarea:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  .count { align-self: flex-end; font-size: 10px; color: var(--text-muted); letter-spacing: .04em; text-transform: none; }
  .count.over { color: var(--ls-red); }

  .pair { display: grid; gap: var(--space-3); grid-template-columns: 1fr; }
  @media (min-width: 32rem) { .pair { grid-template-columns: 1fr 1fr; } }

  .preview { margin: var(--space-1) 0; }
  .picker {
    display: grid; gap: var(--space-2); border: 0; padding: 0; margin: 0;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 9rem), 1fr));
    max-height: 18rem; overflow-y: auto;
  }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  .bopt {
    display: flex; flex-direction: column; gap: var(--space-1);
    padding: var(--space-1); cursor: pointer;
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0);
  }
  .bopt.sel { border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .bopt input { accent-color: var(--accent); }
  .bname, .bnone {
    font-size: 10px; color: var(--text-muted); letter-spacing: .06em;
    text-transform: none;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  .check {
    display: flex; align-items: flex-start; gap: var(--space-2);
    font-size: var(--text-sm); color: var(--text); line-height: 1.4;
  }
  .check input { accent-color: var(--accent); margin-top: 3px; }
  .hint { margin: 0; font-size: 10px; color: var(--text-muted); letter-spacing: .04em; }

  .row { display: flex; gap: var(--space-2); }
  .btn {
    flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm);
    color: var(--text); background: var(--metal-1);
    border: 1px solid var(--edge); cursor: pointer;
  }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }

  .danger {
    display: flex; flex-direction: column; gap: var(--space-2);
    margin-top: var(--space-3); padding-top: var(--space-3);
    border-top: 1px solid var(--edge);
  }
  .kicker { margin: 0; color: var(--ls-red); text-transform: uppercase; letter-spacing: .2em; font-size: 10px; }
  .warn { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.5; }
  .btn.danger-btn { flex: 0 0 auto; align-self: flex-start; color: var(--ls-red); border-color: color-mix(in srgb, var(--ls-red) 45%, var(--edge)); }
</style>
