<script>
  // The flip spread: how far the cheapest player ask sits below the bot's buy cap.
  // A wider amber bar = a fatter margin. This is a static, verifiable derived value
  // (min_ask vs bot_cap from the same snapshot), so it stays AMBER chrome, never
  // Ibad. The numerals are tabular; the bar is never the only signal (spread text
  // shows alongside it).
  let { minAsk = 0, botCap = 0 } = $props();

  let spread = $derived(Math.max(0, (Number(botCap) || 0) - (Number(minAsk) || 0)));
  // Fill fraction of the spread against the cap (how much of the cap is headroom).
  let frac = $derived(botCap > 0 ? Math.max(0, Math.min(1, spread / botCap)) : 0);
  let pct = $derived(Math.round(frac * 100));
</script>

<div class="spread" title="Buy at {Number(minAsk).toLocaleString()}, bot pays up to {Number(botCap).toLocaleString()}">
  <div class="track" role="meter" aria-valuenow={pct} aria-valuemin="0" aria-valuemax="100" aria-label="Flip margin {pct} percent of cap">
    <span class="fill" style="width:{Math.max(4, pct)}%"></span>
  </div>
  <span class="val mono">+{spread.toLocaleString()}</span>
</div>

<style>
  .spread { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
  .track {
    position: relative; flex: 1; height: 5px; border-radius: 3px;
    background: color-mix(in srgb, var(--edge) 60%, transparent); overflow: hidden;
    min-width: 40px;
  }
  .fill {
    position: absolute; inset: 0 auto 0 0; height: 100%; border-radius: 3px;
    background: linear-gradient(90deg, var(--accent-soft), var(--accent));
    box-shadow: 0 0 6px var(--accent-glow);
    transition: width var(--motion-mid) var(--ease-out);
  }
  .val {
    font-size: var(--text-xs); font-variant-numeric: tabular-nums;
    color: var(--accent-text); white-space: nowrap;
  }
  @media (prefers-reduced-motion: reduce) { .fill { transition: none; } }
</style>
