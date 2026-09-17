<script module>
  // Per-instance heading id, so several dialogs can coexist without colliding
  // aria-labelledby targets.
  let seq = 0;
  function nextId() { return (seq += 1); }
</script>

<script>
  // Modal: the one focus-trapped dialog surface for the portal. Distilled from
  // AccountManageDialog (scrim + inert + Escape) and AugmentActionDialog, whose
  // 60-line comment block records the two live incidents this component exists
  // to make unrepeatable:
  //
  //   * 2026-08-03: the dialog inerted a container that CONTAINED it. `inert` is
  //     INHERITED, so it killed its own X and Cancel buttons and left the player
  //     stuck with a dead overlay. The `!el.contains(panelEl)` filter below is
  //     the backstop for that, and it stays even though the portal makes it
  //     redundant: the portal is WHAT makes it redundant, and this is what
  //     catches the portal regressing.
  //   * 2026-08-14: mounted deep in the tree, it opened ~47px too high with its
  //     header clipped under the topbar and no scrim dim, then snapped into
  //     place seconds later. Same CSS as a sibling dialog that had neither bug,
  //     so the mount depth WAS the bug. Both nodes portal to <body> here, which
  //     makes position and stacking independent of the call site.
  //
  // Two further fixes over the dialogs it replaces:
  //   * the focusable selector is the UNION including input/select/textarea, so
  //     text fields can no longer escape the Tab wrap (they can today in
  //     AccountManageDialog and SendSolariDialog, which omit them);
  //   * `.shell` is the inert target, not `.page`. `.shell` is the layout main
  //     and exists on every route; `.page` does not exist on routes/maps/[key].
  import { tick } from 'svelte';

  let {
    // The dialog may be driven by `open`, or simply mounted conditionally by the
    // caller (the shipped pattern in +layout.svelte), which is why this defaults
    // to true rather than false.
    open = true,
    title = '',
    labelledBy = '',
    label = '',
    size = 'md',
    dismissible = true,
    onClose,
    children,
    footer,
  } = $props();

  const autoId = `ls-modal-title-${nextId()}`;

  let panelEl = $state(null);
  let closeEl = $state(null);

  let headingId = $derived(title ? autoId : labelledBy || null);

  // Both nodes move to <body> on mount and are removed on destroy.
  function portalToBody(node) {
    document.body.appendChild(node);
    return { destroy() { node.remove(); } };
  }

  $effect(() => {
    // Wait for the panel: the ancestor test below is meaningless without it.
    if (!open || !panelEl) return;
    const opener = document.activeElement;
    const bg = [...document.querySelectorAll('.topbar, .shell, .tabbar, .tb-sheet, .ftr')]
      .filter(Boolean)
      .filter((el) => !el.contains(panelEl));
    for (const el of bg) el.setAttribute('inert', '');
    tick().then(() => (closeEl || panelEl)?.focus());
    return () => {
      for (const el of bg) el.removeAttribute('inert');
      if (opener && typeof opener.focus === 'function') opener.focus();
    };
  });

  const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

  function onKeydown(e) {
    if (e.key === 'Escape') {
      if (dismissible) onClose?.();
      return;
    }
    if (e.key !== 'Tab' || !panelEl) return;
    const f = panelEl.querySelectorAll(FOCUSABLE);
    if (!f.length) return;
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function onScrim() {
    if (dismissible) onClose?.();
  }
</script>

{#if open}
  <div class="scrim" role="presentation" onclick={onScrim} use:portalToBody></div>
  <div
    class="modal size-{size}"
    role="dialog"
    aria-modal="true"
    tabindex="-1"
    aria-labelledby={headingId}
    aria-label={headingId ? null : label || null}
    bind:this={panelEl}
    onkeydown={onKeydown}
    use:portalToBody
  >
    {#if title || dismissible}
      <header class="mhead">
        {#if title}<h2 id={autoId} class="mono">{title}</h2>{/if}
        {#if dismissible}
          <button class="x" type="button" bind:this={closeEl} onclick={() => onClose?.()} aria-label="Close">&times;</button>
        {/if}
      </header>
    {/if}

    <div class="mbody">
      {@render children?.()}
    </div>

    {#if footer}
      <footer class="mfoot">{@render footer()}</footer>
    {/if}
  </div>
{/if}

<style>
  .scrim {
    position: fixed; inset: 0;
    background: color-mix(in srgb, var(--bg-deep) 76%, transparent);
    z-index: var(--z-dialog);
  }
  .modal {
    position: fixed; z-index: calc(var(--z-dialog) + 1);
    top: 50%; left: 50%; transform: translate(-50%, -50%);
    max-height: calc(100vh - 3rem); overflow: auto;
    background: var(--metal-0); border: 1px solid var(--edge-hi);
    border-radius: var(--radius-sm);
    box-shadow: 0 18px 48px rgba(0, 0, 0, .5);
    animation: modal-in var(--motion-fast, .16s) var(--ease-out) both;
  }
  .size-sm { width: min(22rem, calc(100vw - 2rem)); }
  .size-md { width: min(26rem, calc(100vw - 2rem)); }
  .size-lg { width: min(38rem, calc(100vw - 2rem)); }

  .mhead {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-3); padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--edge);
  }
  .mhead h2 {
    margin: 0; font-size: var(--text-sm); color: var(--accent);
    text-transform: uppercase; letter-spacing: .1em;
  }
  .x {
    margin-left: auto; font-size: var(--text-lg); line-height: 1; color: var(--text-muted);
    background: none; border: 0; cursor: pointer; padding: 0 var(--space-1);
  }
  .x:hover { color: var(--text); }

  .mbody { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4); }
  .mfoot {
    display: flex; gap: var(--space-2); justify-content: flex-end;
    padding: var(--space-3) var(--space-4); border-top: 1px solid var(--edge);
  }

  @keyframes modal-in {
    from { opacity: 0; transform: translate(-50%, calc(-50% + 8px)); }
    to { opacity: 1; transform: translate(-50%, -50%); }
  }

  /* The global reduced-motion rule in app.css only SHORTENS durations, it does
     not remove the animation, so the entry motion is killed explicitly here. */
  @media (prefers-reduced-motion: reduce) {
    .modal { animation: none; }
  }
</style>
