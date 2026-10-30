<script>
  // Self-relink (parity audit G1-a, owner 2026-10-30: a plain button now, the A2
  // flow later). A real form POST to the shipped /portal/link/relink with the
  // csrf cookie echoed in the body, the same shape as + Alt. That route binds
  // nothing itself: it re-enters the first-link picker and ownership quiz, so a
  // relink is proved exactly like a first link.
  //
  // The server sends a profile (non-Discord) session back to #sign-in-security
  // from this route, so for one of those this says why instead of offering a
  // button that cannot work.
  import { auth } from '$lib/auth.svelte.js';
  import { readCookie, CSRF_COOKIE } from '$lib/api.js';
</script>

<section class="relink" aria-labelledby="relink-title">
  <h3 id="relink-title" class="title mono">Relink to another character</h3>
  {#if auth.profileSession}
    <p class="explain">Relinking goes through the Discord ownership check, so it needs a Discord sign-in. Sign out and sign back in with Discord, then come back here.</p>
  {:else}
    <p class="explain">Point your Discord at a different character, for example after a character transfer. You search for the character and answer the same ownership questions as your first link. You are signed out of the portal until you finish, and the new link replaces the accounts linked now.</p>
    <form method="POST" action="/portal/link/relink">
      <input type="hidden" name="csrf_token" value={readCookie(CSRF_COOKIE)} />
      <button class="btn" type="submit">Relink</button>
    </form>
  {/if}
</section>

<style>
  .relink { display: grid; gap: var(--space-2); margin-top: var(--space-4); padding-top: var(--space-4); border-top: 1px solid var(--edge); }
  .title { margin: 0; font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; color: var(--text); }
  .explain { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
  .btn {
    justify-self: start; display: inline-block; min-height: 40px; cursor: pointer;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge);
  }
  .btn:hover { border-color: var(--accent); }
</style>
