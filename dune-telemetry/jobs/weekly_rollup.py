"""
Phase 3 - weekly rollup / raw-position pruning job.

Runs daily. Deletes vehicle_positions rows older than 7 days whose distance
contribution has already been integrated into flight_distance_weekly, and
VACUUMs weekly to reclaim space.

Guard: only prune rows with ts < the flight_distance bookmark - never delete a
sample the integration job has not yet consumed. combat_events and presence
are NOT pruned this session (small; keep full history).
"""
from __future__ import annotations

import logging
import time

import db

log = logging.getLogger("telemetry.weekly_rollup")

RETENTION_SECONDS = 7 * 24 * 3600
VACUUM_SECONDS = 7 * 24 * 3600


def run(ctx):
    store = ctx.store
    now = int(time.time())
    cutoff = now - RETENTION_SECONDS
    bookmark = int(db.get_cursor(store, "flight_distance") or 0)

    # Only prune rows already consumed by the integration job (ts < bookmark)
    # AND older than the retention window.
    prune_before = min(cutoff, bookmark)
    cur = store.execute(
        "DELETE FROM vehicle_positions WHERE ts < ?", (prune_before,))
    pruned = cur.rowcount

    # VACUUM at most weekly. The cursor records the last vacuum epoch.
    last_vacuum = int(db.get_cursor(store, "last_vacuum") or 0)
    vacuumed = False
    if now - last_vacuum >= VACUUM_SECONDS:
        store.commit()
        store.execute("VACUUM")
        db.set_cursor(store, "last_vacuum", now, now)
        vacuumed = True

    log.info("weekly_rollup: pruned %d vehicle_positions rows (before ts %d)%s",
             pruned, prune_before, ", vacuumed" if vacuumed else "")


JOB = {"name": "weekly_rollup", "interval_attr": "rollup_interval", "run": run}
