"""Identity-keyed Solari gift caps + durable gift-attempt ledger.

Sibling of portal_rate_limit.py: same windowed-COUNT idiom, same SQLite source
of truth, no in-memory state.

WHY THIS EXISTS. The gift caps in scripts/dune-gift-op.sh run in Postgres and
key on account_id. The discord_id <-> account_id map lives in a DIFFERENT
database (admin.db, ls_account_links), so Postgres cannot collapse a player's
accounts into one identity. Multi-accounting is permitted policy, so an
account-keyed cap is bypassed by relaying through your own alts: every hop gets
a fresh per-pair allowance. Keying both caps on the DISCORD identity closes it,
and that check therefore has to run here, in FastAPI, ahead of the writer.

The Postgres account-keyed caps stay exactly as they are, as a backstop.

Three jobs, one table (portal_gift_events):

  1. per-identity daily cap        — summed across every account linked to the
     sender's discord_id.
  2. per-identity-PAIR daily cap   — (sender identity, recipient identity). This
     is the half that actually kills the relay chain; capping the sender's daily
     total alone merely redistributes it across destinations.
  3. durable attempt record        — this table is in a different database from
     the money movement, so the Postgres RAISE EXCEPTION that rolls back a
     refused gift cannot roll back the record that it was attempted.

INTRA-IDENTITY TRANSFERS (sender identity == recipient identity) are recorded
but NOT counted against either cap. That is safe only because (2) caps the
moment money crosses to a different identity, which makes a self-hop useless as
a laundering step, and it keeps moving your own Solari between your own accounts
free, which is the legitimate use.

RESERVE-THEN-SETTLE. check_and_reserve() inserts a 'pending' row inside the same
SQLite write transaction that counted the window, and live pending rows count.
Without that, two concurrent submits both read a not-yet-breached cap and both
pass, which is the same class of hole this module exists to close. A pending row
whose request died mid-relay stops counting after PENDING_TTL_SECONDS, so a
crash cannot wedge a player's allowance.

Counters count 'applied' and live 'pending' only. A 'replay' moved no money (its
original 'applied' row is already counted) and refused/failed attempts must not
lock a player out of their own allowance.
"""
from __future__ import annotations

import portal_identity
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)

# Mirrors the writer's defaults (scripts/dune-gift-op.sh). Same numbers, tighter
# key: for a single-account identity these are exactly the Postgres caps, so a
# player with one linked account sees no behaviour change whatsoever. Keep the
# two sides in sync — the writer is still the backstop, not the primary.
GIFT_MAX_PER_DAY = int(os.environ.get("GIFT_MAX_PER_DAY", "20"))
GIFT_MAX_PER_PAIR_PER_DAY = int(os.environ.get("GIFT_MAX_PER_PAIR_PER_DAY", "5"))
GIFT_MAX_AMOUNT = int(os.environ.get("GIFT_MAX_AMOUNT", "5000000"))

# Recipient-side fan-in. ALERT, never block: blocking punishes a popular guild
# bank, and a ring funnelling into one destination is a thing to look at, not a
# thing to refuse. Threshold is Solari received across 30 days from OTHER
# identities; 50M is ten gifts at the ceiling, ~0.21% of the 23.8B supply.
GIFT_INBOUND_ALERT_30D = int(os.environ.get("GIFT_INBOUND_ALERT_30D", "50000000"))
GIFT_INBOUND_ALERT_COOLDOWN_HOURS = 24

# Relay timeout on this path is 40s; 3x headroom before a stranded reservation
# stops counting against the sender.
PENDING_TTL_SECONDS = 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_gift_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key      TEXT    NOT NULL UNIQUE,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    settled_at           TEXT,
    sender_identity      TEXT    NOT NULL,
    sender_account_id    INTEGER NOT NULL,
    recipient_identity   TEXT    NOT NULL,
    recipient_account_id INTEGER NOT NULL,
    amount               INTEGER NOT NULL,
    intra_identity       INTEGER NOT NULL DEFAULT 0,
    status               TEXT    NOT NULL,
    fail_reason          TEXT,
    CHECK (amount > 0),
    CHECK (status IN ('pending','applied','replay','deferred','refused','failed'))
);
CREATE INDEX IF NOT EXISTS idx_pge_sender
    ON portal_gift_events(sender_identity, created_at);
CREATE INDEX IF NOT EXISTS idx_pge_pair
    ON portal_gift_events(sender_identity, recipient_identity, created_at);
CREATE INDEX IF NOT EXISTS idx_pge_inbound
    ON portal_gift_events(recipient_identity, created_at);
"""

# Counted window: a gift that moved money, or one currently in flight.
_LIVE = "(status = 'applied' OR (status = 'pending' AND created_at >= ?))"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ago(**kw) -> str:
    return _stamp(_now() - timedelta(**kw))


def init() -> None:
    """Idempotent table + index creation (also done lazily by every writer)."""
    conn = get_db()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def identity_for_account(account_id: int) -> Optional[str]:
    """The discord_id that owns this game account, or None if it has no active
    link. Newest active link wins, matching _resolve_account_by_char_name's
    convention: ls_account_links is UNIQUE(discord_id, account_id), which does
    not by itself forbid two identities holding a live link to one account."""
    if not account_id:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            ("""SELECT discord_id FROM ls_account_links
                WHERE account_id = ? AND revoked_at IS NULL
                ORDER BY linked_at DESC LIMIT 1""").replace("ls_account_links", portal_identity.link_table(conn)),
            (int(account_id),),
        ).fetchone()
    finally:
        conn.close()
    return str(row["discord_id"]) if row else None


def recipient_identity_key(account_id: int) -> str:
    """Cap key for a recipient. Falls back to a per-account sentinel so an
    unlinked recipient still binds the pair cap to something instead of
    collapsing every unlinked recipient into one shared NULL bucket. The portal
    path resolves recipients through ls_account_links, so the fallback is
    defensive only."""
    return identity_for_account(account_id) or f"acct:{int(account_id)}"


def accounts_for_identity(discord_id: str) -> list[int]:
    """Every game account linked to this identity. Not used by the cap queries
    (they key on the stored identity directly, which is cheaper and does not
    go stale when a link is revoked mid-window) — exposed for ops/tests."""
    if not discord_id:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            ("""SELECT account_id FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL
                ORDER BY account_id""").replace("ls_account_links", portal_identity.link_table(conn)),
            (str(discord_id),),
        ).fetchall()
    finally:
        conn.close()
    return [int(r["account_id"]) for r in rows]


def check_and_reserve(*, idempotency_key: str, sender_identity: str,
                      sender_account_id: int, recipient_identity: str,
                      recipient_account_id: int,
                      amount: int) -> Tuple[bool, str, int]:
    """Identity-collapsed pre-check, then reserve the attempt in the same write.

    Returns (ok, reason, retry_after_seconds). On breach a durable 'refused' row
    is written before returning, so a probe of the caps leaves a trace.

    An idempotency_key that already has a row skips the caps entirely and is
    allowed through unreserved: it is either a retry of something already
    counted, or a retry of something that moved no money. Either way the
    Postgres UNIQUE(idempotency_key) gate decides, and re-counting it here would
    refuse a legitimate retry-after-timeout sitting on the cap boundary."""
    intra = 1 if sender_identity == recipient_identity else 0
    day_floor = _ago(days=1)
    pending_floor = _ago(seconds=PENDING_TTL_SECONDS)

    conn = get_db()
    try:
        conn.executescript(_SCHEMA)
        # Explicit transaction control: the count and the reservation that acts
        # on it must be one write, or two concurrent submits both pass a cap
        # neither has breached yet.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        sender_keys = portal_identity.quota_keys(conn, sender_identity)
        recipient_keys = portal_identity.quota_keys(conn, recipient_identity)

        prior = conn.execute(
            "SELECT status FROM portal_gift_events WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None and prior["status"] in ("applied", "replay", "pending"):
            # Already counted (applied/pending) or a provable no-op replay: the
            # Postgres UNIQUE(idempotency_key) gate adjudicates downstream.
            conn.commit()
            return True, "", 0
        # A prior 'refused'/'failed'/'deferred' row moved no money and never
        # reached the Postgres gate, so there is nothing to replay -- a reused
        # key MUST face the caps again, or one refusal converts into an
        # allowance on resend. On pass, the row is re-reserved in place (the
        # UPSERT below).

        if not intra:
            day = conn.execute(
                f"""SELECT COUNT(*) AS c FROM portal_gift_events
                     WHERE sender_identity IN ({",".join("?" * len(sender_keys))})
                       AND intra_identity = 0
                       AND created_at >= ?
                       AND {_LIVE}""",
                (*sender_keys, day_floor, pending_floor),
            ).fetchone()["c"]
            if day >= GIFT_MAX_PER_DAY:
                _refuse(conn, idempotency_key, sender_identity, sender_account_id,
                        recipient_identity, recipient_account_id, amount, intra,
                        f"identity daily cap ({day} in 24h, max {GIFT_MAX_PER_DAY})")
                return False, "daily", 24 * 3600

            pair = conn.execute(
                f"""SELECT COUNT(*) AS c FROM portal_gift_events
                     WHERE sender_identity IN ({",".join("?" * len(sender_keys))})
                       AND recipient_identity IN ({",".join("?" * len(recipient_keys))})
                       AND intra_identity = 0
                       AND created_at >= ?
                       AND {_LIVE}""",
                (*sender_keys, *recipient_keys, day_floor, pending_floor),
            ).fetchone()["c"]
            if pair >= GIFT_MAX_PER_PAIR_PER_DAY:
                _refuse(conn, idempotency_key, sender_identity, sender_account_id,
                        recipient_identity, recipient_account_id, amount, intra,
                        f"identity pair cap ({pair} in 24h, "
                        f"max {GIFT_MAX_PER_PAIR_PER_DAY})")
                return False, "pair", 24 * 3600

        conn.execute(
            """INSERT INTO portal_gift_events
                 (idempotency_key, sender_identity, sender_account_id,
                  recipient_identity, recipient_account_id, amount,
                  intra_identity, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(idempotency_key) DO UPDATE SET
                 status = 'pending', created_at = datetime('now'),
                 settled_at = NULL, fail_reason = NULL,
                 sender_identity = excluded.sender_identity,
                 sender_account_id = excluded.sender_account_id,
                 recipient_identity = excluded.recipient_identity,
                 recipient_account_id = excluded.recipient_account_id,
                 amount = excluded.amount,
                 intra_identity = excluded.intra_identity""",
            (idempotency_key, sender_identity, int(sender_account_id),
             recipient_identity, int(recipient_account_id), int(amount), intra),
        )
        conn.commit()
        return True, "", 0
    finally:
        conn.close()


def _refuse(conn, idempotency_key, sender_identity, sender_account_id,
            recipient_identity, recipient_account_id, amount, intra, reason):
    """Durable refusal row, committed on the caller's open transaction."""
    conn.execute(
        """INSERT INTO portal_gift_events
             (idempotency_key, sender_identity, sender_account_id,
              recipient_identity, recipient_account_id, amount,
              intra_identity, status, fail_reason, settled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'refused', ?, datetime('now'))
           ON CONFLICT(idempotency_key) DO UPDATE SET
             status = 'refused', fail_reason = excluded.fail_reason,
             settled_at = datetime('now')""",
        (idempotency_key, sender_identity, int(sender_account_id),
         recipient_identity, int(recipient_account_id), int(amount), intra,
         reason[:500]),
    )
    conn.commit()


def settle(idempotency_key: str, status: str,
           fail_reason: str | None = None) -> None:
    """Stamp the writer's verdict onto a reserved attempt.

    Scoped to status='pending' so it can only ever close a row this process
    reserved — a stray settle can never rewrite an 'applied' row that the
    counters are keyed on, nor erase a 'refused' one."""
    conn = get_db()
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            """UPDATE portal_gift_events
                  SET status = ?, fail_reason = ?, settled_at = datetime('now')
                WHERE idempotency_key = ? AND status = 'pending'""",
            (status, (fail_reason or "")[:500] or None, idempotency_key),
        )
        conn.commit()
    finally:
        conn.close()


def inbound_30d_total(recipient_identity: str) -> int:
    """Solari this identity received over 30 days from OTHER identities.
    Intra-identity moves are the player's own money and are excluded."""
    conn = get_db()
    try:
        conn.executescript(_SCHEMA)
        recipient_keys = portal_identity.quota_keys(conn, recipient_identity)
        row = conn.execute(
            f"""SELECT COALESCE(SUM(amount), 0) AS t FROM portal_gift_events
                WHERE recipient_identity IN ({",".join("?" * len(recipient_keys))})
                  AND intra_identity = 0
                  AND status = 'applied'
                  AND created_at >= ?""",
            (*recipient_keys, _ago(days=30)),
        ).fetchone()
    finally:
        conn.close()
    return int(row["t"]) if row else 0


async def alert_inbound_if_over(*, recipient_identity: str, sender_identity: str,
                                recipient_account_id: int, amount: int,
                                ip: str) -> Optional[int]:
    """Fan-in ALERT (never a block). If this identity's 30-day inbound total is
    over the threshold, write a durable audit_log row and post to the ops
    bot-logs channel — the same signal path update_orchestrator uses.

    Deduped to one alert per recipient identity per 24h off the audit_log row
    itself, so the durable record is also the dedupe key and the two cannot
    drift. Returns the 30-day total when an alert fired, else None.

    Best-effort throughout: a Discord outage must never fail a gift.

    OPSEC: discord ids and character-facing amounts only. No real names ever
    reach a channel, and bot-logs is admin-only regardless."""
    try:
        total = inbound_30d_total(recipient_identity)
        if total < GIFT_INBOUND_ALERT_30D:
            return None

        from auth import audit_log, recent_action_count
        since = _ago(hours=GIFT_INBOUND_ALERT_COOLDOWN_HOURS)
        if recent_action_count("portal_gift_inbound_alert", since,
                               targets=[recipient_identity],
                               success_only=False) > 0:
            return total

        detail = (f"30d_inbound={total} threshold={GIFT_INBOUND_ALERT_30D} "
                  f"last_from={sender_identity} last_amount={amount} "
                  f"recipient_account={recipient_account_id}")
        audit_log(None, "portal:gift-monitor", "portal_gift_inbound_alert",
                  recipient_identity, ip, details=detail, success=True)

        try:
            import discord_post
            from config import DISCORD_CH_BOTLOGS
            if DISCORD_CH_BOTLOGS:
                await discord_post.post_to_channel(
                    DISCORD_CH_BOTLOGS,
                    f"Solari fan-in alert: recipient identity `{recipient_identity}` "
                    f"has received {total:,} Solari in 30 days "
                    f"(threshold {GIFT_INBOUND_ALERT_30D:,}). "
                    f"Latest gift {amount:,} from `{sender_identity}`. "
                    f"Not blocked - review only.")
        except Exception:  # noqa: BLE001
            logger.warning("gift: inbound alert post failed", exc_info=True)
        return total
    except Exception:  # noqa: BLE001
        logger.warning("gift: inbound alert check failed", exc_info=True)
        return None
