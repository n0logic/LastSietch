<script>
  // One "Add a game account" control for the two shipped ways to link another
  // game account. Each keeps its old gate and its old server flow untouched:
  //   * Verify in game: the Cielago whisper code (GameVerification linking),
  //     offered when game verification is on and the sign-in is recent.
  //   * Link through Discord (+ Alt): a real form POST to /portal/link/add with
  //     the csrf cookie echoed in the body, a full navigation into the Discord
  //     ownership check. The server sends profile sessions back from that
  //     endpoint, so it is offered to Discord sessions only, as before.
  import { onMount } from 'svelte';
  import { auth } from '$lib/auth.svelte.js';
  import { readCookie, CSRF_COOKIE } from '$lib/api.js';
  import { authStatus } from '$lib/signin.js';
  import GameVerification from './GameVerification.svelte';

  let status = $state(null);
  let open = $state(false);
  let method = $state('game');

  const viaGame = $derived(Boolean(status?.game_enabled && status?.recent));
  const viaDiscord = $derived(!auth.profileSession && auth.multiaccountEnabled);
  const needsConfirm = $derived(Boolean(status?.game_enabled && !status?.recent && auth.profileSession));
  const chosen = $derived(viaGame && viaDiscord ? method : viaGame ? 'game' : viaDiscord ? 'discord' : '');

  onMount(async () => {
    // The backend's redirect for a profile session that tried + Alt, relink or unlink.
    if (location.hash === '#sign-in-security' || auth.accounts.length === 0) open = true;
    try { status = await authStatus(); } catch (e) { status = null; }
  });
</script>

<details class="add" bind:open>
  <summary class="add-summary"><span class="plus" aria-hidden="true">+</span> Add a game account</summary>
  <div class="add-body">
    {#if viaGame && viaDiscord}
      <div class="seg" role="group" aria-label="How to link the account">
        <button class="seg-btn mono" class:on={method === 'game'} type="button" aria-pressed={method === 'game'} onclick={() => (method = 'game')}>Verify in game</button>
        <button class="seg-btn mono" class:on={method === 'discord'} type="button" aria-pressed={method === 'discord'} onclick={() => (method = 'discord')}>Link through Discord</button>
      </div>
    {/if}

    {#if chosen === 'game'}
      <GameVerification linking />
    {:else if chosen === 'discord'}
      <div class="method">
        <p class="explain">Link another game account you own through the Discord ownership check. You leave this page and come back once it is linked.</p>
        <form method="POST" action="/portal/link/add">
          <input type="hidden" name="csrf_token" value={readCookie(CSRF_COOKIE)} />
          <button class="btn" type="submit" title="Link another game account (alt)">Link through Discord</button>
        </form>
      </div>
    {:else if needsConfirm}
      <p class="explain">Confirm a saved sign-in method first. Linking a game account needs a sign-in from the last ten minutes.</p>
      <a class="btn" href="/login">Confirm your sign-in</a>
    {:else if status}
      <p class="explain">Adding another game account is not available right now.</p>
    {/if}
  </div>
</details>

<style>
  .add { margin-top: var(--space-4); border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--metal-0); }
  .add[open] { border-color: var(--edge-hi); }
  .add-summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: var(--space-2);
    min-height: 44px; padding: 0 var(--space-3);
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
  }
  .add-summary::-webkit-details-marker { display: none; }
  .add-summary:hover { color: var(--accent-bright); }
  .plus { color: var(--accent-text); font-size: var(--text-base); }
  .add-body { display: grid; gap: var(--space-3); padding: 0 var(--space-3) var(--space-4); }

  .seg {
    justify-self: start; display: inline-flex; flex-wrap: wrap; overflow: hidden;
    border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--metal-0);
  }
  .seg-btn {
    background: transparent; border: 0; cursor: pointer; min-height: 40px;
    color: var(--text-muted); padding: var(--space-2) var(--space-3);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
  }
  .seg-btn + .seg-btn { border-left: 1px solid var(--edge); }
  .seg-btn:hover { color: var(--text); }
  .seg-btn.on { color: var(--accent-bright); background: color-mix(in srgb, var(--accent) 12%, transparent); }

  .method { display: grid; gap: var(--space-3); }
  .explain { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
  .btn {
    justify-self: start; display: inline-block; text-decoration: none;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-sm); cursor: pointer;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
  }
  .btn:hover { border-color: var(--accent); }
</style>
