<script>
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Advanced from './Advanced.svelte';
  import { authStatus, authPost, authMessage, usePasskey } from '$lib/signin.js';
  let status = $state(null);
  let username = $state('');
  let password = $state('');
  let label = $state('');
  let busy = $state(false);
  let message = $state('');
  let removal = $state(null);
  async function refresh() {
    status = await authStatus();
    username = status.security?.username || '';
  }
  onMount(async () => { try { await refresh(); } catch (error) { message = authMessage(error); } });
  async function act(action, body = {}) {
    if (busy) return;
    busy = true; message = '';
    try {
      let result;
      if (action === 'passkey-add') {
        await authPost('username-save', { username });
        result = await usePasskey('passkey-register-options', 'passkey-register', { label });
        label = '';
      } else result = await authPost(action, body);
      password = ''; removal = null;
      if (result?.url) { window.location.assign(result.url); return; }
      if (result?.signed_out) { window.location.assign('/login'); return; }
      await refresh();
      message = 'Saved. Your security settings are up to date.';
    } catch (error) { message = authMessage(error); }
    finally { busy = false; }
  }
</script>

{#snippet confirmRemoval()}
  <div class="confirmation" role="alert">
    <p>Remove {removal.name}? Sessions using this method will stop working. You must keep another sign-in method.</p>
    <div class="actions"><button disabled={busy} onclick={() => act('method-remove', removal)}>Confirm removal</button><button disabled={busy} onclick={() => removal = null}>Cancel</button></div>
  </div>
{/snippet}

{#if status?.enabled}
  <CarvedSlab>
    <div id="sign-in-security" class="security">
      <p>Save more than one sign-in method so you have a way back in. Your linked game accounts and allowances belong to this profile.</p>
      {#if !status.recent}
        <p>Confirm a saved sign-in method before changing credentials or linked accounts. Confirmation lasts ten minutes.</p>
        <a href="/login">Confirm your sign-in</a>
      {/if}
      <fieldset class="username" disabled={busy || !status.recent}>
        <label>Portal username <input bind:value={username} autocomplete="username" minlength="3" maxlength="32" required /></label>
        <p class="hint">One username for your password and your passkeys.</p>
      </fieldset>
      <fieldset disabled={busy || !status.recent}>
        <legend>Password or passphrase</legend>
        <form onsubmit={(event) => { event.preventDefault(); act('password-save', { username, password }); }}>
          <label>{status.security?.password ? 'New password' : 'Choose a password'} <input type="password" bind:value={password} autocomplete="new-password" minlength="8" maxlength="128" required /></label>
          <p class="hint">Use 8 to 128 characters. Spaces and password-manager paste are supported. Changing your password signs out your other sessions.</p>
          <div class="actions"><button>Save password</button></div>
        </form>
      </fieldset>
      <fieldset disabled={busy || !status.recent}>
        <legend>Passkeys</legend>
        <p class="hint">Use your device lock, fingerprint, face or a security key. Save up to eight passkeys. Adding one signs out your other sessions.</p>
        <ul>
          {#each status.security?.passkeys || [] as key (key.id)}
            <li><span>{key.label}</span></li>
          {:else}<li>No passkeys saved yet.</li>{/each}
        </ul>
        <form onsubmit={(event) => { event.preventDefault(); act('passkey-add'); }}>
          <label>Passkey label <input bind:value={label} maxlength="64" placeholder="For example, my phone" /></label>
          <button>Add passkey</button>
        </form>
      </fieldset>
      <fieldset disabled={busy || !status.recent}>
        <legend>Discord</legend>
        <p class="hint">Discord is optional. Disconnecting it preserves your portal history and allowances, and stops Discord delivery and Discord-based staff access.</p>
        {#if status.security?.discord_linked}
          <button onclick={() => removal = { method: 'discord', name: 'Discord' }}>Disconnect Discord</button>
        {:else}<button onclick={() => act('discord-start', { link: true })}>Connect Discord</button>{/if}
      </fieldset>
      {#if removal?.method === 'discord'}{@render confirmRemoval()}{/if}
      <fieldset disabled={busy || !status.recent}>
        <legend>Active sessions</legend>
        <ul>
          {#each status.security?.sessions || [] as session (session.id)}
            <li><div><strong>{session.current ? 'This browser' : session.method}</strong><p class="hint">Last active {new Date(session.last_seen * 1000).toLocaleString()}</p><p class="device">{session.device || 'Device not recorded'}</p></div><button onclick={() => act('session-revoke', { id: session.id })}>Sign out</button></li>
          {:else}<li>Confirm your sign-in to start a managed session.</li>{/each}
        </ul>
      </fieldset>
      {#if busy}<p role="status">Working. Keep this page open.</p>{/if}
      {#if message}<p role="status">{message}</p>{/if}
      <p class="hint">A game code does not recover an existing profile. If you lose every saved method, contact an admin. Profiles cannot be merged automatically.</p>
      <Advanced label="Advanced: remove a sign-in method">
        <fieldset class="bare" disabled={busy || !status.recent}>
          <ul>
            {#if status.security?.password}
              <li><span>Password</span><button onclick={() => removal = { method: 'password', name: 'your password' }}>Remove password</button></li>
            {/if}
            {#each status.security?.passkeys || [] as key (key.id)}
              <li><span>Passkey: {key.label}</span><button aria-label="Remove passkey {key.label}" onclick={() => removal = { method: 'passkey', id: key.id, name: key.label }}>Remove</button></li>
            {/each}
            {#if !status.security?.password && !status.security?.passkeys?.length}<li>No password or passkey saved.</li>{/if}
          </ul>
        </fieldset>
        {#if removal && removal.method !== 'discord'}{@render confirmRemoval()}{/if}
      </Advanced>
    </div>
  </CarvedSlab>
{:else if message}<p role="status">{message}</p>{/if}

<style>
  .security { display: grid; gap: var(--space-4); }
  p { margin: 0; }
  fieldset { min-width: 0; border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-4); display: grid; gap: var(--space-3); }
  .username { border-color: var(--edge-hi); }
  legend { padding: 0 var(--space-2); } form, label { display: grid; gap: var(--space-2); } form { gap: var(--space-3); }
  input { width: 100%; box-sizing: border-box; padding: 12px; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm); font: inherit; }
  button { padding: 12px 16px; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm); font: inherit; cursor: pointer; }
  button:hover { border-color: var(--accent); } button:disabled, fieldset:disabled { opacity: .6; }
  .actions { display: flex; flex-wrap: wrap; gap: var(--space-2); } ul { margin: 0; padding: 0; list-style: none; }
  li { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); padding: var(--space-3) 0; border-bottom: 1px solid var(--edge); }
  li > div, li > span { min-width: 0; flex: 1 1 auto; overflow-wrap: break-word; } li > button { flex: 0 0 auto; }
  .bare { border: 0; padding: 0; } .device { font-size: var(--text-xs); overflow-wrap: anywhere; }
  .hint { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.6; }
  .confirmation { border: 1px solid var(--accent); padding: var(--space-3); display: grid; gap: var(--space-3); }
</style>
