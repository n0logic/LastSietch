<script>
  // Who the portal acts as: the active game account and the active character.
  // Both selects are the topbar's, moved here unchanged -- the server validates
  // ownership and re-pins the session, and the store reloads the app so every
  // per-account and per-character surface re-resolves against the new pick. A
  // failed switch reverts the control to the selection still in force.
  import { auth, selectAccount, selectCharacter } from '$lib/auth.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import Notice from '$lib/components/Notice.svelte';

  const activeAccount = $derived(auth.accounts.find((a) => a.active) || null);
  const selectedChar = $derived(auth.characters.find((c) => c.selected) || auth.characters[0] || null);
  const multiAccount = $derived(auth.multiaccountEnabled && auth.accounts.length > 1);
  const multiChar = $derived(auth.characters.length > 1);

  let switching = $state(false);
  let failed = $state('');

  async function onAccountSwitch(e) {
    const next = Number(e.target.value);
    const cur = activeAccount?.account_id;
    if (!next || next === cur || switching) return;
    switching = true; failed = '';
    try {
      await selectAccount(next); // reloads on success
    } catch (err) {
      switching = false;
      failed = 'That account switch did not go through. Nothing changed.';
      if (cur != null) e.target.value = String(cur);
    }
  }

  async function onCharSwitch(e) {
    const next = Number(e.target.value);
    const cur = selectedChar?.controller_id;
    if (!next || next === cur || switching) return;
    switching = true; failed = '';
    try {
      await selectCharacter(next); // reloads on success
    } catch (err) {
      switching = false;
      failed = 'That character switch did not go through. Nothing changed.';
      if (cur != null) e.target.value = String(cur);
    }
  }
</script>

<CarvedSlab>
  <p class="kicker mono">Playing as | active bond</p>

  {#if !multiAccount && !multiChar}
    <SealedPanel
      status="empty" action="none"
      emptyText="One account, one character. There is nothing to switch between yet."
    />
  {:else}
    <div class="controls">
      {#if multiAccount}
        <label class="field">
          <span class="lbl mono">Account</span>
          <select
            aria-label="Active account"
            disabled={switching}
            value={activeAccount?.account_id ?? ''}
            onchange={onAccountSwitch}
          >
            {#each auth.accounts as a (a.account_id)}
              <option value={a.account_id}>{a.character_name}</option>
            {/each}
          </select>
        </label>
      {/if}
      {#if multiChar}
        <label class="field">
          <span class="lbl mono">Character</span>
          <select
            aria-label="Active character"
            disabled={switching}
            value={selectedChar?.controller_id ?? ''}
            onchange={onCharSwitch}
          >
            {#each auth.characters as c (c.controller_id)}
              <option value={c.controller_id}>
                {c.char_name}{c.lvl != null ? ` · Lv ${c.lvl}` : ''}{c.online ? ' ●' : ''}
              </option>
            {/each}
          </select>
        </label>
      {/if}
    </div>
    <p class="explain">Switching reloads the portal so every balance, base and write target follows the new selection.</p>
  {/if}

  {#if failed}<Notice tone="error" text={failed} />{/if}
</CarvedSlab>

<style>
  .controls { display: flex; flex-wrap: wrap; gap: var(--space-3); margin-top: var(--space-3); }
  .field { display: flex; flex-direction: column; gap: var(--space-1); min-width: 12rem; flex: 1 1 12rem; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .field select {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
  }
  .field select:disabled { opacity: .5; }
  .explain { margin: var(--space-3) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
</style>
