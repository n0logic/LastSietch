<script>
  // Self-hosted inline-SVG price spark. Takes history points ({t, min_price, ...})
  // and draws the min-price vein. No chart lib (CSP + JS budget). The whole capture
  // table only starts accruing at launch, so the common state right now is EMPTY:
  // we render an honest "Calibrating" placeholder instead of a flat fake line.
  //
  // Amber is the resting stroke (chrome). The vein only warms Ibad-blue when the
  // latest point is a genuine live low (a value moving right now), never otherwise.
  let { points = [], width = 96, height = 26, live = false, label = 'Price history' } = $props();

  let vals = $derived(
    (Array.isArray(points) ? points : [])
      .map((p) => Number(p?.min_price ?? p?.median_price))
      .filter((v) => Number.isFinite(v) && v > 0)
  );
  let calibrating = $derived(vals.length < 2);

  // Map the series into the box (1px inset so the stroke never clips).
  let path = $derived.by(() => {
    if (vals.length < 2) return '';
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || 1;
    const w = width - 2, h = height - 2;
    const step = w / (vals.length - 1);
    return vals
      .map((v, i) => {
        const x = 1 + i * step;
        const y = 1 + h - ((v - min) / span) * h; // invert: higher price = higher
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  });
  // The final point, for the terminal dot (Ibad only when live).
  let lastPt = $derived.by(() => {
    if (vals.length < 2) return null;
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = max - min || 1;
    const w = width - 2, h = height - 2;
    const x = 1 + (vals.length - 1) * (w / (vals.length - 1));
    const y = 1 + h - ((vals[vals.length - 1] - min) / span) * h;
    return { x, y };
  });
</script>

{#if calibrating}
  <span class="calibrating mono" title="Calibrating: gathering price data">Calibrating</span>
{:else}
  <svg
    class="spark" class:live viewBox="0 0 {width} {height}"
    width={width} height={height} role="img" aria-label={label} preserveAspectRatio="none"
  >
    <path class="vein" d={path} />
    {#if lastPt}<circle class="tip" cx={lastPt.x} cy={lastPt.y} r="1.6" />{/if}
  </svg>
{/if}

<style>
  .calibrating {
    display: inline-flex; align-items: center;
    font-size: var(--text-xs); letter-spacing: .06em;
    color: color-mix(in srgb, var(--text-muted) 80%, transparent);
    opacity: .8;
  }
  .spark { display: block; overflow: visible; }
  /* Amber vein = resting chrome. */
  .vein {
    fill: none; stroke: var(--accent-soft); stroke-width: 1.4;
    stroke-linejoin: round; stroke-linecap: round;
  }
  .tip { fill: var(--accent); }
  /* Live low: the vein + tip earn Ibad only when the caller confirms it is moving. */
  .spark.live .vein { stroke: var(--ls-ibad); }
  .spark.live .tip { fill: var(--ls-ibad); filter: drop-shadow(0 0 3px var(--ls-ibad-glow)); }
</style>
