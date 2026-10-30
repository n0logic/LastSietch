#!/usr/bin/env bash
# Map close: read-only record of everything on the closing instance, written on <game-host> with a sha256 manifest.
# Runs from the operator box. Every query is one READ ONLY transaction per file; nothing in the game DB changes.
#
#   ./inventory.sh forensic --from KEY --to KEY   pre-close capture: FORENSICS ONLY, never move input (CLOSE-PLAN Rule 1)
#   ./inventory.sh poststop --from KEY --to KEY   after the closing pod has stopped: the record the window works from
#
# KEY as for map-close.sh (LASTSIETCH_MAP_INSTANCES_FILE). Env: HOLD_TOTEMS / RECOVERY_VEHICLES as for map-close.sh, so the
# printed move_set count matches what the move will select.
# Output: <game-host>:/root/map-close/<from>-<label>-<UTC>/ (0700), files *.csv, counts.txt, MANIFEST.sha256.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="${1:-}"; shift || true
[[ "$LABEL" == forensic || "$LABEL" == poststop ]] || { echo "usage: $0 forensic|poststop --from KEY --to KEY" >&2; exit 2; }
SRC=""; DST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) SRC="$2"; shift 2;;
    --to) DST="$2"; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
RESOLVED=$(python3 "$HERE/map-close.py" resolve --from "$SRC" --to "$DST")
read -r SRC_PART SRC_DIM _ _ _ WORLD_MAP <<< "$RESOLVED"
HOLD_TOTEMS=${HOLD_TOTEMS:-0}; RECOVERY_VEHICLES=${RECOVERY_VEHICLES:-0}
[[ "$HOLD_TOTEMS" =~ ^[0-9]+(,[0-9]+)*$ && "$RECOVERY_VEHICLES" =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "bad id list" >&2; exit 2; }
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/root/map-close/$SRC-$LABEL-$TS

# Closing instance's actor set at record time: every actor on its partition, any state.
K="SELECT id FROM dune.actors WHERE partition_id = $SRC_PART"
declare -A Q=(
  [actors]="SELECT * FROM dune.actors WHERE partition_id = $SRC_PART ORDER BY id"
  [totems]="SELECT t.* FROM dune.totems t WHERE t.id IN ($K) ORDER BY id"
  [buildings]="SELECT b.* FROM dune.buildings b WHERE b.id IN ($K) ORDER BY id"
  [building_instances]="SELECT bi.* FROM dune.building_instances bi WHERE bi.building_id IN ($K) ORDER BY building_id, instance_id"
  [placeables]="SELECT p.* FROM dune.placeables p WHERE p.id IN ($K) ORDER BY id"
  [vehicles]="SELECT v.* FROM dune.vehicles v WHERE v.id IN ($K) ORDER BY id"
  [vehicle_modules]="SELECT m.* FROM dune.vehicle_modules m WHERE m.vehicle_id IN ($K) ORDER BY id"
  [inventories]="WITH RECURSIVE inv AS (SELECT i.* FROM dune.inventories i WHERE i.actor_id IN ($K) OR i.vehicle_module_id IN (SELECT id FROM dune.vehicle_modules WHERE vehicle_id IN ($K)) UNION SELECT c.* FROM dune.inventories c JOIN dune.items it ON it.id = c.item_id JOIN inv ON inv.id = it.inventory_id) SELECT * FROM inv ORDER BY id"
  [items]="WITH RECURSIVE inv AS (SELECT i.id FROM dune.inventories i WHERE i.actor_id IN ($K) OR i.vehicle_module_id IN (SELECT id FROM dune.vehicle_modules WHERE vehicle_id IN ($K)) UNION SELECT c.id FROM dune.inventories c JOIN dune.items it ON it.id = c.item_id JOIN inv ON inv.id = it.inventory_id) SELECT it.* FROM dune.items it WHERE it.inventory_id IN (SELECT id FROM inv) ORDER BY id"
  [permission_actor]="SELECT pa.* FROM dune.permission_actor pa WHERE pa.actor_id IN ($K) ORDER BY actor_id"
  [permission_actor_rank]="SELECT r.* FROM dune.permission_actor_rank r WHERE r.permission_actor_id IN ($K) ORDER BY 1, 2"
  [landclaim_segments]="SELECT s.* FROM dune.landclaim_segments s WHERE s.totem_id IN ($K) ORDER BY totem_id, grid_location_x, grid_location_y"
  [actor_fgl_entities]="SELECT f.* FROM dune.actor_fgl_entities f WHERE f.actor_id IN ($K) ORDER BY actor_id, slot_name"
  [fgl_entities]="SELECT e.* FROM dune.fgl_entities e WHERE e.entity_id IN (SELECT entity_id FROM dune.actor_fgl_entities WHERE actor_id IN ($K)) ORDER BY entity_id"
  [travel_actor_parent]="SELECT t.* FROM dune.travel_actor_parent t WHERE t.id IN ($K) OR t.parent_id IN ($K) ORDER BY id"
  [base_backup_links]="SELECT l.* FROM dune.base_backup_linked_actors l WHERE l.actor_id IN ($K) ORDER BY 1, 2"
  [recovered_vehicles]="SELECT rv.* FROM dune.recovered_vehicles rv WHERE rv.vehicle_id IN ($K) ORDER BY vehicle_id"
  [backup_vehicles]="SELECT bv.* FROM dune.backup_vehicles bv WHERE bv.vehicle_id IN ($K) ORDER BY vehicle_id"
  [respawns_dim]="SELECT r.* FROM dune.player_respawn_locations r WHERE r.map = '$WORLD_MAP' AND r.dimension = $SRC_DIM ORDER BY character_id, id"
  [characters]="SELECT ps.id, ps.account_id, ps.character_name, ps.player_controller_id, ps.player_pawn_id, ps.online_status, ps.character_state, ps.home_dimension_index, ps.return_dimension_index, ps.previous_server_partition_id, ps.last_login_time, a.partition_id AS pawn_partition, a.transform AS pawn_transform, pts.login_target_dimension_index FROM dune.player_state ps JOIN dune.accounts acc ON acc.id = ps.account_id LEFT JOIN dune.actors a ON a.id = ps.player_pawn_id LEFT JOIN dune.player_travel_state pts ON pts.fls_id = acc.\"user\" WHERE a.partition_id = $SRC_PART OR ps.home_dimension_index = $SRC_DIM OR ps.return_dimension_index = $SRC_DIM OR pts.login_target_dimension_index = $SRC_DIM ORDER BY ps.id"
)

COUNTS="SELECT 'state ' || state || ' ' || count(*) FROM dune.actors WHERE partition_id = $SRC_PART GROUP BY state
UNION ALL SELECT 'totems_default ' || count(*) FROM dune.actors a JOIN dune.totems t ON t.id = a.id WHERE a.partition_id = $SRC_PART AND a.state = 'Default'
UNION ALL SELECT 'pieces_default ' || count(*) FROM dune.building_instances bi JOIN dune.actors a ON a.id = bi.building_id WHERE a.partition_id = $SRC_PART AND a.state = 'Default'
UNION ALL SELECT 'vehicles_default ' || count(*) FROM dune.actors a JOIN dune.vehicles v ON v.id = a.id WHERE a.partition_id = $SRC_PART AND a.state = 'Default'
UNION ALL SELECT 'owners_rank1 ' || count(DISTINCT r.player_id) FROM dune.actors a JOIN dune.totems t ON t.id = a.id JOIN dune.permission_actor_rank r ON r.permission_actor_id = a.id AND r.rank = 1 WHERE a.partition_id = $SRC_PART AND a.state = 'Default'
UNION ALL SELECT 'active_pawns_on_source ' || count(*) FROM dune.player_state ps JOIN dune.actors a ON a.id = ps.player_pawn_id WHERE a.partition_id = $SRC_PART AND ps.character_state = 'Active'
UNION ALL SELECT 'online_now ' || count(*) FROM dune.player_state WHERE online_status <> 'Offline'
UNION ALL SELECT 'move_set ' || count(*) FROM dune.actors a LEFT JOIN dune.vehicles v ON v.id = a.id LEFT JOIN dune.buildings b ON b.id = a.id LEFT JOIN dune.placeables p ON p.id = a.id
  WHERE a.partition_id = $SRC_PART AND a.dimension_index = $SRC_DIM AND a.state = 'Default' AND (v.id IS NOT NULL OR b.id IS NOT NULL OR p.id IS NOT NULL)
    AND a.class NOT LIKE '%ChoamExchangeTerminal%' AND a.id <> ALL ('{$RECOVERY_VEHICLES}'::bigint[])
    AND a.id NOT IN (SELECT unnest('{$HOLD_TOTEMS}'::bigint[]) UNION SELECT p2.id FROM dune.placeables p2 JOIN dune.actor_fgl_entities f ON f.entity_id = p2.owner_entity_id AND f.slot_name = 'Actor' WHERE f.actor_id = ANY ('{$HOLD_TOTEMS}'::bigint[]) UNION SELECT bi.building_id FROM dune.building_instances bi JOIN dune.actor_fgl_entities f ON f.entity_id = bi.owner_entity_id AND f.slot_name = 'Actor' WHERE f.actor_id = ANY ('{$HOLD_TOTEMS}'::bigint[]))"

ssh <game-host> "sudo install -d -m 700 $OUT"
ro() { printf 'BEGIN TRANSACTION READ ONLY;\n%s\nROLLBACK;\n' "$1" | ssh <game-host> "sudo /root/dq.sh -X -q -v ON_ERROR_STOP=1 $2"; }
for name in "${!Q[@]}"; do
  ro "COPY (${Q[$name]}) TO STDOUT WITH (FORMAT csv, HEADER);" "" | ssh <game-host> "sudo sh -c 'umask 077; cat > $OUT/$name.csv'"
done
ro "$COUNTS;" "-At" | ssh <game-host> "sudo sh -c 'umask 077; cat > $OUT/counts.txt'"
ssh <game-host> "cd $OUT && sudo sh -c 'umask 077; sha256sum *.csv counts.txt > MANIFEST.sha256' && sudo wc -l *.csv | tail -1 && sudo cat counts.txt && echo manifest sha256 \$(sudo sha256sum MANIFEST.sha256 | cut -c1-64)"
echo "record: <game-host>:$OUT ($LABEL)"
