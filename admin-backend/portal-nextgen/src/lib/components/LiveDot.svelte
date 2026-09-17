<script>
  // Breathing Ibad-blue dot marking a genuinely live/streaming value. `tone`
  // switches to a status hue (danger/warn) when conditions demand it.
  let { tone = 'live' } = $props();
</script>

<span class="dot tone-{tone}" aria-hidden="true"></span>

<style>
  .dot {
    display: inline-block; width: 9px; height: 9px; border-radius: 50%;
    background: var(--ls-ibad); box-shadow: 0 0 0 0 var(--ls-ibad-glow);
    animation: breathe 2s var(--ease-in-out) infinite;
  }
  .tone-danger { background: var(--ls-red); box-shadow: 0 0 0 0 rgba(214, 90, 68, .4); }
  .tone-warn { background: var(--ls-yellow); }
  .tone-idle { background: var(--text-muted); animation: none; }
  @keyframes breathe {
    0% { box-shadow: 0 0 0 0 var(--ls-ibad-glow); }
    70% { box-shadow: 0 0 0 7px transparent; }
    100% { box-shadow: 0 0 0 0 transparent; }
  }
  @media (prefers-reduced-motion: reduce) { .dot { animation: none; } }
</style>
