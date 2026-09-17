<script>
  // A thin ribbon of the viewer's own guild roster with live presence dots. The
  // parent only renders this for a guild the viewer belongs to (fail closed).
  // Online members sort to the front; the online count is plain amber text, the
  // Ibad cue lives only on each member's LiveDot (via RosterToken).
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import RosterToken from './RosterToken.svelte';

  // status: 'loading' | 'ready' | 'error'
  let { members = [], guildName = '', status = 'loading' } = $props();

  let list = $derived(Array.isArray(members) ? members : []);
  // Online first, then by name so the ribbon is stable across polls.
  let sorted = $derived(
    [...list].sort((a, b) => {
      const ao = a?.online_status === 'Online' ? 0 : 1;
      const bo = b?.online_status === 'Online' ? 0 : 1;
      if (ao !== bo) return ao - bo;
      return (a?.character_name || '').localeCompare(b?.character_name || '');
    })
  );
  let onlineCount = $derived(list.filter((m) => m?.online_status === 'Online').length);
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Roster{guildName ? ` | ${guildName}` : ''}</p>
    {#if status === 'ready' && list.length > 0}
      <span class="census mono">{onlineCount} of {list.length} online</span>
    {/if}
  </div>
  {#if status === 'loading'}
    <p class="skeleton">loading</p>
  {:else if status === 'error'}
    <SealedPanel status="error" errorText="Roster presence is not available right now." />
  {:else if list.length === 0}
    <SealedPanel
      status="empty" action="none" art="nobody-online"
      emptyText="No members are logged for this guild yet."
    />
  {:else}
    <div class="ribbon">
      {#each sorted as m (m.player_controller_id)}
        <RosterToken member={m} />
      {/each}
    </div>
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .census { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; white-space: nowrap; }
  .ribbon { display: flex; flex-wrap: wrap; gap: var(--space-2); }
</style>
