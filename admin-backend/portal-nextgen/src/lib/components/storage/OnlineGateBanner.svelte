<script>
  // Write-gate banner. If the player is online OR their status is undetermined, the
  // whole write surface is read-only (fail-closed) and this explains why. Browsing
  // is always available. The server hard-gates regardless; this is honest UI, not
  // the security boundary. Offline + confirmed -> a quiet "ready" strip.
  import { storage } from '$lib/storage.svelte.js';

  let offlineLocked = $derived(!storage.offlineOk);
  let moveLocked = $derived(offlineLocked || !storage.pawnMoveEnabled);
  let undetermined = $derived(storage.online == null);
</script>

{#if storage.status === 'ready'}
  <div class="gate" class:locked={moveLocked} data-state={moveLocked ? 'locked' : 'open'} role="status" aria-live="polite">
    {#if offlineLocked}
      <span class="ico" aria-hidden="true">&#9632;</span>
      <span class="msg">
        {#if undetermined}
          Cannot confirm you are logged out, so storage moves are locked. Browsing is available now.
        {:else}
          Log out of the game to reorganise storage. Browsing is available now.
        {/if}
      </span>
    {:else if !storage.pawnMoveEnabled}
      <span class="ico" aria-hidden="true">&#9632;</span>
      <span class="msg">
        You are logged out. Bank and Backpack transfers are not open yet. Browsing is available now.
      </span>
    {:else}
      <span class="ico open" aria-hidden="true">&#9679;</span>
      <span class="msg">You are logged out. Bank and Backpack transfers are unlocked.</span>
    {/if}
  </div>
{/if}

<style>
  .gate {
    display: flex; align-items: center; gap: var(--space-3);
    padding: var(--space-2) var(--space-4); border-radius: var(--radius-sm);
    border: 1px solid var(--edge); background: var(--metal-0); font-size: var(--text-sm);
  }
  .gate.locked { border-color: color-mix(in srgb, var(--ls-yellow) 45%, var(--edge)); background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0)); }
  .ico { color: var(--ls-yellow); }
  .ico.open { color: var(--ls-green); }
  .msg { color: var(--text-muted); }
</style>
