#!/usr/bin/env bash
# Executes the REAL wave 10 refinery SQL against a throwaway PostgreSQL container.
# It never contacts the game host and never touches the live game database.
#
# Everything else that covers this feature scans source or calls the builders and
# asserts on the strings they produce. A string cannot tell you whether an offline
# gate actually refuses, whether an exact-N drain across three partial stacks
# leaves nothing behind, or whether two refiners racing for the same bank slot
# serialise instead of both minting. This runs the transaction and looks at rows.
#
# The subject SQL is not reconstructed here: it is built by the writer's own
# build_exchange_sql, so a change to the writer that breaks an invariant breaks
# this test.
#
# dune.delete_inventory_item is the ENGINE's function and does not exist in a
# bare postgres. The stand-in below is the contract the writer relies on and
# nothing more: take N units off a stack, delete the row when it reaches zero.
# If the engine's own function ever stops behaving that way, this suite is
# testing a fiction and the trace doc is the place that would say so.
#
# Requires: docker and a local postgres image. The web host has both; the WSL box
# does not, so this is not part of the fast unit suite.
#
#   ./scripts/tests/test_refinery_pg.sh
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
WRITER="$REPO/scripts/dune-refinery-write.py"
DDL="$REPO/scripts/sql/ls_refinery_exchanges.sql"
IMAGE="${PG_IMAGE:-postgres:16-alpine}"
CID=""
PASS=0
FAIL=0

cleanup() {
  [[ -z "$CID" ]] || docker rm -fv "$CID" >/dev/null 2>&1 || true
  [[ -z "${TMPD:-}" ]] || rm -rf "$TMPD"
}
trap cleanup EXIT

ok() {
  PASS=$((PASS+1))
  printf '  PASS %s\n' "$1"
}

bad() {
  FAIL=$((FAIL+1))
  printf '  FAIL %s\n' "$1"
  [[ -z "${2:-}" ]] || printf '       %s\n' "$2"
}

head_() {
  printf '\n== %s ==\n' "$1"
}

ACCOUNT=1001
CTRL=7001
PAWN=5001
BACKPACK=9000
BANK=9001
TOOLBAR=9015
INGOT=CopperBar
SPICE=MelangeSpice
DUST=T1UniqueComponent

psqlq() {
  docker exec -i "$CID" psql -U postgres -d dune -v ON_ERROR_STOP=1 -tA "$@"
}

scalar() {
  psqlq -c "$1" | tr -d '[:space:]'
}

held() {
  scalar "SELECT COALESCE(SUM(stack_size), 0) FROM dune.items WHERE template_id = '$1';"
}

ledgers() {
  scalar "SELECT count(*) FROM dune.ls_refinery_exchanges;"
}

schema_sql() {
  cat <<'EOF'
CREATE ROLE dune;
CREATE SCHEMA dune AUTHORIZATION dune;
CREATE TABLE dune.encrypted_player_state (
  account_id bigint, player_pawn_id bigint, player_controller_id bigint,
  online_status text, reconnect_grace_period_end timestamptz, character_state text
);
CREATE TABLE dune.inventories (
  id bigint PRIMARY KEY, actor_id bigint, inventory_type int, max_item_count bigint,
  max_item_volume bigint
);
CREATE TABLE dune.items (
  id bigserial PRIMARY KEY, inventory_id bigint, stack_size bigint,
  position_index bigint, template_id text, stats jsonb,
  quality_level int DEFAULT 0, acquisition_time bigint DEFAULT 0,
  is_new boolean DEFAULT false
);
ALTER SEQUENCE dune.items_id_seq RESTART WITH 900000;
-- The engine's own partial-stack removal, reduced to the contract the writer
-- depends on: take N units, and the row disappears when the stack hits zero.
CREATE FUNCTION dune.delete_inventory_item(p_item bigint, p_count bigint)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  UPDATE dune.items SET stack_size = stack_size - p_count WHERE id = p_item;
  DELETE FROM dune.items WHERE id = p_item AND stack_size <= 0;
END;
$fn$;
EOF
}

# One account, one character, the three pawn-side inventories the refinery reads.
# A second character on the SAME account exists so the COUNT FILTER resolve is
# exercised against a world where more than one slot is possible.
world_sql() {
  cat <<EOF
TRUNCATE dune.encrypted_player_state, dune.inventories, dune.items,
         dune.ls_refinery_exchanges;
INSERT INTO dune.encrypted_player_state
  (account_id, player_pawn_id, player_controller_id, online_status,
   reconnect_grace_period_end, character_state)
VALUES ($ACCOUNT, $PAWN, $CTRL, 'Offline', NULL, 'Active'),
       ($ACCOUNT, 5002, 7002, 'Offline', NULL, 'Deleted');
INSERT INTO dune.inventories (id, actor_id, inventory_type, max_item_count)
VALUES ($BANK, $PAWN, 30, 20),
       ($BACKPACK, $PAWN, 0, 35),
       ($TOOLBAR, $PAWN, 15, 8),
       (9200, 5002, 30, 20);
EOF
}

item_sql() {   # inventory, stack, slot, template
  printf "INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats)"
  printf " VALUES (%s, %s, %s, '%s', '{}'::jsonb);" "$1" "$2" "$3" "$4"
}

reset() {
  psqlq -c "$(world_sql)" >/dev/null
}

# The transaction under test, built by the writer itself.
build_txn() {   # output_template batches uuid [dry]
  python3 - "$WRITER" "$ACCOUNT" "$CTRL" "$1" "$2" "$3" "${4:-live}" <<'PY'
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("refinery_writer", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
rates = m.load_rates()
assert rates is not None, "the rate table did not load"
recipe = m.find_recipe(rates, sys.argv[4])
assert recipe is not None, "no recipe for %s" % sys.argv[4]
if sys.argv[7] == "legacy":
    rates = dict(rates, rate_version="2026-09-04b")
    recipe = dict(recipe, input_per_batch=5, spice_per_batch=1, output_per_batch=2)
print(m.build_exchange_sql(int(sys.argv[2]), int(sys.argv[3]), recipe,
                           int(sys.argv[5]), sys.argv[6], rates,
                           sys.argv[7] == "dry"))
PY
}

run_txn() {
  printf '%s\n' "$1" | psqlq 2>&1
}

# A refusal must leave EVERY row where it was and write no ledger row. Checking
# the token alone would pass on a transaction that raised after consuming.
expect_refusal() {   # name token txn
  local name="$1" token="$2" txn="$3"
  local in_before sp_before out rc in_after sp_after dust_after rows
  in_before=$(held "$INGOT"); sp_before=$(held "$SPICE")
  out=$(run_txn "$txn"); rc=$?
  in_after=$(held "$INGOT"); sp_after=$(held "$SPICE")
  dust_after=$(held "$DUST"); rows=$(ledgers)
  if [[ $rc -ne 0 && "$in_before" == "$in_after" && "$sp_before" == "$sp_after" \
        && "$dust_after" == "0" && "$rows" == "0" ]] \
     && printf '%s' "$out" | grep -q "$token"; then
    ok "$name"
  else
    bad "$name" "rc=$rc ingots $in_before->$in_after spice $sp_before->$sp_after dust=$dust_after ledger=$rows want=$token"
  fi
}

command -v docker >/dev/null || { echo "FATAL: docker not available" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "FATAL: image $IMAGE not present locally" >&2; exit 2; }

TMPD=$(mktemp -d)

head_ "boot $IMAGE"
CID=$(docker run -d --network none --memory 512m --cpus 1 -e POSTGRES_PASSWORD=t -e POSTGRES_DB=dune "$IMAGE" \
      -c fsync=off -c full_page_writes=off -c shared_buffers=32MB)
for _ in $(seq 1 120); do
  docker exec "$CID" pg_isready -U postgres -d dune >/dev/null 2>&1 && break
  sleep 0.5
done
docker exec "$CID" pg_isready -U postgres -d dune >/dev/null 2>&1 \
  || { docker inspect "$CID" --format '{{json .State}}'; docker logs --tail 40 "$CID"; echo "FATAL: postgres never became ready" >&2; exit 1; }
psqlq -c "$(schema_sql)" >/dev/null
psqlq < "$DDL" >/dev/null

U1=11111111-2222-4333-8444-555555555551
U2=11111111-2222-4333-8444-555555555552
U3=11111111-2222-4333-8444-555555555553
U4=11111111-2222-4333-8444-555555555554
U5=11111111-2222-4333-8444-555555555555
U6=11111111-2222-4333-8444-555555555556
U7=11111111-2222-4333-8444-555555555557
U8=11111111-2222-4333-8444-555555555558
U9=11111111-2222-4333-8444-555555555559
U10=11111111-2222-4333-8444-55555555555a
U11=11111111-2222-4333-8444-55555555555b
U12=11111111-2222-4333-8444-55555555555c
U13=11111111-2222-4333-8444-55555555555d
U14=11111111-2222-4333-8444-55555555555e

# --------------------------------------------------------------------------- #
head_ "exact-N consume across three partial stacks"
reset
psqlq -c "$(item_sql $BANK 10 0 $INGOT) $(item_sql $BANK 10 1 $INGOT) $(item_sql $BANK 5 2 $INGOT) $(item_sql $BANK 1 3 $SPICE)" >/dev/null
out=$(run_txn "$(build_txn $DUST 1 "$U1")"); rc=$?
ing=$(held "$INGOT"); sp=$(held "$SPICE"); dust=$(held "$DUST"); rows=$(ledgers)
stacks=$(scalar "SELECT count(*) FROM dune.items WHERE template_id = '$INGOT';")
if [[ $rc -eq 0 && "$ing" == "0" && "$stacks" == "0" && "$sp" == "0" && "$dust" == "10" && "$rows" == "1" ]]; then
  ok "twenty-five ingots drained from three partial stacks, every emptied row gone"
else
  bad "exact-N drain" "rc=$rc ingots=$ing stacks=$stacks spice=$sp dust=$dust ledger=$rows"
  printf '%s\n' "$out" | head -16
fi

head_ "the consume spans bank, backpack and toolbar in that order"
reset
psqlq -c "$(item_sql $BANK 10 0 $INGOT) $(item_sql $BACKPACK 10 0 $INGOT) $(item_sql $TOOLBAR 5 0 $INGOT) $(item_sql $TOOLBAR 1 1 $SPICE)" >/dev/null
out=$(run_txn "$(build_txn $DUST 1 "$U2")"); rc=$?
ing=$(held "$INGOT"); dust_where=$(scalar "SELECT inventory_id FROM dune.items WHERE template_id = '$DUST';")
if [[ $rc -eq 0 && "$ing" == "0" && "$dust_where" == "$BANK" ]]; then
  ok "ingots came out of all three sources and the dust landed in the bank"
else
  bad "three-source consume" "rc=$rc ingots=$ing dust_inventory=$dust_where"
fi
# The drain order is bank, then backpack, then toolbar. With twenty-five ingots needed
# the toolbar stack is the one that survives.
reset
psqlq -c "$(item_sql $BANK 10 0 $INGOT) $(item_sql $BACKPACK 10 0 $INGOT) $(item_sql $TOOLBAR 6 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
run_txn "$(build_txn $DUST 1 "$U3")" >/dev/null
left=$(scalar "SELECT COALESCE(SUM(stack_size), 0) FROM dune.items WHERE template_id = '$INGOT' AND inventory_id = $TOOLBAR;")
bank_left=$(scalar "SELECT COALESCE(SUM(stack_size), 0) FROM dune.items WHERE template_id = '$INGOT' AND inventory_id = $BANK;")
if [[ "$left" == "1" && "$bank_left" == "0" ]]; then
  ok "the drain empties the bank first and leaves the toolbar for last"
else
  bad "drain order" "toolbar_left=$left bank_left=$bank_left (want 1 and 0)"
fi

head_ "a short input takes nothing at all"
reset
psqlq -c "$(item_sql $BANK 24 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
expect_refusal "twenty-four ingots for a twenty-five-ingot batch is refused, spice untouched" \
  insufficient_input "$(build_txn $DUST 1 "$U4")"
reset
psqlq -c "$(item_sql $BANK 25 0 $INGOT)" >/dev/null
expect_refusal "no melange is refused, ingots untouched" \
  insufficient_spice "$(build_txn $DUST 1 "$U5")"

head_ "the mint merges, then spills at the stack cap"
reset
psqlq -c "$(item_sql $BANK 25 0 $INGOT) $(item_sql $BANK 1 1 $SPICE) $(item_sql $BANK 10 2 $DUST)" >/dev/null
run_txn "$(build_txn $DUST 1 "$U6")" >/dev/null
dust=$(held "$DUST"); dust_rows=$(scalar "SELECT count(*) FROM dune.items WHERE template_id = '$DUST';")
if [[ "$dust" == "20" && "$dust_rows" == "1" ]]; then
  ok "ten dust merged into the existing stack instead of opening a slot"
else
  bad "merge" "dust=$dust rows=$dust_rows (want 20 in 1 row)"
fi
reset
psqlq -c "$(item_sql $BANK 50 0 $INGOT) $(item_sql $BANK 2 1 $SPICE) $(item_sql $BANK 499 2 $DUST)" >/dev/null
run_txn "$(build_txn $DUST 2 "$U7")" >/dev/null
capped=$(scalar "SELECT stack_size FROM dune.items WHERE template_id = '$DUST' AND position_index = 2;")
spill=$(scalar "SELECT COALESCE(SUM(stack_size), 0) FROM dune.items WHERE template_id = '$DUST' AND position_index <> 2;")
if [[ "$capped" == "500" && "$spill" == "19" ]]; then
  ok "the mint fills the stack to 500 and spills the rest into a new slot"
else
  bad "spillover" "capped=$capped spill=$spill (want 500 and 19)"
fi

head_ "the first empty slot, at every shape of max_item_count"
reset
psqlq -c "UPDATE dune.inventories SET max_item_count = -1 WHERE id = $BANK;" >/dev/null
psqlq -c "$(item_sql $BANK 26 0 $INGOT) $(item_sql $BANK 2 7 $SPICE)" >/dev/null
run_txn "$(build_txn $DUST 1 "$U8")" >/dev/null
slot=$(scalar "SELECT position_index FROM dune.items WHERE template_id = '$DUST';")
if [[ "$slot" == "8" ]]; then
  ok "max_item_count -1 means no cap: the dust appends past the highest slot"
else
  bad "mic -1" "slot=$slot (want 8, one past the highest occupied)"
fi
reset
psqlq -c "UPDATE dune.inventories SET max_item_count = 20 WHERE id = $BANK;" >/dev/null
psqlq -c "$(item_sql $BANK 26 0 $INGOT) $(item_sql $BANK 2 2 $SPICE)" >/dev/null
run_txn "$(build_txn $DUST 1 "$U9")" >/dev/null
slot=$(scalar "SELECT position_index FROM dune.items WHERE template_id = '$DUST';")
if [[ "$slot" == "1" ]]; then
  ok "a real slot cap fills the first GAP, not the end"
else
  bad "first gap" "slot=$slot (want 1, the gap between 0 and 2)"
fi
reset
psqlq -c "UPDATE dune.inventories SET max_item_count = 0 WHERE id = $BANK;" >/dev/null
psqlq -c "$(item_sql $BANK 25 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
expect_refusal "a bank reporting zero slots is refused, nothing consumed" \
  no_space "$(build_txn $DUST 1 "$U10")"

head_ "a full bank leaves everything where it was"
reset
psqlq -c "UPDATE dune.inventories SET max_item_count = 2 WHERE id = $BANK;" >/dev/null
psqlq -c "$(item_sql $BANK 26 0 $INGOT) $(item_sql $BANK 2 1 $SPICE)" >/dev/null
expect_refusal "no free slot for the dust is refused before anything is taken" \
  no_space "$(build_txn $DUST 1 "$U11")"

head_ "the mint can reuse a slot emptied by the consume"
reset
psqlq -c "UPDATE dune.inventories SET max_item_count = 2 WHERE id = $BANK;" >/dev/null
psqlq -c "$(item_sql $BANK 25 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
out=$(run_txn "$(build_txn $DUST 1 "$U11")"); rc=$?
if [[ $rc -eq 0 && "$(held "$INGOT")" == "0" && "$(held "$SPICE")" == "0" && "$(held "$DUST")" == "10" && "$(ledgers)" == "1" ]]; then
  ok "consuming both source stacks frees room for the output"
else
  bad "reuse consumed slots" "rc=$rc"
fi

head_ "the offline gate, four ways"
for spec in "online:UPDATE dune.encrypted_player_state SET online_status = 'Online' WHERE player_controller_id = $CTRL;" \
            "grace:UPDATE dune.encrypted_player_state SET reconnect_grace_period_end = now() + interval '5 minutes' WHERE player_controller_id = $CTRL;" \
            "null status:UPDATE dune.encrypted_player_state SET online_status = NULL WHERE player_controller_id = $CTRL;" \
            "no rows at all:DELETE FROM dune.encrypted_player_state WHERE account_id = $ACCOUNT AND player_controller_id = $CTRL;"; do
  label="${spec%%:*}"; setup="${spec#*:}"
  reset
  psqlq -c "$(item_sql $BANK 25 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
  psqlq -c "$setup" >/dev/null
  expect_refusal "$label is refused: undetermined is LOCKED" \
    player_online "$(build_txn $DUST 1 "$U12")"
done

head_ "idempotency"
reset
psqlq -c "$(item_sql $BANK 25 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
out=$(run_txn "$(build_txn $DUST 1 "$U13")"); rc=$?
first_dust=$(held "$DUST"); first_rows=$(ledgers)
out2=$(run_txn "$(build_txn $DUST 1 "$U13")"); rc2=$?
again_dust=$(held "$DUST"); again_rows=$(ledgers)
if [[ $rc -eq 0 && $rc2 -eq 0 && "$first_dust" == "10" && "$again_dust" == "10" \
      && "$first_rows" == "1" && "$again_rows" == "1" ]] \
   && printf '%s' "$out" | grep -Eq '"replay"[[:space:]]*:[[:space:]]*false' \
   && printf '%s' "$out2" | grep -Eq '"replay"[[:space:]]*:[[:space:]]*true'; then
  ok "the same uuid replays the answer and consumes nothing the second time"
else
  bad "replay" "rc=$rc/$rc2 dust=$first_dust/$again_dust ledger=$first_rows/$again_rows"
fi
out=$(run_txn "$(build_txn $DUST 2 "$U13")"); rc=$?
dust=$(held "$DUST")
if [[ $rc -ne 0 && "$dust" == "10" ]] && printf '%s' "$out" | grep -q idempotency_conflict; then
  ok "the same uuid for a different order is refused, not silently applied"
else
  bad "idempotency conflict" "rc=$rc dust=$dust"
fi

head_ "two refiners racing for one batch of stock"
reset
psqlq -c "$(item_sql $BANK 25 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
A=$(build_txn $DUST 1 "$U14")
B=$(build_txn $DUST 1 "$U1")
( printf '%s\n' "$A" | psqlq >"$TMPD/a.out" 2>&1; echo $? >"$TMPD/a.rc" ) &
( printf '%s\n' "$B" | psqlq >"$TMPD/b.out" 2>&1; echo $? >"$TMPD/b.rc" ) &
wait
arc=$(cat "$TMPD/a.rc"); brc=$(cat "$TMPD/b.rc")
dust=$(held "$DUST"); ing=$(held "$INGOT"); rows=$(ledgers)
winners=0
[[ "$arc" == "0" ]] && winners=$((winners+1))
[[ "$brc" == "0" ]] && winners=$((winners+1))
if [[ "$winners" == "1" && "$dust" == "10" && "$ing" == "0" && "$rows" == "1" ]]; then
  ok "the two transactions serialised: exactly one batch, no double spend"
else
  bad "concurrent refine" "winners=$winners dust=$dust ingots=$ing ledger=$rows (rc $arc/$brc)"
fi

head_ "conservation, against the rows that actually moved"
reset
psqlq -c "UPDATE dune.inventories SET max_item_count = 20 WHERE id = $BANK;" >/dev/null
psqlq -c "$(item_sql $BANK 110 0 SteelBar) $(item_sql $BANK 16 1 $SPICE)" >/dev/null
out=$(run_txn "$(build_txn T3UniqueComponent 4 "$U2")"); rc=$?
steel=$(held SteelBar); sp=$(held "$SPICE"); dust=$(held T3UniqueComponent)
led=$(scalar "SELECT input_units || '/' || spice_units || '/' || dust_units FROM dune.ls_refinery_exchanges;")
# Four T3 batches consume 100 ingots and 12 Melange, producing 40 dust.
# Ten ingots and four Melange remain.
if [[ $rc -eq 0 && "$steel" == "10" && "$sp" == "4" && "$dust" == "40" && "$led" == "100/12/40" ]]; then
  ok "consumed inputs and ten dust per batch agree with the ledger"
else
  bad "conservation" "steel=$steel spice=$sp dust=$dust ledger=$led (want 10, 4, 40, 100/12/40)"
fi

head_ "a dry run rehearses and keeps nothing"
reset
psqlq -c "$(item_sql $BANK 25 0 $INGOT) $(item_sql $BANK 1 1 $SPICE)" >/dev/null
out=$(run_txn "$(build_txn $DUST 1 "$U3" dry)"); rc=$?
ing=$(held "$INGOT"); dust=$(held "$DUST"); rows=$(ledgers)
if [[ $rc -eq 0 && "$ing" == "25" && "$dust" == "0" && "$rows" == "0" ]]; then
  ok "the rehearsal ran the whole plan and rolled every row back"
else
  bad "dry run" "rc=$rc ingots=$ing dust=$dust ledger=$rows (want 25, 0, 0)"
fi

head_ "all six approved recipes execute with exact quantities"
INGOTS=(CopperBar IronBar SteelBar AluminiumBar DuraluminumRod T6RefinedResourceA)
for tier in 1 2 3 4 5 6; do
  reset
  ingot=${INGOTS[$((tier-1))]}
  output="T${tier}UniqueComponent"
  psqlq -c "$(item_sql $BANK 25 0 "$ingot") $(item_sql $BACKPACK "$tier" 0 $SPICE)" >/dev/null
  out=$(run_txn "$(build_txn "$output" 1 "$U1")"); rc=$?
  led=$(scalar "SELECT input_units || '/' || spice_units || '/' || dust_units || '/' || rate_version FROM dune.ls_refinery_exchanges;")
  if [[ $rc -eq 0 && "$(held "$ingot")" == "0" && "$(held "$SPICE")" == "0" && "$(held "$output")" == "10" && "$led" == "25/$tier/10/2026-09-07a" ]]; then
    ok "tier $tier: 25 ingots plus $tier Melange produce 10 dust"
  else
    bad "tier $tier recipe" "rc=$rc ledger=$led"
  fi
done

head_ "the unchanged allowance counts dust, not batches"
reset
psqlq -c "$(item_sql $BANK 650 0 $INGOT) $(item_sql $BANK 26 1 $SPICE)" >/dev/null
out=$(run_txn "$(build_txn $DUST 25 "$U1")"); rc=$?
if [[ $rc -eq 0 && "$(held "$DUST")" == "250" && "$(held "$INGOT")" == "25" && "$(held "$SPICE")" == "1" ]]; then
  ok "25 new batches exactly fill the 250-dust allowance"
else
  bad "full allowance" "rc=$rc"
fi
out=$(run_txn "$(build_txn $DUST 1 "$U2")"); rc=$?
if [[ $rc -ne 0 && "$out" == *weekly_cap* && "$(held "$DUST")" == "250" && "$(held "$INGOT")" == "25" && "$(held "$SPICE")" == "1" && "$(ledgers)" == "1" ]]; then
  ok "the next batch is refused without consuming stock or changing the ledger"
else
  bad "allowance overflow" "rc=$rc"
fi

head_ "historical two-dust batches still count after the retune"
reset
psqlq -c "$(item_sql $BANK 625 0 $INGOT) $(item_sql $BANK 30 1 $SPICE)" >/dev/null
run_txn "$(build_txn $DUST 4 "$U1" legacy)" >/dev/null
out=$(run_txn "$(build_txn $DUST 24 "$U2")"); rc=$?
if [[ $rc -eq 0 && "$(held "$DUST")" == "248" && "$(ledgers)" == "2" ]]; then
  ok "8 dust at the old rate plus 240 at the new rate share the same allowance"
else
  bad "mixed rate allowance" "rc=$rc dust=$(held "$DUST")"
fi
out=$(run_txn "$(build_txn $DUST 1 "$U3")"); rc=$?
if [[ $rc -ne 0 && "$out" == *weekly_cap* && "$(held "$DUST")" == "248" && "$(held "$INGOT")" == "5" && "$(held "$SPICE")" == "2" && "$(ledgers)" == "2" ]]; then
  ok "two remaining dust cannot fund a ten-dust batch; prior usage is preserved"
else
  bad "fractional batch remainder" "rc=$rc"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
