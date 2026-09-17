<script>
  // Three sharp carved HUD slabs across the Exchange header:
  //   1. Solari on account. AMBER at rest (owned currency, not live). The ONE
  //      exception in this whole module: while a buy is settling the number DRAINS
  //      to the post-buy total in Ibad-blue (a value moving right now), then settles
  //      back to amber. Reduced motion snaps straight to the total, no Ibad.
  //   2. Watches armed (how many price watches you have SET) with the fired count
  //      (how many have actually TRIPPED and are unseen) called out separately.
  //      These were one conflated number labelled "Alerts armed"; they are not the
  //      same thing and the label was wrong for both. Amber chrome.
  //   3. Active listings (my open orders). Amber chrome.
  import { untrack } from 'svelte';
  import { exchange } from '$lib/exchange.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';

  let target = $derived(Number(exchange.bank.solari) || 0);
  let shown = $state(Number(exchange.bank.solari) || 0);
  let draining = $state(false);
  let raf = 0;

  function reduced() {
    return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // Tween the displayed balance toward the true balance. Ibad is lit only while the
  // number is falling (a live spend); a rise (refund/reconcile up) settles amber.
  $effect(() => {
    const t = target;
    const from = untrack(() => shown);
    if (from === t) return;
    if (reduced()) { shown = t; draining = false; return; }
    draining = t < from;
    const start = performance.now();
    const dur = 640;
    cancelAnimationFrame(raf);
    const step = (nowT) => {
      const p = Math.min(1, (nowT - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      shown = Math.round(from + (t - from) * eased);
      if (p < 1) raf = requestAnimationFrame(step);
      else { shown = t; draining = false; }
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  });

  let armed = $derived(exchange.armedCount || 0);
  let fired = $derived(exchange.firedCount || 0);
  let activeOrders = $derived(exchange.orders.active.length || 0);
</script>

<div class="statbar">
  <CarvedSlab sharp={true} elevation={2}>
    <div class="stat solari" class:draining>
      <p class="kicker mono">Solari on account</p>
      <div class="figure">
        <img class="crest" src="/admin/static/img/dune-icons/T_UI_IconResourceSolarisCoin_D.png" alt="" aria-hidden="true" />
        <span class="amt mono" class:drain-lit={draining}>{shown.toLocaleString()}</span>
      </div>
      <div class="gauge" aria-hidden="true"><span class="gfill"></span></div>
    </div>
  </CarvedSlab>

  <CarvedSlab sharp={true} elevation={2}>
    <div class="stat">
      <p class="kicker mono">Watches armed</p>
      <div class="figure">
        <span class="glyph" aria-hidden="true">&#9650;</span>
        <span class="amt mono">{armed.toLocaleString()}</span>
      </div>
      {#if fired > 0}
        <p class="sub mono fired">{fired.toLocaleString()} fired</p>
      {:else}
        <p class="sub mono">none fired</p>
      {/if}
    </div>
  </CarvedSlab>

  <CarvedSlab sharp={true} elevation={2}>
    <div class="stat">
      <p class="kicker mono">Active listings</p>
      <div class="figure">
        <span class="glyph" aria-hidden="true">&#9632;</span>
        <span class="amt mono">{activeOrders.toLocaleString()}</span>
      </div>
      <p class="sub mono">on the Exchange</p>
    </div>
  </CarvedSlab>
</div>

<style>
  .statbar {
    display: grid; gap: var(--space-3);
    grid-template-columns: 1.3fr 1fr 1fr;
  }
  .stat { display: flex; flex-direction: column; gap: var(--space-2); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .2em; font-size: var(--text-xs); }
  .figure { display: flex; align-items: center; gap: var(--space-2); }
  .crest { width: 26px; height: 26px; object-fit: contain; opacity: .9; filter: drop-shadow(0 0 4px var(--accent-glow)); }
  .glyph { color: var(--accent-soft); font-size: var(--text-sm); }
  /* AMBER at rest (owned balance is not a live-streaming value). */
  .amt { font-size: var(--text-2xl); font-weight: 700; color: var(--accent-text); font-variant-numeric: tabular-nums; letter-spacing: .01em; }
  .sub { margin: 0; font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  /* A fired alert is a thing that actually happened, so it reads as live. */
  .sub.fired { color: var(--ls-ibad); }

  /* The drain cue: only during a settling buy does the number + gauge go Ibad. */
  .amt.drain-lit { color: var(--ls-ibad); text-shadow: 0 0 10px var(--ls-ibad-glow); }
  .gauge {
    position: relative; height: 3px; border-radius: 2px; overflow: hidden;
    background: color-mix(in srgb, var(--edge) 60%, transparent);
  }
  .gfill {
    position: absolute; inset: 0; border-radius: 2px;
    background: linear-gradient(90deg, var(--accent-soft), var(--accent));
    transition: background var(--motion-fast) var(--ease-out);
  }
  .solari.draining .gfill {
    background: linear-gradient(90deg, var(--ls-ibad), color-mix(in srgb, var(--ls-ibad) 20%, transparent));
    animation: sweep .64s var(--ease-out);
  }
  @keyframes sweep { from { transform: translateX(0); } to { transform: translateX(-14%); } }

  @media (max-width: 720px) { .statbar { grid-template-columns: 1fr; } }
  @media (prefers-reduced-motion: reduce) {
    .solari.draining .gfill { animation: none; }
  }
</style>
