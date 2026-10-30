#!/usr/bin/env bash
# Dune Solari GIFT WRITE tool, game-host resident (sibling of dune-guild-op.sh).
#
# Deployed to the game host:/root/dune-gift-op.sh (mode 0750, owner root). Invoked
# ONLY by the forced-command dispatcher (a 'gift-op' action reading --op-b64-stdin),
# reached over the relay SSH key. Runs as root on the game host and does its own
# `sudo kubectl exec` into the Dune DB pod, no nested ssh.
#
# One value-conserving transaction: debit the sender's BANK Solari and credit the
# recipient's, both via dune.adjust_player_virtual_currency_balance (currency
# 'Solaris'::dune.virtualwallettype = 0 = BANK Solari, DB-authoritative / atomic, NO offline
# gate needed, unlike item grants).
#
# D5 (MANDATORY): the proc's negative-RESULT branch clamps to 0, log_cheating's the
# actor, AND references an undefined in_player_id (so it ERRORS on that path). The
# sender side uses a NEGATIVE delta, so we PRE-CHECK sender_balance >= amount inside
# the locked txn and RAISE if short, that keeps the deduction from ever entering the
# bad branch. This is a correctness requirement, not a nicety.
#
# LASTSIETCH_GIFTS_ENABLED (env, default "1" = LIVE as of 2026-07-07): while off (=0), the
# op is code-complete but refuses, returns {"status":"deferred",...} without opening
# a DB txn. Un-darked after the 7/6 2-account live test proved the value-conserving
# two-adjust path; set LASTSIETCH_GIFTS_ENABLED=0 (revert the default + redeploy) to re-dark.
#
# HARD CONSTRAINTS (same as dune-guild-op.sh):
#   * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session
#     into the ALREADY-RUNNING DB pod.
#   * Every op runs inside BEGIN; ... COMMIT; with -v ON_ERROR_STOP=1.
#   * Every field decoded from the base64 JSON is RE-VALIDATED here.
#   * Both controllers are resolved FRESH inside the txn tombstone-safe.

set -euo pipefail

# --- caps / tunables ---------------------------------------------------------
GIFT_MAX_AMOUNT="${GIFT_MAX_AMOUNT:-5000000}"     # per-gift Solari ceiling (abuse brake)
# Raised 1,000,000 -> 5,000,000 on 2026-08-26, owner-approved, and CONDITIONAL on the
# identity-keyed caps in admin-backend/portal_gift_limits.py shipping in the same change:
# the caps below key on account_id, and multi-accounting is permitted policy, so a 5x
# raise without the identity keying multiplies an existing alt-relay path by five.
GIFT_MAX_PER_DAY="${GIFT_MAX_PER_DAY:-20}"        # max gifts a sender may send / 24h (RMT brake)
GIFT_MAX_PER_PAIR_PER_DAY="${GIFT_MAX_PER_PAIR_PER_DAY:-5}"  # max sender->same-recipient / 24h
LASTSIETCH_GIFTS_ENABLED="${LASTSIETCH_GIFTS_ENABLED:-1}"       # 1 = live (un-darked 2026-07-07); set 0 to re-dark

DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

# --- helpers (mirror dune-guild-op.sh) ---------------------------------------
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
  sudo kubectl exec -i -n "$DB_NS" "$DB_POD" -- \
    sh -c '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"' _ -h localhost -p "$DB_PORT" \
    -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 "$@"
}

# A cap trip or the balance pre-check RAISEs inside the money transaction, which
# rolls back that transaction's own ls_guild_gifts audit row along with the
# money, so a refused gift used to leave NOTHING behind. This writes the attempt
# in a SECOND, independent transaction after the first has already rolled back.
#
# It deliberately does NOT write ls_guild_gifts.fail_reason on the original row:
# that table's idempotency_key is UNIQUE and IS the idempotency gate, so a
# 'failed' row under the same key would make the next attempt with that key
# report "already applied, idempotent replay" having moved no money. Details in
# scripts/sql/ls_gift_failures.sql.
#
# Best-effort by design: it must never mask the real error, so its own failure is
# swallowed and the caller still reports the original transaction failure.
record_failure() {
  local sender="$1" recipient="$2" amount="$3" idem="$4" op="$5" req="$6" reason="$7"
  [[ -n "${DB_NS:-}" && -n "${DB_POD:-}" ]] || return 0
  printf '%s\n' "BEGIN;
INSERT INTO dune.ls_gift_failures
  (idempotency_key, sender_account_id, recipient_account_id, amount,
   operator, requested_by_discord_id, fail_reason)
VALUES (NULLIF(:'idem','')::uuid, :sender, :recipient, :amount,
        NULLIF(:'op',''), NULLIF(:'req',''), :'reason');
COMMIT;" | run_psql -tA \
      -v "idem=${idem}" -v "sender=${sender}" -v "recipient=${recipient}" \
      -v "amount=${amount}" -v "op=${op}" -v "req=${req}" \
      -v "reason=${reason}" >/dev/null 2>&1 || true
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

  local sender_account_id recipient_account_id amount idem operator req_by req_mode
  sender_account_id=$(jq_get '.sender_account_id')
  recipient_account_id=$(jq_get '.recipient_account_id')
  amount=$(jq_get '.amount')
  idem=$(jq_get '.idempotency_key')
  operator=$(jq_get '.operator'); [[ -n "$operator" ]] || operator="unknown"
  req_by=$(jq_get '.requested_by_discord_id')
  req_mode=$(jq_get '.mode'); [[ -n "$req_mode" ]] || req_mode="apply"

  validate_uuid "$idem"
  validate_posint "$sender_account_id" "sender_account_id"
  validate_posint "$recipient_account_id" "recipient_account_id"
  validate_posint "$amount" "amount"
  (( amount <= GIFT_MAX_AMOUNT )) || fail_json "amount exceeds gift ceiling (${GIFT_MAX_AMOUNT})" 2
  [[ "$sender_account_id" != "$recipient_account_id" ]] || fail_json "cannot gift to yourself" 2
  case "$req_mode" in apply|dry-run) ;; *) fail_json "invalid mode: $req_mode" 2 ;; esac

  local dry=0
  [[ "$DRY_RUN" == "1" || "$req_mode" == "dry-run" ]] && dry=1

  # --- dark gate --------------------------------------------------------------
  if [[ "$LASTSIETCH_GIFTS_ENABLED" != "1" ]]; then
    printf '{"success":true,"status":"deferred","message":%s}\n' \
      "$(json_str "Solari gifting is disabled (LASTSIETCH_GIFTS_ENABLED=${LASTSIETCH_GIFTS_ENABLED}); gift not applied")"
    exit 0
  fi

  local DETAIL_JSON
  DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c '{amount: (.amount|tonumber), currency: "solari"}')

  # Rate caps are embedded directly into the txn SQL, re-validate as integers.
  [[ "$GIFT_MAX_PER_DAY" =~ ^[0-9]+$ ]] \
    || fail_json "GIFT_MAX_PER_DAY must be an integer: $GIFT_MAX_PER_DAY" 2
  [[ "$GIFT_MAX_PER_PAIR_PER_DAY" =~ ^[0-9]+$ ]] \
    || fail_json "GIFT_MAX_PER_PAIR_PER_DAY must be an integer: $GIFT_MAX_PER_PAIR_PER_DAY" 2

  resolve_db_pod

  # --- assemble the transaction ----------------------------------------------
  # Idempotency gate (temp table into ls_guild_gifts, OWNER dune) -> resolve
  # both controllers FRESH -> lock both vcb rows deterministically -> PRE-CHECK
  # balance (D5) -> two adjusts (value-conserving). Skipped on replay.
  local TXN
  TXN=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- Idempotency gate + durable audit row, same txn as the mutation.
CREATE TEMP TABLE _gift_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_guild_gifts
    (idempotency_key, sender_account_id, recipient_account_id, amount, currency_id,
     detail, operator, requested_by_discord_id, status, applied_at)
  VALUES
    (:'idem'::uuid, :sender_account_id, :recipient_account_id, :amount,
     0, :'detail'::jsonb, :'operator', :'req_by', 'applied', now())
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_guild_gifts WHERE idempotency_key = :'idem'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS op_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new;

SET LOCAL ls.sender_account_id = :'sender_account_id';
SET LOCAL ls.recipient_account_id = :'recipient_account_id';
SET LOCAL ls.amount = :'amount';

DO \$\$
DECLARE
  v_sender    bigint;
  v_recipient bigint;
  v_amount    bigint := current_setting('ls.amount')::bigint;
  v_sender_bal bigint;
  v_cnt_day   bigint;
  v_cnt_pair  bigint;
  v_is_new    boolean;
BEGIN
  SELECT is_new FROM _gift_gate INTO v_is_new;
  IF NOT v_is_new THEN
    RETURN;  -- idempotent replay: no mutation
  END IF;

  -- Resolve both controllers FRESH, tombstone-safe.
  SELECT eps.player_controller_id INTO v_sender
  FROM dune.encrypted_player_state eps
  WHERE eps.account_id = current_setting('ls.sender_account_id')::bigint
    AND eps.character_state IS DISTINCT FROM 'Deleted'
  ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
  LIMIT 1;
  IF v_sender IS NULL THEN
    RAISE EXCEPTION 'could not resolve controller for sender account %', current_setting('ls.sender_account_id');
  END IF;

  SELECT eps.player_controller_id INTO v_recipient
  FROM dune.encrypted_player_state eps
  WHERE eps.account_id = current_setting('ls.recipient_account_id')::bigint
    AND eps.character_state IS DISTINCT FROM 'Deleted'
  ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
  LIMIT 1;
  IF v_recipient IS NULL THEN
    RAISE EXCEPTION 'could not resolve controller for recipient account %', current_setting('ls.recipient_account_id');
  END IF;

  IF v_sender = v_recipient THEN
    RAISE EXCEPTION 'sender and recipient resolve to the same controller';
  END IF;

  -- Lock both balance rows FOR UPDATE in deterministic controller order (deadlock-safe).
  PERFORM 1 FROM dune.player_virtual_currency_balances
   WHERE currency_id = 'Solaris'::dune.virtualwallettype
     AND player_controller_id IN (v_sender, v_recipient)
   ORDER BY player_controller_id
   FOR UPDATE;

  -- PRE-CHECK (D5): sender BANK Solari >= amount so the sender-side negative
  -- delta never drives the proc into its clamp/log_cheating/undefined branch.
  SELECT balance INTO v_sender_bal FROM dune.player_virtual_currency_balances
   WHERE player_controller_id = v_sender AND currency_id = 'Solaris'::dune.virtualwallettype;
  IF COALESCE(v_sender_bal, 0) < v_amount THEN
    RAISE EXCEPTION 'insufficient balance: sender has % need %', COALESCE(v_sender_bal, 0), v_amount;
  END IF;

  -- Anti-RMT rate limits (contract 5.3): count PRIOR applied gifts (exclude this
  -- txn's own already-inserted audit row) in the last 24h. The current row is in
  -- ls_guild_gifts with status='applied' from the gate CTE, so exclude by id.
  SELECT count(*) INTO v_cnt_day FROM dune.ls_guild_gifts
   WHERE sender_account_id = current_setting('ls.sender_account_id')::bigint
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _gift_gate)
     AND created_at >= now() - interval '1 day';
  IF v_cnt_day >= ${GIFT_MAX_PER_DAY} THEN
    RAISE EXCEPTION 'gift rate exceeded: sender daily cap (% in 24h, max %)', v_cnt_day, ${GIFT_MAX_PER_DAY};
  END IF;

  SELECT count(*) INTO v_cnt_pair FROM dune.ls_guild_gifts
   WHERE sender_account_id = current_setting('ls.sender_account_id')::bigint
     AND recipient_account_id = current_setting('ls.recipient_account_id')::bigint
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _gift_gate)
     AND created_at >= now() - interval '1 day';
  IF v_cnt_pair >= ${GIFT_MAX_PER_PAIR_PER_DAY} THEN
    RAISE EXCEPTION 'gift rate exceeded: sender->recipient daily cap (% in 24h, max %)', v_cnt_pair, ${GIFT_MAX_PER_PAIR_PER_DAY};
  END IF;

  -- Record resolved controllers on the audit row.
  UPDATE dune.ls_guild_gifts
     SET sender_controller_id = v_sender, recipient_controller_id = v_recipient
   WHERE id = (SELECT op_id FROM _gift_gate);

  -- Two adjusts, value-conserving.
  PERFORM dune.adjust_player_virtual_currency_balance(v_sender,    'Solaris'::dune.virtualwallettype, -v_amount);
  PERFORM dune.adjust_player_virtual_currency_balance(v_recipient, 'Solaris'::dune.virtualwallettype,  v_amount);
END
\$\$;

SELECT 'RESULT|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _gift_gate;

COMMIT;
EOF
)

  local -a vargs=(
    -v "idem=${idem}"
    -v "sender_account_id=${sender_account_id}"
    -v "recipient_account_id=${recipient_account_id}"
    -v "amount=${amount}"
    -v "operator=${operator}"
    -v "req_by=${req_by}"
    -v "detail=${DETAIL_JSON}"
  )

  if [[ "$dry" == "1" ]]; then
    printf '{"success":true,"status":"dry-run",'
    printf '"message":"dry-run: SQL built, NOT executed","sql":%s}\n' "$(json_str "$TXN")"
    exit 0
  fi

  local result
  if ! result=$(printf '%s\n' "$TXN" | run_psql -tA "${vargs[@]}" 2>&1); then
    local reason
    reason=$(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)
    record_failure "$sender_account_id" "$recipient_account_id" "$amount" \
                   "$idem" "$operator" "$req_by" "$reason"
    fail_json "gift transaction failed: $reason" 6
  fi

  local row op_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  op_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$op_id" ]] || op_id="null"

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","audit_id":%s,"amount":%s,"message":"already applied, idempotent replay, no change made"}\n' \
      "$op_id" "$amount"
    exit 0
  fi
  printf '{"success":true,"status":"applied","audit_id":%s,"amount":%s,"message":"gift applied"}\n' \
    "$op_id" "$amount"
  exit 0
}

# =============================================================================
# Entry point
# =============================================================================
DRY_RUN=0

main() {
  [[ $# -gt 0 ]] || fail_json "usage: dune-gift-op.sh --op-b64 <b64> [--dry-run] | --op-b64-stdin [--dry-run]" 2
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
