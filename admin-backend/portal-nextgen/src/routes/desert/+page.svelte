<script>
  // Desert hub: the landing page the "Desert" nav label now points at. Public, no
  // gate. It answers one question before you pick a board: where is everybody, and
  // is the map you want warm.
  //
  // The traffic table is the whole point; the map links, the 24h pulse and the
  // Landsraad teaser are the three onward paths, in that order. Nothing here is a
  // second copy of a read: the board owns its own store and its own poll, and the
  // pulse and standings come straight off the same public endpoints the dashboard
  // uses.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ServerSchedule from '$lib/components/ServerSchedule.svelte';
  import WorldPulse from '$lib/components/WorldPulse.svelte';
  import LandsraadStanding from '$lib/components/LandsraadStanding.svelte';
  import TrafficBoard from '$lib/components/desert/TrafficBoard.svelte';
  import { api } from '$lib/api.js';

  // KEEP IN SYNC with routes/maps/+page.svelte BOARDS. The maps hub owns the
  // cards (art, blurbs, live counts); this is the link row, so only what a link
  // needs is here.
  const BOARDS = [
    { key: 'deep-desert', inst: 'pve', name: 'Deep Desert', chip: 'PvE' },
    { key: 'deep-desert', inst: 'pvp', name: 'Deep Desert', chip: 'PvP' },
    { key: 'hagga', inst: 'habbanya', name: 'Habbanya', chip: 'PvE' },
    { key: 'hagga', inst: 'kulon', name: 'Kulon', chip: 'PvP' },
    { key: 'hagga', inst: 'amtal', name: 'Amtal', chip: 'Full PvP' },
    { key: 'arrakeen', inst: null, name: 'Arrakeen', chip: 'Social hub' },
    { key: 'harko-village', inst: null, name: 'Harko Village', chip: 'Social hub' },
  ];

  let overview = $state(null);
  let standings = $state(null);

  onMount(() => {
    loadOverview();
    loadStandings();
    const a = setInterval(loadOverview, 30_000);
    const b = setInterval(loadStandings, 60_000);
    return () => { clearInterval(a); clearInterval(b); };
  });

  async function loadOverview() {
    try { overview = await api.server.overview(); } catch (e) {}
  }
  async function loadStandings() {
    try { standings = await api.landsraad.standings(); } catch (e) {}
  }

  let world = $derived(overview?.world ?? null);

  // Same href shape the maps hub builds, so a link from here lands on exactly the
  // board that hub's card would have opened.
  function boardHref(b) {
    return `${base}/maps/${b.key}${b.inst ? `?inst=${b.inst}` : ''}`;
  }
</script>

<svelte:head>
  <title>Desert | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | the sand"
    title="Desert"
    sub="Where the sietch is right now: every world we run, who is on it, and which ones are warm. Pick a board, read the rules, or watch the term."
  />

  <ServerSchedule />

  <TrafficBoard />

  <h2 class="section-label mono">Boards</h2>
  <div class="boards">
    {#each BOARDS as b (b.key + (b.inst ?? ''))}
      <a class="board" href={boardHref(b)}>
        <span class="board-name">{b.name}</span>
        <span class="board-chip mono">{b.chip}</span>
      </a>
    {/each}
  </div>

  <h2 class="section-label mono">World pulse | 24h</h2>
  <div class="wide">
    <CarvedSlab live sharp>
      <WorldPulse {world} />
    </CarvedSlab>
  </div>

  <h2 class="section-label mono">Landsraad | this term</h2>
  <div class="wide">
    <CarvedSlab live sharp>
      <LandsraadStanding {standings} />
      <p class="card-link mono"><a href={`${base}/landsraad`}>The full term board</a></p>
    </CarvedSlab>
  </div>

  <p class="links mono">
    <a href={`${base}/rules`}>Server rules</a>
    <span class="sep" aria-hidden="true">·</span>
    <a href={`${base}/maps`}>All maps</a>
  </p>
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .section-label {
    margin: var(--space-7) 0 var(--space-3); font-size: var(--text-xs);
    letter-spacing: .28em; text-transform: uppercase; color: var(--text-muted); opacity: .8;
  }
  .wide { display: grid; grid-template-columns: 1fr; }

  /* Link row, not cards: the maps hub owns the board art, and repeating it here
     would make this page a second maps hub instead of a way into one. */
  .boards { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .board {
    display: inline-flex; align-items: baseline; gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    text-decoration: none; color: var(--text);
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .board:hover { border-color: var(--accent); color: var(--accent-bright); }
  .board-name { font-size: var(--text-sm); letter-spacing: .04em; }
  .board-chip { font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; color: var(--text-muted); }

  .card-link { margin: var(--space-3) 0 0; font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; }
  .card-link a { color: var(--text-muted); text-decoration: none; transition: color var(--motion-fast) var(--ease-out); }
  .card-link a:hover { color: var(--accent); }

  .links {
    margin: var(--space-7) 0 0; display: flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
  }
  .links a { color: var(--text-muted); text-decoration: none; transition: color var(--motion-fast) var(--ease-out); }
  .links a:hover { color: var(--accent); }
  .sep { color: var(--text-muted); opacity: .5; }
</style>
