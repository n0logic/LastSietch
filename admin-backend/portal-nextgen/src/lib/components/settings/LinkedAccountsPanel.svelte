<script>
  // Linked game accounts: which accounts this Discord holds, plus the two
  // affordances the topbar used to carry. "Manage" opens the shipped
  // alt-aware unlink dialog rather than growing a second one. "+ Alt" is a
  // real form POST to /portal/link/add with the csrf cookie echoed in the
  // body, exactly as the topbar sent it: linking another account is a full
  // navigation into the Discord ownership check, not a JSON write.
  import { auth } from '$lib/auth.svelte.js';
  import { readCookie, CSRF_COOKIE } from '$lib/api.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import AccountManageDialog from '$lib/components/AccountManageDialog.svelte';

  let manageOpen = $state(false);
</script>

<CarvedSlab>
  <p class="kicker mono">Linked accounts | water bonds</p>

  {#if auth.accounts.length === 0}
    <SealedPanel
      status="empty" action="none"
      emptyText="No game account is linked to this profile yet. Link one to see your characters here."
    />
  {:else}
    <ul class="list">
      {#each auth.accounts as a (a.account_id)}
        <li class="row">
          <span class="name">{a.character_name}</span>
          {#if a.active}<span class="badge mono">Active</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}

  <div class="actions">
    {#if auth.profileSession}
      <a class="btn" href="#sign-in-security">Link an account in security settings</a>
    {:else if auth.multiaccountEnabled}
      <form method="POST" action="/portal/link/add">
        <input type="hidden" name="csrf_token" value={readCookie(CSRF_COOKIE)} />
        <button class="btn" type="submit" title="Link another game account (alt)">+ Alt</button>
      </form>
    {/if}
    {#if auth.accounts.length > 0}
      <button class="btn" type="button" onclick={() => (manageOpen = true)}>Manage</button>
    {/if}
  </div>
</CarvedSlab>

{#if manageOpen}
  <AccountManageDialog onClose={() => (manageOpen = false)} />
{/if}

<style>
  .list { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .row {
    display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
    padding: var(--space-2) var(--space-3); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); background: var(--metal-1);
  }
  .name { font-size: var(--text-sm); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge {
    font-size: 10px; letter-spacing: .1em; text-transform: uppercase; white-space: nowrap;
    color: var(--ls-ibad); border: 1px solid color-mix(in srgb, var(--ls-ibad) 45%, transparent);
    border-radius: 999px; padding: 1px var(--space-2);
  }

  .actions { display: flex; gap: var(--space-2); margin-top: var(--space-3); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-1) var(--space-3);
    border-radius: var(--radius-sm); cursor: pointer;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
  }
  .btn:hover { border-color: var(--accent); }
</style>
