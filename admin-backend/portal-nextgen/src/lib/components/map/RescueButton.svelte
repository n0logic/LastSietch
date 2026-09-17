<script>
  // Self-rescue emergency control. Sits on the board frame OUTSIDE .tilt-plane
  // (M1 lesson 2: interactive controls never ride the animating plane) and is
  // shielded (M1 lesson 1), so every button inside uses CAPTURE handlers.
  // Flow: carved button -> confirm sheet stating exactly what happens ->
  // POST /portal/rescue -> result strip. The server is authoritative for every
  // gate; `gate` mirrors the ones the chrome can see (offline, no totem,
  // Deep Desert) as honest disabled states, and the 1/hour cooldown the server
  // reports back is persisted so a refresh cannot fake readiness away.
  import { api } from '$lib/api.js';
  import { shield } from './shield.js';

  // gate: null = usable as far as chrome knows; { reason } = honest disabled.
  let { gate = null } = $props();

  const COOLDOWN_LS = 'ls-rescue-until';

  let confirming = $state(false);
  let busy = $state(false);
  let result = $state(null); // { ok, message }
  let cooldownUntil = $state(0);
  let now = $state(Date.now());

  $effect(() => {
    try { cooldownUntil = Number(localStorage.getItem(COOLDOWN_LS)) || 0; } catch (e) {}
    const id = setInterval(() => { now = Date.now(); }, 1_000);
    return () => clearInterval(id);
  });

  let cooldownLeft = $derived(Math.max(0, Math.ceil((cooldownUntil - now) / 1000)));
  let disabled = $derived(busy || !!gate || cooldownLeft > 0);
  let reason = $derived(
    gate?.reason
      ?? (cooldownLeft > 0
        ? `Rescue is on cooldown. Ready in about ${Math.max(1, Math.ceil(cooldownLeft / 60))}m.`
        : '')
  );

  function startCooldown(seconds) {
    const s = Number(seconds) || 0;
    if (s <= 0) return;
    cooldownUntil = Date.now() + s * 1000;
    try { localStorage.setItem(COOLDOWN_LS, String(cooldownUntil)); } catch (e) {}
  }

  // Frozen contract with the backend (api.maps.rescue): every outcome answers
  // {ok, state, message, cooldown_remaining_s?}. Refusals are semantic (the
  // message is the server's honest copy); only network/shape failures throw.
  async function send() {
    confirming = false;
    busy = true;
    result = null;
    try {
      const d = await api.maps.rescue();
      result = { ok: !!d.ok, message: d.message || (d.ok ? 'Rescue sent.' : 'Rescue refused.') };
      if (d.cooldown_remaining_s > 0) startCooldown(d.cooldown_remaining_s);
      else if (d.ok) startCooldown(3600); // success = fresh 1 hour window
    } catch (e) {
      result = {
        ok: false,
        message: e?.status === 401 || e?.status === 403
          ? 'Your session expired. Sign in again to use rescue.'
          : 'Rescue request failed. Check your connection and retry.',
      };
    } finally {
      busy = false;
    }
  }

  function onKeydown(e) {
    if (e.key === 'Escape') confirming = false;
  }

  // The confirm sheet + scrim are position:fixed, but .board-frame declares
  // perspective, which makes it the CONTAINING BLOCK for fixed descendants --
  // rendered in place they would cover only the board frame, not the screen
  // (aria-modal would lie). Portal them to document.body so fixed really
  // means the viewport.
  function portalToBody(node) {
    document.body.appendChild(node);
    return { destroy() { node.remove(); } };
  }
</script>

<svelte:window onkeydown={confirming ? onKeydown : undefined} />

<div class="rescue" use:shield>
  <button
    class="btn mono"
    disabled={disabled}
    aria-busy={busy}
    title={reason || undefined}
    onclickcapture={() => { if (!disabled) { result = null; confirming = true; } }}
  >
    <span class="beacon" class:armed={!disabled} aria-hidden="true"></span>
    {#if busy}Sending...{:else if cooldownLeft > 0}Rescue in {Math.max(1, Math.ceil(cooldownLeft / 60))}m{:else}Help! I'm stuck{/if}
  </button>
  {#if reason && !busy}
    <p class="reason mono">{reason}</p>
  {/if}
  {#if result}
    <div class="strip" class:ok={result.ok} class:err={!result.ok} role="status" aria-live="polite">
      <p class="strip-msg">{result.message}</p>
      <button class="strip-close" aria-label="Dismiss rescue result" onclickcapture={() => { result = null; }}>&times;</button>
    </div>
  {/if}
</div>

{#if confirming}
  <!-- Portaled outside the app root: delegated on* handlers would never fire
       here, so everything interactive uses CAPTURE handlers (attached natively
       per element, same rule as shielded chrome). -->
  <div class="portal-root" use:portalToBody>
    <div class="scrim" aria-hidden="true" onclickcapture={() => { confirming = false; }}></div>
    <div class="confirm" role="dialog" aria-modal="true" aria-label="Confirm self-rescue">
      <h2 class="confirm-title">Send a rescue?</h2>
      <p class="confirm-body">
        This teleports your character to your own nearest base totem. The
        destination is chosen by the server; you cannot pick it.
      </p>
      <p class="confirm-body">
        Works about once per hour, only while you are logged in to the game,
        and never in the Deep Desert.
      </p>
      <div class="confirm-row">
        <button class="confirm-go mono" onclickcapture={send}>Send rescue</button>
        <button class="confirm-no mono" onclickcapture={() => { confirming = false; }}>Cancel</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .rescue {
    display: flex; flex-direction: column; gap: var(--space-1);
    align-items: flex-start; max-width: 240px;
  }

  /* Carved emergency control: inset bezel plate around a stamped button. */
  .btn {
    display: inline-flex; align-items: center; gap: var(--space-2);
    background: var(--panel); color: var(--text);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); cursor: pointer;
    font-size: var(--text-xs); font-weight: 700;
    letter-spacing: .14em; text-transform: uppercase;
    box-shadow:
      inset 0 1px 0 var(--metal-hi),
      inset 0 0 0 1px rgba(0, 0, 0, .45),
      0 6px 14px rgba(0, 0, 0, .35);
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--ls-red); color: var(--ls-red); }
  .btn:disabled { cursor: default; color: var(--text-muted); opacity: .8; }

  .beacon {
    flex: none; width: 8px; height: 8px; border-radius: 50%;
    background: var(--text-muted); box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .4);
  }
  .beacon.armed {
    background: var(--ls-red);
    animation: beacon-breathe 2.6s var(--ease-in-out) infinite;
  }
  @keyframes beacon-breathe {
    0%, 100% { box-shadow: 0 0 2px rgba(214, 90, 68, .4); }
    50% { box-shadow: 0 0 8px rgba(214, 90, 68, .8); }
  }

  .reason {
    margin: 0; color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .04em; line-height: 1.35;
    background: color-mix(in srgb, var(--bg-deep) 72%, transparent);
    border-radius: var(--radius-sm); padding: 2px var(--space-2);
  }

  .strip {
    display: flex; align-items: flex-start; gap: var(--space-2);
    background: var(--panel); border-radius: var(--radius-sm);
    border: 1px solid var(--edge); padding: var(--space-2) var(--space-3);
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 6px 14px rgba(0, 0, 0, .35);
  }
  .strip.ok { border-color: var(--accent); }
  .strip.err { border-color: color-mix(in srgb, var(--ls-red) 60%, var(--edge)); }
  .strip-msg { margin: 0; font-size: var(--text-xs); line-height: 1.4; color: var(--text); }
  .strip.err .strip-msg { color: color-mix(in srgb, var(--ls-red) 70%, var(--text)); }
  .strip-close {
    flex: none; background: transparent; border: 0; cursor: pointer;
    color: var(--text-muted); font-size: var(--text-base); line-height: 1; padding: 0;
  }
  .strip-close:hover { color: var(--text); }

  /* Confirm sheet: fixed overlay, so it never rides the tilt plane. Modal
     band sits ABOVE the sticky topbar (z 50): while this write-confirm is
     open, nothing interactive (Sign out, theme, nav) may bleed through. */
  .scrim {
    position: fixed; inset: 0; z-index: var(--z-modal, 55);
    background: rgba(0, 0, 0, .45);
  }
  .confirm {
    position: fixed; z-index: calc(var(--z-modal, 55) + 1); left: 50%; top: 50%;
    transform: translate(-50%, -50%);
    width: min(92vw, 380px);
    background: var(--panel); border: 1px solid var(--edge-hi);
    border-radius: var(--radius-sm); padding: var(--space-4);
    box-shadow: inset 0 1px 0 var(--metal-hi), var(--shadow-overlay);
  }
  .confirm-title {
    margin: 0 0 var(--space-2); font-size: var(--text-lg);
    letter-spacing: .04em; text-transform: uppercase;
  }
  .confirm-body {
    margin: 0 0 var(--space-2); color: var(--text-muted);
    font-size: var(--text-sm); line-height: 1.5;
  }
  .confirm-row { display: flex; gap: var(--space-2); margin-top: var(--space-3); }
  .confirm-go {
    flex: 1; background: var(--ls-red); color: var(--bg-deep);
    border: 1px solid var(--ls-red); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); cursor: pointer;
    font-size: var(--text-xs); font-weight: 700;
    letter-spacing: .14em; text-transform: uppercase;
  }
  .confirm-go:hover { filter: brightness(1.1); }
  .confirm-no {
    flex: 1; background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); cursor: pointer;
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
  }
  .confirm-no:hover { border-color: var(--accent); }

  @media (prefers-reduced-motion: reduce) {
    .beacon.armed { animation: none; }
  }
</style>
