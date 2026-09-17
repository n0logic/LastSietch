// useAuthGate: one read of the auth store for every route that gates content.
// Replaces the `auth.status === 'loading' / 'anon'` branch pair copied into 10
// routes, and gives a role check a single place to live.
//
// FAILS CLOSED. `/portal/me` carries `roles` (wave 3): a server-granted list
// resolved from the admin users table by Discord id, `[]` for everyone else and
// on any backend error. An absent or empty field means NOT allowed, always.
// The gate must never infer a role from a Discord handle, a discord id, or any
// client-side allowlist: the browser is not a trust boundary; the server is the
// only place a role is decided.
//
// Returns GETTERS rather than values, so reading `gate.allowed` inside a
// template or an $effect subscribes to the underlying $state proxy. This
// matches the rest of src/lib/*.svelte.js, which use $state objects and plain
// functions and no $derived.
import { auth, LOGIN_URL } from './auth.svelte.js';

export function useAuthGate(opts = {}) {
  const role = opts.role || null;

  function roles() {
    return Array.isArray(auth.roles) ? auth.roles : [];
  }

  function hasRole() {
    return !role || roles().includes(role);
  }

  return {
    get status() { return auth.status; },
    get loading() { return auth.status === 'loading'; },
    get authed() { return auth.status === 'authed'; },
    get anon() { return auth.status === 'anon'; },
    get allowed() { return auth.status === 'authed' && hasRole(); },
    get reason() {
      if (auth.status === 'loading') return 'loading';
      if (auth.status !== 'authed') return 'anon';
      if (!hasRole()) return 'missing_role';
      return 'ok';
    },
    get loginUrl() { return LOGIN_URL; },
  };
}
