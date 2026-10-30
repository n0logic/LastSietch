<script>
  // The rebuilt Events page (P2, direction A "The Weekend Clock"), shown only when
  // the shell switch is on (LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED or ?shell=next). The route
  // owns the store subscription; this component reads the same `events` store the
  // shipped page reads and the same weekend module, so the two can never disagree.
  //
  // Order: this weekend and next, the month strip, Seven Nights, recent events.
  // The page has no tabs of its own today. When the nav lands (P3) it sits under
  // Sietch as a tab and any tab state lives in the URL, not here.
  //
  // Public like the page it replaces: a signed-out visitor reads all of it. The
  // session only buys the reminder toggle; the admin role only buys a link.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { eventRemind, events, remindedFor, toggleRemind } from '$lib/events.svelte.js';
  import { effectOf, describeSlot, monthWeekends, weekendSlots } from '$lib/weekend.js';
  import { nightStates, nightsVisible, recentRows, recentWhen } from '$lib/eventsNext.js';
  import { COPY } from '$lib/components/sietch/copy.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import EffectChip from './EffectChip.svelte';
  import WeekendFeature from './WeekendFeature.svelte';
  import MonthStrip from './MonthStrip.svelte';
  import WeekendRemind from './WeekendRemind.svelte';

  let { feedUrl = '/portal/events.ics', feedPath = '/portal/events.ics', managerUrl = '', admin = false } = $props();

  let now = $state(Date.now());
  let zone = $state('');
  onMount(() => {
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch { zone = ''; }
    const t = setInterval(() => { now = Date.now(); }, 60_000);
    return () => clearInterval(t);
  });

  // Live and upcoming carry the runner's state; past rows let a finished month
  // weekend read "Finished" instead of "Planned".
  let all = $derived([...events.live, ...events.upcoming, ...events.past]);
  let slots = $derived(weekendSlots(now).map((s) => describeSlot(s, all, now)));
  let weekends = $derived(monthWeekends(slots[0].startMs).map((s) => describeSlot(s, all, now)));
  let nights = $derived(nightStates(COPY.nights, now));
  let showNights = $derived(nightsVisible(now, COPY.stall.closes_at));
  let tonight = $derived(nights.find((n) => n.state === 'tonight') || null);
  let recent = $derived(recentRows(events.past));

  function busyFor(id) {
    return eventRemind.busyId != null && String(eventRemind.busyId) === String(id);
  }

  // A past row never reads as live, whatever the column last said: the runner may
  // not have closed it yet. Only the states that describe a finished weekend show.
  function pastState(ev) {
    const s = effectOf(ev)?.state;
    return s === 'done' || s === 'failed' || s === 'missed' ? s : null;
  }
</script>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Sietch events"
    title="Events"
    sub="Every weekend has an event, Friday 12:00 AM to Monday 12:00 AM Eastern: double XP on the 1st Friday of the month, double Landsraad on the 2nd, crafting on the 3rd and harvest on the 4th."
  >
    <a class="feed mono" href={`${base}/help#howto-weekend-events`}>How weekends work</a>
    <a class="btn ghost" href={feedUrl} data-sveltekit-reload title="Add the events board to your calendar app">Subscribe (calendar)</a>
    <a class="feed mono" href={feedPath} data-sveltekit-reload title="The feed address, for calendars that ask for a URL">ICS link</a>
    {#if admin}
      <a class="btn" href={managerUrl} target="_blank" rel="noopener">Manage events ↗</a>
    {/if}
  </PageHeader>

  {#if eventRemind.notice}
    <Notice tone={eventRemind.noticeTone} text={eventRemind.notice} />
  {/if}

  {#if events.status === 'error'}
    <SealedPanel
      status="error" slab={true} width="prose"
      errorText="The events board could not be read right now. Try again shortly."
    />
  {:else if events.status !== 'ready'}
    <SealedPanel status="loading" loadingText="loading" />
  {:else}
    <section class="band">
      <WeekendFeature
        slot={slots[0]} next={slots[1]} {zone}
        reminded={slots[0].event ? remindedFor(slots[0].event.id) : null}
        busy={slots[0].event ? busyFor(slots[0].event.id) : false}
        onToggleRemind={toggleRemind}
      />
    </section>

    <WeekendRemind />

    <section class="band"><MonthStrip {weekends} {now} /></section>

    {#if showNights}
    <section class="band" aria-labelledby="sn-title">
      <h2 id="sn-title" class="band-title mono">Seven Nights of the Sietch</h2>
      <div class="nights-card">
        <ol class="nights">
          {#each nights as n (n.n)}
            <li class={n.state}>
              <span class="num mono">Night {n.n}</span>
              <span class="nt">{n.title}</span>
              <span class="ns mono">{n.state === 'tonight' ? 'Tonight' : n.state === 'past' ? 'Done' : 'Soon'}</span>
            </li>
          {/each}
        </ol>
        <div class="nights-foot">
          {#if tonight}
            <p class="tonight"><span class="cl mono">Tonight ends in</span> <LiveCountdown target={tonight.endsAt} /></p>
          {:else}
            <p class="muted">{nights.every((n) => n.state === 'past') ? 'The nights are done. The Token Stall is still open.' : 'Opens soon.'}</p>
          {/if}
          <a class="btn ghost" href={`${base}/sietch`}>Open the Seven Nights</a>
        </div>
      </div>
    </section>
    {/if}

    <section class="band" aria-labelledby="rc-title">
      <h2 id="rc-title" class="band-title mono">Recent events</h2>
      {#if recent.length > 0}
        <ul class="recent">
          {#each recent as ev (ev.id)}
            {@const eff = effectOf(ev)}
            <li>
              <a class="rt" href={`${base}/events/${ev.id}`}>{ev.title}</a>
              <span class="rw mono">{recentWhen(ev)}</span>
              {#if eff}<EffectChip effect={eff.effect} state={pastState(ev)} scope={eff.scope} open={false} />{/if}
            </li>
          {/each}
        </ul>
      {:else}
        <SealedPanel
          status="empty" action="none" width="prose"
          emptyText="Nothing is on the almanac for this window. Gatherings are posted here as they are called."
        />
      {/if}
    </section>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .band { margin-top: var(--space-6); }
  .band-title { color: var(--accent); text-transform: uppercase; letter-spacing: .28em; font-size: var(--text-xs); margin: 0 0 var(--space-3); }

  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-2) var(--space-4); border-radius: var(--radius-sm); cursor: pointer; text-decoration: none;
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
  }
  .btn:hover { filter: brightness(1.08); }
  .btn.ghost { color: var(--accent); background: transparent; display: inline-block; margin: 0 var(--space-2) var(--space-2) 0; }
  .feed { font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; color: var(--text-muted); margin-right: var(--space-3); }
  .feed:hover { color: var(--accent); }

  .nights-card { padding: var(--space-4); background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm); display: grid; gap: var(--space-4); }
  .nights { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-2); grid-template-columns: 1fr; }
  @media (min-width: 760px) { .nights { grid-template-columns: repeat(7, minmax(0, 1fr)); } }
  .nights li { display: grid; gap: 2px; padding: var(--space-2) var(--space-3); border: 1px solid var(--edge); border-radius: var(--radius-sm); }
  .nights li.tonight { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); }
  .nights li.past { opacity: .65; }
  .num, .ns { font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; color: var(--text-muted); }
  .nights li.tonight .num { color: var(--accent-text); }
  .nt { font-size: var(--text-sm); }
  .nights-foot { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-3); }
  .nights-foot .btn { margin: 0; }
  .tonight, .muted { margin: 0; }
  .muted { color: var(--text-muted); font-size: var(--text-sm); }
  .tonight { display: flex; align-items: baseline; gap: var(--space-3); font: 500 1.4rem/1 var(--font-mono); }
  .tonight :global(*) { font: inherit; }
  .cl { font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--text-muted); }

  .recent { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-2); }
  .recent li {
    display: grid; gap: var(--space-2); grid-template-columns: 1fr; align-items: center;
    padding: var(--space-3); background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  @media (min-width: 760px) { .recent li { grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr) auto; } }
  .rt { text-decoration: none; overflow-wrap: anywhere; }
  .rt:hover { color: var(--accent-text); text-decoration: underline; }
  .rw { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; }
</style>
