<script>
  // Leader-only: who in the guild may READ the shared guild inbox and who may
  // MANAGE it (accept/decline/delete). LIVE (admin.db). Parent mounts this only
  // when the viewer is the Leader; the server re-enforces the Leader gate.
  import { api } from '$lib/api.js';

  let { guildId, config = null } = $props();

  const ROLES = [
    { v: 1, label: 'Members and up' },
    { v: 50, label: 'Officers and up' },
    { v: 100, label: 'Leader only' },
  ];

  // svelte-ignore state_referenced_locally
  let viewMin = $state(Number(config?.view_min_role) || 50);
  // svelte-ignore state_referenced_locally
  let manageMin = $state(Number(config?.manage_min_role) || 100);

  let busy = $state(false);
  // phase: 'idle' | 'saved' | 'failed'
  let phase = $state('idle');
  let message = $state('');

  async function save() {
    if (busy) return;
    busy = true; phase = 'idle'; message = '';
    try {
      const r = await api.guilds.inboxConfigSet(guildId, {
        view_min_role: Number(viewMin),
        manage_min_role: Number(manageMin),
      });
      if (r && r.ok !== false) { phase = 'saved'; message = 'Guild inbox access updated.'; }
      else { phase = 'failed'; message = (r && r.error) || 'The change did not save.'; }
    } catch (e) {
      phase = 'failed'; message = e?.message || 'The registry could not be reached.';
    } finally { busy = false; }
  }
</script>

<section class="cfg">
  <p class="kicker mono">Guild inbox access</p>
  <div class="fields">
    <label class="fld"><span>Who can read</span>
      <select bind:value={viewMin}>
        {#each ROLES as r}<option value={r.v}>{r.label}</option>{/each}
      </select>
    </label>
    <label class="fld"><span>Who can act</span>
      <select bind:value={manageMin}>
        {#each ROLES as r}<option value={r.v}>{r.label}</option>{/each}
      </select>
    </label>
    <button class="btn primary" type="button" onclick={save} disabled={busy}>{busy ? 'Saving' : 'Save'}</button>
  </div>
  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</section>

<style>
  .cfg { display: flex; flex-direction: column; gap: var(--space-2); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .fields { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--space-3); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; text-transform: uppercase; }
  .fld select {
    font-family: var(--font-mono); font-size: var(--text-sm); color: var(--text);
    background: var(--bg-elevated); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); cursor: pointer; text-transform: none;
  }
  .fld select:hover { border-color: var(--accent); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-sm); cursor: pointer;
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { font-size: var(--text-sm); margin: 0; }
  .status[data-phase='saved'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
