<script>
  // War-Table light: Auto (the viewer's clock) | Dawn | Day | Dusk | Night. The choice
  // is stored under the Sand Table's own key, so both views follow the same setting.
  import { MODES } from '$lib/map/holo/light.js';
  let { mode = 'auto', onchange = null } = $props();
</script>

<div class="tod" role="group" aria-label="Light">
  {#each MODES as m}
    <button class="tod-btn mono" class:on={mode === m} aria-pressed={mode === m}
      onclick={() => onchange && onchange(m)}>{m === 'auto' ? 'Auto light' : m}</button>
  {/each}
</div>

<style>
  .tod {
    flex: none; display: inline-flex; border: 1px solid var(--edge);
    border-radius: var(--radius-sm); overflow: hidden; background: var(--metal-0);
  }
  .tod-btn {
    background: transparent; border: 0; cursor: pointer;
    color: var(--text-muted); padding: var(--space-2) var(--space-3);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .tod-btn + .tod-btn { border-left: 1px solid var(--edge); }
  .tod-btn:hover { color: var(--text); }
  .tod-btn.on { color: var(--accent-bright); background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .tod-btn:focus-visible { outline: 2px solid var(--accent-bright); outline-offset: -2px; }
  @media (max-width: 640px) { .tod-btn { padding: var(--space-2); letter-spacing: .06em; } }
</style>
