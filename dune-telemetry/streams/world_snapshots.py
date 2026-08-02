"""
Phase 1 - world-snapshots stream.

Server-wide counters mirroring the old sampler's three count(*) queries on
dune.actors. Stored (ts, metric, value) - the renamed `snapshots` table from
stats.db, kept in the same shape for parallel-run diff parity.

Note the `vehicles` metric here uses the legacy loose '%/Vehicles/%' filter
(it includes BP_VehiclesFabricator_C) on purpose, for stats.db parity. The
vehicle-position stream uses the tighter /FlyingVehicles/ filter and is a
separate concern.
"""
from __future__ import annotations

import logging
import time

import db

log = logging.getLogger("telemetry.world")

COUNT_QUERY = "SELECT count(*) AS n FROM dune.actors WHERE class LIKE %(pattern)s"

METRICS = [
    ("subfiefs", "%BP_TotemSmall.BP_TotemSmall_C"),
    ("structures", "%/Systems/Building/%"),
    ("vehicles", "%/Vehicles/%"),
]


def run(ctx):
    ts = int(time.time())
    out = []
    for metric, pattern in METRICS:
        rows = ctx.gamedb.query(COUNT_QUERY, {"pattern": pattern})
        value = rows[0]["n"] if rows else 0
        out.append((ts, metric, value))
    written = db.insert_many(
        ctx.store, "world_snapshots", ["ts", "metric", "value"], out)
    log.info("world: %s", ", ".join("%s=%d" % (m, v) for _, m, v in out))
    return written


STREAM = {"name": "world", "interval_attr": "world_interval", "run": run}
