<script>
  // The signed-in player's pending guild invites. Parent only mounts this when
  // authed; here we own loading / empty / error presentation in the dry Sietch
  // voice. Empty is a SEALED state, never a fabricated row. Accepting or
  // declining a hail happens on the card; onChanged bubbles up so the parent can
  // refresh the invite list and the guild surfaces it just changed.
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import GuildInviteCard from './GuildInviteCard.svelte';

  // status: 'loading' | 'ready' | 'error'
  let { invites = [], status = 'loading', onChanged } = $props();
  let list = $derived(Array.isArray(invites) ? invites : []);
</script>

<CarvedSlab elevation={2}>
  <p class="kicker mono">Pending invites</p>
  {#if status === 'loading'}
    <p class="skeleton">loading</p>
  {:else if status === 'error'}
    <p class="sealed">The registry could not be reached. Try again shortly.</p>
  {:else if list.length === 0}
    <p class="sealed">No pending invites. The sietch has not called for you.</p>
  {:else}
    <div class="list">
      {#each list as inv (inv.invite_id)}
        <GuildInviteCard invite={inv} {onChanged} />
      {/each}
    </div>
  {/if}
</CarvedSlab>

<style>
  .kicker {
    margin: 0 0 var(--space-3); color: var(--accent);
    text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs);
  }
  .list { display: flex; flex-direction: column; gap: var(--space-3); }
  .sealed { color: var(--text-muted); font-size: var(--text-sm); margin: 0; line-height: 1.45; }
</style>
