<script>
  import { onMount } from 'svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import GameVerification from '$lib/components/settings/GameVerification.svelte';
  import { authStatus, authPost, authMessage, usePasskey } from '$lib/signin.js';
  let status = $state(null);
  let username = $state('');
  let password = $state('');
  let busy = $state(false);
  let message = $state('');
  onMount(async () => {
    const error = new URLSearchParams(location.search).get('error');
    if (error) message = authMessage(error);
    try { status = await authStatus(); }
    catch (error) { message = authMessage(error); }
  });
  async function signIn(method) {
    if (busy) return;
    busy = true; message = '';
    try {
      if (method === 'discord') {
        const result = await authPost('discord-start');
        window.location.assign(result.url);
        return;
      }
      if (method === 'passkey') await usePasskey('passkey-options', 'passkey-login');
      else await authPost('password-login', { username, password });
      password = '';
      window.location.assign(status?.authenticated ? '/settings' : '/');
    } catch (error) { message = authMessage(error); }
    finally { busy = false; }
  }
</script>

<svelte:head><title>Sign in | Last Sietch</title><meta name="robots" content="noindex" /></svelte:head>
<div class="page">
  <PageHeader kicker="Last Sietch | Your passage" title={status?.authenticated ? 'Confirm your sign-in' : 'Welcome back'} sub="Your game accounts, characters and portal history stay together in one profile." />
  <div class="panels">
    <CarvedSlab>
      {#if status?.enabled}
        <h2>Sign in to your profile</h2>
        <button class="primary" disabled={busy} onclick={() => signIn('passkey')}>Continue with a passkey</button>
        <form onsubmit={(event) => { event.preventDefault(); signIn('password'); }}>
          <label>Portal username <input bind:value={username} autocomplete="username" maxlength="32" required /></label>
          <label>Password or passphrase <input type="password" bind:value={password} autocomplete="current-password" maxlength="128" required /></label>
          <button disabled={busy}>Sign in with password</button>
        </form>
        <button class="discord" disabled={busy} onclick={() => signIn('discord')}>Continue with Discord</button>
        <p class="hint">Existing Discord user? Choose Discord to keep your linked accounts, then add a password or passkey in Settings.</p>
      {:else if status}
        <h2>Sign in with Discord</h2>
        <p>Connect your existing Discord account to open your linked characters and settings.</p>
        <a class="primary" href="/portal/login?return_to=/" data-sveltekit-reload>Continue with Discord</a>
      {:else if !message}<p role="status">Loading sign-in options...</p>{/if}
      {#if busy}<p role="status">Waiting for sign-in...</p>{/if}
      {#if message}<p role="alert">{message}</p>{/if}
      <p class="hint">Lost access? Try another method saved on this profile. If none works, contact an admin through the community. Never share a password or verification code.</p>
    </CarvedSlab>
    {#if status?.game_enabled && !status?.authenticated}<CarvedSlab><GameVerification /></CarvedSlab>{/if}
  </div>
</div>

<style>
  .page { max-width: 760px; margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .panels { display: grid; gap: var(--space-4); margin-top: var(--space-4); }
  h2 { font-size: var(--text-lg); margin: 0 0 var(--space-4); }
  form { display: grid; gap: var(--space-3); margin: var(--space-5) 0; }
  label { display: grid; gap: var(--space-2); }
  input { box-sizing: border-box; width: 100%; padding: 12px; font: inherit; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm); }
  button, .primary { display: block; box-sizing: border-box; text-align: center; text-decoration: none; width: 100%; padding: 12px 16px; font: inherit; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer; }
  .primary { border-color: var(--accent); } button:hover { border-color: var(--accent); } button:disabled { opacity: .55; cursor: wait; }
  .hint { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.6; }
</style>
