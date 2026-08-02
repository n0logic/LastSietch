#!/usr/bin/env python3
# Read-only per-player blueprint list for the Last Sietch admin v2 Player Tools
# "Export" subtab (G30 v1 Solido Blueprint Export, source A).
#
# Deployed to <game-host>:/root/dune-blueprints-list.py — invoked by the relay
# over SSH via the dispatcher's `blueprints-list <account_id>` token.
#
# Lists every BuildingBlueprint_CopyDevice item owned by the player wherever
# it lives, joined to its dune.building_blueprints row. Each row carries the
# piece count summed across instances + placeables + pentashields.
#
# Ownership = union of every inventory the account owns (2026-06-11 fix:
# the original pawn-only chain missed devices parked in base storage boxes
# and vehicle cargo — chains mirror dune-containers.py):
#   pawn-anchored:    inv.actor_id = eps.player_pawn_id  (backpack 0,
#                     blueprint storage 15, CHOAM bank 30)
#   placed storage:   placeables -> actor_fgl_entities -> permission_actor_rank
#                     rank 1 -> eps.player_controller_id
#   vehicle cargo:    actors -> permission_actor_rank rank 1 -> controller

import json
import subprocess
import sys

SQL_TEMPLATE = """
SET search_path TO dune, public;
WITH eps AS (
  SELECT player_pawn_id, player_controller_id
    FROM dune.encrypted_player_state
   WHERE account_id = {account_id}::bigint
),
owned_inv AS (
  -- pawn-anchored: backpack (0), blueprint storage (15), CHOAM bank (30)
  SELECT inv.id,
         CASE inv.inventory_type
           WHEN 0  THEN 'backpack'
           WHEN 15 THEN 'blueprint-storage'
           WHEN 30 THEN 'bank'
           ELSE 'other (#' || inv.inventory_type::text || ')'
         END AS loc
    FROM dune.inventories inv
    JOIN eps ON inv.actor_id = eps.player_pawn_id
  UNION
  -- placed storage/placeable inventories in owned bases
  SELECT inv.id, 'storage'
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN eps ON par.player_id = eps.player_controller_id
    JOIN dune.inventories inv ON inv.actor_id = p.id
   WHERE p.is_hologram = false
  UNION
  -- vehicle cargo (actor-anchored, rank-1 owned)
  SELECT inv.id, 'vehicle'
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN eps ON par.player_id = eps.player_controller_id
    JOIN dune.inventories inv ON inv.actor_id = a.id
   WHERE par.rank = 1
)
SELECT coalesce(json_agg(json_build_object(
  'bp_id',             sub.bp_id,
  'item_id',           sub.item_id,
  'inventory_type',    sub.inventory_type,
  'inventory_location',sub.inventory_location,
  'name',              sub.name,
  'instance_count',    sub.instance_count,
  'placeable_count',   sub.placeable_count,
  'pentashield_count', sub.pentashield_count,
  'piece_count',       sub.instance_count + sub.placeable_count + sub.pentashield_count
) ORDER BY sub.bp_id), '[]'::json)
FROM (
  SELECT DISTINCT ON (bp.id)
         bp.id  AS bp_id,
         bp.item_id,
         inv.inventory_type,
         oi.loc AS inventory_location,
         -- Player-set name from the BlueprintItemStats JSONB on the parent
         -- item. Funcom seeds it as "New Build" and the player can rename
         -- in-game. Fall back to "Blueprint #N" if the field is missing
         -- (older items pre-name-feature, or schema drift). The {{...}}
         -- escapes Python str.format(); the SQL literal that lands at the
         -- DB is a single-braced text[] path.
         COALESCE(
           NULLIF(i.stats #>> '{{FBuildingBlueprintItemStats,1,BuildingBlueprintName}}', ''),
           'Blueprint #' || bp.id::text
         ) AS name,
         (SELECT COUNT(*) FROM dune.building_blueprint_instances
            WHERE building_blueprint_id = bp.id)  AS instance_count,
         (SELECT COUNT(*) FROM dune.building_blueprint_placeables
            WHERE building_blueprint_id = bp.id)  AS placeable_count,
         (SELECT COUNT(*) FROM dune.building_blueprint_pentashields
            WHERE building_blueprint_id = bp.id)  AS pentashield_count
    FROM dune.building_blueprints bp
    JOIN dune.items i             ON i.id = bp.item_id
    JOIN dune.inventories inv     ON inv.id = i.inventory_id
    JOIN owned_inv oi             ON oi.id = inv.id
   WHERE i.template_id = 'BuildingBlueprint_CopyDevice'
   ORDER BY bp.id
) sub;
"""


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"available": False,
                          "error": "usage: dune-blueprints-list.py <account_id>"}))
        sys.exit(2)

    account_id = sys.argv[1]
    if not account_id.isdigit():
        print(json.dumps({"available": False,
                          "error": "account_id must be digits"}))
        sys.exit(2)

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
        blueprints = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"available": False, "error": f"parse: {e}",
                          "raw": raw[:500]}))
        sys.exit(1)

    print(json.dumps({"available": True, "account_id": account_id,
                      "count": len(blueprints), "blueprints": blueprints}))


if __name__ == "__main__":
    main()
