"""Player requests queue, admin side (admin plan F7).

The staff surface over player_requests.py: a filtered list, a detail drawer to set
status, staff note, player note and assignee, and a "log a request" form for asks
that arrived by Discord DM. Every write is admin-only, CSRF-gated and audited with
audit_log, targeted 'player_request:<id>', and the drawer's history is read back
from those rows. Fulfilment is NOT here: each type links to the tool that does it,
which keeps its own gate. Nothing here touches the game DB.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import player_requests as store
from auth import audit_log, get_current_user, require_admin, require_csrf

router = APIRouter()
templates = Jinja2Templates(directory="templates")

API = "/api/dune/v2/requests"

_MESSAGES = {
    "bad_type": "Pick a request type.",
    "bad_status": "That is not a request status.",
    "need_totem": "This type needs the base (totem) it is about.",
    "need_vehicle": "This type needs the vehicle it is about.",
    "need_player": "Enter the player's character name.",
    "bad_assignee": "Assign to an admin-panel account, or nobody.",
    "decline_needs_note": "A decline needs a note for the player saying why.",
}


def _admin_or_redirect(request: Request):
    """HTML page guard, same shape as routers/v2.py (repeated, not imported)."""
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _bad(token):
    raise HTTPException(400, _MESSAGES.get(token, "That request could not be saved."))


def _request_id(raw) -> int:
    text = str(raw or "")
    if not text.isdigit() or len(text) > 12:
        raise HTTPException(404, "No such request.")
    return int(text)


async def _write_gate(request: Request):
    user = require_admin(request)
    require_csrf(request, user)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return user, body if isinstance(body, dict) else {}


# --- page -------------------------------------------------------------------

@router.get("/v2/players/requests")
async def v2_requests_page(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "v2/requests.html", {
        "user": user, "current_tab": "players",
        "types": [(k, v["label"], v["ref"]) for k, v in store.TYPES.items()],
        "statuses": store.STATUSES,
        "limits": {"ref": store.REF_MAX, "details": store.DETAILS_MAX, "note": store.NOTE_MAX,
                   "name": store.NAME_MAX}})


# --- reads ------------------------------------------------------------------

@router.get(API)
async def v2_requests_list(request: Request, status: str = "open", type: str = "",
                           assignee: str = "", q: str = ""):
    require_admin(request)
    try:
        rows = store.list_requests(status=status, kind=type or None, assignee=assignee or None, q=q)
    except ValueError as exc:
        _bad(str(exc))
    return {"requests": rows, "counts": store.counts(), "staff": store.staff_names()}


@router.get(API + "/{request_id}")
async def v2_requests_detail(request: Request, request_id: str):
    require_admin(request)
    rid = _request_id(request_id)
    row = store.get(rid)
    if row is None:
        raise HTTPException(404, "No such request.")
    return {"request": store.admin_row(row), "history": store.history(rid),
            "staff": store.staff_names()}


# --- writes -----------------------------------------------------------------

@router.post(API)
async def v2_requests_log(request: Request):
    """Staff log a request that arrived outside the portal (a Discord DM)."""
    user, body = await _write_gate(request)
    fields, error = store.validate_new(body)
    if error:
        _bad(error)
    try:
        rid = store.create(fields, "staff", body.get("char_name"))
    except ValueError as exc:
        _bad(str(exc))
    audit_log(user["id"], user["username"], "player_request_log", store.audit_target(rid), _ip(request),
              store.details_json({"type": fields["type"], "source": "staff"}))
    return {"ok": True, "request": store.admin_row(store.get(rid))}


@router.post(API + "/{request_id}")
async def v2_requests_update(request: Request, request_id: str):
    user, body = await _write_gate(request)
    rid = _request_id(request_id)
    changes = {k: body[k] for k in store.EDITABLE if k in body}
    try:
        row, diff = store.update(rid, changes, expected_updated_at=body.get("updated_at"))
    except LookupError:
        raise HTTPException(404, "No such request.")
    except RuntimeError:
        raise HTTPException(409, "Someone else saved this request since you opened it. Reopen it and try again.")
    except ValueError as exc:
        _bad(str(exc))
    if diff:
        audit_log(user["id"], user["username"], "player_request_update", store.audit_target(rid),
                  _ip(request), store.details_json(diff))
    return {"ok": True, "changed": sorted(diff), "request": row, "history": store.history(rid)}
