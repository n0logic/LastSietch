<script>
  // Maps hub: five planning-board cards. There is no JSON endpoint listing maps
  // (V1's hub is server-rendered), so the board set is a static descriptor kept
  // in step with map_model.MAPS; live player counts come from /players per key.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import ServerSchedule from '$lib/components/ServerSchedule.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import { api, getJSON } from '$lib/api.js';

  // KEEP IN SYNC with admin-backend/map_model.py MAPS (keys, names, card art).
  // All four registry maps are listed (retention contract: no feature loss vs
  // the V1 hub); Deep Desert gets one card per instance.
  const BOARDS = [
    {
      key: 'deep-desert', inst: 'pve', name: 'Deep Desert', chip: 'PvE', dim: 0,
      blurb: 'Survey grid, live spice, worm tracker.',
      art: '/img/v3/plates/dd-pve-dusk-clear-small.jpg', spice: true,
    },
    {
      key: 'deep-desert', inst: 'pvp', name: 'Deep Desert', chip: 'PvP', dim: 1,
      blurb: 'The contested sand. Same board, harder rules.',
      art: '/img/v3/plates/dd-pvp-dusk-clear-small.jpg', spice: true,
    },
    // Three live Hagga sietches share the terrain, split by partition (the public
    // positions feed tags each player with p=1 Habbanya / p=32 Kulon / p=33
    // Amtal), so each card carries its own live count.
    {
      key: 'hagga', inst: 'habbanya', name: 'Habbanya', region: 'Hagga Basin', chip: 'PvE', part: 1,
      blurb: 'Habbanya, home turf. Every cave and wreck at true position.',
      art: '/img/v3/plates/habbanya-dusk-clear-small.jpg', spice: false,
    },
    {
      key: 'hagga', inst: 'kulon', name: 'Kulon', region: 'Hagga Basin', chip: 'PvP', part: 32,
      blurb: 'Kulon, the contested basin. Same ground, harder rules.',
      art: '/img/v3/plates/kulon-dusk-clear-small.jpg', spice: false,
    },
    {
      key: 'hagga', inst: 'amtal', name: 'Amtal', region: 'Hagga Basin', chip: 'Full PvP', part: 33, untracked: true,
      blurb: 'Amtal, no sanctuary. PvP everywhere but the tradeposts, and no map tracks you. Storms never drop shields here: you are safe at home while yours holds, so keep the generators fed.',
      warn: 'The in-game banner out there reads PvE. It lies. Full PvP everywhere outside the tradeposts.',
      art: '/img/v3/plates/amtal-dusk-clear-small.jpg', spice: false,
    },
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

  let players = $state({}); // key -> /players payload
  // Per-partition live Hagga counts from the public positions feed (p=1/32).
  let hagga = $state({ available: false, counts: {} });
  let artFailed = $state({});

  async function poll() {
    for (const board of BOARDS.filter((item) => item.part == null)) {
      try { players[board.key + (board.inst || '')] = await api.maps.players(board.key, board.dim); }
      catch (e) { players[board.key + (board.inst || '')] = null; }
    }
    try {
      const d = await getJSON('/api/dune/positions');
      const counts = {};
      for (const p of (d.players || [])) counts[p.p] = (counts[p.p] || 0) + 1;
      hagga = { available: d.available !== false, counts };
    } catch (e) { hagga = { available: false, counts: {} }; }
  }

  onMount(() => {
    poll();
    const t = setInterval(poll, 45_000);
    return () => clearInterval(t);
  });

  function href(b) {
    return `${base}/maps/${b.key}${b.inst ? `?inst=${b.inst}` : ''}`;
  }

  // Live count for a card: per-partition for the three Hagga sietches, else the
  // whole-map count. Returns null when no live figure is available yet.
  function liveCount(b) {
    // Amtal (owner, 2026-08-27): positions are server-filtered for the full-PvP
    // sietch, so a count here would read a hardcoded 0, so hide it instead.
    if (b.untracked) return null;
    if (b.part != null) return hagga.available ? (hagga.counts[b.part] || 0) : null;
    const reading = players[b.key + (b.inst || '')];
    return reading?.available ? (reading.map_players ?? null) : null;
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

  <div class="grid">
    {#each BOARDS as b (b.key + (b.inst ?? ''))}
      {@const live = liveCount(b)}
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
            {#if live != null}
              <span class="chip chip-players mono">
                <LiveDot tone="live" /> {live} on this map
              </span>
            {/if}
          </div>
          <!-- Untracked sietch: the count is ABSENT, not zero. Saying so where the
               count would have been is the only way a reader can tell the two apart. -->
          {#if b.untracked}
            <SealedPanel
              status="empty" action="none" art="untracked"
              emptyText="No live count on this sietch. The position feed is filtered out here, so nothing on this page can tell you who is on the sand."
            />
          {/if}
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
</style>
