<script>
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { auth } from '$lib/auth.svelte.js';
  import { loadReportArchive, previewReport, reportDate, metricValue } from '$lib/reports.js';
  import ReportArticle from '$lib/components/reports/ReportArticle.svelte';
  let period = $state('all');
  let editions = $state([]);
  let cursor = $state(null);
  let status = $state('loading');
  let moreBusy = $state(false);
  let failure = $state('');
  let controller;
  let sequence = 0;
  let preview = $state(null);
  let previewBusy = $state(false);
  let previewError = $state('');
  const isAdmin = $derived(auth.status === 'authed' && auth.roles.includes('admin'));
  const latest = $derived(editions[0] || null);
  async function read(reset = true) {
    controller?.abort();controller = new AbortController();
    const request = ++sequence;
    if (reset) { status = 'loading'; editions = []; cursor = null; } else moreBusy = true;
    failure = '';
    try {
      const data = await loadReportArchive(period, reset ? null : cursor, controller.signal);
      if (request !== sequence) return;
      const known = new Set(editions.map((e) => e.id));
      editions = reset ? data.editions : [...editions, ...data.editions.filter((e) => !known.has(e.id))];
      cursor = data.next_cursor;status = 'ready';
    } catch (error) {
      if (request !== sequence || error?.name === 'AbortError') return;
      failure = error?.status === 404 ? 'Reports are not available yet.' : 'The archive could not be read. Please try again.';
      if (reset) status = 'error';
    } finally { if (request === sequence) moreBusy = false; }
  }
  function select(value) { if (period === value) return; period = value; read(); }
  async function showPreview(value) {
    previewBusy = true; previewError = '';
    try { preview = await previewReport(value); }
    catch { previewError = 'The edition could not be prepared. Nothing was published.'; }
    finally { previewBusy = false; }
  }
  onMount(() => { read(); return () => { sequence += 1; controller?.abort(); }; });
</script>

<svelte:head><title>Reports | Last Sietch</title><meta name="description" content="The daily Dispatch and Sunday Chronicle from Last Sietch. Explore dated reports, community highlights and recorded activity." /></svelte:head>

<div class="reports-page">
  <header class="archive-header"><div><p class="eyebrow mono">Last Sietch / Public archive</p><h1>The Sietch,<br /><span>in its own time.</span></h1><p class="intro">Daily dispatches and Sunday chronicles. The stories, records and numbers behind the sands, preserved as each edition was published.</p></div><div class="publication-note"><span class="mono">Reports</span><strong>A record worth keeping.</strong><p>Every edition has its own date, reporting window and permanent link.</p></div></header>

  <div class="archive-tools"><div role="group" aria-label="Filter report editions"><button type="button" class:selected={period === 'all'} aria-pressed={period === 'all'} onclick={() => select('all')}>All editions</button><button type="button" class:selected={period === 'daily'} aria-pressed={period === 'daily'} onclick={() => select('daily')}>Daily Dispatch</button><button type="button" class:selected={period === 'weekly'} aria-pressed={period === 'weekly'} onclick={() => select('weekly')}>Sunday Chronicle</button></div><span class="mono">Eastern time</span></div>

  {#if status === 'loading'}
    <section class="skeleton" aria-label="Loading report editions" aria-busy="true"><div></div><div></div><div></div></section>
  {:else if status === 'error'}
    <section class="state-panel" role="status"><p class="eyebrow mono">Archive unavailable</p><h2>{failure}</h2><button type="button" onclick={() => read()}>Try again</button></section>
  {:else if !editions.length}
    <section class="state-panel"><p class="eyebrow mono">The archive begins here</p><h2>No {period === 'all' ? '' : period + ' '}editions have been published yet.</h2><p>Published reports will appear here with their original figures and reporting dates.</p><a href={`${base}/events`}>See what is coming up in the Sietch ↗</a></section>
  {:else}
    {#if latest}
      <a class="lead-edition" href={`${base}/reports/${latest.id}`}><div class="edition-date"><span class="eyebrow mono">Latest {latest.period === 'weekly' ? 'Chronicle' : 'Dispatch'}</span><time datetime={latest.date}>{reportDate(latest.date)}</time><span class="arrow" aria-hidden="true">↗</span></div><div><p class="edition-title">{latest.title}</p><h2>{latest.headline}</h2><p>{latest.summary}</p><div class="mini-metrics">{#each latest.metrics.slice(0,3) as metric (metric.key)}<span><strong>{metricValue(metric)}</strong> {metric.label}</span>{/each}</div><span class="read-link">Read the full edition <span aria-hidden="true">→</span></span></div></a>
    {/if}
    {#if editions.length > 1}<section class="older-editions" aria-label="Earlier editions">{#each editions.slice(1) as edition (edition.id)}<a href={`${base}/reports/${edition.id}`} class="edition-row"><div><span class="eyebrow mono">{edition.period === 'weekly' ? 'Sunday Chronicle' : 'Daily Dispatch'}</span><time datetime={edition.date}>{reportDate(edition.date)}</time></div><div><h2>{edition.headline}</h2><p>{edition.summary}</p></div><span class="row-arrow" aria-hidden="true">↗</span></a>{/each}</section>{/if}
    {#if failure}<p class="load-error" role="status">{failure}</p>{/if}
    {#if cursor != null}<div class="load-more"><button type="button" disabled={moreBusy} onclick={() => read(false)}>{moreBusy ? 'Reading earlier editions…' : 'Earlier editions'}</button></div>{/if}
  {/if}

  {#if isAdmin}
    <section class="preview-tools"><div><p class="eyebrow mono">Editorial desk</p><h2>Preview an edition</h2><p>Previewing does not publish, create an archive entry or consume record markers.</p></div><div class="preview-buttons"><button type="button" disabled={previewBusy} onclick={() => showPreview('daily')}>Preview daily</button><button type="button" disabled={previewBusy} onclick={() => showPreview('weekly')}>Preview weekly</button></div></section>
    {#if previewError}<p role="status">{previewError}</p>{/if}
    {#if previewBusy}<p role="status">Preparing the available observations…</p>{/if}
    {#if preview}<div class="preview-head"><span class="mono">Private preview</span><button type="button" onclick={() => { preview = null; }}>Close preview</button></div><ReportArticle edition={preview} preview={true} />{/if}
  {/if}
</div>

<style>
  .reports-page { max-width: 78rem; margin: 0 auto; padding: var(--space-5) var(--space-4) 3rem; }
  .archive-header { display: grid; grid-template-columns: minmax(0,1fr) 15rem; gap: 3rem; align-items: end; padding: 1.5rem 0 3rem; }
  .eyebrow { color: var(--accent-text); letter-spacing: .15em; text-transform: uppercase; font-size: var(--text-xs); margin: 0 0 1rem; }
  h1 { font-family: var(--font-display); font-weight: 700; font-size: clamp(2rem,5.2vw,3.4rem); line-height: 1.02; letter-spacing: .01em; margin: 0 0 1.5rem; }
  h1 span { color: var(--text-muted); }
  .intro { max-width: 56ch; line-height: 1.7; color: var(--text-muted); font-size: var(--text-base); margin: 0; text-wrap: pretty; }
  .publication-note { border-top: 2px solid var(--accent); padding-top: 1rem; }
  .publication-note span { text-transform: uppercase; letter-spacing: .14em; font-size: var(--text-xs); color: var(--text-muted); }
  .publication-note strong { display: block; font-family: var(--font-display); font-size: var(--text-xl); margin-top: .65rem; }
  .publication-note p { color: var(--text-muted); font-size: var(--text-sm); line-height: 1.6; }
  .archive-tools { display: flex; align-items: center; justify-content: space-between; gap: 1rem; border-block: 1px solid var(--border); padding-block: .9rem; }
  .archive-tools > div { display: flex; gap: .3rem; flex-wrap: wrap; }
  button { color: var(--text-muted); background: transparent; border: 1px solid var(--border); border-radius: 3px; font: inherit; font-size: var(--text-sm); padding: .7rem 1rem; cursor: pointer; transition: color 150ms, border-color 150ms, background 150ms; }
  .archive-tools button { border-color: transparent; }
  button:hover, button.selected { color: var(--accent-text); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
  button:disabled { opacity: .5; cursor: wait; }
  button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 5px; }
  .archive-tools > span { color: var(--text-muted); font-size: var(--text-xs); }
  .lead-edition { display: grid; grid-template-columns: 12rem minmax(0,1fr); gap: 3rem; color: var(--text); text-decoration: none; padding: 2.5rem 0; border-bottom: 1px solid var(--border-strong); }
  .edition-date { display: flex; flex-direction: column; gap: 1rem; }
  .edition-date time { max-width: 9ch; font-family: var(--font-display); font-weight: 600; font-size: var(--text-xl); line-height: 1.05; }
  .edition-date .eyebrow { margin: 0; }
  .arrow { color: var(--accent-text); font-size: var(--text-xl); }
  .edition-title { font-size: var(--text-sm); color: var(--text-muted); margin: 0 0 .6rem; }
  .lead-edition h2 { font-family: var(--font-display); font-size: var(--text-2xl); font-weight: 600; line-height: 1.08; margin: 0 0 1rem; text-wrap: balance; }
  .lead-edition p { max-width: 62ch; color: var(--text-muted); line-height: 1.7; }
  .mini-metrics { display: flex; flex-wrap: wrap; gap: .9rem 1.8rem; margin: 1.5rem 0; color: var(--text-muted); font-size: var(--text-xs); }
  .mini-metrics strong { display: block; font-size: var(--text-xl); font-family: var(--font-display); font-variant-numeric: tabular-nums; color: var(--text); margin-bottom: .2rem; }
  .read-link { color: var(--accent-text); font-size: var(--text-sm); display: flex; gap: .7rem; align-items: center; }
  .lead-edition:hover h2, .edition-row:hover h2 { color: var(--accent-text); }
  .edition-row { display: grid; grid-template-columns: 12rem minmax(0,1fr) 1.5rem; gap: 3rem; padding: 1.8rem 0; border-bottom: 1px solid var(--border); color: var(--text); text-decoration: none; }
  .edition-row .eyebrow { display: block; font-size: var(--text-xs); margin-bottom: .5rem; }
  .edition-row time { color: var(--text-muted); font-size: var(--text-xs); }
  .edition-row h2 { margin: 0 0 .5rem; font-family: var(--font-display); font-size: var(--text-xl); line-height: 1.2; font-weight: 600; }
  .edition-row p { margin: 0; max-width: 68ch; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.65; }
  .row-arrow { color: var(--text-muted); }
  .load-more { text-align: center; padding: 2rem; }
  .state-panel { padding: 3rem 0; min-height: 17rem; max-width: 44rem; }
  .state-panel h2 { font-family: var(--font-display); font-size: var(--text-xl); line-height: 1.15; }
  .state-panel p { line-height: 1.7; color: var(--text-muted); }
  .state-panel a { color: var(--accent-text); }
  .skeleton { padding-block: 2rem; }
  .skeleton div { background: var(--bg-elevated); height: 3rem; margin-block: 1rem; animation: breathing 1.6s ease-in-out infinite alternate; }
  .skeleton div:nth-child(2) { width: 75%; height: 6rem; }
  .skeleton div:nth-child(3) { width: 45%; height: 1.5rem; }
  .preview-tools { display: flex; justify-content: space-between; gap: 1.5rem; align-items: center; margin-top: 3rem; padding-top: 2rem; border-top: 1px solid var(--border); }
  .preview-tools h2 { font-family: var(--font-display); font-size: var(--text-xl); margin: 0; }
  .preview-tools p:not(.eyebrow) { color: var(--text-muted); font-size: var(--text-sm); max-width: 54ch; line-height: 1.65; }
  .preview-buttons { display: flex; gap: .5rem; flex-shrink: 0; }
  .preview-head { display: flex; justify-content: space-between; align-items: center; margin: 1.3rem 0; color: var(--text-muted); font-size: var(--text-xs); }
  .load-error { color: var(--text-muted); }
  @keyframes breathing { from { opacity: .45; } to { opacity: .9; } }
  @media (max-width: 700px) { .archive-header { grid-template-columns: 1fr; gap: 1.5rem; padding-top: .5rem; } .publication-note { display: none; } .archive-tools { align-items: start; } .archive-tools > span { display: none; } .archive-tools button { padding: .6rem .7rem; font-size: var(--text-xs); } .lead-edition { grid-template-columns: 1fr; gap: 1.3rem; padding: 1.6rem 0; } .edition-date { flex-direction: row; flex-wrap: wrap; align-items: center; gap: .8rem; } .edition-date time { font-size: var(--text-base); max-width: none; } .arrow { display: none; } .edition-row { grid-template-columns: 1fr 1.2rem; gap: .8rem; } .edition-row > div:first-child { grid-column: 1 / -1; display: flex; gap: 1rem; flex-wrap: wrap; } .edition-row > div:first-child .eyebrow { margin: 0; } .preview-tools { align-items: start; flex-direction: column; } }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>
