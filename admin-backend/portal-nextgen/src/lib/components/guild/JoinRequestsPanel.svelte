<script>
  // Officer inbox of pending join requests for the viewer's own guild. The list is
  // LIVE (admin.db, officer-gated server-side). The Invite action fires the DARK
  // send_invite handoff (POST .../join-requests/{id}/invite); while
  // GUILD_WRITES_DARK it returns status:'deferred', surfaced as amber "not yet
  // enabled" (never a green success). On a live 'applied' the row shows Invited.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import EliHint from './EliHint.svelte';
  import { api } from '$lib/api.js';

  // Parent mounts this only when the viewer is an Officer/Leader of guildId.
  let { guildId } = $props();

  let requests = $state([]);
  let status = $state('loading'); // 'loading' | 'ready' | 'error'
  let busyId = $state(null);
  // Per-request outcome: { [request_id]: { phase, message } }
  // phase: 'invited' | 'deferred' | 'failed'
  let outcomes = $state({});

  async function load() {
    status = 'loading';
    try {
      const r = await api.guilds.joinRequestList(guildId);
      requests = Array.isArray(r?.requests) ? r.requests : [];
      status = 'ready';
    } catch (e) {
      requests = []; status = 'error';
    }
  }

  async function invite(req) {
    if (busyId != null) return;
    busyId = req.request_id;
    try {
      const r = await api.guilds.joinRequestInvite(guildId, req.request_id);
      const st = r?.status || (r?.success ? 'applied' : 'failed');
      if (st === 'applied' || st === 'replay') {
        outcomes = { ...outcomes, [req.request_id]: { phase: 'invited', message: 'Invite sent. They can answer the hail now.' } };
      } else if (st === 'deferred') {
        outcomes = { ...outcomes, [req.request_id]: { phase: 'deferred', message: r?.message || 'Invites are not yet enabled. Recorded, but nothing was sent.' } };
      } else {
        outcomes = { ...outcomes, [req.request_id]: { phase: 'failed', message: r?.message || r?.fail_reason || 'The invite was refused.' } };
      }
    } catch (e) {
      outcomes = { ...outcomes, [req.request_id]: { phase: 'failed', message: e?.message || 'The registry could not be reached.' } };
    } finally {
      busyId = null;
    }
  }

  onMount(load);
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Join requests</p>
    {#if status === 'ready' && requests.length > 0}<span class="count mono">{requests.length} waiting</span>{/if}
  </div>
  <EliHint text="Players who asked to join. Officers invite or dismiss them." />

  {#if status === 'loading'}
    <p class="skeleton">loading</p>
  {:else if status === 'error'}
    <p class="sealed">The requests could not be reached. Try again shortly.</p>
  {:else if requests.length === 0}
    <p class="sealed">No one is waiting at the gate.</p>
  {:else}
    <ul class="reqs">
      {#each requests as r (r.request_id)}
        {@const oc = outcomes[r.request_id]}
        <li class="req">
          <div class="line">
            <span class="who">{r.requester_char_name || 'Unknown survivor'}</span>
            {#if r.requester_discord_id}<span class="disc mono">discord: {r.requester_discord_id}</span>{/if}
            <span class="when mono">{r.created_at || ''}</span>
          </div>
          {#if r.note}<p class="note">{r.note}</p>{/if}
          <div class="acts">
            {#if oc?.phase === 'invited'}
              <span class="done mono" data-phase="invited">Invited</span>
            {:else}
              <button class="act live" type="button" onclick={() => invite(r)} disabled={busyId != null} title="Invite this player to the guild">
                {busyId === r.request_id ? 'Sending' : 'Invite'}
              </button>
            {/if}
            {#if oc && oc.phase !== 'invited'}
              <span class="done mono" data-phase={oc.phase} role="status">{oc.message}</span>
            {/if}
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .count { font-size: var(--text-xs); color: var(--text-muted); }
  .reqs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); }
  .req {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .line { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--space-2) var(--space-3); }
  .who { font-size: var(--text-sm); color: var(--text); }
  .disc { font-size: var(--text-xs); color: var(--text-muted); }
  .when { font-size: var(--text-xs); color: var(--text-muted); margin-left: auto; white-space: nowrap; }
  .note { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
  .acts { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-1); }
  .act {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--accent); color: var(--bg-deep);
    border: 1px solid var(--accent); border-radius: var(--radius-sm);
    cursor: pointer; box-shadow: 0 0 10px var(--accent-glow);
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .act:hover:not(:disabled) { filter: brightness(1.12); }
  .act:disabled { opacity: .45; cursor: not-allowed; box-shadow: none; }
  .done { font-size: var(--text-xs); letter-spacing: .06em; line-height: 1.4; }
  .done[data-phase='invited'] { color: var(--ls-green); text-transform: uppercase; letter-spacing: .1em; }
  .done[data-phase='deferred'] { color: var(--accent-text); }
  .done[data-phase='failed'] { color: var(--ls-red); }
  .sealed { color: var(--text-muted); font-size: var(--text-sm); margin: 0; line-height: 1.45; }
</style>
