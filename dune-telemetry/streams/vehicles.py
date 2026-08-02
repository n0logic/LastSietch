"""
Phase 3 - vehicle position stream.

One position snapshot per piloted flying vehicle per sweep. The flight-distance
job later integrates these into per-player weekly air distance.

Verified correctness notes (these override the design doc):
  - The pilot resolves via player_state.player_pawn_id = overmap_players.player_id.
    The player_controller_id join yields ZERO rows - do not use it.
  - The class filter is %/FlyingVehicles/% (config.VEHICLE_CLASS_FILTER), NOT
    %/Vehicles/% which also matches BP_VehiclesFabricator_C (a building).
  - (transform).location is in Unreal cm. Raw cm is stored; conversion to
    metres/km happens only at read time in the API.
"""
from __future__ import annotations

import logging
import time

import db
from config import VEHICLE_CLASS_FILTER

log = logging.getLogger("telemetry.vehicles")

QUERY = """
SELECT op.vehicle_id,
       a_v.class,
       (a_v.transform).location.x AS x,
       (a_v.transform).location.y AS y,
       (a_v.transform).location.z AS z,
       ps.account_id     AS pilot_account_id,
       ps.character_name AS pilot_name
FROM dune.overmap_players op
JOIN dune.actors a_v       ON a_v.id = op.vehicle_id
JOIN dune.player_state ps  ON ps.player_pawn_id = op.player_id
WHERE op.vehicle_id IS NOT NULL
  AND a_v.class ILIKE %(filter)s
"""

COLUMNS = ["ts", "vehicle_id", "vehicle_class", "pilot_account_id",
           "pilot_name", "x", "y", "z"]


def run(ctx):
    ts = int(time.time())
    rows = ctx.gamedb.query(QUERY, {"filter": VEHICLE_CLASS_FILTER})
    out = [
        (ts, r["vehicle_id"], r["class"], r.get("pilot_account_id"),
         r.get("pilot_name"), r.get("x"), r.get("y"), r.get("z"))
        for r in rows
    ]
    written = db.insert_many(ctx.store, "vehicle_positions", COLUMNS, out)
    log.info("vehicles: %d piloted flyers, %d rows written", len(rows), written)


STREAM = {"name": "vehicles", "interval_attr": "vehicle_interval", "run": run}
