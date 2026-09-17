"""Portal chat moderation (Fremkit wave 11, lane B).

Chat itself is lane A. This module is everything that happens TO a message after
it exists: a player reports one, a moderator deletes one, a moderator mutes an
author, and the queue those three feed.

Five properties matter more than the feature:

  * WHO MAY MODERATE IS ASKED, NEVER ASSUMED. `moderates(request, channel)` from
    portal_chat_channels is the single answer for "may this caller act in this
    room", and the admin role on top of it is the single answer for "anywhere".
    A guild leader is a moderator of exactly one room, and the check that says so
    runs before every write, not once at the top of the page.
  * AN ADMIN IS NOT A TARGET. A mute resolves its target by character name and
    then refuses if that account maps to the admin role. A guild leader who can
    silence the people holding the kill switch is not a moderation system.
  * ONE ACTION, ONE AUDIT ROW, NEVER THE BODY. Five actions, five action names,
    and `details` carries ids and counts only. A moderation trail that quotes
    what was said is a second copy of the thing retention exists to delete.
    A replay (a second report, a second lift, a re-resolve) writes NO row: it
    changed nothing, and a trail that logs no-ops cannot be counted.
  * ONE ACTIVE MUTE PER (ACCOUNT, CHANNEL). A new mute lifts the one it
    supersedes inside the same transaction. Without that invariant "unmute"
    means "lift one of the mutes" and the composer stays shut for reasons the
    moderator cannot see.
  * DELETION IS A TOMBSTONE. deleted_utc and deleted_by are set and the row
    STAYS, so an open message list does not renumber under a reader mid-scroll.
    Retention is what actually removes rows, and it lives in chat_retention.py
    because it must keep running while this whole feature is dark.

The dark gate is LASTSIETCH_CHAT_ENABLED, resolved through feature_flags on every call
exactly as the refinery's is, so an owner flip lands on the next request without
a restart. While it is off every route answers {ok:false, error:"chat_disabled"}
at HTTP 200: the page renders "not open yet" and never a toast.

account_id is never emitted; char_name is the only public handle. Neither is the
Discord id of whoever moderated: a guild leader reading their queue learns that
an admin acted, never which one.
"""
import portal_identity
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_db
from portal_auth import client_ip
from portal_chat_channels import (
    can_access,
    channel_kind,
    channels_for,
    is_admin,
    moderates,
)
# The dark gate is lane A's, imported rather than re-declared. Two copies of one
# kill switch is two switches: they agree until somebody edits one, and the half
# that still answers "on" is the half that keeps serving players.
from routers.portal_chat import _chat_enabled
from routers.portal import (
    _require_linked_session_json,
    _resolve_account_by_char_name,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()


# --- moderation model (stdlib only below; the test suite execs this section) --

TS_FMT = "%Y-%m-%d %H:%M:%S"

# A reason is a note to another moderator, not a document.
REASON_MAX = 200
# How long an author may take their own message back. Past it the row belongs to
# the room, and only a moderator can remove it.
AUTHOR_DELETE_SECONDS = 300
# 43200 minutes is 30 days, which is also the retention window: a mute that
# outlived every message it was about would be a sentence nobody can review.
MUTE_MINUTES_MAX = 43200
# Queue page sizes. Both lists are moderator-facing and both are bounded, so a
# server-wide incident cannot turn one GET into a whole-table read.
REPORTS_LIMIT = 200
MUTES_LIMIT = 200

# The brake on reporting. A report is the one write an ordinary player can aim at
# somebody else, so it is the one worth flooding: without these, one account can
# bury the queue faster than any moderator can read it. The OPEN cap is the real
# defence (it does not decay), the window is what stops a burst.
REPORTS_OPEN_MAX = 10
REPORTS_PER_WINDOW = 5
REPORT_WINDOW_SECONDS = 600

_TEXT = {
    "chat_disabled": "Chat is being fitted. Back soon.",
    "csrf": "Invalid CSRF token",
    "bad_request": "That moderation request was malformed.",
    "forbidden": "You cannot do that in this channel.",
    "not_found": "That message is no longer there.",
    "report_not_found": "That report is no longer in the queue.",
    "mute_not_found": "That mute is no longer in force.",
    "unknown_player": "That player is not linked to the portal.",
    "rate_limited": "You have reported a lot in the last few minutes. Try again "
                    "shortly.",
    # Same token, different cause. Waiting does not clear this one, so it does
    # not carry a retry_after: saying "try again in 600 seconds" would be a
    # promise the queue cannot keep.
    "reports_pending": "You have as many reports open as we can look at from one "
                       "player. A moderator will get to them.",
}

RESOLVE_ACTIONS = ("dismiss", "delete", "delete_and_mute")


def _now(now=None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime(TS_FMT)


def _stamp(text):
    """One admin.db timestamp as an aware datetime, or None when it cannot be
    read. A row whose stamp does not parse is treated as OLD by every caller
    below, which closes the author's five-minute window rather than opening it
    forever: an unreadable clock must never be the reason a delete is allowed."""
    text = str(text or "").strip()
    if not text:
        return None
    for fmt in (TS_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def clean_reason(value):
    """(reason_or_None, ok). Control characters out, whitespace collapsed, and
    OVER the cap is refused rather than truncated: a moderator who wrote 400
    characters of context should be told it did not fit, not have half of it
    filed under their name."""
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    text = "".join(" " if ch < " " else ch for ch in value)
    text = " ".join(text.split()).strip()
    if len(text) > REASON_MAX:
        return None, False
    return (text or None), True


def mute_minutes(value):
    """(minutes_or_None, ok). None means until lifted. A bool is not an int here
    even though Python says it is: `minutes: true` is a client bug, not a
    one-minute mute."""
    if value is None:
        return None, True
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int):
        minutes = value
    else:
        text = str(value or "").strip()
        if not text.isdigit():
            return None, False
        minutes = int(text)
    if not 1 <= minutes <= MUTE_MINUTES_MAX:
        return None, False
    return minutes, True


def account_for_char_name(name):
    """(account_id, canonical_char_name) or (None, None). The name is re-read off
    the link row rather than echoed from the request, so the audit target is the
    spelling the server holds and not the casing the moderator typed."""
    account_id = _resolve_account_by_char_name(name)
    if not account_id:
        return None, None
    return account_id, char_names_for([account_id]).get(account_id) or str(name).strip()


def char_names_for(account_ids) -> dict:
    """{account_id: char_name} for a batch, in one query. Unlinked or revoked
    accounts are simply absent; a caller renders them as an empty handle rather
    than leaking the id it could not resolve."""
    ids = sorted({int(a) for a in account_ids if a is not None})
    if not ids:
        return {}
    conn = get_db()
    try:
        rows = conn.execute(
            ("SELECT account_id, character_name FROM ls_account_links "
            "WHERE revoked_at IS NULL AND account_id IN (%s) "
            "ORDER BY linked_at ASC").replace('ls_account_links', portal_identity.link_table(conn)) % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
    finally:
        conn.close()
    return {int(r["account_id"]): r["character_name"] for r in rows
            if r["character_name"]}


def discord_for_account(account_id):
    conn = get_db()
    try:
        row = conn.execute(
            ("SELECT discord_id FROM ls_account_links "
            "WHERE account_id = ? AND revoked_at IS NULL "
            "ORDER BY linked_at DESC LIMIT 1").replace("ls_account_links", portal_identity.link_table(conn)),
            (int(account_id),),
        ).fetchone()
    finally:
        conn.close()
    return row["discord_id"] if row else None


def message_row(message_id, channel=None):
    """One message, optionally pinned to a channel. The channel is part of the
    lookup and not checked afterwards, so a moderator of room A cannot act on a
    message in room B by naming their own room in the path."""
    conn = get_db()
    try:
        if channel is None:
            return conn.execute(
                "SELECT * FROM portal_chat_messages WHERE id = ?",
                (int(message_id),)).fetchone()
        return conn.execute(
            "SELECT * FROM portal_chat_messages WHERE id = ? AND channel = ?",
            (int(message_id), channel)).fetchone()
    finally:
        conn.close()


def _mark_deleted(conn, message_id, by_kind, stamp) -> bool:
    """Tombstone one message on a caller-supplied connection, inside whatever
    transaction the caller has open. False when it was already deleted. The
    deleted_utc IS NULL test is in the WHERE clause, so two moderators pressing
    at once produce one winner and one `already` rather than two audit rows."""
    return conn.execute(
        "UPDATE portal_chat_messages SET deleted_utc = ?, deleted_by = ? "
        "WHERE id = ? AND deleted_utc IS NULL",
        (stamp, by_kind, int(message_id))).rowcount > 0


def mark_deleted(message_id, by_kind, now=None) -> bool:
    """_mark_deleted in a transaction of its own, for the delete route."""
    conn = get_db()
    try:
        deleted = _mark_deleted(conn, message_id, by_kind, _now(now))
        conn.commit()
        return deleted
    finally:
        conn.close()


def _insert_mute(conn, account_id, channel, by_kind, by_discord, reason, minutes,
                 moment):
    """The mute write on a caller-supplied connection, inside the caller's
    transaction. Returns (mute_id, until_utc) or (None, None) when it is refused.

    TWO rules live here, and they live here ONCE because both the mute route and
    a delete_and_mute resolve go through this function:

      * ONE ACTIVE MUTE PER (ACCOUNT, CHANNEL). Whatever is in force on the pair
        is lifted in the same transaction, so Lift always means "lift the mute",
        never "lift one of the mutes".
      * A LEADER NEVER OVERRIDES AN ADMIN. A guild leader superseding an admin's
        30-day mute with a one-minute one is an unmute with extra steps, so a
        non-admin facing an admin's row is refused instead. Read and refused
        INSIDE the transaction: a check taken beforehand could be overtaken by an
        admin muting between the read and the write.
    """
    stamp = _now(moment)
    if by_kind != "admin":
        blocking = conn.execute(
            "SELECT id FROM portal_chat_mutes WHERE account_id = ? AND channel = ? "
            "AND lifted_utc IS NULL AND by_kind = 'admin' LIMIT 1",
            (int(account_id), channel)).fetchone()
        if blocking is not None:
            return None, None
    until = _now(moment + timedelta(minutes=minutes)) if minutes else None
    conn.execute(
        "UPDATE portal_chat_mutes SET lifted_utc = ?, lifted_by = ? "
        "WHERE account_id = ? AND channel = ? AND lifted_utc IS NULL",
        (stamp, str(by_discord), int(account_id), channel))
    cur = conn.execute(
        """INSERT INTO portal_chat_mutes
               (account_id, channel, by_kind, by_discord, reason, until_utc,
                created_utc)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (int(account_id), channel, by_kind, str(by_discord), reason, until, stamp))
    return int(cur.lastrowid), until


def existing_report_id(message_id, reporter_account_id):
    """This reporter's report on this message, or None. Read before the brake so
    a second click on a message you already reported answers `already` instead of
    being counted against you and refused."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM portal_chat_reports "
            "WHERE message_id = ? AND reporter_account_id = ?",
            (int(message_id), int(reporter_account_id))).fetchone()
    finally:
        conn.close()
    return int(row["id"]) if row else None


def report_brake(reporter_account_id, now=None):
    """(None, None) when this account may file another report, else
    (message, retry_after) where retry_after is seconds or None.

    Two limits with different shapes. The window is a burst brake and it decays,
    so it can honestly say when to come back. The open cap does not decay: it
    clears when a MODERATOR acts, and no number of seconds is the right answer,
    so that refusal carries no retry_after rather than an invented one."""
    moment = now or datetime.now(timezone.utc)
    since = _now(moment - timedelta(seconds=REPORT_WINDOW_SECONDS))
    conn = get_db()
    try:
        open_count = conn.execute(
            "SELECT COUNT(*) AS n FROM portal_chat_reports "
            "WHERE reporter_account_id = ? AND state = 'open'",
            (int(reporter_account_id),)).fetchone()["n"]
        recent = conn.execute(
            "SELECT created_utc FROM portal_chat_reports "
            "WHERE reporter_account_id = ? AND created_utc > ? "
            "ORDER BY created_utc ASC",
            (int(reporter_account_id), since)).fetchall()
    finally:
        conn.close()
    if int(open_count) >= REPORTS_OPEN_MAX:
        return _TEXT["reports_pending"], None
    if len(recent) >= REPORTS_PER_WINDOW:
        oldest = _stamp(recent[0]["created_utc"])
        if oldest is None:
            return _TEXT["rate_limited"], REPORT_WINDOW_SECONDS
        left = REPORT_WINDOW_SECONDS - (moment - oldest).total_seconds()
        return _TEXT["rate_limited"], max(1, int(left) + 1)
    return None, None


def insert_report(message_id, channel, reporter_account_id, reason, now=None):
    """(report_id, already). The UNIQUE (message_id, reporter_account_id) index is
    the contract; the SELECT and the INSERT share one immediate transaction so
    two tabs cannot both read "no row" and then both insert."""
    stamp = _now(now)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM portal_chat_reports "
            "WHERE message_id = ? AND reporter_account_id = ?",
            (int(message_id), int(reporter_account_id))).fetchone()
        if row is not None:
            conn.rollback()
            return int(row["id"]), True
        cur = conn.execute(
            """INSERT INTO portal_chat_reports
                   (message_id, channel, reporter_account_id, reason, created_utc, state)
               VALUES (?, ?, ?, ?, ?, 'open')""",
            (int(message_id), channel, int(reporter_account_id), reason, stamp))
        conn.commit()
        return int(cur.lastrowid), False
    finally:
        conn.close()


def insert_mute(account_id, channel, by_kind, by_discord, reason, minutes,
                now=None):
    """_insert_mute in a transaction of its own, for the mute route.
    (mute_id, until_utc) or (None, None) when a leader would override an admin."""
    moment = now or datetime.now(timezone.utc)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        mute_id, until = _insert_mute(conn, account_id, channel, by_kind,
                                      by_discord, reason, minutes, moment)
        if mute_id is None:
            conn.rollback()
            return None, None
        conn.commit()
        return mute_id, until
    finally:
        conn.close()


def mute_row(mute_id):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM portal_chat_mutes WHERE id = ?",
                            (int(mute_id),)).fetchone()
    finally:
        conn.close()


def lift_mute(mute_id, by_discord, now=None) -> bool:
    """False when it was already lifted. Same shape as mark_deleted and for the
    same reason: the test is in the WHERE clause, so the row decides, not a read
    taken a moment earlier."""
    stamp = _now(now)
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE portal_chat_mutes SET lifted_utc = ?, lifted_by = ? "
            "WHERE id = ? AND lifted_utc IS NULL",
            (stamp, str(by_discord), int(mute_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def apply_resolution(report_id, action, by_kind, by_discord, message_id,
                     target_account_id, channel, reason, minutes, now=None):
    """Close one report AND carry out what it decided, in one transaction.

    Returns (already, deleted, mute_id, error). Three separate transactions could
    interleave with a concurrent delete or a concurrent mute and leave a report
    marked actioned with nothing done, or a message tombstoned under a report
    somebody else dismissed a moment earlier. The state = 'open' test is in the
    UPDATE, so the row decides who won and the loser writes nothing at all."""
    moment = now or datetime.now(timezone.utc)
    stamp = _now(moment)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        closed = conn.execute(
            "UPDATE portal_chat_reports SET state = ?, resolved_by = ?, "
            "resolved_utc = ? WHERE id = ? AND state = 'open'",
            ("dismissed" if action == "dismiss" else "actioned", str(by_discord),
             stamp, int(report_id))).rowcount
        if not closed:
            conn.rollback()
            return True, False, None, None

        mute_id = None
        deleted = False
        if action in ("delete", "delete_and_mute"):
            deleted = _mark_deleted(conn, message_id, by_kind, stamp)
        if action == "delete_and_mute":
            mute_id, _until = _insert_mute(conn, target_account_id, channel,
                                           by_kind, by_discord, reason, minutes,
                                           moment)
            if mute_id is None:
                # A leader whose mute would override an admin's takes NOTHING
                # with it: the report goes back in the queue and the message is
                # un-deleted, because the whole resolve is one decision.
                conn.rollback()
                return False, False, None, "forbidden"
        conn.commit()
        return False, deleted, mute_id, None
    finally:
        conn.close()


def report_row(report_id):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM portal_chat_reports WHERE id = ?",
                            (int(report_id),)).fetchone()
    finally:
        conn.close()


def _channel_clause(channels):
    """('', []) for every channel, or a bound IN clause. `channels is None` means
    an admin and is the ONLY way to read across rooms; an empty list is a
    moderator of nothing and must match no row rather than all of them."""
    if channels is None:
        return "", []
    if not channels:
        return " AND 1 = 0", []
    return (" AND r.channel IN (%s)" % ",".join("?" * len(channels))), list(channels)


def list_reports(channels, state="open", limit=REPORTS_LIMIT):
    """The queue, open first. Each row carries the reported message so the
    moderator can judge it without a second round trip, and the reporter's
    handle so a serial reporter is visible."""
    where, params = _channel_clause(channels)
    if state != "all":
        where += " AND r.state = 'open'"
    sql = ("""SELECT r.id AS id, r.message_id AS message_id, r.channel AS channel,
                     r.reason AS reason, r.created_utc AS created_utc,
                     r.state AS state, r.resolved_utc AS resolved_utc,
                     r.reporter_account_id AS reporter_account_id,
                     m.char_name AS message_char_name, m.body AS message_body,
                     m.account_id AS message_account_id,
                     m.created_utc AS message_created_utc,
                     m.deleted_utc AS message_deleted_utc
                FROM portal_chat_reports r
                LEFT JOIN portal_chat_messages m ON m.id = r.message_id
               WHERE 1 = 1""" + where +
           """ ORDER BY (r.state = 'open') DESC, r.created_utc DESC, r.id DESC
               LIMIT ?""")
    conn = get_db()
    try:
        rows = conn.execute(sql, params + [int(limit)]).fetchall()
    finally:
        conn.close()
    names = char_names_for([r["reporter_account_id"] for r in rows])
    out = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "channel": r["channel"],
            "reason": r["reason"],
            "created_utc": r["created_utc"],
            "state": r["state"],
            "resolved_utc": r["resolved_utc"],
            "reporter_char_name": names.get(int(r["reporter_account_id"]), ""),
            # The message as it stands. A deleted message keeps its body HERE and
            # only here: a moderator deciding whether to mute has to be able to
            # read what was said, which is exactly what the public list refuses.
            "message": {
                "id": int(r["message_id"]),
                "char_name": r["message_char_name"] or "",
                "body": r["message_body"] or "",
                "created_utc": r["message_created_utc"],
                "deleted": r["message_deleted_utc"] is not None,
            } if r["message_char_name"] is not None else None,
        })
    return out


def list_mutes(channels, limit=MUTES_LIMIT, now=None):
    """Every mute still in force, newest first. WHO muted is deliberately not in
    the payload: a guild leader reading this learns that an admin acted, never
    which admin, and by_kind is all the UI needs to render the row."""
    where, params = _channel_clause(channels)
    sql = ("""SELECT r.id AS id, r.account_id AS account_id, r.channel AS channel,
                     r.by_kind AS by_kind, r.reason AS reason,
                     r.until_utc AS until_utc, r.created_utc AS created_utc
                FROM portal_chat_mutes r
               WHERE r.lifted_utc IS NULL""" + where +
           " ORDER BY r.created_utc DESC, r.id DESC LIMIT ?")
    conn = get_db()
    try:
        rows = conn.execute(sql, params + [int(limit)]).fetchall()
    finally:
        conn.close()
    names = char_names_for([r["account_id"] for r in rows])
    moment = now or datetime.now(timezone.utc)
    out = []
    for r in rows:
        until = _stamp(r["until_utc"])
        out.append({
            "id": int(r["id"]),
            "char_name": names.get(int(r["account_id"]), ""),
            "channel": r["channel"],
            "by_kind": r["by_kind"],
            "reason": r["reason"],
            "until_utc": r["until_utc"],
            "created_utc": r["created_utc"],
            "active": until is None or until > moment,
        })
    return out


def _audit(discord_id, audit_action, target, ip, details) -> None:
    """One audit row per moderation action. auth is imported at call time so the
    stdlib-only suite can execute this section without dragging the admin auth
    stack in behind it, and the action is a STRING LITERAL at every call site so
    the Audit page's scanner can offer it as a filter.

    `details` never carries a message body. A failing audit write is logged and
    never turned into a 500 after the moderation write already committed."""
    try:
        from auth import audit_log
        audit_log(None, "portal:%s" % discord_id, audit_action, str(target), ip,
                  details=json.dumps(details, separators=(",", ":")), success=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("portal chat: audit write failed for %s: %s",
                       audit_action, exc)


# --- routes ------------------------------------------------------------------
# Registration order is load-bearing: /portal/chat/mute and the /moderation
# literals must be matched before anything parametrised on the channel.


def _err(token, status=200, message=None, **extra):
    """The refusal envelope. Everything chat refuses is a soft 200 carrying a
    token, the storage family's shape, so the page renders a sentence and never
    a toast. Two refusals keep their own status and neither comes through here:
    CSRF is a 403 because it is not a player-visible state, and the 401 for an
    anonymous caller is the gate's own.

    `message` overrides the token's default sentence, for the one token whose two
    causes need two of them; `extra` carries retry_after. Built here rather than
    patched onto a rendered response, which already has its content-length."""
    payload = {"ok": False, "error": token,
               "message": message or _TEXT.get(token, _TEXT["bad_request"])}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


def _moderation_gate(request):
    """(gate, None) or (None, refusal): the linked session and the dark flag, in
    the one order every route here uses. The session is checked FIRST so an
    anonymous caller gets the 401 envelope whether chat is open or not: a dark
    feature must not become an oracle for who is signed in."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return None, early
    if not _chat_enabled():
        return None, _err("chat_disabled")
    return gate, None


async def _moderation_write(request):
    """(gate, body, None) or (None, None, refusal). Gate, dark flag, CSRF."""
    gate, early = _moderation_gate(request)
    if early is not None:
        return None, None, early
    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return None, None, _v2_err("csrf", _TEXT["csrf"], status=403)
    return gate, body, None


def _row_id(raw):
    text = str(raw or "").strip()
    return int(text) if text.isdigit() and len(text) <= 18 else None


async def _delete_kind(request, discord_id, account_id, channel, row, now=None):
    """Which authority is deleting this message, or None for nobody.

    Order is the ruling's order: an admin anywhere, then a moderator of THIS
    room, then the author inside the five-minute window. The author branch is
    last so a guild leader deleting their own old message is still filed as
    'leader', which is what the audit trail should say happened."""
    if is_admin(discord_id):
        return "admin"
    if await moderates(request, channel):
        return "leader"
    if int(row["account_id"]) != int(account_id):
        return None
    created = _stamp(row["created_utc"])
    if created is None:
        return None
    moment = now or datetime.now(timezone.utc)
    if (moment - created).total_seconds() > AUTHOR_DELETE_SECONDS:
        return None
    return "author"


async def _moderated_channels(request, discord_id):
    """None for an admin (every channel), else the channels this caller
    moderates. An empty list is a caller who moderates nothing, which every
    reader below turns into `forbidden` rather than an empty queue: telling
    someone their queue is empty is a different claim from telling them they
    have no queue."""
    if is_admin(discord_id):
        return None
    out = []
    for entry in (await channels_for(request) or []):
        if entry.get("can_moderate") and entry.get("id"):
            out.append(entry["id"])
    return out


@router.post("/portal/chat/{channel}/messages/{message_id}/report")
async def portal_chat_report(request: Request, channel: str, message_id: str):
    """Report a message to the moderators. Any member of the room, one report per
    reporter per message: a second one answers `already` and writes neither a
    row nor an audit line. Auth: linked session + CSRF."""
    gate, body, early = await _moderation_write(request)
    if early is not None:
        return early
    _session, discord_id, account_id, _row = gate

    mid = _row_id(message_id)
    if mid is None or channel_kind(channel) is None:
        return _err("bad_request")
    if not await can_access(request, channel):
        return _err("forbidden")

    reason, ok = clean_reason((body or {}).get("reason"))
    if not ok:
        return _err("bad_request")

    row = message_row(mid, channel)
    if row is None:
        return _err("not_found")

    # A re-click on a message you already reported is answered before the brake,
    # so it costs nothing and is never counted against you.
    existing = existing_report_id(mid, account_id)
    if existing is not None:
        return _v2_ok({"report_id": existing, "already": True})

    message, retry_after = report_brake(account_id)
    if message is not None:
        return _err("rate_limited", message=message, retry_after=retry_after)

    report_id, already = insert_report(mid, channel, account_id, reason)
    if already:
        return _v2_ok({"report_id": report_id, "already": True})

    _audit(discord_id, audit_action="portal_chat_report",
           target=row["char_name"], ip=client_ip(request),
           details={"channel": channel, "message_id": mid,
                    "report_id": report_id,
                    "reporter_account_id": int(account_id),
                    "has_reason": reason is not None})
    return _v2_ok({"report_id": report_id, "already": False})


@router.post("/portal/chat/{channel}/messages/{message_id}/delete")
async def portal_chat_delete(request: Request, channel: str, message_id: str):
    """Remove a message. An admin anywhere, a guild leader inside their own guild
    channel, the author within five minutes. The row stays with deleted_utc and
    deleted_by set, so an open message list does not renumber under a reader.
    Auth: linked session + CSRF."""
    gate, _body, early = await _moderation_write(request)
    if early is not None:
        return early
    _session, discord_id, account_id, _row = gate

    mid = _row_id(message_id)
    if mid is None or channel_kind(channel) is None:
        return _err("bad_request")

    row = message_row(mid, channel)
    if row is None:
        return _err("not_found")

    by_kind = await _delete_kind(request, discord_id, account_id, channel, row)
    if by_kind is None:
        return _err("forbidden")

    if not mark_deleted(mid, by_kind):
        return _v2_ok({"message_id": mid, "already": True})

    _audit(discord_id, audit_action="portal_chat_delete",
           target=row["char_name"], ip=client_ip(request),
           details={"channel": channel, "message_id": mid, "by_kind": by_kind,
                    "author_account_id": int(row["account_id"])})
    return _v2_ok({"message_id": mid, "deleted_by": by_kind, "already": False})


async def _mute_scope(request, discord_id, channel):
    """('admin'|'leader', None) or (None, token): may this caller mute in this
    channel, and under whose authority.

    '' is everywhere and is ADMIN ONLY. A leader is held to `guild:` twice over:
    once by moderates(), which is lane A's answer, and once by the prefix test
    here, which is this module's. Two checks because the blast radius of the
    first one being wrong is a guild leader silencing the whole server."""
    if is_admin(discord_id):
        if channel and not await moderates(request, channel):
            return None, "bad_request"
        return "admin", None
    if not channel:
        return None, "forbidden"
    if not channel.startswith("guild:") or not await moderates(request, channel):
        return None, "forbidden"
    return "leader", None


@router.post("/portal/chat/mute")
async def portal_chat_mute(request: Request):
    """Silence one player. `{char_name, channel, minutes, reason}`; channel '' is
    everywhere and is admin only, minutes null is until lifted. A target holding
    the admin role is refused. Auth: linked session + CSRF."""
    gate, body, early = await _moderation_write(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate
    body = body or {}

    channel = body.get("channel")
    channel = "" if channel is None else channel
    if not isinstance(channel, str) or (channel and channel_kind(channel) is None):
        return _err("bad_request")

    by_kind, token = await _mute_scope(request, discord_id, channel)
    if token is not None:
        return _err(token)

    minutes, ok = mute_minutes(body.get("minutes"))
    if not ok:
        return _err("bad_request")
    reason, ok = clean_reason(body.get("reason"))
    if not ok:
        return _err("bad_request")

    raw_name = body.get("char_name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return _err("bad_request")
    target_account, target_name = account_for_char_name(raw_name)
    if target_account is None:
        return _err("unknown_player")

    # An admin is never a target. Resolved through the same server-side mapping
    # the caller's own role came from, so a leader cannot silence the people who
    # hold the kill switch and an admin cannot silence another admin by mistake.
    target_discord = discord_for_account(target_account)
    if target_discord and is_admin(target_discord):
        return _err("forbidden")

    mute_id, until = insert_mute(target_account, channel, by_kind, discord_id,
                                 reason, minutes)
    if mute_id is None:
        # A leader whose mute would have superseded an admin's. Refused rather
        # than stored alongside it, which would leave two mutes in force on one
        # pair and make Lift ambiguous.
        return _err("forbidden")
    _audit(discord_id, audit_action="portal_chat_mute", target=target_name,
           ip=client_ip(request),
           details={"channel": channel, "mute_id": mute_id, "by_kind": by_kind,
                    "target_account_id": int(target_account),
                    "minutes": minutes, "until_utc": until,
                    "has_reason": reason is not None})
    return _v2_ok({"mute": {"id": mute_id, "char_name": target_name,
                            "channel": channel, "until_utc": until,
                            "by_kind": by_kind, "reason": reason}})


@router.post("/portal/chat/unmute")
async def portal_chat_unmute(request: Request):
    """Lift a mute by id. An admin lifts any; a leader lifts only one that sits in
    a channel they moderate, which excludes the '' everywhere mutes an admin
    sets. Auth: linked session + CSRF."""
    gate, body, early = await _moderation_write(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate

    raw = (body or {}).get("mute_id")
    mute_id = _row_id(raw)
    if mute_id is None:
        return _err("bad_request")

    row = mute_row(mute_id)
    if row is None:
        return _err("mute_not_found")

    # The row's own channel AND the row's own by_kind decide, never anything the
    # caller named. A guild leader lifting an admin's mute would be an unmute
    # with extra steps, so the authority that set it has to be one they outrank.
    if not is_admin(discord_id):
        if not row["channel"] or not await moderates(request, row["channel"]):
            return _err("forbidden")
        if row["by_kind"] != "leader":
            return _err("forbidden")

    if not lift_mute(mute_id, discord_id):
        return _v2_ok({"mute_id": mute_id, "already": True})

    names = char_names_for([row["account_id"]])
    _audit(discord_id, audit_action="portal_chat_unmute",
           target=names.get(int(row["account_id"]), ""), ip=client_ip(request),
           details={"channel": row["channel"], "mute_id": mute_id,
                    "target_account_id": int(row["account_id"])})
    return _v2_ok({"mute_id": mute_id, "already": False})


@router.get("/portal/chat/moderation/reports")
async def portal_chat_moderation_reports(request: Request):
    """The report queue, open first. An admin sees every channel; a guild leader
    sees only the rooms they moderate; everyone else is refused. Auth: linked
    session."""
    gate, early = _moderation_gate(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate

    channels = await _moderated_channels(request, discord_id)
    if channels is not None and not channels:
        return _err("forbidden")

    state = "all" if str(request.query_params.get("state") or "").strip() == "all" \
        else "open"
    return _v2_ok({"state": state, "admin": channels is None,
                   "reports": list_reports(channels, state)})


@router.get("/portal/chat/moderation/mutes")
async def portal_chat_moderation_mutes(request: Request):
    """Every mute still in force, scoped the same way the queue is. Auth: linked
    session."""
    gate, early = _moderation_gate(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate

    channels = await _moderated_channels(request, discord_id)
    if channels is not None and not channels:
        return _err("forbidden")

    return _v2_ok({"admin": channels is None, "mutes": list_mutes(channels)})


@router.post("/portal/chat/moderation/reports/{report_id}/resolve")
async def portal_chat_resolve(request: Request, report_id: str):
    """Close one report: `{action}` in dismiss | delete | delete_and_mute, the
    last one carrying `minutes` and `reason`.

    ONE audit row for the whole resolve, whatever it did. The delete and the mute
    it performs are parts of this action, not actions of their own, and filing
    them separately would make a queue of 40 reports read as 120 moderation
    events. Auth: linked session + CSRF."""
    gate, body, early = await _moderation_write(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate
    body = body or {}

    rid = _row_id(report_id)
    if rid is None:
        return _err("bad_request")
    action = str(body.get("action") or "").strip()
    if action not in RESOLVE_ACTIONS:
        return _err("bad_request")

    report = report_row(rid)
    if report is None:
        return _err("report_not_found")

    channel = report["channel"]
    if not is_admin(discord_id) and not await moderates(request, channel):
        return _err("forbidden")
    by_kind = "admin" if is_admin(discord_id) else "leader"

    minutes = None
    reason = None
    if action == "delete_and_mute":
        minutes, ok = mute_minutes(body.get("minutes"))
        if not ok:
            return _err("bad_request")
        reason, ok = clean_reason(body.get("reason"))
        if not ok:
            return _err("bad_request")
        # A leader may only mute inside their own guild channel, and the report's
        # channel is the one they would be muting in. Checked BEFORE anything is
        # written, so a refused mute never leaves a deleted message behind it.
        _scope, token = await _mute_scope(request, discord_id, channel)
        if token is not None:
            return _err(token)

    message = message_row(int(report["message_id"]))
    if action in ("delete", "delete_and_mute") and message is None:
        return _err("not_found")

    if action == "delete_and_mute":
        target_discord = discord_for_account(message["account_id"])
        if target_discord and is_admin(target_discord):
            return _err("forbidden")

    already, deleted, mute_id, error = apply_resolution(
        rid, action, by_kind, discord_id, int(report["message_id"]),
        int(message["account_id"]) if message is not None else None,
        channel, reason, minutes)
    if error is not None:
        return _err(error)
    if already:
        return _v2_ok({"report_id": rid, "already": True})

    _audit(discord_id, audit_action="portal_chat_resolve",
           target=(message["char_name"] if message is not None else ""),
           ip=client_ip(request),
           details={"channel": channel, "report_id": rid,
                    "message_id": int(report["message_id"]),
                    "action": action, "by_kind": by_kind, "deleted": deleted,
                    "mute_id": mute_id, "minutes": minutes})
    return _v2_ok({"report_id": rid, "action": action, "deleted": deleted,
                   "mute_id": mute_id, "already": False})
