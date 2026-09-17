<script>
  // Notice: the one write-result / status line. Distilled from the route-level
  // `.write-notice` block (bases, exchange, karum, rewards, storage all carry a
  // byte-identical copy) and the 24 component `role="status"` lines that carry a
  // `data-phase`.
  //
  // a11y: an error is the only tone that interrupts, so it renders `role="alert"`;
  // every other tone is a polite `role="status"`. The tone to colour map is the
  // shipped one, amber chrome only (Ibad blue is live data, never a status line).
  //
  // `phase` is echoed as `data-phase` so a component still carrying its own
  // per-phase CSS keeps rendering correctly while it migrates. No dismissal and
  // no timers: AnnouncementBanner owns its own countdown and stays a consumer.
  let { tone = 'info', text = '', phase = null, live = true, children } = $props();
</script>

{#if tone === 'error'}
  <p class="notice" role="alert" data-tone="error" data-phase={phase}>
    {#if text}{text}{:else}{@render children?.()}{/if}
  </p>
{:else}
  <p class="notice" role="status" aria-live={live ? 'polite' : 'off'} data-tone={tone} data-phase={phase}>
    {#if text}{text}{:else}{@render children?.()}{/if}
  </p>
{/if}

<style>
  .notice { margin: var(--space-3) 0 0; font-size: var(--text-sm); line-height: 1.45; }
  .notice[data-tone='ok'] { color: var(--ls-green); }
  .notice[data-tone='warn'] { color: var(--accent-text); }
  .notice[data-tone='error'] { color: var(--ls-red); }
  .notice[data-tone='info'] { color: var(--text-muted); }
  .notice[data-tone='pending'] { color: var(--text-muted); }
</style>
