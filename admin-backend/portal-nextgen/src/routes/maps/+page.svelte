<script>
  // Maps hub: one planning-board card per open map instance, then the social hubs.
  // Owner ruling 2026-09-30: total population only,
  // so the cards carry no per-world count; one server-wide figure sits on top.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ServerSchedule from '$lib/components/ServerSchedule.svelte';
  import { api } from '$lib/api.js';
  import { mapInstances, loadMapInstances } from '$lib/mapInstances.svelte.js';
  import { MAP_NAMES, boardOrder, sharesName } from '$lib/mapInstances.js';

  // Instance boards come from the map-instance config (GET /portal/features, in
  // board order); the social hubs are game maps with one instance each. Card copy
  // is keyed by instance key; an instance without any gets its plate and no blurb.
  const COPY = {
    'dd-pve': { blurb: 'Survey grid, live spice, worm tracker.' },
    'dd-pvp': { blurb: 'The contested sand. Same board, harder rules.' },
    habbanya: { blurb: 'Habbanya, home turf. Every cave and wreck at true position.' },
    amtal: {
      blurb: 'Amtal, no sanctuary. PvP everywhere, tradeposts included, and no map tracks you. The Riftwatch and Helius Gate settlements are the only safe areas. Storms never drop shields here: you are safe at home while yours holds, so keep the generators fed.',
      warn: 'Tradepost banners can flicker to Guild Peace. Tradeposts are still PvP.',
    },
  };
  // Map-level facts (map_model.MAPS has_spice), not per-server config.
  const SPICE_MAPS = ['deep-desert'];
  const HUBS = [
    {
      key: 'arrakeen', inst: null, name: 'Arrakeen', chip: 'Social hub',
      blurb: 'The capital. Services, vendors, representatives.',
      art: '/admin/static/img/maps/arrakeen-map.webp?v=3', spice: false,
    },
    {
      key: 'harko-village', inst: null, name: 'Harko Village', chip: 'Social hub',
      blurb: 'Harkonnen ground. Watch your back, count your solari.',
      art: '/admin/static/img/maps/harko-village-map.webp?v=3', spice: false,
    },
  ];

  function instanceBoard(i, all) {
    const copy = COPY[i.key] || {};
    return {
      key: i.map, inst: i.inst, name: i.name, chip: i.mode,
      region: sharesName(all, i) ? null : MAP_NAMES[i.map] || null,
      part: i.partition, dim: i.dim, untracked: !i.tracked,
      blurb: copy.blurb || '', warn: copy.warn || null,
      art: `/img/v3/plates/${i.key}-dusk-clear-small.jpg`, spice: SPICE_MAPS.includes(i.map),
    };
  }

  let BOARDS = $derived([...boardOrder(mapInstances.list).map((i) => instanceBoard(i, mapInstances.list)), ...HUBS]);

  let online = $state(null); // server-wide total, the only live count shown
  let artFailed = $state({});

  async function poll() {
    try {
      const s = await api.server.duneStatus();
      online = s && s.available !== false && typeof s.online_players === 'number' ? s.online_players : null;
    } catch (e) { online = null; }
  }

  onMount(() => {
    loadMapInstances();
    poll();
    const t = setInterval(poll, 45_000);
    return () => clearInterval(t);
  });

  function href(b) {
    return `${base}/maps/${b.key}${b.inst ? `?inst=${b.inst}` : ''}`;
  }

</script>

<svelte:head>
  <title>Maps | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | planning tables"
    title="Maps"
    sub="Live, coordinate-accurate boards built from our own server data. Every cave, wreck, ore node and spice field at its true position. Pick a board."
  />

  <ServerSchedule />

  <p class="worldlink"><a href="{base}/world">See the whole world on the Sand Table</a></p>

  {#if online != null}
    <p class="online mono"><LiveDot tone="live" /> {online} {online === 1 ? 'player' : 'players'} online across the server</p>
  {/if}

  <div class="grid">
    {#each BOARDS as b (b.key + (b.inst ?? ''))}
      <a class="card" href={href(b)}>
        <div class="art" aria-hidden="true">
          {#if b.art && !artFailed[b.key]}
            <img src={b.art} alt="" loading="lazy" onerror={() => { artFailed[b.key] = true; }} />
          {:else}
            <div class="map-backdrop--sand art-fill"></div>
          {/if}
          <span class="scrim"></span>
          <span class="chip inst-chip mono">{b.chip}</span>
        </div>
        <div class="body">
          {#if b.region}<p class="region">{b.region}</p>{/if}
          <h2>{b.name}</h2>
          <p class="blurb">{b.blurb}</p>
          {#if b.warn}<p class="warn mono">⚠ {b.warn}</p>{/if}
          <div class="chips">
            {#if b.spice}<span class="chip chip-spice mono">Live spice</span>{/if}
          </div>
        </div>
        <span class="brackets" aria-hidden="true"></span>
      </a>
    {/each}
  </div>

  <p class="footnote mono">
    Static layers refresh each Coriolis cycle. Live overlays update straight from the server.
  </p>
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .online { display: flex; align-items: center; gap: var(--space-2); margin: 0 0 var(--space-4); font-size: var(--text-sm); color: var(--text-muted); }
  .warn { margin: var(--space-1) 0 0; font-size: 0.78rem; line-height: 1.4; color: var(--ls-red); }

  .grid {
    display: grid; gap: var(--space-5);
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  }

  /* Board card: a carved plate whose face is the board art. */
  .card {
    position: relative; display: flex; flex-direction: column;
    text-decoration: none; color: var(--text); overflow: hidden;
    background: var(--panel);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 18px 44px -28px var(--shadow-cast);
    transition: transform var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .card:hover { transform: translateY(-3px); border-color: var(--accent); }

  .art { position: relative; aspect-ratio: 16 / 9; overflow: hidden; }
  .art img, .art-fill { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .scrim {
    position: absolute; inset: 0;
    background: linear-gradient(to top, color-mix(in srgb, var(--metal-0) 85%, transparent), transparent 60%);
  }
  .inst-chip {
    position: absolute; top: var(--space-2); right: var(--space-2);
    background: color-mix(in srgb, var(--bg-deep) 80%, transparent);
    color: var(--accent-bright); border-color: var(--accent-soft);
  }

  .body { padding: var(--space-3) var(--space-4) var(--space-4); }
  .region { margin: 0 0 4px; font-size: 12px; color: var(--text-muted); }
  .body h2 {
    font-size: var(--text-lg); letter-spacing: .06em; text-transform: uppercase;
    margin: 0 0 var(--space-1);
  }
  .blurb { color: var(--text-muted); font-size: var(--text-sm); margin: 0 0 var(--space-3); line-height: 1.4; }

  .chips { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .chip {
    display: inline-flex; align-items: center; gap: var(--space-1);
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    padding: 2px var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid var(--edge); color: var(--text-muted);
  }
  .chip-spice {
    color: var(--ls-melange-hi);
    border-color: color-mix(in srgb, var(--ls-melange-soft) 50%, var(--edge));
  }
  .chip-players { color: var(--text); }

  /* Etched corner brackets on hover: the card resolves as an instrument. */
  .brackets::before, .brackets::after {
    content: ''; position: absolute; width: 13px; height: 13px;
    border: 1.5px solid var(--edge-hi); opacity: 0; pointer-events: none;
    transition: opacity var(--motion-fast) var(--ease-out);
  }
  .brackets::before { top: 6px; left: 6px; border-right: 0; border-bottom: 0; }
  .brackets::after { bottom: 6px; right: 6px; border-left: 0; border-top: 0; }
  .card:hover .brackets::before, .card:hover .brackets::after { opacity: .85; }

  .footnote {
    color: var(--text-muted); font-size: var(--text-xs);
    margin: var(--space-7) 0 0; opacity: .75; letter-spacing: .04em;
  }

  /* Cards rise in on load, staggered (same pattern as the dashboard). */
  .grid > .card { opacity: 0; transform: translateY(14px); animation: rise .55s var(--ease-out) forwards; }
  .grid > .card:nth-child(1) { animation-delay: .04s; }
  .grid > .card:nth-child(2) { animation-delay: .12s; }
  .grid > .card:nth-child(3) { animation-delay: .20s; }
  .grid > .card:nth-child(4) { animation-delay: .28s; }
  .grid > .card:nth-child(5) { animation-delay: .36s; }
  .grid > .card:nth-child(6) { animation-delay: .44s; }
  .grid > .card:nth-child(7) { animation-delay: .52s; }
  @media (prefers-reduced-motion: reduce) {
    .grid > .card { opacity: 1; transform: none; animation: none; }
  }
  @keyframes rise { to { opacity: 1; transform: none; } }
  .worldlink { margin: 0 0 var(--space-4); }
  .worldlink a { color: var(--accent-text); letter-spacing: .08em; text-transform: uppercase; font-size: var(--text-sm); text-decoration: none; border-bottom: 1px solid var(--accent-soft); padding-bottom: 2px; }
  .worldlink a:hover, .worldlink a:focus-visible { color: var(--accent-bright); border-bottom-color: var(--accent-bright); }
</style>
