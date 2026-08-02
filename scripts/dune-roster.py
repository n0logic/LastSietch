#!/usr/bin/env python3
# Read-only per-map online player roster for the Last Sietch relay. No writes.
# Deployed to lastsietch-dune:/root/dune-roster.py — invoked by the relay over SSH.
#
# PII rule: this DOES emit character names. The relay endpoint that proxies
# this output is auth-gated for that reason — never expose it on /api/dune/*
# public surfaces.
import json
import subprocess

# player_state is a VIEW that decrypts character_name. Join the pawn actor via
# player_pawn_id for map + transform; faction/guild are LEFT JOINs so a player
# with no faction or guild still appears (null fields).
ROSTER_SQL = """
SELECT coalesce(json_agg(
         json_build_object('map', map, 'players', players) ORDER BY map),
       '[]'::json)
FROM (
  SELECT a.map AS map,
         json_agg(json_build_object(
           'name', ps.character_name,
           'x', round((((a.transform).location).x)::numeric,0)::bigint,
           'y', round((((a.transform).location).y)::numeric,0)::bigint,
           'guild', g.guild_name,
           'faction', f.name
         ) ORDER BY ps.character_name) AS players
  FROM dune.player_state ps
  JOIN dune.actors a ON a.id = ps.player_pawn_id
  LEFT JOIN dune.player_faction pf ON pf.actor_id = ps.player_pawn_id
  LEFT JOIN dune.factions f ON f.id = pf.faction_id
  LEFT JOIN dune.guild_members gm ON gm.player_id = ps.player_pawn_id
  LEFT JOIN dune.guilds g ON g.guild_id = gm.guild_id
  WHERE ps.online_status='Online'
  GROUP BY a.map
) m;
"""


def main():
    out = subprocess.run(["/root/dq.sh", "-tAc", ROSTER_SQL],
                         capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        print(json.dumps({"available": False, "error": "db query failed",
                          "detail": (out.stderr or out.stdout).strip()[:300]}))
        return
    try:
        maps = json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        print(json.dumps({"available": False, "error": "db returned non-JSON",
                          "detail": out.stdout.strip()[:300]}))
        return

    print(json.dumps({"available": True, "maps": maps}))


if __name__ == "__main__":
    main()
