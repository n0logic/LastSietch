<script>
  import LiveCountdown from './LiveCountdown.svelte';
  // Two-great-house Landsraad standings: crest + decided-tile score per faction,
  // leader emphasized, term countdown. Crests are served same-origin from the
  // admin static mount (CSP img-src 'self').
  let { standings } = $props();
  const CREST = (c) => `/admin/static/img/factions/${c}.png`;
  let rails = $derived(standings?.rails ?? []);
  let lead = $derived.by(() => {
    if (rails.length < 2) return null;
    if (rails[0].score === rails[1].score) return 'tie';
    return rails[0].score > rails[1].score ? rails[0].slug : rails[1].slug;
  });
</script>

<p class="card-kicker">Landsraad{standings?.term?.test_term ? ' | test term' : ''}</p>
{#if standings?.available && rails.length >= 2}
  <div class="houses">
    {#each rails as r, i}
      {#if i === 1}<span class="vs mono">vs</span>{/if}
      <div class="house" class:lead={lead === r.slug}>
        {#if r.crest}
          <img class="crest" src={CREST(r.crest)} alt={r.name} width="34" height="34" loading="lazy" />
        {/if}
        <span class="hscore">{r.score}</span>
        <span class="hname mono">{r.name}</span>
      </div>
    {/each}
  </div>
  <p class="muted mono">
    {standings.decided ?? 0}/{(standings.decided ?? 0) + (standings.contested ?? 0)} houses decided
    {#if standings.term?.end_utc} | term ends <LiveCountdown target={standings.term.end_utc} />{/if}
  </p>
{:else if standings && !standings.available}
  <p class="big muted-strong">No active term</p>
  <p class="muted">the Landsraad reconvenes soon</p>
{:else}
  <p class="skeleton">...</p>
{/if}

<style>
  .houses { display: flex; align-items: center; justify-content: center; gap: var(--space-3); margin: var(--space-1) 0 0; }
  .house {
    flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px;
    padding: var(--space-2); border: 1px solid transparent; border-radius: var(--radius-sm);
  }
  .house.lead { border-color: color-mix(in srgb, var(--accent) 45%, transparent); background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 12%, transparent), transparent); }
  .crest { width: 34px; height: 34px; object-fit: contain; filter: drop-shadow(0 0 4px rgba(0,0,0,.5)); }
  .hscore {
    font-family: var(--font-display); font-weight: 700; font-size: var(--text-2xl);
    line-height: 1; color: var(--text-muted); font-variant-numeric: tabular-nums;
  }
  .house.lead .hscore { color: var(--accent-bright); text-shadow: 0 0 16px var(--accent-glow); }
  .hname { font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; color: var(--text-muted); }
  .vs { font-size: var(--text-xs); letter-spacing: .16em; color: var(--text-muted); opacity: .6; }
  /* shared utility classes mirrored from +page so the component reads consistently */
  .card-kicker { margin: 0 0 var(--space-3); font-family: var(--font-mono); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .24em; color: var(--accent); }
  .muted { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-3) 0 0; line-height: 1.45; }
  .muted-strong { color: var(--text-muted); }
  .big { font-family: var(--font-display); font-weight: 700; font-size: clamp(1.7rem, 3vw, 2.2rem); line-height: 1; margin: 0; color: var(--text); }
</style>
