"""Portal mailbox endpoints (social layer Tier 3).

Player + guild inbox surface: notifications, player-to-player DMs, gift receipts.
Pure admin.db (portal_messages + portal_guild_inbox_config + portal_message_block);
zero game-DB touch. Sender/recipient identity is ALWAYS derived server-side from
the session (spoofed-sender defense); account_id is never emitted to the client.
Guild-inbox read-state is GUILD-LEVEL (one shared row per guild message).

Shared session/authz/guild helpers are reused from routers.portal (loaded first
by main.py), so the role gates match the rest of the portal exactly.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import mailbox
import player_profile
from database import get_db
from portal_auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    csrf_for_session,
    validate_csrf,
)
from routers.portal import (
    _guild_member_account,
    _load_guilds,
    _read_body,
    _require_linked_session,
    _resolve_account_by_char_name,
)

logger = logging.getLogger("portal")
router = APIRouter()

# DM abuse brake: at most N user-messages per sender per hour.
DM_RATE_MAX_PER_HOUR = 30
DM_PAIR_MAX_PER_DAY = 5   # anti-spam: max DMs from one sender to one recipient / day


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _hour_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _csrf_ok(request: Request, form) -> bool:
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided = request.headers.get(CSRF_HEADER, "") or (form.get("csrf_token", "") or "")
    return bool(session_token) and validate_csrf(provided, csrf_for_session(session_token))


# Whitelist of payload keys allowed out to the client. Defense-in-depth: even if
# a producer accidentally stows an account_id (or any other internal key) in a
# payload, it can NEVER reach the client through a message list. Keep this list
# to display/deep-link data only — no account_id-ish keys, ever.
# 🔴 A key that is not in here is SILENTLY STRIPPED on read, so a new notification type that
# forgets to register its keys renders as an empty card and looks like a bug in the sender.
# `mailbox.post` also strips anything containing "account_id" at the source, which is a second
# line of defence behind this allow-list. Do not weaken either.
#
# The Karum keys (listing_id .. counterparty_name) carry a trade receipt. `kind` is NOT
# extended for them: portal_messages.kind is CHECK-constrained to notification|user|gift in
# BOTH mailbox.py and the DDL, so a fourth enum value would mean editing two places that must
# agree for no player-visible benefit. Karum reuses `notification` and puts its sub-type in the
# payload's own `kind` key.
_PAYLOAD_ALLOWED_KEYS = frozenset({
    "kind", "request_id", "guild_id", "guild_name", "amount", "currency",
    # The Karum (player-to-player trade venue)
    "listing_id", "item_name", "stack_size", "quality_level", "price",
    "counterparty_name", "collect_at",
})


def _safe_payload(raw) -> dict:
    try:
        payload = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k in _PAYLOAD_ALLOWED_KEYS}


def _project(row) -> dict:
    """Canonical client-safe message shape (NO account_id; payload whitelisted)."""
    payload = _safe_payload(row["payload"])
    return {
        "id": row["id"],
        "sender_kind": row["sender_kind"],
        "sender_char_name": row["sender_char_name"],
        "subject": row["subject"],
        "body": row["body"],
        "kind": row["kind"],
        "payload": payload,
        "state": row["state"],
        "created_at": row["created_at"],
    }


def _inbox_config(guild_id: int) -> tuple[int, int]:
    """(view_min_role, manage_min_role) for a guild, with the 50/100 defaults."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT view_min_role, manage_min_role FROM portal_guild_inbox_config "
            "WHERE guild_id = ?", (int(guild_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return 50, 100
    return int(row["view_min_role"]), int(row["manage_min_role"])


def _caller_guild_role(guild, account_id: int):
    """The caller's numeric role in this guild, or None if not a member."""
    m = _guild_member_account(guild, account_id)
    return int(m.get("role_id") or 1) if m else None


# ---- player inbox -----------------------------------------------------------

@router.get("/portal/messages")
async def messages_list(request: Request):
    """The caller's player inbox (not deleted), newest-first."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _row = gate
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT * FROM portal_messages
                WHERE recipient_kind = 'player' AND recipient_id = ?
                  AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC""",
            (int(aid),),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "messages": [_project(r) for r in rows]})


@router.get("/portal/messages/unread-count")
async def messages_unread_count(request: Request):
    """Bell badge: unread, not deleted, recipient = self."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _row = gate
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_messages
                WHERE recipient_kind = 'player' AND recipient_id = ?
                  AND state = 'unread' AND deleted_at IS NULL""",
            (int(aid),),
        ).fetchone()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "count": int(row["c"])})


# ---- guild inbox ------------------------------------------------------------

@router.get("/portal/guilds/{guild_id:int}/messages")
async def guild_messages_list(request: Request, guild_id: int):
    """A guild's shared inbox. Gated to caller role >= view_min_role."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _row = gate

    data = await _load_guilds()
    if data is None:
        return JSONResponse({"ok": False, "error": "Guild data is temporarily "
                             "unavailable."}, status_code=503)
    guild = data["by_id"].get(int(guild_id))
    if not guild:
        return JSONResponse({"ok": False, "error": "Unknown guild."}, status_code=404)
    role = _caller_guild_role(guild, aid)
    view_min, _manage_min = _inbox_config(int(guild_id))
    if role is None or role < view_min:
        return JSONResponse({"ok": False, "error": "Not permitted."}, status_code=403)

    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT * FROM portal_messages
                WHERE recipient_kind = 'guild' AND recipient_id = ?
                  AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC""",
            (int(guild_id),),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "guild_id": int(guild_id),
                         "messages": [_project(r) for r in rows]})


# ---- read / delete (ownership-gated) ----------------------------------------

async def _authz_message_action(request: Request, message_id: int, aid: int):
    """Load a message + verify the caller may act on it. Returns (row, err_response).
    Player message: recipient must be self. Guild message: caller role >=
    manage_min_role of that guild."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM portal_messages WHERE id = ?",
                           (int(message_id),)).fetchone()
    finally:
        conn.close()
    if not row or row["deleted_at"]:
        return None, JSONResponse({"ok": False, "error": "Message not found."},
                                  status_code=404)
    if row["recipient_kind"] == "player":
        if int(row["recipient_id"]) != int(aid):
            return None, JSONResponse({"ok": False, "error": "Not permitted."},
                                      status_code=403)
        return row, None
    # guild message
    data = await _load_guilds()
    guild = data["by_id"].get(int(row["recipient_id"])) if data else None
    if not guild:
        return None, JSONResponse({"ok": False, "error": "Not permitted."},
                                  status_code=403)
    role = _caller_guild_role(guild, aid)
    _view_min, manage_min = _inbox_config(int(row["recipient_id"]))
    if role is None or role < manage_min:
        return None, JSONResponse({"ok": False, "error": "Not permitted."},
                                  status_code=403)
    return row, None


@router.post("/portal/messages/{message_id:int}/read")
async def message_read(request: Request, message_id: int):
    """Mark a message read. Player: recipient self; guild: role >= manage_min_role."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _row = gate
    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    row, err = await _authz_message_action(request, message_id, aid)
    if err is not None:
        return err
    conn = get_db()
    try:
        conn.execute(
            "UPDATE portal_messages SET state = 'read', read_at = ?, "
            "read_by_account_id = ? WHERE id = ? AND deleted_at IS NULL",
            (_now(), int(aid), int(message_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


@router.post("/portal/messages/{message_id:int}/delete")
async def message_delete(request: Request, message_id: int):
    """Soft-delete a message (same ownership rule as read)."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _row = gate
    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    row, err = await _authz_message_action(request, message_id, aid)
    if err is not None:
        return err
    conn = get_db()
    try:
        conn.execute("UPDATE portal_messages SET deleted_at = ? WHERE id = ?",
                     (_now(), int(message_id)))
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


# ---- player-to-player DM ----------------------------------------------------

def _is_blocked(recipient_account_id: int, sender_account_id: int) -> bool:
    """True if the recipient has blocked the sender."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM portal_message_block WHERE account_id = ? "
            "AND blocked_account_id = ?",
            (int(recipient_account_id), int(sender_account_id)),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _dm_rate_ok(sender_account_id: int) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_messages
                WHERE sender_account_id = ? AND kind = 'user'
                  AND created_at >= ?""",
            (int(sender_account_id), _hour_ago()),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"]) < DM_RATE_MAX_PER_HOUR


def _day_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _dm_pair_rate_ok(sender_account_id: int, recipient_account_id: int) -> bool:
    """Anti-spam: cap DMs from ONE sender to ONE recipient per day so the public
    directory cannot be used to flood a single player."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_messages
                WHERE sender_account_id = ? AND recipient_kind = 'player'
                  AND recipient_id = ? AND kind = 'user' AND created_at >= ?""",
            (int(sender_account_id), int(recipient_account_id), _day_ago()),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"]) < DM_PAIR_MAX_PER_DAY


def _has_thread(a_account_id: int, b_account_id: int) -> bool:
    """True if a DM already exists in EITHER direction between the two accounts.
    Lets replies through even after the recipient opts out of the directory."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT 1 FROM portal_messages
                WHERE kind = 'user' AND recipient_kind = 'player'
                  AND ((sender_account_id = ? AND recipient_id = ?)
                    OR (sender_account_id = ? AND recipient_id = ?))
                LIMIT 1""",
            (int(a_account_id), int(b_account_id),
             int(b_account_id), int(a_account_id)),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


@router.post("/portal/messages/send")
async def message_send(request: Request):
    """Freeform player-to-player DM. Sender from session; recipient resolved
    server-side. Rejects self-send + blocked pairs; rate-limited per hour."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, srow = gate
    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def _fail(msg: str, status: int = 400):
        return JSONResponse({"ok": False, "error": msg}, status_code=status)

    body_text = (form.get("body", "") or "").strip()
    if not body_text:
        return _fail("Message body is required.")

    # Recipient is addressed by character name ONLY (resolved server-side via
    # ls_account_links). A raw client-supplied account_id is NOT trusted — it
    # would allow DM-to-any-account by id-guessing / enumeration.
    recipient_account_id = _resolve_account_by_char_name(
        form.get("recipient_char_name", "") or "")
    if not recipient_account_id:
        return _fail("That player is not linked to the portal.", status=404)
    if recipient_account_id == int(aid):
        return _fail("You cannot message yourself.")
    if _is_blocked(recipient_account_id, int(aid)):
        return _fail("You cannot message this player.", status=403)
    # Directory opt-out: a player who set their profile private is not cold-DMable,
    # but replies within an existing thread still go through.
    if (not player_profile.is_listed(recipient_account_id)
            and not _has_thread(int(aid), recipient_account_id)):
        return _fail("This player is not accepting new messages.", status=403)
    if not _dm_rate_ok(int(aid)):
        return _fail("You are sending messages too quickly. Try again later.",
                     status=429)
    if not _dm_pair_rate_ok(int(aid), recipient_account_id):
        return _fail("You have messaged this player too many times today.",
                     status=429)

    mid = mailbox.post(
        "player", int(recipient_account_id), "user",
        subject=form.get("subject", "") or "", body=body_text,
        sender_kind="player", sender_account_id=int(aid),
        sender_char_name=srow["character_name"] or "")
    return JSONResponse({"ok": True, "id": mid})


# ---- block management (shape defined; small) --------------------------------

async def _resolve_block_target(form) -> int | None:
    # Char-name only (server-resolved); no client-supplied account_id trusted.
    return _resolve_account_by_char_name(form.get("blocked_char_name", "") or "")


@router.post("/portal/messages/block")
async def message_block(request: Request):
    """Block a player from DMing the caller."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _row = gate
    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    target = await _resolve_block_target(form)
    if not target or target == int(aid):
        return JSONResponse({"ok": False, "error": "Invalid target."},
                            status_code=400)
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO portal_message_block "
            "(account_id, blocked_account_id, created_at) VALUES (?, ?, ?)",
            (int(aid), int(target), _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


@router.post("/portal/messages/unblock")
async def message_unblock(request: Request):
    """Remove a block set by the caller."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _row = gate
    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    target = await _resolve_block_target(form)
    if not target:
        return JSONResponse({"ok": False, "error": "Invalid target."},
                            status_code=400)
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM portal_message_block WHERE account_id = ? "
            "AND blocked_account_id = ?", (int(aid), int(target)),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


# ---- guild inbox config (Leader only) ---------------------------------------

@router.post("/portal/guilds/{guild_id:int}/inbox-config")
async def guild_inbox_config_set(request: Request, guild_id: int):
    """Set the guild-inbox view/manage role thresholds. Leader (100) only."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, srow = gate
    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    data = await _load_guilds()
    if data is None:
        return JSONResponse({"ok": False, "error": "Guild data is temporarily "
                             "unavailable."}, status_code=503)
    guild = data["by_id"].get(int(guild_id))
    if not guild:
        return JSONResponse({"ok": False, "error": "Unknown guild."}, status_code=404)
    role = _caller_guild_role(guild, aid)
    if role != 100:
        return JSONResponse({"ok": False, "error": "Only a guild Leader can edit "
                             "the inbox config."}, status_code=403)

    def _role_field(name: str, default: int) -> int:
        raw = str(form.get(name, "") or "").strip()   # JSON sends role levels as ints
        val = int(raw) if raw.isdigit() else default
        return val if val in (1, 50, 100) else default

    view_min = _role_field("view_min_role", 50)
    manage_min = _role_field("manage_min_role", 100)
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO portal_guild_inbox_config
                   (guild_id, view_min_role, manage_min_role, set_by_account_id,
                    set_by_char_name, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   view_min_role     = excluded.view_min_role,
                   manage_min_role   = excluded.manage_min_role,
                   set_by_account_id = excluded.set_by_account_id,
                   set_by_char_name  = excluded.set_by_char_name,
                   updated_at        = excluded.updated_at""",
            (int(guild_id), view_min, manage_min, int(aid),
             srow["character_name"] or "", _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "view_min_role": view_min,
                         "manage_min_role": manage_min})


# ---- public player directory + DM visibility --------------------------------

@router.get("/portal/players")
async def players_directory(request: Request):
    """Public player directory: linked players who have not opted out, char_name
    + blurb only (never account_id). Optional ?q= case-insensitive name search."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    q = request.query_params.get("q")
    return JSONResponse({"ok": True, "players": player_profile.list_public(q)})


@router.get("/portal/profile")
async def profile_get(request: Request):
    """The caller's own directory visibility (default public) + blurb."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, _srow = gate
    prof = player_profile.get_profile(int(aid))
    return JSONResponse({"ok": True, "listed": prof["listed"], "blurb": prof["blurb"]})


@router.post("/portal/profile")
async def profile_set(request: Request):
    """Set the caller's directory visibility (one-click opt-out) + optional blurb.
    listed defaults to public; sending listed=0 opts the player out."""
    gate, early = _require_linked_session(request)
    if early is not None:
        return early
    _, _discord_id, aid, srow = gate
    try:
        form = await _read_body(request)
    except Exception:
        form = {}
    if not _csrf_ok(request, form):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    raw = str(form.get("listed", "1")).strip().lower()
    listed = raw in ("1", "true", "on", "yes")
    prof = player_profile.set_profile(
        int(aid), char_name=srow["character_name"] or "",
        listed=listed, blurb=form.get("blurb", "") or "")
    return JSONResponse({"ok": True, "listed": prof["listed"], "blurb": prof["blurb"]})
