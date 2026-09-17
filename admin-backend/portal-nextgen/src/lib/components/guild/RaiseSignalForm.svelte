<script>
  // Solo LFG self-post. The player raises their own signal on the Seeker Wall
  // (LIVE, admin.db, one row per player). account_id is server-derived; this only
  // sends the post's fields. Shows the current signal with a Lower option.
  import { api } from '$lib/api.js';

  // mine: the viewer's own seeker row | null; onChanged: () => reload the wall.
  let { mine = null, onChanged } = $props();

  const NOTE_MAX = 280;
  const TAG_MAX = 40;
  const TTL_CHOICES = [
    { h: 6, label: '6 hours' },
    { h: 24, label: '1 day' },
    { h: 72, label: '3 days' },
    { h: 168, label: '1 week' },
  ];

  let hasSignal = $derived(mine != null);
  // svelte-ignore state_referenced_locally
  let playstyle = $state(mine?.playstyle || '');
  // svelte-ignore state_referenced_locally
  let timezone = $state(mine?.timezone || '');
  // svelte-ignore state_referenced_locally
  let role = $state(mine?.role || '');
  // svelte-ignore state_referenced_locally
  let note = $state(mine?.note || '');
  let ttlHours = $state(72);

  let busy = $state(false);
  // phase: 'idle' | 'saved' | 'lowered' | 'failed'
  let phase = $state('idle');
  let message = $state('');

  let tooLong = $derived(note.length > NOTE_MAX);

  async function raise() {
    if (busy || tooLong) return;
    busy = true; phase = 'idle'; message = '';
    try {
      const r = await api.guilds.lfgUpsert({
        playstyle: playstyle.slice(0, TAG_MAX),
        timezone: timezone.slice(0, TAG_MAX),
        role: role.slice(0, TAG_MAX),
        note: note.slice(0, NOTE_MAX),
        ttl_hours: ttlHours,
      });
      if (r && r.ok !== false) {
        phase = 'saved'; message = 'Signal raised. Other survivors can find you now.';
        onChanged?.(r);
      } else {
        phase = 'failed'; message = (r && r.error) || 'Your signal did not raise.';
      }
    } catch (e) {
      phase = 'failed'; message = e?.message || 'The registry could not be reached.';
    } finally { busy = false; }
  }

  async function lower() {
    if (busy) return;
    busy = true; phase = 'idle'; message = '';
    try {
      await api.guilds.lfgDelete();
      phase = 'lowered'; message = 'Signal lowered.';
      playstyle = ''; timezone = ''; role = ''; note = '';
      onChanged?.();
    } catch (e) {
      phase = 'failed'; message = e?.message || 'Could not lower your signal.';
    } finally { busy = false; }
  }
</script>

<section class="raise">
  <p class="kicker mono">{hasSignal ? 'Your signal' : 'Raise your signal'}</p>
  <div class="fields">
    <label class="fld"><span>Role / playstyle tag</span>
      <input type="text" bind:value={role} maxlength={TAG_MAX} placeholder="Solo, sardaukar-hunter, base-builder" />
    </label>
    <label class="fld"><span>Playstyle</span>
      <input type="text" bind:value={playstyle} maxlength={TAG_MAX} placeholder="PvP, PvE, casual" />
    </label>
    <label class="fld"><span>Timezone</span>
      <input type="text" bind:value={timezone} maxlength={TAG_MAX} placeholder="NA-East, EU, OCE" />
    </label>
    <label class="fld"><span>Signal expires after</span>
      <select bind:value={ttlHours}>
        {#each TTL_CHOICES as c}<option value={c.h}>{c.label}</option>{/each}
      </select>
    </label>
    <label class="fld full"><span>Note</span>
      <textarea bind:value={note} rows="2" maxlength={NOTE_MAX + 20} placeholder="What you are looking for and when you ride."></textarea>
    </label>
  </div>
  <div class="foot mono">
    <span class:over={tooLong}>{note.length} / {NOTE_MAX}</span>
    <div class="acts">
      {#if hasSignal}
        <button class="btn ghost" type="button" onclick={lower} disabled={busy}>Lower signal</button>
      {/if}
      <button class="btn primary" type="button" onclick={raise} disabled={busy || tooLong}>
        {busy ? 'Working' : hasSignal ? 'Update signal' : 'Raise signal'}
      </button>
    </div>
  </div>
  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</section>

<style>
  .raise { display: flex; flex-direction: column; gap: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 12rem), 1fr)); gap: var(--space-3); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; text-transform: uppercase; }
  .fld.full { grid-column: 1 / -1; }
  .fld input, .fld select, textarea {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
    text-transform: none; letter-spacing: normal;
  }
  textarea { resize: vertical; min-height: 3rem; line-height: 1.45; }
  .fld input:focus-visible, .fld select:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .foot { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); font-size: var(--text-xs); color: var(--text-muted); }
  .foot .over { color: var(--ls-red); }
  .acts { display: flex; gap: var(--space-2); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-1) var(--space-4);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: filter var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn.ghost { color: var(--text-muted); }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { font-size: var(--text-sm); margin: 0; }
  .status[data-phase='saved'], .status[data-phase='lowered'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
