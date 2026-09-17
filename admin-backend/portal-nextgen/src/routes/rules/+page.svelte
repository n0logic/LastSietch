<script>
  // Server Rules: the live gameplay settings the Director actually reports,
  // grouped by the catalog's own categories. Public, no gate, no polling: these
  // values change on a config push, not on a tick, so one read on mount is the
  // honest cadence and every extra poll would just spend the rate limiter.
  //
  // EVERY label on this page comes from the payload. No knob name, category name
  // or unit is typed here, because a literal in the svelte is a second source of
  // truth that drifts silently the day the catalog is corrected: the page would
  // keep printing the old name over the new value.
  //
  // `source: 'unread'` means the settings block was missing from the feed and the
  // backend served its last good snapshot. That is a real fact about the read,
  // not about the server, so it is stamped rather than hidden.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import { api } from '$lib/api.js';

  let status = $state('loading'); // 'loading' | 'ready' | 'error'
  let payload = $state(null);

  let groups = $derived(Array.isArray(payload?.groups) ? payload.groups : []);
  // Values the live feed cannot carry (ini and client properties). Catalog data,
  // labelled source=config, rendered apart from the live rows and never as one.
  let config = $derived(payload?.config && Array.isArray(payload.config.rows) && payload.config.rows.length ? payload.config : null);
  let unread = $derived(
    groups.some((g) => (g.rows || []).some((r) => r.source === 'unread'))
  );
  let aged = $derived(payload?.stale === true || unread);

  // read_at is the SERVER's stamp. A time built from the browser clock would keep
  // looking current while the feed sat dead, which is the exact failure the warn
  // line exists to disclose.
  // A server stamp may carry an offset (+00:00) or a Z; only a NAIVE stamp gets a
  // Z appended. Appending Z to an offset stamp made Date.parse return NaN.
  const hasZone = (v) => /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(String(v));
  let stamp = $derived.by(() => {
    const at = payload?.read_at;
    if (!at) return '';
    const t = Date.parse(hasZone(at) ? String(at) : `${at}Z`);
    if (!Number.isFinite(t)) return '';
    const d = new Date(t);
    return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`;
  });

  // Booleans read as on/off rather than true/false: that is a presentation of the
  // value, not a claim about it. Everything else prints as the feed sent it.
  function shownValue(row) {
    if (Array.isArray(row?.varies) && row.varies.length) return 'varies by map';
    const v = row?.value;
    if (v === null || v === undefined) return 'not read';
    if (v === true) return 'on';
    if (v === false) return 'off';
    return `${v}${row.unit ? ` ${row.unit}` : ''}`;
  }

  onMount(async () => {
    try {
      payload = await api.server.rules();
      status = 'ready';
    } catch (e) {
      status = 'error';
    }
  });
</script>

<svelte:head>
  <title>Server Rules | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | house rules"
    title="Server Rules"
    sub="The gameplay settings our servers are actually running, read straight from the BattleGroup Director. Not a wiki page, not a screenshot of an ini file: what the maps report right now."
  />

  <!-- House policy, not a live value. It renders in EVERY state, including
       while the settings feed is loading and when it has failed: a conduct rule
       does not stop applying because the Director stopped answering, and a
       player sent here to read what griefing means must find it either way.
       Nothing in this block comes from the payload, which is why it sits outside
       the status branch rather than inside the ready one. -->
  <div class="conduct">
    <CarvedSlab>
      <p class="kicker mono">Conduct</p>
      <h2 class="gname">Rules of the Sietch</h2>
      <p class="cintro">Live by the code and the spice will flow. The same seven rules are pinned in the Discord.</p>
      <ol class="rules">
        <li><b>Respect the tribe.</b> Treat every member with respect. No harassment, hate speech, slurs, or personal attacks.</li>
        <li><b>Stronger together.</b> Share knowledge and resources; help new survivors find their footing.</li>
        <li>
          <b>Play fair.</b> No cheating, exploiting bugs, or griefing friendly players. Report problems to staff.
          <span class="sub">Griefing is using game mechanics, or grouping up, to deliberately target players or groups who are obviously less powerful than you. Think of the school bully: picking a fight against someone who cannot win it. A fair fight between capable players is PvP working as intended.</span>
        </li>
        <li><b>Stay on-topic.</b> Use the right channel; coordinate Deep Desert runs in the Discord.</li>
        <li><b>No spam or ads.</b> No spam, self-promotion, or unsolicited DMs.</li>
        <li><b>Follow Discord's rules.</b> You must follow Discord's Terms of Service and Community Guidelines.</li>
        <li><b>Staff have the final say.</b> Listen to the Naib and Gatekeepers; their decisions are final.</li>
      </ol>
    </CarvedSlab>
  </div>

  {#if status === 'loading'}
    <p class="skeleton mono">reading the server settings</p>
  {:else if status === 'error' || groups.length === 0}
    <Notice
      tone="warn"
      text="The server settings feed is not answering, and we have nothing we last read to show you. Try again in a few minutes."
    />
  {:else}
    {#if aged}
      <Notice
        tone="warn"
        text="The server settings feed is not answering. These are the values we last read, at {stamp}."
      />
    {/if}

    <div class="groups">
      {#each groups as group (group.category)}
        <section class="group">
          <h2 class="gname">{group.label}</h2>
          <ul class="rows" role="list">
            {#each group.rows as row (row.key)}
              <li class="row" class:unread={row.source === 'unread'}>
                <div class="what">
                  <span class="label">{row.label}</span>
                  {#if row.controls}<span class="controls">{row.controls}</span>{/if}
                  {#if Array.isArray(row.varies) && row.varies.length}
                    <ul class="varies" role="list">
                      {#each row.varies as v (v.row_key)}
                        <li class="vary mono">{v.row_key}: {shownValue({ value: v.value, unit: row.unit })}</li>
                      {/each}
                    </ul>
                  {/if}
                </div>
                <span class="value mono">{shownValue(row)}</span>
              </li>
            {/each}
          </ul>
        </section>
      {/each}
    </div>

    {#if stamp}
      <p class="foot mono">Read from the BattleGroup Director at {stamp}.</p>
    {/if}

    {#if config}
      <section class="group config" aria-labelledby="config-h">
        <h2 class="gname" id="config-h">Land claim, from server config</h2>
        <p class="note">{config.note}</p>
        <ul class="rows" role="list">
          {#each config.rows as row (row.key)}
            <li class="row">
              <div class="what">
                <span class="label">{row.label}</span>
                {#if row.controls}<span class="controls">{row.controls}</span>{/if}
                {#if row.basis}<span class="basis mono">{row.basis}</span>{/if}
              </div>
              <span class="value mono">{shownValue(row)}</span>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  /* Conduct sits above the generated groups and keeps their heading treatment,
     so house policy and read settings read as one page rather than two. */
  .conduct { margin-bottom: var(--space-6); }
  .cintro { margin: 0 0 var(--space-3); font-size: var(--text-sm); color: var(--text-muted); max-width: 62ch; line-height: 1.55; }
  .rules { margin: 0; padding: 0 0 0 1.6em; max-width: 68ch; display: flex; flex-direction: column; gap: var(--space-2); }
  .rules li { font-size: var(--text-sm); color: var(--text); line-height: 1.55; }
  .rules li::marker { color: var(--accent); font-family: var(--font-mono); }
  .rules b { color: var(--accent-text); font-weight: 600; }
  .sub { display: block; margin-top: var(--space-1); color: var(--text-muted); }

  .groups { display: flex; flex-direction: column; gap: var(--space-6); }
  .group {
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-lg); padding: var(--space-5);
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 18px 44px -28px var(--shadow-cast);
  }
  .gname {
    margin: 0 0 var(--space-4); font-size: var(--text-lg);
    text-transform: uppercase; letter-spacing: .06em; color: var(--accent-bright);
  }

  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .row {
    display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: baseline;
    gap: var(--space-3); padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  /* A row served from the last-good snapshot is dimmed, so a stale number never
     wears the same weight as a live one. */
  .row.unread { opacity: .72; }

  .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .label { font-size: var(--text-sm); color: var(--text); }
  .controls { font-size: var(--text-xs); color: var(--text-muted); line-height: 1.4; }
  /* Values are settings, not streaming data, so they stay amber. */
  .value { font-size: var(--text-sm); color: var(--accent-text); white-space: nowrap; font-variant-numeric: tabular-nums; }

  .varies { list-style: none; margin: var(--space-1) 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .vary { font-size: 10px; color: var(--text-muted); }

  .foot { margin: var(--space-6) 0 0; font-size: var(--text-xs); color: var(--text-muted); opacity: .75; letter-spacing: .04em; }

  @media (max-width: 40rem) {
    .row { grid-template-columns: 1fr; }
    .value { justify-self: start; }
  }
  .config { margin-top: var(--space-6); }
  .note { margin: 0 0 var(--space-3); color: var(--text-muted); font-size: var(--text-sm); max-width: 62ch; }
  .basis { display: block; margin-top: 2px; color: var(--text-muted); font-size: var(--text-xs); }
</style>
