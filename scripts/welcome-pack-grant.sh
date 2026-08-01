#!/usr/bin/env bash
# =============================================================================
# Dune Awakening - Sietch Welcome Package grant script
# =============================================================================
# Grants a one-time "welcome package" of items, currency and unlocked research
# to a player account on a self-hosted Dune Awakening server, by writing
# directly to the server's PostgreSQL database.
#
# It is idempotent: it records each grant in a tracking table and refuses to
# grant the same pack twice.
#
# -----------------------------------------------------------------------------
# HOW IT WORKS
# -----------------------------------------------------------------------------
# The Funcom self-host server keeps all player/world state in a Postgres DB
# running inside a Kubernetes pod. This script `kubectl exec`s into that pod
# and runs psql. It:
#   1. Resolves an account_id -> player actor -> inventory IDs.
#   2. Checks a tracking table so a pack is never granted twice.
#   3. INSERTs items into the player's backpack + hotbar, adds currency, and
#      flips some research recipes to "Purchased".
#   4. Records the grant in the tracking table.
#
# -----------------------------------------------------------------------------
# PREREQUISITES
# -----------------------------------------------------------------------------
#  - `kubectl` access to the cluster running the Dune server (with `sudo`).
#  - A tracking table in the `dune` database. Create it once with:
#
#      CREATE TABLE dune.welcome_pack_grants (
#        account_id       bigint PRIMARY KEY,
#        granted_at       timestamptz NOT NULL DEFAULT now(),
#        granted_items    integer     NOT NULL DEFAULT 0,
#        notes            text
#      );
#
# -----------------------------------------------------------------------------
# USAGE
# -----------------------------------------------------------------------------
#   ./welcome-pack-grant.sh <account_id>            # grant the pack
#   ./welcome-pack-grant.sh <account_id> --dry-run  # show what would happen
#
# =============================================================================

set -euo pipefail   # abort on any error / unset variable / failed pipe stage

# -----------------------------------------------------------------------------
# CONFIGURE THESE FOR YOUR SERVER  <<< EDIT BEFORE USE >>>
# -----------------------------------------------------------------------------
# Your battlegroup's Kubernetes namespace. Find it with:
#     kubectl get ns | grep funcom
NS="funcom-seabass-<YOUR-BG-NAMESPACE>"

# The Postgres StatefulSet pod inside that namespace. Find it with:
#     kubectl get pods -n "$NS" | grep db-dbdepl
POD="<YOUR-BG-NAME>-db-dbdepl-sts-0"

# Postgres port exposed inside the DB pod (Funcom's self-host default is 15433).
DB_PORT="15433"
# -----------------------------------------------------------------------------

# --- argument parsing --------------------------------------------------------
ACCOUNT_ID="${1:?usage: $0 <account_id> [--dry-run]}"   # required: the target account
MODE="full"
[[ "${2:-}" == "--dry-run" ]] && MODE="dry"             # optional: preview only

# --- run_psql: execute SQL (from stdin) inside the DB pod --------------------
# Pulls the DB password from the pod's own environment, then pipes psql in.
# Pass extra psql flags as arguments, e.g. `run_psql -t -A` for raw scalar output.
run_psql() {
  local extra_args="$*"
  bash -c "
    PGPASS=\$(sudo kubectl exec -n $NS $POD -- printenv POSTGRES_PASSWORD)
    sudo kubectl exec -i -n $NS $POD -- env PGPASSWORD=\$PGPASS \
      psql -h localhost -p $DB_PORT -U postgres -d dune -v ON_ERROR_STOP=1 $extra_args
  "
}

# --- STEP 1: resolve the account --------------------------------------------
# Show a human-readable summary of the account: its actor, inventories,
# whether it was already granted, and its online status.
echo "=== resolving account $ACCOUNT_ID ==="
LOOKUP_SQL=$(cat <<EOF
SELECT
  eps.account_id,
  eps.player_pawn_id   AS actor_id,
  (SELECT id FROM dune.inventories WHERE actor_id = eps.player_pawn_id AND inventory_type = 0)  AS main_id,
  (SELECT id FROM dune.inventories WHERE actor_id = eps.player_pawn_id AND inventory_type = 15) AS hotbar_id,
  (SELECT granted_at FROM dune.welcome_pack_grants WHERE account_id = $ACCOUNT_ID) AS already_granted_at,
  eps.online_status,
  eps.last_login_time
FROM dune.encrypted_player_state eps
WHERE eps.account_id = $ACCOUNT_ID;
EOF
)
echo "$LOOKUP_SQL" | run_psql || { echo "lookup failed"; exit 1; }

# Grab the actor id and the two inventory ids we will write into.
#   inventory_type 0  = MAIN backpack
#   inventory_type 15 = HOTBAR
ACTOR_ID=$(echo "SELECT player_pawn_id FROM dune.encrypted_player_state WHERE account_id = $ACCOUNT_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
MAIN_ID=$(echo  "SELECT id FROM dune.inventories WHERE actor_id = $ACTOR_ID AND inventory_type = 0;"  | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
HOTBAR_ID=$(echo "SELECT id FROM dune.inventories WHERE actor_id = $ACTOR_ID AND inventory_type = 15;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')

if [[ -z "$ACTOR_ID" || -z "$MAIN_ID" || -z "$HOTBAR_ID" ]]; then
  echo "could not resolve inventories: actor=$ACTOR_ID main=$MAIN_ID hotbar=$HOTBAR_ID"
  exit 1
fi
echo "actor=$ACTOR_ID main=$MAIN_ID hotbar=$HOTBAR_ID mode=$MODE"

# --- STEP 2: idempotency gate ------------------------------------------------
# The tracking table's `notes` column carries a version tag. If this account
# already has a pack, do not grant again. (Version tags shown here are an
# example scheme - adjust to your own.)
NOTES=$(echo "SELECT notes FROM dune.welcome_pack_grants WHERE account_id = $ACCOUNT_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
if [[ "$NOTES" == *"v1.4"* ]]; then
  echo "account $ACCOUNT_ID already has the current pack. exiting."
  exit 0
fi
# Older-pack accounts: upgrades are handled by a separate backfill script,
# not granted automatically here, to avoid duplicate items.
if [[ "$NOTES" == *"v1"* ]]; then
  echo "account $ACCOUNT_ID has an older pack ($NOTES)."
  echo "Run your upgrade/backfill script instead of this grant script."
  exit 0
fi

# Find the next free slot in the MAIN backpack. New items are always appended
# at MAX(position_index)+1 so they never collide with the player's own items.
# (Dune's items table allows duplicate (inventory_id, position_index) rows and
# the client only renders the lowest-id row per slot - appending avoids that.)
NEXT_MAIN=$(echo "SELECT COALESCE(MAX(position_index), -1) + 1 FROM dune.items WHERE inventory_id = $MAIN_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')

# --- STEP 3 (dry-run): preview and exit -------------------------------------
if [[ "$MODE" == "dry" ]]; then
  echo "=== DRY RUN. would INSERT the pack starting at MAIN pos=$NEXT_MAIN"
  echo "    (23 backpack items), HOTBAR positions 1-4 (decajon pre-filled),"
  echo "    currency, and flip the base-construction research to Purchased. ==="
  exit 0
fi

# --- STEP 4: the grant (single transaction) ---------------------------------
# Everything below runs as one BEGIN/COMMIT so a failure leaves no partial pack.
#
# Per-item columns:
#   stack_size       quantity in the stack (1 for gear, 100000 for the coin stack)
#   position_index   slot; backpack items use NEXT_MAIN+offset, hotbar uses 1-4
#   template_id      the game's item identifier (case-insensitive in practice)
#   stats            jsonb of item stats; '{}' = defaults
#   quality_level    item quality tier (0 = base, 5 = top tier here)
#   acquisition_time 0 marks this as an admin/script grant (vs a real timestamp)
#   is_new           shows the "new item" highlight in the UI
GRANT_SQL=$(cat <<EOF
BEGIN;

-- MAIN BACKPACK -------------------------------------------------------------
-- 5 armor pieces + suspensor belt + power pack + Holtzman shield +
-- 10 ornithopter parts + fuel/welding mats + a 100k coin stack + a unique sword.
INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats, quality_level, acquisition_time, is_new) VALUES
  ($MAIN_ID, 1,      $((NEXT_MAIN + 0)),  'combat_choam_light06_helmet',  '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 1)),  'combat_choam_light06_top',     '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 2)),  'combat_choam_light06_bottom',  '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 3)),  'combat_choam_light06_gloves',  '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 4)),  'combat_choam_light06_boots',   '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 5)),  'fullsuspensorbelt',            '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 6)),  'powerpack4',                   '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 7)),  'holtzmanshieldactivedrain3',   '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 8)),  'ornithopterlightchassis_6',    '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 9)),  'ornithopterlighthullback_6',   '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 10)), 'ornithopterlighthullfront_6',  '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 11)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),  -- wing 1/4
  ($MAIN_ID, 1,      $((NEXT_MAIN + 12)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),  -- wing 2/4
  ($MAIN_ID, 1,      $((NEXT_MAIN + 13)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),  -- wing 3/4
  ($MAIN_ID, 1,      $((NEXT_MAIN + 14)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),  -- wing 4/4
  ($MAIN_ID, 1,      $((NEXT_MAIN + 15)), 'ornithopterlightengine_6',     '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 16)), 'ornithopterlightgenerator_6',  '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 17)), 'ornithopterlightboost_6',      '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 18)), 'FuelCanister_Large',           '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 19)), 'FuelCanister_Large',           '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 20)), 'weldingmaterial',              '{}'::jsonb, 0, 0, true),
  -- 100,000-coin stack. The stats jsonb gives it a durability sub-struct.
  ($MAIN_ID, 100000, $((NEXT_MAIN + 21)), 'SolarisCoin', '{"FItemStackAndDurabilityStats": [[], {"DecayedMaxDurability": 0.0}]}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 22)), 'UniqueSword_05',               '{}'::jsonb, 5, 0, true);  -- unique long blade

-- HOTBAR --------------------------------------------------------------------
-- Tools at fixed positions 1-4. The decajon (canteen) is pre-filled by writing
-- its stats jsonb directly: 5000 water + 10000 fuel.
INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats, quality_level, acquisition_time, is_new) VALUES
  ($HOTBAR_ID, 1, 1, 'miningtool_2h_light',  '{}'::jsonb, 0, 0, true),
  ($HOTBAR_ID, 1, 2, 'repairtool5',          '{}'::jsonb, 0, 0, true),
  ($HOTBAR_ID, 1, 3, 'vehiclebackuptool',    '{}'::jsonb, 0, 0, true),
  ($HOTBAR_ID, 1, 4, 'decajon',
    '{"FFillableItemStats": [[], {"FillableType": "Water", "CurrentAmount": 5000.0}], "FFuelContainerStats": [[], {"CurrentFuelAmount": 10000.0}], "FItemStackAndDurabilityStats": [[], {}]}'::jsonb,
    0, 0, true);

-- CURRENCY ------------------------------------------------------------------
-- Add 100,000 Landsraad House Scrip. currency_id=1 is House Scrip; the virtual
-- currency table is keyed by the player's controller actor, not the account.
-- UPSERT: add to the balance if a row already exists.
INSERT INTO dune.player_virtual_currency_balances (player_controller_id, currency_id, balance)
SELECT player_controller_id, 1, 100000
  FROM dune.encrypted_player_state WHERE account_id = $ACCOUNT_ID
ON CONFLICT (player_controller_id, currency_id) DO UPDATE
  SET balance = dune.player_virtual_currency_balances.balance + 100000;

-- PRE-RESEARCH --------------------------------------------------------------
-- Flip a set of base-construction recipes to "Purchased" so new players can
-- build immediately. Research lives as a jsonb array on the player actor;
-- this rebuilds that array, setting UnlockedState=Purchased for the named keys.
WITH new_arr AS (
  SELECT jsonb_agg(
    CASE WHEN t.elem->>'ItemKey' IN (
      'BLD_BasicLighting_Patent','BLD_ChoamShelterSet','BLD_PowerGenerator_Patent',
      'BLD_SpiceSilo_Patent','BLD_Totem_Small_Patent','DA_GRP_ConstructionStarterPack',
      'RCP_BuildingDroneRecipe'
    ) THEN jsonb_set(jsonb_set(t.elem, '{UnlockedState}', '"Purchased"'), '{bIsNewEntry}', 'false')
    ELSE t.elem END
    ORDER BY t.idx
  ) AS arr
  FROM dune.actors a
  CROSS JOIN LATERAL jsonb_array_elements(a.properties#>'{TechKnowledgePlayerComponent,m_TechKnowledge,m_TechKnowledgeData}') WITH ORDINALITY AS t(elem, idx)
  WHERE a.id = $ACTOR_ID
)
UPDATE dune.actors
SET properties = jsonb_set(
  properties,
  '{TechKnowledgePlayerComponent,m_TechKnowledge,m_TechKnowledgeData}',
  COALESCE(new_arr.arr, properties#>'{TechKnowledgePlayerComponent,m_TechKnowledge,m_TechKnowledgeData}')
)
FROM new_arr
WHERE dune.actors.id = $ACTOR_ID;

-- TRACKING ------------------------------------------------------------------
-- Record the grant so this account is never granted again. The `notes` string
-- doubles as the version tag the idempotency gate above reads.
INSERT INTO dune.welcome_pack_grants (account_id, granted_items, notes)
VALUES ($ACCOUNT_ID, 27, 'v1.4: 23 backpack + 4 hotbar + 7 research + 100k coin + 100k scrip')
ON CONFLICT (account_id) DO UPDATE
  SET granted_at = NOW(),
      granted_items = dune.welcome_pack_grants.granted_items + EXCLUDED.granted_items,
      notes = EXCLUDED.notes;

COMMIT;

-- Post-grant sanity check: item counts per inventory, and the tracking row.
SELECT inv.inventory_type, COUNT(i.id) AS items
  FROM dune.inventories inv LEFT JOIN dune.items i ON i.inventory_id = inv.id
  WHERE inv.id IN ($MAIN_ID, $HOTBAR_ID)
  GROUP BY inv.inventory_type ORDER BY inv.inventory_type;
SELECT * FROM dune.welcome_pack_grants WHERE account_id = $ACCOUNT_ID;
EOF
)

echo "$GRANT_SQL" | run_psql

echo "=== welcome pack granted to account $ACCOUNT_ID. ==="
echo "the player must fully exit and relog to see the new items, currency,"
echo "decajon fill and research."
