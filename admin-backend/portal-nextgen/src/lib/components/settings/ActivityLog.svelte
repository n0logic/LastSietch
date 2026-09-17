<script>
  // The player's own activity: the audit rows already written for their linked
  // accounts, read back through one session-scoped endpoint. Read-only, and
  // never fabricated -- an empty log stays empty rather than growing a
  // placeholder row, and a failed read says so instead of showing nothing.
  //
  // Wave 7 added an item-transfer source to the merge. Those rows carry the four
  // facts a summary line cannot hold on its own (item, grade, counterparty,
  // direction), so they render an extra detail line. Every other source keeps
  // exactly the {t, kind, summary} shape it had, and a transfer row missing its
  // facts falls back to the summary rather than printing an empty detail.
  import { onMount } from 'svelte';
  import { api } from '$lib/api.js';
  import { gradeLabel } from '$lib/components/karum/grade.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  let status = $state('loading'); // 'loading' | 'error' | 'empty' | 'ready'
  let items = $state([]);

  function when(t) {
    const d = new Date(t);
    return Number.isNaN(d.getTime()) ? String(t ?? '') : d.toLocaleString();
  }

  /** "Sent Sandcrawler Part G3 to Stilgar", or null for a row that is not a
   *  transfer. Grade 0 is Base, a real grade, so the test is against null and
   *  never against falsiness. */
  function transferLine(row) {
    if (!row?.item || !row?.direction) return null;
    const received = row.direction === 'received';
    const grade = row.grade == null ? '' : ` ${gradeLabel(row.grade) || ''}`.trimEnd();
    const who = row.counterparty || 'another player';
    return `${received ? 'Received' : 'Sent'} ${row.item}${grade} ${received ? 'from' : 'to'} ${who}`;
  }

  async function load() {
    status = 'loading';
    try {
      const r = await api.settings.activity();
      items = Array.isArray(r?.items) ? r.items : [];
      status = items.length ? 'ready' : 'empty';
    } catch (e) {
      items = [];
      status = 'error';
    }
  }

  onMount(load);
</script>

{#if status === 'ready'}
  <CarvedSlab sharp={true}>
    <p class="kicker mono">Your activity | the ledger</p>
    <ul class="log">
      {#each items as row, i (`${row.t}-${i}`)}
        <li class="entry">
          <span class="t mono">{when(row.t)}</span>
          <span class="kind mono">{row.kind}</span>
          <span class="summary">
            {row.summary}
            {#if transferLine(row)}
              <span class="detail">{transferLine(row)}</span>
            {/if}
          </span>
        </li>
      {/each}
    </ul>
  </CarvedSlab>
{:else}
  <SealedPanel
    slab={true}
    {status}
    loadingText="reading your ledger"
    action="none"
    emptyText="Nothing is recorded against your accounts yet. Claims, transfers and portal writes show up here."
    errorText="Your activity could not be read right now. Try again shortly."
  />
{/if}

<style>
  .log { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-direction: column; }
  .entry {
    display: grid; grid-template-columns: 11rem 8rem minmax(0, 1fr);
    gap: var(--space-3); align-items: baseline;
    padding: var(--space-2) 0; border-top: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  .entry:first-child { border-top: 0; }
  .t { color: var(--text-muted); font-size: var(--text-xs); }
  .kind {
    color: var(--accent-text); font-size: var(--text-xs);
    text-transform: uppercase; letter-spacing: .1em;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .summary { color: var(--text); line-height: 1.45; }
  .detail { display: block; font-size: var(--text-xs); color: var(--text-muted); }

  @media (max-width: 640px) {
    .entry { grid-template-columns: 1fr; gap: var(--space-1); }
  }
</style>
