<script>
  // Freeform player-to-player DM composer. The SENDER is always derived from the
  // session server-side (never sent). The recipient is addressed by character name
  // (the server resolves the account); the client never handles account ids. When
  // `recipientCharName` is supplied from context (a roster row, a message reply)
  // the recipient field is locked to that name.
  import { api } from '$lib/api.js';

  let { recipientCharName = '', locked = false, onSent } = $props();

  const SUBJ_MAX = 120;
  const BODY_MAX = 1000;
  // svelte-ignore state_referenced_locally
  let to = $state(recipientCharName || '');
  let subject = $state('');
  let body = $state('');
  let busy = $state(false);
  // phase: 'idle' | 'sent' | 'blocked' | 'failed'
  // 'blocked' (amber) covers the recipient refusing new messages (403) or the
  // sender hitting a rate/daily cap (429): expected outcomes, not errors.
  let phase = $state('idle');
  let message = $state('');

  let bodyTooLong = $derived(body.length > BODY_MAX);
  let canSend = $derived(!busy && to.trim().length > 0 && body.trim().length > 0 && !bodyTooLong);

  async function send() {
    if (!canSend) return;
    busy = true; phase = 'idle'; message = '';
    try {
      const r = await api.messages.send({
        recipient_char_name: to.trim(),
        subject: subject.trim().slice(0, SUBJ_MAX) || undefined,
        body: body.trim().slice(0, BODY_MAX),
      });
      if (r && r.ok !== false) {
        phase = 'sent'; message = `Sent to ${to.trim()}.`;
        subject = ''; body = '';
        if (!locked) to = '';
        onSent?.(r);
      } else {
        phase = 'failed'; message = (r && r.error) || 'The message did not send.';
      }
    } catch (e) {
      if (e?.status === 403 || e?.status === 429) {
        phase = 'blocked';
        message = e?.message || 'This player is not accepting new messages.';
      } else {
        phase = 'failed';
        message = e?.message || 'The courier could not be reached.';
      }
    } finally {
      busy = false;
    }
  }
</script>

<div class="composer">
  <label class="fld"><span>To</span>
    <input type="text" bind:value={to} readonly={locked} placeholder="Character name" aria-label="Recipient character name" />
  </label>
  <label class="fld"><span>Subject</span>
    <input type="text" bind:value={subject} maxlength={SUBJ_MAX + 10} placeholder="Optional" />
  </label>
  <label class="fld"><span>Message</span>
    <textarea bind:value={body} rows="3" maxlength={BODY_MAX + 40} placeholder="Speak plainly. The desert is long."></textarea>
  </label>
  <div class="foot mono">
    <span class:over={bodyTooLong}>{body.length} / {BODY_MAX}</span>
    <button class="btn primary" type="button" onclick={send} disabled={!canSend}>{busy ? 'Sending' : 'Send'}</button>
  </div>
  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</div>

<style>
  .composer { display: flex; flex-direction: column; gap: var(--space-2); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; text-transform: uppercase; }
  .fld input, textarea {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
    text-transform: none; letter-spacing: normal;
  }
  textarea { resize: vertical; min-height: 4rem; line-height: 1.45; }
  .fld input[readonly] { color: var(--text-muted); background: var(--bg-elevated); }
  .fld input:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .foot { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); font-size: var(--text-xs); color: var(--text-muted); }
  .foot .over { color: var(--ls-red); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-4);
    border-radius: var(--radius-sm); cursor: pointer;
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { font-size: var(--text-sm); margin: 0; }
  .status[data-phase='sent'] { color: var(--ls-green); }
  .status[data-phase='blocked'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
