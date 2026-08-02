#!/usr/bin/env bash
# capture-placeable-defaults.sh — read defaults from a LIVE placeable and emit a
# SQL UPSERT for dune.ls_solido_class_defaults.
#
# Used during G22 Phase 2b empirical capture (
# ITEM-G22-BUILD-SPEC.md §11). After placing an instance of a v1 seed class
# in-game with the normal construction tool, run this script against the new
# actor_id to extract default_components, default_health, container metadata
# (inventory_type / max_item_count / max_item_volume / component_name_hash) and
# the power-circuit flag.
#
# READ-ONLY against dune.* — every query goes through ssh lastsietch-dune /root/dq.sh.
# No DB writes. Output goes to stdout for review-then-paste into
# scripts/dune-grant-schema.sql. Operators are expected to back up the schema
# file (.bak-YYYYMMDDThhmmssZ) before pasting and applying.
#
# Two invocation modes:
#   capture-placeable-defaults.sh <actor_id> <class_short_name>
#       Emit one UPSERT for the named class, derived from <actor_id>.
#       class_short_name is the registry primary key (e.g. Generator,
#       SpiceSilo, MediumStorageContainer).
#
#   capture-placeable-defaults.sh --account <account_id> [--limit N]
#       List recently-placed placeable actors whose totem belongs to
#       <account_id> (default N=50). Use this to find the actor_id of
#       something you just placed.

set -euo pipefail

DQ="ssh lastsietch-dune /root/dq.sh"

usage() {
  sed -n '2,/^set -euo pipefail/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
  exit 64
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

dq_query() {
  # pipe-separated (psql default for unaligned), no headers — easy bash parsing.
  # SQL goes over stdin to avoid SSH+remote-shell re-interpretation of parens
  # and to dodge tab-char loss in the SSH arg-vector pass-through.
  printf '%s\n' "$1" | $DQ -v ON_ERROR_STOP=1 -tA
}

dq_raw() {
  # single-column unaligned, headers off — for the big UPSERT blob.
  printf '%s\n' "$1" | $DQ -v ON_ERROR_STOP=1 -tA
}

cmd_enumerate() {
  local account_id="$1" limit="${2:-50}"
  [[ "$account_id" =~ ^[0-9]+$ ]] || die "account_id must be numeric: $account_id"
  [[ "$limit"      =~ ^[0-9]+$ ]] || die "--limit must be numeric: $limit"

  # Trace path (per our internal notes.md + live verification):
  #   placeables.owner_entity_id -> actor_fgl_entities.entity_id (totem entity)
  #   actor_fgl_entities.actor_id -> permission_actor_rank.permission_actor_id
  #   permission_actor_rank.player_id -> encrypted_player_state.player_controller_id
  #   encrypted_player_state.account_id = $account_id
  echo "# Recently-placed placeables for account_id=$account_id (most recent first, limit $limit)"
  echo "# Columns: actor_id  class_path"
  dq_query "
    SELECT p.id::text, a.class
      FROM dune.placeables p
      JOIN dune.actors a ON a.id = p.id
      JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
      JOIN dune.permission_actor_rank par ON par.permission_actor_id = afe.actor_id
      JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
     WHERE eps.account_id = ${account_id}
     ORDER BY p.id DESC
     LIMIT ${limit};
  "
}

cmd_capture() {
  local actor_id="$1" class_short="$2"
  [[ "$actor_id" =~ ^[0-9]+$ ]]         || die "actor_id must be numeric: $actor_id"
  [[ "$class_short" =~ ^[A-Za-z0-9_]+$ ]] || die "class_short must match [A-Za-z0-9_]+: $class_short"

  # Pre-flight: actor exists in dune.actors. We don't require placeables
  # membership here — the reserved kinds Totem and Building are not placeables,
  # but they still need default_components captured from any live exemplar
  # (e.g. capture 'Totem' from actor 660 in a live sub-fief base). When the
  # actor isn't a placeable, the pbt CTE LEFT JOIN yields NULL building_type,
  # which is the correct state for those registry rows (the G22 preflight
  # check at dune-grant.sh only inspects placeables_building_type for entries
  # that actually appear in _g22_stage_placeables — Totem/Building never do).
  local exists
  exists=$(dq_query "
    SELECT CASE WHEN EXISTS (
             SELECT 1 FROM dune.actors WHERE id = ${actor_id}
           ) THEN '1' ELSE '0' END;
  ")
  [[ "$exists" == "1" ]] || die "actor_id ${actor_id} is not in dune.actors (place it first, or use --account to enumerate)"

  # Build the UPSERT entirely server-side using quote_literal to keep JSONB
  # safely escaped. Returns one row of UPSERT text on stdout.
  #
  # Captures all 3 P2 columns per consensus OQ1+N1+N3:
  #   - default_components / default_properties (per-class FGL components)
  #   - placeables_building_type (from dune.placeables.building_type — empirical,
  #     NOT derivable from class_short_name)
  #   - is_active = true (this class is captured + ready for v1)
  local sql
  sql=$(cat <<SQL
WITH a AS (
  SELECT id, class FROM dune.actors WHERE id = ${actor_id}
),
pbt AS (
  -- LEFT-JOIN-friendly: returns one row with building_type=NULL when the
  -- actor isn't a placeable (Totem / Building reserved kinds).
  SELECT (SELECT building_type FROM dune.placeables WHERE id = ${actor_id}) AS building_type
),
slots AS (
  SELECT afe.slot_name, fe.components
    FROM dune.actor_fgl_entities afe
    JOIN dune.fgl_entities fe ON fe.entity_id = afe.entity_id
   WHERE afe.actor_id = ${actor_id}
),
agg AS (
  SELECT COALESCE(jsonb_object_agg(slot_name, components), '{}'::jsonb) AS comp
    FROM slots
),
inv AS (
  SELECT i.inventory_type, i.max_item_count, i.max_item_volume,
         ai.component_name_hash
    FROM dune.inventories i
    JOIN dune.actor_inventories ai ON ai.inventory_id = i.id
   WHERE i.actor_id = ${actor_id}
   LIMIT 1
),
props AS (
  SELECT (s.components#>>'{FHealthComponent,1,m_CurrentHealth}')::real AS health,
         (s.components ? 'FPowerCircuitElementComponent') AS pwr
    FROM slots s WHERE s.slot_name = 'Actor'
)
SELECT
  '-- Captured from actor_id=${actor_id} on ' || current_date::text
  || E' (via scripts/capture-placeable-defaults.sh).\n'
  || '-- Review live-state fields (m_CurrentHealth, fuel timers, shelter trace)' || E'\n'
  || '-- before applying — the capture preserves whatever state the live actor' || E'\n'
  || '-- happened to be in when read.' || E'\n'
  || 'INSERT INTO dune.ls_solido_class_defaults' || E'\n'
  || '  (class_short_name, full_class_path, default_properties, default_components,' || E'\n'
  || '   has_container_inventory, inventory_type, inventory_max_count, inventory_max_volume,' || E'\n'
  || '   component_name_hash, has_power_circuit, notes,' || E'\n'
  || '   placeables_building_type, is_active)' || E'\n'
  || 'VALUES' || E'\n'
  || '  ('
  || quote_literal('${class_short}') || ','
  || quote_literal(a.class) || ',' || E'\n'
  || '   '
  || quote_literal(jsonb_build_object(
       'default_health', COALESCE(props.health, 100.0)
     )::text) || '::jsonb,' || E'\n'
  || '   '
  || quote_literal(agg.comp::text) || '::jsonb,' || E'\n'
  || '   '
  || CASE WHEN inv.component_name_hash IS NOT NULL THEN 'true' ELSE 'false' END || ','
  || COALESCE(inv.inventory_type::text, 'NULL') || ','
  || COALESCE(inv.max_item_count::text, 'NULL') || ','
  || COALESCE(inv.max_item_volume::text, 'NULL') || ',' || E'\n'
  || '   '
  || COALESCE(inv.component_name_hash::text, 'NULL') || ','
  || CASE WHEN props.pwr THEN 'true' ELSE 'false' END || ',' || E'\n'
  || '   '
  || quote_literal('capture from actor ' || ${actor_id}::text
                   || ' on ' || current_date::text) || ',' || E'\n'
  || '   '
  || COALESCE(quote_literal(pbt.building_type), 'NULL') || ','
  || 'true'
  || ')' || E'\n'
  || 'ON CONFLICT (class_short_name) DO UPDATE SET' || E'\n'
  || '  full_class_path          = EXCLUDED.full_class_path,' || E'\n'
  || '  default_properties       = EXCLUDED.default_properties,' || E'\n'
  || '  default_components       = EXCLUDED.default_components,' || E'\n'
  || '  has_container_inventory  = EXCLUDED.has_container_inventory,' || E'\n'
  || '  inventory_type           = EXCLUDED.inventory_type,' || E'\n'
  || '  inventory_max_count      = EXCLUDED.inventory_max_count,' || E'\n'
  || '  inventory_max_volume     = EXCLUDED.inventory_max_volume,' || E'\n'
  || '  component_name_hash      = EXCLUDED.component_name_hash,' || E'\n'
  || '  has_power_circuit        = EXCLUDED.has_power_circuit,' || E'\n'
  || '  notes                    = EXCLUDED.notes,' || E'\n'
  || '  placeables_building_type = EXCLUDED.placeables_building_type,' || E'\n'
  || '  is_active                = EXCLUDED.is_active;'
  FROM a CROSS JOIN pbt CROSS JOIN agg CROSS JOIN props LEFT JOIN inv ON true;
SQL
)
  dq_raw "$sql"
}

main() {
  [[ $# -ge 1 ]] || usage
  case "$1" in
    -h|--help) usage ;;
    --account)
      shift
      [[ $# -ge 1 ]] || die "--account requires <account_id>"
      local acc="$1" lim=50
      shift
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --limit) shift; [[ $# -ge 1 ]] || die "--limit needs a value"; lim="$1"; shift ;;
          *) die "unexpected arg: $1" ;;
        esac
      done
      cmd_enumerate "$acc" "$lim"
      ;;
    *)
      [[ $# -eq 2 ]] || usage
      cmd_capture "$1" "$2"
      ;;
  esac
}

main "$@"
