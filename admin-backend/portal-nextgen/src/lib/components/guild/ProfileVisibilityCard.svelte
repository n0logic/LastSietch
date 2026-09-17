<script>
  // The caller's own directory visibility. A single checkbox controls whether they
  // appear in the public player directory (and can therefore receive unsolicited
  // DMs); toggling AUTO-SAVES optimistically and reverts on failure. An optional
  // one-line blurb (tagline) saves on blur. All admin.db metadata resolved
  // server-side from the session; no id is ever sent. Neutral/amber chrome.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import { api } from '$lib/api.js';

  const BLURB_MAX = 140;

  let listed = $state(true);
  let blurb = $state('');
  let status = $state('loading'); // 'loading' | 'ready' | 'error'
  let savingListed = $state(false);
  let lastSavedBlurb = '';
  let note = $state(null); // { phase: 'saved' | 'failed', message }

  async function load() {
    status = 'loading';
    try {
      const r = await api.players.profileGet();
      listed = r?.listed !== false; // default listed
      blurb = typeof r?.blurb === 'string' ? r.blurb : '';
      lastSavedBlurb = blurb;
      status = 'ready';
    } catch (e) {
      status = 'error';
    }
  }

  async function toggleListed() {
    if (savingListed) return;
    const next = !listed;
    listed = next; // optimistic
    savingListed = true; note = null;
    try {
      const r = await api.players.profileSet({ listed: next ? '1' : '0', blurb });
      listed = r?.listed !== false;
      note = next
        ? { phase: 'saved', message: 'Listed. Other players can find and message you.' }
        : { phase: 'saved', message: 'Hidden. You no longer appear in the directory.' };
    } catch (e) {
      listed = !next; // revert
      note = { phase: 'failed', message: e?.message || 'Could not save. Try again.' };
    } finally {
      savingListed = false;
    }
  }

  async function saveBlurb() {
    const v = blurb.trim().slice(0, BLURB_MAX);
    blurb = v;
    if (v === lastSavedBlurb) return;
    note = null;
    try {
      const r = await api.players.profileSet({ listed: listed ? '1' : '0', blurb: v });
      lastSavedBlurb = typeof r?.blurb === 'string' ? r.blurb : v;
      note = { phase: 'saved', message: 'Tagline saved.' };
    } catch (e) {
      note = { phase: 'failed', message: e?.message || 'Could not save your tagline.' };
    }
  }

  onMount(load);
</script>

<CarvedSlab elevation={2}>
  <p class="kicker mono">Your visibility | player directory</p>

  {#if status === 'loading'}
    <p class="skeleton">loading</p>
  {:else if status === 'error'}
    <p class="sealed">Your visibility could not be loaded. Try again shortly.</p>
  {:else}
    <p class="explain">
      {#if listed}
        You're listed in the public player directory, so other players can find you
        and send you messages. Uncheck to hide and stop unsolicited messages.
      {:else}
        You're hidden from the public player directory: other players cannot find
        you there or message you unsolicited. Check the box to be listed.
      {/if}
    </p>

    <label class="toggle">
      <input type="checkbox" checked={listed} onchange={toggleListed} disabled={savingListed} />
      <span>List me in the player directory</span>
    </label>

    <label class="fld"><span>Tagline</span>
      <input
        type="text"
        bind:value={blurb}
        onblur={saveBlurb}
        maxlength={BLURB_MAX}
        placeholder="One short line others see next to your name (optional)"
      />
    </label>

    {#if note}
      <p class="note" data-phase={note.phase} role="status" aria-live="polite">{note.message}</p>
    {/if}
  {/if}
</CarvedSlab>

<style>
  .kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .explain { margin: 0 0 var(--space-4); color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; max-width: 56ch; }
  .toggle {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-sm); color: var(--text); cursor: pointer; user-select: none;
    margin-bottom: var(--space-4);
  }
  .toggle input { accent-color: var(--accent); width: 1.05rem; height: 1.05rem; }
  .toggle input:disabled { cursor: progress; }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; text-transform: uppercase; }
  .fld input {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
    text-transform: none; letter-spacing: normal;
  }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .note { margin: var(--space-3) 0 0; font-size: var(--text-sm); }
  .note[data-phase='saved'] { color: var(--ls-green); }
  .note[data-phase='failed'] { color: var(--ls-red); }
  .sealed { color: var(--text-muted); font-size: var(--text-sm); margin: 0; line-height: 1.45; }
</style>
