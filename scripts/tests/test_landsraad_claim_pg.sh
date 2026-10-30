#!/usr/bin/env bash
# Integration test for the portal Landsraad CLAIM writer (scripts/dune-landsraad-claim.py).
#
# EXECUTES the real transaction against a throwaway postgres whose schema and native procs
# were dumped from the live game DB (fixtures/landsraad_claim_schema.sql). The SQL under
# test is pulled from the writer's own --print-sql, so a change to the writer that breaks
# a rule breaks this test.
#
# The CANARY section at the end re-runs chosen cases against deliberately broken SQL and
# requires them to FAIL (Working Rule 4: a test nobody has watched fail is not a test).
#
# Requires: docker, python3. Run on <web-host> (the WSL box has no docker).
#   ./scripts/tests/test_landsraad_claim_pg.sh
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
WRITER="$REPO/scripts/dune-landsraad-claim.py"
SCHEMA="$REPO/scripts/tests/fixtures/landsraad_claim_schema.sql"
PAK="$REPO/admin-backend/data/dune-item-pak-meta.json"   # deployed beside the writer
IMAGE="${PG_IMAGE:-postgres:17.4-alpine}"   # live game DB is 17.4
CID=""
PASS=0
FAIL=0

cleanup() { [[ -z "$CID" ]] || docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [[ -z "${2:-}" ]] || printf '        %s\n' "$2"; }
head_() { printf '\n== %s ==\n' "$1"; }
psqlq() { docker exec -i "$CID" psql -U postgres -d dune -v ON_ERROR_STOP=1 -tA "$@"; }
q()     { psqlq -c "$1" 2>/dev/null | tr -d '[:space:]'; }
expect() { # label expected actual
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "expected [$2] got [$3]"; fi
}

ACCT=1644
CTRL=3041
PAWN=3042
BP=7000
EXCH=610

# Shapes copied from live rows (2026-10-06): the pawn carries the unlock list under
# CustomizationLibraryActorComponent exactly like this on all 205 live characters.
seed() { # $1 = backpack max_item_count
  psqlq <<EOF >/dev/null
TRUNCATE dune.landsraad_house_rewards, dune.ls_item_delivery_log, dune.dune_exchange_fulfilled_orders,
         dune.dune_exchange_orders, dune.items, dune.inventories, dune.encrypted_player_state,
         dune.actors, dune.encrypted_accounts, dune.farm_variables CASCADE;
INSERT INTO dune.encrypted_accounts (id, "user", encrypted_funcom_id) VALUES ($ACCT, 'FLSHEX0001', convert_to('tester#1', 'UTF8'));
INSERT INTO dune.actors (id, class, properties) VALUES
  ($CTRL, '/Script/DuneSandbox.DunePlayerController', '{}'),
  ($PAWN, '/Game/Dune/Characters/Player/BP_DunePlayerCharacter.BP_DunePlayerCharacter_C',
   '{"CustomizationLibraryActorComponent": {"m_UnlockedCustomizationSerializableList": {"m_UnlockedCustomizationIds": [{"m_CustomizationId": "MTX_Smug_Formal01_Top"}, {"m_CustomizationId": "PlaceableDyePackImota"}]}}}');
INSERT INTO dune.encrypted_player_state (id, account_id, encrypted_character_name, player_controller_id, player_pawn_id,
  online_status, last_login_time, character_state)
  VALUES (1, $ACCT, '\\x00', $CTRL, $PAWN, 'Offline', now() - interval '1 hour', 'Active');
INSERT INTO dune.inventories (id, actor_id, inventory_type, max_item_count, max_item_volume) VALUES ($BP, $PAWN, 0, $1, 175);
INSERT INTO dune.actors (id, class) VALUES (9999, 'exchange');
INSERT INTO dune.inventories (id, actor_id, inventory_type, max_item_count) VALUES ($EXCH, 9999, 2, 0);
INSERT INTO dune.farm_variables (universe_time_timestamp, down_time_accumulation) VALUES (now() - interval '300 days', 0);
SELECT setval('dune.items_id_seq', 100000);
EOF
}
reward() { psqlq -c "INSERT INTO dune.landsraad_house_rewards (player_id, house_name, template_id, amount) VALUES ($CTRL, '$1', '$2', $3);" >/dev/null; }
item()   { psqlq -c "INSERT INTO dune.items (id, inventory_id, stack_size, position_index, template_id, stats) VALUES (nextval('dune.items_id_seq'), $1, $2, $3, '$4', '{}');" >/dev/null; }

CTX='{"stacks": {"T6GunPart": 500, "T6BladePart": 500, "UniqueSword_05_Schematic": 1, "RepairTool5": 1},
      "cats": {"T6GunPart": [1048832, 2], "T6BladePart": [1048832, 2], "UniqueSword_05_Schematic": [2101504, 3]}}'

claim_sql() { # mode [--swatches]
  python3 "$WRITER" --owner-ctrl "$CTRL" --mode "$1" --print-sql --context-json "$CTX" --pak-meta "$PAK" ${2:-}
}
run() { # sql on stdin -> stdout json line or error text
  docker exec -i "$CID" psql -U postgres -d dune -v ON_ERROR_STOP=1 -tA 2>&1 | grep -E '^\{|ERROR' | tail -1
}

# --- start postgres ---------------------------------------------------------
command -v docker >/dev/null || { echo "docker required (run on <web-host>)"; exit 2; }
CID=$(docker run -d -e POSTGRES_PASSWORD=x -e POSTGRES_DB=dune "$IMAGE")
for _ in $(seq 1 30); do docker exec "$CID" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
psqlq -q < "$SCHEMA" >/dev/null || { echo "schema load failed"; exit 2; }

# --- cases ------------------------------------------------------------------
head_ "everything fits in the backpack"
seed 10
reward DA_HouseArgosaz T6GunPart 40
reward DA_HouseArgosaz SolarisCoin 50000
reward DA_HouseTseida UniqueSword_05_Schematic 1
out=$(claim_sql apply | run)
expect "writer reports ok" "true" "$(echo "$out" | python3 -c 'import json,sys;print(str(json.load(sys.stdin)["ok"]).lower())' 2>/dev/null)"
expect "ledger fully withdrawn by the native proc" "0" "$(q "SELECT sum(amount) FROM dune.landsraad_house_rewards")"
expect "ledger rows kept at 0, not deleted (game semantics)" "3" "$(q "SELECT count(*) FROM dune.landsraad_house_rewards")"
expect "3 stacks in the backpack" "3" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"
expect "all q0" "0" "$(q "SELECT max(quality_level) FROM dune.items WHERE inventory_id=$BP")"
expect "Solari stack carries the coin stats shape" "1" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP AND template_id='SolarisCoin' AND stats ? 'FItemStackAndDurabilityStats'")"
expect "no exchange orders" "0" "$(q "SELECT count(*) FROM dune.dune_exchange_orders")"
expect "delivery log: 3 db/delivered rows" "3" "$(q "SELECT count(*) FROM dune.ls_item_delivery_log WHERE lane='db' AND status='delivered' AND source='landsraad_claim'")"

head_ "a second claim finds nothing"
out=$(claim_sql apply | run)
expect "withdrawn_lines 0" "0" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["withdrawn_lines"])' 2>/dev/null)"
expect "still 3 stacks" "3" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"

head_ "overflow goes to the exchange, backpack-only overflow stays at the rep"
seed 5            # 5 slots, 2 kept free -> 3 usable
item $BP 1 0 Bandage
reward DA_HouseArgosaz T6GunPart 1200        # 3 stacks of 500/500/200
reward DA_HouseArgosaz T6BladePart 10
reward DA_HouseHagal RepairTool5 1           # no exchange category
out=$(claim_sql apply | run)
expect "writer reports ok" "true" "$(echo "$out" | python3 -c 'import json,sys;print(str(json.load(sys.stdin)["ok"]).lower())' 2>/dev/null)"
expect "backpack used up to the safety margin (1 pre-existing + 2 new)" "3" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"
expect "RepairTool5 got a slot before the exchange-capable lines" "1" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP AND template_id='RepairTool5'")"
expect "exchange orders created for the rest" "3" "$(q "SELECT count(*) FROM dune.dune_exchange_orders")"
expect "every order is a Completed/CANCELED row" "3" "$(q "SELECT count(*) FROM dune.dune_exchange_fulfilled_orders WHERE completion_type=3")"
expect "no NULL expiry (a NULL does not render)" "0" "$(q "SELECT count(*) FROM dune.dune_exchange_orders WHERE expiration_time IS NULL")"
expect "orders owned by the controller" "3" "$(q "SELECT count(*) FROM dune.dune_exchange_orders WHERE owner_id=$CTRL")"
expect "stacks never exceed the observed max" "500" "$(q "SELECT max(stack_size) FROM dune.items WHERE template_id='T6GunPart'")"
expect "1210 parts delivered in total" "1210" "$(q "SELECT sum(stack_size) FROM dune.items WHERE template_id IN ('T6GunPart','T6BladePart')")"
expect "ledger fully withdrawn" "0" "$(q "SELECT sum(amount) FROM dune.landsraad_house_rewards")"

head_ "backpack-only line with no room is left at the rep, not lost"
seed 2            # 2 slots, both kept free -> 0 usable
reward DA_HouseHagal RepairTool5 1
reward DA_HouseArgosaz T6GunPart 5
out=$(claim_sql apply | run)
expect "RepairTool5 still owed in the ledger" "1" "$(q "SELECT amount FROM dune.landsraad_house_rewards WHERE template_id='RepairTool5'")"
expect "the exchange-capable line was still claimed" "0" "$(q "SELECT amount FROM dune.landsraad_house_rewards WHERE template_id='T6GunPart'")"
expect "receipt says it stayed at the rep" "rep:no_room" "$(echo "$out" | python3 -c 'import json,sys;print(",".join(l["lane"]+":"+str(l["note"]) for l in json.load(sys.stdin)["lines"] if l["template"]=="RepairTool5"))' 2>/dev/null)"

head_ "Solari merges into a stack already in the bag"
seed 10
item $BP 30000 4 SolarisCoin
reward DA_HouseArgosaz SolarisCoin 80000
claim_sql apply | run >/dev/null
expect "existing stack topped up to the cap" "100000" "$(q "SELECT max(stack_size) FROM dune.items WHERE template_id='SolarisCoin'")"
expect "remainder in one new stack" "110000" "$(q "SELECT sum(stack_size) FROM dune.items WHERE template_id='SolarisCoin'")"
expect "two Solari stacks" "2" "$(q "SELECT count(*) FROM dune.items WHERE template_id='SolarisCoin'")"

head_ "online player: apply refused, nothing changes"
seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq -c "UPDATE dune.encrypted_player_state SET online_status='Online'" >/dev/null
out=$(claim_sql apply | run)
expect "refused by the START gate (before any write)" "1" "$(echo "$out" | grep -c "player_online (status=")"
expect "ledger untouched" "40" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"
expect "backpack untouched" "0" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"

head_ "LoggingOut and reconnect grace both count as online"
psqlq -c "UPDATE dune.encrypted_player_state SET online_status='LoggingOut'" >/dev/null
expect "LoggingOut refused" "1" "$(claim_sql apply | run | grep -c player_online)"
psqlq -c "UPDATE dune.encrypted_player_state SET online_status='Offline', reconnect_grace_period_end=now() + interval '5 minutes'" >/dev/null
expect "inside grace refused" "1" "$(claim_sql apply | run | grep -c player_online)"

head_ "login racing the claim rolls everything back"
seed 10
reward DA_HouseArgosaz T6GunPart 40
# test-only trigger: the moment the writer logs a delivery, the player "logs in"
psqlq >/dev/null <<'EOF'
CREATE OR REPLACE FUNCTION dune._test_login() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN UPDATE dune.encrypted_player_state SET online_status = 'Online'; RETURN NEW; END $$;
CREATE TRIGGER _test_login AFTER INSERT ON dune.ls_item_delivery_log FOR EACH ROW EXECUTE FUNCTION dune._test_login();
EOF
out=$(claim_sql apply | run)
expect "player_online at commit" "1" "$(echo "$out" | grep -c 'player_online at commit')"
expect "ledger untouched" "40" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"
expect "backpack untouched" "0" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"
psqlq -c "DROP TRIGGER _test_login ON dune.ls_item_delivery_log" >/dev/null

head_ "preview runs the real plan and persists nothing, even while online"
seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq -c "UPDATE dune.encrypted_player_state SET online_status='Online'" >/dev/null
out=$(claim_sql preview | run)
expect "preview reports online" "true" "$(echo "$out" | python3 -c 'import json,sys;print(str(json.load(sys.stdin)["online"]).lower())' 2>/dev/null)"
expect "preview plans a backpack line" "backpack" "$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["lines"][0]["lane"])' 2>/dev/null)"
expect "ledger untouched" "40" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"
expect "backpack untouched" "0" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"
expect "no delivery log rows" "0" "$(q "SELECT count(*) FROM dune.ls_item_delivery_log")"

head_ "swatches"
seed 10
reward DA_HouseEcaz Ecaz_Placeables_Swatch 1
reward DA_HouseImota Imota_Placeables_Swatch 1
reward DA_HouseHagal Hagal_LightArmor_Swatch 1
claim_sql apply | run >/dev/null
expect "flag off: swatches stay at the rep" "3" "$(q "SELECT sum(amount) FROM dune.landsraad_house_rewards")"
out=$(claim_sql apply --swatches | run)
expect "flag on: all withdrawn" "0" "$(q "SELECT sum(amount) FROM dune.landsraad_house_rewards")"
expect "Ecaz placeable unlock appended" "1" "$(q "SELECT count(*) FROM dune.actors a, jsonb_array_elements(a.properties#>'{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}') e WHERE a.id=$PAWN AND e->>'m_CustomizationId'='PlaceableDyePackEcaz'")"
expect "Hagal light armor unlock appended" "1" "$(q "SELECT count(*) FROM dune.actors a, jsonb_array_elements(a.properties#>'{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}') e WHERE a.id=$PAWN AND e->>'m_CustomizationId'='LArmCharDyepackHagal'")"
expect "already-owned Imota not duplicated" "1" "$(q "SELECT count(*) FROM dune.actors a, jsonb_array_elements(a.properties#>'{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}') e WHERE a.id=$PAWN AND e->>'m_CustomizationId'='PlaceableDyePackImota'")"
expect "existing unlocks preserved" "1" "$(q "SELECT count(*) FROM dune.actors a, jsonb_array_elements(a.properties#>'{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}') e WHERE a.id=$PAWN AND e->>'m_CustomizationId'='MTX_Smug_Formal01_Top'")"

head_ "swatch ids: game spellings override, unknown ids stay at the rep"
seed 10
reward DA_HouseTaligari Taligari_Stillsuit_Swatch 1
reward DA_HouseWayku Wakyu_Placeables_Swatch 1
reward DA_HouseHagal Hagal_Placeables_Swatch 1
out=$(claim_sql apply --swatches | run)
ul() { q "SELECT count(*) FROM dune.actors a, jsonb_array_elements(a.properties#>'{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}') e WHERE a.id=$PAWN AND e->>'m_CustomizationId'='$1'"; }
expect "Taligari stillsuit writes the game's Talgari id" "1" "$(ul StillSCharDyepackTalgari)"
expect "never the naive Taligari id" "0" "$(ul StillSCharDyepackTaligari)"
expect "Hagal placeable (T3-confirmed) unlocked" "1" "$(ul PlaceableDyePackHagal)"
expect "unknown Wakyu id never written" "0" "$(ul PlaceableDyePackWakyu)"
expect "unknown Wakyu line stays at the rep" "1" "$(q "SELECT amount FROM dune.landsraad_house_rewards WHERE template_id='Wakyu_Placeables_Swatch'")"
expect "unknown Wakyu reported as swatch_unmapped" "1" "$(echo "$out" | grep -c swatch_unmapped)"

head_ "a pawn without the unlock component fails loudly, not silently"
seed 10
reward DA_HouseEcaz Ecaz_Placeables_Swatch 1
reward DA_HouseArgosaz T6GunPart 5
psqlq -c "UPDATE dune.actors SET properties='{}' WHERE id=$PAWN" >/dev/null
out=$(claim_sql apply --swatches | run)
expect "unlock_write_failed" "1" "$(echo "$out" | grep -c unlock_write_failed)"
expect "whole claim rolled back" "6" "$(q "SELECT sum(amount) FROM dune.landsraad_house_rewards")"

head_ "the native proc rejecting a line rolls the claim back"
seed 10
reward DA_HouseArgosaz T6GunPart 40
# inflate what the writer will withdraw: a line it delivers in full while the ledger holds less
out=$(claim_sql apply | sed "s/v_withdraw := v_withdraw$/v_withdraw := v_withdraw || ROW('DA_HouseArgosaz','T6GunPart',1)::dune.landsraadplayerhousereward/" | run)
expect "withdraw rejected" "1" "$(echo "$out" | grep -ci 'cannot withdraw house reward')"
expect "backpack untouched" "0" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"


head_ "logoff persistence: the pawn is still live in the partition, apply refused"
seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq -c "UPDATE dune.encrypted_player_state SET logoff_persistence_end_time=(now() AT TIME ZONE 'UTC') + interval '4 minutes'" >/dev/null
expect "inside logoff persistence refused" "1" "$(claim_sql apply | run | grep -c player_online)"
expect "ledger untouched" "40" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"
expect "preview reports online inside logoff persistence" "true" "$(claim_sql preview | run | python3 -c 'import json,sys;print(str(json.load(sys.stdin)["online"]).lower())' 2>/dev/null)"
psqlq -c "UPDATE dune.encrypted_player_state SET logoff_persistence_end_time=(now() AT TIME ZONE 'UTC') - interval '1 minute'" >/dev/null
claim_sql apply | run >/dev/null
expect "past logoff persistence the claim goes through" "0" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"

head_ "the timers are UTC, whatever the session time zone"
seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq -c "UPDATE dune.encrypted_player_state SET reconnect_grace_period_end=(now() AT TIME ZONE 'UTC') + interval '5 minutes'" >/dev/null
expect "grace still refused with session TZ +9" "1" "$( { echo "SET TIME ZONE 'Asia/Tokyo';"; claim_sql apply; } | run | grep -c player_online)"
psqlq -c "UPDATE dune.encrypted_player_state SET reconnect_grace_period_end=NULL, logoff_persistence_end_time=(now() AT TIME ZONE 'UTC') + interval '5 minutes'" >/dev/null
expect "logoff persistence still refused with session TZ +9" "1" "$( { echo "SET TIME ZONE 'Asia/Tokyo';"; claim_sql apply; } | run | grep -c player_online)"
expect "ledger untouched" "40" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"

head_ "another character on the same account is online: apply refused"
seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq -c "INSERT INTO dune.actors (id, class) VALUES (3051, 'ctrl'), (3052, 'pawn');
          INSERT INTO dune.encrypted_player_state (id, account_id, encrypted_character_name, player_controller_id, player_pawn_id,
            online_status, last_login_time, character_state)
          VALUES (2, $ACCT, '\\x01', 3051, 3052, 'Online', now(), 'Active')" >/dev/null
expect "alt slot online refused" "1" "$(claim_sql apply | run | grep -c player_online)"
expect "ledger untouched" "40" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"
psqlq -c "UPDATE dune.encrypted_player_state SET online_status='Offline' WHERE id=2" >/dev/null
claim_sql apply | run >/dev/null
expect "the claim still targets the requested character only" "1" "$(q "SELECT count(*) FROM dune.items WHERE inventory_id=$BP")"

head_ "stacks are capped by the PAK max even when an oversized stack exists live"
seed 10
reward DA_HouseArgosaz T6GunPart 1200
out=$(python3 "$WRITER" --owner-ctrl "$CTRL" --mode apply --print-sql --pak-meta "$PAK" \
        --context-json '{"stacks": {"T6GunPart": 1000}, "cats": {}}' | run)
expect "no stack above the PAK max of 500" "500" "$(q "SELECT max(stack_size) FROM dune.items WHERE template_id='T6GunPart'")"
expect "1200 delivered as 500/500/200" "3" "$(q "SELECT count(*) FROM dune.items WHERE template_id='T6GunPart'")"

head_ "a template the item catalog does not know stays at the rep"
seed 10
reward DA_HouseArgosaz NotARealRewardThing 3
reward DA_HouseArgosaz T6GunPart 5
out=$(claim_sql apply | run)
expect "unknown template still owed in the ledger" "3" "$(q "SELECT amount FROM dune.landsraad_house_rewards WHERE template_id='NotARealRewardThing'")"
expect "no item row written for it" "0" "$(q "SELECT count(*) FROM dune.items WHERE template_id='NotARealRewardThing'")"
expect "receipt says it stayed at the rep" "rep:unknown_item" "$(echo "$out" | python3 -c 'import json,sys;print(",".join(l["lane"]+":"+str(l["note"]) for l in json.load(sys.stdin)["lines"] if l["template"]=="NotARealRewardThing"))' 2>/dev/null)"
expect "the known line was still claimed" "0" "$(q "SELECT amount FROM dune.landsraad_house_rewards WHERE template_id='T6GunPart'")"

head_ "a held lock times the claim out instead of queueing behind it"
seed 10
reward DA_HouseArgosaz T6GunPart 40
docker exec -d "$CID" psql -U postgres -d dune -c "BEGIN; SELECT 1 FROM dune.inventories WHERE id=$BP FOR UPDATE; SELECT pg_sleep(20); COMMIT;"
sleep 1
t0=$(date +%s)
out=$(claim_sql apply | run)
t1=$(date +%s)
expect "lock timeout reported" "1" "$(echo "$out" | grep -c 'lock timeout')"
expect "gave up within 10s" "1" "$([[ $((t1 - t0)) -lt 10 ]] && echo 1 || echo 0)"
expect "ledger untouched" "40" "$(q "SELECT amount FROM dune.landsraad_house_rewards")"
docker exec "$CID" psql -U postgres -d dune -tAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(20)%' AND pid <> pg_backend_pid()" >/dev/null

# --- canaries: break the SQL on purpose, the matching case must go red -----------
head_ "CANARY: each guard is load-bearing"
seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq -c "UPDATE dune.encrypted_player_state SET online_status='Online'" >/dev/null
out=$(claim_sql apply | sed 's/IF v_online AND NOT v_preview THEN/IF false THEN/' | run)
# The end gate still catches it (defence in depth), so what proves the start gate is that
# the refusal above came from it: with it gone, the start-gate message disappears.
if [[ "$out" == *"player_online at commit"* && "$out" != *"player_online (status="* ]]; then
  ok "start gate removed -> refusal moves to the end gate (start gate proven)"
else bad "canary start gate" "out=$out"; fi

seed 10
reward DA_HouseArgosaz T6GunPart 40
out=$(claim_sql apply | sed 's/PERFORM dune.landsraad_withdraw_house_reward/PERFORM 1 --/' | run)
c=$(q "SELECT amount FROM dune.landsraad_house_rewards")
if [[ "$c" == "40" ]]; then ok "native withdraw removed -> ledger not decremented (assertion proven)"; else bad "canary withdraw" "ledger=$c"; fi

seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq >/dev/null <<'EOF'
CREATE TRIGGER _test_login AFTER INSERT ON dune.ls_item_delivery_log FOR EACH ROW EXECUTE FUNCTION dune._test_login();
EOF
out=$(claim_sql apply | sed 's/IF v_online_end THEN$/IF false THEN/' | run)
c=$(q "SELECT amount FROM dune.landsraad_house_rewards")
if [[ "$c" == "0" ]]; then ok "end gate removed -> racing login commits (gate proven)"; else bad "canary end gate" "ledger=$c out=$out"; fi
psqlq -c "DROP TRIGGER _test_login ON dune.ls_item_delivery_log" >/dev/null

seed 10
reward DA_HouseArgosaz T6GunPart 40
psqlq -c "UPDATE dune.encrypted_player_state SET logoff_persistence_end_time=(now() AT TIME ZONE 'UTC') + interval '4 minutes'" >/dev/null
claim_sql apply | sed 's/OR s.logoff_persistence_end_time > /OR false AND s.logoff_persistence_end_time > /' | run >/dev/null
c=$(q "SELECT amount FROM dune.landsraad_house_rewards")
if [[ "$c" == "0" ]]; then ok "logoff-persistence check removed -> claim commits into a live pawn (check proven)"; else bad "canary logoff persistence" "ledger=$c"; fi

seed 10
reward DA_HouseArgosaz T6GunPart 1200
python3 "$WRITER" --owner-ctrl "$CTRL" --mode apply --print-sql --pak-meta "$PAK" \
  --context-json '{"stacks": {"T6GunPart": 1000}, "cats": {}}' | sed "s/('T6GunPart', 500)/('T6GunPart', 1000)/" | run >/dev/null
c=$(q "SELECT max(stack_size) FROM dune.items WHERE template_id='T6GunPart'")
if [[ "$c" == "1000" ]]; then ok "PAK cap removed -> oversized stack written (cap proven)"; else bad "canary PAK cap" "max=$c"; fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
