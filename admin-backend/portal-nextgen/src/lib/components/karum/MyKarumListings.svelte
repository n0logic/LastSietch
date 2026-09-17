<script>
  // Your own Karum ledger. Every transient state keeps explicit copy because a row with
  // goods or Solari in flight must never look idle or failed.
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import { karum, cancelListing, cancelRequest, uuidv4 } from '$lib/karum.svelte.js';
  import { tierGradeLabel, wantedGradeLabel } from './grade.js';

  let view = $state('listings');
  let mine = $derived(karum.mine || []);
  let bought = $derived(karum.purchases || []);
  let requests = $derived(karum.myRequests || []);
  let fills = $derived(karum.fills || []);
  let liveMine = $derived(mine.filter((row) => !['sold', 'cancelled', 'failed'].includes(row.status)));
  let liveRequests = $derived(requests.filter((row) => !['filled', 'cancelled', 'failed'].includes(row.status)));
  let history = $derived.by(() => [
    ...mine.filter((row) => ['sold', 'cancelled', 'failed'].includes(row.status))
      .map((row) => ({ key: `sell-${row.listing_id}`, direction: 'sold', row })),
    ...bought.map((row) => ({ key: `bought-${row.listing_id}`, direction: 'bought', row })),
    ...fills.filter((row) => row.status === 'filled')
      .map((row) => ({ key: `filled-${row.request_id}`, direction: 'filled', row })),
    ...requests.filter((row) => ['filled', 'cancelled', 'failed'].includes(row.status))
      .map((row) => ({ key: `request-${row.request_id}`, direction: 'requested', row })),
  ]);

  const keys = new Map();
  let busyId = $state(null);
  let busyRequestId = $state(null);
  let notice = $state('');
  let noticeTone = $state('info');

  const STATE_COPY = {
    pending: 'being listed',
    active: 'for sale',
    selling: 'a buyer is paying now',
    filling: 'a filler is settling now',
    reconciling: 'confirming a trade, nothing lost',
    sold: 'sold',
    filled: 'filled',
    returning: 'coming back to you',
    cancelled: 'cancelled',
    paid_undelivered: 'paid, delivery in progress',
    failed: 'did not complete, nothing moved',
  };
  const PULLABLE = new Set(['active']);

  async function pull(row) {
    if (busyId != null) return;
    busyId = row.listing_id;
    notice = '';
    if (!keys.has(row.listing_id)) keys.set(row.listing_id, uuidv4());
    const result = await cancelListing({ listingId: row.listing_id, uuid: keys.get(row.listing_id) });
    if (result.ok) {
      keys.delete(row.listing_id);
      notice = `Pulled. Collect it from the Completed tab at ${result.collectAt} (it shows as CANCELED).`;
      noticeTone = 'ok';
    } else if (result.deferred) {
      notice = result.message;
      noticeTone = 'info';
    } else {
      notice = result.message;
      noticeTone = 'warn';
    }
    busyId = null;
  }

  async function cancelWanted(row) {
    if (busyRequestId != null) return;
    busyRequestId = row.request_id;
    notice = '';
    const result = await cancelRequest({ requestId: row.request_id });
    if (result.ok) {
      notice = 'Wanted order cancelled. No Solari was reserved.';
      noticeTone = 'ok';
    } else {
      notice = result.message;
      noticeTone = 'warn';
    }
    busyRequestId = null;
  }
</script>

<div class="mine">
  <div class="mine-head">
    <p class="panel-kicker mono">Your Karum</p>
    <div class="mine-tabs" role="tablist" aria-label="Your Karum activity">
      <button class:on={view === 'listings'} role="tab" aria-selected={view === 'listings'}
              type="button" onclick={() => (view = 'listings')}>My listings</button>
      <button class:on={view === 'requests'} role="tab" aria-selected={view === 'requests'}
              type="button" onclick={() => (view = 'requests')}>My requests</button>
      <button class:on={view === 'history'} role="tab" aria-selected={view === 'history'}
              type="button" onclick={() => (view = 'history')}>Trade history</button>
    </div>
  </div>

  {#if notice}
    <p class="notice" data-tone={noticeTone} role="status" aria-live="polite">{notice}</p>
  {/if}

  {#if view === 'listings'}
    {#if liveMine.length === 0}
      <SealedPanel
        status="empty" action="none" art="no-orders"
        emptyText="You have nothing for sale. Search your eligible bank stacks to list one."
      />
    {:else}
      <ul class="rows">
        {#each liveMine as row (row.listing_id)}
          <li class="row" data-status={row.status}>
            <div class="what">
              <span class="name">{row.display_name || row.template_id}</span>
              <span class="qty mono">x{(Number(row.stack_size) || 1).toLocaleString()}{#if Number(row.quality_level) > 0} &middot; {tierGradeLabel(row.tier, row.quality_level)}{/if}</span>
            </div>
            <span class="ask mono">{(Number(row.price) || 0).toLocaleString()}</span>
            <span class="state mono">{STATE_COPY[row.status] || row.status}</span>
            {#if PULLABLE.has(row.status)}
              <button class="btn" type="button" onclick={() => pull(row)} disabled={busyId != null}>
                {busyId === row.listing_id ? 'Pulling' : 'Pull'}
              </button>
            {:else}<span class="spacer"></span>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  {:else if view === 'requests'}
    {#if liveRequests.length === 0}
      <SealedPanel
        status="empty" action="none" art="no-orders"
        emptyText="You have no open wanted orders."
      />
    {:else}
      <ul class="rows">
        {#each liveRequests as row (row.request_id)}
          <li class="row" data-status={row.status}>
            <div class="what">
              <span class="name">{row.display_name || row.template_id}</span>
              <span class="qty mono">wanted x{(Number(row.stack_size) || 1).toLocaleString()} &middot; {wantedGradeLabel(row.quality_level, row.quality_mode)} &middot; {row.funded === true ? 'funded now' : row.funded === false ? 'low funds' : 'funds unknown'}</span>
            </div>
            <span class="ask mono">{(Number(row.price) || 0).toLocaleString()}</span>
            <span class="state mono">{STATE_COPY[row.status] || row.status}</span>
            {#if row.status === 'active'}
              <button class="btn" type="button" onclick={() => cancelWanted(row)} disabled={busyRequestId != null}>
                {busyRequestId === row.request_id ? 'Cancelling' : 'Cancel'}
              </button>
            {:else}<span class="spacer"></span>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  {:else}
    {#if history.length === 0}
      <SealedPanel
        status="empty" action="none" art="no-orders"
        emptyText="Completed and cancelled trades will appear here."
      />
    {:else}
      <ul class="rows">
        {#each history as entry (entry.key)}
          <li class="row" data-status={entry.row.status}>
            <div class="what">
              <span class="name">{entry.row.display_name || entry.row.template_id}</span>
              <span class="qty mono">{entry.direction} &middot; x{(Number(entry.row.stack_size) || 1).toLocaleString()}</span>
            </div>
            <span class="ask mono">{(Number(entry.row.price) || 0).toLocaleString()}</span>
            <span class="state mono">
              {entry.direction === 'bought' && entry.row.status === 'sold'
                ? 'collect at a terminal'
                : STATE_COPY[entry.row.status] || entry.row.status}
            </span>
            <span class="spacer"></span>
          </li>
        {/each}
      </ul>
      <p class="foot mono">Collected items show as CANCELED in the Completed tab. That is the only way the game can display them.</p>
    {/if}
  {/if}
</div>

<style>
  .mine { display: flex; flex-direction: column; gap: var(--space-3); }
  .mine-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .mine-tabs { display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; }
  .mine-tabs button { font-family: var(--font-mono); font-size: 10px; letter-spacing: .07em; text-transform: uppercase; color: var(--text-muted); background: var(--bg-deep); border: 0; border-right: 1px solid var(--edge); padding: var(--space-1) var(--space-2); cursor: pointer; }
  .mine-tabs button:last-child { border-right: 0; }
  .mine-tabs button.on { color: var(--accent-bright); background: var(--metal-1); }
  .mine-tabs button:active { transform: translateY(1px); }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .row {
    display: grid; grid-template-columns: 1fr auto 10rem 5rem; align-items: center; gap: var(--space-3);
    padding: var(--space-2) var(--space-3); background: var(--metal-0);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  /* The two transient states and the one that can need a human read differently. */
  .row[data-status='selling'], .row[data-status='reconciling'], .row[data-status='returning'] {
    border-color: color-mix(in srgb, var(--ls-yellow) 40%, var(--edge));
  }
  .row[data-status='paid_undelivered'] { border-color: color-mix(in srgb, var(--ls-yellow) 65%, var(--edge)); }
  .row[data-status='sold'] { border-color: color-mix(in srgb, var(--ls-green) 40%, var(--edge)); }
  .row[data-status='failed'] { opacity: .6; }
  .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .name { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .qty { font-size: 10px; color: var(--text-muted); }
  .ask { font-size: var(--text-sm); color: var(--accent-bright); font-variant-numeric: tabular-nums; }
  .state { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .spacer { display: block; }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm);
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer;
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .notice { margin: 0; font-size: var(--text-xs); line-height: 1.4; }
  .notice[data-tone='ok'] { color: var(--ls-green); }
  .notice[data-tone='warn'] { color: var(--ls-yellow); }
  .notice[data-tone='info'] { color: var(--accent-text); }
  .foot { margin: 0; font-size: 10px; color: var(--text-muted); line-height: 1.4; }

  @media (max-width: 40rem) {
    .mine-head { align-items: stretch; flex-direction: column; }
    .mine-tabs { width: 100%; }
    .mine-tabs button { flex: 1 1 0; }
    .row { grid-template-columns: 1fr auto; row-gap: var(--space-1); }
    .state { grid-column: 1 / -1; }
    .spacer { display: none; }
  }
</style>
