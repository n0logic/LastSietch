"""Portal events -> Discord scheduled events: the desired-state side of the sync.

Cielago (same box) polls /_internal/events/discord/pending, makes Discord match,
and acks. This module decides what Discord SHOULD show and records what it does
show; it never calls Discord. Plan: the design notes (not published)
WEEKEND-EVENTS-PLAN-2026-10-23.md (stage 2) and EVENTS-MANAGER-PLAN-2026-09-28.md.

The desired Discord status is derived from the event and the clock, never stored:
published + before start = scheduled, started = active, ended = completed, draft or
cancelled = canceled. So ACTIVE at the start and COMPLETED at the end need no job of
their own: the poll that sees the clock pass hands the event out again.

An ack carries the `rev` (updated_at) it was built from. When the event was edited
in between, the ack records Discord's answer but leaves the event pending, so the
edit is synced on the next poll instead of being lost.

Stdlib only (plus database.get_db), so the test suite imports it directly.
"""
from datetime import datetime, timedelta, timezone

from database import get_db

DEFAULT_LENGTH = timedelta(hours=2)
FINAL = ("completed", "canceled")
NAME_MAX = 100
DESCRIPTION_MAX = 1000
LOCATION_MAX = 100
DEFAULT_LOCATION = "The Last Sietch"
PORTAL_EVENT_URL = "https://portal.lastsietch.com/events/%d"
ERROR_MAX = 300

_COLUMNS = ("id, title, banner, starts_utc, ends_utc, map_name, description, status, "
            "updated_at, discord_sync, discord_event_id, discord_status, discord_state")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(text):
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def window(row):
    """(start, end) as aware datetimes; an open-ended event runs two hours."""
    start = _parse(row["starts_utc"])
    end = _parse(row["ends_utc"]) or start + DEFAULT_LENGTH
    return start, end


def desired_status(row, now=None):
    """What Discord should show, or None when it should show nothing at all."""
    now = now or _now()
    if row["status"] != "published" or not row["discord_sync"]:
        return "canceled" if row["discord_event_id"] else None
    start, end = window(row)
    if now >= end:
        return "completed"
    if now >= start:
        return "active"
    return "scheduled"


def _trim(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def description(row) -> str:
    """The event text with the portal link as the LAST line. The link is also the
    adoption marker Cielago looks for before creating, so it must always fit."""
    link = PORTAL_EVENT_URL % int(row["id"])
    body = _trim(row["description"], DESCRIPTION_MAX - len(link) - 2)
    return (body + "\n\n" + link) if body else link


def item(row, desired) -> dict:
    start, end = window(row)
    return {
        "id": int(row["id"]),
        "rev": row["updated_at"],
        "desired": desired,
        "discord_event_id": row["discord_event_id"],
        "discord_status": row["discord_status"],
        "name": _trim(row["title"], NAME_MAX),
        "description": description(row),
        "location": _trim(row["map_name"] or DEFAULT_LOCATION, LOCATION_MAX),
        "starts_utc": start.isoformat(),
        "ends_utc": end.isoformat(),
        "banner": row["banner"] or None,
        "marker": PORTAL_EVENT_URL % int(row["id"]),
    }


def pending(limit=50, now=None) -> list:
    """Events whose Discord side needs work, oldest start first.

    A row needs work when it was written since the last sync (pending), the last
    attempt failed, or the clock moved it to a new status. Nothing is handed out
    for an event Discord never saw and that is already over or withdrawn, since
    there is nothing to create and nothing to cancel."""
    now = now or _now()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT %s FROM portal_events"
            " WHERE (discord_sync = 1 OR discord_event_id IS NOT NULL)"
            "   AND (discord_status IS NULL OR discord_status NOT IN ('completed', 'canceled'))"
            " ORDER BY starts_utc ASC" % _COLUMNS).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        desired = desired_status(row, now)
        if desired is None:
            continue
        if not row["discord_event_id"] and desired != "scheduled":
            continue
        if row["discord_state"] in ("pending", "failed") or desired != row["discord_status"]:
            out.append(item(row, desired))
        if len(out) >= limit:
            break
    return out


def ack(results) -> int:
    """Record Cielago's outcome per event. Each result:
    {id, rev, ok, discord_event_id?, discord_status?, error?}. Idempotent."""
    now = _stamp(_now())
    done = 0
    conn = get_db()
    try:
        for r in results:
            if not isinstance(r, dict) or not str(r.get("id", "")).isdigit():
                continue
            eid, rev = int(r["id"]), str(r.get("rev") or "")
            if r.get("ok"):
                status = r.get("discord_status")
                if status not in ("scheduled", "active") + FINAL:
                    continue
                cur = conn.execute(
                    """UPDATE portal_events
                          SET discord_event_id = ?, discord_status = ?, discord_error = NULL,
                              discord_synced_at = ?,
                              discord_state = CASE WHEN updated_at = ? THEN 'synced' ELSE 'pending' END
                        WHERE id = ?""",
                    (str(r.get("discord_event_id") or "") or None, status, now, rev, eid))
            else:
                cur = conn.execute(
                    """UPDATE portal_events
                          SET discord_state = 'failed', discord_error = ?
                        WHERE id = ?""",
                    (str(r.get("error") or "unknown error")[:ERROR_MAX], eid))
            done += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return done
