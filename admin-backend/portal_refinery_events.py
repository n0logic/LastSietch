"""Identity-keyed weekly caps for the Ingot Refinery (wave 10).

Sibling of portal_transfer_events.py and portal_gift_limits.py: same table
shape, same reserve-then-settle idiom, same SQLite source of truth, no
in-memory state. Two things about this counter are NOT the same, and both are
load-bearing:

  1. IT SUMS UNITS, IT DOES NOT COUNT ROWS. Every other counter in this repo
     asks how many attempts a player made. This one asks how much DUST they
     received, because one request can mint up to 50 batches. A COUNT here
     would let 50 requests of 50 batches through a cap of 250.
  2. SIX INDEPENDENT COUNTERS, NEVER ONE POOL. The owner's ruling is 250 dusts
     of EACH tier per week. The window is therefore keyed on
     (actor_identity, output_template): a player who has maxed Copper this week
     still has a full Plastanium allowance, and nothing a player does to one
     tier moves another tier's number.

WHY THE DISCORD IDENTITY IS THE KEY. LASTSIETCH_MULTIACCOUNT lets one person hold
several linked game accounts, and the account_id <-> discord_id map lives in
admin.db, which Postgres cannot see. An account-keyed cap is therefore
multiplied by however many alts a player has linked, which is the exact hole
the transfer lane found. account_id and owner_ctrl are stored on every row
anyway, so the owner can loosen the cap to per-account later without a
migration, and so the game-side backstop over dune.ls_refinery_exchanges can
be reconciled against these rows.

RESERVE-THEN-SETTLE. check_and_reserve() inserts a 'pending' row inside the
same SQLite write transaction that summed the window, and live pending rows
count toward it. Without that, two concurrent submits both read a not-yet-
breached cap and both pass. A pending row whose request died mid-relay stops
counting after PENDING_TTL_SECONDS, so a crash cannot wedge a week of a
player's allowance.

Counted statuses: 'applied', 'replay' and live 'pending'. Refused, failed and
deferred minted no dust and must not spend a player's own allowance.
"""
from __future__ import annotations

import portal_identity
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from database import get_db

logger = logging.getLogger(__name__)

# Owner ruling R2: 250 dusts of EACH tier per identity per rolling seven days.
# The rate table on the game host is the authority for what a batch COSTS; this
# is the ceiling on what a week may PRODUCE, and it lives here because the cap
# key (the Discord identity) is only knowable on this side.
WEEKLY_DUST_CAP = 250
WINDOW_DAYS = 7

# SAFETY bound, NOT the product ceiling. The rate table on the game host owns the
# real per-request limit (max_batches_per_request, 50 today) and the route
# refuses above whatever the catalog published, so the owner can retune it
# without a deploy. This number is only the outer wall a hand-built body cannot
# climb, and it is one full output stack: 500 dust is the most a single mint
# could land in one slot, so nothing legitimate is above it.
SAFETY_MAX_BATCHES_PER_REQUEST = 500

# Relay timeout on this path is 45s; 4x headroom before a stranded reservation
# stops counting against the player who made it.
PENDING_TTL_SECONDS = 180

STATUSES = ("pending", "applied", "replay", "deferred", "refused", "failed")

# Facts the route may stamp onto a reserved attempt from the writer's answer,
# mapped from ITS key names onto this table's columns; the first key present
# wins. Whitelisted because settle() takes **facts, so only these names ever
# reach the UPDATE and only as a column name this module chose.
#
# dust_units is deliberately NOT here. It is the column the caps are summed
# over and it is fixed at reservation time. Letting the writer's echo rewrite
# it would mean a truncated or misparsed response could silently hand a player
# back an allowance they have already spent.
_SETTLE_FACTS = (
    ("input_units", ("input_units",)),
    ("spice_units", ("spice_units",)),
    ("input_template", ("input_template",)),
    ("owner_ctrl", ("owner_ctrl",)),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_refinery_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key   TEXT    NOT NULL UNIQUE,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    settled_at        TEXT,
    actor_identity    TEXT    NOT NULL,
    account_id        INTEGER NOT NULL,
    owner_ctrl        INTEGER,
    tier              INTEGER NOT NULL,
    output_template   TEXT    NOT NULL,
    input_template    TEXT,
    batches           INTEGER NOT NULL,
    dust_units        INTEGER NOT NULL,
    input_units       INTEGER,
    spice_units       INTEGER,
    status            TEXT    NOT NULL,
    fail_reason       TEXT,
    CHECK (batches > 0),
    CHECK (dust_units > 0),
    CHECK (status IN ('pending','applied','replay','deferred','refused','failed'))
);
CREATE INDEX IF NOT EXISTS idx_pre_identity_output
    ON portal_refinery_events(actor_identity, output_template, created_at);
CREATE INDEX IF NOT EXISTS idx_pre_account
    ON portal_refinery_events(account_id, created_at);
"""

# Counted window: an exchange that minted dust, or one currently in flight.
_LIVE = "(status IN ('applied','replay') OR (status = 'pending' AND created_at >= ?))"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ago(**kw) -> str:
    return _stamp(_now() - timedelta(**kw))


def _ensure(conn) -> None:
    """Lazy DDL: one cheap sqlite_master probe per call, the CREATE statements
    only when the table is missing. The Workshop page calls caps() on every
    load, and a test may recreate the database file under the same path, so
    neither "always run the script" nor a process-wide flag fits."""
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'portal_refinery_events'"
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


def _used(conn, actor_identity: str, output_template: str,
          window_floor: str, pending_floor: str) -> int:
    """Dust units this identity has taken of ONE tier inside the window.

    SUM, not COUNT, and COALESCE so an identity with no rows reads 0 rather
    than None."""
    keys = portal_identity.quota_keys(conn, actor_identity)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(dust_units), 0) AS units
              FROM portal_refinery_events
             WHERE actor_identity IN ({",".join("?" * len(keys))})
               AND output_template = ?
               AND created_at >= ?
               AND {_LIVE}""",
        (*keys, str(output_template), window_floor, pending_floor),
    ).fetchone()
    return int(row["units"] or 0)


def check_and_reserve(idempotency_key: str, actor_identity: str, account_id: int,
                      owner_ctrl: Optional[int], tier: int, output_template: str,
                      input_template: Optional[str], batches: int,
                      dust_units: int, input_units: Optional[int] = None,
                      spice_units: Optional[int] = None) -> Tuple[bool, str, int, int]:
    """Identity-collapsed weekly pre-check for ONE tier, then reserve the
    attempt in the same write.

    Returns (ok, reason, retry_after_seconds, used). `reason` is 'weekly_cap' on
    a breach; `used` is this tier's window total INCLUDING the reservation on a
    pass, and the current total on a refusal. On breach a durable 'refused' row
    is written before returning, so a probe of the cap leaves a trace.

    An idempotency_key that already holds a live row skips the cap entirely and
    is allowed through unreserved: the same key twice is a REPLAY, not a second
    reservation, and the writer's UNIQUE ledger adjudicates it. Re-counting it
    here would refuse a legitimate retry-after-timeout sitting on the boundary.
    """
    batches = int(batches)
    dust_units = int(dust_units)
    window_floor = _ago(days=WINDOW_DAYS)
    pending_floor = _ago(seconds=PENDING_TTL_SECONDS)

    conn = get_db()
    try:
        _ensure(conn)
        # Explicit transaction control: the sum and the reservation that acts on
        # it must be one write, or two concurrent submits both pass a cap
        # neither has breached yet.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")

        prior = conn.execute(
            "SELECT status FROM portal_refinery_events WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if prior is not None and prior["status"] in ("applied", "replay", "pending"):
            used = _used(conn, actor_identity, output_template,
                         window_floor, pending_floor)
            conn.commit()
            return True, "", 0, used
        # A prior 'refused'/'failed'/'deferred' row minted no dust and never
        # reached the writer's ledger, so there is nothing to replay: a reused
        # key MUST face the cap again, or one refusal converts into an allowance
        # on resend. On pass, the row is re-reserved in place.

        used = _used(conn, actor_identity, output_template,
                     window_floor, pending_floor)
        if used + dust_units > WEEKLY_DUST_CAP:
            _refuse(conn, idempotency_key, actor_identity, account_id, owner_ctrl,
                    tier, output_template, input_template, batches, dust_units,
                    input_units, spice_units,
                    f"weekly dust cap ({used} of {WEEKLY_DUST_CAP} in "
                    f"{WINDOW_DAYS}d, asked {dust_units})")
            return False, "weekly_cap", WINDOW_DAYS * 24 * 3600, used

        conn.execute(
            """INSERT INTO portal_refinery_events
                 (idempotency_key, actor_identity, account_id, owner_ctrl, tier,
                  output_template, input_template, batches, dust_units,
                  input_units, spice_units, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(idempotency_key) DO UPDATE SET
                 status = 'pending', created_at = datetime('now'),
                 settled_at = NULL, fail_reason = NULL,
                 actor_identity = excluded.actor_identity,
                 account_id = excluded.account_id,
                 owner_ctrl = excluded.owner_ctrl,
                 tier = excluded.tier,
                 output_template = excluded.output_template,
                 input_template = excluded.input_template,
                 batches = excluded.batches,
                 dust_units = excluded.dust_units,
                 input_units = excluded.input_units,
                 spice_units = excluded.spice_units""",
            (idempotency_key, str(actor_identity), int(account_id),
             _int_or_none(owner_ctrl), int(tier), str(output_template),
             _str_or_none(input_template), batches, dust_units,
             _int_or_none(input_units), _int_or_none(spice_units)),
        )
        conn.commit()
        return True, "", 0, used + dust_units
    finally:
        conn.close()


def _refuse(conn, idempotency_key, actor_identity, account_id, owner_ctrl, tier,
            output_template, input_template, batches, dust_units, input_units,
            spice_units, reason):
    """Durable refusal row, committed on the caller's open transaction."""
    conn.execute(
        """INSERT INTO portal_refinery_events
             (idempotency_key, actor_identity, account_id, owner_ctrl, tier,
              output_template, input_template, batches, dust_units,
              input_units, spice_units, status, fail_reason, settled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'refused', ?, datetime('now'))
           ON CONFLICT(idempotency_key) DO UPDATE SET
             status = 'refused', fail_reason = excluded.fail_reason,
             settled_at = datetime('now')""",
        (idempotency_key, str(actor_identity), int(account_id),
         _int_or_none(owner_ctrl), int(tier), str(output_template),
         _str_or_none(input_template), int(batches), int(dust_units),
         _int_or_none(input_units), _int_or_none(spice_units), str(reason)[:500]),
    )
    conn.commit()


def settle(idempotency_key: str, status: str, fail_reason: str | None = None,
           **facts) -> None:
    """Stamp the writer's verdict onto a reserved attempt, plus whatever it told
    us about what was actually consumed (see _SETTLE_FACTS for the keys it
    accepts and the one it deliberately drops).

    Scoped to status='pending' so it can only ever close a row this process
    reserved: a stray settle can never rewrite an 'applied' row the counters are
    keyed on, nor erase a 'refused' one. That also makes it idempotent, since
    the second settle of one key matches nothing."""
    if status not in STATUSES:
        logger.warning("refinery: refusing to settle an unknown status %r", status)
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
            "UPDATE portal_refinery_events SET %s "
            " WHERE idempotency_key = ? AND status = 'pending'" % ", ".join(sets),
            args,
        )
        conn.commit()
    finally:
        conn.close()


def _int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value):
    if value is None:
        return None
    return str(value)[:120]


def _fact(column, value):
    """One stored fact, typed for its column."""
    if column == "input_template":
        return _str_or_none(value)
    return _int_or_none(value)


def caps(actor_identity: str, output_templates) -> dict:
    """Per-tier {cap, used, left} for every template asked about, read-only.

    This is the catalog's only source for "dust left this week". The cap that
    DECIDES an exchange is re-summed inside check_and_reserve, so a stale count
    here authorises nothing. A template with no rows reads a full allowance
    rather than being absent, so the page never has to guess."""
    wanted = [str(t) for t in (output_templates or []) if t]
    if not wanted:
        return {}
    window_floor = _ago(days=WINDOW_DAYS)
    pending_floor = _ago(seconds=PENDING_TTL_SECONDS)
    marks = ",".join("?" * len(wanted))

    conn = get_db()
    try:
        _ensure(conn)
        keys = portal_identity.quota_keys(conn, actor_identity)
        rows = conn.execute(
            f"""SELECT output_template, COALESCE(SUM(dust_units), 0) AS units
                  FROM portal_refinery_events
                 WHERE actor_identity IN ({",".join("?" * len(keys))})
                   AND output_template IN ({marks})
                   AND created_at >= ?
                   AND {_LIVE}
                 GROUP BY output_template""",
            keys + wanted + [window_floor, pending_floor],
        ).fetchall()
    finally:
        conn.close()

    used = {str(r["output_template"]): int(r["units"] or 0) for r in rows}
    out = {}
    for template in wanted:
        spent = used.get(template, 0)
        out[template] = {
            "weekly_dust_cap": WEEKLY_DUST_CAP,
            "weekly_dust_used": spent,
            "weekly_dust_left": max(0, WEEKLY_DUST_CAP - spent),
        }
    return out
