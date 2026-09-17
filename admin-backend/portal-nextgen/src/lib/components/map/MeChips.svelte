<script>
  // You/Bases/Vehicles me-layer toggles with live counts. Presentational:
  // the route owns `show` persistence and feeds the engine's layer visibility;
  // counts derive from the engine's onMe relay of /portal/maps/{key}/me.
  // `online` is pre-gated on /me feed freshness by the route (Ibad rule);
  // `stale` marks a self position whose feed has gone quiet: dim amber pip.
  let {
    counts = { self: 0, bases: 0, vehicles: 0 },
    show = { self: true, bases: true, vehicles: true },
    online = false,
    stale = false,
    onchange,
  } = $props();

  const CHIPS = [
    { key: 'self', label: 'You' },
    { key: 'bases', label: 'Bases' },
    { key: 'vehicles', label: 'Vehicles' },
  ];

  function toggle(key) {
    onchange?.({ ...show, [key]: !show[key] });
  }
</script>

<div class="me" role="group" aria-label="My presence layers">
  <span class="label mono">My presence</span>
  {#each CHIPS as chip (chip.key)}
    <button
      class="chip mono"
      class:off={!show[chip.key]}
      class:empty={!counts[chip.key]}
      aria-pressed={!!show[chip.key]}
      onclick={() => toggle(chip.key)}
    >
      <span
        class="swatch swatch-{chip.key}"
        class:live={chip.key === 'self' && online && counts.self > 0}
        class:stale={chip.key === 'self' && stale}
        aria-hidden="true"
      ></span>
      {chip.label}
      {#if chip.key !== 'self'}<span class="n">{counts[chip.key] || 0}</span>{/if}
    </button>
  {/each}
</div>

<style>
  .me {
    display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2);
  }
  .label {
    color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .18em; text-transform: uppercase;
  }
  .chip {
    display: inline-flex; align-items: center; gap: var(--space-2);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--edge); border-radius: var(--radius-xl);
    padding: var(--space-1) var(--space-3); cursor: pointer;
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out), opacity var(--motion-fast) var(--ease-out);
  }
  .chip:hover { border-color: var(--accent); }
  .chip.off { color: var(--text-muted); opacity: .55; }
  .chip.off .swatch { opacity: .35; }
  .chip.empty:not(.off) { color: var(--text-muted); }

  .swatch {
    flex: none; width: 9px; height: 9px; border-radius: 50%;
    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .4);
  }
  /* Own position is the ONLY genuinely live pip: Ibad, breathing when online.
     A quiet /me feed degrades it to dim amber (honesty rule: fresh feed or no
     Ibad), same as the console's 'last read' treatment. */
  .swatch-self { background: var(--ls-ibad); }
  .swatch-self.live {
    box-shadow: 0 0 6px var(--ls-ibad-glow);
    animation: me-breathe 2.4s var(--ease-in-out) infinite;
  }
  .swatch-self.stale { background: var(--accent-soft); animation: none; box-shadow: none; }
  .swatch-bases { background: var(--accent); border-radius: 2px; }
  .swatch-vehicles { background: var(--accent-soft); }

  .n { color: var(--text-muted); }
  .chip:not(.off):not(.empty) .n { color: var(--text); }

  @keyframes me-breathe {
    0%, 100% { box-shadow: 0 0 3px var(--ls-ibad-glow); }
    50% { box-shadow: 0 0 9px var(--ls-ibad); }
  }
  @media (prefers-reduced-motion: reduce) {
    .swatch-self.live { animation: none; }
  }
</style>
