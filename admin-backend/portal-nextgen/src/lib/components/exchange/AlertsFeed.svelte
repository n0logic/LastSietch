<script>
  // Fired price alerts, newest first: the alerts a watch actually TRIPPED, which is
  // a different thing from the watches themselves (WatchlistCard lists those). Each
  // row names the item, the price that tripped it, the target that was set, and
  // when. Rendering the feed is what marks them seen — the overview read is polled
  // and must never clear the badge on its own, or an alert the player never looked
  // at disappears silently.
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import { exchange, markAlertsSeen } from '$lib/exchange.svelte.js';
  import { iconUrl } from '$lib/icons.js';

  let alerts = $derived(exchange.alerts || []);
  let fired = $derived(exchange.firedCount || 0);

  // Which rows were unseen WHEN THE FEED OPENED. Snapshotted because marking them
  // seen flips a.seen to true in the store on the same tick, which would otherwise
  // wipe the highlight out from under the player in the act of showing it to them.
  // The snapshot is what the eye reads; the store is what the next visit reads.
  let unseenAtOpen = $state(new Set());

  // Clear once per mount, and only the rows this feed actually RENDERED that were
  // unseen. The feed shows a newest-first slice, so clearing by account would mark
  // alerts seen that were never on the page.
  let cleared = false;
  $effect(() => {
    if (cleared || fired <= 0 || alerts.length === 0) return;
    const ids = alerts.filter((a) => a.seen === false).map((a) => a.id);
    if (ids.length === 0) return;
    cleared = true;
    unseenAtOpen = new Set(ids);
    markAlertsSeen(ids);
  });

  // created_at is a UTC timestamp from admin.db; render it as an age so the row
  // reads without the viewer doing timezone arithmetic. Unparseable falls back to
  // the raw stamp, never a fabricated "just now".
  // A server stamp may carry an offset (+00:00) or a Z; only a NAIVE stamp gets a
  // Z appended. Appending Z to an offset stamp made Date.parse return NaN.
  const hasZone = (v) => /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(String(v));
  function ago(stamp) {
    if (!stamp) return '';
    const t = Date.parse(hasZone(stamp) ? String(stamp) : `${stamp}Z`);
    if (!Number.isFinite(t)) return String(stamp);
    const s = Math.floor((Date.now() - t) / 1000);
    if (s < 60) return 'just now';
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }
</script>

<div class="feed">
  <p class="kicker mono">Alerts fired</p>
  {#if alerts.length === 0}
    <SealedPanel
      status="empty" action="none" art="no-alerts"
      emptyText="No alerts have fired yet. Arm a price watch and this fills in when one trips."
    />
  {:else}
    <ul class="rows">
      {#each alerts as a (a.id)}
        <li class="row" class:unseen={unseenAtOpen.has(a.id)}>
          <img class="icon" src={iconUrl(a.icon)} alt="" aria-hidden="true" loading="lazy" />
          <span class="name" title={a.name}>{a.name || a.template_id}</span>
          <span class="price mono">{Number(a.match_price).toLocaleString()}</span>
          <span class="tgt mono">watch &le;{Number(a.threshold_price).toLocaleString()}</span>
          <span class="when mono">{ago(a.created_at)}</span>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .feed { display: flex; flex-direction: column; gap: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .2em; font-size: var(--text-xs); }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .row {
    display: grid; align-items: center; gap: var(--space-2);
    grid-template-columns: 26px minmax(0, 1fr) auto;
    padding: var(--space-1) var(--space-2);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--metal-0);
  }
  /* An unseen alert is the one thing here that just happened, so it earns Ibad. */
  .row.unseen { border-color: color-mix(in srgb, var(--ls-ibad) 45%, var(--edge)); }
  .row.unseen .price { color: var(--ls-ibad); }
  .icon { width: 26px; height: 26px; object-fit: contain; }
  .name { min-width: 0; font-size: var(--text-sm); color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .price { font-size: var(--text-sm); color: var(--accent-text); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .tgt {
    grid-column: 2; font-size: var(--text-xs); color: var(--text-muted);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .when {
    grid-column: 3; text-align: right; font-size: var(--text-xs);
    color: var(--text-muted); white-space: nowrap;
  }
</style>
