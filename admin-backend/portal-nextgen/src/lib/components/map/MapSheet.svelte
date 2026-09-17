<script>
  // Mobile bottom sheet with a drag handle and three detents (peek/half/full).
  // Detents are fractions of the sheet's height kept ON screen. Dragging below
  // peek closes. Reduced motion snaps without transition (app.css global kill).
  let { open = false, label = 'Sheet', onclose, children } = $props();

  const DETENTS = { peek: 0.18, half: 0.55, full: 0.94 };
  let detent = $state('half');
  let sheetEl = $state(null);

  let dragging = $state(false);
  let dragStartY = 0;
  let dragStartShown = 0;
  let dragShown = $state(0); // fraction shown while dragging

  $effect(() => { if (open) detent = 'half'; });

  let shownFrac = $derived(dragging ? dragShown : DETENTS[detent]);

  function onPointerDown(e) {
    if (!sheetEl) return;
    dragging = true;
    dragStartY = e.clientY;
    dragStartShown = DETENTS[detent];
    dragShown = dragStartShown;
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function onPointerMove(e) {
    if (!dragging || !sheetEl) return;
    const h = sheetEl.offsetHeight || 1;
    const delta = (dragStartY - e.clientY) / h; // up = more shown
    dragShown = Math.min(DETENTS.full, Math.max(0.02, dragStartShown + delta));
  }
  function onPointerUp() {
    if (!dragging) return;
    dragging = false;
    // Snap to the nearest detent; below peek closes the sheet.
    if (dragShown < DETENTS.peek * 0.6) { onclose?.(); return; }
    let best = 'peek', bestDist = Infinity;
    for (const [name, frac] of Object.entries(DETENTS)) {
      const d = Math.abs(frac - dragShown);
      if (d < bestDist) { best = name; bestDist = d; }
    }
    detent = best;
  }
  function cycleDetent() {
    detent = detent === 'peek' ? 'half' : detent === 'half' ? 'full' : 'peek';
  }
  function onKeydown(e) {
    if (e.key === 'Escape') onclose?.();
    if (e.key === 'ArrowUp' && detent !== 'full') { detent = detent === 'peek' ? 'half' : 'full'; e.preventDefault(); }
    if (e.key === 'ArrowDown') {
      if (detent === 'full') detent = 'half';
      else if (detent === 'half') detent = 'peek';
      else onclose?.();
      e.preventDefault();
    }
  }
</script>

{#if open}
  <div class="scrim" aria-hidden="true" onclick={() => onclose?.()}></div>
  <div
    class="sheet"
    class:dragging
    bind:this={sheetEl}
    role="dialog"
    aria-label={label}
    style:transform={`translateY(${(1 - shownFrac) * 100}%)`}
  >
    <button
      class="handle"
      aria-label="{label}: drag or press arrow keys to resize, Escape to close"
      onpointerdown={onPointerDown}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={onPointerUp}
      onclick={cycleDetent}
      onkeydown={onKeydown}
    >
      <span class="grip" aria-hidden="true"></span>
    </button>
    <div class="body">
      {@render children?.()}
    </div>
  </div>
{/if}

<style>
  .scrim {
    position: fixed; inset: 0; z-index: 30;
    background: rgba(0, 0, 0, .35);
  }
  .sheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 31;
    height: min(86dvh, 640px);
    display: flex; flex-direction: column;
    background: var(--panel); border: 1px solid var(--edge-hi); border-bottom: 0;
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    box-shadow: inset 0 1px 0 var(--metal-hi), var(--shadow-overlay);
    transition: transform var(--motion-mid) var(--ease-out);
    touch-action: none;
    padding-bottom: env(safe-area-inset-bottom, 0);
  }
  .sheet.dragging { transition: none; }

  .handle {
    flex: none; width: 100%; padding: var(--space-3) 0 var(--space-2);
    background: transparent; border: 0; cursor: grab;
    display: grid; place-items: center;
  }
  .handle:active { cursor: grabbing; }
  .grip {
    width: 44px; height: 4px; border-radius: 2px;
    background: var(--edge-hi);
  }

  .body {
    flex: 1; min-height: 0; overflow-y: auto;
    padding: 0 var(--space-4) var(--space-5);
    overscroll-behavior: contain;
  }
</style>
