<script>
  // Rare or destructive controls, folded away but never removed. A native
  // details/summary so the toggle is a real keyboard control and screen
  // readers announce it as expanded or collapsed without extra wiring.
  let { label = 'Advanced', children } = $props();
</script>

<details class="advanced">
  <summary class="mono">{label}</summary>
  <div class="advanced-body">
    {@render children()}
  </div>
</details>

<style>
  .advanced { border: 1px dashed var(--edge); border-radius: var(--radius-sm); }
  .advanced[open] { border-style: solid; }
  summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: var(--space-2);
    min-height: 44px; padding: 0 var(--space-3);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; color: var(--text-muted);
  }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: '›'; font-size: var(--text-base); transition: transform var(--motion-fast) var(--ease-out); }
  .advanced[open] summary::before { transform: rotate(90deg); }
  summary:hover { color: var(--text); }
  .advanced-body { display: grid; gap: var(--space-3); padding: 0 var(--space-3) var(--space-3); }
</style>
