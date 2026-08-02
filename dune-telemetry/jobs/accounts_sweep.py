"""
Account-prune sweeper job.

Reads the live `dune.accounts.id` set via the logger's read-only psycopg2
handle and DELETEs rows from local telemetry.db whose account_id is no longer
in the live set. Targets the four account-keyed tables only; forensic /
historical tables (combat_events, grant_events, *_positions) are intentionally
left alone so references to since-deleted accounts survive for post-mortem.

Hard safety:
- Never DELETEs on an empty live set (game-DB transient discovery glitch).
- Bails if live_ids count drops below `ACCOUNTS_SWEEP_MIN_LIVE` (default 5).
- ZERO writes to dune.* schema. Reads dune.accounts.id only.
"""
from __future__ import annotations

import logging

log = logging.getLogger("telemetry.accounts_sweep")

# account_id is stored as TEXT in every target table (see db.py SCHEMA).
TARGET_TABLES = (
    "player_progression",
    "player_progression_levelups",
    "roster_snapshot",
    "presence",
)


def _stage_live(store, live_ids):
    """Stage the live id set into a session-local temp table so NOT IN scales
    past SQLite's 999-parameter ceiling without per-chunk delete passes."""
    store.execute("DROP TABLE IF EXISTS _accounts_sweep_live")
    store.execute(
        "CREATE TEMP TABLE _accounts_sweep_live (account_id TEXT PRIMARY KEY)")
    store.executemany(
        "INSERT OR IGNORE INTO _accounts_sweep_live(account_id) VALUES (?)",
        [(aid,) for aid in live_ids])


def _delete_outside(store, table):
    cur = store.execute(
        "DELETE FROM %s WHERE account_id NOT IN "
        "(SELECT account_id FROM _accounts_sweep_live)" % table)
    return cur.rowcount


def run(ctx):
    cfg = ctx.config
    rows = ctx.gamedb.query("SELECT id FROM dune.accounts", {})
    live_ids = {str(r["id"]) for r in rows if r.get("id") is not None}

    if not live_ids:
        log.warning(
            "accounts_sweep: live id set is EMPTY - skipping sweep (no deletes)")
        return

    min_live = cfg.accounts_sweep_min_live
    if len(live_ids) < min_live:
        log.warning(
            "accounts_sweep: live id count %d below floor %d - skipping sweep",
            len(live_ids), min_live)
        return

    _stage_live(ctx.store, live_ids)
    try:
        for table in TARGET_TABLES:
            pruned = _delete_outside(ctx.store, table)
            log.info("accounts_sweep: pruned %d rows from %s (live=%d)",
                     pruned, table, len(live_ids))
    finally:
        ctx.store.execute("DROP TABLE IF EXISTS _accounts_sweep_live")


JOB = {"name": "accounts_sweep",
       "interval_attr": "accounts_sweep_interval",
       "run": run}
