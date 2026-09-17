<script>
  // PvE/PvP instance switch: the dashboard seg control generalized. Purely
  // presentational; the owner persists the choice (ls-dim) and drives the engine.
  let { instances = [], dim = 0, onchange } = $props();
</script>

{#if instances.length > 1}
  <div class="tabs" role="tablist" aria-label="Instance">
    {#each instances as inst (inst.key)}
      <button
        role="tab"
        aria-selected={inst.dim === dim}
        class:on={inst.dim === dim}
        onclick={() => onchange?.(inst.dim)}
      >{inst.label}</button>
    {/each}
  </div>
{/if}

<style>
  .tabs {
    display: inline-flex; border: 1px solid var(--edge);
    border-radius: var(--radius-sm); overflow: hidden; background: var(--metal-0);
  }
  .tabs button {
    background: transparent; color: var(--text-muted); border: 0;
    padding: var(--space-2) var(--space-4); font-size: var(--text-xs); cursor: pointer;
    font-family: var(--font-mono); letter-spacing: .16em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .tabs button + button { border-left: 1px solid var(--edge); }
  .tabs button:hover { color: var(--text); }
  .tabs button.on {
    color: var(--accent-bright);
    background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 18%, transparent), transparent);
    box-shadow: inset 0 -2px 0 var(--accent); font-weight: 700;
  }
</style>
