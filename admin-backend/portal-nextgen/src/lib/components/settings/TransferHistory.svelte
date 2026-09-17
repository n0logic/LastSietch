<script>
  // Item transfers, both directions, for every account on this session. The
  // activity log next door merges transfers in among claims and portal writes;
  // this is the dedicated list, so a player chasing "did that Perforator ever
  // arrive" is not reading past twenty unrelated rows to find out.
  //
  // The rows come off the admin.db mirror, which is written only when the writer
  // answers applied or replay. A refusal is not a transfer and never appears
  // here; a timeout is never settled as failed. `counterparty` is a character
  // name the server already resolved, so nothing here looks up an id.
  import { onMount } from 'svelte';
  import { storage, loadTransfers } from '$lib/storage.svelte.js';
  import { gradeLabel } from '$lib/components/karum/grade.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  const STATUS_TEXT = {
    applied: 'delivered',
    replay: 'delivered',
    pending: 'in flight',
    deferred: 'paused',
    refused: 'refused',
    failed: 'did not complete',
  };

  function when(t) {
    const d = new Date(t);
    return Number.isNaN(d.getTime()) ? String(t ?? '') : d.toLocaleString();
  }

  // Grade 0 is Base, a real grade: the test is against null, never falsiness.
  function itemText(row) {
    const grade = row?.grade == null ? null : gradeLabel(row.grade);
    return grade ? `${row.item} ${grade}` : String(row?.item ?? '');
  }

  onMount(loadTransfers);
</script>

{#if storage.transfersStatus === 'ready'}
  <CarvedSlab sharp={true}>
    <p class="kicker mono">Item transfers | in and out</p>
    <ul class="log">
      {#each storage.transfers as row, i (`${row.t}-${i}`)}
        <li class="entry">
          <span class="t mono">{when(row.t)}</span>
          <span class="dir mono" data-dir={row.direction}>{row.direction === 'received' ? 'In' : 'Out'}</span>
          <span class="what">
            {itemText(row)}
            <span class="who">{row.direction === 'received' ? 'from' : 'to'} {row.counterparty}</span>
          </span>
          <span class="state mono">{STATUS_TEXT[row.status] || row.status}</span>
        </li>
      {/each}
    </ul>
  </CarvedSlab>
{:else}
  <SealedPanel
    slab={true}
    status={storage.transfersStatus === 'idle' ? 'loading' : storage.transfersStatus}
    loadingText="reading your transfers"
    action="none"
    art="no-orders"
    emptyText="No items have moved between you and another player yet. Sends and arrivals both show up here."
    errorText="Your transfer history could not be read right now. Try again shortly."
  />
{/if}

<style>
  .log { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-direction: column; }
  .entry {
    display: grid; grid-template-columns: 11rem 3rem minmax(0, 1fr) 8rem;
    gap: var(--space-3); align-items: baseline;
    padding: var(--space-2) 0; border-top: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  .entry:first-child { border-top: 0; }
  .t { color: var(--text-muted); font-size: var(--text-xs); }
  .dir {
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em;
    color: var(--text-muted);
  }
  .dir[data-dir='received'] { color: var(--ls-green); }
  .what { color: var(--text); line-height: 1.45; }
  .who { display: block; font-size: var(--text-xs); color: var(--text-muted); }
  .state {
    font-size: var(--text-xs); color: var(--accent-text);
    text-transform: uppercase; letter-spacing: .1em;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  @media (max-width: 640px) {
    .entry { grid-template-columns: 1fr; gap: var(--space-1); }
  }
</style>
