<script>
  // Pure-CSS durability bar. Accepts durability in any of the shapes the item read
  // might carry: a 0..1 fraction, a 0..100 percent, or a {current,max} pair. It
  // normalizes to a fraction, exposes it as an aria meter, and warms the fill red
  // under 25%. No transition under reduced motion (the width jump is instant).
  let { durability = null, size = 'bar', label = 'Durability' } = $props();

  function toFrac(d) {
    if (d == null) return null;
    if (typeof d === 'number') return d > 1 ? Math.max(0, Math.min(1, d / 100)) : Math.max(0, Math.min(1, d));
    if (typeof d === 'object') {
      const cur = Number(d.current ?? d.value ?? d.cur);
      const max = Number(d.max ?? d.maximum ?? d.total);
      if (!isFinite(cur) || !isFinite(max) || max <= 0) return null;
      return Math.max(0, Math.min(1, cur / max));
    }
    return null;
  }

  let frac = $derived(toFrac(durability));
  let pct = $derived(frac == null ? 0 : Math.round(frac * 100));
  let low = $derived(frac != null && frac < 0.25);
  let mid = $derived(frac != null && frac >= 0.25 && frac < 0.6);
</script>

{#if frac != null}
  <div
    class="meter {size}"
    class:low
    class:mid
    role="meter"
    aria-valuenow={pct}
    aria-valuemin="0"
    aria-valuemax="100"
    aria-label="{label} {pct} percent"
    title="{label}: {pct}%"
  >
    <span class="fill" style="width:{pct}%"></span>
  </div>
{/if}

<style>
  .meter {
    position: relative; width: 100%; height: 4px; border-radius: 2px;
    background: color-mix(in srgb, var(--edge) 60%, transparent);
    overflow: hidden;
  }
  .meter.ring { height: 3px; }
  .fill {
    position: absolute; inset: 0 auto 0 0; height: 100%;
    background: var(--ls-green);
    border-radius: 2px;
    transition: width var(--motion-mid) var(--ease-out);
  }
  .meter.mid .fill { background: var(--ls-yellow); }
  .meter.low .fill { background: var(--ls-red); }
  @media (prefers-reduced-motion: reduce) {
    .fill { transition: none; }
  }
</style>
