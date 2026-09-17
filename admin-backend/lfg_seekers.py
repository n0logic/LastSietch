"""Solo LFG "Seeker Wall" (social layer Tier 2).

A guild-less player posts a self-authored "looking for a guild" card. Self-post /
self-edit / expiry, one row per player (account_id PK). All state lives in admin.db
(portal_lfg_seekers); zero game-DB touch. account_id ALWAYS comes from the session
(never the client) and is NEVER emitted to other players — char_name is the public
display key. Free-text fields are length-capped here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from database import get_db

PLAYSTYLE_MAX = 40
TIMEZONE_MAX = 40
ROLE_MAX = 60
NOTE_MAX = 400
TTL_HOURS_DEFAULT = 168        # 7 days
TTL_HOURS_MAX = 720            # 30 days


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _expiry(ttl_hours: int | None) -> str:
    hours = TTL_HOURS_DEFAULT if not ttl_hours else int(ttl_hours)
    hours = max(1, min(hours, TTL_HOURS_MAX))
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _public(row) -> dict:
    """Client-safe seeker projection (NO account_id)."""
    return {
        "char_name": row["char_name"],
        "playstyle": row["playstyle"],
        "timezone": row["timezone"],
        "role": row["role"],
        "note": row["note"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
    }


def upsert(account_id: int, *, char_name: str | None, playstyle: str | None,
           timezone: str | None, role: str | None, note: str | None,
           ttl_hours: int | None = None) -> dict:
    """Insert/replace the caller's own seeker row. Returns the public projection."""
    playstyle = (playstyle or "").strip()[:PLAYSTYLE_MAX] or None
    timezone = (timezone or "").strip()[:TIMEZONE_MAX] or None
    role = (role or "").strip()[:ROLE_MAX] or None
    note = (note or "").strip()[:NOTE_MAX] or None
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO portal_lfg_seekers
                   (account_id, char_name, playstyle, timezone, role, note,
                    updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                   char_name  = excluded.char_name,
                   playstyle  = excluded.playstyle,
                   timezone   = excluded.timezone,
                   role       = excluded.role,
                   note       = excluded.note,
                   updated_at = excluded.updated_at,
                   expires_at = excluded.expires_at""",
            (int(account_id), char_name or None, playstyle, timezone, role, note,
             _now(), _expiry(ttl_hours)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM portal_lfg_seekers WHERE account_id = ?",
            (int(account_id),),
        ).fetchone()
    finally:
        conn.close()
    return _public(row)


def delete(account_id: int) -> None:
    """Remove the caller's own seeker row."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM portal_lfg_seekers WHERE account_id = ?",
                     (int(account_id),))
        conn.commit()
    finally:
        conn.close()


def list_active() -> list[dict]:
    """Active seekers (never expired OR expiry in the future), newest-first.
    account_id is never included in the payload."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT * FROM portal_lfg_seekers
                WHERE expires_at IS NULL OR expires_at > ?
                ORDER BY updated_at DESC""",
            (_now(),),
        ).fetchall()
    finally:
        conn.close()
    return [_public(r) for r in rows]
