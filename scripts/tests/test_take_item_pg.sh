#!/usr/bin/env bash
# Integration test for the SHARED GATED TAKE (scripts/lib/dune-take-item.sh) and for the
# transaction scripts/dune-item-transfer-op.sh assembles around it.
#
# This EXECUTES the real SQL. Everything else in scripts/tests/ scans source, which cannot
# tell you whether an offline gate actually refuses an online player. It runs against a
# throwaway postgres container with a minimal `dune` schema, so it never touches the game
# DB and never needs the game host.
#
# The subject SQL is not reconstructed here: it is pulled from the writer's own --dry-run
# output, so a change to the writer that breaks the gate breaks this test.
#
# Requires: docker, a local postgres image. NOT part of the fast unit suite (deploy
# preflight runs scripts/tests/test_storage_v2.py); run this when touching the take.
#
#   ./scripts/tests/test_take_item_pg.sh
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
WRITER="$REPO/scripts/dune-item-transfer-op.sh"
LIB="$REPO/scripts/lib/dune-take-item.sh"
IMAGE="${PG_IMAGE:-postgres:16-alpine}"
CID=""

PASS=0
FAIL=0

cleanup() { [[ -z "$CID" ]] || docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [[ -z "${2:-}" ]] || printf '        %s\n' "$2"; }
head_() { printf '\n== %s ==\n' "$1"; }

# --- fixtures ----------------------------------------------------------------
# Two accounts, each with one pawn, one CHOAM bank (inventory_type 30), and one item in
# the sender's bank. Column sets are the subset the take and the writer actually read.
SENDER_ACCT=1001
RECIP_ACCT=2002
SENDER_BANK=9001
RECIP_BANK=9002
EXCHANGE_INV=610
ITEM=4242
TEMPLATE=T6BladePart

psqlq() { docker exec -i "$CID" psql -U postgres -d dune -v ON_ERROR_STOP=1 -tA "$@"; }

schema_sql() {
  cat <<'EOF'
CREATE SCHEMA IF NOT EXISTS dune;

CREATE TABLE dune.encrypted_player_state (
  account_id                 bigint,
  player_pawn_id             bigint,
  player_controller_id       bigint,
  online_status              text,
  reconnect_grace_period_end timestamptz,
  character_state            text,
  last_avatar_activity       timestamptz
);

CREATE TABLE dune.inventories (
  id              bigint PRIMARY KEY,
  actor_id        bigint,
  inventory_type  int,
  max_item_count  bigint,
  max_item_volume double precision
);

CREATE TABLE dune.items (
  id               bigint PRIMARY KEY,
  inventory_id     bigint,
  stack_size       bigint,
  position_index   bigint,
  template_id      text,
  stats            jsonb,
  quality_level    bigint,
  acquisition_time bigint,
  is_new           boolean,
  volume_override  double precision
);

-- Stub of the real exchange-inventory resolver: on live, dune.get_exchange_inventory_id(2)
-- returns 610, the market bot's own bot_inv_id, which is where Karum escrow lands.
CREATE FUNCTION dune.get_exchange_inventory_id(int) RETURNS bigint
  LANGUAGE sql IMMUTABLE AS $fn$ SELECT 610::bigint $fn$;
EOF
}

reset_sql() {
  cat <<EOF
TRUNCATE dune.encrypted_player_state, dune.inventories, dune.items, dune.ls_item_transfers;

INSERT INTO dune.encrypted_player_state
  (account_id, player_pawn_id, player_controller_id, online_status,
   reconnect_grace_period_end, character_state, last_avatar_activity)
VALUES
  ($SENDER_ACCT, 5001, 7001, 'Offline', NULL, 'Active', now()),
  ($RECIP_ACCT,  5002, 7002, 'Offline', NULL, 'Active', now());

INSERT INTO dune.inventories (id, actor_id, inventory_type, max_item_count, max_item_volume)
VALUES ($SENDER_BANK, 5001, 30, 50, 500),
       ($RECIP_BANK,  5002, 30, 50, 500),
       ($EXCHANGE_INV, NULL, 2, -1, -1);

INSERT INTO dune.items
  (id, inventory_id, stack_size, position_index, template_id, stats, quality_level,
   acquisition_time, is_new, volume_override)
VALUES ($ITEM, $SENDER_BANK, 3, 0, '$TEMPLATE', '{}'::jsonb, 6, 0, false, 1.0);
EOF
}

# --- the subject SQL ---------------------------------------------------------
# Pulled from the writer itself so this test cannot drift from what ships.
IDEM=11111111-2222-3333-4444-555555555555
build_txn() {
  local payload
  payload=$(printf '{"sender_account_id":%s,"recipient_account_id":%s,"item_id":%s,"template_id":"%s","idempotency_key":"%s","operator":"pgtest","mode":"dry-run"}' \
              "$SENDER_ACCT" "$RECIP_ACCT" "$ITEM" "${1:-$TEMPLATE}" "$IDEM" | base64 -w0)
  printf '%s' "$payload" | LASTSIETCH_ITEM_TRANSFER_ENABLED=1 "$WRITER" --op-b64-stdin \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sql"])'
}

run_txn() {
  printf '%s\n' "$1" | psqlq \
    -v "idem=$IDEM" \
    -v "sender_account_id=$SENDER_ACCT" \
    -v "recipient_account_id=$RECIP_ACCT" \
    -v "item_id=$ITEM" \
    -v "template_id=$TEMPLATE" \
    -v "operator=pgtest" \
    -v "req_by=" \
    -v 'detail={"kind":"bank_item_transfer"}' 2>&1
}

# scenario <name> <setup sql | ""> <expect: applied|replay|token> [txn override]
scenario() {
  local name="$1" setup="$2" expect="$3" txn="${4:-$TXN}"
  psqlq -c "$(reset_sql)" >/dev/null 2>&1
  [[ -z "$setup" ]] || psqlq -c "$setup" >/dev/null 2>&1
  # Where the row sits BEFORE the transaction. A refusal must leave it exactly here; some
  # setups deliberately park it somewhere other than the sender's bank.
  local before
  before=$(psqlq -c "SELECT COALESCE((SELECT inventory_id FROM dune.items WHERE id=$ITEM)::text,'GONE');" | tr -d '[:space:]')
  local out rc
  out=$(run_txn "$txn"); rc=$?
  local where
  where=$(psqlq -c "SELECT COALESCE((SELECT inventory_id FROM dune.items WHERE id=$ITEM)::text,'GONE');" | tr -d '[:space:]')
  local count
  count=$(psqlq -c "SELECT count(*) FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')

  case "$expect" in
    applied|replay)
      if [[ $rc -ne 0 ]]; then
        bad "$name: expected $expect, transaction failed" "$(printf '%s' "$out" | tr '\n' ' ' | tail -c 220)"
        return
      fi
      if ! printf '%s' "$out" | grep -q "RESULT|.*|$expect"; then
        bad "$name: expected RESULT state '$expect'" "$(printf '%s' "$out" | grep -F 'RESULT|' || echo 'no RESULT line')"
        return
      fi
      if [[ "$expect" == "applied" && "$where" != "$RECIP_BANK" ]]; then
        bad "$name: item should be in recipient bank $RECIP_BANK, is in $where"
        return
      fi
      if [[ "$count" != "1" ]]; then
        bad "$name: item row count is $count, must stay exactly 1 (never copied)"
        return
      fi
      ok "$name"
      ;;
    *)
      if [[ $rc -eq 0 ]]; then
        bad "$name: expected refusal '$expect', transaction SUCCEEDED"
        return
      fi
      if ! printf '%s' "$out" | grep -q "$expect"; then
        bad "$name: expected token '$expect'" "$(printf '%s' "$out" | tr '\n' ' ' | tail -c 220)"
        return
      fi
      if [[ "$where" != "$before" ]]; then
        bad "$name: refused but the item MOVED ($before -> $where) - rollback failed"
        return
      fi
      ok "$name (refused, item unmoved, txn rolled back)"
      ;;
  esac
}

# --- boot --------------------------------------------------------------------
command -v docker >/dev/null || { echo "FATAL: docker not available" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "FATAL: image $IMAGE not present locally (set PG_IMAGE=)" >&2; exit 2; }

head_ "boot $IMAGE"
CID=$(docker run -d --rm -e POSTGRES_PASSWORD=t -e POSTGRES_DB=dune "$IMAGE" \
        -c fsync=off -c full_page_writes=off)
for _ in $(seq 1 60); do
  docker exec "$CID" pg_isready -U postgres -d dune >/dev/null 2>&1 && break
  sleep 0.5
done
docker exec "$CID" pg_isready -U postgres -d dune >/dev/null 2>&1 \
  || { echo "FATAL: postgres never became ready" >&2; exit 1; }
echo "  ready ($(docker exec "$CID" psql -U postgres -tAc 'SHOW server_version' | tr -d '[:space:]'))"

psqlq -c "$(schema_sql)" >/dev/null
# The real ledger DDL, verbatim, minus the ALTER ... OWNER TO dune (no dune role here).
grep -v 'OWNER TO dune' "$REPO/scripts/sql/ls_item_transfers.sql" | psqlq >/dev/null
echo "  schema + ls_item_transfers loaded"

TXN=$(build_txn)
[[ -n "$TXN" ]] || { echo "FATAL: could not build the transaction from the writer" >&2; exit 1; }

# --- the offline gate --------------------------------------------------------
head_ "offline gate (the whole point: a take from a loaded session resurrects)"
scenario "sender Offline, past grace -> the take is allowed" "" applied
scenario "sender Online -> refused" \
  "UPDATE dune.encrypted_player_state SET online_status='Online' WHERE account_id=$SENDER_ACCT;" \
  player_online
scenario "sender Offline but inside reconnect grace -> refused" \
  "UPDATE dune.encrypted_player_state SET reconnect_grace_period_end=now()+interval '5 min' WHERE account_id=$SENDER_ACCT;" \
  player_online
scenario "sender status NULL (undetermined) -> refused, fail-closed" \
  "UPDATE dune.encrypted_player_state SET online_status=NULL WHERE account_id=$SENDER_ACCT;" \
  player_online
scenario "sender has no character row at all -> refused, fail-closed" \
  "DELETE FROM dune.encrypted_player_state WHERE account_id=$SENDER_ACCT;" \
  player_online
scenario "sender grace already expired -> allowed" \
  "UPDATE dune.encrypted_player_state SET reconnect_grace_period_end=now()-interval '1 min' WHERE account_id=$SENDER_ACCT;" \
  applied
scenario "a SECOND character on the account is Online -> refused (account-scoped gate)" \
  "INSERT INTO dune.encrypted_player_state (account_id,player_pawn_id,player_controller_id,online_status,character_state,last_avatar_activity) VALUES ($SENDER_ACCT,5003,7003,'Online','Active',now()-interval '1 day');" \
  player_online
scenario "a Deleted tombstone stuck Online must NOT block a live Offline character" \
  "INSERT INTO dune.encrypted_player_state (account_id,player_pawn_id,player_controller_id,online_status,character_state,last_avatar_activity) VALUES ($SENDER_ACCT,5004,7004,'Online','Deleted',NULL);" \
  applied
scenario "RECIPIENT being Online does NOT block: a give is online-safe" \
  "UPDATE dune.encrypted_player_state SET online_status='Online' WHERE account_id=$RECIP_ACCT;" \
  applied

# --- ownership, identity, existence -----------------------------------------
head_ "ownership / identity / existence"
scenario "item not in the sender's bank -> refused" \
  "UPDATE dune.items SET inventory_id=$RECIP_BANK WHERE id=$ITEM;" \
  item_not_found
scenario "sender has no CHOAM bank -> refused" \
  "DELETE FROM dune.inventories WHERE id=$SENDER_BANK;" \
  no_bank
scenario "recipient has no CHOAM bank -> refused" \
  "DELETE FROM dune.inventories WHERE id=$RECIP_BANK;" \
  no_bank
TXN_BAD_TPL=$(build_txn WrongTemplate)
scenario "template identity guard: swapped payload -> refused" "" item_not_found "$TXN_BAD_TPL"

# --- capacity + rate caps ----------------------------------------------------
head_ "capacity + anti-RMT caps"
scenario "recipient bank at capacity -> refused, move rolled back" \
  "UPDATE dune.inventories SET max_item_count=0 WHERE id=$RECIP_BANK;" \
  bank_full
scenario "sender daily cap exhausted -> refused before dune.items is touched" \
  "INSERT INTO dune.ls_item_transfers (idempotency_key, sender_account_id, recipient_account_id, item_id, status, created_at) SELECT gen_random_uuid(), $SENDER_ACCT, $RECIP_ACCT, 1, 'applied', now() FROM generate_series(1,30);" \
  rate_limited

# --- idempotency -------------------------------------------------------------
head_ "idempotency (the replay short-circuit)"
psqlq -c "$(reset_sql)" >/dev/null
first=$(run_txn "$TXN"); rc1=$?
second=$(run_txn "$TXN"); rc2=$?
where=$(psqlq -c "SELECT inventory_id FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')
rows=$(psqlq -c "SELECT count(*) FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')
ledger=$(psqlq -c "SELECT count(*) FROM dune.ls_item_transfers WHERE idempotency_key='$IDEM'::uuid;" | tr -d '[:space:]')
if [[ $rc1 -eq 0 && $rc2 -eq 0 ]] \
   && printf '%s' "$first"  | grep -q 'RESULT|.*|applied' \
   && printf '%s' "$second" | grep -q 'RESULT|.*|replay' \
   && [[ "$where" == "$RECIP_BANK" && "$rows" == "1" && "$ledger" == "1" ]]; then
  ok "exact retry replays: 1 item row, 1 ledger row, no second move"
else
  bad "replay: rc=$rc1/$rc2 inv=$where rows=$rows ledger=$ledger" \
      "$(printf '%s' "$second" | tr '\n' ' ' | tail -c 220)"
fi

# --- the ledger backfill -----------------------------------------------------
head_ "ledger backfill from _take_result"
psqlq -c "$(reset_sql)" >/dev/null
run_txn "$TXN" >/dev/null
led=$(psqlq -F'|' -c "SELECT sender_pawn_id, recipient_pawn_id, sender_bank_inv_id, recipient_bank_inv_id, template_id, stack_size, quality_level FROM dune.ls_item_transfers WHERE idempotency_key='$IDEM'::uuid;" | tr -d '[:space:]')
if [[ "$led" == "5001|5002|$SENDER_BANK|$RECIP_BANK|$TEMPLATE|3|6" ]]; then
  ok "pawn ids, both bank ids, template, stack and quality all recorded"
else
  bad "ledger backfill wrong" "got '$led' want '5001|5002|$SENDER_BANK|$RECIP_BANK|$TEMPLATE|3|6'"
fi

# --- the Karum shape ---------------------------------------------------------
# Same library, different destination. This is what makes owner decision D3 true: the
# Phase 1 writer reuses this take instead of writing a third one.
head_ "the Karum shape (dst=exchange, min_position, stats sentinel)"
# shellcheck source=/dev/null
. "$LIB"
KARUM_CORR=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
KARUM_SQL=$(karum_take_item item_id=$ITEM owner_account_id=$SENDER_ACCT dst=exchange \
              correlation_id=$KARUM_CORR expected_template=$TEMPLATE \
              min_position=900000 stats_patch='{"HolKarum":{"listing_id":4711}}')
if [[ -z "$KARUM_SQL" ]]; then
  bad "karum_take_item emitted nothing"
else
  psqlq -c "$(reset_sql)" >/dev/null
  kout=$(printf 'BEGIN;\nSET LOCAL search_path TO dune, public;\n%s\nSELECT '"'"'TAKE|'"'"' || src_inv || '"'"'|'"'"' || dst_inv || '"'"'|'"'"' || position_index FROM _take_result;\nCOMMIT;\n' "$KARUM_SQL" | psqlq 2>&1)
  krc=$?
  kwhere=$(psqlq -c "SELECT inventory_id FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')
  kpos=$(psqlq -c "SELECT position_index FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')
  kmark=$(psqlq -c "SELECT stats->'HolKarum'->>'listing_id' FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')
  if [[ $krc -eq 0 && "$kwhere" == "$EXCHANGE_INV" && "$kpos" == "900000" && "$kmark" == "4711" ]]; then
    ok "escrowed into exchange inv $EXCHANGE_INV at slot $kpos, HolKarum sentinel present"
  else
    bad "karum take: rc=$krc inv=$kwhere pos=$kpos marker=$kmark" "$(printf '%s' "$kout" | tr '\n' ' ' | tail -c 220)"
  fi

  # The gate is in the library, so it protects the Karum caller with no extra code.
  psqlq -c "$(reset_sql)" >/dev/null
  psqlq -c "UPDATE dune.encrypted_player_state SET online_status='Online' WHERE account_id=$SENDER_ACCT;" >/dev/null
  gout=$(printf 'BEGIN;\nSET LOCAL search_path TO dune, public;\n%s\nCOMMIT;\n' "$KARUM_SQL" | psqlq 2>&1)
  grc=$?
  gwhere=$(psqlq -c "SELECT inventory_id FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')
  if [[ $grc -ne 0 ]] && printf '%s' "$gout" | grep -q player_online && [[ "$gwhere" == "$SENDER_BANK" ]]; then
    ok "seller Online -> the Karum listing take is refused by the same gate"
  else
    bad "karum gate: rc=$grc inv=$gwhere" "$(printf '%s' "$gout" | tr '\n' ' ' | tail -c 220)"
  fi

  # A skip flag set by the caller must stop the take dead, or an idempotent replay
  # re-runs it against a source that no longer holds the row.
  psqlq -c "$(reset_sql)" >/dev/null
  sout=$(printf "BEGIN;\nSET LOCAL search_path TO dune, public;\nSET LOCAL ls.take_skip = '1';\n%s\nSELECT 'ROWS|' || count(*) FROM _take_result;\nCOMMIT;\n" "$KARUM_SQL" | psqlq 2>&1)
  swhere=$(psqlq -c "SELECT inventory_id FROM dune.items WHERE id=$ITEM;" | tr -d '[:space:]')
  if printf '%s' "$sout" | grep -q 'ROWS|0' && [[ "$swhere" == "$SENDER_BANK" ]]; then
    ok "ls.take_skip=1 -> the take no-ops and publishes no result row"
  else
    bad "take_skip: inv=$swhere" "$(printf '%s' "$sout" | tr '\n' ' ' | tail -c 220)"
  fi
fi

# --- verdict -----------------------------------------------------------------
printf '\n== %s passed, %s failed ==\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
