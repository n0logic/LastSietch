<script>
  // Home panel: the 7-cell daily reward cycle, what is waiting, and when the
  // next one lands. READ ONLY. The claim itself stays on /rewards (wave 8
  // ruling 11), so there is no button here and no claim path: the strip reports
  // and links, it never writes.
  //
  // `enabled === false` is the DEFERRED answer and wins over everything else:
  // when the server has rewards switched off, a grid of claimable-looking cells
  // is a promise the backend will refuse. It is checked first, before the cycle.
  //
  // A null cycle is sealed. A cycle we cannot read is not an empty week.
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';

  let { cycle = null, claimableTotal = null, nextClaimUtc = null, enabled = true } = $props();

  // The overview serves the cycle either as the bare 7-cell array or wrapped in
  // the daily object that carries it; both are read, neither is required.
  let cells = $derived.by(() => {
    const raw = Array.isArray(cycle) ? cycle : cycle?.cycle;
    return Array.isArray(raw) ? raw.slice(0, 7) : [];
  });

  // Same vocabulary the /rewards calendar uses, minus the claim affordance.
  const STATE = {
    claimed: { glyph: '✓', label: 'Claimed' },
    claimable: { glyph: '!', label: 'Waiting' },
    lapsed: { glyph: '×', label: 'Expired' },
    pending: { glyph: '⋯', label: 'Pending' },
    missed: { glyph: '·', label: 'Missed' },
    upcoming: { glyph: '\u{1F512}', label: 'Upcoming' },
  };
  function chip(s) { return STATE[s] || STATE.upcoming; }
  // ASCII, and the same token the aria-label spells out: an en dash here reads
  // as a minus at 10px and the house copy rule bans the long dashes anyway.
  const NO_FIGURE = '--';

  function amountText(n) {
    return typeof n === 'number' && Number.isFinite(n) ? Math.floor(n).toLocaleString() : '';
  }

  let total = $derived(
    typeof claimableTotal === 'number' && Number.isFinite(claimableTotal)
      ? Math.floor(claimableTotal)
      : null
  );
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Rewards</p>
    {#if enabled !== false && nextClaimUtc}
      <span class="next mono"><LiveCountdown target={nextClaimUtc} prefix="next in" /></span>
    {/if}
  </div>

  {#if enabled === false}
    <SealedPanel
      status="empty" action="none"
      emptyText="Rewards are paused. Nothing can be collected while they are."
    />
  {:else if cells.length === 0}
    <SealedPanel status="empty" action="none" emptyText="The reward cycle could not be read." />
  {:else}
    <div class="cells">
      {#each cells as c, i (i)}
        {@const s = chip(c?.state)}
        <div
          class="cell {c?.state ?? 'upcoming'}"
          class:milestone={c?.milestone}
          class:today={c?.is_today}
          aria-label="Day {c?.cycle_day ?? i + 1}, {amountText(c?.amount) || 'no figure'} Solari, {s.label.toLowerCase()}"
        >
          <small class="mono">Day {c?.cycle_day ?? i + 1}</small>
          <b class="mono">{amountText(c?.amount) || NO_FIGURE}</b>
          <span class="g" aria-hidden="true">{s.glyph}</span>
        </div>
      {/each}
    </div>

    <div class="foot">
      {#if total === null}
        <span class="line">The claimable total could not be read.</span>
      {:else if total > 0}
        <span class="line">Waiting for you</span>
        <b class="mono amt">{total.toLocaleString()}</b>
      {:else}
        <span class="line">Nothing outstanding this cycle.</span>
      {/if}
    </div>

    <a class="cta mono" href="{base}/rewards">Open rewards</a>
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .next { font-size: var(--text-xs); white-space: nowrap; }

  .cells { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 3px; }
  .cell {
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
    padding: var(--space-2) 1px; min-width: 0; text-align: center;
    border: 1px solid var(--edge); border-radius: 4px;
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .cell small { font-size: 10px; color: var(--text-muted); letter-spacing: .04em; }
  .cell b { font-size: 10px; font-weight: 600; color: var(--accent-text); }
  .cell .g { font-size: var(--text-xs); line-height: 1; color: var(--text-muted); }

  .cell.milestone { border-style: dashed; border-color: var(--accent-soft); }
  .cell.today { outline: 1px solid color-mix(in srgb, var(--accent) 55%, transparent); outline-offset: 1px; }
  .cell.claimed { border-color: color-mix(in srgb, var(--ls-green) 45%, var(--edge)); }
  .cell.claimed .g { color: var(--ls-green); }
  /* Waiting: lit, but it is a reading, not a button. The press lives on /rewards. */
  .cell.claimable {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, var(--metal-0));
  }
  .cell.claimable .g, .cell.claimable b { color: var(--accent-bright); }
  .cell.upcoming { opacity: .42; }
  .cell.upcoming b { color: var(--text-muted); }
  .cell.missed { opacity: .55; }
  .cell.missed b { color: var(--text-muted); }
  .cell.lapsed b { color: var(--text-muted); }

  .foot {
    display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3);
    margin-top: var(--space-3);
  }
  .line { font-size: var(--text-sm); color: var(--text-muted); }
  .amt { font-size: var(--text-lg); font-weight: 700; color: var(--accent-text); }

  .cta {
    display: inline-block; margin-top: var(--space-3); text-decoration: none;
    color: var(--accent-text); font-size: var(--text-xs);
    letter-spacing: .14em; text-transform: uppercase;
  }
  .cta:hover { color: var(--accent-bright); }
</style>
