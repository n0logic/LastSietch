<script>
  // Characters: the linked game accounts and who the portal acts as, in one
  // card (this was two, "Linked accounts" and "Playing as", listing the same
  // accounts twice). The active account is marked in the list and the others
  // carry a Play as switch; the active account's character select sits in its
  // row. Both switches are the topbar's, unchanged: the server validates
  // ownership and re-pins the session, and the store reloads the app so every
  // per-account and per-character surface re-resolves against the new pick.
  // "Manage" opens the shipped alt-aware unlink dialog rather than growing a
  // second one. Adding an account is the one AddGameAccount control.
  import { auth, selectAccount, selectCharacter } from '$lib/auth.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import AccountManageDialog from '$lib/components/AccountManageDialog.svelte';
  import AddGameAccount from './AddGameAccount.svelte';
  import RelinkAccount from './RelinkAccount.svelte';
  import { shell } from '$lib/shellNext.svelte.js';

  let manageOpen = $state(false);

  const activeAccount = $derived(auth.accounts.find((a) => a.active) || null);
  const selectedChar = $derived(auth.characters.find((c) => c.selected) || auth.characters[0] || null);
  const multiAccount = $derived(auth.multiaccountEnabled && auth.accounts.length > 1);
  const multiChar = $derived(auth.characters.length > 1);

  let switching = $state(false);
  let failed = $state('');

  async function onAccountSwitch(next) {
    if (!next || next === activeAccount?.account_id || switching) return;
    switching = true; failed = '';
    try {
      await selectAccount(next); // reloads on success
    } catch (err) {
      switching = false;
      failed = 'That account switch did not go through. Nothing changed.';
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
  <p class="kicker mono">Characters | water bonds</p>

  {#if auth.accounts.length === 0}
    <SealedPanel
      status="empty" action="none"
      emptyText="No game account is linked to this profile yet. Add one below to see your characters here."
    />
  {:else}
    <ul class="list">
      {#each auth.accounts as a (a.account_id)}
        <li class="row" class:active={a.active}>
          <div class="who">
            <span class="name">{a.character_name}</span>
            {#if a.active}<span class="badge mono">Playing as</span>{/if}
          </div>
          {#if a.active && multiChar}
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
          {:else if !a.active && multiAccount}
            <button
              class="btn"
              type="button"
              disabled={switching}
              aria-label="Play as {a.character_name}"
              onclick={() => onAccountSwitch(a.account_id)}
            >Play as</button>
          {/if}
        </li>
      {/each}
    </ul>
    {#if multiAccount || multiChar}
      <p class="explain">Switching reloads the portal so every balance, base and write target follows the new selection.</p>
    {/if}
    <div class="actions">
      <button class="btn" type="button" onclick={() => (manageOpen = true)}>Manage</button>
    </div>
  {/if}

  {#if failed}<Notice tone="error" text={failed} />{/if}

  <AddGameAccount />
  <!-- G1-a self-relink: new with the P3 shell, so it draws only behind the switch
       (the layout reads it). The shipped shell keeps relink on Classic. -->
  {#if shell.status === 'ready' && shell.next && auth.accounts.length > 0}<RelinkAccount />{/if}
</CarvedSlab>

{#if manageOpen}
  <AccountManageDialog onClose={() => (manageOpen = false)} />
{/if}

<style>
  .list { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .row {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-3);
    padding: var(--space-3); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); background: var(--metal-1);
  }
  .row.active { border-color: color-mix(in srgb, var(--ls-ibad) 45%, var(--edge)); }
  .who { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
  .name { font-size: var(--text-sm); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge {
    font-size: 10px; letter-spacing: .1em; text-transform: uppercase; white-space: nowrap;
    color: var(--ls-ibad); border: 1px solid color-mix(in srgb, var(--ls-ibad) 45%, transparent);
    border-radius: 999px; padding: 1px var(--space-2);
  }

  .field { display: flex; flex-direction: column; gap: var(--space-1); min-width: 12rem; flex: 0 1 16rem; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .field select {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-2);
  }
  .field select:disabled { opacity: .5; }
  .explain { margin: var(--space-3) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }

  .actions { display: flex; gap: var(--space-2); margin-top: var(--space-3); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-sm); cursor: pointer;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
  }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .5; }
</style>
