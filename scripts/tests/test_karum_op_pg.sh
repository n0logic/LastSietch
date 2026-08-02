#!/usr/bin/env bash
# Integration test for the Karum writer (scripts/dune-karum-op.sh), Phase 1 L1.
#
# This EXECUTES the writer's real SQL against a throwaway postgres with a minimal `dune`
# schema. It never touches the game DB and never needs the game host.
#
# It exists because the writer is the most dangerous file in the feature: it moves money and
# goods, and three of its failure modes are invisible to a source scan.
#   * psql does not interpolate :vars inside a dollar-quoted DO block, so a plpgsql body that
#     reads correct is rejected by the server. Only running it finds that.
#   * the payment gate must return BEFORE touching a balance on a replay. "Is the gate
#     written" and "does a retry double-charge" are different questions.
#   * paid and delivered are separate booleans precisely because they can disagree, and the
#     interesting cases are the disagreements.
#
# The subject SQL is pulled from the writer's own --dry-run, so it cannot drift from what
# ships. Requires docker and a local postgres image. NOT part of the fast unit suite.
#
#   ./scripts/tests/test_karum_op_pg.sh
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
WRITER="$REPO/scripts/dune-karum-op.sh"
IMAGE="${PG_IMAGE:-postgres:16-alpine}"
CID=""
PASS=0
FAIL=0

cleanup() { [[ -z "$CID" ]] || docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [[ -z "${2:-}" ]] || printf '        %s\n' "$2"; }
head_() { printf '\n== %s ==\n' "$1"; }

SELLER=1001
BUYER=2002
SELLER_BANK=9001
EXCHANGE_INV=610
ITEM=4242
TEMPLATE=IronBar
LISTING=4711
PRICE=12500

psqlq() { docker exec -i "$CID" psql -U postgres -d dune -v ON_ERROR_STOP=1 -tA "$@"; }
scalar() { psqlq -c "$1" | tr -d '[:space:]'; }

schema_sql() {
  cat <<'EOF'
CREATE SCHEMA IF NOT EXISTS dune;

CREATE TABLE dune.encrypted_player_state (
  account_id bigint, player_pawn_id bigint, player_controller_id bigint,
  online_status text, reconnect_grace_period_end timestamptz,
  character_state text, last_avatar_activity timestamptz
);
CREATE TABLE dune.accounts (id bigint PRIMARY KEY, "user" text);
CREATE TABLE dune.inventories (
  id bigint PRIMARY KEY, actor_id bigint, inventory_type int,
  max_item_count bigint, max_item_volume double precision
);
CREATE TABLE dune.items (
  id bigint PRIMARY KEY, inventory_id bigint, stack_size bigint, position_index bigint,
  template_id text, stats jsonb, quality_level bigint, acquisition_time bigint,
  is_new boolean, volume_override double precision
);
CREATE TABLE dune.player_virtual_currency_balances (
  player_controller_id bigint, currency_id bigint, balance bigint,
  PRIMARY KEY (player_controller_id, currency_id)
);
CREATE TABLE dune.dune_exchange_orders (
  id bigserial PRIMARY KEY, revision int, exchange_id int, owner_id bigint, item_id bigint,
  template_id text, category_mask bigint NOT NULL, category_depth bigint NOT NULL,
  access_point_id int, is_npc_order boolean, item_price bigint,
  expiration_time bigint, durability_cur bigint, durability_max bigint, quality_level bigint
);
CREATE TABLE dune.dune_exchange_fulfilled_orders (
  order_id bigint, completion_type int, stack_size bigint,
  source_order_id bigint, original_order_id bigint
);
CREATE TABLE dune.farm_variables (
  universe_time_timestamp timestamptz, down_time_accumulation bigint
);

-- Stubs of the Funcom-side functions the writer calls. get_solaris_id and
-- get_exchange_inventory_id are reads; the balance adjust is the real mutation, modelled as
-- the proc's happy path (it also clamps and logs cheating on some inputs, which is exactly
-- why the writer pre-checks the balance rather than relying on it).
CREATE FUNCTION dune.get_solaris_id() RETURNS bigint
  LANGUAGE sql IMMUTABLE AS $fn$ SELECT 0::bigint $fn$;
CREATE FUNCTION dune.get_exchange_inventory_id(int) RETURNS bigint
  LANGUAGE sql IMMUTABLE AS $fn$ SELECT 610::bigint $fn$;
CREATE FUNCTION dune.adjust_player_virtual_currency_balance(bigint, bigint, bigint)
  RETURNS void LANGUAGE sql AS $fn$
  UPDATE dune.player_virtual_currency_balances
     SET balance = balance + $3
   WHERE player_controller_id = $1 AND currency_id = $2
$fn$;
EOF
}

reset_sql() {
  cat <<EOF
TRUNCATE dune.encrypted_player_state, dune.accounts, dune.inventories, dune.items,
         dune.player_virtual_currency_balances, dune.dune_exchange_orders,
         dune.dune_exchange_fulfilled_orders, dune.farm_variables,
         dune.ls_karum_escrow, dune.ls_karum_payments, dune.ls_item_delivery_log;

INSERT INTO dune.encrypted_player_state
  (account_id, player_pawn_id, player_controller_id, online_status,
   reconnect_grace_period_end, character_state, last_avatar_activity)
VALUES ($SELLER, 5001, 7001, 'Offline', NULL, 'Active', now()),
       ($BUYER,  5002, 7002, 'Offline', NULL, 'Active', now());

INSERT INTO dune.accounts (id, "user") VALUES ($SELLER, 'fls-seller'), ($BUYER, 'fls-buyer');

INSERT INTO dune.inventories (id, actor_id, inventory_type, max_item_count, max_item_volume)
VALUES ($SELLER_BANK, 5001, 30, 50, 500),
       (9002, 5002, 30, 50, 500),
       ($EXCHANGE_INV, NULL, 2, -1, -1);

-- The listed stack: 500 IronBar, grade 2. Grade and stack must survive escrow intact.
INSERT INTO dune.items
  (id, inventory_id, stack_size, position_index, template_id, stats, quality_level,
   acquisition_time, is_new, volume_override)
VALUES ($ITEM, $SELLER_BANK, 500, 0, '$TEMPLATE', '{}'::jsonb, 2, 0, false, 1.0);

INSERT INTO dune.player_virtual_currency_balances (player_controller_id, currency_id, balance)
VALUES (7001, 0, 1000), (7002, 0, 50000);

-- A pre-existing exchange order for this template, so category_mask/depth can be copied the
-- way the proven claim lane copies them.
INSERT INTO dune.dune_exchange_orders
  (revision, exchange_id, owner_id, item_id, template_id, category_mask, category_depth,
   access_point_id, is_npc_order, item_price, expiration_time, durability_cur,
   durability_max, quality_level)
VALUES (1, 2, 999, 1, '$TEMPLATE', 64, 3, 1, true, 100, 99999999, 0, 0, 0);

INSERT INTO dune.farm_variables (universe_time_timestamp, down_time_accumulation)
VALUES (now() - interval '30 days', 0);
EOF
}

# Pull the writer's own SQL for one action. $1 = json payload fragment (no braces).
build() {
  local payload="{$1}"
  printf '%s' "$(printf '%s' "$payload" | base64 -w0)" \
    | LASTSIETCH_KARUM_ENABLED=1 "$WRITER" --op-b64-stdin \
    | python3 -c 'import json,sys
d = json.load(sys.stdin)
if d.get("status") != "dry-run":
    sys.stderr.write("writer did not return dry-run SQL: %s\n" % json.dumps(d)); sys.exit(1)
print(d["sql"])'
}

# The writer emits one labelled chunk per transaction, separated by blank lines. BUY has two
# and they must be run as two transactions, which is the whole point of the split.
run_chunk() {
  local sql="$1" corr="$2" extra_vars=("${@:3}")
  printf '%s\n' "$sql" | psqlq -v "corr=$corr" -v "operator=pgtest" "${extra_vars[@]}" 2>&1
}

split_chunks() {
  # Emits chunk boundaries on the marker comments the writer prints.
  printf '%s\n' "$1" | awk '
    /^-- karum-/ { if (n++) print "\x1e"; }
    { print }
  '
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

psqlq -c "$(schema_sql)" >/dev/null
for f in "$REPO/ops/rmq-item-delivery/schema.sql" \
         "$REPO/scripts/sql/ls_karum_escrow.sql" \
         "$REPO/scripts/sql/ls_karum_payments.sql"; do
  grep -v 'OWNER TO dune' "$f" | psqlq >/dev/null \
    || { echo "FATAL: could not apply $f" >&2; exit 1; }
done
echo "  schema + the three ls_* ledgers loaded"

# --- the dark gate -----------------------------------------------------------
head_ "the dark gate (LASTSIETCH_KARUM_ENABLED unset)"
dark=$(printf '%s' "$(printf '{"action":"karum-list","listing_id":%d,"seller_account_id":%d,"item_id":%d,"template_id":"%s","correlation_id":"11111111-1111-1111-1111-111111111111"}' \
        "$LISTING" "$SELLER" "$ITEM" "$TEMPLATE" | base64 -w0)" | "$WRITER" --op-b64-stdin)
if printf '%s' "$dark" | grep -q '"status":"deferred"' \
   && printf '%s' "$dark" | grep -q '"paid":false' \
   && printf '%s' "$dark" | grep -q '"delivered":false'; then
  ok "every action defers while dark, with paid and delivered both present and false"
else
  bad "dark gate" "$dark"
fi

# --- LIST --------------------------------------------------------------------
head_ "karum-list: the one take, offline-gated"
CORR_L=aaaaaaaa-0000-0000-0000-000000000001
LIST_SQL=$(build "\"action\":\"karum-list\",\"listing_id\":$LISTING,\"seller_account_id\":$SELLER,\"item_id\":$ITEM,\"template_id\":\"$TEMPLATE\",\"correlation_id\":\"$CORR_L\",\"mode\":\"dry-run\"")
if [[ -z "$LIST_SQL" ]]; then
  bad "could not build karum-list SQL"
else
  psqlq -c "$(reset_sql)" >/dev/null
  out=$(run_chunk "$LIST_SQL" "$CORR_L" -v "listing_id=$LISTING" -v "seller_account_id=$SELLER" \
          -v "item_id=$ITEM" -v "template_id=$TEMPLATE"); rc=$?
  inv=$(scalar "SELECT inventory_id FROM dune.items WHERE id=$ITEM;")
  pos=$(scalar "SELECT position_index FROM dune.items WHERE id=$ITEM;")
  esc=$(psqlq -F'|' -c "SELECT state,item_id,inventory_id,template_id,stack_size,quality_level,seller_ctrl FROM dune.ls_karum_escrow WHERE correlation_id='$CORR_L';" | tr -d '[:space:]')
  if [[ $rc -eq 0 && "$inv" == "$EXCHANGE_INV" ]] \
     && printf '%s' "$out" | grep -q 'RESULT|.*|applied'; then
    ok "item escrowed into exchange inv $EXCHANGE_INV"
  else
    bad "list did not escrow (rc=$rc inv=$inv)" "$(printf '%s' "$out" | tr '\n' ' ' | tail -c 300)"
  fi
  if [[ "$esc" == "held|$ITEM|$EXCHANGE_INV|$TEMPLATE|500|2|7001" ]]; then
    ok "escrow ledger backfilled: stack 500 and grade 2 preserved, seller_ctrl recorded"
  else
    bad "escrow ledger wrong" "got '$esc'"
  fi
  if [[ -n "$pos" && "$pos" -ge 1000000000 ]]; then
    ok "escrow slot $pos allocated above the market bot's range (LT-7 mitigation)"
  else
    bad "position_index not in the Karum range" "got '$pos'"
  fi

  # Replay must not re-run the take.
  out2=$(run_chunk "$LIST_SQL" "$CORR_L" -v "listing_id=$LISTING" -v "seller_account_id=$SELLER" \
           -v "item_id=$ITEM" -v "template_id=$TEMPLATE")
  rows=$(scalar "SELECT count(*) FROM dune.ls_karum_escrow WHERE correlation_id='$CORR_L';")
  if printf '%s' "$out2" | grep -q 'RESULT|.*|replay' && [[ "$rows" == "1" ]]; then
    ok "exact retry replays: one escrow row, take skipped"
  else
    bad "list replay" "$(printf '%s' "$out2" | tr '\n' ' ' | tail -c 300)"
  fi

  # The gate is in the shared library, so it protects this caller with no extra code.
  psqlq -c "$(reset_sql)" >/dev/null
  psqlq -c "UPDATE dune.encrypted_player_state SET online_status='Online' WHERE account_id=$SELLER;" >/dev/null
  out3=$(run_chunk "$LIST_SQL" "$CORR_L" -v "listing_id=$LISTING" -v "seller_account_id=$SELLER" \
           -v "item_id=$ITEM" -v "template_id=$TEMPLATE")
  inv3=$(scalar "SELECT inventory_id FROM dune.items WHERE id=$ITEM;")
  escrows=$(scalar "SELECT count(*) FROM dune.ls_karum_escrow;")
  if printf '%s' "$out3" | grep -q player_online && [[ "$inv3" == "$SELLER_BANK" && "$escrows" == "0" ]]; then
    ok "seller Online -> refused, item unmoved, NO escrow row committed"
  else
    bad "online seller was not refused (inv=$inv3 escrows=$escrows)" "$(printf '%s' "$out3" | tr '\n' ' ' | tail -c 300)"
  fi

  # --- no_category: refuse what could never be handed back -------------------
  # The defect this prevents (2026-07-27, listing 6): every leg that gives the item to
  # somebody builds an exchange order, and category_mask/depth can only be copied from a
  # real order for the same template. With no such order the item reaches nobody -- not the
  # buyer, not the seller, not the operator page. The listing has to refuse instead, and it
  # must leave NOTHING behind when it does.
  #
  # Two shapes, because they failed differently before the fix: no order row at all (the
  # original bug), and an order whose mask is 0 (which the old check let through, producing
  # an order the client files under no category header at all -- the player sees a loss).
  for shape in "no order at all:DELETE FROM dune.dune_exchange_orders;" \
               "only a mask-0 order:UPDATE dune.dune_exchange_orders SET category_mask=0;"; do
    label="${shape%%:*}"; mutate="${shape#*:}"
    psqlq -c "$(reset_sql)" >/dev/null
    psqlq -c "$mutate" >/dev/null
    outc=$(run_chunk "$LIST_SQL" "$CORR_L" -v "listing_id=$LISTING" -v "seller_account_id=$SELLER" \
             -v "item_id=$ITEM" -v "template_id=$TEMPLATE")
    invc=$(scalar "SELECT inventory_id FROM dune.items WHERE id=$ITEM;")
    escc=$(scalar "SELECT count(*) FROM dune.ls_karum_escrow;")
    if printf '%s' "$outc" | grep -q no_category && [[ "$invc" == "$SELLER_BANK" && "$escc" == "0" ]]; then
      ok "$label -> listing refused with no_category, item unmoved, no escrow row"
    else
      bad "$label was not refused (inv=$invc escrows=$escc)" "$(printf '%s' "$outc" | tr '\n' ' ' | tail -c 300)"
    fi
  done

  # --- the snapshot is what makes the return survive ------------------------
  # 🔴 THE ACTUAL BUG. dune_exchange_orders is TRANSIENT: rows go away when an order fills
  # or is culled. So a template that was categorisable when the seller listed can be
  # uncategorisable by the time they cancel -- and before the snapshot, the cancel then had
  # no category to copy and the item was stranded in inventory 610 with no route out.
  #
  # Escrow the item while an order exists, then take that order away, then cancel. The
  # snapshot on the escrow row has to carry it home on its own.
  psqlq -c "$(reset_sql)" >/dev/null
  run_chunk "$LIST_SQL" "$CORR_L" -v "listing_id=$LISTING" -v "seller_account_id=$SELLER" \
    -v "item_id=$ITEM" -v "template_id=$TEMPLATE" >/dev/null
  snap=$(psqlq -F'|' -c "SELECT category_mask,category_depth FROM dune.ls_karum_escrow WHERE correlation_id='$CORR_L';" | tr -d '[:space:]')
  if [[ "$snap" == "64|3" ]]; then
    ok "list captured the category onto the escrow row (mask 64 depth 3)"
  else
    bad "no category snapshot taken" "got '$snap'"
  fi

  CORR_TR=aaaaaaaa-0000-0000-0000-0000000000ff
  TRANS_SQL=$(build "\"action\":\"karum-cancel\",\"listing_id\":$LISTING,\"seller_account_id\":$SELLER,\"price\":$PRICE,\"correlation_id\":\"$CORR_TR\",\"mode\":\"dry-run\"")
  psqlq -c "DELETE FROM dune.dune_exchange_orders;" >/dev/null
  outt=$(run_chunk "$TRANS_SQL" "$CORR_TR" -v "listing_id=$LISTING"); trc=$?
  towner=$(scalar "SELECT owner_id FROM dune.dune_exchange_orders WHERE item_id=$ITEM;")
  tstate=$(scalar "SELECT state FROM dune.ls_karum_escrow WHERE correlation_id='$CORR_L';")
  if [[ $trc -eq 0 && "$towner" == "7001" && "$tstate" == "returned" ]]; then
    ok "template lost its last exchange order mid-escrow -> the snapshot still returns it"
  else
    bad "the return did NOT survive losing the live order (rc=$trc owner=$towner state=$tstate)" \
        "$(printf '%s' "$outt" | tr '\n' ' ' | tail -c 300)"
  fi
fi

# --- BUY ---------------------------------------------------------------------
head_ "karum-buy: payment first, then delivery, as two transactions"
CORR_B=bbbbbbbb-0000-0000-0000-000000000001
BUY_SQL=$(build "\"action\":\"karum-buy\",\"listing_id\":$LISTING,\"buyer_account_id\":$BUYER,\"seller_account_id\":$SELLER,\"amount\":$PRICE,\"correlation_id\":\"$CORR_B\",\"mode\":\"dry-run\"")
PAY_SQL=$(printf '%s' "$BUY_SQL" | awk '/^-- karum-buy transaction B/{exit} {print}')
DLV_SQL=$(printf '%s' "$BUY_SQL" | awk '/^-- karum-buy transaction B/{f=1} f{print}')

relist() {
  psqlq -c "$(reset_sql)" >/dev/null
  run_chunk "$LIST_SQL" "$CORR_L" -v "listing_id=$LISTING" -v "seller_account_id=$SELLER" \
    -v "item_id=$ITEM" -v "template_id=$TEMPLATE" >/dev/null
}

buy_vars=(-v "listing_id=$LISTING" -v "buyer_account_id=$BUYER" -v "seller_account_id=$SELLER"
          -v "amount=$PRICE" -v 'detail={"kind":"karum_buy"}')

if [[ -z "$PAY_SQL" || -z "$DLV_SQL" ]]; then
  bad "could not split the buy into its two transactions"
else
  relist
  pay=$(run_chunk "$PAY_SQL" "$CORR_B" "${buy_vars[@]}"); prc=$?
  bbal=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7002 AND currency_id=0;")
  sbal=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7001 AND currency_id=0;")
  if [[ $prc -eq 0 ]] && printf '%s' "$pay" | grep -q 'PAID|.*|applied' \
     && [[ "$bbal" == "37500" && "$sbal" == "13500" ]]; then
    ok "payment is value-conserving: buyer 50000->37500, seller 1000->13500"
  else
    bad "payment wrong (buyer=$bbal seller=$sbal)" "$(printf '%s' "$pay" | tr '\n' ' ' | tail -c 300)"
  fi

  # 🔴 The reason dune.ls_karum_payments exists.
  pay2=$(run_chunk "$PAY_SQL" "$CORR_B" "${buy_vars[@]}")
  bbal2=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7002 AND currency_id=0;")
  prows=$(scalar "SELECT count(*) FROM dune.ls_karum_payments WHERE correlation_id='$CORR_B'::uuid;")
  if printf '%s' "$pay2" | grep -q 'PAID|.*|replay' && [[ "$bbal2" == "37500" && "$prows" == "1" ]]; then
    ok "retried payment is a REPLAY: not one balance touched, no second charge"
  else
    bad "retried payment double-charged (buyer=$bbal2 rows=$prows)" "$(printf '%s' "$pay2" | tr '\n' ' ' | tail -c 300)"
  fi

  dlv=$(run_chunk "$DLV_SQL" "$CORR_B" "${buy_vars[@]}"); drc=$?
  ord=$(psqlq -F'|' -c "SELECT owner_id,item_id,quality_level,stack_size FROM dune.dune_exchange_orders o JOIN dune.dune_exchange_fulfilled_orders f ON f.order_id=o.id WHERE o.item_id=$ITEM;" | tr -d '[:space:]')
  expiry=$(scalar "SELECT expiration_time FROM dune.dune_exchange_orders WHERE item_id=$ITEM;")
  ctype=$(scalar "SELECT completion_type FROM dune.dune_exchange_fulfilled_orders f JOIN dune.dune_exchange_orders o ON o.id=f.order_id WHERE o.item_id=$ITEM;")
  escstate=$(scalar "SELECT state FROM dune.ls_karum_escrow WHERE correlation_id='$CORR_L';")
  logstate=$(scalar "SELECT status FROM dune.ls_item_delivery_log WHERE correlation_id='$CORR_B';")
  logsrc=$(psqlq -F'|' -c "SELECT source,lane FROM dune.ls_item_delivery_log WHERE correlation_id='$CORR_B';" | tr -d '[:space:]')
  if [[ $drc -eq 0 ]] && printf '%s' "$dlv" | grep -q 'DLV|applied'; then
    ok "delivery adopted the escrowed row into a claim"
  else
    bad "delivery failed (rc=$drc)" "$(printf '%s' "$dlv" | tr '\n' ' ' | tail -c 300)"
  fi
  if [[ "$ord" == "7002|$ITEM|2|500" ]]; then
    ok "order owned by the BUYER, real grade 2 and stack 500 carried through"
  else
    bad "claim structure wrong" "got '$ord' want '7002|$ITEM|2|500'"
  fi
  if [[ -n "$expiry" && "$expiry" != "0" ]]; then
    ok "expiration_time is set, not NULL (a NULL row does not render at all)"
  else
    bad "expiration_time must never be NULL" "got '$expiry'"
  fi
  [[ "$ctype" == "3" ]] && ok "completion_type 3, the only format the client renders" \
                        || bad "completion_type wrong" "got '$ctype'"
  [[ "$escstate" == "delivered" ]] && ok "escrow closed as delivered" \
                                   || bad "escrow state" "got '$escstate'"
  [[ "$logstate" == "pending" && "$logsrc" == "auction|exchange" ]] \
    && ok "delivery log row is auction/exchange and still pending until actually collected" \
    || bad "delivery log" "status=$logstate src=$logsrc"

  # The item never left 610 and was never copied.
  inv=$(scalar "SELECT inventory_id FROM dune.items WHERE id=$ITEM;")
  cnt=$(scalar "SELECT count(*) FROM dune.items WHERE id=$ITEM;")
  [[ "$inv" == "$EXCHANGE_INV" && "$cnt" == "1" ]] \
    && ok "the item was ADOPTED, not minted: still one row, still in 610" \
    || bad "item moved or was copied" "inv=$inv rows=$cnt"

  # Delivery replay must not create a second order.
  dlv2=$(run_chunk "$DLV_SQL" "$CORR_B" "${buy_vars[@]}")
  orders=$(scalar "SELECT count(*) FROM dune.dune_exchange_orders WHERE item_id=$ITEM;")
  if printf '%s' "$dlv2" | grep -q 'DLV|replay' && [[ "$orders" == "1" ]]; then
    ok "retried delivery replays: exactly one claim, never two"
  else
    bad "delivery replay made a second claim (orders=$orders)" "$(printf '%s' "$dlv2" | tr '\n' ' ' | tail -c 300)"
  fi

  # Insufficient funds must roll transaction A back WHOLE: that is what licenses L4 to
  # revert the listing to 'active' instead of stranding it.
  relist
  psqlq -c "UPDATE dune.player_virtual_currency_balances SET balance=5 WHERE player_controller_id=7002;" >/dev/null
  CORR_P=bbbbbbbb-0000-0000-0000-00000000dead
  poor=$(run_chunk "$PAY_SQL" "$CORR_P" "${buy_vars[@]}")
  pbal=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7002 AND currency_id=0;")
  prow=$(scalar "SELECT count(*) FROM dune.ls_karum_payments WHERE correlation_id='$CORR_P'::uuid;")
  if printf '%s' "$poor" | grep -q insufficient_funds && [[ "$pbal" == "5" && "$prow" == "0" ]]; then
    ok "insufficient funds rolls txn A back whole: no balance change, NO payment row"
  else
    bad "insufficient funds (bal=$pbal payment_rows=$prow)" "$(printf '%s' "$poor" | tr '\n' ' ' | tail -c 300)"
  fi

  # Escrow provably gone: must COMMIT reconciled_missing rather than raise, or L4 can
  # neither refund nor see it.
  relist
  psqlq -c "DELETE FROM dune.items WHERE id=$ITEM;" >/dev/null
  CORR_M=bbbbbbbb-0000-0000-0000-00000000f00d
  miss=$(run_chunk "$DLV_SQL" "$CORR_M" "${buy_vars[@]}")
  mstate=$(scalar "SELECT state FROM dune.ls_karum_escrow WHERE correlation_id='$CORR_L';")
  if printf '%s' "$miss" | grep -q 'DLV|escrow_missing' && [[ "$mstate" == "reconciled_missing" ]]; then
    ok "missing escrow COMMITS reconciled_missing instead of raising"
  else
    bad "escrow_missing handling" "state=$mstate $(printf '%s' "$miss" | tr '\n' ' ' | tail -c 200)"
  fi
fi

# --- CANCEL ------------------------------------------------------------------
head_ "karum-cancel: the return is a give down the same claim lane"
CORR_C=cccccccc-0000-0000-0000-000000000001
CANCEL_SQL=$(build "\"action\":\"karum-cancel\",\"listing_id\":$LISTING,\"seller_account_id\":$SELLER,\"price\":$PRICE,\"correlation_id\":\"$CORR_C\",\"mode\":\"dry-run\"")
if [[ -z "$CANCEL_SQL" ]]; then
  bad "could not build karum-cancel SQL"
else
  relist
  before_b=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7002 AND currency_id=0;")
  can=$(run_chunk "$CANCEL_SQL" "$CORR_C" -v "listing_id=$LISTING"); crc=$?
  owner=$(scalar "SELECT owner_id FROM dune.dune_exchange_orders WHERE item_id=$ITEM;")
  cstate=$(scalar "SELECT state FROM dune.ls_karum_escrow WHERE correlation_id='$CORR_L';")
  after_b=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7002 AND currency_id=0;")
  if [[ $crc -eq 0 && "$owner" == "7001" && "$cstate" == "returned" ]]; then
    ok "returned to the SELLER's controller, escrow closed as returned"
  else
    bad "cancel wrong (owner=$owner state=$cstate)" "$(printf '%s' "$can" | tr '\n' ' ' | tail -c 300)"
  fi
  [[ "$before_b" == "$after_b" ]] \
    && ok "no money moved on a cancel (payment only happens at buy time)" \
    || bad "a cancel moved money" "$before_b -> $after_b"
fi

# --- ADMIN REFUND ------------------------------------------------------------
head_ "karum-admin refund: a NEW payments row, never an UPDATE"
CORR_R=dddddddd-0000-0000-0000-000000000001
REFUND_SQL=$(build "\"action\":\"karum-admin\",\"admin_action\":\"refund\",\"listing_id\":$LISTING,\"buyer_account_id\":$BUYER,\"seller_account_id\":$SELLER,\"amount\":$PRICE,\"original_correlation_id\":\"$CORR_B\",\"correlation_id\":\"$CORR_R\",\"mode\":\"dry-run\"")
if [[ -z "$REFUND_SQL" ]]; then
  bad "could not build the refund SQL"
else
  relist
  run_chunk "$PAY_SQL" "$CORR_B" "${buy_vars[@]}" >/dev/null
  ref=$(run_chunk "$REFUND_SQL" "$CORR_R" -v "orig_corr=$CORR_B" -v "listing_id=$LISTING" \
          -v "buyer_account_id=$BUYER" -v "seller_account_id=$SELLER" -v "amount=$PRICE" \
          -v 'detail={"kind":"karum_refund"}'); rrc=$?
  bbal=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7002 AND currency_id=0;")
  origstate=$(scalar "SELECT status FROM dune.ls_karum_payments WHERE correlation_id='$CORR_B'::uuid;")
  revptr=$(scalar "SELECT reversal_corr_id FROM dune.ls_karum_payments WHERE correlation_id='$CORR_B'::uuid;")
  newrow=$(scalar "SELECT count(*) FROM dune.ls_karum_payments WHERE correlation_id='$CORR_R'::uuid;")
  if [[ $rrc -eq 0 && "$bbal" == "50000" && "$origstate" == "reversed" \
        && "$revptr" == "$CORR_R" && "$newrow" == "1" ]]; then
    ok "buyer made whole, original stamped reversed, refund is its own row"
  else
    bad "refund wrong (buyer=$bbal orig=$origstate ptr=$revptr rows=$newrow)" \
        "$(printf '%s' "$ref" | tr '\n' ' ' | tail -c 300)"
  fi
  ref2=$(run_chunk "$REFUND_SQL" "$CORR_R" -v "orig_corr=$CORR_B" -v "listing_id=$LISTING" \
           -v "buyer_account_id=$BUYER" -v "seller_account_id=$SELLER" -v "amount=$PRICE" \
           -v 'detail={"kind":"karum_refund"}')
  bbal2=$(scalar "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=7002 AND currency_id=0;")
  if printf '%s' "$ref2" | grep -q 'REFUND|.*|replay' && [[ "$bbal2" == "50000" ]]; then
    ok "retried refund is a replay: no double-credit"
  else
    bad "retried refund double-credited" "buyer=$bbal2"
  fi
fi

printf '\n== %s passed, %s failed ==\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
