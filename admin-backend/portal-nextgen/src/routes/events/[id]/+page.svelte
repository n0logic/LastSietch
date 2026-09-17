<script>
  // One event, deep linkable at /events/<id>. This is the link a
  // player pastes into Discord, so it has to answer for a stranger with no
  // session at all.
  //
  // Two things are deliberate here, both borrowed from bases/[id]:
  //   * the id is validated against a plain-integer pattern BEFORE any request,
  //     so a junk or enormous id never becomes a lookup and never reaches copy;
  //   * a missing event seals to one panel that says nothing about WHICH id was
  //     asked for. A draft, a deleted event and one that never existed are one
  //     answer, because telling them apart is exactly what a probe wants.
  //
  // The reminder wiring is the board's, unchanged: optimistic with a rollback,
  // and absent entirely when we cannot say whether it is set (anonymous, or a
  // failed read of the player's own reminders).
  import { onMount, untrack } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import {
    eventRemind, events, loadDetail, loadMine, remindedFor, toggleRemind,
  } from '$lib/events.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import EventDetail from '$lib/components/events/EventDetail.svelte';

  const gate = useAuthGate();

  // A plain positive integer, bounded, so a giant string is a client-side
  // refusal rather than a request the backend has to reject.
  let id = $derived(
    /^[1-9][0-9]{0,11}$/.test(String($page.params.id || '')) ? String($page.params.id) : ''
  );

  // The store outlives the route, so an event whose id is not the one in the URL
  // is the PREVIOUS page's, still loaded: it reads as loading rather than
  // flashing the wrong event for a frame.
  let stale = $derived(
    events.detail.status === 'ready' && String(events.detail.id) !== id
  );
  let phase = $derived(!id ? 'missing' : stale ? 'loading' : events.detail.status);
  let item = $derived(phase === 'ready' ? events.detail.event : null);

  // true | false | null. Null renders the toggle absent, which is the honest
  // answer for a visitor with no session and for a reminders read that failed.
  let reminded = $derived(item ? remindedFor(item.id) : null);
  let busy = $derived(
    item != null && eventRemind.busyId != null
    && String(eventRemind.busyId) === String(item.id)
  );

  let loadedId = '';
  $effect(() => {
    const want = id;
    if (!want || want === loadedId) return;
    loadedId = want;
    untrack(() => loadDetail(want));
  });

  // The board's poll belongs to the board. This page needs one thing the detail
  // read does not carry: whether THIS player has the reminder set. Read once on
  // mount, and again if a session resolves after we got here.
  onMount(() => { loadMine(); });
  let mineRead = false;
  $effect(() => {
    if (gate.status !== 'authed' || mineRead) return;
    mineRead = true;
    untrack(loadMine);
  });
</script>

<svelte:head>
  <title>{item?.title ? `${item.title} | Last Sietch` : 'Event | Last Sietch'}</title>
</svelte:head>

<div class="page">
  <PageHeader kicker="Last Sietch | Sietch events" title={item?.title || 'Event'}>
    <a class="back mono" href="{base}/events">Back to the board</a>
  </PageHeader>

  {#if eventRemind.notice}
    <Notice tone={eventRemind.noticeTone} text={eventRemind.notice} />
  {/if}

  {#if phase === 'loading' || phase === 'idle'}
    <SealedPanel status="loading" loadingText="loading" slab={true} />
  {:else if phase === 'missing'}
    <SealedPanel
      status="empty" action="none" art="no-events" slab={true} width="prose"
      emptyText="That event is not on the board. It may have been called off, or the link may be out of date. The board is still there."
    />
  {:else if phase === 'error'}
    <SealedPanel
      status="error" slab={true} width="prose"
      errorText="That event could not be read right now. Try again shortly."
    />
  {:else if item}
    <EventDetail event={item} {reminded} {busy} onToggleRemind={toggleRemind} />
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .back {
    display: inline-block; font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; color: var(--text-muted); text-decoration: none;
  }
  .back:hover { color: var(--accent-text); }
</style>
