#!/usr/bin/env python3
# Per-player map overlay: one read call returns the logged-in player's OWN
# location, base totems, and owned vehicles, each with map + dimension + world
# coordinates. Powers the portal map "Show my location" overlay.
#
# Deployed to lastsietch-dune:/root/dune-player-map.py, invoked by the relay over SSH
# via the dispatcher's `player-map <account_id>` token.
#
# Read-only. SELECT statements only; no game-state mutation, no restart.
#
# PII: this feed carries the player's own position and base/vehicle positions,
# so it is NOT public — the admin-backend only ever serves it for the account
# bound to the caller's own portal session (never an arbitrary account_id).
#
# Data layout (live schema verified 2026-06-09):
#   - dune.encrypted_player_state: account_id -> (player_controller_id, player_pawn_id)
#   - dune.actors: the pawn row (id = player_pawn_id) carries map / dimension_index
#     / partition_id / (transform).location.{x,y}. dune.player_state.online_status
#     gives live vs last-known. A pawn keeps its last transform while offline.
#   - bases = BP_Totem / BP_TotemSmall placeables owned at rank 1 via the standard
#     ownership chain (placeables.owner_entity_id -> actor_fgl_entities ->
#     permission_actor_rank rank=1 -> player_controller_id).
#   - vehicles = actors owned at rank 1 whose class is under /Vehicles/ (excluding
#     fabricators/dismantled). DD vehicle rows persist their last transform too.
#
# Output shape:
#   {"available": true, "account_id": N,
#    "self":     {"map","dim","part","x","y","online"} | null,
#    "bases":    [{"map","dim","part","x","y","kind","name"} ...],
#    "vehicles": [{"map","dim","part","x","y","class"} ...]}

import json
import subprocess
import sys


# Identity: confirm the account exists + has a pawn.
EXISTS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_build_object(
  'player_controller_id', eps.player_controller_id,
  'player_pawn_id',       eps.player_pawn_id
), '{{}}'::json)
FROM dune.encrypted_player_state eps
WHERE eps.account_id = {account_id}::bigint;
"""

# Own position from the pawn actor + live online flag. Last-known transform is
# kept while offline, so this is non-null even for an offline player.
SELF_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_build_object(
  'map',    a.map,
  'dim',    a.dimension_index,
  'part',   a.partition_id,
  'x',      round((((a.transform).location).x)::numeric, 0)::bigint,
  'y',      round((((a.transform).location).y)::numeric, 0)::bigint,
  'online', (ps.online_status = 'Online')
), '{{}}'::json)
FROM dune.encrypted_player_state eps
JOIN dune.actors a ON a.id = eps.player_pawn_id
LEFT JOIN dune.player_state ps ON ps.player_pawn_id = eps.player_pawn_id
WHERE eps.account_id = {account_id}::bigint
  AND a.transform IS NOT NULL
LIMIT 1;
"""

# Base totems owned at rank 1 (the player's claimed bases). BP_Totem = a full
# base anchor, BP_TotemSmall = a small/outpost anchor.
BASES_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'map',  a.map,
  'dim',  a.dimension_index,
  'part', a.partition_id,
  'x',    round((((a.transform).location).x)::numeric, 0)::bigint,
  'y',    round((((a.transform).location).y)::numeric, 0)::bigint,
  'kind', CASE WHEN a.class ILIKE '%TotemSmall%' THEN 'outpost' ELSE 'base' END,
  'name', nullif(CASE WHEN pa.actor_name NOT LIKE '##%' AND pa.actor_name <> 'None'
                      THEN btrim(pa.actor_name) END, '')
) ORDER BY a.id), '[]'::json)
FROM dune.placeables p
JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
JOIN dune.permission_actor_rank par ON par.permission_actor_id = afe.actor_id AND par.rank = 1
JOIN dune.actors a ON a.id = p.id
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
LEFT JOIN dune.permission_actor pa ON pa.actor_id = p.id
WHERE eps.account_id = {account_id}::bigint
  AND a.class ILIKE '%Totem%'
  AND a.transform IS NOT NULL;
"""

# Vehicles the player owns at rank 1, with their last-known position + class
# (the admin-backend maps class -> friendly name + extracted icon).
VEHICLES_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'map',   a.map,
  'dim',   a.dimension_index,
  'part',  a.partition_id,
  'x',     round((((a.transform).location).x)::numeric, 0)::bigint,
  'y',     round((((a.transform).location).y)::numeric, 0)::bigint,
  'class', split_part(a.class, '.', 2)
) ORDER BY a.id), '[]'::json)
FROM dune.permission_actor_rank par
JOIN dune.actors a ON a.id = par.permission_actor_id
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
WHERE eps.account_id = {account_id}::bigint
  AND par.rank = 1
  AND a.class ILIKE '%/Vehicles/%'
  AND a.class NOT ILIKE '%Fabricator%'
  AND a.class NOT ILIKE '%Dismantled%'
  AND a.transform IS NOT NULL;
"""


def _query_json(sql, fallback="null"):
    """Run psql via the dq.sh wrapper, strip the SET header, return parsed JSON."""
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


def build(account_id, query_json):
    """Assemble the per-player overlay payload. `account_id` is a digit string."""
    ident = query_json(EXISTS_SQL.format(account_id=account_id), "{}")
    if not ident or not ident.get("player_pawn_id"):
        return {"available": False, "error": "account_not_found",
                "account_id": int(account_id)}

    self_pos = query_json(SELF_SQL.format(account_id=account_id), "{}") or None
    if self_pos == {}:
        self_pos = None
    bases = query_json(BASES_SQL.format(account_id=account_id), "[]") or []
    vehicles = query_json(VEHICLES_SQL.format(account_id=account_id), "[]") or []

    return {
        "available": True,
        "account_id": int(account_id),
        "self": self_pos,
        "bases": bases,
        "vehicles": vehicles,
    }


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"available": False,
                          "error": "usage: dune-player-map.py <account_id>"}))
        sys.exit(2)

    account_id = sys.argv[1]
    if not account_id.isdigit():
        print(json.dumps({"available": False,
                          "error": "account_id must be digits"}))
        sys.exit(2)

    try:
        result = build(account_id, _query_json)
    except Exception as exc:  # noqa: BLE001 - surface any DB error as JSON
        print(json.dumps({"available": False, "error": str(exc)[:500],
                          "account_id": int(account_id)}))
        sys.exit(1)

    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
