"""Portal identity codes + own-activity log (Fremkit wave 4).

An identity code is a short typeable handle for one portal identity (the Discord
id behind the session): 8 symbols drawn from a 32-symbol alphabet with 0/1/I/O
removed, so a code read off a screen or said out loud cannot be mistyped into
somebody else's. It exists so one player can hand another something to type
without either side seeing a Discord id or an account_id.

A code is a VALUE, never a URL. Nothing here emits, builds or accepts one with a
scheme or a slash: the payload regex is the whole accepted surface, and it is
applied before any query runs.

All state is admin.db (portal_identity_codes + portal_code_lookups); zero
game-DB touch. Session gate, refusal envelope and CSRF check are reused from
routers.portal (loaded first by main.py) so the auth story matches the rest of
the portal exactly. Wave 4 shipped mint / rotate / lookup / display; wave 7 adds
resolve_code, the one resolver that returns an account_id, so a code can address
a transfer without ever travelling any further than this module.
"""
import portal_identity
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from database import get_db
from routers.portal import (
    _require_linked_session_json,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()


# --- code model (stdlib only below; the test suite execs this section) -------

# 32 symbols: A-Z minus I and O, 2-9. 0/1/I/O are dropped as a pair-wise
# confusion set, so a handwritten or spoken code has one reading.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
# The contract payload (V3 proposal 3.2). Wider than the mint alphabet by I and
# O deliberately: a typed I or O is well-formed input that simply matches no row,
# which is a cleaner refusal than "bad code" for a player who mis-heard one.
CODE_RE = re.compile(r"^[A-Z2-9]{8}$")

# Uniqueness comes from the partial unique index, not from a pre-check, so two
# concurrent mints cannot both pass. A collision is ~1 in 10^12 per attempt.
MINT_ATTEMPTS = 6

ROTATE_COOLDOWN_SECONDS = 600
LOOKUP_WINDOW_MINUTES = 10
LOOKUP_MAX_PER_WINDOW = 20

ACTIVITY_DEFAULT_LIMIT = 50
ACTIVITY_MAX_LIMIT = 200
# Per-source scan cap. Each kind reads at most this many of its own newest rows;
# the merge then trims to the caller's limit.
ACTIVITY_SCAN = 200

# Karum events a player did, not the machinery around them. Attempts, failures,
# retries and operator recoveries stay out: an activity log that reports every
# internal step reads as breakage.
_KARUM_EVENT_COPY = {
    "list_applied": "Listed an item on the Karum",
    "buy_applied": "Bought an item on the Karum",
    "cancel_applied": "Cancelled a Karum listing",
    "request_post_applied": "Posted a wanted order on the Karum",
    "request_fill_applied": "Filled a wanted order on the Karum",
    "request_cancelled": "Cancelled a wanted order",
}

_MAIL_COPY = {
    "notification": "Received a notification",
    "user": "Received a message",
    "gift": "Received a gift",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _window_start(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")


def generate_code() -> str:
    """One fresh code. secrets, never random: this is a shareable identity."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw) -> str:
    """Trim + upper-case the caller's input. Nothing else: a value carrying a
    scheme, a slash or any other character fails CODE_RE below rather than being
    cleaned into something that matches."""
    return str(raw or "").strip().upper()


def valid_code(value: str) -> bool:
    return bool(CODE_RE.fullmatch(value or ""))


def active_code_row(identity: str):
    """The identity's live code as (code, created_at), or None."""
    if not identity:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT code, created_at FROM portal_identity_codes
                WHERE identity = ? AND active = 1""",
            (str(identity),),
        ).fetchone()
    finally:
        conn.close()
    return (row["code"], row["created_at"]) if row else None


def mint_code(identity: str):
    """Insert a fresh active code for an identity that has none. Returns
    (code, created_at) or None if every attempt collided. The IntegrityError
    path also covers a concurrent mint of the SAME identity (the partial unique
    index on identity), which is why it re-reads instead of just retrying."""
    if not identity:
        return None
    conn = get_db()
    try:
        for _ in range(MINT_ATTEMPTS):
            code = generate_code()
            created_at = _now()
            try:
                conn.execute(
                    """INSERT INTO portal_identity_codes
                           (identity, code, created_at, active)
                       VALUES (?, ?, ?, 1)""",
                    (str(identity), code, created_at),
                )
                conn.commit()
                return code, created_at
            except sqlite3.Error:
                # a collision (IntegrityError) retries; a locked file (OperationalError)
                # must reach the route's code_unavailable refusal, never a 500 (review 9/3)
                conn.rollback()
                row = conn.execute(
                    """SELECT code, created_at FROM portal_identity_codes
                        WHERE identity = ? AND active = 1""",
                    (str(identity),),
                ).fetchone()
                if row:
                    return row["code"], row["created_at"]
    finally:
        conn.close()
    logger.warning("portal codes: mint gave up after %d attempts", MINT_ATTEMPTS)
    return None


def rotate_cooldown_remaining(identity: str) -> int:
    """Seconds left before this identity may rotate again, 0 when clear. Measured
    from the last ROTATION, the moment the previous code stopped being current,
    so a freshly minted code can be rotated once right away: a mint is not a
    rotation (live QA 2026-09-03). Read off the rows, so the cap survives a restart."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT MAX(rotated_at) FROM portal_identity_codes
                WHERE identity = ? AND active = 0""",
            (str(identity),),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return 0
    try:
        last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return max(int(ROTATE_COOLDOWN_SECONDS - elapsed), 0)


def rotate_code(identity: str):
    """Retire the identity's current code and mint a new one. The retire and the
    insert share one transaction: a half-applied rotation would either strand the
    identity with no code or leave two live ones, and the partial unique index
    would then refuse every later mint."""
    if not identity:
        return None
    conn = get_db()
    try:
        for _ in range(MINT_ATTEMPTS):
            code = generate_code()
            stamp = _now()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """UPDATE portal_identity_codes
                          SET active = 0, rotated_at = ?
                        WHERE identity = ? AND active = 1""",
                    (stamp, str(identity)),
                )
                conn.execute(
                    """INSERT INTO portal_identity_codes
                           (identity, code, created_at, active)
                       VALUES (?, ?, ?, 1)""",
                    (str(identity), code, stamp),
                )
                conn.commit()
                return code, stamp
            except sqlite3.Error:
                # a collision (IntegrityError) retries; a locked file (OperationalError)
                # must reach the route's code_unavailable refusal, never a 500 (review 9/3)
                conn.rollback()
    finally:
        conn.close()
    logger.warning("portal codes: rotate gave up after %d attempts", MINT_ATTEMPTS)
    return None


def lookups_in_window(identity: str) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_code_lookups
                WHERE identity = ? AND looked_at >= ?""",
            (str(identity), _window_start(LOOKUP_WINDOW_MINUTES * 60)),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"] if row else 0)


def record_lookup(identity: str, found: bool) -> None:
    """Append-only. The looked-up code is deliberately NOT stored: this log
    answers "how many did this identity try", never "which"."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO portal_code_lookups (identity, looked_at, found) VALUES (?, ?, ?)",
            (str(identity), _now(), 1 if found else 0),
        )
        conn.commit()
    finally:
        conn.close()


def display_name_for_code(code: str):
    """The public handle behind a live code, or None. Character name only: the
    identity, the account_id and the profile row itself never leave this
    function. Newest linked character, the same pick the public directory uses.

    Directory opt-out (portal_player_profile.listed) is not consulted here on
    purpose. That flag governs being FOUND by browsing; a code is not browsable
    and only its owner can hand it out, so the code itself is the consent."""
    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT COALESCE(NULLIF(l.character_name, ''), p.char_name) AS display_name
                 FROM portal_identity_codes c
                 JOIN ls_account_links l ON l.discord_id = c.identity
                                         AND l.revoked_at IS NULL
                 LEFT JOIN portal_player_profile p ON p.account_id = l.account_id
                WHERE c.code = ? AND c.active = 1
                ORDER BY l.linked_at DESC
                LIMIT 1""").replace("ls_account_links", portal_identity.link_table(conn)),
            (code,),
        ).fetchone()
    finally:
        conn.close()
    return row["display_name"] if row and row["display_name"] else None


def resolve_code(code, requester_identity):
    """A live code to the identity behind it:
    {"identity", "account_id", "display_name"}, or None.

    The ONLY resolver that returns an account_id. It exists so a transfer can be
    addressed by code without the code ever reaching a URL, a relay payload, an
    audit row or a log line: the caller resolves inside the request and carries
    account ids from there.

    Malformed input is refused before any query runs, so a walk of the code space
    cannot even reach the lookup log. One resolution spends exactly one lookup
    slot whether or not it found anything, so a miss costs a caller the same as a
    hit. Newest live link wins, the same pick display_name_for_code makes; an
    inactive (rotated) code resolves to nothing, immediately. Never raises: an
    unreadable admin.db becomes None, which the caller refuses on, rather than a
    500 out of the middle of a write path."""
    candidate = normalize_code(code)
    if not valid_code(candidate) or not requester_identity:
        return None
    try:
        conn = get_db()
        try:
            row = conn.execute(
                ("""SELECT c.identity AS identity, l.account_id AS account_id,
                          COALESCE(NULLIF(l.character_name, ''), p.char_name) AS display_name
                     FROM portal_identity_codes c
                     JOIN ls_account_links l ON l.discord_id = c.identity
                                             AND l.revoked_at IS NULL
                     LEFT JOIN portal_player_profile p ON p.account_id = l.account_id
                    WHERE c.code = ? AND c.active = 1
                    ORDER BY l.linked_at DESC
                    LIMIT 1""").replace("ls_account_links", portal_identity.link_table(conn)),
                (candidate,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("portal codes: resolve failed", exc_info=True)
        return None
    # The throttle record is bookkeeping: a write that fails (a locked admin.db)
    # must not turn a resolution that succeeded into "code unknown".
    try:
        record_lookup(requester_identity, row is not None)
    except Exception:  # noqa: BLE001
        logger.warning("portal codes: lookup record failed", exc_info=True)
    if row is None:
        return None
    return {"identity": str(row["identity"]),
            "account_id": int(row["account_id"]),
            "display_name": row["display_name"] or None}


def transfer_remaining(requester_identity, resolved):
    """(sends left today, sends left to this player) for the confirm step, or
    (None, None) when nothing resolved. Additive detail only: the caps that
    decide a transfer are re-counted on the write, and the game host's own caps
    are the backstop, so a stale count here can never authorise anything."""
    if not resolved:
        return None, None
    try:
        import portal_transfer_events
        counts = portal_transfer_events.caps(requester_identity, resolved["identity"])
        return (max(int(counts["daily_cap"]) - int(counts["daily_used"]), 0),
                max(int(counts["pair_cap"]) - int(counts["pair_used"]), 0))
    except Exception as exc:   # an absent mirror table, like the activity merge
        logger.debug("portal codes: transfer caps unavailable: %s", exc)
        return None, None


def own_account_ids(identity: str) -> list:
    """Every account the session's identity still has a live link to. The
    activity log is scoped to exactly this set and nothing else."""
    if not identity:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            ("""SELECT DISTINCT account_id FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL""").replace("ls_account_links", portal_identity.link_table(conn)),
            (str(identity),),
        ).fetchall()
    finally:
        conn.close()
    return [int(r["account_id"]) for r in rows]


def _norm_ts(value) -> str:
    """Timestamps land here in two shapes: the portal's own
    'YYYY-MM-DD HH:MM:SS' and ls_reward_claims' ISO-8601. Fold to the former so
    the cross-source merge sorts on one format instead of mixing 'T' and ' '."""
    text = str(value or "").strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:19]


def _reward_items(conn, ids, marks):
    out = []
    for row in conn.execute(
        """SELECT created_at, reward_kind, amount FROM ls_reward_claims
            WHERE account_id IN (%s)
            ORDER BY created_at DESC LIMIT ?""" % marks,
        ids + [ACTIVITY_SCAN],
    ):
        if row["reward_kind"] == "daily_solari":
            summary = "Claimed {:,} Solari from the daily reward".format(int(row["amount"] or 0))
        elif str(row["reward_kind"]).startswith("monthly"):
            summary = "Claimed the monthly reward"
        else:
            summary = "Claimed the weekly reward"
        out.append({"t": _norm_ts(row["created_at"]), "kind": "reward", "summary": summary})
    return out


def _karum_items(conn, ids, marks):
    out = []
    events = list(_KARUM_EVENT_COPY)
    for row in conn.execute(
        """SELECT created_at, event FROM portal_karum_events
            WHERE account_id IN (%s) AND event IN (%s)
            ORDER BY created_at DESC LIMIT ?""" % (marks, ",".join("?" * len(events))),
        ids + events + [ACTIVITY_SCAN],
    ):
        out.append({"t": _norm_ts(row["created_at"]), "kind": "karum",
                    "summary": _KARUM_EVENT_COPY[row["event"]]})
    return out


def _gift_items(conn, ids, marks):
    out = []
    for row in conn.execute(
        """SELECT created_at, amount, sender_account_id FROM portal_gift_events
            WHERE status = 'applied'
              AND (sender_account_id IN (%s) OR recipient_account_id IN (%s))
            ORDER BY created_at DESC LIMIT ?""" % (marks, marks),
        ids + ids + [ACTIVITY_SCAN],
    ):
        sent = int(row["sender_account_id"] or 0) in ids
        out.append({"t": _norm_ts(row["created_at"]), "kind": "gift",
                    "summary": "{} {:,} Solari".format("Sent" if sent else "Received",
                                                       int(row["amount"] or 0))})
    return out


def _transfer_items(conn, ids, marks):
    out = []
    for row in conn.execute(
        """SELECT created_at, item_name, template_id, item_grade,
                  sender_account_id, recipient_account_id
             FROM portal_transfer_events
            WHERE status IN ('applied','replay')
              AND (sender_account_id IN (%s) OR recipient_account_id IN (%s))
            ORDER BY created_at DESC LIMIT ?""" % (marks, marks),
        ids + ids + [ACTIVITY_SCAN],
    ):
        sent = int(row["sender_account_id"] or 0) in ids
        item = ((row["item_name"] or "").strip()
                or (row["template_id"] or "").strip() or "an item")
        other = int((row["recipient_account_id"] if sent else row["sender_account_id"]) or 0)
        out.append({"t": _norm_ts(row["created_at"]), "kind": "transfer",
                    "summary": "%s %s" % ("Sent" if sent else "Received", item),
                    "item": item, "grade": row["item_grade"],
                    "counterparty": other,
                    "direction": "sent" if sent else "received"})
    # The counterparty is a character name, never an id: one batched lookup for
    # the page (the same helper history() uses), not one query per row.
    if out:
        import portal_transfer_events
        names = portal_transfer_events._character_names(
            conn, sorted({r["counterparty"] for r in out if r["counterparty"]}))
        for r in out:
            r["counterparty"] = (names.get(r["counterparty"]) or "").strip()
    return out


def _rescue_items(conn, ids, marks):
    out = []
    for row in conn.execute(
        """SELECT used_at, from_map FROM portal_rescue_log
            WHERE account_id IN (%s)
            ORDER BY used_at DESC LIMIT ?""" % marks,
        ids + [ACTIVITY_SCAN],
    ):
        where = (row["from_map"] or "").strip()
        out.append({"t": _norm_ts(row["used_at"]), "kind": "rescue",
                    "summary": "Used a sand rescue" + (" on %s" % where if where else "")})
    return out


def _mail_items(conn, ids, marks):
    out = []
    for row in conn.execute(
        """SELECT created_at, kind, sender_char_name FROM portal_messages
            WHERE recipient_kind = 'player' AND recipient_id IN (%s)
              AND deleted_at IS NULL
            ORDER BY created_at DESC LIMIT ?""" % marks,
        ids + [ACTIVITY_SCAN],
    ):
        summary = _MAIL_COPY.get(row["kind"], "Received a message")
        sender = (row["sender_char_name"] or "").strip()
        if sender:
            summary += " from %s" % sender
        out.append({"t": _norm_ts(row["created_at"]), "kind": "mail", "summary": summary})
    return out


# Every source is an admin.db table that already carries an account_id. Only the
# `?` placeholder runs are interpolated into these statements; every value is
# bound. portal_gift_events and portal_transfer_events are created lazily by
# their own modules rather than by init_db, which is the concrete reason the
# merge below tolerates a source that is not there.
_ACTIVITY_SOURCES = (_reward_items, _karum_items, _gift_items, _transfer_items,
                     _rescue_items, _mail_items)


def activity_items(account_ids, limit, with_status=False):
    """Merged read-only activity across the caller's own accounts, newest first.
    Each source is capped at ACTIVITY_SCAN of its own rows before the merge; no
    summary carries an id of any kind. A source that cannot be read is skipped:
    an activity panel is a convenience, and one absent table must not 500 it."""
    if not account_ids:
        return {"items": [], "unavailable": []} if with_status else []
    ids = [int(a) for a in account_ids]
    marks = ",".join("?" * len(ids))
    items = []
    unavailable = []
    conn = get_db()
    try:
        for source in _ACTIVITY_SOURCES:
            try:
                items.extend(source(conn, ids, marks))
            except Exception as exc:   # sqlite errors AND a row that does not format
                unavailable.append(source.__name__.removeprefix("_").removesuffix("_items"))
                logger.debug("portal codes: activity source %s unavailable: %s",
                             source.__name__, exc)
    finally:
        conn.close()

    items.sort(key=lambda i: i["t"], reverse=True)
    if with_status:
        return {"items": items[:limit], "unavailable": unavailable}
    return items[:limit]


# --- routes ------------------------------------------------------------------


@router.get("/portal/settings/codes/mine")
async def portal_codes_mine(request: Request):
    """The caller's own identity code, minted on first call. `rotated_at` is the
    moment the returned code became the current one (contract field name; the
    row's own rotated_at is when it stops being current). Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, _, _ = gate

    current = active_code_row(discord_id) or mint_code(discord_id)
    if current is None:
        return _v2_err("code_unavailable",
                       "Your code could not be issued. Try again in a moment.",
                       status=503)
    return _v2_ok({"code": current[0], "rotated_at": current[1]})


@router.post("/portal/settings/codes/rotate")
async def portal_codes_rotate(request: Request):
    """Retire the caller's code and issue a new one. The previous code stops
    resolving immediately, so this is also the "I shared it too widely" button.
    One rotation per 10 minutes per identity. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, _, _ = gate

    _body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    if active_code_row(discord_id) is None:
        current = mint_code(discord_id)
    else:
        remaining = rotate_cooldown_remaining(discord_id)
        if remaining > 0:
            return _v2_err("rotate_cooldown",
                           "You changed your code recently. Try again in %d minutes."
                           % max(1, (remaining + 59) // 60),
                           status=429)
        current = rotate_code(discord_id)
    if current is None:
        return _v2_err("code_unavailable",
                       "Your code could not be changed. Try again in a moment.",
                       status=503)
    return _v2_ok({"code": current[0], "rotated_at": current[1]})


@router.get("/portal/settings/codes/lookup")
async def portal_codes_lookup(request: Request, code: str = ""):
    """Resolve a code someone gave you to the handle behind it, and to how many
    sends you have left today and to that player. Returns a character name and
    two counts, or nothing: no Discord id, no account_id, no profile detail.
    Malformed input is refused before any query runs. Throttled to 20 lookups
    per 10 minutes per identity, so the code space cannot be walked.
    Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, _, _ = gate

    candidate = normalize_code(code)
    if not valid_code(candidate):
        return _v2_err("bad_code", "That is not a valid code.", status=400)

    if lookups_in_window(discord_id) >= LOOKUP_MAX_PER_WINDOW:
        return _v2_err("lookup_throttled",
                       "You have looked up as many codes as you can for now. "
                       "Try again in a few minutes.",
                       status=429)

    # resolve_code spends the one lookup slot this call is allowed, and is also
    # what the transfer writes use, so a confirm here and the send that follows
    # agree on which player a code means.
    resolved = resolve_code(candidate, discord_id)
    daily_remaining, pair_remaining = transfer_remaining(discord_id, resolved)
    return _v2_ok({"found": resolved is not None,
                   "display_name": resolved["display_name"] if resolved else None,
                   "daily_remaining": daily_remaining,
                   "pair_remaining": pair_remaining})


@router.get("/portal/settings/activity")
async def portal_settings_activity(request: Request, limit: int = ACTIVITY_DEFAULT_LIMIT):
    """The caller's own recent activity, merged from the admin.db tables that
    already record it. Read-only, scoped to the accounts this identity is
    linked to, and additive: nothing here is a new source of truth.
    Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, _, _ = gate

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = ACTIVITY_DEFAULT_LIMIT
    limit = max(1, min(limit, ACTIVITY_MAX_LIMIT))
    return _v2_ok({"items": activity_items(own_account_ids(discord_id), limit)})
