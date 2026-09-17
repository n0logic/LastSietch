<script>
  // Ticking countdown to an absolute ISO instant. Ibad blue (live data). Ticks at
  // minute resolution (cheap, battery-friendly); the target is absolute so a stale
  // cache never skews it.
  let { target = null, label = '', prefix = '' } = $props();

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 30000);
    return () => clearInterval(id);
  });

  function fmt(ms) {
    if (ms <= 0) return null;
    let s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60);
    const parts = [];
    if (d) parts.push(d + 'd');
    if (d || h) parts.push(h + 'h');
    parts.push(m + 'm');
    return parts.join(' ');
  }

  // Treat a timezone-less ISO timestamp (e.g. "2026-06-30T04:55:00") as UTC, so a
  // server _utc value without a trailing Z is not parsed as the viewer's local time.
  function asUtc(t) {
    if (typeof t === 'string' && t.includes('T') && !/[zZ]|[+-]\d\d:?\d\d$/.test(t)) return t + 'Z';
    return t;
  }
  let ms = $derived(target ? new Date(asUtc(target)).getTime() - now : 0);
  let txt = $derived(fmt(ms));
</script>

<span class="countdown mono">
  {#if prefix}<span class="cd-prefix">{prefix}</span>{/if}
  <strong>{txt ?? 'now'}</strong>
  {#if label}<span class="cd-label">{label}</span>{/if}
</span>

<style>
  .countdown { display: inline-flex; align-items: baseline; gap: var(--space-2); }
  .cd-prefix, .cd-label { color: var(--text-muted); font-size: var(--text-sm); }
  strong { color: var(--ls-ibad); font-weight: 700; letter-spacing: .02em; }
</style>
