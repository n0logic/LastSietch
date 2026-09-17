<script>
  // Per-map up/down chips with live player counts. Reads the public status view
  // (status.maps[{name, players, up, on_demand}]). Counts only, no positions.
  // On-demand hubs (Arrakeen / Harko Village) spin down when empty and spin up
  // on travel — a down pod there is normal, so we show a calm "on travel"
  // (amber) rather than an alarming red "down".
  let { maps = [] } = $props();
</script>

{#if maps?.length}
  <ul class="maplist">
    {#each maps as m}
      <li class="maprow" class:down={!m.up && !m.on_demand} class:ondemand={!m.up && m.on_demand}>
        <span class="mname">
          <i class="dot" class:up={m.up} class:ondemand={!m.up && m.on_demand}></i>{m.name}
        </span>
        <span class="mcount mono">{m.up ? `${m.players ?? 0}` : (m.on_demand ? 'on travel' : 'down')}</span>
      </li>
    {/each}
  </ul>
{:else}
  <p class="skeleton mono">reading status…</p>
{/if}

<style>
  .maplist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .maprow {
    display: flex; align-items: center; justify-content: space-between; gap: var(--space-3);
    padding: var(--space-2) var(--space-3); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0);
  }
  .maprow.down { opacity: .72; }
  .maprow.ondemand { opacity: .85; }
  .mname { display: inline-flex; align-items: center; gap: var(--space-2); color: var(--text); font-size: var(--text-sm); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ls-red); display: inline-block; flex: none; }
  .dot.up { background: var(--ls-green); box-shadow: 0 0 6px color-mix(in srgb, var(--ls-green) 60%, transparent); }
  /* On-demand hub spun down: amber, not red — it's available on travel, not an outage. */
  .dot.ondemand { background: var(--accent); }
  .mcount { color: var(--text-muted); font-size: var(--text-xs); letter-spacing: .08em; font-variant-numeric: tabular-nums; }
  .skeleton { color: var(--text-muted); opacity: .6; }
</style>
