<script>
  // Leader/Officer control for the OWN guild's recruiting beacon. LIVE (admin.db).
  // Parent mounts this only when my_role_can_edit. Toggles the amber beacon on/off
  // and edits the structured filters the Signal Board searches over.
  import { api } from '$lib/api.js';
  import RecruitingBeacon from './RecruitingBeacon.svelte';

  let { guildId, recruiting = null } = $props();

  const MSG_MAX = 280;
  const TAG_MAX = 40;
  // Seed once from the prop; parent gates mount on loaded guild context.
  // svelte-ignore state_referenced_locally
  let open = $state(recruiting?.open === true || recruiting?.open === 1);
  // svelte-ignore state_referenced_locally
  let playstyle = $state(recruiting?.playstyle || '');
  // svelte-ignore state_referenced_locally
  let timezone = $state(recruiting?.timezone || '');
  // svelte-ignore state_referenced_locally
  let language = $state(recruiting?.language || '');
  // svelte-ignore state_referenced_locally
  let npFriendly = $state(recruiting?.new_player_friendly === true || recruiting?.new_player_friendly === 1);
  // svelte-ignore state_referenced_locally
  let discordUrl = $state(recruiting?.discord_url || '');
  // svelte-ignore state_referenced_locally
  let message = $state(recruiting?.message || '');

  let editing = $state(false);
  let busy = $state(false);
  // phase: 'idle' | 'saved' | 'failed'
  let phase = $state('idle');
  let statusMsg = $state('');

  let tooLong = $derived(message.length > MSG_MAX);

  async function save(nextOpen) {
    if (busy || tooLong) return;
    busy = true; phase = 'idle'; statusMsg = '';
    try {
      const r = await api.guilds.recruitingSet({
        guild_id: guildId,
        open: nextOpen ? 1 : 0,
        playstyle: playstyle.slice(0, TAG_MAX),
        timezone: timezone.slice(0, TAG_MAX),
        language: language.slice(0, TAG_MAX),
        new_player_friendly: npFriendly ? 1 : 0,
        discord_url: discordUrl.slice(0, 200),
        message: message.slice(0, MSG_MAX),
      });
      if (r && r.ok !== false) {
        open = nextOpen; phase = 'saved';
        statusMsg = nextOpen ? 'Beacon lit. Your sietch shows on the Signal Board.' : 'Beacon dark. Your sietch is hidden from recruiting filters.';
        editing = false;
      } else {
        phase = 'failed'; statusMsg = (r && r.error) || 'The change did not save.';
      }
    } catch (e) {
      phase = 'failed'; statusMsg = e?.message || 'The registry could not be reached.';
    } finally {
      busy = false;
    }
  }
</script>

<section class="qr">
  <div class="head">
    <div class="state">
      <p class="kicker mono">Recruiting beacon</p>
      {#if open}<RecruitingBeacon />{:else}<span class="dark mono">beacon dark</span>{/if}
    </div>
    <div class="acts">
      {#if open}
        <button class="btn" type="button" onclick={() => save(false)} disabled={busy}>Turn off</button>
      {:else}
        <button class="btn primary" type="button" onclick={() => save(true)} disabled={busy}>Turn on</button>
      {/if}
      <button class="btn ghost" type="button" onclick={() => { editing = !editing; }} disabled={busy}>
        {editing ? 'Close' : 'Edit details'}
      </button>
    </div>
  </div>

  {#if editing}
    <div class="fields">
      <label class="fld"><span>Playstyle</span>
        <input type="text" bind:value={playstyle} maxlength={TAG_MAX} placeholder="PvP, PvE, casual, hardcore" />
      </label>
      <label class="fld"><span>Timezone</span>
        <input type="text" bind:value={timezone} maxlength={TAG_MAX} placeholder="NA-East, EU, OCE" />
      </label>
      <label class="fld"><span>Language</span>
        <input type="text" bind:value={language} maxlength={TAG_MAX} placeholder="English" />
      </label>
      <label class="fld"><span>Discord invite URL</span>
        <input type="url" bind:value={discordUrl} maxlength="200" placeholder="https://discord.gg/..." />
      </label>
      <label class="chk"><input type="checkbox" bind:checked={npFriendly} /> New-player friendly</label>
      <label class="fld full"><span>Recruiting blurb</span>
        <textarea bind:value={message} rows="3" maxlength={MSG_MAX + 20} placeholder="Who you want, when you ride, what you stand for."></textarea>
      </label>
      <div class="meta mono">
        <span class:over={tooLong}>{message.length} / {MSG_MAX}</span>
        <button class="btn primary" type="button" onclick={() => save(open)} disabled={busy || tooLong}>
          {busy ? 'Saving' : 'Save details'}
        </button>
      </div>
    </div>
  {/if}

  {#if phase !== 'idle' && statusMsg}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{statusMsg}</p>
  {/if}
</section>

<style>
  .qr { display: flex; flex-direction: column; gap: var(--space-3); }
  .head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; }
  .state { display: flex; align-items: center; gap: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .dark { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .1em; text-transform: uppercase; }
  .acts { display: flex; gap: var(--space-2); }
  .fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 14rem), 1fr)); gap: var(--space-3); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; }
  .fld.full { grid-column: 1 / -1; }
  .fld input, textarea {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  textarea { resize: vertical; min-height: 4rem; line-height: 1.45; }
  .fld input:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chk { display: inline-flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm); color: var(--text-muted); }
  .chk input { accent-color: var(--accent); }
  .meta { grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); font-size: var(--text-xs); color: var(--text-muted); }
  .meta .over { color: var(--ls-red); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), filter var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn.ghost { color: var(--text-muted); }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
  .status { font-size: var(--text-sm); margin: 0; }
  .status[data-phase='saved'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
