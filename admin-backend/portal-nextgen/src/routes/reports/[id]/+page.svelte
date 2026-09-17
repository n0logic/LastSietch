<script>
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { loadReport, reportDate } from '$lib/reports.js';
  import ReportArticle from '$lib/components/reports/ReportArticle.svelte';
  let edition = $state(null);
  let state = $state('loading');
  let error = $state('');
  let retry = $state(0);
  $effect(() => {
    const id = $page.params.id;
    retry;
    const controller = new AbortController();
    let alive = true;
    edition = null;state = 'loading';
    loadReport(id, controller.signal).then((value) => { if (alive) { edition = value; state = 'ready'; } })
      .catch((cause) => { if (!alive || cause?.name === 'AbortError') return; state = cause?.status === 404 ? 'missing' : 'error'; error = state === 'missing' ? 'This edition is not available.' : 'This report could not be read right now.'; });
    return () => { alive = false; controller.abort(); };
  });
</script>

<svelte:head><title>{edition ? `${edition.title} · ${reportDate(edition.date)}` : 'Report'} | Last Sietch</title><meta name="description" content={edition?.summary || 'A dated edition from the Last Sietch report archive.'} /></svelte:head>
<div class="report-page"><a class="back" href={`${base}/reports`}>← All reports</a>
  {#if state === 'ready' && edition}<ReportArticle {edition} />
  {:else if state === 'loading'}<div class="loading" role="status" aria-busy="true"><span class="mono">Reading the edition</span><div></div><div></div></div>
  {:else}<section class="error" role="status"><p class="mono">Last Sietch / Reports</p><h1>{error}</h1><p>Published editions keep their original address. You can return to the archive to find another report.</p>{#if state === 'error'}<button type="button" onclick={() => retry += 1}>Try again</button>{/if}</section>{/if}
</div>
<style>
  .report-page { max-width: 78rem; margin: 0 auto; padding: var(--space-5) var(--space-4) 3rem; }
  .back { display: inline-block; margin: 0 0 1.5rem; color: var(--text-muted); font-size: var(--text-sm); text-decoration: none; }
  .back:hover { color: var(--accent-text); }
  .back:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 5px; }
  .loading { padding: 2rem 0; color: var(--text-muted); min-height: 25rem; }
  .loading div { height: 5rem; background: var(--bg-elevated); margin-top: 1.5rem; }
  .loading div:last-child { width: 70%; height: 9rem; }
  .error { padding: 3rem 0; max-width: 42rem; }
  .error > .mono { color: var(--accent-text); font-size: var(--text-xs); }
  h1 { font-family: var(--font-display); font-size: clamp(2rem, 5.2vw, 3.4rem); line-height: 1.02; }
  p { color: var(--text-muted); line-height: 1.7; }
  button { font: inherit; font-size: var(--text-sm); background: transparent; border: 1px solid var(--border); color: var(--accent-text); padding: .75rem 1rem; cursor: pointer; }
</style>
