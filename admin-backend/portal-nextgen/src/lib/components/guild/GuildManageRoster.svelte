<script>
  // Officer/Leader roster with per-member management. Rows come from
  // /portal/guilds/data (own guild only, and only when my_role_can_edit, which is
  // when the backend includes player_controller_id + role_id + is_self). Each row
  // offers a MemberActionMenu gated client-side by the viewer's role; the server
  // re-enforces. Member ops ship DARK, so actions surface "not yet enabled".
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import EliHint from './EliHint.svelte';
  import MemberActionMenu from './MemberActionMenu.svelte';

  // members: manage rows from /portal/guilds/data (role_id, is_self, controller id;
  // NO online_status). presence: rows from /portal/guilds/{id}/presence (which DO
  // carry online_status). We merge presence in by controller id (fallback name) so
  // the live dot is accurate without fanning the census across the directory.
  let { members = [], presence = [], guildId, viewerRole = 1, onChanged } = $props();

  let list = $derived(Array.isArray(members) ? members : []);
  let onlineByCtrl = $derived(
    new Map(
      (Array.isArray(presence) ? presence : [])
        .filter((p) => p?.online_status === 'Online')
        .map((p) => [p.player_controller_id, true])
    )
  );
  // Full presence rows by controller id, so an offline member can still show the
  // last-seen stamp the census carries.
  let presenceByCtrl = $derived(
    new Map(
      (Array.isArray(presence) ? presence : [])
        .filter((p) => p?.player_controller_id != null)
        .map((p) => [p.player_controller_id, p])
    )
  );
  let onlineByName = $derived(
    new Map(
      (Array.isArray(presence) ? presence : [])
        .filter((p) => p?.online_status === 'Online')
        .map((p) => [p.character_name, true])
    )
  );
  // Presence is binary: 'Online' (case-sensitive) or OFFLINE. A row we cannot match
  // into the presence census is offline, never "unknown" -- the census lists every
  // member, so an absent row means not online.
  function isOnline(m) {
    if (m?.online_status === 'Online') return true; // if data ever carries it
    if (m?.player_controller_id != null && onlineByCtrl.get(m.player_controller_id)) return true;
    return onlineByName.get(m?.character_name) === true;
  }
  function presenceLabel(m) {
    if (isOnline(m)) return 'Online';
    const seen = m?.last_activity || presenceByCtrl.get(m?.player_controller_id)?.last_activity;
    return seen ? `Offline · last seen ${seen}` : 'Offline';
  }
  // Leader first, then Officers, then Members; within a rank by name.
  let sorted = $derived(
    [...list].sort((a, b) => {
      const ar = Number(b?.role_id) || 0, br = Number(a?.role_id) || 0;
      if (ar !== br) return ar - br;
      return (a?.character_name || '').localeCompare(b?.character_name || '');
    })
  );

  function roleName(m) {
    const r = Number(m?.role_id) || 0;
    if (m?.role_name) return m.role_name;
    if (r === 100) return 'Leader';
    if (r === 50) return 'Officer';
    return 'Member';
  }
</script>

<CarvedSlab elevation={2}>
  <p class="kicker mono">Manage members</p>
  <EliHint text="Everyone in your guild. Officers can promote, demote, or remove." />
  {#if list.length === 0}
    <p class="sealed">No members to manage.</p>
  {:else}
    <ul class="roster">
      {#each sorted as m (m.player_controller_id ?? m.character_name)}
        <li class="row">
          <div class="who">
            <LiveDot tone={isOnline(m) ? 'live' : 'idle'} />
            <span class="name">{m.character_name || 'Unknown survivor'}</span>
            {#if m.is_self}<span class="you mono">you</span>{/if}
            <span class="role mono">{roleName(m)}</span>
            <span class="presence mono" class:offline={!isOnline(m)}>{presenceLabel(m)}</span>
          </div>
          <MemberActionMenu member={m} {guildId} {viewerRole} {onChanged} />
        </li>
      {/each}
    </ul>
  {/if}
</CarvedSlab>

<style>
  .kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .roster { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .row {
    display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .who { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); min-width: 0; }
  .name { font-size: var(--text-sm); color: var(--text); }
  .you { font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase; color: var(--accent-text); }
  .role { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .08em; text-transform: uppercase; }
  .presence { font-size: var(--text-xs); color: var(--ls-green); letter-spacing: .04em; }
  .presence.offline { color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .sealed { color: var(--text-muted); font-size: var(--text-sm); margin: 0; line-height: 1.45; }
</style>
