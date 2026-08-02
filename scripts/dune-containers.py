#!/usr/bin/env python3
# Read-only per-player container list for the Last Sietch admin v2 Player Tools tab
# (and, eventually, the public portal Account page). Deployed to
# lastsietch-dune:/root/dune-containers.py — invoked by the relay over SSH via the
# dispatcher's `containers-list <account_id>` token.
#
# Filters: world POIs are excluded by the JOIN on owner_entity_id (NULL =>
# unowned world POI, dropped by the inner JOIN). Holograms (is_hologram=true)
# are filtered explicitly. building_type is restricted to the 4-class storage
# whitelist from icehunter db.go:3395-3399.
#
# Owner-resolution chain (per spec lines 1486-1513, live schema 2026-05-24):
#   placeables.owner_entity_id -> actor_fgl_entities.entity_id
#   actor_fgl_entities.actor_id -> permission_actor_rank.permission_actor_id (rank=1)
#   permission_actor_rank.player_id -> encrypted_player_state.player_controller_id
#   encrypted_player_state.account_id = $account_id

import json
import subprocess
import sys

SQL_TEMPLATE = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'id', sub.id,
  'inv_id', sub.inv_id,
  'owner_ctrl', sub.owner_ctrl,
  'name', sub.name,
  'class', sub.class,
  'label', sub.label,
  'map', sub.map,
  'item_count', sub.item_count,
  'max_item_count', sub.max_item_count,
  'max_item_volume', sub.max_item_volume
) ORDER BY sub.id), '[]'::json)
FROM (
  SELECT p.id,
         -- The placeable's container `id` is p.id (the items drawer + V1 resolve a
         -- box by placeable id), but the WRITER gates on inventory ids. Emit the real
         -- inventory id alongside so a move can target it: without this the portal
         -- hands a placeable id to owned_inv_sql, never matches, and every box move
         -- is rejected not_owner. Bank/vehicle branches already key by inv.id.
         MAX(inv.id) AS inv_id,
         -- Rank-1 owner. This read is ACCOUNT-scoped, so a multi-character account
         -- lists every character's containers (and one bank row per character).
         -- Consumers that must act AS a character (the bank pick behind a write)
         -- filter on this; without it they take the first row and can act against
         -- the wrong character's inventory.
         MAX(par.player_id) AS owner_ctrl,
         COALESCE(MAX(CASE
             WHEN pa.actor_name NOT LIKE '##%' AND pa.actor_name <> 'None'
             THEN pa.actor_name
         END), '') AS name,
         p.building_type AS class,
         CASE p.building_type
           WHEN 'SpiceSilo_Placeable'            THEN 'Small Storage Container'
           WHEN 'GenericContainer_Placeable'     THEN 'Chest'
           WHEN 'StorageContainer_Placeable'     THEN 'Storage Container'
           WHEN 'MediumStorageContainer_Placeable' THEN 'Medium Storage Container'
           ELSE p.building_type
         END AS label,
         CASE
           WHEN a.map = 'DeepDesert' AND a.partition_id = 31 THEN 'Deep Desert (PvP)'
           WHEN a.map = 'DeepDesert'                         THEN 'Deep Desert (PvE)'
           WHEN a.map = 'HaggaBasin'                         THEN 'Hagga Basin'
           ELSE COALESCE(a.map, '')
         END AS map,
         COUNT(i.id) AS item_count,
         MAX(inv.max_item_count) AS max_item_count,
         MAX(inv.max_item_volume) AS max_item_volume
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe
         ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN dune.encrypted_player_state eps
         ON eps.player_controller_id = par.player_id
    LEFT JOIN dune.actors a            ON a.id = p.id
    LEFT JOIN dune.permission_actor pa ON pa.actor_id = p.id
    LEFT JOIN dune.inventories inv     ON inv.actor_id = p.id
    LEFT JOIN dune.items i             ON i.inventory_id = inv.id
    WHERE p.building_type = ANY(ARRAY[
            'SpiceSilo_Placeable',
            'GenericContainer_Placeable',
            'StorageContainer_Placeable',
            'MediumStorageContainer_Placeable']::text[])
      AND p.is_hologram = false
      AND eps.account_id = {account_id}::bigint
    GROUP BY p.id, p.building_type, a.map, a.partition_id

  UNION ALL

  -- CHOAM bank storage: a per-character inventory (inventory_type 30) owned by
  -- the player's PAWN, not a placeable, so it bypasses the placeable chain.
  -- Its container id = the bank inventory id (unique); the items drawer resolves
  -- the bank by inv.id (see dune-container-items.py). our internal notes.
  SELECT inv.id AS id,
         inv.id AS inv_id,   -- bank container id IS the inventory id; uniform field
         MAX(eps.player_controller_id) AS owner_ctrl,  -- the bank's OWNING character
         '' AS name,
         'CHOAMBank' AS class,
         'CHOAM Bank Storage' AS label,
         '' AS map,
         COUNT(i.id) AS item_count,
         MAX(inv.max_item_count) AS max_item_count,
         MAX(inv.max_item_volume) AS max_item_volume
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    LEFT JOIN dune.items i ON i.inventory_id = inv.id
    WHERE inv.inventory_type = 30
      AND eps.account_id = {account_id}::bigint
    GROUP BY inv.id

  UNION ALL

  -- Vehicle cargo: vehicles are actors owned directly via permission_actor_rank
  -- (actors.id = permission_actor_id, rank 1), NOT placeables. Cargo lives in the
  -- vehicle's inventory_type 0. Container id = that cargo inventory id (keyed by
  -- inv.id in the items drawer, like the bank). class carries the BP type so the
  -- portal can map a vehicle icon + friendly name + flag it as a vehicle.
  -- NOTE: Deep Desert vehicle cargo is RAM-resident on the DD pod and NOT in this
  -- DB, so a DD vehicle lists with item_count 0; the portal marks it unavailable.
  SELECT inv.id AS id,
         inv.id AS inv_id,   -- vehicle cargo container id IS the inventory id
         MAX(par.player_id) AS owner_ctrl,   -- rank-1 owner (see placeables branch)
         '' AS name,
         'Vehicle:' || split_part(a.class, '.', 2) AS class,
         'Vehicle' AS label,
         CASE
           WHEN a.map = 'DeepDesert' AND a.partition_id = 31 THEN 'Deep Desert (PvP)'
           WHEN a.map = 'DeepDesert'                         THEN 'Deep Desert (PvE)'
           WHEN a.map = 'HaggaBasin'                         THEN 'Hagga Basin'
           ELSE COALESCE(a.map, '')
         END AS map,
         COUNT(i.id) AS item_count,
         MAX(inv.max_item_count) AS max_item_count,
         MAX(inv.max_item_volume) AS max_item_volume
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    -- inventory_type 0 = the vehicle's cargo; max_item_count > 0 means a storage
    -- module is installed (buggy boot, orni storage, Regis spice container, ...).
    -- Without a module the cargo inv has 0 capacity, so we skip it.
    JOIN dune.inventories inv ON inv.actor_id = a.id
         AND inv.inventory_type = 0 AND inv.max_item_count > 0
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    LEFT JOIN dune.items i ON i.inventory_id = inv.id
    WHERE par.rank = 1
      AND eps.account_id = {account_id}::bigint
      AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
           OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
           OR a.class ILIKE '%containervehicle%')
    GROUP BY inv.id, a.class, a.map, a.partition_id

  UNION ALL

  -- Storage-less vehicles: owned vehicles that have INSTALLED PARTS (dune.vehicle_modules)
  -- but NO cargo module -- Scout/Carrier ornithopters, Sandbikes, bare crawlers. They
  -- never had an inventory_type-0 cargo inv so the branch above skips them and they were
  -- invisible in the browser. Surface them keyed by one of their (untyped) inventory ids
  -- so the parts panel + Refurbish Vehicle reach them (both resolve the vehicle from any
  -- inv id via inv.actor_id). max_item_count 0 flags "no storage" -> the portal renders
  -- the parts panel only, no cargo grid.
  SELECT (SELECT MIN(inv.id) FROM dune.inventories inv WHERE inv.actor_id = a.id) AS id,
         (SELECT MIN(inv.id) FROM dune.inventories inv WHERE inv.actor_id = a.id) AS inv_id,
         MAX(par.player_id) AS owner_ctrl,
         '' AS name,
         'Vehicle:' || split_part(a.class, '.', 2) AS class,
         'Vehicle' AS label,
         CASE
           WHEN a.map = 'DeepDesert' AND a.partition_id = 31 THEN 'Deep Desert (PvP)'
           WHEN a.map = 'DeepDesert'                         THEN 'Deep Desert (PvE)'
           WHEN a.map = 'HaggaBasin'                         THEN 'Hagga Basin'
           ELSE COALESCE(a.map, '')
         END AS map,
         0 AS item_count,
         0 AS max_item_count,
         0 AS max_item_volume
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    WHERE par.rank = 1
      AND eps.account_id = {account_id}::bigint
      AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
           OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
           OR a.class ILIKE '%containervehicle%')
      AND EXISTS (SELECT 1 FROM dune.vehicle_modules vm WHERE vm.vehicle_id = a.id)
      AND EXISTS (SELECT 1 FROM dune.inventories inv WHERE inv.actor_id = a.id)
      AND NOT EXISTS (SELECT 1 FROM dune.inventories inv
                       WHERE inv.actor_id = a.id
                         AND inv.inventory_type = 0 AND inv.max_item_count > 0)
    GROUP BY a.id, a.class, a.map, a.partition_id
) sub;
"""


def build(account_id, qjson):
    """Shared builder: return the containers payload for one account, using an
    injected `qjson(sql, fallback)` runner. The collector (psycopg2) and main()
    (dq.sh) both call this so the relay path and the mirror are byte-identical.
    See docs/dune-research/PHASE-2-MIRROR-CONTRACT-2026-06-05.md."""
    sql = SQL_TEMPLATE.format(account_id=int(account_id))
    containers = qjson(sql, "[]") or []
    return {"available": True, "account_id": str(account_id),
            "count": len(containers), "containers": containers}


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"available": False,
                          "error": "usage: dune-containers.py <account_id>"}))
        sys.exit(2)

    account_id = sys.argv[1]
    if not account_id.isdigit():
        print(json.dumps({"available": False,
                          "error": "account_id must be digits"}))
        sys.exit(2)

    # account_id has passed the isdigit() allowlist — safe to embed directly.
    sql = SQL_TEMPLATE.format(account_id=account_id)

    try:
        out = subprocess.run(
            ["/root/dq.sh", "-tAc", sql],
            capture_output=True, text=True, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        print(json.dumps({"available": False, "error": "timeout"}))
        sys.exit(1)

    if out.returncode != 0:
        print(json.dumps({"available": False,
                          "error": (out.stderr or out.stdout).strip()[:500]}))
        sys.exit(1)

    # psql emits "SET" on its own line before the JSON result; pick the last
    # non-empty line which is the JSON array (or fallback to '[]').
    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    if not raw:
        raw = "[]"
    try:
        containers = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"available": False, "error": f"parse: {e}",
                          "raw": raw[:500]}))
        sys.exit(1)

    print(json.dumps({"available": True, "account_id": account_id,
                      "count": len(containers), "containers": containers}))


if __name__ == "__main__":
    main()
