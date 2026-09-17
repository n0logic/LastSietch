// Thin client for the existing FastAPI JSON API. Same-origin in production
// (the app is served at the root of portal.lastsietch.com), proxied per-prefix
// in dev. credentials:'same-origin' so the ls_portal_session cookie (path=/)
// rides along for gated endpoints. No third-party libs (CSP script-src 'self').

/** GET a JSON endpoint. Throws on non-2xx (caller decides fallback). */
export async function getJSON(path, { signal } = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { accept: 'application/json' },
    signal,
  });
  if (!res.ok) {
    const err = new Error(`GET ${path} -> ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/** "Download my data": fetch the character export and trigger a browser save.
 *  A real export carries Content-Disposition: attachment; the dark/deferred and
 *  error responses are plain JSON with none, so we branch on that header.
 *  Returns {ok, filename} | {deferred, message} | {error, message}. */
export async function downloadMyData() {
  let res;
  try {
    res = await fetch('/portal/export/character', { credentials: 'same-origin' });
  } catch (e) {
    return { error: true, message: 'Network error, please try again.' };
  }
  const cd = res.headers.get('content-disposition') || '';
  if (!cd.includes('attachment')) {
    const j = await res.json().catch(() => ({}));
    if (j && j.status === 'deferred') return { deferred: true, message: j.message };
    return { error: true, message: (j && j.error) || `Export failed (HTTP ${res.status}).` };
  }
  const blob = await res.blob();
  const m = cd.match(/filename="([^"]+)"/);
  const fname = (m && m[1]) || 'lastsietch-character.json';
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fname;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return { ok: true, filename: fname };
}

// Portal CSRF: the backend sets ls_portal_csrf (JS-readable) alongside the
// httponly session cookie. Mutating endpoints (logout) require it echoed back
// as the X-Portal-CSRF-Token header. Names mirror portal_auth.py.
export const CSRF_COOKIE = 'ls_portal_csrf';
export const CSRF_HEADER = 'X-Portal-CSRF-Token';

/** Read a single document.cookie value (empty string when absent). */
export function readCookie(name) {
  const safe = name.replace(/[.$?*|{}()[\]\\/+^]/g, '\\$&');
  const m = document.cookie.match(new RegExp('(?:^|; )' + safe + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

/** RFC-4122 v4 uuid via the platform crypto (CSP-clean, no lib). Falls back to a
 *  getRandomValues build where randomUUID is unavailable. Callers mint once per
 *  intended write and reuse it across retries so the server can dedupe. */
export function uuidv4() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const b = new Uint8Array(16);
  c.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, '0'));
  return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`;
}

/** POST a CSRF-gated portal endpoint. redirect:'manual' so the 302 these
 *  endpoints return is not auto-followed (we just want its Set-Cookie applied).
 *  Returns the (possibly opaque) Response; callers update their own state. */
export async function postCsrf(path) {
  return fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    redirect: 'manual',
    headers: { [CSRF_HEADER]: readCookie(CSRF_COOKIE) },
  });
}

/** Send a CSRF-gated JSON mutation (waypoint CRUD). Fail-closed: any non-2xx
 *  throws with the server's error/detail message when one is present, so
 *  optimistic UI callers roll back instead of silently diverging. */
export async function sendCsrfJSON(method, path, body, { keepalive = false } = {}) {
  const headers = { [CSRF_HEADER]: readCookie(CSRF_COOKIE) };
  if (body !== undefined) headers['content-type'] = 'application/json';
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    keepalive,
  });
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok || (data && data.ok === false)) {
    const err = new Error(
      (data && (data.error || data.detail)) || `${method} ${path} -> ${res.status}`
    );
    err.status = res.status;
    err.data = data; // full envelope ({error, message, ...}) for friendly-copy callers
    throw err;
  }
  return data;
}

/** Send a CSRF-gated form-encoded mutation (profile visibility). Same fail-closed
 *  contract as sendCsrfJSON, but the body is application/x-www-form-urlencoded to
 *  match the portal's form endpoints. Undefined/null fields are omitted. */
export async function sendCsrfForm(path, fields) {
  const body = new URLSearchParams();
  for (const [k, v] of Object.entries(fields || {})) {
    if (v !== undefined && v !== null) body.set(k, String(v));
  }
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      [CSRF_HEADER]: readCookie(CSRF_COOKIE),
      'content-type': 'application/x-www-form-urlencoded',
      accept: 'application/json',
    },
    body,
  });
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok || (data && data.ok === false)) {
    const err = new Error(
      (data && (data.error || data.detail)) || `POST ${path} -> ${res.status}`
    );
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/** POST /portal/rescue with JSON content negotiation (frozen contract with
 *  the backend: Accept: application/json + CSRF header, nothing else). Every
 *  outcome answers {ok, state, message, cooldown_remaining_s?} where `state`
 *  is the V1 audit-string enum ('ok' = success; offline/cooldown/
 *  no_destination/deep_desert_blocked/... on refusal) that RescueButton
 *  mirrors into honest gate copy. Refusals are semantic (ok:false + state),
 *  NOT throws; only network/shape failures throw. Never parse the V1 HTML
 *  fragment: that stays the htmx path for the live V1 page. */
export async function postRescue() {
  const res = await fetch('/portal/rescue', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      accept: 'application/json',
      [CSRF_HEADER]: readCookie(CSRF_COOKIE),
    },
  });
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!data || typeof data.state !== 'string') {
    const err = new Error(`POST /portal/rescue -> ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// --- typed-ish endpoint helpers (extend as surfaces are wired) -------------
// `api` is namespaces only: every call reads api.<domain>.<verb>, so a call site
// never has to work out whether a domain happens to be flat or nested. That guess
// is what left `api.solido.importFile` in the V2 solido page for months (e482525):
// a member that does not exist is `undefined`, so every Import press threw a
// TypeError into a generic catch and reported "Import failed".
//
// guard() closes that at runtime. An unknown STRING key throws a named error rather
// than answering undefined. Symbols, `then` and `toJSON` pass straight through: awaiting an
// object probes it for `then`, and the platform reads symbol keys when inspecting
// and serializing, so throwing on either would break ordinary Promise resolution.
function guard(prefix, obj) {
  return new Proxy(obj, {
    get(target, prop, receiver) {
      if (typeof prop === 'symbol' || prop === 'then' || prop === 'toJSON' || prop in target) {
        return Reflect.get(target, prop, receiver);
      }
      throw new Error(`api.${prefix}${String(prop)} does not exist`);
    },
  });
}

const NAMESPACES = {
  // --- Session identity + the character/account switchers. -----------------------
  session: {
    me: () => getJSON('/portal/me'),
    logout: () => postCsrf('/portal/logout'),
    // --- V2 multi-character switcher (session-gated). characters() lists every
    // non-Deleted character on the active account with {controller_id, char_name,
    // lvl, online, is_default, selected}; selectCharacter() sets a signed cookie
    // the write routes resolve against (the server re-validates ownership, so a
    // stale/forged selection fails safe to the default pick). Accounts with a
    // single character never see the switcher.
    characters: () => getJSON('/portal/characters'),
    selectCharacter: (controller_id) =>
      sendCsrfJSON('POST', '/portal/select-character', { controller_id }),
    // Multi-account: switch the active linked account. Server validates the
    // account_id against the caller's own active links before re-pinning the session.
    selectAccount: (account_id) =>
      sendCsrfJSON('POST', '/portal/select-account', { account_id }),
    // Multi-account: unlink one of the caller's own linked accounts (alt-aware —
    // names WHICH account, unlike the legacy V1 route that always dropped
    // whatever the session was pinned to). Ownership is re-proven server-side
    // against the session's discord_id; account_id here only says which one,
    // never asserts identity. Response {ok, unlinked, session: 'unchanged' |
    // 'switched' | 'cleared', account_id?}.
    unlinkAccount: (account_id) =>
      sendCsrfJSON('POST', '/portal/link/unlink-account', { account_id }),
  },

  // --- Public server state (no session needed). -----------------------------------
  server: {
    // Public server status, drives the header online/offline pill.
    duneStatus: () => getJSON('/api/dune/status'),
    overview: () => getJSON('/portal/server/overview'),
    announcement: () => getJSON('/portal/announcement'),
    // Sietch traffic + the live gameplay-settings board. Both are shaped from the
    // one BattleGroup Director read, both degrade to {available:false} rather than
    // refusing, and both are rate-limited server-side, so callers poll no faster
    // than the 60s cache behind them.
    traffic: () => getJSON('/api/dune/traffic'),
    rules: () => getJSON('/api/dune/rules'),
    // Which player-facing gates are open right now (wave 12). Public, cached 60s
    // server-side; the Help page hides entries for features that answer false.
    features: () => getJSON('/portal/features'),
  },

  // --- V2 Home composition (wave 8). ONE session-gated read that composes the
  // personal half of the dashboard from loaders that already exist: character,
  // rewards, guild + presence, mailbox, wallet, orders, caps. Linked session
  // required (401 for anon and for a connected Discord with no character, which
  // is why the store never sends it in those states). Every sub-object is
  // independently null when its loader fails, so a half-answer seals one panel
  // instead of blanking the page; a null is ABSENT and must never be read as 0.
  // Additive by construction: it composes existing reads and changes no other
  // response shape, so Classic V1 is untouched. -----------------------------
  home: {
    overview: () => getJSON('/portal/home/v2'),
    activity: () => getJSON('/portal/activity'),
  },

  // --- Maps: the board reads, the live position feeds, the personal waypoint layer
  // and the rescue write. -----------------------------------------------------------
  maps: {
    data: (key) => getJSON(`/portal/maps/${key}/data`),
    live: (key) => getJSON(`/portal/maps/${key}/live`),
    // dim scopes the count to ONE instance of a multi-world map. Omitted =
    // whole-map total, which reads identically on every instance card.
    players: (key, dim) => getJSON(`/portal/maps/${key}/players${dim != null ? `?dim=${dim}` : ''}`),
    // Live other-player + world-vehicle positions on Hagga (public, PII-safe:
    // coords + partition/type only). Same feed as the public lastsietch.com/dune
    // board; the map filters each to the selected sietch by partition.
    dunePositions: () => getJSON('/api/dune/positions'),
    duneVehicles: () => getJSON('/api/dune/vehicles'),
    // M2 personal layer (all session-gated; 401 = anon, callers degrade).
    waypoints: {
      list: (key) => getJSON(`/portal/maps/${key}/waypoints`),
      add: (key, wp) => sendCsrfJSON('POST', `/portal/maps/${key}/waypoints`, wp),
      // Rename: PATCH body is {note} ONLY (frozen contract; coords in body = 400).
      update: (key, id, note) => sendCsrfJSON('PATCH', `/portal/maps/${key}/waypoints/${id}`, { note }),
      delete: (key, id) => sendCsrfJSON('DELETE', `/portal/maps/${key}/waypoints/${id}`),
    },
    // JSON-negotiated rescue: {ok, state, message, cooldown_remaining_s?}.
    rescue: () => postRescue(),
  },

  // --- V2 Guilds (all session-gated; the player is resolved server-side, so no
  // id is ever sent. 401 = anon, 404 = not in a guild; callers seal/hide the
  // panel rather than fabricate a state). ------------------------------------
  guilds: {
    // Viewer guild context: which guild I'm in + can-I-edit + the current blurb to
    // prefill the editor. Absent/failed -> caller treats as "not in a guild".
    me: () => getJSON('/portal/guilds/me'),
    // Pending guild invites for the signed-in player.
    invites: () => getJSON('/portal/guilds/invites'),
    // Member census for a guild the viewer belongs to (members-only, server-gated).
    presence: (id) => getJSON(`/portal/guilds/${id}/presence`),
    // Guild op write (CSRF). Phase 1 op = edit_description; social layer adds member
    // ops (promote|demote|remove). actor + player derived server-side; the UI passes
    // target_player_controller_id (never account_id). Response {success, status, op,
    // guild_id, audit_id, fail_reason, message}. A semantic refusal (incl. a DARK op
    // returning status:'deferred') comes back as data (no throw); only transport
    // failures throw. Member ops ship DARK behind GUILD_WRITES_DARK: a 'deferred'
    // result means "not yet enabled", NOT an error.
    op: (body) => sendCsrfJSON('POST', '/portal/guilds/op', body),

    // --- V2 social layer (all session-gated; account resolved server-side, never
    // sent by the client). Reads are LIVE; writes that hit dark flags return
    // status:'deferred' and are surfaced as "not yet enabled". --------------------

    // Signal Board directory: live roster/faction merged with recruiting rows. The
    // caller's own guild rows carry per-member player_controller_id + role_id +
    // is_self ONLY when my_role_can_edit, so the UI can gate member ops.
    data: () => getJSON('/portal/guilds/data'),
    // Leader/Officer recruiting quick-toggle + structured filters for own guild.
    recruitingSet: (body) => sendCsrfJSON('POST', '/portal/guilds/recruiting', body),

    // Solo LFG Seeker Wall (amber beacon, admin.db). Self-post / self-edit / expiry.
    lfgList: () => getJSON('/portal/guilds/lfg'),
    lfgUpsert: (body) => sendCsrfJSON('POST', '/portal/guilds/lfg', body),
    lfgDelete: () => sendCsrfJSON('POST', '/portal/guilds/lfg/delete'),

    // Join-requests. Create is any linked player -> a guild; list is officer-gated.
    joinRequestCreate: (id, body) => sendCsrfJSON('POST', `/portal/guilds/${id}/join-request`, body),
    joinRequestList: (id) => getJSON(`/portal/guilds/${id}/join-requests`),
    // Officer Invite handoff: fires the DARK send_invite for a pending request. The
    // requester is resolved server-side from the join row. Envelope mirrors guilds.op
    // ({success, status, ...}); status:'deferred' while GUILD_WRITES_DARK -> surface
    // as "not yet enabled", never a success.
    joinRequestInvite: (guildId, requestId) =>
      sendCsrfJSON('POST', `/portal/guilds/${guildId}/join-requests/${requestId}/invite`),

    // Guild inbox config (Leader-only). {view_min_role, manage_min_role}.
    inboxConfigSet: (id, body) => sendCsrfJSON('POST', `/portal/guilds/${id}/inbox-config`, body),
  },

  // Mailbox (admin.db). Player inbox + guild inbox + freeform DMs + bell badge.
  messages: {
    list: () => getJSON('/portal/messages'),
    unreadCount: () => getJSON('/portal/messages/unread-count'),
    guildMessages: (id) => getJSON(`/portal/guilds/${id}/messages`),
    read: (id) => sendCsrfJSON('POST', `/portal/messages/${id}/read`),
    delete: (id) => sendCsrfJSON('POST', `/portal/messages/${id}/delete`),
    send: (body) => sendCsrfJSON('POST', '/portal/messages/send', body),
    // Solari gifting (rides the mailbox). DARK behind LASTSIETCH_GIFTS_ENABLED -> deferred.
    // The recipient is EXACTLY ONE of recipient_char_name or recipient_code. A
    // code is resolved inside the request and never travels further, so it rides
    // the BODY and never a path or a query string. Refusals carry the wave 7
    // tokens bad_code, code_unknown, lookup_throttled, linked_alt and
    // self_transfer alongside the existing ones.
    giftSend: (body) => sendCsrfJSON('POST', '/portal/gifts/send', body),
  },

  // --- V2 public player directory (admin.db metadata; neutral/amber, never live
  // game state). char_name is the only identifier ever handled; the server
  // resolves accounts. list() is public (empty q lists everyone listed).
  // profileGet/profileSet are the caller's OWN directory visibility (form-encoded
  // POST with CSRF). messages.send to a directory row can now 403 (not accepting) or
  // 429 (rate-limited); those come back as thrown errors with .status for callers.
  players: {
    list: (q) => getJSON(`/portal/players${q ? `?q=${encodeURIComponent(q)}` : ''}`),
    profileGet: () => getJSON('/portal/profile'),
    profileSet: ({ listed, blurb }) => sendCsrfForm('/portal/profile', { listed, blurb }),
  },

  // --- V2 Storage (all session-gated; owner_ctrl/account resolved server-side,
  // never sent by the client). Reads are LIVE + never gated. Writes are CSRF +
  // uuid-idempotent via sendCsrfJSON: any non-2xx OR {ok:false} throws so the
  // optimistic caller rolls back. Envelope shapes are frozen in section 8 of
  // STORAGE-MODULE-BUILD-CONTRACT-2026-07-07.md. ----------------------------
  storage: {
    // One-shot overview: online/offline gate + bank (solari + inv30 grid) + owned
    // containers + caps. The page boots off this; the rail/grid then lazy-fetch.
    overview: () => getJSON('/portal/storage/v2'),
    // Owned-container list (mirror-first, relay fallback server-side).
    containers: () => getJSON('/portal/containers/v2'),
    // Item grid for one container, keyed by SPARSE position_index.
    containerItems: (id) => getJSON(`/portal/containers/v2/${id}/items`),
    // Augmented-gear roll-up across every owned container + the bank, server-side.
    // Deliberately ONE call: the page used to fan out per container and hung when
    // only part of that fan-out completed.
    augmentedItems: () => getJSON('/portal/storage/v2/augmented'),
    // Cross-container item locator (which container holds a template).
    search: (q) => getJSON(`/portal/containers/v2/search?q=${encodeURIComponent(q)}`),

    // Bank currency writes. The V2 JSON endpoints live under /portal/storage/v2/*
    // (the bare /portal/storage/* paths are the V1 HTML/Form routes; do NOT touch
    // those). amount capped 100000 server-side (defense in depth); the UI validates
    // inline too. deposit mode = 'sweep' (all coins) | 'amount'.
    withdraw: (body) => sendCsrfJSON('POST', '/portal/storage/v2/withdraw', body),
    deposit: (body) => sendCsrfJSON('POST', '/portal/storage/v2/deposit', body),

    // Drag-drop MOVE (Tier 3, own-account only). Behind LASTSIETCH_STORAGE_MOVE_ENABLED:
    // flag off comes back {ok:false, error:'move_disabled'} at HTTP 200 (surfaced
    // honestly, no fake success). Offline-gated server-side; the UI mirrors the gate.
    move: (body) => sendCsrfJSON('POST', '/portal/storage/v2/move', body),

    // Narrow selected-character move lane. The client sends storage kinds, never
    // arbitrary inventory ids; the portal and writer both resolve Bank and Backpack.
    pawnMove: (body) => sendCsrfJSON('POST', '/portal/storage/v2/pawn-move', body),

    // Tier 5 cross-player transfer. Ships DARK: a {ok:true, status:'deferred'}
    // response MUST be rendered as "not yet enabled", NEVER as a success.
    // Recipient is EXACTLY ONE of recipient_char_name or recipient_code, the same
    // contract messages.giftSend takes: the code rides the BODY, never a URL.
    transfer: (body) => sendCsrfJSON('POST', '/portal/storage/v2/transfer', body),
    // Both-directions transfer history off the admin.db mirror, session-scoped.
    // Rows are {t, direction, item, grade, counterparty, status}; counterparty is
    // a character name and never an id, so nothing here is resolved client-side.
    transfers: ({ limit = 50 } = {}) =>
      getJSON(`/portal/storage/v2/transfers?limit=${limit}`),

    // Repair (V2 JSON dispatch over the existing V1 writer chain; offline-gated
    // server-side). box + gear share the portal_repair cooldown bucket; everything
    // is its own 24h bucket behind the REPAIR_ALL_ENABLED kill-switch (which returns
    // {ok:false, error:'disabled'} at HTTP 200 when off). box needs {inv_id}.
    repairBox: (body) => sendCsrfJSON('POST', '/portal/storage/v2/repair/box', body),
    repairGear: (body) => sendCsrfJSON('POST', '/portal/storage/v2/repair/gear', body),
    repairEverything: (body) => sendCsrfJSON('POST', '/portal/storage/v2/repair/everything', body),
    // Per-vehicle in-place refurbish: reverses max-durability decay on ONE owned
    // vehicle's mounted parts (no dismounting). Needs {inv_id} = selected vehicle's
    // container id. Own cooldown bucket behind the REPAIR_VEHICLE_ENABLED kill-switch.
    repairVehicle: (body) => sendCsrfJSON('POST', '/portal/storage/v2/repair/vehicle', body),
    // Installed-parts durability list for a selected vehicle (read-only).
    vehicleParts: (containerId) => getJSON(`/portal/storage/v2/vehicle/${containerId}/parts`),
  },

  // --- V2 Ingot Refinery (session-gated; owner_ctrl/account/inventory ids all
  // resolved server-side, never sent by the client). catalog() is the read: rate
  // table, per-tier weekly allowance, and holdings summed across bank + backpack
  // + toolbar SERVER-SIDE (the toolbar never appears in the container browser, and
  // a client-side fan-out per source is what hung the grid on 2026-08-02).
  // exchange() is CSRF + uuid-idempotent via sendCsrfJSON.
  //
  // SHIPS DARK behind LASTSIETCH_REFINERY_ENABLED plus the host's /etc/lastsietch/refinery-enabled
  // flag file. Either being off answers {ok:false, error:'refinery_disabled'} at
  // HTTP 200 (the storage family's dark form), which sendCsrfJSON turns into a
  // throw carrying the envelope on err.data. That MUST render as the quiet
  // "not open yet" state -- never a toast, and never a success. Same for
  // 'recipe_disabled', the per-tier switch inside the rate table.
  //
  // The exchange body ECHOES the whole rate the page was shown
  // (expected_input_template / expected_spice_template / the three per-batch
  // counts); a rate retuned while the page sat open comes back 'rate_changed'
  // with nothing taken.
  refinery: {
    catalog: () => getJSON('/portal/refinery/v2/catalog'),
    exchange: (body) => sendCsrfJSON('POST', '/portal/refinery/v2/exchange', body),
  },

  // --- V2 Exchange / CHOAM market (all session-gated; buyer/seller controller +
  // account resolved server-side, never sent by the client). Reads are LIVE + never
  // gated; writes are CSRF + uuid-idempotent via sendCsrfJSON (mint the uuid ONCE
  // per logical write and reuse it across retries so the server can dedupe). BUY is
  // ONLINE-SAFE; SELL is OFFLINE-gated server-side. Envelope shapes are frozen in
  // EXCHANGE-MODULE-BUILD-CONTRACT-2026-07-13.md. --------------------------------
  market: {
    // Overview: {bank_solari, watches[], alert_count, tabs, csrf}. The page boots
    // off this, then the browse list / flip board / my-orders lazy-fetch.
    overview: () => getJSON('/portal/market/v2'),
    // Unseen fired-alert count for the topbar badge -> {ok, alert_count}. admin.db
    // only: the full overview above resolves the buyer controller + bank through
    // the relay against the live game DB, which is far too expensive to pay on
    // every page load just to paint a badge.
    alertsCount: () => getJSON('/portal/market/v2/alerts/count'),
    // Bot budget tracker -> {available, updated_at, window_days, scopes[]}.
    botLimits: () => getJSON('/portal/market/v2/bot-limits'),
    // Browse search -> {rows[], page, more}. Empty filters list everything (page 0).
    search: ({ q = '', category = '', kind = '', sort = '', page = 0 } = {}) => {
      const p = new URLSearchParams();
      if (q) p.set('q', q);
      if (category) p.set('category', category);
      if (kind) p.set('kind', kind);
      if (sort) p.set('sort', sort);
      if (page) p.set('page', String(page));
      const qs = p.toString();
      return getJSON(`/portal/market/v2/search${qs ? `?${qs}` : ''}`);
    },
    // One template's price ladder + bot buy tiers + the caller's bank.
    item: (tpl) => getJSON(`/portal/market/v2/item?tpl=${encodeURIComponent(tpl)}`),
    // Price history for the sparkline -> {points[], low_7d, calibrating?}.
    history: (tpl) => getJSON(`/portal/market/v2/history?tpl=${encodeURIComponent(tpl)}`),
    // My listings -> {active[], completed[], history[]}. NO expires_at exists, so
    // active rows carry no countdown (cancel/relist only).
    myOrders: () => getJSON('/portal/market/v2/my-orders'),
    // Bot-floor flip board -> {rows[]} (cheapest player ask < bot cap).
    flips: () => getJSON('/portal/market/v2/flips'),

    // Buy off a ladder rung. Body {order_id, revision, count, uuid} -> {ok, bank_after, delivered}.
    buy: (body) => sendCsrfJSON('POST', '/portal/market/v2/buy', body),
    // List an item (replaces the SellDialog's V1 HTML-token path). Body
    // {container_id, item_id, count, price, duration_days, tpl, uuid} -> {ok, fee, bank_after, error, message}.
    sell: (body) => sendCsrfJSON('POST', '/portal/market/v2/sell', body),
    // Mark the NAMED fired alerts seen -> {ok, cleared, alert_count}. Send only the
    // ids the feed actually rendered: an unbounded clear would mark alerts seen
    // that were never on the page. alert_count is the FRESH unseen count, not 0.
    alertsSeen: (ids) => sendCsrfJSON('POST', '/portal/market/v2/alerts/seen', { ids }),
    // Cancel / relist my own listing (online-safe). uuid-idempotent.
    cancelOrder: (body) => sendCsrfJSON('POST', '/portal/market/v2/orders/cancel', body),
    relistOrder: (body) => sendCsrfJSON('POST', '/portal/market/v2/orders/relist', body),

    // Market watch / price alert (NOT offline-gated; safe online). Form-encoded V1
    // routes that answer JSON when Accept: application/json. Body {template_id, max_price}.
    watchlist: () => getJSON('/portal/watchlist'),
    watchAdd: (fields) => sendCsrfForm('/portal/watchlist/add', fields),
    watchRemove: (fields) => sendCsrfForm('/portal/watchlist/remove', fields),
    // List on Exchange (RIGHT panel, tradeable-only). V1 sell path: form-encoded,
    // offline-gated, funds the fee from the bank. Body {container_id, item_id, count,
    // price, duration_days, tpl}. Non-2xx throws (fail-closed). Retained for any V1
    // caller; the SellDialog now posts market.sell (JSON) instead.
    listOnExchange: (fields) => sendCsrfForm('/portal/market/sell', fields),
  },

  // ---- The Karum: player-to-player trade venue --------------------------------
  // Fixed-ask listings. Browsing is public; listing, buying and cancelling need a
  // linked session. Ships DARK behind LASTSIETCH_KARUM_ENABLED: a {ok:true,
  // status:'deferred'} MUST render as "not open yet", NEVER as a success.
  //
  // Every write is uuid-idempotent. Mint the uuid ONCE per intended action and reuse
  // it across retries: the game DB dedupes on it, so a resend after a lost response
  // replays instead of double-charging. A fresh uuid per click would defeat that.
  karum: {
    // Overview: the live board, your listings, your purchases, banked Solari, your
    // advisory online state, caps, flags, csrf. Session-gated.
    overview: () => getJSON('/portal/karum'),
    // Public paged browse. sort = new | old | cheap | dear.
    // grade (wanted board only) is the grade the BROWSER HOLDS: it returns the orders
    // that grade can fill, which includes any-grade rows and `min` rows at or below it.
    search: ({ q = '', template = '', sort = 'new', side = 'sell', page = 1,
               grade = null } = {}) => {
      const p = new URLSearchParams();
      if (q) p.set('q', q);
      if (template) p.set('template', template);
      if (sort) p.set('sort', sort);
      if (side !== 'sell') p.set('side', side);
      // Base is 0 and is a real filter; `if (grade)` would drop it.
      if (grade != null) p.set('grade', String(grade));
      if (page > 1) p.set('page', String(page));
      const qs = p.toString();
      return getJSON(`/portal/karum/search${qs ? `?${qs}` : ''}`);
    },
    // One listing (public).
    detail: (id) => getJSON(`/portal/karum/listing/${encodeURIComponent(id)}`),
    // The caller's own bank items that could be listed: tradeable, not already live.
    sellable: () => getJSON('/portal/karum/sellable'),
    // Tradeable item templates for the shared searchable posting picker.
    catalog: () => getJSON('/portal/karum/catalog'),

    // LIST is the only OFFLINE-GATED leg in the feature: the item has to leave your
    // bank, and taking from a loaded session is not durable. A `player_online`
    // refusal is routine, not an error.
    list: (body) => sendCsrfJSON('POST', '/portal/karum/list', body),
    // BUY answers with `paid` and `delivered` separately, and a 202 means "in
    // flight, do not retry blindly": `reconciling` (we do not yet know if payment
    // committed) or `paid_undelivered` (it did, the goods are coming).
    buy: (body) => sendCsrfJSON('POST', '/portal/karum/buy', body),
    cancel: (body) => sendCsrfJSON('POST', '/portal/karum/cancel', body),
    requestPost: (body) => sendCsrfJSON('POST', '/portal/karum/request/post', body),
    requestFill: (body) => sendCsrfJSON('POST', '/portal/karum/request/fill', body),
    requestCancel: (body) => sendCsrfJSON('POST', '/portal/karum/request/cancel', body),
  },

  // --- V2 Bases / Solido (the copy-device blueprint + community market domain;
  // NOT the world sub-fief card). Scope = all-linked accounts (V1 parity). Reads
  // are public/session as noted; writes are CSRF + uuid-idempotent via
  // sendCsrfJSON (mint the uuid ONCE per logical write, reuse across retries).
  // The V1 HTML routes stay intact; these are JSON siblings under /v2. Envelope
  // {ok, error?, ...}. rename is OFFLINE-gated server-side; publish re-exports the
  // blueprint server-side (never trusts a client blob). ---------------------------
  bases: {
    // Overview: linked chars + per-char blueprint sections (bp_id/name/piece_count
    // + merged published state) + caps + flags + csrf. Session-gated.
    overview: () => getJSON('/portal/bases/v2'),
    // Community market gallery -> {listings[], has_more}. Public, read-only.
    market: ({ sort = 'new', tag = '', page = 0 } = {}) => {
      const p = new URLSearchParams();
      if (sort) p.set('sort', sort);
      if (tag) p.set('tag', tag);
      if (page) p.set('page', String(page));
      const qs = p.toString();
      return getJSON(`/portal/solido/v2/market${qs ? `?${qs}` : ''}`);
    },
    // One listing's detail card (public).
    detail: (id) => getJSON(`/portal/solido/v2/${encodeURIComponent(id)}`),
    // The blueprint blob the 3D viewer consumes for a MARKET listing (public, keyed
    // by publish_id). RAW blueprint JSON, not enveloped ({available:false} only when
    // the blob is gone); otherwise the Solido-format {instances[], placeables[]}.
    blueprint: (id) => getJSON(`/portal/solido/v2/${encodeURIComponent(id)}/blueprint`),
    // The blueprint blob for the caller's OWN base (keyed by in-game bp_id, which
    // an unpublished base has no publish_id for). Reuses the existing V1 export
    // endpoint (ownership-gated server-side), which returns the raw blueprint JSON.
    ownBlueprint: (bpId) => getJSON(`/portal/my-bases/${encodeURIComponent(bpId)}/export`),
    // The download HREF for a market listing, not a fetch: a plain <a download>
    // lets the browser own the save, so the server's Content-Disposition filename
    // survives and the blob is never buffered through JS. Returns a string.
    download: (id) => `/portal/solido/v2/${encodeURIComponent(id)}/download`,
    // The closed filter vocabulary behind the gallery chip rail (public):
    // {tags[], size_bands[], purposes[]}. Server-owned, so the rail can never
    // offer a filter the market cannot answer.
    tags: () => getJSON('/portal/solido/v2/tags'),
    // The caller's own recent imports, newest first (session-scoped, max 20):
    // {rows:[{at, delivery, source_type, result, message}]}.
    imports: () => getJSON('/portal/bases/v2/imports'),

    // Writes (CSRF + client_uuid idempotency). The backend accepts the CSRF token
    // as the X-CSRF-Token header OR a body csrf_token; sendCsrfJSON sets the portal
    // header, and we ALSO echo csrf_token in the body (belt-and-suspenders, matches
    // the rewards module) so either accepted path is satisfied. client_uuid is the
    // idempotency key — callers mint it ONCE per logical write and reuse on retry.
    // publish body: {game_bp_id, title, description, tags[], client_uuid, csrf}.
    // The title/description/tags are the player's; the blueprint blob is NOT sent
    // and never can be, because the server re-exports it from the live base.
    // Re-publishing an owned listing updates it in place (same endpoint, same body).
    publish: (body) => sendCsrfJSON('POST', '/portal/bases/v2/publish', { ...body, csrf_token: readCookie(CSRF_COOKIE) }),
    unpublish: (body) => sendCsrfJSON('POST', '/portal/bases/v2/unpublish', { ...body, csrf_token: readCookie(CSRF_COOKIE) }),
    // Publish-time gallery thumbnail: the PNG the client renders offscreen right
    // after a publish. Existing V1 endpoint (keyed by publish_id, owner-checked,
    // PNG-magic + size validated server-side); it reads CSRF from the header, and
    // csrf_token rides in the body for parity with the sibling writes. The caller
    // is identified by the session — never by a client-supplied account_id.
    thumbnail: (publishId, body) =>
      sendCsrfJSON('POST', `/portal/solido/${encodeURIComponent(publishId)}/thumbnail`,
        { ...body, csrf_token: readCookie(CSRF_COOKIE) }),
    // Rename is OFFLINE-gated server-side; a 409 player_online refusal comes back
    // as a thrown error the store surfaces as a warn (not a hard failure).
    rename: (body) => sendCsrfJSON('POST', '/portal/bases/v2/rename', { ...body, csrf_token: readCookie(CSRF_COOKIE) }),
    // The ONE import lane (wave 6 ruling 9.5): a public link, a listing already on
    // our market, or a pasted blueprint_text, delivered to a linked character's
    // CHOAM bank or offline-gated backpack. Body carries source_type plus the one
    // matching field: link | publish_id | blueprint_text. The V1 HTML route stays
    // live for Classic; nothing in V2 posts it any more. client_uuid is the
    // idempotency key.
    import: (body) => sendCsrfJSON('POST', '/portal/bases/v2/import', { ...body, csrf_token: readCookie(CSRF_COOKIE) }),
  },

  // --- V2 Character (session-gated; the selected controller is resolved
  // server-side from the selection cookie, so no id is ever sent). The overview
  // read is LIVE + never gated. Shape is frozen in section MODULE 1 of
  // V2-MODULE-PORTS-BUILD-CONTRACT-2026-07-15.md: {character, vitals, faction_rep,
  // specializations, journey, landsraad_teaser, equipped, csrf_token,
  // character_name}. RAW ints (UI formats); icon SLUGS only. Sections that could
  // not resolve for a non-default selected char carry ctrl_scoped:false so the UI
  // shows a subtle "reflects last logout" note. --------------------------------
  character: {
    overview: () => getJSON('/portal/character/v2'),
  },

  // --- V2 Landsraad. overview() is session-gated, READ-ONLY and account-scoped
  // (the switcher does NOT re-key it). No writes, no CSRF. Shape frozen in MODULE 2
  // of the contract: {board, rewards, active_character_name, rewards_error}. `board`
  // is the term-global great-house board (tiles + rails + term countdown anchor);
  // `rewards` is the player's own pending house rewards. Either half may be null
  // and each renders independently. Placeable-swatch reward rows carry an optional
  // .swatch:{chips:[hex], exact:false} the backend attaches inline (no 2nd fetch).
  // standings() is the older PUBLIC feed, left untouched; it is a sibling, not a
  // replacement. -------------------------------------------------------------------
  landsraad: {
    overview: () => getJSON('/portal/landsraad/v2'),
    standings: () => getJSON('/portal/landsraad/standings'),
  },

  // --- V2 Login Rewards (session-gated; account resolved server-side, never sent
  // by the client). The overview read is LIVE + never gated. Claim is CSRF-gated:
  // the caller sends only { reward_kind } ("daily_solari" | "weekly_item"); the
  // server derives a deterministic uuid5 idempotency key, so no client uuid is
  // sent. The csrf_token is echoed in the body (backend requires it there in
  // addition to the header). The system ships DARK behind LASTSIETCH_REWARD_ENABLED: a
  // {ok:true, status:'deferred'} response MUST render as "not yet enabled", NEVER
  // as a success. status 'applied' AND 'replay' are both success. Any non-2xx OR
  // {ok:false, error} throws so the optimistic caller rolls back. -----------------
  rewards: {
    overview: () => getJSON('/portal/rewards/overview'),
    claim: (body) =>
      sendCsrfJSON('POST', '/portal/rewards/claim', { ...body, csrf_token: readCookie(CSRF_COOKIE) }),
  },

  // --- V2 Settings (session-gated; the identity is resolved server-side from
  // the session, so no id is ever sent). activity() is the player's OWN audit
  // trail, {items:[{t, kind, summary}]}; transfer rows carry item, grade,
  // counterparty and direction on top of that.
  //
  // Identity codes are the 8-symbol handle from the 32-symbol alphabet (no
  // 0/1/I/O): mine() mints on first read, rotate() invalidates the previous
  // code, lookup() answers {found, display_name, daily_remaining,
  // pair_remaining} and NEVER an id. A code is a
  // payload, never a URL: it is not rendered as a link, not accepted as one,
  // and the QR carries the eight characters and nothing else. -----------------
  settings: {
    activity: (limit = 50) => getJSON(`/portal/settings/activity?limit=${limit}`),
    // Wave 10b account preferences. get() returns every scope the identity owns;
    // set() shallow-merges one scope (a null value deletes that key). Keys are
    // whitelisted server-side; see prefs.svelte.js for the client contract.
    prefs: {
      get: () => getJSON('/portal/settings/prefs'),
      set: (scope, prefs, opts) => sendCsrfJSON('PUT', '/portal/settings/prefs', { scope, prefs }, opts),
    },
    codes: {
      mine: () => getJSON('/portal/settings/codes/mine'),
      rotate: () => sendCsrfJSON('POST', '/portal/settings/codes/rotate'),
      lookup: (code) =>
        getJSON(`/portal/settings/codes/lookup?code=${encodeURIComponent(code)}`),
    },
  },

  // --- V2 Chat (wave 11). Everything needs a linked session; the live stream is
  // an EventSource opened by chat.svelte.js (not a fetch), so it is not listed
  // here. Channel ids are opaque strings from channels(); send() carries a
  // client uuid so a retry after a dropped response never posts twice. -------
  chat: {
    channels: () => getJSON('/portal/chat/channels'),
    messages: (channel, { before = null, limit = 50 } = {}) =>
      getJSON(`/portal/chat/${encodeURIComponent(channel)}/messages?limit=${limit}${before ? `&before=${encodeURIComponent(before)}` : ''}`),
    send: (channel, body, clientKey) =>
      sendCsrfJSON('POST', `/portal/chat/${encodeURIComponent(channel)}/send`, { body, client_key: clientKey }),
    read: (channel, lastId) =>
      sendCsrfJSON('POST', `/portal/chat/${encodeURIComponent(channel)}/read`, { last_id: lastId }),
    report: (channel, id, reason) =>
      sendCsrfJSON('POST', `/portal/chat/${encodeURIComponent(channel)}/messages/${encodeURIComponent(id)}/report`, { reason }),
    remove: (channel, id) =>
      sendCsrfJSON('POST', `/portal/chat/${encodeURIComponent(channel)}/messages/${encodeURIComponent(id)}/delete`),
    mute: (payload) => sendCsrfJSON('POST', '/portal/chat/mute', payload),
    unmute: (muteId) => sendCsrfJSON('POST', '/portal/chat/unmute', { mute_id: muteId }),
    moderation: {
      reports: (state = 'open') => getJSON(`/portal/chat/moderation/reports?state=${encodeURIComponent(state)}`),
      mutes: () => getJSON('/portal/chat/moderation/mutes'),
      resolve: (reportId, payload) =>
        sendCsrfJSON('POST', `/portal/chat/moderation/reports/${encodeURIComponent(reportId)}/resolve`, payload),
    },
  },

  // --- V2 Events (wave 9). The list and the detail are PUBLIC: an anonymous
  // visitor reads the whole board, and only the reminder is gated. Both are
  // served from a 60s server cache, so a cancellation can read up to a minute
  // stale here; the mailbox notice the backend sends every reminded player is
  // the immediate half of that pair, and the page is the slow half.
  //
  // `mine` is the linked player's own reminder ids, private and no-store. It is
  // never sent for an anonymous or unlinked viewer: that read answers 401 for
  // them, and a 401 in the network log every two minutes is noise, not a signal.
  // A FAILED read is null, never an empty list: "we could not read your
  // reminders" and "you have no reminders" are different sentences, and only one
  // of them is ours to say.
  //
  // The admin verbs are role-gated SERVER-side and answer 404 to everyone else.
  // The UI gate only hides controls that would 404; the browser grants nothing.
  // create/update/cancel are the composer's three writes, and cancel carries the
  // typed CANCEL token as `confirm` because a cancellation mails every reminded
  // player and cannot be taken back.
  events: {
    list: (window) => getJSON(`/portal/events?window=${encodeURIComponent(window)}&limit=50`),
    detail: (id) => getJSON(`/portal/events/${encodeURIComponent(id)}`),
    mine: () => getJSON('/portal/events/mine'),
    remind: (id) => sendCsrfJSON('POST', `/portal/events/${encodeURIComponent(id)}/remind`),
    unremind: (id) => sendCsrfJSON('POST', `/portal/events/${encodeURIComponent(id)}/unremind`),
    adminList: () => getJSON('/portal/events/admin/list'),
    adminCreate: (body) => sendCsrfJSON('POST', '/portal/events/admin/create', body),
    adminUpdate: (id, body) =>
      sendCsrfJSON('POST', `/portal/events/admin/${encodeURIComponent(id)}/update`, body),
    adminCancel: (id, confirm, reason) =>
      sendCsrfJSON('POST', `/portal/events/admin/${encodeURIComponent(id)}/cancel`,
        { confirm, reason }),
  },

  // --- Deliveries (wave 12). The packages the server sent this account: the
  // Welcome Package and any Return Package, with where each part landed. One
  // session-gated read for the active account; the Home card reads the summary
  // off /portal/home/v2 instead.
  deliveries: {
    list: () => getJSON('/portal/deliveries/v2'),
  },
};

// The nested groups are guarded first so the assignment still lands on a plain
// object, then each namespace, then the root.
NAMESPACES.maps.waypoints = guard('maps.waypoints.', NAMESPACES.maps.waypoints);
NAMESPACES.settings.codes = guard('settings.codes.', NAMESPACES.settings.codes);
NAMESPACES.settings.prefs = guard('settings.prefs.', NAMESPACES.settings.prefs);
NAMESPACES.chat.moderation = guard('chat.moderation.', NAMESPACES.chat.moderation);
for (const name of Object.keys(NAMESPACES)) {
  NAMESPACES[name] = guard(`${name}.`, NAMESPACES[name]);
}

export const api = guard('', NAMESPACES);
