#!/usr/bin/env bash
# Read-only Dune Base Backup (BB) listing helpers for the Last Sietch relay.
#
# Deployed to lastsietch-dune:/root/dune-bb-list.sh (mode 0750, owner root). Invoked
# ONLY by the forced-command dispatcher /root/dune-relay-dispatch.sh via the
# `bb-available-sources` and `bb-slot-count <account_id>` tokens.
#
# Subcommands:
#   available-sources       JSON array of base_backups joined to their totem
#                           actor + linked-actor count (source picker)
#   slot-count <account_id> JSON object {account_id, controller_id, slot_count}
#                           for the requested account
#   detail <backup_id>      JSON object for one base_backup: identity + owner +
#                           totem + linked-actor count + per-class composition
#                           breakdown (the admin Bases detail drawer)
#
# Strict read-only — every query is a SELECT executed through /root/dq.sh
# (the same psql shim dune-tags.py uses). No DB writes are performed here.
set -euo pipefail

# Run an arbitrary read-only SELECT and emit the last non-empty, non-"SET" line
# of psql output. Mirrors dune-tags.py's parsing convention.
run_select() {
  local sql="$1"
  local out
  if ! out=$(/root/dq.sh -tAc "$sql" 2>/dev/null); then
    return 1
  fi
  local raw=""
  while IFS= read -r line; do
    line="${line//$'\r'/}"
    line="${line## }"
    line="${line%% }"
    if [[ -n "$line" && "$line" != "SET" ]]; then
      raw="$line"
    fi
  done <<<"$out"
  printf '%s' "$raw"
}

cmd_available_sources() {
  local sql
  sql=$(cat <<'EOF'
SET search_path TO dune, public;
WITH totem_actors AS (
  SELECT bbla.id AS backup_id, bbla.actor_id AS totem_actor_id
    FROM dune.base_backup_linked_actors bbla
    JOIN dune.actors a ON a.id = bbla.actor_id
   WHERE a.class LIKE '%Totem%'
),
counts AS (
  SELECT id AS backup_id, COUNT(*) AS linked_actor_count
    FROM dune.base_backup_linked_actors
   GROUP BY id
),
owners AS (
  -- Resolve base_backups.player_id (controller actor_id) → account_id →
  -- decrypted character name. Returns NULL gracefully if any link is broken
  -- (legacy/orphaned backup rows, missing player_state, etc.).
  SELECT bb.id AS backup_id,
         dune.decrypt_user_data(eps.encrypted_character_name) AS owner_name,
         ea.id AS owner_account_id
    FROM dune.base_backups bb
    LEFT JOIN dune.encrypted_player_state eps ON eps.player_controller_id = bb.player_id
    LEFT JOIN dune.encrypted_accounts ea ON ea.id = eps.account_id
)
SELECT COALESCE(json_agg(
  json_build_object(
    'id', bb.id,
    'name', bb.base_backup_name,
    'player_id', bb.player_id,
    'owner_account_id', o.owner_account_id,
    'owner_name', o.owner_name,
    'totem_actor_id', ta.totem_actor_id,
    'linked_actor_count', c.linked_actor_count
  ) ORDER BY bb.id
), '[]'::json) AS sources
FROM dune.base_backups bb
LEFT JOIN totem_actors ta ON ta.backup_id = bb.id
LEFT JOIN counts c ON c.backup_id = bb.id
LEFT JOIN owners o ON o.backup_id = bb.id;
EOF
)
  local raw
  if ! raw=$(run_select "$sql"); then
    printf '{"available":false,"error":"bb available-sources query failed","sources":[]}\n'
    exit 1
  fi
  [[ -n "$raw" ]] || raw="[]"
  printf '{"available":true,"sources":%s}\n' "$raw"
}

cmd_slot_count() {
  local account_id="${1:-}"
  if [[ -z "$account_id" || ! "$account_id" =~ ^[0-9]+$ ]]; then
    printf '{"available":false,"error":"account_id must be a positive integer"}\n'
    exit 2
  fi

  local sql
  sql=$(cat <<EOF
SET search_path TO dune, public;
SELECT COALESCE(
  (SELECT json_build_object(
            'account_id', ${account_id}::bigint,
            'controller_id', controller.id,
            'slot_count', (SELECT COUNT(*)
                             FROM dune.base_backups
                            WHERE player_id = controller.id)
          )
     FROM dune.encrypted_accounts ea
     JOIN dune.encrypted_player_state eps ON eps.account_id = ea.id
     JOIN dune.actors controller ON controller.id = eps.player_controller_id
    WHERE ea.id = ${account_id}::bigint
    LIMIT 1),
  json_build_object(
    'account_id', ${account_id}::bigint,
    'controller_id', NULL,
    'slot_count', 0
  )
) AS result;
EOF
)
  local raw
  if ! raw=$(run_select "$sql"); then
    printf '{"available":false,"error":"bb slot-count query failed"}\n'
    exit 1
  fi
  if [[ -z "$raw" ]]; then
    printf '{"available":false,"error":"bb slot-count query produced no output"}\n'
    exit 1
  fi
  printf '{"available":true,"result":%s}\n' "$raw"
}

cmd_detail() {
  local backup_id="${1:-}"
  if [[ -z "$backup_id" || ! "$backup_id" =~ ^[0-9]+$ ]]; then
    printf '{"available":false,"error":"backup_id must be a positive integer"}\n'
    exit 2
  fi

  local sql
  sql=$(cat <<EOF
SET search_path TO dune, public;
WITH meta AS (
  SELECT bb.id,
         bb.base_backup_name AS name,
         bb.player_id,
         dune.decrypt_user_data(eps.encrypted_character_name) AS owner_name,
         ea.id AS owner_account_id
    FROM dune.base_backups bb
    LEFT JOIN dune.encrypted_player_state eps ON eps.player_controller_id = bb.player_id
    LEFT JOIN dune.encrypted_accounts ea ON ea.id = eps.account_id
   WHERE bb.id = ${backup_id}::bigint
),
totem AS (
  SELECT bbla.actor_id AS totem_actor_id
    FROM dune.base_backup_linked_actors bbla
    JOIN dune.actors a ON a.id = bbla.actor_id
   WHERE bbla.id = ${backup_id}::bigint AND a.class LIKE '%Totem%'
   LIMIT 1
),
classes AS (
  SELECT a.class AS class, COUNT(*) AS count
    FROM dune.base_backup_linked_actors bbla
    JOIN dune.actors a ON a.id = bbla.actor_id
   WHERE bbla.id = ${backup_id}::bigint
   GROUP BY a.class
)
SELECT COALESCE(
  (SELECT json_build_object(
            'id', m.id,
            'name', m.name,
            'player_id', m.player_id,
            'owner_name', m.owner_name,
            'owner_account_id', m.owner_account_id,
            'totem_actor_id', (SELECT totem_actor_id FROM totem),
            'linked_actor_count', COALESCE((SELECT SUM(count) FROM classes), 0),
            'classes', COALESCE(
              (SELECT json_agg(json_build_object('class', class, 'count', count)
                       ORDER BY count DESC, class)
                 FROM classes),
              '[]'::json)
          )
     FROM meta m),
  'null'::json
) AS detail;
EOF
)
  local raw
  if ! raw=$(run_select "$sql"); then
    printf '{"available":false,"error":"bb detail query failed"}\n'
    exit 1
  fi
  if [[ -z "$raw" || "$raw" == "null" ]]; then
    printf '{"available":false,"error":"base backup not found"}\n'
    exit 0
  fi
  printf '{"available":true,"detail":%s}\n' "$raw"
}

main() {
  if [[ $# -eq 0 ]]; then
    printf '{"available":false,"error":"usage: dune-bb-list.sh available-sources | slot-count <account_id> | detail <backup_id>"}\n'
    exit 2
  fi
  local sub="$1"; shift
  case "$sub" in
    available-sources)
      [[ $# -eq 0 ]] || {
        printf '{"available":false,"error":"available-sources takes no arguments"}\n'
        exit 2
      }
      cmd_available_sources
      ;;
    slot-count)
      cmd_slot_count "${1:-}"
      ;;
    detail)
      cmd_detail "${1:-}"
      ;;
    *)
      printf '{"available":false,"error":"unknown subcommand: %s"}\n' "$sub"
      exit 2
      ;;
  esac
}

main "$@"
