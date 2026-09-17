<script>
  // A 5-wide item grid, scrollable to max_item_count rows (NOT a fixed 5x5). Slots
  // are keyed by SPARSE position_index; empty slots render as valid drop targets.
  // mic == -1 grows as needed; mic == 0 is volume-only (no stack slots, so no empty
  // droppables, just the items it already holds). The whole grid is one MOVE drop
  // zone: a dropped item relocates to THIS inventory (server picks first-empty).
  //
  // view='list' (wave 10b) swaps the tiles for one row per item (ItemCell
  // layout='list'). A list renders NO empty slots - the free count is a footer
  // line instead - but the wrap is still the same drop zone and the rows are
  // still the same drag sources.
  import ItemCell from './ItemCell.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import { storage, moveItem } from '$lib/storage.svelte.js';

  // panel: 'bank' | 'container'. containerId = this inventory's id (the MOVE dst).
  let {
    items = [], mic = 0, miv = 0, status = 'ready',
    panel = 'container', containerId, draggableItems = false,
    storageKind = '', writable = false, allowMarketSell = true, view = 'grid',
  } = $props();

  let dragOver = $state(false);

  // position_index -> item, so empty slots are the absent indices.
  let byIndex = $derived.by(() => {
    const m = new Map();
    for (const it of items) m.set(Number(it.position_index), it);
    return m;
  });
  let maxOccupied = $derived(items.reduce((mx, it) => Math.max(mx, Number(it.position_index) || 0), -1));

  // How many indexed slots to render. Cap huge containers so the DOM stays sane;
  // occupied slots always render even beyond the cap.
  const SLOT_CAP = 600;
  let slotCount = $derived.by(() => {
    if (mic === 0) return 0;                          // volume-only: items only
    if (mic === -1) return maxOccupied + 6;           // unlimited: a few trailing
    return Math.min(mic, SLOT_CAP);
  });
  let indices = $derived.by(() => {
    const n = Math.max(slotCount, maxOccupied + 1);
    return Array.from({ length: Math.max(0, n) }, (_, i) => i);
  });

  function canAccept(e) {
    return writable && storage.pawnMoveEnabled && storage.offlineOk &&
      e.dataTransfer && e.dataTransfer.types &&
      Array.from(e.dataTransfer.types).includes('application/x-ls-item');
  }
  function onDragOver(e) {
    if (!canAccept(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    dragOver = true;
  }
  function onDragLeave() { dragOver = false; }
  async function onDrop(e) {
    dragOver = false;
    const raw = e.dataTransfer.getData('application/x-ls-item') || e.dataTransfer.getData('text/plain');
    if (!raw) return;
    e.preventDefault();
    let p; try { p = JSON.parse(raw); } catch { return; }
    if (!p || p.src_container_id === containerId || p.src_storage === storageKind) return;
    await moveItem(p.item_id, containerId, p.src_container_id, p.template);
  }

  let dropDisabled = $derived(mic === 0 || !writable);

  // List view: the sparse position_index still decides the order, and the slot
  // count only survives as the footer line.
  let rows = $derived(
    [...items].sort((a, b) => (Number(a.position_index) || 0) - (Number(b.position_index) || 0))
  );
  let freeSlots = $derived(Math.max(0, mic - items.length));
</script>

<div
  class="grid-wrap"
  class:list={view === 'list'}
  class:drag-over={dragOver && !dropDisabled}
  role="list"
  ondragover={onDragOver}
  ondragleave={onDragLeave}
  ondrop={onDrop}
>
  {#if status === 'loading'}
    <div class="grid">
      {#each Array(10) as _, i (i)}<div class="slot skeleton-slot"></div>{/each}
    </div>
  {:else if status === 'error'}
    <SealedPanel status="error" errorText="Could not read this container." />
  {:else if items.length === 0 && (mic === 0 || view === 'list')}
    <SealedPanel status="empty" action="none" art="empty-vault" emptyText="Empty." />
  {:else if view === 'list'}
    <div class="list">
      {#each rows as it (it.item_id)}
        <div class="row" class:hot={storage.highlight?.containerId === containerId && storage.highlight?.template === it.template} role="listitem">
          <ItemCell item={it} {panel} srcContainerId={containerId}
            draggable={draggableItems} {storageKind} {allowMarketSell} layout="list" />
        </div>
      {/each}
    </div>
  {:else}
    <div class="grid">
      {#if mic === 0}
        {#each items as it (it.item_id)}
          <div class="slot" role="listitem">
            <ItemCell item={it} {panel} srcContainerId={containerId}
              draggable={draggableItems} {storageKind} {allowMarketSell} />
          </div>
        {/each}
      {:else}
        {#each indices as idx (idx)}
          {@const it = byIndex.get(idx)}
          <div class="slot" class:filled={!!it} class:hot={!!it && storage.highlight?.containerId === containerId && storage.highlight?.template === it?.template} role="listitem">
            {#if it}
              <ItemCell item={it} {panel} srcContainerId={containerId}
                draggable={draggableItems} {storageKind} {allowMarketSell} />
            {:else}
              <span class="idx mono" aria-hidden="true">{idx}</span>
            {/if}
          </div>
        {/each}
      {/if}
    </div>
  {/if}
</div>

{#if view === 'list' && mic > 0 && status === 'ready'}
  <p class="slots-free mono">{freeSlots} of {mic} slots free</p>
{/if}

<style>
  .grid-wrap {
    max-height: 22rem; overflow-y: auto; padding: var(--space-1);
    border: 1px solid transparent; border-radius: var(--radius-sm);
    transition: border-color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .grid-wrap.drag-over { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
  /* Rows are taller than a tile row, so the list gets a little more height before
     it scrolls. The grid keeps its shipped 22rem exactly. */
  .grid-wrap.list { max-height: 28rem; }
  /* The list is a size container so a row can re-flow on the PANEL width, not
     the viewport: the bank and backpack panels are narrow on a wide desktop. */
  .list { display: flex; flex-direction: column; gap: var(--space-2); container-type: inline-size; }
  .row { min-width: 0; }
  .row.hot :global(.cell) { border-color: var(--ls-fremen); box-shadow: 0 0 0 2px color-mix(in srgb, var(--ls-fremen) 55%, transparent); }
  .slots-free { margin: var(--space-2) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
  .grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: var(--space-2); }
  .slot { min-width: 0; }
  .slot:not(.filled) {
    display: grid; place-items: center; aspect-ratio: 1;
    border: 1px dashed color-mix(in srgb, var(--edge) 70%, transparent);
    border-radius: var(--radius-sm); background: color-mix(in srgb, var(--edge) 8%, transparent);
  }
  .slot .idx { font-size: var(--text-xs); color: color-mix(in srgb, var(--text-muted) 55%, transparent); }
  .slot.hot :global(.cell) { border-color: var(--ls-fremen); box-shadow: 0 0 0 2px color-mix(in srgb, var(--ls-fremen) 55%, transparent); }
  .skeleton-slot { aspect-ratio: 1; border-radius: var(--radius-sm); }
  @media (max-width: 520px) { .grid { grid-template-columns: repeat(4, 1fr); } }
</style>
