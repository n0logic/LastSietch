<script>
  // Sietch > Sietches > Habbanya: a short first guide to our PvE sietch. House
  // policy, typed here like the Amtal page; the live rates stay on the Rules page,
  // which reads them off the server, so this never quotes a number that can drift
  // apart from it. The community board comes later.
  import { base } from '$app/paths';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';

  const HOW_IT_PLAYS = [
    ['PvE', 'Habbanya is the Hagga Basin as a PvE world. Build, gather, craft and run the story at your own pace. For PvP, Amtal is the other sietch.'],
    ['Your base', 'Keep your generators fueled. If you stop playing for 30 days you get a notice in your portal mailbox and on Discord, then 14 days of grace. After that the base is saved to your Base Vault and cleared from the map. A returning owner can ask staff about restoring it.'],
    ['Rates', 'Habbanya runs richer gathering than stock. The current values are on the Rules page, read live from the server.'],
    ['Fair play', 'No cheating, no exploits and no griefing. The full code is on the Rules page.'],
  ];
</script>

<PageHeader
  kicker="Last Sietch | Sietches"
  title="Habbanya"
  sub="Our PvE sietch on the Hagga Basin: the home most of the sietch builds in."
/>

<section class="plays" aria-labelledby="hb-plays">
  <h2 id="hb-plays" class="kicker mono">How Habbanya plays</h2>
  <dl class="plist">
    {#each HOW_IT_PLAYS as [term, text] (term)}
      <dt>{term}</dt>
      <dd>{text}</dd>
    {/each}
  </dl>
</section>

<CarvedSlab>
  <p class="kicker mono">Find your way in</p>
  <ul class="links">
    <li><a href={`${base}/maps/hagga?inst=habbanya`}>The Habbanya map board</a></li>
    <li><a href={`${base}/guilds`}>Guilds recruiting now</a></li>
    <li><a href={`${base}/events`}>This weekend and what is next</a></li>
    <li><a href={`${base}/rules`}>Server rules and live rates</a></li>
  </ul>
  <p class="later">A Habbanya community board is coming to this page.</p>
</CarvedSlab>

<style>
  .plays { margin-bottom: var(--space-5); }
  .plist { margin: 0; max-width: 72ch; }
  .plist dt { font-weight: 600; margin-top: var(--space-3); }
  .plist dd { margin: 0.2rem 0 0; color: var(--text-muted); line-height: 1.45; }
  .links { margin: var(--space-2) 0 0; padding-left: 1.2rem; display: grid; gap: var(--space-2); }
  .links a { color: var(--accent-text); }
  .later { margin: var(--space-3) 0 0; color: var(--text-muted); font-size: var(--text-sm); }
</style>
