<script>
  // Home instrument: the sandstorm on the player's map. Compact by default (the
  // sector, or the countdown to the next storm), expands in place.
  //
  // The freshness rule and the compass helper are COPIES of the ones the desert
  // home page has carried since v2, deliberately not an import: the route owns
  // its own derivations and a shared helper would make this panel move whenever
  // that page is refactored. Both must stay in step, and the suite pins the
  // constant and the bearing formula here.
  //
  // Ibad/danger tone is reserved for a storm that is genuinely blowing now.
  // Nothing here says whether anything the player owns is inside the track:
  // there is no radius source, so the panel does not guess one.
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';

  let { storm = null, mapLabel = '', now = Date.now() } = $props();

  // A storm counts as on the map while its position reader is fresh: the reader
  // tracks the moving storm live, so scan freshness is the active signal.
  const STORM_SCAN_FRESH_MS = 12 * 60_000;

  // World +x = East, +y = South, and heading_yaw matches the map engine's
  // (cos,sin) screen convention, so bearing = (yaw + 90) deg clockwise from North.
  function compass(yaw) {
    if (yaw == null || !isFinite(yaw)) return null;
    const b = (((yaw + 90) % 360) + 360) % 360;
    return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(b / 45) % 8];
  }

  let open = $state(false);
  let sector = $derived(storm?.storm_sector || null);
  let heading = $derived(compass(storm?.heading_yaw));
  let active = $derived.by(() => {
    if (!sector || !storm?.storm_scanned_utc) return false;
    const since = now - new Date(storm.storm_scanned_utc).getTime();
    return since >= 0 && since < STORM_SCAN_FRESH_MS;
  });
  let eta = $derived(storm?.next_eta_utc || null);
  let cycleMin = $derived(
    typeof storm?.mean_interval_min === 'number' ? Math.round(storm.mean_interval_min) : null
  );
  let confPct = $derived(
    typeof storm?.confidence === 'number' ? Math.round(storm.confidence * 100) : null
  );
  let label = $derived(mapLabel || 'the sand');
</script>

{#if storm === null || storm === undefined}
  <SealedPanel slab status="empty" action="none" emptyText="Storm telemetry could not be read." />
{:else}
  <CarvedSlab sharp live={active} hot={active}>
    <button class="face" type="button" aria-expanded={open} onclick={() => (open = !open)}>
      <span class="eyebrow mono">{active ? 'Sandstorm' : 'Next sandstorm'} | {label}</span>
      {#if active}
        <span class="val danger"><LiveDot tone="danger" /> {sector}</span>
      {:else if eta}
        <span class="val"><LiveCountdown target={eta} /></span>
      {:else}
        <span class="val muted">no telemetry</span>
      {/if}
      <svg class="glyph" viewBox="0 0 36 36" aria-hidden="true">
        <path d="M6 13h17a4 4 0 1 0-4-4" />
        <path d="M4 19h22a4.5 4.5 0 1 1-4.5 4.5" />
        <path d="M8 25h9" />
      </svg>
      <span class="caret" aria-hidden="true"></span>
    </button>
    {#if open}
      <div class="body">
        {#if active}
          <p class="row"><span>Heading</span><b class="mono">{heading ?? 'across the sand'}</b></p>
          <p class="line">Make for rock.</p>
        {:else if eta}
          {#if cycleMin != null}<p class="row"><span>Cycle</span><b class="mono">~{cycleMin}m</b></p>{/if}
          {#if confPct != null}<p class="row"><span>Confidence</span><b class="mono">{confPct}%</b></p>{/if}
        {:else}
          <p class="line">No storm telemetry on {label} yet.</p>
        {/if}
      </div>
    {/if}
  </CarvedSlab>
{/if}

<style>
  .face {
    width: 100%; display: grid; grid-template-columns: 1fr auto; gap: 2px var(--space-3);
    align-items: center; text-align: left; background: none; border: 0; padding: 0;
    color: inherit; cursor: pointer;
  }
  .eyebrow {
    grid-column: 1; font-size: var(--text-xs); text-transform: uppercase;
    letter-spacing: .18em; color: var(--accent);
  }
  .val {
    grid-column: 1; display: inline-flex; align-items: baseline; gap: var(--space-2);
    font-family: var(--font-mono); font-variant-numeric: tabular-nums;
    font-size: var(--text-2xl); font-weight: 700; line-height: 1; color: var(--text);
    overflow-wrap: anywhere;
  }
  .val.danger { color: var(--ls-red); }
  .val.muted { color: var(--text-muted); font-size: var(--text-lg); font-weight: 400; }
  .glyph {
    grid-row: 1 / 3; grid-column: 2; align-self: center; width: 34px; height: 34px;
    fill: none; stroke: var(--accent); stroke-width: 1.6; stroke-linecap: round; opacity: .8;
  }
  .caret {
    grid-column: 2; justify-self: end; width: 8px; height: 8px;
    border-right: 1.5px solid var(--text-muted); border-bottom: 1.5px solid var(--text-muted);
    transform: rotate(45deg); margin-top: var(--space-2);
    transition: transform var(--motion-fast) var(--ease-out);
  }
  .face[aria-expanded='true'] .caret { transform: rotate(225deg); }
  .face:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

  .body {
    margin-top: var(--space-3); padding-top: var(--space-3);
    border-top: 1px solid var(--edge); display: flex; flex-direction: column; gap: var(--space-2);
  }
  .row { margin: 0; display: flex; justify-content: space-between; gap: var(--space-3); font-size: var(--text-sm); color: var(--text-muted); }
  .row b { font-weight: 500; color: var(--text); }
  .line { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; }

  /* Phone: the instruments row is two columns wide, which leaves a seven
     figure Solari number nowhere to go. The glyph drops, the caret moves up
     beside the eyebrow, and the figure takes the full card width one size
     down rather than breaking mid-number. */
  @media (max-width: 520px) {
    .glyph { display: none; }
    .val { grid-column: 1 / -1; font-size: var(--text-lg); }
    .caret { grid-row: 1; margin-top: 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .caret { transition: none; }
  }
</style>
