// Native-listener shield for chrome overlaid INSIDE the engine viewport
// (mandatory M1 lesson, spec section 3). Svelte 5 delegates on* handlers to
// the app root, so they fire AFTER the engine's native viewport gesture
// listeners have already consumed the event; a delegated stopPropagation is
// too late. This action stops propagation natively at the chrome's root so
// the engine never sees the gesture. Side effect: it also starves Svelte's
// root-delegated handlers, so buttons INSIDE a shielded node must use capture
// handlers (onclickcapture etc.), which Svelte attaches natively per element.
export function shield(node) {
  const stop = (e) => e.stopPropagation();
  const types = ['pointerdown', 'pointerup', 'pointermove', 'click', 'wheel'];
  for (const t of types) node.addEventListener(t, stop);
  return { destroy() { for (const t of types) node.removeEventListener(t, stop); } };
}
