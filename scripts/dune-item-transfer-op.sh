#!/usr/bin/env bash
# Dune CHOAM BANK item transfer WRITE tool — game-host resident (sibling of
# dune-gift-op.sh). Tier 5 of the portal social layer.
#
# Deployed to the game host:/root/dune-item-transfer-op.sh (mode 0750, owner root).
# Invoked ONLY by the forced-command dispatcher (an 'item-transfer-op' action reading
# --op-b64-stdin), reached over the relay SSH key. Runs as root on the game host and
# does its own `sudo kubectl exec` into the Dune DB pod — no nested ssh.
#
# WHAT IT DOES: moves ONE whole item stack from the SENDER's CHOAM bank into the
# RECIPIENT's CHOAM bank, atomically, in a single txn. The move is a single-row
# re-home:
#
#     UPDATE dune.items
#        SET inventory_id = <recipient bank>, position_index = <max+1>, is_new = true
#      WHERE id = <item_id> AND inventory_id = <sender bank>;
#
# Pinning the WHERE to the sender's OWN bank inventory makes ownership, atomicity, and
# dupe/loss-safety structural: the sender can only move an item that is in their own
# bank, exactly one row is affected, and the row is moved (never copied). A second run
# finds 0 rows in the source and is a clean no-op (and the idempotency gate turns an
# exact retry into a replay). This SUPERSEDES the delete+insert design in the 7/6
# contract, which had a dupe/loss window.
#
# 🔴 THE "ONLINE-SAFE" CLAIM THIS SCRIPT WAS BUILT ON IS FALSE. CORRECTED 2026-07-26.
# It used to read: "both banks are inventory_type=30, which is server-queried on bank-UI
# open, NO offline gate needed." Re-tested live on build 24376904 and both halves are wrong:
#
#   * The bank is NOT queried on UI open. It loads at a ZONE TRANSITION. Closing and
#     reopening the bank menu does not refresh it; the owner had to fly out and back.
#   * Removing an item from an ONLINE player's bank is NOT durable. It holds only until
#     that player's session moves the item, at which point the server restores it under
#     its ORIGINAL primary key and it survives a full reload. That is a duplication path.
#
# ✅ FIXED 2026-07-27 (Karum contract Phase 0, owner decision D3). THE SENDER IS NOW
# OFFLINE-GATED, and the gate is not written here: it lives in the SHARED take,
# scripts/lib/dune-take-item.sh, which this script and the Phase 1 Karum writer both
# source. One gated-take implementation in the codebase, not two. The take does the
# offline gate under a row lock (fail-closed on undetermined status), locks the item row
# pinned to the sender's own bank, runs the template identity guard, and performs the
# single-row re-home. This script keeps only what is its own: idempotency, anti-RMT rate
# caps, recipient capacity, and the ls_item_transfers ledger, all in the SAME
# transaction as the take.
#
# ⚠️ STILL UNTESTED, and it is why this stays DARK: the 7/26 test characterised a DELETE.
# This is an UPDATE that re-homes the row. A sender whose session still holds the stale
# item could plausibly yank it back OUT of the recipient's bank by moving it. The gate is
# supposed to make that unreachable; LT-0 (ops/bank-online-delete-test/run.sh:
# insert -> update-move -> update-verify) tells us what happens if the gate ever
# regresses. Do NOT un-dark before LT-0 is run and recorded.
#
# See and
#: give is online-safe, TAKE is not.
#
# WHOLE STACKS ONLY: partial-stack split (send N of a stack of M) is intentionally NOT
# supported — a split reintroduces a delete+insert-of-remainder window. Deferred.
#
# LASTSIETCH_ITEM_TRANSFER_ENABLED (env, default "0" = OFF/DARK): while off, the op is
# code-complete but refuses — returns {"status":"deferred",...} without opening a DB
# txn. Un-dark (default -> 1, redeploy) ONLY after the 2-account live go/no-go.
#
# HARD CONSTRAINTS (same as dune-gift-op.sh):
#   * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session
#     into the ALREADY-RUNNING DB pod.
#   * Every op runs inside BEGIN; ... COMMIT; with -v ON_ERROR_STOP=1.
#   * Every field decoded from the base64 JSON is RE-VALIDATED here.
#   * Both banks are resolved FRESH inside the txn, tombstone-safe (pawn-keyed).

set -euo pipefail

# --- caps / tunables ---------------------------------------------------------
XFER_MAX_PER_DAY="${XFER_MAX_PER_DAY:-30}"              # max transfers a sender may send / 24h (RMT brake)
XFER_MAX_PER_PAIR_PER_DAY="${XFER_MAX_PER_PAIR_PER_DAY:-15}"  # max sender->same-recipient / 24h
LASTSIETCH_ITEM_TRANSFER_ENABLED="${LASTSIETCH_ITEM_TRANSFER_ENABLED:-0}"   # 0 = dark/refuse; 1 = live

DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

# --- helpers (mirror dune-gift-op.sh) ---------------------------------------
json_str() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

# The optional 3rd arg is a stable error token, emitted as "error". The relay passes our
# JSON through verbatim and the portal route already reads result["error"], so a token
# here is the difference between the UI saying "log out of the game first" and saying
# "that transfer could not be completed".
fail_json() {
  local msg="$1"
  local code="${2:-1}"
  local token="${3:-}"
  if [[ -n "$token" ]]; then
    printf '{"success":false,"status":"failed","error":%s,"message":%s}\n' \
      "$(json_str "$token")" "$(json_str "$msg")"
  else
    printf '{"success":false,"status":"failed","message":%s}\n' "$(json_str "$msg")"
  fi
  exit "$code"
}

# Every RAISE on this path leads with a stable lowercase token: player_online, no_bank,
# item_not_found and take_failed come from the shared take, bank_full and rate_limited
# from this script. Matched as substrings of the psql error, newest-cause-wins is not a
# concern because a transaction only aborts once.
XFER_ERROR_TOKENS=(player_online no_bank item_not_found take_failed bank_full rate_limited)

error_token_of() {
  local blob token
  blob=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  for token in "${XFER_ERROR_TOKENS[@]}"; do
    case "$blob" in *"$token"*) printf '%s' "$token"; return 0 ;; esac
  done
  printf 'write_failed'
}

# The gated take is SHARED, never reimplemented here (Karum contract D3 / section 3.1).
# Sourced lazily, AFTER the dark gate, so a box that has the writer but not yet the
# library still returns a clean {status:"deferred"} instead of a new failure mode.
# Repo layout is scripts/lib/, host layout is /root/lib/; dirname covers both.
require_take_lib() {
  local lib
  lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/dune-take-item.sh"
  if [[ ! -r "$lib" ]]; then
    fail_json "shared take library missing at $lib (deploy scripts/lib/dune-take-item.sh beside this writer)" 3 "write_failed"
  fi
  # shellcheck source=lib/dune-take-item.sh
  . "$lib"
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

  local sender_account_id recipient_account_id item_id template_id idem operator req_by req_mode
  sender_account_id=$(jq_get '.sender_account_id')
  recipient_account_id=$(jq_get '.recipient_account_id')
  item_id=$(jq_get '.item_id')
  template_id=$(jq_get '.template_id')      # identity guard (optional but recommended)
  idem=$(jq_get '.idempotency_key')
  operator=$(jq_get '.operator'); [[ -n "$operator" ]] || operator="unknown"
  req_by=$(jq_get '.requested_by_discord_id')
  req_mode=$(jq_get '.mode'); [[ -n "$req_mode" ]] || req_mode="apply"

  validate_uuid "$idem"
  validate_posint "$sender_account_id" "sender_account_id"
  validate_posint "$recipient_account_id" "recipient_account_id"
  validate_posint "$item_id" "item_id"
  [[ "$sender_account_id" != "$recipient_account_id" ]] || fail_json "cannot transfer to yourself" 2
  case "$req_mode" in apply|dry-run) ;; *) fail_json "invalid mode: $req_mode" 2 ;; esac
  # template_id, when present, must be a game template token. Empty = skip the guard.
  # Charset and bounds match the relay's own guard on expected_template (relay/app.py),
  # which allows a dash: the narrower alnum+underscore rule here used to reject a payload
  # the relay had already accepted.
  if [[ -n "$template_id" && ! "$template_id" =~ ^[A-Za-z0-9_-]{2,64}$ ]]; then
    fail_json "template_id must be 2-64 chars of letters, digits, _ or -: $template_id" 2
  fi

  local dry=0
  [[ "$DRY_RUN" == "1" || "$req_mode" == "dry-run" ]] && dry=1

  # --- dark gate --------------------------------------------------------------
  if [[ "$LASTSIETCH_ITEM_TRANSFER_ENABLED" != "1" ]]; then
    printf '{"success":true,"status":"deferred","message":%s}\n' \
      "$(json_str "CHOAM bank item transfer is disabled (LASTSIETCH_ITEM_TRANSFER_ENABLED=${LASTSIETCH_ITEM_TRANSFER_ENABLED}); transfer not applied")"
    exit 0
  fi

  local DETAIL_JSON
  DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c '{item_id: (.item_id|tostring), template_id: (.template_id // null), kind: "bank_item_transfer"}')

  # Rate caps are embedded directly into the txn SQL — re-validate as integers.
  [[ "$XFER_MAX_PER_DAY" =~ ^[0-9]+$ ]] \
    || fail_json "XFER_MAX_PER_DAY must be an integer: $XFER_MAX_PER_DAY" 2
  [[ "$XFER_MAX_PER_PAIR_PER_DAY" =~ ^[0-9]+$ ]] \
    || fail_json "XFER_MAX_PER_PAIR_PER_DAY must be an integer: $XFER_MAX_PER_PAIR_PER_DAY" 2

  require_take_lib

  # The SHARED gated take (scripts/lib/dune-take-item.sh). Its only caller-specific
  # parameter is the destination: this passes the recipient's bank, the Karum writer
  # passes exchange inventory 610. That is a parameter, not a fork.
  local -a take_args=(
    item_id="$item_id"
    owner_account_id="$sender_account_id"
    dst="bank:${recipient_account_id}"
    correlation_id="$idem"
  )
  [[ -z "$template_id" ]] || take_args+=(expected_template="$template_id")
  local TAKE_SQL
  TAKE_SQL=$(karum_take_item "${take_args[@]}") \
    || fail_json "could not build the gated take for item $item_id" 2 "write_failed"

  # --- assemble the transaction ----------------------------------------------
  # Idempotency gate -> replay short-circuit -> anti-RMT rate caps -> THE SHARED GATED
  # TAKE (offline gate under a row lock, fresh tombstone-safe bank resolution, item lock
  # pinned to the sender's bank, identity guard, single-row re-home) -> recipient capacity
  # + audit-row backfill. ONE transaction: the gate and this ledger commit together or
  # not at all, so there is no window where the item has moved and nothing records it.
  local TXN
  TXN=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- Idempotency gate + durable audit row, same txn as the mutation.
CREATE TEMP TABLE _xfer_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_item_transfers
    (idempotency_key, sender_account_id, recipient_account_id, item_id, template_id,
     detail, operator, requested_by_discord_id, status, applied_at)
  VALUES
    (:'idem'::uuid, :sender_account_id, :recipient_account_id, :item_id,
     NULLIF(:'template_id',''), :'detail'::jsonb, :'operator', :'req_by', 'applied', now())
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_item_transfers WHERE idempotency_key = :'idem'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS op_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new;

SET LOCAL ls.sender_account_id    = :'sender_account_id';
SET LOCAL ls.recipient_account_id = :'recipient_account_id';

-- REPLAY SHORT-CIRCUIT. Everything downstream, the shared take included, checks
-- ls.take_skip and no-ops, so an exact retry re-reads its own audit row and mutates
-- nothing. Without this the take would run a second time against a sender bank that no
-- longer holds the row and abort on its exactly-one-row assertion, turning a harmless
-- retry into a hard failure.
DO \$gate\$
BEGIN
  IF NOT (SELECT is_new FROM _xfer_gate) THEN
    PERFORM set_config('ls.take_skip', '1', true);
  END IF;
END
\$gate\$;

-- Anti-RMT rate limits: count PRIOR applied transfers (excluding this txn's own
-- already-inserted audit row) in the last 24h. Checked BEFORE the take so a rate-limited
-- request never touches dune.items at all.
DO \$caps\$
DECLARE
  v_cnt_day  bigint;
  v_cnt_pair bigint;
BEGIN
  IF COALESCE(current_setting('ls.take_skip', true), '') = '1' THEN RETURN; END IF;

  SELECT count(*) INTO v_cnt_day FROM dune.ls_item_transfers
   WHERE sender_account_id = current_setting('ls.sender_account_id')::bigint
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _xfer_gate)
     AND created_at >= now() - interval '1 day';
  IF v_cnt_day >= ${XFER_MAX_PER_DAY} THEN
    RAISE EXCEPTION 'rate_limited (sender daily cap: % in 24h, max %)', v_cnt_day, ${XFER_MAX_PER_DAY};
  END IF;

  SELECT count(*) INTO v_cnt_pair FROM dune.ls_item_transfers
   WHERE sender_account_id = current_setting('ls.sender_account_id')::bigint
     AND recipient_account_id = current_setting('ls.recipient_account_id')::bigint
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _xfer_gate)
     AND created_at >= now() - interval '1 day';
  IF v_cnt_pair >= ${XFER_MAX_PER_PAIR_PER_DAY} THEN
    RAISE EXCEPTION 'rate_limited (sender->recipient daily cap: % in 24h, max %)', v_cnt_pair, ${XFER_MAX_PER_PAIR_PER_DAY};
  END IF;
END
\$caps\$;

${TAKE_SQL}

-- Recipient capacity + audit-row backfill, both driven off the facts the shared take
-- published into _take_result. Nothing here re-derives an inventory id.
DO \$post\$
DECLARE
  r      record;
  v_cap  bigint;
  v_used bigint;
BEGIN
  IF COALESCE(current_setting('ls.take_skip', true), '') = '1' THEN RETURN; END IF;

  SELECT * INTO r FROM _take_result;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'take_failed (the shared take published no result row)';
  END IF;

  -- Capacity is checked AFTER the re-home rather than before, which is equivalent and
  -- needs no second bank resolution: the row is already in the destination so the count
  -- includes it, and a RAISE here rolls the move back with everything else.
  -- max_item_count semantics match dune-storage-write.py: 0 = no slots, -1 = unlimited.
  SELECT inv.max_item_count,
         (SELECT count(*) FROM dune.items x WHERE x.inventory_id = inv.id)
    INTO v_cap, v_used
    FROM dune.inventories inv WHERE inv.id = r.dst_inv;
  IF v_cap = 0 THEN
    RAISE EXCEPTION 'bank_full (recipient bank % has no item slots)', r.dst_inv;
  ELSIF v_cap > 0 AND v_used > v_cap THEN
    RAISE EXCEPTION 'bank_full (recipient bank % would hold % of cap %)', r.dst_inv, v_used, v_cap;
  END IF;

  UPDATE dune.ls_item_transfers
     SET sender_pawn_id        = r.src_pawn_id,
         recipient_pawn_id     = r.dst_pawn_id,
         sender_bank_inv_id    = r.src_inv,
         recipient_bank_inv_id = r.dst_inv,
         template_id           = COALESCE(template_id, r.template_id),
         stack_size            = r.stack_size,
         quality_level         = r.quality_level
   WHERE id = (SELECT op_id FROM _xfer_gate);
END
\$post\$;

SELECT 'RESULT|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _xfer_gate;

COMMIT;
EOF
)

  local -a vargs=(
    -v "idem=${idem}"
    -v "sender_account_id=${sender_account_id}"
    -v "recipient_account_id=${recipient_account_id}"
    -v "item_id=${item_id}"
    -v "template_id=${template_id}"
    -v "operator=${operator}"
    -v "req_by=${req_by}"
    -v "detail=${DETAIL_JSON}"
  )

  # Dry-run returns the assembled SQL and touches nothing. It deliberately exits BEFORE
  # resolve_db_pod so it also works off-box (the test suite asserts on this exact SQL, and
  # a reviewer can read the real transaction without shelling into the game host).
  if [[ "$dry" == "1" ]]; then
    printf '{"success":true,"status":"dry-run",'
    printf '"message":"dry-run: SQL built, NOT executed","sql":%s}\n' "$(json_str "$TXN")"
    exit 0
  fi

  resolve_db_pod

  local result
  if ! result=$(printf '%s\n' "$TXN" | run_psql -tA "${vargs[@]}" 2>&1); then
    local token
    token=$(error_token_of "$result")
    fail_json "item transfer failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)" \
              6 "$token"
  fi

  local row op_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  op_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$op_id" ]] || op_id="null"

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","audit_id":%s,"item_id":%s,"message":"already applied, idempotent replay, no change made"}\n' \
      "$op_id" "$item_id"
    exit 0
  fi
  printf '{"success":true,"status":"applied","audit_id":%s,"item_id":%s,"message":"item transferred to recipient bank"}\n' \
    "$op_id" "$item_id"
  exit 0
}

# =============================================================================
# Entry point
# =============================================================================
DRY_RUN=0

main() {
  [[ $# -gt 0 ]] || fail_json "usage: dune-item-transfer-op.sh --op-b64 <b64> [--dry-run] | --op-b64-stdin [--dry-run]" 2
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
