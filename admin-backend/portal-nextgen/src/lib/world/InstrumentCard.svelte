<script>
  // Hover / focus instrument card, shared by the Guild chart and the 3D table.
  // DOM, never WebGL text. Copy rule: no em dashes. Band marks come ONLY from the
  // enum-only bands endpoint; with no band the line is simply absent.
  let { model, anchor = null, bounds = null, onopen = null } = $props();

  const W = 292;
  let pos = $derived.by(() => {
    if (!anchor || !bounds) return null;
    const left = anchor.x + 22 + W > bounds.w - 8 ? Math.max(8, anchor.x - 22 - W) : anchor.x + 22;
    const top = Math.max(8, Math.min(bounds.h - 220, anchor.y - 40));
    return `left:${left}px;top:${top}px`;
  });
</script>

{#if model}
  <aside class="card" class:docked={!pos} style={pos || undefined} aria-live="polite" data-status={model.status || 'none'}>
    <p class="kind mono">{model.kind}</p>
    <h2>{model.title}</h2>
    {#if model.facets.length}
      <ul class="facets">
        {#each model.facets as f (f.key)}
          <li>
            <span class="fname">{f.name}</span>
            <span class="rule">{f.rule}</span>
            {#if f.band}
              <span class="band mono" title="Relative activity">
                {#each [0, 1, 2] as i}<i class:lit={i < f.band.marks}></i>{/each}
                {f.band.label}
              </span>
            {/if}
          </li>
        {/each}
      </ul>
    {:else if model.band}
      <p class="bandline mono">
        {#each [0, 1, 2] as i}<i class:lit={i < model.band.marks}></i>{/each}
        {model.band.label}
      </p>
    {/if}
    {#if model.description}<p class="desc">{model.description}</p>{/if}
    {#if model.statusText}
      <p class="status"><span>Status</span> <b class="st-{model.status}">{model.statusText}</b></p>
    {/if}
    {#if model.live && onopen}<p class="hint mono">Enter or click to open the War-Table</p>{/if}
  </aside>
{/if}

<style>
  .card {
    position: absolute; z-index: 20; width: 292px; max-width: calc(100% - 16px);
    padding: 12px 14px 12px; pointer-events: none;
    background: linear-gradient(180deg, rgba(22,18,13,.96), rgba(12,10,8,.97));
    border: 1px solid var(--edge-hi, #605445); border-radius: 2px;
    box-shadow: 0 10px 28px rgba(0,0,0,.55), inset 0 1px 0 var(--metal-hi);
    color: var(--text); font-family: 'Saira Condensed', var(--font-body, sans-serif);
  }
  .card.docked { position: static; width: auto; max-width: none; }
  .kind { margin: 0 0 2px; font-size: 11px; letter-spacing: .24em; text-transform: uppercase; color: var(--text-muted); }
  h2 { margin: 0 0 8px; font-size: 20px; letter-spacing: .14em; text-transform: uppercase; color: var(--accent-bright, var(--accent)); font-weight: 600; }
  .facets { list-style: none; margin: 0 0 8px; padding: 0 0 8px; border-bottom: 1px solid var(--edge); display: grid; gap: 3px; }
  .facets li { display: flex; flex-wrap: wrap; gap: 0 8px; align-items: baseline; font-size: 14px; letter-spacing: .06em; }
  .fname { text-transform: uppercase; }
  .rule { color: var(--text-muted); text-transform: uppercase; font-size: 13px; }
  .band, .bandline { margin-left: auto; font-size: 11px; color: var(--text-muted); display: inline-flex; gap: 3px; align-items: center; }
  .bandline { margin: 0 0 8px; }
  .band i, .bandline i { width: 7px; height: 7px; border-radius: 50%; border: 1px solid var(--edge-hi); display: inline-block; }
  .band i.lit, .bandline i.lit { background: var(--accent-bright, var(--accent)); border-color: var(--accent-bright, var(--accent)); box-shadow: 0 0 6px var(--accent-glow); }
  .desc { margin: 0 0 8px; font-size: 14px; line-height: 1.35; color: var(--text-muted); }
  .status { margin: 0; display: flex; gap: 12px; align-items: baseline; font-size: 14px; letter-spacing: .1em; text-transform: uppercase; }
  .status span { color: var(--text-muted); }
  .status b { font-weight: 600; }
  .st-up { color: var(--accent-bright, var(--accent)); }
  .st-down { color: #9b8f80; }
  .st-on-demand { color: var(--text); }
  .st-blocked { color: #d2693a; }
  .st-unknown { color: var(--text-muted); }
  .hint { margin: 8px 0 0; font-size: 10.5px; color: var(--text-muted); letter-spacing: .08em; }
</style>
