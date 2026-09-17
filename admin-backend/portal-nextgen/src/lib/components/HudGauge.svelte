<script>
  // Pure SVG arc gauge (no chart lib). Amber chrome; `live` switches the fill to
  // Ibad blue for streaming values. Renders instantly, no JS animation loop.
  let { value = 0, max = 100, label = '', unit = '', display = null, size = 96, live = false } = $props();

  const R = 42;
  const C = 2 * Math.PI * R;
  let frac = $derived(max > 0 ? Math.max(0, Math.min(1, value / max)) : 0);
  let dash = $derived((C * frac).toFixed(2));
  let shown = $derived(display != null ? display : `${value}${unit}`);
</script>

<figure class="gauge" style="--sz:{size}px">
  <svg viewBox="0 0 100 100" width={size} height={size} aria-hidden="true">
    <circle class="track" cx="50" cy="50" r={R} fill="none" />
    <circle
      class="fill"
      class:live
      cx="50" cy="50" r={R} fill="none"
      stroke-dasharray="{dash} {C}"
      stroke-linecap="round"
      transform="rotate(-90 50 50)"
    />
  </svg>
  <figcaption>
    <span class="val mono">{shown}</span>
    {#if label}<span class="lbl">{label}</span>{/if}
  </figcaption>
</figure>

<style>
  .gauge { position: relative; width: var(--sz); margin: 0; display: inline-grid; place-items: center; }
  svg { display: block; }
  .track { stroke: var(--border); stroke-width: 7; }
  .fill { stroke: var(--accent-bright); stroke-width: 7; filter: drop-shadow(0 0 4px var(--accent-glow)); }
  .fill.live { stroke: var(--ls-ibad); filter: drop-shadow(0 0 5px var(--ls-ibad-glow)); }
  figcaption {
    position: absolute; inset: 0; display: grid; place-content: center; text-align: center; gap: 2px;
  }
  .val { font-size: var(--text-lg); font-weight: 600; color: var(--text); }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
</style>
