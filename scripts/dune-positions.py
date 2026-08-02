#!/usr/bin/env python3
# Read-only online player positions for the Last Sietch relay. No writes.
# Deployed to <game-host>:/root/dune-positions.py — invoked by the relay over SSH.
#
# PII rule: coordinates only. Never select or emit character names or account ids.
# `m` (map name) and `p` (partition) are world ids, not identifiers of a person.
#
# 2026-07-27: was HARDCODED to HaggaBasin, which is why the admin dashboard could
# only ever draw the two Hagga instances. Measured at the time of the change:
# 5 players online across FOUR maps (Hagga 2, DeepDesert 1, PitDungeon 1,
# SandfliesFortress 1), so 3 of 5 were invisible to every consumer of this feed.
#
# Now emits every map and tags each player with `m`. Consumers that only want
# Hagga filter on it; `map` is retained at the top level as "HaggaBasin" for
# backward compatibility with the public site, which reads this shape today —
# changing that field would break a live page for a cosmetic win.
import json
import subprocess

POSITIONS_SQL = """
SELECT coalesce(json_agg(json_build_object(
         'x', x, 'y', y, 'p', partition_id, 'm', map, 'd', dim)), '[]'::json)
FROM (
  SELECT round((((a.transform).location).x)::numeric,0)::bigint AS x,
         round((((a.transform).location).y)::numeric,0)::bigint AS y,
         a.partition_id AS partition_id,
         a.map AS map,
         a.dimension_index AS dim
  FROM dune.actors a
  JOIN dune.player_state ps ON ps.player_pawn_id = a.id
  WHERE ps.online_status='Online'
) p;
"""


def fail(detail):
    print(json.dumps({"map": "HaggaBasin", "available": False,
                      "error": "db query failed", "detail": detail[:300]}))


def main():
    out = subprocess.run(["/root/dq.sh", "-tAc", POSITIONS_SQL],
                         capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        return fail((out.stderr or out.stdout).strip())
    try:
        players = json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        return fail(out.stdout.strip())

    # Per-map counts, so a consumer can show "3 elsewhere" without walking the
    # list, and so instanced sub-maps we draw no backdrop for (PitDungeon,
    # SandfliesFortress) are still visibly accounted for rather than silently
    # dropped.
    by_map = {}
    for pl in players:
        by_map[pl.get("m") or "?"] = by_map.get(pl.get("m") or "?", 0) + 1

    print(json.dumps({
        "map": "HaggaBasin",          # legacy top-level field; do not repurpose
        "count": len(players),
        "players": players,
        "by_map": by_map,
        "available": True,
    }))


if __name__ == "__main__":
    main()
