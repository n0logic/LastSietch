#!/usr/bin/env bash
# Grant the Sietch Welcome Package to a Dune account.
#
# v1.8 (current): fight gear toned down to Mk3 for new players (owner decision
#                 2026-10-20). Armor -> Slaver Scout (Combat_Choam_Light03_*, T3),
#                 Replica Pulse-sword -> Artisan Sword (CHOAMSword_0, T3), House
#                 Maula Pistol -> Artisan Maula Pistol (ChoamSda4, T3), Holtzman
#                 Shield Mk5 -> Mk3 (HoltzmanShieldActiveDrain2), Power Pack Mk6 ->
#                 Mk3 (PowerPack6), Filter Extractor Mk5 -> Mk3
#                 (BodyFluidExtractor_Unique_Water_03). All of these plus the Medium
#                 Blood Sack now at q0. Ornithopter kit, belt, tools, Solari, Scrip,
#                 Intel and research unchanged. Same 27 main items, same slots, and
#                 every swap has the same item-data volume as the item it replaces.
#                 Named v1.8, not v1.7: "v1.7" is already the online RMQ lane's
#                 stamp (v1.7-rmq) and research-scrip-sweep.sql pays scrip to any
#                 notes LIKE 'v1.7%'. Accounts at v1.0 to v1.7 keep what they got;
#                 there is no backfill and no clawback.
#
# v1.6: adds the blood/water survival kit to MAIN -- Filter Extractor
#                 Mk5 (BodyFluidExtractor_Unique_Water_05, q5) + Medium Blood Sack
#                 (Bloodsack_02, q5). Lets new players harvest water early. Fits
#                 easily: starter backpack cap = 175 vol / 35 slots; +12 vol /
#                 +2 slots (pack now 27 main items). v1.5->v1.6 backfill exists.
#
# v1.5: ranged starter kit in MAIN -- House Maula Pistol Mk5
#                 (ChoamSda5, q5) + 250 Light Darts (Ammo, q0). Pistol matches
#                 the welcome pack's Mk5 convention (Cutteray Mk5, Welding
#                 Torch Mk5, Holtzman Mk5). 250 darts = ~5 mags.
#
# v1.4: Replica Pulse-sword (UniqueSword_05, T6, q5) in MAIN backpack.
#                Confirmed visible in-game on a test account after relog 2026-05-19.
#
# v1.3 layout: 22 main items + 4 hotbar items (decajon prefilled) +
# 7 Base Construction research entries flipped to Purchased + 100k Solari + 100k Scrip + 100 Intel.
#
# Layout (v1.8):
#   MAIN backpack (inventory_type=0): 27 items starting at MAX(position_index)+1
#     - 5 Slaver Scout T3 Light armor pieces (q0): helmet/top/bottom/gloves/boots
#     - Full Suspensor Belt (q0)
#     - PowerPack6 = Power Pack Mk3 (q0)
#     - HoltzmanShieldActiveDrain2 = Holtzman Shield Mk3 (q0)
#     - 10 orni parts (q5): chassis/2 hull/4 wings/engine/generator/thruster
#     - 2x FuelCanister_Large (q0)
#     - 1x weldingmaterial (q0)
#     - 100k SolarisCoin stack (q0)
#     - CHOAMSword_0 = Artisan Sword T3 (q0)
#     - ChoamSda4 = Artisan Maula Pistol T3 (q0)
#     - 250x Ammo = Light Darts (q0, single stack)
#     - BodyFluidExtractor_Unique_Water_03 = Filter Extractor Mk3 (q0)
#     - Bloodsack_02 = Medium Blood Sack (q0)
#   HOTBAR (inventory_type=15): 4 items at default positions 1-4
#     - 1: miningtool_2h_light (Cutteray Mk5)
#     - 2: repairtool5 (Mk5 Welding Torch)
#     - 3: vehiclebackuptool
#     - 4: decajon (pre-filled water 10000; water-only container, no fuel)
#   Currency: 100k House Scrip (currency_id=1), 100 Intel Points
#   Research: 7 Base Construction Kit ItemKeys -> Purchased
#
# Usage:
#   ./grant.sh <account_id>
#   ./grant.sh <account_id> --dry-run

set -euo pipefail

ACCOUNT_ID="${1:?usage: $0 <account_id> [--dry-run]}"
MODE="full"
[[ "${2:-}" == "--dry-run" ]] && MODE="dry"

# One pack per stable identity (fls_id) per cooldown window. The watcher exports
# this; default 30 here covers standalone invocation. Single source of truth is
# the watcher env.
WELCOME_PACK_COOLDOWN_DAYS="${WELCOME_PACK_COOLDOWN_DAYS:-30}"

NS=${LASTSIETCH_GAME_NAMESPACE:?set LASTSIETCH_GAME_NAMESPACE}
POD=${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-db-dbdepl-sts-0

run_psql() {
  sudo kubectl exec -i -n "$NS" "$POD" -- \
    sh -c '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"' _ \
    -h localhost -p 15432 -U postgres -d dune -v ON_ERROR_STOP=1 "$@"
}

echo "=== resolving account $ACCOUNT_ID ==="
LOOKUP_SQL=$(cat <<EOF
SELECT
  eps.account_id,
  eps.player_pawn_id   AS actor_id,
  (SELECT id FROM dune.inventories WHERE actor_id = eps.player_pawn_id AND inventory_type = 0)  AS main_id,
  (SELECT id FROM dune.inventories WHERE actor_id = eps.player_pawn_id AND inventory_type = 15) AS hotbar_id,
  (SELECT granted_at FROM dune.ls_welcome_pack_grants WHERE account_id = $ACCOUNT_ID) AS already_granted_at,
  eps.online_status,
  eps.last_login_time
FROM dune.encrypted_player_state eps
WHERE eps.account_id = $ACCOUNT_ID;
EOF
)
echo "$LOOKUP_SQL" | run_psql || { echo "lookup failed"; exit 1; }

# Resolve to the pawn that actually owns a main backpack (inventory_type=0),
# most-recently-logged-in first. An account can have MULTIPLE encrypted_player_state
# rows -- a stale/orphan pawn stuck Online alongside the real character -- and the
# old `SELECT player_pawn_id ... WHERE account_id` then returned >1 row, which
# `tr -d '[:space:]'` mashed into one bogus id (the 2026-06-24 Thoryn / account
# 16798 grant failure: actors 34643 [orphan, no inventories] + 34646 [real char]
# collapsed to 3464334646 -> "could not resolve inventories").
ACTOR_ID=$(echo "SELECT eps.player_pawn_id
                   FROM dune.encrypted_player_state eps
                  WHERE eps.account_id = $ACCOUNT_ID
                    AND eps.character_state IS DISTINCT FROM 'Deleted'
                    AND EXISTS (SELECT 1 FROM dune.inventories i
                                 WHERE i.actor_id = eps.player_pawn_id AND i.inventory_type = 0)
                  ORDER BY eps.last_login_time DESC
                  LIMIT 1;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
MAIN_ID=$(echo  "SELECT id FROM dune.inventories WHERE actor_id = $ACTOR_ID AND inventory_type = 0;"  | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
HOTBAR_ID=$(echo "SELECT id FROM dune.inventories WHERE actor_id = $ACTOR_ID AND inventory_type = 15;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')

if [[ -z "$ACTOR_ID" || -z "$MAIN_ID" || -z "$HOTBAR_ID" ]]; then
  echo "could not resolve inventories: actor=$ACTOR_ID main=$MAIN_ID hotbar=$HOTBAR_ID"
  exit 1
fi
echo "actor=$ACTOR_ID main=$MAIN_ID hotbar=$HOTBAR_ID mode=$MODE"

# Stable identity for this account. FLS id = dune.accounts."user" (quoted
# reserved word); funcom_id = Display#tag. Stamped on the grant row so dedup is
# identity-based, not account_id-based.
FLS_ID=$(echo "SELECT \"user\" FROM dune.accounts WHERE id = $ACCOUNT_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
FUNCOM_ID=$(echo "SELECT funcom_id FROM dune.accounts WHERE id = $ACCOUNT_ID;" | run_psql -t -A 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
echo "fls_id=${FLS_ID:-<unresolved>} funcom_id=${FUNCOM_ID:-<unresolved>}"
# SQL-escape single quotes before interpolation (display names can contain them).
FUNCOM_ID="${FUNCOM_ID//\'/\'\'}"

# Identity cooldown guard: refuse if this identity already received a pack inside
# the cooldown window under a DIFFERENT account_id (re-roll). Defence in depth;
# the watcher filters too, but a direct grant.sh call must honor the policy.
if [[ -n "$FLS_ID" ]]; then
  IN_COOLDOWN=$(echo "SELECT 1 FROM dune.ls_welcome_pack_grants
                        WHERE fls_id = '$FLS_ID'
                          AND account_id <> $ACCOUNT_ID
                          AND granted_at > now() - (INTERVAL '1 day') * $WELCOME_PACK_COOLDOWN_DAYS
                        LIMIT 1;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
  if [[ "$IN_COOLDOWN" == "1" ]]; then
    echo "identity $FLS_ID already received a welcome pack within ${WELCOME_PACK_COOLDOWN_DAYS}-day cooldown. refusing re-roll grant for account $ACCOUNT_ID."
    exit 0
  fi
fi

# Version gate. Any existing stamp means the account already has a pack, and it
# keeps that pack (owner decision 2026-10-20: no backfill, no clawback). The old
# v1.x upgrade hints are gone on purpose: v1.8 is a step DOWN in fight gear, so
# nothing here may point an older account at it or at the older backfills.
# A cleared stamp (the reroll heal's REGRANT-REROLL-HEAL, or a manual
# REGRANT-INPROGRESS) contains no "v1" and falls through to a fresh v1.8 grant.
NOTES=$(echo "SELECT notes FROM dune.ls_welcome_pack_grants WHERE account_id = $ACCOUNT_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')
if [[ "$NOTES" == *"v1.8"* ]]; then
  echo "account $ACCOUNT_ID already at v1.8. exiting."
  exit 0
fi
if [[ "$NOTES" == *"v1"* ]]; then
  echo "account $ACCOUNT_ID already has the welcome pack (${NOTES%%:*}). it keeps that pack; no v1.8 grant, no backfill. exiting."
  exit 0
fi

NEXT_MAIN=$(echo "SELECT COALESCE(MAX(position_index), -1) + 1 FROM dune.items WHERE inventory_id = $MAIN_ID;" | run_psql -t -A 2>/dev/null | tr -d '[:space:]')

# Place the 4 hotbar tools at the FIRST FREE hotbar slots (0-7) instead of a
# hardcoded 1-4. A brand-new player has an empty bar so they land at 0-3; a
# re-roll player who already has items on 1-4 no longer gets a duplicate-slot
# collision. The hotbar is a fixed 8-slot bar (max_item_count=8, valid pos 0-7);
# if it can't hold all 4, the remainder append to the main backpack after the 27
# main items (backpack over-capacity is a tolerated state). This is what the
# one-off ops/hotbar-dedup cleanup did for the packs granted before this patch.
mapfile -t FREE_HB < <(echo "SELECT g FROM generate_series(0,7) g WHERE g NOT IN (SELECT position_index FROM dune.items WHERE inventory_id = $HOTBAR_ID) ORDER BY g;" | run_psql -t -A 2>/dev/null | grep -E '^[0-9]+$')
_ovf=0
for _i in 0 1 2 3; do
  if [[ $_i -lt ${#FREE_HB[@]} ]]; then
    printf -v "T${_i}_INV" '%s' "$HOTBAR_ID"; printf -v "T${_i}_POS" '%s' "${FREE_HB[$_i]}"
  else
    printf -v "T${_i}_INV" '%s' "$MAIN_ID";   printf -v "T${_i}_POS" '%s' "$((NEXT_MAIN + 27 + _ovf))"; _ovf=$((_ovf+1))
  fi
done

if [[ "$MODE" == "dry" ]]; then
  echo "=== DRY RUN. would INSERT v1.8 contents starting at MAIN pos=$NEXT_MAIN (27 items incl. Slaver Scout armor + Artisan Sword + Artisan Maula Pistol + 250 Light Darts + Holtzman Shield Mk3 + Power Pack Mk3 + Filter Extractor Mk3 + Medium Blood Sack + Scout Ornithopter Mk6 kit), HOTBAR tools at inv:pos ${T0_INV}:${T0_POS} ${T1_INV}:${T1_POS} ${T2_INV}:${T2_POS} ${T3_INV}:${T3_POS} (free hotbar slots: ${FREE_HB[*]:-none}; overflow -> backpack), decajon pre-filled, 7 base-construction research flipped to Purchased. ==="
  exit 0
fi

GRANT_SQL=$(cat <<EOF
BEGIN;

-- Serialize concurrent grants for the same identity (e.g. overlapping watcher
-- invocations racing a re-roll) so two grants cannot interleave past the
-- cooldown guard. Released at COMMIT/ROLLBACK.
SELECT pg_advisory_xact_lock(hashtext('holwp:' || '$FLS_ID'));

-- MAIN BACKPACK: armor + belt + powerpack + Holtzman + orni parts + fuel/wire + SolarisCoin + sword + pistol + darts + water kit
-- v1.8 swapped lines use the engine's canonical CamelCase (what the game itself
-- writes into dune.items, verified live 2026-10-20); the lowercase spellings
-- are BUG-012 and do not stack with the game's own copies.
INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats, quality_level, acquisition_time, is_new) VALUES
  ($MAIN_ID, 1,      $((NEXT_MAIN + 0)),  'Combat_Choam_Light03_Helmet',  '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 1)),  'Combat_Choam_Light03_Top',     '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 2)),  'Combat_Choam_Light03_Bottom',  '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 3)),  'Combat_Choam_Light03_Gloves',  '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 4)),  'Combat_Choam_Light03_Boots',   '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 5)),  'fullsuspensorbelt',            '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 6)),  'PowerPack6',                   '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 7)),  'HoltzmanShieldActiveDrain2',   '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 8)),  'ornithopterlightchassis_6',    '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 9)),  'ornithopterlighthullback_6',   '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 10)), 'ornithopterlighthullfront_6',  '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 11)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 12)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 13)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 14)), 'ornithopterlightlocomotion_6', '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 15)), 'ornithopterlightengine_6',     '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 16)), 'ornithopterlightgenerator_6',  '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 17)), 'ornithopterlightboost_6',      '{}'::jsonb, 5, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 18)), 'FuelCanister_Large',           '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 19)), 'FuelCanister_Large',           '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 20)), 'weldingmaterial',              '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 100000, $((NEXT_MAIN + 21)), 'SolarisCoin', '{"FItemStackAndDurabilityStats": [[], {"DecayedMaxDurability": 0.0}]}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 22)), 'CHOAMSword_0',                 '{}'::jsonb, 0, 0, true),
  -- ranged starter kit (Artisan Maula Pistol + 250 Light Darts)
  ($MAIN_ID, 1,      $((NEXT_MAIN + 23)), 'ChoamSda4',                    '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 250,    $((NEXT_MAIN + 24)), 'Ammo',                         '{}'::jsonb, 0, 0, true),
  -- blood/water survival kit (Filter Extractor Mk3 + Medium Blood Sack)
  ($MAIN_ID, 1,      $((NEXT_MAIN + 25)), 'BodyFluidExtractor_Unique_Water_03', '{}'::jsonb, 0, 0, true),
  ($MAIN_ID, 1,      $((NEXT_MAIN + 26)), 'Bloodsack_02',                 '{}'::jsonb, 0, 0, true);

-- HOTBAR: tools at the first free hotbar slots (0-7); overflow appended to the
-- main backpack (see FREE_HB placement above). Decaliterjon pre-filled 10000
-- WATER (water-only container; the old FFuelContainerStats was wrong — the
-- decajon holds no fuel).
INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats, quality_level, acquisition_time, is_new) VALUES
  ($T0_INV, 1, $T0_POS, 'miningtool_2h_light',  '{}'::jsonb, 0, 0, true),
  ($T1_INV, 1, $T1_POS, 'repairtool5',          '{}'::jsonb, 0, 0, true),
  ($T2_INV, 1, $T2_POS, 'vehiclebackuptool',    '{}'::jsonb, 0, 0, true),
  ($T3_INV, 1, $T3_POS, 'decajon',
    '{"FFillableItemStats": [[], {"FillableType": "Water", "CurrentAmount": 10000.0}], "FItemStackAndDurabilityStats": [[], {}]}'::jsonb,
    0, 0, true);

-- 100,000 Landsraad House Scrip (currency_id=1)
INSERT INTO dune.player_virtual_currency_balances (player_controller_id, currency_id, balance)
SELECT player_controller_id, 'HouseCredit'::dune.virtualwallettype, 100000
  FROM dune.encrypted_player_state WHERE account_id = $ACCOUNT_ID AND player_pawn_id = $ACTOR_ID
ON CONFLICT (player_controller_id, currency_id) DO UPDATE
  SET balance = dune.player_virtual_currency_balances.balance + 100000;

-- 100 Intel Points: NOT applied here. m_TechKnowledgePoints is RAM-resident
-- while the player is online; a DB write at grant time (player just logged in)
-- is clobbered by the game's next actor-save. Intel is instead applied by the
-- watcher's intel_sweep once the account is Offline (intel-sweep.sql), so the
-- game loads DB->RAM on next login. Tracked via ls_welcome_pack_grants.intel_applied_at.

-- PRE-RESEARCH: flip 7 Base Construction Kit ItemKeys to Purchased.
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

INSERT INTO dune.ls_welcome_pack_grants (account_id, fls_id, funcom_id, granted_items, notes, granted_actor_id)
VALUES ($ACCOUNT_ID, NULLIF('$FLS_ID', ''), NULLIF('$FUNCOM_ID', ''), 31, 'v1.8: 27 main (Mk3 fight gear: Slaver Scout armor + Artisan Sword + Artisan Maula Pistol + 250 Light Darts + Holtzman Mk3 + Power Pack Mk3 + Filter Extractor Mk3 + Medium Blood Sack; Scout Ornithopter Mk6 kit) + 4 hotbar (decajon prefilled) + 7 base-construction research + 100k Solari + 100k Scrip + 100 Intel', $ACTOR_ID)
ON CONFLICT (account_id) DO UPDATE
  SET granted_at = NOW(),
      granted_items = dune.ls_welcome_pack_grants.granted_items + EXCLUDED.granted_items,
      notes = EXCLUDED.notes,
      fls_id = COALESCE(dune.ls_welcome_pack_grants.fls_id, EXCLUDED.fls_id),
      funcom_id = COALESCE(dune.ls_welcome_pack_grants.funcom_id, EXCLUDED.funcom_id),
      -- reroll-heal: always point the stamp at the pawn we actually delivered to,
      -- so a later delete of THIS pawn is detectable and a re-grant re-anchors here.
      granted_actor_id = EXCLUDED.granted_actor_id;

COMMIT;

SELECT inv.inventory_type, COUNT(i.id) AS items
  FROM dune.inventories inv LEFT JOIN dune.items i ON i.inventory_id = inv.id
  WHERE inv.id IN ($MAIN_ID, $HOTBAR_ID)
  GROUP BY inv.inventory_type ORDER BY inv.inventory_type;
SELECT * FROM dune.ls_welcome_pack_grants WHERE account_id = $ACCOUNT_ID;
EOF
)

echo "$GRANT_SQL" | run_psql

echo "=== welcome pack v1.8 granted to account $ACCOUNT_ID. ==="
echo "player must fully exit + relog to see new items + Scrip + decajon fill + research."
echo "intel points (+100) are applied separately by the watcher intel_sweep once the account is Offline."
