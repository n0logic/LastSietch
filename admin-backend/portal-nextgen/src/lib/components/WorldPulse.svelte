<script>
  // 24h world-pulse instrument: peak concurrency, play-hours, and the world
  // counters (subfiefs/structures/vehicles) when telemetry has them, over a
  // dependency-free inline-SVG presence sparkline. Counts only, no PII. Any
  // null stat is hidden rather than shown as a dash.
  let { world = null } = $props();

  // Numeric stats, in display order; nulls are dropped so the row only shows
  // what the feed actually has (counters can be null when world telemetry is down).
  let stats = $derived.by(() => {
    if (!world) return [];
    const fmtH = (h) => (h >= 100 ? Math.round(h) : h.toFixed(1));
    return [
      world.peak != null ? { k: 'peak online', v: world.peak } : null,
      world.play_hours != null ? { k: 'play-hours', v: fmtH(world.play_hours) } : null,
      world.subfiefs != null ? { k: 'subfiefs', v: world.subfiefs } : null,
      world.structures != null ? { k: 'structures', v: world.structures } : null,
      world.vehicles != null ? { k: 'vehicles', v: world.vehicles } : null,
    ].filter(Boolean);
  });

  // Sparkline geometry from series:[{ts,count}]. Viewbox 0..100 x 0..28; the
  // path is a normalized polyline (min..max scaled to the box height).
  const W = 100, H = 28, PAD = 2;
  let spark = $derived.by(() => {
    const s = world?.series;
    if (!Array.isArray(s) || s.length < 2) return null;
    const counts = s.map((p) => Number(p.count) || 0);
    const lo = Math.min(...counts), hi = Math.max(...counts);
    const span = hi - lo || 1;
    const n = counts.length;
    const pts = counts.map((c, i) => {
      const x = PAD + (i / (n - 1)) * (W - 2 * PAD);
      const y = (H - PAD) - ((c - lo) / span) * (H - 2 * PAD);
      return [Math.round(x * 100) / 100, Math.round(y * 100) / 100];
    });
    const line = pts.map((p) => p.join(',')).join(' ');
    const area = `${PAD},${H - PAD} ${line} ${W - PAD},${H - PAD}`;
    return { line, area, last: pts[pts.length - 1], hi, lo };
  });
</script>

<div class="pulse">
  {#if stats.length}
    <div class="stats">
      {#each stats as s}
        <div class="stat">
          <span class="v mono">{s.v}</span>
          <span class="k mono">{s.k}</span>
        </div>
      {/each}
    </div>
  {:else}
    <p class="skeleton mono">reading the world…</p>
  {/if}

  {#if spark}
    <svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">
      <polyline class="spark-area" points={spark.area} />
      <polyline class="spark-line" points={spark.line} />
      <circle class="spark-head" cx={spark.last[0]} cy={spark.last[1]} r="1.4" />
    </svg>
    <p class="spark-cap mono">online, last 24h · peak {spark.hi}</p>
  {/if}
</div>

<style>
  .pulse { display: flex; flex-direction: column; gap: var(--space-3); }
  .stats { display: flex; flex-wrap: wrap; gap: var(--space-4) var(--space-5); }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .v {
    font-family: var(--font-display); font-weight: 700; font-size: clamp(1.4rem, 2.4vw, 1.8rem);
    line-height: 1; color: var(--text); font-variant-numeric: tabular-nums;
  }
  .k { font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); }
  .spark { width: 100%; height: 40px; display: block; overflow: visible; }
  .spark-line { fill: none; stroke: var(--ls-ibad); stroke-width: 1.1; vector-effect: non-scaling-stroke; }
  .spark-area { fill: color-mix(in srgb, var(--ls-ibad) 12%, transparent); stroke: none; }
  .spark-head { fill: var(--ls-ibad); filter: drop-shadow(0 0 3px var(--ls-ibad-glow)); }
  .spark-cap { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .08em; margin: 0; opacity: .8; }
  .skeleton { color: var(--text-muted); opacity: .6; }
</style>
