"""
Login-days stream (login-rewards V2).

One row per online ACCOUNT per UTC day into portal_login_days, the daily-active
signal the rewards streak calc reads. Same source as the presence stream:
dune.player_state (Funcom's canonical online view, tombstone-safe — NOT
encrypted_player_state, whose Deleted-character rows stay stuck at Online), with
DISTINCT account_id so one online player is one row.

INSERT OR IGNORE on the (account_id, date_utc) PRIMARY KEY makes every sweep for
the same day idempotent, so first_seen_ts keeps the FIRST sighting of the day.

Boundary = UTC midnight (date(ts,'unixepoch')). Deliberately NOT coupled to
universe_time (that is an in-game timespan basis with no daily-reset concept).
Streak = consecutive date_utc per account, derived downstream. Deleted accounts
self-clean via jobs/accounts_sweep.py, same as the other per-account tables.
"""
from __future__ import annotations

import logging
import time

import db

log = logging.getLogger("telemetry.login_days")

QUERY = (
    "SELECT DISTINCT account_id FROM dune.player_state "
    "WHERE online_status = 'Online' AND account_id IS NOT NULL"
)


def run(ctx):
    ts = int(time.time())
    # date(ts,'unixepoch') semantics: UTC calendar day.
    date_utc = time.strftime("%Y-%m-%d", time.gmtime(ts))
    rows = ctx.gamedb.query(QUERY)
    # account_id stored as TEXT to match presence + the other per-account tables.
    out = [(str(r["account_id"]), date_utc, ts)
           for r in rows if r.get("account_id") is not None]
    written = db.insert_many_ignore(
        ctx.store, "portal_login_days",
        ["account_id", "date_utc", "first_seen_ts"], out)
    log.info("login_days: %d online, %d new day-rows (%s)",
             len(rows), written, date_utc)


STREAM = {"name": "login_days", "interval_attr": "login_days_interval", "run": run}
