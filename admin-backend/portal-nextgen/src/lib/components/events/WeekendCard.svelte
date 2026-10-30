<script>
  // This weekend / Next weekend (player portal plan B2). Props only: the route
  // hands in the published events it already reads (live + upcoming), and this
  // card matches them to the rotation in $lib/weekend.js.
  //
  // A published event wins over the rotation. With none, the rotation's plan
  // shows as "Planned", never as confirmed. Times print in Eastern, the server's
  // clock, with the viewer's own reading beside them when it differs.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { dayText, describeSlot, monthWeekends, timePair, weekendSlots } from '$lib/weekend.js';
  import EffectChip from './EffectChip.svelte';

  let { events = [], month = false } = $props();

  // One minute is plenty: the card changes state at a weekend's edges.
  let now = $state(Date.now());
  let zone = $state('');
  onMount(() => {
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch { zone = ''; }
    const t = setInterval(() => { now = Date.now(); }, 60_000);
    return () => clearInterval(t);
  });

  let slots = $derived(weekendSlots(now).map((s) => describeSlot(s, events, now)));
  let strip = $derived(month ? monthWeekends(slots[0].startMs).map((s) => describeSlot(s, events, now)) : []);
  const LABELS = ['This weekend', 'Next weekend'];
</script>

<section class="weekend" aria-label="Weekend events">
  {#each slots as d, i}
    {@const start = timePair(d.startMs, zone)}
    {@const end = timePair(d.endMs, zone)}
    <article class="slot" class:open={d.open}>
      <p class="label mono">{d.open ? 'Happening now' : LABELS[i]}{#if d.planned} · Planned{/if}</p>
      <h3>
        {#if d.event}<a href={`${base}/events/${d.event.id}`}>{d.title}</a>{:else}{d.title}{/if}
      </h3>
      {#if d.info}
        <EffectChip effect={d.effect} state={d.state} scope={d.scope} open={d.open} detail={true} />
      {:else}
        <p class="meaning">No set effect for this weekend yet. Watch the Events page.</p>
      {/if}
      <dl class="when">
        <div>
          <dt>{d.open ? 'Started' : 'Starts'}</dt>
          <dd>{start.et}{#if start.local}<span class="local">{start.local} your time</span>{/if}</dd>
        </div>
        <div>
          <dt>Ends</dt>
          <dd>{end.et}{#if end.local}<span class="local">{end.local} your time</span>{/if}</dd>
        </div>
      </dl>
      {#if d.planned && d.info}
        <p class="note">In the weekend rotation. Not on the events board yet.</p>
      {/if}
    </article>
  {/each}

  {#if strip.length > 0}
    <ol class="strip" aria-label="Weekends this month">
      {#each strip as d}
        <li class:planned={d.planned} class:open={d.open}>
          <span class="day mono">{dayText(d.startMs)}</span>
          <span class="what">{d.info ? d.info.chip : 'Open'}</span>
          {#if d.planned}<span class="tag mono">Planned</span>{/if}
        </li>
      {/each}
    </ol>
  {/if}
</section>

<style>
  .weekend { display: grid; gap: var(--space-4); grid-template-columns: 1fr; }
  @media (min-width: 760px) { .weekend { grid-template-columns: 1fr 1fr; } }
  .slot {
    padding: var(--space-4); background: var(--panel); border: 1px solid var(--edge);
    border-top: 2px solid var(--accent-soft); border-radius: var(--radius-sm); min-width: 0;
  }
  .slot.open { border-top-color: var(--ls-ibad); }
  .label { margin: 0 0 var(--space-2); font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--accent); }
  .slot.open .label { color: var(--ls-ibad); }
  h3 { margin: 0 0 var(--space-3); }
  h3 a { text-decoration: none; }
  .meaning, .note { margin: 0; font-size: var(--text-sm); color: var(--text-muted); }
  .note { margin-top: var(--space-3); font-size: var(--text-xs); }
  .when { margin: var(--space-3) 0 0; display: grid; gap: var(--space-2); font-size: var(--text-sm); }
  .when div { display: grid; grid-template-columns: 4.5rem 1fr; gap: var(--space-2); }
  .when dt { color: var(--text-muted); }
  .when dd { margin: 0; overflow-wrap: anywhere; }
  .local { display: block; color: var(--text-muted); font-size: var(--text-xs); }
  .strip { grid-column: 1 / -1; list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .strip li { display: flex; flex-direction: column; gap: 2px; padding: var(--space-2) var(--space-3); border: 1px solid var(--edge); border-radius: var(--radius-sm); font-size: var(--text-sm); min-width: 8rem; }
  .strip li.planned { opacity: .75; border-style: dashed; }
  .strip li.open { border-color: var(--ls-ibad); }
  .strip .day, .strip .tag { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .08em; }
</style>
