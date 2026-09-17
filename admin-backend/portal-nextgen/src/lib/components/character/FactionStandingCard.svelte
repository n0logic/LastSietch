<script>
  // Faction standing: the character's rank + reputation progress with their great
  // house. Ibad-blue progress bar to the next rank (a genuine live value). Null
  // rep (unaligned / no data) renders an honest empty note.
  import { factionCrestUrl } from '$lib/icons.js';

  let { rep = null } = $props();

  let crest = $derived(rep ? factionCrestUrl(rep.crest || rep.faction) : null);
  let pct = $derived(Math.max(0, Math.min(100, Number(rep?.pct) || 0)));
  function fmt(n) { const v = Number(n); return Number.isFinite(v) ? v.toLocaleString() : '0'; }
</script>

<div class="standing">
  <p class="panel-kicker mono">Faction standing</p>
  {#if !rep}
    <p class="hollow">No great-house allegiance on record.</p>
  {:else}
    <div class="head">
      {#if crest}<img class="crest" src={crest} alt="" aria-hidden="true" loading="lazy" />{/if}
      <div class="titles">
        <span class="faction">{rep.faction || 'Faction'}</span>
        <span class="rank mono">{rep.rank_name || `Rank ${rep.rank ?? 0}`}</span>
      </div>
      {#if rep.at_max}<span class="badge mono">Max rank</span>{/if}
    </div>

    {#if !rep.at_max}
      <div class="bar" role="progressbar" aria-valuenow={pct} aria-valuemin="0" aria-valuemax="100"
           aria-label="Reputation to next rank">
        <div class="fill" style="width:{pct}%"></div>
      </div>
      <div class="meta mono">
        <span>{fmt(rep.standing)} rep</span>
        {#if rep.next_rank}<span>{fmt(rep.to_next)} to {rep.next_rank}</span>{/if}
      </div>
    {:else}
      <p class="meta mono"><span>{fmt(rep.standing)} rep</span></p>
    {/if}
  {/if}
</div>

<style>
  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; }
  .head { display: flex; align-items: center; gap: var(--space-3); margin-bottom: var(--space-3); }
  .crest { width: 44px; height: 44px; object-fit: contain; flex: 0 0 auto; }
  .titles { display: flex; flex-direction: column; min-width: 0; }
  .faction { font-size: var(--text-base); color: var(--text); font-weight: 600; }
  .rank { font-size: var(--text-sm); color: var(--accent-text); text-transform: uppercase; letter-spacing: .1em; }
  .badge { margin-left: auto; font-size: var(--text-xs); color: var(--ls-green); text-transform: uppercase; letter-spacing: .1em; border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 2px var(--space-2); }
  .bar { height: 8px; background: var(--metal-0); border: 1px solid var(--edge); border-radius: 999px; overflow: hidden; }
  .fill { height: 100%; background: var(--ls-ibad); box-shadow: 0 0 8px color-mix(in srgb, var(--ls-ibad) 60%, transparent); }
  .meta { display: flex; justify-content: space-between; gap: var(--space-3); margin: var(--space-2) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
</style>
