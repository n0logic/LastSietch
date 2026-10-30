# Player requests queue (admin plan F7). The one store behind both doors: the portal
# files a request for the signed-in player (routers/portal_requests.py) and the admin
# panel triages, notes, assigns and closes it (routers/v2_requests.py). admin.db only;
# nothing here reads or writes the game DB, and nothing here fulfils a request. Each
# type is routed to the tool that does (TYPES[...]["tool"]), and that tool keeps its
# own gate.
#
# Two projections, never mixed: admin_row() carries everything; player_row() carries
# only what the player may see. assignee and staff_note are staff-only, because an
# assignee is an admin account name and an admin's real name must never reach a
# player surface. player_note is the one staff-written line the player reads.

import json
from datetime import datetime, timezone

from database import get_db

# type -> labels, which reference the request carries, and the admin tool that
# fulfils it (plan rows A4/A6, C1, A14, C4). A tool with href None is not built yet.
TYPES = {
    "brt_relocate": {
        "label": "BRT relocate", "player_label": "Base relocation", "ref": "totem",
        "tool": "A6 relocate wizard", "href": "/admin/v2/players/vault"},
    "dd_base": {
        "label": "Deep Desert base", "player_label": "Deep Desert base", "ref": "totem",
        "tool": "A4/A6 Base Vault wizards", "href": "/admin/v2/players/vault"},
    "choam_terminal": {
        "label": "CHOAM terminal", "player_label": "CHOAM terminal", "ref": "totem",
        "tool": "C1 spawn presets", "href": None},
    "inactive_base_report": {
        "label": "Inactive base report", "player_label": "Inactive base report", "ref": "totem",
        "tool": "A14 inactive-base review", "href": "/admin/v2/players/claims"},
    "vehicle_recovery": {
        "label": "Vehicle recovery", "player_label": "Vehicle recovery", "ref": "vehicle",
        "tool": "C4 vehicle recovery", "href": None},
}
STATUSES = ("new", "triaged", "scheduled", "done", "declined")
OPEN_STATUSES = ("new", "triaged", "scheduled")
CLOSED_STATUSES = ("done", "declined")
# What a player is told. Triage is internal bookkeeping, so it reads as submitted.
PLAYER_STATUS = {"new": "submitted", "triaged": "submitted", "scheduled": "scheduled",
                 "done": "done", "declined": "declined"}

REF_MAX = 80
DETAILS_MAX = 600
NOTE_MAX = 1000
NAME_MAX = 64
# Player-side brakes: open requests per identity, and new requests per day.
PLAYER_OPEN_MAX = 5
PLAYER_DAY_MAX = 10
LIST_LIMIT = 200
HISTORY_LIMIT = 100
EDITABLE = ("status", "staff_note", "player_note", "assignee")


def _now():
    # Microseconds, so two edits inside one second still differ for the stale-edit check.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def clean_text(value, limit):
    """Printable, whitespace-collapsed, capped; None for empty or non-string."""
    if not isinstance(value, str):
        return None
    value = value.replace("—", "-").replace("–", "-")
    text = " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())
    return text[:limit] or None


def _clean_block(value, limit):
    """Like clean_text but keeps line breaks, for notes and details."""
    if not isinstance(value, str):
        return None
    lines = [" ".join("".join(c for c in line if c.isprintable()).split())
             for line in value.replace("\r\n", "\n").split("\n")]
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text[:limit] or None


def validate_new(body):
    """(fields, None) or (None, error token) for a new request body. The reference
    a type needs is required; the other one is dropped."""
    body = body if isinstance(body, dict) else {}
    kind = body.get("type")
    if kind not in TYPES:
        return None, "bad_type"
    ref_kind = TYPES[kind]["ref"]
    totem = clean_text(body.get("totem_ref"), REF_MAX) if ref_kind == "totem" else None
    vehicle = clean_text(body.get("vehicle_ref"), REF_MAX) if ref_kind == "vehicle" else None
    details = _clean_block(body.get("details"), DETAILS_MAX)
    if ref_kind == "totem" and not totem:
        return None, "need_totem"
    if ref_kind == "vehicle" and not vehicle:
        return None, "need_vehicle"
    return {"type": kind, "totem_ref": totem, "vehicle_ref": vehicle, "details": details}, None


def player_brake(discord_id, fields):
    """None, or the token for why this identity may not file another request now."""
    conn = get_db()
    try:
        open_n = conn.execute(
            "SELECT COUNT(*) FROM player_requests WHERE discord_id = ? AND status IN (%s)"
            % ",".join("?" * len(OPEN_STATUSES)), (discord_id, *OPEN_STATUSES)).fetchone()[0]
        if open_n >= PLAYER_OPEN_MAX:
            return "too_many_open"
        day_n = conn.execute(
            "SELECT COUNT(*) FROM player_requests WHERE discord_id = ?"
            " AND created_at >= datetime('now', '-1 day')", (discord_id,)).fetchone()[0]
        if day_n >= PLAYER_DAY_MAX:
            return "rate_limited"
        dup = conn.execute(
            "SELECT id FROM player_requests WHERE discord_id = ? AND type = ?"
            " AND IFNULL(totem_ref, '') = ? AND IFNULL(vehicle_ref, '') = ?"
            " AND status IN (%s)" % ",".join("?" * len(OPEN_STATUSES)),
            (discord_id, fields["type"], fields["totem_ref"] or "", fields["vehicle_ref"] or "",
             *OPEN_STATUSES)).fetchone()
        if dup:
            return "duplicate"
    finally:
        conn.close()
    return None


def create(fields, source, char_name, discord_id=None, account_id=None):
    """Insert one request with status new. Returns its id."""
    if source not in ("portal", "staff"):
        raise ValueError("bad_source")
    name = clean_text(char_name, NAME_MAX)
    if not name:
        raise ValueError("need_player")
    stamp = _now()
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO player_requests (type, status, source, discord_id, account_id, char_name,"
            " totem_ref, vehicle_ref, details, created_at, updated_at)"
            " VALUES (?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fields["type"], source, discord_id, int(account_id) if account_id else None, name,
             fields.get("totem_ref"), fields.get("vehicle_ref"), fields.get("details"), stamp, stamp))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get(request_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM player_requests WHERE id = ?", (request_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def admin_row(row):
    meta = TYPES.get(row["type"], {})
    out = dict(row)
    out["type_label"] = meta.get("label", row["type"])
    out["tool"] = meta.get("tool")
    out["tool_href"] = meta.get("href")
    out["open"] = row["status"] in OPEN_STATUSES
    return out


def player_row(row):
    meta = TYPES.get(row["type"], {})
    return {
        "id": row["id"],
        "type": row["type"],
        "type_label": meta.get("player_label", row["type"]),
        "status": PLAYER_STATUS.get(row["status"], "submitted"),
        "totem_ref": row["totem_ref"],
        "vehicle_ref": row["vehicle_ref"],
        "details": row["details"],
        "note": row["player_note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_requests(status="open", kind=None, assignee=None, q=None, limit=LIST_LIMIT):
    """Admin list, newest first. status: 'open' (new+triaged+scheduled), 'all', or one
    status. assignee: a username, '-' for unassigned, or None for anyone."""
    where, params = [], []
    if status == "open":
        where.append("status IN (%s)" % ",".join("?" * len(OPEN_STATUSES)))
        params.extend(OPEN_STATUSES)
    elif status in STATUSES:
        where.append("status = ?")
        params.append(status)
    elif status not in (None, "", "all"):
        raise ValueError("bad_status")
    if kind:
        if kind not in TYPES:
            raise ValueError("bad_type")
        where.append("type = ?")
        params.append(kind)
    if assignee == "-":
        where.append("assignee IS NULL")
    elif assignee:
        where.append("assignee = ?")
        params.append(assignee)
    text = clean_text(q, NAME_MAX)
    if text:
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(char_name LIKE ? ESCAPE '\\' OR totem_ref LIKE ? ESCAPE '\\'"
                     " OR vehicle_ref LIKE ? ESCAPE '\\' OR CAST(id AS TEXT) = ?)")
        params.extend([like, like, like, text])
    sql = "SELECT * FROM player_requests"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), LIST_LIMIT)))
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [admin_row(dict(r)) for r in rows]


def counts():
    conn = get_db()
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM player_requests GROUP BY status").fetchall()
    finally:
        conn.close()
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    out["open"] = sum(out[s] for s in OPEN_STATUSES)
    return out


def player_requests_for(discord_id, limit=50):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM player_requests WHERE discord_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (discord_id, limit)).fetchall()
    finally:
        conn.close()
    return [player_row(dict(r)) for r in rows]


def staff_names():
    """Active admin-panel accounts an item can be assigned to."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT username FROM users WHERE is_active = 1 ORDER BY username COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [r["username"] for r in rows]


def update(request_id, body, expected_updated_at=None):
    """Apply a staff edit. Returns (row, diff) where diff is {field: [before, after]}
    and is empty when nothing changed (no write). Raises ValueError(token) on a bad
    value, LookupError when the row is gone, and RuntimeError('stale') when someone
    else saved after `expected_updated_at`."""
    before = get(request_id)
    if before is None:
        raise LookupError("not_found")
    if expected_updated_at is not None and expected_updated_at != before["updated_at"]:
        raise RuntimeError("stale")
    body = body if isinstance(body, dict) else {}
    after = dict(before)
    if "status" in body:
        if body["status"] not in STATUSES:
            raise ValueError("bad_status")
        after["status"] = body["status"]
    if "staff_note" in body:
        after["staff_note"] = _clean_block(body["staff_note"], NOTE_MAX)
    if "player_note" in body:
        after["player_note"] = _clean_block(body["player_note"], NOTE_MAX)
    if "assignee" in body:
        name = clean_text(body["assignee"], NAME_MAX)
        # Only a NEW assignee must be a live account; keeping one since deactivated is not an edit.
        if name and name != before["assignee"] and name not in staff_names():
            raise ValueError("bad_assignee")
        after["assignee"] = name
    if after["status"] == "declined" and not after["player_note"]:
        raise ValueError("decline_needs_note")
    diff = {f: [before[f], after[f]] for f in EDITABLE if before[f] != after[f]}
    if not diff:
        return admin_row(before), {}
    stamp = _now()
    if "status" in diff:
        after["closed_at"] = stamp if after["status"] in CLOSED_STATUSES else None
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE player_requests SET status = ?, staff_note = ?, player_note = ?, assignee = ?,"
            " closed_at = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
            (after["status"], after["staff_note"], after["player_note"], after["assignee"],
             after["closed_at"], stamp, request_id, before["updated_at"]))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount != 1:
        raise RuntimeError("stale")
    return admin_row(get(request_id)), diff


def audit_target(request_id):
    return "player_request:%d" % int(request_id)


def details_json(details):
    return json.dumps(details, separators=(",", ":"))


def history(request_id, limit=HISTORY_LIMIT):
    """Every audit row for one request, oldest first."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT username, action, timestamp, details FROM audit_log WHERE target = ?"
            " ORDER BY id ASC LIMIT ?", (audit_target(request_id), limit)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            details = json.loads(r["details"]) if r["details"] else None
        except ValueError:
            details = r["details"]
        out.append({"by": r["username"], "action": r["action"], "at": r["timestamp"],
                    "details": details})
    return out
