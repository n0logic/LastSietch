<script>
  // Amtal kill boards and kill-streak bounties (BOUNTIES-SPEC §3). Reads the
  // amtal-pvp store (public /portal/amtal/pvp): names, counts and spice only.
  // Staff play too and stay on the board, marked staff (owner 2026-10-20).
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import { pvp, subscribePvp } from '$lib/amtal-pvp.svelte.js';

  onMount(() => subscribePvp());

  let range = $state('week');

  // Titles (owner 2026-10-20): staff appear on the board but never hold one.
  const titleNote = $derived(pvp.titles?.rules ? [
    ['Sardaukar of the Week', `most kills last week, at least ${pvp.titles.rules.sardaukar_min_kills}`],
    ['Kanly', `longest kill streak last week, at least ${pvp.titles.rules.kanly_min_streak}`],
    ['Headtaker', 'collected a streak bounty'],
    ['Blooded', `${pvp.titles.rules.blooded_min_kills} kills this season`],
  ] : []);
  function holder(title) { return pvp.titles?.weekly?.[title] ?? null; }
  const rows = $derived(range === 'week' ? pvp.week : pvp.season);

  function ago(stamp) {
    if (!stamp) return '';
    const t = Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(String(stamp)) ? stamp : `${stamp}Z`);
    if (!Number.isFinite(t)) return '';
    const mins = Math.max(0, Math.floor((Date.now() - t) / 60000));
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 48) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }
</script>

{#if pvp.enabled}
  <CarvedSlab live>
    <p class="kicker mono">Amtal PvP</p>
    <h2 class="pname">Who rules the open sand</h2>
    <p class="pintro">
      Player kills on Amtal. Killing the same player again within 10 minutes does not count.
      String together {pvp.streakStep} kills without dying and a bounty of
      {pvp.streakSpice.toLocaleString()} spice goes on your head, growing every {pvp.streakStep} kills.
      Whoever ends the streak collects it.
    </p>

    {#if pvp.status === 'loading' || pvp.status === 'idle'}
      <p class="skeleton mono">reading the board</p>
    {:else if pvp.status === 'error' || !pvp.available}
      <Notice tone="warn" text="The PvP board is not answering right now. Try again in a few minutes." />
    {:else}
      {#if titleNote.length}
        <h3 class="sub mono">Titles{#if pvp.titles.last_week_label}<span class="wk">week {pvp.titles.last_week_label}</span>{/if}</h3>
        <ul class="titles" role="list">
          {#each titleNote as [t, rule] (t)}
            <li>
              <span class="tchip">{t}</span>
              <span class="tholder" class:none={!holder(t)}>{#if t === 'Sardaukar of the Week' || t === 'Kanly'}{holder(t) ?? 'unclaimed'}{/if}</span>
              <span class="trule">{rule}</span>
            </li>
          {/each}
        </ul>
        <p class="tnote">Staff appear on the board but cannot hold a title.</p>
      {/if}

      <h3 class="sub mono">On a streak</h3>
      {#if pvp.streaks.length === 0}
        <p class="empty mono">Nobody is on a streak. The sand is quiet, for now.</p>
      {:else}
        <ul class="list" role="list">
          {#each pvp.streaks as s (s.name)}
            <li class="row streak" class:marked={s.bounty > 0}>
              <span class="name">{s.name}{#if s.staff}<span class="staff mono">staff</span>{/if}{#each s.titles ?? [] as t (t)}<span class="tchip sm">{t}</span>{/each}</span>
              <span class="mono">{s.kills} {s.kills === 1 ? 'kill' : 'kills'}</span>
              <span class="spice mono">{s.bounty > 0 ? `${s.bounty.toLocaleString()} spice bounty` : `${pvp.streakStep - (s.kills % pvp.streakStep)} to a bounty`}</span>
            </li>
          {/each}
        </ul>
      {/if}

      <div class="head">
        <h3 class="sub mono">Kills</h3>
        <div class="seg" role="group" aria-label="Board range">
          <button type="button" class:on={range === 'week'} aria-pressed={range === 'week'} onclick={() => (range = 'week')}>This week</button>
          <button type="button" class:on={range === 'season'} aria-pressed={range === 'season'} onclick={() => (range = 'season')}>Season</button>
        </div>
      </div>
      {#if rows.length === 0}
        <p class="empty mono">{range === 'week' ? 'No kills on Amtal yet this week.' : 'No kills on Amtal yet this season.'}</p>
      {:else}
        <table class="board">
          <thead><tr><th>#</th><th>Name</th><th>Kills</th><th>Deaths</th><th>K/D</th><th>Best streak</th></tr></thead>
          <tbody>
            {#each rows as e, i (e.name)}
              <tr>
                <td class="mono">{i + 1}</td>
                <td>{e.name}{#if e.staff}<span class="staff mono">staff</span>{/if}{#each e.titles ?? [] as t (t)}<span class="tchip sm">{t}</span>{/each}</td>
                <td class="mono">{e.kills}</td>
                <td class="mono">{e.deaths}</td>
                <td class="mono">{e.kd}</td>
                <td class="mono">{e.best_streak}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      {#if pvp.collected.length}
        <h3 class="sub mono">Bounties collected</h3>
        <ul class="list" role="list">
          {#each pvp.collected as c (`${c.victim}-${c.at}`)}
            <li class="row">
              <span>{c.collector ?? 'The desert'} ended {c.victim}'s {c.streak}-kill streak</span>
              <span class="spice mono">{c.collector ? `${c.bounty.toLocaleString()} spice` : 'unclaimed'}</span>
              <span class="age mono">{ago(c.at)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  </CarvedSlab>
{/if}

<style>
  .pname { margin: 0.1rem 0 0.4rem; }
  .pintro { color: var(--text-muted); margin-bottom: var(--space-4); max-width: 60ch; }
  .skeleton, .empty { color: var(--text-muted); }
  .sub { margin: var(--space-4) 0 var(--space-2); font-size: 0.8rem; text-transform: uppercase; letter-spacing: .12em; color: var(--text-muted); }
  .head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  .seg { display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; }
  .seg button { background: none; border: 0; color: var(--text-muted); padding: 0.3rem 0.7rem; font: inherit; font-size: 0.85rem; cursor: pointer; }
  .seg button.on { background: var(--panel-2, rgba(0,0,0,.18)); color: var(--text); }
  .list { display: flex; flex-direction: column; gap: 0.4rem; margin: 0; padding: 0; }
  .row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.65rem;
    border: 1px solid var(--edge);
    border-radius: var(--radius-sm);
    background: var(--panel-2, rgba(0,0,0,.18));
  }
  .row.marked { border-color: var(--live, #4db6d8); }
  .name { font-weight: 600; }
  .staff { margin-left: 0.5rem; padding: 0 0.35rem; border: 1px solid var(--edge); border-radius: 3px; font-size: 0.68rem; text-transform: uppercase; letter-spacing: .1em; color: var(--text-muted); vertical-align: middle; }
  /* Ibad blue is reserved for live data. */
  .spice { color: var(--live, #4db6d8); font-weight: 700; }
  .age { color: var(--text-muted); font-size: 0.8rem; }
  .wk { margin-left: 0.6rem; text-transform: none; letter-spacing: 0; }
  .titles { display: grid; gap: 0.35rem; margin: 0; padding: 0; list-style: none; }
  .titles { grid-template-columns: max-content max-content 1fr; column-gap: 0.75rem; }
  .titles li { display: contents; }
  .tchip { display: inline-block; padding: 0.05rem 0.45rem; border: 1px solid var(--accent, #d9822b); border-radius: 3px; color: var(--accent, #d9822b); font-size: 0.78rem; font-weight: 600; white-space: nowrap; }
  .titles .tchip { justify-self: start; }
  .tchip.sm { margin-left: 0.4rem; font-size: 0.68rem; vertical-align: middle; }
  .trule { color: var(--text-muted); font-size: 0.85rem; }
  .tholder { font-weight: 600; }
  .tholder.none { color: var(--text-muted); font-weight: 400; }
  .tnote { color: var(--text-muted); font-size: 0.78rem; margin: 0.35rem 0 0; }
  .board { width: 100%; border-collapse: collapse; }
  .board th { text-align: left; font-weight: 600; font-size: 0.78rem; color: var(--text-muted); padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--edge); }
  .board td { padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border-subtle, var(--edge)); }
  @media (max-width: 560px) {
    .row { grid-template-columns: 1fr auto; row-gap: 0.2rem; }
    .age { grid-column: 1 / -1; }
    .titles { grid-template-columns: max-content 1fr; row-gap: 0.15rem; }
    .trule { grid-column: 1 / -1; margin-bottom: 0.35rem; }
    .board th:nth-child(4), .board td:nth-child(4), .board th:nth-child(6), .board td:nth-child(6) { display: none; }
    /* A long title chip may wrap here, or a wide font pushes the board off a phone. */
    .tchip.sm { white-space: normal; }
  }
</style>
