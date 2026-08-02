"""
Phase 2 - combat stream: dune.game_events harvest.

Harvests ALL game_events rows since a persisted cursor and writes them to
combat_events. event_type 0 is a death (victim + killer resolved); all other
types are stored opaque with raw custom_data, for future interpretation.

game_events is a rolling ~3-day window of <1000 rows with NO primary key and
NO index on universe_time, so `WHERE universe_time > :cursor` is a seq scan -
fine at this size. Do NOT add an index: that would be DDL against dune.* and
is forbidden.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

import db

log = logging.getLogger("telemetry.combat")

# Harvest ALL event_types, ordered so the cursor advances monotonically.
HARVEST_QUERY = """
SELECT actor_id, universe_time, map, partition_id, event_type, x, y, z, custom_data
FROM dune.game_events
WHERE universe_time > %(cursor)s
ORDER BY universe_time ASC
"""

# Victim / killer resolution. NOTE: deaths resolve the player via
# player_controller_id (this is correct for game_events). The vehicle stream
# uses player_pawn_id instead - do not confuse the two.
RESOLVE_QUERY = """
SELECT ps.account_id, ps.character_name
FROM dune.player_state ps
WHERE ps.player_controller_id = %(actor_id)s
"""

COLUMNS = [
    "dedup_key", "occurred_at", "occurred_epoch", "map", "partition_id",
    "event_type", "actor_id", "victim_account_id", "victim_name",
    "killer_type", "killer_account_id", "killer_name", "damage_type",
    "causer_row_index", "x", "y", "z", "raw", "harvested_at",
]

# A very-old ISO ts so the first sweep (no cursor) grabs the whole window.
_EPOCH_START = "1970-01-01T00:00:00+00:00"


def _to_iso(value):
    """Normalise a universe_time value (datetime or string) to ISO-8601 text."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _to_epoch(iso_text):
    """Best-effort epoch seconds from an ISO-8601 string."""
    try:
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def _custom_data_text(custom_data):
    """game_events.custom_data jsonb arrives as a dict (psycopg2) or text."""
    if custom_data is None:
        return ""
    if isinstance(custom_data, (dict, list)):
        # Sorted keys so md5 is stable regardless of dict ordering.
        return json.dumps(custom_data, sort_keys=True, separators=(",", ":"))
    return str(custom_data)


def _custom_data_dict(custom_data):
    if isinstance(custom_data, dict):
        return custom_data
    if isinstance(custom_data, str) and custom_data.strip():
        try:
            parsed = json.loads(custom_data)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _killer_controller_id(killer_player_id):
    """Parse m_KillerPlayerId '!!act#N'. '!!act#0' = no player killer -> None."""
    if not killer_player_id or "#" not in str(killer_player_id):
        return None
    try:
        n = int(str(killer_player_id).split("#")[1])
    except (ValueError, IndexError):
        return None
    return n if n != 0 else None


def run(ctx):
    cursor = db.get_cursor(ctx.store, "combat") or _EPOCH_START
    overlap = ctx.config.combat_cursor_overlap

    # Re-read the boundary on restart (overlap window); dedup absorbs repeats.
    query_cursor = cursor
    epoch_cursor = _to_epoch(cursor)
    if epoch_cursor > 0:
        query_cursor = datetime.fromtimestamp(
            max(0, epoch_cursor - overlap), tz=timezone.utc).isoformat()

    rows = ctx.gamedb.query(HARVEST_QUERY, {"cursor": query_cursor})
    harvested_at = int(time.time())

    # Per-sweep cache: controller_id -> (account_id, character_name).
    resolve_cache = {}

    def resolve(controller_id):
        if controller_id is None:
            return None, None
        if controller_id in resolve_cache:
            return resolve_cache[controller_id]
        found = ctx.gamedb.query(RESOLVE_QUERY, {"actor_id": controller_id})
        if found:
            result = (found[0].get("account_id"), found[0].get("character_name"))
        else:
            result = (None, None)
        resolve_cache[controller_id] = result
        return result

    out = []
    max_universe_time = None
    for row in rows:
        iso = _to_iso(row["universe_time"])
        if max_universe_time is None or iso > max_universe_time:
            max_universe_time = iso
        cd_text = _custom_data_text(row.get("custom_data"))
        event_type = row.get("event_type")
        dedup_key = "%s|%s|%s|%s" % (
            iso, row.get("actor_id"), event_type,
            hashlib.md5(cd_text.encode("utf-8")).hexdigest())

        victim_account_id = victim_name = None
        killer_type = killer_account_id = killer_name = None
        damage_type = causer_row_index = None

        if event_type == 0:
            victim_account_id, victim_name = resolve(row.get("actor_id"))
            cd = _custom_data_dict(row.get("custom_data"))
            killer_type = cd.get("m_KillerType")
            damage_type = cd.get("m_DamageType")
            causer_row_index = cd.get("m_CauserRowIndex")
            killer_cid = _killer_controller_id(cd.get("m_KillerPlayerId"))
            if killer_cid is not None:
                killer_account_id, killer_name = resolve(killer_cid)

        out.append((
            dedup_key, iso, _to_epoch(iso), row.get("map"),
            row.get("partition_id"), event_type, row.get("actor_id"),
            victim_account_id, victim_name, killer_type, killer_account_id,
            killer_name, damage_type, causer_row_index,
            row.get("x"), row.get("y"), row.get("z"), cd_text, harvested_at,
        ))

    written = db.insert_many_ignore(ctx.store, "combat_events", COLUMNS, out)

    # Advance the cursor to the max universe_time seen (not overlap-adjusted).
    # Leave the cursor unchanged on a zero-row sweep.
    if max_universe_time is not None:
        db.set_cursor(ctx.store, "combat", max_universe_time, harvested_at)

    log.info("combat: %d events harvested, %d new rows", len(rows), written)


STREAM = {"name": "combat", "interval_attr": "combat_interval", "run": run}
