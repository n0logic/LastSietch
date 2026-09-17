<script>
  // Streak flame: a big mono streak number (amber) beside a HudGauge arc that fills
  // toward the next milestone (center shows "current/milestone"), with a "best: N"
  // readout. The number breathes an amber glow (~2.4s), gated off under reduced
  // motion. Plain DOM/CSS + the shared SVG gauge (no chart lib).
  import HudGauge from '$lib/components/HudGauge.svelte';

  // `current` is the TOTAL consecutive streak (the big number). `cycle` is the
  // position in the repeating 7-day ramp cycle and drives the gauge toward the next
  // milestone; it resets to 1 after each completed cycle while the total keeps
  // climbing. Falls back to `current` when the backend does not supply a cycle.
  let { current = 0, cycle = null, best = 0, nextMilestone = 7 } = $props();

  let gaugeMax = $derived(nextMilestone > 0 ? nextMilestone : 7);
  let gaugeSrc = $derived(cycle == null ? current : cycle);
  let gaugeVal = $derived(Math.max(0, Math.min(gaugeSrc, gaugeMax)));
</script>

<div class="flame">
  <div class="count">
    <span class="num mono">{current}</span>
    <span class="lbl">day{current === 1 ? '' : 's'} in a row</span>
  </div>

  <HudGauge
    value={gaugeVal}
    max={gaugeMax}
    display={`${gaugeVal}/${gaugeMax}`}
    label="to reward"
    size={108}
  />

  <p class="best mono">best: {best}</p>
</div>

<style>
  .flame {
    display: flex; align-items: center; gap: var(--space-5); flex-wrap: wrap;
  }
  .count { display: flex; flex-direction: column; }
  .num {
    font-size: clamp(2.6rem, 8vw, 3.8rem); font-weight: 700; line-height: 1;
    color: var(--accent-text);
    text-shadow: 0 0 14px var(--accent-glow);
    animation: streak-breathe 2.4s var(--ease-in-out) infinite;
  }
  .lbl {
    font-size: var(--text-xs); color: var(--text-muted);
    text-transform: uppercase; letter-spacing: .18em;
  }
  .best {
    margin: 0; margin-left: auto;
    font-size: var(--text-sm); color: var(--text-muted);
    letter-spacing: .04em;
  }
  @keyframes streak-breathe {
    0%, 100% { text-shadow: 0 0 10px color-mix(in srgb, var(--accent) 30%, transparent); }
    50% { text-shadow: 0 0 22px var(--accent-glow); }
  }
  @media (prefers-reduced-motion: reduce) {
    .num { animation: none; text-shadow: 0 0 12px var(--accent-glow); }
  }
</style>
