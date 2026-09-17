// Account preferences (wave 10b). One JSON document per scope: '' is identity-wide,
// 'char:<controller_id>' is one character. The server owns the doc; localStorage keeps
// a mirror so a page paints with the last known choice before the GET returns and an
// anonymous visitor keeps a browser-only preference. Keys are whitelisted server-side
// (portal_prefs.py); an unknown key is refused whole, so add the key there first.
//
// Two rules keep a shared browser honest (review 2026-09-04):
//   1. The mirror is stamped with the identity it belongs to and ignored on mismatch,
//      and logout() drops it, so player B never paints or merges player A's doc.
//   2. Nothing is PUT until the server doc has been read (prefs.status 'ready'), so an
//      unverified mirror can never be merged over the real doc.
import { api } from '$lib/api.js';
import { auth } from '$lib/auth.svelte.js';

export const MIRROR_KEY = 'ls-prefs';
const PUT_DEBOUNCE = 400;
const RETRY_BASE = 5000;
const RETRY_MAX = 60000;

export const PREF_DEFAULTS = Object.freeze({
  storage_view: 'grid',
  map_hidden: {},
  map_viewer: {},
  reach_pins: [],
  reach_recent: [],
  market_searches: [],
  activity_seen: null,
});

export const prefs = $state({
  status: 'idle', // 'idle' | 'loading' | 'ready' | 'anon'
  identity: {},
  scopes: {},
  updated_utc: null,
});

const isObj = (v) => !!v && typeof v === 'object' && !Array.isArray(v);

// The identity the mirror belongs to. '' is an anonymous visitor; an authed player
// is keyed by handle, so a handle change only costs one repaint from the server.
function who() {
  return auth.status === 'authed' ? `u:${auth.discordHandle || ''}` : '';
}

// A handle-less authed player (no link row for the active account) cannot be told
// apart from the next one on the same browser, so the mirror is skipped for them.
function mirrorable() {
  return !(auth.status === 'authed' && !auth.discordHandle);
}

function readMirror() {
  if (!mirrorable()) return { identity: {}, scopes: {} };
  try {
    const raw = localStorage.getItem(MIRROR_KEY);
    const doc = raw ? JSON.parse(raw) : null;
    if (isObj(doc) && doc.who === who()) {
      return { identity: isObj(doc.identity) ? doc.identity : {}, scopes: isObj(doc.scopes) ? doc.scopes : {} };
    }
  } catch (e) {}
  return { identity: {}, scopes: {} };
}

function writeMirror() {
  if (!mirrorable()) return;
  try {
    localStorage.setItem(MIRROR_KEY, JSON.stringify({ who: who(), identity: prefs.identity, scopes: prefs.scopes }));
  } catch (e) {}
}

/** Called from logout(): the next visitor on this browser starts from nothing. */
export function dropMirror() {
  try { localStorage.removeItem(MIRROR_KEY); } catch (e) {}
  prefs.identity = {};
  prefs.scopes = {};
  prefs.updated_utc = null;
  prefs.status = 'anon';
  pending.clear();
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
  retries.clear();
}

function docFor(scope) {
  if (!scope) return prefs.identity;
  return prefs.scopes[scope] || {};
}

function putDoc(scope, doc) {
  if (scope) prefs.scopes = { ...prefs.scopes, [scope]: doc };
  else prefs.identity = doc;
}

/** 'char:<controller_id>' of the selected character, or '' when none is known. */
export function charScope() {
  const sel = (auth.characters || []).find((c) => c?.selected);
  const id = Number(sel?.controller_id);
  return Number.isInteger(id) && id > 0 ? `char:${id}` : '';
}

export function getPref(key, { scope = '' } = {}) {
  if (scope) {
    const v = docFor(scope)[key];
    if (v !== undefined && v !== null) return v;
  }
  const v = prefs.identity[key];
  if (v !== undefined && v !== null) return v;
  return PREF_DEFAULTS[key];
}

// One pending PATCH per scope (never the whole doc); keys set while a flush waits
// fold into it, and a failed flush keeps the patch and retries with backoff.
const pending = new Map();
const timers = new Map();
const retries = new Map();

function canWrite() {
  return auth.status === 'authed' && prefs.status === 'ready';
}

function schedule(scope, delay) {
  clearTimeout(timers.get(scope));
  timers.set(scope, setTimeout(() => flush(scope), delay));
}

function queue(scope, patch) {
  if (!canWrite()) return;
  pending.set(scope, { ...(pending.get(scope) || {}), ...patch });
  schedule(scope, PUT_DEBOUNCE);
}

function applyPatch(doc, patch) {
  const next = { ...doc };
  for (const [k, v] of Object.entries(patch)) {
    if (v === null || v === undefined) delete next[k];
    else next[k] = v;
  }
  return next;
}

async function flush(scope, { keepalive = false } = {}) {
  const patch = pending.get(scope);
  pending.delete(scope);
  timers.delete(scope);
  if (!patch || !canWrite()) return;
  try {
    const r = await api.settings.prefs.set(scope, patch, { keepalive });
    if (r?.ok && isObj(r.prefs)) {
      // A patch queued while this one was in flight is newer than the response:
      // lay it over the server doc so the player's latest pick survives.
      putDoc(scope, applyPatch(r.prefs, pending.get(scope) || {}));
      prefs.updated_utc = r.updated_utc ?? prefs.updated_utc;
      writeMirror();
      retries.delete(scope);
      return;
    }
    // A refused patch (whitelist, caps) is dropped: resending it cannot succeed.
    retries.delete(scope);
  } catch (e) {
    // sendCsrfJSON throws on every refusal too (ok:false or a non-2xx), so a
    // 4xx is a verdict, not an outage: resending it cannot succeed. Drop it.
    const st = Number(e?.status) || 0;
    if (st >= 400 && st < 500 && st !== 408 && st !== 429) {
      retries.delete(scope);
      return;
    }
    // Transport failure or a server error: keep THIS patch (newer keys win over
    // it) and retry with backoff.
    pending.set(scope, { ...patch, ...(pending.get(scope) || {}) });
    const n = (retries.get(scope) || 0) + 1;
    retries.set(scope, n);
    schedule(scope, Math.min(RETRY_MAX, RETRY_BASE * 2 ** (n - 1)));
  }
}

// pagehide: a plain fetch is routinely cancelled at unload; keepalive lets it land.
function flushAll() {
  for (const scope of Array.from(pending.keys())) flush(scope, { keepalive: true });
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', flushAll);
}

export function setPref(key, value, { scope = '' } = {}) {
  if (!Object.hasOwn(PREF_DEFAULTS, key)) throw new Error(`unknown pref ${key}`);
  putDoc(scope, applyPatch(docFor(scope), { [key]: value }));
  writeMirror();
  queue(scope, { [key]: value === undefined ? null : value });
}

export function clearScope(scope) {
  const nulls = Object.fromEntries(Object.keys(docFor(scope)).map((k) => [k, null]));
  if (scope) {
    const { [scope]: _drop, ...rest } = prefs.scopes;
    prefs.scopes = rest;
  } else {
    prefs.identity = {};
  }
  writeMirror();
  if (Object.keys(nulls).length) queue(scope, nulls);
}

/** Mirror first so the page paints with the last known choice, then the server
 *  doc replaces it WHOLE when the visitor is linked. A failed read keeps the mirror
 *  for painting but leaves writes closed (status stays off 'ready'). */
export async function loadPrefs() {
  const m = readMirror();
  prefs.identity = m.identity;
  prefs.scopes = m.scopes;
  if (auth.status !== 'authed') {
    prefs.status = 'anon';
    return;
  }
  prefs.status = 'loading';
  try {
    const r = await api.settings.prefs.get();
    if (r?.ok) {
      prefs.identity = isObj(r.identity) ? r.identity : {};
      prefs.scopes = isObj(r.scopes) ? r.scopes : {};
      prefs.updated_utc = r.updated_utc ?? null;
      writeMirror();
      prefs.status = 'ready';
      return;
    }
  } catch (e) {}
  prefs.status = 'anon';
}
