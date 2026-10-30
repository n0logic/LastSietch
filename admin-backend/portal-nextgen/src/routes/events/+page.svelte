<script>
  // The Events board: what the sietch is doing next, and what it just did.
  //
  // Fully PUBLIC. A signed-out visitor reads both windows exactly as a linked
  // player does. The only thing a session buys is the reminder toggle, and the
  // only thing the admin ROLE buys is a link to the Events Manager in the admin
  // panel, where events are written since 2.22.0. The link is a convenience, not
  // a gate: the admin panel has its own login.
  //
  // Two windows, one board. Upcoming leads because that is what a player came
  // for; Past sits under it because a gathering nobody can attend any more is
  // still the answer to "did I miss it". A board with neither seals to ONE panel
  // rather than two empty headings: two seals in a row read as two failures.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { eventRemind, events, remindedFor, subscribe, toggleRemind } from '$lib/events.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import EventCard from '$lib/components/events/EventCard.svelte';
  import EventList from '$lib/components/events/EventList.svelte';
  import WeekendCard from '$lib/components/events/WeekendCard.svelte';
  import EventsNext from '$lib/components/events/EventsNext.svelte';
  import { shell, loadShell, shellExpectedNext } from '$lib/shellNext.svelte.js';

  const MANAGER_URL = 'https://admin.lastsietch.com/admin/v2/players/events';

  // The public calendar feed (the same published rows as this board). webcal://
  // asks a calendar app to SUBSCRIBE rather than import once; it needs the host,
  // so the https path stands in until the page is in a browser.
  const FEED_PATH = '/portal/events.ics';
  let feedUrl = $state(FEED_PATH);
  onMount(() => { feedUrl = `webcal://${location.host}${FEED_PATH}`; });

  // Role gate, server-granted. `allowed` is authed AND the role, and it fails
  // closed on an absent roles field.
  const admin = useAuthGate({ role: 'admin' });

  // One subscriber, one timer, one visibility listener for the whole page.
  onMount(subscribe);

  // The rebuilt page is dark behind the Home switch (shell.next). The shipped page
  // below draws whenever the switch is off or still unread, so a flag-off viewer
  // sees exactly what they saw before.
  onMount(() => { if (shell.status !== 'ready') loadShell(); });
  // Until the switch is read, hold a small panel only when the override or the last
  // read says it is on. Otherwise the shipped page draws at once, so flag-off never waits.
  const expectNext = shell.status !== 'ready' && shellExpectedNext();

  // The card callbacks hand back an ID, not the row.
  function openEvent(eventId) {
    const id = eventId != null ? String(eventId) : '';
    if (!id) return;
    goto(`${base}/events/${encodeURIComponent(id)}`);
  }

  function busyFor(eventId) {
    return eventRemind.busyId != null && String(eventRemind.busyId) === String(eventId);
  }

  let empty = $derived(
    events.status === 'ready' && events.live.length === 0 && events.upcoming.length === 0 && events.past.length === 0
  );

  // Upcoming carries the toggles, so the route mounts its cards itself and owns
  // the four-state surface EventList would otherwise have drawn around them.
  // Past stays on EventList: a gathering that has already happened cannot be
  // reminded about, so a toggle there would be a control with nothing behind it.
  let upcomingPhase = $derived(
    events.status === 'ready' && events.upcoming.length === 0 ? 'empty' : events.status
  );
</script>

<svelte:head>
  <title>Events | Last Sietch</title>
</svelte:head>

{#if shell.status === 'ready' && shell.next}
  <EventsNext {feedUrl} feedPath={FEED_PATH} managerUrl={MANAGER_URL} admin={admin.allowed} />
{:else if shell.status !== 'ready' && expectNext}
  <div class="page"><SealedPanel status="loading" loadingText="loading" /></div>
{:else}
<div class="page">
  <PageHeader
    kicker="Last Sietch | Sietch events"
    title="Events"
    sub="What the sietch is doing next, and what it has just done. Every weekend has an event, Friday 12:00 AM to Monday 12:00 AM Eastern: double XP on the 1st Friday of the month, double Landsraad on the 2nd, crafting on the 3rd and harvest on the 4th."
  >
    <a class="feed mono" href={`${base}/help#howto-weekend-events`}>How weekends work</a>
    <a class="btn ghost" href={feedUrl} data-sveltekit-reload title="Add the events board to your calendar app">Subscribe (calendar)</a>
    <a class="feed mono" href={FEED_PATH} data-sveltekit-reload title="The feed address, for calendars that ask for a URL">ICS link</a>
    {#if admin.allowed}
      <a class="btn" href={MANAGER_URL} target="_blank" rel="noopener">Manage events ↗</a>
    {/if}
  </PageHeader>

  {#if eventRemind.notice}
    <Notice tone={eventRemind.noticeTone} text={eventRemind.notice} />
  {/if}

  <!-- The rotation shows even on an empty board: a weekend with nothing
       published yet still reads as planned. Held until the first list read. -->
  {#if events.status === 'ready'}
    <section class="band weekend-band">
      <h2 class="band-title mono">Weekend events</h2>
      <WeekendCard events={[...events.live, ...events.upcoming]} month={true} />
    </section>
  {/if}

  {#if events.status === 'error'}
    <SealedPanel
      status="error" slab={true} width="prose"
      errorText="The events board could not be read right now. Try again shortly."
    />
  {:else if empty}
    <SealedPanel
      status="empty" action="none" art="no-events" slab={true} width="prose"
      emptyText="Nothing is on the board. When the sietch calls a gathering, it is posted here first."
    />
  {:else}
    {#if events.live.length > 0}
      <section class="band">
        <h2 class="band-title mono">Happening now</h2>
        <div class="cards">
          {#each events.live as ev (ev.id)}
            <EventCard
              event={ev}
              reminded={remindedFor(ev.id)}
              busy={busyFor(ev.id)}
              onToggleRemind={toggleRemind}
              onOpen={openEvent}
            />
          {/each}
        </div>
      </section>
    {/if}

    <section class="band">
      <h2 class="band-title mono">Upcoming</h2>
      {#if upcomingPhase === 'ready'}
        <div class="cards">
          {#each events.upcoming as ev (ev.id)}
            <EventCard
              event={ev}
              reminded={remindedFor(ev.id)}
              busy={busyFor(ev.id)}
              onToggleRemind={toggleRemind}
              onOpen={openEvent}
            />
          {/each}
        </div>
      {:else if upcomingPhase === 'empty'}
        <SealedPanel
          status="empty" action="none" width="prose"
          emptyText="Nothing is scheduled yet. Gatherings are posted here as they are called."
        />
      {:else if upcomingPhase === 'error'}
        <SealedPanel
          status="error" width="prose"
          errorText="The almanac could not be read right now. Try again shortly."
        />
      {:else}
        <SealedPanel status="loading" loadingText="loading" />
      {/if}
    </section>

    <section class="band">
      <h2 class="band-title mono">Past</h2>
      <EventList events={events.past} status={events.status} onOpen={openEvent} />
    </section>
  {/if}

</div>
{/if}

<style>
  /* Phone first: one column, and every band spaced the same. The grid the cards
     sit in is EventList's to own, not the page's. */
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  /* The same column the list component draws, so the two bands line up. */
  .cards { display: grid; gap: var(--space-4); grid-template-columns: 1fr; }
  @media (min-width: 900px) {
    .cards { grid-template-columns: repeat(auto-fill, minmax(min(100%, 26rem), 1fr)); }
  }

  .band { margin-top: var(--space-6); }
  .band:first-of-type { margin-top: 0; }
  /* Amber chrome. Ibad blue is live data and never a heading. */
  .band-title {
    color: var(--accent); text-transform: uppercase; letter-spacing: .28em;
    font-size: var(--text-xs); margin: 0 0 var(--space-3);
  }

  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-sm); cursor: pointer;
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
  }
  .btn { text-decoration: none; }
  .btn:hover { filter: brightness(1.08); }
  .btn.ghost { color: var(--accent); background: transparent; display: inline-block; margin: 0 var(--space-2) var(--space-2) 0; }
  .feed {
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    color: var(--text-muted); margin-right: var(--space-3);
  }
  .feed:hover { color: var(--accent); }

</style>
