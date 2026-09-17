<script>
  // Mailbox surface: the player's personal inbox, a DM composer, and (for an
  // officer) their guild inbox as a second tab. All admin.db, session-gated; the
  // sender/recipient identity is server-resolved. Reads mark messages read on the
  // server and reconcile the topbar bell badge via the shared mailbox store.
  import { onMount, untrack } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import EliHint from './EliHint.svelte';
  import MessageList from './MessageList.svelte';
  import MessageComposer from './MessageComposer.svelte';
  import { api } from '$lib/api.js';
  import { refreshUnread, decrementUnread } from '$lib/mailbox.svelte.js';

  // guildInbox: { guild_id, name, canManage } | null (officer of a guild).
  let { guildInbox = null } = $props();

  let tab = $state('inbox'); // 'inbox' | 'compose' | 'guild'

  let messages = $state([]);
  let inboxStatus = $state('loading');
  let guildMessages = $state([]);
  let guildStatus = $state('idle');
  let busyId = $state(null);
  let replyTo = $state(''); // char name prefilled into the composer
  // Transient outcome of a guild-inbox Invite action (dark-flag aware).
  let inviteNote = $state(null); // { phase: 'invited'|'deferred'|'failed', message }

  async function loadInbox() {
    inboxStatus = 'loading';
    try {
      const r = await api.messages.list();
      messages = Array.isArray(r?.messages) ? r.messages : [];
      inboxStatus = 'ready';
    } catch (e) {
      messages = []; inboxStatus = 'error';
    }
    refreshUnread();
  }

  async function loadGuildInbox() {
    if (!guildInbox?.guild_id) return;
    guildStatus = 'loading';
    try {
      const r = await api.messages.guildMessages(guildInbox.guild_id);
      guildMessages = Array.isArray(r?.messages) ? r.messages : [];
      guildStatus = 'ready';
    } catch (e) {
      guildMessages = []; guildStatus = 'error';
    }
  }

  function markLocalRead(listRef, id) {
    return listRef.map((m) => (m.id === id ? { ...m, state: 'read' } : m));
  }

  async function readPlayer(m) {
    if (busyId) return;
    busyId = m.id;
    try {
      await api.messages.read(m.id);
      messages = markLocalRead(messages, m.id);
      decrementUnread(1);
    } catch (e) { /* leave unread; a reload reconciles */ }
    finally { busyId = null; }
  }
  async function deletePlayer(m) {
    if (busyId) return;
    busyId = m.id;
    try {
      await api.messages.delete(m.id);
      messages = messages.filter((x) => x.id !== m.id);
      if (m.state === 'unread') decrementUnread(1);
    } catch (e) { /* keep row on failure */ }
    finally { busyId = null; }
  }
  async function readGuild(m) {
    if (busyId) return;
    busyId = m.id;
    try { await api.messages.read(m.id); guildMessages = markLocalRead(guildMessages, m.id); }
    catch (e) { /* noop */ } finally { busyId = null; }
  }
  async function deleteGuild(m) {
    if (busyId) return;
    busyId = m.id;
    try { await api.messages.delete(m.id); guildMessages = guildMessages.filter((x) => x.id !== m.id); }
    catch (e) { /* noop */ } finally { busyId = null; }
  }

  async function inviteFromInbox(m) {
    const requestId = m?.payload?.request_id;
    if (busyId || !guildInbox?.guild_id || requestId == null) return;
    busyId = m.id; inviteNote = null;
    try {
      const r = await api.guilds.joinRequestInvite(guildInbox.guild_id, requestId);
      const st = r?.status || (r?.success ? 'applied' : 'failed');
      if (st === 'applied' || st === 'replay') {
        inviteNote = { phase: 'invited', message: 'Invite sent. They can answer the hail now.' };
        guildMessages = markLocalRead(guildMessages, m.id);
      } else if (st === 'deferred') {
        inviteNote = { phase: 'deferred', message: r?.message || 'Invites are not yet enabled. Recorded, but nothing was sent.' };
      } else {
        inviteNote = { phase: 'failed', message: r?.message || r?.fail_reason || 'The invite was refused.' };
      }
    } catch (e) {
      inviteNote = { phase: 'failed', message: e?.message || 'The registry could not be reached.' };
    } finally { busyId = null; }
  }

  function reply(m) {
    replyTo = m.sender_char_name || '';
    tab = 'compose';
  }
  function openGuildTab() {
    tab = 'guild';
    if (guildStatus === 'idle') untrack(loadGuildInbox);
  }

  onMount(loadInbox);
</script>

<CarvedSlab elevation={2}>
  <EliHint text="Your messages and notifications. Reply, or start a new one." />
  <div class="tabs" role="tablist">
    <button class="tab" class:on={tab === 'inbox'} role="tab" aria-selected={tab === 'inbox'} onclick={() => (tab = 'inbox')}>Inbox</button>
    <button class="tab" class:on={tab === 'compose'} role="tab" aria-selected={tab === 'compose'} onclick={() => { replyTo = ''; tab = 'compose'; }}>Compose</button>
    {#if guildInbox}
      <button class="tab" class:on={tab === 'guild'} role="tab" aria-selected={tab === 'guild'} onclick={openGuildTab}>Guild inbox</button>
    {/if}
    <button class="refresh mono" type="button" onclick={() => (tab === 'guild' ? loadGuildInbox() : loadInbox())} aria-label="Refresh">refresh</button>
  </div>

  {#if tab === 'inbox'}
    <MessageList
      messages={messages}
      status={inboxStatus}
      scope="player"
      {busyId}
      onRead={readPlayer}
      onDelete={deletePlayer}
      onReply={reply}
    />
  {:else if tab === 'compose'}
    <MessageComposer recipientCharName={replyTo} locked={!!replyTo} onSent={() => { loadInbox(); tab = 'inbox'; }} />
  {:else if tab === 'guild' && guildInbox}
    {#if inviteNote}
      <p class="invite-note" data-phase={inviteNote.phase} role="status" aria-live="polite">{inviteNote.message}</p>
    {/if}
    <MessageList
      messages={guildMessages}
      status={guildStatus === 'idle' ? 'loading' : guildStatus}
      scope="guild"
      canManage={guildInbox.canManage === true}
      {busyId}
      onRead={readGuild}
      onDelete={deleteGuild}
      onInvite={guildInbox.canManage === true ? inviteFromInbox : undefined}
    />
  {/if}
</CarvedSlab>

<style>
  .tabs { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-4); flex-wrap: wrap; }
  .tab {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text-muted);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .tab:hover { border-color: var(--accent); }
  .tab.on { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .refresh {
    margin-left: auto; font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    color: var(--text-muted); background: none; border: 0; cursor: pointer; padding: var(--space-1);
  }
  .refresh:hover { color: var(--accent-text); }
  .invite-note { font-size: var(--text-sm); margin: 0 0 var(--space-3); }
  .invite-note[data-phase='invited'] { color: var(--ls-green); }
  .invite-note[data-phase='deferred'] { color: var(--accent-text); }
  .invite-note[data-phase='failed'] { color: var(--ls-red); }
</style>
