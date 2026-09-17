<script>
  // Ask to join a guild. LIVE (admin.db) for any linked player not already in a
  // guild. Expands to an optional note, posts, and reports the outcome honestly;
  // the server treats a duplicate as an idempotent "already requested".
  import { api } from '$lib/api.js';

  let { guildId, guildName = '' } = $props();

  const MAX = 240;
  let open = $state(false);
  let note = $state('');
  let busy = $state(false);
  // phase: 'idle' | 'sent' | 'already' | 'failed'
  let phase = $state('idle');
  let message = $state('');

  let tooLong = $derived(note.length > MAX);

  async function send() {
    if (busy || tooLong) return;
    busy = true;
    try {
      const body = note.trim() ? { note: note.trim() } : {};
      const r = await api.guilds.joinRequestCreate(guildId, body);
      const status = r?.status || 'pending';
      if (status === 'pending') { phase = 'sent'; message = `Request raised to ${guildName || 'the guild'}. An officer will answer.`; }
      else { phase = 'already'; message = 'You have already asked to join this guild.'; }
      open = false;
    } catch (e) {
      phase = 'failed';
      message = e?.message || 'The registry could not take your request. Try again shortly.';
    } finally {
      busy = false;
    }
  }
</script>

<div class="rtj">
  {#if phase === 'sent' || phase === 'already'}
    <p class="done mono" data-phase={phase} role="status">{message}</p>
  {:else}
    {#if !open}
      <button class="btn" type="button" onclick={() => { open = true; phase = 'idle'; message = ''; }}>
        Request to join
      </button>
    {:else}
      <div class="form">
        <textarea
          bind:value={note}
          rows="2"
          maxlength={MAX + 20}
          placeholder="Optional: why you want to ride with them."
        ></textarea>
        <div class="row">
          <span class="count mono" class:over={tooLong}>{note.length} / {MAX}</span>
          <div class="acts">
            <button class="btn ghost" type="button" onclick={() => { open = false; }} disabled={busy}>Cancel</button>
            <button class="btn primary" type="button" onclick={send} disabled={busy || tooLong}>
              {busy ? 'Sending' : 'Send request'}
            </button>
          </div>
        </div>
      </div>
    {/if}
    {#if phase === 'failed' && message}
      <p class="done mono" data-phase="failed" role="status">{message}</p>
    {/if}
  {/if}
</div>

<style>
  .rtj { display: flex; flex-direction: column; gap: var(--space-2); }
  .form { display: flex; flex-direction: column; gap: var(--space-2); }
  textarea {
    width: 100%; resize: vertical; min-height: 3rem;
    font-family: var(--font-sans); font-size: var(--text-sm); line-height: 1.45;
    color: var(--text); background: var(--metal-0);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  .count { font-size: var(--text-xs); color: var(--text-muted); }
  .count.over { color: var(--ls-red); }
  .acts { display: flex; gap: var(--space-2); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), filter var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn.ghost { color: var(--text-muted); }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
  .done { font-size: var(--text-xs); margin: 0; color: var(--text-muted); line-height: 1.4; }
  .done[data-phase='sent'], .done[data-phase='already'] { color: var(--accent-text); }
  .done[data-phase='failed'] { color: var(--ls-red); }
</style>
