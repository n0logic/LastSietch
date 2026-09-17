"""Portal events + reminders (Fremkit wave 9).

An event is a scheduled thing on the servers (a gathering, a faction war, a
maintenance window) that an admin writes in the portal and every visitor can
read. Three properties matter more than the feature:

  * A DRAFT IS INVISIBLE. Only `published` reaches the public list, and only
    `published` or `cancelled` reaches the public detail. A draft is a 404 to
    everyone who is not an admin, which is also what the admin surfaces
    themselves answer to a player: an admin lane must not confirm it exists.
  * THE PUBLIC PAYLOAD IS A KEY SET, NOT A ROW. `public_event` builds a fresh
    dict of exactly PUBLIC_KEYS per row, so a column added to the table later
    (`created_by_discord_id` is already one) cannot ride out to the browser.
  * A REMINDER IS A ROW, NOT A COUNTER. remind/unremind are INSERT OR IGNORE
    and DELETE, so the same call twice is the same state, and a cancel walks
    those rows once and marks them notified in the same transaction that
    cancels the event.

All state is admin.db (portal_events + portal_event_reminders); zero game-DB
touch. Session gate, refusal envelope, CSRF check and the role lookup are
reused from routers.portal (loaded first by main.py) so the auth story matches
the rest of the portal exactly. Delivery of the T-1h notice is not here: this
module owns the two tables and the endpoints, the in-process loop reads them.
"""
import inspect
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from database import get_db
from portal_auth import client_ip
from routers.portal import (
    _require_linked_session_json,
    _roles_for_discord,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()


# --- event model (stdlib only below; the test suite execs this section) ------

EVENT_KINDS = ("community", "faction_war", "maintenance", "other")
# What a write may set. `cancelled` is reachable only through the cancel route,
# which has a typed confirmation and a mailbox fan-out attached to it.
WRITABLE_STATUSES = ("draft", "published")
# What an anonymous reader may see. A cancelled event stays readable on purpose:
# a player who followed a link to it deserves the cancellation, not a 404.
PUBLIC_STATUSES = ("published", "cancelled")

TITLE_MAX = 200
DESCRIPTION_MAX = 4000
MAP_NAME_MAX = 64
HOST_MAX = 64

LIST_LIMIT_MAX = 50
PAST_WINDOW_DAYS = 90
ADMIN_LIST_LIMIT = 200

# Lead ruling 8: 500 reminders per event, 20 ACTIVE per player (a reminder on an
# event that already ran holds no slot). 60 toggles per account per hour is the
# anti-hammering tier on top of both.
EVENT_REMINDER_CAP = 500
PLAYER_REMINDER_CAP = 20
TOGGLES_PER_HOUR = 60
ADMIN_DEBOUNCE_SECONDS = 10
# An edit that moves the start by more than this re-arms the reminder rows.
REARM_MINUTES = 15

PUBLIC_CACHE = "public, max-age=60"
PRIVATE_CACHE = "private, no-store"

# The eleven keys of the event contract (plan section 5). Everything that leaves
# this module for a browser is built from exactly these, in this order.
PUBLIC_KEYS = ("id", "title", "kind", "banner", "starts_utc", "ends_utc", "map",
               "description", "host", "status", "updated_at")

TOGGLE_ACTION = "portal_event_remind_toggle"
ADMIN_ACTIONS = ("portal_event_create", "portal_event_update", "portal_event_cancel")

# Last Sietch voice rule: no em dashes or en dashes in user-facing copy. Written as
# escapes so the rule holds for this file too.
EM_EN_DASH_RE = re.compile("[\u2014\u2013]")

# A banner is a slug, never a path: the URL is built in the component. The
# allowlist is the basenames actually shipped, read once at import, so a slug
# that has no webp cannot be stored and a webp nobody removed cannot be lost.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# The static adapter copies static/ into build/ and ONLY build/ is rsynced to
# <web-host> (deploy-portal-nextgen.sh), so read build/ first and fall back to
# static/ for a dev checkout (the dd_layout_match.py precedent).
_NEXTGEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "portal-nextgen")
BANNER_DIR = os.path.join(_NEXTGEN, "build", "img", "v2", "banners")
if not os.path.isdir(BANNER_DIR):
    BANNER_DIR = os.path.join(_NEXTGEN, "static", "img", "v2", "banners")


def _shipped_banners():
    try:
        names = os.listdir(BANNER_DIR)
    except OSError:
        logger.warning("portal events: no banner directory at %s", BANNER_DIR)
        return frozenset()
    return frozenset(n[:-len(".webp")] for n in names
                     if n.endswith(".webp") and _SLUG_RE.fullmatch(n[:-len(".webp")]))


BANNERS = _shipped_banners()


def valid_banner(value) -> bool:
    """A slug from the shipped set and nothing else. `/`, `.` and `?` are
    refused ahead of the set membership so a traversal or a query string is a
    refusal on its own terms even if the allowlist is empty."""
    slug = str(value or "").strip()
    if not slug or any(ch in slug for ch in ("/", ".", "?", "\\")):
        return False
    return bool(_SLUG_RE.fullmatch(slug)) and slug in BANNERS


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _since(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _stamp(text):
    """One stored timestamp as an aware datetime, or None."""
    try:
        return datetime.strptime(str(text or "")[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_utc(value):
    """A client time to the stored 'YYYY-MM-DD HH:MM:SS', or None.

    Accepted: the contract's 'YYYY-MM-DD HH:MM:SSZ' and the ISO shape a browser
    emits ('...T...Z', optionally without seconds or with a fraction). An
    offset other than Z is refused rather than guessed at: every time in this
    table is UTC, and a silent misreading of a local time moves an event."""
    text = str(value or "").strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1].strip()
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def clean_text(value) -> str:
    """Free text as it is STORED: em and en dashes folded to a hyphen (the house
    voice rule, applied server-side so it holds whatever wrote the row) and
    trimmed. Length is checked by the caller, never truncated silently."""
    return EM_EN_DASH_RE.sub("-", str(value or "")).strip()


def public_event(row) -> dict:
    """One row as the public contract object: a fresh dict of exactly
    PUBLIC_KEYS. Never `created_by_discord_id`, never an account id, never the
    row itself."""
    return {"id": int(row["id"]),
            "title": row["title"] or "",
            "kind": row["kind"] or "other",
            "banner": row["banner"] or None,
            "starts_utc": row["starts_utc"],
            "ends_utc": row["ends_utc"] or None,
            "map": row["map_name"] or None,
            "description": row["description"] or "",
            "host": row["host"] or None,
            "status": row["status"],
            "updated_at": row["updated_at"]}


_EVENT_COLUMNS = ("id, title, kind, banner, starts_utc, ends_utc, map_name, "
                  "description, host, status, published_at, created_at, updated_at")


def list_events(window: str, limit: int) -> list:
    """Published events only. `upcoming` is soonest first from now; `past` is
    newest first inside the 90-day window, so the page never grows without
    bound."""
    now = _now()
    if window == "past":
        sql = ("SELECT %s FROM portal_events WHERE status = 'published'"
               " AND starts_utc < ? AND starts_utc >= ?"
               " ORDER BY starts_utc DESC LIMIT ?") % _EVENT_COLUMNS
        args = (now, _since(PAST_WINDOW_DAYS * 86400), limit)
    else:
        sql = ("SELECT %s FROM portal_events WHERE status = 'published'"
               " AND starts_utc >= ? ORDER BY starts_utc ASC LIMIT ?") % _EVENT_COLUMNS
        args = (now, limit)
    conn = get_db()
    try:
        return [public_event(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def admin_events(limit: int) -> list:
    """Every event whatever its status, newest start first. Same projection as
    the public list: the composer edits the contract object, and an admin has no
    use for the author column either."""
    conn = get_db()
    try:
        return [public_event(r) for r in conn.execute(
            ("SELECT %s FROM portal_events ORDER BY starts_utc DESC LIMIT ?"
             % _EVENT_COLUMNS), (limit,))]
    finally:
        conn.close()


def event_row(event_id, statuses=None):
    """One row (sqlite3.Row) or None. `statuses` narrows the read at the query,
    not after it, so a draft never reaches a caller that must not see it."""
    if not event_id:
        return None
    sql = "SELECT %s FROM portal_events WHERE id = ?" % _EVENT_COLUMNS
    args = [int(event_id)]
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args.extend(statuses)
    conn = get_db()
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def reminder_event_ids(account_id) -> list:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT event_id FROM portal_event_reminders WHERE account_id = ?"
            " ORDER BY event_id",
            (int(account_id),),
        ).fetchall()
    finally:
        conn.close()
    return [int(r["event_id"]) for r in rows]


def has_reminder(event_id, account_id) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM portal_event_reminders WHERE event_id = ? AND account_id = ?",
            (int(event_id), int(account_id)),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def event_reminder_count(event_id) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM portal_event_reminders WHERE event_id = ?",
            (int(event_id),),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"] if row else 0)


def player_reminder_count(account_id) -> int:
    """ACTIVE reminders: rows on published events that have not started. A past
    event holds no slot, so the cap is a cap on what is still coming."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c
                 FROM portal_event_reminders r
                 JOIN portal_events e ON e.id = r.event_id
                WHERE r.account_id = ? AND e.status = 'published'
                  AND e.starts_utc > ?""",
            (int(account_id), _now()),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"] if row else 0)


def add_reminder(event_id, account_id) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO portal_event_reminders (event_id, account_id, created_at)"
            " VALUES (?, ?, ?)",
            (int(event_id), int(account_id), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def remove_reminder(event_id, account_id) -> None:
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM portal_event_reminders WHERE event_id = ? AND account_id = ?",
            (int(event_id), int(account_id)),
        )
        conn.commit()
    finally:
        conn.close()


def toggles_in_window(account_id) -> int:
    """Remind/unremind presses by this account in the last hour, counted off the
    audit rows the toggles themselves write (the portal_rate_limit shape: a
    windowed COUNT over a durable table, so a restart cannot reset the cap)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log"
            " WHERE action = ? AND target = ? AND timestamp >= ?",
            (TOGGLE_ACTION, str(int(account_id)), _since(3600)),
        ).fetchone()
    finally:
        conn.close()
    return int(row["c"] if row else 0)


def admin_debounce_remaining(username) -> int:
    """Seconds this admin must wait before the next write, 0 when clear. Same
    windowed COUNT, over the three audited admin actions: a double-fired submit
    button must not create the same event twice."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE username = ? AND timestamp >= ?"
            " AND action IN (%s)" % ",".join("?" * len(ADMIN_ACTIONS)),
            [str(username), _since(ADMIN_DEBOUNCE_SECONDS)] + list(ADMIN_ACTIONS),
        ).fetchone()
    finally:
        conn.close()
    return ADMIN_DEBOUNCE_SECONDS if row and int(row["c"]) else 0


def _audit(username, audit_action, target, ip, details) -> None:
    """One audit row. auth is imported at call time so the stdlib-only suite can
    execute this section without dragging bcrypt (and the whole admin auth
    stack) in behind it. Every call site passes the action as a STRING LITERAL
    so the Audit page's scanner can offer it as a filter (lane C). A failing
    audit write is logged, never a 500 after the data write already committed
    (review MEDIUM-6)."""
    try:
        from auth import audit_log
        audit_log(None, str(username), audit_action, str(target), ip,
                  details=json.dumps(details, separators=(",", ":")), success=True)
    except Exception as exc:
        logger.warning("portal events: audit write failed for %s: %s", audit_action, exc)


def rearm_needed(old_start, new_start) -> bool:
    """True when an edit moved the start far enough that an already-sent notice
    is wrong. Any move over REARM_MINUTES counts, earlier or later: a notice
    sent for the old time is equally stale in both directions. Only a start that
    is still in the future can be re-armed; a past one has nothing to send."""
    old, new = _stamp(old_start), _stamp(new_start)
    if old is None or new is None:
        return False
    if new <= datetime.now(timezone.utc):
        return False
    return abs((new - old).total_seconds()) > REARM_MINUTES * 60


def _field(body, keys, existing, column):
    """The value a write should bind: the body's when it carries the key (the
    composer sends the whole form), otherwise the row's current value, so an
    update is a merge and never blanks a field the client left out."""
    for key in keys:
        if key in body:
            return body.get(key)
    return existing[column] if existing is not None else None


def validate_event(body, existing=None):
    """(fields, error, message). `fields` is exactly what the write binds, with
    every free-text value already dash-folded and length-checked. Nothing here
    reads a session: identity is the caller's business, shape is this one's."""
    if not isinstance(body, dict):
        body = {}

    title = clean_text(_field(body, ("title",), existing, "title"))
    if not title:
        return None, "invalid_title", "An event needs a title."
    if len(title) > TITLE_MAX:
        return None, "invalid_title", "A title is at most %d characters." % TITLE_MAX

    kind = str(_field(body, ("kind",), existing, "kind") or "community").strip().lower()
    if kind not in EVENT_KINDS:
        return None, "invalid_kind", "Pick one of: %s." % ", ".join(EVENT_KINDS)

    banner = str(_field(body, ("banner",), existing, "banner") or "").strip()
    if banner and not valid_banner(banner):
        return None, "invalid_banner", "That banner is not one of the shipped set."

    starts_utc = parse_utc(_field(body, ("starts_utc", "starts"), existing, "starts_utc"))
    if not starts_utc:
        return None, "invalid_starts_utc", \
            "A start time must be UTC, like 2026-09-10 19:00:00Z."

    ends_raw = _field(body, ("ends_utc", "ends"), existing, "ends_utc")
    ends_utc = None
    if str(ends_raw or "").strip():
        ends_utc = parse_utc(ends_raw)
        if not ends_utc:
            return None, "invalid_ends_utc", \
                "An end time must be UTC, like 2026-09-10 21:00:00Z."
        if ends_utc < starts_utc:
            return None, "invalid_ends_utc", "An event cannot end before it starts."

    map_name = clean_text(_field(body, ("map", "map_name"), existing, "map_name"))
    if len(map_name) > MAP_NAME_MAX:
        return None, "invalid_map", "A map name is at most %d characters." % MAP_NAME_MAX

    host = clean_text(_field(body, ("host",), existing, "host"))
    if len(host) > HOST_MAX:
        return None, "invalid_host", "A host is at most %d characters." % HOST_MAX

    description = clean_text(_field(body, ("description",), existing, "description"))
    if len(description) > DESCRIPTION_MAX:
        return None, "invalid_description", \
            "A description is at most %d characters." % DESCRIPTION_MAX

    status = str(_field(body, ("status",), existing, "status") or "draft").strip().lower()
    if status not in WRITABLE_STATUSES:
        return None, "invalid_status", "An event is either a draft or published."

    return ({"title": title, "kind": kind, "banner": banner or None,
             "starts_utc": starts_utc, "ends_utc": ends_utc,
             "map_name": map_name or None, "host": host or None,
             "description": description or None, "status": status},
            None, None)


def insert_event(fields, discord_id) -> int:
    now = _now()
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO portal_events
                   (title, kind, banner, starts_utc, ends_utc, map_name, description,
                    host, status, created_by_discord_id, created_at, updated_at,
                    published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fields["title"], fields["kind"], fields["banner"], fields["starts_utc"],
             fields["ends_utc"], fields["map_name"], fields["description"],
             fields["host"], fields["status"], str(discord_id), now, now,
             now if fields["status"] == "published" else None),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_event(event_id, fields, row) -> None:
    """The edit and the re-arm in one transaction: a start that moved without
    its reminder rows following would send a notice for a time nobody agreed
    to. published_at is stamped once, on the first publish, and never moved."""
    now = _now()
    published_at = row["published_at"] or (
        now if fields["status"] == "published" else None)
    rearm = rearm_needed(row["starts_utc"], fields["starts_utc"])
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE portal_events
                  SET title = ?, kind = ?, banner = ?, starts_utc = ?, ends_utc = ?,
                      map_name = ?, description = ?, host = ?, status = ?,
                      published_at = ?, updated_at = ?
                WHERE id = ?""",
            (fields["title"], fields["kind"], fields["banner"], fields["starts_utc"],
             fields["ends_utc"], fields["map_name"], fields["description"],
             fields["host"], fields["status"], published_at, now, int(event_id)),
        )
        if rearm:
            conn.execute(
                "UPDATE portal_event_reminders SET notified_at = NULL WHERE event_id = ?",
                (int(event_id),),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_event(event_id, reason) -> list:
    """Cancel the event and mark every reminder row notified, in ONE
    transaction, and return the account ids that were holding a reminder.

    The mailbox fan-out happens after this returns, not inside it: mailbox.post
    opens its own connection and SQLite has one writer, so posting from inside
    this transaction would deadlock against it. The order is deliberate. If the
    fan-out then dies, a player misses a cancellation notice; the other order
    would leave a cancelled event still able to send "starting in an hour"."""
    now = _now()
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT account_id FROM portal_event_reminders WHERE event_id = ?",
            (int(event_id),),
        ).fetchall()
        conn.execute(
            """UPDATE portal_events
                  SET status = 'cancelled', cancelled_at = ?, cancel_reason = ?,
                      updated_at = ?
                WHERE id = ?""",
            (now, reason or None, now, int(event_id)),
        )
        conn.execute(
            "UPDATE portal_event_reminders SET notified_at = ? WHERE event_id = ?",
            (now, int(event_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return [int(r["account_id"]) for r in rows]


def notify_cancelled(account_ids, event) -> int:
    """One mailbox notification per reminder row. Best effort per row: a mailbox
    that refuses one message must not swallow the rest of the fan-out, and none
    of it may undo a cancel that is already committed."""
    sent = 0
    try:
        import mailbox
    except ImportError:
        logger.warning("portal events: mailbox unavailable; cancel notices skipped")
        return 0
    body = "%s was cancelled." % event["title"]
    if event.get("starts_utc"):
        body += " It was scheduled for %s UTC." % event["starts_utc"]
    for account_id in account_ids:
        try:
            mailbox.post("player", int(account_id), "notification",
                         subject="Event cancelled", body=body,
                         payload={"event_id": event["id"], "event": "cancelled"},
                         sender_kind="system")
            sent += 1
        except Exception:  # noqa: BLE001
            logger.warning("portal events: cancel notice failed for event %s",
                           event["id"], exc_info=True)
    return sent


async def cross_post(event) -> None:
    """Announce a freshly published event in Discord. Best effort in every
    direction: the helper is dev-4's and lands on its own branch (ImportError /
    AttributeError until it does), it decides for itself whether a channel is
    configured, and a Discord outage must never fail a publish that is already
    written."""
    try:
        import discord_post
        post = getattr(discord_post, "post_event", None)
        if post is None:
            logger.debug("portal events: discord_post.post_event absent; no cross-post")
            return
        result = post(event)
        if inspect.isawaitable(result):
            await result
    except (ImportError, AttributeError):
        logger.debug("portal events: Discord cross-post helper not present")
    except Exception:  # noqa: BLE001
        logger.warning("portal events: Discord cross-post failed", exc_info=True)


def _event_id(raw):
    """A path segment to an int id, or None. Bounded before any query runs, so
    a junk segment is a clean 404 rather than a framework 422."""
    text = str(raw or "").strip()
    if not text.isdigit() or len(text) > 12:
        return None
    return int(text)


# --- routes ------------------------------------------------------------------
# Registration order is load-bearing: `/portal/events/mine` and the admin paths
# are literals that must be matched before `/portal/events/{event_id}`, or the
# parametrised route swallows them.


@router.get("/portal/events")
async def portal_events_list(request: Request, window: str = "upcoming",
                             limit: int = LIST_LIMIT_MAX):
    """The public events list. Published only, never a draft. Anonymous, and
    cached for a minute at the edge: a cancel is up to 60 s stale here on
    purpose (lead ruling 5), because the mailbox notice is immediate."""
    window = "past" if str(window or "").strip().lower() == "past" else "upcoming"
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = LIST_LIMIT_MAX
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    resp = _v2_ok({"window": window, "events": list_events(window, limit)})
    resp.headers["Cache-Control"] = PUBLIC_CACHE
    return resp


@router.get("/portal/events/mine")
async def portal_events_mine(request: Request):
    """The event ids this player has a reminder on. Never cached: it is one
    player's state and the toggle must read back what it just wrote.
    Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _session, _discord_id, account_id, _row = gate
    resp = _v2_ok({"reminders": reminder_event_ids(account_id)})
    resp.headers["Cache-Control"] = PRIVATE_CACHE
    return resp


@router.get("/portal/events/admin/list")
async def portal_events_admin_list(request: Request):
    """Every event including drafts. Auth: linked session whose Discord id maps
    to the admin role; anyone else gets the 404 a missing route would give."""
    _gate, early = _require_admin(request)
    if early is not None:
        return early
    resp = _v2_ok({"events": admin_events(ADMIN_LIST_LIMIT)})
    resp.headers["Cache-Control"] = PRIVATE_CACHE
    return resp


@router.get("/portal/events/{event_id}")
async def portal_event_detail(request: Request, event_id: str):
    """One event for the deep link. Published or cancelled; a draft is a 404 to
    everyone, which is the same answer an id that never existed gets."""
    row = event_row(_event_id(event_id), statuses=PUBLIC_STATUSES)
    if row is None:
        return _v2_err("not_found", "That event is not available.", status=404)
    resp = _v2_ok({"event": public_event(row)})
    resp.headers["Cache-Control"] = PUBLIC_CACHE
    return resp


@router.post("/portal/events/{event_id}/remind")
async def portal_event_remind(request: Request, event_id: str):
    """Remind me before this event. Idempotent: the same call twice leaves one
    row. Auth: linked session + CSRF."""
    return await _toggle(request, event_id, True)


@router.post("/portal/events/{event_id}/unremind")
async def portal_event_unremind(request: Request, event_id: str):
    """Drop the reminder. Idempotent, and allowed after the event has started:
    a player must always be able to stop hearing about something."""
    return await _toggle(request, event_id, False)


async def _toggle(request: Request, event_id: str, on: bool):
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _session, discord_id, account_id, _row = gate

    _body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    # A remind needs a published event; an unremind takes any status, so a
    # cancelled event can still be cleared off a player's list.
    eid = _event_id(event_id)
    row = event_row(eid, statuses=("published",) if on else None)
    if row is None:
        return _v2_err("not_found", "That event is not available.", status=404)

    if toggles_in_window(account_id) >= TOGGLES_PER_HOUR:
        return _v2_err("reminder_throttled",
                       "You have changed your reminders a lot in the last hour. "
                       "Try again shortly.", status=429)

    already = has_reminder(eid, account_id)
    if on and not already:
        starts = _stamp(row["starts_utc"])
        if starts is not None and starts <= datetime.now(timezone.utc):
            return _v2_err("event_started", "That event has already started.", status=409)
        if event_reminder_count(eid) >= EVENT_REMINDER_CAP:
            return _v2_err("event_reminder_cap",
                           "This event has as many reminders as it can hold.",
                           status=429)
        if player_reminder_count(account_id) >= PLAYER_REMINDER_CAP:
            return _v2_err("player_reminder_cap",
                           "You are holding %d reminders already. Drop one to add "
                           "another." % PLAYER_REMINDER_CAP, status=429)
        add_reminder(eid, account_id)
    elif not on and already:
        remove_reminder(eid, account_id)

    _audit("portal:%s" % discord_id, audit_action="portal_event_remind_toggle", target=account_id,
           ip=client_ip(request), details={"event_id": eid, "on": bool(on)})
    resp = _v2_ok({"event_id": eid, "reminded": bool(on)})
    resp.headers["Cache-Control"] = PRIVATE_CACHE
    return resp


def _require_admin(request: Request):
    """(gate, None) or (None, refusal). Every refusal is the SAME 404 a missing
    route gives (the solido.py admin-lane precedent): an anonymous caller and a
    linked player who is not an admin must not be able to tell the composer's
    endpoints exist, and the role comes from the server-side users.role mapping,
    never from anything the browser sent."""
    gate, early = _require_linked_session_json(request)
    if early is not None or "admin" not in _roles_for_discord(gate[1]):
        return None, _v2_err("not_found", "Not found.", status=404)
    return gate, None


async def _admin_write(request: Request):
    """(gate, body, None) or (None, None, refusal): the gate, the CSRF check and
    the per-admin debounce, in the one order every admin write uses."""
    gate, early = _require_admin(request)
    if early is not None:
        return None, None, early
    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return None, None, _v2_err("csrf", "Invalid CSRF token", status=403)
    remaining = admin_debounce_remaining("self:%s" % gate[1])
    if remaining > 0:
        return None, None, _v2_err(
            "debounce", "That was a moment ago. Try again in %d seconds." % remaining,
            status=429)
    return gate, body, None


@router.post("/portal/events/admin/create")
async def portal_events_admin_create(request: Request):
    """Create an event, as a draft or published outright. Auth: linked session +
    admin role + CSRF, debounced 10 s per admin."""
    gate, body, early = await _admin_write(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate

    fields, error, message = validate_event(body)
    if error:
        return _v2_err(error, message, status=400)

    event_id = insert_event(fields, discord_id)
    _audit("self:%s" % discord_id, audit_action="portal_event_create", target=event_id,
           ip=client_ip(request),
           details={"event_id": event_id, "kind": fields["kind"], "status": fields["status"]})

    event = public_event(event_row(event_id))
    if fields["status"] == "published":
        await cross_post(event)
    return _v2_ok({"event": event})


@router.post("/portal/events/admin/{event_id}/update")
async def portal_events_admin_update(request: Request, event_id: str):
    """Edit an event, publish a draft, or move it. A move of more than 15
    minutes re-arms the reminder rows. Auth as create."""
    gate, body, early = await _admin_write(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate

    eid = _event_id(event_id)
    row = event_row(eid)
    if row is None:
        return _v2_err("not_found", "That event is not available.", status=404)
    if row["status"] == "cancelled":
        return _v2_err("event_cancelled", "That event was cancelled.", status=409)

    fields, error, message = validate_event(body, existing=row)
    if error:
        return _v2_err(error, message, status=400)

    rearmed = rearm_needed(row["starts_utc"], fields["starts_utc"])
    update_event(eid, fields, row)
    _audit("self:%s" % discord_id, audit_action="portal_event_update", target=eid,
           ip=client_ip(request),
           details={"event_id": eid, "status": fields["status"], "rearmed": rearmed})

    event = public_event(event_row(eid))
    if fields["status"] == "published" and row["status"] != "published":
        await cross_post(event)
    return _v2_ok({"event": event, "rearmed": rearmed})


@router.post("/portal/events/admin/{event_id}/cancel")
async def portal_events_admin_cancel(request: Request, event_id: str):
    """Cancel an event and tell everyone who asked to be reminded. Body carries
    the typed confirmation `{"confirm": "CANCEL", "reason": "..."}`: a cancel
    mails every reminder holder, so it is not a button you can lean on."""
    gate, body, early = await _admin_write(request)
    if early is not None:
        return early
    _session, discord_id, _account_id, _row = gate

    eid = _event_id(event_id)
    row = event_row(eid)
    if row is None:
        return _v2_err("not_found", "That event is not available.", status=404)

    if str((body or {}).get("confirm") or "").strip() != "CANCEL":
        return _v2_err("confirm_required",
                       "Type CANCEL to confirm. Everyone holding a reminder is told.",
                       status=400)

    if row["status"] == "cancelled":
        # Already cancelled: the notices went out with the first cancel, and
        # sending them twice is worse than doing nothing.
        return _v2_ok({"event": public_event(row), "notified": 0})

    reason = clean_text((body or {}).get("reason"))[:DESCRIPTION_MAX] or None
    account_ids = cancel_event(eid, reason)
    event = public_event(event_row(eid))
    notified = notify_cancelled(account_ids, event)
    _audit("self:%s" % discord_id, audit_action="portal_event_cancel", target=eid,
           ip=client_ip(request),
           details={"event_id": eid, "reminders": len(account_ids), "notified": notified})
    return _v2_ok({"event": event, "notified": notified})
