"""Guild join-requests (social layer Tier 2).

The CANONICAL join-request record lives ONLY here (portal_guild_join_requests in
admin.db). The mailbox stores NO join-request lifecycle state — it is a pure
notification surface. A linked player raises a pending request against a guild;
an Officer/Leader of that guild sees it and fires the (dark) send_invite op,
which flips the row status -> 'invited'.

Pure admin.db; zero game-DB touch. requester_account_id ALWAYS comes from the
session and is NEVER emitted to the client. All authz is enforced by the caller
(the portal endpoints) against the live cached roster.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from database import get_db

NOTE_MAX = 400
TTL_HOURS_DEFAULT = 336            # 14 days before a pending request auto-expires
# Rate limit: a single account may raise at most N join-requests per window
# (across all guilds) — mirrors the windowed-COUNT idiom in portal_rate_limit.
RATE_MAX_PER_WINDOW = 10
RATE_WINDOW_HOURS = 24


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=TTL_HOURS_DEFAULT)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _window_start() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=RATE_WINDOW_HOURS)).strftime(
        "%Y-%m-%d %H:%M:%S")


def rate_ok(requester_account_id: int) -> bool:
    """True if this account is under the join-request rate cap for the window."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_guild_join_requests
                WHERE requester_account_id = ? AND created_at >= ?""",
            (int(requester_account_id), _window_start()),
        ).fetchone()
    finally:
        conn.close()
    return row["c"] < RATE_MAX_PER_WINDOW


def create_request(*, requester_account_id: int, guild_id: int,
                   requester_char_name: str | None,
                   requester_discord_id: str | None,
                   note: str | None) -> dict:
    """Insert a pending join-request. Idempotent on the (guild, requester) partial
    unique index: a duplicate pending request returns the existing row as
    is_new=False. Returns {status, request_id, is_new}."""
    note = (note or "").strip()[:NOTE_MAX] or None
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO portal_guild_join_requests
                   (requester_account_id, requester_char_name, requester_discord_id,
                    guild_id, note, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
               ON CONFLICT(guild_id, requester_account_id)
                   WHERE status = 'pending' DO NOTHING""",
            (int(requester_account_id), requester_char_name or None,
             requester_discord_id or None, int(guild_id), note, _now(), _expiry()),
        )
        conn.commit()
        if cur.lastrowid and cur.rowcount:
            new_id, is_new = int(cur.lastrowid), True
        else:
            # Conflict path: fetch the existing pending row's id.
            prior = conn.execute(
                """SELECT id FROM portal_guild_join_requests
                    WHERE guild_id = ? AND requester_account_id = ?
                      AND status = 'pending'""",
                (int(guild_id), int(requester_account_id)),
            ).fetchone()
            new_id, is_new = (int(prior["id"]) if prior else 0), False
    finally:
        conn.close()
    return {"status": "pending", "request_id": new_id, "is_new": is_new}


def list_pending(guild_id: int) -> list[dict]:
    """Pending requests for a guild. NEVER leaks requester_account_id to the
    client; the caller (portal) must have already role-gated the request."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, requester_char_name, requester_discord_id, note,
                      created_at, expires_at
                 FROM portal_guild_join_requests
                WHERE guild_id = ? AND status = 'pending'
                ORDER BY created_at ASC""",
            (int(guild_id),),
        ).fetchall()
    finally:
        conn.close()
    return [{
        "request_id": r["id"],
        "requester_char_name": r["requester_char_name"],
        "requester_discord_id": r["requester_discord_id"],
        "note": r["note"],
        "created_at": r["created_at"],
        "expires_at": r["expires_at"],
    } for r in rows]


def get_pending(request_id: int, guild_id: int) -> dict | None:
    """One pending request (server-side view, includes requester_account_id for
    the invite handoff). Returns None if not pending / wrong guild."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT * FROM portal_guild_join_requests
                WHERE id = ? AND guild_id = ? AND status = 'pending'""",
            (int(request_id), int(guild_id)),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def mark_invited(request_id: int, acted_by_account_id: int) -> int:
    """Flip a pending request -> 'invited' after the invite op fires. Returns
    rowcount (0 if it was not pending, i.e. a race)."""
    conn = get_db()
    try:
        cur = conn.execute(
            """UPDATE portal_guild_join_requests
                  SET status = 'invited', acted_by_account_id = ?
                WHERE id = ? AND status = 'pending'""",
            (int(acted_by_account_id), int(request_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
