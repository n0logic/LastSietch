"""IP address retention: 90 days, then the address goes.

The chat_retention shape: one in-process loop, once an hour. Two tables hold a
client IP with no end date, and the privacy policy promises 90 days:

  * AUDIT_LOG keeps its rows. It is the security trail (who did what, when), so
    only ip_address is cleared. The column is NOT NULL in the shipped schema and
    SQLite cannot drop that constraint without rebuilding the table, so the
    cleared value is '' rather than NULL. audit_log stores no user agent.
  * PORTAL_LINK_ATTEMPTS rows are deleted outright. Every reader looks back at
    most 24 hours (portal_rate_limit, the v2 lockout card) and the admin history
    card shows the newest 15, so nothing depends on a row past 90 days. The age
    is the row's NEWEST stamp, so an attempt whose callback or answer landed
    inside the window is not swept on the strength of an old state_issued_at.

Like chat retention there is no kill switch: a retention promise that a flag can
pause is not one. A tick before either table exists is a no-op.

No address is read or logged here. It counts rows.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database import get_db

log = logging.getLogger(__name__)

TS_FMT = "%Y-%m-%d %H:%M:%S"

RETENTION_INTERVAL = 3600
IP_DAYS = 90


def _cutoff(moment: datetime, days: int) -> str:
    """Both tables stamp 'YYYY-MM-DD HH:MM:SS' UTC, so a lexicographic compare
    is a chronological one."""
    return (moment - timedelta(days=days)).strftime(TS_FMT)


def _present(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
        "('audit_log', 'portal_link_attempts')"
    ).fetchall()
    return {r[0] for r in rows}


def run_once(now: datetime | None = None) -> dict:
    """Apply the 90-day window. Returns {audit_ips, link_attempts} row counts."""
    moment = now or datetime.now(timezone.utc)
    cut = _cutoff(moment, IP_DAYS)
    conn = get_db()
    try:
        present = _present(conn)
        conn.execute("BEGIN IMMEDIATE")
        audit = 0
        if "audit_log" in present:
            audit = conn.execute(
                "UPDATE audit_log SET ip_address = '' "
                "WHERE ip_address != '' AND timestamp < ?",
                (cut,)).rowcount
        attempts = 0
        if "portal_link_attempts" in present:
            attempts = conn.execute(
                "DELETE FROM portal_link_attempts WHERE "
                "MAX(state_issued_at, COALESCE(callback_hit_at, ''), "
                "COALESCE(state_consumed_at, ''), COALESCE(attempt_at, '')) < ?",
                (cut,)).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    counts = {"audit_ips": max(audit, 0), "link_attempts": max(attempts, 0)}
    if counts["audit_ips"] or counts["link_attempts"]:
        log.info("ip_retention: cleared audit_ips=%d deleted link_attempts=%d",
                 counts["audit_ips"], counts["link_attempts"])
    return counts


async def sync_loop(app):
    """Sweep once at start, then hourly. A failing tick is logged and the loop
    continues; cancellation propagates so the lifespan can shut it down."""
    log.info("ip_retention: loop started (interval=%ss)", RETENTION_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("ip_retention: tick failed: %s", exc)
        await asyncio.sleep(RETENTION_INTERVAL)
