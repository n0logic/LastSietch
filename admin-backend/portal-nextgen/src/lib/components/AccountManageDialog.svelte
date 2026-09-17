<script>
  // Linked-accounts manager: the alt-aware unlink surface for V2 (V1 Classic's
  // /portal/account page has an unlink button; V2's topbar switcher never grew
  // one). The dialog chrome, focus trap, inert background and Escape handling
  // now come from the shared Modal primitive, which also fixes this dialog's
  // own trap bug: the hand-rolled selector here omitted input/select/textarea,
  // so a text field could tab out of the panel.
  //
  // Each row confirms inline rather than stacking a second modal, naming the
  // character and stating plainly that unlinking is irreversible from here.
  import { auth, unlinkAccount } from '$lib/auth.svelte.js';
  import Modal from '$lib/components/Modal.svelte';

  let { onClose } = $props();

  let pendingId = $state(null); // account_id currently showing its inline confirm
  let busyId = $state(null);    // account_id whose unlink is in flight
  let errorId = $state(null);
  let errorMsg = $state('');

  // Who the player lands on if this one goes: the same oldest-linked-first
  // order the server picks (and the same order /portal/me already returns
  // auth.accounts in), so the warning can name a real character instead of
  // speaking in the abstract.
  function survivorName(accountId) {
    const rest = auth.accounts.filter((a) => a.account_id !== accountId);
    return rest[0]?.character_name || null;
  }

  function askUnlink(accountId) {
    pendingId = accountId;
    errorId = null; errorMsg = '';
  }
  function cancelUnlink() {
    pendingId = null;
  }

  async function confirmUnlink(account) {
    busyId = account.account_id;
    errorId = null; errorMsg = '';
    try {
      await unlinkAccount(account.account_id); // reloads or navigates away on success
    } catch (e) {
      busyId = null;
      errorId = account.account_id;
      errorMsg = e?.message === 'not_your_account'
        ? 'That account is no longer linked to your profile.'
        : 'The unlink did not go through. Nothing changed.';
    }
  }
</script>

<Modal title="Linked accounts" size="md" {onClose}>
  {#if auth.accounts.length === 0}
    <p class="note">No linked accounts.</p>
  {:else}
    <ul class="list">
      {#each auth.accounts as a (a.account_id)}
        <li class="row">
          <div class="who">
            <span class="name">{a.character_name}</span>
            {#if a.active}<span class="badge">Active</span>{/if}
          </div>

          {#if pendingId === a.account_id}
            <div class="confirm">
              <p class="warn" role="note">
                Unlink {a.character_name}?
                {#if survivorName(a.account_id)}
                  {a.active
                    ? `You are currently playing as this account: unlinking it switches you to ${survivorName(a.account_id)}.`
                    : 'Your other linked accounts are unaffected.'}
                {:else}
                  This is your only linked account: unlinking it signs you out completely.
                {/if}
                To bring it back you will need to reconnect through Discord and pass the ownership check again. This cannot be undone from here.
              </p>
              {#if errorId === a.account_id}
                <p class="status" role="status" aria-live="polite">{errorMsg}</p>
              {/if}
              <div class="rowbtns">
                <button
                  class="btn danger" type="button"
                  onclick={() => confirmUnlink(a)} disabled={busyId === a.account_id}
                  aria-label={`Confirm unlink ${a.character_name}`}
                >{busyId === a.account_id ? 'Unlinking' : 'Unlink'}</button>
                <button class="btn" type="button" onclick={cancelUnlink} disabled={busyId === a.account_id}>Cancel</button>
              </div>
            </div>
          {:else}
            <button
              class="btn ghost" type="button"
              onclick={() => askUnlink(a.account_id)}
              aria-label={`Unlink ${a.character_name}`}
            >Unlink</button>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</Modal>

<style>
  .note { margin: 0; font-size: var(--text-sm); color: var(--text-muted); }

  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .row {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-2); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-1);
  }
  .who { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
  .name { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
    color: var(--ls-ibad); border: 1px solid color-mix(in srgb, var(--ls-ibad) 45%, transparent);
    border-radius: 999px; padding: 1px var(--space-2); white-space: nowrap;
  }

  .confirm { display: flex; flex-direction: column; gap: var(--space-2); }
  .warn {
    margin: 0; font-size: var(--text-xs); line-height: 1.5; color: var(--text-muted);
    padding: var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--ls-yellow) 45%, var(--edge));
    background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0));
  }
  .status { margin: 0; font-size: var(--text-xs); color: var(--ls-red); }

  .rowbtns { display: flex; gap: var(--space-2); }
  .btn {
    flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm);
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer;
  }
  .btn.ghost { flex: 0 0 auto; background: var(--bg-elevated); color: var(--text-muted); }
  .btn.ghost:hover { border-color: var(--accent); color: var(--text); }
  .btn.danger { color: var(--bg-deep); background: var(--ls-red); border-color: var(--ls-red); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
</style>
