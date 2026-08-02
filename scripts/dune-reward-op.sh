#!/usr/bin/env bash
# Dune login-REWARDS WRITE tool — game-host resident (sibling of dune-gift-op.sh).
#
# Deployed to the game host:/root/dune-reward-op.sh (mode 0750, owner root).
# Invoked ONLY by the forced-command dispatcher (a 'reward-op' action reading
# --op-b64-stdin), reached over the relay SSH key. Runs as root on the game host
# and does its own `sudo kubectl exec` into the Dune DB pod — no nested ssh.
#
# Two reward kinds (login-rewards V2, Phase 1):
#   Per-account cap is CALENDAR-boundary: daily_solari = per UTC calendar day,
#   weekly_item = per ISO week (date_trunc, not a rolling 24h/7d window), so a
#   legit next-period claim is never false-rejected across a midnight/week edge.
#   The UNIQUE(idempotency_key) guard is the real double-grant backstop; the
#   backend keys idempotency = uuid5(account, kind, period).
#
#   daily_solari  — CREDIT ONLY. One atomic txn: idempotency+audit gate row in
#                   dune.ls_reward_claims, then a single positive
#                   dune.adjust_player_virtual_currency_balance (currency
#                   dune.get_solaris_id() = 0 = BANK Solari). A positive delta
#                   never hits the D5 negative-clamp branch (see dune-gift-op.sh),
#                   so there is NO balance pre-check and NO sender/debit side.
#   weekly_item   — mint a fixed item into the player's CHOAM bank (inv_type 30)
#                   via dune-grant.sh G29 bank_items_batch (ONLINE-SAFE, proven
#                   2026-05-23). The reward row is RESERVED status='pending' in a
#                   first txn (idempotency + weekly cap), the mint is delegated to
#                   dune-grant.sh (its own idempotency + audit), then the row is
#                   flipped to 'applied'. We do NOT open-code the items INSERT.
#   monthly_augment — Phase 2 (gated on the bank-render proof-of-life); no writer
#                   path here yet, so it is rejected cleanly.
#
# LASTSIETCH_REWARD_ENABLED (env, default "0" = DARK): while off the op is code-complete
# but refuses — returns {"status":"deferred",...} WITHOUT opening a DB txn.
# Un-dark only after a 1-account live proof in an announced window, mirroring the
# default flip back to the repo the same session.
#
# HARD CONSTRAINTS (same as dune-gift-op.sh):
#   * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql
#     session into the ALREADY-RUNNING DB pod (and shells dune-grant.sh, which
#     does the same).
#   * Every op runs inside BEGIN; ... COMMIT; with -v ON_ERROR_STOP=1.
#   * Every field decoded from the base64 JSON is RE-VALIDATED here.
#   * The player controller is resolved FRESH inside the txn, tombstone-safe.

set -euo pipefail

# --- caps / tunables ---------------------------------------------------------
# The portal grants the accumulate sweep ONE DAY PER TXN (idempotent per rewarded
# day), so each grant is a single ramp rung (<=25k) and the per-UTC-day count can be
# up to a full cycle (7) when a player catches up several unclaimed days at once.
REWARD_MAX_AMOUNT="${REWARD_MAX_AMOUNT:-100000}"          # per-claim Solari ceiling (abuse brake)
REWARD_SOLARI_MAX_PER_DAY="${REWARD_SOLARI_MAX_PER_DAY:-7}"   # applied daily_solari grants / UTC day (one full 7-day sweep)
REWARD_ITEM_MAX_PER_WEEK="${REWARD_ITEM_MAX_PER_WEEK:-1}"     # applied weekly_item claims / ISO week
REWARD_ITEM_QUALITY="${REWARD_ITEM_QUALITY:-3}"          # default grade for weekly_item mint
LASTSIETCH_REWARD_ENABLED="${LASTSIETCH_REWARD_ENABLED:-0}"            # 0 = DARK (default); set 1 to go live
[[ -f /etc/lastsietch/reward-enabled ]] && LASTSIETCH_REWARD_ENABLED=1  # go-live flag file (rm to kill-switch; mirrors servercmd-enabled)
GRANT_SCRIPT="${GRANT_SCRIPT:-/root/dune-grant.sh}"      # G29 bank mint path (weekly_item)

DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

# --- helpers (mirror dune-gift-op.sh) ---------------------------------------
json_str() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

fail_json() {
  local msg="$1"
  local code="${2:-1}"
  printf '{"success":false,"status":"failed","message":%s}\n' "$(json_str "$msg")"
  exit "$code"
}

resolve_db_pod() {
  local ns pod
  ns=$(sudo kubectl get ns -o name 2>/dev/null \
         | sed 's#^namespace/##' \
         | grep -E '^funcom-seabass-' | head -n1 || true)
  if [[ -z "$ns" ]]; then
    echo "FATAL: could not resolve the Dune namespace (no funcom-seabass-* ns)" >&2
    exit 3
  fi
  pod=$(sudo kubectl get pods -n "$ns" -o name 2>/dev/null \
          | sed 's#^pod/##' \
          | grep -E -- '-db-dbdepl-sts-0$' | head -n1 || true)
  if [[ -z "$pod" ]]; then
    echo "FATAL: could not resolve the Dune DB pod in namespace $ns" >&2
    exit 3
  fi
  DB_NS="$ns"
  DB_POD="$pod"
}

run_psql() {
  local pgpass
  pgpass=$(sudo kubectl exec -n "$DB_NS" "$DB_POD" -- printenv POSTGRES_PASSWORD 2>/dev/null)
  if [[ -z "$pgpass" ]]; then
    echo "FATAL: could not read POSTGRES_PASSWORD from DB pod $DB_POD" >&2
    exit 3
  fi
  sudo kubectl exec -i -n "$DB_NS" "$DB_POD" -- \
    env PGPASSWORD="$pgpass" psql -h localhost -p "$DB_PORT" \
    -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 "$@"
}

OP_JSON=""
jq_get() { printf '%s' "$OP_JSON" | jq -r "$1 // empty"; }

validate_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || fail_json "invalid idempotency_key (must be a UUID): $1" 2
}
validate_posint() {
  [[ "$1" =~ ^[0-9]+$ && "$1" != "0" ]] || fail_json "$2 must be a positive integer: $1" 2
}
validate_template_id() {
  [[ "$1" =~ ^[A-Za-z0-9_]+$ ]] || fail_json "invalid template_id (alnum + _ only): $1" 2
}

# =============================================================================
# daily_solari — single atomic txn: gate row + positive credit.
# =============================================================================
do_daily_solari() {
  local account_id="$1" idem="$2" amount="$3" operator="$4" dry="$5"

  validate_posint "$amount" "amount"
  (( amount <= REWARD_MAX_AMOUNT )) || fail_json "amount exceeds reward ceiling (${REWARD_MAX_AMOUNT})" 2
  [[ "$REWARD_SOLARI_MAX_PER_DAY" =~ ^[0-9]+$ ]] \
    || fail_json "REWARD_SOLARI_MAX_PER_DAY must be an integer: $REWARD_SOLARI_MAX_PER_DAY" 2

  local DETAIL_JSON
  DETAIL_JSON=$(jq -nc --argjson amt "$amount" \
    '{amount:$amt, currency:"solari", reward_kind:"daily_solari"}')

  local TXN
  TXN=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- Idempotency gate + durable audit row, same txn as the credit.
CREATE TEMP TABLE _reward_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_reward_claims
    (idempotency_key, account_id, reward_kind, detail, operator, status, applied_at)
  VALUES
    (:'idem'::uuid, :account_id, :'reward_kind', :'detail'::jsonb, :'operator', 'applied', now())
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_reward_claims WHERE idempotency_key = :'idem'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS op_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new;

SET LOCAL ls.account_id = :'account_id';
SET LOCAL ls.amount = :'amount';

DO \$\$
DECLARE
  v_ctrl   bigint;
  v_amount bigint := current_setting('ls.amount')::bigint;
  v_cnt    bigint;
  v_is_new boolean;
BEGIN
  SELECT is_new FROM _reward_gate INTO v_is_new;
  IF NOT v_is_new THEN
    RETURN;  -- idempotent replay: no mutation
  END IF;

  -- Per-account daily cap: prior APPLIED daily_solari claims THIS UTC CALENDAR
  -- DAY (exclude this txn's own already-inserted audit row). Calendar-boundary,
  -- NOT a rolling 24h window, so a legit next-day claim made <24h after the last
  -- one is not false-rejected across UTC midnight. Both sides compared in UTC
  -- wall-clock so the session TimeZone cannot shift the boundary.
  SELECT count(*) INTO v_cnt FROM dune.ls_reward_claims
   WHERE account_id = current_setting('ls.account_id')::bigint
     AND reward_kind = 'daily_solari'
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _reward_gate)
     AND (created_at AT TIME ZONE 'UTC') >= date_trunc('day', now() AT TIME ZONE 'UTC');
  IF v_cnt >= ${REWARD_SOLARI_MAX_PER_DAY} THEN
    RAISE EXCEPTION 'reward cap: account % already has % applied daily_solari this UTC day (max %)',
      current_setting('ls.account_id'), v_cnt, ${REWARD_SOLARI_MAX_PER_DAY};
  END IF;

  -- Resolve the controller FRESH, tombstone-safe (skip Deleted characters).
  SELECT eps.player_controller_id INTO v_ctrl
  FROM dune.encrypted_player_state eps
  WHERE eps.account_id = current_setting('ls.account_id')::bigint
    AND eps.character_state IS DISTINCT FROM 'Deleted'
  ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
  LIMIT 1;
  IF v_ctrl IS NULL THEN
    RAISE EXCEPTION 'could not resolve controller for account %', current_setting('ls.account_id');
  END IF;

  -- CREDIT ONLY. Positive delta never drives the proc into its D5 clamp/
  -- log_cheating/undefined branch, so no balance pre-check is needed.
  PERFORM dune.adjust_player_virtual_currency_balance(v_ctrl, dune.get_solaris_id(), v_amount);

  -- Record the resolved controller on the audit row.
  UPDATE dune.ls_reward_claims
     SET detail = detail || jsonb_build_object('controller_id', v_ctrl)
   WHERE id = (SELECT op_id FROM _reward_gate);
END
\$\$;

SELECT 'RESULT|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _reward_gate;

COMMIT;
EOF
)

  local -a vargs=(
    -v "idem=${idem}"
    -v "account_id=${account_id}"
    -v "amount=${amount}"
    -v "operator=${operator}"
    -v "reward_kind=daily_solari"
    -v "detail=${DETAIL_JSON}"
  )

  if [[ "$dry" == "1" ]]; then
    printf '{"success":true,"status":"dry-run","reward_kind":"daily_solari",'
    printf '"message":"dry-run: SQL built, NOT executed","sql":%s}\n' "$(json_str "$TXN")"
    exit 0
  fi

  resolve_db_pod

  local result
  if ! result=$(printf '%s\n' "$TXN" | run_psql -tA "${vargs[@]}" 2>&1); then
    fail_json "daily_solari transaction failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)" 6
  fi

  local row op_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  op_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$op_id" ]] || op_id="null"

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","reward_kind":"daily_solari","audit_id":%s,"amount":%s,"message":"already applied, idempotent replay, no change made"}\n' \
      "$op_id" "$amount"
    exit 0
  fi
  printf '{"success":true,"status":"applied","reward_kind":"daily_solari","audit_id":%s,"amount":%s,"message":"daily Solari credited"}\n' \
    "$op_id" "$amount"
  exit 0
}

# =============================================================================
# weekly_item — reserve gate row (pending), delegate the mint to dune-grant.sh
# G29 bank_items_batch, then flip the row to applied/failed. The mint is a
# separate script/txn, so it CANNOT share our BEGIN..COMMIT; we lean on the
# reward-side idempotency_key + the grant-side idempotency_key (same UUID, two
# tables) so a relay retry is safe on both halves.
# =============================================================================
do_weekly_item() {
  local account_id="$1" idem="$2" template_id="$3" quality="$4" operator="$5" dry="$6"

  validate_template_id "$template_id"
  [[ "$quality" =~ ^[0-9]+$ ]] || fail_json "quality_level must be an integer 0..5: $quality" 2
  (( quality <= 5 )) || fail_json "quality_level out of range (0..5): $quality" 2
  [[ "$REWARD_ITEM_MAX_PER_WEEK" =~ ^[0-9]+$ ]] \
    || fail_json "REWARD_ITEM_MAX_PER_WEEK must be an integer: $REWARD_ITEM_MAX_PER_WEEK" 2

  local DETAIL_JSON
  DETAIL_JSON=$(jq -nc --arg tpl "$template_id" --argjson q "$quality" \
    '{template_id:$tpl, quality:$q, stack_size:1, reward_kind:"weekly_item", delivery:"bank"}')

  # The grant payload delegated to dune-grant.sh G29 (its own idempotency+audit).
  local GRANT_JSON GRANT_B64
  GRANT_JSON=$(jq -nc \
    --arg idem "$idem" --argjson aid "$account_id" --arg op "$operator" \
    --arg tpl "$template_id" --argjson q "$quality" \
    '{grant_type:"bank_items_batch", account_id:$aid, operator:$op,
      idempotency_key:$idem,
      detail:{items:[{template_id:$tpl, stack_size:1, quality:$q}]}}')
  GRANT_B64=$(printf '%s' "$GRANT_JSON" | base64 -w0)

  if [[ "$dry" == "1" ]]; then
    printf '{"success":true,"status":"dry-run","reward_kind":"weekly_item",'
    printf '"message":"dry-run: gate + grant built, NOT executed","grant_payload":%s}\n' \
      "$(json_str "$GRANT_JSON")"
    exit 0
  fi

  resolve_db_pod

  # --- Phase A: reserve the claim (idempotency + weekly cap) ------------------
  local GATE_SQL
  GATE_SQL=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

CREATE TEMP TABLE _reward_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_reward_claims
    (idempotency_key, account_id, reward_kind, detail, operator, status)
  VALUES
    (:'idem'::uuid, :account_id, :'reward_kind', :'detail'::jsonb, :'operator', 'pending')
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id, status
),
prior AS (
  SELECT id, status FROM dune.ls_reward_claims WHERE idempotency_key = :'idem'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior))         AS op_id,
  (EXISTS (SELECT 1 FROM ins))                                  AS is_new,
  COALESCE((SELECT status FROM ins), (SELECT status FROM prior)) AS status;

SET LOCAL ls.account_id = :'account_id';

DO \$\$
DECLARE v_cnt bigint; v_is_new boolean;
BEGIN
  SELECT is_new FROM _reward_gate INTO v_is_new;
  IF NOT v_is_new THEN
    RETURN;  -- existing claim: cap already counted on first insert
  END IF;
  -- Per-account weekly cap: prior APPLIED weekly_item claims THIS ISO WEEK.
  -- Calendar-boundary (date_trunc('week', ...) = Monday 00:00 UTC), NOT a rolling
  -- 7d window, so a legit next-week claim is not false-rejected across the week
  -- boundary. Both sides compared in UTC wall-clock (session-TZ-independent).
  SELECT count(*) INTO v_cnt FROM dune.ls_reward_claims
   WHERE account_id = current_setting('ls.account_id')::bigint
     AND reward_kind = 'weekly_item'
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _reward_gate)
     AND (created_at AT TIME ZONE 'UTC') >= date_trunc('week', now() AT TIME ZONE 'UTC');
  IF v_cnt >= ${REWARD_ITEM_MAX_PER_WEEK} THEN
    RAISE EXCEPTION 'reward cap: account % already has % applied weekly_item this ISO week (max %)',
      current_setting('ls.account_id'), v_cnt, ${REWARD_ITEM_MAX_PER_WEEK};
  END IF;
END
\$\$;

SELECT 'GATE|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'new' ELSE 'existing' END || '|' || COALESCE(status,'')
  FROM _reward_gate;

COMMIT;
EOF
)

  local -a gate_vargs=(
    -v "idem=${idem}"
    -v "account_id=${account_id}"
    -v "operator=${operator}"
    -v "reward_kind=weekly_item"
    -v "detail=${DETAIL_JSON}"
  )

  local gate_out
  if ! gate_out=$(printf '%s\n' "$GATE_SQL" | run_psql -tA "${gate_vargs[@]}" 2>&1); then
    fail_json "weekly_item gate transaction failed: $(printf '%s' "$gate_out" | tr '\n' ' ' | tail -c 400)" 6
  fi

  local gate_row op_id gate_new gate_status
  gate_row=$(printf '%s' "$gate_out" | grep -E '^GATE\|' | tail -n1)
  op_id=$(printf '%s' "$gate_row" | cut -d'|' -f2 | tr -d '[:space:]')
  gate_new=$(printf '%s' "$gate_row" | cut -d'|' -f3 | tr -d '[:space:]')
  gate_status=$(printf '%s' "$gate_row" | cut -d'|' -f4 | tr -d '[:space:]')
  [[ -n "$op_id" ]] || fail_json "weekly_item gate returned no op_id" 6

  # Already fully applied on a prior call: true replay, do NOT re-mint.
  if [[ "$gate_status" == "applied" ]]; then
    printf '{"success":true,"status":"replay","reward_kind":"weekly_item","audit_id":%s,"template_id":%s,"message":"already applied, idempotent replay, no change made"}\n' \
      "$op_id" "$(json_str "$template_id")"
    exit 0
  fi

  # --- Phase B: delegate the mint to dune-grant.sh G29 (idempotent) -----------
  local grant_out grant_status
  if ! grant_out=$(printf '%s' "$GRANT_B64" | "$GRANT_SCRIPT" --grant-b64-stdin 2>&1); then
    weekly_grant_fail "$op_id" "$grant_out" "grant script exited non-zero"
  fi
  grant_status=$(printf '%s' "$grant_out" | jq -r '.status // empty' 2>/dev/null || true)

  if [[ "$grant_status" != "applied" && "$grant_status" != "replay" ]]; then
    weekly_grant_fail "$op_id" "$grant_out" "grant status=${grant_status:-unknown}"
  fi

  # --- Phase C: flip the reward row to applied --------------------------------
  local grant_id
  grant_id=$(printf '%s' "$grant_out" | jq -r '.grant_id // empty' 2>/dev/null || true)
  local extra
  extra=$(jq -nc --arg gs "$grant_status" --arg gid "${grant_id:-}" \
    '{grant_status:$gs} + (if $gid == "" then {} else {grant_id:($gid|tonumber)} end)')

  local upd_out
  if ! upd_out=$(printf '%s\n' \
    "UPDATE dune.ls_reward_claims SET status='applied', applied_at=now(), detail = detail || :'extra'::jsonb WHERE id = :op_id;" \
    | run_psql -tA -v "op_id=${op_id}" -v "extra=${extra}" 2>&1); then
    # The mint succeeded but the bookkeeping flip failed. Surface loudly; the row
    # stays 'pending' and a retry (same idem) will replay the mint harmlessly and
    # re-attempt the flip.
    fail_json "weekly_item minted (grant_id=${grant_id:-?}) but ledger flip failed: $(printf '%s' "$upd_out" | tr '\n' ' ' | tail -c 300)" 6
  fi

  printf '{"success":true,"status":"applied","reward_kind":"weekly_item","audit_id":%s,"grant_id":%s,"template_id":%s,"message":"weekly item minted to CHOAM bank"}\n' \
    "$op_id" "${grant_id:-null}" "$(json_str "$template_id")"
  exit 0
}

# Best-effort: mark a reserved weekly_item row failed (never masks the real error).
mark_weekly_failed() {
  local op_id="$1" reason="$2" extra
  extra=$(jq -nc --arg r "$reason" '{fail_reason:$r}')
  printf '%s\n' \
    "UPDATE dune.ls_reward_claims SET status='failed', detail = detail || :'extra'::jsonb WHERE id = :op_id;" \
    | run_psql -tA -v "op_id=${op_id}" -v "extra=${extra}" >/dev/null 2>&1 || true
}

# Classify a weekly_item mint failure, mark the row failed (retryable), and emit a
# structured error. A full CHOAM bank (G29 BANK_BATCH_FAIL) is a distinct, retryable
# case (free space + claim again) that must NOT read as a generic/"already" error.
weekly_grant_fail() {
  local op_id="$1" grant_out="$2" ctx="$3"
  local tail; tail=$(printf '%s' "$grant_out" | tr '\n' ' ' | tail -c 300)
  mark_weekly_failed "$op_id" "${ctx}: ${tail}"
  if printf '%s' "$grant_out" | grep -q 'BANK_BATCH_FAIL: bank capacity exceeded'; then
    printf '{"success":false,"status":"failed","error":"bank_full","reward_kind":"weekly_item","message":%s}\n' \
      "$(json_str "Your CHOAM bank is full. Free up some space, then claim again.")"
    exit 6
  fi
  if printf '%s' "$grant_out" | grep -q 'BANK_BATCH_FAIL: no bank inventory'; then
    printf '{"success":false,"status":"failed","error":"bank_unopened","reward_kind":"weekly_item","message":%s}\n' \
      "$(json_str "Open your CHOAM bank in-game once so it exists, then claim.")"
    exit 6
  fi
  fail_json "weekly_item mint failed (${ctx}): ${tail}" 6
}

do_op() {
  local b64="$1"
  if [[ ! "$b64" =~ ^[A-Za-z0-9+/=]+$ ]]; then
    fail_json "op payload is not valid base64" 2
  fi
  local decoded
  decoded=$(printf '%s' "$b64" | base64 -d 2>/dev/null || true)
  [[ -n "$decoded" ]] || fail_json "op payload failed base64 decode" 2
  printf '%s' "$decoded" | jq -e . >/dev/null 2>&1 || fail_json "op payload is not valid JSON" 2
  OP_JSON="$decoded"

  local account_id reward_kind idem amount template_id quality operator req_mode
  account_id=$(jq_get '.account_id')
  reward_kind=$(jq_get '.reward_kind')
  idem=$(jq_get '.idempotency_key')
  amount=$(jq_get '.amount')
  template_id=$(jq_get '.template_id')
  quality=$(jq_get '.quality_level')
  operator=$(jq_get '.operator'); [[ -n "$operator" ]] || operator="lastsietch-reward"
  req_mode=$(jq_get '.mode'); [[ -n "$req_mode" ]] || req_mode="apply"

  validate_uuid "$idem"
  validate_posint "$account_id" "account_id"
  case "$reward_kind" in
    daily_solari|weekly_item|monthly_augment) ;;
    *) fail_json "invalid reward_kind: $reward_kind" 2 ;;
  esac
  case "$req_mode" in apply|dry-run) ;; *) fail_json "invalid mode: $req_mode" 2 ;; esac

  local dry=0
  [[ "$DRY_RUN" == "1" || "$req_mode" == "dry-run" ]] && dry=1

  # --- dark gate --------------------------------------------------------------
  if [[ "$LASTSIETCH_REWARD_ENABLED" != "1" ]]; then
    printf '{"success":true,"status":"deferred","reward_kind":%s,"message":%s}\n' \
      "$(json_str "$reward_kind")" \
      "$(json_str "login rewards are disabled (LASTSIETCH_REWARD_ENABLED=${LASTSIETCH_REWARD_ENABLED}); reward not applied")"
    exit 0
  fi

  case "$reward_kind" in
    daily_solari)
      do_daily_solari "$account_id" "$idem" "$amount" "$operator" "$dry"
      ;;
    weekly_item)
      [[ -n "$quality" ]] || quality="$REWARD_ITEM_QUALITY"
      do_weekly_item "$account_id" "$idem" "$template_id" "$quality" "$operator" "$dry"
      ;;
    monthly_augment)
      fail_json "monthly_augment is Phase 2 (gated on the bank-render proof-of-life); no writer path yet" 2
      ;;
  esac
}

# =============================================================================
# Entry point
# =============================================================================
DRY_RUN=0

main() {
  [[ $# -gt 0 ]] || fail_json "usage: dune-reward-op.sh --op-b64 <b64> [--dry-run] | --op-b64-stdin [--dry-run]" 2
  local mode="" b64=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --op-b64)
        mode="op"
        b64="${2:-}"
        [[ -n "$b64" ]] || fail_json "--op-b64 requires a base64 payload" 2
        shift 2
        ;;
      --op-b64-stdin)
        mode="op"
        b64=$(cat)
        b64="${b64//[[:space:]]/}"
        [[ -n "$b64" ]] || fail_json "--op-b64-stdin requires a base64 payload on stdin" 2
        shift
        ;;
      --dry-run) DRY_RUN=1; shift ;;
      *) fail_json "unknown argument: $1" 2 ;;
    esac
  done
  case "$mode" in
    op) do_op "$b64" ;;
    *)  fail_json "no mode given (--op-b64 or --op-b64-stdin)" 2 ;;
  esac
}

main "$@"
