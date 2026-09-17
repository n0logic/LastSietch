<script>
  // Daily ramp track. Two render paths, chosen by whether the backend has
  // shipped the new dated calendar yet:
  //  - `calendar` (28 cells) present: a REAL 28-day period calendar, Monday to
  //    Sunday, four real weeks -- dates the player actually logged in, with the
  //    Solari amount they really earned (correctly dropping back to 10,000
  //    after a broken streak; a missed day just shows empty, never guessed).
  //    This is also what makes the monthly reward's 15-of-28 requirement
  //    countable on-screen instead of only living in a separate gauge.
  //  - `calendar` absent (older backend / partial deploy): falls back to the
  //    original streak-relative 7-cell cycle row, unchanged -- "no real dates,
  //    the ramp cycle IS the unit," with "show month" stacking 3 fabricated
  //    upcoming rows.
  // Either way, claiming stays ONE accumulate action: any claimable cell fires
  // the same onClaim, which sweeps every unclaimed logged day in the pool.
  let { cycle = [], ramp = [], weeks = 4, cycleLen = 7, milestones = [],
        calendar = [], onClaim = () => {}, claiming = false } = $props();

  const SOLARI_ICON = '/admin/static/img/dune-icons/T_UI_IconResourceSolarisCoin_D.png';

  const STATE = {
    claimed:   { glyph: '✓', label: 'Claimed' },
    claimable: { glyph: '!',      label: 'Claim' },
    // Logged in, never claimed, and its 7-day ramp cycle has since rolled. claim_pool
    // does not carry those days, so painting them "Claim" advertised Solari that can
    // no longer be collected. The backend now marks which cells are really in the pool.
    lapsed:    { glyph: '×',      label: 'Expired' },
    pending:   { glyph: '⋯', label: 'Pending' },
    missed:    { glyph: '·',      label: 'Missed' },
    upcoming:  { glyph: '\u{1F512}', label: 'Upcoming' },  // padlock
  };
  function chip(s) { return STATE[s] || STATE.upcoming; }
  function amtText(n) { return (Number(n) || 0).toLocaleString(); }

  // ---- fallback path: the old streak-relative 7-cell cycle row ----
  let expanded = $state(false);
  const cd = (c) => Number(c?.cycle_day) || 0;
  const isMilestone = (day) => Array.isArray(milestones) && milestones.includes(day);

  // A future week: the repeating ramp, every cell upcoming.
  function futureRow() {
    return (ramp || []).map((a, i) => ({
      cycle_day: i + 1, amount: a, state: 'upcoming', is_today: false,
      milestone: isMilestone(i + 1),
    }));
  }

  let curRow = $derived((cycle && cycle.length) ? cycle : futureRow());
  let rows = $derived.by(() => {
    const out = [curRow];
    if (expanded) for (let w = 1; w < (weeks || 4); w++) out.push(futureRow());
    return out;
  });
  function rowLabel(i) { return i === 0 ? 'This week' : `Week ${i + 1}`; }
  function cycleCellLabel(c) {
    return `Day ${cd(c)}, ${amtText(c.amount)} Solari, ${chip(c.state).label.toLowerCase()}`;
  }

  // ---- new path: the real 28-day dated calendar ----
  let hasCalendar = $derived(Array.isArray(calendar) && calendar.length === 28);
  const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  // A missed day is checked AFTER is_today/is_future so "today, login not yet
  // recorded" reads as pending (the 300s recorder lag), never as missed.
  function calState(c) {
    if (c.is_future) return 'upcoming';
    if (c.is_today && !c.logged) return 'pending';
    if (!c.logged) return 'missed';
    if (c.claimed) return 'claimed';
    // Logged, unclaimed, and NOT in the pool the next claim would sweep: its 7-day
    // ramp cycle has rolled and claim_pool will never carry it. `in_pool` is
    // authoritative from the backend; older payloads without it keep the previous
    // behaviour rather than silently marking every past day expired.
    if (c.in_pool === false) return 'lapsed';
    return 'claimable';
  }
  function dayNum(dateStr) {
    const d = dateStr ? new Date(`${dateStr}T00:00:00Z`) : null;
    return d && !isNaN(d.getTime()) ? d.getUTCDate() : '';
  }
  function calCellLabel(c, state) {
    const parts = [c.date, c.amount != null ? `${amtText(c.amount)} Solari` : chip(state).label];
    if (c.milestone) parts.push('milestone');
    return parts.join(', ');
  }
  let calRows = $derived.by(() => {
    if (!hasCalendar) return [];
    const out = [];
    for (let r = 0; r < 4; r++) out.push(calendar.slice(r * 7, r * 7 + 7));
    return out;
  });
</script>

<div class="cal">
  <div class="cal-head">
    <p class="panel-kicker mono">Daily rewards</p>
    {#if !hasCalendar}
      <button class="expand mono" type="button" onclick={() => (expanded = !expanded)} aria-expanded={expanded}>
        {expanded ? 'This week' : 'Show month'}
      </button>
    {/if}
  </div>

  {#if hasCalendar}
    <div class="weeks dated">
      <div class="wk-row head" aria-hidden="true">
        <span class="wk-lbl mono"></span>
        <div class="grid">
          {#each WEEKDAYS as wd (wd)}<span class="wd mono">{wd}</span>{/each}
        </div>
      </div>
      {#each calRows as row, ri (ri)}
        <div class="wk-row">
          <span class="wk-lbl mono">Week {ri + 1}</span>
          <div class="grid">
            {#each row as c, ci (ci)}
              {@const state = calState(c)}
              {#if state === 'claimable'}
                <button
                  class="cell claimable" class:milestone={c.milestone} class:today={c.is_today}
                  type="button" onclick={onClaim} disabled={claiming}
                  aria-label={calCellLabel(c, state)}
                >
                  <span class="date mono">{dayNum(c.date)}</span>
                  <span class="amt mono">{c.amount != null ? amtText(c.amount) : '–'}</span>
                  <span class="g pulse" aria-hidden="true">{chip(state).glyph}</span>
                </button>
              {:else}
                <div class="cell {state}" class:milestone={c.milestone} class:today={c.is_today} aria-label={calCellLabel(c, state)}>
                  <span class="date mono">{dayNum(c.date)}</span>
                  <span class="amt mono">{c.amount != null ? amtText(c.amount) : '–'}</span>
                  <span class="g" aria-hidden="true">{chip(state).glyph}</span>
                </div>
              {/if}
            {/each}
          </div>
        </div>
      {/each}
    </div>

    <ul class="legend" role="list">
      {#each Object.entries(STATE) as [key, s] (key)}
        <li class="legend-item"><span class="g" aria-hidden="true">{s.glyph}</span><span class="t">{s.label}</span></li>
      {/each}
    </ul>
    <p class="note">
      A missed day only means no login that day; it does not reset the monthly reward's day count above.
    </p>
  {:else}
    <div class="weeks">
      {#each rows as row, ri (ri)}
        <div class="wk-row">
          {#if expanded}<span class="wk-lbl mono">{rowLabel(ri)}</span>{/if}
          <div class="grid">
            {#each row as c, ci (ci)}
              {#if c.state === 'claimable'}
                <button
                  class="cell claimable"
                  class:milestone={c.milestone}
                  class:today={c.is_today}
                  type="button"
                  onclick={onClaim}
                  disabled={claiming}
                  aria-label={cycleCellLabel(c)}
                >
                  <span class="day mono">Day {cd(c)}</span>
                  <img class="ico" src={SOLARI_ICON} alt="" aria-hidden="true" loading="lazy" />
                  <span class="amt mono">{amtText(c.amount)}</span>
                  <span class="tag"><span class="g pulse" aria-hidden="true">{chip(c.state).glyph}</span><span class="t">{chip(c.state).label}</span></span>
                </button>
              {:else}
                <div class="cell {c.state}" class:milestone={c.milestone} class:today={c.is_today} aria-label={cycleCellLabel(c)}>
                  <span class="day mono">Day {cd(c)}</span>
                  <img class="ico" src={SOLARI_ICON} alt="" aria-hidden="true" loading="lazy" />
                  <span class="amt mono">{amtText(c.amount)}</span>
                  <span class="tag"><span class="g" aria-hidden="true">{chip(c.state).glyph}</span><span class="t">{chip(c.state).label}</span></span>
                </div>
              {/if}
            {/each}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .cal { display: flex; flex-direction: column; gap: var(--space-3); }
  .cal-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .expand {
    background: var(--metal-1); color: var(--text-muted);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .expand:hover { border-color: var(--accent); color: var(--accent-text); }

  .weeks { display: flex; flex-direction: column; gap: var(--space-3); overflow-x: auto; }
  .wk-row { display: flex; flex-direction: column; gap: var(--space-1); min-width: 32rem; }
  .wk-lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  /* 7 equal columns = the ramp days (or, in the dated grid, real weekdays). */
  .grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: var(--space-2); }

  .cell {
    display: flex; flex-direction: column; align-items: center; gap: var(--space-1);
    padding: var(--space-2); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi);
    text-align: center; color: var(--text); min-width: 0;
  }
  .cell.milestone { border-color: var(--accent-soft); box-shadow: inset 0 1px 0 var(--metal-hi), 0 0 0 1px color-mix(in srgb, var(--accent) 40%, transparent); }
  .cell.today { outline: 1px solid color-mix(in srgb, var(--accent) 55%, transparent); outline-offset: 1px; }

  .day { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .04em; }
  .ico { width: 28px; height: 28px; object-fit: contain; opacity: .95; }
  .amt { font-size: var(--text-sm); color: var(--accent-text); font-variant-numeric: tabular-nums; }

  .tag { display: inline-flex; align-items: center; gap: 4px; font-size: 10px; letter-spacing: .06em; text-transform: uppercase; }
  .tag .g { font-size: var(--text-xs); line-height: 1; }
  .tag .t { color: var(--text-muted); }

  /* Upcoming: dim, coin/amount reserved so rows stay height-aligned. */
  .cell.upcoming { opacity: .42; }
  .cell.upcoming .amt { color: var(--text-muted); }
  .cell.upcoming .g, .cell.upcoming .tag .t { color: var(--text-muted); }
  /* Pending: today, login row not yet recorded. */
  .cell.pending { border-color: color-mix(in srgb, var(--accent) 30%, var(--edge)); }
  .cell.pending .g, .cell.pending .tag .t { color: var(--accent-text); }
  /* Claimed. */
  .cell.claimed { border-color: color-mix(in srgb, var(--ls-green) 45%, var(--edge)); }
  .cell.claimed .g, .cell.claimed .tag .t { color: var(--ls-green); }
  /* Missed: a real past day with no login. Distinct from upcoming (a future
     day) by both glyph shape and label, never conveyed by the dim tint alone. */
  .cell.missed { opacity: .55; }
  .cell.missed .amt, .cell.missed .g, .cell.missed .tag .t { color: var(--text-muted); }

  /* Claimable: lit + glowing; the whole pool claims at once. */
  .cell.claimable {
    cursor: pointer;
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, var(--metal-0));
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 0 16px -3px var(--accent-glow);
    animation: cell-glow 2.2s var(--ease-in-out) infinite;
  }
  .cell.claimable .g, .cell.claimable .tag .t { color: var(--accent-bright); font-weight: 700; }
  .cell.claimable:hover:not(:disabled) { filter: brightness(1.08); }
  .cell.claimable:disabled { opacity: .6; cursor: progress; }
  .cell.claimable:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .pulse { animation: pulse 1s var(--ease-in-out) infinite; }

  /* ---- the dated 28-cell calendar: denser, so cells drop the icon + inline
     text label (a shared legend below carries the label once instead) and
     shrink to fit without forcing horizontal scroll on a phone. ---- */
  .weeks.dated .wk-row { min-width: 0; }
  .weeks.dated .wk-row.head { margin-bottom: -2px; }
  .weeks.dated .grid { grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 3px; }
  .wd { text-align: center; font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .04em; }
  .weeks.dated .cell { padding: 3px 1px; gap: 2px; border-radius: 4px; }
  .weeks.dated .date { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: 0; }
  .weeks.dated .amt { font-size: 10px; }
  .weeks.dated .g { font-size: var(--text-xs); line-height: 1; }

  .legend { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-3); }
  .legend-item { display: inline-flex; align-items: center; gap: 4px; font-size: var(--text-xs); color: var(--text-muted); }
  .legend-item .g { font-size: var(--text-sm); line-height: 1; color: var(--text); }

  .note { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }

  @keyframes cell-glow {
    0%, 100% { box-shadow: inset 0 1px 0 var(--metal-hi), 0 0 10px -4px var(--accent-glow); }
    50% { box-shadow: inset 0 1px 0 var(--metal-hi), 0 0 20px -2px var(--accent-glow); }
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.35); opacity: .7; }
  }
  @media (prefers-reduced-motion: reduce) {
    .cell.claimable, .pulse { animation: none; }
  }
</style>
