<script>
  // Rewards summary: the player's own unclaimed Landsraad rewards at a glance,
  // as carved stat slabs. Pulled from the rewards half of the overview (account-
  // scoped). Independent of the term board.
  let { summary = null, oldestDays = null } = $props();

  function fmt(n) { const v = Number(n); return Number.isFinite(v) ? v.toLocaleString() : '0'; }

  let stats = $derived(summary ? [
    { label: 'Reward lines', value: summary.total_lines },
    { label: 'Solari', value: summary.total_solari },
    { label: 'Houses', value: summary.houses_with_rewards },
    { label: 'Schematics', value: summary.schematic_lines },
    { label: 'Swatches', value: summary.swatch_lines },
    { label: 'Other', value: summary.other_lines },
  ] : []);
</script>

<div class="rsummary">
  <div class="head">
    <p class="panel-kicker mono">Your rewards</p>
    {#if summary?.oldest_days != null}
      <span class="oldest mono">oldest {summary.oldest_days}d</span>
    {/if}
  </div>
  {#if !summary}
    <p class="hollow">Your rewards could not be read right now.</p>
  {:else if Number(summary.total_lines) === 0 && Number(summary.total_solari) === 0}
    <p class="hollow">No unclaimed house rewards waiting.</p>
  {:else}
    <div class="grid">
      {#each stats as s (s.label)}
        <div class="slab">
          <span class="num mono">{fmt(s.value)}</span>
          <span class="lbl">{s.label}</span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .oldest { font-size: var(--text-xs); color: var(--text-muted); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: var(--space-2); }
  .slab {
    display: flex; flex-direction: column; gap: 2px; min-width: 0;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3);
  }
  .num { font-size: 17px; font-weight: 700; color: var(--text); line-height: 1.1; white-space: nowrap; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
</style>
