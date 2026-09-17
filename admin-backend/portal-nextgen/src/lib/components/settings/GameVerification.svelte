<script>
  import { authPost, authMessage, usePasskey } from '$lib/signin.js';
  let { linking = false } = $props();
  let character = $state('');
  let code = $state('');
  let username = $state('');
  let password = $state('');
  let challenge = $state('');
  let verified = $state(false);
  let busy = $state(false);
  let message = $state('');

  async function act(action) {
    if (busy) return;
    busy = true; message = '';
    try {
      if (action === 'start') {
        const result = await authPost('game-start', { character });
        challenge = result.challenge_id;
        message = `Check the Cielago whisper to ${result.character_name}. The code expires in five minutes.`;
      } else if (action === 'verify') {
        await authPost('game-verify', { challenge_id: challenge, code });
        code = '';
        if (linking) {
          await authPost('game-link', { challenge_id: challenge });
          window.location.reload();
        } else verified = true;
      } else if (action === 'password') {
        await authPost('bootstrap-password', { challenge_id: challenge, username, password });
        password = '';
        window.location.assign('/settings');
      } else {
        await usePasskey('bootstrap-passkey-options', 'bootstrap-passkey', { challenge_id: challenge, username, label: 'First passkey' });
        window.location.assign('/settings');
      }
    } catch (error) { message = authMessage(error); }
    finally { busy = false; }
  }
</script>

<div class="verification">
  <h3>{linking ? 'Link a game account' : 'New here? Verify in game'}</h3>
  {#if verified}
    <p>Game account verified. Save your first sign-in method within ten minutes to finish creating your profile.</p>
    <form onsubmit={(event) => { event.preventDefault(); act('password'); }}>
      <label>Portal username <input bind:value={username} autocomplete="username" minlength="3" maxlength="32" required /></label>
      <label>Password or passphrase <input type="password" bind:value={password} autocomplete="new-password" minlength="8" maxlength="128" /></label>
      <p class="hint">Use 8 to 128 characters. No email is needed.</p>
      <div class="actions">
        <button disabled={busy || password.length < 8}>Create profile with password</button>
        <button type="button" disabled={busy || username.length < 3} onclick={() => act('passkey')}>Create profile with passkey</button>
      </div>
    </form>
  {:else if challenge}
    <form onsubmit={(event) => { event.preventDefault(); act('verify'); }}>
      <label>Verification code <input bind:value={code} inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label>
      <div class="actions"><button disabled={busy}>Verify code</button><button type="button" disabled={busy} onclick={() => { challenge = ''; code = ''; message = ''; }}>Start again</button></div>
    </form>
  {:else}
    <p>Stay in game and enter your exact character name. Cielago will send a private code for this browser.</p>
    {#if !linking}<p class="hint">Already used the portal? Sign in to your existing profile. A game code cannot recover an existing profile or combine accounts.</p>{/if}
    <form onsubmit={(event) => { event.preventDefault(); act('start'); }}>
      <label>Online character <input bind:value={character} maxlength="80" autocomplete="off" required /></label>
      <button disabled={busy}>Send verification code</button>
    </form>
  {/if}
  {#if busy}<p role="status">Working. Keep this page open.</p>{/if}
  {#if message}<p role="status">{message}</p>{/if}
</div>

<style>
  .verification { display: grid; gap: var(--space-3); }
  h3, p { margin: 0; }
  form, label { display: grid; gap: var(--space-2); }
  form { gap: var(--space-3); }
  input { width: 100%; box-sizing: border-box; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); padding: 12px; border-radius: var(--radius-sm); font: inherit; }
  .actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  button { padding: 12px 16px; color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm); font: inherit; cursor: pointer; }
  button:hover { border-color: var(--accent); } button:disabled { opacity: .55; cursor: wait; }
  .hint { color: var(--text-muted); font-size: var(--text-sm); }
</style>
