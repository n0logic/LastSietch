#!/usr/bin/env bash
# Welcome-pack v1.5 -> v1.6 backfill: add the v1.6 blood/water survival kit to
# MAIN backpack for accounts that already received v1.5 but missed the new items.
#
# v1.6 delta: + BodyFluidExtractor_Unique_Water_05 (Filter Extractor Mk5, q5)
#             + Bloodsack_02 (Medium Blood Sack, q5)
#
# Fits the starter backpack easily (cap 175 vol / 35 slots; +12 vol / +2 slots).
#
# Idempotent: skips accounts whose notes already contain "v1.6". Refuses to run
# on accounts that aren't at v1.5 (run the main grant.sh / v1.5-backfill.sh first).
#
# Usage:
#   ./v1.6-backfill.sh <account_id>
#   ./v1.6-backfill.sh <account_id> --dry-run

set -euo pipefail

ACCOUNT_ID="${1:?usage: $0 <account_id> [--dry-run]}"
MODE="full"
[[ "${2:-}" == "--dry-run" ]] && MODE="dry"

NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
POD="${POD:-sh-<your-hostid>-<random>-db-dbdepl-sts-0}"

run_psql() {
  local extra_args="$*"
  bash -c "
    PGPASS=\$(sudo kubectl exec -n $NS $POD -- printenv POSTGRES_PASSWORD)
    sudo kubectl exec -i -n $NS $POD -- env PGPASSWORD=\$PGPASS psql -h localhost -p 15432 -U postgres -d dune -v ON_ERROR_STOP=1 $extra_args
  "
}

echo "=== v1.6 backfill: account $ACCOUNT_ID ==="

NOTES=$(echo "SELECT notes FROM dune.ls_welcome_pack_grants WHERE account_id = $ACCOUNT_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
if [[ -z "$NOTES" ]]; then
  echo "account $ACCOUNT_ID has NO welcome-pack grant. Skipping (run grant.sh first)."
  exit 0
fi
if [[ "$NOTES" == *"v1.6"* ]]; then
  echo "account $ACCOUNT_ID already at v1.6. Skipping."
  exit 0
fi
if [[ "$NOTES" != *"v1.5"* ]]; then
  echo "account $ACCOUNT_ID is not at v1.5 (notes='$NOTES'). Run the main grant.sh / v1.5-backfill.sh first."
  exit 1
fi

ACTOR_ID=$(echo "SELECT player_pawn_id FROM dune.encrypted_player_state WHERE account_id = $ACCOUNT_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
MAIN_ID=$(echo  "SELECT id FROM dune.inventories WHERE actor_id = $ACTOR_ID AND inventory_type = 0;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')

if [[ -z "$ACTOR_ID" || -z "$MAIN_ID" ]]; then
  echo "could not resolve actor/main inventory for account $ACCOUNT_ID"
  exit 1
fi
echo "actor=$ACTOR_ID main=$MAIN_ID mode=$MODE"

NEXT_MAIN=$(echo "SELECT COALESCE(MAX(position_index), -1) + 1 FROM dune.items WHERE inventory_id = $MAIN_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')

if [[ "$MODE" == "dry" ]]; then
  echo "=== DRY RUN. would INSERT v1.6 backfill at MAIN pos=$NEXT_MAIN: BodyFluidExtractor_Unique_Water_05 (Filter Extractor Mk5, q5) + Bloodsack_02 (Medium Blood Sack, q5). would bump notes to v1.6. ==="
  exit 0
fi

BACKFILL_SQL=$(cat <<EOF
BEGIN;

-- v1.6 blood/water survival kit (Filter Extractor Mk5 + Medium Blood Sack).
INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats, quality_level, acquisition_time, is_new) VALUES
  ($MAIN_ID, 1, $((NEXT_MAIN + 0)), 'BodyFluidExtractor_Unique_Water_05', '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1, $((NEXT_MAIN + 1)), 'Bloodsack_02',                       '{}'::jsonb, 5, 0, true);

-- Bump granted_items by 2 and replace notes with the v1.6 string.
INSERT INTO dune.ls_welcome_pack_grants (account_id, granted_items, notes)
VALUES ($ACCOUNT_ID, 2, 'v1.6: 27 main (armor + Replica Pulse-sword + Maula Pistol Mk5 + 250 Light Darts + Filter Extractor Mk5 + Medium Blood Sack) + 4 hotbar (decajon prefilled) + 7 base-construction research + 100k Solari + 100k Scrip + 100 Intel')
ON CONFLICT (account_id) DO UPDATE
  SET granted_at = NOW(),
      granted_items = dune.ls_welcome_pack_grants.granted_items + EXCLUDED.granted_items,
      notes = EXCLUDED.notes;

COMMIT;

SELECT 'after:' AS marker, account_id, granted_items, notes
  FROM dune.ls_welcome_pack_grants WHERE account_id = $ACCOUNT_ID;
EOF
)

echo "$BACKFILL_SQL" | run_psql

echo "=== v1.6 backfill applied to account $ACCOUNT_ID. ==="
echo "player must fully exit + relog to see the Filter Extractor Mk5 + Medium Blood Sack in MAIN backpack."
