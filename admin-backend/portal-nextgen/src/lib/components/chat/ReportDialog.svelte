<script>
  // Report one message to the moderators. One field, one write, one sentence
  // back.
  //
  // A second report from the same account on the same message is NOT an error:
  // the server answers {ok:true, already:true} and writes nothing, and this says
  // so plainly rather than pretending a fresh report was filed. Reporting twice
  // because you were not sure the first one landed is the normal thing a player
  // does, and it should read as "we already have it", not as a failure.
  //
  // The reason is optional. A report with no words is still a report, and the
  // queue carries the message itself; forcing a sentence out of someone would
  // only produce "bad" a hundred times.
  //
  // The dialog chrome, focus trap, inert background and Escape all come from the
  // shared Modal primitive.
  import { api } from '$lib/api.js';
  import Modal from '$lib/components/Modal.svelte';
  import Notice from '$lib/components/Notice.svelte';

  let {
    channel,
    messageId,
    author = '',
    onDone,
    onClose,
  } = $props();

  // Mirrors the column bound in plan 1d: reason <= 200 chars.
  const REASON_MAX = 200;

  // A refusal is a 200 with {ok:false, error, message} that sendCsrfJSON
  // rethrows with the envelope on err.data. The envelope's `message` is the
  // COMPOSER's copy for every token the composer does not share, so a
  // `forbidden` here would read "That message could not be sent." Ours, by
  // token, instead.
  const REFUSALS = {
    forbidden: 'That message is not one you can report.',
    not_found: 'That message is already gone.',
    bad_request: 'That report could not be filed. Try a shorter reason.',
    chat_disabled: 'Chat is being fitted. Back soon.',
  };

  let reason = $state('');
  let busy = $state(false);
  let sent = $state(false);
  let already = $state(false);
  let error = $state('');

  let left = $derived(REASON_MAX - reason.length);

  // The one exit AFTER a report is filed: tell the parent (which unmounts this
  // and refreshes the row), or just close where no parent is listening.
  function finish() {
    (onDone ?? onClose)?.();
  }

  async function submit(e) {
    e.preventDefault();
    if (busy || sent) return;
    busy = true;
    error = '';
    try {
      const r = await api.chat.report(channel, messageId, reason.trim());
      already = r?.already === true;
      sent = true;
      // NOT onDone here. The parent's onDone unmounts this dialog, so calling it
      // on the write would tear the confirmation down in the same tick it was
      // rendered and the player would never learn whether the report landed, or
      // that they had already filed it. The confirmation's own Close is the exit.
    } catch (err) {
      error = REFUSALS[err?.data?.error]
        || 'That report could not be filed right now. Try again in a moment.';
    } finally {
      busy = false;
    }
  }
</script>

<!-- Once the report is filed, EVERY exit is `finish`: the X and Escape have to
     tell the parent the same thing the Close button does. -->
<Modal title="Report message" size="sm" onClose={sent ? finish : onClose}>
  {#if sent}
    {#if already}
      <p class="line">You had already reported this message. The moderators have it once, which is all they need.</p>
    {:else}
      <p class="line">Reported. A moderator will look at it.</p>
    {/if}
    <div class="actions">
      <button class="btn primary" type="button" onclick={finish}>Close</button>
    </div>
  {:else}
    <p class="line">
      {#if author}This sends {author}'s message to the moderators.{:else}This sends the message to the moderators.{/if}
      Say what is wrong with it if you can; the moderators see the message either way.
    </p>
    <form onsubmit={submit}>
      <label class="fld">
        <span class="lbl mono">Reason (optional)</span>
        <textarea
          rows="3"
          maxlength={REASON_MAX}
          bind:value={reason}
          placeholder="What is wrong with this message"
        ></textarea>
      </label>
      <p class="count mono" aria-live="polite">{left} characters left</p>
      {#if error}<Notice tone="error" text={error} />{/if}
      <div class="actions">
        <button class="btn" type="button" onclick={onClose}>Cancel</button>
        <button class="btn primary" type="submit" disabled={busy}>{busy ? 'Sending' : 'Send report'}</button>
      </div>
    </form>
  {/if}
</Modal>

<style>
  .line { margin: 0 0 var(--space-3); font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; }
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
