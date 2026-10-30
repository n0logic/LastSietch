<script>
  // The month's weekends as a strip (P2). Props only: `weekends` are describeSlot()
  // results for the Eastern month. Each cell says what the weekend does and where
  // it stands, using stripEntry() so a failed, missed or unread effect can never
  // wear the live style. Phone: the strip scrolls sideways inside itself and the
  // page never does.
  import { base } from '$app/paths';
  import { dayText } from '$lib/weekend.js';
  import { monthName, stripEntry } from '$lib/eventsNext.js';

  let { weekends = [], now = Date.now() } = $props();

  let month = $derived(weekends.length ? monthName(weekends[0].startMs) : '');
</script>

{#if weekends.length > 0}
  <section class="month" aria-label={`Weekends in ${month}`}>
    <h2 class="band-title mono">{month} weekends</h2>
    <ol class="strip">
      {#each weekends as d (d.startMs)}
        {@const e = stripEntry(d, now)}
        <li class={e.kind} class:current={d.open}>
          <span class="day mono">{dayText(d.startMs)}</span>
          <span class="what">
            {#if d.event}<a href={`${base}/events/${d.event.id}`}>{d.info ? d.info.chip : d.title}</a>{:else}{d.info ? d.info.chip : 'No set bonus'}{/if}
          </span>
          <span class="badges">
            <span class="tag mono" data-kind={e.kind}>{e.text}</span>
            {#if e.restart}<span class="tag restart mono">Restart weekend</span>{/if}
          </span>
        </li>
      {/each}
    </ol>
  </section>
{/if}

<style>
  .band-title { color: var(--accent); text-transform: uppercase; letter-spacing: .28em; font-size: var(--text-xs); margin: 0 0 var(--space-3); }
  .strip { list-style: none; margin: 0; padding: 0 0 var(--space-2); display: flex; gap: var(--space-3); overflow-x: auto; scroll-snap-type: x proximity; }
  li {
    flex: 1 0 11rem; scroll-snap-align: start; display: grid; gap: var(--space-2); align-content: start;
    padding: var(--space-3); background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  li.planned, li.scheduled { border-style: dashed; }
  li.ended, li.done, li.open { opacity: .75; }
  li.current { border-color: var(--accent); }
  li.live { border-color: var(--ls-ibad); }
  li.notlive { border-color: var(--ls-red); }
  .day { font-size: var(--text-xs); letter-spacing: .08em; color: var(--text-muted); }
  .what { font-size: var(--text-sm); }
  .what a { color: inherit; text-decoration: none; }
  .what a:hover { color: var(--accent-text); text-decoration: underline; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .tag { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; padding: 2px 8px; border: 1px solid var(--edge-hi); border-radius: 99px; color: var(--text-muted); white-space: nowrap; }
  .tag[data-kind='live'] { color: var(--ls-ibad); border-color: var(--ls-ibad); }
  .tag[data-kind='notlive'] { color: var(--ls-red); border-color: var(--ls-red); }
  .tag.restart { border-style: dashed; }
</style>
