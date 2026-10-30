<script>
  // What /desert carried that the Maps landing did not (parity audit G1-f): the
  // 24h world pulse and the Landsraad term snippet. The schedule is already on
  // the Maps hub. Same public endpoints and cadence as the Desert hub.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import WorldPulse from '$lib/components/WorldPulse.svelte';
  import LandsraadStanding from '$lib/components/LandsraadStanding.svelte';
  import { api } from '$lib/api.js';

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
</script>

<section class="now" aria-label="The desert right now">
  <div>
    <h2 class="label mono">World pulse | 24h</h2>
    <CarvedSlab live sharp>
      <WorldPulse {world} />
    </CarvedSlab>
  </div>
  <div>
    <h2 class="label mono">Landsraad | this term</h2>
    <CarvedSlab live sharp>
      <LandsraadStanding {standings} />
      <p class="more mono"><a href={`${base}/landsraad`}>The full term board</a></p>
    </CarvedSlab>
  </div>
</section>

<style>
  .now {
    max-width: var(--content-max); margin: 0 auto; padding: 0 var(--space-4) var(--space-8);
    display: grid; gap: var(--space-4); grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); align-items: start;
  }
  .label { margin: 0 0 var(--space-3); font-size: var(--text-xs); letter-spacing: .28em; text-transform: uppercase; color: var(--text-muted); }
  .more { margin: var(--space-3) 0 0; font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; }
  .more a { color: var(--text-muted); text-decoration: none; }
  .more a:hover { color: var(--accent); }
</style>
