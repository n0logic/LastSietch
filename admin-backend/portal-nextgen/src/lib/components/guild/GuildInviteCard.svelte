<script>
  // One pending guild invite, with the Accept / Decline self-actions wired to the
  // guild-op write path. The invite is identified by invite_id ONLY — the account
  // is resolved server-side from the session and is never sent from here. A
  // status:'deferred' response means the write is not yet enabled and is surfaced
  // honestly, never as success. Guild name + who hailed you are the headline.
  import { api, uuidv4 } from '$lib/api.js';

  let { invite, onChanged } = $props();

  let name = $derived(invite?.guild_name || 'Unnamed guild');
  let sender = $derived(invite?.sender_character_name || 'a guild officer');
  // invite_sent_timespan is on the game's universe-time basis and is meaningless
  // as printed; the relay adds sent_ago_s (plain seconds). No figure = no chip,
  // never the raw number.
  let when = $derived(agoLabel(invite?.sent_ago_s));
  function agoLabel(s) {
    if (typeof s !== 'number' || !Number.isFinite(s) || s < 0) return '';
    if (s < 60) return 'hailed just now';
    const m = Math.floor(s / 60);
    if (m < 60) return `hailed ${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 48) return `hailed ${h}h ago`;
    return `hailed ${Math.floor(h / 24)}d ago`;
  }
  let blurb = $derived(invite?.guild_description || '');
  let inviteId = $derived(invite?.invite_id ?? null);

  let busy = $state(null);   // 'accept_invite' | 'reject_invite' | null
  // phase: 'idle' | 'applied' | 'deferred' | 'failed'
  let phase = $state('idle');
  let message = $state('');
  let done = $state(false);

  async function act(op, label) {
    if (busy || done || inviteId == null) return;
    busy = op;
    phase = 'idle'; message = '';
    try {
      const r = await api.guilds.op({ op, invite_id: inviteId, idempotency_key: uuidv4() });
      const status = r?.status || (r?.success ? 'applied' : 'failed');
      if (status === 'applied' || status === 'replay') {
        phase = 'applied'; message = r?.message || `${label} ${name}.`;
        done = true;
        onChanged?.(r);
      } else if (status === 'deferred') {
        phase = 'deferred';
        message = r?.message || 'This action is not yet enabled. Nothing has changed.';
      } else {
        phase = 'failed'; message = r?.message || r?.fail_reason || 'The hail was refused.';
        // Controller flip: the hail belongs to another of the player's characters
        // (the writer answers as the one played last). The server already dropped
        // the stale row from its cache; refresh so the list tells the truth.
        if (r?.fail_reason === 'invite_not_for_active_character') onChanged?.(r);
      }
    } catch (e) {
      phase = 'failed'; message = e?.message || 'The registry could not be reached.';
    } finally { busy = null; }
  }
</script>

<article class="invite">
  <div class="head">
    <h3 class="gname">{name}</h3>
    {#if when}<span class="when mono">{when}</span>{/if}
  </div>
  {#if blurb}<p class="blurb">{blurb}</p>{/if}
  <p class="hail mono">Hailed by {sender}</p>
  {#if !done}
    <div class="actions">
      <button class="act" type="button" disabled={busy !== null || inviteId == null}
              onclick={() => act('accept_invite', 'Joined')}>
        {busy === 'accept_invite' ? 'Working' : 'Accept'}
      </button>
      <button class="act ghost" type="button" disabled={busy !== null || inviteId == null}
              onclick={() => act('reject_invite', 'Declined')}>
        {busy === 'reject_invite' ? 'Working' : 'Decline'}
      </button>
    </div>
  {/if}
  {#if phase !== 'idle' && message}
    <p class="istatus" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</article>

<style>
  .invite {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); }
  .gname {
    font-size: var(--text-lg); letter-spacing: .04em; text-transform: uppercase;
    color: var(--text);
  }
  .when { font-size: var(--text-xs); color: var(--text-muted); white-space: nowrap; }
  .blurb { color: var(--text-muted); font-size: var(--text-sm); line-height: 1.4; margin: 0; }
  .hail { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; margin: 0; }
  .actions { display: flex; align-items: center; gap: var(--space-2); margin-top: var(--space-1); }
  .act {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out);
  }
  .act:hover:not(:disabled) { border-color: var(--accent); }
  .act:disabled { opacity: .45; cursor: not-allowed; }
  .act.ghost { color: var(--text-muted); }
  .istatus { font-size: var(--text-xs); margin: 0; line-height: 1.4; }
  .istatus[data-phase='applied'] { color: var(--ls-green); }
  .istatus[data-phase='deferred'] { color: var(--accent-text); }
  .istatus[data-phase='failed'] { color: var(--ls-red); }
</style>
