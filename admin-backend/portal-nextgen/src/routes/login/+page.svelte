<script>
  // Two doors: "New to the portal" and "I've been here before". New players get
  // the original portal's Discord verification walk-through (sign in, find your
  // character, answer three quick questions), with Verify in game as the
  // no-Discord route. Returning players get Discord first and fold passkey and
  // password under a native details/summary. A browser that has signed in
  // before opens on the returning door; /start always opens the new one.
  import { onMount } from 'svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import GameVerification from '$lib/components/settings/GameVerification.svelte';
  import { authStatus, authPost, authMessage, usePasskey } from '$lib/signin.js';
  const SEEN = 'ls-signed-in-before';
  let status = $state(null);
  let username = $state('');
  let password = $state('');
  let busy = $state(false);
  let message = $state('');
  let path = $state('new');
  let othersOpen = $state(false);
  onMount(async () => {
    const params = new URLSearchParams(location.search);
    const error = params.get('error');
    if (error) message = authMessage(error);
    try { if (localStorage.getItem(SEEN)) path = 'back'; } catch (e) {}
    if (params.get('from') === 'start') path = 'new';
    try {
      status = await authStatus();
      if (status?.authenticated) {
        path = 'back';
        if (!status?.security?.discord_linked) othersOpen = true;
      }
    }
    catch (error) { message = authMessage(error); }
  });
  async function signIn(method) {
    if (busy) return;
    busy = true; message = '';
    try {
      if (method === 'discord') {
        try { localStorage.setItem(SEEN, '1'); } catch (e) {}
        const result = await authPost('discord-start');
        window.location.assign(result.url);
        return;
      }
      if (method === 'passkey') await usePasskey('passkey-options', 'passkey-login');
      else await authPost('password-login', { username, password });
      password = '';
      try { localStorage.setItem(SEEN, '1'); } catch (e) {}
      window.location.assign(status?.authenticated ? '/settings' : '/');
    } catch (error) { message = authMessage(error); }
    finally { busy = false; }
  }
</script>

<svelte:head><title>Sign in | Last Sietch</title><meta name="robots" content="noindex" /></svelte:head>
<div class="page">
  <PageHeader kicker="Last Sietch | Your passage" title={status?.authenticated ? 'Confirm your sign-in' : 'Enter the Sietch'} sub="Link your Dune character to see your storage, bases, market orders and Landsraad standing from anywhere." />
  <CarvedSlab>
    {#if status?.enabled}
      {#if !status.authenticated}
        <div class="doors" role="group" aria-label="Have you used the portal before?">
          <button class="door" aria-pressed={path === 'new'} onclick={() => (path = 'new')}>
            <span class="door-title">New to the portal</span>
            <span class="door-sub">Link your character for the first time</span>
          </button>
          <button class="door" aria-pressed={path === 'back'} onclick={() => (path = 'back')}>
            <span class="door-title">I've been here before</span>
            <span class="door-sub">Sign in to your existing profile</span>
          </button>
        </div>
      {/if}

      {#if path === 'new' && !status.authenticated}
        <h2>Verify your Last Sietch character</h2>
        <ol class="steps">
          <li><strong>Sign in with Discord.</strong> We use it only to confirm who you are. We never post for you or read your servers.</li>
          <li><strong>Find your character.</strong> Search the exact name shown above your character in game, not your Discord or Steam name.</li>
          <li><strong>Answer three quick questions</strong> about that character so we know it's yours.</li>
        </ol>
        <button class="primary" disabled={busy} onclick={() => signIn('discord')}>Verify with Discord</button>
        <p class="hint">Afterwards you can add a password or passkey in Settings, so Discord is not your only way in.</p>
        {#if status.game_enabled}
          <div class="alt">
            <GameVerification title="No Discord? Verify in game instead" />
            <p class="hint">After the code, you pick a portal username and a password or passkey.</p>
          </div>
        {/if}
      {:else}
        <h2>Sign in to your profile</h2>
        <button class="primary" disabled={busy} onclick={() => signIn('discord')}>Continue with Discord</button>
        <p class="hint">Use the same Discord account you linked before to keep your characters and history.</p>
        <details class="other" bind:open={othersOpen}>
          <summary class="other-summary">Other ways to sign in</summary>
          <div class="other-body">
            <button disabled={busy} onclick={() => signIn('passkey')}>Continue with a passkey</button>
            <form onsubmit={(event) => { event.preventDefault(); signIn('password'); }}>
              <label>Portal username <input bind:value={username} autocomplete="username" maxlength="32" required /></label>
              <label>Password or passphrase <input type="password" bind:value={password} autocomplete="current-password" maxlength="128" required /></label>
              <button disabled={busy}>Sign in with password</button>
            </form>
          </div>
        </details>
      {/if}
    {:else if status}
      <h2>Sign in with Discord</h2>
      <p>Connect your Discord account to link your character and open your settings.</p>
      <a class="primary" href="/portal/login?return_to=/" data-sveltekit-reload>Continue with Discord</a>
    {:else if !message}<p role="status">Loading sign-in options...</p>{/if}
    {#if busy}<p role="status">Waiting for sign-in...</p>{/if}
    {#if message}<p role="alert">{message}</p>{/if}
    <p class="hint">Lost access? Try another method saved on this profile. If none works, contact an admin through the community. Never share a password or verification code.</p>
  </CarvedSlab>
</div>

<style>
  .page { max-width: 760px; margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  h2 { font-size: var(--text-lg); margin: 0 0 var(--space-4); }
  form { display: grid; gap: var(--space-3); }
  label { display: grid; gap: var(--space-2); }
  input { box-sizing: border-box; width: 100%; padding: 12px; font: inherit; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm); }
  button, .primary { display: block; box-sizing: border-box; text-align: center; text-decoration: none; width: 100%; padding: 12px 16px; font: inherit; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer; }
  .primary { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, var(--metal-1)); color: var(--accent-bright); font-weight: 600; }
  button:hover { border-color: var(--accent); } button:disabled { opacity: .55; cursor: wait; }
  .hint { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.6; }

  .doors { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-3); margin-bottom: var(--space-6); }
  .door { display: grid; gap: var(--space-1); text-align: left; padding: var(--space-4); }
  .door[aria-pressed='true'] { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, var(--metal-1)); }
  .door-title { font-weight: 600; color: var(--text); }
  .door[aria-pressed='true'] .door-title { color: var(--accent-bright); }
  .door-sub { font-size: var(--text-sm); color: var(--text-muted); }
  @media (max-width: 520px) { .doors { grid-template-columns: 1fr; } }

  .steps { margin: 0 0 var(--space-5); padding-left: 1.4em; display: grid; gap: var(--space-3); line-height: 1.6; }
  .steps strong { color: var(--text); }
  .alt { display: grid; gap: var(--space-3); margin-top: var(--space-6); padding-top: var(--space-5); border-top: 1px solid var(--border-subtle); }
  .alt .hint { margin: 0; }

  .other { margin: var(--space-2) 0 var(--space-4); border: 1px solid var(--edge); border-radius: var(--radius-sm); }
  .other[open] { border-color: var(--edge-hi); }
  .other-summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: var(--space-2);
    min-height: 48px; padding: 0 var(--space-4); color: var(--text);
  }
  .other-summary::-webkit-details-marker { display: none; }
  .other-summary::before { content: '›'; color: var(--accent-text); font-size: var(--text-lg); transition: transform var(--motion-fast) var(--ease-out); }
  .other[open] .other-summary::before { transform: rotate(90deg); }
  .other-summary:hover { color: var(--accent-bright); }
  .other-body { display: grid; gap: var(--space-5); padding: 0 var(--space-4) var(--space-4); }
</style>
