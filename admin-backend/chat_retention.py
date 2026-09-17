"""Portal chat retention (Fremkit wave 11, lane B).

One in-process loop, the event_reminders shape: once an hour, delete the chat
rows that have aged past the owner's ruling. Three windows, three tables:

  * MESSAGES older than 30 days, and every report that pointed at one of them.
  * REPORTS resolved more than 90 days ago, whatever their message.
  * MUTES lifted, or expired, more than 30 days ago.

Three properties this module exists to hold:

  * THE DARK FLAG DOES NOT STOP IT. LASTSIETCH_CHAT_ENABLED decides whether players can
    reach chat. It has nothing to say about how long the messages they already
    sent are kept, and wiring retention to a feature gate is how a switched-off
    feature quietly becomes an unbounded archive. Nothing in this file reads the
    flag.
  * A PERMANENT MUTE IS PERMANENT. until_utc NULL and lifted_utc NULL is "until
    lifted" and matches no window here. Only a mute that has an END is old
    enough to forget, which is why the two clauses both test a non-null column
    rather than testing for absence.
  * REPORTS GO BEFORE THEIR MESSAGES. portal_chat_reports.message_id points at a
    row this tick is about to delete, so the child rows are removed first and in
    the same transaction. The other order leaves reports referring to messages
    nobody can read.

A tick before lane A's tables exist is a no-op, not an hourly traceback: the
four tables are looked up in sqlite_master first, so this loop is safe to ship
ahead of the schema and safe to leave running if chat is ever removed.

No message body is read, logged or copied anywhere in this file. It counts rows.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database import get_db

log = logging.getLogger(__name__)

TS_FMT = "%Y-%m-%d %H:%M:%S"

# Hourly. Retention is a 30-day promise, so the difference between a tick every
# minute and a tick every hour is noise, and the cheap one is correct.
RETENTION_INTERVAL = 3600

# The owner's windows, in days.
MESSAGE_DAYS = 30
REPORT_DAYS = 90
MUTE_DAYS = 30

TABLES = ("portal_chat_messages", "portal_chat_reads", "portal_chat_mutes",
          "portal_chat_reports")


def _cutoff(moment: datetime, days: int) -> str:
    """The admin.db timestamp `days` before `moment`. The column is a fixed-width
    UTC string, so a lexicographic compare is a chronological one."""
    return (moment - timedelta(days=days)).strftime(TS_FMT)


def _tables_present(conn) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
        "('portal_chat_messages', 'portal_chat_mutes', 'portal_chat_reports')"
    ).fetchall()
    return len(rows) == 3


def run_once(now: datetime | None = None) -> dict:
    """Apply all three windows. Returns {messages, reports, mutes} row counts.

    One transaction: a tick that deleted the messages and then failed before
    their reports would leave the queue pointing at rows that are gone, and the
    next tick would not find them again because the message window is measured
    against the messages table."""
    moment = now or datetime.now(timezone.utc)
    conn = get_db()
    try:
        if not _tables_present(conn):
            return {"messages": 0, "reports": 0, "mutes": 0, "skipped": True}
        message_cut = _cutoff(moment, MESSAGE_DAYS)
        report_cut = _cutoff(moment, REPORT_DAYS)
        mute_cut = _cutoff(moment, MUTE_DAYS)

        conn.execute("BEGIN IMMEDIATE")
        # Children first, and selected by their PARENT's age rather than their
        # own: a report filed yesterday about a message from five weeks ago goes
        # with the message it is about, because what it points at is gone.
        orphaned = conn.execute(
            "DELETE FROM portal_chat_reports WHERE message_id IN "
            "(SELECT id FROM portal_chat_messages WHERE created_utc < ?)",
            (message_cut,)).rowcount
        messages = conn.execute(
            "DELETE FROM portal_chat_messages WHERE created_utc < ?",
            (message_cut,)).rowcount
        # Long-resolved reports whose message is still inside its own window.
        resolved = conn.execute(
            "DELETE FROM portal_chat_reports "
            "WHERE resolved_utc IS NOT NULL AND resolved_utc < ?",
            (report_cut,)).rowcount
        # A mute that ENDED, either because it was lifted or because it ran out.
        # A row with both columns null is "until lifted" and is never swept.
        mutes = conn.execute(
            "DELETE FROM portal_chat_mutes WHERE "
            "(lifted_utc IS NOT NULL AND lifted_utc < ?) OR "
            "(lifted_utc IS NULL AND until_utc IS NOT NULL AND until_utc < ?)",
            (mute_cut, mute_cut)).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    counts = {"messages": max(messages, 0),
              "reports": max(orphaned, 0) + max(resolved, 0),
              "mutes": max(mutes, 0), "skipped": False}
    if counts["messages"] or counts["reports"] or counts["mutes"]:
        log.info("chat_retention: swept messages=%d reports=%d mutes=%d",
                 counts["messages"], counts["reports"], counts["mutes"])
    return counts


async def sync_loop(app):
    """Background loop: sweep once an hour. There is no kill switch on purpose
    (see the module docstring). A failing tick is logged and the loop continues;
    cancellation propagates so the lifespan can shut it down."""
    log.info("chat_retention: loop started (interval=%ss)", RETENTION_INTERVAL)
    while True:
        await asyncio.sleep(RETENTION_INTERVAL)
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("chat_retention: tick failed: %s", exc)
