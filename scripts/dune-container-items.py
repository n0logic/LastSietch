#!/usr/bin/env python3
# Read-only per-container items list for the Last Sietch admin v2 Player Tools tab
# container drill-down (LIFT-10). Deployed to lastsietch-dune:/root/dune-container-items.py
# — invoked over SSH by the lastsietch-relay dispatcher via the
# `container-items <account_id> <container_id> [page]` token.
#
# Pattern observed in icehunter db.go:3476-3506 (no LICENSE => clean-room;
# re-implemented from scratch).
#
# Ownership verification is pushed down to the SQL: the JOIN chain proves the
# (account_id, container_id) pair before returning items. Zero rows => owner
# check failed AND/OR container is empty. The available/error flag in the
# emitted JSON disambiguates: a deliberate EXISTS pre-check sets
# `available=false, error="not_owned"` when the FastAPI route should map to 404.

import json
import subprocess
import sys

PAGE_SIZE = 100

# Ownership check (placeable) — same owner-resolution chain as dune-containers.py.
OWNS_SQL = """
SET search_path TO dune, public;
SELECT EXISTS (
  SELECT 1
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe
         ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN dune.encrypted_player_state eps
         ON eps.player_controller_id = par.player_id
    WHERE p.id = {container_id}::bigint
      AND eps.account_id = {account_id}::bigint
      -- MUST be an actual storage container, and MUST own an inventory.
      -- Container ids live in TWO OVERLAPPING namespaces (placeable id for a box,
      -- inventory id for the bank/vehicle -- see
      -- our internal notes). This check runs FIRST,
      -- so without these guards ANY owned placeable whose id collides with a bank or
      -- vehicle INVENTORY id hijacks the lookup, match flips to inv.actor_id, and the
      -- real container reads back EMPTY. Live 2026-07-16: acct 1644's bank is
      -- inventory 2914, and placeable 2914 is `Atre_WallArt_01_Placeable` (a wall
      -- decoration with no inventory at all) -- so the bank grid rendered empty.
      AND p.is_hologram = false
      AND p.building_type = ANY(ARRAY[
            'SpiceSilo_Placeable',
            'GenericContainer_Placeable',
            'StorageContainer_Placeable',
            'MediumStorageContainer_Placeable']::text[])
      AND EXISTS (SELECT 1 FROM dune.inventories i2 WHERE i2.actor_id = p.id)
) AS owns;
"""

# Ownership check (inventory-keyed) — covers the two stores that are NOT
# placeables and whose container id IS the inventory's own id (resolved by
# inv.id, not actor id). Tried only when the placeable check fails:
#   1. CHOAM bank   : inventory_type 30 on the account's PAWN.
#   2. Vehicle cargo: inventory_type 0 on a vehicle actor the account owns at
#      rank 1 (permission_actor_rank.permission_actor_id = vehicle actor id).
# our internal notes / vehicle ownership cracked 2026-06-04.
OWNS_BANK_SQL = """
SET search_path TO dune, public;
SELECT EXISTS (
  SELECT 1
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    WHERE inv.id = {container_id}::bigint
      AND inv.inventory_type = 30
      AND eps.account_id = {account_id}::bigint
  UNION ALL
  SELECT 1
    FROM dune.inventories inv
    JOIN dune.permission_actor_rank par ON par.permission_actor_id = inv.actor_id AND par.rank = 1
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    JOIN dune.actors a ON a.id = inv.actor_id
    WHERE inv.id = {container_id}::bigint
      AND inv.inventory_type = 0
      AND inv.max_item_count > 0
      AND eps.account_id = {account_id}::bigint
      AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
           OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
           OR a.class ILIKE '%containervehicle%')
) AS owns;
"""

# Items list — JSONB digs for durability are best-effort; missing returns ''.
# {match} is the join column: 'inv.actor_id' for a placeable container, 'inv.id'
# for the CHOAM bank (keyed by the bank inventory's own id).
ITEMS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'id', sub.id,
  'template_id', sub.template_id,
  'stack_size', sub.stack_size,
  'quality', sub.quality_level,
  'position_index', sub.position_index,
  'cur_dur', sub.cur_dur,
  'max_dur', sub.max_dur
) ORDER BY sub.template_id, sub.id), '[]'::json)
FROM (
  SELECT i.id,
         i.template_id,
         i.stack_size,
         i.quality_level,
         i.position_index,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability', '')    AS cur_dur,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability', '') AS max_dur
    FROM dune.items i
    JOIN dune.inventories inv ON inv.id = i.inventory_id
    WHERE {match} = {container_id}::bigint
    ORDER BY i.template_id, i.id
    LIMIT {limit} OFFSET {offset}
) sub;
"""

COUNT_SQL = """
SET search_path TO dune, public;
SELECT COUNT(*)::int AS n
  FROM dune.items i
  JOIN dune.inventories inv ON inv.id = i.inventory_id
  WHERE {match} = {container_id}::bigint;
"""

# Collector-only: ALL items across ALL of an account's containers in one query,
# each tagged with the SAME container_id the container LIST assigns (placeable =
# p.id, bank/vehicle = inv.id). The per-item projection + ordering are IDENTICAL
# to ITEMS_SQL so the mirror can paginate locally and reproduce the live pages
# byte-for-byte. Restricted to the same storage whitelist as the container list,
# so only list-visible (clickable) containers get buckets.
#
ALL_ITEMS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'container_id', sub.cid,
  'id', sub.id,
  'template_id', sub.template_id,
  'stack_size', sub.stack_size,
  'quality', sub.quality_level,
  'position_index', sub.position_index,
  'cur_dur', sub.cur_dur,
  'max_dur', sub.max_dur
) ORDER BY sub.cid, sub.template_id, sub.id), '[]'::json)
FROM (
  -- Placeable containers: container_id = p.id (= inv.actor_id).
  SELECT p.id AS cid, i.id, i.template_id, i.stack_size, i.quality_level,
         i.position_index,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability', '')    AS cur_dur,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability', '') AS max_dur
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    JOIN dune.inventories inv ON inv.actor_id = p.id
    JOIN dune.items i ON i.inventory_id = inv.id
    WHERE p.building_type = ANY(ARRAY[
            'SpiceSilo_Placeable',
            'GenericContainer_Placeable',
            'StorageContainer_Placeable',
            'MediumStorageContainer_Placeable']::text[])
      AND p.is_hologram = false
      AND eps.account_id = {account_id}::bigint

  UNION ALL

  -- CHOAM bank (inventory_type 30 on the pawn): container_id = inv.id.
  SELECT inv.id AS cid, i.id, i.template_id, i.stack_size, i.quality_level,
         i.position_index,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability', '')    AS cur_dur,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability', '') AS max_dur
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    JOIN dune.items i ON i.inventory_id = inv.id
    WHERE inv.inventory_type = 30
      AND eps.account_id = {account_id}::bigint

  UNION ALL

  -- Vehicle cargo (inventory_type 0, storage module installed): container_id = inv.id.
  SELECT inv.id AS cid, i.id, i.template_id, i.stack_size, i.quality_level,
         i.position_index,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability', '')    AS cur_dur,
         COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability', '') AS max_dur
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN dune.inventories inv ON inv.actor_id = a.id AND inv.inventory_type = 0 AND inv.max_item_count > 0
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    JOIN dune.items i ON i.inventory_id = inv.id
    WHERE par.rank = 1
      AND eps.account_id = {account_id}::bigint
      AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
           OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
           OR a.class ILIKE '%containervehicle%')
) sub;
"""


def build_all(account_id, qjson):
    """Collector-only: items bucketed by container_id for one account (ALL pages,
    all containers). Returns {"<cid>": [ {id,template_id,stack_size,quality,
    cur_dur,max_dur}, ... ]} with each bucket ordered by (template_id, id) so the
    mirror paginates it identically to the live per-container path. `qjson(sql,
    fallback)` is the injected runner."""
    sql = ALL_ITEMS_SQL.format(account_id=int(account_id))
    flat = qjson(sql, "[]") or []
    buckets = {}
    for it in flat:
        cid = str(it.get("container_id"))
        buckets.setdefault(cid, []).append({
            "id": it.get("id"),
            "template_id": it.get("template_id"),
            "stack_size": it.get("stack_size"),
            "quality": it.get("quality"),
            "position_index": it.get("position_index"),
            "cur_dur": it.get("cur_dur"),
            "max_dur": it.get("max_dur"),
        })
    return buckets


def _run(sql: str) -> str:
    try:
        out = subprocess.run(
            ["/root/dq.sh", "-tAc", sql],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(json.dumps({"available": False, "error": "timeout"}))
    if out.returncode != 0:
        raise SystemExit(json.dumps({"available": False,
                                     "error": (out.stderr or out.stdout).strip()[:500]}))
    # psql prints "SET" on a separate line before the result; pick the LAST
    # non-empty / non-"SET" line as the value.
    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    return raw


def main():
    argv = sys.argv[1:]
    if len(argv) < 2 or len(argv) > 3:
        print(json.dumps({"available": False,
                          "error": "usage: dune-container-items.py <account_id> <container_id> [page]"}))
        sys.exit(2)
    account_id, container_id = argv[0], argv[1]
    page_arg = argv[2] if len(argv) == 3 else "1"
    if not account_id.isdigit() or not container_id.isdigit() or not page_arg.isdigit():
        print(json.dumps({"available": False, "error": "args must be digits"}))
        sys.exit(2)

    page = max(int(page_arg), 1)
    offset = (page - 1) * PAGE_SIZE

    # Step 1: ownership pre-check. Try the placeable chain first; if that fails,
    # try the CHOAM bank (inventory_type 30 keyed by inv.id). The matched mode
    # decides how items are joined: placeables key on inv.actor_id, the bank on
    # inv.id. False on both => 404 in the FastAPI route.
    owns_raw = _run(OWNS_SQL.format(account_id=account_id, container_id=container_id))
    if owns_raw.lower() in ("t", "true", "1"):
        match = "inv.actor_id"
    else:
        bank_raw = _run(OWNS_BANK_SQL.format(account_id=account_id, container_id=container_id))
        if bank_raw.lower() in ("t", "true", "1"):
            match = "inv.id"
        else:
            print(json.dumps({"available": False, "error": "not_owned",
                              "account_id": account_id, "container_id": container_id,
                              "items": [], "count": 0, "total_count": 0,
                              "page": page, "page_size": PAGE_SIZE}))
            return

    # Step 2: total count (so the template can render "Page N of M").
    total_raw = _run(COUNT_SQL.format(match=match, container_id=container_id))
    try:
        total = int(total_raw or "0")
    except ValueError:
        total = 0

    # Step 3: paginated items list.
    items_raw = _run(ITEMS_SQL.format(match=match, container_id=container_id,
                                      limit=PAGE_SIZE, offset=offset))
    try:
        items = json.loads(items_raw or "[]")
    except json.JSONDecodeError as e:
        print(json.dumps({"available": False, "error": f"parse: {e}",
                          "raw": items_raw[:500]}))
        sys.exit(1)

    print(json.dumps({
        "available": True,
        "account_id": account_id,
        "container_id": container_id,
        "items": items,
        "count": len(items),
        "total_count": total,
        "page": page,
        "page_size": PAGE_SIZE,
    }))


if __name__ == "__main__":
    main()
