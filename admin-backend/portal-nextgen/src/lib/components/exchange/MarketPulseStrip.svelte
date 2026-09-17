<script>
  // A thin ticker across the top of the Exchange. Three readouts:
  //   - how many templates you are tracking (armed WATCHES, not fired alerts —
  //     the two were previously conflated), AMBER chrome
  //   - the live "synced Ns ago" heartbeat, the ONLY genuinely live value here so
  //     the ONLY thing that earns Ibad-blue + a breathing LiveDot. When the read
  //     goes STALE (> 60s) the Ibad is suppressed to a plain amber "last synced"
  //     stamp (honesty rule: never lit unless it is actually moving).
  //   - a rare ticker: the names you are tracking, scrolling. Any watch whose live
  //     market min has crossed at/below its alert is lit Ibad (a value moving now);
  //     the rest stay amber. Under reduced motion the marquee is static.
  import { exchange } from '$lib/exchange.svelte.js';
  import LiveDot from '$lib/components/LiveDot.svelte';

  const STALE_MS = 60000;

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 1000);
    return () => clearInterval(id);
  });

  let syncedAt = $derived(exchange.pulse.syncedAt || 0);
  let agoMs = $derived(syncedAt ? Math.max(0, now - syncedAt) : Infinity);
  let stale = $derived(agoMs > STALE_MS);
  let agoText = $derived.by(() => {
    if (!syncedAt) return 'not yet';
    const s = Math.floor(agoMs / 1000);
    if (s < 1) return 'just now';
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    return `${Math.floor(m / 60)}h ago`;
  });

  let tracked = $derived(exchange.pulse.tracked || 0);
  // A watch "crossed" when its live market min is at/below the alert target. Only
  // present if the overview row carries a min_price; absent -> never faked as live.
  // Gated on !stale: a crossing read from a stale sync is no longer verifiably live,
  // so it must not light Ibad (same honesty rule as the heartbeat).
  function crossed(w) {
    if (stale) return false;
    const min = Number(w?.min_price);
    const tgt = Number(w?.max_price);
    return Number.isFinite(min) && Number.isFinite(tgt) && min > 0 && min <= tgt;
  }
  let ticker = $derived(exchange.watches.slice(0, 24));
  // Only marquee when there are enough names to actually overflow the strip; a few
  // names render static (no drift into the sync text). When scrolling we render the
  // set twice so translateX(-50%) is a seamless loop.
  let scroll = $derived(ticker.length > 8);
</script>

<div class="pulse" role="status" aria-live="off">
  <span class="seg tracked">
    <span class="k mono">Tracking</span>
    <strong class="v mono">{tracked}</strong>
  </span>

  <span class="seg sync" class:stale>
    {#if !stale}<LiveDot />{/if}
    <span class="k mono">{stale ? 'Last synced' : 'Synced'}</span>
    <span class="v mono" class:lit={!stale}>{agoText}</span>
  </span>

  <div class="ticker" class:scrolling={scroll} aria-hidden="true">
    {#if ticker.length}
      <div class="rail" class:drift={scroll}>
        {#each (scroll ? [...ticker, ...ticker] : ticker) as w, i (w.template_id + '-' + i)}
          <span class="chip" class:hot={crossed(w)}>
            {w.name || w.template_id}
            {#if crossed(w)}<span class="mark mono">alert</span>{/if}
          </span>
        {/each}
      </div>
    {:else}
      <span class="idle mono">No price watches armed yet</span>
    {/if}
  </div>
</div>

<style>
  .pulse {
    display: flex; align-items: center; gap: var(--space-4);
    padding: var(--space-2) var(--space-4);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi);
    overflow: hidden;
  }
  .seg { display: inline-flex; align-items: baseline; gap: var(--space-2); white-space: nowrap; }
  .k { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .14em; color: var(--text-muted); }
  .v { font-size: var(--text-sm); font-variant-numeric: tabular-nums; color: var(--accent-text); }
  .tracked .v { font-weight: 700; }

  .seg.sync { align-items: center; }
  /* Live heartbeat earns Ibad; stale/absent falls back to a plain amber stamp. */
  .sync .v.lit { color: var(--ls-ibad); }
  .sync.stale .v { color: var(--text-muted); }

  /* Static ticker: fade only the RIGHT edge (overflow), full-strength left so the
     first name reads cleanly and never bleeds into the sync stamp. Extra left pad
     keeps it clear of the "synced" segment. */
  .ticker { flex: 1; min-width: 0; overflow: hidden; padding-left: var(--space-3); -webkit-mask-image: linear-gradient(90deg, #000 0%, #000 92%, transparent); mask-image: linear-gradient(90deg, #000 0%, #000 92%, transparent); }
  /* When actually scrolling (many names), fade both edges for the marquee. */
  .ticker.scrolling { -webkit-mask-image: linear-gradient(90deg, transparent, #000 6%, #000 94%, transparent); mask-image: linear-gradient(90deg, transparent, #000 6%, #000 94%, transparent); }
  .rail { display: inline-flex; gap: var(--space-3); white-space: nowrap; }
  .rail.drift { will-change: transform; animation: drift 32s linear infinite; }
  .chip { font-size: var(--text-xs); color: var(--text-muted); display: inline-flex; align-items: center; gap: var(--space-1); }
  /* A watched item whose live min has crossed its alert: a value moving now = Ibad. */
  .chip.hot { color: var(--ls-ibad); }
  .chip.hot .mark {
    font-size: 9px; text-transform: uppercase; letter-spacing: .1em;
    color: var(--bg-deep); background: var(--ls-ibad); border-radius: 2px; padding: 0 3px;
  }
  .idle { font-size: var(--text-xs); color: var(--text-muted); }
  @keyframes drift { from { transform: translateX(0); } to { transform: translateX(-50%); } }

  @media (max-width: 640px) { .seg.tracked { display: none; } }
  @media (prefers-reduced-motion: reduce) {
    .rail.drift { animation: none; }
  }
</style>
