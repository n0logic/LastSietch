"""Minted portal sessions for the QA harness (harness phase 1, lane A).

The browser harness has to sign in as a real linked player to exercise the
LIVE-PLAYER-QA rows, and driving Discord's OAuth consent screen from a headless
chromium is neither repeatable nor something we want a test run doing to a real
identity. This route mints the same session the OAuth callback mints, from the
same helpers, and hands the cookie attributes back so the harness can set an
identical cookie in the browser.

That makes it the single most dangerous surface in the portal, so it is built to
be INVISIBLE rather than merely refused:

  * TWO GATES ANSWER A BARE 404, never 401 and never 403. An env gate that is
    off, or a caller that is not pure loopback, gets exactly what a path with no
    route behind it gets, down to the body: HTTPException(404) renders through
    the app's own handler. Nothing about this route can be probed from outside.
  * EVERY OTHER VERB ANSWERS THE SAME 404. A path that is registered for POST
    alone answers 405 with an `Allow: POST` header to a GET, which is an
    existence oracle that no amount of gating on the POST would close. The
    second handler below takes the rest of the verbs and refuses them the same
    way, and neither handler is in the OpenAPI schema.
  * PURE LOOPBACK IS portal_auth.is_test_run's RULE, COPIED. No x-forwarded-for
    header at all AND a loopback peer. Caddy sets that header on every request it
    proxies, so an edge caller can never satisfy this even if the Caddy deny in
    ops/caddy/Caddyfile.<web-host> were missing. Two independent layers, neither
    depending on the other.
  * THE CALLER CANNOT CHOOSE AN IDENTITY. The body names an account_id only. The
    discord_id it is minted for comes from LASTSIETCH_QA_IDENTITIES, so the set of
    identities this route can ever issue is whatever the operator put in the
    environment, and a stolen key still cannot mint a session for a player who is
    not on that list.
  * THE LINK IS READ, NOT ASSUMED. An allowlisted pair still needs an unrevoked
    ls_account_links row, so a session is never minted for a link the player has
    since revoked.

Env, all three set by the operator on the box and never by this lane:
  LASTSIETCH_QA_SESSIONS=1                    the gate. Read off os.environ directly,
                                       never through feature_flags: the override
                                       file that helper consults first is written
                                       by an owner-facing panel, and no panel
                                       should be able to open this.
  LASTSIETCH_QA_KEY=<32+ random bytes hex>    compared with hmac.compare_digest.
  LASTSIETCH_QA_IDENTITIES=<did>:<aid>,...    the identity allowlist.

Never logged and never echoed: the key, any cookie value, and the discord_id.
The audit row carries the account and the harness run tag and nothing else.
"""
import hmac
import json
import logging
import os
import re
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import audit_log
from config import PORTAL_SESSION_IDLE_MAX_AGE
from database import get_db
from portal_auth import (
    COOKIE_PATH,
    CSRF_COOKIE,
    SELCHAR_COOKIE,
    SESSION_COOKIE,
    csrf_for_session,
    issue_selchar_cookie,
    issue_session_cookie,
)

logger = logging.getLogger("portal")
router = APIRouter()

GATE = "LASTSIETCH_QA_SESSIONS"
LOOPBACK = {"127.0.0.1", "::1"}
# Every session this route mints is stamped with the loopback address, because
# that is the peer that actually asked for it. A harness session is therefore
# distinguishable from a player's in the audit trail by its ip alone.
MINT_IP = "127.0.0.1"
MINT_LIMIT = 30
MINT_WINDOW_S = 60.0
RUN_ID_MAX = 64

# One identity entry: <discord_id>:<account_id>, both decimal. Anything else is
# dropped rather than guessed at.
_IDENTITY_RE = re.compile(r"^(\d+):(\d+)$")
# The harness's own run tag lands in an audit row, so it is reduced to the
# characters a tag can hold before it is stored.
_RUN_ID_STRIP_RE = re.compile(r"[^A-Za-z0-9:._-]")

# Monotonic stamps of the requests that got past both invisibility gates.
_MINTS = deque()
_IDENTITIES_WARNED = False


# --- gates -------------------------------------------------------------------

def _gate_open() -> bool:
    """The env gate, read STRAIGHT off the process environment.

    Deliberately not feature_flags.enabled: that consults
    data/feature_flags.json before the environment, and the override file is
    written by an owner-facing panel. A gate that can mint a player's session
    must be settable by nothing but the service unit's own environment, so this
    reads os.environ and stops there."""
    return (os.environ.get(GATE) or "").strip() == "1"


def _pure_loopback(request: Request) -> bool:
    """portal_auth.is_test_run's trusted-caller rule, copied exactly: NO
    x-forwarded-for header at all, and a loopback peer."""
    if request.headers.get("x-forwarded-for", ""):
        return False
    host = request.client.host if request.client else ""
    return host in LOOPBACK


def _rate_limited(now=None) -> bool:
    """30 per minute, in-process, over a deque of monotonic stamps.

    It counts every request that got past both invisibility gates, not only the
    ones that went on to mint. A cap that only counted successes would let a
    harness stuck in a retry loop hammer the key comparison and the identity
    read forever, which is the runaway this exists to stop."""
    now = time.monotonic() if now is None else now
    while _MINTS and (now - _MINTS[0]) > MINT_WINDOW_S:
        _MINTS.popleft()
    if len(_MINTS) >= MINT_LIMIT:
        return True
    _MINTS.append(now)
    return False


def _key_ok(provided) -> bool:
    """Constant-time compare against LASTSIETCH_QA_KEY. An unset key refuses everything,
    so a box that never configured this cannot be talked into minting. The
    expected value is stripped because an env file trailing a newline is an
    operator typo, not an authorisation decision; what the caller sent is
    compared exactly as sent."""
    expected = (os.environ.get("LASTSIETCH_QA_KEY") or "").strip()
    if not expected or not isinstance(provided, str) or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _identities() -> dict:
    """account_id -> discord_id, parsed from LASTSIETCH_QA_IDENTITIES.

    Defensive on purpose: surrounding whitespace is trimmed, empty items are
    skipped, and an item that is not two decimal numbers is dropped. The count
    of dropped items is logged ONCE at warning and their text never is, because
    a malformed entry still holds a real discord id."""
    global _IDENTITIES_WARNED
    out, skipped = {}, 0
    for item in (os.environ.get("LASTSIETCH_QA_IDENTITIES") or "").split(","):
        item = item.strip()
        if not item:
            continue
        found = _IDENTITY_RE.match(item)
        if not found:
            skipped += 1
            continue
        out[int(found.group(2))] = found.group(1)
    if skipped and not _IDENTITIES_WARNED:
        _IDENTITIES_WARNED = True
        logger.warning("portal_qa: LASTSIETCH_QA_IDENTITIES has %d unusable entries, "
                       "ignored (their text is never logged)", skipped)
    return out


def _character_name(discord_id: str, account_id: int):
    """The character on an UNREVOKED link for this pair, or None. Nothing else
    off the row leaves this function."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT character_name FROM ls_account_links
                WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL
                LIMIT 1""",
            (str(discord_id), int(account_id)),
        ).fetchone()
    finally:
        conn.close()
    return row["character_name"] if row else None


# --- body + response shaping -------------------------------------------------

async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _as_int(value):
    """A positive integer, or None. A bool is refused before int() sees it:
    JSON true would otherwise resolve to account 1."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _run_id(value) -> str:
    if not isinstance(value, str):
        return ""
    return _RUN_ID_STRIP_RE.sub("", value.strip())[:RUN_ID_MAX]


def _cookie(name: str, value: str, http_only: bool) -> dict:
    """One cookie described exactly as portal_auth.set_cookie would set it, so
    the harness can put an identical one in the browser: path /portal, the 7 day
    idle window, secure, samesite lax."""
    return {
        "name": name,
        "value": value,
        "path": COOKIE_PATH,
        "max_age": PORTAL_SESSION_IDLE_MAX_AGE,
        "secure": True,
        "http_only": http_only,
        "same_site": "lax",
    }


def _envelope(payload: dict, status_code: int) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


# --- route -------------------------------------------------------------------

# Every OTHER verb on the same path. Without this, Starlette matches the path,
# finds no handler for the method and answers 405 with an `Allow: POST` header,
# which tells a prober both that the path exists and what it wants. This raises
# the SAME bare HTTPException(404) Starlette raises for a path that matches
# nothing at all, so every verb on this path is answered identically.
#
# include_in_schema=False on both: the app serves /openapi.json, and admin's
# `handle /admin/*` strips its prefix straight into the app, so a route in the
# schema is a route anyone can enumerate.
@router.api_route("/portal/qa/session", include_in_schema=False,
                  methods=["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
async def portal_qa_session_other_verbs(request: Request):
    raise HTTPException(status_code=404)


@router.post("/portal/qa/session", include_in_schema=False)
async def portal_qa_session(request: Request):
    """Mint a portal session for an allowlisted account. See the module
    docstring for the gate order and why the first two answer 404."""
    if not _gate_open():
        raise HTTPException(status_code=404)
    if not _pure_loopback(request):
        raise HTTPException(status_code=404)
    if _rate_limited():
        return _envelope({"ok": False, "error": "rate_limited"}, 429)

    body = await _read_body(request)
    if not _key_ok(body.get("key")):
        return _envelope({"ok": False, "error": "forbidden"}, 403)

    account_id = _as_int(body.get("account_id"))
    discord_id = _identities().get(account_id) if account_id else None
    if not discord_id:
        return _envelope({"ok": False, "error": "unknown_account"}, 404)

    character_name = _character_name(discord_id, account_id)
    if not character_name:
        return _envelope({"ok": False, "error": "unlinked"}, 404)

    session_value = issue_session_cookie(discord_id, account_id, MINT_IP)
    csrf = csrf_for_session(session_value)
    cookies = [_cookie(SESSION_COOKIE, session_value, True),
               _cookie(CSRF_COOKIE, csrf, False)]
    controller_id = _as_int(body.get("controller_id"))
    if controller_id:
        cookies.append(_cookie(SELCHAR_COOKIE,
                               issue_selchar_cookie(account_id, controller_id),
                               True))

    run_id = _run_id(body.get("run_id"))
    audit_log(None, "qa-harness", "portal_qa_session", "account:%d" % account_id,
              MINT_IP, details=json.dumps({"run_id": run_id}) if run_id else None)
    logger.info("portal_qa: minted a session for account %s (%d cookies)",
                account_id, len(cookies))
    return _envelope({"ok": True, "account_id": account_id,
                      "character_name": character_name,
                      "csrf": csrf, "cookies": cookies}, 200)
