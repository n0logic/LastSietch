// Shared portal identity store. Wraps the existing FastAPI Discord-OAuth flow:
//   GET  /portal/login?return_to=/           -> Discord authorize (full nav)
//   GET  /portal/me                           -> { authenticated, discord_handle, linked }
//   POST /portal/logout (CSRF)                -> clears session + csrf cookies
// The session cookie (ls_portal_session, path=/) is set by the live host,
// so identity only resolves on the deployed origin; localhost dev has no cookie
// and degrades to the anonymous chrome (status 'anon'), which is the correct
// logged-out experience.
import { api } from './api.js';

// $state object (not a reassignable binding) so the same proxy is shared across
// every importing component and property mutations propagate reactively.
export const auth = $state({
  status: 'loading', // 'loading' | 'anon' | 'authed'
  discordHandle: '',
  linked: [], // [{ character_name, linked_at }]
  // Server-authoritative flag for the "Download my data" control (dark until
  // LASTSIETCH_EXPORT_ENABLED=1). Mirrors /portal/me.export_enabled.
  exportEnabled: false,
  // Multi-character: every non-Deleted character on the ACTIVE account.
  // [{ controller_id, char_name, lvl, online, is_default, selected }]. Empty
  // for anon or single-character accounts that predate the switcher load.
  characters: [],
  // Multi-account: every game account linked to this Discord.
  // [{ account_id, character_name, active }]. Empty unless the feature is on.
  accounts: [],
  multiaccountEnabled: false,
  profileSession: false,
  // Server-granted role names. /portal/me does not send this field yet, so it stays
  // empty and every role-gated surface fails closed. A role is NEVER inferred from a
  // discord handle or any client-side allowlist.
  roles: [],
});

// return_to is allowlisted server-side to a relative path (on the portal host it
// defaults to /); '/' keeps an existing-link user inside the app after callback.
export const LOGIN_URL = '/login';

function setAnon() {
  auth.status = 'anon';
  auth.discordHandle = '';
  auth.linked = [];
  auth.characters = [];
  auth.accounts = [];
  auth.multiaccountEnabled = false;
  auth.profileSession = false;
  auth.exportEnabled = false;
  auth.roles = [];
}

/** Resolve identity from /portal/me. Any failure (401, network) degrades to the
 *  anonymous chrome rather than blocking the page. */
export async function loadAuth() {
  try {
    const me = await api.session.me();
    if (me && me.authenticated) {
      auth.status = 'authed';
      auth.discordHandle = me.discord_handle || '';
      auth.linked = Array.isArray(me.linked) ? me.linked : [];
      auth.accounts = Array.isArray(me.accounts) ? me.accounts : [];
      auth.multiaccountEnabled = me.multiaccount_enabled === true;
      auth.profileSession = me.profile_session === true;
      auth.exportEnabled = me.export_enabled === true;
      auth.roles = Array.isArray(me.roles) ? me.roles : [];
      await loadCharacters();
    } else {
      setAnon();
    }
  } catch (e) {
    setAnon();
  }
}

/** Load the active account's character list for the switcher. Failure leaves an
 *  empty list (switcher hidden), so a list outage never blocks the app. */
export async function loadCharacters() {
  try {
    const r = await api.session.characters();
    auth.characters = Array.isArray(r?.characters) ? r.characters : [];
  } catch (e) {
    auth.characters = [];
  }
}

/** Switch the character the portal acts as. The server validates ownership and
 *  sets the selection cookie; we then reload so every per-character surface
 *  (bank balance, vitals, and all write targets) re-resolves against the newly
 *  selected character. Throws on failure so the caller can revert the control. */
export async function selectCharacter(controllerId) {
  await api.session.selectCharacter(controllerId);
  try { window.location.reload(); } catch (e) {}
}

/** Switch the active linked account. The server re-pins the session to the chosen
 *  account (validated against the caller's own links); we reload so every
 *  per-account surface (characters, bank, storage, ...) re-resolves. Throws on
 *  failure so the caller can revert the control. */
export async function selectAccount(accountId) {
  await api.session.selectAccount(accountId);
  try { window.location.reload(); } catch (e) {}
}

/** Unlink one of the caller's own linked accounts (alt-aware: the legacy V1
 *  unlink always dropped whatever the session happened to be pinned to; this
 *  names the account_id explicitly). The server re-proves ownership from the
 *  session and reports what happened to the session so we can navigate
 *  correctly: 'cleared' (that was the last account) sends the player to the
 *  V2 home so they land somewhere sane while signed out, 'switched' or
 *  'unchanged' just reload in place so every account-scoped surface
 *  re-resolves. Throws on failure so the confirmation dialog can show the
 *  error and let the player retry or cancel. */
export async function unlinkAccount(accountId) {
  if (auth.profileSession) {
    const { authPost, authMessage } = await import('./signin.js');
    try {
      const result = await authPost('account-unlink', { account_id: accountId });
      window.location.reload();
      return result;
    } catch (error) { throw new Error(authMessage(error)); }
  }
  const r = await api.session.unlinkAccount(accountId);
  try {
    if (r?.session === 'cleared') window.location.href = '/';
    else window.location.reload();
  } catch (e) {}
  return r;
}

/** Sign out: clear the server session (cookies dropped via the 302's Set-Cookie),
 *  then flip local state to anonymous. */
export async function logout() {
  try {
    await api.session.logout();
  } catch (e) {
    // Even on transport failure, drop local state so the UI reflects sign-out.
  }
  // The rescue cooldown mirror is per ACCOUNT, not per browser: drop it so a
  // different account signing in does not inherit a false disabled state
  // (the server cooldown stays authoritative either way).
  try { localStorage.removeItem('ls-rescue-until'); } catch (e) {}
  // The preferences mirror belongs to the player who just left (lazy import:
  // prefs.svelte.js imports this module).
  import('$lib/prefs.svelte.js').then((m) => m.dropMirror()).catch(() => {});
  setAnon();
}
