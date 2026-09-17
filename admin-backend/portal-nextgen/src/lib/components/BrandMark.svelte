<script>
  // Last Sietch sigil: the nested downward-triangle glyph from the brand logo
  // (lastsietch.com/assets/icon.png) over concentric scan-pulse rings. Crisp at
  // any size, accent-driven (recolors with the active house), glows amber.
  let { size = 30 } = $props();
</script>

<span class="mark" style="--mk:{size}px" aria-hidden="true">
  <svg viewBox="0 0 32 32" width={size} height={size} fill="none">
    <defs>
      <linearGradient id="lsCore" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="var(--accent-bright)" />
        <stop offset="1" stop-color="var(--ls-red)" />
      </linearGradient>
    </defs>
    <!-- scan-pulse rings -->
    <g class="rings" stroke="var(--accent)" stroke-width="1" opacity=".5">
      <circle class="r-out" cx="16" cy="15" r="8.6" />
      <circle cx="16" cy="15" r="6.1" />
    </g>
    <!-- nested triangle, point-down -->
    <path d="M7 8.3 H25 L16 22.3 Z" stroke="var(--accent)" stroke-width="1.7"
          stroke-linejoin="round" />
    <path class="core" d="M12.3 11.5 H19.7 L16 17.3 Z" fill="url(#lsCore)" />
  </svg>
</span>

<style>
  .mark {
    display: inline-grid; place-items: center;
    width: var(--mk); height: var(--mk);
    filter: drop-shadow(0 0 5px var(--accent-glow));
  }
  .mark svg { display: block; }
  .core { filter: drop-shadow(0 0 3px var(--accent-glow)); }
  /* Slow sensor sweep on the outer ring; off under reduced-motion. */
  .r-out { transform-origin: 16px 15px; animation: scan 4.5s var(--ease-in-out) infinite; }
  @keyframes scan {
    0%, 100% { transform: scale(.82); opacity: .15; }
    50%      { transform: scale(1.08); opacity: .6; }
  }
  @media (prefers-reduced-motion: reduce) {
    .r-out { animation: none; }
  }
</style>
