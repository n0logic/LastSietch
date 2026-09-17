<script>
  // Sietch Traffic: the seven canonical map instances the BattleGroup Director
  // reports, plus the collapsed instanced-worlds group. Table from 760px (the
  // shell's phone breakpoint), cards below, because a seven-column table on a
  // phone is a horizontal scroll nobody reads.
  //
  // Three honesty rules run through every cell here:
  //   * null is NEVER 0. A scalar the Director did not report renders as "not
  //     observed"; an empty server and an unread server are different facts.
  //   * Amtal carries no public count (owner ruling 891b8aa). Its row shows the
  //     state and the scaling and nothing that could be read as a headcount.
  //   * Ibad blue lands on the live NUMBER only. The chrome around it (chips,
  //     pills, labels, the scaling sentence) stays amber.
  //
  // The gauge is drawn only when the cap AND the count are both real: a gauge
  // pinned at zero because the count was missing reads as an empty server.
  import HudGauge from '$lib/components/HudGauge.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import { traffic, watchTraffic } from '$lib/traffic.svelte.js';

  // The board owns the subscription rather than the route, so every surface that
  // mounts it gets the poll, and two boards on one page still share one timer.
  $effect(() => watchTraffic());

  let rows = $derived(Array.isArray(traffic.rows) ? traffic.rows : []);
  let instanced = $derived(traffic.instanced || null);
  let expanded = $state(false);

  // The four states the shaper emits. `unknown` means the map was absent from
  // the payload entirely, which is a read gap, not a cold server.
  const STATE_COPY = {
    warm: 'warm',
    cold: 'cold',
    spins_up_on_travel: 'spins up on travel',
    unknown: 'not observed',
  };
  function stateCopy(state) { return STATE_COPY[state] || 'not observed'; }

  // One sentence off cfg.minServers / cfg.numExtraServers. minServers 0 means
  // nothing is held warm and the world is started by the first traveller.
  function scalingSentence(row) {
    const s = row?.scaling;
    // Dimension maps (Deep Desert, Hagga) carry no scaling config on the wire because they
    // never scale: they are always on. Only an instanced map can be missing its config.
    if (!s || s.min_servers == null) return row?.inst === 'main' || row?.instances == null ? 'scaling not observed' : 'always on';
    const parts = [s.min_servers >= 1 ? `keeps ${s.min_servers} warm` : 'spins up on travel'];
    if (s.extra_servers != null && s.extra_servers > 0) parts.push(`${s.extra_servers} spare`);
    return parts.join(', ');
  }

  function showsCount(row) { return row?.tracked !== false; }
  function showsGauge(row) {
    return showsCount(row) && row?.cap != null && row?.players != null;
  }

  // read_at is the SERVER's stamp, never our clock: an "as of" built from the
  // browser clock would keep looking current while the feed sat dead.
  // A server stamp may carry an offset (+00:00) or a Z; only a NAIVE stamp gets a
  // Z appended. Appending Z to an offset stamp made Date.parse return NaN.
  const hasZone = (v) => /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(String(v));
  let stamp = $derived.by(() => {
    if (!traffic.readAt) return '';
    const t = Date.parse(hasZone(traffic.readAt) ? String(traffic.readAt) : `${traffic.readAt}Z`);
    if (!Number.isFinite(t)) return '';
    const d = new Date(t);
    return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`;
  });

  // Summary of the collapsed group. Any missing counter drops out of the line
  // rather than printing a zero we did not read.
  let instancedSummary = $derived.by(() => {
    if (!instanced) return '';
    const maps = instanced.maps;
    const parts = [`${maps ?? 0} instanced world${maps === 1 ? '' : 's'}`];
    if (instanced.warm != null) parts.push(`${instanced.warm} warm`);
    if (instanced.players != null) parts.push(`${instanced.players} player${instanced.players === 1 ? '' : 's'}`);
    if (instanced.queue != null && instanced.queue > 0) parts.push(`${instanced.queue} queued`);
    return parts.join(', ');
  });

  // Busy worlds first so an expanded group opens on the ones worth reading;
  // everything else keeps its payload order.
  let instancedRows = $derived.by(() => {
    const list = Array.isArray(instanced?.rows) ? [...instanced.rows] : [];
    const busy = (r) => ((Number(r?.players) || 0) + (Number(r?.queue) || 0)) > 0;
    return list.sort((a, b) => (busy(b) ? 1 : 0) - (busy(a) ? 1 : 0));
  });
</script>

<div class="board">
  <div class="head">
    <p class="kicker mono">Sietch traffic</p>
    {#if traffic.available && traffic.onlinePlayers != null}
      <span class="census mono">
        <LiveDot tone="live" /> <span class="live-n">{traffic.onlinePlayers}</span> across the battlegroup
      </span>
    {/if}
  </div>

  {#if traffic.status === 'idle' || traffic.status === 'loading'}
    <p class="skeleton mono">reading the battlegroup</p>
  {:else if !traffic.available || rows.length === 0}
    <Notice
      tone="warn"
      text="The battlegroup feed is not answering right now, so there is no live map traffic to show."
    />
  {:else}
    {#if traffic.stale}
      <Notice
        tone="warn"
        text="The battlegroup feed is not answering. This is what we last read, at {stamp}."
      />
    {/if}

    <!-- Table from 760px up. -->
    <table class="wide">
      <thead>
        <tr>
          <th scope="col">World</th>
          <th scope="col">Mode</th>
          <th scope="col">State</th>
          <th scope="col" class="num">Online</th>
          <th scope="col" class="num">Queue</th>
          <th scope="col">Scaling</th>
          <th scope="col" class="gauge-col"><span class="vh">Capacity</span></th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.key)}
          <tr>
            <th scope="row" class="name">{row.name}</th>
            <td><span class="chip mono">{row.mode}</span></td>
            <td><span class="pill mono" data-state={row.state}>{stateCopy(row.state)}</span></td>
            <td class="num">
              {#if !showsCount(row)}
                <span class="muted">no public count</span>
              {:else if row.players == null}
                <span class="muted">not observed</span>
              {:else}
                <LiveDot tone="live" /> <span class="live-n mono">{row.players}</span>
              {/if}
            </td>
            <td class="num">
              {#if !showsCount(row)}
                <span class="muted">no public count</span>
              {:else if row.queue == null}
                <span class="muted">not observed</span>
              {:else}
                <span class="mono">{row.queue}</span>
              {/if}
            </td>
            <td class="scaling">{scalingSentence(row)}</td>
            <td class="gauge-col">
              {#if showsGauge(row)}
                <HudGauge
                  value={row.players} max={row.cap} size={58} live={true}
                  display={`${row.players}/${row.cap}`}
                />
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    <!-- Cards below 760px. -->
    <ul class="narrow" role="list">
      {#each rows as row (row.key)}
        <li class="card">
          <div class="c-head">
            <span class="name">{row.name}</span>
            <span class="chip mono">{row.mode}</span>
          </div>
          <div class="c-line">
            <span class="pill mono" data-state={row.state}>{stateCopy(row.state)}</span>
            {#if !showsCount(row)}
              <span class="muted">no public count</span>
            {:else if row.players == null}
              <span class="muted">not observed</span>
            {:else}
              <span class="c-count">
                <LiveDot tone="live" /> <span class="live-n mono">{row.players}</span>
                <span class="muted">online</span>
              </span>
            {/if}
            {#if showsCount(row) && row.queue != null && row.queue > 0}
              <span class="muted mono">{row.queue} queued</span>
            {/if}
          </div>
          <p class="scaling">{scalingSentence(row)}</p>
        </li>
      {/each}
    </ul>

    {#if instanced}
      <section class="instanced">
        <button
          class="disclose mono" type="button"
          aria-expanded={expanded} onclick={() => (expanded = !expanded)}
        >
          <span class="caret" aria-hidden="true">{expanded ? '−' : '+'}</span>
          {instancedSummary}
        </button>
        {#if expanded}
          <ul class="inst-rows" role="list">
            {#each instancedRows as r (r.key)}
              <li class="inst-row">
                <span class="inst-name">{r.name}</span>
                <span class="pill mono" data-state={r.state}>{stateCopy(r.state)}</span>
                <span class="inst-count">
                  {#if r.players == null}
                    <span class="muted">not observed</span>
                  {:else if r.players > 0}
                    <LiveDot tone="live" /> <span class="live-n mono">{r.players}</span>
                  {:else}
                    <span class="mono muted">0</span>
                  {/if}
                </span>
                <span class="inst-queue mono">
                  {#if r.queue != null && r.queue > 0}{r.queue} queued{/if}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    {/if}

    {#if traffic.totals}
      <p class="foot mono">
        {#if traffic.totals.travel_requests != null}{traffic.totals.travel_requests} travel requests{/if}
        {#if traffic.totals.travel_requests != null && traffic.totals.login_requests != null} &middot; {/if}
        {#if traffic.totals.login_requests != null}{traffic.totals.login_requests} login requests{/if}
        {#if stamp} &middot; read at {stamp}{/if}
      </p>
    {/if}
  {/if}
</div>

<style>
  .board { display: flex; flex-direction: column; gap: var(--space-3); }
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .census { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; white-space: nowrap; }
  /* Ibad lands on the live number and nothing around it. */
  .live-n { color: var(--ls-ibad); font-variant-numeric: tabular-nums; }
  .muted { color: var(--text-muted); font-size: var(--text-xs); }

  .vh {
    position: absolute; width: 1px; height: 1px; overflow: hidden;
    clip-path: inset(50%); white-space: nowrap;
  }

  table.wide { width: 100%; border-collapse: collapse; }
  table.wide th, table.wide td {
    padding: var(--space-2) var(--space-2); text-align: left; vertical-align: middle;
    border-bottom: 1px solid var(--edge);
  }
  table.wide thead th {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    text-transform: uppercase; color: var(--text-muted); font-weight: 400;
    border-bottom: 1px solid var(--edge-hi);
  }
  table.wide tbody tr:last-child th, table.wide tbody tr:last-child td { border-bottom: 0; }
  table.wide .num { text-align: right; white-space: nowrap; }
  table.wide .gauge-col { width: 66px; text-align: center; }
  .name {
    font-family: var(--font-display); font-size: var(--text-sm); text-transform: uppercase;
    letter-spacing: .05em; color: var(--text); font-weight: 400;
  }
  .scaling { color: var(--text-muted); font-size: var(--text-xs); line-height: 1.4; margin: 0; }

  .chip {
    display: inline-block; font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
    padding: 2px var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid var(--edge); color: var(--text-muted); white-space: nowrap;
  }
  .pill {
    display: inline-block; font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
    padding: 2px var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid var(--edge); color: var(--text-muted); white-space: nowrap;
  }
  /* State is chrome, so it stays amber/red; only the counts go Ibad. */
  .pill[data-state='warm'] { color: var(--accent-bright); border-color: var(--accent-soft); }
  .pill[data-state='cold'] { color: var(--ls-red); border-color: color-mix(in srgb, var(--ls-red) 45%, var(--edge)); }

  ul.narrow { list-style: none; margin: 0; padding: 0; display: none; flex-direction: column; gap: var(--space-2); }
  .card {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3); background: var(--metal-0);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .c-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
  .c-line { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
  .c-count { display: inline-flex; align-items: center; gap: 5px; font-size: var(--text-sm); }

  @media (max-width: 759px) {
    table.wide { display: none; }
    ul.narrow { display: flex; }
  }

  .instanced { border-top: 1px solid var(--edge); padding-top: var(--space-3); }
  .disclose {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-xs); letter-spacing: .08em; color: var(--text-muted);
    background: transparent; border: 0; padding: 0; cursor: pointer;
  }
  .disclose:hover { color: var(--accent-text); }
  .caret { color: var(--accent); }
  .inst-rows { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .inst-row {
    display: grid; grid-template-columns: minmax(0, 1fr) auto auto auto;
    align-items: center; gap: var(--space-2);
    padding: var(--space-1) var(--space-2);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .inst-name { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .inst-count, .inst-queue { font-size: var(--text-xs); white-space: nowrap; }
  .inst-queue { color: var(--text-muted); }

  .foot { margin: 0; font-size: 10px; color: var(--text-muted); letter-spacing: .04em; opacity: .8; }

  @media (max-width: 480px) {
    .inst-row { grid-template-columns: minmax(0, 1fr) auto; row-gap: 2px; }
  }
</style>
