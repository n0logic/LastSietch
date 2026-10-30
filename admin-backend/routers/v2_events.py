"""Events Manager: the admin-panel surface for portal events (ADMIN-02).

The player portal owns the store (routers/portal_events.py: portal_events +
portal_event_reminders, validate/insert/update/cancel). This router is a second
door onto the SAME write functions, behind the admin panel's own login, so an
event written here is byte-for-byte what the portal board and the reminder loop
already read. Nothing here writes SQL against portal_events itself.

What it adds over the old in-portal composer:

  * TIMES ARE ENTERED IN EASTERN. The server clock and every announcement run on
    America/New_York; the browser composer took the operator's local time and a
    mistyped zone moved an event. `starts_et` / `ends_et` are converted here with
    zoneinfo, so DST is the library's problem, and the list shows UTC + Eastern.
  * HISTORY. Every write audits a before/after diff of the fields it changed,
    and /history reads those rows back for one event (including writes the
    portal composer made before it retired).
  * PREVIEW. The banner is served from the portal build through this host, since
    the admin CSP allows images from 'self' only.

Plan of record: the design notes (not published).
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import event_effects
from auth import audit_log, get_current_user, require_admin, require_csrf
from database import get_db
from routers import portal_events as store

router = APIRouter()
templates = Jinja2Templates(directory="templates")

API = "/api/dune/v2/events"
EASTERN = ZoneInfo("America/New_York")
HISTORY_LIMIT = 100
DIFF_FIELDS = ("title", "kind", "banner", "starts_utc", "ends_utc", "map_name",
               "host", "description", "status", "discord_sync", "effect", "effect_scope")


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


def eastern_to_utc(value):
    """'YYYY-MM-DDTHH:MM' (a datetime-local value, Eastern wall time) to the stored
    'YYYY-MM-DD HH:MM:SS' UTC, or None. Seconds are accepted and kept."""
    text = str(value or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=EASTERN)
        except ValueError:
            continue
        return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return None


def utc_to_eastern(stored):
    """Stored UTC to 'YYYY-MM-DDTHH:MM' Eastern (the edit form's value), or None."""
    moment = store._stamp(stored)
    if moment is None:
        return None
    return moment.astimezone(EASTERN).strftime("%Y-%m-%dT%H:%M")


def normalise_times(body):
    """Map the form's Eastern fields onto the store's UTC fields. An Eastern
    value that does not parse is an error, never a silent fallback to UTC."""
    body = dict(body or {})
    for et_key, utc_key, label in (("starts_et", "starts_utc", "start"), ("ends_et", "ends_utc", "end")):
        if et_key not in body:
            continue
        raw = str(body.pop(et_key) or "").strip()
        if not raw:
            body[utc_key] = ""
            continue
        converted = eastern_to_utc(raw)
        if converted is None:
            raise HTTPException(400, "The %s time must be a date and time in Eastern." % label)
        body[utc_key] = converted
    return apply_effect_window(body)


def apply_effect_window(body):
    """An effect event's end is not typed: the form sends a length from the
    closed set and the end is the start plus that many hours of Eastern wall
    time. Any other length is refused here, before the store sees it."""
    effect = str(body.get("effect") or "none").strip().lower()
    if effect == "none" or "effect_hours" not in body:
        body.pop("effect_hours", None)
        return body
    try:
        hours = int(body.pop("effect_hours"))
    except (TypeError, ValueError):
        hours = None
    if hours not in event_effects.WINDOW_HOURS:
        raise HTTPException(400, "Pick an effect length from the list.")
    if body.get("starts_utc"):
        body["ends_utc"] = event_effects.end_for(body["starts_utc"], hours)
    return body


def admin_view(row, reminders=0):
    """The contract object plus what only an operator needs."""
    event = store.public_event(row)
    event.update({
        "starts_et": utc_to_eastern(row["starts_utc"]),
        "ends_et": utc_to_eastern(row["ends_utc"]),
        "published_at": row["published_at"],
        "created_at": row["created_at"],
        "cancelled_at": row["cancelled_at"],
        "cancel_reason": row["cancel_reason"],
        "reminders": int(reminders),
        "discord_sync": bool(row["discord_sync"]),
        "discord_status": row["discord_status"],
        "discord_state": row["discord_state"],
        "discord_error": row["discord_error"],
        "discord_synced_at": row["discord_synced_at"],
        "effect_error": row["effect_error"],
        "effect_job": row["effect_job"],
        "effect_job_at": row["effect_job_at"],
        "effect_started_at": row["effect_started_at"],
        "effect_ended_at": row["effect_ended_at"],
        "effect_hours": event_effects.wall_hours(row["starts_utc"], row["ends_utc"])
        if row["effect"] not in (None, "none") else None,
    })
    return event


def list_admin_events():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT %s FROM portal_events ORDER BY starts_utc DESC LIMIT ?" % store._EVENT_COLUMNS,
            (store.ADMIN_LIST_LIMIT,)).fetchall()
        counts = dict(conn.execute(
            "SELECT event_id, COUNT(*) FROM portal_event_reminders GROUP BY event_id").fetchall())
    finally:
        conn.close()
    return [admin_view(r, counts.get(r["id"], 0)) for r in rows]


def event_history(event_id):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, username, action, details FROM audit_log"
            " WHERE target = ? AND action IN (%s) ORDER BY timestamp DESC, id DESC LIMIT ?"
            % ",".join("?" * len(store.ADMIN_ACTIONS)),
            [str(int(event_id))] + list(store.ADMIN_ACTIONS) + [HISTORY_LIMIT]).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            details = json.loads(r["details"] or "{}")
        except ValueError:
            details = {}
        out.append({"at": r["timestamp"], "by": r["username"], "action": r["action"], "details": details})
    return out


def diff(before, fields):
    changed = {}
    for key in DIFF_FIELDS:
        old = before[key] if before is not None else None
        new = fields.get(key)
        if (old or None) != (new or None):
            changed[key] = [old, new]
    return changed


def _row_or_404(event_id):
    eid = store._event_id(event_id)
    row = store.event_row(eid)
    if row is None:
        raise HTTPException(404, "That event does not exist.")
    return eid, row


async def _write_gate(request: Request):
    user = require_admin(request)
    require_csrf(request, user)
    if store.admin_debounce_remaining(user["username"]) > 0:
        raise HTTPException(429, "That was a moment ago. Try again in a few seconds.")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return user, body if isinstance(body, dict) else {}


def _details(details):
    return json.dumps(details, separators=(",", ":"))


# --- page -------------------------------------------------------------------

@router.get("/v2/players/events")
async def v2_events_page(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "v2/events.html", {
        "user": user, "current_tab": "players", "kinds": store.EVENT_KINDS,
        "effects": event_effects.EFFECTS, "scopes": event_effects.SCOPES,
        "windows": [(h, event_effects.WINDOW_LABELS[h]) for h in event_effects.WINDOW_HOURS],
        "banners": sorted(store.BANNERS), "limits": {
            "title": store.TITLE_MAX, "description": store.DESCRIPTION_MAX,
            "map": store.MAP_NAME_MAX, "host": store.HOST_MAX}})


# --- reads ------------------------------------------------------------------

@router.get(API)
async def v2_events_list(request: Request):
    require_admin(request)
    return {"events": list_admin_events()}


@router.get(API + "/banner/{slug}")
async def v2_events_banner(request: Request, slug: str):
    require_admin(request)
    if not store.valid_banner(slug):
        raise HTTPException(404, "No such banner.")
    return FileResponse(os.path.join(store.BANNER_DIR, slug + ".webp"), media_type="image/webp")


@router.get(API + "/{event_id}/history")
async def v2_events_history(request: Request, event_id: str):
    require_admin(request)
    eid, _row = _row_or_404(event_id)
    return {"history": event_history(eid)}


# --- writes -----------------------------------------------------------------

@router.post(API)
async def v2_events_create(request: Request):
    user, body = await _write_gate(request)
    fields, error, message = store.validate_event(normalise_times(body))
    if error:
        raise HTTPException(400, message)
    eid = store.insert_event(fields, "admin:%s" % user["username"])
    # Action names stay string literals: the Audit page scan offers them as filters.
    audit_log(user["id"], user["username"], "portal_event_create", str(eid), _ip(request), details=_details(
           {"event_id": eid, "status": fields["status"], "changed": diff(None, fields)}))
    row = store.event_row(eid)
    if fields["status"] == "published":
        await store.cross_post(store.public_event(row))
    return {"event": admin_view(row)}


def next_month(now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    return (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)


def existing_effect_starts():
    conn = get_db()
    try:
        # Cancelled rows count: a cancelled weekend is a decision, not a gap to refill.
        rows = conn.execute("SELECT starts_utc FROM portal_events WHERE effect IS NOT NULL"
                            " AND effect <> 'none'").fetchall()
    finally:
        conn.close()
    return {r["starts_utc"] for r in rows}


@router.post(API + "/generate")
async def v2_events_generate(request: Request):
    """Generate next month: the rotation's weekends as drafts. A weekend that
    already has an effect event starting at the same time (draft, published or
    cancelled) is skipped, so a second press creates nothing and a cancel sticks."""
    user, body = await _write_gate(request)
    month = str(body.get("month") or "").strip()
    if month:
        try:
            parsed = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise HTTPException(400, "The month must look like 2026-11.")
        year, mon = parsed.year, parsed.month
    else:
        year, mon = next_month()
    taken = existing_effect_starts()
    created, skipped = [], []
    for fields in event_effects.month_drafts(year, mon):
        if fields["starts_utc"] in taken:
            skipped.append(fields["starts_utc"])
            continue
        eid = store.insert_event(fields, "admin:%s" % user["username"])
        # Action names stay string literals: the Audit page scan offers them as filters.
        audit_log(user["id"], user["username"], "portal_event_create", str(eid), _ip(request), details=_details(
            {"event_id": eid, "status": "draft", "generated": "%04d-%02d" % (year, mon),
             "changed": diff(None, fields)}))
        created.append(admin_view(store.event_row(eid)))
    return {"month": "%04d-%02d" % (year, mon), "created": created, "skipped": skipped}


@router.post(API + "/{event_id}")
async def v2_events_update(request: Request, event_id: str):
    """Edit, publish (status=published) or unpublish (status=draft). A cancelled
    event is read-only: cancel is final, as it is for the players it notified."""
    user, body = await _write_gate(request)
    eid, row = _row_or_404(event_id)
    if row["status"] == "cancelled":
        raise HTTPException(409, "That event was cancelled and can no longer be edited.")
    fields, error, message = store.validate_event(normalise_times(body), existing=row)
    if error:
        raise HTTPException(400, message)
    changed = diff(row, fields)
    rearmed = store.rearm_needed(row["starts_utc"], fields["starts_utc"])
    try:
        store.update_event(eid, fields, row)
    except store.EffectRaced as exc:
        raise HTTPException(409, str(exc))
    # Action names stay string literals: the Audit page scan offers them as filters.
    audit_log(user["id"], user["username"], "portal_event_update", str(eid), _ip(request), details=_details(
           {"event_id": eid, "status": fields["status"], "rearmed": rearmed, "changed": changed}))
    fresh = store.event_row(eid)
    if fields["status"] == "published" and row["status"] != "published":
        await store.cross_post(store.public_event(fresh))
    return {"event": admin_view(fresh), "rearmed": rearmed}


@router.post(API + "/{event_id}/cancel")
async def v2_events_cancel(request: Request, event_id: str):
    user, body = await _write_gate(request)
    eid, row = _row_or_404(event_id)
    if str(body.get("confirm") or "").strip() != "CANCEL":
        raise HTTPException(400, "Type CANCEL to confirm. Everyone holding a reminder is told.")
    if row["status"] == "cancelled":
        return {"event": admin_view(row), "notified": 0}
    reason = store.clean_text(body.get("reason"))[:store.DESCRIPTION_MAX] or None
    if not reason:
        raise HTTPException(400, "Give a reason. Players see it on the event page.")
    account_ids = store.cancel_event(eid, reason)
    fresh = store.event_row(eid)
    notified = store.notify_cancelled(account_ids, store.public_event(fresh))
    # Action names stay string literals: the Audit page scan offers them as filters.
    audit_log(user["id"], user["username"], "portal_event_cancel", str(eid), _ip(request), details=_details(
           {"event_id": eid, "reminders": len(account_ids), "notified": notified,
            "changed": {"status": [row["status"], "cancelled"], "cancel_reason": [None, reason]}}))
    return {"event": admin_view(fresh), "notified": notified}
