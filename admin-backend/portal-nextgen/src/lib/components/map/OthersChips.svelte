<script>
  // Live other-player / world-vehicle layer toggles (Hagga only). Owner ruling
  // 2026-09-30: live positions and vehicles are for signed-in players only, so
  // a signed-out visitor sees a sign-in prompt instead of an empty layer. The
  // route owns persistence + engine wiring; this is presentational.
  import { LOGIN_URL } from '$lib/auth.svelte.js';
  let {
    count = null,
    show = { players: true, vehicles: true },
    signedIn = true,
    onchange,
  } = $props();

  function toggle(key) {
    onchange?.({ ...show, [key]: !show[key] });
  }
</script>

<div class="others" role="group" aria-label="Live players layer">
  <span class="label mono">Live players</span>
  {#if !signedIn}
    <a class="signin mono" href={LOGIN_URL} data-sveltekit-reload>Sign in to see other players</a>
  {:else}
  <button
    class="chip mono"
    class:off={!show.players}
    aria-pressed={show.players}
    onclick={() => toggle('players')}
  >
    <span class="swatch swatch-player" aria-hidden="true"></span>
    Players{#if count != null}<span class="n">{count}</span>{/if}
  </button>
  <button
    class="chip mono"
    class:off={!show.vehicles}
    aria-pressed={show.vehicles}
    onclick={() => toggle('vehicles')}
  >
    <span class="swatch swatch-vehicle" aria-hidden="true"></span>
    Vehicles
  </button>
  {/if}
</div>

<style>
  .signin { font-size: var(--text-xs); letter-spacing: .06em; color: var(--accent-text); text-decoration: underline; text-underline-offset: 3px; }
  .others {
    display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2);
  }
  .label {
    color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .18em; text-transform: uppercase;
  }
  .chip {
    display: inline-flex; align-items: center; gap: var(--space-2);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--edge); border-radius: var(--radius-xl);
    padding: var(--space-1) var(--space-3); cursor: pointer;
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out), opacity var(--motion-fast) var(--ease-out);
  }
  .chip:hover { border-color: var(--accent); }
  .chip.off { color: var(--text-muted); opacity: .55; }
  .chip.off .swatch { opacity: .35; }

  .swatch {
    flex: none; width: 9px; height: 9px; border-radius: 50%;
    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .4);
  }
  /* Ibad = live crowd (matches the board dots); vehicles are dim amber. */
  .swatch-player { background: var(--ls-ibad); box-shadow: 0 0 5px var(--ls-ibad-glow); }
  .swatch-vehicle { background: var(--accent-soft); }

  .n { color: var(--text-muted); }
  .chip:not(.off) .n { color: var(--text); }
</style>
