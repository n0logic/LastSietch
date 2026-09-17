"""Identity-keyed item-transfer caps + the portal-side transfer mirror (wave 7).

Sibling of portal_gift_limits.py: same table shape, same reserve-then-settle
idiom, same SQLite source of truth, no in-memory state.

WHY THIS EXISTS, twice over:

  1. CAPS. The caps in scripts/dune-item-transfer-op.sh run in Postgres and key
     on account_id. The discord_id <-> account_id map lives in a DIFFERENT
     database (admin.db, ls_account_links), so Postgres cannot collapse a
     player's accounts into one identity, and an account-keyed pair cap is
     bypassed by relaying through your own alts. Keying both caps on the DISCORD
     identity closes it, and that check therefore has to run here, ahead of the
     writer. The Postgres account-keyed caps stay exactly as they are, as a
     backstop.
  2. HISTORY. The item ledger is dune.ls_item_transfers, and the portal may not
     read dune.* (the read-only access rule). Without a mirror on this side neither
     party can ever be shown what moved, so this table is the only both-parties
     transfer history the portal can serve.

MIRROR ONLY WHAT MOVED. settle() takes the writer's verdict, and only 'applied'
and 'replay' are treated as history. A DARK 'deferred' moved nothing and must
never read as a transfer; refusals stay in the audit log.

RESERVE-THEN-SETTLE. check_and_reserve() inserts a 'pending' row inside the same
SQLite write transaction that counted the window, and live pending rows count.
Without that, two concurrent submits both read a not-yet-breached cap and both
pass, which is the same class of hole this module exists to close. A pending row
whose request died mid-relay stops counting after PENDING_TTL_SECONDS, so a
crash cannot wedge a player's allowance.

Counters count 'applied', 'replay' and live 'pending'. Refused, failed and
deferred attempts moved no item and must not lock a player out of their own
allowance.
"""
from __future__ import annotations

import portal_identity
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)

# Mirrors the writer's constants (scripts/dune-item-transfer-op.sh:72-73). Same
# numbers, tighter key: for a single-account identity these are exactly the
# Postgres caps, so a player with one linked account sees no behaviour change.
# Keep the two sides in sync -- the writer is the backstop, not the primary.
XFER_MAX_PER_DAY = 30
XFER_MAX_PER_PAIR_PER_DAY = 15

# Relay timeout on this path is 45s; 3x headroom before a stranded reservation
# stops counting against the sender.
PENDING_TTL_SECONDS = 180

HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 200

# What a row shows when the counterparty has no live link left, or the item name
# never reached settle. Never an id: this list is read by both players.
UNKNOWN_COUNTERPARTY = "Another player"
UNKNOWN_ITEM = "An item"

STATUSES = ("pending", "applied", "replay", "deferred", "refused", "failed")

# Facts the route hands us about the item that moved, mapped from ITS key names
# onto this table's columns; the first key present wins. Whitelisted because
# settle() takes **facts, so only these names ever reach the UPDATE and only as
# a column name this module chose.
#
# The route also sends audit_id, item_id and stack_size. item_id is already
# stored at reserve time, and neither of the other two has a column here or a
# place in the history shape, so the whitelist drops them.
_SETTLE_FACTS = (
    ("item_name", ("display_name", "item_name")),
    ("item_grade", ("quality_level", "item_grade")),
    ("template_id", ("template_id",)),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_transfer_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key      TEXT    NOT NULL UNIQUE,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    settled_at           TEXT,
    sender_identity      TEXT    NOT NULL,
    sender_account_id    INTEGER NOT NULL,
    recipient_identity   TEXT    NOT NULL,
    recipient_account_id INTEGER NOT NULL,
    item_id              INTEGER NOT NULL,
    template_id          TEXT,
    item_name            TEXT,
    item_grade           INTEGER,
    status               TEXT    NOT NULL,
    fail_reason          TEXT,
    CHECK (item_id > 0),
    CHECK (status IN ('pending','applied','replay','deferred','refused','failed'))
);
CREATE INDEX IF NOT EXISTS idx_pte_sender
    ON portal_transfer_events(sender_identity, created_at);
CREATE INDEX IF NOT EXISTS idx_pte_pair
    ON portal_transfer_events(sender_identity, recipient_identity, created_at);
CREATE INDEX IF NOT EXISTS idx_pte_sender_account
    ON portal_transfer_events(sender_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pte_recipient_account
    ON portal_transfer_events(recipient_account_id, created_at);
"""

# Counted window: a transfer that moved an item, or one currently in flight.
_LIVE = "(status IN ('applied','replay') OR (status = 'pending' AND created_at >= ?))"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ago(**kw) -> str:
    return _stamp(_now() - timedelta(**kw))


def _ensure(conn) -> None:
    """Lazy DDL: one cheap sqlite_master probe per call, the six CREATE
    statements only when the table is missing. The overview hot path calls
    caps() on every load, and a test may recreate the database file under the
    same path, so neither "always run the script" nor a process-wide flag fits."""
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'portal_transfer_events'"
    ).fetchone()
    if have is None:
        conn.executescript(_SCHEMA)


def init() -> None:
    """Idempotent table + index creation (also done lazily by every caller)."""
    conn = get_db()
    try:
        _ensure(conn)
        conn.commit()
    finally:
        conn.close()


def check_and_reserve(idempotency_key: str, sender_identity: str,
                      sender_account_id: int, recipient_identity: str,
                      recipient_account_id: int, item_id: int,
                      template_id: Optional[str] = None) -> Tuple[bool, str, int]:
    """Identity-collapsed pre-check, then reserve the attempt in the same write.

    Returns (ok, reason, retry_after_seconds); reason is 'daily' or 'pair'. On
    breach a durable 'refused' row is written before returning, so a probe of the
    caps leaves a trace.

    An idempotency_key that already has a live row skips the caps entirely and is
    allowed through unreserved: the same key twice is a REPLAY, not a second
    reservation, and the writer's UNIQUE(idempotency_key) gate adjudicates it.
    Re-counting it here would refuse a legitimate retry-after-timeout sitting on
    the cap boundary."""
    day_floor = _ago(days=1)
    pending_floor = _ago(seconds=PENDING_TTL_SECONDS)

    conn = get_db()
    try:
        _ensure(conn)
        # Explicit transaction control: the count and the reservation that acts
        # on it must be one write, or two concurrent submits both pass a cap
        # neither has breached yet.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        sender_keys = portal_identity.quota_keys(conn, sender_identity)
        recipient_keys = portal_identity.quota_keys(conn, recipient_identity)

        prior = conn.execute(
            "SELECT status FROM portal_transfer_events WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None and prior["status"] in ("applied", "replay", "pending"):
            conn.commit()
            return True, "", 0
        # A prior 'refused'/'failed'/'deferred' row moved no item and never
        # reached the writer's gate, so there is nothing to replay -- a reused
        # key MUST face the caps again, or one refusal converts into an
        # allowance on resend. On pass, the row is re-reserved in place.

        day = conn.execute(
            f"""SELECT COUNT(*) AS c FROM portal_transfer_events
                 WHERE sender_identity IN ({",".join("?" * len(sender_keys))})
                   AND created_at >= ?
                   AND {_LIVE}""",
            (*sender_keys, day_floor, pending_floor),
        ).fetchone()["c"]
        if day >= XFER_MAX_PER_DAY:
            _refuse(conn, idempotency_key, sender_identity, sender_account_id,
                    recipient_identity, recipient_account_id, item_id, template_id,
                    f"identity daily cap ({day} in 24h, max {XFER_MAX_PER_DAY})")
            return False, "daily", 24 * 3600

        pair = conn.execute(
            f"""SELECT COUNT(*) AS c FROM portal_transfer_events
                 WHERE sender_identity IN ({",".join("?" * len(sender_keys))})
                   AND recipient_identity IN ({",".join("?" * len(recipient_keys))})
                   AND created_at >= ?
                   AND {_LIVE}""",
            (*sender_keys, *recipient_keys, day_floor, pending_floor),
        ).fetchone()["c"]
        if pair >= XFER_MAX_PER_PAIR_PER_DAY:
            _refuse(conn, idempotency_key, sender_identity, sender_account_id,
                    recipient_identity, recipient_account_id, item_id, template_id,
                    f"identity pair cap ({pair} in 24h, "
                    f"max {XFER_MAX_PER_PAIR_PER_DAY})")
            return False, "pair", 24 * 3600

        conn.execute(
            """INSERT INTO portal_transfer_events
                 (idempotency_key, sender_identity, sender_account_id,
                  recipient_identity, recipient_account_id, item_id,
                  template_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(idempotency_key) DO UPDATE SET
                 status = 'pending', created_at = datetime('now'),
                 settled_at = NULL, fail_reason = NULL,
                 sender_identity = excluded.sender_identity,
                 sender_account_id = excluded.sender_account_id,
                 recipient_identity = excluded.recipient_identity,
                 recipient_account_id = excluded.recipient_account_id,
                 item_id = excluded.item_id,
                 template_id = excluded.template_id""",
            (idempotency_key, str(sender_identity), int(sender_account_id),
             str(recipient_identity), int(recipient_account_id), int(item_id),
             _fact("template_id", template_id)),
        )
        conn.commit()
        return True, "", 0
    finally:
        conn.close()


def _refuse(conn, idempotency_key, sender_identity, sender_account_id,
            recipient_identity, recipient_account_id, item_id, template_id, reason):
    """Durable refusal row, committed on the caller's open transaction."""
    conn.execute(
        """INSERT INTO portal_transfer_events
             (idempotency_key, sender_identity, sender_account_id,
              recipient_identity, recipient_account_id, item_id,
              template_id, status, fail_reason, settled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'refused', ?, datetime('now'))
           ON CONFLICT(idempotency_key) DO UPDATE SET
             status = 'refused', fail_reason = excluded.fail_reason,
             settled_at = datetime('now')""",
        (idempotency_key, str(sender_identity), int(sender_account_id),
         str(recipient_identity), int(recipient_account_id), int(item_id),
         _fact("template_id", template_id), reason[:500]),
    )
    conn.commit()


def settle(idempotency_key: str, status: str, fail_reason: str | None = None,
           **facts) -> None:
    """Stamp the writer's verdict onto a reserved attempt, plus whatever it told
    us about the item that moved (see _SETTLE_FACTS for the keys it accepts and
    the ones it deliberately drops).

    Scoped to status='pending' so it can only ever close a row this process
    reserved -- a stray settle can never rewrite an 'applied' row the counters
    are keyed on, nor erase a 'refused' one. That also makes it idempotent: the
    second settle of one key matches nothing."""
    if status not in STATUSES:
        logger.warning("transfer: refusing to settle an unknown status %r", status)
        return
    sets = ["status = ?", "fail_reason = ?", "settled_at = datetime('now')"]
    args = [status, (fail_reason or "")[:500] or None]
    for column, keys in _SETTLE_FACTS:
        for key in keys:
            if key in facts:
                sets.append("%s = ?" % column)
                args.append(_fact(column, facts[key]))
                break
    args.append(idempotency_key)

    conn = get_db()
    try:
        _ensure(conn)
        conn.execute(
            "UPDATE portal_transfer_events SET %s "
            " WHERE idempotency_key = ? AND status = 'pending'" % ", ".join(sets),
            args,
        )
        conn.commit()
    finally:
        conn.close()


def _fact(column, value):
    """One stored fact, typed for its column. Grade Base is 0, a real grade and
    not an absent one, so only None reads as unknown here."""
    if value is None:
        return None
    if column == "item_grade":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return str(value)[:200]


def caps(sender_identity: str, recipient_identity: str) -> dict:
    """What this sender has spent, against both caps. Read-only, and the confirm
    step's only source for "sends left": the caps that DECIDE a transfer are
    re-counted inside check_and_reserve, so a stale count here authorises
    nothing."""
    day_floor = _ago(days=1)
    pending_floor = _ago(seconds=PENDING_TTL_SECONDS)
    conn = get_db()
    try:
        _ensure(conn)
        sender_keys = portal_identity.quota_keys(conn, sender_identity)
        recipient_keys = portal_identity.quota_keys(conn, recipient_identity)
        day = conn.execute(
            f"""SELECT COUNT(*) AS c FROM portal_transfer_events
                 WHERE sender_identity IN ({",".join("?" * len(sender_keys))})
                   AND created_at >= ?
                   AND {_LIVE}""",
            (*sender_keys, day_floor, pending_floor),
        ).fetchone()["c"]
        pair = conn.execute(
            f"""SELECT COUNT(*) AS c FROM portal_transfer_events
                 WHERE sender_identity IN ({",".join("?" * len(sender_keys))})
                   AND recipient_identity IN ({",".join("?" * len(recipient_keys))})
                   AND created_at >= ?
                   AND {_LIVE}""",
            (*sender_keys, *recipient_keys, day_floor, pending_floor),
        ).fetchone()["c"]
    finally:
        conn.close()
    return {"daily_cap": XFER_MAX_PER_DAY, "daily_used": int(day),
            "pair_cap": XFER_MAX_PER_PAIR_PER_DAY, "pair_used": int(pair)}


def _character_names(conn, account_ids) -> dict:
    """account_id -> character name, newest live link wins (the same pick
    display_name_for_code makes). An account with no live link is simply absent,
    and the caller shows UNKNOWN_COUNTERPARTY rather than the id."""
    if not account_ids:
        return {}
    marks = ",".join("?" * len(account_ids))
    import portal_identity
    out = {}
    for row in conn.execute(
        """SELECT l.account_id AS account_id,
                  COALESCE(NULLIF(l.character_name, ''), p.char_name) AS display_name
             FROM ls_account_links l
             LEFT JOIN portal_player_profile p ON p.account_id = l.account_id
            WHERE l.account_id IN (%s) AND l.revoked_at IS NULL
            ORDER BY l.linked_at DESC""".replace('ls_account_links', portal_identity.link_table(conn)) % marks,
        list(account_ids),
    ):
        aid = int(row["account_id"])
        if aid not in out and row["display_name"]:
            out[aid] = row["display_name"]
    return out


def history(account_ids: list, limit: int = HISTORY_DEFAULT_LIMIT) -> list:
    """Both directions of confirmed transfer history for one player's accounts,
    newest first. Only 'applied' and 'replay' rows: a deferred or refused attempt
    moved nothing and is not history.

    No row carries an identifier of any kind. The counterparty is a CHARACTER
    NAME, resolved here rather than stored, so a rename or a relink shows the
    name the other player goes by now."""
    ids = [int(a) for a in (account_ids or []) if a]
    if not ids:
        return []
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = HISTORY_DEFAULT_LIMIT
    limit = max(1, min(limit, HISTORY_MAX_LIMIT))
    marks = ",".join("?" * len(ids))

    conn = get_db()
    try:
        _ensure(conn)
        rows = conn.execute(
            """SELECT created_at, sender_account_id, recipient_account_id,
                      item_name, template_id, item_grade, status
                 FROM portal_transfer_events
                WHERE status IN ('applied','replay')
                  AND (sender_account_id IN (%s) OR recipient_account_id IN (%s))
                ORDER BY created_at DESC LIMIT ?""" % (marks, marks),
            ids + ids + [limit],
        ).fetchall()
        others = set()
        for row in rows:
            others.add(int(row["recipient_account_id"])
                       if int(row["sender_account_id"]) in ids
                       else int(row["sender_account_id"]))
        names = _character_names(conn, sorted(others))
    finally:
        conn.close()

    out = []
    for row in rows:
        sent = int(row["sender_account_id"]) in ids
        other = int(row["recipient_account_id"]) if sent else int(row["sender_account_id"])
        out.append({
            "t": str(row["created_at"] or "")[:19],
            "direction": "sent" if sent else "received",
            "item": ((row["item_name"] or "").strip()
                     or (row["template_id"] or "").strip() or UNKNOWN_ITEM),
            "grade": row["item_grade"],
            "counterparty": names.get(other) or UNKNOWN_COUNTERPARTY,
            "status": row["status"],
        })
    return out
