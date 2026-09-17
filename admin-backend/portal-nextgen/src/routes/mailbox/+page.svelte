<script>
  // Mailbox surface: personal inbox, DM composer, and (for an officer who may view
  // it) their guild inbox. Session-gated; anon seals. The guild-inbox context is
  // derived from /portal/guilds/data (own guild role vs the inbox config's
  // view/manage minimums); if that feed has not shipped, the guild tab simply does
  // not appear and the personal mailbox still works.
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { api } from '$lib/api.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import Mailbox from '$lib/components/guild/Mailbox.svelte';
  import DeliveriesPanel from '$lib/components/mailbox/DeliveriesPanel.svelte';
  import PlayerDirectory from '$lib/components/guild/PlayerDirectory.svelte';

  const gate = useAuthGate();

  let guildInbox = $state(null); // { guild_id, name, canManage } | null

  async function resolveGuildInbox() {
    try {
      const r = await api.guilds.data();
      const guilds = Array.isArray(r) ? r : (r?.guilds ?? []);
      const mine = guilds.find((g) => g?.is_mine === true);
      if (!mine) { guildInbox = null; return; }
      const role = Number(mine.my_role_id) || 1;
      const cfg = mine.inbox_config || {};
      const viewMin = Number(cfg.view_min_role) || 50;
      const manageMin = Number(cfg.manage_min_role) || 100;
      if (role >= viewMin) {
        guildInbox = { guild_id: mine.guild_id, name: mine.name || 'Your sietch', canManage: role >= manageMin };
      } else {
        guildInbox = null;
      }
    } catch (e) {
      guildInbox = null; // feed not shipped -> personal mailbox only
    }
  }

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(resolveGuildInbox); }
    else if (status === 'anon') { loaded = false; }
  });
</script>

<svelte:head>
  <title>Mailbox | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | courier"
    title="Mailbox"
    sub="Hails, notices, and the word your sietch sends. Read what waits, and send your own."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to read your mailbox and send word across the desert."
    />
  {:else}
    <div class="stack">
      <!-- What the server sent this player sits ABOVE the inbox: it is the thing
           they came here to check when a whisper told them a package landed, and
           the Home card's `#deliveries` link lands on it. Every one of its seals
           lives inside the component, so this page keeps exactly one login gate. -->
      <DeliveriesPanel />
      <Mailbox {guildInbox} />
      <PlayerDirectory />
      <!-- The visibility toggle moved to Settings in wave 4. The directory is
           right above this line, so say where its switch went. -->
      <p class="dir-note">Whether you appear in this directory, and the line you show beside your name, are set in <a href={`${base}/settings`}>Settings</a>.</p>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .stack { display: flex; flex-direction: column; gap: var(--space-5); }
  .dir-note { margin: 0; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; }
  .dir-note a { color: var(--accent-text); }
</style>
