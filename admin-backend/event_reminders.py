"""Portal event reminders (Fremkit wave 9).

One in-process loop, the market_watch shape: every EVENT_REMINDER_INTERVAL
seconds, find the portal_event_reminders rows whose event starts within the next
hour and mail each one a mailbox notification.

Three properties this module exists to hold:

  * A REMINDER FIRES ONCE. notified_at is the ledger, and it is written on the
    same connection right after the mailbox row lands. Two ticks that overlap the
    same window send one message, not two.
  * A LATE TICK IS STILL A USEFUL TICK. The window is "starts in the future, and
    within the next hour", not "starts in 60 +/- 1 minutes". A loop that was down
    for half an hour still warns everyone whose event has not started yet, which
    is the only version of this a player would call working.
  * AN EVENT THAT IS NOT HAPPENING NEVER SENDS. Draft and cancelled are filtered
    in SQL, next to the window, so there is no path that reaches mailbox.post
    without having proved the event is published and still ahead.

The notification body carries no account id and no discord id. mailbox.post
strips *account_id* keys from a payload as a backstop, but nothing here passes
one in the first place: the recipient is addressed by recipient_id, and the
payload is the event id alone.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import config
import mailbox
from database import get_db

log = logging.getLogger(__name__)

TS_FMT = "%Y-%m-%d %H:%M:%S"

# T-1h reminder. The window's lower bound is "now", so an event that already
# started drops out of it rather than sending a message about the past.
LEAD_SECONDS = 3600

# Lead ruling 8: at most 200 rows per tick, so one popular event cannot turn a
# single tick into a multi-minute mailbox write storm.
TICK_LIMIT = 200

EVENT_URL = "https://portal.lastsietch.com/events/{id}"

_DUE_SQL = """
    SELECT r.event_id AS event_id,
           r.account_id AS account_id,
           e.title AS title,
           e.kind AS kind,
           e.starts_utc AS starts_utc,
           e.map_name AS map_name
      FROM portal_event_reminders r
      JOIN portal_events e ON e.id = r.event_id
     WHERE r.notified_at IS NULL
       AND e.status = 'published'
       AND e.starts_utc > ?
       AND e.starts_utc <= ?
     ORDER BY e.starts_utc ASC, r.event_id ASC, r.account_id ASC
     LIMIT ?
"""


def _now_str(now: datetime | None) -> tuple[str, str]:
    """(now, now + lead) as admin.db timestamps. The column is a fixed-width UTC
    string, so lexical comparison is chronological comparison."""
    moment = now or datetime.now(timezone.utc)
    return (moment.strftime(TS_FMT),
            (moment + timedelta(seconds=LEAD_SECONDS)).strftime(TS_FMT))


def _body(row) -> str:
    lines = [str(row["title"] or "").strip() or "Event"]
    kind = str(row["kind"] or "").strip()
    if kind:
        lines.append(f"Kind: {kind}")
    lines.append(f"Starts: {row['starts_utc']} UTC")
    map_name = str(row["map_name"] or "").strip()
    if map_name:
        lines.append(f"Map: {map_name}")
    lines.append(EVENT_URL.format(id=row["event_id"]))
    return "\n".join(lines)


def run_once(now: datetime | None = None) -> int:
    """Send every due T-1h reminder. Returns the number of rows sent.

    mailbox.post opens its own connection and commits before returning, so the
    notified_at write below is issued after that transaction has closed: the two
    writers never hold the database at the same time. Marking one row at a time
    means a failure mid-batch costs at most the row it was on, never a replay of
    the rows already delivered."""
    now_s, horizon_s = _now_str(now)
    conn = get_db()
    try:
        rows = conn.execute(_DUE_SQL, (now_s, horizon_s, TICK_LIMIT)).fetchall()
        sent = 0
        for row in rows:
            # One poison row (a locked mailbox write, a bad title) must cost only
            # itself: log it and move on, never abort the tick and starve every
            # row behind it until the event falls out of the window.
            try:
                mailbox.post(
                    recipient_kind="player",
                    recipient_id=int(row["account_id"]),
                    kind="notification",
                    subject=f"Starts in an hour: {str(row['title'] or '').strip()}",
                    body=_body(row),
                    payload={"event_id": int(row["event_id"])},
                    sender_kind="system",
                )
            except Exception as exc:
                log.warning("event_reminders: post failed for event %s account %s: %s",
                            row["event_id"], row["account_id"], exc)
                continue
            try:
                conn.execute(
                    "UPDATE portal_event_reminders SET notified_at = ? "
                    "WHERE event_id = ? AND account_id = ?",
                    (now_s, row["event_id"], row["account_id"]))
                conn.commit()
            except Exception as exc:
                # A mark failure is a statement about the DATABASE (a lock), not
                # this row: continuing would post every remaining row unmarked
                # and the next tick would re-send them all. Stop the tick here;
                # this one row is the only possible duplicate.
                log.error("event_reminders: sent but could not mark event %s account %s: %s",
                          row["event_id"], row["account_id"], exc)
                break
            sent += 1
        if sent:
            log.info("event_reminders: tick sent=%d", sent)
        return sent
    finally:
        conn.close()


async def sync_loop(app):
    """Background loop: deliver due reminders every EVENT_REMINDER_INTERVAL
    seconds. Self-disables when EVENT_REMINDERS_ENABLED is off so the kill switch
    costs a restart and nothing else. A failing tick is logged and the loop
    continues; cancellation propagates so the lifespan can shut it down."""
    if not config.EVENT_REMINDERS_ENABLED:
        log.info("event_reminders: disabled (LASTSIETCH_EVENT_REMINDERS_ENABLED=0); loop not started")
        return
    log.info("event_reminders: loop started (interval=%ss)", config.EVENT_REMINDER_INTERVAL)
    while True:
        await asyncio.sleep(config.EVENT_REMINDER_INTERVAL)
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("event_reminders: tick failed: %s", exc)
