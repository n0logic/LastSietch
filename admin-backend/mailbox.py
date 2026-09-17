"""Portal mailbox helper (social layer Tier 3).

Pure admin.db (SQLite), reusable by Tiers 1, 2, 4 with ZERO game-DB touch. Two
functions:

  post(...)                 insert one portal_messages row, return the new id
  mark_read_by_payload(...)  mark the matching guild-inbox notification read

Guild-inbox read-state is GUILD-LEVEL: one shared portal_messages row per guild
message, no per-officer fan-out. mark_read_by_payload records read_by_account_id
(the officer who acted) and flips state in one write.

All callers derive recipient/sender identity SERVER-SIDE; nothing here is
client-driven. account_id is never surfaced to other players by these helpers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from database import get_db

# Sender/recipient text is length-capped where a caller passes free text.
SUBJECT_MAX = 200
BODY_MAX = 4000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def post(recipient_kind: str, recipient_id: int, kind: str, *,
         subject: str | None = None, body: str | None = None,
         payload: dict | None = None, sender_kind: str = "system",
         sender_account_id: int | None = None, sender_char_name: str | None = None,
         state: str = "unread", expires_at: str | None = None) -> int:
    """Insert one portal_messages row. Returns the new row id.

    payload is a dict, JSON-serialized to the payload TEXT column. subject/body
    are length-capped. The CHECK constraints on the table enforce the valid
    enum values for recipient_kind/sender_kind/kind/state."""
    if recipient_kind not in ("player", "guild"):
        raise ValueError(f"invalid recipient_kind: {recipient_kind}")
    if sender_kind not in ("player", "guild", "system"):
        raise ValueError(f"invalid sender_kind: {sender_kind}")
    if kind not in ("notification", "user", "gift"):
        raise ValueError(f"invalid kind: {kind}")
    if int(recipient_id) <= 0:
        raise ValueError("recipient_id must be a positive integer")

    subject = (subject or "").strip()[:SUBJECT_MAX] or None
    body = (body or "").strip()[:BODY_MAX] or None
    # Never persist an account_id inside a payload. The inbox is client-visible
    # (and the guild inbox is officer-visible), so an account_id here would be an
    # identity leak + a char_name<->account_id correlation. Nothing downstream
    # needs it: request_id keys the read-state, and account_id lookups go through
    # the canonical tables. Strip any account_id-ish key at the source.
    safe_payload = {k: v for k, v in (payload or {}).items()
                    if "account_id" not in k.lower()}
    payload_json = json.dumps(safe_payload, separators=(",", ":"))

    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO portal_messages
                   (sender_kind, sender_account_id, sender_char_name,
                    recipient_kind, recipient_id, subject, body, kind,
                    payload, state, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sender_kind,
             int(sender_account_id) if sender_account_id else None,
             sender_char_name or None,
             recipient_kind, int(recipient_id), subject, body, kind,
             payload_json, state, _now(), expires_at),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def mark_read_by_payload(guild_id: int, request_id: int,
                         actor_account_id: int | None = None) -> int:
    """Mark the matching GUILD-inbox notification read (guild-wide single row)
    once any officer acts. Matches recipient_kind='guild' AND recipient_id=guild_id
    AND json_extract(payload,'$.request_id')=request_id AND state='unread'. Sets
    state='read', read_at=now, read_by_account_id=actor. Returns rowcount."""
    conn = get_db()
    try:
        cur = conn.execute(
            """UPDATE portal_messages
                  SET state = 'read', read_at = ?, read_by_account_id = ?
                WHERE recipient_kind = 'guild'
                  AND recipient_id = ?
                  AND json_extract(payload, '$.request_id') = ?
                  AND state = 'unread'
                  AND deleted_at IS NULL""",
            (_now(), int(actor_account_id) if actor_account_id else None,
             int(guild_id), int(request_id)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
