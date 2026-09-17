<script>
  // Amber "recruiting" beacon pill. This is a CALL, not live telemetry, so it
  // wears amber (accent) and never Ibad blue. Purely presentational.
  let { label = 'Recruiting' } = $props();
</script>

<span class="beacon mono" aria-label={label}>
  <span class="pulse" aria-hidden="true"></span>{label}
</span>

<style>
  .beacon {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent-text);
    padding: var(--space-1) var(--space-3);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--edge));
    border-radius: var(--radius-sm);
    white-space: nowrap;
  }
  .pulse {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 0 var(--accent-glow);
    animation: beacon-pulse 2.2s var(--ease-in-out) infinite;
  }
  @keyframes beacon-pulse {
    0% { box-shadow: 0 0 0 0 var(--accent-glow); }
    70% { box-shadow: 0 0 0 6px transparent; }
    100% { box-shadow: 0 0 0 0 transparent; }
  }
  @media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }
</style>
