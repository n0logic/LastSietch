<script>
  // Two-state map viewer switch: Carved board (flat 2D) | Holo table (3D). Was
  // embedded in SpiceHudConsole (spice maps only); lifted out so every board
  // (incl. Hagga, which has no spice console) can switch views. viewerKind =
  // 'holo' | 'carved' (active viewer); holoAvailable gates the holo option
  // (false = WebGL2 unavailable, shown disabled); onchange(pref) persists the
  // device-local choice upstream.
  let { viewerKind = 'carved', holoAvailable = false, onchange = null } = $props();
</script>

{#if onchange}
  <div class="viewer-toggle" role="group" aria-label="Map viewer">
    <button
      class="viewer-btn mono"
      class:on={viewerKind === 'carved'}
      aria-pressed={viewerKind === 'carved'}
      onclick={() => onchange('carved')}
    >Board</button>
    <button
      class="viewer-btn mono"
      class:on={viewerKind === 'holo'}
      aria-pressed={viewerKind === 'holo'}
      disabled={!holoAvailable}
      title={holoAvailable ? 'Amber holo table' : 'WebGL2 unavailable'}
      onclick={() => onchange('holo')}
    >Holo table</button>
  </div>
{/if}

<style>
  .viewer-toggle {
    flex: none; display: inline-flex; border: 1px solid var(--edge);
    border-radius: var(--radius-sm); overflow: hidden; background: var(--metal-0);
  }
  .viewer-btn {
    background: transparent; border: 0; cursor: pointer;
    color: var(--text-muted); padding: var(--space-2) var(--space-3);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .viewer-btn + .viewer-btn { border-left: 1px solid var(--edge); }
  .viewer-btn:hover:not(:disabled) { color: var(--text); }
  .viewer-btn.on { color: var(--accent-bright); background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .viewer-btn:disabled { opacity: .4; cursor: not-allowed; }
</style>
