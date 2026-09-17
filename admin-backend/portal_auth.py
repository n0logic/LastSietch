"""Portal session + CSRF helpers (Discord OAuth flow, P4).

Two parallel cookie-based auth surfaces cohabit on this FastAPI service:
admin (path=/admin, ls_session) and portal (path=/, ls_portal_session).
Admin's path scoping keeps its cookie off every portal route; the portal cookie
went to path=/ in wave 13a because the SPA now serves pages at the root of
portal.lastsietch.com (see COOKIE_PATH below).

Three signed payloads exist in P4:
1. state_token — issued at /portal/login, verified at /portal/oauth/callback.
   itsdangerous max_age=600. Carries OAuth nonce + return_to.
2. link_flow cookie — issued at oauth/callback if no existing link found,
   carries discord_id + discord_handle into the picker/quiz flow.
   max_age=300.
3. session cookie — issued after quiz pass. Carries discord_id + account_id.
   max_age=7d idle (cookie), absolute timeout 30d enforced server-side.

All three use itsdangerous.URLSafeTimedSerializer; secrets live in env vars.
State and session secrets are separate (different rotation cadences).

Never log full cookie values, state tokens, or client_secret.
"""
import hashlib
import hmac
import logging
import time
from typing import Optional
import portal_identity

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import (
    PORTAL_LINK_FLOW_MAX_AGE,
    PORTAL_OAUTH_STATE_MAX_AGE,
    PORTAL_OAUTH_STATE_SECRET,
    PORTAL_SESSION_ABS_MAX_AGE,
    PORTAL_SESSION_IDLE_MAX_AGE,
    PORTAL_SESSION_SECRET,
)

logger = logging.getLogger(__name__)

SESSION_COOKIE = "ls_portal_session"
CSRF_COOKIE = "ls_portal_csrf"
LINK_FLOW_COOKIE = "ls_portal_link_flow"
SELCHAR_COOKIE = "ls_portal_selchar"
# "/" since wave 13a, not "/portal". The SPA reads ls_portal_csrf out of
# document.cookie (api.js, LinkedAccountsPanel), and on portal.lastsietch.com its
# pages live at the root: a cookie scoped to /portal is simply not visible to a
# document at /karum, so every state-changing call from those pages would go out
# without a CSRF token. Admin cookies stay on path=/admin, so the two surfaces
# still do not see each other.
COOKIE_PATH = "/"
# Where these cookies lived BEFORE wave 13a. delete_cookie only clears a cookie
# at the exact path it names, so a browser still holding a pre-deploy
# path=/portal cookie would keep it through every logout, and a rollback would
# leave it holding two. Logout names both paths until no session minted before
# the move can still be alive (PORTAL_SESSION_ABS_MAX_AGE, 30 days), after which
# this and its use in clear_cookie can go.
LEGACY_COOKIE_PATH = "/portal"
CSRF_HEADER = "X-Portal-CSRF-Token"

# httponly per cookie, mirroring how each one is SET below. A tombstone whose
# attributes differ from the cookie it replaces is not reliably a deletion: a
# browser matches on name, domain and path, and a Secure cookie is not replaced
# by a non-Secure one over the same connection in every implementation. The
# safe rule is to spell the deletion exactly like the set.
_COOKIE_HTTPONLY = {
    SESSION_COOKIE: True,
    CSRF_COOKIE: False,      # the page reads this one out of document.cookie
    LINK_FLOW_COOKIE: True,
    SELCHAR_COOKIE: True,
}

# Salt strings namespace the same secret across different token kinds — even
# if a state secret leaks, it cannot mint a link_flow or session token.
_SALT_STATE = "portal-oauth-state"
_SALT_LINK_FLOW = "portal-link-flow"
_SALT_SESSION = "portal-session"
_SALT_PROFILE_SESSION = "portal-profile-session-v1"
_SALT_PICK = "portal-pick"
_SALT_ATTEMPT = "portal-attempt"
_SALT_SELCHAR = "portal-selchar"


def _state_signer() -> URLSafeTimedSerializer:
    if not PORTAL_OAUTH_STATE_SECRET:
        raise RuntimeError("PORTAL_OAUTH_STATE_SECRET not configured")
    return URLSafeTimedSerializer(PORTAL_OAUTH_STATE_SECRET, salt=_SALT_STATE)


def _session_signer(salt: str) -> URLSafeTimedSerializer:
    if not PORTAL_SESSION_SECRET:
        raise RuntimeError("PORTAL_SESSION_SECRET not configured")
    return URLSafeTimedSerializer(PORTAL_SESSION_SECRET, salt=salt)


# --- OAuth state token (issued /portal/login, verified /portal/oauth/callback) ---

def issue_state_token(nonce: str, return_to: str) -> str:
    return _state_signer().dumps({"n": nonce, "r": return_to})


def verify_state_token(token: str) -> Optional[dict]:
    """Returns payload dict on success; None on bad/expired signature.
    Caller must distinguish 'bad' vs 'expired' by re-running with split."""
    try:
        return _state_signer().loads(token, max_age=PORTAL_OAUTH_STATE_MAX_AGE)
    except SignatureExpired:
        return None
    except BadSignature:
        return None


def verify_state_token_split(token: str) -> tuple[Optional[dict], str]:
    """Returns (payload | None, status) where status in {'ok','expired','bad'}.
    Useful when the route wants to render error_state_expired vs a generic 400."""
    signer = _state_signer()
    try:
        payload = signer.loads(token, max_age=PORTAL_OAUTH_STATE_MAX_AGE)
        return payload, "ok"
    except SignatureExpired:
        return None, "expired"
    except BadSignature:
        return None, "bad"


# --- Link-flow cookie (between oauth/callback and quiz pass) ---

def issue_link_flow(discord_id: str, discord_handle: str, relink: bool = False,
                    add: bool = False, auth_time=None) -> str:
    payload = {"did": discord_id, "dh": discord_handle, "iat": int(time.time())}
    if auth_time is not None:
        payload['auth_at'] = int(auth_time)
    if relink:
        # Marks a self-serve relink (player already has a session). On quiz pass
        # the link router revokes the player's OTHER active links so a relink
        # leaves exactly one active character (e.g. clears a now-deleted char).
        payload["rl"] = 1
    if add:
        # Marks a multi-account ADD (player already has a session + >=1 verified
        # link). The link router keeps existing links intact and, when
        # MULTIACCOUNT_REQUIRE_QUIZ=0, skips the ownership quiz for the added
        # account (still refusing any already-linked account).
        payload["md"] = "add"
    return _session_signer(_SALT_LINK_FLOW).dumps(payload)


def verify_link_flow(token: str) -> Optional[dict]:
    try:
        return _session_signer(_SALT_LINK_FLOW).loads(
            token, max_age=PORTAL_LINK_FLOW_MAX_AGE
        )
    except (SignatureExpired, BadSignature):
        return None


# --- Pick token (between picker render and select submit) ---

def issue_pick_token(discord_id: str, account_id: int, character_name: str) -> str:
    return _session_signer(_SALT_PICK).dumps(
        {"did": discord_id, "aid": account_id, "cn": character_name, "iat": int(time.time())}
    )


def verify_pick_token(token: str) -> Optional[dict]:
    try:
        # Same window as link_flow (5 min) — picker is a single short-lived step.
        return _session_signer(_SALT_PICK).loads(token, max_age=PORTAL_LINK_FLOW_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None


# --- Attempt token (between quiz render and quiz submit; carries attempt row id) ---

def issue_attempt_token(attempt_id: int, discord_id: str, account_id: int) -> str:
    return _session_signer(_SALT_ATTEMPT).dumps(
        {"a": attempt_id, "did": discord_id, "aid": account_id, "iat": int(time.time())}
    )


def verify_attempt_token(token: str) -> Optional[dict]:
    try:
        # 10-min quiz window; players can't read the quiz forever.
        return _session_signer(_SALT_ATTEMPT).loads(token, max_age=PORTAL_OAUTH_STATE_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None


# --- Portal session cookie ---

def _legacy_profile_context(discord_id, account_id):
    try:
        from database import get_db
        conn = get_db()
        try:
            return portal_identity.legacy_session_context(conn, discord_id, account_id)
        finally:
            conn.close()
    except Exception:
        return None


def issue_session_cookie(discord_id: str, account_id: int, ip: str, *, issued_at=None, profile_bound=False, expected_profile=None, authentication_time=None) -> str:
    now = int(time.time())
    issued_at = now if issued_at is None else issued_at
    if type(issued_at) is not int or not 0 < issued_at <= now or now - issued_at > PORTAL_SESSION_ABS_MAX_AGE:
        raise portal_identity.ProfileMigrationError('session_issue_time_invalid')
    payload = {'did': discord_id, 'aid': account_id, 'iat': issued_at, 'ip': ip or ''}
    if authentication_time is not None:
        import config
        if getattr(config, 'PORTAL_AUTH_ENABLED', False):
            from database import get_db
            from portal_credentials import create_session
            conn = get_db()
            try:
                context = portal_identity.legacy_session_context(conn, discord_id, account_id)
                if context is None or not 0 <= now - authentication_time <= PORTAL_LINK_FLOW_MAX_AGE:
                    raise portal_identity.ProfileMigrationError('profile_session_unavailable')
                return create_session(conn, context['pid'], 'discord', discord_id, auth_at=authentication_time, account_id=account_id)
            finally:
                conn.close()
    salt = _SALT_SESSION
    if profile_bound or portal_identity.enforcement_required():
        context = _legacy_profile_context(discord_id, account_id)
        if context is None:
            raise portal_identity.ProfileMigrationError('profile_session_unavailable')
        if expected_profile is not None and context['pid'] != expected_profile:
            raise portal_identity.ProfileMigrationError('profile_session_identity_changed')
        payload.update(context)
        salt = _SALT_PROFILE_SESSION
    return _session_signer(salt).dumps(payload)


def issue_switched_session_cookie(session, account_id, ip):
    if session.get('sid'):
        from database import get_db
        from portal_credentials import switch_session
        conn = get_db()
        try:
            return switch_session(conn, session, account_id)
        finally:
            conn.close()
    bound = portal_identity.enforcement_required() or 'pid' in session
    if bound:
        return issue_session_cookie(session['did'], account_id, ip,
                                    issued_at=session['iat'], profile_bound=True,
                                    expected_profile=session.get('pid'))
    return issue_session_cookie(session['did'], account_id, ip)


def verify_session_cookie(token: str) -> Optional[dict]:
    """Idle-window check (7d). Returns None on bad/expired.
    Absolute-30d check is enforced by the caller against payload['iat']."""
    if isinstance(token, str) and token.startswith('ls3_'):
        try:
            from database import get_db
            from portal_credentials import session
            conn = get_db()
            try:
                return session(conn, token)
            finally:
                conn.close()
        except Exception:
            return None
    for salt in (_SALT_PROFILE_SESSION, _SALT_SESSION):
        try:
            payload = _session_signer(salt).loads(token, max_age=PORTAL_SESSION_IDLE_MAX_AGE)
            break
        except SignatureExpired:
            return None
        except BadSignature:
            continue
    else:
        return None
    if not isinstance(payload, dict):
        return None
    issued_at = payload.get('iat')
    now = int(time.time())
    if type(issued_at) is not int or not 0 < issued_at <= now or now - issued_at > PORTAL_SESSION_ABS_MAX_AGE:
        return None
    bound = salt == _SALT_PROFILE_SESSION
    if not bound and ('pid' in payload or 'lv' in payload):
        return None
    if bound or portal_identity.enforcement_required():
        context = _legacy_profile_context(payload.get('did'), payload.get('aid'))
        if context is None:
            return None
        if bound:
            if payload.get('pid') != context['pid'] or type(payload.get('lv')) is not int or payload['lv'] != context['lv']:
                return None
        elif context['lv'] != 1:
            return None
        return {**payload, **context}
    return payload


# --- Selected-character cookie (portal multi-character switcher) ---
# Carries the controller_id of the character the player is currently acting as,
# bound to their account_id. Signed so it cannot be forged, but the backend
# ALSO re-validates the controller against the account's live character list on
# every write, so a stale/forged value can only ever fail safe to the default
# pick. Kept separate from the session cookie so switching characters never
# resets the session's absolute-timeout clock.

def issue_selchar_cookie(account_id: int, controller_id: int) -> str:
    return _session_signer(_SALT_SELCHAR).dumps(
        {"aid": int(account_id), "ctrl": int(controller_id), "iat": int(time.time())}
    )


def verify_selchar_cookie(token: str, account_id: int) -> Optional[int]:
    """Returns the selected controller_id iff the cookie is valid AND bound to
    this account_id; otherwise None (caller falls back to the default pick)."""
    try:
        payload = _session_signer(_SALT_SELCHAR).loads(
            token, max_age=PORTAL_SESSION_IDLE_MAX_AGE
        )
    except (SignatureExpired, BadSignature):
        return None
    if int(payload.get("aid", -1)) != int(account_id):
        return None
    ctrl = payload.get("ctrl")
    try:
        return int(ctrl) if ctrl is not None else None
    except (TypeError, ValueError):
        return None


def get_portal_session(request: Request) -> Optional[dict]:
    """Convenience: returns session payload dict if cookie is present + valid,
    else None. Does NOT update last_session_at — caller does that to avoid
    forcing a DB write on lightweight reads like /portal/me."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_session_cookie(token)


# --- CSRF (per-session HMAC) ---

def csrf_for_session(session_cookie_value: str) -> str:
    """HMAC-SHA256(PORTAL_SESSION_SECRET, session_cookie_value). Stable across
    requests for a given session; rotates when the session does."""
    if not PORTAL_SESSION_SECRET:
        raise RuntimeError("PORTAL_SESSION_SECRET not configured")
    return hmac.new(
        PORTAL_SESSION_SECRET.encode("utf-8"),
        session_cookie_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def csrf_for_link_flow(link_flow_cookie_value: str) -> str:
    """Same HMAC pattern, but for the pre-session link-flow surface so the
    picker/quiz POSTs are CSRF-protected before the player has a session."""
    if not PORTAL_SESSION_SECRET:
        raise RuntimeError("PORTAL_SESSION_SECRET not configured")
    return hmac.new(
        PORTAL_SESSION_SECRET.encode("utf-8"),
        ("link-flow:" + link_flow_cookie_value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_csrf(provided: str, expected: str) -> bool:
    """Constant-time compare. Empty/None inputs always fail."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


# --- Cookie helpers (apply to FastAPI Response) ---

def set_cookie(response, name: str, value: str, *, max_age: int, httponly: bool = True):
    """Centralized cookie set with portal's standard flags."""
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path=COOKIE_PATH,
        httponly=httponly,
        secure=True,
        samesite="lax",
    )


def clear_cookie(response, name: str):
    for path in (COOKIE_PATH, LEGACY_COOKIE_PATH):
        response.delete_cookie(name, path=path, secure=True,
                               httponly=_COOKIE_HTTPONLY.get(name, True),
                               samesite="lax")


def client_ip(request: Request) -> str:
    """The RIGHTMOST X-Forwarded-For entry, which is the hop Caddy appended and
    therefore the peer it actually saw. The leftmost entry is whatever the
    client sent: Caddy appends rather than replaces, so trusting the left end
    let a caller pick their own rate-limit bucket and their own audit ip by
    sending a header. Caddy now overwrites the header with {remote_host}
    (ops/caddy/Caddyfile.<web-host>), which makes the two ends the same value;
    the right end is the one that stays correct either way. Falls back to
    request.client.host, the loopback peer, when there is no header at all."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else ""


def is_test_run(request: Request) -> bool:
    """Smoke-test isolation gate. True iff ?test=1 AND caller is trusted:
    either pure-localhost (no X-Forwarded-For + request.client.host in
    {127.0.0.1, ::1}) OR carries a valid admin ls_session cookie.
    Rows flagged is_test_run=1 are excluded from rate-limit SELECTs so QA's
    smoke runs do not lock out real users."""
    if request.query_params.get("test", "") != "1":
        return False
    if not request.headers.get("x-forwarded-for", ""):
        host = request.client.host if request.client else ""
        if host in {"127.0.0.1", "::1"}:
            return True
    token = request.cookies.get("ls_session", "")
    if not token:
        return False
    try:
        from database import get_db
        conn = get_db()
        try:
            row = conn.execute(
                """SELECT u.role FROM sessions s JOIN users u ON s.user_id = u.id
                   WHERE s.token = ? AND s.expires_at > datetime('now')
                     AND u.is_active = 1""",
                (token,),
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row["role"] == "admin")
    except Exception:
        return False
