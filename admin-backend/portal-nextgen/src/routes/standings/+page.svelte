<script>
  // Standings: the Top 10 by level (then XP) and the level spread across every
  // character. Public, no gate. Moved here from lastsietch.com/dune (owner ruling
  // 2026-09-30). /api/dune/standings leaves the sietch crew out server-side and
  // carries no online flag and no exact XP, and refreshes hourly, so this page
  // can't be used to see who is in game.
  // One read on mount: the snapshot upstream refreshes about every ten minutes.
  import { onMount } from 'svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import TitleChip from '$lib/components/TitleChip.svelte';
  import { api } from '$lib/api.js';

  let status = $state('loading'); // 'loading' | 'ready' | 'error'
  let payload = $state(null);

  let top = $derived(Array.isArray(payload?.top) ? payload.top : []);
  let buckets = $derived(Array.isArray(payload?.levels?.buckets) ? payload.levels.buckets : []);
  let peak = $derived(buckets.length ? Math.max(1, ...buckets) : 1);

  onMount(async () => {
    try {
      payload = await api.server.standings();
      status = payload?.available ? 'ready' : 'error';
    } catch (e) {
      status = 'error';
    }
  });
</script>

<svelte:head>
  <title>Standings | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | standings"
    title="Standings"
    sub="The ten highest characters on the server, by level and then experience. Sietch crew are left off so the board belongs to the community. Updated hourly."
  />

  {#if status === 'loading'}
    <p class="skeleton mono">reading the standings</p>
  {:else if status === 'error'}
    <Notice tone="warn" text="The standings feed is not answering right now. Try again in a few minutes." />
  {:else}
    <div class="grid">
      <CarvedSlab>
        <p class="kicker mono">Top 10</p>
        <ol class="board" role="list">
          {#each top as row (row.rank)}
            <li class="entry" class:crown={row.rank === 1}>
              <span class="rank mono">{row.rank}</span>
              <span class="name">{row.name}<TitleChip title={row.title} /></span>
              <span class="lvl mono">L{row.lvl}</span>
            </li>
          {/each}
        </ol>
      </CarvedSlab>

      <CarvedSlab>
        <p class="kicker mono">Level spread</p>
        <p class="lead"><b>{payload.levels.count}</b> characters · <b>{payload.levels.at_cap}</b> at level 200</p>
        <div class="bars" role="img" aria-label="Characters per 10 levels, from level 1 to 200">
          {#each buckets as n, i (i)}
            <div class="bar" style="--h: {Math.round((n / peak) * 100)}%" title="Levels {i * 10 + 1} to {i * 10 + 10}: {n}">
              <span class="n mono">{n || ''}</span>
            </div>
          {/each}
        </div>
        <div class="axis mono"><span>L1</span><span>L100</span><span>L200</span></div>
      </CarvedSlab>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .grid { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: var(--space-5); align-items: start; }
  .kicker { margin: 0 0 var(--space-3); font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase; color: var(--accent-text); }

  .board { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .entry {
    display: grid; grid-template-columns: 2.2rem minmax(0, 1fr) auto; align-items: baseline;
    gap: var(--space-3); padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .entry.crown { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent-glow) inset; }
  .rank { font-family: var(--font-display); font-weight: 700; font-size: var(--text-lg); color: var(--accent-bright); }
  .name { font-size: var(--text-base); color: var(--text); min-width: 0; overflow-wrap: anywhere; }
  .lvl { font-size: var(--text-sm); color: var(--accent-text); font-variant-numeric: tabular-nums; }

  .lead { margin: 0 0 var(--space-4); color: var(--text-muted); font-size: var(--text-sm); }
  .lead b { color: var(--accent-bright); font-family: var(--font-display); font-size: var(--text-xl); }
  .bars { display: grid; grid-template-columns: repeat(20, minmax(0, 1fr)); gap: 3px; height: 180px; align-items: end; }
  .bar {
    position: relative; height: var(--h); min-height: 2px;
    background: linear-gradient(180deg, var(--accent-bright), var(--accent-soft));
    border-radius: 2px 2px 0 0;
  }
  .n { position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); padding-bottom: 2px; font-size: 10px; color: var(--text-muted); }
  .axis { display: flex; justify-content: space-between; margin-top: var(--space-2); font-size: var(--text-xs); color: var(--text-muted); }

  @media (max-width: 52rem) {
    .grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 30rem) {
    .entry { grid-template-columns: 2rem minmax(0, 1fr) auto; }
  }
</style>
