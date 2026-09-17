"""Portal OAuth router (P4 — Discord identity surface).

Routes (all under /portal/, public unless noted):
- GET  /portal/             — landing (anonymous) OR 302 to /portal/account if linked
- GET  /portal/login        — issue state_token, 302 to Discord authorize
- GET  /portal/oauth/callback — verify state, exchange code, fetch identity, route
- GET  /portal/account      — logged-in dashboard (session-cookie required)
- GET  /portal/me           — JSON identity introspection (session-cookie required)
- POST /portal/logout       — clear cookies (session-cookie + CSRF required)

Cookie scoping: every portal cookie has path=/ since wave 13a (the SPA pages on
portal.lastsietch.com live at the root and read ls_portal_csrf out of
document.cookie). Admin's path=/admin cookies are still never sent here.

Hard safety:
- Zero direct dune.* SQL. All live-state reads route through routers/dune.py
  cached helpers in portal_quiz.py.
- client_secret + state_token + session cookie values are NEVER logged.
- Pre-quiz surfaces (/portal/, /portal/login, /portal/oauth/callback) refuse to
  leak account_id, character_name, container_id, or any internal identifier.
"""
import portal_identity
import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import config
from http_body import read_body as _read_body
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from config import (
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    DISCORD_OAUTH_REDIRECT_URI,
    PORTAL_DISCORD_INVITE_URL,
    PORTAL_OAUTH_HTTP_TIMEOUT,
    PORTAL_SESSION_IDLE_MAX_AGE,
)
import mirror
import rewards
import market_watch
import market_categories
import market_history
import portal_gift_limits
import spice_fields
import map_model
from database import get_db
from portal_host import oauth_redirect_uri, safe_return_to, v2_home
from portal_auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    LINK_FLOW_COOKIE,
    SELCHAR_COOKIE,
    SESSION_COOKIE,
    clear_cookie,
    client_ip,
    csrf_for_link_flow,
    csrf_for_session,
    get_portal_session,
    is_test_run,
    issue_link_flow,
    issue_selchar_cookie,
    issue_session_cookie,
    issue_switched_session_cookie,
    issue_state_token,
    set_cookie,
    validate_csrf,
    verify_selchar_cookie,
    verify_state_token_split,
)
from portal_rate_limit import (
    check_oauth_callback_ip,
    check_oauth_start_ip,
)

logger = logging.getLogger("portal")

# 3D gear viewer demo section: visible when PORTAL_SHOW_GEAR_DEMO=1 (dev only).
import os as _os
_SHOW_GEAR_DEMO = _os.environ.get("PORTAL_SHOW_GEAR_DEMO", "0") == "1"

# Item-reward template_ids that are not the Solari currency get a friendly
# label via the shared name_lookups sidecar (pak -> curated -> camelCase
# synthesis). Solari (SolarisCoin) is handled separately as the currency total.
from name_lookups import ITEMS as _ITEM_NAMES
# Real in-game item icons (extracted client paks); falls back to an unknown
# glyph when a template_id has no mapped icon.
from item_icons import icon_for as _icon_for
# Durable-item classification (weapons/gear/tools/vehicle parts) so never-used
# durable items with empty per-instance durability still show a full bar.
from item_durable import is_durable as _is_durable
# Real game house/faction crests (extracted T_UI_IconsLandsraadFactionHouse*);
# None when unmapped so the board falls back to the monogram.
from house_crests import crest_for as _house_crest, faction_crest_for as _faction_crest
from stat_icons import icon_for as _stat_icon
from container_icons import icon_for as _container_icon
# Per-house in-game rep location (shared source of truth with the admin grant UI).
from data.house_reps import HOUSE_REP_LOCATIONS

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# _read_body (JSON-or-form body reader) lives in the fastapi-free http_body
# module so it is unit-testable without the web stack; imported at top.


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- PWA: installable web app (manifest + service worker) --------------------
# Both are served from under /portal/ so the service worker's scope covers the
# whole portal (a SW can only control pages at or below its own path). The portal
# is reverse-proxied at /portal/* to this backend, so these land in-scope.
_PWA_MANIFEST = {
    "name": "Last Sietch Dune Portal",
    "short_name": "Last Sietch",
    "description": "Live Deep Desert spice + sandworm tracker, market, storage and "
                   "character tools for the Last Sietch Dune server.",
    "start_url": "/portal/account",
    "scope": "/portal/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#0c0a07",
    "theme_color": "#0c0a07",
    "icons": [
        {"src": "/assets/icon.png", "sizes": "256x256", "type": "image/png",
         "purpose": "any"},
        {"src": "/assets/icon.png", "sizes": "256x256", "type": "image/png",
         "purpose": "maskable"},
        {"src": "/assets/favicon.png", "sizes": "32x32", "type": "image/png"},
    ],
}

# Stale-while-revalidate for the static shell (CSS/JS/brand), network-first for
# everything dynamic (the live map/market/spice data must stay fresh). SWR means a
# PWA user gets the cached shell instantly (snappy warm load) BUT the SW always
# refetches in the background and updates the cache, so the NEXT load picks up any
# changed CSS/JS automatically -- no more "old CSS stuck in cache" for installed
# users, even between CACHE bumps. Still bump CACHE when you want to force an
# immediate purge of every shell asset on next activate. Deliberately minimal: this
# makes the portal installable + snappy, NOT a full offline app.
_PWA_SW = """\
const CACHE = 'ls-portal-v68';
const SHELL = [
  '/admin/static/css/portal/portal.css',
  '/admin/static/css/design-system.css',
  '/admin/static/js/portal.js',
  '/admin/static/js/portal-cinematic.js',
  '/assets/icon.png',
];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) =>
    Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  // Static shell: stale-while-revalidate. Serve cache immediately when present,
  // but always refetch in the background and update the cache so the next load is
  // fresh. New/changed CSS/JS self-heals within one visit.
  const isShell = url.pathname.startsWith('/admin/static/') ||
                  url.pathname.startsWith('/assets/');
  if (isShell) {
    e.respondWith(caches.open(CACHE).then((c) => c.match(req).then((hit) => {
      const net = fetch(req).then((res) => {
        if (res && res.status === 200) c.put(req, res.clone());
        return res;
      }).catch(() => hit);
      return hit || net;
    })));
    return;
  }
  // Everything else (live data, pages): network-first, fall back to cache offline.
  e.respondWith(fetch(req).catch(() => caches.match(req)));
});
"""


@router.get("/portal/manifest.webmanifest")
async def portal_manifest():
    return JSONResponse(_PWA_MANIFEST,
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/portal/sw.js")
async def portal_service_worker():
    # Service-Worker-Allowed lets the SW claim the /portal/ scope even though it
    # is served from /portal/sw.js (default scope would be the file's own dir).
    return Response(_PWA_SW, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/portal/",
                             "Cache-Control": "no-cache"})


def _config_ok() -> bool:
    """Portal routes 503 if any of the 4 required env vars are missing."""
    from config import PORTAL_OAUTH_STATE_SECRET, PORTAL_SESSION_SECRET
    return bool(
        DISCORD_CLIENT_ID
        and DISCORD_CLIENT_SECRET
        and DISCORD_OAUTH_REDIRECT_URI
        and PORTAL_OAUTH_STATE_SECRET
        and PORTAL_SESSION_SECRET
    )


def _active_link_for_discord(discord_id: str) -> Optional[dict]:
    """Returns the first active link row for this discord_id, or None.
    Active = revoked_at IS NULL. Multi-link support uses the first row as
    'active'; the session cookie can later pin a different one."""
    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT id, account_id, character_name, discord_handle, linked_at
                 FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL
                ORDER BY linked_at ASC
                LIMIT 1""").replace("ls_account_links", portal_identity.link_table(conn)),
            (discord_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _linked_characters(discord_id: str) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            ("""SELECT account_id, character_name, linked_at
                 FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL
                ORDER BY linked_at ASC""").replace("ls_account_links", portal_identity.link_table(conn)),
            (discord_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _touch_last_session(account_id: int) -> None:
    """Best-effort last_session_at update. Errors swallowed so a tx hiccup
    never 5xx's a page render."""
    try:
        conn = get_db()
        try:
            conn.execute(
                """UPDATE ls_account_links
                      SET last_session_at = datetime('now')
                    WHERE account_id = ? AND revoked_at IS NULL""",
                (account_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("portal: last_session_at update failed: %s", exc)


def _render_error(request: Request, template: str, ctx: dict, status: int = 200) -> Response:
    """Render an /portal/error_*.html template with consistent shape."""
    return templates.TemplateResponse(request, f"portal/{template}", ctx, status_code=status)


def _render_link_revoked(request: Request) -> Response:
    """Render the 'Account link revoked' page AND clear the stale session
    cookies. The session cookie outlives an operator-revoke, so without this
    the 'Verify a character' button (-> /portal/login) would re-auth, find no
    active link, and the still-valid session would bounce the user straight
    back to the revoked page — an apparent 'button just reloads' loop. Clearing
    the session breaks the loop; back_url -> /portal/login drops the (already
    Discord-authed) user back into the verify flow."""
    resp = _render_error(
        request,
        "error_link_revoked.html",
        {"title": "Account link revoked",
         "message": "Your account link has been revoked.",
         "reason": "operator-revoked",
         "back_url": "/portal/login"},
    )
    clear_cookie(resp, SESSION_COOKIE)
    clear_cookie(resp, CSRF_COOKIE)
    return resp


def _attach_session_cookies(response: Response, session_value: str) -> None:
    """Set ls_portal_session (httponly) + ls_portal_csrf (JS-readable)."""
    set_cookie(response, SESSION_COOKIE, session_value,
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=True)
    set_cookie(response, CSRF_COOKIE, csrf_for_session(session_value),
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=False)


def base_ctx(request: Request, *, discord_handle: Optional[str] = None) -> dict:
    """Common template context: portal_session dict (or None) for base.html
    header + portal_csrf_token for hidden form fields. CSRF is derived from
    whichever cookie governs the current surface (session or link-flow). If
    neither is present, returns empty strings so the template can no-op."""
    session = get_portal_session(request)
    if session is not None:
        session_cookie = request.cookies.get(SESSION_COOKIE, "")
        token = csrf_for_session(session_cookie) if session_cookie else ""
        # If the caller has a fresher handle (e.g. just fetched from DB),
        # prefer it; otherwise derive from session-bound DB lookup is left to
        # the caller. Default to whatever discord_handle was passed.
        aid = int(session.get("aid") or 0)
        if not discord_handle:
            # Routes that don't fetch the link row themselves (maps, spice, …)
            # still need the rail account chip populated.
            link = _active_link_for_discord(str(portal_identity.actor_key(session)))
            discord_handle = (link or {}).get("discord_handle") or ""
        return {
            "portal_session": {"discord_handle": discord_handle},
            "portal_csrf_token": token,
            # The rail logout form only renders when this is present; it must
            # be on every authed page, not just the ones that set it manually.
            "logout_post_url": "/portal/logout",
            # Unseen market price alerts drive the nav bell badge.
            "portal_alert_count": market_watch.unseen_alert_count(aid) if aid else 0,
        }
    link_flow_cookie = request.cookies.get(LINK_FLOW_COOKIE, "")
    if link_flow_cookie:
        from portal_auth import csrf_for_link_flow, verify_link_flow
        flow = verify_link_flow(link_flow_cookie)
        handle = (flow or {}).get("dh", "") if flow else ""
        return {
            "portal_session": {"discord_handle": discord_handle or handle},
            "portal_csrf_token": csrf_for_link_flow(link_flow_cookie),
        }
    return {"portal_session": None, "portal_csrf_token": ""}


# ---------------------------------------------------------------- routes ---


_LANDING_BANNER_KINDS = frozenset(
    {"session_expired", "oauth_cancelled", "logged_out", "link_revoked"}
)


@router.get("/portal/")
async def portal_landing(request: Request, b: Optional[str] = None):
    """V2 is the default portal (2026-07-17 reveal). The root now redirects to the
    V2 SPA, which renders both the anonymous connect-Discord state and the authed
    dashboard. The classic V1 experience stays reachable at /portal/account (the
    'Classic' link in the V2 top nav). ?b= is accepted for backward-compat but the
    V2 SPA owns its own flash state, so it is no longer rendered here."""
    return RedirectResponse(url=v2_home(request.headers.get("host", "").lower()),
                            status_code=302)


@router.get("/portal/login")
async def portal_login(request: Request, return_to: str = ""):
    """Issue state token, log the attempt, 302 to Discord. Rate-limited per IP.
    Auth: public."""
    if config.PORTAL_AUTH_ENABLED:
        return RedirectResponse('/login', status_code=302)
    if not _config_ok():
        logger.warning("portal: login attempted but config incomplete")
        raise HTTPException(status_code=503, detail="Portal not configured")

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")[:512]
    host = request.headers.get("host", "").lower()

    ok, retry = check_oauth_start_ip(ip)
    if not ok:
        logger.info("portal: oauth_start rate-limited ip=%s", ip)
        resp = _render_error(
            request,
            "error_rate_limited.html",
            {"title": "Too many requests",
             "message": "Please slow down and try again shortly.",
             "retry_after_seconds": retry,
             "back_url": "/portal/"},
            status=429,
        )
        resp.headers["Retry-After"] = str(retry)
        return resp

    # Allowlist return_to. The rule moved into portal_host.safe_return_to when the
    # SPA took over the root of portal.lastsietch.com: on that host a legitimate
    # landing spot is /karum or /, not a /portal/* path, so the allowlist is now
    # "a relative path that cannot leave the site" with a per-host default.
    return_to = safe_return_to(return_to, host)

    nonce = secrets.token_urlsafe(16)
    state_token = issue_state_token(nonce, return_to)

    # Log the attempt row up front — this is what oauth_start IP rate-limit
    # counts against.
    test_run = 1 if is_test_run(request) else 0
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO portal_link_attempts
                 (state_token, state_issued_at, ip_addr, user_agent, is_test_run)
               VALUES (?, ?, ?, ?, ?)""",
            (state_token, _now_iso(), ip, ua, test_run),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("portal: oauth_start ip=%s", ip)

    authorize_url = (
        "https://discord.com/oauth2/authorize"
        f"?response_type=code&client_id={DISCORD_CLIENT_ID}"
        f"&scope=identify&state={state_token}"
        f"&redirect_uri={oauth_redirect_uri(host)}"
    )
    return RedirectResponse(url=authorize_url, status_code=302)


@router.get("/portal/oauth/callback")
async def portal_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Discord callback. Verify state, exchange code, fetch identity, then:
    - existing link → set session cookie + 302 /portal/account
    - no link      → set link_flow cookie + 302 /portal/link
    Auth: public."""
    if state and state.startswith('pa3.'):
        from routers.portal_signin import discord_callback
        return await discord_callback(request, code, state, error)
    if config.PORTAL_AUTH_ENABLED:
        return RedirectResponse('/login?error=challenge_expired', status_code=303)
    if not _config_ok():
        raise HTTPException(status_code=503, detail="Portal not configured")

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")[:512]

    if error:
        logger.info("portal: oauth_declined err=%s ip=%s", error, ip)
        return _render_error(
            request,
            "error_oauth_declined.html",
            {"title": "Discord login cancelled",
             "message": "You declined the Discord login or Discord rejected the request.",
             "back_url": "/portal/"},
        )

    # Stamp callback_hit_at BEFORE state validation so forged-/tampered-state
    # callbacks still count against the per-IP rate-limit (M-6 review fix).
    # Two-step: try to update the existing row (minted by /portal/login); if
    # none matches, insert a synthetic row. Avoids ON CONFLICT against the
    # partial unique index (SQLite UPSERT requires exact index match including
    # WHERE clause, which caused "does not match" errors with the partial index).
    received_state_truncated = (state or "")[:512] if state else "no-state"
    test_run = 1 if is_test_run(request) else 0
    try:
        conn = get_db()
        try:
            updated = conn.execute(
                """UPDATE portal_link_attempts
                      SET callback_hit_at = datetime('now')
                    WHERE state_token = ?""",
                (received_state_truncated,),
            ).rowcount
            if not updated:
                conn.execute(
                    """INSERT INTO portal_link_attempts
                         (state_token, state_issued_at, callback_hit_at, ip_addr, user_agent, is_test_run)
                       VALUES (?, datetime('now'), datetime('now'), ?, ?, ?)""",
                    (received_state_truncated, ip, ua, test_run),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # Don't block the callback path on a logging-table hiccup.
        logger.warning("portal: callback_hit_at stamp failed: %s", exc)

    ok_cb, retry_cb = check_oauth_callback_ip(ip)
    if not ok_cb:
        logger.info("portal: oauth_callback rate-limited ip=%s", ip)
        resp = _render_error(
            request,
            "error_rate_limited.html",
            {"title": "Too many requests",
             "message": "Please slow down and try again shortly.",
             "retry_after_seconds": retry_cb,
             "back_url": "/portal/"},
            status=429,
        )
        resp.headers["Retry-After"] = str(retry_cb)
        return resp

    if not code or not state:
        logger.info("portal: oauth_callback missing code or state ip=%s", ip)
        return _render_error(
            request,
            "error_state_expired.html",
            {"title": "Login link expired",
             "message": "Please start over.",
             "back_url": "/portal/"},
            status=400,
        )

    payload, state_status = verify_state_token_split(state)
    if state_status != "ok" or payload is None:
        logger.warning(
            "portal: oauth_callback_state_mismatch status=%s ip=%s", state_status, ip
        )
        # Both 'expired' and 'bad' surface the same template; differentiated
        # only in the log so a tampered state is forensically separable.
        return _render_error(
            request,
            "error_state_expired.html",
            {"title": "Login link expired",
             "message": "Please start over.",
             "back_url": "/portal/"},
            status=400,
        )

    # Atomic state-token consumption (M-3 review fix). Defense-in-depth on
    # top of Discord's own single-use enforcement of `code`: a replay of a
    # captured state after first consumption hits rowcount=0 and is rejected.
    conn = get_db()
    try:
        cur = conn.execute(
            """UPDATE portal_link_attempts
                  SET state_consumed_at = datetime('now')
                WHERE state_token = ? AND state_consumed_at IS NULL""",
            (state,),
        )
        conn.commit()
        consumed_rowcount = cur.rowcount
    finally:
        conn.close()
    if consumed_rowcount == 0:
        logger.warning("portal: oauth_callback_state_replay ip=%s", ip)
        return _render_error(
            request,
            "error_state_expired.html",
            {"title": "Login link expired",
             "message": "Please start over.",
             "back_url": "/portal/"},
            status=400,
        )

    # Exchange code → access_token.
    try:
        async with httpx.AsyncClient(timeout=PORTAL_OAUTH_HTTP_TIMEOUT) as client:
            token_resp = await client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    # The host the callback ARRIVED on, so this always matches the
                    # redirect_uri the authorize step sent. Discord refuses an
                    # exchange whose redirect_uri differs from the one the code was
                    # minted for, and the portal now answers on two hosts.
                    "redirect_uri": oauth_redirect_uri(
                        request.headers.get("host", "").lower()),
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        logger.warning("portal: discord token exchange transport error: %s", exc)
        return _render_error(
            request,
            "error_oauth_declined.html",
            {"title": "Discord login failed",
             "message": "We could not reach Discord. Please try again.",
             "back_url": "/portal/"},
            status=503,
        )

    if token_resp.status_code >= 400:
        # NOTE: never log the response body — may echo redirect_uri / state.
        logger.warning("portal: discord token exchange %d", token_resp.status_code)
        return _render_error(
            request,
            "error_oauth_declined.html",
            {"title": "Discord login cancelled",
             "message": "Discord rejected the login attempt.",
             "back_url": "/portal/"},
        )

    access_token = token_resp.json().get("access_token", "")
    if not access_token:
        logger.warning("portal: discord token exchange empty access_token")
        return _render_error(
            request,
            "error_oauth_declined.html",
            {"title": "Discord login cancelled",
             "message": "Discord did not return an access token.",
             "back_url": "/portal/"},
        )

    # Fetch identity (discard access_token immediately after).
    try:
        async with httpx.AsyncClient(timeout=PORTAL_OAUTH_HTTP_TIMEOUT) as client:
            ident_resp = await client.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("portal: discord identity transport error: %s", exc)
        return _render_error(
            request,
            "error_oauth_declined.html",
            {"title": "Discord login failed",
             "message": "We could not read your Discord identity. Please try again.",
             "back_url": "/portal/"},
            status=503,
        )

    if ident_resp.status_code >= 400:
        logger.warning("portal: discord identity %d", ident_resp.status_code)
        return _render_error(
            request,
            "error_oauth_declined.html",
            {"title": "Discord login cancelled",
             "message": "Discord rejected the identity request.",
             "back_url": "/portal/"},
        )

    ident = ident_resp.json()
    discord_id = str(ident.get("id") or "")
    discord_handle = ident.get("global_name") or ident.get("username") or ""

    if not discord_id.isdigit():
        logger.warning("portal: malformed discord id from /users/@me")
        return _render_error(
            request,
            "error_oauth_declined.html",
            {"title": "Discord login failed",
             "message": "Discord returned a malformed response. Please try again.",
             "back_url": "/portal/"},
            status=502,
        )

    # Bind discord_id to the (already-state-consumed) attempt row for audit /
    # rate-limit purposes. state_consumed_at was set above as the atomic gate.
    conn = get_db()
    try:
        conn.execute(
            """UPDATE portal_link_attempts
                  SET discord_id = ?
                WHERE state_token = ?""",
            (discord_id, state),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("portal: oauth_callback_ok did=%s ip=%s", discord_id, ip)

    # Existing link? Drop straight into the session.
    existing = _active_link_for_discord(discord_id)
    if existing:
        session_value = issue_session_cookie(discord_id, int(existing["account_id"]), ip, authentication_time=int(time.time()))
        _touch_last_session(int(existing["account_id"]))
        resp = RedirectResponse(url=payload.get("r") or "/portal/account", status_code=302)
        _attach_session_cookies(resp, session_value)
        return resp

    # New link flow — short-lived flow cookie carries discord_id forward.
    flow_value = issue_link_flow(discord_id, discord_handle, auth_time=int(time.time()))
    resp = RedirectResponse(url="/portal/link", status_code=302)
    from config import PORTAL_LINK_FLOW_MAX_AGE
    set_cookie(resp, LINK_FLOW_COOKIE, flow_value, max_age=PORTAL_LINK_FLOW_MAX_AGE, httponly=True)
    # Pre-session CSRF token bound to the link_flow cookie value — JS reads
    # this to echo as X-Portal-CSRF-Token on link/select and link/quiz POSTs.
    set_cookie(resp, CSRF_COOKIE, csrf_for_link_flow(flow_value),
               max_age=PORTAL_LINK_FLOW_MAX_AGE, httponly=False)
    return resp


def _roles_for_discord(discord_id: str) -> list:
    """Role names granted to the admin account mapped to this Discord id.

    The mapping is admin.db `users.discord_id`, which an operator sets explicitly
    (ops/deploy-portal-roles.sh --set). A role is NEVER inferred from a
    discord_handle or any client-visible allowlist: the browser is not a trust
    boundary and the admin session lives on a different origin and cookie.

    FAILS CLOSED on anything at all. portal_me rides main.py's restart, so a throw
    here takes the whole admin app down; an empty list only seals a role-gated
    surface, which is the correct answer for an unmapped player anyway.
    """
    if not discord_id:
        return []
    try:
        conn = get_db()
        try:
            import portal_identity
            discord_id = portal_identity.discord_for_identity(conn, discord_id)
            if not discord_id:
                return []
            row = conn.execute(
                "SELECT role FROM users WHERE discord_id = ? AND is_active = 1",
                (discord_id,),
            ).fetchone()
        finally:
            conn.close()
        return [row["role"]] if row and row["role"] else []
    except Exception:
        logger.debug("portal_me role lookup failed", exc_info=True)
        return []


@router.get("/portal/me")
async def portal_me(request: Request):
    """JSON identity. 401 with {authenticated:false} when no session."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    discord_id = portal_identity.actor_key(session)
    active_account_id = int(session.get("aid") or 0)

    # Single DB round-trip carries discord_handle alongside the projected
    # display fields. account_id stays server-side; the JSON output strips it
    # since portal callers (templates render server-side; future SPA widgets)
    # don't need to know the internal identifier (L-10 review fix).
    conn = get_db()
    try:
        rows = conn.execute(
            ("""SELECT account_id, character_name, linked_at, discord_handle
                 FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL
                ORDER BY linked_at ASC""").replace("ls_account_links", portal_identity.link_table(conn)),
            (discord_id,),
        ).fetchall()
    finally:
        conn.close()

    safe_linked = [
        {"character_name": r["character_name"], "linked_at": r["linked_at"]}
        for r in rows
    ]
    # Multi-account switcher payload. account_id is exposed here (a deviation from
    # the L-10 "strip account_id" rule) because the V2 account switcher needs a
    # stable key to POST /portal/select-account — and that endpoint re-validates
    # ownership from the session's discord_id, so the id is only ever the caller's
    # OWN. Only surfaced when the feature is enabled.
    accounts = [
        {
            "account_id": int(r["account_id"]),
            "character_name": r["character_name"],
            "active": int(r["account_id"]) == active_account_id,
        }
        for r in rows
    ] if config.MULTIACCOUNT_ENABLED else []
    discord_handle = next(
        (r["discord_handle"] for r in rows if int(r["account_id"]) == active_account_id),
        "",
    )
    return JSONResponse(
        {
            "authenticated": True,
            "discord_handle": discord_handle,
            "linked": safe_linked,
            "accounts": accounts,
            "multiaccount_enabled": config.MULTIACCOUNT_ENABLED,
            "profile_session": bool(session.get('sid')),
            # Server-authoritative feature flag: the frontend shows the
            # "Download my data" control only when the export is un-darked.
            "export_enabled": config.EXPORT_ENABLED,
            # Server-granted roles. [] for every unmapped session, which is what the
            # V2 auth gate reads as "not allowed" (authGate.svelte.js fails closed).
            "roles": _roles_for_discord(discord_id),
        }
    )


# Player tags are ALL Funcom game-progression flags (Contract/Journey/
# DialogueFlags/Exploration/Faction/... verified 2026-07-17: no Last Sietch/admin/
# moderation tags exist in dune.player_tags). We ALLOWLIST by namespace so the
# player export ships the legitimate journey progress AND auto-excludes anything
# an admin might later add under a non-game prefix (e.g. "Last Sietch.*"/"Watchlist").
_PLAYER_TAG_NAMESPACES = frozenset({
    "NPE", "Journey", "JourneySets", "Contract", "DialogueFlags",
    "DunipediaFlags", "Exploration", "BigMoments", "Faction", "Caste",
    "Character", "DLC", "MapMarkers", "Tutorial", "Achievement",
    "Mnemonic", "Recollection", "Store", "Cosmetic",
})


def _filter_player_tags(tags) -> list:
    """Keep only known Funcom game-progression namespaces; drop everything else
    (fail-safe against future internal tags leaking into a player-facing export)."""
    out = [t for t in (tags or [])
           if isinstance(t, str) and t.split(".", 1)[0] in _PLAYER_TAG_NAMESPACES]
    return sorted(out)


@router.get("/portal/export/character")
async def portal_export_character(request: Request):
    """Player self-service "Download my data": a personal snapshot of the
    SELECTED character (keepsake / portability copy, NOT a self-restore backup).

    Read-only. account_id + controller are resolved SERVER-SIDE from the session
    (never trusted from the client), so a player can only ever export their own
    selected character. Ships DARK behind LASTSIETCH_EXPORT_ENABLED."""
    import re as _re_exp
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    active_account_id = int(session.get("aid") or 0)
    if not active_account_id:
        return JSONResponse({"authenticated": False}, status_code=401)

    if not config.EXPORT_ENABLED:
        # Dark-launch: honest "not yet" rather than a 404, mirroring rewards.
        return JSONResponse({"ok": True, "status": "deferred",
                             "message": "Character data export is not enabled yet."})

    ctrl = _selected_ctrl(request, active_account_id)
    from relay import call_relay

    # 1. Character snapshot (controller-scoped when a character is selected).
    export_path = f"/dune/player/{active_account_id}/character/export"
    if ctrl:
        export_path += f"?ctrl={int(ctrl)}"
    try:
        snap_env = await call_relay(export_path, timeout=60)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal export: snapshot fetch failed acct=%s: %s",
                       active_account_id, exc)
        return JSONResponse({"ok": False, "error": "export_unavailable"}, status_code=503)
    if not snap_env or not snap_env.get("available"):
        return JSONResponse({"ok": False,
                             "error": (snap_env or {}).get("error") or "unavailable"},
                            status_code=404)
    snapshot = snap_env.get("snapshot") or {}
    character = snapshot.get("character") or {}

    # 2. Economy (controller-scoped). Best-effort; never fail the whole export.
    economy = {}
    try:
        prog_path = f"/dune/player/{active_account_id}/progress"
        if ctrl:
            prog_path += f"?ctrl={int(ctrl)}"
        prog = await call_relay(prog_path, timeout=30)
        econ = (prog or {}).get("economy") or {}
        economy = {"bank_solari": econ.get("bank_solari"),
                   "pocket_solari": econ.get("pocket_solari"),
                   "scrip": econ.get("scrip")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal export: economy fetch failed acct=%s: %s",
                       active_account_id, exc)
        economy = {"error": True}

    # 3. Storage summary (account-wide, mirror-first, read-only). The mirror is
    # account-keyed (per-char container reads were deferred in the multichar
    # work), so this is labelled account-wide, not per-character.
    storage = {"note": "Account-wide storage as of your last save.", "items": []}
    try:
        idx = mirror.get_storage_search(active_account_id)
        if idx is None:
            from routers.dune import _cached_container_search
            idx = await _cached_container_search(str(active_account_id))
        agg: dict = {}
        for r in ((idx or {}).get("rows") or []):
            tpl = r.get("template_id") or ""
            if tpl:
                agg[tpl] = agg.get(tpl, 0) + int(r.get("qty") or 0)
        storage["items"] = [
            {"item": _ITEM_NAMES.lookup_or_synthesize(t), "template_id": t, "quantity": q}
            for t, q in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))]
        storage["distinct_items"] = len(agg)
        storage["total_quantity"] = sum(agg.values())
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal export: storage fetch failed acct=%s: %s",
                       active_account_id, exc)
        storage = {"error": True, "note": "storage unavailable"}

    # 4. Journey progress = game-progression tags, namespace-allowlisted.
    journey_progress = _filter_player_tags(character.get("tags"))

    bundle = {
        "ls_bundle_schema": "player-export-v1",
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server": "Last Sietch (lastsietch.com)",
        "notice": ("This is a personal snapshot of your character data as of now. "
                   "It is a keepsake and portability copy, NOT a backup you can "
                   "restore yourself. Your character is saved on the server and "
                   "backed up every 6 hours."),
        "character": {
            "name":                  character.get("character_name"),
            "map":                   character.get("map"),
            "online_status":         character.get("online_status"),
            "level_component":       character.get("FLevelComponent"),
            "faction":               character.get("FactionPlayerComponent"),
            "specialization_tracks": character.get("specialization_tracks"),
        },
        "economy": economy,
        "storage": storage,
        "journey_progress": journey_progress,
        # Full-fidelity raw snapshot for portability (tags already allowlisted
        # above are the player-facing view; raw actor props kept for a future
        # import path). NOT surfaced as human-readable.
        "raw_snapshot": snapshot,
    }

    charname = character.get("character_name") or "character"
    safe = (_re_exp.sub(r"[^A-Za-z0-9_-]+", "_", charname)[:40] or "character")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    fname = f"lastsietch-{safe}-{stamp}.json"
    return Response(
        content=json.dumps(bundle, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/portal/characters")
async def portal_characters(request: Request):
    """Portal multi-character switcher: every non-Deleted character on the
    player's ACTIVE account (session aid), most-recently-active first. Marks the
    default pick and which one is currently selected (from the selected-character
    cookie, falling back to the default when the cookie is absent/stale).
    account_id stays server-side; controller_id is the same opaque per-character
    handle the write routes already resolve to."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    active_account_id = int(session.get("aid") or 0)
    try:
        from relay import call_relay
        payload = await call_relay(
            f"/dune/player/{active_account_id}/progress?list=1", timeout=20)
    except Exception as exc:
        logger.warning("portal: character list failed for acct %s: %s",
                       active_account_id, exc)
        return JSONResponse({"characters": [], "error": "unavailable"})
    chars = (payload or {}).get("characters") or []
    ctrl_ids = [c.get("controller_id") for c in chars]
    sel = _selected_ctrl(request, active_account_id)
    effective = sel if sel in ctrl_ids else None
    out = []
    for c in chars:
        ctrl = c.get("controller_id")
        selected = (ctrl == effective) if effective is not None else bool(c.get("is_default"))
        out.append({
            "controller_id": ctrl,
            "char_name": c.get("char_name"),
            "lvl": c.get("lvl"),
            "online": bool(c.get("online")),
            "is_default": bool(c.get("is_default")),
            "selected": selected,
        })
    return JSONResponse({"characters": out})


@router.post("/portal/select-character")
async def portal_select_character(request: Request):
    """Portal multi-character: set the character the player is acting as. The
    requested controller is VALIDATED against the account's live non-Deleted
    characters before the signed selection cookie is issued; every write route
    additionally re-resolves through the host progress script, which is the
    ultimate ownership authority (a stale/forged cookie fails safe to the
    default pick). CSRF-gated like every other portal write."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"ok": False, "error": "unauthenticated"}, status_code=401)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    body = await _read_body(request)
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (body.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)
    active_account_id = int(session.get("aid") or 0)
    try:
        ctrl = int(body.get("controller_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad_controller"}, status_code=400)
    try:
        from relay import call_relay
        payload = await call_relay(
            f"/dune/player/{active_account_id}/progress?list=1", timeout=20)
    except Exception as exc:
        logger.warning("portal: select-character list failed acct %s: %s",
                       active_account_id, exc)
        return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)
    valid = {c.get("controller_id") for c in ((payload or {}).get("characters") or [])}
    if ctrl not in valid:
        return JSONResponse({"ok": False, "error": "not_your_character"}, status_code=403)
    resp = JSONResponse({"ok": True, "controller_id": ctrl})
    set_cookie(resp, SELCHAR_COOKIE,
               issue_selchar_cookie(active_account_id, ctrl),
               max_age=PORTAL_SESSION_IDLE_MAX_AGE)
    return resp


@router.post("/portal/select-account")
async def portal_select_account(request: Request):
    """Multi-account: switch the active linked account. The requested account_id is
    VALIDATED against the caller's OWN active links (keyed on the signed session's
    discord_id, never the request body) before a fresh session cookie pinned to it
    is issued. The old account's selected-character cookie is cleared so the new
    account resolves to its own default character. CSRF-gated like select-character."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"ok": False, "error": "unauthenticated"}, status_code=401)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    body = await _read_body(request)
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (body.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)
    discord_id = str(portal_identity.actor_key(session))
    try:
        want = int(body.get("account_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad_account"}, status_code=400)
    # Ownership: the Discord must hold an ACTIVE link to the requested account.
    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT 1 FROM ls_account_links
                WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL LIMIT 1""").replace("ls_account_links", portal_identity.link_table(conn)),
            (discord_id, want),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "not_your_account"}, status_code=403)
    ip = client_ip(request)
    resp = JSONResponse({"ok": True, "account_id": want})
    session_value = issue_switched_session_cookie(session, want, ip)
    set_cookie(resp, SESSION_COOKIE, session_value,
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=True)
    set_cookie(resp, CSRF_COOKIE, csrf_for_session(session_value),
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=False)
    clear_cookie(resp, SELCHAR_COOKIE)
    return resp


@router.get("/portal/account")
async def portal_account(request: Request):
    """Logged-in dashboard. Renders linked-character list + active character
    details. Live state (lvl / map / faction) fetched best-effort from the
    relay cached helpers; falls back to stored snapshot fields on relay miss."""
    session = get_portal_session(request)
    if not session:
        return RedirectResponse(url="/portal/", status_code=302)
    discord_id = portal_identity.actor_key(session)
    active_account_id = int(session.get("aid") or 0)

    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT character_name, discord_handle, linked_at
                 FROM ls_account_links
                WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL""").replace("ls_account_links", portal_identity.link_table(conn)),
            (discord_id, active_account_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        # Link was revoked under our feet — render the revoked page.
        return _render_link_revoked(request)

    _touch_last_session(active_account_id)

    # Best-effort live state. VC0 perf: the card needs ~6 distinct relay reads
    # (snapshot, tags, current-map, progress, specializations, journey,
    # landsraad). They were awaited serially, stacking latency on cold loads
    # (every first load after link/login + every per-account TTL expiry). Fan the
    # independent reads out concurrently; only current_map depends on the
    # snapshot (it needs the character name), so chain just that pair. Each helper
    # already degrades to None on failure; _safe keeps the gather first-paint-safe.
    # (The journey card re-reads tags, but it shares _cached_player_tags' single-
    # flight lock with _read_player_tags, so that is one network round-trip.)
    from portal_quiz import (
        _current_faction,
        _derive_current_map,
        _find_player_in_snapshot,
        _read_player_tags,
    )

    async def _safe(coro, label):
        try:
            return await coro
        except Exception as exc:
            logger.warning("portal: account fetch %s failed: %s", label, exc)
            return None

    async def _snapshot_and_map():
        # Fast path: the mirror's denormalized scalars cover everything this card
        # reads from the snapshot (char_name / online / lvl / intel). Shape it like
        # a snapshot row so the downstream consumption is unchanged. current_map
        # stays the live cached roster in v1 (not mirrored). Falls back to the live
        # snapshot on miss/stale/flag-off.
        snap = None
        scalars = mirror.get_scalars(active_account_id)
        if scalars is not None:
            snap = {
                "account_id": active_account_id,
                "char_name": scalars["char_name"],
                "lvl": scalars["lvl"],
                "intel": scalars["intel"],
                "online_status": "Online" if scalars["online"] else "Offline",
            }
        if snap is None:
            snap = await _find_player_in_snapshot(active_account_id)
        cmap = None
        if snap:
            cmap = await _derive_current_map(snap.get("char_name") or row["character_name"])
        return snap, cmap

    snap_map, tags, progress, specializations, journey, shaped = await asyncio.gather(
        _safe(_snapshot_and_map(), "snapshot/map"),
        _safe(_read_player_tags(active_account_id), "tags"),
        _safe(_load_progress(active_account_id), "progress"),
        _safe(_load_specializations(active_account_id), "specializations"),
        _safe(_load_journey(active_account_id), "journey"),
        _safe(_load_landsraad(active_account_id), "landsraad"),
    )

    snap, current_map = snap_map if snap_map else (None, None)
    lvl = None
    online = False
    intel = None
    last_online = None
    if snap:
        lvl = snap.get("lvl")
        online = bool(snap.get("online_status") == "Online")
        intel = snap.get("intel")
        # Snapshot doesn't carry a last-online timestamp; surface a coarse
        # marker via online_status. v1.1 can plumb a real timestamp.
        last_online = "Online now" if online else None
    # Current faction from the live progression block, not the alignment tags
    # (which retain the ORIGINAL faction after a switch). Keeps this card's
    # top-line faction consistent with the faction-rep card below it.
    faction = _current_faction(progress, tags or [])

    # Character + economy stats (best-effort; card slots degrade to '—' on miss).
    char_xp = None
    unspent_sp = None
    bank_solari = None
    pocket_solari = None
    scrip = None
    faction_rep = None
    if progress:
        char = progress.get("character") or {}
        econ = progress.get("economy") or {}
        char_xp = char.get("xp")
        unspent_sp = char.get("unspent_sp")
        # Two distinct Solari stores: bank = vcb wallet (in-game top-right);
        # pocket = SolarisCoin items across backpack + containers + vehicles.
        bank_solari = econ.get("bank_solari", econ.get("solari"))
        pocket_solari = econ.get("pocket_solari")
        scrip = econ.get("scrip")
        # Faction standing card — only for the two aligned houses that have rep.
        fac = progress.get("faction") or {}
        if fac.get("faction_id") in (1, 2) and fac.get("reputation") is not None:
            rk = _faction_rank(fac["reputation"], fac.get("faction_name"))
            faction_rep = {
                "faction": fac.get("faction_name"),
                "crest": _faction_crest(fac.get("faction_name")),
                "standing_icon": _stat_icon("faction-standing"),
                "rank": rk["rank"],
                "rank_name": rk["rank_name"],
                "standing_display": f"{rk['standing']:,}",
                "at_max": rk["at_max"],
                "pct": rk["pct"],
                "next_rank": rk.get("next_rank"),
                "to_next_display": f"{rk['to_next']:,}" if not rk["at_max"] else None,
            }

    # specializations + journey + shaped(landsraad) were fetched concurrently above.

    # Landsraad rewards teaser (best-effort; card hidden on miss or when empty).
    landsraad_teaser = None
    if shaped and shaped.get("available") and shaped["summary"]["total_lines"] > 0:
        s = shaped["summary"]
        landsraad_teaser = {
            "total_lines": s["total_lines"],
            "total_solari_display": s["total_solari_display"],
            "schematic_lines": s["schematic_lines"],
        }

    linked_chars = _linked_characters(discord_id)
    linked_list = [
        {
            "character_name": r["character_name"],
            "linked_at": r["linked_at"],
            "is_active": int(r["account_id"]) == active_account_id,
        }
        for r in linked_chars
    ]

    live_block = {
        "character_level": lvl,
        "current_map": current_map,
        "faction": faction,
        # Faction emblem basename (atreides|harkonnen) for the redesigned top
        # card; None for Unaligned/unknown so the card shows an "N/A" chip.
        "faction_crest": _faction_crest(faction) if faction and faction != "Unaligned" else None,
        "intel_points": intel,
        "character_xp": char_xp,
        "character_xp_display": f"{char_xp:,}" if char_xp is not None else None,
        "unspent_sp": unspent_sp,
        "bank_solari": bank_solari,
        "bank_solari_display": f"{bank_solari:,}" if bank_solari is not None else None,
        "pocket_solari": pocket_solari,
        "pocket_solari_display": f"{pocket_solari:,}" if pocket_solari else None,
        "scrip": scrip,
        "scrip_display": f"{scrip:,}" if scrip is not None else None,
        "last_online": last_online,
    }
    # Heuristic: if every key is None, the relay call effectively failed —
    # let the template show its info banner instead of an empty card.
    live_error = None
    if all(v is None for v in live_block.values()):
        live_error = "Live game data is temporarily unavailable. Please refresh in a minute."

    ctx = {
        "discord_handle": row["discord_handle"],
        "active_character": {
            "character_name": row["character_name"],
            "lvl": lvl,
            "linked_at": row["linked_at"],
            "current_map": current_map,
            "online": online,
            "faction": faction,
        },
        # Alias for dev-frontend's account.html top-line header.
        "active_character_name": row["character_name"],
        "linked": linked_list,
        "linked_characters": linked_list,  # legacy/alias for either template
        "live": None if live_error else live_block,
        "live_error": live_error,
        "faction_rep": faction_rep,
        "specializations": specializations,
        "journey": journey,
        "landsraad_teaser": landsraad_teaser,
        # Optional stat/currency glyphs for the vitals card (None when the PNG
        # is not present, so the label renders text-only). slug -> basename.
        "stat_icons": {
            "level": _stat_icon("level"),
            "intel": _stat_icon("intel"),
            "xp": _stat_icon("xp"),
            "skill_point": _stat_icon("skill-point"),
            "solari": _stat_icon("solari"),
            "solari_bank": _stat_icon("solari-bank"),
            "scrip": _stat_icon("scrip"),
        },
        "logout_post_url": "/portal/logout",
        # Show the 3D gear demo section when PORTAL_SHOW_GEAR_DEMO=1 (dev only).
        "show_gear_demo": _SHOW_GEAR_DEMO,
    }
    # Equipped gear for the character stage. Live feed = the player's equipped
    # inventory (inventory_type=1) via the relay collector; falls back to the
    # dev demo set (PORTAL_SHOW_GEAR_DEMO) or a coming-soon placeholder.
    # Shape: [{slot, name, template_id, category, quality, variant_id, swatch_id}].
    equipped_items = await _load_equipped(active_account_id)
    if equipped_items is None and _SHOW_GEAR_DEMO:
        equipped_items = [
            {"slot": "Chest", "name": "CHOAM Stillsuit",
             "template_id": "Stillsuit_Choam_Unique_Dashed06_Top", "category": "garment",
             "quality": 3, "variant_id": "06", "swatch_id": "Dune_Tan"},
            {"slot": "Head", "name": "Smuggler Assault Helmet",
             "template_id": "D_Combat_Smug_Assault05_Helmet", "category": "garment",
             "quality": 2, "variant_id": "05", "swatch_id": ""},
            {"slot": "Primary", "name": "Atreides LMG",
             "template_id": "AtreLMG1", "category": "ranged", "quality": 4},
            {"slot": "Melee", "name": "Minotaur Sword",
             "template_id": "Minotaur_Sword", "category": "melee", "quality": 1},
        ]
    ctx["equipped_items"] = equipped_items
    ctx["equipped_items_json"] = json.dumps(equipped_items or [])
    ctx.update(base_ctx(request, discord_handle=row["discord_handle"]))
    return templates.TemplateResponse(request, "portal/account.html", ctx)


# Public Deep Desert spice-field map. Restored 2026-06-08 after the active-field
# signal research: the live producer now reports the authoritative Large field_id
# per dimension (dune.resourcefield_state), which build_grid pairs against the
# survey cache to pin a surveyed field, auto-detect rotation, and shade the legal
# band (rows D-I) when awaiting a fresh survey. Read-only + global; safe to be
# public. (RE: the world position is never persisted, so the exact sector still
# comes from a survey keyed to field_id; see SPICE-FIELDID-DECODE.md.)
@router.get("/portal/spice")
async def portal_spice(request: Request):
    data = spice_fields.load()
    try:
        from routers.dune import cached_spice_active
        active = await cached_spice_active()
    except Exception as exc:  # relay/DB down -> render the catalog without the live overlay
        logger.warning("portal_spice: live active fetch failed: %s", exc)
        active = None
    grid = spice_fields.build_grid(data, active if isinstance(active, dict) else None)
    ctx = {"grid": grid}
    ctx.update(base_ctx(request))
    ctx["active_nav"] = "spice"
    return templates.TemplateResponse(request, "portal/spice.html", ctx)


# ---- Maps hub: coordinate-accurate Deep Desert + Hagga Basin maps ----------
# The static POI/resource layer comes from our own game DB (dune.markers); the
# data endpoint is cached and PII-safe so the public site can consume the same
# call later (no duplicate backend load). Live overlays (active spice, players,
# bases) layer on via the existing relay endpoints.
_MAP_DATA_CACHE: dict[str, tuple[float, dict]] = {}
_MAP_DATA_TTL = 3600.0          # markers change once per 14-day Coriolis cycle


@router.get("/portal/maps")
async def portal_maps(request: Request):
    """Maps hub: pick a map (Deep Desert / Hagga Basin) and instance."""
    maps = [map_model.map_meta(k) for k in map_model.MAPS]
    ctx = {"maps": [m for m in maps if m]}
    ctx.update(base_ctx(request))
    ctx["active_nav"] = "maps"
    return templates.TemplateResponse(request, "portal/maps.html", ctx)


@router.get("/portal/maps/{map_key}")
async def portal_map(request: Request, map_key: str):
    """Coordinate-accurate map renderer for one map (DD or Hagga)."""
    meta = map_model.map_meta(map_key)
    if not meta:
        raise HTTPException(404, "Unknown map")
    ctx = {"map": meta}
    ctx.update(base_ctx(request))
    ctx["active_nav"] = "maps"
    return templates.TemplateResponse(request, "portal/map.html", ctx)


@router.get("/portal/maps/{map_key}/data")
async def portal_map_data(map_key: str):
    """Cached marker/legend/calibration JSON for the JS engine. Reusable by the
    public site. No live state here (overlays are fetched separately)."""
    if map_key not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    now = time.monotonic()
    hit = _MAP_DATA_CACHE.get(map_key)
    if hit and now - hit[0] < _MAP_DATA_TTL:
        data = hit[1]
    else:
        data = map_model.build_data(map_key)
        if data is None:
            raise HTTPException(404, "Unknown map")
        _MAP_DATA_CACHE[map_key] = (now, data)
    # Replace the hardcoded Large spice candidate sites with the RAM-authoritative
    # set the reader auto-discovers this cycle (the sites change only at the
    # Coriolis reset, so this is the static-per-cycle layer; the active blow is a
    # separate live overlay). Falls back to the hardcoded list if RAM is
    # unavailable. Returned in a shallow copy so the static marker cache isn't
    # poisoned by the live value.
    if data.get("has_spice"):
        try:
            from routers.dune import cached_spice_active
            # Cap the live spice fetch so /data never blocks on a slow/cold relay
            # round-trip (a cold game-box read can be 15-30s). Shield the call so it
            # finishes in the background and warms the 90s cache for the next
            # request; THIS response then falls back to the static per-cycle
            # candidates already in build_data (+ the /live overlay the client polls
            # separately). Keeps the map snappy on first load for everyone.
            _spice_task = asyncio.ensure_future(cached_spice_active())
            _spice_task.add_done_callback(lambda t: t.cancelled() or t.exception())
            active = await asyncio.wait_for(asyncio.shield(_spice_task), timeout=4.0)
            instant = {s for dim in (active.get("dimensions") or {}).values()
                       for s in (dim.get("ram_candidates") or [])}
            # Union the instantaneous candidates with everything accumulated this
            # Coriolis cycle, so the map plots EVERY candidate Large site seen this
            # cycle (the reader only instantiates ~3 at a time and rotates them).
            try:
                import spice_candidates_acc
                spice_candidates_acc.record(active)
                cands = spice_candidates_acc.union(instant)
            except Exception as exc:
                logger.warning("portal_map_data: candidate accumulation failed: %s", exc)
                cands = sorted(instant)
            if cands:
                data = {**data, "spice_candidates": cands}
            # Exact per-candidate coords (Part B). The reader emits ram_candidates_xy
            # ([{sector,x,y}]) once Phase 2 lands; until then this stays empty and the
            # client falls back to sectorCenter(sector). The accumulator carries the
            # coords across rotations so a site that rotated away keeps its position.
            try:
                import spice_candidates_acc
                coords: dict[str, list] = {}
                for dim in (active.get("dimensions") or {}).values():
                    for c in (dim.get("ram_candidates_xy") or []):
                        sec = (c.get("sector") or "").strip().upper()
                        nxy = map_model.project(map_key, c.get("x"), c.get("y")) \
                            if c.get("x") is not None and c.get("y") is not None else None
                        if sec and nxy:
                            coords[sec] = [nxy[0], nxy[1]]
                acc_xy = spice_candidates_acc.coords_by_sector()
                for sec, (wx, wy) in acc_xy.items():
                    if sec not in coords:
                        nxy = map_model.project(map_key, wx, wy)
                        if nxy:
                            coords[sec] = [nxy[0], nxy[1]]
                if coords:
                    data = {**data, "spice_candidate_coords": coords}
            except Exception as exc:
                logger.warning("portal_map_data: candidate coords failed: %s", exc)
            # Medium spice layer (Part A). The candidate medium SITES are the full
            # per-cycle set with exact coords (no accumulation); spatially identical
            # across PvE/PvP, so union the dims and dedupe by rounded projected coord.
            # Only a SUBSET is active/erupted at a time and rotates, so carry a 4th
            # element `active` (a site is active if erupted in EITHER dim, matching
            # this layer's dim-union design). Each entry is [nx, ny, sector, active].
            # Empty until the reader emits ram_mediums (degrades to no medium layer,
            # never an error); pre-active readers omit `active` -> falsy -> inactive.
            try:
                meds: list[list] = []
                idx: dict[tuple[float, float], list] = {}
                for dim in (active.get("dimensions") or {}).values():
                    for md in (dim.get("ram_mediums") or []):
                        x, y = md.get("x"), md.get("y")
                        if x is None or y is None:
                            continue
                        nxy = map_model.project(map_key, x, y)
                        if not nxy:
                            continue
                        rk = (round(nxy[0], 1), round(nxy[1], 1))
                        act = bool(md.get("active"))
                        ex = idx.get(rk)
                        if ex is not None:
                            if act:
                                ex[3] = True   # erupted in either dim -> active
                            continue
                        entry = [nxy[0], nxy[1], (md.get("sector") or "").upper(), act]
                        idx[rk] = entry
                        meds.append(entry)
                if meds:
                    data = {**data, "spice_mediums": meds}
            except Exception as exc:
                logger.warning("portal_map_data: medium layer build failed: %s", exc)
            # M4: identify the active Coriolis layout so the holo War-Table can
            # render the real baked heightfield for THIS cycle. Scores the live
            # spice candidate/medium coords + static shipwreck markers against the
            # 12 baked fingerprints. Absent fingerprints (pre-bake) -> no field,
            # so the client stays on the seeded procedural relief. Never blocks.
            try:
                import dd_layout_match
                layout = dd_layout_match.match(
                    data,
                    data.get("spice_candidate_coords"),
                    data.get("spice_mediums"),
                )
                # Owner override for THIS cycle wins over the matcher (set with
                # ops/dd-layout-override.sh; expires at the next Coriolis reset).
                override = dd_layout_match.override_for_cycle()
                if override:
                    layout = override
                if layout:
                    data = {**data, "layout": layout}
                    # Islands: once the layout is known, the 2D map gets the baked
                    # rock-island backdrop for it instead of the flat sand.
                    bd = dd_layout_match.backdrop_for(map_key, layout)
                    if bd:
                        data = {**data, "backdrop": bd}
            except Exception as exc:
                logger.warning("portal_map_data: layout match failed: %s", exc)
        except Exception as exc:
            logger.warning("portal_map_data: live candidate fetch failed: %s", exc)
        # Deterministic Coriolis cycle window (no game-box read): the engine logs a
        # fixed 05:00 UTC / 14-day cadence, so the next-reset countdown is computed
        # from the anchor. Recomputed per request (cheap) so it stays fresh under
        # the static-marker cache; the client renders a live countdown from it.
        try:
            import spice_candidates_acc
            cw = spice_candidates_acc.cycle_window()
            if cw:
                data = {**data, "coriolis": cw}
        except Exception as exc:
            logger.warning("portal_map_data: coriolis window failed: %s", exc)
    return JSONResponse(data, headers={"Cache-Control": "public, max-age=900"})


async def _spice_overlay(map_key: str) -> dict:
    """Active-Large spice overlay payload for a DD map (or empty). Reads the
    process-wide TTL cache (cached_spice_active), so this is cheap to call as often
    as the live feed polls -- the relay/SSH hit happens at most once per cache TTL
    regardless of caller count."""
    try:
        from routers.dune import cached_spice_active
        active = await cached_spice_active()
    except Exception as exc:
        logger.warning("spice overlay fetch failed: %s", exc)
        active = {"dimensions": {}}
    active = active if isinstance(active, dict) else {"dimensions": {}}
    # Accumulate the candidate set per Coriolis cycle (the live feed is the most
    # frequent spice poll, so this is where the per-cycle union fills in). Best
    # effort; never break the overlay on a logging failure.
    try:
        import spice_candidates_acc
        spice_candidates_acc.record(active)
    except Exception as exc:
        logger.warning("spice candidate accumulation failed: %s", exc)
    # Project the active-Large RAM coords (ram_x/ram_y) into map space so the client
    # plots the blow at its EXACT position instead of the sector centroid. Returned
    # in a shallow copy so the shared TTL cache (cached_spice_active) is never
    # mutated. Falls back to sectorCenter(ram_sector) client-side when absent.
    try:
        dims = active.get("dimensions") or {}
        out_dims = {}
        for d, info in dims.items():
            if not isinstance(info, dict):
                out_dims[d] = info
                continue
            info2 = dict(info)
            rx, ry = info.get("ram_x"), info.get("ram_y")
            if rx is not None and ry is not None:
                nxy = map_model.project(map_key, rx, ry)
                if nxy:
                    info2["ram_nx"], info2["ram_ny"] = nxy[0], nxy[1]
            # Project each surfaced Large (Spice Harvest = 2-3 at once) so the client
            # plots every blow at its exact position. Falls back to sectorCenter on
            # any field missing coords. Shallow-copies each entry to avoid mutating
            # the shared TTL cache.
            fields = info.get("ram_active_fields") or []
            if fields:
                proj = []
                for f in fields:
                    f2 = dict(f) if isinstance(f, dict) else f
                    if isinstance(f2, dict) and f2.get("x") is not None and f2.get("y") is not None:
                        nxy = map_model.project(map_key, f2["x"], f2["y"])
                        if nxy:
                            f2["nx"], f2["ny"] = nxy[0], nxy[1]
                    proj.append(f2)
                info2["ram_active_fields"] = proj
            out_dims[d] = info2
        return {**active, "dimensions": out_dims}
    except Exception as exc:
        logger.warning("spice overlay projection failed: %s", exc)
        return active


async def _worms_overlay(map_key: str) -> dict:
    """Sandworm overlay payload for a DD map: each worm's last-known position
    normalized to map coords (same calibration as the static markers) + threat
    state + staleness. Reads the process-wide TTL cache (cached_worms). PII-safe
    (worm positions only, no players)."""
    try:
        from routers.dune import cached_worms
        live = await cached_worms()
    except Exception as exc:
        logger.warning("worms overlay fetch failed: %s", exc)
        return {"dimensions": {}}
    if not isinstance(live, dict):
        return {"dimensions": {}}
    out = {}
    for dim, info in (live.get("dimensions") or {}).items():
        worms = []
        for w in (info.get("worms") or []):
            x, y = w.get("x"), w.get("y")
            if x is None or y is None:
                continue
            nxy = map_model.project(map_key, x, y)
            if not nxy:
                continue
            worms.append({
                "id": w.get("id"), "nx": nxy[0], "ny": nxy[1],
                "sector": w.get("sector"), "threat": w.get("threat"),
                "surfaced": w.get("surfaced"), "enraged": w.get("enraged"),
                "wants_breach": w.get("wants_breach"),
                "in_safe_zone": w.get("in_safe_zone"), "age_s": w.get("age_s"),
            })
        out[str(dim)] = {"label": info.get("label"), "worms": worms}
    return {"dimensions": out, "generated_utc": live.get("generated_utc")}


async def _no_overlay() -> dict:
    """Empty layer payload, awaitable so it can sit in the /live gather()."""
    return {"dimensions": {}}


async def _no_storm_overlay() -> dict:
    return {"dimensions": {}, "available": False}


def _storm_world_keys(map_key: str) -> dict[str, str]:
    """{reader world key -> client dimension key} for one map.

    The storm readers key Deep Desert by dimension ("0"/"1") and each Hagga
    sietch by PARTITION ("hagga:1" Habbanya, "hagga:32" Kulon, "hagga:33" Amtal)
    -- the prefix is load-bearing, since Habbanya's partition 1 would otherwise
    collide with the DD PvP dimension key 1. Both map clients look a storm up by
    the selected INSTANCE's dim (drawStorm reads dimensions[String(dim)]), so
    this does two jobs at once: it FILTERS the shared relay payload down to the
    worlds belonging to this map (a Hagga storm must never be projected with the
    DD calibration, or vice versa), and it RE-KEYS onto the instance dim, which
    is what stops a Kulon storm drawing on the Habbanya tab.
    """
    m = map_model.MAPS.get(map_key) or {}
    out: dict[str, str] = {}
    for inst in (m.get("instances") or []):
        dim = inst.get("dim")
        if dim is None:
            continue
        part = inst.get("part")
        # A map whose instances carry a partition is partition-keyed upstream.
        world = f"{map_key}:{part}" if part is not None else str(dim)
        out[world] = str(dim)
    return out


async def _sandstorm_overlay(map_key: str) -> dict:
    """Sandstorm overlay payload for a storm-bearing map. PRIMARY signal is per-dimension
    last spawn / next-storm ETA / mean cadence / confidence -- TIME-ONLY, because
    the DD logs carry no storm coordinates, so the client renders a countdown
    banner, not a position.

    Consensus forward hook (#5): IF the live storm RAM reader supplies a moving
    storm CENTER (center_x/center_y) -- with radius, heading_yaw, stage -- per
    dimension, this projects the center into map space (center_nx/center_ny via
    the same calibration the static markers use) and scales the world radius to
    the map frame (radius_nr); heading_yaw + stage + active pass through. An
    optional swept-band geometry (start_/end_ x/y) is projected too if a future
    reader ever emits it. These keys stay ABSENT until the reader is live; today's
    time-only feed passes through verbatim and the client draws banner-only. The
    ETA/region/Coriolis-countdown fields are never touched. Reads the process-wide
    TTL cache (cached_sandstorm). PII-safe (no players)."""
    try:
        from routers.dune import cached_sandstorm
        live = await cached_sandstorm()
    except Exception as exc:
        logger.warning("sandstorm overlay fetch failed: %s", exc)
        return {"dimensions": {}, "available": False}
    if not isinstance(live, dict):
        return {"dimensions": {}, "available": False}
    # Guarded projection. Returned in a shallow copy so the shared TTL cache
    # (cached_sandstorm) is never mutated. Only a dim carrying a numeric world
    # center gains center_nx/center_ny (+ radius_nr from a numeric world radius);
    # heading_yaw/stage/active ride through the dict copy untouched. A no-op on
    # the current coordinate-less feed.
    def _num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    try:
        dims = live.get("dimensions")
        if not isinstance(dims, dict):
            return live
        m = map_model.MAPS.get(map_key)
        span = (m or {}).get("cal", {}).get("spanX")
        # The relay payload carries EVERY world (both DD dims + all three Hagga
        # sietches) in one dict, so each map takes only its own and re-keys them
        # onto the instance dim the client indexes by. DD is unchanged by this:
        # its worlds are already keyed "0"/"1" and map to themselves.
        out_dims = {}
        for world, ckey in _storm_world_keys(map_key).items():
            info = dims.get(world)
            if info is None:
                continue
            if not isinstance(info, dict):
                out_dims[ckey] = info
                continue
            info2 = dict(info)            # heading_yaw / stage / active pass through
            cx, cy = info.get("center_x"), info.get("center_y")
            if _num(cx) and _num(cy):
                nxy = map_model.project(map_key, cx, cy)
                if nxy:
                    info2["center_nx"], info2["center_ny"] = nxy[0], nxy[1]
                    r = info.get("radius")
                    # NOTE: re-storm-server must confirm whether the stored 0x59C
                    # value is r or r^2 (it is used as r^2 in overlap-damage); if
                    # r^2, sqrt() it upstream in the reader, not here.
                    if _num(r) and span:
                        info2["radius_nr"] = round(float(r) / span * map_model.VIEW, 1)
            # Optional swept-band geometry (dead-code until a reader emits it).
            for sx_key, sy_key, nx_key, ny_key in (
                    ("start_x", "start_y", "start_nx", "start_ny"),
                    ("end_x", "end_y", "end_nx", "end_ny")):
                sxv, syv = info.get(sx_key), info.get(sy_key)
                if _num(sxv) and _num(syv):
                    pxy = map_model.project(map_key, sxv, syv)
                    if pxy:
                        info2[nx_key], info2[ny_key] = pxy[0], pxy[1]
            out_dims[ckey] = info2
        # `available` now also requires this map to actually have a world in the
        # payload -- otherwise Hagga would inherit DD's availability and render a
        # countdown banner for storms that belong to another map.
        return {**live, "dimensions": out_dims,
                "available": bool(live.get("available")) and bool(out_dims)}
    except Exception as exc:
        logger.warning("sandstorm overlay projection failed: %s", exc)
        return live


@router.get("/portal/maps/{map_key}/live")
async def portal_map_live(map_key: str):
    """ONE consolidated live-overlay feed for a map: the active spice blow, the
    sandworm tracker, AND the sandstorm ETA forecast in a single response, so the
    client polls once instead of running a separate timer per layer. Each layer is
    composed from its own process-wide TTL cache (spice ~90s, worms ~10s,
    sandstorm ~30s), so a fast client poll stays cheap -- the game-box is hit at
    most once per layer's TTL no matter how many players (or the admin panel)
    consume this. Reusable by the public site + admin. The private per-player
    overlay stays on /me (session-gated, not global)."""
    m = map_model.MAPS.get(map_key)
    if not m:
        raise HTTPException(404, "Unknown map")
    # spice + worms stay DD-only (has_spice); storms are now their own axis, so
    # Hagga gets the sandstorm layer without inheriting the spice/worm ones.
    has_spice = bool(m.get("has_spice"))
    has_storms = bool(m.get("has_storms"))
    if not (has_spice or has_storms):
        return JSONResponse({"spice": {"dimensions": {}}, "worms": {"dimensions": {}},
                             "sandstorm": {"dimensions": {}, "available": False}})
    spice, worms, sandstorm = await asyncio.gather(
        _spice_overlay(map_key) if has_spice else _no_overlay(),
        _worms_overlay(map_key) if has_spice else _no_overlay(),
        _sandstorm_overlay(map_key) if has_storms else _no_storm_overlay())
    return JSONResponse({"spice": spice, "worms": worms, "sandstorm": sandstorm})


@router.get("/portal/maps/{map_key}/spice")
async def portal_map_spice(map_key: str):
    """Live active-Large spice overlay (DD only). Kept as a standalone endpoint for
    any consumer that wants spice alone; the public map uses /live."""
    m = map_model.MAPS.get(map_key)
    if not m:
        raise HTTPException(404, "Unknown map")
    if not m.get("has_spice"):
        return JSONResponse({"dimensions": {}})
    return JSONResponse(await _spice_overlay(map_key))


@router.get("/portal/maps/{map_key}/worms")
async def portal_map_worms(map_key: str):
    """Live sandworm overlay (DD only). Kept standalone for any consumer that wants
    worms alone; the public map uses /live."""
    m = map_model.MAPS.get(map_key)
    if not m:
        raise HTTPException(404, "Unknown map")
    if not m.get("has_spice"):
        return JSONResponse({"dimensions": {}})
    return JSONResponse(await _worms_overlay(map_key))


@router.get("/portal/maps/{map_key}/sandstorm")
async def portal_map_sandstorm(map_key: str):
    """Live sandstorm ETA forecast (storm-bearing maps). Kept standalone for any
    consumer that wants the storm countdown alone; the public map uses /live."""
    m = map_model.MAPS.get(map_key)
    if not m:
        raise HTTPException(404, "Unknown map")
    if not m.get("has_storms"):
        return JSONResponse({"dimensions": {}, "available": False})
    return JSONResponse(await _sandstorm_overlay(map_key))


async def _player_counts(map_key: str, dim: int | None = None) -> dict:
    """Player counts for a map + server-wide online. PII-safe — counts only,
    never identities. Works for every map (not just the spice ones).

    `dim` selects ONE dimension of a multi-world map. Without it the figure is
    the whole map summed across dimensions, which is right for a map total but
    WRONG on a per-instance card: DD showed "2 here" on both the PvE and PvP
    cards while only dim 0 held those two players, and Hagga showed the combined
    dim0+dim1+dim2 total on all three of its instance cards (2026-08-29).
    Callers rendering a single instance must pass its dim."""
    m = map_model.MAPS.get(map_key)
    if not m:
        return {"available": False}
    try:
        from routers.dune import _fetch_dune_status
        status = await _fetch_dune_status()
    except Exception as exc:
        logger.warning("player counts fetch failed: %s", exc)
        return {"available": False}
    if not isinstance(status, dict):
        return {"available": False}
    name = m.get("name")
    if dim is None:
        # Whole-map total. One map can span several partitions (dual-DD 8+31,
        # dual-Hagga 1+32+33); each is its own farm_state row under the same
        # friendly name, so sum them.
        map_players = sum(
            (e.get("players") or 0)
            for e in (status.get("maps") or [])
            if e.get("name") == name
        )
    else:
        # One instance only. Falls back to the whole-map total if the feed has
        # no per-dimension rows (older relay), so the counter degrades to the
        # previous behaviour rather than silently reading zero.
        rows = [d for d in (status.get("map_dims") or []) if d.get("name") == name]
        if rows:
            map_players = sum((d.get("players") or 0)
                              for d in rows if d.get("dim") == dim)
        else:
            map_players = sum((e.get("players") or 0)
                              for e in (status.get("maps") or [])
                              if e.get("name") == name)
    return {
        "available": True,
        "map_name": name,
        "map_dim": dim,
        "map_players": map_players,
        "server_players": status.get("online_players") or 0,
    }


@router.get("/portal/maps/{map_key}/players")
async def portal_map_players(map_key: str, dim: int | None = None):
    """Live player counts for a map (this-map total + server-wide online). Polled
    by every map page (not gated on spice) for the on-map player counter.

    Pass ?dim=N to count ONE dimension of a multi-world map (DD PvE/PvP,
    Hagga Habbanya/Kulon/Amtal). Omitted = whole-map total, unchanged."""
    if map_key not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    return JSONResponse(await _player_counts(map_key, dim))


# --------------------------------------------------------------------------- #
# M-LIVE — background cache refresher + SSE live stream                        #
# Removes the client-poll latency stage: <web-host> polls the relay itself at  #
# each cache's EXISTING TTL cadence (so caches are always warm) and pushes     #
# changes to the browser over SSE. /live + /players stay untouched as the      #
# poll fallback. Public data only — same payloads, nothing session-gated.      #
# --------------------------------------------------------------------------- #

# Cadences MIRROR each feed's existing ttl_cache TTL (routers/dune.py). The
# refresher moves WHO triggers the read-through refresh (a server timer instead
# of the first unlucky visitor), not how often: calling a ttl_cache-wrapped fn
# is a no-op while its entry is fresh, so the relay/game box can never be hit
# faster than the TTL no matter what this loop does. `players` is the dune
# status feed behind /portal/maps/{key}/players (60s ttl). The per-account 8s
# player_map feed is session-gated (/me) and intentionally NOT warmed here —
# background-polling accounts nobody is watching would ADD game-box load.
_LIVE_REFRESH_FEEDS = (
    ("worms", 10.0),
    ("sandstorm", 30.0),
    ("spice", 90.0),
    ("players", 60.0),
)
_live_refresh_tasks: list = []
_live_refresh_started = False


async def _live_refresh_fetch(feed: str):
    from routers.dune import (
        _fetch_dune_status,
        cached_sandstorm,
        cached_spice_active,
        cached_worms,
    )
    fn = {"worms": cached_worms, "sandstorm": cached_sandstorm,
          "spice": cached_spice_active, "players": _fetch_dune_status}[feed]
    await fn()


async def _live_refresh_loop(feed: str, interval: float):
    """One feed's keep-warm timer. Best-effort; a failed tick is logged and
    retried next tick (ttl_cache serves graceful-stale to readers meanwhile)."""
    while True:
        try:
            await _live_refresh_fetch(feed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("live refresher %s: %s", feed, exc)
        await asyncio.sleep(interval)


_live_refresher_disabled_logged = False


def _ensure_live_refresher():
    """Start the refresher tasks exactly once per process. Wired to router
    startup below; ALSO called lazily from the live endpoints as a fallback so
    the refresher still runs if a fastapi/starlette combination skips router
    on_event handlers under the custom app lifespan (main.py stays untouched)."""
    from config import RELAY_URL as _relay_url
    if not _relay_url:
        # No relay, no live feeds. Say so once; this is called per request.
        global _live_refresher_disabled_logged
        if not _live_refresher_disabled_logged:
            logger.info("live refresher disabled: relay not configured")
            _live_refresher_disabled_logged = True
        return
    global _live_refresh_started
    if _live_refresh_started:
        return
    _live_refresh_started = True
    for feed, interval in _LIVE_REFRESH_FEEDS:
        _live_refresh_tasks.append(
            asyncio.create_task(_live_refresh_loop(feed, interval)))
    logger.info("live refresher started (%s)",
                ", ".join(f"{f}@{int(i)}s" for f, i in _LIVE_REFRESH_FEEDS))


@router.on_event("startup")
async def _live_refresher_startup():
    _ensure_live_refresher()


@router.on_event("shutdown")
async def _live_refresher_shutdown():
    global _live_refresh_started
    for t in _live_refresh_tasks:
        t.cancel()
    for t in _live_refresh_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    _live_refresh_tasks.clear()
    _live_refresh_started = False


# SSE connection limits: streams are long-lived so they need their own caps
# (the request-rate limiters don't model held-open connections). Per-IP covers
# a runaway tab loop; total covers the process (admin-backend is single-proc).
_STREAM_MAX_PER_IP = 4
_STREAM_MAX_TOTAL = 64
_STREAM_POLL_S = 2.0          # cache re-read cadence (process-local, no relay hit)
_STREAM_KEEPALIVE_S = 25.0
_stream_conns_by_ip: dict[str, int] = {}
_stream_conns_total = 0


async def _stream_feed_payloads(map_key: str, has_spice: bool,
                                has_storms: bool = False) -> dict:
    """Current payload per SSE feed for a map — the EXACT same objects /live
    and /players serve, so the stream and the poll fallback are interchangeable
    to the client. Reads process-local TTL caches only (kept warm by the
    refresher), so calling this every _STREAM_POLL_S is cheap and never adds
    relay/game-box load beyond the existing TTL cadences."""
    if has_spice:
        spice, worms, sandstorm, players = await asyncio.gather(
            _spice_overlay(map_key), _worms_overlay(map_key),
            _sandstorm_overlay(map_key), _player_counts(map_key))
        return {"spice": spice, "worms": worms,
                "sandstorm": sandstorm, "players": players}
    if has_storms:
        # Storms without spice (Hagga): only the feeds this map actually has, so
        # the client never receives an empty spice/worms frame it would redraw.
        sandstorm, players = await asyncio.gather(
            _sandstorm_overlay(map_key), _player_counts(map_key))
        return {"sandstorm": sandstorm, "players": players}
    return {"players": await _player_counts(map_key)}


@router.get("/portal/maps/{map_key}/stream")
async def portal_map_stream(request: Request, map_key: str):
    """SSE live transport for the map overlays (M-LIVE). Multiplexes named
    events spice / worms / sandstorm / players (storm-only maps: sandstorm +
    players; maps with neither: players only -- same gates as /live).
    Contract (agreed with the map client):
    - On connect: one event per feed immediately (current snapshot).
    - After that: an event fires ONLY when its payload changed (compare against
      the last-sent serialization); silence means no change.
    - id: monotonic per-connection counter shared across event types.
      Last-Event-ID is ignored — reconnect just replays the full snapshot.
    - ': keepalive' comment every 25s idle so proxies keep the stream open.
    - One final 'event: error' + {"available": false} then close on a fatal
      mid-stream failure (same convention as /api/dune/positions/stream);
      the client EventSource onerror falls back to the /live + /players polls.
    Public data only — identical payloads to /live + /players, nothing
    session-gated. 429 over the per-IP/total caps (non-200 => client falls
    back to polling)."""
    m = map_model.MAPS.get(map_key)
    if not m:
        raise HTTPException(404, "Unknown map")
    global _stream_conns_total
    ip = client_ip(request)
    if (_stream_conns_total >= _STREAM_MAX_TOTAL
            or _stream_conns_by_ip.get(ip, 0) >= _STREAM_MAX_PER_IP):
        raise HTTPException(429, "Too many live-stream connections")
    _ensure_live_refresher()

    has_spice = bool(m.get("has_spice"))
    has_storms = bool(m.get("has_storms"))

    async def gen():
        # Slot accounting lives INSIDE the generator: reserve on first
        # iteration, release in finally. A generator that is never started
        # (request cancelled between handler return and first body iteration)
        # never reserves, so a slot cannot leak for the life of the process.
        # The cap CHECK above runs pre-reservation, so simultaneous handshakes
        # can briefly overshoot the cap by their in-flight count — accepted
        # over a permanent leak.
        global _stream_conns_total
        _stream_conns_total += 1
        _stream_conns_by_ip[ip] = _stream_conns_by_ip.get(ip, 0) + 1
        event_id = 0
        last_body: dict[str, str] = {}
        last_send = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payloads = await _stream_feed_payloads(map_key, has_spice,
                                                          has_storms)
                except Exception as exc:
                    logger.warning("map stream %s: feed read failed: %s",
                                   map_key, exc)
                    yield b'event: error\ndata: {"available": false}\n\n'
                    break
                frames = []
                for feed, payload in payloads.items():
                    body = json.dumps(payload, separators=(",", ":"),
                                      sort_keys=True)
                    if last_body.get(feed) == body:
                        continue
                    last_body[feed] = body
                    event_id += 1
                    frames.append(f"id: {event_id}\nevent: {feed}\ndata: {body}\n\n")
                if frames:
                    last_send = time.monotonic()
                    yield "".join(frames).encode()
                elif time.monotonic() - last_send >= _STREAM_KEEPALIVE_S:
                    last_send = time.monotonic()
                    yield b": keepalive\n\n"
                await asyncio.sleep(_STREAM_POLL_S)
        finally:
            _stream_conns_total -= 1
            n = _stream_conns_by_ip.get(ip, 1) - 1
            if n <= 0:
                _stream_conns_by_ip.pop(ip, None)
            else:
                _stream_conns_by_ip[ip] = n

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# Waypoints — private per-player pins + notes on the map                      #
# PRIVATE: account_id always comes from the session, never from the client.   #
# The table lives in admin.db (NOT dune.*), same pattern as guild_recruiting. #
# --------------------------------------------------------------------------- #

_WP_MAX = 50          # hard cap: waypoints per player per map
_WP_NOTE_MAX = 200    # note char cap


@router.get("/portal/maps/{map_key}/waypoints")
async def portal_map_waypoints_list(request: Request, map_key: str):
    """Return the logged-in player's waypoints for this map as JSON.
    401 when unauthenticated."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    if map_key not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    account_id = int(session.get("aid") or 0)
    if account_id <= 0:
        return JSONResponse({"authenticated": False}, status_code=401)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, nx, ny, note, created_at FROM portal_map_waypoints "
            "WHERE account_id = ? AND map_key = ? ORDER BY id",
            (account_id, map_key),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({
        "authenticated": True, "map": map_key,
        "waypoints": [{"id": r["id"], "nx": r["nx"], "ny": r["ny"],
                       "note": r["note"] or "", "created_at": r["created_at"]}
                      for r in rows],
    })


@router.post("/portal/maps/{map_key}/waypoints")
async def portal_map_waypoints_add(request: Request, map_key: str):
    """Add a waypoint for the logged-in player. JSON body: {nx, ny, note?}.
    Returns the new waypoint row. 401 when unauthenticated."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    if map_key not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    account_id = int(session.get("aid") or 0)
    if account_id <= 0:
        return JSONResponse({"authenticated": False}, status_code=401)

    # CSRF: waypoints mutate state, so require the CSRF header.
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")

    try:
        nx = float(body.get("nx") or 0)
        ny = float(body.get("ny") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "nx/ny must be numbers")

    # Clamp to 0..VIEW (1000) per table CHECK constraint.
    from map_model import VIEW as MAP_VIEW
    nx = max(0.0, min(MAP_VIEW, nx))
    ny = max(0.0, min(MAP_VIEW, ny))
    note = str(body.get("note") or "").strip()[:_WP_NOTE_MAX]

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM portal_map_waypoints WHERE account_id = ? AND map_key = ?",
            (account_id, map_key),
        ).fetchone()[0]
        if existing >= _WP_MAX:
            return JSONResponse(
                {"ok": False, "error": f"Waypoint limit ({_WP_MAX}) reached for this map."},
                status_code=400,
            )
        cur = conn.execute(
            "INSERT INTO portal_map_waypoints (account_id, map_key, nx, ny, note) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id, nx, ny, note, created_at",
            (account_id, map_key, round(nx, 1), round(ny, 1), note or None),
        )
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    return JSONResponse({
        "ok": True,
        "waypoint": {"id": row["id"], "nx": row["nx"], "ny": row["ny"],
                     "note": row["note"] or "", "created_at": row["created_at"]},
    })


@router.delete("/portal/maps/{map_key}/waypoints/{waypoint_id:int}")
async def portal_map_waypoints_delete(request: Request, map_key: str, waypoint_id: int):
    """Delete one of the logged-in player's waypoints. 401 when unauthenticated,
    404 when the waypoint doesn't belong to this player."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    if map_key not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    account_id = int(session.get("aid") or 0)
    if account_id <= 0:
        return JSONResponse({"authenticated": False}, status_code=401)

    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    conn = get_db()
    try:
        result = conn.execute(
            "DELETE FROM portal_map_waypoints WHERE id = ? AND account_id = ? AND map_key = ?",
            (waypoint_id, account_id, map_key),
        )
        if result.rowcount == 0:
            conn.close()
            raise HTTPException(404, "Waypoint not found")
        conn.commit()
    finally:
        conn.close()

    return JSONResponse({"ok": True, "deleted": waypoint_id})


@router.patch("/portal/maps/{map_key}/waypoints/{waypoint_id:int}")
async def portal_map_waypoints_rename(request: Request, map_key: str, waypoint_id: int):
    """Rename one of the logged-in player's waypoints. JSON body: {note} ONLY —
    coords in the body are REJECTED (400), position edits stay out of scope.
    Note validation is identical to the POST route; ownership check mirrors
    DELETE (401 when unauthenticated, 404 when the waypoint doesn't belong to
    this player). Returns {ok, waypoint} with the updated row."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    if map_key not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    account_id = int(session.get("aid") or 0)
    if account_id <= 0:
        return JSONResponse({"authenticated": False}, status_code=401)

    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    # Position edits are out of scope — reject rather than silently ignore so a
    # client can never believe it moved a pin.
    if "nx" in body or "ny" in body:
        raise HTTPException(400, "Waypoint position cannot be edited")
    note = str(body.get("note") or "").strip()[:_WP_NOTE_MAX]

    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE portal_map_waypoints SET note = ? "
            "WHERE id = ? AND account_id = ? AND map_key = ? "
            "RETURNING id, nx, ny, note, created_at",
            (note or None, waypoint_id, account_id, map_key),
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            raise HTTPException(404, "Waypoint not found")
        conn.commit()
    finally:
        conn.close()

    return JSONResponse({
        "ok": True,
        "waypoint": {"id": row["id"], "nx": row["nx"], "ny": row["ny"],
                     "note": row["note"] or "", "created_at": row["created_at"]},
    })


@router.get("/portal/maps/{map_key}/me")
async def portal_map_me(request: Request, map_key: str):
    """Per-player overlay for the logged-in player on this map: their own
    position (last-known when offline), base totems, and owned vehicles. PRIVATE:
    served ONLY for the caller's own session-bound account — never public, never
    an arbitrary account_id. 401 when unauthenticated so the map JS hides the
    overlay control. Coordinates are normalized server-side with the same map
    calibration as the static markers, so they plot in lockstep."""
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"authenticated": False}, status_code=401)
    if map_key not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    account_id = int(session.get("aid") or 0)
    if account_id <= 0:
        return JSONResponse({"authenticated": False}, status_code=401)

    try:
        from routers.dune import cached_player_map
        raw = await cached_player_map(account_id)
    except Exception as exc:
        logger.warning("portal_map_me: live fetch failed: %s", exc)
        return JSONResponse({"authenticated": True, "available": False,
                             "map": map_key})

    if not isinstance(raw, dict) or not raw.get("available"):
        return JSONResponse({"authenticated": True, "available": False,
                             "map": map_key})

    def _norm(mk):
        """Filter a marker to this map + attach normalized coords. None if the
        marker is on a different map or carries no position."""
        if map_model.game_map_to_key(mk.get("map")) != map_key:
            return None
        x, y = mk.get("x"), mk.get("y")
        if x is None or y is None:
            return None
        nxy = map_model.project(map_key, x, y)
        if not nxy:
            return None
        return nxy

    self_out = None
    sm = raw.get("self") or None
    if sm:
        nxy = _norm(sm)
        if nxy:
            self_out = {"nx": nxy[0], "ny": nxy[1],
                        "dim": sm.get("dim"), "part": sm.get("part"),
                        "online": bool(sm.get("online"))}

    bases_out = []
    for b in (raw.get("bases") or []):
        nxy = _norm(b)
        if not nxy:
            continue
        bases_out.append({"nx": nxy[0], "ny": nxy[1], "dim": b.get("dim"),
                          "part": b.get("part"),
                          "kind": b.get("kind") or "base", "name": b.get("name")})

    vehicles_out = []
    for v in (raw.get("vehicles") or []):
        nxy = _norm(v)
        if not nxy:
            continue
        cls = v.get("class") or ""
        vehicles_out.append({
            "nx": nxy[0], "ny": nxy[1], "dim": v.get("dim"),
            "part": v.get("part"),
            "name": _friendly_vehicle(cls),
            # container_icons.icon_for keys vehicles off a "Vehicle:" prefix.
            "icon": _container_icon("Vehicle:" + cls) if cls else None,
        })

    return JSONResponse({
        "authenticated": True,
        "available": True,
        "map": map_key,
        "self": self_out,
        "bases": bases_out,
        "vehicles": vehicles_out,
    })


def _house_base(house_name: str) -> str:
    """DA_HouseNovebruns -> 'Novebruns'. Falls back to the raw name."""
    base = house_name or ""
    if base.startswith("DA_House"):
        base = base[len("DA_House"):]
    elif base.startswith("DA_"):
        base = base[len("DA_"):]
    return base.strip() or (house_name or "")


def _friendly_house(house_name: str) -> str:
    """DA_HouseNovebruns -> 'House Novebruns'. Falls back to the raw name."""
    if not house_name:
        return "Unknown house"
    base = _house_base(house_name)
    return f"House {base}" if base else house_name


def _house_monogram(house_name: str) -> str:
    """Short heraldic placeholder for a house tile until real crests land:
    first three letters of the house base name, e.g. Novebruns -> 'NOV'."""
    base = _house_base(house_name)
    return (base[:3] or "?").upper()


def _shape_ladder(raw_ladder: Optional[dict]) -> Optional[dict]:
    """Current-term reward ladder for one house (the in-game board view):
    goal + this player's contribution + threshold->reward rows with reliable
    catalog quantities and a 'reached' flag (contribution >= threshold)."""
    if not raw_ladder:
        return None
    goal = int(raw_ladder.get("goal") or 0)
    contrib = int(raw_ladder.get("my_contribution") or 0)
    rewards = []
    for r in (raw_ladder.get("rewards") or []):
        tid = r.get("template_id") or ""
        amount = int(r.get("amount") or 0)
        threshold = int(r.get("threshold") or 0)
        is_solari = tid == "SolarisCoin"
        rewards.append({
            "threshold": threshold,
            "threshold_display": f"{threshold:,}",
            "name": "Solari" if is_solari else _ITEM_NAMES.lookup_or_synthesize(tid),
            "amount": amount,
            "amount_display": f"{amount:,}",
            "is_solari": is_solari,
            "reached": contrib >= threshold,
        })
    return {
        "goal": goal,
        "goal_display": f"{goal:,}",
        "my_contribution": contrib,
        "my_contribution_display": f"{contrib:,}",
        "pct": min(100, round(contrib / goal * 100)) if goal > 0 else 0,
        "reached_count": sum(1 for r in rewards if r["reached"]),
        "rewards": rewards,
    }


def _shape_landsraad_for_render(payload: dict) -> dict:
    """Turn the raw relay payload (data only) into a display-ready context:
    friendly house + item names, comma-formatted numbers, summary buckets,
    and the oldest-reward age. Player-facing presentation lives here, not in
    the lastsietch-dune data script (data/presentation split)."""
    available = bool(payload.get("available"))
    raw_summary = payload.get("summary") or {}
    raw_houses = payload.get("houses") or []

    schematic_lines = 0
    swatch_lines = 0
    other_lines = 0
    houses = []
    for h in raw_houses:
        items = []
        for it in (h.get("items") or []):
            tid = it.get("template_id") or ""
            amount = it.get("amount") or 0
            if "Schematic" in tid:
                schematic_lines += 1
            elif "Swatch" in tid:
                swatch_lines += 1
            else:
                other_lines += 1
            items.append({
                "name": _ITEM_NAMES.lookup_or_synthesize(tid),
                "amount": amount,
            })
        solari = int(h.get("solari") or 0)
        houses.append({
            "name": _friendly_house(h.get("house_name") or ""),
            "raw": h.get("house_name") or "",
            "rep_location": HOUSE_REP_LOCATIONS.get(h.get("house_name") or ""),
            "solari": solari,
            "solari_display": f"{solari:,}" if solari else None,
            "line_count": int(h.get("line_count") or 0),
            "items": items,
        })

    # Collected-reward history (amount-0 rows the rep no longer shows), grouped
    # by house, newest first. Display only (no amount survives a full withdraw).
    now = datetime.now(timezone.utc)
    claimed_by_raw = {}
    for c in (payload.get("claimed") or []):
        raw = c.get("house_name") or ""
        c_items = []
        for it in (c.get("items") or []):
            tid = it.get("template_id") or ""
            ep = it.get("claimed_epoch")
            days = None
            if ep:
                try:
                    days = max(0, (now - datetime.fromtimestamp(int(ep), timezone.utc)).days)
                except Exception:
                    days = None
            c_items.append({
                "name": "Solari" if tid == "SolarisCoin" else _ITEM_NAMES.lookup_or_synthesize(tid),
                "claimed_days": days,
            })
        claimed_by_raw[raw] = c_items

    total_solari = int(raw_summary.get("total_solari") or 0)
    oldest_epoch = raw_summary.get("oldest_epoch")
    oldest_days = None
    if oldest_epoch:
        try:
            delta = datetime.now(timezone.utc) - datetime.fromtimestamp(int(oldest_epoch), timezone.utc)
            oldest_days = max(0, delta.days)
        except Exception:
            oldest_days = None

    summary = {
        "total_lines": int(raw_summary.get("total_lines") or 0),
        "total_solari": total_solari,
        "total_solari_display": f"{total_solari:,}",
        "houses_with_rewards": int(raw_summary.get("houses_with_rewards") or 0),
        "schematic_lines": schematic_lines,
        "swatch_lines": swatch_lines,
        "other_lines": other_lines,
        "oldest_days": oldest_days,
    }

    # Full 25-house board (in-game Landsraad layout): every house gets a tile,
    # reward data merged onto the ones the player has pending. Stable alpha order
    # so tile positions don't jump between visits. Reward tiles render highlighted
    # with a count badge; empty houses render dimmed (faithful to the board).
    raw_ladders = payload.get("ladders") or {}
    rewards_by_raw = {h["raw"]: h for h in houses}
    board = []
    for raw in sorted(HOUSE_REP_LOCATIONS, key=lambda r: _house_base(r).lower()):
        rh = rewards_by_raw.get(raw)
        items = rh["items"] if rh else []
        solari = rh["solari"] if rh else 0
        line_count = rh["line_count"] if rh else 0
        board.append({
            "raw": raw,
            "name": _friendly_house(raw),
            "short": _house_base(raw),
            "monogram": _house_monogram(raw),
            "crest": _house_crest(raw),
            "rep_location": HOUSE_REP_LOCATIONS.get(raw),
            "has_rewards": bool(line_count),
            "reward_count": line_count,
            "item_count": len(items),
            "solari": solari,
            "solari_display": f"{solari:,}" if solari else None,
            "items": items,
            "claimed": claimed_by_raw.get(raw, []),
            "ladder": _shape_ladder(raw_ladders.get(raw)),
        })

    return {"available": available, "summary": summary, "houses": houses, "board": board}


async def _load_landsraad(account_id: int) -> Optional[dict]:
    """Best-effort fetch + shape of a player's unclaimed Landsraad rewards.
    Returns None on any failure so callers can degrade gracefully."""
    try:
        # Fast path: local mirror (self-gates on flag + staleness; None on miss).
        payload = mirror.get_section(account_id, "landsraad")
        if payload is None:
            from routers.dune import cached_landsraad_rewards
            payload = await cached_landsraad_rewards(account_id)
        return _shape_landsraad_for_render(payload or {})
    except Exception as exc:
        logger.warning("portal: landsraad fetch failed: %s", exc)
        return None


# The Landsraad board is contested by the two great houses only. (dune.factions:
# 1 Atreides, 2 Harkonnen, 3 None, 4 Smuggler.) Order = render order on the rails
# (Atreides left, Harkonnen right), matching the in-game LANDSRAAD tab.
_BOARD_FACTIONS = [(1, "Atreides", "atreides"), (2, "Harkonnen", "harkonnen")]


def _faction_meta(faction_id) -> Optional[dict]:
    """Map a faction id to its board {id, name, slug}, or None for the
    unaligned/smuggler/None ids (which never win a great-house tile)."""
    try:
        fid = int(faction_id)
    except (TypeError, ValueError):
        return None
    for i, name, slug in _BOARD_FACTIONS:
        if i == fid:
            return {"id": i, "name": name, "slug": slug}
    return None


def _shape_board_ladder(rewards: list, my_contribution: Optional[int], goal: int) -> list:
    """Shape a tile's term reward ladder (threshold -> reward) for display.
    Term-global (no per-player data) except the optional `reached` flag, which is
    set from the session player's own contribution when signed in."""
    out = []
    for r in (rewards or []):
        tid = r.get("template_id") or ""
        amount = int(r.get("amount") or 0)
        threshold = int(r.get("threshold") or 0)
        is_solari = tid == "SolarisCoin"
        row = {
            "threshold": threshold,
            "threshold_display": f"{threshold:,}",
            "name": "Solari" if is_solari else _ITEM_NAMES.lookup_or_synthesize(tid),
            "amount": amount,
            "amount_display": f"{amount:,}",
            "is_solari": is_solari,
        }
        if my_contribution is not None:
            row["reached"] = my_contribution >= threshold
        out.append(row)
    return out


def _shape_landsraad_board(payload: dict, my_contrib_by_raw: Optional[dict] = None,
                           viewer_faction_id: Optional[int] = None) -> dict:
    """Turn the raw term-board relay payload (data only) into a display-ready
    context: friendly house + item names, crests, faction names/slugs, per-tile
    progress, great-house score, top-guild contributors, and the term countdown
    anchor. `my_contrib_by_raw` (raw house_name -> the signed-in player's
    contribution) is the ONLY per-player input; it overlays the session voting
    power + per-tile personal contribution. None => public board only.

    `viewer_faction_id` scopes per-tile PROGRESS to the viewer's own great house
    (Owner decision 2026-06-12: players must not see the opposing faction's exact
    numbers — no intel advantage). 1/2 => only that faction's row ships in the
    payload; anything else => no progress rows (goal + claim state only).
    Claim state, score, and contributors stay public (in-game parity)."""
    if not payload or not payload.get("available"):
        return {"available": False}
    term = payload.get("term") or {}
    if not term:
        return {"available": False}

    factions = payload.get("factions") or {}
    my_contrib_by_raw = my_contrib_by_raw or {}

    tiles = []
    for t in (payload.get("tiles") or []):
        raw = t.get("house_name") or ""
        goal = int(t.get("goal_amount") or 0)
        progress = t.get("progress") or {}
        # Progress rows scoped to the viewer's own great house only — the
        # opposing faction's numbers never leave the server. "leading" is
        # always False here (it would compare against the hidden faction).
        amounts = {fid: int((progress.get(str(fid)) or 0)) for fid, _, _ in _BOARD_FACTIONS}
        fac_rows = []
        for fid, name, slug in _BOARD_FACTIONS:
            if fid != viewer_faction_id:
                continue
            amt = amounts[fid]
            fac_rows.append({
                "slug": slug,
                "name": name,
                "amount": amt,
                "amount_display": f"{amt:,}",
                "pct": min(100, round(amt / goal * 100)) if goal > 0 else 0,
                "leading": False,
            })
        my_contribution = my_contrib_by_raw.get(raw)
        tiles.append({
            "board_index": int(t.get("board_index") or 0),
            "name": _friendly_house(raw),
            "short": _house_base(raw),
            "monogram": _house_monogram(raw),
            "crest": _house_crest(raw),
            "rep_location": HOUSE_REP_LOCATIONS.get(raw),
            "completed": bool(t.get("completed")),
            "sysselraad": bool(t.get("sysselraad")),
            "winner": _faction_meta(t.get("winning_faction_id")),
            "goal": goal,
            "goal_display": f"{goal:,}",
            "factions": fac_rows,
            "rewards": _shape_board_ladder(t.get("rewards"), my_contribution, goal),
            "my_contribution": my_contribution,
            "my_contribution_display": (f"{my_contribution:,}"
                                        if my_contribution is not None else None),
        })

    # Great-house score (decided tiles per faction) + top-guild contributors,
    # both keyed by faction slug for the rails.
    raw_score = payload.get("score") or {}
    raw_guilds = payload.get("top_guilds") or {}
    rails = []
    for fid, name, slug in _BOARD_FACTIONS:
        guilds = []
        for g in (raw_guilds.get(str(fid)) or []):
            amt = int(g.get("amount") or 0)
            guilds.append({"guild": g.get("guild") or "—",
                           "amount": amt, "amount_display": f"{amt:,}"})
        rails.append({
            "slug": slug,
            "name": name,
            "crest": _faction_crest(slug),
            "score": int(raw_score.get(str(fid)) or 0),
            "top_guilds": guilds,
        })

    # Session overlay: voting power = sum of the player's own contributions across
    # all tiles this term (None when not signed in / no contributions).
    voting_power = None
    if my_contrib_by_raw:
        total = sum(int(v or 0) for v in my_contrib_by_raw.values())
        voting_power = total
    contested = sum(1 for t in tiles if not t["winner"])

    # Recent term winners (newest first), resolved to slug for the history strip.
    history = []
    for fid in (payload.get("winner_history") or []):
        m = _faction_meta(fid)
        if m:
            history.append(m["slug"])

    return {
        "available": True,
        "term": {
            "term_id": term.get("term_id"),
            "end_utc": term.get("end_utc"),
            "start_utc": term.get("start_utc"),
            "test_term": bool(term.get("test_term")),
        },
        "tiles": tiles,
        "rails": rails,
        "winner_history": history,
        "contested": contested,
        "decided": len(tiles) - contested,
        "voting_power": voting_power,
        "voting_power_display": (f"{voting_power:,}" if voting_power is not None else None),
    }


async def _load_landsraad_board(my_contrib_by_raw: Optional[dict] = None,
                                viewer_faction_id: Optional[int] = None) -> Optional[dict]:
    """Best-effort fetch + shape of the term-global Landsraad board. Returns None
    on any failure so the page degrades to the rewards section. `my_contrib_by_raw`
    overlays the signed-in player's voting power + per-tile contribution;
    `viewer_faction_id` scopes the progress rows (see _shape_landsraad_board)."""
    try:
        from routers.dune import cached_landsraad_board
        payload = await cached_landsraad_board()
        shaped = _shape_landsraad_board(payload or {}, my_contrib_by_raw,
                                        viewer_faction_id)
        return shaped if shaped.get("available") else None
    except Exception as exc:
        logger.warning("portal: landsraad board fetch failed: %s", exc)
        return None


# Operator announcement banner: a single JSON file editable live (no restart) at
# /opt/lastsietch-admin/portal-announce.json (override with PORTAL_ANNOUNCE_FILE). Shape:
#   {"active": true, "type": "info|maintenance|alert", "title": "...",
#    "body": "...", "starts_utc": "...Z", "ends_utc": "...Z", "link": "/...",
#    "id": "2026-06-30-coriolis"}
# Set "active": false (or delete the file) to hide the banner.
_ANNOUNCE_FILE = _os.environ.get("PORTAL_ANNOUNCE_FILE") or _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "portal-announce.json")
_ANNOUNCE_TYPES = {"info", "maintenance", "alert"}


@router.get("/portal/announcement")
async def portal_announcement():
    """Public operator announcement (planned maintenance / notices) for the V2
    dashboard banner. Reads the JSON file each request so edits go live without a
    restart. Returns {active:false} when nothing is posted."""
    try:
        if _os.path.exists(_ANNOUNCE_FILE):
            with open(_ANNOUNCE_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict) and raw.get("active"):
                a_type = raw.get("type") if raw.get("type") in _ANNOUNCE_TYPES else "info"
                return JSONResponse({
                    "active": True,
                    "type": a_type,
                    "title": str(raw.get("title") or ""),
                    "body": str(raw.get("body") or ""),
                    "starts_utc": raw.get("starts_utc"),
                    "ends_utc": raw.get("ends_utc"),
                    "link": raw.get("link"),
                    "id": str(raw.get("id") or raw.get("title") or "announce"),
                }, headers={"Cache-Control": "public, max-age=30"})
    except Exception as exc:
        logger.warning("portal: announcement read failed: %s", exc)
    return JSONResponse({"active": False}, headers={"Cache-Control": "public, max-age=30"})


@router.get("/portal/landsraad/standings")
async def portal_landsraad_standings():
    """Public Landsraad term standings for the V2 dashboard: per-faction score
    (decided tiles), crest basenames, term countdown anchor, contested/decided
    counts, recent winners. NO per-player data and NO opposing-progress numbers
    (score + claim state are public per the board contract). Returns
    {available:false} when there is no active term or the relay is down."""
    board = await _load_landsraad_board()
    if not board:
        return JSONResponse({"available": False},
                            headers={"Cache-Control": "public, max-age=30"})
    rails = [
        {"slug": r["slug"], "name": r["name"], "crest": r.get("crest"), "score": r.get("score", 0)}
        for r in (board.get("rails") or [])
    ]
    return JSONResponse(
        {
            "available": True,
            "term": board.get("term"),
            "rails": rails,
            "contested": board.get("contested"),
            "decided": board.get("decided"),
            "winner_history": board.get("winner_history") or [],
        },
        headers={"Cache-Control": "public, max-age=30"},
    )


async def _load_progress(account_id: int) -> Optional[dict]:
    """Best-effort fetch of a player's character + economy stats for the account
    dashboard card. Returns None on any failure so the card degrades gracefully."""
    try:
        payload = mirror.get_section(account_id, "progress")
        if payload is None:
            from routers.dune import cached_player_progress
            payload = await cached_player_progress(account_id)
        if payload and payload.get("available"):
            return payload
    except Exception as exc:
        logger.warning("portal: progress fetch failed: %s", exc)
    return None


# Five specialization tracks, each capped at level 100; 41 keystones per track
# = 205 total (verified against the live skill tree). Specialization XP is a
# separate system from character XP (two separate XP pools).
_SPEC_LEVEL_CAP = 100
_SPEC_KEYSTONES_TOTAL = 205

# Faction rank tiers: (rank, cumulative standing threshold, name). Game uses >=.
# Ranks 0-5 named, 6-19 unnamed (render "Tier N"), 20 faction-specific (Envoy /
# Enforcer). Thresholds verified against the live faction table.
_FACTION_TIERS = [
    (0, 0, "Outsider"), (1, 100, "Mercenary"), (2, 250, "Recruit"),
    (3, 500, "Contractor"), (4, 1000, "Agent"), (5, 2000, "House Operator"),
    (6, 2225, None), (7, 2525, None), (8, 2900, None), (9, 3350, None),
    (10, 3875, None), (11, 4475, None), (12, 5150, None), (13, 5900, None),
    (14, 6725, None), (15, 7625, None), (16, 8600, None), (17, 9650, None),
    (18, 10775, None), (19, 11975, None), (20, 12475, None),
]
_FACTION_RANK20_NAME = {"Atreides": "Envoy", "Harkonnen": "Enforcer"}


def _faction_rank(reputation: int, faction_name: Optional[str]) -> dict:
    """Map numeric standing to its rank tier + progress toward the next rank."""
    rep = int(reputation)
    cur, nxt = _FACTION_TIERS[0], None
    for i, tier in enumerate(_FACTION_TIERS):
        if rep >= tier[1]:
            cur = tier
            nxt = _FACTION_TIERS[i + 1] if i + 1 < len(_FACTION_TIERS) else None
        else:
            break
    rank, cur_thr, name = cur
    if name is None:
        name = _FACTION_RANK20_NAME.get(faction_name or "", "Envoy") if rank == 20 else f"Tier {rank}"
    out = {"rank": rank, "rank_name": name, "standing": rep, "at_max": nxt is None, "pct": 100}
    if nxt is not None:
        span = nxt[1] - cur_thr
        out["pct"] = max(0, min(100, round((rep - cur_thr) / span * 100))) if span > 0 else 100
        out["to_next"] = nxt[1] - rep
        out["next_rank"] = nxt[0]
    return out


async def _load_specializations(account_id: int) -> Optional[dict]:
    """Best-effort fetch of a player's specialization tracks (level/xp per track)
    + owned-keystone count for the account card. Returns None on any failure so
    the card degrades gracefully (it simply does not render)."""
    try:
        payload = mirror.get_section(account_id, "specializations")
        if payload is None:
            from routers.dune import cached_player_specializations
            payload = await cached_player_specializations(account_id)
    except Exception as exc:
        logger.warning("portal: specializations fetch failed: %s", exc)
        return None
    if not payload or not payload.get("available"):
        return None
    tracks = []
    for name, v in (payload.get("spec_tracks") or {}).items():
        try:
            lvl_f = float(v.get("level") or 0)
        except (TypeError, ValueError):
            lvl_f = 0.0
        lvl = int(lvl_f)
        tracks.append({
            "name": name,
            "level": lvl,
            # bar shows progress toward the level cap (overall track maturity)
            "pct": max(0, min(100, round(lvl_f / _SPEC_LEVEL_CAP * 100))),
            "xp": int(v.get("xp") or 0),
            # per-track glyph (spec-combat / spec-crafting / ...), None if absent
            "icon": _stat_icon(f"spec-{name.lower()}"),
        })
    if not tracks:
        return None
    tracks.sort(key=lambda t: (-t["level"], t["name"]))
    return {
        "tracks": tracks,
        "level_cap": _SPEC_LEVEL_CAP,
        "keystones_owned": len(payload.get("owned_keystone_ids") or []),
        "keystones_total": _SPEC_KEYSTONES_TOTAL,
    }


async def _load_journey(account_id: int) -> Optional[dict]:
    """Best-effort fetch + classification of the player's journey/exploration
    tags (dune.admin_read_player_tags) into a progress summary for the account
    card: story arcs completed, points of interest discovered, big moments, and
    codex entries. Returns None on any failure so the card degrades gracefully."""
    try:
        payload = mirror.get_section(account_id, "tags")
        if payload is None:
            from routers.dune import _cached_player_tags
            payload = await _cached_player_tags(str(account_id))
    except Exception as exc:
        logger.warning("portal: journey tags fetch failed: %s", exc)
        return None
    if not payload or not payload.get("available"):
        return None
    import journey_tags
    return journey_tags.summarize(payload.get("tags") or [])


# Equipment slot label by position_index (Dune equip order; 6+ = tools/utility)
# for the EQUIPPED inventory (inventory_type=1).
_EQUIP_SLOT_BY_POS = {0: "Head", 1: "Chest", 2: "Legs", 3: "Hands", 4: "Feet", 5: "Back"}


def _hotbar_slot_label(position) -> str:
    """Hotbar (inventory_type=15) position -> display label. The portal bar
    only renders positions 0-7 (1-indexed as "Slot 1".."Slot 8"); anything
    outside that range is carried but not bar-visible."""
    try:
        pos = int(position)
    except (TypeError, ValueError):
        return "Stowed"
    return f"Slot {pos + 1}" if 0 <= pos <= 7 else "Stowed"


_AUG_EFFECT_RANGE = re.compile(
    r"^(?P<stat>.+?)\s*(?P<lo>[-+]?\d+(?:\.\d+)?)%\s*-\s*(?P<hi>[-+]?\d+(?:\.\d+)?)%$")


def _resolve_augment_effect(effects, rolls):
    """This augment's ACTUAL value on this item, or None when it cannot be known.

    🔴 ONLY resolves a SINGLE-effect augment, and that restriction is the whole
    point. Rolls are positional against the effect list, but the game's order is
    its own asset order and does NOT match the catalogue's display order --
    confirmed in game 2026-08-03 against three augments, which produced three
    different permutations with no shared rule (Karpov 38 alone is [Damage,
    Accuracy, RateOfFire, ShieldDamage] against a catalogue that lists Accuracy
    first). With one effect there is one roll and no order to get wrong; with two
    or more, a label would attach a correct number to the WRONG stat, which is
    worse than no label because it looks authoritative.

    Value is linear in the roll (min + roll*(max-min)), verified against the
    in-game Augment Station to the displayed decimal on Damage (+58.2%),
    Accuracy (+23.7%) and Shield Damage (+18.9%). A flat effect ("Rate of Fire
    -20%") returns None: its printed text is already exact and the game ignores
    that roll slot entirely.

    Resolved HERE, not on the game host, because the reroll response rebuilds
    augments from NEW rolls carried alongside the PREVIOUS read's effect text --
    a value computed upstream would be stale exactly when the player is staring
    at it."""
    if not isinstance(effects, list) or len(effects) != 1:
        return None
    if not isinstance(rolls, list) or len(rolls) != 1:
        return None
    m = _AUG_EFFECT_RANGE.match(str(effects[0]).strip())
    if not m:
        return None
    try:
        lo, hi, roll = float(m.group("lo")), float(m.group("hi")), float(rolls[0])
    except (TypeError, ValueError):
        return None
    roll = max(0.0, min(1.0, roll))
    return "%s %+.1f%%" % (m.group("stat"), lo + roll * (hi - lo))


def _with_resolved(augs) -> list:
    """Attach `resolved` in place. For the paths that hand the reader's augment
    list straight through instead of going via _sanitize_augments."""
    if not isinstance(augs, list):
        return []
    for a in augs:
        if isinstance(a, dict):
            a["resolved"] = _resolve_augment_effect(a.get("effects"), a.get("rolls"))
    return augs


def _sanitize_augments(raw) -> list:
    """Coerce the reader's augment list into the shape the card expects.
    Fail soft: any malformed entry is dropped rather than raised, so a bad
    augment payload degrades to "no augments" instead of breaking the card."""
    out = []
    if not isinstance(raw, list):
        return out
    for aug in raw:
        if not isinstance(aug, dict):
            continue
        rolls = aug.get("rolls")
        effects = aug.get("effects")
        out.append({
            "name": aug.get("name") or "",
            "grade": aug.get("grade"),
            "rolls": rolls if isinstance(rolls, list) else [],
            "label": aug.get("label") or aug.get("name") or "",
            "effects": effects if isinstance(effects, list) else [],
            # This dict IS what the card sees; a field left out here is dropped
            # with no error anywhere.
            "resolved": _resolve_augment_effect(effects, rolls),
        })
    return out


async def _load_owned_augments(account_id: int) -> list:
    """Standalone augment consumables the player owns, for the swap picker.
    Reads the SAME cached payload as `_load_equipped` (so this costs no extra
    relay round trip) and the reader scopes it to exactly the inventories
    dune-augment.py will consume from. Empty list on any failure: the dialog
    then offers reroll only, which needs nothing owned, rather than advertising
    a swap that cannot complete."""
    try:
        from routers.dune import cached_player_equipped
        payload = await cached_player_equipped(account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: owned augments fetch failed: %s", exc)
        return []
    if not payload or not payload.get("available"):
        return []
    out = []
    for aug in (payload.get("owned_augments") or []):
        if not isinstance(aug, dict) or not aug.get("name"):
            continue
        try:
            count = int(aug.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        effects = aug.get("effects")
        out.append({
            "name": str(aug["name"]),
            "label": str(aug.get("label") or aug["name"]),
            "grade": aug.get("grade"),
            "count": count,
            "effects": [str(e) for e in effects] if isinstance(effects, list) else [],
        })
    return out


async def _load_equipped(account_id: int) -> Optional[list]:
    """Best-effort fetch of the player's worn + carried loadout for the 3D
    character stage: EQUIPPED gear (inventory_type=1, armour/tools) plus the
    HOTBAR (inventory_type=15, where weapons live, and their augments with
    them: an augmented weapon is invisible to an equipped-only read). Maps
    each row to {slot, name, template_id, category, quality, variant_id,
    swatch_id, item_id, source, inv_type, position, cur_dur, max_dur,
    augments} (name via name_lookups, category via market_categories).
    Returns None on any failure / empty so the stage falls back to the demo or
    coming-soon placeholder. Both inventories are RAM-fragile, so this
    reflects the last-OFFLINE loadout — same semantics as the other cards."""
    try:
        from routers.dune import cached_player_equipped
        payload = await cached_player_equipped(account_id)
    except Exception as exc:
        logger.warning("portal: equipped gear fetch failed: %s", exc)
        return None
    if not payload or not payload.get("available"):
        return None
    out = []
    for it in (payload.get("items") or []):
        tid = it.get("template_id") or ""
        if not tid:
            continue
        source = it.get("source") or "equipped"
        if source == "hotbar":
            slot = _hotbar_slot_label(it.get("position"))
        else:
            slot = _EQUIP_SLOT_BY_POS.get(it.get("position"), "Utility")
        out.append({
            "slot": slot,
            "name": _ITEM_NAMES.lookup_or_synthesize(tid),
            "template_id": tid,
            "category": market_categories.classify(tid),
            "quality": it.get("quality"),
            "variant_id": it.get("variant_id") or "",
            "swatch_id": it.get("swatch_id") or "",
            "item_id": it.get("item_id"),
            "source": source,
            "inv_type": it.get("inv_type"),
            "position": it.get("position"),
            "cur_dur": it.get("cur_dur") or "",
            "max_dur": it.get("max_dur") or "",
            "augments": _sanitize_augments(it.get("augments")),
        })
    return out or None


@router.get("/portal/landsraad")
async def portal_landsraad(request: Request):
    """Public player page: the signed-in player's own unclaimed Landsraad
    rewards, per house, with Solari + item breakdown. Scoped strictly to the
    OAuth-bound active account, so a player only ever sees their own."""
    session = get_portal_session(request)
    if not session:
        return RedirectResponse(url="/portal/", status_code=302)
    discord_id = portal_identity.actor_key(session)
    active_account_id = int(session.get("aid") or 0)

    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT character_name, discord_handle
                 FROM ls_account_links
                WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL""").replace("ls_account_links", portal_identity.link_table(conn)),
            (discord_id, active_account_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return _render_link_revoked(request)

    _touch_last_session(active_account_id)

    rewards = await _load_landsraad(active_account_id)

    # Session overlay for the live board: the player's own per-house contribution
    # this term, reused from the rewards ladders (no extra per-player relay call).
    my_contrib_by_raw = {}
    if rewards and rewards.get("board"):
        for b in rewards["board"]:
            lad = b.get("ladder")
            if lad is not None and b.get("raw"):
                my_contrib_by_raw[b["raw"]] = int(lad.get("my_contribution") or 0)

    # Viewer faction scopes the board's progress rows (own house only — no
    # opposing-faction intel). Mirror-first read; None => no progress rows.
    viewer_faction_id = None
    progress = await _load_progress(active_account_id)
    if progress:
        fac = progress.get("faction") or {}
        if fac.get("faction_id") in (1, 2):
            viewer_faction_id = int(fac["faction_id"])

    board = await _load_landsraad_board(my_contrib_by_raw or None, viewer_faction_id)

    ctx = {
        "active_character_name": row["character_name"],
        "rewards": rewards,
        "rewards_error": rewards is None,
        "board": board,
        "logout_post_url": "/portal/logout",
    }
    ctx.update(base_ctx(request, discord_handle=row["discord_handle"]))
    return templates.TemplateResponse(request, "portal/landsraad.html", ctx)


# ------------------------------------------------------- server -------------
# Public, anonymous, read-only server status page. Aggregates server-wide LIVE
# state (counts only — never per-player data) from the cached relay helpers and
# links out to the deep pages. NO login required (unlike most portal pages), NO
# controls, NO writes. Every feed is isolated so one dead source degrades a
# single card, never the whole page.

# Admin-editable events + next Deep Desert reset date live in this sidecar so
# copy changes need no code deploy (mirrors the other static/data sidecars).
_SERVER_EVENTS_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(__file__)), "static", "data",
    "server-events.json")

# Events list is rendered with these kinds; anything else falls back to "info".
_SERVER_EVENT_KINDS = frozenset({"info", "alert", "event", "maintenance"})


def _load_server_events() -> dict:
    """Read the admin-curated events + next-DD-reset + build label sidecar.
    Player-safe by construction (free-text copy admins write). Returns a safe
    default envelope on any read/parse failure so the card degrades gracefully."""
    try:
        with open(_SERVER_EVENTS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        events = []
        for e in (data.get("events") or []):
            if not isinstance(e, dict):
                continue
            kind = str(e.get("kind") or "info").strip().lower()
            if kind not in _SERVER_EVENT_KINDS:
                kind = "info"
            title = str(e.get("title") or "").strip()
            body = str(e.get("body") or "").strip()
            if title or body:
                events.append({"title": title, "body": body, "kind": kind})
        build_label = data.get("build_label")
        return {
            "available": True,
            "events": events,
            "next_dd_reset_utc": data.get("next_dd_reset_utc") or None,
            "build_label": str(build_label).strip() if build_label else None,
        }
    except Exception as exc:
        logger.warning("portal: server-events sidecar read failed: %s", exc)
        return {"available": False, "events": [],
                "next_dd_reset_utc": None, "build_label": None}


def _shape_server_world(world: dict, presence: dict) -> dict:
    """World-pulse 24h aggregates: subfief / structure / vehicle counters (from
    telemetry/world) joined with peak-concurrent + total play-hours (from the
    presence series). Counts only — no positions, no owners, no PII."""
    wc = world.get("counters") if isinstance(world.get("counters"), dict) else world
    world_ok = bool(world.get("available", True)) and isinstance(world, dict)
    pres_ok = bool(presence.get("available", True)) and isinstance(presence, dict)
    return {
        "available": world_ok or pres_ok,
        "window": "24h",
        "subfiefs": wc.get("subfiefs") if world_ok else None,
        "structures": wc.get("structures") if world_ok else None,
        "vehicles": wc.get("vehicles") if world_ok else None,
        "peak": presence.get("peak") if pres_ok else None,
        "play_hours": presence.get("play_hours") if pres_ok else None,
        # 24h concurrency series for the dashboard sparkline (counts only, no PII).
        "series": presence.get("series") if pres_ok else None,
    }


async def _load_server_overview() -> dict:
    """Build the player-safe view model for /portal/server. Counts only — no
    per-player data. All live reads route through the cached relay helpers, so
    public traffic is served from the in-process TTL cache and never amplifies
    SSH/DB load. Per-feed isolation: a failing feed yields its own
    {available: False} envelope, not an exception."""
    from routers.dune import (
        cached_status, cached_presence, cached_world_pulse,
        cached_last_funcom_push,
    )

    async def _safe(coro):
        try:
            val = await coro
            return val if isinstance(val, dict) else {"available": False}
        except Exception as exc:
            logger.warning("portal: server overview feed failed: %s", exc)
            return {"available": False}

    status, presence, world, funcom = await asyncio.gather(
        _safe(cached_status()),
        _safe(cached_presence("24h")),
        _safe(cached_world_pulse("24h")),
        _safe(cached_last_funcom_push()),
    )

    # Faction-blind public board: None/None => no per-house progress rows.
    board = await _load_landsraad_board(None, None)
    events = _load_server_events()

    bg = status.get("battlegroup") or {}
    status_view = {
        "available": bool(status.get("available")),
        "up": bool(status.get("available")) and not bg.get("error"),
        # Public nameplate fields (server title + region only; no ids/secrets).
        "name": bg.get("title"),
        "region": bg.get("region"),
        "online_players": status.get("online_players"),
        "maps": [
            {"name": m.get("name"), "players": m.get("players"),
             "up": bool(m.get("up")), "on_demand": bool(m.get("on_demand"))}
            for m in (status.get("maps") or [])
        ],
        "instances": status.get("instances") or {},
    }

    # Build/patch line — ONLY the admin-curated build label + the relative time
    # of the last Funcom settingsUpdate. sha256 / push-count / diff / bgd_rpc are
    # admin-only and never leave this function.
    build_view = {
        "build_label": events.get("build_label"),
        "settings_updated_utc": (funcom.get("last_ts")
                                 if funcom.get("available") is not False else None),
    }
    build_view["available"] = bool(build_view["build_label"]
                                   or build_view["settings_updated_utc"])

    return {
        "status": status_view,
        "build": build_view,
        "world": _shape_server_world(world, presence),
        "landsraad": board,
        "events": events,
    }


@router.get("/portal/server")
async def portal_server(request: Request):
    """Public, anonymous, read-only server status page. Renders for logged-out
    visitors (no session required). Aggregate counts only; links out to the
    deep pages (Landsraad, Maps, …) for detail."""
    overview = await _load_server_overview()
    ctx = {
        "status": overview["status"],
        "build": overview["build"],
        "world": overview["world"],
        "board": overview["landsraad"],
        "events": overview["events"],
        "active_nav": "server",
        "discord_invite_url": PORTAL_DISCORD_INVITE_URL,
    }
    ctx.update(base_ctx(request))
    return templates.TemplateResponse(request, "portal/server.html", ctx)


def _server_time_payload(now=None):
    from spice_candidates_acc import cycle_window

    now = now or datetime.now(timezone.utc)
    try:
        window = cycle_window(now)
    except Exception:
        window = None
    return {
        "available": True,
        "server_now_utc": now.astimezone(timezone.utc).isoformat(),
        "time_zone": "America/New_York",
        "coriolis": {"available": bool(window), **(window or {})},
    }


@router.get("/portal/server/time")
async def portal_server_time():
    return JSONResponse(_server_time_payload(), headers={"Cache-Control": "no-store"})


@router.get("/portal/server/overview")
async def portal_server_overview(request: Request):
    """JSON twin of /portal/server for the V2 SPA dashboard (Sietch Weather
    Station). Same player-safe view model, served from the cached relay helpers
    so public traffic never amplifies SSH/DB load. Anonymous, read-only."""
    overview = await _load_server_overview()
    now = datetime.now(timezone.utc)
    overview = {**overview, "clock": {"utc": now.isoformat(), "local_hour": now.astimezone().hour}}
    return JSONResponse(overview, headers={"Cache-Control": "public, max-age=20"})


# ------------------------------------------------------- guilds -------------
# Public guild directory + recruitment board. The guild roster + Landsraad
# contributions are READ-ONLY from the game DB (via routers/dune.cached_guilds);
# the "recruiting" flag + blurb + contact note are OUR metadata
# (portal_guild_recruiting in admin.db). The actual guild join always happens
# in-game — the portal only surfaces the directory + recruitment signalling.

import guild_recruiting
import lfg_seekers
import guild_join
import mailbox

# Faction render order + slugs (dune.factions: 1 Atreides, 2 Harkonnen,
# 3 None/unaligned, 4 Smuggler). Smuggler before the unaligned bucket.
_GUILD_FACTION_ORDER = [1, 2, 4, 3]
_GUILD_FACTION_SLUG = {1: "atreides", 2: "harkonnen", 3: "unaligned", 4: "smuggler"}
_GUILD_FACTION_FALLBACK = {1: "Atreides", 2: "Harkonnen", 3: "Unaligned", 4: "Smuggler"}
# Verified against the live data 2026-06-09: every guild has exactly ONE
# role_id=100 (the Leader); 50 = Officer; 1 = Member. (Higher number = higher
# rank — opposite of the first guess.)
_GUILD_ROLE_NAMES = {100: "Leader", 50: "Officer", 1: "Member"}
# Rank order for sorting members Leader -> Officer -> Member.
_GUILD_ROLE_RANK = {100: 0, 50: 1, 1: 2}
# Roles allowed to edit a guild's recruiting status / blurb (Leader + Officer).
_GUILD_EDIT_ROLES = (100, 50)


def _guild_role_name(role_id: int) -> str:
    return _GUILD_ROLE_NAMES.get(int(role_id or 1), "Member")


def _shape_guild(raw: dict, factions: dict, recruiting_meta: dict) -> dict:
    """Shape one raw guild record (from cached_guilds) + its portal recruiting
    metadata into the template view model. Sorts members Leader>Officer>Member."""
    gid = int(raw.get("guild_id"))
    fid = int(raw.get("guild_faction") or 0)
    members = sorted(
        raw.get("members") or [],
        key=lambda m: (_GUILD_ROLE_RANK.get(int(m.get("role_id") or 1), 9),
                       (m.get("char_name") or "").lower()),
    )
    for m in members:
        m["role_name"] = _guild_role_name(m.get("role_id"))
    leaders = [m["char_name"] for m in members
               if int(m.get("role_id") or 1) == 100 and m.get("char_name")]
    officers = [m["char_name"] for m in members
                if int(m.get("role_id") or 1) == 50 and m.get("char_name")]
    # Who to contact to join: leaders first, then officers; if a guild recorded
    # neither (small/partial roster), fall back to any named member.
    contacts = leaders + officers
    if not contacts:
        contacts = [m["char_name"] for m in members if m.get("char_name")][:3]

    meta = recruiting_meta.get(gid) or {}
    in_game_desc = (raw.get("guild_description") or "").strip()
    blurb = (meta.get("blurb") or "").strip()
    return {
        "guild_id": gid,
        "guild_name": raw.get("guild_name") or f"Guild {gid}",
        "faction_id": fid,
        "faction_name": _GUILD_FACTION_FALLBACK.get(fid) or factions.get(str(fid)) or "Unaligned",
        "faction_slug": _GUILD_FACTION_SLUG.get(fid, "unaligned"),
        "member_count": int(raw.get("member_count") or len(members)),
        "members": members,
        "leaders": leaders,
        "officers": officers,
        "contacts": contacts,
        "in_game_description": in_game_desc,
        "blurb": blurb,
        # What the card/detail shows as the description: in-game if the player
        # set one, else the portal-authored blurb, else nothing.
        "description": in_game_desc or blurb,
        "contact_note": (meta.get("contact_note") or "").strip(),
        "recruiting": bool(meta.get("recruiting")),
        # Structured Signal-Board filters (social layer Tier 2); None when unset.
        "recruiting_playstyle": meta.get("playstyle"),
        "recruiting_timezone": meta.get("timezone"),
        "recruiting_language": meta.get("language"),
        "recruiting_new_player_friendly": bool(meta.get("new_player_friendly")),
        "recruiting_discord_url": meta.get("discord_url"),
        "contrib_overall": float(raw.get("contrib_overall") or 0.0),
        "contrib_session": float(raw.get("contrib_session") or 0.0),
    }


async def _load_guilds() -> Optional[dict]:
    """The full guild directory shaped for the portal: guilds grouped by faction,
    overall + this-session Landsraad rankings, and the recruiting board. Returns
    None if the live read is unavailable (page renders a graceful fallback)."""
    try:
        from routers.dune import cached_guilds
        raw = await cached_guilds()
    except Exception:
        logger.warning("portal: guild directory fetch failed", exc_info=True)
        return None
    if not isinstance(raw, dict) or raw.get("error"):
        return None

    factions = raw.get("factions") or {}
    recruiting_meta = guild_recruiting.get_all()
    guilds = [_shape_guild(g, factions, recruiting_meta) for g in (raw.get("guilds") or [])]

    # Rankings. Overall = full historic ladder (all contributing guilds);
    # session = current Landsraad term, top 5.
    overall = sorted([g for g in guilds if g["contrib_overall"] > 0],
                     key=lambda g: -g["contrib_overall"])
    session_full = sorted([g for g in guilds if g["contrib_session"] > 0],
                          key=lambda g: -g["contrib_session"])
    rank_overall = {g["guild_id"]: i + 1 for i, g in enumerate(overall)}
    rank_session = {g["guild_id"]: i + 1 for i, g in enumerate(session_full)}
    for g in guilds:
        g["rank_overall"] = rank_overall.get(g["guild_id"])
        g["rank_session"] = rank_session.get(g["guild_id"])

    by_faction = []
    for fid in _GUILD_FACTION_ORDER:
        members = [g for g in guilds if g["faction_id"] == fid]
        if not members:
            continue
        members.sort(key=lambda g: (-g["member_count"], g["guild_name"].lower()))
        fname = _GUILD_FACTION_FALLBACK.get(fid) or factions.get(str(fid)) or "Unaligned"
        by_faction.append({
            "faction_id": fid,
            "faction_name": fname,
            "faction_slug": _GUILD_FACTION_SLUG.get(fid, "unaligned"),
            # Real game faction crest (Atreides/Harkonnen only; None for the rest).
            "crest": _faction_crest(fname),
            "is_main": fid in (1, 2),
            "guilds": members,
        })

    recruiting = sorted([g for g in guilds if g["recruiting"]],
                        key=lambda g: (-g["member_count"], g["guild_name"].lower()))

    return {
        "guilds": guilds,
        "by_id": {g["guild_id"]: g for g in guilds},
        "by_faction": by_faction,
        "recruiting": recruiting,
        "rank_overall": overall,
        "rank_session": session_full[:5],
        "current_term": raw.get("current_term"),
        "total_guilds": len(guilds),
    }


def _guild_editable_role(guild: Optional[dict], account_id: int) -> Optional[int]:
    """The logged-in account's role in this guild IF they may edit recruiting
    (Leader/Officer), else None. account_id is matched against each member's
    resolved account_id (guild_members.player_id -> player_state.account_id)."""
    if not guild:
        return None
    for m in guild.get("members") or []:
        if m.get("account_id") and int(m["account_id"]) == int(account_id):
            role = int(m.get("role_id") or 100)
            return role if role in _GUILD_EDIT_ROLES else None
    return None


@router.get("/portal/guilds")
async def portal_guilds(request: Request):
    """Public guild directory + recruitment board. Lists guilds per faction with
    member counts, the overall + this-session Landsraad rankings, and the guilds
    currently flagged recruiting. A Leader/Officer sees an edit affordance for
    their own guild (handled on the detail page)."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, row = gate
    _touch_last_session(active_account_id)

    data = await _load_guilds()
    # Which guild (if any) the signed-in player belongs to, + can they edit it.
    my_guild = None
    my_role = None
    if data:
        for g in data["guilds"]:
            if any(m.get("account_id") and int(m["account_id"]) == active_account_id
                   for m in g["members"]):
                my_guild = g
                my_role = _guild_editable_role(g, active_account_id)
                break

    ctx = {
        "active_character_name": row["character_name"],
        "guilds_data": data,
        "guilds_error": data is None,
        "my_guild": my_guild,
        "my_role_can_edit": my_role is not None,
        "active_nav": "guilds",
        "logout_post_url": "/portal/logout",
    }
    ctx.update(base_ctx(request, discord_handle=row["discord_handle"]))
    return templates.TemplateResponse(request, "portal/guilds.html", ctx)


@router.get("/portal/guilds/{guild_id:int}")
async def portal_guild_detail(request: Request, guild_id: int):
    """One guild's detail page: description (in-game or portal blurb), the contact
    in-game name(s) to reach out to, the full roster, ranking standing, and — for
    a Leader/Officer of this guild — the recruiting toggle + blurb editor."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, row = gate
    _touch_last_session(active_account_id)

    data = await _load_guilds()
    guild = (data["by_id"].get(int(guild_id)) if data else None)
    can_edit = _guild_editable_role(guild, active_account_id) is not None if guild else False

    ctx = {
        "active_character_name": row["character_name"],
        "guild": guild,
        "guilds_data": data,
        "guild_missing": (data is not None and guild is None),
        "guilds_error": data is None,
        "can_edit": can_edit,
        "active_nav": "guilds",
        "logout_post_url": "/portal/logout",
    }
    ctx.update(base_ctx(request, discord_handle=row["discord_handle"]))
    return templates.TemplateResponse(request, "portal/guild_detail.html", ctx)


@router.post("/portal/guilds/recruiting")
async def portal_guild_recruiting_set(request: Request):
    """Set/clear a guild's recruiting flag + portal blurb + contact note. Only a
    Leader/Officer of that guild (verified live against dune.guild_members via the
    cached directory) may edit. CSRF required. JSON for the fetch path, else 302
    back to the guild detail page."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, row = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    wants_json = "application/json" in (request.headers.get("accept", "") or "")
    guild_id_raw = str(form.get("guild_id", "") or "").strip()   # JSON may send guild_id as an int

    def _fail(msg: str, status: int = 400):
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=status)
        back = f"/portal/guilds/{guild_id_raw}" if guild_id_raw.isdigit() else "/portal/guilds"
        return RedirectResponse(url=f"{back}?err=1", status_code=302)

    if not guild_id_raw.isdigit():
        return _fail("Unknown guild.")
    guild_id = int(guild_id_raw)

    # Live authorization: the caller must currently be a Leader/Officer.
    data = await _load_guilds()
    if data is None:
        return _fail("Guild data is temporarily unavailable. Try again shortly.", status=503)
    guild = data["by_id"].get(guild_id)
    if not guild:
        return _fail("Unknown guild.", status=404)
    role = _guild_editable_role(guild, active_account_id)
    if role is None:
        return _fail("Only a guild Leader or Officer can edit recruiting.", status=403)

    # Field aliases: dev-2 sends `open`/`message`; keep `recruiting`/`blurb` too.
    recruiting_raw = (form.get("open", None) if form.get("open", None) is not None
                      else form.get("recruiting", ""))
    recruiting = str(recruiting_raw or "").strip().lower() in ("1", "true", "on", "yes")
    blurb = (form.get("message", None) if form.get("message", None) is not None
             else form.get("blurb", "")) or ""
    contact_note = form.get("contact_note", "") or ""
    playstyle = form.get("playstyle", "") or ""
    timezone = form.get("timezone", "") or ""
    language = form.get("language", "") or ""
    new_player_friendly = str(form.get("new_player_friendly", "") or "").strip().lower() in (
        "1", "true", "on", "yes")
    discord_url = form.get("discord_url", "") or ""

    guild_recruiting.set_recruiting(
        guild_id, recruiting=recruiting, blurb=blurb, contact_note=contact_note,
        account_id=active_account_id, discord_id=discord_id,
        char_name=row["character_name"] or "",
        playstyle=playstyle, timezone=timezone, language=language,
        new_player_friendly=new_player_friendly, discord_url=discord_url)

    if wants_json:
        return JSONResponse({"ok": True, "recruiting": recruiting})
    return RedirectResponse(url=f"/portal/guilds/{guild_id}?saved=1", status_code=302)


# ---- P0/P1 guild JSON surface (SvelteKit portal) ----------------------------
# These live under /portal/ so the ls_portal_session cookie rides along. The
# frontend identity store exposes NO account_id/role client-side, so the backend
# computes the authz booleans SERVER-SIDE:
#   - my_role_can_edit: the session player's role in that guild is Leader(100)
#     or Officer(50). Gates the edit affordance only; the write path re-verifies
#     is_player_guild_admin inside the DB txn regardless.
#   - is_self: a roster/census row belongs to the session player.

import re as _guild_re
_GUILD_OP_UUID_RE = _guild_re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _guild_member_account(guild: Optional[dict], account_id: int) -> Optional[dict]:
    """The session player's own member row in this guild, or None."""
    if not guild:
        return None
    for m in guild.get("members") or []:
        if m.get("account_id") and int(m["account_id"]) == int(account_id):
            return m
    return None


def _guild_inbox_config(guild_id: int) -> tuple:
    """(view_min_role, manage_min_role) for a guild's mailbox, defaulting to
    (50, 100) when unset. Mirrors messages._inbox_config; kept local to avoid a
    portal<->messages import cycle."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT view_min_role, manage_min_role FROM portal_guild_inbox_config "
            "WHERE guild_id = ?", (int(guild_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return 50, 100
    return int(row["view_min_role"]), int(row["manage_min_role"])


@router.get("/portal/guilds/data")
async def portal_guilds_data(request: Request):
    """The full guild directory (same shape as _load_guilds) as JSON, decorated
    with the session player's server-computed authz: which guild they belong to
    and, per guild, my_role_can_edit. Auth: linked session."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, active_account_id, _row = gate
    _touch_last_session(active_account_id)

    data = await _load_guilds()
    if data is None:
        return JSONResponse({"ok": False, "available": False,
                             "error": "Guild data is temporarily unavailable."},
                            status_code=503)

    my_guild_id = None
    guilds_out = []
    for g in data["guilds"]:
        mine = _guild_member_account(g, active_account_id)
        can_edit = _guild_editable_role(g, active_account_id) is not None
        is_mine = mine is not None
        my_role_id = int(mine.get("role_id") or 1) if mine else None
        if is_mine:
            my_guild_id = g["guild_id"]
        # Expose per-member player_controller_id + role details ONLY for the
        # caller's OWN guild AND only when they may edit it (Leader/Officer) — the
        # UI needs it to target member ops. NEVER for other guilds; account_id is
        # never emitted.
        expose_ctrl = is_mine and can_edit
        members = []
        for m in g.get("members") or []:
            is_self = bool(m.get("account_id") and
                           int(m["account_id"]) == active_account_id)
            cname = m.get("char_name")
            row = {
                # both keys for compatibility: character_name (dev-2) + char_name.
                "character_name": cname,
                "char_name": cname,
                "role_id": int(m.get("role_id") or 1),
                "role_name": m.get("role_name") or _guild_role_name(m.get("role_id")),
                "is_self": is_self,
            }
            if expose_ctrl and m.get("player_id"):
                row["player_controller_id"] = int(m["player_id"])
            members.append(row)

        # Recruiting sub-object (None when the guild is not flagged recruiting).
        recruiting_obj = None
        if g.get("recruiting"):
            recruiting_obj = {
                "open": True,
                "playstyle": g.get("recruiting_playstyle"),
                "timezone": g.get("recruiting_timezone"),
                "language": g.get("recruiting_language"),
                "new_player_friendly": bool(g.get("recruiting_new_player_friendly")),
                "discord_url": g.get("recruiting_discord_url"),
                "message": g.get("blurb") or None,
            }

        view_min, manage_min = _guild_inbox_config(g["guild_id"])

        guilds_out.append({
            "guild_id": g["guild_id"],
            # name aliases: name (dev-2) + guild_name (existing).
            "name": g["guild_name"],
            "guild_name": g["guild_name"],
            "faction": g["faction_name"],
            "faction_id": g["faction_id"],
            "faction_name": g["faction_name"],
            "faction_slug": g["faction_slug"],
            "member_count": g["member_count"],
            "description": g["description"],
            "recruiting": recruiting_obj,        # object|null (dev-2 shape)
            "recruiting_open": bool(g["recruiting"]),  # boolean convenience flag
            "contrib_overall": g["contrib_overall"],
            "contrib_session": g["contrib_session"],
            "rank_overall": g.get("rank_overall"),
            "rank_session": g.get("rank_session"),
            "members": members,
            "is_mine": is_mine,
            "my_role_id": my_role_id,
            "my_role_can_edit": can_edit,
            "inbox_config": {"view_min_role": view_min,
                             "manage_min_role": manage_min},
        })

    return JSONResponse({
        "ok": True,
        "available": True,
        "my_guild_id": my_guild_id,
        "current_term": data.get("current_term"),
        "total_guilds": data.get("total_guilds"),
        "guilds": guilds_out,
    })


@router.get("/portal/guilds/invites")
async def portal_guild_invites(request: Request):
    """The session player's OWN pending guild invites. account_id is taken from
    the session, never the client. Degrades to an empty list if the read is
    unavailable. Auth: linked session."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, active_account_id, _row = gate

    try:
        from routers.dune import cached_guild_invites
        payload = await cached_guild_invites(active_account_id)
    except Exception:
        logger.warning("portal: guild invites fetch failed", exc_info=True)
        return JSONResponse({"ok": True, "available": False, "invites": []})

    if not isinstance(payload, dict) or payload.get("error"):
        return JSONResponse({"ok": True, "available": False, "invites": []})
    return JSONResponse({
        "ok": True,
        "available": bool(payload.get("available", True)),
        "invites": payload.get("invites") or [],
    })


@router.get("/portal/guilds/{guild_id:int}/presence")
async def portal_guild_presence(request: Request, guild_id: int):
    """Online-state census of a guild's roster. MEMBER-GATED: the session player
    must belong to this guild (verified live against the cached directory). Each
    row is flagged is_self for the caller; internal account_id is stripped.
    Auth: linked session."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, active_account_id, _row = gate

    data = await _load_guilds()
    if data is None:
        return JSONResponse({"ok": False, "available": False,
                             "error": "Guild data is temporarily unavailable."},
                            status_code=503)
    guild = data["by_id"].get(int(guild_id))
    if not guild:
        return JSONResponse({"ok": False, "error": "Unknown guild."},
                            status_code=404)
    if _guild_member_account(guild, active_account_id) is None:
        return JSONResponse({"ok": False, "error": "Members only."},
                            status_code=403)

    try:
        from routers.dune import cached_guild_census
        payload = await cached_guild_census(int(guild_id))
    except Exception:
        logger.warning("portal: guild census fetch failed", exc_info=True)
        return JSONResponse({"ok": True, "available": False, "members": []})
    if not isinstance(payload, dict) or payload.get("error"):
        return JSONResponse({"ok": True, "available": False, "members": []})

    members = []
    for m in payload.get("members") or []:
        aid = m.get("account_id")
        members.append({
            "player_controller_id": m.get("player_controller_id"),
            "character_name": m.get("character_name"),
            "online_status": m.get("online_status"),
            "last_activity": m.get("last_activity"),
            "server_info": m.get("server_info"),
            "is_self": bool(aid and int(aid) == active_account_id),
        })
    return JSONResponse({"ok": True, "available": True,
                         "guild_id": int(guild_id), "members": members})


# Portal ops exposed on /portal/guilds/op. edit_description is LIVE; the member
# ops (promote/demote/remove) ship DARK behind GUILD_WRITES_DARK on the writer
# (remove is additionally reason-gated). accept_invite/reject_invite answer the
# caller's OWN invite — a self-action, so they carry no guild_id and no
# Leader/Officer gate. send_invite is still not a raw portal op: officer-side
# invites flow through Tier 2's join-request path.
_PORTAL_GUILD_OPS = {"edit_description", "promote", "demote", "remove",
                     "accept_invite", "reject_invite"}
_MEMBER_OPS = {"promote", "demote", "remove"}
_INVITE_OPS = {"accept_invite", "reject_invite"}

# Per-account throttle for the guild WRITE path. Every op opens a real psql txn and
# takes the guild lock on the live game DB behind a 40 s relay call. Before the
# invite verbs the Leader/Officer gate was the cheap early reject; now any linked
# player reaches the relay, so the throttle sits ahead of it. In-process state
# (lastsietch-admin runs one worker); a restart simply resets it.
_GUILD_OP_RATE_MAX, _GUILD_OP_RATE_WINDOW_S = 10, 60.0
_guild_op_hits: dict = {}


def _guild_op_rate_ok(account_id) -> bool:
    now = time.monotonic()
    if len(_guild_op_hits) > 256:
        # Opportunistic sweep: accounts whose newest hit fell out of the window are
        # dropped, so idle accounts do not accumulate for the process lifetime.
        for stale in [k for k, v in _guild_op_hits.items()
                      if not v or now - v[-1] >= _GUILD_OP_RATE_WINDOW_S]:
            _guild_op_hits.pop(stale, None)
    hits = _guild_op_hits.setdefault(account_id, [])
    hits[:] = [t for t in hits if now - t < _GUILD_OP_RATE_WINDOW_S]
    if len(hits) >= _GUILD_OP_RATE_MAX:
        return False
    hits.append(now)
    return True
# Mailbox payload sub-kind for the notification dropped to the target on success.
_MEMBER_OP_NOTIFY = {"promote": "promoted", "demote": "demoted", "remove": "removed"}
# Characters that must never reach dune.guilds.guild_description. Funcom builds the
# guild-invite notify payload as hand-concatenated JSON with no escaping, so a " or
# \ (or a raw newline / control char) in the description yields a malformed payload
# that every game pod drops — silently killing every invite the guild sends, with no
# in-game error. Cost guild 31 its invites on 2026-07-23.
# dune.add_guild_invite is patched to escape properly (ops/guild-invite-json-escape/),
# but every Funcom build update restores Funcom's version, so keep the stored value
# clean here rather than depending on the patch being in place.
_GUILD_DESC_FORBIDDEN_RE = _guild_re.compile(r'["\\\x00-\x1f\x7f]')
_GUILD_DESC_FORBIDDEN_MSG = (
    "Straight quotes (\") and backslashes (\\) aren't allowed in a guild description "
    "— they break guild invites. Try curly quotes (“ ”) instead."
)


def _member_by_controller(guild: Optional[dict], controller_id: int) -> Optional[dict]:
    """A guild member row matched by player_controller_id (guild_members.player_id
    from the roster), or None. Used to resolve the target of a member op + its
    role for the early-reject hierarchy hint."""
    if not guild:
        return None
    for m in guild.get("members") or []:
        if m.get("player_id") and int(m["player_id"]) == int(controller_id):
            return m
    return None


# The guild writer reports a refused transaction as the raw psql tail, newlines
# folded: "guild op transaction failed: ERROR:  guild is full CONTEXT:  PL/pgSQL
# function ...". The ERROR sentence is the writer's (or the game proc's) reason
# and worth showing; the prefix and the CONTEXT/DETAIL/HINT tail are neither
# player copy nor safe to echo. Returns player copy, never an empty string.
_PSQL_TAIL_RE = re.compile(r"\s+(?:CONTEXT|DETAIL|HINT|LINE \d+):.*$", re.S)
_INVITE_FLIP_REASON = "invite_not_for_active_character"
_INVITE_FLIP_COPY = ("This hail was sent to another character on your account. The character "
                     "you played last is the one who answers; switch to it in game, then check "
                     "your invites again.")


def _player_refusal(raw: str) -> str:
    text = str(raw or "").strip()
    if "ERROR:" in text:
        text = text.split("ERROR:", 1)[1]
        text = _PSQL_TAIL_RE.sub("", text).strip(" .:")
        return (text[:1].upper() + text[1:] + ".") if text else "The registry refused the change."
    if text.lower().startswith("guild op transaction failed"):
        return "The registry refused the change."
    return text or "The registry refused the change."


@router.post("/portal/guilds/op")
async def portal_guild_op(request: Request):
    """Guild-operation WRITE path: edit_description (LIVE), member management
    promote/demote/remove (DARK behind GUILD_WRITES_DARK on the writer), and
    accept_invite/reject_invite on the caller's own invite (self-action: no
    guild_id, no Leader/Officer gate; the writer verifies invite ownership). The
    acting player's account_id is injected from the SESSION — never the client.
    CSRF required. Forwards to the relay, which hands the op to dune-guild-op.sh
    (guild lock + authz re-verify + audit). Member ops target a
    target_player_controller_id (controller-scoped, from the roster), NEVER an
    account_id. Returns a normalized JSON envelope. Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _row = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def _fail(msg: str, status: int = 400):
        return JSONResponse({"success": False, "status": "failed", "message": msg},
                            status_code=status)

    op = (form.get("op", "") or "").strip()
    if op not in _PORTAL_GUILD_OPS:
        return _fail("Unsupported guild operation.", status=400)
    if not _guild_op_rate_ok(active_account_id):
        return _fail("Too many guild operations. Wait a moment and retry.", status=429)

    # An invite op answers the caller's own invite. There is no guild to name and
    # no role to hold, so both the guild_id requirement and the Leader/Officer gate
    # below are skipped for these two verbs only. The writer re-verifies that the
    # invite belongs to the actor's own controller before it writes.
    is_invite_op = op in _INVITE_OPS
    guild_id = None
    guild = None
    actor_role = None

    if not is_invite_op:
        guild_id_raw = str(form.get("guild_id", "") or "").strip()   # JSON may send guild_id as an int
        if not guild_id_raw.isdigit() or int(guild_id_raw) <= 0:
            return _fail("Unknown guild.")
        guild_id = int(guild_id_raw)

        # Live authorization hint. The write txn re-enforces every gate regardless;
        # this is an early clean reject.
        data = await _load_guilds()
        if data is None:
            return _fail("Guild data is temporarily unavailable. Try again shortly.",
                         status=503)
        guild = data["by_id"].get(guild_id)
        if not guild:
            return _fail("Unknown guild.", status=404)
        actor_role = _guild_editable_role(guild, active_account_id)  # 100/50 or None
        if actor_role is None:
            return _fail("Only a guild Leader or Officer can manage this guild.",
                         status=403)

    # --- op-specific validation + relay body -----------------------------------
    detail: dict = {}
    extra: dict = {}
    target_account_id = None      # roster-resolved; mailbox recipient only, never returned
    target_ctrl = None

    if is_invite_op:
        invite_raw = str(form.get("invite_id", "") or "").strip()
        if not invite_raw.isdigit() or int(invite_raw) <= 0:
            return _fail("An invite is required.")
        detail = {"invite_id": int(invite_raw)}
    elif op == "edit_description":
        description = form.get("description", "") or ""
        # A raw newline is invalid inside a JSON string literal too, but pasting a
        # paragraph in is a reasonable thing to do — fold that whitespace rather
        # than rejecting it. Anything still matching after the fold is a character
        # nobody types on purpose, or the two that players do: " and \.
        description = _guild_re.sub(r"[\r\n\t]+", " ", description).strip()
        if len(description) > 2000:
            return _fail("Description is too long (2000 characters max).")
        if _GUILD_DESC_FORBIDDEN_RE.search(description):
            return _fail(_GUILD_DESC_FORBIDDEN_MSG)
        detail = {"description": description}
    else:
        # Member op: controller-scoped target + hierarchy hint (contract 2.2).
        tc_raw = (form.get("target_player_controller_id", "") or "").strip()
        if not tc_raw.isdigit() or int(tc_raw) <= 0:
            return _fail("A target member is required.")
        target_ctrl = int(tc_raw)
        target = _member_by_controller(guild, target_ctrl)
        if target is None:
            return _fail("That member is not in this guild.", status=404)
        target_role = int(target.get("role_id") or 1)
        target_account_id = target.get("account_id")

        # Hierarchy pre-check (server re-enforces in the txn).
        if target_account_id and int(target_account_id) == int(active_account_id):
            return _fail("You cannot run a member action on yourself.", status=400)
        if actor_role <= target_role:
            return _fail("You cannot act on a member of equal or higher rank.",
                         status=403)

        if op in ("promote", "demote"):
            nr_raw = (form.get("new_role", "") or "").strip()
            if not nr_raw.isdigit():
                return _fail("A target role is required.")
            new_role = int(nr_raw)
            if op == "promote":
                if new_role not in (50, 100):
                    return _fail("Promote role must be Officer or Leader.")
                if new_role == 100 and actor_role != 100:
                    return _fail("Only a Leader can transfer leadership.", status=403)
            else:  # demote
                if new_role not in (1, 50):
                    return _fail("Demote role must be Member or Officer.")
            detail = {"new_role": new_role}
        extra["target_player_controller_id"] = target_ctrl

    # Idempotency: accept a client-provided key, else mint one per submit.
    import uuid as _uuid
    idem = str(form.get("idempotency_key", "") or "").strip()
    if not _GUILD_OP_UUID_RE.match(idem):
        idem = str(_uuid.uuid4())

    body = {
        "op": op,
        "actor_account_id": active_account_id,   # from session, NEVER the client
        "detail": detail,
        "idempotency_key": idem,
        "mode": "apply",
        "requested_by_discord_id": discord_id,
    }
    if guild_id is not None:
        body["guild_id"] = guild_id
    body.update(extra)

    ip = client_ip(request)
    result = None
    try:
        from relay import call_relay
        result = await call_relay("/dune/guild-op", method="POST",
                                  json_body=body, timeout=40)
    except HTTPException as exc:
        logger.warning("portal: guild-op relay error acct=%s guild=%s op=%s: %s",
                       active_account_id, guild_id, op, exc.detail)
        return _fail("The guild service is unavailable right now. Try again shortly.",
                     status=502)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: guild-op failed acct=%s guild=%s op=%s: %s",
                       active_account_id, guild_id, op, exc)
        return _fail("Could not apply the change right now. Try again shortly.",
                     status=502)
    finally:
        from auth import audit_log
        audit_target = (f"invite:{detail.get('invite_id')}" if is_invite_op
                        else str(guild_id))
        audit_log(None, f"portal:{discord_id}", "portal_guild_op",
                  f"{op}:{audit_target}", ip)

    if not isinstance(result, dict):
        return _fail("Unexpected response from the guild service.", status=502)

    # A refused write reaches the player as copy, not as a psql transcript (the raw
    # text stays in the log). The one refusal a player can act on is the controller
    # flip on an invite: the read listed the hail for one character and the writer
    # resolved the actor to another (the most recently played one), typically
    # because they logged into an alt in between. Name it, and drop the stale row
    # from the cached read so the refresh that follows shows the truth.
    if result.get("status") == "failed":
        raw = str(result.get("message") or result.get("fail_reason") or "")
        logger.warning("portal: guild-op refused acct=%s op=%s: %s",
                       active_account_id, op, raw[:400])
        result = {**result, "message": _player_refusal(raw)}
        if is_invite_op and "does not belong to actor" in raw:
            result["fail_reason"] = _INVITE_FLIP_REASON
            result["message"] = _INVITE_FLIP_COPY
            try:
                from cache import invalidate
                invalidate("dune.guild_invites", active_account_id)
            except Exception:  # noqa: BLE001
                logger.warning("portal: invite cache invalidate failed", exc_info=True)

    # On a real apply of a member op, notify the target via the mailbox. Only
    # 'applied' (not 'deferred'/'replay') drops a notification; the target's
    # account_id is used solely as the mailbox key and is never returned.
    if (op in _MEMBER_OPS and result.get("status") == "applied"
            and target_account_id):
        try:
            import mailbox
            mailbox.post(
                "player", int(target_account_id), "notification",
                payload={"kind": _MEMBER_OP_NOTIFY[op], "guild_id": guild_id,
                         "guild_name": guild.get("guild_name")})
        except Exception:  # noqa: BLE001
            logger.warning("portal: member-op mailbox notify failed guild=%s op=%s",
                           guild_id, op, exc_info=True)

    # An applied invite answer changes what the caller's next reads must say: the
    # pending-invites read (20 s cache) and, on accept, the guild directory (60 s)
    # and that guild's census. Drop them so the page refresh that follows the click
    # sees the new state instead of "not in a guild" for up to a minute. Same key
    # shapes as the cached reads (positional args only).
    # The same staleness applies to promote/demote/remove/edit_description (the roster
    # re-read after an Officer promotes showed the old role for a minute), so every
    # applied op drops the directory + census; only invite answers touch the invites read.
    if result.get("status") in ("applied", "replay"):
        try:
            from cache import invalidate, invalidate_prefix
            if op in _INVITE_OPS:
                invalidate("dune.guild_invites", active_account_id)
            if op != "reject_invite":
                invalidate("dune.guilds")
                invalidate_prefix("dune.guild_census")
        except Exception:  # noqa: BLE001
            logger.warning("portal: guild-op cache invalidate failed op=%s", op, exc_info=True)

    # Degrade gracefully if the relay/writer omits fields.
    return JSONResponse({
        "success": bool(result.get("success")),
        "status": result.get("status"),
        "op": op,
        "guild_id": guild_id,
        "audit_id": result.get("audit_id"),
        "proc_result": result.get("proc_result"),
        "fail_reason": result.get("fail_reason"),
        "message": result.get("message") or "",
    })


# ---- Tier 2: Solo LFG "Seeker Wall" -----------------------------------------
# All admin.db. Self-post / self-edit / expiry. account_id from the session,
# never the client; never emitted to other players (char_name is the key).

@router.get("/portal/guilds/lfg")
async def portal_lfg_list(request: Request):
    """List active guild-seekers (LFG wall). Auth: linked session."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, active_account_id, _row = gate
    _touch_last_session(active_account_id)
    return JSONResponse({"ok": True, "seekers": lfg_seekers.list_active()})


@router.post("/portal/guilds/lfg")
async def portal_lfg_upsert(request: Request):
    """Upsert the caller's own seeker row. Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, active_account_id, row = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    ttl_raw = (form.get("ttl_hours", "") or "").strip()
    ttl_hours = int(ttl_raw) if ttl_raw.isdigit() else None
    seeker = lfg_seekers.upsert(
        active_account_id,
        char_name=row["character_name"] or "",
        playstyle=form.get("playstyle", "") or "",
        timezone=form.get("timezone", "") or "",
        role=form.get("role", "") or "",
        note=form.get("note", "") or "",
        ttl_hours=ttl_hours)
    return JSONResponse({"ok": True, "seeker": seeker})


@router.post("/portal/guilds/lfg/delete")
async def portal_lfg_delete(request: Request):
    """Remove the caller's own seeker row. Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, active_account_id, _row = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    lfg_seekers.delete(active_account_id)
    return JSONResponse({"ok": True})


# ---- Tier 2: guild join-requests --------------------------------------------
# Canonical record in admin.db (portal_guild_join_requests). The invite handoff
# reuses the DARK send_invite op. {guild_id:int} convertor avoids shadowing the
# literal /portal/guilds/* routes.

@router.post("/portal/guilds/{guild_id:int}/join-request")
async def portal_guild_join_request(request: Request, guild_id: int):
    """Raise a pending join-request against a guild. Any linked player. Idempotent
    on (guild, requester). Rate-limited per account. On a NEW request, drops a
    notification into the guild inbox. Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, row = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    data = await _load_guilds()
    if data is None:
        return JSONResponse({"ok": False, "error": "Guild data is temporarily "
                             "unavailable."}, status_code=503)
    guild = data["by_id"].get(int(guild_id))
    if not guild:
        return JSONResponse({"ok": False, "error": "Unknown guild."},
                            status_code=404)
    # A current member cannot request to join their own guild.
    if _guild_member_account(guild, active_account_id) is not None:
        return JSONResponse({"ok": False, "error": "You are already in this guild."},
                            status_code=400)
    if not guild_join.rate_ok(active_account_id):
        return JSONResponse({"ok": False, "error": "Too many join-requests. Try "
                             "again later."}, status_code=429)

    result = guild_join.create_request(
        requester_account_id=active_account_id, guild_id=int(guild_id),
        requester_char_name=row["character_name"] or "",
        requester_discord_id=discord_id,
        note=form.get("note", "") or "")

    if result["is_new"] and result["request_id"]:
        try:
            # NOTE: NEVER put requester_account_id (or any account_id) in the
            # payload — the guild inbox is officer-visible and _project() would
            # surface it to the client. The invite handoff reads the requester's
            # account_id server-side via guild_join.get_pending(request_id).
            mailbox.post(
                "guild", int(guild_id), "notification",
                subject="New join request",
                body=f"{row['character_name']} asked to join your guild.",
                payload={"kind": "join_request", "request_id": result["request_id"]})
        except Exception:  # noqa: BLE001
            logger.warning("portal: join-request mailbox notify failed guild=%s",
                           guild_id, exc_info=True)

    return JSONResponse({"ok": True, "status": result["status"],
                         "request_id": result["request_id"]})


@router.get("/portal/guilds/{guild_id:int}/join-requests")
async def portal_guild_join_requests_list(request: Request, guild_id: int):
    """List a guild's pending join-requests. Officer-gated (role 50/100 of THIS
    guild). requester_account_id is never leaked. Auth: linked session."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, active_account_id, _row = gate

    data = await _load_guilds()
    if data is None:
        return JSONResponse({"ok": False, "error": "Guild data is temporarily "
                             "unavailable."}, status_code=503)
    guild = data["by_id"].get(int(guild_id))
    if not guild:
        return JSONResponse({"ok": False, "error": "Unknown guild."},
                            status_code=404)
    if _guild_editable_role(guild, active_account_id) is None:
        return JSONResponse({"ok": False, "error": "Officers only."},
                            status_code=403)

    return JSONResponse({"ok": True, "guild_id": int(guild_id),
                         "requests": guild_join.list_pending(int(guild_id))})


@router.post("/portal/guilds/{guild_id:int}/join-requests/{request_id:int}/invite")
async def portal_guild_join_request_invite(request: Request, guild_id: int,
                                           request_id: int):
    """Officer accepts a join-request: fires the DARK send_invite op with the
    requester as target (account_id resolved server-side from the join row), then
    flips the row -> invited, clears the guild-inbox nag, and notifies the
    requester. Auth: linked session + CSRF + Officer of THIS guild."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _row = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def _fail(msg: str, status: int = 400):
        return JSONResponse({"success": False, "status": "failed", "message": msg},
                            status_code=status)

    data = await _load_guilds()
    if data is None:
        return _fail("Guild data is temporarily unavailable. Try again shortly.",
                     status=503)
    guild = data["by_id"].get(int(guild_id))
    if not guild:
        return _fail("Unknown guild.", status=404)
    if _guild_editable_role(guild, active_account_id) is None:
        return _fail("Only a guild Leader or Officer can invite.", status=403)

    req = guild_join.get_pending(int(request_id), int(guild_id))
    if not req:
        return _fail("That request is no longer pending.", status=404)
    requester_account_id = int(req["requester_account_id"])

    import uuid as _uuid
    idem = str(_uuid.uuid4())
    body = {
        "op": "send_invite",
        "guild_id": int(guild_id),
        "actor_account_id": active_account_id,     # from session, NEVER the client
        "target_account_id": requester_account_id,
        "detail": {},
        "idempotency_key": idem,
        "mode": "apply",
        "requested_by_discord_id": discord_id,
    }

    ip = client_ip(request)
    try:
        from relay import call_relay
        result = await call_relay("/dune/guild-op", method="POST",
                                  json_body=body, timeout=40)
    except HTTPException as exc:
        logger.warning("portal: join-invite relay error guild=%s req=%s: %s",
                       guild_id, request_id, exc.detail)
        return _fail("The guild service is unavailable right now. Try again shortly.",
                     status=502)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: join-invite failed guild=%s req=%s: %s",
                       guild_id, request_id, exc)
        return _fail("Could not send the invite right now. Try again shortly.",
                     status=502)
    finally:
        from auth import audit_log
        audit_log(None, f"portal:{discord_id}", "portal_guild_join_invite",
                  f"{guild_id}:{request_id}", ip)

    if not isinstance(result, dict):
        return _fail("Unexpected response from the guild service.", status=502)

    # The invite op is DARK: 'deferred' while GUILD_WRITES_DARK != 0, 'applied'
    # once enabled. Advance the request + notify on either — a deferred invite
    # still means the officer accepted; the write just isn't live yet.
    status = result.get("status")
    if status in ("applied", "deferred", "replay"):
        guild_join.mark_invited(int(request_id), active_account_id)
        try:
            mailbox.mark_read_by_payload(int(guild_id), int(request_id),
                                         active_account_id)
            mailbox.post(
                "player", requester_account_id, "notification",
                subject="Guild invite",
                body=f"{guild.get('guild_name')} invited you to join.",
                payload={"kind": "guild_invite", "guild_id": int(guild_id)})
        except Exception:  # noqa: BLE001
            logger.warning("portal: join-invite mailbox update failed guild=%s req=%s",
                           guild_id, request_id, exc_info=True)

    return JSONResponse({
        "success": bool(result.get("success")),
        "status": status,
        "op": "send_invite",
        "guild_id": int(guild_id),
        "request_id": int(request_id),
        "audit_id": result.get("audit_id"),
        "message": result.get("message") or "",
    })


# ---- Tier 4: Solari gifting (rides the mailbox) -----------------------------
# Auto-credit at send (no escrow), value-conserving. DARK behind LASTSIETCH_GIFTS_ENABLED
# on the writer. sender = session; a client can never gift AS another player.

def _resolve_account_by_char_name(name: str) -> Optional[int]:
    """Resolve a character name -> account_id via admin.db ls_account_links
    (plaintext character_name; the game DB stores names ENCRYPTED so they cannot
    be matched there). Case-insensitive, active links only, newest first. Returns
    None if the name is not linked. Used only as a server-side gift/DM recipient
    key; never surfaced to the client."""
    name = (name or "").strip()
    if not name:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT account_id FROM ls_account_links
                WHERE character_name = ? COLLATE NOCASE AND revoked_at IS NULL
                ORDER BY linked_at DESC LIMIT 1""").replace("ls_account_links", portal_identity.link_table(conn)),
            (name,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["account_id"]) if row else None


def _resolve_recipient(body, requester_identity, noun):
    """Recipient for a cross-player write: EXACTLY ONE of recipient_char_name or
    recipient_code. Both, or neither, is a bad_request rather than one of them
    quietly winning.

    🔴 The CODE never leaves this function. It is resolved to an account id here
    and only the id travels on: no relay payload, no audit target, no mailbox
    payload and no log line ever carries the eight characters. Resolution runs
    again on every write even when the client already confirmed the name through
    the lookup route -- a code can rotate between confirm and send, and a
    confirmed name is not evidence that it still points anywhere.

    Returns (recipient_account_id, None) or (None, (token, friendly, status))."""
    name = str(body.get("recipient_char_name", "") or "").strip()
    code = str(body.get("recipient_code", "") or "").strip()
    if bool(name) == bool(code):
        return None, ("bad_request",
                      "Choose a player by name or by code, not both.", 400)

    if name:
        account_id = _resolve_account_by_char_name(name)
        if not account_id:
            return None, ("recipient_unlinked",
                          "That player is not linked to the portal, so they cannot "
                          f"receive {noun}.", 404)
        return account_id, None

    from routers import portal_codes
    candidate = portal_codes.normalize_code(code)
    if not portal_codes.valid_code(candidate):
        return None, ("bad_code", "That is not a valid code.", 400)
    # Same throttle the lookup route enforces, on the same counter: a write path is
    # otherwise a second, uncapped way to walk the code space.
    if (portal_codes.lookups_in_window(requester_identity)
            >= portal_codes.LOOKUP_MAX_PER_WINDOW):
        return None, ("lookup_throttled",
                      "You have looked up as many codes as you can for now. "
                      "Try again in a few minutes.", 429)
    resolved = portal_codes.resolve_code(candidate, requester_identity)
    if not resolved or not resolved.get("account_id"):
        return None, ("code_unknown",
                      "That code is not in use. Ask them for a fresh one.", 404)
    return int(resolved["account_id"]), None


def _linked_alt_refusal(active_account_id, discord_id, recipient_account_id,
                        self_text, alt_text):
    """🔴 SELF-TRADE on BOTH keys, the Karum's guard (portal_karum_buy) applied to
    the two cross-player writes. Linked alts share a discord_id, so the account
    check alone is trivially defeated by someone who owns both sides. An identity
    that will not resolve on either side is a REJECT, not a pass: fail closed.

    Returns None to allow, else (token, friendly, status)."""
    if int(recipient_account_id) == int(active_account_id):
        return ("self_transfer", self_text, 400)
    recipient_discord = portal_gift_limits.identity_for_account(recipient_account_id)
    if (not discord_id or not recipient_discord
            or str(recipient_discord) == str(discord_id)):
        return ("linked_alt", alt_text, 400)
    return None


@router.post("/portal/gifts/send")
async def portal_gift_send(request: Request):
    """Send BANK Solari to another player. Auto-credited at send (dark behind
    LASTSIETCH_GIFTS_ENABLED on the writer). Sender is the SESSION account, never the
    client. Recipient is resolved server-side by character name via
    ls_account_links. Self-send rejected. Auth: linked session + CSRF.

    Caps run twice on purpose. Here, keyed on DISCORD IDENTITY (both the daily
    total and the per-pair count, collapsed across every account a player has
    linked) — see portal_gift_limits. Then again in the writer's transaction,
    keyed on account_id, as a backstop. The identity check cannot live in
    Postgres because the identity map is in admin.db.

    Every outcome is audited with its real success value and settled onto a
    durable portal_gift_events row, so a refusal is distinguishable from a
    send."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _row = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    ip = client_ip(request)

    def _audit(ok: bool, detail: str, target: str):
        """Durable attempt record in admin.db. success= is the REAL outcome:
        this call used to pass five positional args, which left `success`
        defaulted to True and recorded every refusal as a success."""
        from auth import audit_log
        audit_log(None, f"portal:{discord_id}", "portal_gift_send",
                  target, ip, details=detail[:500] or None, success=ok)

    def _fail(msg: str, status: int = 400, *, detail: str = "",
              target: str = "", error: str = "") -> JSONResponse:
        _audit(False, detail or msg, target)
        payload = {"success": False, "status": "failed", "message": msg}
        # Additive: the wave 7 tokens (bad_request, bad_code, code_unknown,
        # lookup_throttled, self_transfer, linked_alt) need a machine-readable
        # key. Older refusals keep the exact envelope they already ship.
        if error:
            payload["error"] = error
        return JSONResponse(payload, status_code=status)

    # str() first: a JSON body delivers amount as an int, and .strip() on an int
    # was a 500 on the first-ever real UI send (2026-08-27) — every prior gift
    # had gone through the writer directly, so this path was never exercised.
    amount_raw = str(form.get("amount", "") or "").strip()
    if not amount_raw.isdigit() or int(amount_raw) <= 0:
        return _fail("Enter a positive Solari amount.", detail="bad_amount")
    amount = int(amount_raw)
    # Mirror the writer's ceiling here for a clean error. The writer stays the
    # real gate; this only avoids a relay round-trip ending in a raw cap string.
    if amount > portal_gift_limits.GIFT_MAX_AMOUNT:
        return _fail(
            f"A single gift is capped at {portal_gift_limits.GIFT_MAX_AMOUNT:,} Solari.",
            detail="over_ceiling")

    # Recipient resolution (server-side only). By character name, or by identity
    # CODE which is resolved here and never echoed anywhere. No client-supplied
    # account_id is trusted (blocks id-guess enumeration).
    recipient_account_id, recipient_err = _resolve_recipient(
        form, discord_id, "a gift")
    if recipient_err is not None:
        token, friendly, status_code = recipient_err
        # recipient_unlinked predates wave 7 and keeps its exact envelope.
        return _fail(friendly, status=status_code, detail=token,
                     error="" if token == "recipient_unlinked" else token)
    target = f"{recipient_account_id}:{amount}"
    alt_err = _linked_alt_refusal(active_account_id, discord_id, recipient_account_id,
                                  "You cannot gift yourself.",
                                  "You cannot gift your own linked account.")
    if alt_err is not None:
        token, friendly, status_code = alt_err
        return _fail(friendly, status=status_code,
                     detail="self_send" if token == "self_transfer" else token,
                     target=target, error=token)

    # Idempotency: accept a client-provided key (guild-ops convention), else mint
    # one. Minting per submit means a retry after "could not be reached" is a
    # SECOND gift; a stable client key makes it a replay.
    import uuid as _uuid
    idem = str(form.get("idempotency_key", "") or "").strip()
    if not _GUILD_OP_UUID_RE.match(idem):
        idem = str(_uuid.uuid4())

    # Identity-collapsed caps. The writer's caps key on account_id and Postgres
    # cannot see ls_account_links, so the identity check has to run here; the
    # account-keyed caps downstream stay as the backstop.
    sender_identity = discord_id
    recipient_identity = portal_gift_limits.recipient_identity_key(recipient_account_id)
    ok, reason, retry_after = portal_gift_limits.check_and_reserve(
        idempotency_key=idem,
        sender_identity=sender_identity,
        sender_account_id=active_account_id,
        recipient_identity=recipient_identity,
        recipient_account_id=recipient_account_id,
        amount=amount)
    if not ok:
        msg = ("You have sent as many gifts as you can today. Try again tomorrow."
               if reason == "daily" else
               "You have sent this player as many gifts as you can today. "
               "Try again tomorrow.")
        _audit(False, f"rate_limited:{reason}", target)
        resp = JSONResponse({"success": False, "status": "failed", "message": msg},
                            status_code=429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    body = {
        "sender_account_id": active_account_id,   # from session, NEVER the client
        "recipient_account_id": recipient_account_id,
        "amount": amount,
        "idempotency_key": idem,
        "mode": "apply",
        "requested_by_discord_id": discord_id,
    }

    try:
        from relay import call_relay
        result = await call_relay("/dune/gift-op", method="POST",
                                  json_body=body, timeout=40)
    except HTTPException as exc:
        logger.warning("portal: gift relay error acct=%s: %s",
                       active_account_id, exc.detail)
        portal_gift_limits.settle(idem, "failed", f"relay: {exc.detail}")
        return _fail("The gift service is unavailable right now. Try again shortly.",
                     status=502, detail="relay_unavailable", target=target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: gift failed acct=%s: %s", active_account_id, exc)
        portal_gift_limits.settle(idem, "failed", f"relay: {exc}")
        return _fail("Could not send the gift right now. Try again shortly.",
                     status=502, detail="relay_error", target=target)

    if not isinstance(result, dict):
        portal_gift_limits.settle(idem, "failed", "non-dict relay response")
        return _fail("Unexpected response from the gift service.", status=502,
                     detail="bad_relay_response", target=target)

    # Settle the reservation with the writer's verdict. A refusal inside the
    # writer's transaction rolls its own Postgres audit row back; this row is in
    # admin.db, so it survives that rollback.
    status = str(result.get("status") or "failed")
    applied = status == "applied"
    if status not in ("applied", "replay", "deferred"):
        status = "failed"
    portal_gift_limits.settle(idem, status,
                              None if applied else str(result.get("message") or ""))

    # On a real credit, drop a claimed gift receipt into the recipient's mailbox.
    if applied:
        try:
            mailbox.post("player", int(recipient_account_id), "gift",
                         state="claimed",
                         payload={"amount": amount, "currency": "solari"})
        except Exception:  # noqa: BLE001
            logger.warning("portal: gift mailbox receipt failed", exc_info=True)
        # Fan-in watch: nothing caps INBOUND Solari, and nothing should — a
        # popular guild bank is not an abuser. Alert only.
        await portal_gift_limits.alert_inbound_if_over(
            recipient_identity=recipient_identity,
            sender_identity=sender_identity,
            recipient_account_id=recipient_account_id,
            amount=amount, ip=ip)

    _audit(bool(result.get("success")) and status in ("applied", "replay"),
           f"{status}:{result.get('message') or ''}", target)

    return JSONResponse({
        "success": bool(result.get("success")),
        "status": result.get("status"),
        "amount": amount,
        "audit_id": result.get("audit_id"),
        "message": result.get("message") or "",
    })


@router.get("/portal/guilds/me")
async def portal_guilds_me(request: Request):
    """The viewer's own guild context, for the SvelteKit editor to prefill/gate.
    Returns {in_guild, guild_id, guild_name, my_role_can_edit, description}.
    my_role_can_edit is the CANONICAL server-computed gate (role 100 or 50); the
    GuildDescriptionEditor mounts only when it is true (the write path still
    re-verifies is_player_guild_admin inside the txn regardless). Fails CLOSED:
    401 unauthenticated, 404 not in a guild, 503 data unavailable. Never returns
    account_id or the raw role — only the booleans/strings above."""
    session = get_portal_session(request)
    active_account_id = int(session.get("aid") or 0) if session else 0
    if active_account_id <= 0:
        return JSONResponse({"in_guild": False, "my_role_can_edit": False,
                             "error": "unauthenticated"}, status_code=401)
    _touch_last_session(active_account_id)

    data = await _load_guilds()
    if data is None:
        return JSONResponse({"in_guild": False, "my_role_can_edit": False,
                             "error": "unavailable"}, status_code=503)

    my_guild = None
    for g in data["guilds"]:
        if _guild_member_account(g, active_account_id) is not None:
            my_guild = g
            break
    if my_guild is None:
        return JSONResponse({"in_guild": False, "guild_id": None, "guild_name": None,
                             "my_role_can_edit": False, "description": None},
                            status_code=404)

    can_edit = _guild_editable_role(my_guild, active_account_id) is not None
    return JSONResponse({
        "in_guild": True,
        "guild_id": my_guild["guild_id"],
        "guild_name": my_guild["guild_name"],
        "my_role_can_edit": can_edit,
        # In-game description (dune.guilds.guild_description) — what
        # edit_guild_description edits, so the editor prefills the live value.
        "description": my_guild.get("in_game_description") or "",
    })


# ----------------------------------------------------- containers -----------
#
# Public, read-only mirror of the admin container browser, scoped strictly to
# the OAuth-bound account. account_id ALWAYS comes from session.aid; the
# client only ever supplies a container_id, and the game-side helper enforces
# ownership (a container_id that isn't owned by the account returns
# `not_owned`, which we surface as a 404). So a logged-in player can only ever
# read their own chests.


# Vehicle BP class -> friendly name for the storage browser tiles.
_VEHICLE_NAMES = {
    "BP_Buggy_CHOAM_C": "Buggy",
    "BP_Sandbike_CHOAM_C": "Sandbike",
    "BP_SandCrawler_CHOAM_C": "Sandcrawler",
    "BP_LightOrnithopter_Choam_C": "Scout Ornithopter",
    "BP_MediumOrnithopter_CHOAM_C": "Assault Ornithopter",
    "BP_TransportOrnithopter_CHOAM_C": "Carrier Ornithopter",
    "BP_ContainerVehicle_C": "Cargo Container",
}


def _friendly_vehicle(bp_class: str) -> str:
    """Map a vehicle BP class to a player-facing name; fall back to a cleaned
    version of the class for any vehicle we have not catalogued yet."""
    if bp_class in _VEHICLE_NAMES:
        return _VEHICLE_NAMES[bp_class]
    stem = bp_class.removeprefix("BP_").removesuffix("_C")
    stem = stem.replace("_CHOAM", "").replace("_Choam", "").replace("_", " ").strip()
    return stem or "Vehicle"


def _shape_containers_for_render(payload: dict) -> dict:
    """Shape the relay container-list envelope for the portal template. Keeps
    only display fields; surfaces both the container TYPE (`label`, e.g.
    'Small Storage Container') and the player's custom name (`name`) when set.

    TWO ID NAMESPACES (see dune-containers.py): `id` is the container id the READ
    path is keyed by -- a placeable id for a box, an inventory id for the bank and
    vehicle cargo. `inv_id` is the real dune.inventories.id in every branch. Any
    WRITER (storage MOVE, repair box) gates on owned_inv_sql(), which only knows
    inventory ids, so it must be handed `inv_id` -- never `id`. `inv_id` is absent
    on a pre-deploy read model / mirror blob, so every consumer falls back to `id`.

    The two namespaces OVERLAP -- a placeable id can equal a *different* box's
    inventory id -- so handing a writer an `id` does not fail safe: it either misses
    (`not_owner`) or silently hits the WRONG inventory. Treat mixing them as a
    data-integrity bug, not a cosmetic one."""
    containers = []
    for c in (payload.get("containers") or []):
        custom = (c.get("name") or "").strip()
        cls = c.get("class") or ""
        is_bank = cls == "CHOAMBank"
        is_backpack = cls == "Backpack"
        is_vehicle = cls.startswith("Vehicle:")
        # Map is already friendly-shaped by the read script (e.g. 'Hagga Basin',
        # 'Deep Desert (PvE)'/'(PvP)'). Vehicle/container cargo persists in the
        # DB on every map, so contents are always shown.
        cmap = c.get("map") or ""
        item_count = c.get("item_count") or 0
        if is_vehicle:
            type_label = _friendly_vehicle(cls.split(":", 1)[1])
        elif is_bank:
            type_label = "CHOAM Bank Storage"
        elif is_backpack:
            type_label = "Backpack"
        else:
            type_label = c.get("label") or cls or "Container"
        containers.append({
            "id": c.get("id"),
            # Real inventory id for the writers. Falls back to `id`, which is already
            # correct for the bank + vehicle branches and is the only value a
            # pre-deploy snapshot carries.
            "inv_id": c.get("inv_id") or c.get("id"),
            # Rank-1 owning controller. This list is ACCOUNT-scoped, so on a multi-
            # character account it spans characters. Anything acting AS a character
            # must filter on this (see `_pick_bank`). None on a pre-deploy snapshot.
            "owner_ctrl": c.get("owner_ctrl"),
            "custom_name": custom,
            "type_label": type_label,
            # bank is account-wide (no map); vehicles + placed containers show theirs
            "map": "" if (is_bank or is_backpack) else cmap,
            "item_count": item_count,
            "icon": _container_icon(cls),
            "is_bank": is_bank,
            "is_backpack": is_backpack,
            "is_pawn_storage": is_bank or is_backpack,
            "is_vehicle": is_vehicle,
            # Phase-0 capacity fields (carry through verbatim from the read path):
            # max_item_count = slot cap (-1 = unlimited, 0 = volume-gated only),
            # max_item_volume = volume cap. The Storage Manager sizes its grid to
            # max_item_count (5 cols wide, scrollable). Absent on older snapshots.
            "max_item_count": c.get("max_item_count"),
            "max_item_volume": c.get("max_item_volume"),
        })
    # Pawn storage is pinned first, then vehicles, then placed containers. This
    # keeps Bank and Backpack together without making world containers writable.
    containers.sort(key=lambda c: (0 if c["is_bank"] else
                                   (1 if c["is_backpack"] else
                                    (2 if c["is_vehicle"] else 3)),
                                   -(c["item_count"] or 0), c["type_label"], c["id"] or 0))
    return {
        "available": bool(payload.get("available")),
        "count": len(containers),
        "containers": containers,
    }


def _pick_bank(clist: list, owner_ctrl=None):
    """The CHOAM bank belonging to `owner_ctrl` (the character being acted as).

    The container read is ACCOUNT-scoped, so a multi-character account lists ONE BANK
    ROW PER CHARACTER. Taking the first is_bank row is wrong twice over: the shaper
    sorts banks by -item_count, so the row picked is whichever character happens to
    hold the most items -- which is not necessarily the selected one, and the read
    does not filter Deleted characters, so a re-rolled character's tombstoned bank
    can win. The player then sees that bank's grid next to the SELECTED character's
    Solari balance, and every write against it fails not_owner (the writer gates on
    the selected controller). Observed live 2026-07-16 on acct 3563: bank 7242 (ctrl
    7651, Deleted, 30 items) outranked bank 32530 (ctrl 35487, Active, 0 items).

    Falls back to the first is_bank row when owner_ctrl is unknown or the read
    predates `owner_ctrl`, which is exactly today's behaviour and is always correct
    on a single-character account (the overwhelmingly common case).
    """
    banks = [c for c in clist if c.get("is_bank")]
    if not banks:
        return None
    if owner_ctrl is not None:
        for c in banks:
            if c.get("owner_ctrl") is not None and str(c["owner_ctrl"]) == str(owner_ctrl):
                return c
    return banks[0]


def _pick_backpack(clist: list, owner_ctrl):
    """Return only the active selected character's backpack.

    Backpack rows did not exist in legacy snapshots, so there is no compatibility
    reason to fall back to another character. An unresolved or stale selected
    controller therefore fails closed instead of exposing an alternate backpack.
    """
    if owner_ctrl is None:
        return None
    for c in clist:
        if (c.get("is_backpack") and c.get("owner_ctrl") is not None
                and str(c["owner_ctrl"]) == str(owner_ctrl)):
            return c
    return None


def _pick_pawn_storage_pair(clist: list, owner_ctrl):
    """Strict selected-character Bank and Backpack pair, with no account fallback."""
    if owner_ctrl is None:
        return None, None
    bank = next((c for c in clist
                 if c.get("is_bank") and c.get("owner_ctrl") is not None
                 and str(c.get("owner_ctrl")) == str(owner_ctrl)), None)
    return bank, _pick_backpack(clist, owner_ctrl)


def _visible_storage_for_ctrl(clist: list, owner_ctrl):
    """Keep account-owned world storage, plus only this character's pawn storage."""
    out = []
    for c in clist:
        if not c.get("is_pawn_storage"):
            out.append(c)
        elif (owner_ctrl is not None and c.get("owner_ctrl") is not None
              and str(c["owner_ctrl"]) == str(owner_ctrl)):
            out.append(c)
    return out


def _blob_has_inv_id(payload) -> bool:
    """True when a mirror storage blob was built by a read that emits `inv_id`.

    The mirror blob is produced by the TELEMETRY COLLECTOR (/opt/lastsietch-telemetry, a
    separate long-running service), NOT by the per-call relay exec of
    scripts/dune-containers.py -- so it lags that script by however long it takes the
    collector to be updated. A pre-`inv_id` blob makes `_shape_containers_for_render`
    fall back to `id`, which silently re-arms the container-id namespace bug (box
    moves reject not_owner, or worse, collide onto the WRONG inventory). Treating such
    a blob as a MISS costs a relay round-trip and keeps writes correct; it self-heals
    the moment the collector emits inv_id. Container ids and inventory ids are
    different namespaces that overlap, so never widen the owned-inventory match.
    """
    cs = (payload or {}).get("containers") or []
    return bool(cs) and any(c.get("inv_id") is not None for c in cs)


async def _load_containers(account_id: int):
    """Best-effort fetch + shape of a player's storage containers. Returns None
    on any failure so the page degrades gracefully."""
    try:
        payload = mirror.get_storage_containers(account_id)
        # Correctness over speed: a blob with no inv_id cannot safely feed a writer.
        if payload is not None and not _blob_has_inv_id(payload):
            payload = None
        if payload is None:
            from routers.dune import _cached_player_containers
            payload = await _cached_player_containers(str(account_id))
        return _shape_containers_for_render(payload or {})
    except Exception as exc:
        logger.warning("portal: containers fetch failed: %s", exc)
        return None


def _decorate_portal_items(items: list) -> list:
    """Friendly name + real icon basename per item, mirroring the admin
    _decorate_items shaping plus an `icon` for the portal's icon grid.

    This is a whitelist: a key the reader sends but that is not repeated below
    does not reach the client. `augments` was selected by the SQL and dropped
    here, which is why the Augmented Gear page read empty for a player whose
    bank item demonstrably carried three."""
    out = []
    for it in (items or []):
        tpl = it.get("template_id") or ""
        out.append({
            "id": it.get("id"),
            "template_id": tpl,
            "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
            "icon": _icon_for(tpl),
            "durable": _is_durable(tpl),
            # False = game flags it non-tradeable (no exchange listing); gates
            # the storage popover's "List on Exchange" action.
            "tradeable": market_categories.is_tradeable(tpl),
            "stack_size": it.get("stack_size"),
            "quality": it.get("quality"),
            "cur_dur": it.get("cur_dur") or "",
            "max_dur": it.get("max_dur") or "",
            # Phase-0 grid slot (carried through verbatim); the Storage Manager
            # right/bank panels key each tile by position_index. Sparse, 0-based.
            "position_index": it.get("position_index"),
            # Installed augments, when the game-host reader is new enough to send
            # them; [] on an older script (partial deploy), never absent.
            "augments": _with_resolved(it.get("augments") or []),
            # Coarse category ("weapons"/"vehicles"/...). Resolved here rather
            # than read, so it is present for EVERY item -- including the many
            # thousands with no gear-stats.json row, which is what left the item
            # popover with no category line at all.
            "category": market_categories.classify(tpl),
        })
    return out


async def _load_container_items(account_id: int, container_id: str, page: int) -> dict:
    """Best-effort items fetch for one container owned by the account. Returns
    a render-ready envelope; `error='not_owned'` is propagated for the route to
    map to a 404."""
    try:
        raw = mirror.get_storage_items(account_id, container_id, page)
        if raw is None:
            from routers.dune import _cached_player_container_items
            raw = await _cached_player_container_items(str(account_id), str(container_id), page)
    except Exception as exc:
        logger.warning("portal: container-items fetch failed: %s", exc)
        return {"available": False, "error": "relay_unavailable", "items": [],
                "container_id": container_id, "page": page, "page_size": 100,
                "total_count": 0}
    return {
        "available": bool(raw.get("available")),
        "error": raw.get("error"),
        "stale": bool(raw.get("stale")),
        "container_id": container_id,
        "items": _decorate_portal_items(raw.get("items") or []),
        "total_count": raw.get("total_count") or 0,
        "page": raw.get("page") or page,
        "page_size": raw.get("page_size") or 100,
    }


def _decorate_vehicle_parts(parts: list) -> list:
    """Friendly name per installed part + carry the durability/wear numbers. `health_pct`
    is current/factory; `integrity_pct` is the decayed cap/factory (< 100 = permanent
    decay the Refurbish Vehicle button reverses)."""
    out = []
    for p in (parts or []):
        tpl = p.get("template_id") or ""
        out.append({
            "template_id": tpl,
            "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
            "icon": _icon_for(tpl),
            "current": p.get("current"),
            "cap": p.get("cap"),
            "factory_max": p.get("factory_max"),
            "health_pct": p.get("health_pct"),
            "integrity_pct": p.get("integrity_pct"),
        })
    return out


async def _load_vehicle_parts(account_id: int, container_id: str) -> dict:
    """Installed-parts durability for one owned vehicle (from dune.vehicle_modules, live
    via the relay). `error='not_owned'` is propagated for the route to map to a 404."""
    try:
        from relay import call_relay
        raw = await call_relay(
            f"/dune/player/{account_id}/_vehicle/{container_id}/_parts", timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: vehicle-parts fetch failed: %s", exc)
        return {"available": False, "error": "relay_unavailable",
                "container_id": container_id, "parts": []}
    return {
        "available": bool(raw.get("available")),
        "error": raw.get("error"),
        "container_id": container_id,
        "parts": _decorate_vehicle_parts(raw.get("parts") or []),
        "count": raw.get("count") or 0,
    }


def _require_linked_session(request: Request):
    """Shared gate for the container routes: returns (session, discord_id,
    account_id, link_row) or a Response to return early (302 / revoked page)."""
    session = get_portal_session(request)
    if not session:
        return None, RedirectResponse(url="/portal/", status_code=302)
    discord_id = portal_identity.actor_key(session)
    account_id = int(session.get("aid") or 0)
    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT character_name, discord_handle
                 FROM ls_account_links
                WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL""").replace("ls_account_links", portal_identity.link_table(conn)),
            (discord_id, account_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None, _render_link_revoked(request)
    return (session, discord_id, account_id, row), None


def _selected_ctrl(request: Request, account_id: int):
    """Portal multi-character: the controller_id the player is currently acting
    as, read from the signed selected-character cookie and bound to this
    account. Returns int or None (None = use the account's default pick). Cheap
    (signature + aid check only); the host progress script re-validates the
    controller against the account's live characters and falls back to the
    default pick if it is stale/forged, so this is fail-safe by construction."""
    token = request.cookies.get(SELCHAR_COOKIE)
    if not token:
        return None
    return verify_selchar_cookie(token, account_id)


@router.get("/portal/containers")
async def portal_containers(request: Request):
    """Legacy container browser. Folded into the Storage Manager; this exact path
    301-redirects to /portal/storage. Kept deliberately cheap: no session gate, no
    DB/mirror work on this hop (the destination handles auth + data)."""
    return RedirectResponse("/portal/storage", status_code=301)


async def _load_container_search(account_id: int, needle: str, owner_ctrl=None) -> dict:
    """Cross-container item search. Pulls the whole-account item index (cached),
    decorates friendly names, filters by the needle (against friendly name OR
    raw template_id), and groups matches by item with per-container quantities."""
    if len(needle) < 2:
        return {"too_short": True, "items": [], "available": True}
    try:
        payload = mirror.get_storage_search(account_id)
        if payload is None:
            from routers.dune import _cached_container_search
            payload = await _cached_container_search(str(account_id))
    except Exception as exc:
        logger.warning("portal: container-search fetch failed: %s", exc)
        return {"error": True, "items": [], "available": False}

    groups: dict = {}
    for r in (payload.get("rows") or []):
        # Search follows the selected character for pawn-side inventories. World
        # storage stays account-scoped, matching the rest of the Storage module.
        if r.get("container_type") in ("CHOAM Bank Storage", "Backpack"):
            if (owner_ctrl is None or r.get("owner_ctrl") is None
                    or str(r.get("owner_ctrl")) != str(owner_ctrl)):
                continue
        tpl = r.get("template_id") or ""
        name = _ITEM_NAMES.lookup_or_synthesize(tpl)
        if needle not in name.lower() and needle not in tpl.lower():
            continue
        g = groups.get(tpl)
        if g is None:
            g = groups[tpl] = {"template_id": tpl, "name": name,
                               "icon": _icon_for(tpl), "total_qty": 0, "containers": []}
        qty = int(r.get("qty") or 0)
        g["total_qty"] += qty
        g["containers"].append({
            "container_id": r.get("container_id"),
            "container_name": (r.get("container_name") or "").strip(),
            "container_type": r.get("container_type") or "Container",
            "qty": qty,
        })

    items = sorted(groups.values(), key=lambda g: -g["total_qty"])[:50]
    for g in items:
        g["containers"].sort(key=lambda c: -c["qty"])
    return {"items": items, "match_count": len(items),
            "available": bool(payload.get("available"))}


@router.get("/portal/containers/search")
async def portal_container_search(request: Request, q: str = ""):
    """Cross-container item search fragment for the signed-in player. Scoped to
    session.aid; returns matches grouped by item with which boxes hold them."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    ctrl, _ = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    results = await _load_container_search(
        active_account_id, (q or "").strip().lower(), ctrl)
    return templates.TemplateResponse(
        request, "portal/_fragments/container_search.html",
        {"results": results, "query": (q or "").strip()},
    )


@router.get("/portal/containers/{container_id}/items")
async def portal_container_items(request: Request, container_id: str, page: int = 1):
    """Items fragment for one of the signed-in player's containers. Ownership is
    enforced server-side (account_id = session.aid); a container_id that isn't
    the player's returns 404, so the URL can't be walked to read other chests."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    if not container_id.isdigit():
        raise HTTPException(status_code=400, detail="container_id must be a positive integer")
    page = max(int(page or 1), 1)

    items = await _load_container_items(active_account_id, container_id, page)
    if not items.get("available") and items.get("error") == "not_owned":
        raise HTTPException(status_code=404, detail="Container not found for your account")

    return templates.TemplateResponse(
        request, "portal/_fragments/container_items.html",
        {"container_items": items, "container_id": container_id},
    )


# ------------------------------------------------ storage manager -----------
#
# A 3-panel offline-only Storage Manager: LEFT = CHOAM Bank (Solari balance +
# a Credit<->Coin transfer control + the bank inv30 grid), MIDDLE = the owned
# container list, RIGHT = the selected container's grid (read-only this phase).
# Reads reuse the ownership-enforced container path (account_id = session.aid).
# The two write paths (WITHDRAW Credit->Coin, DEPOSIT/SWEEP Coin->Credit) are
# OFFLINE-only: the lastsietch-dune writer hard-gates on online_status + grace under a
# row lock, so this UI gate is advisory only. owner_ctrl is resolved SERVER-SIDE
# from the session and never accepted from the client. Phase 2 (drag-drop MOVE)
# is deferred; the right panel is read-only for now.

_STORAGE_WITHDRAW_CAP = 100000     # SolarisCoin stack max (per-transfer ceiling)
_STORAGE_MIN_INTERVAL_SECONDS = 3.0
_last_storage_at: dict = {}


def _storage_rate_ok(account_id: int) -> bool:
    now = time.monotonic()
    last = _last_storage_at.get(account_id)
    if last is not None and (now - last) < _STORAGE_MIN_INTERVAL_SECONDS:
        return False
    _last_storage_at[account_id] = now
    return True


async def _resolve_online(account_id: int):
    """Advisory online flag for the Storage Manager UI gate: True / False, or
    None when undetermined. Mirror scalars first (fast), live snapshot fallback.
    The writer re-checks online_status authoritatively, so this is display-only."""
    try:
        scalars = mirror.get_scalars(account_id)
        if scalars is not None:
            return bool(scalars["online"])
        from portal_quiz import _find_player_in_snapshot
        snap = await _find_player_in_snapshot(account_id)
        if snap:
            return bool(snap.get("online_status") == "Online")
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: storage online resolve failed acct=%s: %s", account_id, exc)
    return None


# Friendly text for the storage writer's error tokens (the writer/relay own these).
_STORAGE_ERROR_TEXT = {
    "player_online": "Log out of the game first, then reorganise. Storage edits "
                     "only apply while you are offline.",
    "no_bank": "We could not find your CHOAM bank. Open the bank in-game once, then retry.",
    "insufficient_bank": "Not enough banked Solari for that withdrawal.",
    "bank_full": "Your bank's item storage is full. Free a slot and try again.",
    "no_coins": "You have no Solari Coins to deposit.",
    "insufficient_coins": "You do not have that many Solari Coins on hand.",
    "write_failed": "The exchange could not complete that change. Please try again.",
}


def _storage_result_response(request: Request, *, ok: bool, message: str,
                             bank_display: str = None, status: int = 200):
    return templates.TemplateResponse(
        request, "portal/_fragments/storage_result.html",
        {"ok": ok, "message": message, "bank_display": bank_display},
        status_code=status,
    )


@router.get("/portal/storage")
async def portal_storage(request: Request):
    """The 3-panel Storage Manager for the signed-in player. Browsing always
    works (mirror-first reads); the transfer control is OFFLINE-only and is
    rendered view-only while the player is online. Auth: linked session."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, row = gate

    _touch_last_session(active_account_id)

    # Bank balance comes from the fresh authoritative progress read (same source
    # the SELL/BUY routes use); online is an advisory snapshot read.
    _ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    online = await _resolve_online(active_account_id)

    containers = await _load_containers(active_account_id)
    bank_container = None
    if containers and containers.get("available"):
        # Follow the SELECTED character: the list is account-scoped and spans every
        # character's bank, so an unfiltered pick can show another character's grid
        # next to this character's Solari balance. See `_pick_bank`.
        bank_container = _pick_bank(containers["containers"], _ctrl)

    bank_container_id = bank_container["id"] if bank_container else None
    bank_max_slots = bank_container.get("max_item_count") if bank_container else None
    bank_items = None
    if bank_container_id is not None:
        bank_items = await _load_container_items(
            active_account_id, str(bank_container_id), 1)

    # Advisory repair-cap state for the UI (live re-checked server-side on POST).
    # box + gear share the portal_repair bucket; everything is its own 24h bucket.
    repair_box_cd = repair_cooldown_remaining(
        "portal_repair", active_account_id,
        config.REPAIR_BOX_WINDOW_MIN * 60, config.REPAIR_BOX_CAP)
    repair_all_cd = repair_cooldown_remaining(
        "portal_repair_everything", active_account_id,
        config.REPAIR_ALL_WINDOW_HR * 3600, config.REPAIR_ALL_CAP)

    session_token = request.cookies.get(SESSION_COOKIE, "")
    ctx = {
        "active_character_name": row["character_name"],
        "containers": containers,
        "containers_error": containers is None,
        "repair_box_cooldown": repair_box_cd,
        "repair_all_cooldown": repair_all_cd,
        "repair_all_enabled": config.REPAIR_ALL_ENABLED,
        "repair_box_enabled": config.REPAIR_BOX_ENABLED,
        "bank_container_id": bank_container_id,
        "bank_items": bank_items,
        "bank_max_slots": bank_max_slots,
        "bank_solari": bank_solari,
        "bank_solari_display": f"{bank_solari:,}" if bank_solari is not None else None,
        # online: True (locked) / False (writes allowed) / None (undetermined ->
        # treat as locked, with a soft "could not verify" note).
        "online": online,
        "withdraw_cap": _STORAGE_WITHDRAW_CAP,
        "withdraw_cap_display": f"{_STORAGE_WITHDRAW_CAP:,}",
        "storage_csrf_token": csrf_for_session(session_token) if session_token else "",
        "logout_post_url": "/portal/logout",
    }
    ctx.update(base_ctx(request, discord_handle=row["discord_handle"]))
    return templates.TemplateResponse(request, "portal/storage.html", ctx)


async def _storage_write(request: Request, *, action: str, body: dict,
                         active_account_id: int, discord_id: str,
                         owner_ctrl: int, audit_extra: dict):
    """Shared dispatch + audit + result-fragment tail for WITHDRAW/DEPOSIT. The
    caller has already validated inputs, resolved owner_ctrl SERVER-SIDE, and
    passed the relay job body. Returns the storage_result.html fragment."""
    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay(
            f"/dune/storage/{action}", method="POST", json_body=body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: storage %s relay error acct=%s: %s",
                       action, active_account_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: storage %s failed acct=%s: %s",
                       action, active_account_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", f"portal_storage_{action}",
            str(owner_ctrl), ip,
            details=json.dumps({
                "account_id": active_account_id,
                "owner_ctrl": owner_ctrl,
                **audit_extra,
                "result": "ok" if ok else (err_token or "error"),
                # The writer returns `amount` (withdraw) / `swept_total` (deposit),
                # not `total`; record whichever is present so a money move is never
                # logged with a null amount.
                "amount": ((result or {}).get("amount") or (result or {}).get("swept_total"))
                          if isinstance(result, dict) else None,
                "bank_after": (result or {}).get("bank_after") if isinstance(result, dict) else None,
            }),
            success=ok,
        )

    if ok:
        # The write changed this player's bank + coin storage; drop the read cache
        # so the next container/storage read reflects it (best-effort; the mirror
        # path refreshes on its own sync cadence).
        try:
            from cache import invalidate
            invalidate("dune.player_containers", str(active_account_id))
        except Exception:  # noqa: BLE001 - cache drop is best-effort
            pass

    return ok, err_token, result


@router.post("/portal/storage/withdraw")
async def portal_storage_withdraw(request: Request):
    """Move banked Solari (Credit) into SolarisCoin items in the CHOAM bank's item
    storage (Credit -> Coin). OFFLINE-only (writer hard-gates). owner_ctrl resolved
    SERVER-SIDE. Per-transfer cap 100,000. Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    raw = (form.get("amount", "") or "").strip().replace(",", "")
    amount = int(raw) if raw.isdigit() else 0
    if amount <= 0:
        return _storage_result_response(
            request, ok=False, message="Enter a whole number of Solari above 0.",
            status=400)
    # Defense in depth: the writer also enforces the cap.
    if amount > _STORAGE_WITHDRAW_CAP:
        return _storage_result_response(
            request, ok=False,
            message=f"You can withdraw at most {_STORAGE_WITHDRAW_CAP:,} Solari at a time.",
            status=400)

    if not _storage_rate_ok(active_account_id):
        return _storage_result_response(
            request, ok=False,
            message="One transfer at a time, please. Wait a moment and retry.",
            status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _storage_result_response(
            request, ok=False,
            message="We could not verify your in-game character right now. "
                    "Please try again in a moment.", status=502)

    ok, err_token, result = await _storage_write(
        request, action="withdraw",
        body={"owner_ctrl": owner_ctrl, "amount": amount},
        active_account_id=active_account_id, discord_id=discord_id,
        owner_ctrl=owner_ctrl, audit_extra={"amount": amount})

    if ok:
        moved = result.get("amount") if isinstance(result, dict) else None
        moved = moved if isinstance(moved, int) else amount
        bank_after = result.get("bank_after") if isinstance(result, dict) else None
        bank_disp = f"{bank_after:,}" if isinstance(bank_after, int) else None
        msg = (f"Withdrew {moved:,} Solari into Coins in your bank storage. "
               "Pick them up from your CHOAM bank in-game.")
        return _storage_result_response(request, ok=True, message=msg,
                                        bank_display=bank_disp)

    friendly = _STORAGE_ERROR_TEXT.get(
        err_token, "That withdrawal could not be completed. Please try again.")
    status = 409 if err_token in ("player_online", "insufficient_bank", "bank_full") else 200
    return _storage_result_response(request, ok=False, message=friendly, status=status)


@router.post("/portal/storage/deposit")
async def portal_storage_deposit(request: Request):
    """Move SolarisCoin items back into banked Solari (Coin -> Credit). Two modes:
    `sweep` = deposit ALL Solari Coins across owned inventories; `amount` = deposit
    exactly N. OFFLINE-only (writer hard-gates). owner_ctrl resolved SERVER-SIDE.
    Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    mode = (form.get("mode", "") or "").strip().lower()
    if mode not in ("sweep", "amount"):
        return _storage_result_response(
            request, ok=False, message="That deposit request was malformed.",
            status=400)
    amount = None
    if mode == "amount":
        raw = (form.get("amount", "") or "").strip().replace(",", "")
        amount = int(raw) if raw.isdigit() else 0
        if amount <= 0:
            return _storage_result_response(
                request, ok=False, message="Enter a whole number of Solari above 0.",
                status=400)

    if not _storage_rate_ok(active_account_id):
        return _storage_result_response(
            request, ok=False,
            message="One transfer at a time, please. Wait a moment and retry.",
            status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _storage_result_response(
            request, ok=False,
            message="We could not verify your in-game character right now. "
                    "Please try again in a moment.", status=502)

    body = {"owner_ctrl": owner_ctrl, "mode": mode}
    if amount is not None:
        body["amount"] = amount
    audit_extra = {"mode": mode, "amount": amount}

    ok, err_token, result = await _storage_write(
        request, action="deposit", body=body,
        active_account_id=active_account_id, discord_id=discord_id,
        owner_ctrl=owner_ctrl, audit_extra=audit_extra)

    if ok:
        moved = result.get("swept_total") if isinstance(result, dict) else None
        moved_disp = f"{moved:,}" if isinstance(moved, int) else "your"
        bank_after = result.get("bank_after") if isinstance(result, dict) else None
        bank_disp = f"{bank_after:,}" if isinstance(bank_after, int) else None
        if mode == "sweep":
            msg = f"Deposited all {moved_disp} Solari Coins to your bank."
        else:
            msg = f"Deposited {moved_disp} Solari Coins to your bank."
        return _storage_result_response(request, ok=True, message=msg,
                                        bank_display=bank_disp)

    friendly = _STORAGE_ERROR_TEXT.get(
        err_token, "That deposit could not be completed. Please try again.")
    status = 409 if err_token in ("player_online", "no_coins", "insufficient_coins") else 200
    return _storage_result_response(request, ok=False, message=friendly, status=status)


@router.get("/portal/storage/{container_id}/items")
async def portal_storage_items(request: Request, container_id: str, page: int = 1):
    """Slot-grid items fragment for the Storage Manager's RIGHT panel (read-only
    this phase). Same ownership enforcement as the container browser: account_id =
    session.aid, so a container_id the player does not own 404s. Resolves the
    container's slot cap (max_item_count) server-side to size the grid."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    if not container_id.isdigit():
        raise HTTPException(status_code=400, detail="container_id must be a positive integer")
    page = max(int(page or 1), 1)

    items = await _load_container_items(active_account_id, container_id, page)
    if not items.get("available") and items.get("error") == "not_owned":
        raise HTTPException(status_code=404, detail="Container not found for your account")

    # Slot cap + a display label for the panel header, resolved server-side from
    # the owned-container list (mirror-fast). Display-only; not security-relevant.
    max_slots = None
    type_label = "Container"
    containers = await _load_containers(active_account_id)
    if containers and containers.get("available"):
        match = next((c for c in containers["containers"]
                      if str(c.get("id")) == container_id), None)
        if match:
            max_slots = match.get("max_item_count")
            type_label = match.get("custom_name") or match.get("type_label") or "Container"

    return templates.TemplateResponse(
        request, "portal/_fragments/storage_items.html",
        {"container_items": items, "container_id": container_id,
         "max_slots": max_slots, "type_label": type_label},
    )


# ----------------------------------------------------- repair ---------------
#
# Item Repair: restore the durability of the signed-in player's OWN gear while
# OFFLINE. Three tiers calling the lastsietch-relay repair endpoints (dev-1 owns the
# relay + writer; this layer only resolves owner_ctrl SERVER-SIDE, gates CSRF +
# rolling caps, and maps the writer's JSON to friendly copy). owner_ctrl is the
# caller's player_controller_id, resolved from the session-linked account, and is
# NEVER read from the request body. The writer re-validates ownership and the
# offline gate authoritatively; the UI/cap here are advisory convenience.
#
# Caps (audit_log windowed COUNT, per account_id): box + gear share the
# `portal_repair` bucket (REPAIR_BOX_CAP / REPAIR_BOX_WINDOW_MIN); everything is
# the separate `portal_repair_everything` bucket (REPAIR_ALL_CAP / 24h).

# Friendly text for the repair writer's error tokens (the writer/relay own these).
_REPAIR_ERROR_TEXT = {
    "player_online": ("This repair only applies while you are logged out of Dune. "
                      "If you are still in-game, log out and try again. If you already "
                      "logged out, give the server a minute to catch up, then retry."),
    "not_owned": "That looks like it is not yours to repair. Refresh and try again.",
    "no_inv": "We could not find anything to repair there. Refresh and try again.",
    "write_failed": "The repair could not be completed. Please try again.",
}


def _repair_result_response(request: Request, *, ok: bool, message: str,
                            status: int = 200):
    """Render the shared storage_result fragment for a repair outcome. Reuses the
    market-buy-result markup so portal-storage.js's resultOk() detects success."""
    return templates.TemplateResponse(
        request, "portal/_fragments/storage_result.html",
        {"ok": ok, "message": message, "bank_display": None},
        status_code=status,
    )


def repair_cooldown_remaining(action: str, account_id: int,
                              window_seconds: int, cap: int,
                              target: str | None = None) -> int:
    """Seconds until a capped repair bucket frees a slot, or 0 when under cap.
    Looks at the oldest of the most-recent `cap` successful audit rows in the
    window; once that row ages out the count drops below cap. Advisory only.
    `target` is the audit-row key the bucket counts by (defaults to the account;
    the vehicle tier passes `account:inv_id` so each vehicle is its own bucket)."""
    tgt = target if target is not None else str(account_id)
    since = (datetime.now(timezone.utc)
             - timedelta(seconds=window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp FROM audit_log WHERE action = ? AND target = ? "
            "AND success = 1 AND timestamp >= ? ORDER BY timestamp ASC",
            (action, tgt, since),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < cap:
        return 0
    oldest = rows[len(rows) - cap]["timestamp"]
    try:
        oldest_dt = datetime.fromisoformat(str(oldest))
    except ValueError:
        return 0
    if oldest_dt.tzinfo is None:
        oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
    remaining = (oldest_dt + timedelta(seconds=window_seconds)
                 - datetime.now(timezone.utc)).total_seconds()
    return int(remaining) if remaining > 0 else 0


def _fmt_mmss(secs: int) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def _fmt_relative(secs: int) -> str:
    secs = max(0, int(secs))
    h, m = secs // 3600, (secs % 3600) // 60
    if h and m:
        return f"in {h}h {m}m"
    if h:
        return f"in {h}h"
    if m:
        return f"in {m}m"
    return "shortly"


def _repair_csrf_ok(request: Request, form) -> bool:
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    return bool(session_token) and validate_csrf(provided, csrf_for_session(session_token))


async def _repair_write(request: Request, *, action: str, body: dict,
                        active_account_id: int, discord_id: str,
                        owner_ctrl: int, audit_action: str,
                        audit_target: str | None = None):
    """Dispatch a repair job to the relay, audit it, drop the read cache. Returns
    (ok, err_token, result). owner_ctrl is already resolved SERVER-SIDE. `audit_target`
    is the audit-row key the rate-limit bucket counts by (defaults to the account; the
    vehicle tier passes `account:inv_id` so its cooldown is per-vehicle)."""
    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay(
            f"/dune/repair/{action}", method="POST", json_body=body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: repair %s relay error acct=%s: %s",
                       action, active_account_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: repair %s failed acct=%s: %s",
                       action, active_account_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", audit_action,
            audit_target or str(active_account_id), ip,
            details=json.dumps({
                "account_id": active_account_id,
                "owner_ctrl": owner_ctrl,
                "tier": action,
                "semantic": (result or {}).get("semantic") if isinstance(result, dict) else None,
                "repaired_count": (result or {}).get("repaired_count") if isinstance(result, dict) else None,
                "result": "ok" if ok else (err_token or "error"),
            }),
            success=ok,
        )

    if ok:
        try:
            from cache import invalidate
            invalidate("dune.player_containers", str(active_account_id))
        except Exception:  # noqa: BLE001 - cache drop is best-effort
            pass

    return ok, err_token, result


def _repair_success_message(action: str, repaired_count) -> str:
    n = repaired_count if isinstance(repaired_count, int) else 0
    if n <= 0:
        return "Everything here is already at full durability."
    noun = "item" if n == 1 else "items"
    if action == "everything":
        # Scope narrowed 2026-08-03 to the pawn side (backpack, worn, hotbar, CHOAM bank).
        # Bases, vehicles and guild storage are no longer touched: their contents live in
        # the server's memory while the base is loaded, so a write there never surfaced.
        return (f"Repaired and refurbished {n:,} {noun} in your backpack, on your character "
                "and in your CHOAM bank. Log back in to see them restored in-game.")
    if action == "vehicle":
        part_noun = "part" if n == 1 else "parts"
        return (f"Refurbished {n:,} installed {part_noun} on this vehicle, restoring their "
                "maximum durability without dismounting. Relog to see them restored in-game.")
    if action == "box":
        # Honest copy: a container's contents are held by the server for as long as the
        # base is loaded, so this write cannot show up on a relog the way gear does.
        return (f"Repaired {n:,} {noun} in that container. Containers are held by the server "
                "while your base is loaded, so this will not appear in-game until the next "
                "server restart.")
    return f"Repaired {n:,} {noun}. Relog to see them restored in-game."


async def _dispatch_repair(request: Request, *, action: str, extra_body: dict,
                           audit_action: str, cap_action: str,
                           window_seconds: int, cap: int, cooldown_msg):
    """Shared body for the three repair routes: linked-session + CSRF gate, the
    rolling cap check, SERVER-SIDE owner_ctrl resolve, dispatch, friendly copy."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _repair_csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    inv_id = None
    if action in ("box", "vehicle"):
        raw = (form.get("inv_id", "") or "").strip()
        inv_id = int(raw) if raw.isdigit() else 0
        if inv_id <= 0:
            return _repair_result_response(
                request, ok=False,
                message=("That vehicle could not be identified. Refresh and try again."
                         if action == "vehicle"
                         else "That container could not be identified. Refresh and try again."),
                status=400)
        # ID NAMESPACE TRANSLATION (see `_shape_containers_for_render`). Despite the
        # field name the client sends the READ container id, which for a placed box is
        # a PLACEABLE id, while the repair writer gates on INVENTORY ids
        # (dune-repair-write.py owned_inv_sql). A box id therefore matched nothing: the
        # target set came back empty and this reported SUCCESS having repaired 0 items,
        # silently, since it shipped. Bank/vehicle were unaffected (their container id
        # already IS the inventory id).
        containers = await _load_containers(active_account_id)
        clist = containers["containers"] if containers and containers.get("available") else []
        box = next((c for c in clist if str(c.get("id")) == str(inv_id)), None)
        if box is not None:
            inv_id = box.get("inv_id") or inv_id

    # Rolling cap (checked BEFORE dispatch; a row is recorded only on a successful write
    # inside _repair_write). The vehicle tier is PER-VEHICLE (account:inv_id); the others
    # are per-account.
    cap_target = (f"{active_account_id}:{inv_id}" if action == "vehicle"
                  else str(active_account_id))
    from auth import recent_action_count
    since = (datetime.now(timezone.utc)
             - timedelta(seconds=window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    recent = recent_action_count(
        cap_action, since, targets=[cap_target])
    if recent >= cap:
        remaining = repair_cooldown_remaining(
            cap_action, active_account_id, window_seconds, cap, target=cap_target)
        return _repair_result_response(
            request, ok=False, message=cooldown_msg(remaining), status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _repair_result_response(
            request, ok=False,
            message="We could not verify your in-game character right now. "
                    "Please try again in a moment.", status=502)

    body = {"action": action, "owner_ctrl": owner_ctrl}
    if inv_id is not None:
        body["inv_id"] = inv_id
    body.update(extra_body)

    ok, err_token, result = await _repair_write(
        request, action=action, body=body,
        active_account_id=active_account_id, discord_id=discord_id,
        owner_ctrl=owner_ctrl, audit_action=audit_action, audit_target=cap_target)

    if ok:
        repaired = result.get("repaired_count") if isinstance(result, dict) else None
        return _repair_result_response(
            request, ok=True, message=_repair_success_message(action, repaired))

    friendly = _REPAIR_ERROR_TEXT.get(
        err_token, "The repair could not be completed. Please try again.")
    status = 409 if err_token == "player_online" else 200
    return _repair_result_response(request, ok=False, message=friendly, status=status)


@router.post("/portal/repair/box")
async def portal_repair_box(request: Request):
    """Vanilla-repair every durable row in ONE owned container (tops Current up to
    Decayed). inv_id from the body is sanity-checked positive; the writer
    re-validates ownership. OFFLINE-only (writer hard-gates). Shares the box+gear
    cap. Auth: linked session + CSRF."""
    from config import REPAIR_BOX_WINDOW_MIN, REPAIR_BOX_CAP
    # Kill-switch parity with the V2 route (2026-08-27): the tier was pulled
    # 2026-08-03 because a container held in server RAM makes the DB write
    # invisible in-game, but this V1 route kept accepting POSTs.
    if not config.REPAIR_BOX_ENABLED:
        return _repair_result_response(
            request, ok=False,
            message="Container repair is paused. Move items to your backpack or "
                    "CHOAM bank and use the repair there instead.")
    return await _dispatch_repair(
        request, action="box", extra_body={},
        audit_action="portal_repair", cap_action="portal_repair",
        window_seconds=REPAIR_BOX_WINDOW_MIN * 60, cap=REPAIR_BOX_CAP,
        cooldown_msg=lambda s: f"Repair is on cooldown. Next available in {_fmt_mmss(s)}.")


@router.post("/portal/repair/gear")
async def portal_repair_gear(request: Request):
    """Vanilla-repair the caller's backpack, worn armor and hotbar/weapons (inv
    types 0/1/15). OFFLINE-only (writer hard-gates). Shares the box+gear cap.
    owner_ctrl resolved SERVER-SIDE. Auth: linked session + CSRF."""
    from config import REPAIR_BOX_WINDOW_MIN, REPAIR_BOX_CAP
    return await _dispatch_repair(
        request, action="gear", extra_body={},
        audit_action="portal_repair", cap_action="portal_repair",
        window_seconds=REPAIR_BOX_WINDOW_MIN * 60, cap=REPAIR_BOX_CAP,
        cooldown_msg=lambda s: f"Repair is on cooldown. Next available in {_fmt_mmss(s)}.")


@router.post("/portal/repair/vehicle")
async def portal_repair_vehicle(request: Request):
    """Refurbish every durable part on ONE owned vehicle IN PLACE — restores Current AND
    Decayed up to factory Max on the mounted parts, no dismounting. The client sends the
    selected vehicle's container id; _dispatch_repair translates it to the real inventory
    id and the writer expands it to the whole vehicle's inventories, re-validating that the
    caller holds a permission rank on that vehicle. OFFLINE-only (writer hard-gates). Own
    rolling cap at the box-repair cadence. Auth: linked session + CSRF."""
    from config import (REPAIR_VEHICLE_ENABLED, REPAIR_VEHICLE_WINDOW_MIN,
                        REPAIR_VEHICLE_CAP)
    if not REPAIR_VEHICLE_ENABLED:
        return _repair_result_response(
            request, ok=False,
            message="Vehicle Refurbish is temporarily unavailable while we verify it. "
                    "Per-item and Backpack &amp; Equipped repair still work.")
    return await _dispatch_repair(
        request, action="vehicle", extra_body={},
        audit_action="portal_repair_vehicle", cap_action="portal_repair_vehicle",
        window_seconds=REPAIR_VEHICLE_WINDOW_MIN * 60, cap=REPAIR_VEHICLE_CAP,
        cooldown_msg=lambda s: f"Vehicle Refurbish is on cooldown. Next available in {_fmt_mmss(s)}.")


@router.post("/portal/repair/everything")
async def portal_repair_everything(request: Request):
    """Premium Repair & Refurbish across all owned storage + bank + backpack +
    equipped (DD excluded), restoring Current AND Decayed up to factory Max. Once
    per 24h. OFFLINE-only (writer hard-gates). owner_ctrl resolved SERVER-SIDE.
    Auth: linked session + CSRF."""
    from config import REPAIR_ALL_WINDOW_HR, REPAIR_ALL_CAP, REPAIR_ALL_ENABLED
    if not REPAIR_ALL_ENABLED:
        return _repair_result_response(
            request, ok=False,
            message="Full Repair &amp; Refurbish is temporarily unavailable while we "
                    "verify it. Per-item and Backpack &amp; Equipped repair still work.")
    return await _dispatch_repair(
        request, action="everything", extra_body={},
        audit_action="portal_repair_everything", cap_action="portal_repair_everything",
        window_seconds=REPAIR_ALL_WINDOW_HR * 3600, cap=REPAIR_ALL_CAP,
        cooldown_msg=lambda s: f"Full refurbish is once per day. Next available {_fmt_relative(s)}.")


# ============================================================================
# V2 Storage Manager — NEW sibling JSON endpoints (SvelteKit portal-nextgen).
#
# These are ADDITIVE. The V1 HTML `/portal/storage` page and its POST routes
# (withdraw/deposit/repair) above stay UNTOUCHED and live. Every V2 endpoint is
# namespaced under `.../v2...` so it never collides with a V1 route, returns a
# JSON envelope (never a template), and reuses the SAME proven helpers:
# `_resolve_buyer_ctrl_and_bank` (server-resolved owner_ctrl), `_load_containers`
# /`_shape_containers_for_render`, `_load_container_items`/`_decorate_portal_items`,
# `_storage_write`/`_repair_write` (same relay chain + caps + rate-limit + offline
# gate). owner_ctrl / sender / account_id are ALWAYS server-resolved from the
# session, NEVER read from the client body.
#
# Envelope: success {ok:true, ...}. Failure {ok:false, error:"<token>",
# message:"<friendly>"} with a non-2xx status. Two SOFT gates return 200 on
# purpose: unsupported container MOVE returns
# {ok:false, error:"native_move_unavailable"}; Tier 5 TRANSFER while
# DARK -> {ok:true, status:"deferred"} (never a fake success).

# Supported gates resolve through feature_flags; the game-host writer re-gates.
# Broad container MOVE remains unavailable until a native backend is implemented.
def _v2_move_enabled() -> bool:
    # SQL-only relocation cannot synchronize loaded containers.
    return False


def _v2_pawn_move_enabled() -> bool:
    """Advisory portal gate for Bank and Backpack moves. Writer file gate wins."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_STORAGE_PAWN_MOVE_ENABLED", "0")


def _v2_market_backpack_enabled() -> bool:
    """Advisory portal gate for LISTING out of the backpack. Default OFF.
    /etc/lastsietch/market-sell-backpack-enabled on the game host is the authority and the
    only security boundary; this copy decides whether the UI offers the door at all,
    so the player is not sent into a refusal the writer would answer anyway."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_MARKET_SELL_BACKPACK_ENABLED", "0")


# Upper bound on containers scanned by the Augmented Gear roll-up. The live max
# is 71 owned containers (55 non-empty) for the heaviest account on the server, so
# this clears real usage with room to spare while still capping the worst case on
# a mirror miss, where each container costs a relay round trip.
_AUGMENTED_SCAN_CAP = 120


def _v2_augment_enabled() -> bool:
    """Kill-switch for the player-facing augment reroll/swap path. Default OFF.
    Mirrors LASTSIETCH_AUGMENT_ENABLED on the game host, which is what the writer itself
    gates on -- this copy only decides whether the portal offers the door, and is
    NOT the security boundary. Both must be on for a write to land.

    Resolved by feature_flags, re-read on every call and never memoised: the
    data/feature_flags.json override wins, then the process environment
    (os.environ.get("LASTSIETCH_AUGMENT_ENABLED", "0") == "1"), then the coded
    default OFF. The owner-only Systems toggle writes that override, so a flip
    lands on the next request without restarting lastsietch-admin."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_AUGMENT_ENABLED", "0")


def _v2_targeted_augment_upgrade_enabled() -> bool:
    """Kill-switch for the TRANSPLANT swap path (the incoming augment keeps the
    consumed copy's real rolls instead of having them redrawn). Default OFF.

    This gate has to cover TWO things or it does not roll anything back: the
    same-name/same-grade 409 below, AND the `roll_mode` actually dispatched to the
    writer. Gating only the 409 would relax the picker while still redrawing the
    rolls, which is the one combination that is worse than either end state."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_AUGMENT_TARGETED_UPGRADE_ENABLED", "0")


def _v2_transfer_enabled() -> bool:
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_ITEM_TRANSFER_ENABLED", "0")


# How many item-grid pages to drop per container after a move. The grid cache is
# keyed per (account, container, page) and there is no per-account prefix drop, so
# this is a bounded sweep, not a guarantee. V2 only ever reads page 1
# (`api.containerItems` sends no page param); 3 covers V1's paginated drawer for a
# normal container. A move into page 4+ of a huge container can still show a stale
# grid for the cache's 30s TTL -- acceptable, and it self-heals.
_MOVE_INVALIDATE_PAGES = 3

# Friendly text for the MOVE writer's error tokens (the writer/relay own these).
_MOVE_ERROR_TEXT = {
    "player_online": "Log out of the game first, then reorganise. Moves only apply "
                     "while you are offline.",
    "item_not_found": "That item is no longer where you left it. Refresh and try again.",
    "not_owner": "That is not yours to move. Refresh and try again.",
    "dst_no_slots": "That container has no stackable slots. Pick another destination.",
    "dst_full_slots": "That container is full. Free a slot and try again.",
    "dst_full_volume": "That container does not have room for this item's volume.",
    "dst_on_deep_desert": "Deep Desert storage can't be a move destination.",
    "move_disabled": "Drag-and-drop moves are not turned on yet.",
    "native_move_unavailable": "Moving items between base or vehicle containers is currently unavailable.",
    "move_failed": "That move could not be completed. Refresh and try again.",
    "write_failed": "That move could not be completed. Please try again.",
}


_PAWN_MOVE_ERROR_TEXT = {
    "player_online": "Log out of the game first, then move the item.",
    "item_not_found": "That item is no longer there. Refresh and try again.",
    "not_owner": "That item is not in this character's Bank or Backpack.",
    "no_bank": "We could not find this character's CHOAM bank.",
    "no_backpack": "We could not find this character's backpack.",
    "dst_no_slots": "The destination does not accept item stacks.",
    "dst_full_slots": "The destination is full. Free a slot and try again.",
    "dst_full_volume": "The destination does not have room for this stack.",
    "idempotency_conflict": "That move request was already used for a different item.",
    "pawn_move_disabled": "Bank and Backpack transfers are not open yet.",
    "move_failed": "That move could not be completed. Refresh and try again.",
    "write_failed": "That move could not be completed. Please try again.",
}


# Friendly text for the Tier 5 TRANSFER writer's error tokens. The writer's own messages
# name bank inventory ids and account ids, so they are operator-facing only and must never
# be handed to a player: everything the player sees comes from this dict.
_TRANSFER_ERROR_TEXT = {
    "player_online": "Log out of the game first, then send. An item can only leave your "
                     "bank while you are offline.",
    "no_bank": "We could not find a CHOAM bank for one of you. Open the bank in-game "
               "once, then retry.",
    "item_not_found": "That item is no longer in your bank. Refresh and try again.",
    "bank_full": "That player's bank is full. Ask them to free a slot and try again.",
    "rate_limited": "You have sent as many items as you can for now. Try again later.",
    "take_failed": "That send could not be completed. Refresh your bank and try again.",
    "transfer_failed": "That transfer could not be completed.",
    "write_failed": "That transfer could not be completed. Please try again.",
}

_TRANSFER_HISTORY_DEFAULT = 20
_TRANSFER_HISTORY_MAX = 50


def _transfer_caps(sender_identity: str) -> dict:
    """Additive cap preview for the storage overview, off the same mirror the
    transfer route reserves against. Degrades to an empty dict rather than taking
    the whole overview down: the table is lazy and this is decoration, not a gate.
    The pair cap is the constant, so no recipient is named here."""
    try:
        import portal_transfer_events
        caps = portal_transfer_events.caps(sender_identity, None) or {}
    except Exception:  # noqa: BLE001
        logger.warning("portal: transfer caps unavailable", exc_info=True)
        return {}
    return {"transfer_daily_cap": caps.get("daily_cap"),
            "transfer_daily_used": caps.get("daily_used"),
            "transfer_pair_cap": caps.get("pair_cap")}


def _v2_ok(data: dict = None, status: int = 200):
    payload = {"ok": True}
    if data:
        payload.update(data)
    return JSONResponse(payload, status_code=status)


def _v2_err(error: str, message: str, status: int = 400):
    return JSONResponse({"ok": False, "error": error, "message": message},
                        status_code=status)


def _require_linked_session_json(request: Request):
    """JSON-API gate: returns (gate_tuple, None) or (None, 401_json). Both an
    unauthenticated request and a revoked link fail closed as a 401 JSON envelope
    (the frontend treats non-2xx as a throw), never a 302 to an HTML page."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return None, _v2_err("unauthenticated",
                             "Sign in to the portal to use storage.", status=401)
    return gate, None


async def _v2_body_and_csrf(request: Request):
    """Read a JSON body (via _read_body; V2 mutations never use FastAPI Form) and
    validate CSRF. Returns (body_dict, csrf_ok)."""
    try:
        body = await _read_body(request)
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided = request.headers.get(CSRF_HEADER, "") or (body.get("csrf_token", "") or "")
    csrf_ok = bool(session_token) and validate_csrf(provided, csrf_for_session(session_token))
    return body, csrf_ok


def _v2_container(c: dict) -> dict:
    """Shape one `_shape_containers_for_render` container into the V2 JSON contract."""
    cmap = c.get("map") or ""
    return {
        "id": c.get("id"),
        "name": c.get("custom_name") or "",
        "type": c.get("type_label") or "Container",
        "location": cmap,
        "item_count": c.get("item_count") or 0,
        "mic": c.get("max_item_count"),
        "miv": c.get("max_item_volume"),
        "icon": c.get("icon"),
        "is_bank": bool(c.get("is_bank")),
        "is_backpack": bool(c.get("is_backpack")),
        "is_pawn_storage": bool(c.get("is_pawn_storage")),
        "is_vehicle": bool(c.get("is_vehicle")),
        "is_deep_desert": "Deep Desert" in cmap,
        "cached": None,
    }


def _v2_item(it: dict) -> dict:
    """Shape one `_decorate_portal_items` item into the V2 JSON contract."""
    return {
        "item_id": it.get("id"),
        "template": it.get("template_id"),
        "name": it.get("name"),
        "icon": it.get("icon"),
        "position_index": it.get("position_index"),
        "stack_size": it.get("stack_size"),
        "tradeable": bool(it.get("tradeable")),
        "durable": bool(it.get("durable")),
        "durability": {
            "cur": it.get("cur_dur") or "",
            "max": it.get("max_dur") or "",
            "quality": it.get("quality"),
        },
        # [] on an older game-host script that doesn't send this key yet (partial
        # deploy), same fail-soft sanitizer the character equipped card uses.
        "augments": _sanitize_augments(it.get("augments")),
        # Same key name the Character equipped payload already uses, so the
        # shared item card can read one field regardless of which page it is on.
        "category": it.get("category") or "",
    }


@router.get("/portal/storage/v2")
async def portal_storage_v2(request: Request):
    """V2 overview: bank (Solari + inv30 grid), owned containers, caps, flags,
    advisory online state, CSRF token. Reads only (mirror-first). Auth: linked
    session (JSON 401 otherwise)."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, row = gate
    _touch_last_session(active_account_id)

    _ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    online = await _resolve_online(active_account_id)

    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    # Follow the SELECTED character (see `_pick_bank`): an unfiltered pick can hand
    # back another character's bank -- and the shaper's -item_count sort makes that
    # the DEFAULT outcome whenever an alt (or a tombstoned re-roll) holds more items.
    bank_container = _pick_bank(clist, _ctrl)
    if (bank_container is not None and _ctrl is not None
            and (bank_container.get("owner_ctrl") is None
                 or str(bank_container.get("owner_ctrl")) != str(_ctrl))):
        bank_container = None
    backpack_container = _pick_backpack(clist, _ctrl)
    visible_clist = _visible_storage_for_ctrl(clist, _ctrl)

    bank_inv_id = bank_mic = bank_miv = None
    bank_items = []
    if bank_container is not None:
        bank_inv_id = bank_container.get("id")
        bank_mic = bank_container.get("max_item_count")
        bank_miv = bank_container.get("max_item_volume")
        raw = await _load_container_items(active_account_id, str(bank_inv_id), 1)
        bank_items = [_v2_item(it) for it in (raw.get("items") or [])]

    backpack_inv_id = backpack_mic = backpack_miv = None
    backpack_items = []
    if backpack_container is not None:
        backpack_inv_id = backpack_container.get("id")
        backpack_mic = backpack_container.get("max_item_count")
        backpack_miv = backpack_container.get("max_item_volume")
        raw = await _load_container_items(active_account_id, str(backpack_inv_id), 1)
        backpack_items = [_v2_item(it) for it in (raw.get("items") or [])]

    session_token = request.cookies.get(SESSION_COOKIE, "")
    return _v2_ok({
        # online: True (locked) / False (writes allowed) / None (undetermined -> LOCKED).
        "online": online,
        "offline_ok": online is False,
        "bank": {
            "solari": bank_solari,
            "inv_id": bank_inv_id,
            "mic": bank_mic,
            "miv": bank_miv,
            "items": bank_items,
        },
        "backpack": {
            "inv_id": backpack_inv_id,
            "mic": backpack_mic,
            "miv": backpack_miv,
            "items": backpack_items,
        },
        "containers": [_v2_container(c) for c in visible_clist],
        # transfer_* are additive: the withdraw cap keeps its exact key and value.
        "caps": {"withdraw_cap": _STORAGE_WITHDRAW_CAP,
                 **_transfer_caps(discord_id)},
        "flags": {
            "move_enabled": _v2_move_enabled(),
            "pawn_move_enabled": _v2_pawn_move_enabled(),
            "market_sell_backpack_enabled": _v2_market_backpack_enabled(),
            "transfer_enabled": _v2_transfer_enabled(),
        },
        "character_name": row["character_name"],
        "csrf_token": csrf_for_session(session_token) if session_token else "",
    })


@router.get("/portal/containers/v2")
async def portal_containers_v2(request: Request):
    """V2 owned-container list (JSON). Scoped to session.aid. Read-only."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    _touch_last_session(active_account_id)

    ctrl, _ = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))

    containers = await _load_containers(active_account_id)
    if containers is None:
        return _v2_err("unavailable", "Storage is unavailable right now.", status=503)
    clist = containers["containers"] if containers.get("available") else []
    visible_clist = _visible_storage_for_ctrl(clist, ctrl)
    return _v2_ok({"containers": [_v2_container(c) for c in visible_clist]})


async def _augmented_scan(account_id: int, owner_ctrl=None):
    """(rows, scanned, truncated) for every augmented item across the account's
    OWNED containers + bank. Shared by the read endpoint and by the reroll/swap
    write handler's ownership check, so the two can never disagree about which
    items are in scope.

    Bounded on purpose: a container scan is cheap against the mirror but falls
    back to a live relay call per container on a mirror miss, and an unbounded
    loop there would turn one page load into 70+ ssh round trips."""
    containers = await _load_containers(account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    if owner_ctrl is not None:
        clist = _visible_storage_for_ctrl(clist, owner_ctrl)
    rows = []
    scanned = 0
    truncated = False
    for c in clist:
        if (c.get("item_count") or 0) <= 0:
            continue
        if scanned >= _AUGMENTED_SCAN_CAP:
            truncated = True
            break
        scanned += 1
        try:
            raw = await _load_container_items(account_id, str(c.get("id")), 1)
        except Exception as exc:  # noqa: BLE001 - one bad container never kills the page
            logger.warning("portal: augmented scan failed acct=%s container=%s: %s",
                           account_id, c.get("id"), exc)
            continue
        for it in (raw.get("items") or []):
            shaped = _v2_item(it)
            if not shaped.get("augments"):
                continue
            rows.append({
                "item": shaped,
                "augments": shaped["augments"],
                "container_id": c.get("id"),
                # Keys must match what the container SHAPER emits, which is
                # custom_name / type_label. It never emits name/label/class, so
                # this fell through to the literal "Container" for EVERY row --
                # bank, backpack, vehicle and placed container alike. Surfaced
                # 2026-08-18 while verifying the backpack change: an owner-controlled character's
                # backpack item reported loc='Container'. Worse now that the
                # backpack is augmentable, since the panel calls it a container
                # directly above text explaining that containers cannot be used.
                "container_name": (c.get("custom_name") or c.get("type_label")
                                   or "Container"),
                "is_deep_desert": "Deep Desert" in (c.get("map") or ""),
                "is_bank": bool(c.get("is_bank")),
                "is_backpack": bool(c.get("is_backpack")),
                # 🔴 Can this row be rerolled/swapped at all? PAWN-SIDE storage
                # only -- the CHOAM bank (inventory_type 30) and the character's
                # own BACKPACK (type 0). Both hang off the pawn actor and
                # re-hydrate from the DB at login, and the writer already
                # offline-gates every augment write, so neither can be clobbered
                # by a live partition.
                #
                # A placed container or vehicle is different in kind: its contents
                # live in a partition server's RAM for as long as that partition is
                # up (28 of 30 at any moment), where our UPDATE is retracted and
                # our DELETE resurrected. The writer refuses those outright, so a
                # button here would only produce a not_owner after the player had
                # committed to the action.
                #
                # This deliberately mirrors dune-augment.py's ownership check,
                # which is `inv.actor_id = v_pawn` -- "backpack / worn / hotbar /
                # CHOAM bank". Limiting this to the bank made the PORTAL stricter
                # than the writer it was protecting: a player with an augmented
                # weapon in their backpack was told to "put it in the CHOAM bank",
                # about an item already on their character.
                "can_augment": bool(c.get("is_pawn_storage")),
            })
    return rows, scanned, truncated


@router.get("/portal/storage/v2/augmented")
async def portal_storage_v2_augmented(request: Request):
    """Every item across the player's OWNED containers + bank that carries an
    installed augment, in ONE request. Read-only. Auth: linked session.

    WHY THIS EXISTS AS AN ENDPOINT. The Augmented Gear page originally fanned out
    on the client: one `/portal/containers/v2/{id}/items` call per owned
    container, awaited as a single Promise.all. For a player with 55 non-empty
    containers that is 55 requests to render (typically) one row, and it hung in
    production 2026-08-02: only 12 of the 55 ever reached the server, so the
    promise never settled and the page showed loading skeletons indefinitely with
    no timeout and no error path. Server-side this is one pass over data the
    mirror already holds in a single blob.

    Augments are rare (279 items server-wide), so an empty list is the normal
    answer and is NOT an error."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    _touch_last_session(active_account_id)

    ctrl, _ = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    rows, scanned, truncated = await _augmented_scan(active_account_id, ctrl)
    return _v2_ok({
        "rows": rows,
        "scanned": scanned,
        "truncated": truncated,
        # The reroll/swap dialog needs all three to render; sending them here
        # means the Storage entry point costs no extra round trip. Same names and
        # semantics as the Character page's `equipped` block, so one dialog
        # component serves both surfaces.
        "augments_enabled": _v2_augment_enabled(),
        "player_online": await _safe_call(_resolve_online(active_account_id)),
        "owned_augments": (await _safe_call(_load_owned_augments(active_account_id))) or [],
    })


@router.get("/portal/containers/v2/{container_id}/items")
async def portal_container_v2_items(request: Request, container_id: str, page: int = 1):
    """V2 item grid for ONE owned container (JSON). Ownership enforced server-side
    (account_id = session.aid); a container_id the player does not own 404s. Also
    resolves the container's slot/volume caps for the grid. Read-only."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    if not container_id.isdigit():
        return _v2_err("bad_request", "container_id must be a positive integer", status=400)
    page = max(int(page or 1), 1)

    mic = miv = None
    match = None
    containers = await _load_containers(active_account_id)
    if not containers or not containers.get("available"):
        return _v2_err("unavailable", "Storage is unavailable right now.", status=503)
    match = next((c for c in containers["containers"]
                  if str(c.get("id")) == container_id), None)
    if match is None:
        return _v2_err("not_found", "Container not found for your account", status=404)
    if match.get("is_pawn_storage"):
        ctrl, _ = await _resolve_buyer_ctrl_and_bank(
            active_account_id, _selected_ctrl(request, active_account_id))
        if (ctrl is None or match.get("owner_ctrl") is None
                or str(match.get("owner_ctrl")) != str(ctrl)):
            return _v2_err("not_found", "Container not found for this character",
                           status=404)
    mic = match.get("max_item_count")
    miv = match.get("max_item_volume")

    items = await _load_container_items(active_account_id, container_id, page)
    if not items.get("available") and items.get("error") == "not_owned":
        return _v2_err("not_found", "Container not found for your account", status=404)
    return _v2_ok({
        "items": [_v2_item(it) for it in (items.get("items") or [])],
        "mic": mic, "miv": miv,
        "total_count": items.get("total_count") or 0,
        "page": items.get("page") or page,
        "page_size": items.get("page_size") or 100,
    })


@router.get("/portal/containers/v2/search")
async def portal_container_v2_search(request: Request, q: str = ""):
    """V2 cross-container item locator (JSON). Scoped to session.aid; returns matches
    grouped by item with which containers hold them. Read-only."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    ctrl, _ = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    results = await _load_container_search(
        active_account_id, (q or "").strip().lower(), ctrl)
    if results.get("too_short"):
        return _v2_ok({"hits": [], "match_count": 0, "too_short": True})
    if not results.get("available"):
        return _v2_err("unavailable", "Search is unavailable right now.", status=503)
    hits = [{
        "template": g.get("template_id"),
        "name": g.get("name"),
        "icon": g.get("icon"),
        "total_qty": g.get("total_qty"),
        "containers": g.get("containers") or [],
    } for g in (results.get("items") or [])]
    return _v2_ok({"hits": hits, "match_count": results.get("match_count") or len(hits)})


async def _v2_resolve_selected_inventory(active_account_id: int, container_id):
    """Translate the portal's SELECTED container id into an inventory id, verifying it
    belongs to this account. Returns (inventory_id, container_dict) or (None, None).

    🔴 The namespace translation is DATA INTEGRITY, not cosmetics -- see the long note
    in /portal/storage/v2/move. A container id is a READ id: for a placed box that is a
    PLACEABLE id, while the writer only ever matches INVENTORY ids, and the two keyspaces
    OVERLAP. Forwarding the read id either misses (a harmless `not_owner`) or COLLIDES
    with a different box's inventory and silently targets the WRONG CONTAINER. Bank and
    vehicle are safe either way -- their container id already IS their inventory id --
    which is why the fallback below is exactly right for them.

    A bad or foreign id resolves to None and the caller proceeds with NO selection; the
    writer then applies its own priority order. A selection is only ever a preference, so
    an unresolvable one must never become an error."""
    if container_id is None:
        return None, None
    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    hit = next((c for c in clist if str(c.get("id")) == str(container_id)), None)
    if hit is None:
        return None, None
    if "Deep Desert" in (hit.get("map") or ""):
        # DD inventories are excluded from every write path; the writer enforces it too.
        return None, None
    return (hit.get("inv_id") or container_id), hit


def _v2_place_label(container: dict, kind: str) -> str:
    """Player-facing name for where something landed. Prefers the box's own name, falls
    back to its type, then to the writer's `kind`. The placeholder names the game stores
    as `##Type_Placeable` are never shown to a player."""
    if kind == "backpack":
        return "your backpack"
    if kind == "bank":
        return "your CHOAM bank"
    if container:
        name = (container.get("name") or "").strip()
        if name and not name.startswith("##"):
            return name
        label = (container.get("label") or "").strip()
        if label:
            return label
    return "one of your containers"


@router.post("/portal/storage/v2/withdraw")
async def portal_storage_v2_withdraw(request: Request):
    """V2 WITHDRAW (Credit -> Coin). JSON body {amount, container_id?, uuid?}. OFFLINE-only
    (writer hard-gates). owner_ctrl resolved SERVER-SIDE. Cap 100000 (route + writer).
    Reuses the SAME `_storage_write` relay chain as V1. Auth: linked session + CSRF.

    container_id is the container the player has open, forwarded as a PREFERRED
    destination. Since 2026-07-25 the writer resolves: backpack -> this selection ->
    other owned containers -> bank last, and reports back where the coins actually
    landed so the response can name it."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    raw = str(body.get("amount", "") or "").strip().replace(",", "")
    amount = int(raw) if raw.isdigit() else 0
    if amount <= 0:
        return _v2_err("bad_amount", "Enter a whole number of Solari above 0.", status=400)
    if amount > _STORAGE_WITHDRAW_CAP:
        return _v2_err("cap_exceeded",
                       f"You can withdraw at most {_STORAGE_WITHDRAW_CAP:,} Solari at a time.",
                       status=400)
    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One transfer at a time. Wait a moment and retry.",
                       status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    sel_inv, _sel_container = await _v2_resolve_selected_inventory(
        active_account_id, str(body.get("container_id") or "").strip() or None)

    wd_body = {"owner_ctrl": owner_ctrl, "amount": amount}
    if sel_inv is not None:
        wd_body["dst_inventory_id"] = sel_inv

    ok, err_token, result = await _storage_write(
        request, action="withdraw", body=wd_body,
        active_account_id=active_account_id, discord_id=discord_id,
        owner_ctrl=owner_ctrl, audit_extra={"amount": amount, "surface": "v2",
                                            "dst_inventory_id": sel_inv})

    if ok:
        moved = result.get("amount") if isinstance(result, dict) else None
        dst_inv = (result or {}).get("dst_inv")
        dst_kind = (result or {}).get("dst_kind")
        # Name the destination back to the player. The writer returns an INVENTORY id, so
        # match the container list on inv_id (not id) -- the two namespaces overlap.
        dst_container = None
        if dst_inv is not None and dst_kind not in ("backpack",):
            containers = await _load_containers(active_account_id)
            clist = containers["containers"] if containers and containers.get("available") else []
            dst_container = next(
                (c for c in clist
                 if str(c.get("inv_id") or c.get("id")) == str(dst_inv)), None)
        return _v2_ok({
            "amount": moved if isinstance(moved, int) else amount,
            "bank_after": (result or {}).get("bank_after"),
            "coin_stack": (result or {}).get("coin_id"),
            "merged": (result or {}).get("merged"),
            "dst_inv": dst_inv,
            "dst_kind": dst_kind,
            "dst_slot": (result or {}).get("dst_slot"),
            "dst_label": _v2_place_label(dst_container, dst_kind),
        })
    friendly = _STORAGE_ERROR_TEXT.get(
        err_token, "That withdrawal could not be completed. Please try again.")
    status = 409 if err_token in ("player_online", "insufficient_bank", "bank_full") else 400
    return _v2_err(err_token or "write_failed", friendly, status=status)


@router.post("/portal/storage/v2/deposit")
async def portal_storage_v2_deposit(request: Request):
    """V2 DEPOSIT (Coin -> Credit). JSON body {mode:'sweep'|'amount', amount?, uuid?}.
    OFFLINE-only (writer hard-gates). owner_ctrl resolved SERVER-SIDE. Reuses the SAME
    `_storage_write` relay chain as V1. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    mode = str(body.get("mode", "") or "").strip().lower()
    if mode not in ("sweep", "amount"):
        return _v2_err("bad_request", "That deposit request was malformed.", status=400)
    amount = None
    if mode == "amount":
        raw = str(body.get("amount", "") or "").strip().replace(",", "")
        amount = int(raw) if raw.isdigit() else 0
        if amount <= 0:
            return _v2_err("bad_amount", "Enter a whole number of Solari above 0.", status=400)

    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One transfer at a time. Wait a moment and retry.",
                       status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    sel_inv, sel_container = await _v2_resolve_selected_inventory(
        active_account_id, str(body.get("container_id") or "").strip() or None)

    write_body = {"owner_ctrl": owner_ctrl, "mode": mode}
    if amount is not None:
        write_body["amount"] = amount
    # A selection scopes the consume to that ONE inventory -- the only way coins sitting
    # in a player's backpack can be deposited at all. Absent, the writer uses the historic
    # whole-owned-set behaviour, so an unscoped SWEEP never starts emptying pockets.
    if sel_inv is not None:
        write_body["src_inventory_id"] = sel_inv
    ok, err_token, result = await _storage_write(
        request, action="deposit", body=write_body,
        active_account_id=active_account_id, discord_id=discord_id,
        owner_ctrl=owner_ctrl, audit_extra={"mode": mode, "amount": amount, "surface": "v2",
                                            "src_inventory_id": sel_inv})

    if ok:
        src_kind = (result or {}).get("src_kind")
        return _v2_ok({
            "mode": mode,
            "swept_total": (result or {}).get("swept_total"),
            "bank_after": (result or {}).get("bank_after"),
            "src_inv": (result or {}).get("src_inv"),
            "src_kind": src_kind,
            "src_label": (_v2_place_label(sel_container, src_kind)
                          if (result or {}).get("src_inv") is not None else None),
        })
    friendly = _STORAGE_ERROR_TEXT.get(
        err_token, "That deposit could not be completed. Please try again.")
    status = 409 if err_token in ("player_online", "no_coins", "insufficient_coins") else 400
    return _v2_err(err_token or "write_failed", friendly, status=status)


async def _v2_dispatch_repair(request: Request, *, action: str):
    """JSON-envelope repair dispatch: mirrors `_dispatch_repair` (CSRF + rolling cap +
    SERVER-SIDE owner_ctrl resolve + `_repair_write` relay chain) but returns JSON.
    Shares the SAME audit buckets/caps as the V1 repair routes."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    if action == "everything" and not config.REPAIR_ALL_ENABLED:
        return _v2_err("disabled",
                       "Full Repair & Refurbish is temporarily unavailable. Per-item and "
                       "Backpack & Equipped repair still work.", status=200)
    if action == "vehicle" and not config.REPAIR_VEHICLE_ENABLED:
        return _v2_err("disabled",
                       "Vehicle Refurbish is temporarily unavailable. Per-item and "
                       "Backpack & Equipped repair still work.", status=200)
    if action == "box" and not config.REPAIR_BOX_ENABLED:
        return _v2_err("disabled",
                       "Container repair is paused. A container is held by the server "
                       "while your base is loaded, so the repair could not show in-game. "
                       "Backpack & Equipped repair and Refurbish Everything still work.",
                       status=200)

    inv_id = None
    if action in ("box", "vehicle"):
        raw = str(body.get("inv_id", "") or "").strip()
        inv_id = int(raw) if raw.isdigit() else 0
        if inv_id <= 0:
            return _v2_err("bad_request",
                           ("That vehicle could not be identified. Refresh and try again."
                            if action == "vehicle"
                            else "That container could not be identified. Refresh and try again."),
                           status=400)
        # ID NAMESPACE TRANSLATION (same defect as the MOVE path -- see
        # `_shape_containers_for_render`). The client sends the READ container id
        # (`storage.selectedId`), which for a placed box is a PLACEABLE id. The repair
        # writer gates it against its own owned_inv_sql(), which returns INVENTORY ids
        # (dune-repair-write.py:232), so a box id never matched: the target set came
        # back empty and the repair silently reported success having fixed 0 items.
        # Bank + vehicle were unaffected (their container id already IS the inv id).
        containers = await _load_containers(active_account_id)
        clist = containers["containers"] if containers and containers.get("available") else []
        box = next((c for c in clist if str(c.get("id")) == str(inv_id)), None)
        if box is not None:
            inv_id = box.get("inv_id") or inv_id

    if action == "everything":
        from config import REPAIR_ALL_WINDOW_HR, REPAIR_ALL_CAP
        cap_action, window_seconds, cap = ("portal_repair_everything",
                                           REPAIR_ALL_WINDOW_HR * 3600, REPAIR_ALL_CAP)
        audit_action = "portal_repair_everything"
    elif action == "vehicle":
        from config import REPAIR_VEHICLE_WINDOW_MIN, REPAIR_VEHICLE_CAP
        cap_action, window_seconds, cap = ("portal_repair_vehicle",
                                           REPAIR_VEHICLE_WINDOW_MIN * 60, REPAIR_VEHICLE_CAP)
        audit_action = "portal_repair_vehicle"
    else:
        from config import REPAIR_BOX_WINDOW_MIN, REPAIR_BOX_CAP
        cap_action, window_seconds, cap = ("portal_repair",
                                           REPAIR_BOX_WINDOW_MIN * 60, REPAIR_BOX_CAP)
        audit_action = "portal_repair"

    # Rate-limit bucket key. The vehicle tier is PER-VEHICLE (account:inv_id) so each
    # vehicle has its own cooldown -- the announcement promised per-vehicle, and a shared
    # account bucket meant 3 refurbishes across ANY vehicles tripped a global cooldown.
    # box/gear/everything stay per-account.
    cap_target = (f"{active_account_id}:{inv_id}" if action == "vehicle"
                  else str(active_account_id))

    from auth import recent_action_count
    since = (datetime.now(timezone.utc)
             - timedelta(seconds=window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    if recent_action_count(cap_action, since, targets=[cap_target]) >= cap:
        remaining = repair_cooldown_remaining(cap_action, active_account_id, window_seconds, cap,
                                              target=cap_target)
        return _v2_err("cooldown",
                       f"Repair is on cooldown. Next available in {_fmt_mmss(remaining)}.",
                       status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    write_body = {"action": action, "owner_ctrl": owner_ctrl}
    if inv_id is not None:
        write_body["inv_id"] = inv_id
    ok, err_token, result = await _repair_write(
        request, action=action, body=write_body,
        active_account_id=active_account_id, discord_id=discord_id,
        owner_ctrl=owner_ctrl, audit_action=audit_action, audit_target=cap_target)

    if ok:
        repaired = result.get("repaired_count") if isinstance(result, dict) else None
        return _v2_ok({
            "repaired_count": repaired if isinstance(repaired, int) else 0,
            "message": _repair_success_message(action, repaired),
        })
    friendly = _REPAIR_ERROR_TEXT.get(
        err_token, "The repair could not be completed. Please try again.")
    status = 409 if err_token == "player_online" else 400
    return _v2_err(err_token or "write_failed", friendly, status=status)


@router.post("/portal/storage/v2/repair/box")
async def portal_storage_v2_repair_box(request: Request):
    """V2 per-box repair (JSON). OFFLINE-only; shares the box+gear cap. Auth: session + CSRF."""
    return await _v2_dispatch_repair(request, action="box")


@router.post("/portal/storage/v2/repair/gear")
async def portal_storage_v2_repair_gear(request: Request):
    """V2 gear repair (JSON). OFFLINE-only; shares the box+gear cap. Auth: session + CSRF."""
    return await _v2_dispatch_repair(request, action="gear")


@router.post("/portal/storage/v2/repair/everything")
async def portal_storage_v2_repair_everything(request: Request):
    """V2 24h Refurbish Everything (JSON). `REPAIR_ALL_ENABLED` kill-switch preserved.
    OFFLINE-only; own 24h bucket. Auth: session + CSRF."""
    return await _v2_dispatch_repair(request, action="everything")


@router.post("/portal/storage/v2/repair/vehicle")
async def portal_storage_v2_repair_vehicle(request: Request):
    """V2 per-vehicle in-place Refurbish (JSON). Reverses max-durability decay on ONE
    owned vehicle's mounted parts, no dismounting. `REPAIR_VEHICLE_ENABLED` kill-switch;
    own bucket at the box cadence. OFFLINE-only. Auth: session + CSRF."""
    return await _v2_dispatch_repair(request, action="vehicle")


@router.get("/portal/storage/v2/vehicle/{container_id}/parts")
async def portal_storage_v2_vehicle_parts(request: Request, container_id: str):
    """V2 read-only INSTALLED-parts durability list for one owned vehicle (JSON).
    Ownership enforced server-side (account = session.aid); a vehicle the player does not
    own 404s. Reads dune.vehicle_modules. Auth: linked session (no CSRF -- read only)."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    if not container_id.isdigit():
        return _v2_err("bad_request", "container_id must be a positive integer", status=400)
    data = await _load_vehicle_parts(active_account_id, container_id)
    if not data.get("available") and data.get("error") == "not_owned":
        return _v2_err("not_found", "Vehicle not found for your account", status=404)
    # Per-vehicle Refurbish cooldown state so the panel can show uses/reset for THIS vehicle.
    # cap_target matches the refurbish path: account:inv_id, and for a vehicle the container
    # id IS the inventory id.
    from config import REPAIR_VEHICLE_ENABLED, REPAIR_VEHICLE_WINDOW_MIN, REPAIR_VEHICLE_CAP
    window_s = REPAIR_VEHICLE_WINDOW_MIN * 60
    cap_target = f"{active_account_id}:{container_id}"
    cd = uses_left = 0
    if REPAIR_VEHICLE_ENABLED:
        from auth import recent_action_count
        since = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).strftime("%Y-%m-%d %H:%M:%S")
        used = recent_action_count("portal_repair_vehicle", since, targets=[cap_target])
        uses_left = max(0, REPAIR_VEHICLE_CAP - used)
        cd = repair_cooldown_remaining("portal_repair_vehicle", active_account_id,
                                       window_s, REPAIR_VEHICLE_CAP, target=cap_target)
    return _v2_ok({
        "parts": data.get("parts") or [], "count": data.get("count") or 0,
        "refurbish_enabled": bool(REPAIR_VEHICLE_ENABLED),
        "refurbish_cap": REPAIR_VEHICLE_CAP,
        "refurbish_uses_left": uses_left,
        "refurbish_cooldown_remaining": cd,
    })


@router.post("/portal/storage/v2/pawn-move")
async def portal_storage_v2_pawn_move(request: Request):
    """Move one stack between the selected character's Bank and Backpack only."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    if not _v2_pawn_move_enabled():
        return _v2_err("pawn_move_disabled",
                       _PAWN_MOVE_ERROR_TEXT["pawn_move_disabled"], status=200)

    raw_item = str(body.get("item_id", "") or "").strip()
    item_id = int(raw_item) if raw_item.isdigit() and int(raw_item) > 0 else None
    src_storage = str(body.get("expected_source", "") or "").strip().lower()
    dst_storage = str(body.get("destination", "") or "").strip().lower()
    if (item_id is None or src_storage not in ("bank", "backpack")
            or dst_storage not in ("bank", "backpack") or src_storage == dst_storage):
        return _v2_err("bad_request", "That move request was malformed.", status=400)

    idem = str(body.get("uuid", "") or "").strip().lower()
    if not _GUILD_OP_UUID_RE.match(idem):
        return _v2_err("bad_request", "That move request had an invalid UUID.", status=400)

    expected_template = (body.get("expected_template") or "").strip() or None
    if expected_template is not None and not _MARKET_TERM_RE.fullmatch(expected_template):
        return _v2_err("bad_request", "That item could not be identified.", status=400)
    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One move at a time. Wait a moment and retry.",
                       status=429)
    if await _resolve_online(active_account_id) is True:
        return _v2_err("player_online", _PAWN_MOVE_ERROR_TEXT["player_online"],
                       status=409)

    owner_ctrl, _ = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now.",
                       status=502)

    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    bank, backpack = _pick_pawn_storage_pair(clist, owner_ctrl)
    pair = {"bank": bank, "backpack": backpack}
    if pair[src_storage] is None:
        token = "no_bank" if src_storage == "bank" else "no_backpack"
        return _v2_err(token, _PAWN_MOVE_ERROR_TEXT[token], status=409)
    if pair[dst_storage] is None:
        token = "no_bank" if dst_storage == "bank" else "no_backpack"
        return _v2_err(token, _PAWN_MOVE_ERROR_TEXT[token], status=409)

    move_body = {
        "owner_ctrl": owner_ctrl,
        "item_id": item_id,
        "src_storage": src_storage,
        "dst_storage": dst_storage,
        "idempotency_key": idem,
    }
    if expected_template is not None:
        move_body["expected_template"] = expected_template

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay("/dune/storage/pawn-move", method="POST",
                                  json_body=move_body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: pawn storage move relay error acct=%s: %s",
                       active_account_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: pawn storage move failed acct=%s: %s",
                       active_account_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_storage_pawn_move",
            str(owner_ctrl), ip,
            details=json.dumps({
                "account_id": active_account_id,
                "owner_ctrl": owner_ctrl,
                "item_id": item_id,
                "src_storage": src_storage,
                "dst_storage": dst_storage,
                "src_inventory_id": pair[src_storage].get("inv_id"),
                "dst_inventory_id": pair[dst_storage].get("inv_id"),
                "idempotency_key": idem,
                "result": "ok" if ok else (err_token or "error"),
                "replay": bool((result or {}).get("replay")) if isinstance(result, dict) else False,
                "first_empty": (result or {}).get("first_empty") if isinstance(result, dict) else None,
                "volume_unverified": (result or {}).get("volume_unverified") if isinstance(result, dict) else None,
            }),
            success=ok,
        )

    if ok:
        try:
            import mirror as _mirror
            _mirror.invalidate_storage(active_account_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            from cache import invalidate
            invalidate("dune.player_containers", str(active_account_id))
            invalidate("dune.container_search", str(active_account_id))
            for c in pair.values():
                cid = str(c.get("id"))
                for page in range(1, _MOVE_INVALIDATE_PAGES + 1):
                    invalidate("dune.player_container_items",
                               str(active_account_id), cid, page)
        except Exception:  # noqa: BLE001
            pass
        return _v2_ok({
            "item_id": item_id,
            "src_container_id": pair[src_storage].get("id"),
            "dst_container_id": pair[dst_storage].get("id"),
            "src_storage": src_storage,
            "dst_storage": dst_storage,
            "first_empty": (result or {}).get("first_empty"),
            "volume_unverified": (result or {}).get("volume_unverified"),
            "replay": bool((result or {}).get("replay")),
        })

    friendly = _PAWN_MOVE_ERROR_TEXT.get(err_token,
                                         _PAWN_MOVE_ERROR_TEXT["move_failed"])
    if err_token == "pawn_move_disabled":
        return _v2_err(err_token, friendly, status=200)
    status = 409 if err_token in (
        "player_online", "item_not_found", "not_owner", "no_bank", "no_backpack",
        "dst_no_slots", "dst_full_slots", "dst_full_volume", "idempotency_conflict",
    ) else 400
    return _v2_err(err_token or "write_failed", friendly, status=status)


@router.post("/portal/storage/v2/move")
async def portal_storage_v2_move(request: Request):
    """Legacy container MOVE contract. A native backend is not implemented;
    linked-session and CSRF checks precede the unavailable response."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    # Keep the existing soft-refusal HTTP contract without dispatching a SQL move.
    if not _v2_move_enabled():
        return _v2_err("native_move_unavailable",
                       _MOVE_ERROR_TEXT["native_move_unavailable"], status=200)

    def _pos(name):
        raw = str(body.get(name, "") or "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    item_id = _pos("item_id")
    dst_container_id = _pos("dst_container_id")
    if item_id is None or dst_container_id is None:
        return _v2_err("bad_request", "That move request was malformed.", status=400)

    expected_template = (body.get("expected_template") or "").strip() or None
    if expected_template is not None and not _MARKET_TERM_RE.fullmatch(expected_template):
        return _v2_err("bad_request", "That item could not be identified.", status=400)

    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One move at a time. Wait a moment and retry.",
                       status=429)

    # Defense in depth: the destination must be one of the player's OWN containers and
    # must not be a Deep Desert inventory. The writer re-verifies both source and dest
    # ownership + the DD exclusion authoritatively under a row lock.
    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    dst = next((c for c in clist if str(c.get("id")) == str(dst_container_id)), None)
    if dst is None:
        return _v2_err("not_owner", _MOVE_ERROR_TEXT["not_owner"], status=404)
    if "Deep Desert" in (dst.get("map") or ""):
        return _v2_err("dst_on_deep_desert", _MOVE_ERROR_TEXT["dst_on_deep_desert"], status=409)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    # ID NAMESPACE TRANSLATION (see `_shape_containers_for_render`) -- DATA INTEGRITY,
    # not just an error message. `dst_container_id` is a READ id: for a placed box that
    # is a PLACEABLE id, but the writer's owned_inv_sql() only ever returns INVENTORY
    # ids. The two namespaces share a keyspace and OVERLAP, so forwarding the read id
    # had two outcomes:
    #   * no collision -> owned_inv_sql misses -> `not_owner` (the reported bug), or
    #   * COLLISION with a different box's inventory id -> owned_inv_sql MATCHES and the
    #     writer silently re-homed the stack into the WRONG CONTAINER.
    # Live proof (acct 1644, 2026-07-16): placeable 3015's inventory is 2969, and 2969 is
    # itself a placeable id (whose own inventory is 2967); likewise placeable 2900 -> inv
    # 2950 while inv 2900 belongs to placeable 2876. Bank/vehicle were always safe (their
    # container id already IS their inventory id: 2914 -> 2914, 3163 -> 3163).
    # Fallback: a pre-deploy read model carries no `inv_id`; for bank/vehicle `id` is
    # already the inventory id, so the fallback is exactly correct there, and for a box it
    # is no worse than the behaviour this replaces.
    dst_inventory_id = dst.get("inv_id") or dst_container_id
    move_body = {"owner_ctrl": owner_ctrl, "item_id": item_id,
                 "dst_inventory_id": dst_inventory_id}
    if expected_template is not None:
        move_body["expected_template"] = expected_template

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay("/dune/storage/move", method="POST",
                                  json_body=move_body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: storage move relay error acct=%s: %s",
                       active_account_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: storage move failed acct=%s: %s", active_account_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_storage_move",
            str(owner_ctrl), ip,
            details=json.dumps({
                "account_id": active_account_id, "owner_ctrl": owner_ctrl,
                "item_id": item_id,
                # Both namespaces: `dst_container_id` is what the client asked for,
                # `dst_inventory_id` is what the writer was actually given. Logging
                # only the former is what made the id mismatch invisible in audit.
                "dst_container_id": dst_container_id,
                "dst_inventory_id": dst_inventory_id,
                "surface": "v2",
                "result": "ok" if ok else (err_token or "error"),
                "src_inv": (result or {}).get("src_inv") if isinstance(result, dict) else None,
                "first_empty": (result or {}).get("first_empty") if isinstance(result, dict) else None,
                "volume_unverified": (result or {}).get("volume_unverified") if isinstance(result, dict) else None,
            }),
            success=ok,
        )

    if ok:
        # The write landed in the GAME DB, but every portal storage read is served
        # mirror-first (`_load_containers` / `_load_container_items`), and the mirror
        # blob only refreshes on `mirror.sync_loop`. Without dropping it the player is
        # shown the PRE-move snapshot for up to MIRROR_MAX_STALE: the stack appears in
        # neither the source nor the destination. Drop the blob so reads fall through
        # to the live relay until the next sync repopulates it.
        # The writer reports the source as an INVENTORY id; every id the client speaks
        # (and every cache key) is a READ id. Map back once, here, so neither the cache
        # drop below nor the JSON contract leaks the inventory namespace to callers.
        src_inv = (result or {}).get("src_inv") if isinstance(result, dict) else None
        inv_to_read = {str(c.get("inv_id") or c.get("id")): str(c.get("id"))
                       for c in clist}
        src_container_id = (inv_to_read.get(str(src_inv), str(src_inv))
                            if src_inv is not None else None)
        try:
            import mirror as _mirror
            _mirror.invalidate_storage(active_account_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            from cache import invalidate
            invalidate("dune.player_containers", str(active_account_id))
            invalidate("dune.container_search", str(active_account_id))
            # The item GRIDS come from a different cache than the container LIST, keyed
            # per (account, container, page) -- and `container` there is the READ id
            # (`/portal/containers/v2/{container_id}/items` -> `_load_container_items`),
            # NOT the inventory id. Dropping by inventory id would silently miss a
            # placeable's grid, which is the whole bug this endpoint just fixed.
            for cid in filter(None, {str(dst_container_id), src_container_id}):
                for page in range(1, _MOVE_INVALIDATE_PAGES + 1):
                    invalidate("dune.player_container_items",
                               str(active_account_id), cid, page)
        except Exception:  # noqa: BLE001
            pass
        return _v2_ok({
            "item_id": item_id,
            # Both ids are READ ids -- the namespace the client already speaks, so the
            # frontend can refetch either end with `api.containerItems(id)` directly.
            "dst_container_id": dst_container_id,
            "src_container_id": src_container_id,
            "first_empty": (result or {}).get("first_empty"),
            "volume_unverified": (result or {}).get("volume_unverified"),
        })
    friendly = _MOVE_ERROR_TEXT.get(err_token, _MOVE_ERROR_TEXT["move_failed"])
    status = 409 if err_token in ("player_online", "item_not_found", "not_owner",
                                  "dst_no_slots", "dst_full_slots", "dst_full_volume",
                                  "dst_on_deep_desert") else 400
    return _v2_err(err_token or "write_failed", friendly, status=status)


@router.post("/portal/storage/v2/transfer")
async def portal_storage_v2_transfer(request: Request):
    """V2 Tier 5 SEND-FROM-BANK transfer (cross-player, single-row re-home). Sender is
    the SESSION account, NEVER the client. Recipient resolved server-side by char_name
    or by identity CODE (exactly one), the code re-resolved here on the write and never
    echoed onward. The item must live in the sender's OWN bank (is_bank container),
    verified via `_verify_seller_owns_item`; the writer re-verifies pinned to the sender
    bank. Ships DARK (`LASTSIETCH_ITEM_TRANSFER_ENABLED=0` -> {ok:true, status:'deferred'});
    never a fake success. JSON body {item_id, recipient_char_name | recipient_code,
    expected_template?, uuid}. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    def _pos(name):
        raw = str(body.get(name, "") or "").strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    item_id = _pos("item_id")
    if item_id is None:
        return _v2_err("bad_request", "That transfer request was malformed.", status=400)

    expected_template = (body.get("expected_template") or "").strip() or None
    if expected_template is not None and not _MARKET_TERM_RE.fullmatch(expected_template):
        return _v2_err("bad_request", "That item could not be identified.", status=400)

    recipient_account_id, recipient_err = _resolve_recipient(
        body, discord_id, "a transfer")
    if recipient_err is not None:
        token, friendly, status_code = recipient_err
        return _v2_err(token, friendly, status=status_code)
    alt_err = _linked_alt_refusal(active_account_id, discord_id, recipient_account_id,
                                  "You cannot send an item to yourself.",
                                  "You cannot send an item to your own linked account.")
    if alt_err is not None:
        token, friendly, status_code = alt_err
        return _v2_err(token, friendly, status=status_code)

    # OFFLINE-GATE (stop-ship, added 2026-07-27 with Karum Phase 0). A transfer TAKES the
    # item out of the SENDER's bank, and a take from a loaded session is resurrected under
    # its original item id (live-tested 07-26), which is a duplication path. A definitely-
    # online sender is refused here with NO relay/DB touch. Undetermined status is NOT
    # rejected at the edge; the writer's shared take is the authoritative fail-closed gate
    # on online_status + reconnect grace, exactly as /portal/market/v2/sell does it.
    # The RECIPIENT is deliberately not gated: giving to an online player is safe.
    if await _resolve_online(active_account_id) is True:
        return _v2_err("player_online", _TRANSFER_ERROR_TEXT["player_online"], status=409)

    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One transfer at a time. Wait a moment and retry.",
                       status=429)

    # The item must be in the sender's OWN CHOAM bank (is_bank container). Reuse the
    # ownership-enforced container path; a container the sender does not own yields
    # not_owned, and an item not in it yields None -> fail closed.
    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    # Verify against the SELECTED character's bank, not whichever bank sorts first
    # (see `_pick_bank`). Falls back to today's pick when no character is selected.
    bank_container = _pick_bank(clist, _selected_ctrl(request, active_account_id))
    if bank_container is None:
        return _v2_err("no_bank", "We could not find your CHOAM bank. Open it in-game once, "
                       "then retry.", status=409)
    owned_item = await _verify_seller_owns_item(
        active_account_id, str(bank_container.get("id")), item_id)
    if owned_item is None:
        return _v2_err("item_not_found",
                       "That item is no longer in your bank. Refresh and try again.",
                       status=409)
    if expected_template and (owned_item.get("template_id") or "") != expected_template:
        return _v2_err("item_not_found",
                       "That item changed. Refresh your bank and try again.", status=409)

    import uuid as _uuid
    # Idempotency: reuse the client-supplied key so a retry after a lost response
    # replays against the writer's UNIQUE ledger (ON CONFLICT DO NOTHING) instead of
    # executing a second transfer. Server-mint only when the client omits a valid key.
    idem = (str(body.get("uuid", "") or "")).strip().lower()
    if not _GUILD_OP_UUID_RE.match(idem):
        idem = str(_uuid.uuid4())
    transfer_body = {
        "sender_account_id": active_account_id,        # from session, NEVER the client
        "recipient_account_id": recipient_account_id,
        "item_id": item_id,
        "idempotency_key": idem,
        "mode": "apply",
        "requested_by_discord_id": discord_id,
    }
    # Identity guard on the moved row (writer RAISEs on mismatch).
    tmpl = owned_item.get("template_id") or expected_template
    if tmpl and _MARKET_TERM_RE.fullmatch(tmpl):
        transfer_body["expected_template"] = tmpl

    # Identity-collapsed caps + the durable both-parties mirror, reserved BEFORE the
    # relay for the same reason the gift caps are: two concurrent submits otherwise
    # both read a not-yet-breached cap and both pass. The writer's account-keyed caps
    # stay as the backstop. A key that already holds a row is let through unreserved
    # so a retry replays instead of being refused.
    import portal_transfer_events
    sender_identity = discord_id
    recipient_identity = portal_gift_limits.recipient_identity_key(recipient_account_id)
    reserved, cap_reason, retry_after = portal_transfer_events.check_and_reserve(
        idempotency_key=idem,
        sender_identity=sender_identity,
        sender_account_id=active_account_id,
        recipient_identity=recipient_identity,
        recipient_account_id=recipient_account_id,
        item_id=item_id,
        template_id=tmpl)
    if not reserved:
        msg = ("You have sent as many items as you can today. Try again tomorrow."
               if cap_reason == "daily" else
               "You have sent this player as many items as you can today. "
               "Try again tomorrow.")
        resp = _v2_err("rate_limited", msg, status=429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    ip = client_ip(request)
    result = None
    try:
        from relay import call_relay
        result = await call_relay("/dune/item-transfer-op", method="POST",
                                  json_body=transfer_body, timeout=45)
    except HTTPException as exc:
        logger.warning("portal: item-transfer relay error acct=%s: %s",
                       active_account_id, exc.detail)
        return _v2_err("unavailable", "The transfer service is unavailable right now. "
                       "Try again shortly.", status=502)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: item-transfer failed acct=%s: %s", active_account_id, exc)
        return _v2_err("unavailable", "Could not send the item right now. Try again shortly.",
                       status=502)
    finally:
        # 🔴 success= is the REAL outcome. This call used to omit it, which defaulted
        # it to True and recorded every refusal, and every relay failure, as a
        # successful transfer (the gift route's _audit fixed the same defect).
        # `deferred` is NOT a success: nothing moved.
        from auth import audit_log
        relay_status = result.get("status") if isinstance(result, dict) else None
        audit_log(None, f"portal:{discord_id}", "portal_storage_transfer",
                  f"{recipient_account_id}:{item_id}", ip,
                  details=json.dumps({"account_id": active_account_id,
                                      "item_id": item_id, "surface": "v2",
                                      "status": relay_status}),
                  success=bool(relay_status in ("applied", "replay")
                               and isinstance(result, dict)
                               and result.get("success")))

    if not isinstance(result, dict):
        # No usable answer. The reservation stays `pending` and ages out: settling it
        # `failed` here would free the allowance for a transfer that may well have
        # applied (portal.py's Karum rule -- a lost response is not evidence).
        return _v2_err("unavailable", "Unexpected response from the transfer service.",
                       status=502)

    status = result.get("status")
    # DARK: the writer returns status:'deferred' while LASTSIETCH_ITEM_TRANSFER_ENABLED=0.
    # Surface it honestly as a 200 soft state; the frontend renders "not yet available".
    if status == "deferred":
        portal_transfer_events.settle(idem, "deferred")
        return _v2_ok({"status": "deferred",
                       "message": "Send-from-bank transfer is not available yet."})
    if status in ("applied", "replay") and result.get("success"):
        def _num(value, cast):
            try:
                return cast(value)
            except (TypeError, ValueError):
                return None
        portal_transfer_events.settle(
            idem, status,
            audit_id=result.get("audit_id"),
            item_id=item_id,
            template_id=tmpl,
            display_name=owned_item.get("name") or tmpl,
            stack_size=_num(owned_item.get("stack_size"), int) or 1,
            quality_level=_num(owned_item.get("quality"), int) or 0)
        return _v2_ok({"status": status,
                       "item_id": item_id,
                       "message": result.get("message") or "Item transferred."})
    # The writer emits a stable token in `error` (player_online, no_bank, item_not_found,
    # bank_full, rate_limited, take_failed). Friendly text comes from our own dict, never
    # from the writer's message, which names inventory and account ids.
    err_token = result.get("error") or "transfer_failed"
    # A refusal is a definitive answer, so the reservation is released with the token.
    portal_transfer_events.settle(idem, "refused", err_token)
    return _v2_err(err_token,
                   _TRANSFER_ERROR_TEXT.get(err_token,
                                            _TRANSFER_ERROR_TEXT["transfer_failed"]),
                   status=409)


@router.get("/portal/storage/v2/transfers")
async def portal_storage_v2_transfers(request: Request,
                                      limit: int = _TRANSFER_HISTORY_DEFAULT):
    """Both directions of this player's item transfers, off the admin.db mirror.
    Scoped to the accounts the SESSION identity still holds a live link to, never
    to anything the client names. Read-only. Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _TRANSFER_HISTORY_DEFAULT
    limit = max(1, min(_TRANSFER_HISTORY_MAX, limit))

    try:
        from routers import portal_codes
        import portal_transfer_events
        account_ids = portal_codes.own_account_ids(discord_id) or [active_account_id]
        rows = portal_transfer_events.history(account_ids, limit)
    except Exception:  # noqa: BLE001
        # The mirror table is lazy and ships in the same wave as this route. An
        # empty list reads as "no transfers yet", which is the truth on a box that
        # has not written one; a 500 would take the Settings page down with it.
        logger.warning("portal: transfer history unavailable", exc_info=True)
        rows = []
    return _v2_ok({"rows": rows})


# ======================================================= THE KARUM ==========
#
# Player-to-player trade venue (SB-006 + SB-007), L4 of the four-layer contract.
# Contract: docs/dune-research/v2-portal/KARUM-BUILD-CONTRACT-2026-07-27.md
#
# WHAT THIS LAYER OWNS, and it is not the settlement:
#   * session auth, CSRF, identity resolution from the session (NEVER the client)
#   * the ownership pre-check, the anti-abuse gates, the durable rate caps
#   * CONTENTION: the compare-and-set active -> selling in admin.db is the single point
#     that decides who wins a contested listing, and it decides it BEFORE any game write,
#     so a losing racer is never charged
#   * listing lifecycle in admin.db, and the read-side mirror of confirmed effects
#
# WHAT IT MUST NOT DO:
#   * decide idempotency. The GAME DB owns that through UNIQUE correlation_id keys on its
#     own ledgers. admin.db mirrors only AFTER the writer confirms applied or replay, and a
#     DARK deferred records nothing anywhere.
#   * 🔴 revert a listing to `active` on a TIMEOUT. A lost response is not evidence that
#     payment failed; reverting would let a second buyer purchase goods the first already
#     paid for. Timeouts go to `reconciling` and are resolved by re-driving the SAME
#     correlation_id, never by guessing.
#   * emit any internal identifier. See _karum_public.

def _karum_enabled() -> bool:
    """UI mirror of the game-host LASTSIETCH_KARUM_ENABLED. Display only: the writer is the
    authority and its `deferred` is surfaced honestly whatever this says."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_KARUM_ENABLED", "0")


def _karum_wtb_enabled() -> bool:
    """Display gate only. The game-host writer's separate flag is authoritative."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_KARUM_WTB_ENABLED", "0")


_KARUM_MAX_PRICE = 900_000_000        # matches KARUM_MAX_PRICE in dune-karum-op.sh
_KARUM_MAX_LISTINGS_PER_DAY = 20      # per seller; GIFT_MAX_PER_DAY precedent
_KARUM_MAX_PAIR_PER_DAY = 5           # per buyer->seller
_KARUM_PAGE_SIZE = 24
_KARUM_LIVE_STATUSES = ("active", "selling", "reconciling", "returning",
                        "paid_undelivered")
_KARUM_REQUEST_LIVE_STATUSES = ("active", "filling", "reconciling",
                                "paid_undelivered")
_KARUM_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "dune-give-item-catalog.json")
_KARUM_CATALOG_CACHE = {"mtime": None, "items": [], "by_id": {}}
# Grades run Base(0) .. 5. Live-verified 2026-08-25 against dune.items: no row anywhere
# carries a quality_level above 5, and item-data's material_cost_per_grade is 6 slots
# wide for every gradeable template. Every gradeable template in the catalogue is T6.
_KARUM_MAX_QUALITY = 5
_KARUM_QUALITY_MODES = ("exact", "min")

_KARUM_ERROR_TEXT = {
    "player_online": "Log out of the game first, then list. An item can only leave your "
                     "bank or backpack while you are offline.",
    "not_open": "The Karum is not open yet. Nothing was listed.",
    "item_not_found": "That item is no longer in your bank or backpack. Refresh and try again.",
    "not_tradeable": "That item cannot be traded.",
    "no_category": "That item has never been listed on the CHOAM Exchange, so it has no "
                   "Exchange category. The Karum hands goods over through the Exchange, so "
                   "it cannot move an item it would not be able to give back.",
    "no_bank": "We could not find that storage. Open your CHOAM bank in-game once, then retry.",
    "bad_price": "Pick a price between 1 and 900,000,000 Solari.",
    "listing_gone": "Someone else bought that first, or the seller pulled it.",
    "self_trade": "You cannot buy your own listing.",
    "insufficient_funds": "You do not have enough banked Solari for that.",
    "rate_limited": "You have traded as much as you can for now. Try again later.",
    "not_yours": "That listing is not yours.",
    "unavailable": "The Karum is unavailable right now. Try again shortly.",
    "reconciling": "We are confirming this trade. Give it a moment and refresh; you will "
                   "not be charged twice.",
    "paid_undelivered": "You have paid and the goods are on their way. If they do not "
                        "appear shortly we will sort it out.",
    "quantity_mismatch": "That stack no longer has the exact quantity this order needs. "
                         "Refresh your bank and choose another one.",
    "grade_mismatch": "That item is not the grade this order asks for. Refresh your bank "
                      "and choose another one.",
    "request_gone": "Someone else filled that request first, or the requester cancelled it.",
    "wtb_not_open": "Wanted orders are not open yet. Nothing was posted or moved.",
    "write_failed": "That could not be completed. Please try again.",
}


def _karum_display_name(template_id, name) -> str:
    """The Karum's own display name. 419 schematic templates carry the plain item
    name in the curated names file, so a schematic and its item could share a
    label in the picker and on a card ("light armor": the piece or the recipe?
    player report, 2026-09-04). A template whose id ends in Schematic is labelled
    as one here, once, and nowhere is the bare item name shown for a recipe."""
    label = str(name or template_id or "")[:120]
    tid = str(template_id or "").lower()
    if tid.endswith("schematic") and "schematic" not in label.lower():
        label = (label[:110].rstrip() + " Schematic")
    return label


def _karum_catalog_data():
    """Tradeable catalogue entries used by both Karum posting forms.

    This is a version-controlled picker, not settlement authority. Live ownership,
    tradeability, category and stack facts are still verified before every write.
    """
    try:
        mtime = os.path.getmtime(_KARUM_CATALOG_PATH)
        if _KARUM_CATALOG_CACHE["mtime"] == mtime:
            return _KARUM_CATALOG_CACHE
        with open(_KARUM_CATALOG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("portal: Karum catalogue unavailable: %s", exc)
        return {"mtime": None, "items": [], "by_id": {}}

    items = []
    seen = set()
    for row in raw.get("items") or []:
        template_id = row.get("id")
        if (not isinstance(template_id, str)
                or not _MARKET_TERM_RE.fullmatch(template_id)
                or template_id in seen
                or row.get("non_tradeable") is True):
            continue
        seen.add(template_id)
        try:
            max_stack = max(1, int(row.get("pak_max_stack") or 1))
        except (TypeError, ValueError):
            max_stack = 1
        item = {
            "template_id": template_id,
            "name": _karum_display_name(template_id, row.get("name")),
            # Stored on a wanted order so both boards hold the bare in-game name and
            # the search LIKE below matches them the same way; the label is projection.
            "bare_name": str(row.get("name") or template_id)[:120],
            "category": str(row.get("cat") or "other")[:64],
            "max_stack": max_stack,
            "tier": row.get("tier"),
            "rarity": str(row.get("rarity") or "")[:32] or None,
            # Canonical grade flag, carried from dune-market-bot/item-data.json by
            # scripts/build-give-item-catalog.py. Absent means Base only. This decides
            # whether the wanted-order picker offers a grade at all, and it is re-checked
            # server-side at post time so a hand-rolled payload cannot smuggle a grade
            # onto a template that has none.
            "is_gradeable": row.get("is_gradeable") is True,
        }
        items.append(item)
    data = {"mtime": mtime, "items": items,
            "by_id": {row["template_id"]: row for row in items}}
    _KARUM_CATALOG_CACHE.update(data)
    return _KARUM_CATALOG_CACHE


def _karum_resolve_src(clist, ctrl, container_id):
    """Map a client-supplied container_id to one of the SELECTED character's two pawn
    inventories. Returns ("bank"|"backpack", container) or (None, None) to fail closed.

    🔴 This is the container-id trap mitigation, and it is why the client's id is MATCHED
    against a server-resolved pair rather than trusted. A container `id` is not an
    `inv_id` and the two namespaces OVERLAP, so accepting an arbitrary id here would let a
    colliding number escrow the wrong container's item. `_pick_pawn_storage_pair` is
    strict: selected character only, no account fallback, so a tombstoned re-roll's bank
    cannot win either. Never widen this to a general owned-container lookup."""
    bank, backpack = _pick_pawn_storage_pair(clist, ctrl)
    if bank is not None and str(bank.get("id")) == container_id:
        return "bank", bank
    if backpack is not None and str(backpack.get("id")) == container_id:
        return "backpack", backpack
    return None, None


def _karum_parse_quality(body):
    """Parse a wanted order's requested grade off the request body.

    Returns (quality_level, quality_mode, error) where a None level is the LEGACY
    ANY-GRADE contract and NOT the same thing as Base(0), which is a real request.
    A mode without a level normalises to no-grade rather than erroring: it cannot
    produce a wrong write, and rejecting it would break any client that always sends
    the mode. A level without a mode defaults to 'exact', the stricter of the two, so
    a truncated payload can never widen what the requester agreed to accept."""
    raw = body.get("quality_level")
    if raw is None or raw == "":
        return None, None, None
    if isinstance(raw, bool):
        return None, None, "That wanted order was malformed."
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return None, None, "That wanted order was malformed."
    if level < 0 or level > _KARUM_MAX_QUALITY:
        return None, None, f"Grade must be between 0 and {_KARUM_MAX_QUALITY}."
    mode = str(body.get("quality_mode") or "exact").strip().lower()
    if mode not in _KARUM_QUALITY_MODES:
        return None, None, "That grade rule was not recognised."
    return level, mode, None


def _karum_quality_ok(owned_quality, wanted_level, wanted_mode) -> bool:
    """Does a live item's grade satisfy a wanted order? The ONE place that decides.

    A NULL wanted_level is legacy any-grade and matches everything. Anything else is
    compared numerically, so an item whose grade we could not read (None) fails closed
    against a real request rather than sliding through as 0."""
    if wanted_level is None:
        return True
    try:
        have = int(owned_quality)
    except (TypeError, ValueError):
        return False
    want = int(wanted_level)
    if wanted_mode == "min":
        return have >= want
    return have == want


def _karum_row_quality(row):
    """(quality_level, quality_mode) off a request row, tolerating a pre-migration row.

    sqlite3.Row raises IndexError for a column that is not in the result set, and an
    admin.db that has not run the ALTERs yet has neither column. Both absent and NULL
    mean the same thing here: legacy any-grade."""
    def _get(key):
        try:
            return row[key]
        except (IndexError, KeyError, TypeError):
            return None
    level = _get("quality_level")
    if level is None:
        return None, None
    try:
        level = int(level)
    except (TypeError, ValueError):
        return None, None
    mode = _get("quality_mode")
    mode = str(mode).strip().lower() if mode else "exact"
    if mode not in _KARUM_QUALITY_MODES:
        mode = "exact"
    return level, mode


def _karum_public(row, *, own: bool = False) -> dict:
    """🔴 THE PROJECTION BOUNDARY. No account_id, no discord_id, no player_controller_id,
    no inventory_id, no escrow_item_id, no correlation_id, ever. seller_ctrl,
    escrow_item_id and escrow_corr_id exist on the row for the writer's benefit and are
    stripped here, exactly as portal_messages strips account_id at both write and read.

    `seller_name` is emitted because listing is itself the opt-in (owner decision D2,
    the same way blueprint publishing works). No other Karum surface emits any identity."""
    out = {
        "listing_id": row["listing_id"],
        "seller_name": row["seller_name"],
        "display_name": _karum_display_name(row["template_id"], row["display_name"]),
        "icon": _icon_for(row["template_id"]),
        "template_id": row["template_id"],
        "stack_size": row["stack_size"],
        "quality_level": row["quality_level"],
        "durability_cur": row["durability_cur"],
        "durability_max": row["durability_max"],
        "price": row["price"],
        "status": row["status"],
        "created_at": row["created_at"],
        # Tier travels beside quality_level so the UI can render the two as the separate
        # facts they are. Until 2026-08-25 every Karum surface printed the GRADE as
        # `T{quality_level}`, so a T6 G3 Perforator read as "T3".
        "tier": _karum_catalog_data()["by_id"].get(row["template_id"], {}).get("tier"),
    }
    if own:
        # The seller's own view of their own listing. Still no internal ids.
        out["sold_at"] = row["sold_at"]
        out["closed_at"] = row["closed_at"]
    return out


def _karum_request_funded(row, bank_override=None):
    """Advisory funding state from the local read-model mirror.

    A missing or stale mirror is unknown, never a refusal. The writer locks and checks
    the real balance in the same transaction as the filler item take.
    """
    bank = bank_override
    if bank is None:
        try:
            progress = mirror.get_section(int(row["requester_account_id"]), "progress")
            economy = (progress or {}).get("economy") or {}
            bank = economy.get("bank_solari", economy.get("solari"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("portal: wanted-order funding read failed: %s", exc)
            bank = None
    try:
        return int(bank) >= int(row["price"]) if bank is not None else None
    except (TypeError, ValueError):
        return None


def _karum_request_public(row, *, own: bool = False, bank_override=None) -> dict:
    """Public wanted-order projection. Internal identities and correlations stay server-side."""
    out = {
        "request_id": row["request_id"],
        "requester_name": row["requester_name"],
        "display_name": _karum_display_name(row["template_id"], row["display_name"]),
        "icon": _icon_for(row["template_id"]),
        "template_id": row["template_id"],
        "category": row["category"],
        "stack_size": row["stack_size"],
        "price": row["price"],
        "status": row["status"],
        "funded": _karum_request_funded(row, bank_override),
        "created_at": row["created_at"],
    }
    # Grade travels on EVERY wanted surface. A null level is legacy any-grade and the
    # UI renders it as such; it is not flattened to 0, which would read as "Base only".
    quality_level, quality_mode = _karum_row_quality(row)
    out["quality_level"] = quality_level
    out["quality_mode"] = quality_mode
    out["tier"] = _karum_catalog_data()["by_id"].get(row["template_id"], {}).get("tier")
    if own:
        out["filler_name"] = row["filler_name"]
        out["filled_at"] = row["filled_at"]
        out["closed_at"] = row["closed_at"]
    return out


def _karum_event(account_id: int, event: str, *, listing_id=None, discord_id=None,
                 detail=None) -> None:
    """Append-only. Backs the durable rate caps AND the moderation trail, so it is written
    for ATTEMPTS as well as outcomes. Never updated, never deleted. Best-effort: a failure
    to log must not fail a trade, but it is logged loudly because a missing row silently
    loosens a cap."""
    try:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO portal_karum_events "
                "(listing_id, account_id, discord_id, event, detail) VALUES (?,?,?,?,?)",
                (listing_id, int(account_id), str(discord_id) if discord_id else None,
                 event,
                 # COMPACT separators are load-bearing, not cosmetic: the per-pair cap in
                 # _karum_count_events matches this sidecar with LIKE '%"counterparty":N%',
                 # and json.dumps' default ", " / ": " spacing would make that pattern miss
                 # every row, silently disabling the cap.
                 json.dumps(detail, separators=(",", ":")) if detail else None))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("portal: karum event log failed acct=%s event=%s",
                       account_id, event, exc_info=True)


def _karum_count_events(account_id: int, events: tuple, hours: int = 24,
                        counterparty: int = None) -> int:
    """Durable cap counter. Counted over portal_karum_events rather than in memory so a
    deploy cannot reset a cap (portal_blueprint_publish_log precedent). `counterparty`
    narrows it to a pair by reading the sidecar, which is why the sidecar carries a
    counterparty listing rather than an account id."""
    try:
        conn = get_db()
        try:
            marks = ",".join("?" for _ in events)
            sql = (f"SELECT COUNT(*) FROM portal_karum_events "
                   f"WHERE account_id = ? AND event IN ({marks}) "
                   f"AND created_at >= datetime('now', ?)")
            args = [int(account_id), *events, f"-{int(hours)} hours"]
            if counterparty is not None:
                sql += " AND detail LIKE ?"
                args.append(f'%"counterparty":{int(counterparty)}%')
            return int(conn.execute(sql, args).fetchone()[0] or 0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        # Fail CLOSED on a counter we cannot read: an unreadable cap is not an absent cap.
        logger.warning("portal: karum cap count failed acct=%s", account_id, exc_info=True)
        return 10 ** 9


def _karum_listing(listing_id: int):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM portal_karum_listings WHERE listing_id = ?",
            (int(listing_id),)).fetchone()
    finally:
        conn.close()


def _karum_request(request_id: int):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM portal_karum_requests WHERE request_id = ?",
            (int(request_id),)).fetchone()
    finally:
        conn.close()


_KARUM_STAMP_COLUMNS = frozenset({"sold_at", "closed_at"})


def _karum_cas(listing_id: int, expect: str, to: str, *, stamp=(), **columns) -> bool:
    """Compare-and-set on status. THE contention primitive: two buyers racing one listing
    are decided here, in admin.db, before any game write, so the loser never reaches the
    writer and is never charged. Returns True only if this caller made the transition.

    `stamp` names columns to set to SQLite's own datetime('now'), so a timestamp written
    here cannot drift in format from the column defaults. Whitelisted, because these become
    column names in the statement."""
    sets = ["status = ?", "updated_at = datetime('now')"]
    args = [to]
    for col in stamp:
        if col not in _KARUM_STAMP_COLUMNS:
            raise ValueError(f"not a stampable karum column: {col}")
        sets.append(f"{col} = datetime('now')")
    for col, val in columns.items():
        sets.append(f"{col} = ?")
        args.append(val)
    args.extend([int(listing_id), expect])
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE portal_karum_listings SET {', '.join(sets)} "
            f"WHERE listing_id = ? AND status = ?", args)
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _karum_request_cas(request_id: int, expect: str, to: str, *, stamp=(),
                       clear_fill=False, **columns) -> bool:
    sets = ["status = ?", "updated_at = datetime('now')"]
    args = [to]
    for col in stamp:
        if col not in {"filled_at", "closed_at"}:
            raise ValueError(f"not a stampable Karum request column: {col}")
        sets.append(f"{col} = datetime('now')")
    if clear_fill:
        for col in ("filler_account_id", "filler_discord_id", "filler_name",
                    "filler_ctrl", "fill_item_id", "settlement_listing_id",
                    "take_corr_id", "fill_corr_id"):
            sets.append(f"{col} = NULL")
    for col, val in columns.items():
        sets.append(f"{col} = ?")
        args.append(val)
    args.extend([int(request_id), expect])
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE portal_karum_requests SET {', '.join(sets)} "
            "WHERE request_id = ? AND status = ?", args)
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _karum_claim_request(row, *, filler_account_id, filler_discord_id, filler_name,
                         filler_ctrl, owned, fill_corr, take_corr):
    """Atomically claim one active request and create its normal settlement listing."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status FROM portal_karum_requests WHERE request_id = ?",
            (int(row["request_id"]),)).fetchone()
        if current is None or current["status"] != "active":
            conn.rollback()
            return None

        def _number(value, cast):
            try:
                return cast(value)
            except (TypeError, ValueError):
                return None

        quality = _number(owned.get("quality"), int) or 0
        dur_cur = _number(owned.get("cur_dur"), float)
        dur_max = _number(owned.get("max_dur"), float)
        cur = conn.execute(
            "INSERT INTO portal_karum_listings "
            "(seller_account_id, seller_discord_id, seller_name, seller_ctrl, template_id,"
            " display_name, stack_size, quality_level, durability_cur, durability_max,"
            " price, escrow_corr_id, buyer_account_id, buyer_discord_id, buyer_ctrl,"
            " sold_corr_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'selling')",
            (int(filler_account_id), str(filler_discord_id) if filler_discord_id else None,
             filler_name or "Unknown", int(filler_ctrl), row["template_id"],
             owned.get("name") or row["display_name"], int(row["stack_size"]), quality,
             dur_cur, dur_max, int(row["price"]), take_corr,
             int(row["requester_account_id"]), row["requester_discord_id"],
             int(row["requester_ctrl"]), fill_corr))
        listing_id = int(cur.lastrowid)
        changed = conn.execute(
            "UPDATE portal_karum_requests SET status = 'filling', "
            "filler_account_id = ?, filler_discord_id = ?, filler_name = ?, filler_ctrl = ?, "
            "fill_item_id = ?, settlement_listing_id = ?, take_corr_id = ?, fill_corr_id = ?, "
            "updated_at = datetime('now') WHERE request_id = ? AND status = 'active'",
            (int(filler_account_id), str(filler_discord_id) if filler_discord_id else None,
             filler_name or "Unknown", int(filler_ctrl), int(owned["id"]), listing_id,
             take_corr, fill_corr, int(row["request_id"]))).rowcount
        if changed != 1:
            conn.rollback()
            return None
        conn.commit()
        return listing_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _karum_mirror(corr: str, listing_id: int, leg: str, account_id: int, status: str,
                  **cols) -> None:
    """Mirror a CONFIRMED effect. Only ever called with the writer's applied|replay, never
    on deferred. The UNIQUE correlation_id here is a local double-write pre-check, not the
    real idempotency guard, which lives in the game DB."""
    try:
        conn = get_db()
        try:
            keys = ["correlation_id", "listing_id", "leg", "account_id", "status"]
            vals = [corr, int(listing_id), leg, int(account_id), status]
            for k, v in cols.items():
                keys.append(k)
                vals.append(v)
            marks = ",".join("?" for _ in keys)
            conn.execute(f"INSERT OR IGNORE INTO portal_karum_ledger "
                         f"({','.join(keys)}) VALUES ({marks})", vals)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("portal: karum ledger mirror failed corr=%s leg=%s", corr, leg,
                       exc_info=True)


def _karum_notify(account_id: int, subject: str, body: str, payload: dict):
    """Primary channel: the portal mailbox. Zero game-DB touch, no rate limit, no online
    gate, no DMs-closed failure mode. `kind` reuses 'notification' because portal_messages
    CHECK-constrains it in two places that must agree; the Karum sub-type rides in the
    payload. Every payload key must be registered in messages._PAYLOAD_ALLOWED_KEYS or it
    is silently stripped on read. Returns the message id, or None."""
    try:
        return mailbox.post("player", int(account_id), "notification",
                            subject=subject, body=body, payload=payload)
    except Exception:  # noqa: BLE001
        logger.warning("portal: karum mailbox notify failed acct=%s", account_id,
                       exc_info=True)
        return None


async def _karum_relay(path: str, body: dict, timeout: int = 60):
    """Call the relay and normalise the THREE outcomes the caller must distinguish:
    a writer answer, a DARK deferral, and NO USABLE RESPONSE. The third is not a failure
    with paid:false; it is an unknown, and conflating them is the money-dupe path."""
    from relay import call_relay
    try:
        result = await call_relay(path, method="POST", json_body=body, timeout=timeout)
    except HTTPException as exc:
        logger.warning("portal: karum relay error %s: %s", path, exc.detail)
        return {"status": "unknown", "success": False}
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: karum relay failed %s: %s", path, exc)
        return {"status": "unknown", "success": False}
    if not isinstance(result, dict):
        return {"status": "unknown", "success": False}
    return result


def _karum_fail_detail(result: dict, token: str, **extra):
    """Build a `*_failed` event detail that KEEPS the writer's own words.

    🔴 Why this exists. `dune-karum-op.sh`'s fail_json only emits an `error` key when it was
    handed a token; an untokenised failure arrives as
    `{"success":false,"status":"failed","message":"<the actual reason>"}`. The original code
    recorded `detail={"reason": result.get("error") or "write_failed"}`, so on exactly the
    failures where there is no token -- the ones we understand least -- it threw the only
    description away and left `write_failed` behind.

    That is not hypothetical: it happened on the FIRST live listing attempt (2026-07-27,
    account 1644), and there was nothing in admin.db, the relay log or the portal log saying
    what the writer had objected to. A money feature must not discard its own diagnostics.

    Also logs at WARNING so the reason reaches journald even if admin.db is later pruned.
    """
    detail = {"reason": token}
    msg = result.get("message")
    if isinstance(msg, str) and msg.strip():
        detail["writer_message"] = msg.strip()[:400]
    wstatus = result.get("status")
    if isinstance(wstatus, str) and wstatus and wstatus != token:
        detail["writer_status"] = wstatus
    code = result.get("exit_code")
    if isinstance(code, int):
        detail["exit_code"] = code
    stderr = result.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        detail["stderr"] = stderr.strip()[:400]
    detail.update(extra)
    logger.warning("portal: karum failure token=%s writer_status=%s exit=%s msg=%s",
                   token, wstatus, code, (msg or "")[:300])
    return detail


@router.get("/portal/karum")
async def portal_karum_overview(request: Request):
    """Karum overview for both sale listings and whole-order wanted listings."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, row = gate
    _touch_last_session(active_account_id)

    ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    online = await _resolve_online(active_account_id)

    conn = get_db()
    try:
        listings = conn.execute(
            "SELECT * FROM portal_karum_listings WHERE status = 'active' "
            "ORDER BY created_at DESC LIMIT ?", (_KARUM_PAGE_SIZE,)).fetchall()
        mine = conn.execute(
            "SELECT * FROM portal_karum_listings WHERE seller_account_id = ? "
            "AND status NOT IN ('failed') ORDER BY created_at DESC LIMIT 100",
            (active_account_id,)).fetchall()
        bought = conn.execute(
            "SELECT * FROM portal_karum_listings WHERE buyer_account_id = ? "
            "AND status IN ('sold','paid_undelivered') ORDER BY sold_at DESC LIMIT 100",
            (active_account_id,)).fetchall()
        requests = conn.execute(
            "SELECT * FROM portal_karum_requests WHERE status = 'active' "
            "ORDER BY created_at DESC LIMIT ?", (_KARUM_PAGE_SIZE,)).fetchall()
        my_requests = conn.execute(
            "SELECT * FROM portal_karum_requests WHERE requester_account_id = ? "
            "ORDER BY created_at DESC LIMIT 100", (active_account_id,)).fetchall()
        my_fills = conn.execute(
            "SELECT * FROM portal_karum_requests WHERE filler_account_id = ? "
            "AND status IN ('filling','reconciling','paid_undelivered','filled') "
            "ORDER BY updated_at DESC LIMIT 100", (active_account_id,)).fetchall()
    finally:
        conn.close()

    listed_today = _karum_count_events(
        active_account_id, ("list_applied", "request_post_applied"))
    session_token = request.cookies.get(SESSION_COOKIE, "")
    return _v2_ok({
        "listings": [_karum_public(r) for r in listings],
        "my_listings": [_karum_public(r, own=True) for r in mine],
        "my_purchases": [_karum_public(r, own=True) for r in bought],
        "requests": [_karum_request_public(r) for r in requests],
        "my_requests": [_karum_request_public(r, own=True, bank_override=bank_solari)
                        for r in my_requests],
        "my_fills": [_karum_request_public(r, own=True) for r in my_fills],
        "bank_solari": bank_solari,
        "online": online,
        "offline_ok": online is False,
        "caps": {
            "max_price": _KARUM_MAX_PRICE,
            "listings_per_day": _KARUM_MAX_LISTINGS_PER_DAY,
            "listed_today": min(listed_today, _KARUM_MAX_LISTINGS_PER_DAY),
        },
        "flags": {"karum_enabled": _karum_enabled(),
                  "karum_wtb_enabled": _karum_wtb_enabled()},
        "character_name": row["character_name"],
        "csrf_token": csrf_for_session(session_token) if session_token else "",
    })


@router.get("/portal/karum/search")
async def portal_karum_search(request: Request):
    """Browse the board. Anonymous browsing is allowed; buying is not. Paged, and the
    sort is a closed set so the ORDER BY can never be client-supplied SQL."""
    q = (request.query_params.get("q") or "").strip()[:64]
    template = (request.query_params.get("template") or "").strip()[:64]
    sort = (request.query_params.get("sort") or "new").strip()
    side = (request.query_params.get("side") or "sell").strip()
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1

    order = {"new": "created_at DESC", "old": "created_at ASC",
             "cheap": "price ASC", "dear": "price DESC"}.get(sort, "created_at DESC")

    if side not in ("sell", "wanted"):
        return _v2_err("bad_request", "That board is not valid.", status=400)

    table = "portal_karum_listings" if side == "sell" else "portal_karum_requests"
    where = ["status = 'active'"]
    args = []
    if q:
        # Rows store the bare in-game name; the card shows "... Schematic" for a
        # schematic template. A query typed from the card therefore has the word
        # stripped for the name match and pinned to schematic templates instead.
        q_words = [w for w in q.split() if w.lower() != "schematic"]
        q_name = " ".join(q_words).strip()
        if len(q_words) != len(q.split()):
            where.append("lower(template_id) LIKE '%schematic'")
        if q_name:
            where.append("(display_name LIKE ? OR template_id LIKE ?)")
            args.extend([f"%{q_name}%", f"%{q_name}%"])
    if template:
        if not _MARKET_TERM_RE.fullmatch(template):
            return _v2_err("bad_request", "That item filter is not valid.", status=400)
        where.append("template_id = ?")
        args.append(template)

    # GRADE filter. `grade` is the grade the BROWSER HOLDS, not the number stored on the
    # row, because "which of these can I fill" is the question a filler actually has. So
    # it keeps legacy any-grade rows (a literal `quality_level = ?` would hide them, which
    # is backwards: those are the easiest orders to fill) and it keeps `min` orders at or
    # below the held grade.
    #
    # 🔴 This predicate MUST stay equivalent to _karum_quality_ok. Two copies of one rule
    # is the cost of filtering in SQL over a paged query; a client-side filter would break
    # `more` and hand out short pages. test_karum_grade proves the two agree over every
    # combination rather than trusting the comment.
    #
    # Empty string means "no filter"; "0" is Base and is a REAL filter, so this tests the
    # raw string for emptiness BEFORE converting.
    grade_raw = (request.query_params.get("grade") or "").strip()
    if grade_raw:
        if side != "wanted":
            return _v2_err("bad_request", "That filter is not valid on this board.",
                           status=400)
        try:
            grade = int(grade_raw)
        except ValueError:
            return _v2_err("bad_request", "That grade filter is not valid.", status=400)
        if grade < 0 or grade > _KARUM_MAX_QUALITY:
            return _v2_err("bad_request", "That grade filter is not valid.", status=400)
        where.append(
            "(quality_level IS NULL"
            " OR (COALESCE(quality_mode, 'exact') = 'min' AND quality_level <= ?)"
            " OR (COALESCE(quality_mode, 'exact') = 'exact' AND quality_level = ?))")
        args.extend([grade, grade])

    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(where)} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*args, _KARUM_PAGE_SIZE + 1, (page - 1) * _KARUM_PAGE_SIZE)).fetchall()
    finally:
        conn.close()

    more = len(rows) > _KARUM_PAGE_SIZE
    project = _karum_public if side == "sell" else _karum_request_public
    return _v2_ok({"rows": [project(r) for r in rows[:_KARUM_PAGE_SIZE]],
                   "side": side, "page": page, "more": more})


@router.get("/portal/karum/request/{request_id}")
async def portal_karum_request_detail(request: Request, request_id: int):
    row = _karum_request(request_id)
    if row is None or row["status"] not in _KARUM_REQUEST_LIVE_STATUSES + (
            "filled", "cancelled"):
        return _v2_err("request_gone", _KARUM_ERROR_TEXT["request_gone"], status=404)
    return _v2_ok({"request": _karum_request_public(row)})


@router.get("/portal/karum/catalog")
async def portal_karum_catalog(request: Request):
    """Public tradeable item catalogue for the sell and wanted-order typeaheads."""
    data = _karum_catalog_data()
    categorised = _karum_categorised()
    items = data["items"]
    if categorised:
        items = [row for row in items if row["template_id"].lower() in categorised]
    return _v2_ok({"items": items, "category_filter_live": categorised is not None})


@router.get("/portal/karum/listing/{listing_id}")
async def portal_karum_listing(request: Request, listing_id: int):
    """One listing. 404 for anything not live, never 403: we do not leak existence."""
    row = _karum_listing(listing_id)
    if row is None or row["status"] not in _KARUM_LIVE_STATUSES + ("sold", "cancelled"):
        return _v2_err("listing_gone", _KARUM_ERROR_TEXT["listing_gone"], status=404)
    return _v2_ok({"listing": _karum_public(row)})


def _karum_categorised():
    """The template set the Karum can hand back, or None when we cannot tell.

    See mirror.market_categorised_templates for why None means FAIL OPEN. The writer
    resolves the category inside the take transaction and refuses there, so the worst case
    for a false positive here is a clear refusal instead of a hidden row; the worst case for
    failing closed would be nothing being listable at all whenever the mirror hiccups."""
    try:
        return mirror.market_categorised_templates()
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: karum category set unavailable: %s", exc)
        return None


def _karum_no_category(tpl: str, categorised) -> bool:
    """True only when the template is PROVABLY undeliverable. Unknown is not a refusal."""
    if not categorised or not tpl:
        return False
    return tpl.lower() not in categorised


@router.get("/portal/karum/sellable")
async def portal_karum_sellable(request: Request):
    """The seller's own CHOAM bank AND backpack items that could be listed: tradeable, and
    not already in a live listing. Both are resolved for the SELECTED character via
    `_pick_pawn_storage_pair` (strict, no account fallback), because the container read is
    account-scoped and an unfiltered pick can hand back a tombstoned re-roll's bank.

    Backpack added 2026-08-25. 🔴 Every item carries its OWN `container_id` and `src`,
    because the two grids are different inventories and a client that remembered one
    top-level container id would ask to take a backpack item out of the bank. The
    top-level `container_id` remains the BANK for older clients."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    ctrl, _bank = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    bank, backpack = _pick_pawn_storage_pair(clist, ctrl)
    if bank is None:
        return _v2_err("no_bank", _KARUM_ERROR_TEXT["no_bank"], status=409)

    items = []
    for source, kind in ((bank, "bank"), (backpack, "backpack")):
        if source is None:
            continue
        grid = await _load_container_items(active_account_id, str(source.get("id")), 1)
        for raw in (grid.get("items") or []) if grid else []:
            row = _v2_item(raw)
            row["container_id"] = source.get("id")
            row["src"] = kind
            items.append(row)

    conn = get_db()
    try:
        live = {r[0] for r in conn.execute(
            "SELECT escrow_item_id FROM portal_karum_listings "
            f"WHERE status IN ({','.join('?' for _ in _KARUM_LIVE_STATUSES)}) "
            "AND escrow_item_id IS NOT NULL", _KARUM_LIVE_STATUSES)}
    finally:
        conn.close()

    # Hide what the Karum could not hand back. Counted rather than silently dropped: a
    # player looking for an item they can see in-game deserves to be told it is being
    # withheld, and by how much, instead of concluding the page is broken.
    categorised = _karum_categorised()
    candidates = [it for it in items
                  if it.get("tradeable") and it.get("item_id") not in live]
    sellable = [it for it in candidates
                if not _karum_no_category(it.get("template") or "", categorised)]
    return _v2_ok({"items": sellable, "container_id": bank.get("id"),
                   "backpack_container_id": backpack.get("id") if backpack else None,
                   "hidden_no_category": len(candidates) - len(sellable)})


@router.post("/portal/karum/request/post")
async def portal_karum_request_post(request: Request):
    """Post one whole-order wanted listing. No Solari is reserved at post time."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, srow = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    if not _karum_wtb_enabled():
        return _v2_ok({"status": "deferred", "message": _KARUM_ERROR_TEXT["wtb_not_open"]})

    template_id = str(body.get("template_id") or "").strip()
    stack_size = _v2_pos_int(body, "stack_size")
    price = _v2_pos_int(body, "price")
    if (not _MARKET_TERM_RE.fullmatch(template_id)
            or stack_size is None or price is None):
        return _v2_err("bad_request", "That wanted order was malformed.", status=400)
    if price < 1 or price > _KARUM_MAX_PRICE:
        return _v2_err("bad_price", _KARUM_ERROR_TEXT["bad_price"], status=400)
    quality_level, quality_mode, quality_err = _karum_parse_quality(body)
    if quality_err is not None:
        return _v2_err("bad_request", quality_err, status=400)

    import uuid as _uuid
    post_corr = str(body.get("uuid") or "").strip()
    if not _GUILD_OP_UUID_RE.match(post_corr):
        post_corr = str(_uuid.uuid4())
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT * FROM portal_karum_requests WHERE post_corr_id = ?",
            (post_corr,)).fetchone()
    finally:
        conn.close()
    if existing is not None:
        if int(existing["requester_account_id"]) != active_account_id:
            return _v2_err("bad_request", "That request key is already in use.", status=409)
        return _v2_ok({"status": "replay",
                       "request": _karum_request_public(existing, own=True)})

    catalog_item = _karum_catalog_data()["by_id"].get(template_id)
    if catalog_item is None:
        return _v2_err("not_tradeable", "That item is not in the tradeable catalogue.",
                       status=409)
    if stack_size > int(catalog_item["max_stack"]):
        return _v2_err(
            "quantity_mismatch",
            f"That item stacks to {int(catalog_item['max_stack']):,} at most.", status=400)
    # A grade on a template that has none would post an order nothing can ever fill, so
    # it is refused here rather than stored. The picker already hides the control; this
    # is the server-side half, because the picker is not the authority.
    if quality_level is not None and not catalog_item.get("is_gradeable"):
        return _v2_err("bad_request", "That item does not come in grades.", status=400)
    if _karum_no_category(template_id, _karum_categorised()):
        return _v2_err("no_category", _KARUM_ERROR_TEXT["no_category"], status=409)

    if _karum_count_events(active_account_id, (
            "list_applied", "request_post_applied")) >= _KARUM_MAX_LISTINGS_PER_DAY:
        return _v2_err("rate_limited", _KARUM_ERROR_TEXT["rate_limited"], status=429)

    ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    if ctrl is None:
        return _v2_err("unavailable", "We could not verify your character right now.",
                       status=502)

    raced = None
    conn = get_db()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO portal_karum_requests "
                "(requester_account_id, requester_discord_id, requester_name, requester_ctrl, "
                "template_id, display_name, category, stack_size, price, quality_level, "
                "quality_mode, post_corr_id, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active')",
                (active_account_id, str(discord_id) if discord_id else None,
                 srow["character_name"] or "Unknown", int(ctrl), template_id,
                 catalog_item.get("bare_name") or catalog_item["name"], catalog_item["category"], stack_size, price,
                 quality_level, quality_mode, post_corr))
            conn.commit()
            request_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            conn.rollback()
            raced = conn.execute(
                "SELECT * FROM portal_karum_requests WHERE post_corr_id = ?",
                (post_corr,)).fetchone()
            if raced is None:
                raise
    finally:
        conn.close()

    if raced is not None:
        if int(raced["requester_account_id"]) != active_account_id:
            return _v2_err("bad_request", "That request key is already in use.", status=409)
        return _v2_ok({"status": "replay",
                       "request": _karum_request_public(raced, own=True)})

    _karum_event(active_account_id, "request_post_applied", discord_id=discord_id,
                 detail={"request_id": request_id, "price": price,
                         "template": template_id, "stack_size": stack_size,
                         "quality": quality_level, "quality_mode": quality_mode})
    from auth import audit_log
    audit_log(None, f"portal:{discord_id}", "portal_karum_request_post",
              str(request_id), client_ip(request),
              details=json.dumps({"account_id": active_account_id,
                                  "template": template_id, "stack_size": stack_size,
                                  "price": price}), success=True)
    row = _karum_request(request_id)
    return _v2_ok({"status": "applied", "request": _karum_request_public(
        row, own=True, bank_override=bank_solari)})


@router.post("/portal/karum/request/fill")
async def portal_karum_request_fill(request: Request):
    """Fill one exact wanted order with one exact bank stack.

    The portal compare-and-set chooses the filler before any game write. The writer then
    commits the offline-gated take and payment together, followed by delivery.
    """
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, srow = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    if not _karum_wtb_enabled():
        return _v2_ok({"status": "deferred", "message": _KARUM_ERROR_TEXT["wtb_not_open"]})

    request_id = _v2_pos_int(body, "request_id")
    item_id = _v2_pos_int(body, "item_id")
    expected_price = _v2_pos_int(body, "expected_price")
    container_id = str(body.get("container_id") or "").strip()
    expected_template = str(body.get("expected_template") or "").strip()
    if (request_id is None or item_id is None or expected_price is None
            or not container_id.isdigit()
            or not _MARKET_TERM_RE.fullmatch(expected_template)):
        return _v2_err("bad_request", "That fill request was malformed.", status=400)

    wanted = _karum_request(request_id)
    if wanted is None or wanted["status"] != "active":
        return _v2_err("request_gone", _KARUM_ERROR_TEXT["request_gone"], status=409)
    if (int(wanted["price"]) != expected_price
            or wanted["template_id"] != expected_template):
        return _v2_err("request_gone", "The order changed. Refresh and try again.",
                       status=409)
    if int(wanted["requester_account_id"]) == active_account_id:
        return _v2_err("self_trade", "You cannot fill your own wanted order.", status=400)
    requester_discord = wanted["requester_discord_id"]
    if (not discord_id or not requester_discord
            or str(requester_discord) == str(discord_id)):
        return _v2_err("self_trade", "You cannot fill a wanted order from a linked alt.",
                       status=400)

    if await _resolve_online(active_account_id) is True:
        return _v2_err("player_online", "Log out of the game first, then fill. The item "
                       "can only leave your bank or backpack while you are offline.",
                       status=409)
    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One at a time. Wait a moment and retry.", status=429)
    if _karum_count_events(active_account_id, (
            "list_applied", "request_fill_applied")) >= _KARUM_MAX_LISTINGS_PER_DAY:
        return _v2_err("rate_limited", _KARUM_ERROR_TEXT["rate_limited"], status=429)

    filler_ctrl, _ = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    if filler_ctrl is None:
        return _v2_err("unavailable", "We could not verify your character right now.",
                       status=502)
    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    src, _src_container = _karum_resolve_src(clist, filler_ctrl, container_id)
    if src is None:
        return _v2_err("no_bank", _KARUM_ERROR_TEXT["no_bank"], status=409)
    owned = await _verify_seller_owns_item(active_account_id, container_id, item_id)
    if owned is None:
        return _v2_err("item_not_found", _KARUM_ERROR_TEXT["item_not_found"], status=409)
    if (owned.get("template_id") or "") != wanted["template_id"]:
        return _v2_err("item_not_found", "That is not the item this order requests.",
                       status=409)
    try:
        owned_stack = int(owned.get("stack_size") or 1)
    except (TypeError, ValueError):
        owned_stack = 0
    if owned_stack != int(wanted["stack_size"]):
        return _v2_err("quantity_mismatch", _KARUM_ERROR_TEXT["quantity_mismatch"],
                       status=409)
    # GRADE GATE. Refused here, before _karum_claim_request opens the settlement row and
    # before any game write, so a wrong-grade attempt costs the filler nothing and leaves
    # the order `active` for someone else. The writer re-checks inside its own take
    # transaction; this edge check exists to fail fast and with readable copy, not to be
    # the authority.
    wanted_quality, wanted_quality_mode = _karum_row_quality(wanted)
    if not _karum_quality_ok(owned.get("quality"), wanted_quality, wanted_quality_mode):
        return _v2_err("grade_mismatch", _KARUM_ERROR_TEXT["grade_mismatch"], status=409)
    if not owned.get("tradeable"):
        return _v2_err("not_tradeable", _KARUM_ERROR_TEXT["not_tradeable"], status=409)
    if _karum_no_category(wanted["template_id"], _karum_categorised()):
        return _v2_err("no_category", _KARUM_ERROR_TEXT["no_category"], status=409)

    import uuid as _uuid
    fill_corr = str(body.get("uuid") or "").strip()
    if not _GUILD_OP_UUID_RE.match(fill_corr):
        fill_corr = str(_uuid.uuid4())
    take_corr = str(_uuid.uuid5(
        _uuid.NAMESPACE_URL, f"lastsietch:karum:request:{request_id}:take:{fill_corr}"))

    listing_id = _karum_claim_request(
        wanted, filler_account_id=active_account_id, filler_discord_id=discord_id,
        filler_name=srow["character_name"] or "Unknown", filler_ctrl=filler_ctrl,
        owned=owned, fill_corr=fill_corr, take_corr=take_corr)
    if listing_id is None:
        return _v2_err("request_gone", _KARUM_ERROR_TEXT["request_gone"], status=409)

    _karum_event(active_account_id, "request_fill_attempt", listing_id=listing_id,
                 discord_id=discord_id,
                 detail={"request_id": request_id,
                         "counterparty": int(wanted["requester_account_id"]),
                         "price": int(wanted["price"])})
    result = await _karum_relay("/dune/karum/fill", {
        "request_id": request_id,
        "listing_id": listing_id,
        "buyer_account_id": int(wanted["requester_account_id"]),
        "buyer_ctrl": int(wanted["requester_ctrl"]),
        "seller_account_id": active_account_id,
        "seller_ctrl": int(filler_ctrl),
        "item_id": item_id,
        "template_id": wanted["template_id"],
        "stack_size": int(wanted["stack_size"]),
        # SERVER-resolved from the container id the client sent; see _karum_resolve_src.
        "src": src,
        # The writer pins the grade inside the take transaction. Omitted entirely when the
        # order is legacy any-grade, so an old row keeps its old meaning end to end.
        **({} if wanted_quality is None else {
            "expected_quality": int(wanted_quality),
            "quality_mode": wanted_quality_mode,
        }),
        "amount": int(wanted["price"]),
        "take_correlation_id": take_corr,
        "correlation_id": fill_corr,
        "requested_by_discord_id": str(discord_id) if discord_id else None,
        "operator": f"portal:{discord_id}",
    }, timeout=90)
    status = result.get("status")
    paid = result.get("paid")
    delivered = result.get("delivered")

    from auth import audit_log
    audit_log(None, f"portal:{discord_id}", "portal_karum_request_fill",
              f"{request_id}:{listing_id}", client_ip(request),
              details=json.dumps({"account_id": active_account_id,
                                  "requester_account_id": int(wanted["requester_account_id"]),
                                  "amount": int(wanted["price"]), "status": status,
                                  "paid": paid, "delivered": delivered}),
              success=bool(paid and delivered))

    if status == "deferred":
        _karum_cas(listing_id, "selling", "failed", stamp=("closed_at",))
        _karum_request_cas(request_id, "filling", "active", clear_fill=True)
        return _v2_ok({"status": "deferred", "message": _KARUM_ERROR_TEXT["wtb_not_open"]})

    if status == "unknown":
        _karum_cas(listing_id, "selling", "reconciling")
        _karum_request_cas(request_id, "filling", "reconciling")
        _karum_event(active_account_id, "request_fill_failed", listing_id=listing_id,
                     discord_id=discord_id,
                     detail=_karum_fail_detail(
                         result, "no_response", request_id=request_id,
                         counterparty=int(wanted["requester_account_id"])))
        return _v2_err("reconciling", "We are confirming this fill. Do not submit it "
                       "again. The item and payment will not execute twice.", status=202)

    if paid and delivered:
        _karum_cas(listing_id, "selling", "sold", stamp=("sold_at", "closed_at"),
                   escrow_item_id=item_id)
        _karum_request_cas(request_id, "filling", "filled",
                           stamp=("filled_at", "closed_at"))
        _karum_mirror(take_corr, listing_id, "list", active_account_id, status,
                      counterparty_id=int(wanted["requester_account_id"]),
                      template_id=wanted["template_id"], stack_size=int(wanted["stack_size"]),
                      game_item_id=item_id)
        _karum_mirror(fill_corr, listing_id, "pay",
                      int(wanted["requester_account_id"]), status,
                      counterparty_id=active_account_id, amount=int(wanted["price"]))
        _karum_mirror(f"{fill_corr}:deliver", listing_id, "deliver",
                      int(wanted["requester_account_id"]), status,
                      counterparty_id=active_account_id, template_id=wanted["template_id"],
                      stack_size=int(wanted["stack_size"]),
                      game_order_id=result.get("order_id"))
        _karum_event(active_account_id, "request_fill_applied", listing_id=listing_id,
                     discord_id=discord_id,
                     detail={"request_id": request_id,
                             "counterparty": int(wanted["requester_account_id"]),
                             "price": int(wanted["price"])})
        note = ("The requester has paid you. Their item is waiting in the Completed tab "
                "at any CHOAM Exchange terminal and shows as CANCELED, which is normal.")
        _karum_notify(int(wanted["requester_account_id"]),
                      "Your wanted order was filled",
                      f"{wanted['display_name']} is ready to collect. It is in the "
                      "Completed tab at any CHOAM Exchange terminal and shows as CANCELED.",
                      {"kind": "karum_request_filled", "listing_id": listing_id,
                       "item_name": wanted["display_name"],
                       "stack_size": int(wanted["stack_size"]),
                       "price": int(wanted["price"]),
                       "counterparty_name": srow["character_name"] or "Unknown",
                       "collect_at": "any CHOAM Exchange terminal"})
        _karum_notify(active_account_id, "You filled a wanted order",
                      f"You received {int(wanted['price']):,} Solari for "
                      f"{wanted['display_name']}.",
                      {"kind": "karum_request_paid", "listing_id": listing_id,
                       "item_name": wanted["display_name"],
                       "stack_size": int(wanted["stack_size"]),
                       "price": int(wanted["price"])})
        return _v2_ok({"status": status, "earned": int(wanted["price"]),
                       "collect_at": "any CHOAM Exchange terminal", "note": note})

    if paid and not delivered:
        _karum_cas(listing_id, "selling", "paid_undelivered", stamp=("sold_at",),
                   escrow_item_id=item_id)
        _karum_request_cas(request_id, "filling", "paid_undelivered")
        _karum_mirror(take_corr, listing_id, "list", active_account_id, "applied",
                      counterparty_id=int(wanted["requester_account_id"]),
                      template_id=wanted["template_id"], stack_size=int(wanted["stack_size"]),
                      game_item_id=item_id)
        _karum_mirror(fill_corr, listing_id, "pay",
                      int(wanted["requester_account_id"]), "applied",
                      counterparty_id=active_account_id, amount=int(wanted["price"]))
        _karum_event(active_account_id, "request_fill_failed", listing_id=listing_id,
                     discord_id=discord_id,
                     detail=_karum_fail_detail(
                         result, result.get("error") or "undelivered", request_id=request_id,
                         counterparty=int(wanted["requester_account_id"])))
        return _v2_err("paid_undelivered", "The requester paid you and the item is in "
                       "escrow while delivery is completed. Do not submit it again.", status=202)

    _karum_cas(listing_id, "selling", "failed", stamp=("closed_at",))
    _karum_request_cas(request_id, "filling", "active", clear_fill=True)
    token = result.get("error") or "write_failed"
    _karum_event(active_account_id, "request_fill_failed", listing_id=listing_id,
                 discord_id=discord_id,
                 detail=_karum_fail_detail(
                     result, token, request_id=request_id,
                     counterparty=int(wanted["requester_account_id"])))
    return _v2_err(token, _KARUM_ERROR_TEXT.get(token, _KARUM_ERROR_TEXT["write_failed"]),
                   status=409)


@router.post("/portal/karum/request/cancel")
async def portal_karum_request_cancel(request: Request):
    """Cancel an unfilled wanted order. No game state is touched."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate
    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    request_id = _v2_pos_int(body, "request_id")
    if request_id is None:
        return _v2_err("bad_request", "That cancellation was malformed.", status=400)
    wanted = _karum_request(request_id)
    if wanted is None or int(wanted["requester_account_id"]) != active_account_id:
        return _v2_err("request_gone", _KARUM_ERROR_TEXT["request_gone"], status=404)
    if not _karum_request_cas(request_id, "active", "cancelled", stamp=("closed_at",)):
        return _v2_err("request_gone", _KARUM_ERROR_TEXT["request_gone"], status=409)
    _karum_event(active_account_id, "request_cancelled", discord_id=discord_id,
                 detail={"request_id": request_id})
    from auth import audit_log
    audit_log(None, f"portal:{discord_id}", "portal_karum_request_cancel",
              str(request_id), client_ip(request),
              details=json.dumps({"account_id": active_account_id}), success=True)
    return _v2_ok({"status": "cancelled"})


@router.post("/portal/karum/list")
async def portal_karum_list(request: Request):
    """LIST: escrow one bank stack at a fixed ask. THE ONLY GATED LEG in the feature.

    A definitely-online seller is refused here with no relay touch; undetermined status
    falls through to the writer, which is the authoritative fail-closed gate. That is not
    politeness: taking an item from a loaded session is restored under its original item id
    (live-tested 2026-07-26), which is a duplication path."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, srow = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    item_id = _v2_pos_int(body, "item_id")
    price = _v2_pos_int(body, "price")
    container_id = str(body.get("container_id", "") or "").strip()
    expected_template = (body.get("expected_template") or "").strip() or None
    if item_id is None or price is None or not container_id.isdigit():
        return _v2_err("bad_request", "That listing request was malformed.", status=400)
    if price < 1 or price > _KARUM_MAX_PRICE:
        return _v2_err("bad_price", _KARUM_ERROR_TEXT["bad_price"], status=400)
    if expected_template is not None and not _MARKET_TERM_RE.fullmatch(expected_template):
        return _v2_err("bad_request", "That item could not be identified.", status=400)

    # OFFLINE-GATE (stop-ship). Undetermined is NOT rejected here; the writer hard-gates.
    if await _resolve_online(active_account_id) is True:
        return _v2_err("player_online", _KARUM_ERROR_TEXT["player_online"], status=409)

    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One at a time. Wait a moment and retry.", status=429)

    if _karum_count_events(active_account_id, (
            "list_applied", "request_post_applied")) >= _KARUM_MAX_LISTINGS_PER_DAY:
        return _v2_err("rate_limited", _KARUM_ERROR_TEXT["rate_limited"], status=429)

    ctrl, _bank = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    if ctrl is None:
        return _v2_err("unavailable", "We could not verify your character right now.",
                       status=502)

    # The item must be in the seller's OWN bank or backpack, verified through the
    # ownership-enforced container path. Never widen this to an arbitrary container; a
    # container they do not own yields None, and a colliding id is refused by the strict
    # pair match in _karum_resolve_src.
    containers = await _load_containers(active_account_id)
    clist = containers["containers"] if containers and containers.get("available") else []
    src, _src_container = _karum_resolve_src(clist, ctrl, container_id)
    if src is None:
        return _v2_err("no_bank", _KARUM_ERROR_TEXT["no_bank"], status=409)
    owned = await _verify_seller_owns_item(active_account_id, container_id, item_id)
    if owned is None:
        return _v2_err("item_not_found", _KARUM_ERROR_TEXT["item_not_found"], status=409)
    tpl = owned.get("template_id") or ""
    if expected_template and tpl != expected_template:
        return _v2_err("item_not_found", "That item changed. Refresh and try again.",
                       status=409)
    if not owned.get("tradeable"):
        return _v2_err("not_tradeable", _KARUM_ERROR_TEXT["not_tradeable"], status=409)
    if not _MARKET_TERM_RE.fullmatch(tpl):
        return _v2_err("item_not_found", _KARUM_ERROR_TEXT["item_not_found"], status=409)
    # PREVENTION, not politeness: an item whose template has no exchange category can be
    # escrowed and then handed to nobody -- not the buyer, not back to the seller, and not
    # by the operator page, because all three legs build an exchange order the same way.
    # This is the second of the two gates the fix needs (the first hides it from
    # /sellable); the writer is the authoritative one, and it also catches the mask-0
    # templates the mirror cannot see. Refusing here just saves a pointless relay round
    # trip and gives the player the real reason.
    if _karum_no_category(tpl, _karum_categorised()):
        return _v2_err("no_category", _KARUM_ERROR_TEXT["no_category"], status=409)

    import uuid as _uuid
    corr = (str(body.get("uuid", "") or "")).strip()
    if not _GUILD_OP_UUID_RE.match(corr):
        corr = str(_uuid.uuid4())

    # Snapshot the goods off the ownership-verified item. `quality` and the durability
    # pair arrive as display strings from _decorate_portal_items, so coerce explicitly
    # rather than letting sqlite guess: quality_level is an INTEGER the writer compares
    # against, and a silently-stored "" would read back as 0 and misgrade the listing.
    def _num(val, cast):
        try:
            return cast(val)
        except (TypeError, ValueError):
            return None

    quality = _num(owned.get("quality"), int) or 0
    dur_cur = _num(owned.get("cur_dur"), float)
    dur_max = _num(owned.get("max_dur"), float)

    # A `pending` row first, so the listing_id exists to carry into the writer and the
    # escrow ledger. Nothing is live and nothing is escrowed yet.
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO portal_karum_listings "
            "(seller_account_id, seller_discord_id, seller_name, seller_ctrl, template_id,"
            " display_name, stack_size, quality_level, durability_cur, durability_max,"
            " price, escrow_corr_id, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (active_account_id, str(discord_id) if discord_id else None,
             srow["character_name"] or "Unknown", int(ctrl), tpl,
             owned.get("name") or tpl, int(owned.get("stack_size") or 1),
             quality, dur_cur, dur_max, price, corr))
        conn.commit()
        listing_id = int(cur.lastrowid)
    finally:
        conn.close()

    ip = client_ip(request)
    _karum_event(active_account_id, "list_attempt", listing_id=listing_id,
                 discord_id=discord_id, detail={"price": price, "template": tpl})

    result = await _karum_relay("/dune/karum/list", {
        "listing_id": listing_id,
        "seller_account_id": active_account_id,      # from the session, NEVER the client
        "item_id": item_id,
        "template_id": tpl,
        # SERVER-resolved, never the client's word for it: the client sends a container id
        # and _karum_resolve_src decides which of the two pawn inventories that actually is.
        "src": src,
        "correlation_id": corr,
        "requested_by_discord_id": str(discord_id) if discord_id else None,
        "operator": f"portal:{discord_id}",
    })
    status = result.get("status")

    from auth import audit_log
    audit_log(None, f"portal:{discord_id}", "portal_karum_list",
              f"{listing_id}:{item_id}", ip,
              details=json.dumps({"account_id": active_account_id, "price": price,
                                  "template": tpl, "status": status}),
              success=status in ("applied", "replay"))

    if status == "deferred":
        # DARK: the writer did nothing, so neither do we. The pending row is closed as
        # failed rather than left dangling, and NO ledger row is written.
        _karum_cas(listing_id, "pending", "failed", stamp=("closed_at",))
        _karum_event(active_account_id, "list_failed", listing_id=listing_id,
                     discord_id=discord_id, detail={"reason": "deferred"})
        return _v2_ok({"status": "deferred", "message": _KARUM_ERROR_TEXT["not_open"]})

    if status in ("applied", "replay"):
        _karum_cas(listing_id, "pending", "active",
                   escrow_item_id=result.get("item_id") or item_id)
        _karum_mirror(corr, listing_id, "list", active_account_id, status,
                      template_id=tpl, stack_size=int(owned.get("stack_size") or 1),
                      game_item_id=result.get("item_id") or item_id)
        _karum_event(active_account_id, "list_applied", listing_id=listing_id,
                     discord_id=discord_id, detail={"price": price, "template": tpl})
        return _v2_ok({"listing_id": listing_id, "status": status})

    _karum_cas(listing_id, "pending", "failed", stamp=("closed_at",))
    token = result.get("error") or "write_failed"
    _karum_event(active_account_id, "list_failed", listing_id=listing_id,
                 discord_id=discord_id, detail=_karum_fail_detail(result, token))
    return _v2_err(token, _KARUM_ERROR_TEXT.get(token, _KARUM_ERROR_TEXT["write_failed"]),
                   status=409)


@router.post("/portal/karum/buy")
async def portal_karum_buy(request: Request):
    """BUY: pay, then take delivery. The compare-and-set below is the single point that
    decides a contested listing, and it happens BEFORE any game write so a losing racer is
    never charged.

    🔴 The exits from `selling` are the whole design. `paid:false` with a clean writer
    answer reverts to `active` (routine, self-service, buyer not charged). `paid && !
    delivered` is `paid_undelivered` and is RETRIED on the same correlation_id, never
    refunded. NO USABLE RESPONSE goes to `reconciling` and MUST NOT revert to `active`."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    listing_id = _v2_pos_int(body, "listing_id")
    expected_price = _v2_pos_int(body, "expected_price")
    if listing_id is None or expected_price is None:
        return _v2_err("bad_request", "That purchase request was malformed.", status=400)

    row = _karum_listing(listing_id)
    if row is None or row["status"] != "active":
        return _v2_err("listing_gone", _KARUM_ERROR_TEXT["listing_gone"], status=409)
    if int(row["price"]) != expected_price:
        return _v2_err("listing_gone", "The price changed. Refresh and try again.",
                       status=409)

    # 🔴 SELF-TRADE on BOTH keys. Linked alts share a discord_id, so the account check
    # alone is trivially defeated by someone who owns both sides of the trade. An
    # unresolvable identity on either side is a REJECT, not a pass: fail-closed, matching
    # the posture everywhere else.
    seller_account = int(row["seller_account_id"])
    if seller_account == active_account_id:
        return _v2_err("self_trade", _KARUM_ERROR_TEXT["self_trade"], status=400)
    seller_discord = row["seller_discord_id"]
    if not discord_id or not seller_discord or str(seller_discord) == str(discord_id):
        return _v2_err("self_trade", _KARUM_ERROR_TEXT["self_trade"], status=400)

    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One at a time. Wait a moment and retry.", status=429)
    if _karum_count_events(active_account_id, ("buy_applied",),
                           counterparty=seller_account) >= _KARUM_MAX_PAIR_PER_DAY:
        return _v2_err("rate_limited", _KARUM_ERROR_TEXT["rate_limited"], status=429)

    ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    if ctrl is None:
        return _v2_err("unavailable", "We could not verify your character right now.",
                       status=502)
    # Advisory only; the writer pre-checks the balance inside the locked transaction.
    if bank_solari is not None and int(bank_solari) < int(row["price"]):
        return _v2_err("insufficient_funds", _KARUM_ERROR_TEXT["insufficient_funds"],
                       status=409)

    import uuid as _uuid
    corr = (str(body.get("uuid", "") or "")).strip()
    if not _GUILD_OP_UUID_RE.match(corr):
        corr = str(_uuid.uuid4())

    # CONTENTION IS DECIDED HERE, before any game write.
    if not _karum_cas(listing_id, "active", "selling",
                      buyer_account_id=active_account_id,
                      buyer_discord_id=str(discord_id) if discord_id else None,
                      buyer_ctrl=int(ctrl), sold_corr_id=corr):
        return _v2_err("listing_gone", _KARUM_ERROR_TEXT["listing_gone"], status=409)

    ip = client_ip(request)
    _karum_event(active_account_id, "buy_attempt", listing_id=listing_id,
                 discord_id=discord_id,
                 detail={"counterparty": seller_account, "price": int(row["price"])})

    result = await _karum_relay("/dune/karum/buy", {
        "listing_id": listing_id,
        "buyer_account_id": active_account_id,       # from the session, NEVER the client
        "seller_account_id": seller_account,
        "amount": int(row["price"]),
        "correlation_id": corr,
        "requested_by_discord_id": str(discord_id) if discord_id else None,
        "operator": f"portal:{discord_id}",
    })
    status = result.get("status")
    paid = result.get("paid")
    delivered = result.get("delivered")

    from auth import audit_log
    audit_log(None, f"portal:{discord_id}", "portal_karum_buy",
              f"{listing_id}:{seller_account}", ip,
              details=json.dumps({"account_id": active_account_id,
                                  "amount": int(row["price"]), "status": status,
                                  "paid": paid, "delivered": delivered}),
              success=bool(paid and delivered))

    # ---- the four exits from `selling` (contract 6.3b) ----
    if status == "deferred":
        _karum_cas(listing_id, "selling", "active", buyer_account_id=None,
                   buyer_discord_id=None, buyer_ctrl=None, sold_corr_id=None)
        return _v2_ok({"status": "deferred", "message": _KARUM_ERROR_TEXT["not_open"]})

    if status == "unknown":
        # 🔴 A lost response is NOT evidence that payment failed. Reverting to `active`
        # here would let a second buyer purchase goods this buyer may already have paid
        # for. Resolved by re-driving the SAME correlation_id, never by guessing.
        _karum_cas(listing_id, "selling", "reconciling")
        # 🔴 The most diagnostically valuable failure in the feature: we do NOT know whether
        # money moved. The relay's `unknown` shape carries `message` and `stderr` from the
        # ssh/writer attempt, and that text is often the only clue whether the writer ever
        # opened a transaction. Keep every word of it.
        _karum_event(active_account_id, "buy_failed", listing_id=listing_id,
                     discord_id=discord_id,
                     detail=_karum_fail_detail(result, "no_response",
                                               counterparty=seller_account))
        return _v2_err("reconciling", _KARUM_ERROR_TEXT["reconciling"], status=202)

    if paid and delivered:
        _karum_cas(listing_id, "selling", "sold", stamp=("sold_at", "closed_at"))
        _karum_mirror(corr, listing_id, "pay", active_account_id, status,
                      counterparty_id=seller_account, amount=int(row["price"]))
        _karum_mirror(f"{corr}:deliver", listing_id, "deliver", active_account_id, status,
                      counterparty_id=seller_account, template_id=row["template_id"],
                      stack_size=row["stack_size"], quality_level=row["quality_level"],
                      game_order_id=result.get("order_id"))
        _karum_event(active_account_id, "buy_applied", listing_id=listing_id,
                     discord_id=discord_id,
                     detail={"counterparty": seller_account, "price": int(row["price"])})
        # The game will never say "purchase": completion_type 3 renders as CANCELED, so
        # the copy has to say what to expect or the player thinks it failed.
        note = ("It is waiting in the Completed tab at any CHOAM Exchange terminal. It "
                "shows there as CANCELED, which is the only way the game can display a "
                "collected item. That is normal.")
        _karum_notify(active_account_id, "You bought " + (row["display_name"] or "an item"),
                      "Your purchase is ready to collect. " + note,
                      {"kind": "karum_bought", "listing_id": listing_id,
                       "item_name": row["display_name"], "stack_size": row["stack_size"],
                       "price": int(row["price"]), "counterparty_name": row["seller_name"],
                       "collect_at": "any CHOAM Exchange terminal"})
        _karum_notify(seller_account, "Your listing sold",
                      f"{row['display_name']} sold for {int(row['price'])} Solari. "
                      "The Solari are in your bank balance.",
                      {"kind": "karum_sold", "listing_id": listing_id,
                       "item_name": row["display_name"], "stack_size": row["stack_size"],
                       "price": int(row["price"])})
        return _v2_ok({"status": status, "bank_after": (int(bank_solari) - int(row["price"]))
                       if bank_solari is not None else None,
                       "collect_at": "any CHOAM Exchange terminal", "note": note})

    if paid and not delivered:
        # Money moved, goods did not. RETRY the same correlation_id; never refund, because
        # if the delivery landed and only the response was lost, a refund-and-return
        # double-satisfies the trade and creates the item twice.
        _karum_cas(listing_id, "selling", "paid_undelivered", stamp=("sold_at",))
        _karum_mirror(corr, listing_id, "pay", active_account_id, "applied",
                      counterparty_id=seller_account, amount=int(row["price"]))
        # PAID BUT NOT DELIVERED: the buyer's money is gone and the goods are not theirs yet.
        # An operator resolves this by hand from the Karum page, so the writer's own words are
        # the difference between a two-minute fix and an investigation.
        _karum_event(active_account_id, "buy_failed", listing_id=listing_id,
                     discord_id=discord_id,
                     detail=_karum_fail_detail(result,
                                               result.get("error") or "undelivered",
                                               counterparty=seller_account))
        return _v2_err("paid_undelivered", _KARUM_ERROR_TEXT["paid_undelivered"], status=202)

    # Clean `paid: false`: transaction A rolled back whole, so payment provably did not
    # happen. Revert to `active` -- routine, self-service, no operator, and the listing is
    # immediately buyable again by anyone including this buyer.
    _karum_cas(listing_id, "selling", "active", buyer_account_id=None,
               buyer_discord_id=None, buyer_ctrl=None, sold_corr_id=None)
    token = result.get("error") or "write_failed"
    # counterparty stays a TOP-LEVEL key with this exact name: _karum_count_events enforces
    # the per-pair cap by matching the compact-JSON substring '"counterparty":<id>', so the
    # name and the compact separators are load-bearing. Extra sibling keys are harmless.
    _karum_event(active_account_id, "buy_failed", listing_id=listing_id,
                 discord_id=discord_id,
                 detail=_karum_fail_detail(result, token,
                                           counterparty=seller_account))
    return _v2_err(token, _KARUM_ERROR_TEXT.get(token, _KARUM_ERROR_TEXT["write_failed"]),
                   status=409)


@router.post("/portal/karum/cancel")
async def portal_karum_cancel(request: Request):
    """CANCEL: pull an unsold listing. The item comes back through the SAME claim lane a
    buyer collects from, so it stays give-only. No money is ever involved: payment happens
    at buy time and a cancel is only reachable from `active`."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    listing_id = _v2_pos_int(body, "listing_id")
    if listing_id is None:
        return _v2_err("bad_request", "That request was malformed.", status=400)

    row = _karum_listing(listing_id)
    # 404 rather than 403 for someone else's listing: never leak existence.
    if row is None or int(row["seller_account_id"]) != active_account_id:
        return _v2_err("listing_gone", _KARUM_ERROR_TEXT["listing_gone"], status=404)
    if row["status"] != "active":
        return _v2_err("listing_gone", "That listing is no longer active.", status=409)

    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One at a time. Wait a moment and retry.", status=429)

    import uuid as _uuid
    corr = (str(body.get("uuid", "") or "")).strip()
    if not _GUILD_OP_UUID_RE.match(corr):
        corr = str(_uuid.uuid4())

    if not _karum_cas(listing_id, "active", "returning"):
        return _v2_err("listing_gone", _KARUM_ERROR_TEXT["listing_gone"], status=409)

    ip = client_ip(request)
    _karum_event(active_account_id, "cancel_attempt", listing_id=listing_id,
                 discord_id=discord_id)

    result = await _karum_relay("/dune/karum/cancel", {
        "listing_id": listing_id,
        "seller_account_id": active_account_id,
        "price": int(row["price"]),
        "correlation_id": corr,
        "requested_by_discord_id": str(discord_id) if discord_id else None,
        "operator": f"portal:{discord_id}",
    })
    status = result.get("status")

    from auth import audit_log
    audit_log(None, f"portal:{discord_id}", "portal_karum_cancel", str(listing_id), ip,
              details=json.dumps({"account_id": active_account_id, "status": status}),
              success=status in ("applied", "replay"))

    if status == "deferred":
        _karum_cas(listing_id, "returning", "active")
        return _v2_ok({"status": "deferred", "message": _KARUM_ERROR_TEXT["not_open"]})

    if status in ("applied", "replay"):
        _karum_cas(listing_id, "returning", "cancelled", stamp=("closed_at",))
        _karum_mirror(corr, listing_id, "return", active_account_id, status,
                      template_id=row["template_id"], stack_size=row["stack_size"],
                      quality_level=row["quality_level"],
                      game_order_id=result.get("order_id"))
        _karum_event(active_account_id, "cancel_applied", listing_id=listing_id,
                     discord_id=discord_id)
        _karum_notify(active_account_id, "Listing pulled",
                      f"{row['display_name']} is waiting for you in the Completed tab at "
                      "any CHOAM Exchange terminal. It shows there as CANCELED, which is "
                      "the only way the game can display a returned item.",
                      {"kind": "karum_returned", "listing_id": listing_id,
                       "item_name": row["display_name"], "stack_size": row["stack_size"],
                       "collect_at": "any CHOAM Exchange terminal"})
        return _v2_ok({"status": status, "collect_at": "any CHOAM Exchange terminal"})

    # The return failed but the item is still safely in escrow and no money was involved.
    # Leave it in `returning` for the retry, then the admin force-return page.
    token = result.get("error") or "write_failed"
    # No event row here on purpose: the listing stays `returning` and a retry is expected, so
    # an event per attempt would bury the real history. But the writer's reason still has to
    # reach journald, which is what this call is for.
    _karum_fail_detail(result, token)
    return _v2_err(token, _KARUM_ERROR_TEXT.get(token, _KARUM_ERROR_TEXT["write_failed"]),
                   status=409)


# ----------------------------------------------------- rewards --------------
#
# Login-rewards V2: daily Solari ramp + rotating weekly T6 weapon + the Monthly
# Reward (a pre-augmented weapon), keyed PER-ACCOUNT (session.aid, never the
# client), manual claim. Streak + calendar claim-state are derived from the
# telemetry portal_login_days read (relay proxy) plus the local admin.db
# ls_reward_claims mirror. The grant runs on the game host via the reward-op
# relay path and ships DARK (LASTSIETCH_REWARD_ENABLED=0 -> a {status:'deferred'} that
# renders honestly as "not yet enabled", never a fake success). The Monthly
# Reward (Phase 2; wire value stays `monthly_augment`) is a deterministic
# per-(account, period) pick from rewards.MONTHLY_POOL, minted pre-augmented
# straight to the CHOAM bank -- the preview shown here and the item actually
# granted on claim are always the same pick (see rewards.monthly_pool_entry_for).
# Its roll VALUES are randomised by the writer at grant time (not perfect,
# not known ahead of the claim); only the augment names/grades/roll counts are
# fixed by the pool. Unlocks at rewards.MONTHLY_LOGIN_DAYS_REQUIRED distinct
# UTC login days within a FIXED 28-day period (rewards.monthly_period_start,
# NOT a calendar month -- see that function for why) -- a real earning
# requirement, not a lottery, per the owner's naming call.


async def _rewards_login_days(account_id: int) -> list:
    """The account's UTC login dates (newest-first) via the relay proxy; [] on miss."""
    from routers.dune import cached_reward_login_days
    payload = await cached_reward_login_days(account_id)
    return [r.get("date_utc") for r in (payload.get("login_days") or [])
            if r.get("date_utc")]


@router.get("/portal/rewards/overview")
async def portal_rewards_overview(request: Request):
    """Login-rewards overview (JSON) — the FROZEN canonical shape:
    {ok, enabled, server_now_utc, next_claim_utc, streak, daily, weekly, monthly,
     csrf_token}. Streak + calendar claim-state derive from the telemetry
    portal_login_days read plus the local admin.db mirror. `enabled` mirrors the
    game-host LASTSIETCH_REWARD_ENABLED (a cached dry-run probe): false while DARK -> the UI
    shows a "not yet enabled" preview banner and claims defer. Read-only, scoped to
    session.aid; degrades to an empty history (day 1) on miss."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    _touch_last_session(active_account_id)

    try:
        login_days = await _rewards_login_days(active_account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: rewards login-days fetch failed acct=%s: %s",
                       active_account_id, exc)
        login_days = []

    enabled = False
    try:
        from routers.dune import cached_rewards_enabled
        enabled = bool((await cached_rewards_enabled()).get("enabled"))
    except Exception:  # noqa: BLE001
        enabled = False

    login_set = set(login_days)
    today = rewards.utc_today()
    streak = rewards.compute_streak(login_days, today)

    daily_claimed = rewards.claimed_period_keys(active_account_id, "daily_solari")
    weekly_claimed = rewards.claimed_period_keys(active_account_id, "weekly_item")

    today_key = rewards.date_key(today)
    today_claimed = today_key in daily_claimed
    # Accumulate model: one claim grants every unclaimed logged day in the current
    # 7-day cycle (bounded to one ramp, <=122k). pool_total drives the Claim button.
    pool_total, pool_entries = rewards.claim_pool(login_days, daily_claimed, today)
    daily_claimable = streak["logged_today"] and pool_total > 0
    grid_cells = rewards.cycle_cells(login_days, daily_claimed, today)
    # Real dated 28-cell period calendar (owner-locked 2026-08-01), alongside
    # (not replacing) the old streak-relative grid above. The monthly
    # reward's progress count below is derived from THIS SAME list, so the
    # gauge and the grid can never disagree.
    calendar_cells = rewards.period_cells(login_days, daily_claimed, today)
    # Mark the cells the NEXT claim would actually sweep. period_cells knows nothing
    # about the 7-day ramp window, so without this the grid paints every logged
    # unclaimed day as collectible -- including days from an earlier cycle that
    # claim_pool will never carry ("claim within the week"). The player then sees a
    # Claim glyph on Solari that can no longer be claimed. Authoritative here rather
    # than re-derived client-side: the pool rule lives in claim_pool, not the UI.
    _pool_keys = {k for k, _ in pool_entries}
    for _c in calendar_cells:
        _c["in_pool"] = _c.get("date") in _pool_keys

    week_key = rewards.iso_week_key(today)
    weekly_template = rewards.weekly_template_for(today)
    weekly_meta = rewards.item_meta(weekly_template)
    weekly_is_claimed = week_key in weekly_claimed
    weekly_unlocked = streak["current"] >= rewards.WEEKLY_STREAK_REQUIREMENT
    weekly_claimable = weekly_unlocked and not weekly_is_claimed

    # Fixed 28-day period, not a calendar month (owner-locked 2026-08-01: a
    # calendar month makes the requirement unfair across months -- 15 of 28 is
    # 53.6% of Feb but only 48.4% of March). period_key = the period's own
    # start date, so it doubles as period_start below.
    monthly_claimed = rewards.claimed_period_keys(active_account_id, "monthly_augment")
    mstate = rewards.monthly_claim_state(login_days, monthly_claimed, today)
    # During an active grace window the card REPRESENTS the prior period's still-
    # unclaimed reward (earned, claim-by-deadline) rather than the fresh current
    # period; otherwise it is the current period. period_key doubles as period_start.
    if mstate["grace_active"]:
        period_key = mstate["prior_key"]
        period_end_str = mstate["prior_period_end"]
        # Prior period is met by definition here; cap the shown count at the
        # requirement so the card reads "15 of 15" rather than a raw >=15.
        monthly_progress = min(mstate["prior_progress"], rewards.MONTHLY_LOGIN_DAYS_REQUIRED)
        monthly_unlocked = True
        monthly_is_claimed = False
    else:
        period_key = mstate["current_key"]
        period_end_str = mstate["current_period_end"]
        # `progress` is surfaced below so the UI can render "N of <requirement>
        # days" before the gate is met -- a bare boolean makes the requirement
        # invisible until it is already satisfied. Not a streak: a missed day does
        # not reset this count (unlike the daily ramp). Equal by construction to
        # sum(c["logged"] for c in calendar_cells) -- both a distinct-day count
        # over the same current-period window -- so the grid and this never disagree.
        monthly_progress = mstate["current_progress"]
        monthly_unlocked = mstate["current_unlocked"]
        monthly_is_claimed = mstate["current_claimed"]
    monthly_claimable = mstate["claimable"]
    monthly_desc = rewards.monthly_reward_desc(active_account_id, period_key)
    _grace_dl = mstate["grace_deadline"]

    session_token = request.cookies.get(SESSION_COOKIE, "")
    return _v2_ok({
        "enabled": enabled,
        "server_now_utc": rewards._now_iso(),
        "next_claim_utc": rewards.next_utc_midnight().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "streak": {
            "current": streak["current"],
            "best": streak["best"],
            "logged_today": streak["logged_today"],
            "milestone_next": rewards.WEEKLY_STREAK_REQUIREMENT,
            # position in the current 7-day ramp cycle (drives the milestone gauge);
            # total streak keeps counting past a cycle, the gauge tracks 1..cycle_len.
            "cycle_day": streak["cycle_day"],
            "cycle_len": rewards.DAILY_CYCLE_LEN,
        },
        "daily": {
            "today": {
                "date": today_key,
                "cycle_day": streak["today_cycle_day"],
                # ACCUMULATED claimable across the current cycle (the Claim button).
                "amount": pool_total,
                "claimed": today_claimed,
                "claimable": daily_claimable,
            },
            "ramp": rewards.DAILY_SOLARI_RAMP,
            "milestones": list(rewards.DAILY_MILESTONE_DAYS),
            "cycle_len": rewards.DAILY_CYCLE_LEN,
            "weeks": 4,               # rows in the month grid (4 x 7-day cycles)
            "cycle": grid_cells,      # the current cycle, 7 cells with live state (kept for now)
            # Real dated 28-cell calendar (Monday-aligned, 4 rows of 7) for the
            # CURRENT monthly-reward period -- see rewards.period_cells. Added
            # alongside `cycle`, not a replacement (frontend migration is
            # separate work); the two must never be read as disagreeing.
            "calendar": calendar_cells,
            "claimable_total": pool_total,
            "claimable_days": len(pool_entries),
        },
        "weekly": {
            "week_key": week_key,
            "template_id": weekly_template,
            "name": weekly_meta["name"],
            "icon": weekly_meta["icon"],
            "tier": weekly_meta["tier"],
            "rarity": weekly_meta["rarity"],
            "quality_level": rewards.WEEKLY_QUALITY_LEVEL,
            "requirement": rewards.WEEKLY_STREAK_REQUIREMENT,
            "unlocked": weekly_unlocked,
            "claimed": weekly_is_claimed,
            "claimable": weekly_claimable,
            # the weapon rotates at the ISO-week boundary (Monday 00:00 UTC); surface
            # it so the UI can tell players when a new one arrives.
            "rotates_utc": rewards.next_week_start().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rotates_label": rewards.next_week_start().strftime("%a %b %d").replace(" 0", " "),
            "rotates_day": "Monday",
        },
        "monthly": {
            # period_start == period_key (the period's own start date); both a
            # fixed 28-day window, not a calendar month (see period_key above).
            # During grace these name the PRIOR period (the one being collected).
            "period_start": period_key,
            "period_end": period_end_str,
            "template_id": monthly_desc["template_id"],
            "name": monthly_desc["name"],
            "icon": monthly_desc["icon"],
            "rarity": monthly_desc["rarity"],
            "type": monthly_desc["type"],
            "quality_level": rewards.MONTHLY_AUGMENT_QUALITY_LEVEL,
            "augments": monthly_desc["augments"],
            # requirement/progress name the unlock rule the same way weekly names
            # its own ("requirement"): the UI can render "N of <requirement> days"
            # before the gate is met, not just a boolean.
            "requirement": rewards.MONTHLY_LOGIN_DAYS_REQUIRED,
            "progress": monthly_progress,
            "unlocked": monthly_unlocked,
            "claimed": monthly_is_claimed,
            "claimable": monthly_claimable,
            # the reward resets at the period boundary (always a Monday, 00:00 UTC).
            "resets_utc": rewards.next_monthly_period_start().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resets_label": rewards.next_monthly_period_start().strftime("%b %d").replace(" 0", " "),
            # Claim-grace: while `active`, the card above is the PRIOR period's
            # earned-but-unclaimed reward and stays claimable until `deadline_utc`
            # (00:00 UTC, GRACE days into the current period). The UI can show a
            # "claim last period's reward by <deadline_label>" nudge.
            "grace": {
                "active": mstate["grace_active"],
                "days": rewards.MONTHLY_CLAIM_GRACE_DAYS,
                "prior_period_start": mstate["prior_key"],
                "deadline_utc": _grace_dl.strftime("%Y-%m-%dT%H:%M:%SZ") if _grace_dl else None,
                "deadline_label": (_grace_dl.strftime("%b %d").replace(" 0", " ")
                                   if _grace_dl else None),
            },
        },
        "csrf_token": csrf_for_session(session_token) if session_token else "",
    })


@router.post("/portal/rewards/claim")
async def portal_rewards_claim(request: Request):
    """Claim a login reward (JSON). Body
    {reward_kind: daily_solari|weekly_item|monthly_augment, csrf_token}. Account
    is the SESSION aid, NEVER the client; the reward is resolved SERVER-SIDE.
    Idempotency is deterministic per (account, kind, period) so a retry /
    double-click collapses to one grant on the game host (no client uuid is trusted).
    Ships DARK: a writer {status:'deferred'} returns {ok:true,status:'deferred'}
    (render as "not yet enabled", never a success). Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    reward_kind = str(body.get("reward_kind", "") or "").strip()
    if reward_kind not in ("daily_solari", "weekly_item", "monthly_augment"):
        return _v2_err("bad_request", "Unknown reward.", status=400)

    # Recompute eligibility + the reward SERVER-SIDE from the login history; never
    # trust a client-supplied amount / template / day.
    try:
        login_days = await _rewards_login_days(active_account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: rewards claim login-days fetch failed acct=%s: %s",
                       active_account_id, exc)
        return _v2_err("unavailable", "The rewards service is unavailable right now. "
                       "Try again shortly.", status=502)

    today = rewards.utc_today()
    streak = rewards.compute_streak(login_days, today)

    # Each rewarded period is granted as its OWN idempotent game-host txn, keyed by a
    # deterministic idem for that period. So the daily accumulate sweep is idempotent
    # PER DAY: a re-run replays already-granted days and can NEVER double-grant, even
    # if a local-ledger write failed after an earlier grant. ledger_rows is the list of
    # (period_key, amount, template_id, quality_level, augments) to grant + record;
    # augments is only non-None for monthly_augment.
    ledger_rows: list = []

    if reward_kind == "daily_solari":
        daily_claimed = rewards.claimed_period_keys(active_account_id, "daily_solari")
        pool_total, pool_entries = rewards.claim_pool(login_days, daily_claimed, today)
        # The pool is ALREADY-EARNED Solari: claim_pool only counts days the player
        # logged in on and has not claimed, inside the current ramp cycle. Today is in
        # it only if they played today. So a non-empty pool can be collected whether or
        # not they have logged in yet TODAY -- requiring a fresh login to hand over a
        # day they earned last Tuesday just strands it until the cycle rolls and the
        # rule quietly deletes it (owner call 2026-08-03). The logged-in requirement
        # still governs TODAY's rung, which simply is not in the pool until they play.
        if pool_total <= 0 or not pool_entries:
            if not streak["logged_today"]:
                return _v2_err("not_logged_in",
                               "Log in to the game today to earn today's reward.",
                               status=409)
            return _v2_err("already_claimed", "You already claimed this cycle's rewards. "
                           "Come back tomorrow.", status=409)
        for k, amt in pool_entries:   # one grant + one ledger row per swept day
            ledger_rows.append((k, amt, None, None, None))
    elif reward_kind == "weekly_item":
        week_key = rewards.iso_week_key(today)
        if streak["current"] < rewards.WEEKLY_STREAK_REQUIREMENT:
            return _v2_err("locked",
                           f"Reach a {rewards.WEEKLY_STREAK_REQUIREMENT} day streak to "
                           "unlock the weekly weapon.", status=409)
        if rewards.is_claimed(active_account_id, "weekly_item", week_key):
            return _v2_err("already_claimed", "You already claimed this week's weapon. "
                           "A new one rotates in next week.", status=409)
        ledger_rows.append((week_key, None, rewards.weekly_template_for(today),
                            rewards.WEEKLY_QUALITY_LEVEL, None))
    else:  # monthly_augment
        # Resolve eligibility SERVER-SIDE (never trust a client day-count claim),
        # including the grace window: monthly_claim_state picks the CURRENT period
        # when it is unlocked+unclaimed, else the PRIOR period while grace is open
        # (met last period, never claimed). period_key is what the grant is recorded
        # under -- the prior key during a grace claim, so it stays period-scoped.
        monthly_claimed = rewards.claimed_period_keys(active_account_id, "monthly_augment")
        mstate = rewards.monthly_claim_state(login_days, monthly_claimed, today)
        period_key = mstate["claim_key"]
        if period_key is None:
            if mstate["current_claimed"]:
                return _v2_err("already_claimed", "You already claimed this period's reward. "
                               "A new one arrives next period.", status=409)
            progress = mstate["current_progress"]
            return _v2_err("locked",
                           f"Log in on {rewards.MONTHLY_LOGIN_DAYS_REQUIRED} days before "
                           f"the reward period ends to unlock the monthly reward "
                           f"({progress} of {rewards.MONTHLY_LOGIN_DAYS_REQUIRED} so far).",
                           status=409)
        entry = rewards.monthly_pool_entry_for(active_account_id, period_key)
        ledger_rows.append((period_key, None, entry["template_id"],
                            entry["quality_level"], entry["augments"]))

    ip = client_ip(request)
    from relay import call_relay
    from auth import audit_log

    async def _grant(pk, amt, tid, ql, augments=None):
        """One idempotent game-host grant for period `pk`; audits + returns the result
        dict (None on relay error). Idempotency is per (account, kind, pk)."""
        body = {"account_id": active_account_id,  # from session, NEVER the client
                "reward_kind": reward_kind, "mode": "apply",
                "operator": f"portal:{discord_id}"[:64],
                "period_key": pk,   # monthly cap is period-scoped, not calendar-month
                "idempotency_key": rewards.deterministic_idem(
                    active_account_id, reward_kind, pk)}
        if amt is not None:
            body["amount"] = amt
        if tid is not None:
            body["template_id"] = tid
            body["quality_level"] = ql
        if augments is not None:
            body["augments"] = augments
        res = None
        try:
            res = await call_relay("/dune/reward-op", method="POST",
                                   json_body=body, timeout=45)
        except Exception as exc:  # noqa: BLE001
            logger.warning("portal: reward relay error acct=%s pk=%s: %s",
                           active_account_id, pk, exc)
        finally:
            audit_log(None, f"portal:{discord_id}", "portal_reward_claim",
                      f"{reward_kind}:{pk}", ip,
                      details=json.dumps({"account_id": active_account_id,
                                          "reward_kind": reward_kind, "period": pk,
                                          "status": (res or {}).get("status")
                                          if isinstance(res, dict) else None}))
        return res if isinstance(res, dict) else None

    granted_amount = 0
    granted_n = 0
    last_result = None
    for (pk, amt, tid, ql, augments) in ledger_rows:
        res = await _grant(pk, amt, tid, ql, augments)
        last_result = res
        if res is None:
            break  # relay failure: granted days stand + are recorded; retry finishes
        st = res.get("status")
        if st == "deferred":
            break  # DARK (global flag) -> nothing granted; stop
        if st in ("applied", "replay") and res.get("success"):
            # A replay means the game host already had this day (a prior local write
            # failed) -> record it now so the grid catches up; no double grant occurs.
            try:
                detail = {"audit_id": res.get("audit_id")}
                if augments:
                    detail["augments"] = augments
                rewards.record_claim(
                    active_account_id, reward_kind, pk,
                    rewards.deterministic_idem(active_account_id, reward_kind, pk),
                    st, amount=amt, template_id=tid, quality_level=ql,
                    detail=detail)
            except Exception:  # noqa: BLE001
                logger.warning("portal: reward local-mirror write failed acct=%s kind=%s pk=%s",
                               active_account_id, reward_kind, pk, exc_info=True)
            granted_amount += (amt or 0)
            granted_n += 1
        else:
            break  # refusal (cap / bank_full / locked) -> stop, classified below

    if granted_n > 0:
        # At least one period landed. A partial sweep (a later day's relay failed)
        # finishes on the next claim: those days are still in the pool, idempotent.
        out = {"status": "applied", "reward_kind": reward_kind,
               "message": (last_result or {}).get("message") or "Reward claimed."}
        if reward_kind == "daily_solari":
            out["amount"] = granted_amount
            out["days"] = granted_n
        else:
            out["template_id"] = ledger_rows[0][2]
            out["quality_level"] = ledger_rows[0][3]
            if ledger_rows[0][4]:
                out["augments"] = ledger_rows[0][4]
        return _v2_ok(out)

    if last_result is None:
        return _v2_err("unavailable", "The rewards service is unavailable right now. "
                       "Try again shortly.", status=502)
    # DARK: LASTSIETCH_REWARD_ENABLED=0 returns status:'deferred' with NO grant.
    if last_result.get("status") == "deferred":
        return _v2_ok({"status": "deferred",
                       "message": "Login rewards are not enabled yet."})
    # Classify the refusal (bank_full is RETRYABLE and must be caught before the
    # generic "cap"/"already" match, since "bank capacity exceeded" contains "cap").
    failure = rewards.classify_claim_failure(last_result)
    if failure is not None:
        code, fmsg, fstatus = failure
        return _v2_err(code, fmsg, status=fstatus)
    return _v2_err("unavailable",
                   last_result.get("message") or "That reward could not be claimed.",
                   status=502)


# ----------------------------------------------------- market ---------------
#
# Phase 1 (browse, read-only): a public mirror of the live CHOAM exchange,
# OAuth-gated so only confirmed players see it. It reuses the exact admin
# listings-search path (relay /dune/market/listings?q=) -- there is NO buy or
# sell here, and nothing is per-account: the exchange is global market data.
# Players search/browse by a template_id-safe fragment (category chips supply
# common ones); we group the matching listings by item for a market overview.

import re as _re

_MARKET_TERM_RE = _re.compile(r"[A-Za-z0-9_-]{2,64}")

# Browse chips: friendly label -> a template_id-safe fragment that the exchange
# search matches (ILIKE substring). Fragments are calibrated against the LIVE
# exchange (template_ids use short stems like 'kindjal'/'augment', not generic
# words like 'weapon'); each one here had active listings at deploy time.
_MARKET_CATEGORIES = [
    {"label": "Schematics", "q": "schematic"},
    {"label": "Augments", "q": "augment"},
    {"label": "Stillsuits", "q": "stillsuit"},
    {"label": "Armor", "q": "armor"},
    {"label": "Buggy parts", "q": "buggy"},
    {"label": "Sandbike parts", "q": "sandbike"},
    {"label": "Kindjals", "q": "kindjal"},
    {"label": "Spice", "q": "spice"},
]


def _decorate_market_listings(payload: dict) -> dict:
    """Group the relay's flat listing rows by item template for a browse view.
    Each group surfaces the friendly name + icon, how many listings exist, the
    total quantity on offer, the price range, and whether NPC (market-maker bot)
    and/or player sellers are present. Returns a render-ready envelope."""
    rows = payload.get("listings") or []
    groups: dict = {}
    for r in rows:
        tpl = r.get("template_id") or ""
        if not tpl:
            continue
        g = groups.get(tpl)
        if g is None:
            g = groups[tpl] = {
                "template_id": tpl,
                "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
                "icon": _icon_for(tpl),
                "listing_count": 0,
                "total_qty": 0,
                "min_price": None,
                "max_price": None,
                "has_npc": False,
                "has_player": False,
            }
        price = r.get("item_price")
        qty = int(r.get("stack") or 0)
        g["listing_count"] += 1
        g["total_qty"] += qty
        if isinstance(price, int):
            g["min_price"] = price if g["min_price"] is None else min(g["min_price"], price)
            g["max_price"] = price if g["max_price"] is None else max(g["max_price"], price)
        if r.get("is_npc_order"):
            g["has_npc"] = True
        else:
            g["has_player"] = True

    for g in groups.values():
        lo, hi = g["min_price"], g["max_price"]
        if lo is None:
            g["price_display"] = "—"
        elif hi is None or hi == lo:
            g["price_display"] = f"{lo:,}"
        else:
            g["price_display"] = f"{lo:,} – {hi:,}"
        g["total_qty_display"] = f"{g['total_qty']:,}"

    items = sorted(
        groups.values(),
        key=lambda g: (-g["listing_count"], g["min_price"] if g["min_price"] is not None else 1 << 62),
    )[:60]
    return {
        "available": bool(payload.get("listings") is not None),
        "items": items,
        "item_count": len(items),
        "total_matches": payload.get("total_matches"),
        "shown": payload.get("shown"),
        "limit": payload.get("limit") or 200,
    }


async def _load_market_listings(needle: str) -> dict:
    """Validate + fetch + shape live exchange listings for a search term. Returns
    {too_short|invalid} for bad input and {error} on relay failure so the page
    degrades gracefully without leaking internals."""
    needle = (needle or "").strip()
    if len(needle) < 2:
        return {"too_short": True, "items": [], "available": True}
    if not _MARKET_TERM_RE.fullmatch(needle):
        return {"invalid": True, "items": [], "available": True}
    try:
        payload = mirror.search_market(needle)
        if payload is None:
            from routers.dune import _cached_market_listings
            payload = await _cached_market_listings(needle)
    except Exception as exc:
        logger.warning("portal: market listings fetch failed: %s", exc)
        return {"error": True, "items": [], "available": False}
    return _decorate_market_listings(payload or {})


def _price_range_display(lo, hi):
    if lo is None:
        return "—"
    if hi is None or hi == lo:
        return f"{lo:,}"
    return f"{lo:,} – {hi:,}"


_MARKET_PAGE_SIZE = 30


def _merge_bot_buyable(summary: list, bots: dict | None) -> list:
    """Append zero-listing rows for items the market-maker bot buys but nobody
    currently sells, so search/category views answer “what would the bot pay
    for X” even when X is absent from the exchange. No-op when the bot-price
    mirror is unavailable."""
    if not bots:
        return summary
    listed = {r["template_id"].lower() for r in summary}
    merged = list(summary)
    for key, bp in bots.items():
        if key in listed or not bp.get("buyable"):
            continue
        merged.append({"template_id": bp["template_id"], "listing_count": 0,
                       "total_qty": 0, "min_price": None, "max_price": None,
                       "has_npc": False, "has_player": False, "max_quality": 0})
    return merged


def _bot_buy_base_cap(bots: dict | None, tpl: str):
    """The bot's per-unit buy cap for the item's lowest exported quality grade
    (the base item a player most likely sells), or None when the bot won't buy
    it / the export is unavailable."""
    bb = (bots or {}).get(tpl.lower())
    if not bb or not bb.get("buyable") or not bb.get("caps"):
        return None
    try:
        grade = min(bb["caps"], key=lambda k: int(k))
        return int(bb["caps"][grade])
    except (TypeError, ValueError):
        return None


def _decorate_market_summary(rows: list, query: str, sort: str,
                             category: str = "all", page: int = 1,
                             kind: str = "all", bots: dict | None = None) -> dict:
    """Resolve friendly names over the grouped market summary, filter by query
    (item NAME or template fragment), top-level category AND kind (item vs
    schematic), sort, paginate, and decorate for the browse grid. Category-scoping
    + paging replace the old flat top-80 cap so the page doesn't scroll forever."""
    q = (query or "").strip().lower()
    cat = category if market_categories.is_valid(category) else "all"
    kind = kind if kind in ("all", "item", "schematic") else "all"
    page = max(int(page or 1), 1)
    items = []
    for r in rows:
        tpl = r["template_id"]
        if cat != "all" and market_categories.classify(tpl) != cat:
            continue
        sch = market_categories.is_schematic(tpl)
        if kind == "item" and sch:
            continue
        if kind == "schematic" and not sch:
            continue
        name = _ITEM_NAMES.lookup_or_synthesize(tpl)
        if q and q not in name.lower() and q not in tpl.lower():
            continue
        lo = r["min_price"]
        hi = r["max_price"]
        cap = _bot_buy_base_cap(bots, tpl)
        items.append({
            "template_id": tpl,
            "name": name,
            "icon": _icon_for(tpl),
            "is_schematic": sch,
            "listing_count": r["listing_count"],
            "total_qty": r["total_qty"],
            "total_qty_display": f"{r['total_qty']:,}",
            "_min_price": lo if lo is not None else (1 << 62),
            "_max_price": hi if hi is not None else 0,
            "price_display": _price_range_display(lo, r["max_price"]),
            "has_npc": r["has_npc"],
            "has_player": r["has_player"],
            "bot_buy_display": f"{cap:,}" if cap else None,
            # Highest quality grade among the listings — lets the row explain
            # a wide min–max price range (base vs graded chase gear).
            "max_quality": r.get("max_quality") or 0,
        })
    if sort == "price":
        items.sort(key=lambda g: g["_min_price"])
    elif sort == "expensive":
        items.sort(key=lambda g: -g["_max_price"])
    elif sort == "name":
        items.sort(key=lambda g: g["name"].lower())
    else:  # "active" (default): busiest items first
        items.sort(key=lambda g: (-g["listing_count"], g["_min_price"]))
    total = len(items)
    start = (page - 1) * _MARKET_PAGE_SIZE
    page_items = items[start:start + _MARKET_PAGE_SIZE]
    return {"available": True, "items": page_items, "item_count": len(page_items),
            "total_items": total, "sort": sort, "category": cat, "kind": kind,
            "page": page, "has_more": start + _MARKET_PAGE_SIZE < total,
            "append": page > 1}


async def _load_market_browse(query: str, sort: str, category: str = "all",
                              page: int = 1, kind: str = "all") -> dict:
    """Browse/search the exchange by item name (or template fragment), scoped to a
    top-level category + kind (item/schematic) + paginated, powered by the local
    market mirror summary. Falls back to the live relay term search if the mirror
    is unavailable (the fallback is name-search only — no category/kind/paging).

    On a search or category view the summary is widened with items the
    market-maker bot will BUY even though nothing is listed right now (zero
    listings, price “—”), so a player can always look up what the bot pays —
    the default browse-all view stays a true picture of the live exchange."""
    summary = mirror.market_summary()
    if summary is not None:
        bots = mirror.bot_prices_all()
        if len((query or "").strip()) >= 2 or (
                category != "all" and market_categories.is_valid(category)):
            summary = _merge_bot_buyable(summary, bots)
        return _decorate_market_summary(summary, query, sort, category, page,
                                        kind, bots=bots)
    q = (query or "").strip()
    if len(q) < 2:
        return {"available": True, "items": [], "item_count": 0,
                "total_items": 0, "browse_unavailable": True}
    if not _MARKET_TERM_RE.fullmatch(q):
        return {"invalid": True, "items": [], "available": True}
    return await _load_market_listings(q)


async def _load_market_item(template_id: str) -> dict:
    """The price ladder for one item: every current listing, cheapest first, with
    NPC/player + quality + qty. Local mirror first; relay fallback filtered to the
    exact template."""
    tpl = (template_id or "").strip()
    if not tpl or not _MARKET_TERM_RE.fullmatch(tpl):
        return {"available": False, "error": "invalid", "listings": []}
    listings = None
    detail = mirror.market_item_detail(tpl)
    if detail is not None:
        listings = detail["listings"]
    else:
        try:
            from routers.dune import _cached_market_listings
            payload = await _cached_market_listings(tpl)
            raw = (payload or {}).get("listings") or []
            listings = [l for l in raw if (l.get("template_id") or "").lower() == tpl.lower()]
            listings.sort(key=lambda l: (l.get("item_price") or 0, l.get("is_npc_order")))
        except Exception as exc:
            logger.warning("portal: market item fetch failed: %s", exc)
            return {"available": False, "error": True, "listings": []}
    rows = [{
        "price_display": f"{(l.get('item_price') or 0):,}",
        "price": l.get("item_price") or 0,
        "qty": l.get("stack") or l.get("initial_stack_size") or 1,
        "quality": l.get("quality_level") or 0,
        "is_npc": bool(l.get("is_npc_order")),
        # order_id + revision are carried into the row so the Buy control can
        # pin the exact listing the player saw; the writer fails closed if the
        # revision drifts. Absent (older mirror / pre-producer-change) => no Buy.
        "order_id": l.get("order_id"),
        "revision": l.get("revision"),
        "buyable": l.get("order_id") is not None and l.get("revision") is not None,
    } for l in listings]
    return {
        "available": True,
        "template_id": tpl,
        "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
        "icon": _icon_for(tpl),
        "is_schematic": market_categories.is_schematic(tpl),
        "count": len(rows),
        "listings": rows,
        "bot_buy": _bot_buy_for(tpl),
    }


def _bot_buy_for(tpl: str):
    """What the market-maker bot currently pays for this item, shaped for the
    drawer: {"buyable", "tiers": [{grade, cap, cap_display}]} with tiers sorted
    by quality grade (one tier, grade 0, for non-gradeable items). None when
    the bot export is unavailable or the item is unknown to the bot — the
    template then simply omits the line."""
    bp = mirror.bot_price_for(tpl)
    if bp is None:
        return None
    caps = bp.get("caps") or {}
    tiers = []
    for g, cap in caps.items():
        try:
            tiers.append({"grade": int(g), "cap": int(cap),
                          "cap_display": f"{int(cap):,}"})
        except (TypeError, ValueError):
            continue
    tiers.sort(key=lambda t: t["grade"])
    return {"buyable": bool(bp.get("buyable")) and bool(tiers), "tiers": tiers}


@router.get("/portal/market")
async def portal_market(request: Request):
    """Public player page: browse + search the live CHOAM exchange listings.
    Read-only; no buying or selling. Listings are global market data, but the
    page is OAuth-gated to confirmed players for consistency with the rest of
    the portal."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, row = gate

    _touch_last_session(active_account_id)

    # The Market page is the watchlist hub now: show the player's watches +
    # recent alerts as a card, and clear the nav bell on visit.
    watches = market_watch.list_watches(active_account_id)
    alerts = market_watch.list_alerts(active_account_id, limit=6)
    for w in watches:
        w["icon"] = _icon_for(w["template_id"])
    for a in alerts:
        a["icon"] = _icon_for(a["template_id"])
    # Clear only the alerts this page actually renders. It shows a 6-row slice, so
    # the old unbounded clear marked rows seen that were never on the page.
    market_watch.mark_alerts_seen(active_account_id, [a["id"] for a in alerts])

    ctx = {
        "active_character_name": row["character_name"],
        "market_tabs": market_categories.CATEGORIES,
        "watches": watches,
        "watch_count": len(watches),
        "watch_cap": config.MARKET_WATCH_MAX_PER_ACCOUNT,
        "alerts": alerts,
        "logout_post_url": "/portal/logout",
    }
    ctx.update(base_ctx(request, discord_handle=row["discord_handle"]))
    ctx["portal_alert_count"] = 0  # just cleared
    return templates.TemplateResponse(request, "portal/market.html", ctx)


@router.get("/portal/market/search")
async def portal_market_search(request: Request, q: str = "", sort: str = "active",
                               category: str = "all", page: int = 1, kind: str = "all"):
    """Exchange browse/search fragment. Searches by item NAME or template fragment
    over the local market mirror, scoped to a top-level category + kind (item vs
    schematic) + paginated. Empty q = browse-all (busiest items). Used by the
    search box, category tabs, kind filter, sort control, and Load-more."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early

    sort = sort if sort in ("active", "price", "expensive", "name") else "active"
    category = category if market_categories.is_valid(category) else "all"
    kind = kind if kind in ("all", "item", "schematic") else "all"
    try:
        page = max(int(page), 1)
    except (TypeError, ValueError):
        page = 1
    results = await _load_market_browse((q or "").strip(), sort, category, page, kind)
    return templates.TemplateResponse(
        request, "portal/_fragments/market_listings.html",
        {"results": results, "query": (q or "").strip(), "sort": sort,
         "category": category, "kind": kind},
    )


@router.get("/portal/market/item")
async def portal_market_item(request: Request, tpl: str = ""):
    """Item price-ladder fragment: every current listing for one item, cheapest
    first (the 'shop this item' detail). Opened from a row on the Market page.
    Also surfaces the signed-in player's bank Solari so the Buy confirm step can
    show what they have to spend (display only — the writer re-checks the bank)."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    detail = await _load_market_item((tpl or "").strip())
    progress = await _load_progress(active_account_id)
    econ = (progress or {}).get("economy") or {}
    bank_solari = econ.get("bank_solari", econ.get("solari"))
    session_token = request.cookies.get(SESSION_COOKIE, "")
    return templates.TemplateResponse(
        request, "portal/_fragments/market_item.html",
        {"detail": detail,
         "bank_solari": bank_solari,
         "bank_solari_display": f"{bank_solari:,}" if bank_solari is not None else None,
         "portal_csrf_token": csrf_for_session(session_token) if session_token else ""},
    )


# Per-account BUY pacing: one purchase at a time, with a short minimum interval
# between purchases, to stop a confirm-spam loop from firing many writes. The
# portal is single-worker (see portal_rate_limit.py), so an in-process dict is
# the source of truth; it resets on restart (acceptable for a courtesy throttle).
_BUY_MIN_INTERVAL_SECONDS = 3.0
_BUY_MAX_COUNT = 10000  # hard ceiling; the writer also rejects count > stack
_last_buy_at: dict = {}


def _buy_rate_ok(account_id: int) -> bool:
    now = time.monotonic()
    last = _last_buy_at.get(account_id)
    if last is not None and (now - last) < _BUY_MIN_INTERVAL_SECONDS:
        return False
    _last_buy_at[account_id] = now
    return True


async def _resolve_buyer_ctrl_and_bank(account_id: int, selected_ctrl: int = None):
    """Resolve the buyer's controller_id (and current bank Solari) SERVER-SIDE
    from a FRESH per-account progress read. controller_id is never accepted from
    the client — a player can only spend their own bank. Returns
    (controller_id|None, bank_solari|None).

    Portal multi-character: `selected_ctrl` (from the signed selected-character
    cookie) scopes the read to one character on the account. The host script
    re-validates that controller against the account's live, non-Deleted
    characters and falls back to the default most-recently-active pick when it
    does not resolve, so a stale/forged selection can never target another
    account's character (and the returned bank is always the resolved
    character's own bank)."""
    try:
        from relay import call_relay
        path = f"/dune/player/{account_id}/progress"
        if selected_ctrl is not None:
            path += f"?ctrl={int(selected_ctrl)}"
        payload = await call_relay(path, timeout=20)
    except Exception as exc:
        logger.warning("portal: buyer-ctrl resolve failed for acct %s: %s",
                       account_id, exc)
        return None, None
    if not isinstance(payload, dict) or not payload.get("available"):
        return None, None
    ctrl = payload.get("player_controller_id")
    econ = payload.get("economy") or {}
    bank = econ.get("bank_solari", econ.get("solari"))
    try:
        ctrl = int(ctrl) if ctrl is not None else None
    except (TypeError, ValueError):
        ctrl = None
    try:
        bank = int(bank) if bank is not None else None
    except (TypeError, ValueError):
        bank = None
    return ctrl, bank


# Friendly text for the writer's error tokens (the writer/relay own these).
_BUY_ERROR_TEXT = {
    "insufficient_bank": "Not enough banked Solari for this purchase.",
    "revision_drift": "This listing just changed. Refresh and try again.",
    "order_gone": "This listing is no longer available. Refresh and try again.",
    "count_exceeds_stack": "That is more than the listing has in stock.",
    "fulfill_failed": "The exchange could not complete this purchase. "
                      "Your order slots may be full, or the listing changed.",
}


def _buy_result_response(request: Request, *, ok: bool, message: str,
                         bank_display: str = None, status: int = 200):
    return templates.TemplateResponse(
        request, "portal/_fragments/market_buy_result.html",
        {"ok": ok, "message": message, "bank_display": bank_display},
        status_code=status,
    )


@router.post("/portal/market/buy")
async def portal_market_buy(request: Request):
    """Buy one CHOAM-exchange listing for the signed-in player. Delivery lands in
    their in-game Completed tab (exchange storage), funded from their CHOAM bank
    Solari; online+offline safe. The buyer's controller_id is resolved
    SERVER-SIDE from the session — never from the client. Returns a small HTML
    fragment for the drawer. Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def _int_field(name):
        raw = (form.get(name, "") or "").strip()
        if not raw.isdigit():
            return None
        val = int(raw)
        return val if val > 0 else None

    order_id = _int_field("order_id")
    revision = _int_field("revision")
    count = _int_field("count")
    if order_id is None or revision is None or count is None:
        return _buy_result_response(
            request, ok=False,
            message="That purchase request was malformed. Refresh and try again.",
            status=400)
    count = min(count, _BUY_MAX_COUNT)

    # Display-only item name (sanitized; never trusted for the write).
    tpl = (form.get("tpl", "") or "").strip()
    item_name = (_ITEM_NAMES.lookup_or_synthesize(tpl)
                 if tpl and _MARKET_TERM_RE.fullmatch(tpl) else "item")

    if not _buy_rate_ok(active_account_id):
        return _buy_result_response(
            request, ok=False,
            message="One purchase at a time, please. Wait a moment and retry.",
            status=429)

    buyer_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if buyer_ctrl is None:
        return _buy_result_response(
            request, ok=False,
            message="We could not verify your in-game character right now. "
                    "Please try again in a moment.",
            status=502)

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay(
            "/dune/market/buy", method="POST",
            json_body={"order_id": order_id, "revision": revision,
                       "count": count, "buyer_ctrl": buyer_ctrl},
            timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: market buy relay error acct=%s order=%s: %s",
                       active_account_id, order_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: market buy failed acct=%s order=%s: %s",
                       active_account_id, order_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_market_buy",
            str(order_id), ip,
            details=json.dumps({
                "account_id": active_account_id,
                "buyer_ctrl": buyer_ctrl,
                "order_id": order_id,
                "revision": revision,
                "count": count,
                "result": "ok" if ok else (err_token or "error"),
                "total_debited": (result or {}).get("total_debited") if isinstance(result, dict) else None,
            }),
            success=ok,
        )

    if ok:
        total = result.get("total_debited")
        total_disp = f"{total:,}" if isinstance(total, int) else "?"
        bought = result.get("count") or count
        bank_after = result.get("bank_after")
        bank_disp = f"{bank_after:,}" if isinstance(bank_after, int) else None
        msg = (f"Purchased {bought:,} × {item_name} for {total_disp} Solari. "
               "Collect it from the Completed tab at any exchange.")
        return _buy_result_response(request, ok=True, message=msg,
                                    bank_display=bank_disp)

    friendly = _BUY_ERROR_TEXT.get(err_token,
                                   "That purchase could not be completed. "
                                   "Please refresh and try again.")
    status = 409 if err_token in ("revision_drift", "order_gone",
                                  "count_exceeds_stack") else 200
    return _buy_result_response(request, ok=False, message=friendly, status=status)


# ----------------------------------------------- self-rescue teleport -------
#
# B-2b: a confirmed portal player who is stuck (under terrain, in a wreck, etc.)
# can teleport themselves to one of THEIR OWN base totems. Every control here is
# server-side and anti-exploit:
#   * Destination is server-CHOSEN: the nearest OWN base totem in the SAME
#     map/dim as the player, computed from the authoritative per-player map read.
#     Client coordinates are NEVER accepted — this is the single most important
#     control (no "teleport anywhere" primitive is exposed).
#   * Online-only (the publisher also refuses an offline --send).
#   * Durable 1/hour cooldown (SQLite portal_rescue_log), so it survives deploys.
#   * Blocked in Deep Desert entirely (see the DD note below).
# NOTE (combat gate): there is NO in-combat signal anywhere in the repo (no
# combat field in player-map, vitals, or progress), so this ships WITHOUT a
# combat check — accepted per supervisor. The 1/hour cooldown + own-base-only
# destination already remove most fight-escape exploit value (a player's base is
# usually far from a fight, and they get one rescue per hour).

_RESCUE_COOLDOWN_SECONDS = 3600          # 1 rescue / hour / account (durable)
_RESCUE_MIN_INTERVAL_SECONDS = 5.0       # in-process anti-double-submit guard
_last_rescue_at: dict = {}

# Deep Desert is blocked outright. The repo has TWO CONFLICTING and unverified
# PvE/PvP partition discriminators (dune_chat.py: dd_pvp=partition 8 / dd_pve=31,
# both verified:False; dune-containers.py: partition_id=31 => PvP). Because the
# DD-PvP partition cannot be confidently determined from the repo, we gate
# CONSERVATIVELY and block ALL Deep Desert rescue (the map value is "DeepDesert"
# from dune-player-map.py's a.map). Refine to PvP-only once the partition is pinned.
_RESCUE_BLOCKED_MAPS = {"DeepDesert"}


def _rescue_inproc_ok(account_id: int) -> bool:
    """Cheap in-process anti-double-submit (the durable 1/hour limit is the real
    gate; this just stops a double-click racing two relay sends)."""
    now = time.monotonic()
    last = _last_rescue_at.get(account_id)
    if last is not None and (now - last) < _RESCUE_MIN_INTERVAL_SECONDS:
        return False
    _last_rescue_at[account_id] = now
    return True


def _rescue_cooldown_remaining(account_id: int) -> int:
    """Seconds left on the durable 1/hour rescue cooldown, or 0 if clear.
    Windowed COUNT(*) against portal_rescue_log (mirrors portal_rate_limit)."""
    from datetime import timedelta
    window_start = (datetime.now(timezone.utc) - timedelta(seconds=_RESCUE_COOLDOWN_SECONDS)
                    ).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT MAX(used_at) AS last_used FROM portal_rescue_log
                WHERE account_id = ? AND used_at >= ?""",
            (account_id, window_start),
        ).fetchone()
    finally:
        conn.close()
    last_used = row["last_used"] if row else None
    if not last_used:
        return 0
    try:
        used_dt = datetime.strptime(last_used, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0
    elapsed = (datetime.now(timezone.utc) - used_dt).total_seconds()
    remaining = int(_RESCUE_COOLDOWN_SECONDS - elapsed)
    return max(remaining, 0)


def _record_rescue(account_id: int, self_pos: dict, base: dict) -> None:
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO portal_rescue_log
                 (account_id, used_at, from_x, from_y, from_map, to_base_x, to_base_y)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id,
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             self_pos.get("x"), self_pos.get("y"), self_pos.get("map"),
             base.get("x"), base.get("y")),
        )
        conn.commit()
    finally:
        conn.close()


def _choose_rescue_base(self_pos: dict, bases: list) -> Optional[dict]:
    """Server-chosen destination: the NEAREST own base totem in the SAME map AND
    dimension as the player, by squared 2D distance. Returns None if the player
    has no base totem in their current map/dim (caller rejects)."""
    sx, sy = self_pos.get("x"), self_pos.get("y")
    smap, sdim = self_pos.get("map"), self_pos.get("dim")
    if sx is None or sy is None or smap is None:
        return None
    best = None
    best_d2 = None
    for b in bases or []:
        if b.get("map") != smap or b.get("dim") != sdim:
            continue
        bx, by = b.get("x"), b.get("y")
        if bx is None or by is None:
            continue
        d2 = (bx - sx) ** 2 + (by - sy) ** 2
        if best_d2 is None or d2 < best_d2:
            best_d2 = d2
            best = b
    return best


async def _resolve_fls_for_account(account_id: int) -> Optional[str]:
    """account_id -> hex FLS id for the teleport payload (mirrors
    v2_moderation._resolve_fls_id). The grant picker carries funcom_id for every
    account. Returns None if absent — the caller MUST reject rather than guess."""
    try:
        from relay import call_relay
        payload = await call_relay("/dune/grant/players", timeout=30)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    target = str(account_id)
    for p in (payload.get("players") or []):
        if str(p.get("account_id", "")) == target:
            fls = p.get("funcom_id") or p.get("fls_id")
            if isinstance(fls, str) and fls.strip():
                return fls.strip()
            return None
    return None


def _rescue_result_response(request: Request, *, ok: bool, state: str, message: str,
                            cooldown_seconds: int = 0, status: int = 200):
    """Content negotiation: the V2 chrome fetches with Accept: application/json
    and gets {ok, state, message, cooldown_remaining_s?}; `state` is the SAME
    string the audit log records for the outcome (frozen contract with the V2
    RescueButton). Default (no JSON Accept) stays the V1 htmx HTML fragment,
    byte-identical, so the live V1 map page is untouched."""
    if "application/json" in (request.headers.get("accept") or ""):
        body = {"ok": ok, "state": state, "message": message}
        if cooldown_seconds:
            body["cooldown_remaining_s"] = cooldown_seconds
        return JSONResponse(body, status_code=status)
    return templates.TemplateResponse(
        request, "portal/_fragments/rescue_result.html",
        {"ok": ok, "message": message, "cooldown_seconds": cooldown_seconds},
        status_code=status,
    )


@router.post("/portal/rescue")
async def portal_rescue(request: Request):
    """Self-rescue 'I'm stuck' teleport for the signed-in player. Teleports them
    to their own nearest base totem in the same map/dim. Destination is chosen
    SERVER-SIDE from the authoritative per-player map read — client coords are
    never accepted. Online-only, durable 1/hour cooldown, blocked in Deep Desert.
    Auth: linked session + CSRF. Returns a small HTML fragment."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    ip = client_ip(request)

    def _audit(success: bool, result: str, extra: dict = None):
        from auth import audit_log
        details = {"account_id": active_account_id, "result": result}
        if extra:
            details.update(extra)
        audit_log(
            None, f"portal:{discord_id}", "portal_rescue",
            str(active_account_id), ip, details=json.dumps(details), success=success,
        )

    # In-process double-submit guard (the durable cooldown below is the real gate).
    if not _rescue_inproc_ok(active_account_id):
        _audit(False, "rate_limited")
        return _rescue_result_response(
            request, ok=False, state="rate_limited",
            message="One rescue at a time, please. Wait a moment and retry.",
            status=429)

    # Durable 1/hour cooldown (survives restarts).
    cooldown_left = _rescue_cooldown_remaining(active_account_id)
    if cooldown_left > 0:
        _audit(False, "cooldown", {"cooldown_seconds": cooldown_left})
        mins = (cooldown_left + 59) // 60
        return _rescue_result_response(
            request, ok=False, state="cooldown",
            message=f"Rescue is on cooldown. Try again in about {mins} minute"
                    f"{'s' if mins != 1 else ''}.",
            cooldown_seconds=cooldown_left, status=429)

    # Authoritative per-player map read: self position + own base totems.
    try:
        from relay import call_relay
        snapshot = await call_relay(f"/dune/player/{active_account_id}/map", timeout=30)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: rescue map read failed acct=%s: %s", active_account_id, exc)
        snapshot = None

    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        _audit(False, "map_unavailable")
        return _rescue_result_response(
            request, ok=False, state="map_unavailable",
            message="We could not read your in-game position right now. "
                    "Please try again in a moment.",
            status=502)

    self_pos = snapshot.get("self") or None
    bases = snapshot.get("bases") or []

    if not self_pos:
        _audit(False, "no_position")
        return _rescue_result_response(
            request, ok=False, state="no_position",
            message="We could not find your character's position. "
                    "Log in to the game, then try again.")

    # Online-only (the publisher also refuses an offline send).
    if not self_pos.get("online"):
        _audit(False, "offline")
        return _rescue_result_response(
            request, ok=False, state="offline",
            message="You need to be logged in to the game to be rescued.")

    # Block Deep Desert entirely (PvP partition not confidently determinable).
    if self_pos.get("map") in _RESCUE_BLOCKED_MAPS:
        _audit(False, "deep_desert_blocked", {"map": self_pos.get("map")})
        return _rescue_result_response(
            request, ok=False, state="deep_desert_blocked",
            message="Self-rescue is not available in the Deep Desert.")

    # Server-chosen destination: nearest own base totem in the same map/dim.
    base = _choose_rescue_base(self_pos, bases)
    if base is None:
        _audit(False, "no_destination", {"map": self_pos.get("map")})
        return _rescue_result_response(
            request, ok=False, state="no_destination",
            message="No safe destination found. You have no base in your current "
                    "region. Self-rescue can only send you to your own base.")

    # Resolve hex FLS; reject (never guess) if absent.
    fls_id = await _resolve_fls_for_account(active_account_id)
    if not fls_id:
        _audit(False, "fls_unresolved")
        return _rescue_result_response(
            request, ok=False, state="fls_unresolved",
            message="We could not verify your in-game character right now. "
                    "Please try again in a moment.",
            status=502)

    # base totems carry no z; send z=0 with exact:false so the engine snaps the
    # player onto safe ground at the base's x/y.
    bx, by = base.get("x"), base.get("y")
    result = None
    ok = False
    try:
        from relay import call_relay
        result = await call_relay(
            "/dune/server-command", "POST",
            {"verb": "teleport", "player_id": fls_id, "mode": "apply",
             "operator": f"portal:{discord_id}", "reason": "self-rescue",
             "args": {"x": float(bx), "y": float(by), "z": 0.0, "exact": False}},
            timeout=60)
        ok = isinstance(result, dict) and bool(result.get("success"))
    except HTTPException as exc:
        logger.warning("portal: rescue relay error acct=%s: %s", active_account_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: rescue failed acct=%s: %s", active_account_id, exc)

    if ok:
        _record_rescue(active_account_id, self_pos, base)
        _audit(True, "ok", {
            "from_x": self_pos.get("x"), "from_y": self_pos.get("y"),
            "map": self_pos.get("map"),
            "to_base_x": bx, "to_base_y": by,
            "base_kind": base.get("kind"),
        })
        base_label = base.get("name") or ("outpost" if base.get("kind") == "outpost" else "base")
        return _rescue_result_response(
            request, ok=True, state="ok",
            message=f"Rescued. You have been moved to your {base_label}.",
            cooldown_seconds=_RESCUE_COOLDOWN_SECONDS)

    detail = (result or {}).get("detail") if isinstance(result, dict) else None
    _audit(False, "send_failed", {"detail": (detail or "")[:200]})
    # A failed publish (master switch off, gone offline mid-flight, etc.) is
    # surfaced as a friendly message, not a raw error.
    return _rescue_result_response(
        request, ok=False, state="send_failed",
        message="Rescue could not be completed right now. If you just logged in or "
                "moved, wait a moment and try again.")


# ----------------------------------------------------- market SELL ----------
#
# A confirmed player lists an item from one of THEIR OWN persisted base
# containers (or CHOAM bank) on the exchange while OFFLINE — the game only lets
# you list from a live backpack/vehicle, so list-from-storage is our
# differentiator. The seller's controller_id is resolved SERVER-SIDE from the
# session (never the client), and the chosen item_id is verified to live in an
# inventory the seller owns via the SAME ownership-enforced path the container
# browser uses. The writer re-verifies a third time in-transaction.

_SELL_MIN_INTERVAL_SECONDS = 3.0
_SELL_MAX_COUNT = 100000          # hard ceiling; the writer also rejects count > stack
_SELL_MAX_PRICE = 10 ** 12        # sanity ceiling (the exchange wallet tops out far below)
_SELL_DURATIONS = (1, 3, 7, 14)
_SELL_MAX_ITEM_PAGES = 60         # bound the ownership scan (page_size 100 -> 6k items)
_last_sell_at: dict = {}


def _sell_rate_ok(account_id: int) -> bool:
    now = time.monotonic()
    last = _last_sell_at.get(account_id)
    if last is not None and (now - last) < _SELL_MIN_INTERVAL_SECONDS:
        return False
    _last_sell_at[account_id] = now
    return True


def _sell_fee(price: int, days: int) -> int:
    """CONFIRMED CHOAM exchange listing fee (2026-06-05, verified in-game across
    three prices x multiple durations): fee = round(0.01*price*(days+1)) + 20*days.
    Integer half-up (mirrors the writer's authoritative `(price*(days+1)+50)//100`)
    so the audit estimate never drifts 1 Solari from what the writer debits. Float
    round() would use banker's rounding at the .5 boundary. Display/audit only here."""
    return (price * (days + 1) + 50) // 100 + 20 * days


# Friendly text for the writer's error tokens (the writer/relay own these).
_SELL_ERROR_TEXT = {
    "backpack_source_disabled": "Listing from your backpack is paused right now. Move "
                                "the item to your CHOAM bank and list it from there.",
    "container_source_disabled": "Listing from a base container is paused. The server holds "
                                 "a container's contents in memory while your base is loaded, "
                                 "so the item could end up both in the box and on the Exchange. "
                                 "Move it to your CHOAM bank and list it from there.",
    "player_online": "Log out of the game first, then list — items list while you are offline.",
    "insufficient_bank": "Not enough banked Solari to cover the listing fee.",
    "not_owner": "That item is no longer in your storage. Refresh and try again.",
    "item_not_found": "That item is no longer in your storage. Refresh and try again.",
    "count_exceeds_stack": "That is more than the stack holds. Refresh and try again.",
    "slots_full": "Your exchange order slots are full. Cancel a listing in-game, then retry.",
    "category_unresolved": "This item can't be listed on the Exchange yet — nobody has "
                           "listed one before, so it has no Exchange category.",
    "expiration_unresolved": "The exchange is busy right now. Please try again in a moment.",
    "list_failed": "The exchange could not create that listing. Please try again.",
    "write_failed": "The exchange could not create that listing. Please try again.",
}


async def _verify_seller_owns_item(account_id: int, container_id: str, item_id: int):
    """SERVER-SIDE ownership + identity check for SELL. Re-fetches the named
    container's items through the SAME ownership-enforced path the container
    browser uses (`_load_container_items` returns error='not_owned' for a
    container the account does not own), then locates item_id within it. Returns
    the matched (decorated) item dict, or None to fail closed. The seller can
    never list another player's item: a container_id they don't own yields
    not_owned, and an item_id not in their owned container yields None."""
    item_key = str(item_id)
    page = 1
    seen = 0
    while page <= _SELL_MAX_ITEM_PAGES:
        items = await _load_container_items(account_id, container_id, page)
        if not items.get("available"):
            return None  # not_owned / relay_unavailable -> fail closed
        rows = items.get("items") or []
        for it in rows:
            if str(it.get("id")) == item_key:
                return it
        total = items.get("total_count") or 0
        page_size = items.get("page_size") or 100
        seen += len(rows)
        if not rows or seen >= total:
            break
        page += 1
    return None


def _sell_result_response(request: Request, *, ok: bool, message: str,
                          bank_display: str = None, status: int = 200):
    return templates.TemplateResponse(
        request, "portal/_fragments/market_sell_result.html",
        {"ok": ok, "message": message, "bank_display": bank_display},
        status_code=status,
    )


@router.post("/portal/market/sell")
async def portal_market_sell(request: Request):
    """List one of the signed-in player's storage items on the CHOAM exchange.
    OFFLINE-only (the writer hard-gates on online_status + grace). The seller's
    controller_id is resolved SERVER-SIDE from the session, and the chosen item is
    verified to live in a container the seller owns before dispatch. The listing
    fee is funded from the seller's CHOAM bank Solari. Returns a small HTML
    fragment for the storage drawer. Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def _int_field(name):
        raw = (form.get(name, "") or "").strip().replace(",", "")
        if not raw.isdigit():
            return None
        val = int(raw)
        return val if val > 0 else None

    container_id = (form.get("container_id", "") or "").strip()
    item_id = _int_field("item_id")
    count = _int_field("count")
    price = _int_field("price")
    duration_days = _int_field("duration_days")
    tpl = (form.get("tpl", "") or "").strip()

    if (not container_id.isdigit() or item_id is None or count is None
            or price is None or duration_days is None):
        return _sell_result_response(
            request, ok=False,
            message="That listing request was malformed. Refresh and try again.",
            status=400)
    if duration_days not in _SELL_DURATIONS:
        return _sell_result_response(
            request, ok=False, message="Pick a duration of 1, 3, 7 or 14 days.",
            status=400)
    if price >= _SELL_MAX_PRICE:
        return _sell_result_response(
            request, ok=False, message="That price is too high.", status=400)
    count = min(count, _SELL_MAX_COUNT)

    if not _sell_rate_ok(active_account_id):
        return _sell_result_response(
            request, ok=False,
            message="One listing at a time, please. Wait a moment and retry.",
            status=429)

    # SERVER-SIDE ownership + identity check (STOP-SHIP): the seller may only list
    # an item from an inventory THEY own. Resolve the named container through the
    # ownership-enforced path (a container_id the account does not own -> not_owned),
    # then confirm item_id is in it. The writer re-verifies independently.
    owned_item = await _verify_seller_owns_item(active_account_id, container_id, item_id)
    if owned_item is None:
        return _sell_result_response(
            request, ok=False,
            message="That item is no longer in your storage. Refresh and try again.",
            status=409)
    # Identity guard: the server-resolved item must match what the player picked
    # (a swapped/edited client payload fails closed).
    if tpl and (owned_item.get("template_id") or "") != tpl:
        return _sell_result_response(
            request, ok=False,
            message="That item changed. Refresh your storage and try again.",
            status=409)
    stack = owned_item.get("stack_size") or 1
    try:
        stack = int(stack)
    except (TypeError, ValueError):
        stack = 1
    if count > stack:
        return _sell_result_response(
            request, ok=False,
            message="That is more than the stack holds. Refresh and try again.",
            status=409)

    # Seller controller_id resolved SERVER-SIDE from the session — never the client.
    seller_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if seller_ctrl is None:
        return _sell_result_response(
            request, ok=False,
            message="We could not verify your in-game character right now. "
                    "Please try again in a moment.",
            status=502)

    item_name = (_ITEM_NAMES.lookup_or_synthesize(tpl)
                 if tpl and _MARKET_TERM_RE.fullmatch(tpl) else "item")
    fee = _sell_fee(price, duration_days)

    # Pass the SERVER-resolved template (authoritative, already matched against the
    # owned item above) so the writer can do its own swapped-item guard.
    server_tpl = owned_item.get("template_id") or ""
    expected_template = server_tpl if _MARKET_TERM_RE.fullmatch(server_tpl) else None

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        sell_body = {"seller_ctrl": seller_ctrl, "item_id": item_id,
                     "count": count, "price": price,
                     "duration_days": duration_days}
        if expected_template is not None:
            sell_body["expected_template"] = expected_template
        result = await call_relay(
            "/dune/market/sell", method="POST",
            json_body=sell_body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: market sell relay error acct=%s item=%s: %s",
                       active_account_id, item_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: market sell failed acct=%s item=%s: %s",
                       active_account_id, item_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_market_sell",
            str(item_id), ip,
            details=json.dumps({
                "account_id": active_account_id,
                "seller_ctrl": seller_ctrl,
                "container_id": container_id,
                "item_id": item_id,
                "count": count,
                "price": price,
                "duration_days": duration_days,
                "fee_estimate": fee,
                "result": "ok" if ok else (err_token or "error"),
                "fee_charged": (result or {}).get("fee") if isinstance(result, dict) else None,
            }),
            success=ok,
        )

    if ok:
        listed = result.get("count") or count
        charged = result.get("fee")
        fee_disp = f"{charged:,}" if isinstance(charged, int) else f"{fee:,}"
        unit_disp = f"{price:,}"
        bank_after = result.get("bank_after")
        bank_disp = f"{bank_after:,}" if isinstance(bank_after, int) else None
        msg = (f"Listed {listed:,} × {item_name} at {unit_disp} Solari each for "
               f"{duration_days} day{'' if duration_days == 1 else 's'}. "
               f"Fee {fee_disp} Solari debited from your bank.")
        return _sell_result_response(request, ok=True, message=msg,
                                     bank_display=bank_disp)

    friendly = _SELL_ERROR_TEXT.get(err_token,
                                    "That item could not be listed. "
                                    "Please refresh and try again.")
    status = 409 if err_token in ("not_owner", "item_not_found",
                                  "count_exceeds_stack") else 200
    return _sell_result_response(request, ok=False, message=friendly, status=status)


# --------------------------------------------------- my orders (read-only) --
#
# Mirrors the in-game CHOAM "My Orders" panel for the signed-in player: their
# active sell listings, their Completed tab (purchased awaiting Take / sold
# awaiting bank-claim / canceled), and recent realised-trade history. Read-only;
# all data is keyed by the player's controller_id, resolved SERVER-SIDE on
# lastsietch-dune from the session account_id, so the URL can't be walked to read
# another player's orders. (Cancel/Relist write actions land here in a follow-up.)

# completion_type -> (Completed-tab label, sub-line).
# 5 purchased / 4 sold / 3 canceled / 2 expired (order duration elapsed unsold;
# engine's dune_exchange_expire_orders returns the item for relist-or-reclaim).
# Anything unmapped falls back to a NEUTRAL "Closed" (never imply a sale).
_MY_ORDERS_COMPLETION = {
    5: ("Purchased", "Awaiting pickup in-game"),
    4: ("Sold", "Proceeds sent to your bank"),
    3: ("Canceled", "Awaiting pickup or relist"),
    2: ("Expired", "Unsold — relist or reclaim in-game"),
}
# completion_type -> short buy/sell-oriented label for the History list.
_MY_ORDERS_HISTORY = {5: "Bought", 4: "Sold", 3: "Canceled", 2: "Expired"}


def _shape_my_order(o: dict, *, completed: bool = False) -> dict:
    """Shape one order row for render: friendly name + icon + Solari displays.
    Carries order_id + revision so the future Cancel/Relist controls can pin the
    exact order the player saw."""
    tpl = o.get("template_id") or ""
    price = o.get("item_price") or 0
    qty = o.get("stack") or 1
    row = {
        "order_id": o.get("order_id"),
        "revision": o.get("revision"),
        "template_id": tpl,
        "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
        "icon": _icon_for(tpl),
        "price": price,
        "price_display": f"{price:,}",
        "qty": qty,
        "total_display": f"{price * qty:,}",
        "quality": o.get("quality_level") or 0,
    }
    if completed:
        ct = o.get("completion_type")
        label, sub = _MY_ORDERS_COMPLETION.get(ct, ("Closed", ""))
        row["completion_type"] = ct
        row["status_label"] = label
        row["status_sub"] = sub
    return row


async def _load_my_orders(account_id: int) -> dict:
    """Best-effort fetch + shape of the player's My Orders view (active /
    completed / history). Relay-backed with a short TTL cache; degrades to an
    error envelope so the page still renders."""
    try:
        from routers.dune import _cached_my_orders
        payload = await _cached_my_orders(str(account_id))
    except Exception as exc:
        logger.warning("portal: my-orders fetch failed for acct %s: %s", account_id, exc)
        return {"available": False, "error": True,
                "active": [], "completed": [], "history": []}
    if not isinstance(payload, dict) or not payload.get("available"):
        err = payload.get("error") if isinstance(payload, dict) else True
        return {"available": False, "error": err,
                "active": [], "completed": [], "history": []}

    active = [_shape_my_order(o) for o in (payload.get("active") or [])]
    completed = [_shape_my_order(o, completed=True) for o in (payload.get("completed") or [])]
    history = []
    for h in (payload.get("history") or []):
        tpl = h.get("template_id") or ""
        price = h.get("item_price") or 0
        qty = h.get("stack") or 1
        ct = h.get("completion_type")
        history.append({
            "template_id": tpl,
            "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
            "icon": _icon_for(tpl),
            "price_display": f"{price:,}",
            "qty": qty,
            "total_display": f"{price * qty:,}",
            "status_label": _MY_ORDERS_HISTORY.get(ct, "Traded"),
            "completion_type": ct,
            "bot_trade": bool(h.get("bot_trade")),
            "logged_at": h.get("logged_at"),
        })
    return {
        "available": True,
        "active": active, "completed": completed, "history": history,
        "active_count": len(active),
        "completed_count": len(completed),
        "history_count": len(history),
    }


@router.get("/portal/my-orders")
async def portal_my_orders(request: Request):
    """Public player page: the signed-in player's CHOAM My Orders — active sell
    listings, Completed tab, and recent buy/sell history. Read-only; scoped to
    session.aid (the player only ever sees their own orders)."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, row = gate

    _touch_last_session(active_account_id)

    orders = await _load_my_orders(active_account_id)
    ctx = {
        "active_character_name": row["character_name"],
        "orders": orders,
        "logout_post_url": "/portal/logout",
    }
    ctx.update(base_ctx(request, discord_handle=row["discord_handle"]))
    return templates.TemplateResponse(request, "portal/my_orders.html", ctx)


# Cancel/Relist write actions on the My Orders page. Both engine procs are
# ONLINE-SAFE (they only mutate exchange tables + escrow inv 610, never live RAM
# inventory), so there is NO offline gate. owner_ctrl is resolved SERVER-SIDE; the
# lastsietch-dune writer re-verifies ownership + revision in-transaction.
_ORDERS_MIN_INTERVAL_SECONDS = 3.0
_last_order_action_at: dict = {}


def _orders_rate_ok(account_id: int) -> bool:
    now = time.monotonic()
    last = _last_order_action_at.get(account_id)
    if last is not None and (now - last) < _ORDERS_MIN_INTERVAL_SECONDS:
        return False
    _last_order_action_at[account_id] = now
    return True


_ORDERS_ERROR_TEXT = {
    "order_gone": "That order is no longer available. Refresh and try again.",
    "not_owner": "That order isn't yours.",
    "revision_drift": "That order just changed. Refresh and try again.",
    "not_active": "That listing is no longer active.",
    "not_canceled": "Only a canceled order can be relisted.",
    "insufficient_bank": "Not enough banked Solari for the relist fee.",
    "clock_unresolved": "The exchange is busy right now. Please try again shortly.",
    "cancel_failed": "The exchange could not cancel that order. Refresh and try again.",
    "relist_failed": "The exchange could not relist that order. Refresh and try again.",
}


def _orders_result_response(request: Request, *, ok: bool, message: str,
                            bank_display: str = None, status: int = 200):
    return templates.TemplateResponse(
        request, "portal/_fragments/market_order_result.html",
        {"ok": ok, "message": message, "bank_display": bank_display},
        status_code=status,
    )


async def _orders_write(request: Request, *, action: str):
    """Shared CANCEL/RELIST handler. Gate + CSRF + rate-limit, resolve owner_ctrl
    server-side, dispatch to the relay, audit, and return a small HTML fragment.
    Auth: linked session + CSRF."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def _int_field(name):
        raw = (form.get(name, "") or "").strip().replace(",", "")
        if not raw.isdigit():
            return None
        val = int(raw)
        return val if val > 0 else None

    order_id = _int_field("order_id")
    revision = _int_field("revision")
    price = duration_days = None
    if action == "relist":
        price = _int_field("price")
        duration_days = _int_field("duration_days")

    if order_id is None or revision is None or (
            action == "relist" and (price is None or duration_days is None)):
        return _orders_result_response(
            request, ok=False,
            message="That request was malformed. Refresh and try again.", status=400)
    if action == "relist":
        if duration_days not in _SELL_DURATIONS:
            return _orders_result_response(
                request, ok=False, message="Pick a duration of 1, 3, 7 or 14 days.",
                status=400)
        if price >= _MARKET_MAX_PRICE:
            return _orders_result_response(
                request, ok=False, message="That price is too high.", status=400)

    if not _orders_rate_ok(active_account_id):
        return _orders_result_response(
            request, ok=False,
            message="One action at a time, please. Wait a moment and retry.", status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _orders_result_response(
            request, ok=False,
            message="We could not verify your in-game character right now. "
                    "Please try again in a moment.", status=502)

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        body = {"owner_ctrl": owner_ctrl, "order_id": order_id, "revision": revision}
        if action == "relist":
            body["price"] = price
            body["duration_days"] = duration_days
        result = await call_relay(
            f"/dune/market/{action}", method="POST", json_body=body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: market %s relay error acct=%s order=%s: %s",
                       action, active_account_id, order_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: market %s failed acct=%s order=%s: %s",
                       action, active_account_id, order_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", f"portal_market_{action}",
            str(order_id), ip,
            details=json.dumps({
                "account_id": active_account_id,
                "owner_ctrl": owner_ctrl,
                "order_id": order_id,
                "revision": revision,
                "price": price,
                "duration_days": duration_days,
                "result": "ok" if ok else (err_token or "error"),
                "fee_charged": (result or {}).get("fee") if isinstance(result, dict) else None,
            }),
            success=ok,
        )

    if ok:
        # The write just changed this player's orders; drop the read cache so the
        # page reload after the action shows fresh state immediately rather than the
        # stale 20s-TTL snapshot.
        try:
            from cache import invalidate
            invalidate("dune.player_my_orders", str(active_account_id))
        except Exception:  # noqa: BLE001 - cache drop is best-effort
            pass
        if action == "cancel":
            msg = ("Listing canceled. The item is now in your in-game Completed tab "
                   "(the listing fee is forfeited). You can relist it from here.")
            return _orders_result_response(request, ok=True, message=msg)
        charged = result.get("fee")
        fee_disp = f"{charged:,}" if isinstance(charged, int) else "the listing"
        bank_after = result.get("bank_after")
        bank_disp = f"{bank_after:,}" if isinstance(bank_after, int) else None
        stack = result.get("stack")
        stack_disp = f"{stack:,} × " if isinstance(stack, int) else ""
        msg = (f"Relisted {stack_disp}at {price:,} Solari each for "
               f"{duration_days} day{'' if duration_days == 1 else 's'}. "
               f"Fee {fee_disp} Solari debited from your bank.")
        return _orders_result_response(request, ok=True, message=msg, bank_display=bank_disp)

    friendly = _ORDERS_ERROR_TEXT.get(err_token,
                                      "That action could not be completed. "
                                      "Please refresh and try again.")
    status = 409 if err_token in ("order_gone", "not_owner", "revision_drift",
                                  "not_active", "not_canceled") else 200
    return _orders_result_response(request, ok=False, message=friendly, status=status)


@router.post("/portal/my-orders/cancel")
async def portal_my_orders_cancel(request: Request):
    """Cancel one of the signed-in player's ACTIVE CHOAM listings (forfeits the fee,
    item -> Completed tab). Online-safe; no offline gate. Auth: linked session + CSRF."""
    return await _orders_write(request, action="cancel")


@router.post("/portal/my-orders/relist")
async def portal_my_orders_relist(request: Request):
    """Relist one of the signed-in player's CANCELED orders from the Completed tab at
    a new price/duration (relist fee funded from bank Solari). Online-safe. Auth:
    linked session + CSRF."""
    return await _orders_write(request, action="relist")


# ------------------------------------------------ market watchlist (alerts) -
#
# A confirmed player sets "notify me when <item> drops to <= <price>". The
# background watcher (market_watch.py) diffs the local market mirror and fires a
# portal-bell + Cielago-DM alert when the cheapest listing crosses the threshold.
# Read-only on the exchange: no buy, no sell. Watches live in admin.db keyed on
# the session account_id, so the URL can't be walked to read another player's list.

_MARKET_MAX_PRICE = 10 ** 12  # sanity ceiling (the exchange wallet tops out far below)


def _parse_price(raw: str):
    """Parse a player-typed price ('120', '1,500', ' 2000 ') to a positive int,
    or None if it is not a sane price."""
    digits = (raw or "").replace(",", "").strip()
    if not digits.isdigit():
        return None
    val = int(digits)
    if val <= 0 or val >= _MARKET_MAX_PRICE:
        return None
    return val


@router.get("/portal/watchlist")
async def portal_watchlist(request: Request):
    """The watchlist folded into the Market page (the hub). This legacy path 301s
    to /portal/market#watches so old links + the nav still land on the watches."""
    return RedirectResponse(url="/portal/market#watches", status_code=301)


@router.post("/portal/watchlist/add")
async def portal_watchlist_add(request: Request):
    """Create/update a price-alert watch for one item. Accepts a form body
    (template_id + max_price + csrf_token); CSRF via header or body. Returns JSON
    for the fetch path (Accept: application/json) or 302 back to the page for the
    no-JS form path."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    wants_json = "application/json" in (request.headers.get("accept", "") or "")
    template_id = (form.get("template_id", "") or "").strip()
    max_price = _parse_price(form.get("max_price", "") or "")

    def _fail(msg: str, status: int = 400):
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=status)
        return RedirectResponse(url="/portal/watchlist?err=1", status_code=302)

    if not template_id or not _MARKET_TERM_RE.fullmatch(template_id):
        return _fail("That item could not be identified.")
    if max_price is None:
        return _fail("Enter a whole number price above 0.")

    name = _ITEM_NAMES.lookup_or_synthesize(template_id)
    result = market_watch.add_watch(
        active_account_id, discord_id, template_id, name, max_price)
    if not result.get("ok"):
        return _fail(result.get("error") or "Could not add that watch.",
                     status=409 if result.get("at_cap") else 400)

    if wants_json:
        return JSONResponse({"ok": True, "name": name, "max_price": max_price,
                             "max_price_display": f"{max_price:,}"})
    return RedirectResponse(url="/portal/watchlist?added=1", status_code=302)


@router.post("/portal/watchlist/remove")
async def portal_watchlist_remove(request: Request):
    """Delete one of the signed-in player's watches (ownership enforced by
    account_id). Form body: watch_id + csrf_token. 302 back to the page."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    watch_id_raw = (form.get("watch_id", "") or "").strip()
    wants_json = "application/json" in (request.headers.get("accept", "") or "")
    if watch_id_raw.isdigit():
        market_watch.remove_watch(active_account_id, int(watch_id_raw))
    if wants_json:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/portal/watchlist", status_code=302)


@router.get("/portal/market/watches")
async def portal_market_watches(request: Request):
    """Re-render just the Market page's price-alert card (the folded-in watchlist),
    so adding/removing a watch from the drawer can refresh it without a full page
    reload. Returns the market_watches.html fragment for the session account."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    watches = market_watch.list_watches(active_account_id)
    alerts = market_watch.list_alerts(active_account_id, limit=6)
    for w in watches:
        w["icon"] = _icon_for(w["template_id"])
    for a in alerts:
        a["icon"] = _icon_for(a["template_id"])
    session_token = request.cookies.get(SESSION_COOKIE, "")
    return templates.TemplateResponse(
        request, "portal/_fragments/market_watches.html",
        {"watches": watches, "watch_count": len(watches),
         "watch_cap": config.MARKET_WATCH_MAX_PER_ACCOUNT, "alerts": alerts,
         "portal_csrf_token": csrf_for_session(session_token) if session_token else ""},
    )


@router.get("/portal/market/item-search")
async def portal_market_item_search(request: Request, q: str = ""):
    """Lightweight JSON item picker for the price-alert "add a watch" search box:
    distinct exchange items whose friendly name (or template fragment) matches the
    query, with icon + cheapest current price (so the watch price can default near
    the market). Read-only over the local market mirror; scoped behind the session
    gate but global market data (not per-account)."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    needle = (q or "").strip().lower()
    if len(needle) < 2:
        return JSONResponse({"items": []})
    summary = mirror.market_summary()
    out = []
    if summary:
        for r in summary:
            tpl = r["template_id"]
            name = _ITEM_NAMES.lookup_or_synthesize(tpl)
            if needle not in name.lower() and needle not in tpl.lower():
                continue
            lo = r["min_price"]
            out.append({
                "template_id": tpl,
                "name": name,
                "icon": _icon_for(tpl),
                "is_schematic": market_categories.is_schematic(tpl),
                "cheapest": lo if lo is not None else None,
                "cheapest_display": f"{lo:,}" if lo is not None else None,
            })
    out.sort(key=lambda x: x["name"].lower())
    return JSONResponse({"items": out[:12]})


@router.get("/portal/market/rare-recent")
async def portal_market_rare_recent(request: Request, after: str = "", limit: int = 50):
    """Recently-listed rare-rotation items for the Cielago market announcer
    (dune.ls_rare_rotation). Optional `after` cursor (ISO8601 timestamptz)
    returns only rows newer than it; `limit` caps the page (default 50). Global
    market data, read-only via the cached relay path; exposed as a plain JSON GET
    for the announcer cog. Shape: {"rows":[...]}. NOTE: the Cielago cog may also
    read the relay's /dune/market/rare-recent directly (same data) per the
    digest-migration pattern -- this endpoint is the admin-backend-cached
    convenience surface."""
    after = (after or "").strip()
    if after and not re.fullmatch(r"[0-9TZ:+.\-]{1,40}", after):
        raise HTTPException(400, "after must be an ISO8601 UTC timestamp")
    if not (1 <= limit <= 500):
        raise HTTPException(400, "limit must be 1-500")
    try:
        from routers.dune import cached_market_rare_recent
        data = await cached_market_rare_recent(after, limit)
    except Exception as exc:
        logger.warning("market rare-recent fetch failed: %s", exc)
        return JSONResponse({"rows": [], "available": False})
    return JSONResponse(data if isinstance(data, (dict, list)) else {"rows": []})


# ============================================================================ #
# V2 Exchange Module (SvelteKit portal-nextgen): NEW sibling JSON endpoints.
#
# Governing principle (zero-regression): the V1 HTML market routes above stay
# BYTE-IDENTICAL and live. These V2 handlers sit in front of the SAME relay /
# writer / dispatcher chain, REUSING the existing dict-returning shaper +
# validation functions (`_load_market_browse`, `_decorate_market_summary`,
# `_merge_bot_buyable`, `_bot_buy_base_cap`, `_bot_buy_for`, `_load_market_item`,
# `_load_my_orders`, `_verify_seller_owns_item`, `_sell_fee`, the `_SELL_*` /
# `_BUY_*` / `_ORDERS_*` constants + error-text maps) rather than re-deriving any
# market mechanic. They return the `_v2_ok` / `_v2_err` envelope and read JSON
# bodies via `_v2_body_and_csrf` (NEVER FastAPI Form). Identity (buyer/seller
# controller_id) is ALWAYS resolved SERVER-SIDE from the session.
# ============================================================================ #


def _v2_pos_int(body: dict, name: str):
    """Positive int from a JSON body field (accepts stringified ints, strips
    thousands separators), or None."""
    raw = str(body.get(name, "") or "").strip().replace(",", "")
    if not raw.isdigit():
        return None
    val = int(raw)
    return val if val > 0 else None


@router.get("/portal/market/v2")
async def portal_market_v2(request: Request):
    """V2 Exchange overview: bank Solari, the player's price-alert watches +
    unseen-alert count, the category tabs, and a CSRF token. Read-only. Reuses
    `_resolve_buyer_ctrl_and_bank` (bank), `market_watch.list_watches/
    unseen_alert_count`, `market_categories.CATEGORIES`. Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    _touch_last_session(active_account_id)

    _ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))

    watches = market_watch.list_watches(active_account_id)
    for w in watches:
        w["icon"] = _icon_for(w["template_id"])
    alerts = market_watch.list_alerts(active_account_id, limit=6)
    for a in alerts:
        a["icon"] = _icon_for(a["template_id"])

    session_token = request.cookies.get(SESSION_COOKIE, "")
    return _v2_ok({
        "bank_solari": bank_solari,
        "watches": watches,
        "alerts": alerts,
        "alert_count": market_watch.unseen_alert_count(active_account_id),
        "watch_cap": config.MARKET_WATCH_MAX_PER_ACCOUNT,
        "tabs": market_categories.CATEGORIES,
        "csrf_token": csrf_for_session(session_token) if session_token else "",
    })


@router.get("/portal/market/v2/alerts/count")
async def portal_market_v2_alerts_count(request: Request):
    """Unseen fired-alert count for the topbar badge. admin.db ONLY -- deliberately
    NOT the full overview, which resolves the buyer controller and bank Solari
    through the relay against the live game DB. The badge is painted on every hard
    load of every portal page, so it must cost one local COUNT(*), the same as the
    mailbox bell. Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    return _v2_ok({"alert_count": market_watch.unseen_alert_count(active_account_id)})


@router.post("/portal/market/v2/alerts/seen")
async def portal_market_v2_alerts_seen(request: Request):
    """Mark the NAMED fired alerts seen for the SESSION account. Body is
    {ids:[int,...]} -- the ids the feed actually rendered. Scoped on purpose: the
    feed shows a newest-first slice, so an unbounded clear marks alerts seen that
    were never on the page, and the row the player never saw is exactly the one
    the badge exists for. A separate CSRF-gated write rather than a side effect of
    the polled overview read, for the same reason. The account comes from the
    session; any account_id in the body is ignored. Returns the fresh unseen count
    (NOT a hard 0 -- alerts outside the rendered slice are still unseen).
    Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Your session expired. Reload and try again.", status=403)

    raw_ids = body.get("ids")
    ids = raw_ids if isinstance(raw_ids, list) else []

    try:
        cleared = market_watch.mark_alerts_seen(active_account_id, ids)
        remaining = market_watch.unseen_alert_count(active_account_id)
    except Exception:  # noqa: BLE001
        logger.warning("portal: market alerts mark-seen failed acct=%s",
                       active_account_id, exc_info=True)
        return _v2_err("unavailable", "Could not clear the alerts right now.",
                       status=503)
    return _v2_ok({"cleared": cleared, "alert_count": remaining})


@router.get("/portal/market/v2/bot-limits")
async def portal_market_v2_bot_limits(request: Request):
    """V2 Exchange bot-budget tracker: per-category weekly buy/sell cap + units
    used + reset timestamp (from the mirror, written by lastsietch-market-bot each tick).
    Item-override scopes get their display name + icon. Read-only; linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    _touch_last_session(active_account_id)
    snap = mirror.bot_limits_snapshot()
    if not snap:
        return _v2_ok({"available": False, "scopes": []})
    for s in snap.get("scopes", []):
        if s.get("is_item"):
            nm = _ITEM_NAMES.lookup_or_synthesize(s.get("scope", ""))
            if nm:
                s["label"] = nm
            s["icon"] = _icon_for(s.get("scope", ""))
    return _v2_ok({"available": True, **snap})


@router.get("/portal/market/v2/search")
async def portal_market_v2_search(request: Request, q: str = "", sort: str = "active",
                                  category: str = "all", page: int = 1, kind: str = "all"):
    """V2 browse/search (JSON of `_load_market_browse` -> `_decorate_market_summary`
    + `_merge_bot_buyable`). Same mechanics as the V1 `/portal/market/search`
    fragment; only the render differs. Read-only global market data."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early

    sort = sort if sort in ("active", "price", "expensive", "name") else "active"
    category = category if market_categories.is_valid(category) else "all"
    kind = kind if kind in ("all", "item", "schematic") else "all"
    try:
        page = max(int(page), 1)
    except (TypeError, ValueError):
        page = 1

    results = await _load_market_browse((q or "").strip(), sort, category, page, kind)
    bots = mirror.bot_prices_all()
    rows = []
    for it in (results.get("items") or []):
        tpl = it["template_id"]
        lo = it.get("_min_price")
        hi = it.get("_max_price")
        bot_caps = None
        if bots:
            bb = bots.get(tpl.lower())
            if bb and bb.get("buyable"):
                bot_caps = bb.get("caps")
        rows.append({
            "template_id": tpl,
            "name": it.get("name"),
            "icon": it.get("icon"),
            "is_schematic": it.get("is_schematic"),
            "listing_count": it.get("listing_count"),
            "total_qty": it.get("total_qty"),
            # invert the decorate sentinels (1<<62 for absent min, 0 for absent max)
            "min_price": None if lo is None or lo >= (1 << 62) else lo,
            "max_price": None if not hi else hi,
            "has_npc": it.get("has_npc"),
            "has_player": it.get("has_player"),
            "max_quality": it.get("max_quality") or 0,
            "bot_buy_display": it.get("bot_buy_display"),
            "bot_caps": bot_caps,
        })
    return _v2_ok({
        "rows": rows,
        "page": results.get("page") or page,
        "more": bool(results.get("has_more")),
        "sort": sort, "category": category, "kind": kind,
        "browse_unavailable": bool(results.get("browse_unavailable")),
    })


@router.get("/portal/market/v2/item")
async def portal_market_v2_item(request: Request, tpl: str = ""):
    """V2 price ladder for one item (JSON of `_load_market_item` + `_bot_buy_for`).
    Also surfaces the player's bank Solari for the Buy panel (display only; the
    writer re-checks the bank). Read-only. Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate

    detail = await _load_market_item((tpl or "").strip())
    if not detail.get("available"):
        if detail.get("error") == "invalid":
            return _v2_err("bad_request", "That item could not be identified.", status=400)
        return _v2_err("unavailable", "That item is unavailable right now.", status=503)

    _ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    ladder = [{
        "order_id": r.get("order_id"),
        "revision": r.get("revision"),
        "price": r.get("price"),
        "qty": r.get("qty"),
        "quality": r.get("quality"),
        "is_npc": r.get("is_npc"),
        "buyable": r.get("buyable"),
    } for r in (detail.get("listings") or [])]
    bot_tiers = [{"grade": t.get("grade"), "cap": t.get("cap")}
                 for t in ((detail.get("bot_buy") or {}).get("tiers") or [])]
    return _v2_ok({
        "template_id": detail.get("template_id"),
        "name": detail.get("name"),
        "icon": detail.get("icon"),
        "is_schematic": detail.get("is_schematic"),
        "ladder": ladder,
        "bot_tiers": bot_tiers,
        "bank_solari": bank_solari,
    })


@router.get("/portal/market/v2/history")
async def portal_market_v2_history(request: Request, tpl: str = ""):
    """V2 spice-vein price sparkline for one item (NEW; from the tiered
    market_history.db beside admin.db). Returns {points:[{t, min_price, median_price, listing_count}],
    low_7d, calibrating}. Empty series -> calibrating:true. Read-only."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    tpl = (tpl or "").strip()
    if not tpl or not _MARKET_TERM_RE.fullmatch(tpl):
        return _v2_err("bad_request", "That item could not be identified.", status=400)
    return _v2_ok(market_history.history(tpl))


@router.get("/portal/market/v2/my-orders")
async def portal_market_v2_my_orders(request: Request):
    """V2 My Orders (JSON of `_load_my_orders`): active listings, Completed tab,
    recent history. Scoped to session.aid (controller_id resolved server-side on
    lastsietch-dune). NO expires_at exists, so active rows carry no countdown. Read-only."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, _ = gate
    _touch_last_session(active_account_id)

    orders = await _load_my_orders(active_account_id)
    return _v2_ok({
        "active": orders.get("active") or [],
        "completed": orders.get("completed") or [],
        "history": orders.get("history") or [],
        "available": bool(orders.get("available")),
    })


@router.get("/portal/market/v2/flips")
async def portal_market_v2_flips(request: Request):
    """V2 Bot-Floor Flip Board: items whose cheapest PLAYER ask sits BELOW the bot's
    buy cap (buy the player listing low, sell it to the bot at the cap). Derived
    SERVER-SIDE from the PLAYER-only ask floor + bot caps (reuses
    `mirror.market_player_floors` + `mirror.bot_prices_all`). Read-only.

    `min_ask` is the cheapest non-NPC listing (NPC sell orders are not a flip source,
    so they are excluded); the spread is a true player-to-bot arbitrage."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early

    floors = mirror.market_player_floors()
    bots = mirror.bot_prices_all()
    rows = []
    if floors and bots:
        # 🔴 Compare like with like. The ask floor is per (template, GRADE) and the bot's
        # cap is per grade, so each floor must be measured against the cap for ITS OWN
        # grade. The old code took min(caps) and then published that cap's grade as the
        # row's grade, so a grade-4 ask was scored against the grade-0 cap and rendered
        # labelled "G0" - advertising a flip that could never settle (ticket #130).
        # A grade the bot publishes no cap for has no buyer, so it is not a flip at all.
        for tpl_lower, by_grade in floors.items():
            if not isinstance(by_grade, dict):
                continue
            bb = bots.get(tpl_lower)
            if not bb or not bb.get("buyable") or not bb.get("caps"):
                continue
            caps = bb["caps"]
            for grade, lo in by_grade.items():
                if lo is None:
                    continue
                raw_cap = caps.get(str(grade), caps.get(grade))
                if raw_cap is None:
                    continue          # no cap at this grade -> no buyer -> not a flip
                try:
                    cap = int(raw_cap)
                except (TypeError, ValueError):
                    continue
                if lo >= cap:
                    continue
                tpl = bb.get("template_id") or tpl_lower
                rows.append({
                    "template_id": tpl,
                    "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
                    "icon": _icon_for(tpl),
                    "min_ask": lo,
                    "bot_cap": cap,
                    "grade": int(grade),
                    "spread": cap - lo,
                })
    rows.sort(key=lambda x: -x["spread"])
    return _v2_ok({"rows": rows[:_MARKET_PAGE_SIZE]})


@router.post("/portal/market/v2/buy")
async def portal_market_v2_buy(request: Request):
    """V2 BUY one CHOAM listing. JSON body {order_id, revision, count, uuid?}.
    ONLINE-SAFE (never touches live inventory). buyer_ctrl resolved SERVER-SIDE.
    Reuses the SAME `/dune/market/buy` relay/writer chain + `_buy_rate_ok` +
    `_BUY_ERROR_TEXT` as V1. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    order_id = _v2_pos_int(body, "order_id")
    revision = _v2_pos_int(body, "revision")
    count = _v2_pos_int(body, "count")
    uuid = str(body.get("uuid", "") or "")[:64]
    if order_id is None or revision is None or count is None:
        return _v2_err("bad_request",
                       "That purchase request was malformed. Refresh and try again.",
                       status=400)
    count = min(count, _BUY_MAX_COUNT)

    if not _buy_rate_ok(active_account_id):
        return _v2_err("rate_limited",
                       "One purchase at a time. Wait a moment and retry.", status=429)

    buyer_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if buyer_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay(
            "/dune/market/buy", method="POST",
            json_body={"order_id": order_id, "revision": revision,
                       "count": count, "buyer_ctrl": buyer_ctrl},
            timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: v2 market buy relay error acct=%s order=%s: %s",
                       active_account_id, order_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: v2 market buy failed acct=%s order=%s: %s",
                       active_account_id, order_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_market_buy", str(order_id), ip,
            details=json.dumps({
                "account_id": active_account_id, "buyer_ctrl": buyer_ctrl,
                "order_id": order_id, "revision": revision, "count": count,
                "uuid": uuid, "surface": "v2",
                "result": "ok" if ok else (err_token or "error"),
                "total_debited": (result or {}).get("total_debited") if isinstance(result, dict) else None,
            }),
            success=ok)

    if ok:
        return _v2_ok({
            "bank_after": (result or {}).get("bank_after"),
            "delivered": (result or {}).get("count") or count,
            "total_debited": (result or {}).get("total_debited"),
        })
    friendly = _BUY_ERROR_TEXT.get(
        err_token, "That purchase could not be completed. Please refresh and try again.")
    status = 409 if err_token in ("revision_drift", "order_gone",
                                  "count_exceeds_stack") else 400
    return _v2_err(err_token or "write_failed", friendly, status=status)


# V2 SELL idempotency guard (admin.db). SELL is a create with no natural key, so a
# relay-timeout retry of a PARTIAL-stack list could double-list + double-charge the
# fee (BUY/cancel/relist are already safe via revision-pinning). We persist the
# terminal relay outcome under (account_id, uuid) for a short window; a retry with
# the SAME uuid replays the stored envelope verbatim instead of executing again.
_SELL_IDEM_TTL_SECONDS = 120
_SELL_IDEM_PRUNE_SECONDS = _SELL_IDEM_TTL_SECONDS * 3
_SELL_IDEM_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS market_v2_sell_idem ("
    "account_id TEXT NOT NULL, uuid TEXT NOT NULL, result_json TEXT, "
    "created_at TEXT NOT NULL, PRIMARY KEY(account_id, uuid))")


def _sell_idem_lookup(account_id, uuid: str):
    """Stored terminal envelope {payload, status} for a recent (account, uuid) sell,
    or None. Prunes rows older than _SELL_IDEM_PRUNE_SECONDS opportunistically.
    Best-effort: a guard-store failure never blocks a legitimate sell."""
    if not uuid:
        return None
    try:
        conn = get_db()
        try:
            conn.execute(_SELL_IDEM_SCHEMA)
            now = datetime.now(timezone.utc)
            conn.execute("DELETE FROM market_v2_sell_idem WHERE created_at < ?",
                         ((now - timedelta(seconds=_SELL_IDEM_PRUNE_SECONDS)).isoformat(),))
            cutoff = (now - timedelta(seconds=_SELL_IDEM_TTL_SECONDS)).isoformat()
            row = conn.execute(
                "SELECT result_json FROM market_v2_sell_idem "
                "WHERE account_id=? AND uuid=? AND created_at >= ?",
                (str(account_id), uuid, cutoff)).fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: v2 sell idem lookup failed acct=%s: %s", account_id, exc)
        return None
    if not row or not row["result_json"]:
        return None
    try:
        return json.loads(row["result_json"])
    except Exception:  # noqa: BLE001
        return None


def _sell_idem_store(account_id, uuid: str, payload: dict, status: int) -> None:
    """Persist the terminal relay outcome so a same-uuid retry replays it verbatim."""
    if not uuid:
        return
    try:
        conn = get_db()
        try:
            conn.execute(_SELL_IDEM_SCHEMA)
            conn.execute(
                "INSERT INTO market_v2_sell_idem(account_id, uuid, result_json, created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(account_id, uuid) DO UPDATE SET "
                "result_json=excluded.result_json, created_at=excluded.created_at",
                (str(account_id), uuid,
                 json.dumps({"payload": payload, "status": status}),
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: v2 sell idem store failed acct=%s: %s", account_id, exc)


@router.post("/portal/market/v2/sell")
async def portal_market_v2_sell(request: Request):
    """V2 SELL one owned storage item on the CHOAM exchange. JSON body
    {container_id, item_id, count, price, duration_days, tpl?, uuid?}.
    OFFLINE-GATED (stop-ship): a player known to be ONLINE is refused with
    `player_online` BEFORE any relay/DB touch (undetermined status falls through
    to the writer, which hard-gates authoritatively, account-wide). The SOURCE is
    resolved here too: the container_id must match the selected character's own bank
    or backpack, and which one it is travels to the writer as `expected_src`.
    seller_ctrl resolved SERVER-SIDE; ownership re-verified via
    `_verify_seller_owns_item`; fee via
    `_sell_fee`; the writer funds the fee with exactly one bank debit, resolves
    the category mask fail-closed, and derives game-time expiration. Reuses the
    SAME `/dune/market/sell` chain + `_SELL_*` constants + `_SELL_ERROR_TEXT` as
    V1. The client `uuid` is a persistent idempotency key: a same-uuid retry within
    ~120s replays the stored outcome, so a relay-timeout retry cannot double-list or
    double-charge the fee. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    container_id = str(body.get("container_id", "") or "").strip()
    item_id = _v2_pos_int(body, "item_id")
    count = _v2_pos_int(body, "count")
    price = _v2_pos_int(body, "price")
    duration_days = _v2_pos_int(body, "duration_days")
    tpl = str(body.get("tpl", "") or "").strip()
    uuid = str(body.get("uuid", "") or "")[:64]

    if (not container_id.isdigit() or item_id is None or count is None
            or price is None or duration_days is None):
        return _v2_err("bad_request",
                       "That listing request was malformed. Refresh and try again.",
                       status=400)
    if duration_days not in _SELL_DURATIONS:
        return _v2_err("bad_duration", "Pick a duration of 1, 3, 7 or 14 days.", status=400)
    if price >= _SELL_MAX_PRICE:
        return _v2_err("bad_price", "That price is too high.", status=400)
    count = min(count, _SELL_MAX_COUNT)

    # IDEMPOTENCY (stop-ship on double-charge): a same-uuid retry within the window
    # replays the stored terminal relay outcome verbatim instead of re-executing the
    # create (a partial-stack list has no natural key to dedupe on).
    replay = _sell_idem_lookup(active_account_id, uuid)
    if replay is not None:
        return JSONResponse(replay.get("payload") or {},
                            status_code=replay.get("status") or 200)

    # OFFLINE-GATE (stop-ship): a definitely-online player is refused here with NO
    # relay/DB touch. Undetermined status is NOT rejected at the edge; the writer
    # is the authoritative fail-closed gate on online_status + grace.
    if await _resolve_online(active_account_id) is True:
        return _v2_err("player_online", _SELL_ERROR_TEXT["player_online"], status=409)

    if not _sell_rate_ok(active_account_id):
        return _v2_err("rate_limited",
                       "One listing at a time. Wait a moment and retry.", status=429)

    # SERVER-SIDE ownership + identity re-check: the seller may only list an item
    # from a container THEY own (a container_id they don't own -> not_owned).
    owned_item = await _verify_seller_owns_item(active_account_id, container_id, item_id)
    if owned_item is None:
        return _v2_err("not_owner",
                       "That item is no longer in your storage. Refresh and try again.",
                       status=409)
    if tpl and (owned_item.get("template_id") or "") != tpl:
        return _v2_err("item_changed",
                       "That item changed. Refresh your storage and try again.", status=409)
    stack = owned_item.get("stack_size") or 1
    try:
        stack = int(stack)
    except (TypeError, ValueError):
        stack = 1
    if count > stack:
        return _v2_err("count_exceeds_stack",
                       "That is more than the stack holds. Refresh and try again.",
                       status=409)

    seller_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if seller_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    # SOURCE (stop-ship): only the SELECTED character's own bank or backpack can be a
    # listing source, and the portal has to say which one before it calls. Base
    # containers and vehicles have no listing path at all, so anything the strict pair
    # does not match is refused here with the same copy as a lost item rather than being
    # handed to the writer. `_karum_resolve_src` is the container-id trap mitigation: a
    # client-supplied id is MATCHED against a server-resolved pair, never trusted,
    # because container ids and inv_ids share a namespace and collide. Never widen this
    # to a general owned-container lookup.
    containers = await _load_containers(active_account_id)
    if not containers or not containers.get("available"):
        # _load_containers degrades to None on ANY failure (a relay timeout, a
        # collector blob without inv_id). That is a transport fault, not a lost item:
        # naming it not_owner would tell the player their item vanished. 502 + retry copy.
        _audit(False, "unresolved", {"item_id": item_id, "stage": "container_list"})
        return _v2_err("unresolved",
                       "We could not read your storage right now. Please try again in a moment.",
                       status=502)
    expected_src, _src_container = _karum_resolve_src(containers["containers"], seller_ctrl, container_id)
    if expected_src is None:
        return _v2_err("not_owner", _SELL_ERROR_TEXT["not_owner"], status=409)

    fee = _sell_fee(price, duration_days)
    server_tpl = owned_item.get("template_id") or ""
    expected_template = server_tpl if _MARKET_TERM_RE.fullmatch(server_tpl) else None

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        sell_body = {"seller_ctrl": seller_ctrl, "item_id": item_id,
                     "count": count, "price": price, "duration_days": duration_days,
                     "expected_src": expected_src}
        if expected_template is not None:
            sell_body["expected_template"] = expected_template
        result = await call_relay(
            "/dune/market/sell", method="POST", json_body=sell_body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: v2 market sell relay error acct=%s item=%s: %s",
                       active_account_id, item_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: v2 market sell failed acct=%s item=%s: %s",
                       active_account_id, item_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_market_sell", str(item_id), ip,
            details=json.dumps({
                "account_id": active_account_id, "seller_ctrl": seller_ctrl,
                "container_id": container_id, "src": expected_src,
                "item_id": item_id, "count": count,
                "price": price, "duration_days": duration_days, "fee_estimate": fee,
                "uuid": uuid, "surface": "v2",
                "result": "ok" if ok else (err_token or "error"),
                "fee_charged": (result or {}).get("fee") if isinstance(result, dict) else None,
            }),
            success=ok)

    # Store the terminal relay outcome (success OR failure) under the uuid so a
    # timeout retry replays it rather than re-listing. A pre-relay refusal above
    # (player_online, rate_limited, not_owner, ...) never reaches here and is not
    # stored: it never charged, so re-evaluating it on retry is correct.
    if ok:
        charged = (result or {}).get("fee")
        payload = {"ok": True,
                   "fee": charged if isinstance(charged, int) else fee,
                   "bank_after": (result or {}).get("bank_after"),
                   "listed": (result or {}).get("count") or count}
        _sell_idem_store(active_account_id, uuid, payload, 200)
        return JSONResponse(payload, status_code=200)
    friendly = _SELL_ERROR_TEXT.get(
        err_token, "That item could not be listed. Please refresh and try again.")
    status = 409 if err_token in ("player_online", "not_owner", "item_not_found",
                                  "count_exceeds_stack") else 400
    payload = {"ok": False, "error": err_token or "write_failed", "message": friendly}
    _sell_idem_store(active_account_id, uuid, payload, status)
    return JSONResponse(payload, status_code=status)


async def _v2_orders_write(request: Request, *, action: str):
    """V2 CANCEL/RELIST (JSON). ONLINE-SAFE (exchange tables + escrow only, never
    live RAM inventory) so NO offline gate. owner_ctrl resolved SERVER-SIDE; the
    writer re-verifies ownership + revision in-transaction. Reuses the SAME
    `/dune/market/{cancel,relist}` chain + `_orders_rate_ok` + `_ORDERS_ERROR_TEXT`
    as V1. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    order_id = _v2_pos_int(body, "order_id")
    revision = _v2_pos_int(body, "revision")
    uuid = str(body.get("uuid", "") or "")[:64]
    price = duration_days = None
    if action == "relist":
        price = _v2_pos_int(body, "price")
        duration_days = _v2_pos_int(body, "duration_days")

    if order_id is None or revision is None or (
            action == "relist" and (price is None or duration_days is None)):
        return _v2_err("bad_request",
                       "That request was malformed. Refresh and try again.", status=400)
    if action == "relist":
        if duration_days not in _SELL_DURATIONS:
            return _v2_err("bad_duration", "Pick a duration of 1, 3, 7 or 14 days.",
                           status=400)
        if price >= _MARKET_MAX_PRICE:
            return _v2_err("bad_price", "That price is too high.", status=400)

    if not _orders_rate_ok(active_account_id):
        return _v2_err("rate_limited",
                       "One action at a time. Wait a moment and retry.", status=429)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        relay_body = {"owner_ctrl": owner_ctrl, "order_id": order_id, "revision": revision}
        if action == "relist":
            relay_body["price"] = price
            relay_body["duration_days"] = duration_days
        result = await call_relay(
            f"/dune/market/{action}", method="POST", json_body=relay_body, timeout=35)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: v2 market %s relay error acct=%s order=%s: %s",
                       action, active_account_id, order_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: v2 market %s failed acct=%s order=%s: %s",
                       action, active_account_id, order_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", f"portal_market_{action}", str(order_id), ip,
            details=json.dumps({
                "account_id": active_account_id, "owner_ctrl": owner_ctrl,
                "order_id": order_id, "revision": revision, "price": price,
                "duration_days": duration_days, "uuid": uuid, "surface": "v2",
                "result": "ok" if ok else (err_token or "error"),
                "fee_charged": (result or {}).get("fee") if isinstance(result, dict) else None,
            }),
            success=ok)

    if ok:
        # The write changed this player's orders; drop the read cache so the next
        # my-orders fetch shows fresh state rather than the stale 20s snapshot.
        try:
            from cache import invalidate
            invalidate("dune.player_my_orders", str(active_account_id))
        except Exception:  # noqa: BLE001 - cache drop is best-effort
            pass
        return _v2_ok({
            "action": action,
            "fee": (result or {}).get("fee"),
            "bank_after": (result or {}).get("bank_after"),
            "stack": (result or {}).get("stack"),
        })
    friendly = _ORDERS_ERROR_TEXT.get(
        err_token, "That action could not be completed. Please refresh and try again.")
    status = 409 if err_token in ("order_gone", "not_owner", "revision_drift",
                                  "not_active", "not_canceled") else 400
    return _v2_err(err_token or "write_failed", friendly, status=status)


@router.post("/portal/market/v2/orders/cancel")
async def portal_market_v2_orders_cancel(request: Request):
    """V2 cancel one ACTIVE listing (JSON). Online-safe; no offline gate. Auth:
    linked session + CSRF."""
    return await _v2_orders_write(request, action="cancel")


@router.post("/portal/market/v2/orders/relist")
async def portal_market_v2_orders_relist(request: Request):
    """V2 relist one CANCELED order (JSON). Online-safe; no offline gate. Auth:
    linked session + CSRF."""
    return await _v2_orders_write(request, action="relist")


@router.post("/portal/logout")
async def portal_logout(request: Request):
    """Cookie-only logout. CSRF required.
    Auth: session-cookie required."""
    session_token = request.cookies.get(SESSION_COOKIE)
    if not session_token:
        return RedirectResponse(url="/portal/", status_code=302)

    session = get_portal_session(request)
    if not session:
        # Stale cookie — still clear it.
        resp = RedirectResponse(url="/portal/", status_code=302)
        clear_cookie(resp, SESSION_COOKIE)
        clear_cookie(resp, CSRF_COOKIE)
        return resp

    expected_csrf = csrf_for_session(session_token)
    header_csrf = request.headers.get(CSRF_HEADER, "")
    body_csrf = ""
    # The vanilla no-JS form path posts csrf_token in the form body.
    try:
        form = await _read_body(request)
        body_csrf = form.get("csrf_token", "") or ""
    except Exception:
        body_csrf = ""
    provided_csrf = header_csrf or body_csrf
    if not validate_csrf(provided_csrf, expected_csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    if session.get('sid'):
        conn = get_db()
        try:
            conn.execute('UPDATE portal_auth_sessions SET revoked_at=? WHERE id=? AND token_hash=?',
                         (time.time(), session['sid'], session['sh']))
            conn.commit()
        finally:
            conn.close()

    resp = RedirectResponse(url="/portal/?b=logged_out", status_code=302)
    clear_cookie(resp, SESSION_COOKIE)
    clear_cookie(resp, CSRF_COOKIE)
    return resp


# ============================================================================
# V2 MODULE PORTS (2026-07-15): Character + Landsraad JSON siblings for the
# SvelteKit portal. NEW /v2 endpoints only; every V1 HTML handler above is
# byte-untouched. Reads reuse the existing loaders/shapers verbatim; return raw
# ints + icon slugs (the UI formats client-side). Contract:
# docs/dune-research/v2-portal/V2-MODULE-PORTS-BUILD-CONTRACT-2026-07-15.md
#
# Relay ctrl-support (verified against relay/app.py): /dune/player/{id}/progress
# accepts ?ctrl= (per-character); /progression_state and /equipped do NOT (they
# would need a freeze-gated game-box change), so a non-default selected character
# falls back to the account-default value and the payload flags ctrl_scoped:false.
# ============================================================================

async def _load_progress_ctrl(account_id: int, ctrl: int) -> Optional[dict]:
    """Fresh per-character progress read scoped to `ctrl`. Bypasses the account-
    keyed mirror + ttl cache (both only ever hold the account's DEFAULT
    character), so a selected non-default character reports its OWN faction +
    economy. Returns the raw relay progress payload (available) or None."""
    try:
        from relay import call_relay
        payload = await call_relay(
            f"/dune/player/{account_id}/progress?ctrl={int(ctrl)}", timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: ctrl-scoped progress failed acct=%s ctrl=%s: %s",
                       account_id, ctrl, exc)
        return None
    if isinstance(payload, dict) and payload.get("available"):
        return payload
    return None


async def _resolve_char_identity(account_id: int, sel: Optional[int]) -> Optional[dict]:
    """Resolve the SELECTED (or default) character's live name/level/online via
    the same ?list=1 read the switcher uses (per-character, no game-box change).
    Returns one character dict {controller_id, char_name, lvl, online, is_default}
    or None on miss."""
    try:
        from relay import call_relay
        payload = await call_relay(
            f"/dune/player/{account_id}/progress?list=1", timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: char-identity list failed acct=%s: %s", account_id, exc)
        return None
    chars = (payload or {}).get("characters") or []
    if not chars:
        return None
    pick = None
    if sel is not None:
        pick = next((c for c in chars if c.get("controller_id") == sel), None)
    if pick is None:
        pick = next((c for c in chars if c.get("is_default")), None) or chars[0]
    return pick


import re as _augment_re  # noqa: E402 - module has no top-level `re`; same idiom as _re/_guild_re above

_AUGMENT_ID_RE = _augment_re.compile(r"^T\d_Augment_[A-Za-z0-9_]{1,48}$")
_AUGMENT_IDEM_RE = _augment_re.compile(r"^[A-Za-z0-9_-]{8,64}$")


async def _safe_call(coro):
    """Await a best-effort read, degrading to None instead of failing the request.
    Module-level twin of the per-endpoint `_safe` closures, for handlers that need
    it outside a gather()."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: optional read failed: %s", exc)
        return None

# Friendly text per writer error token. The writer/relay own these tokens; the
# frontend has its own copy of this map, so both surfaces stay honest even if one
# is briefly older than the other.
_AUGMENT_ERROR_TEXT = {
    "augment_disabled": "Augment rerolling and swapping is not enabled right now.",
    "player_online": "Log out of the game to change augments. Nothing was changed.",
    "not_owner": "That item is not yours to modify.",
    "item_not_found": "That item is no longer there.",
    # 🔴 The writer consumes PAWN-SIDE ONLY (backpack / worn / hotbar / CHOAM bank),
    # and the picker's read is scoped to exactly the same inventories -- so by far
    # the most common cause of this token is an augment sitting in a container,
    # chest or vehicle, NOT one that was used or traded away. The old text sent
    # players hunting for an item they still have. Say where it has to be instead.
    "augment_not_owned": "That augment has to be on your character to be used \u2014 "
                         "in your backpack, on your hotbar, or in your CHOAM bank. "
                         "One left in a container, chest or vehicle cannot be "
                         "reached. Nothing was changed.",
    "incompatible_augment": "That augment does not fit this item's type. Nothing was changed.",
    "unknown_item_tags": "This item's type could not be verified right now. Nothing was changed.",
    "too_many_augments": "This item already carries its maximum number of augment slots.",
    "idempotency_unavailable": "Augment changes are briefly unavailable. Nothing was changed.",
    # 🔴 HALF-DEPLOY. The portal dispatches only "random" or "transplant" and a
    # current writer accepts both, so this token reaching a player can mean exactly
    # one thing: the game-host writer predates transplant while the flag here is on.
    # DEPLOY ORDER IS writer -> relay -> portal, with
    # LASTSIETCH_AUGMENT_TARGETED_UPGRADE_ENABLED OFF until all three are live.
    # It fails closed -- validate_roll_mode refuses before the transaction, so
    # nothing is consumed -- but unmapped it rendered "The change did not go
    # through", which blames the player's inventory for our deploy ordering.
    "bad_roll_mode": "Augment changes are briefly unavailable while the server "
                     "finishes updating. Nothing was changed. Please try again "
                     "shortly.",
    # Overwritten with a specific "next one in Xh Ym" below; this is the fallback
    # for when the writer could not tell us how long.
    "reroll_capped": "You have used this item's rerolls for now. Try again later.",
    # The writer refused because the change would have dropped one of the item's
    # augment slots (BUG-019). Nothing is modified -- the item keeps every augment.
    "augment_count_shrank": "That change would have removed one of this item's "
                            "augments, so nothing was changed. Please reopen the "
                            "item and try again.",
    # An item cannot carry the same augment in two slots. Without this the request
    # reaches the writer, whose validate_augment_ids() de-dupes the name list, so a
    # 3-slot item arrives as 2 names and the BUG-019 shrink guard refuses it. That
    # refusal is correct but its message ("reopen the item and try again") describes
    # a transient staleness that is not what happened -- a player retried it four times
    # on 2026-08-18 because the text told them to.
    "duplicate_augment": "This item already has that augment in another slot. An "
                         "item cannot carry the same augment twice \u2014 pick a "
                         "different augment, or change the other slot instead.",
    # The writer found nothing carried that rolls higher than what is already
    # fitted, so it refused instead of spending a rare augment for nothing.
    #
    # 🔴 This token means EXACTLY ONE thing: no copy the player holds beats the
    # fitted one at any roll position. It does NOT also cover "a copy qualified
    # but looked risky" -- a copy that is higher somewhere and lower somewhere
    # else is ALLOWED, not refused, and the text must not imply otherwise.
    # Neither may it imply the player owns nothing; they may hold several copies
    # that simply do not beat what is installed.
    #
    # Says neither "upgrade" nor "stat": roll positions are the game's asset order
    # and do NOT map to catalogued effects (the same reason _resolve_augment_effect
    # refuses to label a multi-effect augment), so naming a stat here would assert
    # a mapping this file elsewhere admits it cannot make. It names what was
    # actually compared -- rolls -- and nothing more.
    "no_improvement": "None of the copies you're carrying rolls higher than the "
                      "one already fitted, anywhere. Nothing was changed and "
                      "nothing was destroyed.",
    # The copy the writer picked carries no readable stat rolls. Refusing is the
    # fail-closed answer: installing it would consume the augment and leave an
    # inert slot.
    "augment_roll_data_missing": "That augment has no stat data we can read, so it "
                                 "was not installed. Nothing was changed.",
    # The item's three parallel augment arrays disagree in length. Not a player
    # error and not repairable from here, so say so and ask for a report.
    "augment_arrays_desynced": "This item's augment data is inconsistent, so nothing "
                               "was changed. Please report this.",
    "write_failed": "The change did not go through. Nothing was modified.",
}


@router.post("/portal/character/augment")
async def portal_character_augment(request: Request):
    """Per-item augment REROLL / SWAP. OFFLINE-only, behind LASTSIETCH_AUGMENT_ENABLED
    (default off -> honest `augment_disabled`, no dispatch). JSON body {item_id,
    mode:"reroll"|"swap", augments[], reroll_only?[], swap_slot?, swap_grade?,
    grade?, idempotency_key}. Auth: linked session + CSRF.

    An augment's identity is (name, GRADE). `augments` carries only names, so a
    swap that changes just the grade is invisible in it -- hence `swap_slot`
    (which slot is being replaced) and `swap_grade` (which owned copy is being
    spent). Both are optional so a browser running JS cached from before this
    shipped keeps working; without them a same-name swap is still refused rather
    than silently no-op'd, which is the safe direction.

    🔴 THIS ENDPOINT IS THE ENTIRE AUTH BOUNDARY FOR THE FEATURE. `owner_ctrl` is
    derived HERE from the session and is never read from the body: dune-augment.py
    trusts that one value absolutely to resolve the pawn, ownership and the offline
    gate. A client-supplied owner_ctrl would let anyone reroll or strip augments
    from anyone else's gear. If you are editing this handler, that is the invariant
    to preserve above all others.

    Two further server-side guards exist because the writer REPLACES the item's
    whole augment block from `augments` (`v_stats || <new block>`, not a merge):
      * `augments` is checked against what the item actually carries, so a buggy
        or hostile client cannot send a short list and permanently destroy the
        slots left off it;
      * `grade` for a swapped-in augment is taken from the player's OWN inventory,
        not from the body, so it cannot be inflated on the way through."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    # Soft kill-switch: honest refusal, no dispatch. 200 with ok:false, which the
    # client surfaces as a real sentence (sendCsrfJSON throws on ok:false).
    if not _v2_augment_enabled():
        return _v2_err("augment_disabled", _AUGMENT_ERROR_TEXT["augment_disabled"], status=200)

    raw_item = str(body.get("item_id", "") or "").strip()
    if not raw_item.isdigit() or int(raw_item) <= 0:
        return _v2_err("bad_request", "That request was malformed.", status=400)
    item_id = int(raw_item)

    mode = str(body.get("mode", "") or "").strip()
    if mode not in ("reroll", "swap"):
        return _v2_err("bad_request", "That request was malformed.", status=400)

    def _aug_names(value, required):
        if value is None:
            return None if not required else False
        if not isinstance(value, list) or not value or len(value) > 3:
            return False
        out = []
        for a in value:
            if not isinstance(a, str) or not _AUGMENT_ID_RE.match(a):
                return False
            out.append(a)
        return out

    augments = _aug_names(body.get("augments"), True)
    if augments is False:
        return _v2_err("bad_request", "That request was malformed.", status=400)
    reroll_only = _aug_names(body.get("reroll_only"), False)
    if reroll_only is False:
        return _v2_err("bad_request", "That request was malformed.", status=400)

    # Which SLOT a swap replaces, and which GRADE of the incoming augment the
    # player picked. Both exist because an augment's identity is (name, grade),
    # and `augments` carries only names:
    #   * swap_slot -- on a same-name swap (Damage1 G3 -> Damage1 G5) NO name
    #     differs, so there is nothing to infer the target slot from. Optional,
    #     because a client cached from before this shipped sends neither and must
    #     still get its (working) different-name swaps through; that path falls
    #     back to the name-difference below.
    #   * swap_grade -- a SELECTOR, never an authority. It picks which of the
    #     player's owned copies they meant; the grade actually written is still
    #     read back out of their inventory, so it cannot be inflated here.
    swap_slot = body.get("swap_slot")
    if swap_slot is not None:
        if isinstance(swap_slot, bool) or not isinstance(swap_slot, int) \
                or not 0 <= swap_slot < len(augments):
            return _v2_err("bad_request", "That request was malformed.", status=400)
    swap_grade = body.get("swap_grade")
    if swap_grade is not None:
        if isinstance(swap_grade, bool) or not isinstance(swap_grade, int) \
                or not 1 <= swap_grade <= 5:
            return _v2_err("bad_request", "That request was malformed.", status=400)

    idem = str(body.get("idempotency_key", "") or "").strip()
    if not _AUGMENT_IDEM_RE.match(idem):
        # REQUIRED, not optional. A swap destroys a rare item, so a write with no
        # replay protection is one dropped response away from consuming two.
        return _v2_err("bad_request", "That request was malformed.", status=400)

    if not _storage_rate_ok(active_account_id):
        return _v2_err("rate_limited", "One change at a time. Wait a moment and retry.",
                       status=429)

    # OFFLINE GATE, advisory copy. Fail CLOSED on undetermined: item stats live in
    # RAM while a player is online, so a write against a live session is discarded
    # on their next save tick and the player is told something happened that did
    # not. The writer re-checks authoritatively under the row lock.
    online = await _resolve_online(active_account_id)
    if online is not False:
        return _v2_err("player_online", _AUGMENT_ERROR_TEXT["player_online"], status=409)

    owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _v2_err("unresolved", "We could not verify your character right now. "
                       "Please try again in a moment.", status=502)

    # Defense in depth against a partial `augments` list. The writer REPLACES the
    # whole block, so a list shorter than what the item carries silently deletes
    # the missing slots -- permanently, with the augment items already consumed.
    # Read the item's real current augments and require the request to be a
    # same-length rewrite: identical for a reroll, differing in exactly one slot
    # for a swap.
    # Scope must MATCH the writer's, which is the pawn's own inventories
    # (equipped / hotbar / CHOAM bank) OR any inventory owned outright via
    # owned_inv_sql (placed containers at permission rank 1, non-DeepDesert
    # vehicle cargo). Checking only the equipped read would reject 164 of the 282
    # augmented items on the server -- 58% of them live in boxes and vehicles.
    # The pawn read is tried FIRST because it is a single cheap call; the storage
    # scan only runs when the item is not worn or hotbarred.
    current = None
    current_objs = []
    for it in (await _safe_call(_load_equipped(active_account_id)) or []):
        if str(it.get("item_id")) == str(item_id):
            current_objs = [a for a in (it.get("augments") or []) if a.get("name")]
            current = [a["name"] for a in current_objs]
            break
    if current is None:
        stored, _scanned, _trunc = await _augmented_scan(active_account_id)
        for row in stored:
            if str((row.get("item") or {}).get("item_id")) == str(item_id):
                # Refuse here rather than letting the writer answer not_owner: a
                # placed container or vehicle is RAM-backed by its partition, so
                # the write would be retracted (reroll) or duplicate the consumed
                # augment (swap). Only the pawn-side bank qualifies.
                if not row.get("can_augment"):
                    return _v2_err(
                        "item_not_reachable",
                        "That item is in a storage container or vehicle. Carry it on your "
                        "character or put it in the CHOAM bank, then log out and try again.",
                        status=409)
                current_objs = [a for a in (row.get("augments") or []) if a.get("name")]
                current = [a["name"] for a in current_objs]
                break
    if current is None:
        # No _audit_refusal here: the helper is defined below, alongside the probe.
        # This branch never reached the writer, so nothing can have changed.
        logger.info("portal: augment item %s not in equipped/scan for acct=%s",
                    item_id, active_account_id)
        return _v2_err("item_not_found", _AUGMENT_ERROR_TEXT["item_not_found"], status=404)

    # AUTHORITATIVE, UNCACHED current augments. `current` above comes from the
    # 30s/120s-stale equipped/scan cache that the dialog is ALSO built from, so a
    # request built on a stale snapshot can omit a slot -- and because the writer
    # REPLACES the whole augment block, trusting that list DESTROYED two of a player's
    # augments on 2026-08-06 (BUG-019). The writer's dry-run reads dune.items
    # directly (no cache) via the SAME path the write will lock, so it is ground
    # truth. We rebuild the write from it: a reroll's augment NAMES are always the
    # item's true current set (a reroll never changes WHICH augments are on), and a
    # swap is that set with exactly one slot replaced. The in-writer count invariant
    # is the final backstop; this makes a legit reroll SUCCEED instead of being
    # safe-refused when the player's cached view lagged.
    from relay import call_relay

    # Every refusal below happens BEFORE the write block's own audit_log, so
    # without this they leave no trace at all. That is how the 2026-08-14
    # dry-run bug stayed invisible for three days: players kept getting
    # "That item is no longer there.", the item kept getting rerolled, and the
    # portal audit table showed nothing at all after 08-10.
    def _audit_refusal(token, note=None):
        try:
            from auth import audit_log
            audit_log(
                None, f"portal:{discord_id}", "portal_character_augment",
                str(owner_ctrl), client_ip(request),
                details=json.dumps({
                    "account_id": active_account_id, "owner_ctrl": owner_ctrl,
                    "item_id": item_id, "mode": mode,
                    "augments_requested": augments, "reroll_only": reroll_only,
                    "swap_slot": swap_slot, "swap_grade": swap_grade,
                    "idempotency_key": idem,
                    "stage": "preflight", "note": note,
                    "result": token,
                }),
                success=False,
            )
        except Exception:  # noqa: BLE001 - audit must never block a refusal
            pass

    probe_body = {"owner_ctrl": owner_ctrl, "item_id": item_id,
                  "augments": augments, "roll_mode": "random",
                  "preserve_grades": True, "dry_run": True}
    # Blast-radius limiter, NOT cosmetic. If `dry_run` is ever lost in transit
    # again, a probe carrying reroll_only redraws only the slot the player
    # picked instead of every slot on the item. Costs nothing on a real dry run.
    if reroll_only is not None:
        probe_body["reroll_only"] = reroll_only
    try:
        probe = await call_relay(
            "/dune/augment/apply", method="POST", timeout=30,
            json_body=probe_body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: augment dry-run probe failed acct=%s: %s",
                       active_account_id, exc)
        probe = None
    if not isinstance(probe, dict):
        _audit_refusal("unresolved", "probe returned no dict")
        return _v2_err("unresolved", "We could not verify this item right now. "
                       "Please try again in a moment.", status=502)

    # 🔴 PROVE it was a dry run before trusting a single field of it.
    # `dry_run: true` is echoed ONLY by the writer's do_dry_run path; do_live
    # never emits it. On 2026-08-14 the relay was found to be silently dropping
    # the flag from its request whitelist, so this "probe" was performing a
    # real, uncapped, all-slots reroll and the portal never noticed. Verifying
    # the deployed file said `dry_run: True` proved nothing -- only the response
    # proves what actually executed. Fail CLOSED: a probe we cannot confirm was
    # read-only must never be used to authorise a second write.
    if probe.get("dry_run") is not True:
        logger.error(
            "portal: augment probe did NOT confirm dry_run (acct=%s item=%s keys=%s) -- "
            "refusing. The relay may be dropping dry_run again.",
            active_account_id, item_id, sorted(probe.keys())[:12])
        _audit_refusal("probe_not_dry_run",
                       "writer did not echo dry_run:true -- possible live write")
        return _v2_err("unresolved", "We could not safely verify this item. "
                       "Nothing was changed. Please tell an admin.", status=502)
    # A hard precondition from the authoritative read (offline gate / ownership /
    # reachability) overrides whatever the cached read said.
    _pf = probe.get("preflight_errors") or []
    for _tok in ("player_online", "not_owner", "item_not_found",
                 "unknown_item_kind", "unknown_item_tags"):
        if _tok in _pf:
            _audit_refusal(_tok, "preflight_errors from dry-run probe")
            return _v2_err(_tok, _AUGMENT_ERROR_TEXT.get(
                _tok, _AUGMENT_ERROR_TEXT["write_failed"]), status=409)
    fresh = [str(n) for n in (probe.get("already_installed") or [])]
    if not fresh:
        # Reachable legitimately (item genuinely has no augments). It was ALSO
        # the symptom of the dropped-dry_run bug, because do_live never emits
        # already_installed -- but the dry_run guard above now catches that
        # first, so anything landing here is the real thing.
        _audit_refusal("item_not_found", "probe reported no installed augments")
        return _v2_err("item_not_found", _AUGMENT_ERROR_TEXT["item_not_found"], status=404)
    current = fresh  # ground truth from here on
    # Per-slot grades, index-aligned with `current`. Needed to tell a same-name
    # GRADE swap (a real change, and the whole point of the picker showing grades)
    # from a request that would change nothing but still burn a rare augment.
    # Padded rather than trusted for length: an older writer that predates
    # already_installed_grades sends nothing, and a short list must read as
    # "grade unknown", not silently re-index the slots.
    # 🔴 HALF-DEPLOY GUARD. `already_installed_grades` and `swap_slot` shipped in
    # the SAME writer change, so its presence is how we know the writer on the
    # other end can honour a swap_slot at all. If the portal were deployed ahead
    # of the writer, a same-name swap would be sent to a writer that silently
    # ignores the field: it would consume nothing, keep the old grade, and report
    # SUCCESS -- strictly worse than the refusal this replaced, because the player
    # is told a swap happened. Deploy order is writer -> relay -> portal; this
    # makes getting it wrong a clean refusal instead of a lie.
    grades_known = isinstance(probe.get("already_installed_grades"), list)
    current_grades = [g if isinstance(g, int) and not isinstance(g, bool) else None
                      for g in (probe.get("already_installed_grades") or [])]
    current_grades += [None] * (len(current) - len(current_grades))
    current_grades = current_grades[:len(current)]

    grade = None
    held = None
    # Whether this write asks the writer to TRANSPLANT the consumed copy's rolls
    # rather than redraw them. Only a swap consumes, so a reroll never transplants.
    transplant = False
    if mode == "reroll":
        # A reroll keeps the exact installed set: take the NAMES from ground truth,
        # never the client's (maybe stale) list.
        augments = list(current)
        if reroll_only is not None and any(a not in current for a in reroll_only):
            return _v2_err("bad_request", "That item's augments changed. Reopen it "
                           "and try again.", status=409)
    else:  # swap: ground truth with EXACTLY one slot replaced
        if len(augments) != len(current):
            return _v2_err("bad_request", "That item's augments changed. Reopen it "
                           "and try again.", status=409)
        # WHICH slot is being replaced. A client that names it is authoritative:
        # on a same-name/different-grade swap no name differs, so there is nothing
        # for the server to infer from and the name-difference branch below would
        # refuse a perfectly legitimate swap ("It won't do a swap if you're using
        # the same augment, regardless of grade" -- player report, 2026-08-15). A client
        # cached from before this shipped sends no swap_slot; its different-name
        # swaps still work through the inference path, which is all a bare name
        # list can express.
        if swap_slot is not None and not grades_known:
            # See the half-deploy guard above. Refusing is the honest answer; the
            # alternative is a write that reports success and does nothing.
            logger.error("portal: swap_slot sent but the writer predates it "
                         "(no already_installed_grades) -- refusing acct=%s item=%s",
                         active_account_id, item_id)
            _audit_refusal("unresolved", "writer predates swap_slot support")
            return _v2_err("unresolved", "Augment swapping is briefly unavailable. "
                           "Nothing was changed. Please try again shortly.", status=503)
        if swap_slot is None:
            changed = [i for i, a in enumerate(augments) if a != current[i]]
            if len(changed) != 1:
                return _v2_err("bad_request", "A swap changes exactly one augment.",
                               status=409)
            swap_slot = changed[0]
        elif any(a != current[i] for i, a in enumerate(augments) if i != swap_slot):
            # swap_slot says which slot MAY change; it does not license rewriting
            # the rest of the item. Everything else must still match ground truth.
            return _v2_err("bad_request", "That item's augments changed. Reopen it "
                           "and try again.", status=409)
        incoming = augments[swap_slot]
        # An item cannot hold the same augment twice. current[swap_slot] is
        # excluded on purpose: same name into its OWN slot is a legitimate GRADE
        # swap (8a88f59) and must keep working.
        #
        # 🔴 DO NOT "FIX" THIS BY ALLOWING DUPLICATES. The base game does not
        # support two augments of the same type on one item, and what the engine
        # would do if handed one is UNKNOWN -- untested, not merely disallowed by
        # us (owner, 2026-08-18). Zero of the 361 augmented items on the server
        # carry a duplicate, so nothing today depends on it working.
        #
        # The writer's validate_augment_ids() de-dupes the name list, so a
        # duplicate reaches the BUG-019 shrink guard as a shortened list and is
        # refused there instead -- correctly, but with a message about losing an
        # augment slot, which describes transient staleness and tells the player
        # to reopen and retry. A player retried four times on 2026-08-18 because of
        # exactly that. Refusing HERE is only about giving the true reason; the
        # restriction itself is the engine's, not ours.
        if any(a == incoming for i, a in enumerate(current) if i != swap_slot):
            _audit_refusal("duplicate_augment",
                           f"{incoming} already in another slot of {current}")
            return _v2_err("duplicate_augment",
                           _AUGMENT_ERROR_TEXT["duplicate_augment"], status=409)
        # Grade comes from the player's OWN inventory, never the body. Also proves
        # they actually hold it before we ask the writer to consume it. `swap_grade`
        # only SELECTS among copies they already hold: a player carrying the same
        # augment at two grades (six pawns do, live) has to be able to say which one
        # they are spending, and the writer consumes on (name, grade) so the item
        # destroyed is the one they picked.
        owned = [o for o in (await _safe_call(_load_owned_augments(active_account_id)) or [])
                 if o.get("name") == incoming]
        if swap_grade is not None:
            owned = [o for o in owned if o.get("grade") == swap_grade]
        held = owned[0] if owned else None
        if held is None:
            return _v2_err("augment_not_owned", _AUGMENT_ERROR_TEXT["augment_not_owned"],
                           status=409)
        grade = held.get("grade")
        if isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 5:
            # Fail closed rather than omitting `grade` downstream: the writer's
            # default is 5, so an unreadable grade would silently install the best
            # one in the game.
            _audit_refusal("augment_not_owned", f"unusable grade {grade!r} on {incoming}")
            return _v2_err("augment_not_owned", _AUGMENT_ERROR_TEXT["augment_not_owned"],
                           status=409)
        # Same name AND same grade WAS a guaranteed no-op that would still
        # permanently destroy a rare augment, so it was refused outright. Under
        # transplant it is the exact case the feature exists for -- two copies of
        # the same augment at the same grade with different rolls -- and the
        # refusal is not deleted, it moves to where the rolls actually are: the
        # writer refuses in-transaction with no_improvement if nothing carried
        # rolls higher than what is fitted. With the flag off this stays the only
        # thing protecting players, so the condition line is kept byte-identical.
        if incoming == current[swap_slot] and grade == current_grades[swap_slot]:
            if not _v2_targeted_augment_upgrade_enabled():
                return _v2_err("bad_request", "That augment is already in that slot at "
                               "that grade. Nothing was changed.", status=409)
        # 🔴 SCOPE DECISION (plan §7.1), owner sign-off pending: transplant applies
        # to EVERY swap. `reroll_only = [incoming]` below makes build_roll_payloads
        # redraw the incoming augment's rolls on every consuming swap -- cross-grade
        # and cross-augment included -- so narrowing this would leave the
        # augment-destroying behaviour live for the majority of swaps.
        # To narrow it to same-name/same-grade only, change THIS ONE LINE to:
        #   transplant = (_v2_targeted_augment_upgrade_enabled()
        #                 and incoming == current[swap_slot]
        #                 and grade == current_grades[swap_slot])
        transplant = _v2_targeted_augment_upgrade_enabled()
        # reroll_only must name exactly the incoming augment: that is what keeps
        # every other slot's rolls AND grade untouched.
        reroll_only = [incoming]
        # Rebuild from ground truth so the write targets the item's TRUE slots, not
        # the (possibly stale) client ordering.
        augments = [incoming if i == swap_slot else current[i]
                    for i in range(len(current))]

    augment_body = {
        "owner_ctrl": owner_ctrl,
        "item_id": item_id,
        "augments": augments,
        # Player-facing rolls are a TRUE random draw and can land worse. `perfect`
        # is the operator/prize path only and is never reachable from here.
        # `transplant` is not a third kind of draw: it keeps the consumed copy's
        # REAL rolls instead of drawing at all, which is what stops a hand-picked
        # augment arriving as an average one. Swap-only and flag-gated; the
        # read-only probe above stays on "random" deliberately.
        "roll_mode": "transplant" if transplant else "random",
        # Only a swap installs something new, so only a swap consumes. A reroll of
        # the same augments must cost the player nothing.
        "consume": mode == "swap",
        # Non-negotiable: 34 live items run MIXED per-slot grades, and one uniform
        # grade would silently rewrite gear the player never asked us to touch.
        "preserve_grades": True,
        "idempotency_key": idem,
    }
    if reroll_only is not None:
        augment_body["reroll_only"] = reroll_only
    if grade is not None:
        augment_body["grade"] = grade
    # Only on the real write, and deliberately NOT on the probe above: swap_slot
    # is what tells the writer to consume, so a probe carrying it would become a
    # live swap the moment dry_run went missing again. The probe stays incapable
    # of destroying anything by construction.
    if mode == "swap":
        augment_body["swap_slot"] = swap_slot

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay("/dune/augment/apply", method="POST",
                                  json_body=augment_body, timeout=50)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("portal: augment relay error acct=%s: %s", active_account_id, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: augment failed acct=%s: %s", active_account_id, exc)
    finally:
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_character_augment",
            str(owner_ctrl), ip,
            details=json.dumps({
                "account_id": active_account_id, "owner_ctrl": owner_ctrl,
                "item_id": item_id, "mode": mode,
                "augments_before": current, "augments_after": augments,
                # Grades, not just names: a same-name swap is invisible in the two
                # lists above, so without these the audit row for the exact case
                # this endpoint was fixed for reads as "nothing happened".
                "grades_before": current_grades, "swap_slot": swap_slot,
                "swap_grade": grade,
                "reroll_only": reroll_only,
                # Which write path actually executed. The cheapest independent
                # proof that flipping the flag really does back the change out --
                # it turns "which code path ran" into a SELECT on the audit row.
                "roll_mode": augment_body.get("roll_mode"),
                "idempotency_key": idem,
                "replayed": (result or {}).get("replayed") if isinstance(result, dict) else None,
                "consumed": (result or {}).get("consumed") if isinstance(result, dict) else None,
                "result": "ok" if ok else (err_token or "error"),
            }),
            success=ok,
        )

    if not ok:
        token = err_token or "write_failed"
        text = _AUGMENT_ERROR_TEXT.get(token, _AUGMENT_ERROR_TEXT["write_failed"])
        if token == "bad_roll_mode":
            # Only reachable as the half-deploy described in _AUGMENT_ERROR_TEXT:
            # nothing this endpoint can dispatch is an invalid mode on a current
            # writer. Log it loudly -- the player's message is deliberately vague
            # about our deploy state, so this is the only place it is diagnosable.
            logger.error(
                "portal: writer rejected roll_mode=%r (acct=%s item=%s) -- the game-host "
                "writer predates transplant. Turn LASTSIETCH_AUGMENT_TARGETED_UPGRADE_ENABLED "
                "off, or deploy the writer first (writer -> relay -> portal).",
                augment_body.get("roll_mode"), active_account_id, item_id)
        # The cap refusal is only useful if it says WHEN. The writer returns the
        # allowance and the wait, so build the sentence here rather than making
        # the player guess or retry blindly.
        if token == "reroll_capped" and isinstance(result, dict):
            cap = result.get("cap") or 3
            hours = result.get("window_hours") or 4
            wait = int(result.get("retry_after_seconds") or 0)
            when = ""
            if wait > 0:
                h, m = divmod((wait + 59) // 60, 60)
                when = f" Next one in {h}h {m}m." if h else f" Next one in {m}m."
            text = (f"{cap} rerolls per item every {hours} hours, and this item has "
                    f"used them.{when} Nothing was changed.")
        return _v2_err(token, text, status=200)

    # The gear card is served from a 30s TTL cache; without dropping it the player
    # is shown their PRE-reroll rolls for up to half a minute after being told the
    # reroll landed.
    try:
        from cache import invalidate
        # Key must match how cached_player_equipped was CALLED: _load_equipped
        # passes the int through, so the key is (name, int). A str() here would
        # miss the entry and silently leave the stale card in place.
        invalidate("dune.player_equipped", active_account_id)
    except Exception:  # noqa: BLE001 - cache drop is best-effort
        pass

    # Re-shape the writer's parallel arrays into the {name,label,grade,rolls,effects}
    # objects the card already renders, so the row updates without a refetch. These
    # come from the row the writer READ BACK, so a replay reports the item's real
    # state rather than this attempt's discarded draw.
    # Labels/effects are resolved on the GAME HOST from the augment catalogue and
    # never travel with the writer's response, so carry them across from the two
    # reads that DID have them: the item's own augments, and (for the incoming one)
    # the player's owned list. Falling back to the raw template id would flip the
    # row from "Heavy Caliber Upgrade" to "T6 Augment Damage1" the instant a reroll
    # succeeded, which reads as the portal having lost track of the item.
    known = {a["name"]: a for a in current_objs}
    if mode == "swap" and held:
        known.setdefault(held["name"], held)
    names = result.get("augments") or augments
    grades = result.get("grades") or []
    rolls = result.get("stat_rolls") or []
    shaped = [{
        "name": n,
        "label": (known.get(n) or {}).get("label") or n,
        "grade": grades[i] if i < len(grades) else (known.get(n) or {}).get("grade"),
        "rolls": rolls[i] if i < len(rolls) else [],
        "effects": (known.get(n) or {}).get("effects") or [],
        # Recomputed from the NEW rolls, never carried across from `known`:
        # the effect TEXT survives a reroll but its resolved value does not.
        "resolved": _resolve_augment_effect(
            (known.get(n) or {}).get("effects") or [],
            rolls[i] if i < len(rolls) else []),
    } for i, n in enumerate(names)]
    return {
        "ok": True,
        "item_id": item_id,
        "mode": mode,
        "replayed": bool(result.get("replayed")),
        "consumed": result.get("consumed") or 0,
        "augments": shaped,
        # WHICH slots actually got fresh rolls. Without this the result panel
        # cannot tell a rerolled augment from an untouched one, and listing both
        # identically implies we rerolled things we did not.
        "rerolled": [str(x) for x in (result.get("rerolled") or [])],
    }


@router.get("/portal/character/v2")
async def portal_character_v2(request: Request):
    """V2 Character overview (JSON). Per-character: honors the portal switcher's
    selected controller. Progress (faction + economy) is ctrl-scoped via the
    relay; specializations + equipped relay reads do NOT accept ?ctrl=, so for a
    non-default selected character they fall back to the account-default value
    with ctrl_scoped:false. Reads only. Auth: linked session (JSON 401)."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, row = gate
    _touch_last_session(active_account_id)

    from portal_quiz import _current_faction, _derive_current_map, _read_player_tags

    sel = _selected_ctrl(request, active_account_id)

    async def _safe(coro):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - each card degrades independently
            logger.warning("portal: character/v2 fetch failed: %s", exc)
            return None

    progress_coro = (_load_progress_ctrl(active_account_id, sel) if sel is not None
                     else _load_progress(active_account_id))
    (progress, ident, tags, specializations, journey, shaped, equipped_items
     ) = await asyncio.gather(
        _safe(progress_coro),
        _safe(_resolve_char_identity(active_account_id, sel)),
        _safe(_read_player_tags(active_account_id)),
        _safe(_load_specializations(active_account_id)),
        _safe(_load_journey(active_account_id)),
        _safe(_load_landsraad(active_account_id)),
        _safe(_load_equipped(active_account_id)),
    )

    # Account-default scalars (RAM-cached snapshot): intel + name/level/online
    # fallback when the ?list=1 identity read misses.
    try:
        scalars = mirror.get_scalars(active_account_id)
    except Exception:  # noqa: BLE001
        scalars = None

    # spec/equipped relay reads can't be ctrl-scoped without a game-box change, so
    # they always reflect the account's DEFAULT character. They are therefore
    # correct whenever the resolved character IS the default (no cookie, or the
    # switcher points at the default); flag them not-scoped only for a genuine
    # non-default selection (fail pessimistic when identity can't be resolved).
    ctrl_scoped = (sel is None) or bool(ident and ident.get("is_default"))

    name = (ident or {}).get("char_name") or row["character_name"]
    level = (ident or {}).get("lvl")
    # Tri-state: None when neither the identity resolve nor the RAM scalars
    # answered, so the hero line never states "offline" it could not read.
    online = bool((ident or {}).get("online")) if ident else None
    if ident is None and scalars is not None:
        name = scalars.get("char_name") or name
        level = scalars.get("lvl")
        online = bool(scalars.get("online"))
    current_map = await _safe(_derive_current_map(name)) if name else None

    char = (progress or {}).get("character") or {}
    econ = (progress or {}).get("economy") or {}
    # intel: prefer the ctrl-scoped progress (per-character) if present, else the
    # account-default snapshot.
    intel = char.get("intel")
    if intel is None and scalars is not None:
        intel = scalars.get("intel")

    faction = _current_faction(progress, tags or [])
    faction_crest = (_faction_crest(faction)
                     if faction and faction != "Unaligned" else None)

    faction_rep = None
    fac = (progress or {}).get("faction") or {}
    if fac.get("faction_id") in (1, 2) and fac.get("reputation") is not None:
        rk = _faction_rank(fac["reputation"], fac.get("faction_name"))
        faction_rep = {
            "faction": fac.get("faction_name"),
            "crest": _faction_crest(fac.get("faction_name")),
            "rank": rk["rank"],
            "rank_name": rk["rank_name"],
            "standing": rk["standing"],
            "pct": rk["pct"],
            "at_max": rk["at_max"],
            "next_rank": rk.get("next_rank"),
            "to_next": rk.get("to_next"),
        }

    if specializations is not None:
        specializations["ctrl_scoped"] = ctrl_scoped

    landsraad_teaser = None
    if shaped and shaped.get("available") and shaped["summary"]["total_lines"] > 0:
        s = shaped["summary"]
        landsraad_teaser = {
            "total_lines": s["total_lines"],
            "total_solari": s["total_solari"],
            "schematic_lines": s["schematic_lines"],
        }

    equipped = None
    if equipped_items:
        shaped_items = [{
            "slot": it.get("slot"),
            "name": it.get("name"),
            "template": it.get("template_id"),
            # dune-icon basename for the glyph (template_id is NOT an icon name,
            # so the frontend must resolve via `icon`, same as storage/weapons).
            "icon": _icon_for(it.get("template_id")),
            "category": it.get("category"),
            "quality": it.get("quality"),
            "variant_id": it.get("variant_id") or "",
            "swatch_id": it.get("swatch_id") or "",
            "item_id": it.get("item_id"),
            "source": it.get("source"),
            "augments": _with_resolved(it.get("augments") or []),
            "cur_dur": it.get("cur_dur") or "",
            "max_dur": it.get("max_dur") or "",
        } for it in equipped_items]
        # Counts are derived from what was actually shaped above, not trusted
        # from the reader, so they can never drift from what the card renders.
        equipped = {
            "items": shaped_items,
            "ctrl_scoped": ctrl_scoped,
            "equipped_count": sum(1 for it in shaped_items if it.get("source") == "equipped"),
            "hotbar_count": sum(1 for it in shaped_items if it.get("source") == "hotbar"),
            "augmented_count": sum(1 for it in shaped_items if it.get("augments")),
            # Augment reroll/swap trio, nested here (not at the top level) because
            # that is where the UI reads them from -- see the contract note in
            # lib/augments.svelte.js. Without `augments_enabled` the reroll button
            # cannot render AT ALL, flag or no flag, which is how this feature sat
            # invisible after it was built.
            "augments_enabled": _v2_augment_enabled(),
            # TRI-STATE, and it must stay tri-state. None = undetermined, which the
            # dialog treats as LOCKED. Collapsing it to a bool (as the `online`
            # scalar above does for display) would turn "we do not know" into
            # "they are offline, go ahead" on the one gate where being wrong
            # silently discards the player's write on their next save tick.
            "player_online": await _safe(_resolve_online(active_account_id)),
            "owned_augments": (await _safe(_load_owned_augments(active_account_id))) or [],
        }

    session_token = request.cookies.get(SESSION_COOKIE, "")
    return _v2_ok({
        "character": {
            "name": name,
            "level": level,
            "online": online,
            "current_map": current_map,
            "faction": faction,
            "faction_crest": faction_crest,
            "last_online": "Online now" if online else None,
        },
        "vitals": {
            "intel": intel,
            "xp": char.get("xp"),
            "unspent_sp": char.get("unspent_sp"),
            "bank_solari": econ.get("bank_solari", econ.get("solari")),
            "pocket_solari": econ.get("pocket_solari"),
            "scrip": econ.get("scrip"),
        },
        "faction_rep": faction_rep,
        "specializations": specializations,
        "journey": journey,
        "landsraad_teaser": landsraad_teaser,
        "equipped": equipped,
        "csrf_token": csrf_for_session(session_token) if session_token else "",
        "character_name": name,
    })


# --------------------------------------------------- Landsraad V2 (swatches) --
# Proxy swatch chips (Option A): render each house's HArmCharDyepack<House> armor
# palette as honest "house palette" chips (exact:false). File the accurate
# placeable-dyepack RE extraction as a follow-up. House->LUT key has 3 spelling
# drifts between data.house_reps and static/data/swatch-lut.json.
_LANDSRAAD_SWATCH_ALIAS = {
    "Argosaz": "Agrosaz",
    "Mikkarol": "Mikarrol",
    "Taligari": "Talgari",
}
_SWATCH_LUT_CACHE = {"map": None}


def _swatch_lut_by_house() -> dict:
    """{house_base_lower: ['#RRGGBB', ...]} from each Landsraad house's
    HArmCharDyepack<House> dyepack palette. Loaded once; empty on any failure
    (chips are simply omitted, name renders alone)."""
    cached = _SWATCH_LUT_CACHE["map"]
    if cached is not None:
        return cached
    out = {}
    try:
        path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)),
                             "static", "data", "swatch-lut.json")
        with open(path, "r", encoding="utf-8") as fh:
            swatches = (json.load(fh) or {}).get("swatches") or {}
        for raw in HOUSE_REP_LOCATIONS:
            base = _house_base(raw)
            key = "HArmCharDyepack" + _LANDSRAAD_SWATCH_ALIAS.get(base, base)
            entry = swatches.get(key)
            hexes = (entry or {}).get("hex") or []
            if hexes:
                out[base.lower()] = ["#" + str(h).lstrip("#") for h in hexes]
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: swatch-lut load failed: %s", exc)
    _SWATCH_LUT_CACHE["map"] = out
    return out


def _swatch_chips_for_house(house_base: str) -> Optional[list]:
    chips = _swatch_lut_by_house().get((house_base or "").lower())
    return list(chips) if chips else None


def _attach_board_swatches(shaped_board: dict, raw_board: dict) -> None:
    """Attach proxy swatch chips to board tile reward rows whose template_id is a
    placeable swatch. Correlates shaped tiles<->raw tiles by board_index and
    reward rows by index (the shaper preserves order)."""
    raw_by_idx = {int(t.get("board_index") or -1): t
                  for t in (raw_board.get("tiles") or [])}
    for tile in shaped_board.get("tiles") or []:
        rt = raw_by_idx.get(int(tile.get("board_index") or -1))
        if not rt:
            continue
        raw_rewards = rt.get("rewards") or []
        chips = _swatch_chips_for_house(tile.get("short") or "")
        if not chips:
            continue
        for i, rewrow in enumerate(tile.get("rewards") or []):
            if i >= len(raw_rewards):
                break
            tid = raw_rewards[i].get("template_id") or ""
            if "Swatch" in tid:
                rewrow["swatch"] = {"chips": chips, "exact": False}


def _attach_rewards_swatches(rewards: dict, raw_rewards: dict) -> None:
    """Attach proxy swatch chips to the rewards-half item rows (board + houses)
    whose template_id is a placeable swatch. Correlates by house raw name + item
    index (the shaper preserves per-house item order)."""
    raw_items_by_house = {
        (h.get("house_name") or ""): (h.get("items") or [])
        for h in (raw_rewards.get("houses") or [])
    }
    for section in ("board", "houses"):
        for entry in rewards.get(section) or []:
            raw_items = raw_items_by_house.get(entry.get("raw") or "")
            if not raw_items:
                continue
            chips = _swatch_chips_for_house(_house_base(entry.get("raw") or ""))
            if not chips:
                continue
            for i, it in enumerate(entry.get("items") or []):
                if i >= len(raw_items):
                    break
                tid = raw_items[i].get("template_id") or ""
                if "Swatch" in tid:
                    it["swatch"] = {"chips": chips, "exact": False}


@router.get("/portal/landsraad/v2")
async def portal_landsraad_v2(request: Request):
    """V2 Landsraad overview (JSON). Account-scoped, read-only, no writes/CSRF.
    Wraps the same two data sources portal_landsraad uses (rewards + term board)
    and reuses their shapers verbatim; the board half degrades independently of
    the rewards half. Viewer-faction scoping is preserved (the opposing faction's
    exact per-tile numbers never leave the server). Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, active_account_id, row = gate
    _touch_last_session(active_account_id)

    # Rewards half: fetch raw (mirror-first, same as _load_landsraad) so swatch
    # template ids survive for correlation, then shape verbatim.
    rewards = None
    raw_rewards = None
    try:
        raw_rewards = mirror.get_section(active_account_id, "landsraad")
        if raw_rewards is None:
            from routers.dune import cached_landsraad_rewards
            raw_rewards = await cached_landsraad_rewards(active_account_id)
        rewards = _shape_landsraad_for_render(raw_rewards or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: landsraad/v2 rewards failed: %s", exc)
        rewards, raw_rewards = None, None
    if rewards is not None and raw_rewards:
        _attach_rewards_swatches(rewards, raw_rewards)

    # Session overlay for the board: player's per-house contribution, reused from
    # the rewards ladders (identical to portal_landsraad; no extra relay call).
    my_contrib_by_raw = {}
    if rewards and rewards.get("board"):
        for b in rewards["board"]:
            lad = b.get("ladder")
            if lad is not None and b.get("raw"):
                my_contrib_by_raw[b["raw"]] = int(lad.get("my_contribution") or 0)

    # Viewer faction scopes the board's progress rows (own house only).
    viewer_faction_id = None
    progress = await _load_progress(active_account_id)
    if progress:
        fac = progress.get("faction") or {}
        if fac.get("faction_id") in (1, 2):
            viewer_faction_id = int(fac["faction_id"])

    # Board half (term-global): fetch raw for swatch correlation, shape verbatim.
    board = None
    try:
        from routers.dune import cached_landsraad_board
        raw_board = await cached_landsraad_board()
        shaped_board = _shape_landsraad_board(
            raw_board or {}, my_contrib_by_raw or None, viewer_faction_id)
        if shaped_board.get("available"):
            _attach_board_swatches(shaped_board, raw_board or {})
            board = shaped_board
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: landsraad/v2 board failed: %s", exc)
        board = None

    return _v2_ok({
        "board": board,
        "rewards": rewards,
        "active_character_name": row["character_name"],
        "rewards_error": rewards is None,
    })


# --------------------------------------------------- Home overview V2 (w8) --
#
# ONE composition endpoint for the signed-in Home page, so a phone pays a single
# round trip instead of nine. EIGHT of the nine sub-objects own no data: each is
# built from a loader that already exists and is already cached where it hits the
# relay, so those eight cost only the reads the player's other pages were already
# paying for, and not one of them adds a relay verb, a dune.* read or a line of
# game-database SQL.
#
# `deliveries` is the ninth, and it is the exception, admitted deliberately in
# wave 12. The packages the server sent a player exist on no other portal page,
# so there was no cached loader to compose: it reads the game host over a relay
# verb of its own (/dune/player/{account_id}/deliveries) and a game-host handler
# of its own. It is kept off that box the way everything else here is, by
# `portal_deliveries.load` and its 300 second per-account cache, and that module
# is the ONLY one a Home builder may reach the game host through. A second one
# would be a second game-host read on the most-loaded page in the portal, and it
# has to be argued for rather than imported.
#
# If a number is not already on some existing overview, and cannot be read
# through a cached loader of its own, Home does not show it.
#
# Each sub-object is computed independently and becomes None when its loader
# fails. None means "we could not read this", which the client renders as a
# sealed panel; a 0 would read as "you have none", which is a different sentence
# and a wrong one. Nothing here ever substitutes a zero for a failed read.
#
# The body is display-only: no account id, no controller id, no discord id, no
# identity code. Home is the most widely shared screen in the portal and the one
# most likely to end up in a screenshot.

_HOME_GUILD_MEMBERS_MAX = 12
_HOME_PARTS = ("character", "rewards", "guild", "landsraad", "mailbox", "wallet",
               "orders", "caps", "deliveries")


def _home_ok(label: str, value):
    """A gathered sub-object, or None when its builder raised. One failing loader
    seals one panel and never takes the response down with it."""
    if isinstance(value, BaseException):
        logger.warning("portal: home/v2 %s unavailable: %s", label, value)
        return None
    return value


def _home_ago_seconds(stamp) -> Optional[int]:
    """Whole seconds since a game-host timestamp, or None when it cannot be
    parsed (the census timestamp is upstream text we do not own). Clamped at 0 so
    clock skew never reads as a hail from the future."""
    if not stamp:
        return None
    text = str(stamp).strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - when).total_seconds()))


def _karum_open_count(account_id: int) -> int:
    """The player's OPEN Karum positions: active sale listings plus active wanted
    orders, the same two rows /portal/karum renders as my_listings/my_requests.
    Local admin.db only -- the Karum board lives here, not on the game host."""
    conn = get_db()
    try:
        listings = conn.execute(
            "SELECT COUNT(*) AS c FROM portal_karum_listings "
            "WHERE seller_account_id = ? AND status = 'active'",
            (int(account_id),)).fetchone()["c"]
        wanted = conn.execute(
            "SELECT COUNT(*) AS c FROM portal_karum_requests "
            "WHERE requester_account_id = ? AND status = 'active'",
            (int(account_id),)).fetchone()["c"]
    finally:
        conn.close()
    return int(listings) + int(wanted)


def _home_map_label(raw) -> Optional[str]:
    """The player-facing name of a raw map id, or None when the id is not one of
    the named maps. Both `_display_map` and the roster index fall through to the
    raw string when a lookup misses, which puts an internal id like CB_EcoLab_2
    straight into the Hero line. Home says nothing rather than that: the raw id
    still travels as current_map_raw for callers that want to reason about it."""
    from routers.dune import MAP_DISPLAY_NAMES

    if not raw:
        return None
    text = str(raw)
    base = re.sub(r"_\d+$", "", text)
    return MAP_DISPLAY_NAMES.get(text) or MAP_DISPLAY_NAMES.get(base) or None


def _home_point_int(value) -> Optional[int]:
    """A world id off the positions feed as an int, or None. Booleans and junk
    strings are not ids: a partition we cannot read is None, never a 0, because
    0 is a real dimension."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _home_own_point(account_id: int) -> Optional[dict]:
    """{partition, map_raw} for the caller's OWN point in the live positions
    feed, or None when the feed does not carry it.

    The roster producer emits no partition (scripts/dune-roster.py), so the
    sietch a Hagga player is standing in is knowable only here. The feed is
    matched on exactly one account id, the caller's, and only the two world ids
    come back: no other player's row can reach the response through this. `a`
    arrives as an int or as a numeric string depending on the host emitter, and
    it is absent entirely until the game-host lane that adds it ships, so a miss
    is an ordinary answer and never an error."""
    from routers.dune import positions_raw_payload

    want = int(account_id)
    payload = await positions_raw_payload()
    for point in ((payload or {}).get("players") or []):
        if not isinstance(point, dict):
            continue
        if _home_point_int(point.get("a")) != want:
            continue
        map_raw = point.get("m") or None
        return {
            "partition": _home_point_int(point.get("p")),
            "map_raw": str(map_raw) if map_raw else None,
        }
    return None


async def _home_character(account_id: int, sel, fallback_name: str) -> dict:
    """{name, online, current_map, current_map_raw, current_partition}. Resolves
    the session's ACTIVE character exactly the way /portal/character/v2 does
    (selected controller first, the RAM scalars as the fallback), so the two
    screens can never name different characters.

    Placement starts from ONE lookup in the shared cached roster index -- the
    same source /portal/character/v2's current_map reads through
    `_derive_current_map`, taken as a whole row rather than three scans of it.
    An offline character is on no roster, so it starts out unplaced.

    The roster carries no partition today, so the live positions feed is read for
    the caller's own point and wins over the roster wherever it resolves. A miss
    there (offline, feed down, a host emitter that does not carry the account id
    yet) leaves every field exactly as the roster gave it, partition included.
    Nothing is inferred from the map string: naming the wrong sietch is worse
    than naming none."""
    from portal_quiz import _normalize, _roster_index

    ident = await _safe_call(_resolve_char_identity(account_id, sel))
    try:
        scalars = mirror.get_scalars(account_id)
    except Exception:  # noqa: BLE001
        scalars = None

    name = (ident or {}).get("char_name") or fallback_name
    online = bool((ident or {}).get("online")) if ident else False
    if ident is None and scalars is not None:
        name = scalars.get("char_name") or name
        online = bool(scalars.get("online"))

    placement = None
    if name:
        index = await _safe_call(_roster_index())
        placement = (index or {}).get(_normalize(name))
    map_raw = (placement or {}).get("map_raw")
    partition = (placement or {}).get("partition")

    try:
        live = await _home_own_point(account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal: home/v2 live placement unavailable: %s", exc)
        live = None
    if live:
        if live.get("partition") is not None:
            partition = live["partition"]
        if live.get("map_raw"):
            map_raw = live["map_raw"]

    return {
        "name": name,
        "online": online,
        "current_map": _home_map_label(map_raw),
        "current_map_raw": map_raw,
        "current_partition": partition,
    }


async def _home_rewards(account_id: int) -> dict:
    """{enabled, claimable_total, claimable_days, next_claim_utc,
    weekly_claimable, monthly_claimable} off the SAME builders
    /portal/rewards/overview uses, so the Home strip and the rewards page can
    never disagree about what is claimable. A failed login-days or flag read
    raises rather than collapsing to an empty history: day 1 with nothing to
    claim and "we could not read your history" are not the same answer."""
    from routers.dune import cached_rewards_enabled

    login_days = await _rewards_login_days(account_id)
    enabled = bool((await cached_rewards_enabled()).get("enabled"))

    today = rewards.utc_today()
    streak = rewards.compute_streak(login_days, today)
    daily_claimed = rewards.claimed_period_keys(account_id, "daily_solari")
    weekly_claimed = rewards.claimed_period_keys(account_id, "weekly_item")
    monthly_claimed = rewards.claimed_period_keys(account_id, "monthly_augment")

    pool_total, pool_entries = rewards.claim_pool(login_days, daily_claimed, today)
    weekly_unlocked = streak["current"] >= rewards.WEEKLY_STREAK_REQUIREMENT
    weekly_claimable = weekly_unlocked and rewards.iso_week_key(today) not in weekly_claimed
    mstate = rewards.monthly_claim_state(login_days, monthly_claimed, today)

    return {
        "enabled": enabled,
        # The 7-cell ramp grid, from the same builder that fills the overview's
        # daily.cycle. Same call, same arguments: the strip and the rewards page
        # paint one grid, not two that have to be kept in step by hand.
        "cycle": rewards.cycle_cells(login_days, daily_claimed, today),
        "claimable_total": pool_total,
        "claimable_days": len(pool_entries),
        "next_claim_utc": rewards.next_utc_midnight().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weekly_claimable": bool(weekly_claimable),
        "monthly_claimable": bool(mstate["claimable"]),
    }


def _home_guild_members(rows: list, map_index: dict) -> list:
    """Up to _HOME_GUILD_MEMBERS_MAX census rows, online first then by name.
    Presence is binary and case-sensitive ('Online'), matching RosterToken. Map
    is only meaningful for an online player, and the row carries no controller id
    and no account id -- it is display text, nothing more."""
    from portal_quiz import _normalize

    shaped = []
    for m in rows:
        name = m.get("character_name")
        if not name:
            continue
        online = m.get("online_status") == "Online"
        shaped.append({
            "name": name,
            "online": online,
            "map": map_index.get(_normalize(name)) if online else None,
            "last_seen_ago_s": _home_ago_seconds(m.get("last_activity")),
        })
    shaped.sort(key=lambda r: (0 if r["online"] else 1, (r["name"] or "").lower()))
    return shaped[:_HOME_GUILD_MEMBERS_MAX]


async def _home_guild(account_id: int) -> dict:
    """{name, invite_count, newest_sent_ago_s, roster_size, online_members,
    members}. The invite half is the anchor: a player with NO guild can still
    hold a pending invite, and that is exactly the player the invite line is for,
    so `name` is None for them rather than the whole object. The census half
    degrades on its own (roster_size/online_members None, members []).

    Member-gated by construction: the census is only ever asked for the guild the
    caller was just found in, which is the same check /portal/guilds/{id}/presence
    makes before it calls the identical loader."""
    from routers.dune import cached_guild_census, cached_guild_invites

    invites = await cached_guild_invites(account_id)
    if not isinstance(invites, dict) or invites.get("error"):
        raise RuntimeError("guild invites unavailable")
    pending = invites.get("invites") or []
    # sent_ago_s is computed on the game host against its own universe-time basis
    # (see scripts/dune-guilds.py); newest = the smallest age, absent if none.
    ages = [int(i["sent_ago_s"]) for i in pending if i.get("sent_ago_s") is not None]

    name = None
    roster_size = None
    online_members = None
    members = []
    data = await _load_guilds()
    guild = None
    if data:
        guild = next((g for g in data["guilds"]
                      if _guild_member_account(g, account_id) is not None), None)
    if guild is not None:
        name = guild.get("guild_name")
        census = await cached_guild_census(int(guild["guild_id"]))
        if isinstance(census, dict) and not census.get("error"):
            rows = census.get("members") or []
            roster_size = len(rows)
            online_members = sum(1 for m in rows if m.get("online_status") == "Online")
            map_index = {}
            if online_members:
                from portal_quiz import _roster_map_index
                map_index = await _roster_map_index()
            members = _home_guild_members(rows, map_index)

    return {
        "name": name,
        "invite_count": len(pending),
        "newest_sent_ago_s": min(ages) if ages else None,
        "roster_size": roster_size,
        "online_members": online_members,
        "members": members,
    }


async def _home_landsraad(account_id: int) -> dict:
    """{my_contribution}: the signed-in player's OWN contribution this term,
    summed across houses off the same per-house ladders /portal/landsraad/v2
    overlays onto the board. Personal only -- the standings themselves are public
    and stay on their own route, and no opposing-faction number is read here.

    None when there is no board to count against (no active term, or the read
    failed): a player who has not contributed scores 0, and "there is nothing to
    contribute to" is not the same statement."""
    payload = await _load_landsraad(account_id)
    if payload is None:
        raise RuntimeError("landsraad read unavailable")
    board = payload.get("board") or []
    total = None
    for entry in board:
        ladder = entry.get("ladder")
        if ladder is None:
            continue
        total = (total or 0) + int(ladder.get("my_contribution") or 0)
    return {"my_contribution": total}


async def _home_mailbox(account_id: int) -> dict:
    """{unread}. The nav bell's own counter -- same table, same predicate as
    GET /portal/messages/unread-count (routers/messages.py), which is the number
    this one has to agree with. Local admin.db, no game-database touch."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_messages
                WHERE recipient_kind = 'player' AND recipient_id = ?
                  AND state = 'unread' AND deleted_at IS NULL""",
            (int(account_id),),
        ).fetchone()
    finally:
        conn.close()
    return {"unread": int(row["c"])}


async def _home_wallet(account_id: int, sel) -> dict:
    """{bank_solari}: BANKED Solari only (owner ruling 7), server-resolved for
    the selected character by the same helper every buy/sell route funds from. An
    unreadable bank raises, so the instrument seals rather than showing 0."""
    _ctrl, bank_solari = await _resolve_buyer_ctrl_and_bank(account_id, sel)
    if bank_solari is None:
        raise RuntimeError("bank read unavailable")
    return {"bank_solari": int(bank_solari)}


async def _home_orders(account_id: int) -> dict:
    """{market_open, market_filled_today, karum_open}. The CHOAM halves come off
    the cached My Orders read; filled_today counts realised SALES stamped with
    today's UTC date (completion_type 4, the same map /portal/my-orders labels
    'Sold'), because the Completed tab carries no timestamp to date it by."""
    orders = await _load_my_orders(account_id)
    if not orders.get("available"):
        raise RuntimeError("my-orders read unavailable")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filled = sum(1 for h in (orders.get("history") or [])
                 if h.get("completion_type") == 4
                 and str(h.get("logged_at") or "")[:10] == today)
    return {
        "market_open": (None if orders.get("active_count") is None
                        else int(orders.get("active_count"))),
        "market_filled_today": filled,
        "karum_open": _karum_open_count(account_id),
    }


async def _home_caps(discord_id: str) -> dict:
    """{transfer_daily_remaining} off the same mirror the storage overview's cap
    preview reads. `_transfer_caps` degrades to {} instead of raising, and an
    unreadable cap is not an exhausted cap, so an empty preview seals the field
    rather than telling the player they have 0 sends left."""
    caps = _transfer_caps(discord_id)
    cap = caps.get("transfer_daily_cap")
    used = caps.get("transfer_daily_used")
    if cap is None or used is None:
        raise RuntimeError("transfer caps unavailable")
    return {"transfer_daily_remaining": max(0, int(cap) - int(used))}


async def _home_deliveries(account_id: int) -> dict:
    """{count, pending_count, latest}: the Deliveries card's three numbers, off
    the SAME cached read the Mailbox panel renders in full, so the card and the
    page can never disagree about how many packages are still landing. An
    unavailable read raises, which seals the card; telling a player the server
    has sent them nothing because the game host did not answer is a lie, and
    "you were sent nothing" is the one sentence this card must never guess."""
    import portal_deliveries
    shaped = await portal_deliveries.load(account_id)
    if shaped is None or not shaped.get("available"):
        raise RuntimeError("deliveries read unavailable")
    # The summary the full page renders, not a second count computed here: two
    # copies of "how many are still landing" would disagree the first time a leg
    # rule moved, and the card and the Mailbox panel sit one click apart.
    counts = shaped.get("summary")
    if not isinstance(counts, dict):
        raise RuntimeError("deliveries summary unavailable")
    return {
        "count": counts["count"],
        "pending_count": counts["pending_count"],
        "latest": counts["latest"],
    }


@router.get("/portal/activity")
async def portal_activity_feed(request: Request):
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _, account_id, _ = gate
    import portal_activity
    import portal_deliveries
    from routers.portal_codes import activity_items

    as_of = datetime.now(timezone.utc).isoformat()
    ledger, trades, packages, alerts = await asyncio.gather(
        asyncio.to_thread(activity_items, [account_id], 100, with_status=True),
        _load_my_orders(account_id),
        portal_deliveries.load(account_id),
        asyncio.to_thread(market_watch.list_alerts, account_id, 100),
        return_exceptions=True,
    )
    payload = portal_activity.compose_activity(ledger, trades, packages, alerts)
    return JSONResponse({"ok": True, "as_of": as_of, **payload},
                        headers={"Cache-Control": "private, no-store"})


@router.get("/portal/home/v2")
async def portal_home_v2(request: Request):
    """Wave 8 Home composition (JSON). Everything the signed-in Home page reads
    that is not already a public world endpoint, in one private read: the active
    character, the rewards pool, the sietch (invites + presence), the player's
    own Landsraad contribution, the mailbox badge, banked Solari, order counts,
    the transfer cap remainder, and the packages the server has sent them.

    Anonymous gets a 401 envelope and no fields at all -- there is no partial
    Home. Every sub-object is independently null when its loader failed, and null
    is never rendered as zero. Read-only, no CSRF, no writes. Auth: linked
    session (JSON 401)."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, active_account_id, row = gate
    _touch_last_session(active_account_id)

    sel = _selected_ctrl(request, active_account_id)
    # Independent reads, fanned out: serially awaited this is nine round trips
    # of latency on a phone. return_exceptions keeps one dead loader from
    # cancelling the eight that answered.
    parts = await asyncio.gather(
        _home_character(active_account_id, sel, row["character_name"]),
        _home_rewards(active_account_id),
        _home_guild(active_account_id),
        _home_landsraad(active_account_id),
        _home_mailbox(active_account_id),
        _home_wallet(active_account_id, sel),
        _home_orders(active_account_id),
        _home_caps(discord_id),
        _home_deliveries(active_account_id),
        return_exceptions=True,
    )
    (character, reward_pool, guild, landsraad, inbox, wallet, orders, caps,
     deliveries) = [
        _home_ok(label, value) for label, value in zip(_HOME_PARTS, parts)
    ]
    # private, no-store: this is per-player state behind a session cookie, and a
    # shared cache holding one player's sietch and wallet is the whole hazard.
    return JSONResponse(
        {
            "ok": True,
            "character": character,
            "rewards": reward_pool,
            "guild": guild,
            "landsraad": landsraad,
            "mailbox": inbox,
            "wallet": wallet,
            "orders": orders,
            "caps": caps,
            "deliveries": deliveries,
        },
        headers={"Cache-Control": "private, no-store"},
    )
