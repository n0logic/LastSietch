"""
Stream registry for the telemetry logger.

Each stream module exposes a module-level `STREAM` dict:
  {"name": str, "interval_attr": <Config field name>, "run": run_fn}

The scheduler reads STREAMS (in order) and resolves each stream's cadence from
the named Config field, so cadences stay env-driven. Phased rollout is just a
matter of which entries are listed here; the full collector lists them all.
"""
from streams import (presence, login_days, world_snapshots, connections, roster,
                     combat, vehicles, positions, grant_events, progression,
                     transfers, read_models, storage, market, world_events)

STREAMS = [
    presence.STREAM,
    login_days.STREAM,
    world_snapshots.STREAM,
    connections.STREAM,
    roster.STREAM,
    combat.STREAM,
    vehicles.STREAM,
    positions.STREAM,
    grant_events.STREAM,
    progression.STREAM,
    transfers.STREAM,
    read_models.STREAM,
    storage.STREAM,
    market.STREAM,
    world_events.STREAM,
]
