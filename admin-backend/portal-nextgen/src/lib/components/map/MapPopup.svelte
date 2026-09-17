<script>
  // Marker detail popup anchored at the selected marker's viewport position.
  // `detail` = the engine's onSelect payload; null closes. All feed-derived
  // strings render via text interpolation only (never raw HTML).
  import { shield } from './shield.js';

  let { detail = null, onclose } = $props();

  let copied = $state(false);
  let copyTimer;

  async function copyCoords() {
    if (!detail?.coordStr) return;
    try {
      await navigator.clipboard.writeText(detail.coordStr);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => { copied = false; }, 1600);
    } catch (e) {}
  }

  $effect(() => () => clearTimeout(copyTimer));

  function onKeydown(e) {
    if (e.key === 'Escape') onclose?.();
  }

  // Markers near the viewport's top edge flip the popup below the anchor so it
  // is not clipped by the viewport's overflow:hidden (V1 parity).
  let below = $derived((detail?.screenY ?? 0) < 170);

  // The popup lives inside the engine's viewport: without the shared shield
  // action, a pointerdown on the copy button bubbles to the engine's gesture
  // handler, whose empty hit test emits onSelect(null) and unmounts the popup
  // before click can fire. Buttons inside use CAPTURE handlers (see shield.js).
</script>

<svelte:window onkeydown={detail ? onKeydown : undefined} />

{#if detail}
  <div
    class="popup"
    class:below
    role="dialog"
    aria-label="Marker detail"
    tabindex="-1"
    style:left={`${detail.screenX}px`}
    style:top={`${detail.screenY}px`}
    use:shield
    onkeydown={onKeydown}
  >
    <div class="head">
      <span class="swatch" style:background={detail.color}></span>
      <p class="name">{detail.name}</p>
      <button class="close" aria-label="Close marker detail" onclickcapture={() => onclose?.()}>&times;</button>
    </div>
    <p class="cat mono">
      {detail.catLabel}{#if detail.typeLabel && detail.typeLabel !== detail.catLabel} | {detail.typeLabel}{/if}
    </p>
    {#if detail.coordStr}
      <div class="coords">
        <span class="mono coord-val">{detail.coordStr}</span>
        <button class="copy mono" onclickcapture={copyCoords} aria-live="polite">
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    {/if}
  </div>
{/if}

<style>
  .popup {
    position: absolute; z-index: 12;
    transform: translate(-50%, calc(-100% - 14px));
    min-width: 190px; max-width: 260px;
    background: var(--panel); border: 1px solid var(--edge-hi);
    border-radius: var(--radius-sm); padding: var(--space-3);
    box-shadow: inset 0 1px 0 var(--metal-hi), var(--shadow-overlay);
  }
  /* Anchor stem pointing at the marker. */
  .popup::after {
    content: ''; position: absolute; left: 50%; bottom: -6px;
    width: 10px; height: 10px; transform: translateX(-50%) rotate(45deg);
    background: var(--metal-0); border-right: 1px solid var(--edge-hi);
    border-bottom: 1px solid var(--edge-hi);
  }
  /* Flipped below the anchor near the top edge. */
  .popup.below { transform: translate(-50%, 16px); }
  .popup.below::after {
    bottom: auto; top: -6px; transform: translateX(-50%) rotate(225deg);
  }

  .head { display: flex; align-items: center; gap: var(--space-2); }
  .swatch {
    flex: none; width: 10px; height: 10px; border-radius: 3px;
    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .4);
  }
  .name {
    flex: 1; margin: 0; font-family: var(--font-display); font-weight: 700;
    font-size: var(--text-base); line-height: 1.2; color: var(--text);
    overflow-wrap: anywhere;
  }
  .close {
    flex: none; background: transparent; border: 0; cursor: pointer;
    color: var(--text-muted); font-size: var(--text-lg); line-height: 1;
    padding: 0 var(--space-1);
  }
  .close:hover { color: var(--text); }

  .cat {
    margin: var(--space-1) 0 0; color: var(--accent);
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .16em;
  }
  .coords {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-2); margin-top: var(--space-2);
    border-top: 1px solid var(--border-subtle); padding-top: var(--space-2);
  }
  .coord-val { color: var(--text); font-size: var(--text-xs); }
  .copy {
    background: var(--bg-elevated); color: var(--text-muted);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: 2px var(--space-2); cursor: pointer;
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .copy:hover { color: var(--text); border-color: var(--accent); }
</style>
