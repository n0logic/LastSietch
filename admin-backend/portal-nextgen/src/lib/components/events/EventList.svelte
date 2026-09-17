<script>
  // A column of event cards with the four-state read surface around it. Props
  // only: the route owns the fetch, the poll and the section heading. The prop
  // set is exactly `events status onOpen` (wave 9 contract, frozen), so the seal
  // copy lives here rather than arriving as three more props.
  //
  // The empty branch is a SealedPanel with `action="none"`. An empty list is an
  // honest answer (nothing is scheduled in this window) and it stays sealed
  // rather than growing a placeholder row; a login action here would ask a
  // signed-in player to do a thing that would not change the answer, and the
  // events list is public anyway.
  //
  // The cards in a list carry no remind toggle: this component takes no remind
  // state, so `reminded` stays null and RemindToggle renders absent, which is the
  // correct rendering of "we were not told". Remind lives on the detail surface;
  // a route that wants toggles in a list mounts EventCard itself.
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import EventCard from './EventCard.svelte';

  // status: 'loading' | 'error' | 'ready'
  let { events = [], status = 'loading', onOpen } = $props();

  let list = $derived(Array.isArray(events) ? events : []);
  let phase = $derived(status === 'ready' && list.length === 0 ? 'empty' : status);
</script>

{#if phase === 'ready'}
  <div class="list">
    {#each list as ev (ev.id)}
      <EventCard event={ev} reminded={null} {onOpen} />
    {/each}
  </div>
{:else if phase === 'empty'}
  <SealedPanel
    status="empty" action="none" width="prose"
    emptyText="Nothing is on the almanac for this window. Gatherings are posted here as they are called."
  />
{:else if phase === 'error'}
  <SealedPanel
    status="error" width="prose"
    errorText="The almanac could not be read right now. Try again shortly."
  />
{:else}
  <SealedPanel status="loading" loadingText="loading" />
{/if}

<style>
  .list { display: grid; gap: var(--space-4); grid-template-columns: 1fr; }
  @media (min-width: 900px) {
    .list { grid-template-columns: repeat(auto-fill, minmax(min(100%, 26rem), 1fr)); }
  }
</style>
