"""
Stream A - live player-position stream.

One coords-only snapshot per online player on Hagga Basin per sweep, feeding
the live dashboard map. The table is a short rolling window: each sweep prunes
rows older than POSITIONS_RETENTION_S so the store stays small at the 5s
cadence.

PII rule (S3): coordinates ONLY. This stream never selects or stores character
names or account ids - only (x, y) and the map. The online filter joins
player_state but selects nothing identifying from it.

Verified correctness notes:
  - The player pawn resolves via player_state.player_pawn_id = actors.id, the
    same join the vehicle stream uses (NOT player_controller_id).
  - (transform).location is in Unreal cm. Raw cm is stored; conversion happens
    only at read time in the dashboard.
"""
from __future__ import annotations

import logging
import time

import db

log = logging.getLogger("telemetry.positions")

# Rolling-window retention. Rows older than this are pruned every sweep so the
# live-position table never grows unbounded at the 5s cadence.
POSITIONS_RETENTION_S = 15 * 60

QUERY = """
SELECT a.map AS map,
       a.partition_id AS partition_id,
       (a.transform).location.x AS x,
       (a.transform).location.y AS y
FROM dune.actors a
JOIN dune.player_state ps ON ps.player_pawn_id = a.id
WHERE ps.online_status = 'Online'
  AND a.map = 'HaggaBasin'
"""

COLUMNS = ["ts", "map", "partition_id", "x", "y"]


def run(ctx):
    ts = int(time.time())
    rows = ctx.gamedb.query(QUERY)
    out = [(ts, r["map"], r.get("partition_id"), r.get("x"), r.get("y")) for r in rows]
    written = db.insert_many(ctx.store, "player_positions", COLUMNS, out)
    pruned = ctx.store.execute(
        "DELETE FROM player_positions WHERE ts < ?",
        (ts - POSITIONS_RETENTION_S,)).rowcount
    log.info("positions: %d online, %d rows written, %d pruned",
             len(rows), written, pruned)


STREAM = {"name": "positions", "interval_attr": "positions_interval", "run": run}
