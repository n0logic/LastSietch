<script>
  // Amtal guide page. Public. The PvP board carries its own gate; the rest is house
  // policy (not a live feed), so it is typed here.
  import PageHeader from '$lib/components/PageHeader.svelte';
  import PvpBoard from '$lib/components/amtal/PvpBoard.svelte';

  // Owner decision 2026-09-30: zones on with the PvP table; these two are the only safe areas.
  const SAFE_AREAS = [
    {
      name: 'Riftwatch',
      map: '/img/amtal/map-riftwatch-quartermaster-poi.webp',
      mapAlt: "Map of Amtal's Hagga Rift showing the Riftwatch settlement and its quartermaster marker",
      terminal: '/img/amtal/riftwatch-choam-terminal.webp',
      terminalAlt: 'CHOAM Exchange terminal near the Riftwatch quartermaster',
      where: 'Hagga Rift: the whole Riftwatch settlement is safe. The CHOAM Exchange terminal is near the quartermaster.',
    },
    {
      name: 'Helius Gate',
      map: '/img/amtal/map-helius-gate-quartermaster-poi.webp',
      mapAlt: 'Map of Amtal showing the Helius Gate settlement and its quartermaster marker on the Eastern Shield Wall',
      terminal: '/img/amtal/helius-gate-choam-terminal.webp',
      terminalAlt: 'CHOAM Exchange terminal near the Helius Gate quartermaster',
      where: 'Eastern Shield Wall: the whole Helius Gate settlement is safe. The CHOAM Exchange terminal is near the quartermaster.',
    },
  ];
  // Measured on Amtal 2026-10-05 (kill tests with DB diffs) and live since that evening: polls AM5/AM6, owner values.
  const HOW_IT_PLAYS = [
    ['PvP loot', 'When another player kills you, all the loose loot in your backpack drops for them: resources, fuel cells and schematics you have not learned. Equipped gear, weapons, tools, ammo, uniques, quest items and saved base blueprints stay with you. Being downed and not finished drops nothing.'],
    ['PvP pays', "1.5x stock on hand gathering, cutteray and vehicle mining, plus Funcom's 2.5x in PvP zones, which is everywhere outside Riftwatch and Helius Gate, so 3.75x almost anywhere you harvest (poll AM8). Habbanya runs 1.5x."],
    ['Bigger backpack', '4x inventory volume and 100 slots. Carry more than another sietch allows and you keep it all there; you just cannot pick up more until you unload.'],
    ['Safe arrival', "Coming to Amtal from another sietch or the Deep Desert places you at your own Amtal base if you own one, otherwise at your faction's safe area: Harkonnen at Riftwatch, Atreides at Helius Gate. Logging out and back in on Amtal does not move you."],
    ['Your base', 'Keep your generators fueled. Getting in through an exploit is not allowed: glitching through walls, terrain or shields is banned.'],
  ];
</script>

<svelte:head>
  <title>Amtal | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Amtal"
    title="Amtal: free for all"
    sub="PvP everywhere, including the tradeposts. Two safe areas: the Riftwatch and Helius Gate settlements. Richer rates, bigger backpacks, and a kill drops your loose loot."
  />

  <!-- Own gate (LASTSIETCH_AMTAL_PVP_ENABLED): renders nothing while it is off. -->
  <div class="pvp"><PvpBoard /></div>

  <section class="safe" aria-labelledby="safe-title">
    <h2 id="safe-title" class="kicker mono">Safe areas</h2>
    <p class="safe-intro">
      The game blocks attacks and abilities only in these two settlements. Each has a CHOAM Exchange
      terminal near the quartermaster, so you can trade and collect deliveries in safety. Tradepost banners can flicker to Guild Peace,
      but tradeposts are PvP.
    </p>
    <div class="safe-grid">
      {#each SAFE_AREAS as area (area.name)}
        <article class="safe-card">
          <h3>{area.name}</h3>
          <img src={area.map} alt={area.mapAlt} loading="lazy" />
          <img src={area.terminal} alt={area.terminalAlt} loading="lazy" />
          <p>{area.where}</p>
        </article>
      {/each}
    </div>
  </section>

  <section class="plays" aria-labelledby="plays-title">
    <h2 id="plays-title" class="kicker mono">How Amtal plays</h2>
    <dl class="plist">
      {#each HOW_IT_PLAYS as [term, text] (term)}
        <dt>{term}</dt>
        <dd>{text}</dd>
      {/each}
    </dl>
  </section>
</div>

<style>
  .safe { margin-bottom: var(--space-5); }
  .safe-intro { color: var(--text-muted); max-width: 64ch; margin-bottom: var(--space-4); }
  .safe-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
  .safe-card { display: flex; flex-direction: column; gap: var(--space-2); }
  .safe-card h3 { margin: 0; }
  .safe-card img { width: 100%; height: auto; border-radius: 6px; border: 1px solid var(--border-subtle); }
  .safe-card p { color: var(--text-muted); margin: 0; }
  @media (max-width: 759px) { .safe-grid { grid-template-columns: 1fr; } }
  .plays { margin-bottom: var(--space-5); }
  .plist { margin: 0; max-width: 72ch; }
  .plist dt { font-weight: 600; margin-top: var(--space-3); }
  .plist dd { margin: 0.2rem 0 0; color: var(--text-muted); line-height: 1.45; }
  .pvp:not(:empty) { margin-bottom: var(--space-5); }
</style>
