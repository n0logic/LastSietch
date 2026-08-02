"""
Phase 1 - presence stream.

One row per online ACCOUNT per sweep. Source = dune.player_state (Funcom's
canonical online view, used by their own get-online-players procs), NOT
dune.encrypted_player_state: eps keeps DELETED-character tombstones stuck at
online_status='Online' and holds a row per character, so counting it inflated
concurrency ~5x (24 eps rows vs 5 real players, verified 2026-07-02). DISTINCT
account_id collapses any multi-character rows so one online player = one row.
"""
from __future__ import annotations

import logging
import time

import db

log = logging.getLogger("telemetry.presence")

QUERY = (
    "SELECT DISTINCT account_id FROM dune.player_state "
    "WHERE online_status = 'Online' AND account_id IS NOT NULL"
)


def run(ctx):
    ts = int(time.time())
    rows = ctx.gamedb.query(QUERY)
    out = [(ts, r["account_id"]) for r in rows if r.get("account_id")]
    written = db.insert_many(ctx.store, "presence", ["ts", "account_id"], out)
    log.info("presence: %d online, %d rows written", len(rows), written)


STREAM = {"name": "presence", "interval_attr": "presence_interval", "run": run}
