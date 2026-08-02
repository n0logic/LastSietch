#!/usr/bin/env python3
# Read-only "where is my stuff" index for one account: every item across all of
# the account's storage containers, aggregated by (container, template_id) with
# summed quantity. The portal turns this into a cross-container item search
# ("Granite Stone -> which boxes, how many in each").
#
# Deployed to lastsietch-dune:/root/dune-container-search.py — invoked by the relay
# over SSH via the dispatcher's `container-search <account_id>` token.
#
# Friendly-name matching is done in admin-backend (name_lookups), so this only
# returns raw template_ids + quantities; one DB round-trip for the whole search.
# Owner-resolution + storage whitelist mirror dune-containers.py exactly.

import json
import subprocess
import sys

SQL_TEMPLATE = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'container_id', sub.cid,
  'container_name', sub.cname,
  'container_type', sub.ctype,
  'map', sub.map,
  'template_id', sub.template_id,
  'qty', sub.qty,
  'lines', sub.lines
) ORDER BY sub.template_id, sub.cid), '[]'::json)
FROM (
  SELECT p.id AS cid,
         COALESCE(MAX(CASE
             WHEN pa.actor_name NOT LIKE '##%' AND pa.actor_name <> 'None'
             THEN pa.actor_name
         END), '') AS cname,
         CASE p.building_type
           WHEN 'SpiceSilo_Placeable'              THEN 'Small Storage Container'
           WHEN 'GenericContainer_Placeable'       THEN 'Chest'
           WHEN 'StorageContainer_Placeable'       THEN 'Storage Container'
           WHEN 'MediumStorageContainer_Placeable' THEN 'Medium Storage Container'
           ELSE p.building_type
         END AS ctype,
         COALESCE(a.map, '') AS map,
         i.template_id,
         SUM(COALESCE(i.stack_size, 1)) AS qty,
         COUNT(i.id) AS lines
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe
         ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN dune.encrypted_player_state eps
         ON eps.player_controller_id = par.player_id
    LEFT JOIN dune.actors a            ON a.id = p.id
    LEFT JOIN dune.permission_actor pa ON pa.actor_id = p.id
    JOIN dune.inventories inv          ON inv.actor_id = p.id
    JOIN dune.items i                  ON i.inventory_id = inv.id
    WHERE p.building_type = ANY(ARRAY[
            'SpiceSilo_Placeable',
            'GenericContainer_Placeable',
            'StorageContainer_Placeable',
            'MediumStorageContainer_Placeable']::text[])
      AND p.is_hologram = false
      AND eps.account_id = {account_id}::bigint
      AND i.template_id IS NOT NULL
    GROUP BY p.id, p.building_type, a.map, i.template_id

  UNION ALL

  -- CHOAM bank storage (inventory_type 30 on the player's pawn). Container id =
  -- the bank inventory id, matching dune-containers.py. our internal notes.
  SELECT inv.id AS cid,
         '' AS cname,
         'CHOAM Bank Storage' AS ctype,
         '' AS map,
         i.template_id,
         SUM(COALESCE(i.stack_size, 1)) AS qty,
         COUNT(i.id) AS lines
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    JOIN dune.items i ON i.inventory_id = inv.id
    WHERE inv.inventory_type = 30
      AND eps.account_id = {account_id}::bigint
      AND i.template_id IS NOT NULL
    GROUP BY inv.id, i.template_id

  UNION ALL

  -- Vehicle cargo (inventory_type 0 on an owned vehicle actor). DD vehicle cargo
  -- is RAM-resident (not in this DB) so only Hagga vehicles contribute rows.
  SELECT inv.id AS cid,
         '' AS cname,
         'Vehicle:' || split_part(a.class, '.', 2) AS ctype,
         COALESCE(a.map, '') AS map,
         i.template_id,
         SUM(COALESCE(i.stack_size, 1)) AS qty,
         COUNT(i.id) AS lines
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN dune.inventories inv ON inv.actor_id = a.id AND inv.inventory_type = 0 AND inv.max_item_count > 0
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    JOIN dune.items i ON i.inventory_id = inv.id
    WHERE par.rank = 1
      AND eps.account_id = {account_id}::bigint
      AND i.template_id IS NOT NULL
      AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
           OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
           OR a.class ILIKE '%containervehicle%')
    GROUP BY inv.id, a.class, a.map, i.template_id
) sub;
"""


def build(account_id, qjson):
    """Shared builder: per-(container, template_id) item rollup for one account,
    via an injected `qjson(sql, fallback)` runner (collector psycopg2 + main()
    dq.sh share the SQL so the relay path and mirror match).
    See docs/dune-research/PHASE-2-MIRROR-CONTRACT-2026-06-05.md."""
    sql = SQL_TEMPLATE.format(account_id=int(account_id))
    rows = qjson(sql, "[]") or []
    return {"available": True, "account_id": str(account_id),
            "count": len(rows), "rows": rows}


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"available": False,
                          "error": "usage: dune-container-search.py <account_id>"}))
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

    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    if not raw:
        raw = "[]"
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"available": False, "error": f"parse: {e}",
                          "raw": raw[:500]}))
        sys.exit(1)

    print(json.dumps({"available": True, "account_id": account_id,
                      "count": len(rows), "rows": rows}))


if __name__ == "__main__":
    main()
