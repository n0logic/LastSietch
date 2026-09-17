<script>
  import { onDestroy, untrack } from 'svelte';
  import { api } from '$lib/api.js';
  import { auth } from '$lib/auth.svelte.js';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { getPref, setPref, charScope, prefs } from '$lib/prefs.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  const gate = useAuthGate();
  let items = $state([]);
  let unavailable = $state([]);
  let status = $state('loading');
  let since = $state(null);
  let filter = $state('all');
  let recentOnly = $state(true);
  let limited = $state(false);
  let receipt = null;
  let scope = '';
  let owner = '';
  let seq = 0;
  const TYPES = [{ key: 'all', label: 'All activity' }, { key: 'sale', label: 'Sales' },
    { key: 'delivery', label: 'Deliveries' }, { key: 'reward', label: 'Rewards' }, { key: 'alert', label: 'Price alerts' }];
  let shown = $derived(items.filter((row) => (filter === 'all' || row.kind === filter)
    && (!recentOnly || !since || Date.parse(row.t) > Date.parse(since))));
  let newCount = $derived(since ? items.filter((row) => Date.parse(row.t) > Date.parse(since)).length : null);

  async function load() {
    const token = ++seq;
    status = 'loading';
    receipt = null;
    try {
      const result = await api.home.activity();
      if (token !== seq || !gate.authed) return;
      if (!Array.isArray(result?.items) || !Array.isArray(result?.unavailable)) throw new Error('Invalid activity');
      items = result.items;
      unavailable = result.unavailable;
      limited = result.has_more === true;
      if (!unavailable.length && !limited) receipt = result.as_of;
      status = 'ready';
    } catch (e) {
      if (token !== seq) return;
      items = [];
      status = 'error';
    }
  }

  $effect(() => {
    const identity = gate.authed ? `${auth.discordHandle}:${charScope()}` : '';
    if (identity && ['ready', 'anon'].includes(prefs.status) && identity !== owner) {
      owner = identity;
      scope = charScope();
      since = prefs.status === 'ready' ? getPref('activity_seen', { scope }) || null : null;
      untrack(load);
    } else if (!identity) {
      owner = '';
      seq += 1;
      items = [];
      receipt = null;
    }
  });

  onDestroy(() => {
    seq += 1;
    if (receipt && owner === `${auth.discordHandle}:${charScope()}` && gate.authed && prefs.status === 'ready') {
      setPref('activity_seen', receipt, { scope });
    }
  });

  function when(stamp) {
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(stamp));
  }
</script>

<svelte:head><title>Activity | Last Sietch</title></svelte:head>

<div class="page">
  <PageHeader kicker="Last Sietch | your record" title={since ? 'Since your last visit' : 'Your recent activity'}
    sub="Sales, rewards, deliveries and price alerts for your selected account. Each entry takes you back to the place it happened." />
  {#if gate.loading || (gate.authed && status === 'loading')}
    <div class="loading" aria-label="Loading activity">{#each Array(4) as _}<div class="skeleton"></div>{/each}</div>
  {:else if gate.anon}
    <SealedPanel status="empty" action="login" emptyText="Sign in to see your own activity." />
  {:else if status === 'error'}
    <SealedPanel status="error" action="none" errorText="Your activity could not be read. Your previous visit has been kept." />
    <button class="control" type="button" onclick={load}>Try again</button>
  {:else}
    <div class="toolbar">
      <div class="filters" role="group" aria-label="Activity type">{#each TYPES as type}<button class="control" class:on={filter === type.key} type="button" aria-pressed={filter === type.key} onclick={() => (filter = type.key)}>{type.label}</button>{/each}</div>
      {#if since}<label class="toggle"><input type="checkbox" bind:checked={recentOnly} /> Since last visit{#if newCount != null} ({newCount}){/if}</label>{/if}
    </div>
    {#if unavailable.length}<p class="notice" role="status">Could not read: {unavailable.join(', ')}. This is a partial history; your previous visit will be kept.</p>{/if}
    {#if limited}<p class="notice">Showing the newest 100 records. Your previous visit will be kept so older activity is not marked viewed.</p>{/if}
    {#if shown.length}
      <ol class="timeline">
        {#each shown as row, i (`${row.t}:${row.kind}:${i}`)}
          <li>
            <time datetime={row.t}>{when(row.t)}</time>
            <span class="kind">{TYPES.find((type) => type.key === row.kind)?.label || 'Account'}</span>
            <a href={row.href}><strong>{row.summary}</strong>{#if row.detail}<small>{row.detail}</small>{/if}</a>
            <span class="arrow" aria-hidden="true">↗</span>
          </li>
        {/each}
      </ol>
    {:else}
      <div class="empty"><img src="/img/v3/empty-states/no-alerts.jpg" alt="" /><h2>{unavailable.length ? 'No entries from the sources that answered' : since && recentOnly ? 'You are caught up' : 'No activity in this view'}</h2><p>{since && recentOnly ? 'Switch off the last-visit filter to browse earlier records.' : 'Recorded sales, claims and deliveries will appear here.'}</p></div>
    {/if}
  {/if}
</div>

<style>
  .page { max-width: 1120px; margin: 0 auto; padding: 32px 24px 64px; }
  .toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .control { min-height: 44px; padding: 8px 14px; font: inherit; font-size: 14px; color: var(--text-muted); border: 1px solid var(--edge); border-radius: 4px; background: var(--bg-elevated); cursor: pointer; }
  .control.on { border-color: var(--accent-soft); color: var(--accent-text); }
  .toggle { display: flex; gap: 8px; align-items: center; font-size: 13px; color: var(--text-muted); }
  input { accent-color: var(--accent); }
  .notice { color: var(--text-muted); border-left: 2px solid var(--accent-soft); padding: 10px 14px; font-size: 14px; }
  .timeline { list-style: none; margin: 0; padding: 0; }
  li { display: grid; grid-template-columns: 160px 94px minmax(0, 1fr) 20px; gap: 20px; padding: 22px 0; align-items: baseline; border-top: 1px solid var(--edge); }
  time { color: var(--text-muted); font: 400 12px/1.7 var(--font-mono); }
  .kind { font-size: 12px; color: var(--text-muted); }
  a { color: var(--text); text-decoration: none; }
  a:hover { color: var(--accent-text); }
  strong { display: block; font-weight: 500; }
  small { display: block; margin-top: 4px; font-size: 13px; color: var(--text-muted); }
  .arrow { color: var(--accent-text); }
  .empty { padding: 32px; text-align: center; }
  .empty img { width: 120px; height: 90px; object-fit: cover; border-radius: 6px; opacity: .65; }
  .empty p { color: var(--text-muted); font-size: 14px; }
  .loading { display: grid; gap: 16px; } .loading .skeleton { width: 100%; height: 64px; }
  @media (max-width: 650px) { .page { padding: 24px 16px 40px; } li { grid-template-columns: 1fr auto; gap: 8px; } li a { grid-column: 1 / -1; grid-row: 2; } .arrow { display: none; } }
</style>
