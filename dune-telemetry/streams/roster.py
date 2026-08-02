"""
Phase 1 - roster stream.

Per-sweep online roster: account, character name, map, guild, faction. Adapted
from the relay's dune-roster.py join into flat rows.

PII: character_name is real player data. The read API gates /roster behind
localhost-only exposure; the relay must auth-gate any proxy of it.
"""
from __future__ import annotations

import logging
import time

import db

log = logging.getLogger("telemetry.roster")

QUERY = """
SELECT ps.account_id, ps.character_name, a.map,
       g.guild_name AS guild, f.name AS faction
FROM dune.player_state ps
JOIN dune.actors a ON a.id = ps.player_pawn_id
LEFT JOIN dune.player_faction pf ON pf.actor_id = ps.player_pawn_id
LEFT JOIN dune.factions f ON f.id = pf.faction_id
LEFT JOIN dune.guild_members gm ON gm.player_id = ps.player_pawn_id
LEFT JOIN dune.guilds g ON g.guild_id = gm.guild_id
WHERE ps.online_status='Online'
"""

COLUMNS = ["ts", "account_id", "character_name", "map", "guild", "faction"]


def run(ctx):
    ts = int(time.time())
    rows = ctx.gamedb.query(QUERY)
    out = [
        (ts, r.get("account_id"), r.get("character_name"),
         r.get("map"), r.get("guild"), r.get("faction"))
        for r in rows
    ]
    written = db.insert_many(ctx.store, "roster_snapshot", COLUMNS, out)
    log.info("roster: %d online, %d rows written", len(rows), written)


STREAM = {"name": "roster", "interval_attr": "roster_interval", "run": run}
