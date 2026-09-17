<script>
  // A list of mailbox messages (player inbox OR a guild inbox). Notifications carry
  // a payload.kind sub-type that drives the label + icon and, for join-requests, an
  // Invite affordance. Unread rows are marked with a muted amber edge (chrome, not
  // Ibad). Read/Delete are honest server writes; Reply opens a DM to the sender.
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  let {
    messages = [],
    status = 'loading',       // 'loading' | 'ready' | 'error'
    scope = 'player',         // 'player' | 'guild'
    canManage = true,         // guild inbox: caller role >= manage_min_role
    busyId = null,
    onRead,
    onDelete,
    onReply,
    onInvite,                 // (message) => void; join-request rows only
  } = $props();

  let list = $derived(Array.isArray(messages) ? messages : []);

  // payload.kind -> { icon, label } for the notification sub-types.
  const KINDS = {
    join_request: { icon: '⚑', label: 'Join request' },
    guild_invite: { icon: '✉', label: 'Guild invite' },
    promoted: { icon: '▲', label: 'Promoted' },
    demoted: { icon: '▼', label: 'Demoted' },
    removed: { icon: '✕', label: 'Removed' },
    gift_received: { icon: '◈', label: 'Gift' },
  };

  function payloadKind(m) {
    const k = m?.payload?.kind;
    return k && KINDS[k] ? KINDS[k] : null;
  }
  function isJoinRequest(m) { return m?.kind === 'notification' && m?.payload?.kind === 'join_request'; }
  function isDM(m) { return m?.kind === 'user' && m?.sender_kind === 'player'; }
</script>

{#if status === 'loading'}
  <p class="skeleton">loading</p>
{:else if status === 'error'}
  <SealedPanel status="error" errorText="The mailbox could not be reached. Try again shortly." />
{:else if list.length === 0}
  <SealedPanel
    status="empty" action="none" art="no-mail"
    emptyText={scope === 'guild' ? 'No guild messages waiting.' : 'Your mailbox is empty.'}
  />
{:else}
  <ul class="msgs">
    {#each list as m (m.id)}
      {@const pk = payloadKind(m)}
      <li class="msg" class:unread={m.state === 'unread'} data-kind={m.kind}>
        <div class="line">
          {#if pk}<span class="badge mono">{pk.icon} {pk.label}</span>{/if}
          <span class="from">{m.sender_char_name || (m.sender_kind === 'system' ? 'The Registry' : m.sender_kind === 'guild' ? 'Your sietch' : 'Unknown')}</span>
          <span class="when mono">{m.created_at || ''}</span>
        </div>
        {#if m.subject}<p class="subj">{m.subject}</p>{/if}
        {#if m.body}<p class="body">{m.body}</p>{/if}
        <div class="acts">
          {#if isJoinRequest(m) && scope === 'guild' && canManage && onInvite}
            <button class="act primary" type="button" onclick={() => onInvite?.(m)} disabled={busyId === m.id} title="Invite this player">Invite</button>
          {/if}
          {#if isDM(m) && scope === 'player'}
            <button class="act" type="button" onclick={() => onReply?.(m)}>Reply</button>
          {/if}
          {#if m.state === 'unread' && (scope === 'player' || canManage)}
            <button class="act" type="button" onclick={() => onRead?.(m)} disabled={busyId === m.id}>Mark read</button>
          {/if}
          {#if scope === 'player' || canManage}
            <button class="act ghost" type="button" onclick={() => onDelete?.(m)} disabled={busyId === m.id}>Delete</button>
          {/if}
        </div>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .msgs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); }
  .msg {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .msg.unread { border-left: 3px solid var(--accent); }
  .line { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--space-2) var(--space-3); }
  .badge {
    font-size: var(--text-xs); letter-spacing: .08em; text-transform: uppercase;
    color: var(--accent-text); padding: 0 var(--space-2);
    border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--edge)); border-radius: var(--radius-sm);
  }
  .from { font-size: var(--text-sm); color: var(--text); }
  .msg.unread .from { font-weight: 600; }
  .when { font-size: var(--text-xs); color: var(--text-muted); margin-left: auto; white-space: nowrap; }
  .subj { margin: 0; font-size: var(--text-sm); color: var(--text); font-weight: 600; }
  .body { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
  .acts { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-1); }
  .act {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), filter var(--motion-fast) var(--ease-out);
  }
  .act:hover:not(:disabled) { border-color: var(--accent); }
  .act:disabled { opacity: .45; cursor: not-allowed; }
  .act.ghost { color: var(--text-muted); }
  .act.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .act.primary:hover:not(:disabled) { filter: brightness(1.12); }
</style>
