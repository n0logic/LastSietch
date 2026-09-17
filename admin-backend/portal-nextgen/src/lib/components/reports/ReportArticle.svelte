<script>
  import { base } from '$app/paths';
  import { reportDate, reportTime, reportWindow, metricValue, changeLabel } from '$lib/reports.js';
  let { edition, preview = false } = $props();
  let copied = $state('');
  let points = $derived(edition.activity || []);
  let maximum = $derived(Math.max(1, ...points.map((p) => p.hours ?? 0)));
  let hasChart = $derived(points.length > 1 && points.some((p) => p.hours != null));
  async function copyLink() {
    try {
      await navigator.clipboard.writeText(`${location.origin}${base}/reports/${edition.id}`);
      copied = 'Report link copied';
    } catch { copied = 'Copy the report address from your browser to share it.'; }
  }
</script>

<article class="edition" class:preview aria-label={preview ? 'Unpublished report preview' : 'Published report edition'}>
  <header class="masthead">
    <div class="topline mono"><span>Last Sietch / {edition.period === 'weekly' ? 'Sunday edition' : 'Daily edition'}</span><span>{preview ? 'Preview, not published' : reportDate(edition.date)}</span></div>
    <svelte:element this={preview ? "h2" : "h1"} class="masthead-title">{edition.title}</svelte:element>
    <div class="dateline"><span>{reportWindow(edition.window)}</span><span>{preview ? 'No archive or Discord post created' : `Published ${reportTime(edition.published_at)}`}</span></div>
  </header>

  <section class="lead" aria-labelledby="edition-headline">
    <span class="eyebrow mono">{edition.period === 'weekly' ? 'The week in view' : 'From across the Sietch'}</span>
    <h2 id="edition-headline">{edition.headline}</h2>
    <p>{edition.summary}</p>
  </section>

  {#if edition.metrics?.length}
    <dl class="metric-strip">
      {#each edition.metrics as metric (metric.key)}
        <div><dt>{metric.label}</dt><dd>{metricValue(metric)}{#if metric.unit === 'Solari'}<small> Solari</small>{/if}</dd><p class:positive={metric.change > 0}>{changeLabel(metric)}</p></div>
      {/each}
    </dl>
  {/if}

  {#if hasChart}
    <section class="activity-chart" aria-labelledby="activity-chart-title">
      <div class="section-heading"><h2 id="activity-chart-title">The rhythm of the sands</h2><span class="mono">Recorded player-hours</span></div>
      <div class="chart" role="img" aria-label="Recorded player-hours by period. Missing observations are shown as gaps.">
        {#each points as point (point.start)}
          <div class="chart-column">
            <div class="bar-space"><div class="bar" class:gap={point.hours == null} style:--bar-height={`${point.hours == null ? 1 : Math.max(1, point.hours / maximum * 100)}%`} title={`${point.label}: ${point.hours == null ? 'no verified observations' : `${point.hours.toFixed(1)} player-hours`}`}></div></div>
            <span class="chart-label">{point.label}</span><span class="chart-value mono">{point.hours == null ? 'gap' : `${point.hours.toFixed(1)} h`}</span>
          </div>
        {/each}
      </div>
      <p class="caption">Five-minute sample estimates. Gaps are not presented as zero activity.</p>
      <details class="chart-data"><summary>View activity values</summary><table><thead><tr><th scope="col">Period</th><th scope="col">Recorded player-hours</th></tr></thead><tbody>{#each points as point (point.start)}<tr><th scope="row">{point.label}</th><td>{point.hours == null ? 'No verified observations' : point.hours.toFixed(1)}</td></tr>{/each}</tbody></table></details>
    </section>
  {/if}

  <div class="edition-columns">
    <div class="stories">
      {#each (edition.period === 'weekly' ? edition.highlights || [] : (edition.highlights || []).slice(1)) as highlight (highlight.key)}
        <section class="story"><span class="eyebrow mono">{highlight.kind === 'record' ? 'The record book' : highlight.kind === 'event' ? 'Looking ahead' : 'Sietch notes'}</span><h2>{highlight.title}</h2><p>{highlight.body}</p></section>
      {/each}
      {#each edition.sections || [] as section (section.key)}
        <section class="detail-section" id={`report-${section.key}`}>
          <h2>{section.title}</h2>
          {#if section.description}<p class="caption">{section.description}</p>{/if}
          <dl class="detail-rows">
            {#each section.rows as row, i (i)}
              <div><dt>{#if row.href && /^\/events\/\d+$/.test(row.href)}<a href={`${base}${row.href}`}>{row.label}</a>{:else}{row.label}{/if}</dt><dd>{row.value}</dd></div>
            {/each}
          </dl>
        </section>
      {/each}
    </div>
    <aside class="edition-aside">
      <p class="eyebrow mono">Your next stop</p>
      <h2>Back to the Sietch</h2>
      <p>See what is coming up, browse the Exchange, or explore another edition.</p>
      <nav aria-label="Report actions"><a class="report-action" href={`${base}/events`}>See events <span aria-hidden="true">↗</span></a><a class="report-action" href={`${base}/exchange`}>Open the Exchange <span aria-hidden="true">↗</span></a><a class="report-action" href={`${base}/start`}>Worlds and PvP guidance <span aria-hidden="true">↗</span></a><a class="report-action" href={`${base}/reports`}>Browse the archive <span aria-hidden="true">↗</span></a></nav>
      {#if !preview}<button type="button" class="share" onclick={copyLink}>Copy report link</button><span class="copy-note" aria-live="polite">{copied}</span>{/if}
      <p class="archive-note">This edition preserves its original reporting window and figures. Current conditions may have changed.</p>
    </aside>
  </div>

  {#if edition.stations?.length}
    <section class="station-section" aria-labelledby="standing-board-title">
      <div class="section-heading"><h2 id="standing-board-title">The standing board</h2><span class="mono">Observed {reportTime(edition.captured_at)}</span></div>
      <p class="caption">Standing testing-station records. A recorded runner may represent a group.</p>
      <div class="table-scroll"><table><thead><tr><th scope="col">Testing station</th><th scope="col">Tier</th><th scope="col">Since previous edition</th><th scope="col">Recorded participant</th></tr></thead><tbody>
        {#each edition.stations as station (station.id)}<tr><th scope="row">{station.name}</th><td class="tier mono">{station.tier}</td><td>{station.improved ? `+${station.tier - station.previous_tier}` : station.previous_tier == null ? 'No earlier baseline' : 'Standing'}</td><td>{station.runner || 'Not recorded'}{#if station.party_size > 1}<small>Group of {station.party_size}</small>{/if}</td></tr>{/each}
      </tbody></table></div>
    </section>
  {/if}

  <details class="quality"><summary>About these figures</summary><p>Collected {reportTime(edition.captured_at)}. The reporting window uses Eastern time.</p><ul>{#each edition.quality || [] as note}<li>{note}</li>{/each}</ul></details>
</article>

<style>
  .edition { max-width: 78rem; margin: 0 auto; color: var(--text); }
  .masthead { border-top: 3px solid var(--accent); border-bottom: 1px solid var(--border); padding: 1.15rem 0 1rem; }
  .topline, .dateline { display: flex; justify-content: space-between; flex-wrap: wrap; gap: .5rem 2rem; color: var(--text-muted); font-size: var(--text-xs); }
  .topline { text-transform: uppercase; letter-spacing: .13em; }
  .masthead-title { font-family: var(--font-display); font-weight: 700; font-size: clamp(2rem, 5.2vw, 3.4rem); line-height: 1.02; letter-spacing: .01em; margin: 1.3rem 0 1.1rem; text-wrap: balance; }
  h2 { font-family: var(--font-display); font-size: var(--text-xl); line-height: 1.1; margin: .5rem 0 .85rem; font-weight: 600; text-wrap: balance; }
  .lead { max-width: 56rem; padding: clamp(2rem, 5vw, 4rem) 0 2.5rem; }
  .lead h2 { font-size: var(--text-2xl); letter-spacing: -.015em; }
  p { line-height: 1.65; text-wrap: pretty; }
  .lead p { color: var(--text-muted); font-size: var(--text-base); max-width: 62ch; margin-bottom: 0; }
  .eyebrow { color: var(--accent-text); text-transform: uppercase; font-size: var(--text-xs); letter-spacing: .16em; }
  .metric-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); border-block: 1px solid var(--border); margin: 0 0 2.5rem; padding: 1.4rem 0; gap: 1.25rem; }
  .metric-strip dt { font-size: var(--text-sm); color: var(--text-muted); }
  .metric-strip dd { font-family: var(--font-display); font-weight: 600; font-size: var(--text-2xl); margin: .2rem 0; font-variant-numeric: tabular-nums; line-height: 1.2; }
  .metric-strip dd small { font-family: var(--font-sans); font-size: var(--text-xs); color: var(--text-muted); }
  .metric-strip p { margin: 0; font-size: var(--text-xs); color: var(--text-muted); max-width: 24ch; }
  .metric-strip p.positive { color: var(--accent-text); }
  .section-heading { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: .6rem; }
  .section-heading > span { font-size: var(--text-xs); color: var(--text-muted); }
  .activity-chart { padding: .6rem 0 2.4rem; }
  .chart { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: clamp(.5rem, 2vw, 1.5rem); margin-top: 1.2rem; }
  .chart-column { min-width: 0; }
  .bar-space { height: 11rem; display: flex; align-items: end; border-bottom: 1px solid var(--border); }
  .bar { width: 100%; height: var(--bar-height); min-height: 2px; background: var(--accent); border-radius: 3px 3px 0 0; opacity: .8; }
  .bar:hover { opacity: 1; }
  .bar.gap { background: transparent; border-top: 2px dashed var(--text-muted); }
  .chart-label, .chart-value { display: block; font-size: var(--text-xs); margin-top: .5rem; overflow-wrap: anywhere; }
  .chart-value { color: var(--text-muted); font-size: var(--text-xs); }
  .chart-data { margin-top: 1rem; font-size: var(--text-sm); }
  .caption { font-size: var(--text-sm); color: var(--text-muted); margin-top: .5rem; }
  .edition-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(13rem, .38fr); gap: clamp(2rem, 5vw, 5rem); border-top: 1px solid var(--border); padding-top: 2rem; }
  .stories { min-width: 0; }
  .story, .detail-section { padding: 1rem 0 1.7rem; border-bottom: 1px solid var(--border); }
  .story p { max-width: 65ch; color: var(--text-muted); }
  .detail-rows { margin: 1.1rem 0 0; }
  .detail-rows > div { display: flex; justify-content: space-between; gap: 1.5rem; padding: .7rem 0; border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent); }
  .detail-rows dt { max-width: 64ch; overflow-wrap: anywhere; }
  .detail-rows dd { margin: 0; font-family: var(--font-mono); font-size: var(--text-sm); text-align: right; flex-shrink: 0; }
  .edition-aside { padding-top: .9rem; }
  .edition-aside > p { font-size: var(--text-sm); color: var(--text-muted); }
  .report-action { display: flex; justify-content: space-between; padding: .9rem 0; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); transition: color 150ms; }
  .report-action:hover { color: var(--accent-text); }
  .report-action:focus-visible, .share:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 5px; }
  .share { font: inherit; font-size: var(--text-sm); margin-top: 1.3rem; padding: .7rem 1rem; color: var(--accent-text); background: transparent; border: 1px solid var(--border); cursor: pointer; }
  .copy-note { display: block; min-height: 1.5rem; font-size: var(--text-xs); padding-top: .5rem; }
  .edition-aside .archive-note { font-size: var(--text-xs); margin-top: 1.5rem; }
  .station-section { margin-top: 3rem; }
  .table-scroll { max-width: 100%; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); text-align: left; }
  th, td { padding: 1rem .7rem; border-bottom: 1px solid var(--border); }
  thead th { color: var(--text-muted); font-size: var(--text-xs); font-weight: 500; }
  tbody th { font-weight: 500; }
  td.tier { font-size: var(--text-lg); color: var(--accent-text); }
  td small { display: block; color: var(--text-muted); margin-top: .2rem; }
  .quality { margin-top: 2.5rem; padding-block: 1.4rem; border-block: 1px solid var(--border); font-size: var(--text-sm); color: var(--text-muted); }
  summary { cursor: pointer; color: var(--text); }
  .quality li { margin-bottom: .5rem; max-width: 75ch; line-height: 1.6; }
  .preview { padding: 1.5rem; border: 1px dashed var(--accent); }
  @media (max-width: 650px) { .edition-columns { grid-template-columns: 1fr; gap: 1.25rem; } .metric-strip { grid-template-columns: repeat(2,minmax(0,1fr)); } .lead { padding-top: 2rem; } .bar-space { height: 8rem; } .preview { padding: .75rem; } th, td { padding: .8rem .4rem; min-width: 5rem; } .detail-rows > div { flex-wrap: wrap; gap: .4rem; } .detail-rows dd { flex-shrink: 1; } }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
