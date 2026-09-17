<script>
  // The composer. Enter sends, Shift+Enter is a newline, and the counter runs to
  // 500 because that is where the server cleans and refuses.
  //
  // The box DISABLES with the reason on it rather than accepting a message it
  // knows will bounce: a mute, a rate limit (with the countdown the server sent
  // in retry_after), or a channel this account may read but not post in. The
  // reason is the caller's sentence, because only the store knows which refusal
  // token arrived.
  //
  // A line that opens with '/' is a command. The box does not run it (the store
  // does, and three of the five never leave this tab), but it does say what the
  // one being typed expects, because a command whose shape you only learn by
  // getting it wrong is a command nobody uses twice.
  import { BODY_MAX, COMMANDS } from '$lib/chat.svelte.js';

  let {
    disabled = false,
    reason = '',
    // A refusal that does NOT stop the next attempt (a duplicate, a malformed
    // body). It gets a sentence but not a locked box: the player can fix what
    // they said and send it again, and a silent refusal reads as a lost message.
    notice = '',
    retryAfter = 0,
    busy = false,
    placeholder = 'Say something',
    onsend,
    ondismiss,
  } = $props();

  let text = $state('');
  let box = $state(null);

  let command = $derived.by(() => {
    const t = text.trimStart();
    if (!t.startsWith('/')) return null;
    const word = t.split(/\s/)[0].toLowerCase();
    return COMMANDS.find((c) => c.name === word) || null;
  });
  let commandHint = $derived(command ? command.usage + '  ' + command.help : '');

  let length = $derived(text.length);
  let over = $derived(length > BODY_MAX);
  let canSend = $derived(!disabled && !busy && !over && text.trim().length > 0);

  async function submit() {
    if (!canSend) return;
    const body = text;
    // Clear first: the send is optimistic, so the row is already on screen and a
    // box that stays full reads as "it did not go".
    text = '';
    const ok = await onsend?.(body);
    if (ok === false) text = body;
    box?.focus();
  }

  function onInput() {
    // The notice describes the LAST send. The moment the player edits, it is
    // about a message that no longer exists.
    if (notice) ondismiss?.();
  }

  function onKeydown(e) {
    // Shift+Enter is a newline (the default), and so is Enter while an IME is
    // composing a candidate: swallowing that would eat the word being typed.
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    submit();
  }
</script>

<div class="composer">
  <textarea
    bind:this={box}
    bind:value={text}
    rows="2"
    {disabled}
    placeholder={disabled ? '' : placeholder}
    aria-label="Message"
    oninput={onInput}
    onkeydown={onKeydown}
  ></textarea>

  <div class="foot">
    {#if disabled && reason}
      <p class="reason" role="status" aria-live="polite">
        {reason}{#if retryAfter > 0}&nbsp;{retryAfter}s{/if}
      </p>
    {:else if notice}
      <p class="notice" role="status" aria-live="polite">{notice}</p>
    {:else if commandHint}
      <p class="hint mono">{commandHint}</p>
    {:else}
      <p class="hint mono">Enter sends, Shift+Enter for a new line</p>
    {/if}
    <span class="count mono" class:over aria-live="off">{length} / {BODY_MAX}</span>
    <button class="btn" type="button" onclick={submit} disabled={!canSend}>
      {busy ? 'Sending' : 'Send'}
    </button>
  </div>
</div>

<style>
  .composer { display: flex; flex-direction: column; gap: var(--space-2); }
  textarea {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
    resize: vertical; min-height: 3.2rem; line-height: 1.45;
  }
  textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  textarea:disabled { opacity: .5; cursor: not-allowed; }

  .foot { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
  .hint, .reason, .notice { margin: 0; flex: 1; font-size: var(--text-xs); color: var(--text-muted); }
  .hint { letter-spacing: .06em; }
  .reason { color: var(--accent-text); }
  .notice { color: var(--accent-text); }
  .count { font-size: var(--text-xs); color: var(--text-muted); }
  .count.over { color: var(--ls-red); }

  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-4); border-radius: var(--radius-sm); cursor: pointer;
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
    box-shadow: 0 0 10px var(--accent-glow);
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { filter: brightness(1.12); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn:focus-visible { outline: 2px solid var(--accent-bright); outline-offset: 2px; }

  @media (prefers-reduced-motion: reduce) {
    .btn { transition: none; }
  }
</style>
