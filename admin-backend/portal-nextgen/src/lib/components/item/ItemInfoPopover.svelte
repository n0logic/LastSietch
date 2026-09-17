<script>
  // Hover-to-preview / click-to-pin wrapper around ItemCard, for any item
  // trigger (an icon, a row) anywhere items render. Desktop: hovering the
  // trigger opens a transient preview; a real click/Enter/Space pins it open
  // and moves focus into it. Touch: tap opens (there is no real hover), and
  // it is dismissed via the close button, Escape, or a tap outside - all
  // three always work, since the wrapper cannot tell which input reached it.
  //
  // Portaled to <body> so the card always escapes the scrollable/clipped
  // item grids it is triggered from - position:fixed math computed from the
  // trigger's own viewport rect would otherwise be thrown off by a hovering
  // CarvedSlab ancestor's translateY, which creates a containing block for
  // fixed descendants while it animates.
  import { tick, onDestroy } from 'svelte';
  import ItemCard from './ItemCard.svelte';

  let { item, label, children, hoverTarget = null } = $props();

  let open = $state(false);
  let positioned = $state(false);
  let triggerEl = $state();
  let cardEl = $state();
  let closeBtnEl = $state();
  let left = $state(0);
  let top = $state(0);
  let hoverTimer = null;
  let ro = null;
  let listening = false;

  const OPEN_DELAY = 150;
  const CLOSE_DELAY = 250;
  const PAD = 10;

  function place() {
    if (!triggerEl || !cardEl) return;
    const t = triggerEl.getBoundingClientRect();
    const c = cardEl.getBoundingClientRect();
    let l = t.left;
    let tp = t.bottom + PAD;
    // Flip above the trigger when there is more room there than below.
    if (tp + c.height > window.innerHeight - PAD && t.top - PAD - c.height > PAD) {
      tp = t.top - PAD - c.height;
    }
    l = Math.min(Math.max(PAD, l), Math.max(PAD, window.innerWidth - c.width - PAD));
    tp = Math.min(Math.max(PAD, tp), Math.max(PAD, window.innerHeight - c.height - PAD));
    left = Math.round(l);
    top = Math.round(tp);
    positioned = true;
  }

  function addGlobalListeners() {
    if (listening) return;
    listening = true;
    // capture:true so a scroll on an INNER scrollable ancestor (the item
    // grids scroll internally) still reaches this window-level listener.
    window.addEventListener('scroll', closeCard, { capture: true, passive: true });
    window.addEventListener('resize', closeCard, { passive: true });
    document.addEventListener('keydown', onKeydown);
    document.addEventListener('pointerdown', onOutside, true);
  }
  function removeGlobalListeners() {
    if (!listening) return;
    listening = false;
    window.removeEventListener('scroll', closeCard, true);
    window.removeEventListener('resize', closeCard);
    document.removeEventListener('keydown', onKeydown);
    document.removeEventListener('pointerdown', onOutside, true);
  }

  async function openCard(focusClose = false) {
    open = true;
    positioned = false;
    addGlobalListeners();
    await tick();
    // The card's real height depends on the async gear-stats lookup inside
    // ItemCard, which lands after this first paint - keep repositioning on
    // every size change for as long as the card is open, not just once.
    if (!ro) ro = new ResizeObserver(() => place());
    ro.observe(cardEl);
    requestAnimationFrame(place);
    if (focusClose) {
      await tick();
      closeBtnEl?.focus();
    }
  }
  function closeCard() {
    if (!open) return;
    open = false;
    positioned = false;
    removeGlobalListeners();
    ro?.disconnect();
  }
  function onKeydown(e) {
    if (e.key === 'Escape') { closeCard(); triggerEl?.focus(); }
  }
  function onOutside(e) {
    if (triggerEl?.contains(e.target) || cardEl?.contains(e.target)) return;
    closeCard();
  }
  function onEnter() {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => openCard(false), OPEN_DELAY);
  }
  function onLeave() {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(closeCard, CLOSE_DELAY);
  }
  function onClick() {
    clearTimeout(hoverTimer);
    openCard(true);
  }

  // Optional wider hover surface (owner note 2026-09-04): the storage grid's
  // trigger is only the icon square, so the name row and the cell padding did
  // nothing and the actions button in the corner broke the hover. A host can
  // hand in its whole tile; enter and leave on it drive the same timers.
  $effect(() => {
    const el = hoverTarget;
    if (!el || typeof el.addEventListener !== 'function') return;
    el.addEventListener('mouseenter', onEnter);
    el.addEventListener('mouseleave', onLeave);
    return () => {
      el.removeEventListener('mouseenter', onEnter);
      el.removeEventListener('mouseleave', onLeave);
    };
  });

  onDestroy(() => {
    clearTimeout(hoverTimer);
    removeGlobalListeners();
    ro?.disconnect();
  });

  // Moves its node to <body> on mount, restores nothing on destroy beyond
  // removal (Svelte already tore down the block's contents by then).
  function portal(node) {
    document.body.appendChild(node);
    return { destroy() { node.parentNode?.removeChild(node); } };
  }
</script>

<button
  bind:this={triggerEl}
  type="button"
  class="trigger"
  aria-haspopup="dialog"
  aria-expanded={open}
  aria-label={label || `${item?.name || 'Item'} details`}
  onmouseenter={onEnter}
  onmouseleave={onLeave}
  onclick={onClick}
>
  {@render children?.()}
</button>

{#if open}
  <div
    bind:this={cardEl}
    use:portal
    class="pop"
    class:ready={positioned}
    role="dialog"
    tabindex="-1"
    aria-label="{item?.name || 'Item'} details"
    style="left:{left}px; top:{top}px;"
    onmouseenter={onEnter}
    onmouseleave={onLeave}
  >
    <button
      bind:this={closeBtnEl}
      class="close"
      type="button"
      onclick={() => { closeCard(); triggerEl?.focus(); }}
      aria-label="Close item details"
    >&times;</button>
    <ItemCard {item} />
  </div>
{/if}

<style>
  .trigger {
    all: unset;
    /* grid + place-items:center (not display:block) so a child WITHOUT its
       own explicit sizing - e.g. ItemCell's single-glyph icon fallback -
       still centers within the trigger exactly as it centered within its
       old direct parent, instead of collapsing to the top-left. */
    display: grid; place-items: center; width: 100%; height: 100%;
    cursor: pointer; box-sizing: border-box; border-radius: var(--radius-sm);
  }
  .trigger:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .pop {
    position: fixed; z-index: var(--z-modal, 55);
    visibility: hidden; /* avoid a one-frame flash at (0,0) before placement */
    animation: pop-in var(--motion-fast) var(--ease-out);
  }
  .pop.ready { visibility: visible; }
  .close {
    position: absolute; top: 6px; right: 6px; z-index: 1;
    width: 22px; height: 22px; display: grid; place-items: center; line-height: 1;
    color: var(--text-muted); background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: 3px; cursor: pointer;
  }
  .close:hover { color: var(--accent-text); border-color: var(--accent); }

  @keyframes pop-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) { .pop { animation: none; } }
</style>
