<script>
  // Mute one author, for a while or until someone lifts it.
  //
  // The four presets ARE the choice. A free minutes box would invite 43199 and
  // then a support message asking why it did not take; the server's 1..43200
  // bound is the same 30 days the longest preset already reaches, and "until
  // lifted" is the only unbounded option a moderator ever actually wants.
  //
  // The channel is SHOWN, never chosen here. A guild leader may only mute inside
  // their own guild channel, and printing which room this lands in is what stops
  // a leader from thinking they have silenced someone across the whole sietch. A
  // caller that means "everywhere" passes channel="" and gets that sentence.
  //
  // Two ways out, ONE payload. With `onSubmit` the dialog hands
  // {char_name, channel, minutes, reason} back and writes nothing, which is how
  // the moderation queue folds a mute into its delete_and_mute resolve; without
  // it the dialog posts the same object to api.chat.mute itself. The shape does
  // not fork, so neither does the server's reading of it.
  import { api } from '$lib/api.js';
  import Modal from '$lib/components/Modal.svelte';
  import Notice from '$lib/components/Notice.svelte';

  let {
    charName = '',
    channel = '',          // '' = everywhere (admins only, server-enforced)
    channelLabel = '',
    busy: busyProp = false, // a parent that owns the write owns the spinner too
    error: errorProp = '',
    onSubmit = null,       // (payload) => void; when absent this dialog writes
    onDone,
    onClose,
  } = $props();

  const REASON_MAX = 200;

  // minutes: null = until lifted. 43200 is the server's ceiling (30 days).
  const PRESETS = [
    { key: '1h', label: '1 hour', minutes: 60 },
    { key: '24h', label: '24 hours', minutes: 1440 },
    { key: '7d', label: '7 days', minutes: 10080 },
    { key: 'lifted', label: 'Until lifted', minutes: null },
  ];

  const REFUSALS = {
    forbidden: 'You cannot mute that player in this channel.',
    unknown_player: 'No player answers to that name any more.',
    bad_request: 'That mute could not be applied. Check the length and the reason.',
    chat_disabled: 'Chat is being fitted. Back soon.',
  };

  let preset = $state('24h');
  let reason = $state('');
  let busy = $state(false);
  let error = $state('');

  let chosen = $derived(PRESETS.find((p) => p.key === preset) || PRESETS[1]);
  let where = $derived(channel ? (channelLabel || channel) : 'every channel');
  let working = $derived(busy || busyProp);
  let shown = $derived(error || errorProp);
  let left = $derived(REASON_MAX - reason.length);

  function payload() {
    return {
      char_name: charName,
      channel,
      minutes: chosen.minutes,
      reason: reason.trim(),
    };
  }

  async function submit(e) {
    e.preventDefault();
    if (working) return;
    const body = payload();
    if (onSubmit) {
      onSubmit(body);
      return;
    }
    busy = true;
    error = '';
    try {
      await api.chat.mute(body);
      onDone?.();
    } catch (err) {
      // A refusal is a 200 with {ok:false, error, message} that sendCsrfJSON
      // rethrows with the envelope on err.data. The envelope's `message` is the
      // COMPOSER's copy ("That message could not be sent") for every token the
      // composer does not share, so the sentences are ours, by token.
      error = REFUSALS[err?.data?.error] || 'That mute could not be applied right now.';
    } finally {
      busy = false;
    }
  }
</script>

<Modal title="Mute {charName}" size="sm" {onClose}>
  <p class="line">
    {charName} will not be able to post in <strong>{where}</strong> while the mute stands.
    Reading is unaffected.
  </p>

  <form onsubmit={submit}>
    <fieldset class="presets">
      <legend class="lbl mono">How long</legend>
      {#each PRESETS as p (p.key)}
        <label class="preset" class:on={preset === p.key}>
          <input type="radio" name="mute-length" value={p.key} bind:group={preset} />
          <span>{p.label}</span>
        </label>
      {/each}
    </fieldset>

    <label class="fld">
      <span class="lbl mono">Reason (optional)</span>
      <textarea
        rows="3"
        maxlength={REASON_MAX}
        bind:value={reason}
        placeholder="What this mute is for"
      ></textarea>
    </label>
    <p class="count mono" aria-live="polite">{left} characters left</p>

    {#if shown}<Notice tone="error" text={shown} />{/if}

    <div class="actions">
      <button class="btn" type="button" onclick={onClose}>Cancel</button>
      <button class="btn primary" type="submit" disabled={working || !charName}>
        {working ? 'Applying' : 'Mute'}
      </button>
    </div>
  </form>
</Modal>

<style>
  .line { margin: 0 0 var(--space-3); font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; }
  .line strong { color: var(--text); font-weight: 600; }
  .presets { border: 0; margin: 0 0 var(--space-3); padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .presets legend { padding: 0; margin-bottom: var(--space-2); }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; }
  .preset {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-sm); color: var(--text-muted); cursor: pointer;
    padding: var(--space-1) var(--space-3);
    background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .preset:hover { border-color: var(--accent); }
  .preset.on { color: var(--accent-text); border-color: var(--accent); }
  .preset input { accent-color: var(--accent); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); }
  textarea {
    font: inherit; font-size: var(--text-sm); color: var(--text); resize: vertical;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .count { margin: var(--space-1) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
  .actions { display: flex; justify-content: flex-end; gap: var(--space-2); margin-top: var(--space-4); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn.primary { color: var(--accent-text); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
</style>
