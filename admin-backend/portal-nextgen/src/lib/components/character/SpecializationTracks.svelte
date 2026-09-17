<script>
  // Specialization tracks: each of the character's spec lines with its level +
  // progress to the level cap, plus the keystones-owned summary. Icons are SLUGS
  // resolved against the stat mount. When ctrl_scoped is false (a non-default
  // selected character the relay can't scope), a subtle "last logout" note shows.
  import { statIconUrl } from '$lib/icons.js';

  let { specializations = null } = $props();

  let tracks = $derived(Array.isArray(specializations?.tracks) ? specializations.tracks : []);
  let cap = $derived(Number(specializations?.level_cap) || 100);
  function fmt(n) { const v = Number(n); return Number.isFinite(v) ? v.toLocaleString() : '0'; }
  function pctFor(t) {
    if (t?.pct != null) return Math.max(0, Math.min(100, Number(t.pct) || 0));
    const lvl = Number(t?.level) || 0;
    return cap > 0 ? Math.max(0, Math.min(100, Math.round((lvl / cap) * 100))) : 0;
  }
</script>

<div class="specs">
  <div class="head">
    <p class="panel-kicker mono">Specializations</p>
    {#if specializations}
      <span class="keys mono">{fmt(specializations.keystones_owned)} / {fmt(specializations.keystones_total)} keystones</span>
    {/if}
  </div>

  {#if !specializations || tracks.length === 0}
    <p class="hollow">No specialization progress on record.</p>
  {:else}
    <div class="list">
      {#each tracks as t (t.name)}
        {@const url = statIconUrl(t.icon)}
        {@const pct = pctFor(t)}
        <div class="track">
          {#if url}<img class="glyph" src={url} alt="" aria-hidden="true" loading="lazy" />{/if}
          <div class="body">
            <div class="row">
              <span class="name">{t.name}</span>
              <span class="lvl mono">Lv {fmt(t.level)}{cap ? ` / ${cap}` : ''}</span>
            </div>
            <div class="bar" role="progressbar" aria-valuenow={pct} aria-valuemin="0" aria-valuemax="100" aria-label={`${t.name} progress`}>
              <div class="fill" style="width:{pct}%"></div>
            </div>
          </div>
        </div>
      {/each}
    </div>
    {#if specializations.ctrl_scoped === false}
      <p class="note mono">Reflects your last logout for this character.</p>
    {/if}
  {/if}
</div>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .keys { font-size: var(--text-xs); color: var(--text-muted); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; }
  .list { display: flex; flex-direction: column; gap: var(--space-3); }
  .track { display: flex; align-items: center; gap: var(--space-3); }
  .glyph { width: 30px; height: 30px; object-fit: contain; flex: 0 0 auto; }
  .body { flex: 1 1 auto; min-width: 0; }
  .row { display: flex; justify-content: space-between; gap: var(--space-2); margin-bottom: var(--space-1); }
  .name { font-size: var(--text-sm); color: var(--text); }
  .lvl { font-size: var(--text-xs); color: var(--accent-text); }
  .bar { height: 6px; background: var(--metal-0); border: 1px solid var(--edge); border-radius: 999px; overflow: hidden; }
  .fill { height: 100%; background: var(--accent); }
  .note { margin: var(--space-3) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
</style>
