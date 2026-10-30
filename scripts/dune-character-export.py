#!/usr/bin/env python3
# Character Export, full per-player snapshot for the Last Sietch admin v2 Player
# Tools "Export" subtab. Specced in ADMIN-PANEL-V2-OVERHAUL.md:116
# ("full FLevelComponent + FactionPlayerComponent + tags + spec_tracks +
# actor properties snapshot"). Originally orphaned from P3b; ships in Order 0
# alongside G30 v1 per V2-ADMIN-CONTINUATION-PLAN-2026-05-26.md.
#
# Deployed to lastsietch-dune:/root/dune-character-export.py, invoked by the relay
# over SSH via dispatcher's `character-export <account_id>` token.
#
# Read-only. No game-state mutation. Last Sietch-internal JSON schema, NOT
# Solido-compatible, this is for Last Sietch-internal data portability (player can
# restore their own character snapshot on a future server, the admin can
# diff snapshots for forensic work, etc.).
#
# Data layout (R3 schema 2026-05-24):
#   - dune.encrypted_player_state: account_id -> (player_controller_id, player_pawn_id)
#   - dune.player_state VIEW: same row plus decrypted character_name
#   - dune.actors: controller actor carries FactionPlayerComponent in properties
#                  pawn actor carries placement transform + map
#   - dune.fgl_entities: pawn entity carries FLevelComponent in components
#                        (Funcom GA persists char level/XP/SP here per
#)
#   - dune.specialization_tracks: keyed by (player_id=controller_id, track_type)
#   - dune.player_tags: keyed by character_id = encrypted_player_state.id

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone


# Every snapshot is scoped to exactly ONE live character. encrypted_player_state
# holds one row per character SLOT, Deleted tombstones included (143 of 269 live
# accounts carried one on 2026-10-28), and _query_json keeps the LAST output row,
# so an unscoped account-level read could take controller/pawn props, components
# and tracks from a Deleted slot. RESOLVE_SQL picks the controller first (the
# requested one, or the most-recently-active), never a Deleted one, and every
# later query is pinned to it AND re-filters Deleted. A requested controller
# that is Deleted or not on this account yields account_not_found: it never
# falls back to another character.
def _ctrl_filters(controller_id):
    cid = int(controller_id)
    return {
        "ps_ctrl":  "  AND ps.player_controller_id = %d::bigint" % cid,
        "eps_ctrl": "  AND eps.player_controller_id = %d::bigint" % cid,
    }


def _requested_filter(controller_id):
    if controller_id is None:
        return ""
    return "  AND eps.player_controller_id = %d::bigint" % int(controller_id)


RESOLVE_SQL = """
SET search_path TO dune, public;
SELECT eps.player_controller_id
FROM dune.encrypted_player_state eps
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{requested}
ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
LIMIT 1;
"""


# Header: identity + online state + map/coords (from pawn actor).
HEADER_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_build_object(
  'character_name',       ps.character_name,
  'account_id',           ps.account_id,
  'player_controller_id', ps.player_controller_id,
  'player_pawn_id',       ps.player_pawn_id,
  'online_status',        ps.online_status,
  'life_state',           ps.life_state,
  'map',                  a.map,
  'transform',            a.transform::text
), '{{}}'::json)
FROM dune.player_state ps
JOIN dune.encrypted_player_state eps
  ON eps.player_controller_id = ps.player_controller_id
LEFT JOIN dune.actors a ON a.id = ps.player_pawn_id
WHERE ps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{ps_ctrl}
ORDER BY ps.last_avatar_activity DESC NULLS LAST, ps.player_controller_id DESC
LIMIT 1;
"""

# Controller actor properties JSONB, contains FactionPlayerComponent and
# the rest of the controller-side component bag.
CONTROLLER_PROPERTIES_SQL = """
SET search_path TO dune, public;
SELECT coalesce(a.properties, '{{}}'::jsonb)
FROM dune.actors a
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = a.id
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{eps_ctrl};
"""

# Pawn actor properties JSONB, placement + per-actor state.
PAWN_PROPERTIES_SQL = """
SET search_path TO dune, public;
SELECT coalesce(a.properties, '{{}}'::jsonb)
FROM dune.actors a
JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = a.id
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{eps_ctrl};
"""

# Pawn fgl_entities.components JSONB, contains FLevelComponent. The pawn
# actor maps to MULTIPLE fgl_entity rows (each a different component bag),
# so we aggregate into an array of {entity_id, components} objects. The
# primary char entity carrying FLevelComponent is one element of the array;
# the others carry FItemCraftingComponent etc. Preserving all is required
# for round-trip fidelity.
PAWN_COMPONENTS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'entity_id',  fe.entity_id,
  'components', fe.components
) ORDER BY fe.entity_id), '[]'::json)
FROM dune.fgl_entities fe
JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = afe.actor_id
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{eps_ctrl};
"""

# Controller fgl_entities.components JSONB, same multi-entity aggregation
# on the controller side. May be empty for accounts where the controller
# has no fgl_entities rows.
CONTROLLER_COMPONENTS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'entity_id',  fe.entity_id,
  'components', fe.components
) ORDER BY fe.entity_id), '[]'::json)
FROM dune.fgl_entities fe
JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = afe.actor_id
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{eps_ctrl};
"""

# Player tags for the resolved character, read directly from player_tags keyed
# by character_id = encrypted_player_state.id, so neither an alt's nor a Deleted
# slot's tags bleed into the export. (The account-level admin_read_player_tags
# proc is no longer used here: it cannot be scoped to one live character.) Tags
# are ALL Funcom game-progression flags (Contract/Journey/DialogueFlags/
# Exploration/Faction/... verified 2026-07-17); the portal applies a namespace
# allowlist before returning them to a player.
TAGS_CTRL_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(pt.tag ORDER BY pt.tag), '[]'::json)
FROM dune.player_tags pt
JOIN dune.encrypted_player_state eps ON eps.id = pt.character_id
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{eps_ctrl};
"""

# Specialization tracks: tabular, keyed by controller_id. Each row = one track.
# Live schema (verified 2026-05-26): columns are (player_id, track_type,
# xp_amount int, level real). There's no unspent_points column on this table -
# unspent specialization SP lives in FLevelComponent JSONB on the pawn, which
# the snapshot's pawn_fgl_entities_components dump carries verbatim.
SPEC_TRACKS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'track_type', st.track_type,
  'xp_amount',  st.xp_amount,
  'level',      st.level
) ORDER BY st.track_type), '[]'::json)
FROM dune.specialization_tracks st
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = st.player_id
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{eps_ctrl};
"""


def _query_json(sql, fallback="null"):
    """Run psql with the dq.sh wrapper, strip SET header, return parsed JSON."""
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
    ap = argparse.ArgumentParser(description="Last Sietch character snapshot export (read-only)")
    ap.add_argument("account_id")
    ap.add_argument("--controller", type=int, default=None,
                    help="export this character (portal multichar); omit to "
                         "export the most-recently-active non-Deleted character")
    args = ap.parse_args()

    account_id = args.account_id
    if not account_id.isdigit():
        print(json.dumps({"available": False,
                          "error": "account_id must be digits"}))
        sys.exit(2)

    try:
        resolved = _query_json(
            RESOLVE_SQL.format(account_id=account_id,
                               requested=_requested_filter(args.controller)),
            fallback="null")
        if not isinstance(resolved, int):
            print(json.dumps({"available": False, "error": "account_not_found"}))
            sys.exit(0)
        cf = _ctrl_filters(resolved)

        header = _query_json(HEADER_SQL.format(account_id=account_id, **cf),
                             fallback="{}")
        if not header or not header.get("account_id"):
            print(json.dumps({"available": False, "error": "account_not_found"}))
            sys.exit(0)

        controller_properties = _query_json(
            CONTROLLER_PROPERTIES_SQL.format(account_id=account_id, **cf), fallback="{}")
        pawn_properties = _query_json(
            PAWN_PROPERTIES_SQL.format(account_id=account_id, **cf), fallback="{}")
        # _components SQLs return a JSON ARRAY of {entity_id, components}
        # one element per fgl_entities row on that actor.
        pawn_components = _query_json(
            PAWN_COMPONENTS_SQL.format(account_id=account_id, **cf), fallback="[]")
        controller_components = _query_json(
            CONTROLLER_COMPONENTS_SQL.format(account_id=account_id, **cf), fallback="[]")
        tags = _query_json(TAGS_CTRL_SQL.format(account_id=account_id, **cf), fallback="[]")
        spec_tracks = _query_json(
            SPEC_TRACKS_SQL.format(account_id=account_id, **cf), fallback="[]")
    except subprocess.TimeoutExpired:
        print(json.dumps({"available": False, "error": "timeout"}))
        sys.exit(1)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(json.dumps({"available": False, "error": str(e)[:500]}))
        sys.exit(1)

    # FLevelComponent / FactionPlayerComponent surfaced at the top level for
    # the spec's "full FLevelComponent + FactionPlayerComponent" requirement,
    # while the full JSONB bags are preserved below for fidelity. Tolerant
    # of either being absent on never-played characters.
    #
    # The pawn maps to multiple fgl_entities rows, walk them to find the one
    # carrying FLevelComponent (only one entity has it; the others carry
    # FItemCraftingComponent etc.).
    flevel = None
    for entry in (pawn_components or []):
        if isinstance(entry, dict):
            comp = entry.get("components") or {}
            if isinstance(comp, dict) and "FLevelComponent" in comp:
                flevel = comp["FLevelComponent"]
                break
    ffaction = (controller_properties or {}).get("FactionPlayerComponent")

    snapshot = {
        "ls_schema":   "character-snapshot-v1",
        "exported_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account_id":   int(account_id),
        "character": {
            "character_name":       header.get("character_name"),
            "player_controller_id": header.get("player_controller_id"),
            "player_pawn_id":       header.get("player_pawn_id"),
            "online_status":        header.get("online_status"),
            "life_state":           header.get("life_state"),
            "map":                  header.get("map"),
            "transform":            header.get("transform"),
            "FLevelComponent":            flevel,
            "FactionPlayerComponent":     ffaction,
            "controller_actor_properties":   controller_properties,
            "pawn_actor_properties":         pawn_properties,
            "pawn_fgl_entities_components":       pawn_components,
            "controller_fgl_entities_components": controller_components,
            "tags":                  tags,
            "specialization_tracks": spec_tracks,
        },
    }

    print(json.dumps({"available": True,
                      "account_id": account_id,
                      "snapshot":   snapshot}))


if __name__ == "__main__":
    main()
