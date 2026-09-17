<script>
  // Landsraad teaser: a one-line summary of the character's unclaimed house
  // rewards, linking through to the full Landsraad module. Summary only (the
  // account-scoped detail lives on /landsraad).
  import { base } from '$app/paths';

  let { teaser = null } = $props();

  function fmt(n) { const v = Number(n); return Number.isFinite(v) ? v.toLocaleString() : '0'; }
  let hasAny = $derived(teaser && (Number(teaser.total_lines) > 0 || Number(teaser.total_solari) > 0));
</script>

<div class="teaser">
  <p class="panel-kicker mono">Landsraad rewards</p>
  {#if !teaser}
    <p class="hollow">Landsraad rewards could not be read right now.</p>
  {:else if !hasAny}
    <p class="hollow">No unclaimed house rewards waiting.</p>
  {:else}
    <div class="line">
      <div class="stat"><span class="num mono">{fmt(teaser.total_lines)}</span><span class="lbl">reward lines</span></div>
      <div class="stat"><span class="num mono">{fmt(teaser.total_solari)}</span><span class="lbl">Solari</span></div>
      {#if Number(teaser.schematic_lines) > 0}
        <div class="stat"><span class="num mono">{fmt(teaser.schematic_lines)}</span><span class="lbl">schematics</span></div>
      {/if}
    </div>
  {/if}
  <a class="more" href={`${base}/landsraad`}>Open Landsraad</a>
</div>

<style>
  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; }
  .line { display: flex; gap: var(--space-4); flex-wrap: wrap; }
  .stat { display: flex; flex-direction: column; min-width: 0; }
  .num { font-size: var(--text-lg); font-weight: 700; color: var(--text); line-height: 1.1; white-space: nowrap; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .more {
    display: inline-block; margin-top: var(--space-3);
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent-text); text-decoration: none; border-bottom: 1px solid transparent;
  }
  .more:hover { border-bottom-color: var(--accent); }
</style>
