#!/usr/bin/env bash
# bb-capture.sh — Snapshot every base-backup-related DB row at a known instant.
# Usage:
#   bb-capture.sh <label>           # take a labeled snapshot
#   bb-capture.sh diff <a> <b>      # compare two labeled snapshots
#
# Designed for the in-game BaseBackupTool empirical capture:
#   1. bb-capture.sh pre-save     (BEFORE you save a base in-game)
#   2. <save the base in-game>
#   3. bb-capture.sh post-save
#   4. bb-capture.sh diff pre-save post-save
#   5. (optional, costs a recovery) bb-capture.sh pre-restore + restore + post-restore + diff
#
# Captures, scoped to player_id=19 (n0logic pawn) where relevant:
#   - dune.base_backups (full table, only ~0 rows expected pre-save)
#   - dune.base_backup_linked_actors (full table)
#   - dune.items where template_id ILIKE 'BaseBackupTool%' OR stats::text ILIKE '%PlayerBaseBackup%'
#   - dune.actors with owner_account_id = 2 (n0logic account) — to see actor list & state
#   - dune.actor_fgl_entities + fgl_entities components for owned actors
#   - dune.buildings + building_instances totals for owned actors
#   - dune.placeables totals for owned actors

set -euo pipefail

OUT_BASE="${HOME}/Source/Personal/House0fL0gic/docs/dune-research/base-backup-captures"
mkdir -p "$OUT_BASE"

PLAYER_ID=19          # n0logic pawn
PLAYER_ACCOUNT=2      # n0logic account

cmd="${1:-}"

if [[ "$cmd" == "diff" ]]; then
  a="${2:?label A required}"
  b="${3:?label B required}"
  dir_a="$OUT_BASE/$a"
  dir_b="$OUT_BASE/$b"
  [[ -d "$dir_a" ]] || { echo "missing: $dir_a" >&2; exit 1; }
  [[ -d "$dir_b" ]] || { echo "missing: $dir_b" >&2; exit 1; }

  echo "==== DIFF $a -> $b ===="
  for f in base_backups linked_actors items_bb actors_owned actor_components buildings_owned placeables_owned counts; do
    pa="$dir_a/$f.txt"
    pb="$dir_b/$f.txt"
    if [[ -f "$pa" && -f "$pb" ]]; then
      if ! diff -q "$pa" "$pb" > /dev/null 2>&1; then
        echo
        echo "---- $f differs ----"
        diff -u "$pa" "$pb" | head -200 || true
      fi
    fi
  done
  exit 0
fi

label="${1:?label required (e.g. pre-save, post-save, pre-restore, post-restore)}"
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="$OUT_BASE/$label"
mkdir -p "$out"

echo "Snapshot label=$label ts=$ts -> $out"

run_sql() {
  local file="$1" sql="$2"
  ssh lastsietch-dune "echo \"$sql\" | /root/dq.sh" > "$out/$file.txt" 2>&1
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "  WARN: $file query exited $rc"
  fi
  wc -l < "$out/$file.txt" | xargs -I{} echo "  $file: {} lines"
}

# 1. Full base_backups table
run_sql base_backups "SELECT id, player_id, base_backup_name FROM dune.base_backups ORDER BY id;"

# 2. Full base_backup_linked_actors with joined actor class
run_sql linked_actors "SELECT bbla.id AS backup_id, bbla.actor_id, a.class, a.map, a.partition_id, a.owner_account_id FROM dune.base_backup_linked_actors bbla LEFT JOIN dune.actors a ON a.id = bbla.actor_id ORDER BY bbla.id, bbla.actor_id;"

# 3. All BaseBackupTool items + any item with PlayerBaseBackup in stats
run_sql items_bb "SELECT id, inventory_id, template_id, stack_size, stats FROM dune.items WHERE template_id = 'BaseBackupTool' OR stats::text ILIKE '%PlayerBaseBackup%' ORDER BY id;"

# 4. All actors owned by n0logic (id IS often what the building_id / placeables.id will match)
run_sql actors_owned "SELECT id, class, map, partition_id, dimension_index, properties, owner_account_id, serial FROM dune.actors WHERE owner_account_id = $PLAYER_ACCOUNT ORDER BY id;"

# 5. FGL components on owned actors (chest contents, bench state, etc. live here)
run_sql actor_components "SELECT a.id AS actor_id, a.class, afe.slot_name, fe.entity_id, jsonb_object_keys(fe.components) AS component_key FROM dune.actors a JOIN dune.actor_fgl_entities afe ON afe.actor_id = a.id JOIN dune.fgl_entities fe ON fe.entity_id = afe.entity_id WHERE a.owner_account_id = $PLAYER_ACCOUNT ORDER BY a.id, slot_name, component_key;"

# 6. Buildings owned (buildings.id = actor.id where owner_account_id = us)
run_sql buildings_owned "SELECT b.id AS building_id, b.owner_id, COUNT(bi.*) AS instance_count FROM dune.buildings b JOIN dune.actors a ON a.id = b.id LEFT JOIN dune.building_instances bi ON bi.building_id = b.id WHERE a.owner_account_id = $PLAYER_ACCOUNT GROUP BY b.id, b.owner_id ORDER BY b.id;"

# 7. Placeables owned
run_sql placeables_owned "SELECT p.id AS placeable_id, p.owner_entity_id, p.building_type, p.health FROM dune.placeables p JOIN dune.actors a ON a.id = p.id WHERE a.owner_account_id = $PLAYER_ACCOUNT ORDER BY p.id;"

# 8. Aggregate counts (quick at-a-glance diff)
run_sql counts "
SELECT 'base_backups' AS t, COUNT(*) FROM dune.base_backups WHERE player_id = $PLAYER_ID
UNION ALL SELECT 'linked_actors', COUNT(*) FROM dune.base_backup_linked_actors bbla JOIN dune.base_backups bb ON bb.id = bbla.id WHERE bb.player_id = $PLAYER_ID
UNION ALL SELECT 'bb_items', COUNT(*) FROM dune.items WHERE template_id = 'BaseBackupTool' OR stats::text ILIKE '%PlayerBaseBackup%'
UNION ALL SELECT 'actors_owned', COUNT(*) FROM dune.actors WHERE owner_account_id = $PLAYER_ACCOUNT
UNION ALL SELECT 'buildings_owned', COUNT(*) FROM dune.buildings b JOIN dune.actors a ON a.id=b.id WHERE a.owner_account_id = $PLAYER_ACCOUNT
UNION ALL SELECT 'placeables_owned', COUNT(*) FROM dune.placeables p JOIN dune.actors a ON a.id=p.id WHERE a.owner_account_id = $PLAYER_ACCOUNT
UNION ALL SELECT 'building_instances_owned', COUNT(bi.*) FROM dune.building_instances bi JOIN dune.actors a ON a.id=bi.building_id WHERE a.owner_account_id = $PLAYER_ACCOUNT;
"

echo "Done. Dir: $out"
