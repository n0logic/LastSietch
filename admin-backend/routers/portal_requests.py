"""Player requests, portal side (portal plan D3, admin plan F7).

The portal only files and reads: POST /portal/requests files one request for the
signed-in player, GET /portal/requests lists that player's own rows and their
status. Triage, notes and fulfilment all happen in the admin panel
(routers/v2_requests.py). Rows are keyed on the portal identity, so a player only
ever reads what they filed, and the player projection (player_requests.player_row)
never carries the assignee or the staff note.

Dark by default: LASTSIETCH_PORTAL_REQUESTS_ENABLED (default off). The session is checked
before the flag, so a dark feature never tells an anonymous caller anything.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import feature_flags
import player_requests as store
from portal_auth import client_ip
from routers.portal import (
    _require_linked_session_json,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()

FLAG = "LASTSIETCH_PORTAL_REQUESTS_ENABLED"

_TEXT = {
    "requests_disabled": "Requests are not open yet.",
    "bad_type": "Pick what you are asking for.",
    "need_totem": "Tell us which base this is about: the totem name or where it stands.",
    "need_vehicle": "Tell us which vehicle this is about and where you last had it.",
    "too_many_open": "You have %d requests open. Staff will get to them; you can send another once one closes."
                     % store.PLAYER_OPEN_MAX,
    "rate_limited": "That is a lot of requests for one day. Try again tomorrow.",
    "duplicate": "You already have this request open. Its status is below.",
    "csrf": "Your session expired. Reload the page and try again.",
}


def _enabled() -> bool:
    return feature_flags.enabled(FLAG, "0")


def _err(token, status=200):
    return JSONResponse({"ok": False, "error": token, "message": _TEXT.get(token, "That request could not be sent.")},
                        status_code=status)


def _audit(discord_id, request_id, ip, details):
    """auth is imported at call time, as in portal_chat_mod. A failed audit write is
    logged, never turned into an error after the row already committed."""
    try:
        from auth import audit_log
        audit_log(None, "portal:%s" % discord_id, "player_request_create", store.audit_target(request_id),
                  ip, details=store.details_json(details), success=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal requests: audit write failed for %s: %s", request_id, exc)


@router.get("/portal/requests")
async def portal_requests_list(request: Request):
    """The signed-in player's own requests, newest first. Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    if not _enabled():
        return _err("requests_disabled")
    _session, discord_id, _account_id, _row = gate
    return _v2_ok({"requests": store.player_requests_for(discord_id),
                   "types": [{"type": k, "label": v["player_label"], "ref": v["ref"]}
                             for k, v in store.TYPES.items()]})


@router.post("/portal/requests")
async def portal_requests_create(request: Request):
    """File one request for the signed-in player. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    if not _enabled():
        return _err("requests_disabled")
    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", _TEXT["csrf"], status=403)
    _session, discord_id, account_id, link = gate

    fields, error = store.validate_new(body)
    if error:
        return _err(error)
    brake = store.player_brake(discord_id, fields)
    if brake:
        return _err(brake)
    # A link row can predate character names; staff still need a handle to read.
    name = link["character_name"] or "account %d" % int(account_id)
    request_id = store.create(fields, "portal", name, discord_id=discord_id, account_id=account_id)
    _audit(discord_id, request_id, client_ip(request),
           {"type": fields["type"], "account_id": int(account_id)})
    return _v2_ok({"request": store.player_row(store.get(request_id))})
