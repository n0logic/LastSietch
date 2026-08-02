#!/usr/bin/env python3
# Read-only world-vehicle positions for the Last Sietch relay. No writes.
# Deployed to lastsietch-dune:/root/dune-vehicles.py — invoked by the relay over SSH.
#
# PII rule: coordinates + vehicle TYPE only. Never owners/names/account ids, so
# this feed is safe for the PUBLIC map (vehicle icons, no identity). `m` (map
# name) and `p` (partition) are world ids, not identifiers of a person.
#
# 2026-07-27: was HARDCODED to HaggaBasin, the same bug dune-positions.py had —
# every consumer was blind to vehicles anywhere else. Now emits every map and
# tags each row with `m` + `d`.
#
# ⚠️ CONSUMERS MUST FILTER ON `m`. Before this change, three consumers happened
# to stay correct only because Hagga's partitions (1, 32) do not collide with
# Deep Desert's (8, 31): the public site buckets strictly on partition and drops
# unknown ones, and portal-nextgen's partMatch does the same. That is luck, not
# design — a hub map whose partition IS 1 or 32 would have leaked its vehicles
# onto the public Hagga board. All three now match on `m` first. `map` is kept
# at the top level as "HaggaBasin" for backward compatibility with the public
# site, which reads that field; changing it would break a live page.
import json
import subprocess

VEHICLES_SQL = """
SELECT coalesce(json_agg(json_build_object(
         'x', x, 'y', y, 'p', partition_id, 't', vtype, 'm', map, 'd', dim)), '[]'::json)
FROM (
  SELECT round((((a.transform).location).x)::numeric,0)::bigint AS x,
         round((((a.transform).location).y)::numeric,0)::bigint AS y,
         a.partition_id AS partition_id,
         a.map AS map,
         a.dimension_index AS dim,
         CASE
           WHEN a.class ILIKE '%Ornithopter%' THEN 'ornithopter'
           WHEN a.class ILIKE '%Sandbike%'    THEN 'sandbike'
           WHEN a.class ILIKE '%Buggy%'       THEN 'buggy'
           WHEN a.class ILIKE '%SandCrawler%' THEN 'sandcrawler'
           WHEN a.class ILIKE '%ContainerVehicle%' THEN 'container'
           ELSE 'vehicle'
         END AS vtype
  FROM dune.actors a
  WHERE a.class ILIKE '%/Vehicles/%'
    AND a.class NOT ILIKE '%Fabricator%'
    AND a.class NOT ILIKE '%Dismantled%'
    AND a.class NOT ILIKE '%Placeable%'
) v;
"""


def main():
    out = subprocess.run(["/root/dq.sh", "-tAc", VEHICLES_SQL],
                         capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        print(json.dumps({"map": "HaggaBasin", "available": False,
                          "error": "db query failed",
                          "detail": (out.stderr or out.stdout).strip()[:300]}))
        return
    try:
        vehicles = json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        print(json.dumps({"map": "HaggaBasin", "available": False,
                          "error": "db returned non-JSON",
                          "detail": out.stdout.strip()[:300]}))
        return

    by_map = {}
    for v in vehicles:
        by_map[v.get("m") or "?"] = by_map.get(v.get("m") or "?", 0) + 1

    print(json.dumps({
        "map": "HaggaBasin",          # legacy top-level field; do not repurpose
        "count": len(vehicles),
        "vehicles": vehicles,
        "by_map": by_map,
        "available": True,
    }))


if __name__ == "__main__":
    main()
