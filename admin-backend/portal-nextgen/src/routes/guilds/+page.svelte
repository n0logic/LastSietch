<script>
  // Guilds hub: the water-bond registry + the social layer. Shipped surfaces
  // (pending invites, own roster presence, description editor) stay as-is and LIVE.
  // Added: a filterable Signal Board (recruiting directory), a solo Seeker Wall
  // (LFG), officer member-management, recruiting toggle, join-requests, guild-inbox
  // access, and Solari gifting. Every surface fails closed: signed-out or a backend
  // that has not shipped an endpoint yet seals to an honest state, never a
  // fabricated one. Ibad-blue is reserved for live game data; recruiting is amber.
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { api } from '$lib/api.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import GuildInvitesPanel from '$lib/components/guild/GuildInvitesPanel.svelte';
  import GuildPresenceRibbon from '$lib/components/guild/GuildPresenceRibbon.svelte';
  import GuildDescriptionEditor from '$lib/components/guild/GuildDescriptionEditor.svelte';
  import GuildManageRoster from '$lib/components/guild/GuildManageRoster.svelte';
  import QuickRecruitToggle from '$lib/components/guild/QuickRecruitToggle.svelte';
  import JoinRequestsPanel from '$lib/components/guild/JoinRequestsPanel.svelte';
  import InboxConfigEditor from '$lib/components/guild/InboxConfigEditor.svelte';
  import GiftDialog from '$lib/components/guild/GiftDialog.svelte';
  import SignalBoard from '$lib/components/guild/SignalBoard.svelte';
  import SeekerWall from '$lib/components/guild/SeekerWall.svelte';

  const gate = useAuthGate();

  // Shipped surfaces.
  let invites = $state([]);
  let invitesStatus = $state('loading');
  let me = $state(null); // { in_guild, guild_id, guild_name, my_role_can_edit, description }
  let meStatus = $state('loading');
  let members = $state([]);
  let presenceStatus = $state('loading');

  // Social-layer directory (from /portal/guilds/data; may not have shipped yet).
  let directory = $state([]);
  let directoryStatus = $state('loading'); // loading | ready | error
  let myGuildData = $state(null);          // own guild row (with member controller ids)

  let inGuild = $derived(me?.in_guild === true && me?.guild_id != null);
  let canEdit = $derived(inGuild && me?.my_role_can_edit === true);
  // Viewer role in own guild: prefer the authoritative data feed, fall back to the
  // can-edit hint (Officer) when the directory has not shipped.
  let viewerRole = $derived(Number(myGuildData?.my_role_id) || (canEdit ? 50 : 1));
  let isLeader = $derived(viewerRole === 100);
  let manageMembers = $derived(Array.isArray(myGuildData?.members) ? myGuildData.members : []);
  let inboxCfg = $derived(myGuildData?.inbox_config || null);

  async function loadInvites() {
    invitesStatus = 'loading';
    try {
      const r = await api.guilds.invites();
      invites = Array.isArray(r) ? r : (r?.invites ?? []);
      invitesStatus = 'ready';
    } catch (e) { invites = []; invitesStatus = 'error'; }
  }

  // An answered hail changes both the invite list and (on accept) which guild is
  // yours, so refresh the invites, the guild summary and the directory together.
  function onInviteAnswered() {
    loadInvites();
    loadGuild();
    loadDirectory();
  }

  async function loadPresence(id) {
    presenceStatus = 'loading';
    try {
      const r = await api.guilds.presence(id);
      members = Array.isArray(r) ? r : (r?.members ?? []);
      presenceStatus = 'ready';
    } catch (e) { members = []; presenceStatus = 'error'; }
  }

  async function loadGuild() {
    meStatus = 'loading';
    try {
      const r = await api.guilds.me();
      me = r || null;
      meStatus = 'ready';
      if (r?.in_guild === true && r?.guild_id != null) loadPresence(r.guild_id);
    } catch (e) { me = null; meStatus = 'ready'; }
  }

  async function loadDirectory() {
    directoryStatus = 'loading';
    try {
      const r = await api.guilds.data();
      const guilds = Array.isArray(r) ? r : (r?.guilds ?? []);
      directory = Array.isArray(guilds) ? guilds : [];
      myGuildData = directory.find((g) => g?.is_mine === true) || null;
      directoryStatus = 'ready';
    } catch (e) {
      directory = []; myGuildData = null; directoryStatus = 'error';
    }
  }

  function loadAll() {
    loadInvites();
    loadGuild();
    loadDirectory();
  }

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadAll); }
    else if (status === 'anon') { loaded = false; }   // the directory needs a linked session; anon gets the sealed panel only
  });
</script>

<svelte:head>
  <title>Guilds | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | water-bond registry"
    title="Guilds"
    sub="Your invites, your guild, and the open calls across the desert. Answer the hails you want, raise your own signal, and keep your guild's beacon current."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see your guild invites, roster, and the recruiting board."
    />
  {:else}
    <div class="stack">
      <GuildInvitesPanel {invites} status={invitesStatus} onChanged={onInviteAnswered} />

      {#if meStatus === 'loading'}
        <p class="skeleton">loading</p>
      {:else if inGuild}
        <GuildPresenceRibbon {members} guildName={me?.guild_name || ''} status={presenceStatus} />

        {#if canEdit}
          {#if manageMembers.length > 0}
            <GuildManageRoster members={manageMembers} presence={members} guildId={me.guild_id} {viewerRole} onChanged={loadDirectory} />
          {/if}
          <section class="panel">
            <QuickRecruitToggle guildId={me.guild_id} recruiting={myGuildData?.recruiting || null} />
          </section>
          <JoinRequestsPanel guildId={me.guild_id} />
          {#if isLeader}
            <section class="panel">
              <InboxConfigEditor guildId={me.guild_id} config={inboxCfg} />
            </section>
          {/if}
          <section class="panel">
            <GuildDescriptionEditor guildId={me.guild_id} initialDescription={me?.description || ''} />
          </section>
        {/if}

        <section class="panel">
          <GiftDialog />
        </section>
      {:else}
        <SealedPanel
          status="empty" action="none" art="nobody-online"
          emptyText="You have not sworn to a guild yet. Answer a hail above, raise your signal below, or ask to join a guild on the Signal Board."
        />
      {/if}

      <SignalBoard guilds={directory} status={directoryStatus} canRequest={!inGuild} />

      <!-- Every guild on the Signal Board now has a page. The links live here
           rather than inside the board's cards so the board component stays
           exactly what shipped; one anchor per directory row, in the board's own
           load order. -->
      {#if directoryStatus === 'ready' && directory.length > 0}
        <section class="panel">
          <p class="kicker mono">Guild pages</p>
          <p class="hint">Open a guild to read its roster, its ranks and its recruiting call.</p>
          <ul class="sietch-links">
            {#each directory as g (g.guild_id)}
              <li><a href="{base}/guilds/{g.guild_id}">{g.name || g.guild_name || 'Unnamed guild'}</a></li>
            {/each}
          </ul>
        </section>
      {/if}

      <SeekerWall />
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .stack { display: flex; flex-direction: column; gap: var(--space-5); }
  .panel {
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-lg); padding: var(--space-5);
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 18px 44px -28px var(--shadow-cast);
  }

  .hint { margin: 0 0 var(--space-3); font-size: var(--text-xs); color: var(--text-muted); }
  .sietch-links { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .sietch-links a {
    display: inline-block; font-size: var(--text-sm); text-decoration: none; color: var(--text);
    padding: var(--space-1) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .sietch-links a:hover { color: var(--accent-text); border-color: var(--accent); }

  .stack > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .stack > :global(*:nth-child(1)) { animation-delay: .04s; }
  .stack > :global(*:nth-child(2)) { animation-delay: .10s; }
  .stack > :global(*:nth-child(3)) { animation-delay: .16s; }
  .stack > :global(*:nth-child(4)) { animation-delay: .22s; }
  .stack > :global(*:nth-child(n+5)) { animation-delay: .28s; }
  @keyframes rise { to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) {
    .stack > :global(*) { opacity: 1; transform: none; animation: none; }
  }
</style>
