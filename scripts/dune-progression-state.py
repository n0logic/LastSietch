#!/usr/bin/env python3
# Progression State: combined per-player read backing the v2 admin
# Specializations + Skills pickers. ONE read call gives the picker grids their
# stateful owned/available/learned rendering without hammering the heavier
# character-export.
#
# Deployed to lastsietch-dune:/root/dune-progression-state.py, invoked by the relay
# over SSH via the dispatcher's `progression-state <account_id>` token.
#
# Read-only. SELECT statements only; no game-state mutation, no restart.
#
# Data layout (R3 schema 2026-05-24, mirrors dune-character-export.py):
#   - dune.encrypted_player_state: account_id -> (player_controller_id, player_pawn_id)
#   - dune.purchased_specialization_keystones: keyed by player_id = CONTROLLER id
#     (confirmed vs icehunter db.go:3094 + live data; NOT pawn-keyed)
#   - dune.specialization_tracks: keyed by player_id = CONTROLLER id
#   - dune.fgl_entities: pawn's DuneCharacter slot carries FLevelComponent[1].ModuleData;
#     learned skill blocks are the Skills.Key.* keys with SkillPointsSpent >= 1
#
# Output shape:
#   {"available": true, "account_id": N,
#    "owned_keystone_ids": [int, ...],
#    "spec_tracks": {"<Track>": {"level": real, "xp": int}, ...},
#    "learned_blocks": ["Skills.Key.<...>", ...]}

import json
import subprocess
import sys


EXISTS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_build_object(
  'player_controller_id', eps.player_controller_id,
  'player_pawn_id',       eps.player_pawn_id
), '{{}}'::json)
FROM dune.encrypted_player_state eps
WHERE eps.account_id = {account_id}::bigint;
"""

# Owned keystones: purchased_specialization_keystones is keyed by CONTROLLER id.
OWNED_KEYSTONES_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(pk.keystone_id ORDER BY pk.keystone_id), '[]'::json)
FROM dune.purchased_specialization_keystones pk
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = pk.player_id
WHERE eps.account_id = {account_id}::bigint;
"""

# Spec tracks: keyed by CONTROLLER id. Emitted as an object keyed by track name.
SPEC_TRACKS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_object_agg(st.track_type::text,
         json_build_object('level', st.level, 'xp', st.xp_amount)), '{{}}'::json)
FROM dune.specialization_tracks st
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = st.player_id
WHERE eps.account_id = {account_id}::bigint;
"""

# Learned skill blocks: Skills.Key.* keys in the pawn's DuneCharacter
# FLevelComponent[1].ModuleData with SkillPointsSpent >= 1. ModuleData keys are
# the literal '(TagName="Skills.Key.<id>")'; the inner tag is extracted by regex.
LEARNED_BLOCKS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(s.tag ORDER BY s.tag), '[]'::json)
FROM (
  SELECT substring(kv.key from 'TagName="([^"]+)"') AS tag
  FROM dune.fgl_entities fe
  JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
  JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = afe.actor_id
  CROSS JOIN LATERAL jsonb_each(
    COALESCE(fe.components->'FLevelComponent'->1->'ModuleData', '{{}}'::jsonb)
  ) AS kv(key, value)
  WHERE eps.account_id = {account_id}::bigint
    AND afe.slot_name = 'DuneCharacter'
    AND COALESCE((kv.value->>'SkillPointsSpent')::int, 0) >= 1
) s
WHERE s.tag LIKE 'Skills.Key.%';
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
    """Assemble the progression-state payload. `account_id` is a digit string.
    `query_json(sql, fallback)` runs the SQL and returns parsed JSON (CLI passes
    the dq.sh runner; the collector passes a psycopg2 runner). Returns the same
    dict the CLI prints. Raises on a DB error (caller wraps)."""
    ident = query_json(EXISTS_SQL.format(account_id=account_id), "{}")
    if not ident or not ident.get("player_pawn_id"):
        return {"available": False, "error": "account_not_found",
                "account_id": int(account_id)}

    owned = query_json(OWNED_KEYSTONES_SQL.format(account_id=account_id), "[]")
    spec_tracks = query_json(SPEC_TRACKS_SQL.format(account_id=account_id), "{}")
    learned = query_json(LEARNED_BLOCKS_SQL.format(account_id=account_id), "[]")
    return {
        "available": True,
        "account_id": int(account_id),
        "owned_keystone_ids": owned,
        "spec_tracks": spec_tracks,
        "learned_blocks": learned,
    }


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"available": False,
                          "error": "usage: dune-progression-state.py <account_id>"}))
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
