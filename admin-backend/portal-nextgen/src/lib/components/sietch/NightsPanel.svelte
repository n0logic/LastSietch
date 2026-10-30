<script>
  // The Seven Nights: the calendar of all seven, the public leaderboard, and the
  // opt-out. The board is public and already honours every opt-out server-side
  // ("A wanderer"); the toggle here writes the viewer's own sietch_board_hidden.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import TitleChip from '$lib/components/TitleChip.svelte';
  import { sietch, loadBoard, setSietchPref } from '$lib/sietch.svelte.js';
  import { COPY } from './copy.js';

  let { data } = $props();

  onMount(() => { if (sietch.board.status === 'idle') loadBoard(); });

  let days = $derived(COPY.nights.map((c) => {
    const row = (data?.calendar || []).find((r) => r.n === c.n) || {};
    return { ...c, state: row.state || 'upcoming', earned: Number(row.earned) || 0 };
  }));
  let hidden = $derived(data?.prefs?.board_hidden === true);
  // Flair names come from the catalog the server sent, so a flair that opens later
  // is never named in the bundle ahead of its night.
  function flairLabel(key) {
    if (!key) return '';
    const row = (data?.catalog || []).find((c) => c.pref_key === key);
    return row?.name || (key === 'nightwalker' ? 'Night Walker' : '');
  }
  const ET = new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'America/New_York' });
</script>

<div class="grid">
  <CarvedSlab>
    <p class="kicker mono">The calendar</p>
    <ol class="cal">
      {#each days as d (d.n)}
        <li class={d.state}>
          <span class="when mono">{ET.format(new Date(d.starts_at))}</span>
          <span class="num mono">Night {d.n}</span>
          <span class="title">{d.title}</span>
          <span class="earned mono">
            {#if d.state === 'upcoming'}{d.n === 7 ? 'Ends 7 PM ET' : ''}{:else if d.earned > 0}+{d.earned}{:else}none{/if}
          </span>
          <span class="desc">{d.hook}{#if d.award_text}<span class="award mono">{d.award_text}</span>{/if}</span>
        </li>
      {/each}
    </ol>
    <p class="fine">{COPY.event.dst_note}</p>
  </CarvedSlab>

  <CarvedSlab sharp>
    <p class="kicker mono">{COPY.board.title}</p>
    {#if sietch.board.status === 'loading' && sietch.board.rows.length === 0}
      <p class="skeleton">loading</p>
    {:else if sietch.board.status === 'error'}
      <p class="hint">The leaderboard could not be read right now.</p>
    {:else if sietch.board.rows.length === 0}
      <p class="hint">No lanterns lit yet. Be the first.</p>
    {:else}
      <ol class="board">
        {#each sietch.board.rows as r, i (i)}
          <li>
            <span class="rank mono">{r.rank || i + 1}</span>
            <span class="name">{r.name || COPY.board.anon_name}{#if flairLabel(r.flair)}<TitleChip title={{ key: r.flair, name: flairLabel(r.flair) }} />{/if}</span>
            <span class="pts mono">{Number(r.earned) || 0}</span>
          </li>
        {/each}
      </ol>
    {/if}
    <label class="optout">
      <input type="checkbox" checked={hidden} onchange={async (e) => { await setSietchPref('board_hidden', e.currentTarget.checked); loadBoard(); }} />
      <span>{COPY.board.opt_out}<span class="sub">You show as "{COPY.board.anon_name}". The board refreshes within a minute.</span></span>
    </label>
    {#if sietch.notice}<Notice tone={sietch.noticeTone} text={sietch.notice} />{/if}
  </CarvedSlab>
</div>

<style>
  .grid { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr); gap: var(--space-4); align-items: start; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .cal { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .cal li {
    display: grid; grid-template-columns: 6.5rem 4.5rem minmax(0, 1fr) auto; gap: var(--space-2); align-items: baseline;
    padding: var(--space-3) var(--space-2); border-bottom: 1px solid var(--border-subtle);
  }
  .cal li.tonight { background: color-mix(in srgb, var(--ls-melange-soft) 12%, transparent); border-left: 2px solid var(--ls-melange-soft); }
  .cal li.past .title { color: var(--text-muted); }
  .when, .num { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; }
  .title { font-family: var(--font-display); font-size: var(--text-lg); text-transform: uppercase; }
  .earned { font-size: var(--text-sm); color: var(--ls-green); text-align: right; }
  .cal li.upcoming .earned { color: var(--text-muted); font-size: var(--text-xs); }
  .desc { grid-column: 3 / -1; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.4; }
  .award { display: block; margin-top: 2px; font-size: var(--text-xs); letter-spacing: .06em; color: var(--ls-amber-bright); }
  :global([data-theme='day']) .award { color: #8a5613; }
  .cal li.past .earned:not(:empty) { color: var(--ls-green); }
  @media (max-width: 520px) {
    .cal li { grid-template-columns: 1fr auto; }
    .when { grid-column: 1; } .num { display: none; } .title { grid-column: 1; } .desc { grid-column: 1 / -1; } .earned { grid-row: 1 / span 2; grid-column: 2; align-self: center; }
  }
  .board { list-style: none; margin: 0 0 var(--space-4); padding: 0; }
  .board li { display: grid; grid-template-columns: 2rem minmax(0, 1fr) auto; gap: var(--space-2); align-items: baseline; padding: var(--space-2) 0; border-bottom: 1px solid var(--border-subtle); }
  .rank { color: var(--text-muted); }
  .board li:first-child .rank { color: var(--ls-amber-bright); }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pts { color: var(--ls-amber-bright); font-weight: 600; }
  :global([data-theme='day']) .pts { color: #8a5613; }
  .optout { display: flex; gap: var(--space-2); align-items: flex-start; cursor: pointer; font-size: var(--text-sm); }
  .optout input { width: 18px; height: 18px; margin-top: 2px; accent-color: var(--ls-melange-soft); }
  .sub { display: block; color: var(--text-muted); font-size: var(--text-xs); }
  .fine { font-size: var(--text-sm); color: var(--text-muted); margin: var(--space-3) 0 0; }
  .hint { font-size: var(--text-sm); color: var(--text-muted); margin: 0 0 var(--space-4); }
</style>
