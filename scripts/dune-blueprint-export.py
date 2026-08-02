#!/usr/bin/env python3
# G30 v1 Solido Blueprint Export — single-blueprint Solido-market JSON dump.
#
# Verbatim Python port of icehunter dune-admin's cmdExportBlueprint
# (Go, db.go:2917-3032 at upstream commit MIT v0.10.0). icehunter is the
# author of Solido Market; his JSON schema IS the canonical schema. Port
# follows Q5d resolution 2026-05-26 ("copy icehunter, he made Solido"):
# no version field, no Last Sietch extensions. Output is byte-symmetric with what
# Solido's import flow expects, and round-trippable through G20
# import_blueprint.
#
# Original license: MIT (icehunter v0.10.0). Ported with attribution per
# V2-ADMIN-CONTINUATION-PLAN-2026-05-26.md §F Q1 ("MIT port with attribution").
#
# Deployed to <game-host>:/root/dune-blueprint-export.py — invoked by the relay
# over SSH via dispatcher's `blueprint-export <account_id> <bp_id>` token.
#
# Read-only. Ownership is verified server-side against the union of every
# inventory the account owns (2026-06-11 fix, matches dune-blueprints-list.py:
# pawn-anchored backpack/blueprint-storage/bank + placed base storage +
# vehicle cargo — the original pawn-only chain rejected devices parked in
# storage boxes). A bp_id whose item is NOT in an owned inventory returns
# `{available: false, error: "not_owned"}` so a fat-fingered URL across two
# players can't leak a blueprint into the wrong export.

import json
import subprocess
import sys

# Step 1: verify the blueprint belongs to the account + pull the player-set
# blueprint name (lives in items.stats.FBuildingBlueprintItemStats[1].
# BuildingBlueprintName; Funcom default is "New Build"). owned=true iff
# ownership matches; name=null if absent in stats JSONB.
OWNERSHIP_SQL = """
SET search_path TO dune, public;
WITH eps AS (
  SELECT player_pawn_id, player_controller_id
    FROM dune.encrypted_player_state
   WHERE account_id = {account_id}::bigint
),
owned_inv AS (
  SELECT inv.id
    FROM dune.inventories inv
    JOIN eps ON inv.actor_id = eps.player_pawn_id
  UNION
  SELECT inv.id
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN eps ON par.player_id = eps.player_controller_id
    JOIN dune.inventories inv ON inv.actor_id = p.id
   WHERE p.is_hologram = false
  UNION
  SELECT inv.id
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN eps ON par.player_id = eps.player_controller_id
    JOIN dune.inventories inv ON inv.actor_id = a.id
   WHERE par.rank = 1
)
SELECT coalesce(json_build_object(
  'bp_id',   bp.id,
  'item_id', bp.item_id,
  'name',    NULLIF(i.stats #>> '{{FBuildingBlueprintItemStats,1,BuildingBlueprintName}}', ''),
  'owned',   true
), '{{"owned":false}}'::json)
FROM dune.building_blueprints bp
JOIN dune.items i             ON i.id = bp.item_id
                              AND i.template_id = 'BuildingBlueprint_CopyDevice'
JOIN dune.inventories inv     ON inv.id = i.inventory_id
JOIN owned_inv oi             ON oi.id = inv.id
WHERE bp.id = {bp_id}::bigint;
"""

# Step 2: instances. transform = real[] of len >=4 [x, y, z, rotation].
# Mirrors icehunter cmdExportBlueprint:2924-2955. NOTE: the array LOWER BOUND
# varies by writer — in-game saves store 1-based arrays, but blueprints written
# by the Go import path store 0-BASED arrays ('[0:3]={...}'). Fixed [1]..[4]
# indexing silently shifts those one slot (height dropped, yaw read as z,
# rotation NULL → the 2026-06-12 "flattened base" bug). Index relative to
# array_lower() so both layouts read correctly. (The pre-2026-06-11 corruption
# was the opposite mistake: 0-based indexing against 1-based arrays.)
INSTANCES_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'building_type', bi.building_type,
  'x',             bi.transform[array_lower(bi.transform, 1)],
  'y',             bi.transform[array_lower(bi.transform, 1) + 1],
  'z',             bi.transform[array_lower(bi.transform, 1) + 2],
  'rotation',      bi.transform[array_lower(bi.transform, 1) + 3]
) ORDER BY bi.instance_id), '[]'::json)
FROM dune.building_blueprint_instances bi
WHERE bi.building_blueprint_id = {bp_id}::bigint
  AND array_length(bi.transform, 1) >= 4;
"""

# Step 3: placeables. transform = real[] of len >=6 [x, y, z, rx, ry, rz].
# Mirrors icehunter cmdExportBlueprint:2957-2987. Same lower-bound rule as
# instances.
PLACEABLES_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'building_type', bp.building_type,
  'x',             bp.transform[array_lower(bp.transform, 1)],
  'y',             bp.transform[array_lower(bp.transform, 1) + 1],
  'z',             bp.transform[array_lower(bp.transform, 1) + 2],
  'rx',            bp.transform[array_lower(bp.transform, 1) + 3],
  'ry',            bp.transform[array_lower(bp.transform, 1) + 4],
  'rz',            bp.transform[array_lower(bp.transform, 1) + 5]
) ORDER BY bp.placeable_id), '[]'::json)
FROM dune.building_blueprint_placeables bp
WHERE bp.building_blueprint_id = {bp_id}::bigint
  AND array_length(bp.transform, 1) >= 6;
"""

# Step 4: pentashields. scale = smallint[3]. Mirrors icehunter
# cmdExportBlueprint:2992-3020.
PENTASHIELDS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'placeable_id', ps.placeable_id,
  'scale',        json_build_array(ps.scale[array_lower(ps.scale, 1)],
                                   ps.scale[array_lower(ps.scale, 1) + 1],
                                   ps.scale[array_lower(ps.scale, 1) + 2])
) ORDER BY ps.placeable_id), '[]'::json)
FROM dune.building_blueprint_pentashields ps
WHERE ps.building_blueprint_id = {bp_id}::bigint
  AND array_length(ps.scale, 1) >= 3;
"""


def _query_json(sql, fallback="[]"):
    """Run psql, strip 'SET' header, return parsed JSON (or fallback)."""
    out = subprocess.run(
        ["/root/dq.sh", "-tAc", sql],
        capture_output=True, text=True, timeout=45, check=False)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:500])
    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    if not raw:
        raw = fallback
    return json.loads(raw)


def main():
    if len(sys.argv) != 3:
        print(json.dumps({"available": False,
                          "error": "usage: dune-blueprint-export.py <account_id> <bp_id>"}))
        sys.exit(2)

    account_id, bp_id = sys.argv[1], sys.argv[2]
    if not account_id.isdigit() or not bp_id.isdigit():
        print(json.dumps({"available": False,
                          "error": "account_id and bp_id must be positive integers"}))
        sys.exit(2)

    try:
        # Step 1: ownership gate.
        owner_row = _query_json(
            OWNERSHIP_SQL.format(bp_id=bp_id, account_id=account_id),
            fallback='{"owned":false}')
        if not (owner_row.get("owned") and str(owner_row.get("bp_id")) == bp_id):
            print(json.dumps({"available": False, "error": "not_owned"}))
            sys.exit(0)

        # Steps 2-4: the three building_blueprint_* table dumps.
        instances    = _query_json(INSTANCES_SQL.format(bp_id=bp_id))
        placeables   = _query_json(PLACEABLES_SQL.format(bp_id=bp_id))
        pentashields = _query_json(PENTASHIELDS_SQL.format(bp_id=bp_id))
    except subprocess.TimeoutExpired:
        print(json.dumps({"available": False, "error": "timeout"}))
        sys.exit(1)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(json.dumps({"available": False, "error": str(e)[:500]}))
        sys.exit(1)

    # icehunter blueprintFile struct (db.go:2863-2868):
    #   name?        — optional; round-trip the in-game BuildingBlueprintName
    #                  when present so Solido shows the player's chosen title
    #   instances    — required
    #   placeables   — required
    #   pentashields — optional (omitempty), we still include for fidelity
    bp_name = owner_row.get("name")
    blueprint = {"instances": instances, "placeables": placeables,
                 "pentashields": pentashields}
    if bp_name:
        # Preserve icehunter's struct field order: name first.
        blueprint = {"name": bp_name, **blueprint}

    print(json.dumps({"available": True,
                      "account_id":  account_id,
                      "bp_id":       int(bp_id),
                      "item_id":     owner_row.get("item_id"),
                      "name":        bp_name,
                      "blueprint":   blueprint}))


if __name__ == "__main__":
    main()
