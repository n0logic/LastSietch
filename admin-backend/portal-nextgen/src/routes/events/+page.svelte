<script>
  // The Events board: what the sietch is doing next, and what it just did.
  //
  // Fully PUBLIC. A signed-out visitor reads both windows exactly as a linked
  // player does. The only thing a session buys is the reminder toggle, and the
  // only thing the admin ROLE buys is the composer. Neither is decided here: the
  // gate below hides controls that would 404 anyway, because the server is the
  // one place a role is decided and the browser is not a trust boundary.
  //
  // Two windows, one board. Upcoming leads because that is what a player came
  // for; Past sits under it because a gathering nobody can attend any more is
  // still the answer to "did I miss it". A board with neither seals to ONE panel
  // rather than two empty headings: two seals in a row read as two failures.
  //
  // The composer is mounted only inside the admin branch, not merely hidden by
  // it. A form that exists in the DOM for every visitor is a form a visitor can
  // find, and its submit would be a request we already know the answer to.
  import { onMount, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import {
    EVENT_BANNERS, EVENT_KINDS, cancelEvent, closeComposer, eventAdmin, eventRemind,
    events, loadAdminList, openComposer, remindedFor, submitComposer, subscribe,
    toggleRemind,
  } from '$lib/events.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import EventCard from '$lib/components/events/EventCard.svelte';
  import EventList from '$lib/components/events/EventList.svelte';
  import EventComposer from '$lib/components/events/EventComposer.svelte';

  // Role gate, server-granted. `allowed` is authed AND the role, and it fails
  // closed on an absent roles field.
  const admin = useAuthGate({ role: 'admin' });

  // One subscriber, one timer, one visibility listener for the whole page.
  onMount(subscribe);

  // The admin list is the only read that can see drafts, and it 404s for anyone
  // without the role. Asked once, and only once the gate says yes.
  let adminRead = false;
  $effect(() => {
    if (!admin.allowed || adminRead) return;
    adminRead = true;
    untrack(loadAdminList);
  });

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
    events.status === 'ready' && events.upcoming.length === 0 && events.past.length === 0
  );

  // Upcoming carries the toggles, so the route mounts its cards itself and owns
  // the four-state surface EventList would otherwise have drawn around them.
  // Past stays on EventList: a gathering that has already happened cannot be
  // reminded about, so a toggle there would be a control with nothing behind it.
  let upcomingPhase = $derived(
    events.status === 'ready' && events.upcoming.length === 0 ? 'empty' : events.status
  );

  // Drafts and cancellations never reach the public windows, so the admin rows
  // are the only place they can be reopened for editing.
  let adminRows = $derived(Array.isArray(eventAdmin.rows) ? eventAdmin.rows : []);
</script>

<svelte:head>
  <title>Events | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Sietch events"
    title="Events"
    sub="What the sietch is doing next, and what it has just done."
  >
    {#if admin.allowed}
      <button class="btn" type="button" onclick={() => openComposer(null)}>New event</button>
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
  {:else if empty}
    <SealedPanel
      status="empty" action="none" art="no-events" slab={true} width="prose"
      emptyText="Nothing is on the board. When the sietch calls a gathering, it is posted here first."
    />
  {:else}
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

  {#if admin.allowed}
    <section class="band">
      <h2 class="band-title mono">All events</h2>
      {#if eventAdmin.status === 'error'}
        <SealedPanel
          status="error" width="prose"
          errorText="The full event list could not be read. The public board above is unaffected."
        />
      {:else if eventAdmin.status === 'ready' && adminRows.length === 0}
        <SealedPanel
          status="empty" action="none" width="prose"
          emptyText="No events exist yet, drafts included."
        />
      {:else if adminRows.length}
        <ul class="admin">
          {#each adminRows as row (row.id)}
            <li>
              <button class="row" type="button" onclick={() => openComposer(row)}>
                <span class="rtitle">{row.title}</span>
                <span class="rmeta mono">
                  <span class="rstatus">{row.status}</span>
                  {#if row.starts_utc}<time datetime={row.starts_utc}>{row.starts_utc} UTC</time>{/if}
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {:else}
        <SealedPanel status="loading" loadingText="reading every event" />
      {/if}
    </section>
  {/if}
</div>

{#if admin.allowed && eventAdmin.open}
  <EventComposer
    open={eventAdmin.open}
    mode={eventAdmin.mode}
    event={eventAdmin.target}
    kinds={EVENT_KINDS}
    banners={EVENT_BANNERS}
    busy={eventAdmin.busy}
    error={eventAdmin.error}
    onSubmit={submitComposer}
    onCancelEvent={cancelEvent}
    onClose={closeComposer}
  />
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
  .btn:hover { filter: brightness(1.08); }

  .admin { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .row {
    width: 100%; display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-1) var(--space-3);
    text-align: left; cursor: pointer; padding: var(--space-2) var(--space-3);
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm);
  }
  .row:hover { border-color: var(--accent); }
  .rtitle { font-size: var(--text-sm); min-width: 0; overflow-wrap: anywhere; }
  .rmeta { display: flex; flex-wrap: wrap; gap: var(--space-2); font-size: 10px; color: var(--text-muted); }
  .rstatus { text-transform: uppercase; letter-spacing: .1em; }
</style>
