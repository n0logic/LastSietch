<script>
  // One inventory slot. Shows the item icon, a stack-count badge, and a durability
  // bar; low durability warms red via DurabilityMeter. A "..." affordance opens a
  // per-slot popover of read/market actions (never offline-gated): Add price alert
  // (both panels), List on Exchange (RIGHT container panel + tradeable only), and
  // Send from bank (Tier 5, LEFT bank inv30 only, ships DARK). The cell is a
  // drag SOURCE for MOVE when the write surface is unlocked. The icon is ALSO an
  // ItemInfoPopover trigger (hover/tap the art for the full in-game-style stat
  // card) - a separate affordance from the "..." actions menu below.
  //
  // layout='list' (wave 10b) renders the same item as a ROW instead of a tile:
  // 40px icon, the full name wrapped over at most two lines, the quantity, the
  // durability bar, and the two everyday actions (Move, List on Exchange) as real
  // buttons in DOM order. The "..." menu keeps the rest. Both layouts share every
  // handler, the drag payload and the popover placement.
  import DurabilityMeter from './DurabilityMeter.svelte';
  import WatchAlert from './WatchAlert.svelte';
  import SellDialog from './SellDialog.svelte';
  import TransferDialog from './TransferDialog.svelte';
  import { storage, moveItem } from '$lib/storage.svelte.js';
  import { iconUrl } from '$lib/icons.js';
  import ItemInfoPopover from '$lib/components/item/ItemInfoPopover.svelte';
  import { clickOutside } from '$lib/actions/clickOutside.js';

  // panel: 'bank' | 'container'. srcContainerId is the drag payload origin.
  let {
    item, panel = 'container', srcContainerId, draggable = false,
    storageKind = '', allowMarketSell = true, layout = 'grid',
  } = $props();

  let open = $state(false);
  let action = $state('none'); // none | watch | sell | transfer
  let cellEl = $state();
  let popEl;
  let dropUp = $state(false); // open the popover UPWARD when the cell sits low in
                              // the scrollable grid, so overflow-y:auto can't clip it.
  let popDx = $state(0);      // px shift keeping the popover inside the clipping box
  let popMax = $state(0);     // px width cap so content wider than the panel wraps
                              // instead of overflowing (shifting cannot fix "wider
                              // than the container" — it just moves the overflow to
                              // the other side and raises a horizontal scrollbar).
  const EDGE_PAD = 4;

  let qty = $derived(Number(item?.stack_size) || 0);
  let tradeable = $derived(item?.tradeable === true);
  let icon = $derived(iconUrl(item?.icon));
  let name = $derived(item?.name || item?.template || 'Item');
  let canDrag = $derived(
    draggable && !!storageKind && storage.offlineOk && storage.pawnMoveEnabled
  );
  let moveTargetId = $derived(
    storageKind === 'bank' ? storage.backpack?.inv_id
      : (storageKind === 'backpack' ? storage.bank?.inv_id : null)
  );
  let moveTargetLabel = $derived(storageKind === 'bank' ? 'Backpack' : 'CHOAM Bank');
  let canMove = $derived(canDrag && moveTargetId != null);

  function toggle() {
    open = !open;
    if (!open) { action = 'none'; popDx = 0; dropUp = false; }
  }
  // Opens as well as selects: in the list row there is no menu to open first, the
  // inline "List on Exchange" button goes straight to the dialog.
  function pick(a) { action = a; open = true; }
  function closeAll() { open = false; action = 'none'; popDx = 0; dropUp = false; }

  // Re-place whenever the popover opens OR its content changes. The second half
  // matters: picking "List on Exchange" swaps a 12rem action list for a much wider
  // SellDialog, and placement computed for the menu is meaningless for the dialog
  // (reported 2026-07-24 — the sell dialog's title rendered as "DING WIRE").
  $effect(() => {
    open; action;                       // dependencies
    if (!open) return;
    requestAnimationFrame(placePopover); // measure after the new content lays out
  });

  // Nearest ancestor that actually clips, on EITHER axis.
  function clipParent() {
    let sc = cellEl?.parentElement;
    while (sc && sc !== document.body) {
      const cs = getComputedStyle(sc);
      const clips = (v) => v === 'auto' || v === 'scroll' || v === 'hidden';
      if (clips(cs.overflowY) || clips(cs.overflowX)) break;
      sc = sc.parentElement;
    }
    return sc && sc !== document.body ? sc : null;
  }

  // Keep the popover inside the clipping container, whatever it currently contains.
  //
  // This replaces an earlier left/right FLIP, which could not work: the grid is
  // ~265px and the action list ~192px, so columns 0-1 have room only rightward and
  // columns 3-4 only leftward — but the MIDDLE column has room in neither direction
  // and a binary choice overflows either way. Measuring the real box and shifting by
  // the minimum needed handles every column, and any content width, with no magic
  // numbers to be defeated by a wider child.
  function placePopover() {
    if (!cellEl || !popEl) return;
    const sc = clipParent();
    const b = sc ? sc.getBoundingClientRect()
                 : { top: 0, bottom: window.innerHeight,
                     left: 0, right: window.innerWidth };
    const cell = cellEl.getBoundingClientRect();

    // Cap the width FIRST, so the measurement below sees the WRAPPED box. The
    // Sell/Watch/Transfer dialogs are wider than a narrow storage panel, and no
    // amount of shifting makes an over-wide box fit — it just moves the overflow
    // to the other edge and raises a horizontal scrollbar.
    // Applied imperatively as well as through state: state lands on the next
    // render, and we need the new width in effect for getBoundingClientRect NOW.
    popMax = Math.max(0, Math.round((b.right - b.left) - EDGE_PAD * 2));
    popEl.style.maxWidth = popMax + "px";

    const p = popEl.getBoundingClientRect();

    // Vertical: flip up only if the MEASURED height doesn't fit below and fits better
    // above. Previously this used a hardcoded 220px guess.
    const below = b.bottom - cell.bottom;
    const above = cell.top - b.top;
    dropUp = p.height > below && above > below;

    // Horizontal: p already includes the current shift, so undo it to get the
    // natural box, then clamp that inside the container.
    const rawLeft = p.left - popDx;
    const rawRight = p.right - popDx;
    let dx = 0;
    if (rawRight > b.right - EDGE_PAD) dx = (b.right - EDGE_PAD) - rawRight;
    if (rawLeft + dx < b.left + EDGE_PAD) dx = (b.left + EDGE_PAD) - rawLeft;
    popDx = dx;
  }

  function onDragStart(e) {
    if (!canDrag) { e.preventDefault(); return; }
    const payload = JSON.stringify({
      item_id: item.item_id,
      template: item.template || '',
      src_container_id: srcContainerId,
      src_storage: storageKind,
    });
    e.dataTransfer.setData('application/x-ls-item', payload);
    e.dataTransfer.setData('text/plain', payload);
    e.dataTransfer.effectAllowed = 'move';
  }

  async function moveToOtherPawnStorage() {
    if (!canMove) return;
    closeAll();
    await moveItem(item.item_id, moveTargetId, srcContainerId, item.template || '');
  }
</script>

<!-- Drag is a pointer-only MOVE affordance; the item's content is already exposed
     via the icon alt + name, and every action has a keyboard-reachable popover. -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={cellEl}
  class="cell"
  use:clickOutside={() => { if (open) closeAll(); }}
  class:list={layout === 'list'}
  class:draggable={canDrag}
  draggable={canDrag}
  ondragstart={onDragStart}
  data-item-id={item?.item_id}
>
  {#if layout === 'list'}
    <div class="art">
      <ItemInfoPopover {item} label="Item details: {name}" hoverTarget={cellEl}>
        {#if icon}
          <img src={icon} alt={name} loading="lazy" />
        {:else}
          <span class="glyph mono" aria-hidden="true">{(name[0] || '?').toUpperCase()}</span>
        {/if}
      </ItemInfoPopover>
    </div>
    <div class="row-main">
      <span class="name">{name}</span>
      {#if item?.durable}
        <DurabilityMeter durability={item.durability} label={name} />
      {/if}
    </div>
    <div class="row-side">
      {#if qty > 0}<span class="row-qty mono">{qty.toLocaleString()}</span>{/if}
      {#if storageKind}
        <button class="row-btn" type="button"
          disabled={!canMove} onclick={moveToOtherPawnStorage}>
          Move to {moveTargetLabel}
        </button>
      {/if}
      {#if tradeable && allowMarketSell}
        <button class="row-btn" type="button" onclick={() => pick('sell')}>List on Exchange</button>
      {/if}
      <button class="more mono" type="button" aria-label="Item actions" aria-expanded={open} onclick={toggle}>&hellip;</button>
    </div>
  {:else}
    <div class="art">
      <ItemInfoPopover {item} label="Item details: {name}" hoverTarget={cellEl}>
        {#if icon}
          <img src={icon} alt={name} loading="lazy" />
        {:else}
          <span class="glyph mono" aria-hidden="true">{(name[0] || '?').toUpperCase()}</span>
        {/if}
      </ItemInfoPopover>
      {#if qty > 1}<span class="qty mono">{qty.toLocaleString()}</span>{/if}
      <button class="more mono" type="button" aria-label="Item actions" aria-expanded={open} onclick={toggle}>&hellip;</button>
    </div>

    {#if item?.durable}
      <DurabilityMeter durability={item.durability} label={name} />
    {/if}
    <span class="name" title={name}>{name}</span>
  {/if}

  {#if open}
    <div bind:this={popEl} class="pop" class:up={dropUp} role="menu"
         style="--pop-dx: {popDx}px; --pop-max: {popMax ? popMax + 'px' : 'none'}">
      {#if action === 'none'}
        <p class="pop-name">{name}</p>
        {#if storageKind}
          <button class="pop-item" type="button" role="menuitem"
            disabled={!canMove} onclick={moveToOtherPawnStorage}>
            Move to {moveTargetLabel}
          </button>
        {/if}
        <button class="pop-item" type="button" role="menuitem" onclick={() => pick('watch')}>Add price alert</button>
        {#if tradeable && allowMarketSell}
          <button class="pop-item" type="button" role="menuitem" onclick={() => pick('sell')}>List on Exchange</button>
        {/if}
        {#if panel === 'bank'}
          <button class="pop-item" type="button" role="menuitem" onclick={() => pick('transfer')}>Send from bank&hellip;</button>
        {/if}
        <button class="pop-item close" type="button" onclick={closeAll}>Close</button>
      {:else if action === 'watch'}
        <WatchAlert {item} onDone={closeAll} />
      {:else if action === 'sell'}
        <SellDialog {item} containerId={srcContainerId} onDone={closeAll} />
      {:else if action === 'transfer'}
        <TransferDialog {item} onDone={closeAll} />
      {/if}
    </div>
  {/if}
</div>

<style>
  .cell {
    position: relative; display: flex; flex-direction: column; gap: var(--space-1);
    padding: var(--space-2); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi);
    min-width: 0;
  }
  .cell.draggable { cursor: grab; }
  .cell.draggable:active { cursor: grabbing; }
  .art { position: relative; aspect-ratio: 1; display: grid; place-items: center; background: color-mix(in srgb, var(--edge) 22%, transparent); border-radius: var(--radius-sm); overflow: hidden; }
  .art img { width: 100%; height: 100%; object-fit: contain; }
  .glyph { font-size: var(--text-xl); color: var(--text-muted); }
  .qty {
    position: absolute; right: 3px; bottom: 3px;
    font-size: var(--text-xs); font-variant-numeric: tabular-nums;
    color: var(--text); background: rgba(0,0,0,.62); border: 1px solid var(--edge);
    padding: 0 4px; border-radius: 3px; line-height: 1.4;
  }
  .more {
    position: absolute; right: 2px; top: 2px; width: 18px; height: 18px;
    display: grid; place-items: center; line-height: 1;
    color: var(--text-muted); background: rgba(0,0,0,.5); border: 1px solid var(--edge);
    border-radius: 3px; cursor: pointer; opacity: 0; transition: opacity var(--motion-fast) var(--ease-out);
  }
  .art:hover .more, .more:focus-visible { opacity: 1; }
  .more:hover { color: var(--accent-text); border-color: var(--accent); }
  .name { font-size: var(--text-xs); color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* List row. The tile's column stack becomes one line: icon, name + durability,
     then the quantity and the inline actions. */
  .cell.list {
    display: grid; grid-template-columns: 40px minmax(0, 1fr) auto;
    align-items: center; gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
  }
  .cell.list .art { width: 40px; height: 40px; }
  .cell.list .row-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  /* The list exists to show the whole name, so it wraps to two lines and CLIPS.
     No ellipsis: a truncated name is what the grid tile already gives you. */
  .cell.list .name {
    font-size: var(--text-sm); color: var(--text);
    white-space: normal; text-overflow: clip; overflow: hidden;
    line-height: 1.3; max-height: 2.6em; overflow-wrap: anywhere;
  }
  .cell.list .row-side { display: flex; align-items: center; gap: var(--space-2); }
  .cell.list .row-qty { font-size: var(--text-sm); font-variant-numeric: tabular-nums; color: var(--text-muted); }
  /* The tile hides "..." until the art is hovered; a row has no such surface. */
  .cell.list .more { position: static; opacity: 1; width: 20px; height: 20px; }
  .row-btn {
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .08em;
    color: var(--text-muted); background: transparent; white-space: nowrap;
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .row-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent-text); }
  .row-btn:disabled { opacity: .45; cursor: not-allowed; }
  .row-btn:disabled:hover { border-color: var(--edge); color: var(--text-muted); }
  /* Narrow PANEL (2.14.2): the two inline actions are wider than the bank and
     backpack columns on a desktop, and an `auto` third column let them shove
     the name column down to zero width, so a list row showed no name at all.
     Below 480px of container width the side drops under the name, the same
     shape the phone breakpoint uses for the viewport. */
  @container (max-width: 480px) {
    .cell.list { grid-template-columns: 40px minmax(0, 1fr); }
    .cell.list .row-side { grid-column: 2; flex-wrap: wrap; }
  }
  @media (max-width: 700px) {
    .cell.list { grid-template-columns: 40px minmax(0, 1fr); }
    .cell.list .row-side { grid-column: 2; flex-wrap: wrap; }
  }

  .pop {
    position: absolute; z-index: 5; top: 100%; right: 0; margin-top: 4px; min-width: 12rem;
    display: flex; flex-direction: column; gap: 2px; padding: var(--space-2);
    background: var(--metal-1); border: 1px solid var(--edge-hi); border-radius: var(--radius-sm);
    box-shadow: var(--shadow-overlay);
  }
  /* Flip above the cell near the bottom of the scrollable grid so overflow can't clip it. */
  .pop.up { top: auto; bottom: 100%; margin-top: 0; margin-bottom: 4px; }
  /* Grow rightward for left-column cells — the default right:0 grows leftward, out
     of the grid, and the grid clips on X as well as Y. */
  /* Horizontal keep-inside shift, measured at open and on every content change.
     Replaces a left/right flip that could not satisfy the middle column. */
  .pop { transform: translateX(var(--pop-dx, 0px)); max-width: var(--pop-max, none); }
  .pop-name { margin: 0 0 var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .pop-item {
    text-align: left; font-size: var(--text-sm); color: var(--text);
    background: transparent; border: 1px solid transparent; border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); cursor: pointer;
  }
  .pop-item:hover { border-color: var(--accent); color: var(--accent-text); }
  .pop-item:disabled { color: var(--text-muted); opacity: .55; cursor: not-allowed; }
  .pop-item:disabled:hover { border-color: transparent; color: var(--text-muted); }
  .pop-item.close { color: var(--text-muted); margin-top: var(--space-1); }
</style>
