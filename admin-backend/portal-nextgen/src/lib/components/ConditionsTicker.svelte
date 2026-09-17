<script>
  // Severity-ordered conditions rail: admin-curated events + maintenance/alert
  // notices. Player-safe free-text only. Kinds: alert > maintenance > event >
  // info (rendered in that order, kind-colored left edge). Omits silently when
  // there is nothing to show so the dashboard never carries an empty rail.
  let { events = null } = $props();

  const RANK = { alert: 0, maintenance: 1, event: 2, info: 3 };
  let items = $derived.by(() => {
    const list = events?.events;
    if (!Array.isArray(list) || !list.length) return [];
    return [...list].sort((a, b) => (RANK[a.kind] ?? 9) - (RANK[b.kind] ?? 9));
  });
</script>

{#if items.length}
  <ul class="ticker" aria-label="Live conditions and notices">
    {#each items as e}
      <li class="cond cond-{e.kind}">
        <span class="cond-kind mono">{e.kind}</span>
        <div class="cond-body">
          {#if e.title}<p class="cond-title">{e.title}</p>{/if}
          {#if e.body}<p class="cond-text">{e.body}</p>{/if}
        </div>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .ticker { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); }
  .cond {
    display: flex; gap: var(--space-3); align-items: flex-start;
    border: 1px solid var(--edge); border-left: 3px solid var(--border-strong);
    border-radius: var(--radius-md); padding: var(--space-3) var(--space-4); background: var(--metal-0);
  }
  .cond-alert { border-left-color: var(--ls-red); }
  .cond-maintenance { border-left-color: var(--accent); }
  .cond-event { border-left-color: var(--ls-ibad); }
  .cond-info { border-left-color: var(--ls-info); }
  .cond-kind {
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted);
    padding-top: 2px; flex: none; min-width: 84px;
  }
  .cond-body { min-width: 0; }
  .cond-title { margin: 0; font-weight: 700; color: var(--text); font-size: var(--text-sm); }
  .cond-text { margin: 2px 0 0; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.45; }
</style>
