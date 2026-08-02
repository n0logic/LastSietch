#!/usr/bin/env bash
# QA harness for dune-market-sell.py --dry-run validation.
# Must run ON lastsietch-dune (needs /root/dq.sh). NEVER runs a live list.
#
# Pre-staged test data (verified against live DB 2026-06-05):
#   VALID-PATH seller: ctrl=569 acct=253 OFFLINE  bank=10620659
#   ONLINE-GATE :      ctrl=3041 acct=1644 a reference account ONLINE
#   item for valid+negatives: 2032950  FremenComponent2  stack=23  (owned by ctrl=569)
#   item for over-count:      2032838  GreatHouseComponent2  stack=3  (owned by ctrl=569)
#   item for not_owner:       203380378  ScrapMetal  (owned by acct=1644, NOT ctrl=569)
#   category_mask for FremenComponent2: 84017152 depth=2  (confirmed from live orders)
#   game epoch anchor: MAX(exp)=12128906  game_now_est=12042506
#   Baseline: orders=3292 / sell_orders=3290
#
# dry-run output schema (do_dry_run):
#   {dry_run, seller_ctrl, item_id, template_id, owned, online_status, in_grace,
#    stack, count, quality, unit_price, duration_days, category_mask, category_depth,
#    game_now, expiration_time, fee, bank_before, bank_after_projected, max_orders,
#    preflight_errors: [...]}
# NB: dry-run never emits {ok, error}. Errors surface in preflight_errors[].
#     Hard fail (exit 1, {ok:false,error:...}) only if item_id does not exist.

set -euo pipefail

WRITER="/tmp/dune-market-sell.py"
DQ="/root/dq.sh"

VALID_CTRL=569
VALID_ACCT=253
ONLINE_CTRL=3041
ONLINE_ACCT=1644

VALID_ITEM=2032950        # FremenComponent2  stack=23  ctrl=569 owns
OVER_ITEM=2032838         # GreatHouseComponent2  stack=3  ctrl=569 owns
NOT_OWNED_ITEM=203380378  # ScrapMetal  owned by acct=1644, NOT ctrl=569

VALID_PRICE=5000
VALID_DAYS=3
EXPECTED_FEE=260          # round(0.01*5000*4)+20*3 = 200+60

pass_count=0
fail_count=0
stop_ship=0

log()     { echo "[$(date -u +%T)] $*"; }
pass()    { echo "  [PASS] $*"; pass_count=$(( pass_count + 1 )); }
fail()    { echo "  [FAIL] $*"; fail_count=$(( fail_count + 1 )); }
stopship(){ echo "  [STOP-SHIP] $*"; fail_count=$(( fail_count + 1 )); stop_ship=$(( stop_ship + 1 )); }
header()  { echo; echo "═══ $* ═══"; }
dq()      { "$DQ" -tAc "$1" 2>/dev/null; }

jget() {
    # jget <json_string> <field>  -> field value or __MISSING__
    python3 -c "
import json,sys
try:
    d=json.loads(sys.argv[1])
    v=d.get(sys.argv[2],'__MISSING__')
    print('__MISSING__' if v is None else v)
except Exception:
    print('__MISSING__')
" "$1" "$2" 2>/dev/null || echo "__MISSING__"
}

jpreflight() {
    # jpreflight <json_string> -> space-separated list of preflight_errors tokens
    python3 -c "
import json,sys
try:
    d=json.loads(sys.argv[1])
    print(' '.join(d.get('preflight_errors',[]) or []))
except Exception:
    print('__MISSING__')
" "$1" 2>/dev/null || echo "__MISSING__"
}

run_dry() {
    # run_dry <item_id> <count> <seller_ctrl> <price> <duration_days>
    python3 "$WRITER" \
        --item-id "$1" \
        --count "$2" \
        --seller-ctrl "$3" \
        --price "$4" \
        --duration-days "$5" \
        --dry-run 2>&1 || true
}

snapshot() {
    # snapshot <item_id> <seller_ctrl> -> "item_json|order_count|sell_count|bank"
    local it
    it=$(dq "SELECT row_to_json(r) FROM (SELECT id,template_id,stack_size,quality_level,inventory_id FROM dune.items WHERE id=${1}::bigint) r;")
    local oc sc bk
    oc=$(dq "SELECT COUNT(*)::int FROM dune.dune_exchange_orders;")
    sc=$(dq "SELECT COUNT(*)::int FROM dune.dune_exchange_sell_orders;")
    bk=$(dq "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=${2} AND currency_id=0;")
    echo "${it}|${oc}|${sc}|${bk}"
}

assert_no_mutation() {
    local label="$1" before="$2" after="$3"
    if [ "$before" = "$after" ]; then
        pass "mutation [${label}]: byte-identical"
    else
        stopship "mutation [${label}]: CHANGED before='${before}' after='${after}'"
    fi
}

assert_preflight_has() {
    # assert_preflight_has <label> <expected_token> <preflight_errors_string>
    local label="$1" token="$2" errors="$3"
    if echo "$errors" | grep -qw "$token"; then
        pass "preflight [${label}]: contains '${token}' (errors: ${errors:-<none>})"
    else
        fail "preflight [${label}]: missing '${token}' in preflight_errors '${errors}'"
    fi
}

assert_preflight_empty() {
    local label="$1" errors="$2"
    if [ -z "$errors" ]; then
        pass "preflight [${label}]: empty (no errors)"
    else
        fail "preflight [${label}]: expected no errors but got '${errors}'"
    fi
}

# ── PRE-FLIGHT CHECKS ────────────────────────────────────────────────────────
header "PRE-FLIGHT: verify writer + test assets"

[ -f "$WRITER" ] || { echo "FATAL: ${WRITER} not found -- scp it first"; exit 1; }

VALID_STATUS=$(dq "SELECT online_status FROM dune.encrypted_player_state WHERE account_id=${VALID_ACCT}::bigint LIMIT 1;")
ONLINE_STATUS=$(dq "SELECT online_status FROM dune.encrypted_player_state WHERE account_id=${ONLINE_ACCT}::bigint LIMIT 1;")

log "ctrl=${VALID_CTRL} status : ${VALID_STATUS}"
log "ctrl=${ONLINE_CTRL} status: ${ONLINE_STATUS}"

[ "$VALID_STATUS" = "Offline" ] && pass "valid-path seller (ctrl=${VALID_CTRL}) is Offline" \
    || fail "valid-path seller is ${VALID_STATUS} -- valid-path test may fail with player_online"

[ "$ONLINE_STATUS" = "Online" ] && pass "a reference account (ctrl=${ONLINE_CTRL}) is Online (player_online gate)" \
    || log "WARNING: a reference account is ${ONLINE_STATUS}; player_online gate test may not fire as expected"

LIVE_STACK=$(dq "SELECT stack_size FROM dune.items WHERE id=${VALID_ITEM}::bigint;")
log "item ${VALID_ITEM} live stack_size: ${LIVE_STACK}"
OVER_STACK=$(dq "SELECT stack_size FROM dune.items WHERE id=${OVER_ITEM}::bigint;")
OVER_COUNT=$(( OVER_STACK + 1 ))
log "over-count item ${OVER_ITEM} stack=${OVER_STACK}; test count=${OVER_COUNT}"

# ── STEP 1: Pre-snapshot ─────────────────────────────────────────────────────
header "STEP 1: Pre-snapshot"
BEFORE=$(snapshot "$VALID_ITEM" "$VALID_CTRL")
IFS='|' read -r ITEM_BEFORE ORD_BEFORE SELL_BEFORE BANK_BEFORE <<< "$BEFORE"
log "item row         : ${ITEM_BEFORE}"
log "exchange_orders  : ${ORD_BEFORE}"
log "sell_orders      : ${SELL_BEFORE}"
log "ctrl=${VALID_CTRL} bank : ${BANK_BEFORE}"

# ── STEP 2: Dry-run valid list ───────────────────────────────────────────────
header "STEP 2: Valid-path dry-run (TEST 1)"

OUT=$(run_dry "$VALID_ITEM" 1 "$VALID_CTRL" "$VALID_PRICE" "$VALID_DAYS")
log "Output: ${OUT}"

# Must be valid JSON
if python3 -c "import json,sys; json.loads(sys.argv[1])" "$OUT" 2>/dev/null; then
    pass "valid dry-run: output is valid JSON"
else
    fail "valid dry-run: not valid JSON ('${OUT}')"
    OUT='{"preflight_errors":["parse_failed"]}'
fi

DRY_FLAG=$(jget "$OUT" "dry_run")
[ "$DRY_FLAG" = "True" ] && pass "valid dry-run: dry_run=True" \
    || fail "valid dry-run: dry_run field is '${DRY_FLAG}' (expected True)"

PF_ERRS=$(jpreflight "$OUT")
assert_preflight_empty "valid-path" "$PF_ERRS"

# Fee
ACTUAL_FEE=$(jget "$OUT" "fee")
[ "$ACTUAL_FEE" = "$EXPECTED_FEE" ] && pass "valid dry-run: fee=${ACTUAL_FEE} == ${EXPECTED_FEE}" \
    || fail "valid dry-run: fee mismatch got=${ACTUAL_FEE} expected=${EXPECTED_FEE}"

# expiration_time: present, game-time range 12M-20M
EXP=$(jget "$OUT" "expiration_time")
if [ "$EXP" != "__MISSING__" ] && [ "$EXP" != "None" ]; then
    pass "valid dry-run: expiration_time present (${EXP})"
    EXP_INT=$(python3 -c "print(int('${EXP}'))" 2>/dev/null || echo 0)
    if [ "$EXP_INT" -gt 12000000 ] && [ "$EXP_INT" -lt 20000000 ]; then
        pass "valid dry-run: expiration_time ${EXP_INT} in game-time range (12M-20M)"
    else
        fail "valid dry-run: expiration_time ${EXP_INT} outside game-time range"
    fi
else
    fail "valid dry-run: expiration_time missing or null (${EXP})"
fi

# category_mask: non-null, non-zero
CAT=$(jget "$OUT" "category_mask")
if [ "$CAT" = "84017152" ]; then
    pass "valid dry-run: category_mask=84017152 (matches live order lookup)"
elif [ "$CAT" != "__MISSING__" ] && [ "$CAT" != "0" ] && [ "$CAT" != "None" ]; then
    pass "valid dry-run: category_mask=${CAT} (non-zero; staging snapshot was 84017152)"
else
    fail "valid dry-run: category_mask unresolved (${CAT})"
fi

# ── STEP 3: Post-snapshot -- zero mutation ────────────────────────────────────
header "STEP 3: Post-snapshot (mutation check)"

AFTER=$(snapshot "$VALID_ITEM" "$VALID_CTRL")
IFS='|' read -r ITEM_AFTER ORD_AFTER SELL_AFTER BANK_AFTER <<< "$AFTER"

assert_no_mutation "item row"    "$ITEM_BEFORE"  "$ITEM_AFTER"
assert_no_mutation "order count" "$ORD_BEFORE"   "$ORD_AFTER"
assert_no_mutation "sell count"  "$SELL_BEFORE"  "$SELL_AFTER"
assert_no_mutation "bank"        "$BANK_BEFORE"  "$BANK_AFTER"

if [ "$stop_ship" -gt 0 ]; then
    echo; echo "STOP-SHIP: mutation in dry-run -- aborting"; exit 1
fi

# ── STEP 4: Fee formula spot-checks ──────────────────────────────────────────
header "STEP 4: Fee formula verification (TEST 3)"

check_fee() {
    local price="$1" days="$2" expected="$3" label="$4"
    local o f
    o=$(run_dry "$VALID_ITEM" 1 "$VALID_CTRL" "$price" "$days")
    f=$(jget "$o" "fee")
    [ "$f" = "$expected" ] && pass "fee(p=${price},d=${days})=${f} == ${expected}  [${label}]" \
        || fail "fee(p=${price},d=${days})=${f} != ${expected}  [${label}]"
}

# Contract-verified pairs (all cross-checked against in-game screenshots)
check_fee 10000  14 1780  "contract"
check_fee 1500   1  50    "contract"
check_fee 1500   3  120   "contract"
check_fee 1500   7  260   "contract"
check_fee 1500   14 505   "contract"
check_fee 500000 3  20060 "contract"
check_fee 500000 7  40140 "contract"
check_fee 500000 14 75280 "contract"
# QA spot-checks hand-computed: (p*(d+1)+50)//100 + 20d
check_fee 100     1 22    "QA: (200+50)//100+20=22"
check_fee 5000    3 260   "QA: (20000+50)//100+60=260"
check_fee 1000000 7 80140 "QA: (8000000+50)//100+140=80140"

# ── STEP 5: Negatives ─────────────────────────────────────────────────────────
header "STEP 5: Negatives (TEST 2)"

neg_snapshot() {
    # Snapshot just the counts + a reference account's bank to check for mutation after each negative
    local oc sc bk
    oc=$(dq "SELECT COUNT(*)::int FROM dune.dune_exchange_orders;")
    sc=$(dq "SELECT COUNT(*)::int FROM dune.dune_exchange_sell_orders;")
    bk=$(dq "SELECT balance FROM dune.player_virtual_currency_balances WHERE player_controller_id=${VALID_CTRL} AND currency_id=0;")
    echo "${oc}|${sc}|${bk}"
}

assert_neg_no_mutation() {
    local label="$1"
    local snap
    snap=$(neg_snapshot)
    IFS='|' read -r oc sc bk <<< "$snap"
    local ok=1
    [ "$oc" = "$ORD_BEFORE" ]  || { stopship "neg [${label}]: orders changed ${ORD_BEFORE}->${oc}"; ok=0; }
    [ "$sc" = "$SELL_BEFORE" ] || { stopship "neg [${label}]: sell_orders changed ${SELL_BEFORE}->${sc}"; ok=0; }
    [ "$bk" = "$BANK_BEFORE" ] || { stopship "neg [${label}]: bank changed ${BANK_BEFORE}->${bk}"; ok=0; }
    [ "$ok" = "1" ] && pass "neg [${label}]: no mutation"
}

# --- Negative 1: player_online (a reference account is ONLINE) ---
# Use a reference account's own item so owned=True; only player_online fires
# Item 203380378 (ScrapMetal) is in a reference account's container.
log "player_online test: seller=ctrl=${ONLINE_CTRL} online_status=${ONLINE_STATUS}"
PO_OUT=$(run_dry "$NOT_OWNED_ITEM" 1 "$ONLINE_CTRL" 1000 1)
PO_PF=$(jpreflight "$PO_OUT")
assert_preflight_has "player_online" "player_online" "$PO_PF"
# a reference account owns NOT_OWNED_ITEM (it's ScrapMetal from a reference account's container)
# so owned should be True and not_owner should NOT fire
if echo "$PO_PF" | grep -qw "not_owner"; then
    fail "player_online: unexpectedly got not_owner too (check item ownership)"
else
    pass "player_online: owned=True, only player_online gated (no spurious not_owner)"
fi
assert_neg_no_mutation "player_online"

# --- Negative 2: not_owner (ctrl=569 tries to list a reference account's ScrapMetal) ---
log "not_owner test: seller=ctrl=${VALID_CTRL} item=${NOT_OWNED_ITEM} (acct=1644 owns it)"
NO_OUT=$(run_dry "$NOT_OWNED_ITEM" 1 "$VALID_CTRL" 1000 1)
NO_PF=$(jpreflight "$NO_OUT")
assert_preflight_has "not_owner" "not_owner" "$NO_PF"
# ctrl=569 is OFFLINE so player_online should NOT fire
if echo "$NO_PF" | grep -qw "player_online"; then
    fail "not_owner: unexpected player_online for offline ctrl=${VALID_CTRL}"
else
    pass "not_owner: ctrl=${VALID_CTRL} is offline (no spurious player_online)"
fi
assert_neg_no_mutation "not_owner"

# --- Negative 3: count_exceeds_stack ---
log "count_exceeds_stack test: item=${OVER_ITEM} stack=${OVER_STACK} count=${OVER_COUNT}"
CS_OUT=$(run_dry "$OVER_ITEM" "$OVER_COUNT" "$VALID_CTRL" 1000 1)
CS_PF=$(jpreflight "$CS_OUT")
assert_preflight_has "count_exceeds_stack" "count_exceeds_stack" "$CS_PF"
assert_neg_no_mutation "count_exceeds_stack"

# --- Negative 4: insufficient_bank ---
# fee(600000000,1) = (600000000*2+50)//100 + 20 = 12000000+20 = 12000020
# ctrl=569 bank = ~10620659  =>  12000020 > 10620659
INSUF_PRICE=600000000
INSUF_DAYS=1
INSUF_FEE=$(python3 -c "print((${INSUF_PRICE}*(${INSUF_DAYS}+1)+50)//100 + 20*${INSUF_DAYS})")
log "insufficient_bank test: price=${INSUF_PRICE} days=${INSUF_DAYS} fee=${INSUF_FEE} bank=${BANK_BEFORE}"
IB_OUT=$(run_dry "$VALID_ITEM" 1 "$VALID_CTRL" "$INSUF_PRICE" "$INSUF_DAYS")
IB_PF=$(jpreflight "$IB_OUT")
assert_preflight_has "insufficient_bank" "insufficient_bank" "$IB_PF"
# Sanity: bank really is less than fee
if python3 -c "exit(0 if int('${BANK_BEFORE}') < ${INSUF_FEE} else 1)"; then
    pass "insufficient_bank: bank=${BANK_BEFORE} < fee=${INSUF_FEE} (correct)"
else
    fail "insufficient_bank: bank=${BANK_BEFORE} >= fee=${INSUF_FEE} (test parameters wrong)"
fi
assert_neg_no_mutation "insufficient_bank"

# ── STEP 6: JSON shape ─────────────────────────────────────────────────────────
header "STEP 6: JSON output shape (TEST 4)"

# Success shape fields (from do_dry_run)
SHAPE_OUT=$(run_dry "$VALID_ITEM" 1 "$VALID_CTRL" "$VALID_PRICE" "$VALID_DAYS")
for field in dry_run seller_ctrl item_id template_id owned online_status stack count \
             unit_price duration_days category_mask category_depth game_now \
             expiration_time fee bank_before bank_after_projected preflight_errors; do
    v=$(jget "$SHAPE_OUT" "$field")
    [ "$v" != "__MISSING__" ] && pass "shape: field '${field}' present" \
        || fail "shape: field '${field}' MISSING"
done

# Error token field in hard-fail case (item_not_found, item does not exist)
NOTEXIST_OUT=$(python3 "$WRITER" --item-id 999999999 --count 1 --seller-ctrl "$VALID_CTRL" \
    --price 1000 --duration-days 1 --dry-run 2>&1 || true)
NE_OK=$(jget "$NOTEXIST_OUT" "ok")
NE_ERR=$(jget "$NOTEXIST_OUT" "error")
if [ "$NE_OK" = "False" ] || [ "$NE_OK" = "false" ]; then
    pass "hard-fail (item_not_found): ok=false"
else
    fail "hard-fail (item_not_found): expected ok=false, got ok=${NE_OK}"
fi
if [ "$NE_ERR" = "item_not_found" ]; then
    pass "hard-fail (item_not_found): error=item_not_found"
else
    fail "hard-fail (item_not_found): expected error=item_not_found, got ${NE_ERR}"
fi

# ── FINAL MUTATION SWEEP ───────────────────────────────────────────────────────
header "Final mutation sweep"
FINAL=$(snapshot "$VALID_ITEM" "$VALID_CTRL")
IFS='|' read -r ITEM_FINAL ORD_FINAL SELL_FINAL BANK_FINAL <<< "$FINAL"
assert_no_mutation "item (final)"    "$ITEM_BEFORE"  "$ITEM_FINAL"
assert_no_mutation "orders (final)"  "$ORD_BEFORE"   "$ORD_FINAL"
assert_no_mutation "sells (final)"   "$SELL_BEFORE"  "$SELL_FINAL"
assert_no_mutation "bank (final)"    "$BANK_BEFORE"  "$BANK_FINAL"

# ── SUMMARY ───────────────────────────────────────────────────────────────────
header "QA SUMMARY"
echo "  PASS  : ${pass_count}"
echo "  FAIL  : ${fail_count}"
if [ "$stop_ship" -gt 0 ]; then
    echo "  STOP-SHIP: ${stop_ship} critical mutation(s) -- DO NOT DEPLOY"
fi
echo
if [ "$fail_count" -eq 0 ]; then
    echo "  RESULT: ALL PASS -- READY for reviewer + supervisor live test"
    exit 0
else
    echo "  RESULT: ${fail_count} FAILURE(S) -- review above"
    exit 1
fi
