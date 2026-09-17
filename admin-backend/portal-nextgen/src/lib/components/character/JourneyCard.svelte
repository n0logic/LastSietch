<script>
  // Journey: the character's world-progress summary (story arcs, points of
  // interest, big moments, codex). Raw counts as stat slabs; completed arc names
  // list underneath when present.
  let { journey = null } = $props();

  let arcNames = $derived(Array.isArray(journey?.arc_names) ? journey.arc_names : []);
  function fmt(n) { const v = Number(n); return Number.isFinite(v) ? v.toLocaleString() : '0'; }

  let stats = $derived(journey ? [
    { label: 'Arcs completed', value: journey.arcs_completed },
    { label: 'Points of interest', value: journey.poi_total },
    { label: 'Big moments', value: journey.big_moments_count },
    { label: 'Codex entries', value: journey.codex_count },
  ] : []);
</script>

<div class="journey">
  <p class="panel-kicker mono">Journey</p>
  {#if !journey}
    <p class="hollow">No journey progress on record.</p>
  {:else}
    <div class="grid">
      {#each stats as s (s.label)}
        <div class="slab">
          <span class="num mono">{fmt(s.value)}</span>
          <span class="lbl">{s.label}</span>
        </div>
      {/each}
    </div>
    {#if arcNames.length}
      <ul class="arcs">
        {#each arcNames as name, i (i)}
          <li class="arc">{name}</li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>

<style>
  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: var(--space-2); }
  .slab {
    display: flex; flex-direction: column; gap: 2px; min-width: 0;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3);
  }
  .num { font-size: var(--text-xl); font-weight: 700; color: var(--text); line-height: 1.1; white-space: nowrap; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .arcs { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .arc { font-size: var(--text-xs); color: var(--text-muted); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 2px var(--space-2); }
</style>
