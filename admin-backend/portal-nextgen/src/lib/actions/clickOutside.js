// Svelte action: call the handler when a pointerdown lands outside the node.
// Owner ruling 2026-09-04: every popover menu (chat row actions, storage tile
// actions, Exchange price alert) closes on an outside click, not only on its
// own Close item or Escape.
//
// Capture phase, so a click another handler stops from bubbling still counts.
// A pointerdown inside a dialog is NOT outside: the chat menu opens Report and
// Mute in a portalled Modal, which lives under document.body rather than under
// the menu's node, and closing the menu there would unmount the dialog under
// the player's cursor.
export function clickOutside(node, handler) {
  let fn = handler;
  const onDown = (e) => {
    const t = e.target;
    if (!t || node.contains(t)) return;
    if (t.closest && t.closest('[role="dialog"], [aria-modal="true"]')) return;
    fn?.(e);
  };
  document.addEventListener('pointerdown', onDown, true);
  return {
    update(h) { fn = h; },
    destroy() { document.removeEventListener('pointerdown', onDown, true); },
  };
}
