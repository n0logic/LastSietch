<script>
  // ONE carved instrument console replacing V1's four stacked banners.
  // Four cells (spice / storm / coriolis / worms), severity-ordered, the active
  // cell hot. Ibad blue ONLY on live feeds; staleness is judged from the
  // engine's asOfMs receipt stamps (a `fresh` boolean means "ever received" and
  // would claim live forever after a dead poller), degrading to dim amber with
  // a "last read" chip. Collapses to a swipeable single-row ticker on mobile.
  // `model` = the engine's onConsole payload (M1 contract + asOfMs extension).
  //
  // M2: worm audio cue toggle. The oscillator + danger-transition trigger live
  // in the engine (setAudioEnabled); this chip is the user-facing switch. The
  // route owns persistence (ls-worm-audio, muted default) and passes the state
  // down; the chip renders only when the route wires onaudiotoggle.
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';

  // The Board | Holo table viewer toggle lifted out to a standalone ViewerToggle
  // in the board header (so non-spice maps get it too).
  let {
    model = null, instLabel = '', audioOn = false, onaudiotoggle = null,
  } = $props();

  // 2.5x the 10s /live cadence: one missed poll tolerated, two = stale.
  const STALE_MS = 25_000;

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 5_000);
    return () => clearInterval(id);
  });

  let spice = $derived(model?.spice ?? null);
  let storm = $derived(model?.storm ?? null);
  let coriolis = $derived(model?.coriolis ?? null);
  let worms = $derived(model?.worms ?? null);

  // asOfMs == null -> feed never received (skeleton); older than STALE_MS ->
  // stale (dim amber + chip). Storm is state-driven (its scan gate lives in
  // the engine's derive), so it keeps its own freshness semantics.
  let spiceLive = $derived(spice?.asOfMs != null && now - spice.asOfMs <= STALE_MS);
  let spiceStale = $derived(spice?.asOfMs != null && now - spice.asOfMs > STALE_MS);
  let wormsLive = $derived(worms?.asOfMs != null && now - worms.asOfMs <= STALE_MS);
  let wormsStale = $derived(worms?.asOfMs != null && now - worms.asOfMs > STALE_MS);

  let spiceSector = $derived(spice?.blows?.[0]?.sector ?? null);

  // Severity ordering: an active storm outranks everything, then a live spice
  // blow, then worm danger. Calm cells keep the base spice/storm/coriolis/worms
  // order. Rendered via flex `order` so DOM order (and tab order) stays stable.
  let stormActive = $derived(storm?.state === 'centered' || storm?.state === 'sweeping');
  let spiceRank = $derived(spice?.active ? 3 : 0);
  let stormRank = $derived(stormActive ? 4 : 0);
  let wormRank = $derived(worms?.danger ? 2 : 0);

  // The divider/indent reset must land on the VISUALLY first cell, not the
  // DOM-first one: flex `order` reorders by severity, so :first-child (spice)
  // is often not leftmost. Resolve the leftmost key from the same orders (min
  // order wins; ties keep DOM sequence, matching flex tie-break).
  let cellOrder = $derived({ spice: -spiceRank, storm: -stormRank, coriolis: 0, worms: -wormRank });
  let firstKey = $derived(
    ['spice', 'storm', 'coriolis', 'worms'].reduce((b, k) => (cellOrder[k] < cellOrder[b] ? k : b), 'spice')
  );
</script>

<CarvedSlab sharp live hot={stormActive || !!spice?.active || !!worms?.danger}>
  <div class="console" role="group" aria-label="Desert conditions{instLabel ? ` | ${instLabel}` : ''}">
    <div class="cell" class:hot={spice?.active} class:flush={firstKey === 'spice'} style:order={-spiceRank}>
      <p class="kicker">Spice blow</p>
      {#if !spice || spice.asOfMs == null}
        <p class="skeleton reading">scanning...</p>
      {:else if spice.active}
        <p class="value" class:alive={spiceLive} class:stale={spiceStale}>
          {#if spiceLive}<LiveDot tone="live" />{/if}
          {spiceSector ? `Active at ${spiceSector}` : 'Active, locating'}
        </p>
        <p class="sub mono">
          {#if spice.blows?.length > 1}{spice.blows.length} blows up{/if}
          {#if spice.mediumTotal}{spice.blows?.length > 1 ? ' | ' : ''}{spice.mediumTotal} medium fields{#if spice.mediumActive} ({spice.mediumActive} erupting){/if}{/if}
        </p>
        {#if spiceStale}<span class="chip-stale mono">last read</span>{/if}
      {:else}
        <p class="value calm">No active blow</p>
        {#if spice.mediumTotal}<p class="sub mono">{spice.mediumTotal} medium fields up</p>{/if}
        {#if spiceStale}<span class="chip-stale mono">last read</span>{/if}
      {/if}
    </div>

    <div class="cell" class:hot={stormActive} class:flush={firstKey === 'storm'} style:order={-stormRank}>
      <p class="kicker">{stormActive ? 'Sandstorm' : 'Next sandstorm'}</p>
      {#if !storm}
        <p class="skeleton reading">scanning...</p>
      {:else if storm.state === 'centered'}
        <p class="value danger">
          <LiveDot tone="danger" />
          {storm.sector ? `Over ${storm.sector}` : 'Sweeping now'}
        </p>
        <p class="sub mono">{storm.heading ? `drifting ${storm.heading} (est) | make for rock` : 'make for rock'}</p>
        <!-- Storm rides the same /live poll as spice, so spice's receipt stamp
             is the transport-staleness proxy: a dead poller freezes this cell,
             and the chip discloses it (danger red stays; threat != freshness). -->
        {#if spiceStale}<span class="chip-stale mono">last read</span>{/if}
      {:else if storm.state === 'sweeping'}
        <p class="value danger"><LiveDot tone="danger" /> Sweeping now</p>
        <p class="sub mono">make for rock</p>
        {#if spiceStale}<span class="chip-stale mono">last read</span>{/if}
      {:else if storm.state === 'eta' && storm.etaUtc}
        <p class="value"><LiveCountdown target={storm.etaUtc} /></p>
        {#if storm.label}<p class="sub mono">{storm.label}</p>{/if}
      {:else}
        <p class="value calm">Sealed</p>
        <p class="sub mono">no storm telemetry yet</p>
      {/if}
    </div>

    <div class="cell" class:flush={firstKey === 'coriolis'} style:order={0}>
      <p class="kicker">Coriolis reset</p>
      {#if coriolis?.nextCycleUtc}
        <p class="value"><LiveCountdown target={coriolis.nextCycleUtc} /></p>
        <p class="sub mono">nodes and spice reroll</p>
      {:else}
        <p class="skeleton reading">...</p>
      {/if}
    </div>

    <div class="cell" class:hot={worms?.danger} class:flush={firstKey === 'worms'} style:order={-wormRank}>
      <p class="kicker">Shai-Hulud</p>
      {#if !worms || worms.asOfMs == null}
        <p class="skeleton reading">scanning...</p>
      {:else if worms.total > 0}
        <p class="value" class:danger={worms.danger} class:alive={!worms.danger && wormsLive} class:stale={!worms.danger && wormsStale}>
          {#if worms.danger}<LiveDot tone="danger" />{:else if wormsLive}<LiveDot tone="live" />{/if}
          {worms.total} roaming
        </p>
        <p class="sub mono">
          {#if worms.enraged}{worms.enraged} enraged{/if}
          {#if worms.enraged && worms.breaching} | {/if}
          {#if worms.breaching}{worms.breaching} breaching{/if}
          {#if !worms.enraged && !worms.breaching && worms.sectors?.length}near {worms.sectors.slice(0, 3).join(', ')}{/if}
        </p>
        {#if wormsStale}<span class="chip-stale mono">last read</span>{/if}
      {:else}
        <p class="value calm">None sighted</p>
        <p class="sub mono">watch the sand anyway</p>
        {#if wormsStale}<span class="chip-stale mono">last read</span>{/if}
      {/if}
      {#if onaudiotoggle}
        <button
          class="audio-chip mono"
          class:on={audioOn}
          aria-pressed={audioOn}
          title={audioOn ? 'Worm danger audio cue is on' : 'Worm danger audio cue is off'}
          onclick={() => onaudiotoggle(!audioOn)}
        >
          <span class="audio-glyph" aria-hidden="true">{audioOn ? '♪' : '⊘'}</span>
          {audioOn ? 'Cue on' : 'Cue off'}
        </button>
      {/if}
    </div>

  </div>
</CarvedSlab>

<style>
  .console {
    display: flex; gap: var(--space-4);
    margin: calc(-1 * var(--space-2)) 0;
  }
  .cell {
    flex: 1 1 0; min-width: 0; position: relative;
    padding: var(--space-2) 0 var(--space-2) var(--space-4);
    border-left: 1px solid var(--border-subtle);
  }
  /* .flush = the VISUALLY leftmost cell (resolved in script from flex order):
     no divider, no indent. Dividers stay uniform on every other cell. */
  .cell.flush { padding-left: 0; border-left: 0; }
  /* Hot cell: a lit accent bar along the TOP marks "this instrument is active".
     Kept off the left edge so it never recolors the inter-cell divider. */
  .cell.hot::before {
    content: ''; position: absolute; top: 0; left: var(--space-4); right: 0; height: 2px;
    background: var(--accent); box-shadow: 0 0 8px var(--accent-glow);
    border-radius: 1px;
  }
  .cell.flush.hot::before { left: 0; }

  .kicker {
    margin: 0 0 var(--space-2); font-family: var(--font-mono);
    font-size: var(--text-xs); text-transform: uppercase;
    letter-spacing: .22em; color: var(--accent);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .value {
    font-family: var(--font-display); font-weight: 700;
    font-size: var(--text-lg); line-height: 1.1; margin: 0;
    display: flex; align-items: center; gap: var(--space-2);
    color: var(--text); font-variant-numeric: tabular-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .value.alive { color: var(--ls-ibad); text-shadow: 0 0 14px var(--ls-ibad-glow); }
  .value.danger { color: var(--ls-red); text-shadow: 0 0 14px rgba(214, 90, 68, .3); }
  .value.calm { color: var(--text-muted); }
  /* Stale feed: honest dim amber, no Ibad. */
  .value.stale { color: var(--accent-soft); }
  .sub {
    margin: var(--space-1) 0 0; color: var(--text-muted);
    font-size: var(--text-xs); letter-spacing: .04em; line-height: 1.4;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .sub:empty { display: none; }
  .chip-stale {
    display: inline-block; margin-top: var(--space-1);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent-soft); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: 0 var(--space-2);
  }
  .audio-chip {
    display: inline-flex; align-items: center; gap: var(--space-1);
    margin-top: var(--space-1);
    background: transparent; color: var(--text-muted); cursor: pointer;
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: 0 var(--space-2);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .audio-chip:hover { color: var(--text); border-color: var(--edge-hi); }
  .audio-chip.on { color: var(--accent-bright); border-color: var(--accent); }
  .audio-glyph { font-size: var(--text-xs); line-height: 1; }
  .reading { min-width: 5rem; }

  /* Mobile: single-row swipeable ticker, cells snap into view. */
  @media (max-width: 720px) {
    .console {
      overflow-x: auto; scroll-snap-type: x mandatory;
      scrollbar-width: none; -webkit-overflow-scrolling: touch;
    }
    .console::-webkit-scrollbar { display: none; }
    .cell { flex: 0 0 72%; scroll-snap-align: start; }
  }
</style>
