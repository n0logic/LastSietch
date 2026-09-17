<script>
  // Per-member management (promote / demote / remove) for an Officer or Leader.
  // The client-side hierarchy gate mirrors the wrapper's rules (2.2): actor must
  // outrank the target, never act on self, and only a Leader may transfer (promote
  // to Leader). The SERVER re-enforces every rule regardless. These ops ship DARK
  // behind GUILD_WRITES_DARK: a status:'deferred' response is surfaced honestly as
  // "not yet enabled", never as success and never as an error.
  import { api, uuidv4 } from '$lib/api.js';

  // member: { character_name, role_id, role_name, is_self, player_controller_id }
  let { member, guildId, viewerRole = 1, onChanged } = $props();

  const LEADER = 100, OFFICER = 50, MEMBER = 1;

  let targetRole = $derived(Number(member?.role_id) || MEMBER);
  let isSelf = $derived(member?.is_self === true);
  let hasController = $derived(member?.player_controller_id != null);

  // Base gate: actor is officer+, not self, strictly outranks the target, and we
  // actually have a controller id to target (backend only sends it when editable).
  let canAct = $derived(
    viewerRole >= OFFICER && !isSelf && viewerRole > targetRole && hasController
  );

  // Which concrete ops are offered.
  let canPromoteOfficer = $derived(canAct && targetRole === MEMBER); // Member -> Officer
  let canPromoteLeader = $derived(canAct && targetRole === OFFICER && viewerRole === LEADER); // transfer
  let canDemote = $derived(canAct && targetRole === OFFICER); // Officer -> Member
  let canRemove = $derived(canAct); // any outranked member

  let open = $state(false);
  let pending = $state(null); // { op, new_role, label, danger }
  let busy = $state(false);
  // phase: 'idle' | 'applied' | 'deferred' | 'failed'
  let phase = $state('idle');
  let message = $state('');

  function ask(op, new_role, label, danger = false) {
    pending = { op, new_role, label, danger };
    phase = 'idle'; message = '';
  }

  async function confirm() {
    if (busy || !pending) return;
    busy = true;
    const body = {
      op: pending.op,
      guild_id: guildId,
      target_player_controller_id: member.player_controller_id,
      idempotency_key: uuidv4(),
    };
    if (pending.new_role != null) body.new_role = pending.new_role;
    try {
      const r = await api.guilds.op(body);
      const status = r?.status || (r?.success ? 'applied' : 'failed');
      if (status === 'applied' || status === 'replay') {
        phase = 'applied'; message = r?.message || `${pending.label} applied.`;
        pending = null; open = false;
        onChanged?.(r);
      } else if (status === 'deferred') {
        phase = 'deferred';
        message = r?.message || 'This action is not yet enabled. It has been recorded but will not take effect until an admin opens it.';
        pending = null;
      } else {
        phase = 'failed'; message = r?.message || r?.fail_reason || 'The action was refused.';
        pending = null;
      }
    } catch (e) {
      phase = 'failed'; message = e?.message || 'The registry could not be reached.';
      pending = null;
    } finally { busy = false; }
  }
</script>

{#if canAct}
  <div class="menu">
    {#if !open}
      <button class="mtrig" type="button" onclick={() => { open = true; phase = 'idle'; message = ''; }} aria-label="Manage {member.character_name}">Manage</button>
    {:else if pending}
      <div class="confirm">
        <span class="ask">{pending.label} {member.character_name}?</span>
        <button class="mbtn primary" type="button" onclick={confirm} disabled={busy}>{busy ? 'Working' : 'Confirm'}</button>
        <button class="mbtn ghost" type="button" onclick={() => { pending = null; }} disabled={busy}>Cancel</button>
      </div>
    {:else}
      <div class="opts">
        {#if canPromoteOfficer}<button class="mbtn" type="button" onclick={() => ask('promote', 50, 'Promote to Officer')}>Promote</button>{/if}
        {#if canPromoteLeader}<button class="mbtn" type="button" onclick={() => ask('promote', 100, 'Transfer leadership to', true)}>Make Leader</button>{/if}
        {#if canDemote}<button class="mbtn" type="button" onclick={() => ask('demote', 1, 'Demote to Member')}>Demote</button>{/if}
        {#if canRemove}<button class="mbtn danger" type="button" onclick={() => ask('remove', null, 'Remove', true)}>Remove</button>{/if}
        <button class="mbtn ghost" type="button" onclick={() => { open = false; }}>Close</button>
      </div>
    {/if}
    {#if phase !== 'idle' && message}
      <p class="mstatus" data-phase={phase} role="status" aria-live="polite">{message}</p>
    {/if}
  </div>
{/if}

<style>
  .menu { display: flex; flex-direction: column; gap: var(--space-2); align-items: flex-start; }
  .mtrig {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: 2px var(--space-2);
    background: var(--bg-elevated); color: var(--text-muted);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer;
  }
  .mtrig:hover { border-color: var(--accent); color: var(--text); }
  .opts, .confirm { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
  .ask { font-size: var(--text-xs); color: var(--text); }
  .mbtn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .08em; text-transform: uppercase;
    padding: 2px var(--space-2);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), filter var(--motion-fast) var(--ease-out);
  }
  .mbtn:hover:not(:disabled) { border-color: var(--accent); }
  .mbtn:disabled { opacity: .45; cursor: not-allowed; }
  .mbtn.ghost { color: var(--text-muted); }
  .mbtn.danger { color: var(--ls-red); border-color: color-mix(in srgb, var(--ls-red) 45%, var(--edge)); }
  .mbtn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .mstatus { font-size: var(--text-xs); margin: 0; line-height: 1.4; }
  .mstatus[data-phase='applied'] { color: var(--ls-green); }
  .mstatus[data-phase='deferred'] { color: var(--accent-text); }
  .mstatus[data-phase='failed'] { color: var(--ls-red); }
</style>
