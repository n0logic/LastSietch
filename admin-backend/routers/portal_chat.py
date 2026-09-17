"""Portal chat, the player-facing half (Fremkit wave 11, lane A).

Five routes: the channel rail, one channel's history, a send, a read mark, and
the live stream. Moderation (report, delete, mute, the queues) is
routers/portal_chat_mod.py; retention is chat_retention.py. Everything here is
admin.db plus the membership helpers in portal_chat_channels; zero game-DB
touch, and the only game reads are the ones those helpers already make.

Five properties matter more than the feature:

  * LINKED PLAYERS ONLY, READING AS WELL AS POSTING (owner ruling 2026-09-04).
    Every route gates on `_require_linked_session_json` FIRST, so an anonymous
    caller gets the same 401 envelope everywhere and the stream never opens.
  * IDENTITY IS THE SESSION'S, NEVER THE BODY'S. account_id and the selected
    character come off the signed cookies; `char_name` is the only handle that
    ever leaves this module. A client cannot post as anyone.
  * A CHANNEL ID OFF THE WIRE IS RE-CHECKED. `can_access` decides membership per
    request. The stream drops a channel it cannot verify SILENTLY rather than
    refusing the whole connection: one stale id in a reconnect must not take the
    other rooms down with it.
  * A REPLAY IS THE SAME MESSAGE. `client_key` is unique per account, so a retry
    after a lost response returns the row that already exists with replay:true.
    The replay lookup runs BEFORE the rate and duplicate brakes, or a network
    blip would turn a delivered message into a refusal.
  * A COMMAND IS A KIND, NOT A SYNTAX. `/me` and `/roll` are resolved on the
    SERVER after the cleaner and stored as ordinary rows carrying `kind`, so
    the page renders a kind it was given and never re-parses a body. A roll the
    client computed is a number the client chose.
  * DARK IS A 200, NOT AN ERROR. While LASTSIETCH_CHAT_ENABLED is off every GET and
    POST answers {ok:false, error:"chat_disabled"} at HTTP 200 and the stream
    answers 204, so the page renders "being fitted" and never a toast.

A message body is never logged, cleaned or raw, at any level.
"""
import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import portal_chat_channels as channels
import portal_chat_commands as commands
import portal_chat_filters as filters
from database import get_db
from portal_auth import client_ip
from routers.portal import (
    _require_linked_session_json,
    _resolve_char_identity,
    _selected_ctrl,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()


# --- chat model (stdlib only below; the test suite execs this section) -------

# Player-facing copy for every refusal token in the contract. No em dashes.
_TEXT = {
    "chat_disabled": "Chat is being fitted. Back soon.",
    "not_member": "You are not in that channel.",
    "muted": "You cannot post right now.",
    "rate_limited": "You are sending messages too quickly. Wait a moment.",
    "duplicate": "You just sent that.",
    "unknown_command": "That is not a chat command.",
    "bad_request": "That message could not be sent.",
}

PAGE_LIMIT_DEFAULT = 50
PAGE_LIMIT_MAX = 100
# A client uuid. The same charset the refinery and guild idempotency keys use,
# so there is one spelling of "a client key" on this side of the wire.
CLIENT_KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

# SSE. The cadence and the caps are portal_map_stream's, for the same reasons:
# streams are long-lived, so the request-rate limiters do not model them, and a
# runaway tab loop is what the per-IP cap is for.
STREAM_POLL_S = 2.0
STREAM_KEEPALIVE_S = 25.0
STREAM_MAX_PER_IP = 4
STREAM_MAX_TOTAL = 64
# Channels one connection may subscribe to, and rows one poll may carry. Both
# bound the work a single reconnect can ask for.
STREAM_MAX_CHANNELS = 16
STREAM_BATCH = 100
# Membership is resolved at CONNECT, and a stream outlives the fact it was built
# on: a player kicked from a guild would keep receiving that room until the tab
# closed. Two brakes, because neither is sufficient alone. The re-check drops a
# room the player has left within a minute; the lifetime ends the response so
# EventSource reconnects and EVERYTHING (including a house switch and the
# faction cache behind it) is resolved again from scratch.
STREAM_RECHECK_S = 60.0
STREAM_MAX_LIFETIME_S = 600.0

_stream_conns_by_ip: dict = {}
_stream_conns_total = 0

# char_name is a relay read (the ?list=1 character list), and a send must not
# make one per keystroke-sized message. Short TTL: a rename should show up in
# about a minute, not on the next restart.
NAME_TTL_S = 60.0
_name_cache: dict = {}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seconds_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")


def public_message(row, account_id: int, with_channel: bool = False) -> dict:
    """One row as the client sees it. A deleted row KEEPS its place in the list
    and loses its body here, on the way out, rather than in the table: the
    moderation queue still needs to show what was said."""
    deleted = row["deleted_utc"] is not None
    out = {
        "id": int(row["id"]),
        "char_name": row["char_name"],
        "body": "" if deleted else row["body"],
        # Defaulted rather than trusted: every row posted before wave 11.1 was
        # backfilled by the column DEFAULT, but a NULL that reached the page
        # would be a row the renderer has no branch for.
        "kind": row["kind"] or commands.KIND_SAY,
        "created_utc": row["created_utc"],
        "mine": int(row["account_id"]) == int(account_id),
        "deleted": deleted,
        "deleted_by": row["deleted_by"] if deleted else None,
    }
    if with_channel:
        out["channel"] = row["channel"]
    return out


_ROW_COLUMNS = ("id, channel, account_id, char_name, body, kind, created_utc, "
                "deleted_utc, deleted_by")


def _borrow(conn):
    """(handle, should_close). A read called INSIDE the send transaction must
    never open a second connection: SQLite would queue it behind a lock its own
    caller is holding, which is a self-deadlock rather than a slow query."""
    if conn is not None:
        return conn, False
    return get_db(), True


def message_by_id(message_id: int, conn=None):
    handle, close = _borrow(conn)
    try:
        return handle.execute(
            "SELECT %s FROM portal_chat_messages WHERE id = ?" % _ROW_COLUMNS,
            (int(message_id),),
        ).fetchone()
    finally:
        if close:
            handle.close()


def message_by_client_key(account_id: int, client_key: str, conn=None):
    handle, close = _borrow(conn)
    try:
        return handle.execute(
            "SELECT %s FROM portal_chat_messages"
            " WHERE account_id = ? AND client_key = ?" % _ROW_COLUMNS,
            (int(account_id), str(client_key)),
        ).fetchone()
    finally:
        if close:
            handle.close()


def insert_message(channel, account_id, controller_id, char_name, body, client_key,
                   kind=commands.KIND_SAY, conn=None):
    """(row, replay). The UNIQUE (account_id, client_key) index is the only thing
    standing between a retried POST and a second message, so the IntegrityError
    is caught and re-read rather than pre-checked: two concurrent retries would
    both pass a pre-check and both insert.

    With `conn` the caller owns the transaction and this neither commits nor
    closes: `guarded_send` needs the insert to land or roll back with the brakes
    that allowed it."""
    row_id = None
    handle, close = _borrow(conn)
    try:
        cur = handle.execute(
            """INSERT INTO portal_chat_messages
                   (channel, account_id, controller_id, char_name, body, kind,
                    client_key, created_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(channel), int(account_id),
             int(controller_id) if controller_id is not None else None,
             str(char_name), str(body), str(kind), client_key or None, now_utc()),
        )
        if close:
            handle.commit()
        row_id = int(cur.lastrowid)
    except sqlite3.IntegrityError:
        if close:
            handle.rollback()
        else:
            raise
    finally:
        if close:
            handle.close()

    if row_id is None:
        existing = message_by_client_key(account_id, client_key) if client_key else None
        if existing is None:
            raise sqlite3.IntegrityError("chat insert refused with no replay row")
        return existing, True
    return message_by_id(row_id, conn=conn), False


def guarded_send(channel, account_id, controller_id, char_name, raw_body, client_key):
    """(token, value) for one send attempt, decided under ONE write transaction.

    token is 'ok' or 'replay' (value = the row), 'muted' (value = the mute),
    'rate_limited' (value = seconds), or a refusal token with value None.

    The order inside the transaction is the contract: replay, mute, clean,
    COMMAND, rate, duplicate. The command parse sits where it does because a
    muted player is refused before their command is resolved (a mute is about
    the player, not the message) and because the brakes must count the body that
    would actually be stored, which for /roll is not the body that was typed.

    BEGIN IMMEDIATE takes the write lock BEFORE the brakes read, so two sends
    racing on the same account cannot both count nine rows in the burst window
    and both insert a tenth. Check-then-insert without it is a rate limit that
    holds for one caller at a time and no others.

    NOTHING AWAITED HAPPENS IN HERE. char_name is resolved by the caller before
    the transaction opens, because holding SQLite's write lock across a relay
    call would hold it for the relay timeout and stall every other sender."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Ahead of the brakes on purpose: a retry of a message that already
        # landed is the same message, not a duplicate and not a burst.
        if client_key:
            existing = message_by_client_key(account_id, client_key, conn=conn)
            if existing is not None:
                return "replay", existing
        mute = channels.active_mute(account_id, channel, conn=conn)
        if mute is not None:
            return "muted", mute
        ok, cleaned, reason = filters.clean_body(raw_body)
        if not ok:
            return reason, None
        # AFTER the cleaner, never before it: the cap, the link allowlist and
        # the mention strip have already run on what the player typed, so a
        # command can neither carry a longer message than a plain one nor wear a
        # slash in front of a live link to get it past the filter.
        kind, cleaned, reason = commands.parse(cleaned)
        if reason is not None:
            return reason, None
        wait = filters.retry_after(account_id, conn=conn)
        if wait:
            return "rate_limited", wait
        if filters.is_duplicate(account_id, cleaned, conn=conn):
            return "duplicate", None
        try:
            row, _replay = insert_message(channel, account_id, controller_id,
                                          char_name, cleaned, client_key, kind=kind,
                                          conn=conn)
        except sqlite3.IntegrityError:
            # Two retries of the same client_key raced past the lookup above.
            # The index refused the second; the first one's row is the answer.
            conn.rollback()
            existing = message_by_client_key(account_id, client_key, conn=conn) \
                if client_key else None
            if existing is None:
                raise
            return "replay", existing
        conn.commit()
        return "ok", row
    finally:
        try:
            conn.rollback()   # no-op after a commit; releases the lock on a refusal
        except sqlite3.ProgrammingError:
            pass
        conn.close()


def load_messages(channel: str, before, limit: int):
    """(messages oldest-first, has_more). Paged DESCENDING on id (which is what
    `before` walks) and reversed on the way out, so the page can append a block
    without re-sorting and the ids stay dense across a delete."""
    sql = ["SELECT %s FROM portal_chat_messages WHERE channel = ?" % _ROW_COLUMNS]
    args = [str(channel)]
    if before is not None:
        sql.append("AND id < ?")
        args.append(int(before))
    sql.append("ORDER BY id DESC LIMIT ?")
    args.append(int(limit) + 1)
    conn = get_db()
    try:
        rows = conn.execute(" ".join(sql), args).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > limit
    return list(reversed(rows[:limit])), has_more


def mark_read(account_id: int, channel: str, last_id: int) -> int:
    """Stamp the read mark, never backwards. A second tab a page behind must not
    resurrect unread counts the player has already cleared."""
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO portal_chat_reads (account_id, channel, last_id)
                    VALUES (?, ?, ?)
               ON CONFLICT(account_id, channel) DO UPDATE
                    SET last_id = MAX(last_id, excluded.last_id)""",
            (int(account_id), str(channel), int(last_id)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT last_id FROM portal_chat_reads WHERE account_id = ? AND channel = ?",
            (int(account_id), str(channel)),
        ).fetchone()
    finally:
        conn.close()
    return int(row["last_id"]) if row else int(last_id)


def newest_message_id(channel_ids) -> int:
    ids = list(channel_ids or [])
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM portal_chat_messages WHERE channel IN (%s)" % marks,
            ids,
        ).fetchone()
    finally:
        conn.close()
    return int(row["m"] or 0)


def messages_after(channel_ids, last_id: int, limit: int = STREAM_BATCH, conn=None):
    ids = list(channel_ids or [])
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    handle, close = _borrow(conn)
    try:
        return handle.execute(
            "SELECT %s FROM portal_chat_messages"
            " WHERE channel IN (%s) AND id > ? ORDER BY id ASC LIMIT ?"
            % (_ROW_COLUMNS, marks),
            (*ids, int(last_id), int(limit)),
        ).fetchall()
    finally:
        if close:
            handle.close()


def deletions_after(channel_ids, since_utc: str, limit: int = STREAM_BATCH, conn=None):
    """Rows stamped deleted since `since_utc`. A delete lands on a row the
    stream has already sent, so it can never be found by walking ids; the
    partial index on deleted_utc is what keeps this off a full scan."""
    ids = list(channel_ids or [])
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    handle, close = _borrow(conn)
    try:
        return handle.execute(
            "SELECT id, channel, deleted_utc FROM portal_chat_messages"
            " WHERE deleted_utc IS NOT NULL AND deleted_utc > ? AND channel IN (%s)"
            " ORDER BY deleted_utc ASC, id ASC LIMIT ?" % marks,
            (str(since_utc), *ids, int(limit)),
        ).fetchall()
    finally:
        if close:
            handle.close()


def mute_signature(mute) -> str:
    """A stable string for "the viewer's mute state right now", so the stream can
    emit `event: mute` on a CHANGE rather than on every poll."""
    if not mute:
        return ""
    return "%s|%s|%s" % (mute.get("id"), mute.get("channel"), mute.get("until_utc") or "")


def public_mute(mute):
    if not mute:
        return None
    return {"until_utc": mute.get("until_utc"), "channel": mute.get("channel"),
            "reason": mute.get("reason")}


def poll_frames(account_id, subscribed, last_id, since_utc, mute_sig):
    """One admin.db pass for a connected stream.

    Returns (frames, last_id, since_utc, mute_sig) where a frame is
    (event_name, payload, sse_id_or_None). Only a `message` frame carries an
    SSE id, because Last-Event-ID resumes the message walk and a delete or a
    mute must not move that cursor."""
    # One connection for the whole tick, lent to all three reads. Three separate
    # connections per poll per stream is three sqlite opens every two seconds
    # per connected tab, for reads that are already inside one logical snapshot.
    conn = get_db()
    try:
        return _poll_frames(conn, account_id, subscribed, last_id, since_utc, mute_sig)
    finally:
        conn.close()


def _poll_frames(conn, account_id, subscribed, last_id, since_utc, mute_sig):
    frames = []
    for row in messages_after(subscribed, last_id, conn=conn):
        last_id = max(last_id, int(row["id"]))
        if row["deleted_utc"] is not None:
            continue  # deleted before it was ever sent: nothing to remove
        frames.append(("message", public_message(row, account_id, with_channel=True),
                       int(row["id"])))

    # The delete cursor is a CLOCK, not a row stamp, and it is wound back one
    # second: created_utc has one-second resolution, so advancing it to the
    # newest stamp seen would drop a second delete landing in the same second.
    # One second of overlap costs at most a repeated `deleted` event, and
    # removing an already-removed row is idempotent on the page.
    poll_stamp = _seconds_ago(1)
    for row in deletions_after(subscribed, since_utc, conn=conn):
        frames.append(("deleted", {"channel": row["channel"], "id": int(row["id"])}, None))
    since_utc = max(since_utc, poll_stamp)

    mute = channels.active_mute(account_id, conn=conn)
    sig = mute_signature(mute)
    if sig != mute_sig:
        mute_sig = sig
        frames.append(("mute", {"muted": public_mute(mute)}, None))

    return frames, last_id, since_utc, mute_sig


def sse_frame(event: str, payload: dict, sse_id=None) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    head = "id: %d\n" % sse_id if sse_id is not None else ""
    return ("%sevent: %s\ndata: %s\n\n" % (head, event, body)).encode()


def parse_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# --- routes ------------------------------------------------------------------


def _chat_enabled() -> bool:
    """Portal kill switch for chat. Default OFF.

    Resolved by feature_flags, re-read on every call and never memoised: the
    data/feature_flags.json override wins, then the process environment
    (os.environ.get("LASTSIETCH_CHAT_ENABLED", "0") == "1"), then the coded default
    OFF. The owner-only Systems toggle writes that override, so a flip lands on
    the next request without restarting lastsietch-admin."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_CHAT_ENABLED", "0")


def _err(token: str, **extra):
    """A refusal the page renders in place: HTTP 200, ok:false, and the token
    the client switches on. Never a non-2xx, which api.js turns into a throw."""
    payload = {"ok": False, "error": token,
               "message": _TEXT.get(token, _TEXT["bad_request"])}
    payload.update(extra)
    return JSONResponse(payload, status_code=200)


async def _char_name(request: Request, account_id: int, link_row) -> str:
    """The selected character's live name, the handle every message carries.
    Falls back to the link row's character_name when the relay does not answer,
    so a send never stores an empty handle. Cached per (account, controller)."""
    ctrl = _selected_ctrl(request, account_id)
    key = (int(account_id), ctrl)
    hit = _name_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    try:
        ident = await _resolve_char_identity(account_id, ctrl)
    except Exception:  # noqa: BLE001 - a name is not worth failing a send for
        ident = None
    fallback = (link_row["character_name"] if link_row is not None else "") or ""
    name = str((ident or {}).get("char_name") or fallback).strip()
    if name:
        _name_cache[key] = (time.monotonic() + NAME_TTL_S, name)
    return name


@router.get("/portal/chat/channels")
async def portal_chat_channels_list(request: Request):
    """The channel rail plus who the viewer is. `me.muted` is a mute anywhere,
    because the composer has to explain itself before a channel is picked.
    Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, account_id, link_row = gate
    if not _chat_enabled():
        return _err("chat_disabled")

    rows = await channels.channels_for(request)
    mute = channels.active_mute(account_id)
    return _v2_ok({
        "channels": rows,
        "me": {
            "char_name": await _char_name(request, account_id, link_row),
            "muted": public_mute(mute),
            "admin": channels.is_admin(discord_id),
        },
    })


@router.get("/portal/chat/{channel}/messages")
async def portal_chat_messages(request: Request, channel: str):
    """One channel's history, oldest-first, `before` walking backwards from an
    id. A channel the caller is not in refuses `not_member` rather than 404ing:
    a 404 would tell a stranger which guild channels exist. Auth: linked
    session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _discord_id, account_id, _link_row = gate
    if not _chat_enabled():
        return _err("chat_disabled")
    if not await channels.can_access(request, channel):
        return _err("not_member")

    # A junk limit falls back to the default; a number out of range is CLAMPED,
    # not defaulted, so ?limit=0 is one row and not fifty.
    limit = parse_int(request.query_params.get("limit"), PAGE_LIMIT_DEFAULT)
    limit = max(1, min(limit, PAGE_LIMIT_MAX))
    before = parse_int(request.query_params.get("before"))
    if before is not None and before <= 0:
        before = None

    rows, has_more = load_messages(channel, before, limit)
    return _v2_ok({"channel": channel, "has_more": has_more,
                   "messages": [public_message(r, account_id) for r in rows]})


@router.post("/portal/chat/{channel}/send")
async def portal_chat_send(request: Request, channel: str):
    """Post one message. Every refusal is a 200 carrying its token, so the
    composer can show the reason in place. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _discord_id, account_id, link_row = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    if not _chat_enabled():
        return _err("chat_disabled")
    if not await channels.can_access(request, channel):
        return _err("not_member")

    client_key = body.get("client_key")
    if client_key is not None:
        client_key = str(client_key).strip()
        if not client_key:
            client_key = None
        elif not CLIENT_KEY_RE.fullmatch(client_key):
            return _err("bad_request")

    # Resolved BEFORE the write transaction opens: this is the one awaited call
    # on the send path, and guarded_send must not hold the write lock across a
    # relay timeout. Cached per (account, controller), so it is a lookup and not
    # a round trip on all but the first send of the minute.
    char_name = await _char_name(request, account_id, link_row)
    if not char_name:
        return _err("bad_request")

    token, value = guarded_send(channel, account_id,
                                _selected_ctrl(request, account_id),
                                char_name, body.get("body"), client_key)
    if token in ("ok", "replay"):
        return _v2_ok({"message": public_message(value, account_id),
                       "replay": token == "replay"})
    if token == "muted":
        return _err("muted", until_utc=value.get("until_utc"))
    if token == "rate_limited":
        return _err("rate_limited", retry_after=value)
    if token == "unknown_command":
        # The list, not just the refusal: a player who mistyped /wisper needs to
        # be told what exists, and the two the composer answers locally are on
        # it because they exist to the player even though they never arrive here.
        return _err("unknown_command", commands=list(commands.COMMANDS))
    return _err(token)


@router.post("/portal/chat/{channel}/read")
async def portal_chat_read(request: Request, channel: str):
    """Mark this channel read up to `last_id`. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _discord_id, account_id, _link_row = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    if not _chat_enabled():
        return _err("chat_disabled")
    if not await channels.can_access(request, channel):
        return _err("not_member")

    last_id = parse_int(body.get("last_id"), 0) or 0
    if last_id < 0:
        return _err("bad_request")
    return _v2_ok({"channel": channel, "last_id": mark_read(account_id, channel, last_id)})


@router.get("/portal/chat/stream")
async def portal_chat_stream(request: Request):
    """SSE for the subscribed channels. Named events `message`, `deleted`,
    `mute` and `heartbeat`, and the message id as the SSE id so the browser's
    own Last-Event-ID resumes the walk.

    A channel in `?channels=` that is unknown, or that this session is not in,
    is DROPPED without a word. Refusing the connection would let one stale id in
    a reconnect take down the rooms the player really is in.

    MEMBERSHIP DOES NOT SURVIVE THE CONNECTION IT WAS RESOLVED ON. The
    subscribed set is re-checked against `can_access` every STREAM_RECHECK_S and
    a channel that no longer passes is dropped mid-stream, and the whole
    response ends after STREAM_MAX_LIFETIME_S so the client reconnects and
    resolves everything again. A player removed from a guild stops receiving
    that room within the minute, not when they next close the tab.

    `heartbeat` is an EVENT and not only a `:` comment: a comment keeps proxies
    open but a client watchdog cannot observe one, so a silent-but-alive stream
    would look identical to a dead one.

    204 while the flag is off: an EventSource treats it as a closed stream and
    stops, which is what "chat is being fitted" should do to a live connection.
    429 over the per-IP or total cap (the client falls back to polling)."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, _discord_id, account_id, _link_row = gate
    if not _chat_enabled():
        return Response(status_code=204)

    # Capped BEFORE the first can_access, not while iterating. can_access reads
    # the guild directory and the faction cache, so a query string naming ten
    # thousand channels nobody is in would otherwise buy ten thousand membership
    # resolutions off one request: the old loop only stopped early once sixteen
    # were ACCEPTED, which an attacker simply never lets happen.
    wanted = [c.strip() for c in (request.query_params.get("channels") or "").split(",")
              if c.strip()]
    wanted = list(dict.fromkeys(wanted))   # de-dupe first, so a repeat cannot spend the budget
    if len(wanted) > STREAM_MAX_CHANNELS:
        return Response(status_code=429)
    subscribed = [cid for cid in wanted if await channels.can_access(request, cid)]

    global _stream_conns_total
    ip = client_ip(request)
    if (_stream_conns_total >= STREAM_MAX_TOTAL
            or _stream_conns_by_ip.get(ip, 0) >= STREAM_MAX_PER_IP):
        return Response(status_code=429)

    resume = parse_int(request.headers.get("Last-Event-ID"))
    if resume is None:
        resume = parse_int(request.query_params.get("last_id"))
    # No cursor means "from now": replaying the whole channel on connect would
    # duplicate everything the page just fetched over /messages.
    last_id = resume if resume is not None and resume >= 0 else newest_message_id(subscribed)
    # Wound back one second for the same reason poll_frames winds its cursor
    # back: deleted_utc has one-second resolution, so a delete stamped in the
    # second this connection opened would otherwise never be > the cursor.
    since_utc = _seconds_ago(1)
    mute_sig = mute_signature(channels.active_mute(account_id))

    async def gen():
        # Slot accounting lives INSIDE the generator, as it does in
        # portal_map_stream: a generator that is never started never reserves,
        # so a cancelled handshake cannot leak a slot for the life of the
        # process.
        global _stream_conns_total
        _stream_conns_total += 1
        _stream_conns_by_ip[ip] = _stream_conns_by_ip.get(ip, 0) + 1
        cursor, since, sig = last_id, since_utc, mute_sig
        live = list(subscribed)
        started = time.monotonic()
        last_beat = started
        last_recheck = started
        try:
            while True:
                if await request.is_disconnected():
                    break
                # The switch is re-read on every tick, not just at connect: a
                # stream opened while chat was live would otherwise outlive the
                # flag going dark, and CH-10 has to hold on the SERVER, not
                # because the page happened to close its EventSource.
                if not _chat_enabled():
                    break
                now = time.monotonic()
                # Ends the RESPONSE, not the feature: EventSource reconnects on
                # its own and the next connection re-resolves membership.
                if now - started >= STREAM_MAX_LIFETIME_S:
                    break
                if now - last_recheck >= STREAM_RECHECK_S:
                    last_recheck = now
                    live = [c for c in live if await channels.can_access(request, c)]
                try:
                    frames, cursor, since, sig = poll_frames(
                        account_id, live, cursor, since, sig)
                except Exception:  # noqa: BLE001
                    logger.warning("chat stream: poll failed", exc_info=True)
                    yield b'event: error\ndata: {"available": false}\n\n'
                    break
                out = [sse_frame(*f) for f in frames]
                if now - last_beat >= STREAM_KEEPALIVE_S:
                    last_beat = now
                    # The comment as well as the event: a proxy that strips
                    # unknown event types still sees bytes on the wire.
                    out.append(b": keepalive\n\n")
                    out.append(sse_frame("heartbeat",
                                         {"ts": now_utc(), "channels": live}))
                if out:
                    yield b"".join(out)
                await asyncio.sleep(STREAM_POLL_S)
        finally:
            _stream_conns_total -= 1
            n = _stream_conns_by_ip.get(ip, 1) - 1
            if n <= 0:
                _stream_conns_by_ip.pop(ip, None)
            else:
                _stream_conns_by_ip[ip] = n

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform",
                 "X-Accel-Buffering": "no", "Connection": "keep-alive"})
