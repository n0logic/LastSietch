<script>
  // Phase 1 write: a Leader/Officer edits their guild's description. Parent only
  // mounts this when the backend reports my_role_can_edit:true (role is never
  // computed client-side). Flow is dry-run then apply: Preview mints ONE
  // idempotency_key and Save reuses it, so a retried Save dedupes to a replay
  // rather than a second write. Editing the text after a preview invalidates the
  // key (it is now a different intended change) and forces a fresh preview.
  import { api, uuidv4 } from '$lib/api.js';

  let { guildId, initialDescription = '' } = $props();

  const MAX = 500;
  // Seed the editable buffer once from the prop. The editor mounts only after the
  // guild context has loaded (parent gates on my_role_can_edit), so the prop is
  // stable at mount and a one-time capture is correct.
  // svelte-ignore state_referenced_locally
  let text = $state(initialDescription || '');
  let idemKey = $state('');       // minted at preview, reused for save
  let previewOk = $state(false);  // a dry-run succeeded for the current text
  let busy = $state(false);
  // phase: 'idle' | 'dry-run' | 'queued' | 'applied' | 'replay' | 'failed'
  let phase = $state('idle');
  let message = $state('');

  // Straight quotes and backslashes break Funcom's guild-invite notify payload —
  // it is hand-built JSON with no escaping, so one " in the description silently
  // kills every invite the guild sends (guild 31, 2026-07-23). This is a hint only:
  // the backend check is authoritative and also rejects control characters.
  const BAD_CHARS = /["\\]/;
  let dirty = $derived(text !== (initialDescription || ''));
  let tooLong = $derived(text.length > MAX);
  let hasBadChars = $derived(BAD_CHARS.test(text));
  let blocked = $derived(tooLong || hasBadChars);

  // Any edit after a preview means the key no longer describes what is on screen.
  function onInput() {
    previewOk = false;
    idemKey = '';
    if (phase !== 'idle') { phase = 'idle'; message = ''; }
  }

  function resolve(res, mode) {
    // Semantic outcomes come back as data (no throw). Map the status enum to the
    // on-screen phase + copy; prefer the server message when it sends one.
    const status = res?.status || (res?.success ? 'applied' : 'failed');
    if (mode === 'dry-run') {
      if (res?.success && (status === 'applied' || status === 'deferred' || status === 'replay')) {
        previewOk = true; phase = 'dry-run';
        message = res?.message || 'Dry run clean. Ready to save.';
      } else {
        previewOk = false; phase = 'failed';
        message = res?.message || res?.fail_reason || 'Dry run rejected the change.';
      }
      return;
    }
    // apply
    if (status === 'applied') { phase = 'applied'; message = 'Guild description updated. It is live for the guild now.'; }
    else if (status === 'replay') { phase = 'replay'; message = res?.message || 'Already applied.'; }
    else if (status === 'deferred') { phase = 'queued'; message = res?.message || 'Queued. It will apply shortly.'; }
    else { phase = 'failed'; message = res?.message || res?.fail_reason || 'The change did not apply.'; }
  }

  async function run(mode) {
    if (busy || blocked) return;
    if (!idemKey) idemKey = uuidv4(); // mint once (at preview); save reuses it
    busy = true;
    if (mode === 'dry-run') { phase = 'idle'; message = ''; }
    try {
      const res = await api.guilds.op({
        op: 'edit_description',
        guild_id: guildId,
        detail: { description: text },
        idempotency_key: idemKey,
        mode,
        ts: Date.now(),
      });
      resolve(res, mode);
    } catch (e) {
      previewOk = false; phase = 'failed';
      message = e?.message || 'The registry could not be reached.';
    } finally {
      busy = false;
    }
  }
</script>

<section class="editor">
  <p class="kicker mono">Edit description</p>
  <label class="lbl" for="guild-desc">Guild description</label>
  <textarea
    id="guild-desc"
    bind:value={text}
    oninput={onInput}
    rows="4"
    maxlength={MAX + 40}
    placeholder="What your sietch stands for. Who you want. When you ride."
  ></textarea>
  <div class="meta mono">
    <span class:over={tooLong}>{text.length} / {MAX}</span>
    {#if tooLong}<span class="warn">Too long to save.</span>{/if}
    {#if hasBadChars}<span class="warn">Remove " and \ — they break guild invites. Curly quotes (“ ”) are fine.</span>{/if}
  </div>

  <div class="actions">
    <button class="btn" type="button" onclick={() => run('dry-run')} disabled={busy || blocked || !dirty}>
      {busy && phase !== 'dry-run' ? 'Working' : 'Preview (dry run)'}
    </button>
    <button class="btn primary" type="button" onclick={() => run('apply')} disabled={busy || blocked || !previewOk}>
      Save
    </button>
  </div>

  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</section>

<style>
  .editor { display: flex; flex-direction: column; gap: var(--space-2); }
  .kicker { margin: 0 0 var(--space-1); color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .1em; text-transform: uppercase; }
  textarea {
    width: 100%; resize: vertical; min-height: 5.5rem;
    font-family: var(--font-sans); font-size: var(--text-sm); line-height: 1.5;
    color: var(--text); background: var(--metal-0);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .meta { display: flex; gap: var(--space-3); font-size: var(--text-xs); color: var(--text-muted); }
  .meta .over { color: var(--ls-red); }
  .meta .warn { color: var(--ls-red); }
  .actions { display: flex; gap: var(--space-2); margin-top: var(--space-1); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-2) var(--space-4);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), filter var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn.primary {
    color: var(--bg-deep); background: var(--accent); border-color: var(--accent);
    box-shadow: 0 0 10px var(--accent-glow);
  }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
  .status { font-size: var(--text-sm); margin: var(--space-1) 0 0; color: var(--text-muted); }
  .status[data-phase='applied'], .status[data-phase='replay'] { color: var(--ls-green); }
  .status[data-phase='queued'], .status[data-phase='dry-run'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
