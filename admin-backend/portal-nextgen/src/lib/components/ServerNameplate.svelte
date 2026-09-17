<script>
  // Etched station nameplate for the dashboard hero: server title + region +
  // an up/down status pill + the Coriolis reset countdown (the one time-sensitive
  // clock a player checks before logging on). Player-safe fields only
  // (status.name/region come from the public battlegroup view). Degrades to the
  // brand name when the status feed is unavailable so the hero never reads empty.
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';

  let { status = null, coriolis = null } = $props();

  let name = $derived(status?.name || 'Last Sietch');
  let region = $derived(status?.region || null);
  let up = $derived(status?.up ?? null);
</script>

<div class="nameplate">
  <div class="plate">
    <span class="name">{name}</span>
    {#if region}<span class="region mono">{region}</span>{/if}
  </div>
  {#if up === true}
    <span class="pill pill-up mono"><i class="dot"></i> online</span>
  {:else if up === false}
    <span class="pill pill-down mono"><i class="dot"></i> offline</span>
  {:else}
    <span class="pill pill-idle mono">…</span>
  {/if}
  {#if coriolis?.next_cycle_utc}
    <span class="coriolis mono" title="Deep Desert regenerates: nodes + spice reroll">
      <span class="coriolis-label">Coriolis storm in</span>
      <span class="coriolis-clock"><LiveCountdown target={coriolis.next_cycle_utc} /></span>
    </span>
  {/if}
</div>

<style>
  .nameplate { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
  .plate {
    display: inline-flex; align-items: baseline; gap: var(--space-2);
    padding: var(--space-1) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .name {
    font-family: var(--font-display); font-weight: 700; letter-spacing: .02em;
    font-size: var(--text-md); color: var(--text);
  }
  .region {
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text-muted);
  }
  .pill {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
    padding: 2px var(--space-2); border-radius: var(--radius-sm); border: 1px solid var(--edge);
  }
  .pill .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
  .pill-up { color: var(--ls-green); border-color: color-mix(in srgb, var(--ls-green) 50%, var(--edge)); }
  .pill-up .dot { background: var(--ls-green); box-shadow: 0 0 7px var(--ls-green); }
  .pill-down { color: var(--ls-red); border-color: color-mix(in srgb, var(--ls-red) 50%, var(--edge)); }
  .pill-down .dot { background: var(--ls-red); }
  .pill-idle { color: var(--text-muted); }
  .coriolis {
    display: inline-flex; align-items: baseline; gap: var(--space-2);
    font-size: var(--text-xs); letter-spacing: .08em; font-variant-numeric: tabular-nums;
  }
  .coriolis-label { color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .coriolis-clock { color: var(--accent-text); }
</style>
