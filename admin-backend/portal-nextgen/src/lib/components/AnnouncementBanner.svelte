<script>
  import { onMount } from 'svelte';
  import LiveCountdown from './LiveCountdown.svelte';
  // Operator announcement banner (planned maintenance / notices). Driven by the
  // /portal/announcement JSON feed. Dismissible per-id (localStorage), so a new
  // announcement re-shows even after a previous one was dismissed.
  let { announcement } = $props();

  let dismissedId = $state('');
  onMount(() => { try { dismissedId = localStorage.getItem('ls-announce-dismissed') || ''; } catch (e) {} });

  let a = $derived(announcement?.active ? announcement : null);
  let show = $derived(!!a && a.id !== dismissedId);
  let now = $state(Date.now());
  onMount(() => { const t = setInterval(() => { now = Date.now(); }, 30000); return () => clearInterval(t); });

  function utc(s) { return s && s.includes('T') && !/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s + 'Z' : s; }
  let startsMs = $derived(a?.starts_utc ? new Date(utc(a.starts_utc)).getTime() : null);
  let endsMs = $derived(a?.ends_utc ? new Date(utc(a.ends_utc)).getTime() : null);
  let phase = $derived.by(() => {
    if (startsMs && now < startsMs) return 'upcoming';
    if (endsMs && now < endsMs) return 'active';
    if (endsMs && now >= endsMs) return 'past';
    return 'open';
  });
  const LABEL = { info: 'Notice', maintenance: 'Planned maintenance', alert: 'Alert' };

  function dismiss() {
    dismissedId = a.id;
    try { localStorage.setItem('ls-announce-dismissed', a.id); } catch (e) {}
  }
</script>

{#if show && phase !== 'past'}
  <aside class="ann ann-{a.type}" role="status">
    <span class="ann-bar" aria-hidden="true"></span>
    <div class="ann-body">
      <p class="ann-head mono">
        <span class="ann-tag">{LABEL[a.type] ?? 'Notice'}</span>
        {#if a.title}<span class="ann-title">{a.title}</span>{/if}
      </p>
      {#if a.body}<p class="ann-text">{a.body}</p>{/if}
    </div>
    <div class="ann-when mono">
      {#if phase === 'upcoming'}starts in <LiveCountdown target={a.starts_utc} />
      {:else if phase === 'active'}<span class="now">in progress</span>{#if endsMs} | ends in <LiveCountdown target={a.ends_utc} />{/if}
      {/if}
    </div>
    {#if a.link}<a class="ann-link mono" href={a.link}>Details</a>{/if}
    <button class="ann-x" onclick={dismiss} aria-label="Dismiss">&times;</button>
  </aside>
{/if}

<style>
  .ann {
    position: relative; display: flex; align-items: center; gap: var(--space-3);
    margin: 0 0 var(--space-5); padding: var(--space-3) var(--space-4) var(--space-3) var(--space-5);
    background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-md);
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 10px 28px -22px #000; overflow: hidden;
  }
  .ann-bar { position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--accent); box-shadow: 0 0 12px var(--accent-glow); }
  .ann-info .ann-bar { background: var(--ls-info); box-shadow: 0 0 12px rgba(90,169,214,.4); }
  .ann-maintenance .ann-bar { background: var(--accent); }
  .ann-alert .ann-bar { background: var(--ls-red); box-shadow: 0 0 12px rgba(214,90,68,.45); }
  .ann-alert { border-color: color-mix(in srgb, var(--ls-red) 40%, var(--edge)); }

  .ann-body { flex: 1; min-width: 0; }
  .ann-head { margin: 0; display: flex; align-items: baseline; gap: var(--space-3); flex-wrap: wrap; }
  .ann-tag { font-size: var(--text-xs); letter-spacing: .22em; text-transform: uppercase; color: var(--accent); }
  .ann-alert .ann-tag { color: var(--ls-red); }
  .ann-info .ann-tag { color: var(--ls-info); }
  .ann-title { font-family: var(--font-display); font-weight: 700; letter-spacing: .03em; font-size: var(--text-lg); color: var(--text); text-transform: none; }
  .ann-text { margin: 2px 0 0; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.4; }
  .ann-when { font-size: var(--text-xs); letter-spacing: .12em; color: var(--text-muted); white-space: nowrap; display: flex; align-items: baseline; gap: var(--space-2); }
  .ann-when .now { color: var(--accent-bright); text-transform: uppercase; }
  .ann-link { font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; color: var(--accent-text); text-decoration: none; border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-3); }
  .ann-link:hover { border-color: var(--accent); }
  .ann-x { background: transparent; border: 0; color: var(--text-muted); font-size: 22px; line-height: 1; cursor: pointer; padding: 0 var(--space-1); }
  .ann-x:hover { color: var(--text); }
  @media (max-width: 720px) {
    .ann { flex-wrap: wrap; }
    .ann-when { width: 100%; }
  }
</style>
