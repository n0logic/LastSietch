<script>
  // "Remind me" for one event. Three states, and the third is the one that
  // matters: `reminded === null` means WE DO NOT KNOW (anonymous, or a `mine`
  // read that failed), and an unknown renders the toggle ABSENT. Drawn in the
  // off position it would be a statement that the player has no reminder set,
  // and it would be one click away from a write they cannot make.
  //
  // `disabled` is the caller's: an event that has started or been cancelled has
  // nothing left to remind anyone about, and the backend refuses those anyway.
  let { reminded = null, busy = false, disabled = false, onToggle } = $props();

  let known = $derived(reminded === true || reminded === false);
  let on = $derived(reminded === true);
</script>

{#if known}
  <button
    class="remind mono" class:on
    type="button"
    aria-pressed={on}
    disabled={busy || disabled}
    onclick={() => onToggle?.(!on)}
  >{busy ? 'Working' : on ? 'Reminder set' : 'Remind me'}</button>
{/if}

<style>
  .remind {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-4);
    color: var(--text-muted); background: var(--metal-1);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out),
                color var(--motion-fast) var(--ease-out);
  }
  .remind:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
  .remind.on {
    color: var(--bg-deep); background: var(--accent); border-color: var(--accent);
    box-shadow: 0 0 10px var(--accent-glow);
  }
  .remind.on:hover:not(:disabled) { filter: brightness(1.1); }
  .remind:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .remind:disabled { opacity: .45; cursor: not-allowed; }
</style>
