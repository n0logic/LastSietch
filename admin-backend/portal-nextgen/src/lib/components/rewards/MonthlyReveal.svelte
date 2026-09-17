<script>
  // Monthly reveal: the dialog a successful monthly claim opens. Driven entirely
  // by the store (`rewards.reveal`, set in claimMonthly), never by MonthlyTrack,
  // which shows the gate and never claims on its own -- the real claim path is
  // ClaimAction -> claimAll -> claimMonthly.
  //
  // The sequence is CSS phase classes only (sealed chest -> charge/shake ->
  // radial amber burst -> RewardCard scales in). Nothing is minted here: the
  // reward has already landed in the player's CHOAM bank by the time this opens,
  // and the card is the snapshot the store took before the write.
  //
  // The trap, Escape, the scrim and the portal-to-body all belong to Modal; this
  // component must never hand-roll them (Modal's header comment records the two
  // live incidents that rule comes from).
  import Modal from '$lib/components/Modal.svelte';
  import { detectQuality } from '$lib/quality.js';
  import RewardCard from './RewardCard.svelte';

  let { open = false, reward = null, onclose } = $props();

  let host = $state(null);
  let phase = $state('sealed'); // sealed | charging | burst | revealed

  // ENHANCED gate mirrors the ContainerHero self-gate (high tier + webgl + motion).
  function enhanceGatePass() {
    const q = detectQuality();
    return q.tier === 'high' && !q.reducedMotion && q.webgl;
  }

  // Phase 2 hook. Intentionally a no-op today: nothing is imported and nothing
  // draws, so the CSS reveal below is the only visual. In Phase 2 this is where a
  // lazy spice-particle burst would mount:
  //   const { createHoloViewer } = await import('$lib/map/holo/index.js');
  async function mountEnhanced() {
    if (!enhanceGatePass() || !host) return;
    return; // no live draw wired
  }

  // Drive the baseline reveal off `open`. No live draw, no reward mutation.
  $effect(() => {
    if (!open) { phase = 'sealed'; return; }
    phase = 'charging';
    const t1 = setTimeout(() => { phase = 'burst'; mountEnhanced(); }, 900);
    const t2 = setTimeout(() => { phase = 'revealed'; }, 1500);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  });
</script>

<Modal {open} title="Monthly reward" size="sm" onClose={onclose}>
  <div class="reveal" bind:this={host} data-phase={phase}>
    <div class="stage">
      <div class="burst" aria-hidden="true"></div>
      <div class="chest" aria-hidden="true">?</div>
      {#if phase === 'revealed' && reward}
        <div class="prize"><RewardCard {reward} /></div>
      {/if}
    </div>
    {#if phase === 'revealed'}
      <p class="landed">Collect it in game from any CHOAM bank terminal.</p>
    {/if}
  </div>
</Modal>

<style>
  .reveal { display: grid; place-items: center; padding: var(--space-4) 0; }
  .stage { position: relative; width: 9rem; height: 9rem; display: grid; place-items: center; }
  /* The card replaces the chest in the SAME cell. Two in-flow grid items would
     stack into two rows and push the card out of the stage and over the line
     below it, which is what happened the first time this actually rendered. */
  .chest, .prize { grid-area: 1 / 1; }
  .landed { margin: var(--space-3) 0 0; font-size: var(--text-sm); color: var(--text-muted); text-align: center; }

  .chest {
    font-family: var(--font-display); font-size: var(--text-3xl); font-weight: 700;
    color: var(--ls-melange-hi);
    width: 6rem; height: 6rem; display: grid; place-items: center;
    border: 1px solid color-mix(in srgb, var(--ls-melange) 55%, var(--edge));
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--ls-melange) 12%, var(--metal-0));
    box-shadow: 0 0 18px -2px var(--ls-melange-glow);
  }
  [data-phase='charging'] .chest { animation: shake .5s var(--ease-in-out) infinite; }
  [data-phase='burst'] .chest, [data-phase='revealed'] .chest { opacity: 0; transform: scale(.6); }

  .burst {
    position: absolute; inset: 0; border-radius: 999px; opacity: 0; pointer-events: none;
    background: radial-gradient(circle, var(--accent-glow) 0%, transparent 70%);
  }
  [data-phase='burst'] .burst { animation: burst .6s var(--ease-out) forwards; }

  .prize { animation: prize-in .4s var(--ease-out) forwards; }

  @keyframes shake { 0%,100% { transform: translateX(0); } 25% { transform: translateX(-3px) rotate(-2deg); } 75% { transform: translateX(3px) rotate(2deg); } }
  @keyframes burst { 0% { opacity: 0; transform: scale(.4); } 40% { opacity: 1; } 100% { opacity: 0; transform: scale(2.2); } }
  @keyframes prize-in { from { opacity: 0; transform: scale(.7); } to { opacity: 1; transform: none; } }

  @media (prefers-reduced-motion: reduce) {
    .chest, .burst, .prize { animation: none; }
  }
</style>
