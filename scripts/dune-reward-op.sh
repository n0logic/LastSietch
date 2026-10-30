#!/usr/bin/env bash
# Dune login-REWARDS WRITE tool, game-host resident (sibling of dune-gift-op.sh).
#
# Deployed to the game host:/root/dune-reward-op.sh (mode 0750, owner root).
# Invoked ONLY by the forced-command dispatcher (a 'reward-op' action reading
# --op-b64-stdin), reached over the relay SSH key. Runs as root on the game host
# and does its own `sudo kubectl exec` into the Dune DB pod, no nested ssh.
#
# Three reward kinds (login-rewards V2):
#   Per-account cap is CALENDAR-boundary: daily_solari = per UTC calendar day,
#   weekly_item = per ISO week, monthly_augment = per UTC calendar month
#   (date_trunc, not a rolling window), so a legit next-period claim is never
#   false-rejected across a midnight/week/month edge. The UNIQUE(idempotency_key)
#   guard is the real double-grant backstop; the backend keys idempotency =
#   uuid5(account, kind, period).
#
#   daily_solari , CREDIT ONLY. One atomic txn: idempotency+audit gate row in
#                   dune.ls_reward_claims, then a single positive
#                   dune.adjust_player_virtual_currency_balance (currency
#                   'Solaris'::dune.virtualwallettype = 0 = BANK Solari). A positive delta
#                   never hits the D5 negative-clamp branch (see dune-gift-op.sh),
#                   so there is NO balance pre-check and NO sender/debit side.
#   weekly_item  , mint a fixed item into the player's CHOAM bank (inv_type 30)
#                   via dune-grant.sh G29 bank_items_batch (ONLINE-SAFE, proven
#                   2026-05-23). The reward row is RESERVED status='pending' in a
#                   first txn (idempotency + weekly cap), the mint is delegated to
#                   dune-grant.sh (its own idempotency + audit), then the row is
#                   flipped to 'applied'. We do NOT open-code the items INSERT.
#   monthly_augment, mint ONE pre-augmented weapon into the CHOAM bank (inv_type
#                   30), stats jsonb (FAugmentedItemStats + durability) built at
#                   INSERT time. Does NOT delegate to dune-grant.sh G29 (G29's
#                   items INSERT hardcodes stats='{}'::jsonb, no per-item stats
#                   blob support), so this is open-coded here, in ONE atomic txn
#                   (idempotency+audit+bank-resolve+capacity+insert), mirroring
#                   daily_solari's single-txn shape rather than weekly_item's
#                   two-phase delegation. The bank-resolve CTE filters
#                   character_state <> 'Deleted' and takes exactly one row
#                   (measured live 2026-08-01: the unfiltered G29 join can resolve
#                   TWO banks for one account -- a live one and a Deleted
#                   character's leftover -- and mint into both). augment/roll
#                   data is supplied by the caller (rewards.py MONTHLY_POOL);
#                   this script re-validates it but does not invent roll counts.
#
# LASTSIETCH_REWARD_ENABLED (env, default "0" = DARK): while off the op is code-complete
# but refuses, returns {"status":"deferred",...} WITHOUT opening a DB txn.
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
REWARD_AUGMENT_MAX_PER_MONTH="${REWARD_AUGMENT_MAX_PER_MONTH:-1}"   # applied monthly_augment claims / UTC month
REWARD_AUGMENT_QUALITY_DEFAULT="${REWARD_AUGMENT_QUALITY_DEFAULT:-5}"  # fallback grade if the caller omits one
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
  sudo kubectl exec -i -n "$DB_NS" "$DB_POD" -- \
    sh -c '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"' _ -h localhost -p "$DB_PORT" \
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
# daily_solari, single atomic txn: gate row + positive credit.
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
  PERFORM dune.adjust_player_virtual_currency_balance(v_ctrl, 'Solaris'::dune.virtualwallettype, v_amount);

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
# weekly_item, reserve gate row (pending), delegate the mint to dune-grant.sh
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

# =============================================================================
# monthly_augment, mint ONE pre-augmented weapon into the CHOAM bank, stats
# jsonb built at INSERT time. Self-contained (does NOT delegate to dune-grant.sh
# G29 -- G29's items INSERT hardcodes stats='{}'::jsonb, no per-item stats blob
# support), single atomic txn (idempotency + monthly cap + bank resolve +
# capacity + insert), mirroring do_daily_solari's one-txn shape.
# =============================================================================
do_monthly_augment() {
  local account_id="$1" idem="$2" template_id="$3" quality="$4" augments_json="$5" operator="$6" dry="$7" period_key="$8"

  [[ -n "$period_key" ]] || fail_json "monthly_augment requires period_key" 2
  validate_template_id "$template_id"
  [[ -n "$quality" ]] || quality="$REWARD_AUGMENT_QUALITY_DEFAULT"
  [[ "$quality" =~ ^[0-9]+$ ]] || fail_json "quality_level must be an integer 0..5: $quality" 2
  (( quality <= 5 )) || fail_json "quality_level out of range (0..5): $quality" 2
  [[ "$REWARD_AUGMENT_MAX_PER_MONTH" =~ ^[0-9]+$ ]] \
    || fail_json "REWARD_AUGMENT_MAX_PER_MONTH must be an integer: $REWARD_AUGMENT_MAX_PER_MONTH" 2

  # Re-validate the augment list server-side (the caller is rewards.py's curated
  # MONTHLY_POOL, but every field crossing this boundary gets re-checked here
  # regardless): 1..3 entries (the game's own weapon-augment slot cap), each a
  # T<digit>_Augment_* id, grade 1..5, and a roll count in a sane range. We do
  # NOT derive/guess the roll count here -- it is supplied by the caller and
  # only re-bounded, because a wrong count renders an empty augment slot in game.
  if ! printf '%s' "$augments_json" | jq -e '
        type == "array" and length >= 1 and length <= 3 and
        all(
          (.name // "" | test("^T[0-9]+_Augment_[A-Za-z0-9_]+$"))
          and ((.grade // 0) | type == "number") and ((.grade // 0) >= 1) and ((.grade // 0) <= 5)
          and ((.rolls // 0) | type == "number") and ((.rolls // 0) >= 1) and ((.rolls // 0) <= 10)
        )
      ' >/dev/null 2>&1; then
    fail_json "augments must be a 1..3 element array of {name: T#_Augment_*, grade 1..5, rolls 1..10}" 2
  fi

  local DETAIL_JSON
  DETAIL_JSON=$(jq -nc --arg tpl "$template_id" --argjson q "$quality" --argjson augs "$augments_json" \
    --arg pk "$period_key" \
    '{template_id:$tpl, quality:$q, stack_size:1, reward_kind:"monthly_augment",
      delivery:"bank", period_key:$pk, augments:$augs}')

  # Random roll (owner-locked 2026-08-01: NOT perfect -- a guaranteed grade-5
  # 3-augment weapon every month would remove augment rolls as something
  # players chase). The roll COUNT is still the augment's real roll shape
  # (caller-supplied, never guessed here -- a wrong COUNT renders an empty
  # augment slot in game); only the VALUES are randomised, each a fresh
  # uniform draw in [0.0, 1.0], rounded to 6 decimal places to match what the
  # game itself stores. jq has no RNG builtin, so this one step shells to
  # python3 (already a hard dependency of this script -- see json_str() above).
  # Durability defaults to a flat 100/100/100 and gets overwritten in-SQL with
  # a real sample from an existing row of the same template when one exists
  # (see the `dur` CTE below).
  local STATS_JSON
  STATS_JSON=$(printf '%s' "$augments_json" | python3 -c '
import json, random, sys

augments = json.load(sys.stdin)
roll_data = [
    {"StatRolls": [round(random.uniform(0.0, 1.0), 6) for _ in range(int(a["rolls"]))],
     "AppliedEffectIndices": []}
    for a in augments
]
stats = {
    "FCustomizationStats": [[], {}],
    "FWeaponItemStats": [[], {}],
    "FItemStackAndDurabilityStats": [[], {"MaxDurability": 100.0, "CurrentDurability": 100.0,
                                          "DecayedMaxDurability": 100.0}],
    "FAugmentedItemStats": [[], {
        "AppliedAugments": [{"Name": a["name"]} for a in augments],
        "AppliedAugmentQualities": [a["grade"] for a in augments],
        "AppliedAugmentRollData": roll_data,
    }],
}
print(json.dumps(stats, separators=(",", ":")))
')
  [[ -n "$STATS_JSON" ]] || fail_json "failed to build the augment stats blob" 6

  local TXN
  TXN=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- Idempotency gate + durable audit row, same txn as the mint (mirrors
-- do_daily_solari: single-phase, no separate reserve/flip like weekly_item,
-- since nothing here is delegated out to a second script/txn).
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
-- These three cross the psql/PL-pgSQL boundary via SET LOCAL, NOT :'var'
-- interpolation: psql does not substitute :vars inside a dollar-quoted DO block,
-- so referencing :'stats' / :'template_id' / :quality_level below would reach the
-- server as a literal ':' and abort with a syntax error (the reason no
-- monthly_augment ever granted before 2026-08-10). Same pattern as ls.account_id
-- / ls.amount in do_daily_solari, which is why that path worked and this did not.
SET LOCAL ls.template_id   = :'template_id';
SET LOCAL ls.quality_level = :'quality_level';
SET LOCAL ls.stats         = :'stats';
SET LOCAL ls.period_key    = :'period_key';

DO \$\$
DECLARE
  v_cnt     bigint;
  v_is_new  boolean;
  v_inv     bigint;
  v_cap     int;
  v_used    int;
  v_max_pos int;
  v_durmax  double precision;
  v_stats   jsonb := current_setting('ls.stats')::jsonb;
  v_item_id bigint;
BEGIN
  SELECT is_new FROM _reward_gate INTO v_is_new;
  IF NOT v_is_new THEN
    RETURN;  -- idempotent replay: no mutation
  END IF;

  -- Per-account PER-PERIOD cap: one applied monthly_augment for THIS reward
  -- period (the 28-day window, identified by detail->>'period_key'), regardless
  -- of WHEN it was granted. The old calendar-month boundary was wrong: the reward
  -- period is a rolling 28-day window, so a retro grant for a prior period would
  -- consume a later period's calendar-month slot, and a legit next-period claim
  -- landing in the same calendar month would be falsely blocked. The deterministic
  -- idempotency_key already collapses exact same-period replays; this cap is the
  -- defense-in-depth that stops a second same-period grant arriving via a DIFFERENT
  -- idem, now correctly scoped to the period rather than the calendar month.
  SELECT count(*) INTO v_cnt FROM dune.ls_reward_claims
   WHERE account_id = current_setting('ls.account_id')::bigint
     AND reward_kind = 'monthly_augment'
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _reward_gate)
     AND detail->>'period_key' = current_setting('ls.period_key');
  IF v_cnt >= ${REWARD_AUGMENT_MAX_PER_MONTH} THEN
    RAISE EXCEPTION 'reward cap: account % already has % applied monthly_augment for period % (max %)',
      current_setting('ls.account_id'), v_cnt, current_setting('ls.period_key'), ${REWARD_AUGMENT_MAX_PER_MONTH};
  END IF;

  -- Resolve the CHOAM bank FRESH, tombstone-safe, exactly one row: a Deleted
  -- character's leftover bank inventory must never be a mint target. Measured
  -- live 2026-08-01: the G29 bank-resolve CTE has no character_state filter and
  -- no LIMIT, so for an account carrying a Deleted-character tombstone it can
  -- join to TWO type-30 inventories and mint into both. That bug is NOT
  -- reproduced here.
  SELECT inv.id, inv.max_item_count,
         (SELECT COUNT(*) FROM dune.items WHERE inventory_id = inv.id),
         COALESCE((SELECT MAX(position_index) FROM dune.items WHERE inventory_id = inv.id), -1)
    INTO v_inv, v_cap, v_used, v_max_pos
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 30
   WHERE eps.account_id = current_setting('ls.account_id')::bigint
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1;

  IF v_inv IS NULL THEN
    RAISE EXCEPTION 'BANK_BATCH_FAIL: no bank inventory for account=% (bank is pawn-keyed, never opened in-game?)',
      current_setting('ls.account_id');
  END IF;
  IF v_used + 1 > v_cap THEN
    RAISE EXCEPTION 'BANK_BATCH_FAIL: bank capacity exceeded (used % + 1 > cap %)', v_used, v_cap;
  END IF;

  -- Best-effort real durability sample from an existing row of the same
  -- template (any account); falls back to the stats blob's built-in default
  -- (100/100/100) when the template has never been seen with durability stats.
  --
  -- ** NOTE: no backticks anywhere in this comment. ** This SQL rides an UNQUOTED
  -- heredoc, so a backtick runs as command substitution: it both spams stderr and
  -- silently deletes the quoted text from the emitted SQL.
  --
  -- Last Sietch 2026-08-13 (BUG-019 follow-up). The old version of this sampler was wrong
  -- in THREE ways, all measured against live data:
  --
  --   1. Its filter was  stats ? 'FItemStackAndDurabilityStats' , which matches a
  --      row whose durability object is EMPTY. That is the single most common
  --      shape on real weapon templates (200+ rows each vs a handful of populated
  --      ones), and ORDER BY id DESC LIMIT 1 made picking one the likely case --
  --      blanking durability entirely, not merely partially.
  --   2. jsonb_set REPLACES the object rather than merging, so any sample with
  --      fewer keys than the default silently drops the rest. That is what mangled
  --      a player's restoration (sampled CurrentDurability only; caught on
  --      read-back and corrected by hand).
  --   3. It sampled the NEWEST row, which is usually a weapon somebody has been
  --      using. Copying its CurrentDurability mints the reward pre-damaged --
  --      measured live, Kindjal_0 would ship at 25.9/55.0 and Kindjal_2 at
  --      ~51/100, i.e. a "reward" at half durability.
  --
  -- Fix: derive the template's PRISTINE ceiling as the highest DecayedMaxDurability
  -- ever recorded for it (that recovers 100 where the newest row says 55), then
  -- build all three keys explicitly at that value. Constructing the object instead
  -- of copying one means it can never come out empty or partial, so this no longer
  -- depends on any single row being well-formed. MaxDurability is deliberately
  -- not required when scanning: live data says the game's normal shape is
  -- CurrentDurability+DecayedMaxDurability (35k rows) with MaxDurability usually
  -- absent, so requiring it would reject nearly every genuine weapon.
  -- No durability data at all for the template -> keep the built-in 100/100/100.
  SELECT max(((stats #> '{FItemStackAndDurabilityStats,1}')->>'DecayedMaxDurability')::float)
    INTO v_durmax
    FROM dune.items
   WHERE template_id = current_setting('ls.template_id')
     AND jsonb_typeof((stats #> '{FItemStackAndDurabilityStats,1}')->'DecayedMaxDurability') = 'number';
  IF v_durmax IS NOT NULL AND v_durmax > 0 THEN
    v_stats := jsonb_set(v_stats, '{FItemStackAndDurabilityStats,1}',
                 jsonb_build_object('MaxDurability',        v_durmax,
                                    'CurrentDurability',    v_durmax,
                                    'DecayedMaxDurability', v_durmax));
  END IF;

  INSERT INTO dune.items
    (inventory_id, stack_size, position_index, template_id, stats,
     quality_level, acquisition_time, is_new)
  VALUES
    (v_inv, 1, v_max_pos + 1, current_setting('ls.template_id'), v_stats,
     current_setting('ls.quality_level')::int, 0, true)
  RETURNING id INTO v_item_id;

  UPDATE dune.ls_reward_claims
     SET detail = detail || jsonb_build_object('bank_inv', v_inv, 'item_id', v_item_id)
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
    -v "operator=${operator}"
    -v "reward_kind=monthly_augment"
    -v "detail=${DETAIL_JSON}"
    -v "template_id=${template_id}"
    -v "quality_level=${quality}"
    -v "stats=${STATS_JSON}"
    -v "period_key=${period_key}"
  )

  if [[ "$dry" == "1" ]]; then
    # `stats` is embedded raw (already valid JSON, not a string) so a caller can
    # inspect the exact augment/roll payload that would be minted -- e.g. to
    # confirm roll VALUES are randomised while roll COUNTS stay fixed to what
    # was requested, without ever touching the DB.
    printf '{"success":true,"status":"dry-run","reward_kind":"monthly_augment",'
    printf '"message":"dry-run: SQL built, NOT executed","stats":%s,"sql":%s}\n' \
      "$STATS_JSON" "$(json_str "$TXN")"
    exit 0
  fi

  resolve_db_pod

  local result
  if ! result=$(printf '%s\n' "$TXN" | run_psql -tA "${vargs[@]}" 2>&1); then
    if printf '%s' "$result" | grep -q 'BANK_BATCH_FAIL: bank capacity exceeded'; then
      printf '{"success":false,"status":"failed","error":"bank_full","reward_kind":"monthly_augment","message":%s}\n' \
        "$(json_str "Your CHOAM bank is full. Free up some space, then claim again.")"
      exit 6
    fi
    if printf '%s' "$result" | grep -q 'BANK_BATCH_FAIL: no bank inventory'; then
      printf '{"success":false,"status":"failed","error":"bank_unopened","reward_kind":"monthly_augment","message":%s}\n' \
        "$(json_str "Open your CHOAM bank in-game once so it exists, then claim.")"
      exit 6
    fi
    fail_json "monthly_augment transaction failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)" 6
  fi

  local row op_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  op_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$op_id" ]] || op_id="null"

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","reward_kind":"monthly_augment","audit_id":%s,"template_id":%s,"message":"already applied, idempotent replay, no change made"}\n' \
      "$op_id" "$(json_str "$template_id")"
    exit 0
  fi
  printf '{"success":true,"status":"applied","reward_kind":"monthly_augment","audit_id":%s,"template_id":%s,"message":"monthly augmented weapon minted to CHOAM bank"}\n' \
    "$op_id" "$(json_str "$template_id")"
  exit 0
}

# =============================================================================
# sn_shop_bank / sn_shop_solari / sn_shop_spec / sn_shop_charxp: Seven Nights Token Stall deliveries.
#
# The portal debits LS Tokens in admin.db first (leg 1), then sends ONLY
# {sku, account_id, controller_id, profile_id, idempotency_key = delivery_key}.
# Everything that decides WHAT is minted lives in SN_SHOP_SKUS below, so a buggy
# or hostile caller can at most ask for a known SKU. One atomic txn per call:
# idempotency + audit row, per-profile backstops, controller ownership, emote
# and unlock dedupe, bank capacity, insert, then any Solari the SKU carries.
# Any RAISE rolls the whole txn back, so a bank SKU with Solari is all-or-nothing.
#
# sn_shop_spec adds spec_xp to one specialization track through the native proc
# dune.set_specialization_xp_and_level, which SETS: the txn reads the current xp,
# adds, clamps at 44182 and passes the curve-matching level (a level that does not
# match the xp reverts on relog). A track already at the cap refuses already_owned.
# The track loads into RAM on login, so unless the character is Offline and past
# its reconnect grace (Online, LoggingOut, NULL, anything else) the call answers
# "deferred" (whole rollback, no claim row) and the portal re-drives the same key
# until the character is offline.
#
# sn_shop_charxp adds char_xp to the character level through the proven G11 v2+v3
# write of scripts/dune-grant.sh build_char_xp_grant: TotalXPEarned on the pawn's
# DuneCharacter FLevelComponent[1], capped at 344440, the levels gained credited to
# TotalSkillPoints AND UnspentSkillPoints, and the intel delta to
# m_TechKnowledgePoints. Spent points are never recomputed (the icehunter wipe). A character at 344440 refuses
# already_owned; missing curve functions or no level row refuse rejected. Same
# offline deferral as sn_shop_spec: the level is RAM-resident in game.
#
# Failure codes (the portal refunds ONLY these, everything else re-drives the
# same key): rejected, not_owner, cap, already_owned, capacity, bank_unopened.
#
# Own gate, independent of LASTSIETCH_REWARD_ENABLED: SN_GATE_FILE holds "*" or one
# account id per line (staged launch: owner, then testers, then *). Absent =
# every call defers with no txn. rm is the kill switch.
#
# No script-execution verb, no ServerExec, no RMQ verb: this path has no route to the MTX
# event verbs, and a payload that even names one is refused.
# =============================================================================
SN_GATE_FILE="${SN_GATE_FILE:-/etc/lastsietch/sietch-shop-enabled}"
# Per-profile ceiling on ALL Stall Solari (purses plus Solari carried by bank
# SKUs). It is the exact legitimate maximum, sum(solari * limit) over the
# allowlist: 2 purses x 50,000 + Feast 250,000 + Hollow Moon 50,000. The per-SKU
# limits already stop a repeat; this catches a profile that somehow got more, and
# test_sietch_nights.py pins it to the allowlist so it cannot drift.
SN_SOLARI_MAX_PER_PROFILE="${SN_SOLARI_MAX_PER_PROFILE:-400000}"
SN_SPEC_XP_CAP=44182
SN_SPEC_TRACKS=" Combat Crafting Gathering Exploration Sabotage "
SN_CHAR_XP_CAP=344440

# Generated from admin-backend/data/sietch_nights_catalog.json (bank, wallet, spec
# and charxp lanes). test_sietch_nights.py pins parity; edit both together.
read -r -d '' SN_SHOP_SKUS <<'SKUS' || true
{
 "sn-aramsham": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "SardaukarCaptain_Sword"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "SardaukarCaptain_Sword"
  ]
 },
 "sn-awakened-stillsuit": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "AgentX_Stillsuit"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "AgentX_Stillsuit_Helmet",
   "AgentX_Stillsuit_Boots",
   "AgentX_Stillsuit_Gloves",
   "AgentX_Stillsuit_Torso"
  ]
 },
 "sn-benegesserit": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "AdvancedTrainer_BeneGeserit_SetVariant"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "Trainer_BeneGeserit_Helmet_MeshVariant",
   "Trainer_BeneGeserit_Top_MeshVariant",
   "Trainer_BeneGeserit_Gloves_MeshVariant",
   "Trainer_BeneGeserit_Bottom_MeshVariant",
   "Trainer_BeneGeserit_Boots_MeshVariant"
  ]
 },
 "sn-charxp-wayfarer": {
  "char_xp": 1200,
  "kind": "sn_shop_charxp",
  "limit": 1
 },
 "sn-choam-lights": {
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "Choam_AdvLighting_Patent"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1
 },
 "sn-contraband-swatch": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "B1C3_SmugTech_Swatch"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "SmugTechGlobal"
  ]
 },
 "sn-gear-forge-set": {
  "items": [
   {
    "grade": 5,
    "qty": 1,
    "template_id": "Combat_Hark_MedUnique02_Helmet"
   },
   {
    "grade": 5,
    "qty": 1,
    "template_id": "Combat_Hark_MedUnique02_Top"
   },
   {
    "grade": 5,
    "qty": 1,
    "template_id": "Combat_Hark_MedUnique02_Gloves"
   },
   {
    "grade": 5,
    "qty": 1,
    "template_id": "Combat_Hark_MedUnique02_Bottom"
   },
   {
    "grade": 5,
    "qty": 1,
    "template_id": "Combat_Hark_MedUnique02_Boots"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1
 },
 "sn-gear-lightning": {
  "items": [
   {
    "grade": 5,
    "qty": 1,
    "template_id": "UniqueAr6_Electric"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1
 },
 "sn-gear-perforator": {
  "items": [
   {
    "grade": 5,
    "qty": 1,
    "template_id": "UniqueScattergun5"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1
 },
 "sn-gear-radsuit": {
  "items": [
   {
    "grade": 5,
    "qty": 1,
    "template_id": "Radiation_Suit_T6_Unique_Armored"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1
 },
 "sn-gear-tabr-boots": {
  "items": [
   {
    "grade": 5,
    "qty": 1,
    "template_id": "Combat_Light_Unique_WormThreat_Boots_06"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1
 },
 "sn-healkit": {
  "items": [
   {
    "grade": 0,
    "qty": 5,
    "template_id": "HealthPack_Channeled_4"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 3
 },
 "sn-hidden-hand": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "B1C3_Shaman_Raider"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "B1C3_Shaman_Raider_Top",
   "B1C3_Shaman_Raider_Boots",
   "B1C3_Shaman_Raider_Bottom",
   "B1C3_Shaman_Raider_Gloves",
   "B1C3_Shaman_Raider_Helmet"
  ]
 },
 "sn-iodine": {
  "items": [
   {
    "grade": 0,
    "qty": 5,
    "template_id": "AntiRadiationPill"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 3
 },
 "sn-moritani-heavy": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "Moritani_HeavyArmor_Swatch"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "HArmCharDyepackMoritani"
  ]
 },
 "sn-moritani-placeables": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "Moritani_Placeables_Swatch"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "PlaceableDyePackMoritani"
  ]
 },
 "sn-moritani-stillsuit": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "Moritani_Stillsuit_Swatch"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "StillSCharDyepackMoritani"
  ]
 },
 "sn-mutelli-placeables": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "Mutelli_Placeables_Swatch"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "PlaceableDyePackMutelli"
  ]
 },
 "sn-secret-baron-feast": {
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "Bloodsack_Unique_Durable_06"
   },
   {
    "grade": 0,
    "qty": 1,
    "template_id": "Bloodsack_Unique_Durable_03"
   },
   {
    "fallback": {
     "grade": 0,
     "qty": 1,
     "skip_if_unlocked": [
      "SardaukarCaptain_Sword"
     ],
     "template_id": "SardaukarCaptain_Sword"
    },
    "grade": 0,
    "qty": 1,
    "skip_if_unlocked": [
     "GluttonsMeatHook_Variant"
    ],
    "template_id": "GluttonsMeatHook_Variant"
   },
   {
    "grade": 0,
    "qty": 250,
    "template_id": "MelangeSpice"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "solari": 250000
 },
 "sn-secret-hollow-moon": {
  "items": [
   {
    "grade": 0,
    "qty": 50,
    "template_id": "MelangeSpice"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "solari": 50000
 },
 "sn-slaver-heavy": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "SlaverHeavy_T3_SetVariant"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "SlaverHeavy_T3_Helmet_MeshVariant",
   "SlaverHeavy_T3_Top_MeshVariant",
   "SlaverHeavy_T3_Gloves_MeshVariant",
   "SlaverHeavy_T3_Bottom_MeshVariant",
   "SlaverHeavy_T3_Boots_MeshVariant"
  ]
 },
 "sn-slaver-light": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "SlaverLight_T3_SetVariant"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "SlaverLight_T3_Helmet_MeshVariant",
   "SlaverLight_T3_Top_MeshVariant",
   "SlaverLight_T3_Gloves_MeshVariant",
   "SlaverLight_T3_Bottom_MeshVariant",
   "SlaverLight_T3_Boots_MeshVariant"
  ]
 },
 "sn-solari-purse": {
  "kind": "sn_shop_solari",
  "limit": 2,
  "solari": 50000
 },
 "sn-spec-combat": {
  "kind": "sn_shop_spec",
  "limit": 1,
  "spec_xp": 1500,
  "track": "Combat"
 },
 "sn-spec-crafting": {
  "kind": "sn_shop_spec",
  "limit": 1,
  "spec_xp": 1500,
  "track": "Crafting"
 },
 "sn-spec-exploration": {
  "kind": "sn_shop_spec",
  "limit": 1,
  "spec_xp": 1500,
  "track": "Exploration"
 },
 "sn-spec-gathering": {
  "kind": "sn_shop_spec",
  "limit": 1,
  "spec_xp": 1500,
  "track": "Gathering"
 },
 "sn-spec-sabotage": {
  "kind": "sn_shop_spec",
  "limit": 1,
  "spec_xp": 1500,
  "track": "Sabotage"
 },
 "sn-tyrant-worm-face": {
  "dedupe": "unlock",
  "items": [
   {
    "grade": 0,
    "qty": 1,
    "template_id": "StoryContent_Sinnu_Helmet_Variant"
   }
  ],
  "kind": "sn_shop_bank",
  "limit": 1,
  "unlock_ids": [
   "SinnuHelmet_Variant"
  ]
 }
}
SKUS

sn_gate_open() {
  local aid="$1"
  [[ -f "$SN_GATE_FILE" ]] || return 1
  grep -qxF '*' "$SN_GATE_FILE" && return 0
  grep -qxF "$aid" "$SN_GATE_FILE"
}

sn_fail() {
  local code="$1" kind="$2" msg="$3"
  printf '{"success":false,"status":"failed","error":%s,"reward_kind":%s,"message":%s}\n' \
    "$(json_str "$code")" "$(json_str "$kind")" "$(json_str "$msg")"
  exit "${4:-6}"
}

sn_template_denied() {
  local t="${1,,}"
  [[ "$t" == mtx_* || "$t" == dlc_* || "$t" == halloween* || "$t" == *twitch* ]]
}

do_sn_shop() {
  local kind="$1" account_id="$2" idem="$3" operator="$4" dry="$5"
  local sku ctrl profile_id discord_id
  sku=$(jq_get '.sku')
  ctrl=$(jq_get '.controller_id')
  profile_id=$(jq_get '.profile_id')
  discord_id=$(jq_get '.discord_id')

  if printf '%s' "$OP_JSON" | grep -qiE 'cheat[s]cript|mtxstop|endactivemtx|endallmtx|endmtxevent|schedulemtx'; then
    sn_fail rejected "$kind" "payload names a banned verb" 2
  fi
  [[ "$sku" =~ ^sn-[a-z0-9-]{1,48}$ ]] || sn_fail rejected "$kind" "invalid sku: $sku" 2
  [[ "$ctrl" =~ ^[1-9][0-9]{0,18}$ ]] || sn_fail rejected "$kind" "controller_id must be a positive integer" 2
  [[ "$profile_id" =~ ^[0-9a-f]{32}$ ]] || sn_fail rejected "$kind" "profile_id must be 32 hex chars" 2
  [[ -z "$discord_id" || "$discord_id" =~ ^[1-9][0-9]{0,19}$ ]] || sn_fail rejected "$kind" "invalid discord_id" 2

  local entry
  entry=$(printf '%s' "$SN_SHOP_SKUS" | jq -c --arg s "$sku" '.[$s] // empty')
  [[ -n "$entry" ]] || sn_fail rejected "$kind" "sku is not on the writer allowlist: $sku" 2
  [[ "$(printf '%s' "$entry" | jq -r '.kind')" == "$kind" ]] \
    || sn_fail rejected "$kind" "sku $sku is not a $kind sku" 2

  local limit items solari dedupe n_items unlocks track spec_xp char_xp
  limit=$(printf '%s' "$entry" | jq -r '.limit')
  track=$(printf '%s' "$entry" | jq -r '.track // ""')
  spec_xp=$(printf '%s' "$entry" | jq -r '.spec_xp // 0')
  char_xp=$(printf '%s' "$entry" | jq -r '.char_xp // 0')
  unlocks=$(printf '%s' "$entry" | jq -c '.unlock_ids // []')
  items=$(printf '%s' "$entry" | jq -c '.items // []')
  solari=$(printf '%s' "$entry" | jq -r '.solari // 0')
  dedupe=$(printf '%s' "$entry" | jq -r '.dedupe // ""')
  n_items=$(printf '%s' "$items" | jq 'length')
  validate_posint "$limit" "allowlist limit"
  local tpl
  # Every template that could be minted, fallbacks included.
  for tpl in $(printf '%s' "$items" | jq -r '.. | objects | .template_id? // empty'); do
    validate_template_id "$tpl"
    sn_template_denied "$tpl" && sn_fail rejected "$kind" "template $tpl is on the deny list" 2
  done
  for tpl in $(printf '%s' "$unlocks" | jq -r '.[]'); do validate_template_id "$tpl"; done
  for tpl in $(printf '%s' "$items" | jq -r '.. | objects | .skip_if_unlocked? // empty | .[]'); do
    validate_template_id "$tpl"
  done
  [[ "$dedupe" != "unlock" || "$unlocks" != "[]" ]] || sn_fail rejected "$kind" "unlock dedupe needs unlock_ids" 2
  local qty
  for qty in $(printf '%s' "$items" | jq -r '.. | objects | .qty? // empty'); do
    [[ "$qty" =~ ^[1-9][0-9]{0,2}$ ]] && (( qty <= 500 )) || sn_fail rejected "$kind" "item qty out of range" 2
  done
  case "$kind" in
    sn_shop_bank)
      (( n_items >= 1 && n_items <= 5 )) || sn_fail rejected "$kind" "bank sku needs 1..5 items" 2
      if [[ "$solari" != "0" ]]; then
        validate_posint "$solari" "allowlist solari"
        (( solari <= SN_SOLARI_MAX_PER_PROFILE )) || sn_fail rejected "$kind" "solari exceeds the backstop" 2
      fi
      ;;
    sn_shop_solari)
      validate_posint "$solari" "allowlist solari"
      (( solari <= SN_SOLARI_MAX_PER_PROFILE )) || sn_fail rejected "$kind" "solari exceeds the backstop" 2
      ;;
    sn_shop_spec)
      (( n_items == 0 )) && [[ "$solari" == "0" ]] || sn_fail rejected "$kind" "spec sku carries xp only" 2
      [[ "$SN_SPEC_TRACKS" == *" $track "* ]] || sn_fail rejected "$kind" "invalid track: $track" 2
      validate_posint "$spec_xp" "allowlist spec_xp"
      (( spec_xp <= SN_SPEC_XP_CAP )) || sn_fail rejected "$kind" "spec_xp exceeds the track cap" 2
      ;;
    sn_shop_charxp)
      (( n_items == 0 )) && [[ "$solari" == "0" && "$spec_xp" == "0" && -z "$track" ]] \
        || sn_fail rejected "$kind" "charxp sku carries character xp only" 2
      validate_posint "$char_xp" "allowlist char_xp"
      (( char_xp <= SN_CHAR_XP_CAP )) || sn_fail rejected "$kind" "char_xp exceeds the level cap" 2
      ;;
  esac

  local DETAIL_JSON
  DETAIL_JSON=$(jq -nc --arg sku "$sku" --argjson ctrl "$ctrl" --arg pid "$profile_id" \
    --arg did "$discord_id" --argjson items "$items" --argjson sol "$solari" --arg k "$kind" \
    --arg track "$track" --argjson sxp "$spec_xp" --argjson cxp "$char_xp" \
    '{reward_kind:$k, sku:$sku, controller_id:$ctrl, profile_id:$pid, event:"sn2026"}
     + (if $did == "" then {} else {discord_id:$did} end)
     + (if $k == "sn_shop_bank" then {items:$items, delivery:"bank"}
        elif $k == "sn_shop_spec" then {track:$track, spec_xp:$sxp}
        elif $k == "sn_shop_charxp" then {char_xp:$cxp}
        else {solari:$sol} end)
     + (if $k == "sn_shop_bank" and $sol > 0 then {solari:$sol} else {} end)')

  local TXN
  TXN=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

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
SET LOCAL ls.ctrl       = :'controller_id';
SET LOCAL ls.profile_id = :'profile_id';
SET LOCAL ls.sku        = :'sku';
SET LOCAL ls.kind       = :'reward_kind';
SET LOCAL ls.sku_limit  = :'sku_limit';
SET LOCAL ls.items      = :'items';
SET LOCAL ls.solari     = :'solari';
SET LOCAL ls.dedupe     = :'dedupe';
SET LOCAL ls.unlocks    = :'unlocks';
SET LOCAL ls.solari_max = :'solari_max';
SET LOCAL ls.track      = :'track';
SET LOCAL ls.spec_xp    = :'spec_xp';
SET LOCAL ls.char_xp    = :'char_xp';

DO \$\$
DECLARE
  v_is_new  boolean;
  v_op      bigint;
  v_kind    text   := current_setting('ls.kind');
  v_ctrl    bigint := current_setting('ls.ctrl')::bigint;
  v_items   jsonb  := current_setting('ls.items')::jsonb;
  v_solari  bigint := current_setting('ls.solari')::bigint;
  v_pawn    bigint;
  v_cnt     bigint;
  v_sum     bigint;
  v_inv     bigint;
  v_cap     int;
  v_used    int;
  v_pos     int;
  v_it      jsonb;
  v_ids     jsonb := '[]'::jsonb;
  v_item_id bigint;
  v_have    text[];
  v_pick    jsonb;
  v_skips   jsonb;
  v_subs    jsonb := '[]'::jsonb;
  v_xp      int;
  v_new_xp  int;
  v_level   real;
  v_entity  bigint;
  v_cxp     bigint;
  v_new_cxp bigint;
  v_levels  int;
  v_intel   int;
BEGIN
  SELECT is_new, op_id FROM _reward_gate INTO v_is_new, v_op;
  IF NOT v_is_new THEN
    RETURN;  -- idempotent replay: no mutation
  END IF;

  -- Per-profile backstop for this SKU, across every linked account.
  SELECT count(*) INTO v_cnt FROM dune.ls_reward_claims
   WHERE reward_kind = v_kind AND status = 'applied' AND id <> v_op
     AND detail->>'profile_id' = current_setting('ls.profile_id')
     AND detail->>'sku' = current_setting('ls.sku');
  IF v_cnt >= current_setting('ls.sku_limit')::int THEN
    RAISE EXCEPTION 'SN_SHOP_FAIL:cap profile % already has % of %', current_setting('ls.profile_id'),
      v_cnt, current_setting('ls.sku');
  END IF;

  -- The controller must be a live character on THIS account.
  SELECT eps.player_pawn_id INTO v_pawn
    FROM dune.encrypted_player_state eps
   WHERE eps.account_id = current_setting('ls.account_id')::bigint
     AND eps.player_controller_id = v_ctrl
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   LIMIT 1;
  IF v_pawn IS NULL THEN
    RAISE EXCEPTION 'SN_SHOP_FAIL:not_owner controller % is not a live character of account %',
      v_ctrl, current_setting('ls.account_id');
  END IF;

  -- The Solari backstop counts every Stall Solari on the profile: purses and the
  -- Solari a bank SKU carries alike.
  IF v_solari > 0 THEN
    SELECT COALESCE(sum((detail->>'solari')::bigint), 0) INTO v_sum FROM dune.ls_reward_claims
     WHERE reward_kind IN ('sn_shop_solari', 'sn_shop_bank') AND status = 'applied' AND id <> v_op
       AND detail->>'profile_id' = current_setting('ls.profile_id');
    IF v_sum + v_solari > current_setting('ls.solari_max')::bigint THEN
      RAISE EXCEPTION 'SN_SHOP_FAIL:cap profile % at % Solari (max %)', current_setting('ls.profile_id'),
        v_sum, current_setting('ls.solari_max');
    END IF;
  END IF;

  IF v_kind = 'sn_shop_solari' THEN
    -- CREDIT ONLY: a positive delta never reaches the proc's clamp branch.
    PERFORM dune.adjust_player_virtual_currency_balance(v_ctrl, 'Solaris'::dune.virtualwallettype, v_solari);
    RETURN;
  END IF;

  IF v_kind IN ('sn_shop_spec', 'sn_shop_charxp') THEN
    -- The track and the level are RAM-resident while the character is in game:
    -- write only when Offline and past the reconnect grace. Any other status defers.
    IF EXISTS (SELECT 1 FROM dune.encrypted_player_state eps
                WHERE eps.player_controller_id = v_ctrl
                  AND (eps.online_status::text IS DISTINCT FROM 'Offline'
                       OR (eps.reconnect_grace_period_end IS NOT NULL
                           AND eps.reconnect_grace_period_end > now()))) THEN
      RAISE EXCEPTION 'SN_SHOP_DEFER:online controller % is in game', v_ctrl;
    END IF;
  END IF;

  IF v_kind = 'sn_shop_charxp' THEN
    IF to_regprocedure('dune.ls_xp_to_level(bigint)') IS NULL
       OR to_regprocedure('dune.ls_intel_at_level(integer)') IS NULL THEN
      RAISE EXCEPTION 'SN_SHOP_FAIL:rejected dune.ls_xp_to_level or dune.ls_intel_at_level is missing';
    END IF;
    SELECT count(*) INTO v_cnt
      FROM dune.fgl_entities fe
      JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
     WHERE afe.actor_id = v_pawn AND afe.slot_name = 'DuneCharacter'
       AND fe.components ? 'FLevelComponent'
       AND jsonb_typeof(fe.components #> '{FLevelComponent,1}') = 'object';
    IF v_cnt <> 1 THEN
      RAISE EXCEPTION 'SN_SHOP_FAIL:rejected % DuneCharacter FLevelComponent rows for pawn %', v_cnt, v_pawn;
    END IF;
    SELECT fe.entity_id, COALESCE((fe.components#>>'{FLevelComponent,1,TotalXPEarned}')::bigint, 0)
      INTO v_entity, v_cxp
      FROM dune.fgl_entities fe
      JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
     WHERE afe.actor_id = v_pawn AND afe.slot_name = 'DuneCharacter'
       AND fe.components ? 'FLevelComponent'
       AND jsonb_typeof(fe.components #> '{FLevelComponent,1}') = 'object'
       FOR UPDATE OF fe;
    IF v_cxp >= 344440 THEN
      RAISE EXCEPTION 'SN_SHOP_FAIL:already_owned character % is at the level cap', v_ctrl;
    END IF;
    -- G11 v2: additive XP plus the levels gained credited to Total AND Unspent
    -- skill points, from the observed values in this row.
    WITH target AS (
      SELECT fe.entity_id,
             COALESCE((fe.components#>>'{FLevelComponent,1,TotalXPEarned}')::bigint, 0)   AS pre_xp,
             COALESCE((fe.components#>>'{FLevelComponent,1,TotalSkillPoints}')::int, 0)   AS pre_total_sp,
             COALESCE((fe.components#>>'{FLevelComponent,1,UnspentSkillPoints}')::int, 0) AS pre_unspent_sp
        FROM dune.fgl_entities fe
       WHERE fe.entity_id = v_entity
    ),
    calc AS (
      SELECT entity_id, pre_xp, pre_total_sp, pre_unspent_sp,
             LEAST(pre_xp + current_setting('ls.char_xp')::bigint, 344440::bigint) AS new_xp,
             (dune.ls_xp_to_level(LEAST(pre_xp + current_setting('ls.char_xp')::bigint, 344440::bigint))
              - dune.ls_xp_to_level(pre_xp))                                           AS levels_gained
        FROM target
    )
    UPDATE dune.fgl_entities fe
       SET components = jsonb_set(jsonb_set(jsonb_set(
           fe.components,
           '{FLevelComponent,1,TotalXPEarned}',      to_jsonb(calc.new_xp)),
           '{FLevelComponent,1,TotalSkillPoints}',   to_jsonb(calc.pre_total_sp + calc.levels_gained)),
           '{FLevelComponent,1,UnspentSkillPoints}', to_jsonb(calc.pre_unspent_sp + calc.levels_gained))
      FROM calc
     WHERE fe.entity_id = calc.entity_id
    RETURNING calc.pre_xp, calc.new_xp, calc.levels_gained INTO v_cxp, v_new_cxp, v_levels;
    -- G11 v3: delta-credit intel on the pawn, never set it (spent intel stays spent).
    v_intel := GREATEST(0, dune.ls_intel_at_level(dune.ls_xp_to_level(v_new_cxp))
                           - dune.ls_intel_at_level(dune.ls_xp_to_level(v_cxp)));
    UPDATE dune.actors a
       SET properties = jsonb_set(
             a.properties,
             '{TechKnowledgePlayerComponent,m_TechKnowledgePoints}',
             to_jsonb(COALESCE((a.properties#>>'{TechKnowledgePlayerComponent,m_TechKnowledgePoints}')::int, 0)
                      + v_intel))
     WHERE a.id = v_pawn
       AND a.properties ? 'TechKnowledgePlayerComponent'
       AND v_intel > 0;
    UPDATE dune.ls_reward_claims
       SET detail = detail || jsonb_build_object('xp_before', v_cxp, 'xp_after', v_new_cxp,
                                                 'levels_gained', v_levels, 'intel_gained', v_intel)
     WHERE id = v_op;
    RETURN;
  END IF;

  IF v_kind = 'sn_shop_spec' THEN
    SELECT st.xp_amount INTO v_xp FROM dune.specialization_tracks st
     WHERE st.player_id = v_ctrl
       AND st.track_type = current_setting('ls.track')::dune.specializationtracktype
       FOR UPDATE;
    v_xp := COALESCE(v_xp, 0);
    IF v_xp >= 44182 THEN
      RAISE EXCEPTION 'SN_SHOP_FAIL:already_owned % track is at the cap', current_setting('ls.track');
    END IF;
    v_new_xp := LEAST(v_xp + current_setting('ls.spec_xp')::int, 44182);
    -- Linear interpolation over the live xp->level curve, the same 46 points as
    -- ops/spec-xp-boost/spec-xp-boost.py (test_sietch_nights.py pins them equal).
    SELECT (c.l0 + (v_new_xp - c.x0)::double precision / (c.x1 - c.x0) * (c.l1 - c.l0))::real
      INTO v_level
      FROM (SELECT lag(x) OVER (ORDER BY x) AS x0, lag(l) OVER (ORDER BY x) AS l0, x AS x1, l AS l1
              FROM (VALUES
                (0,0),(100,1),(125,1.2380953),(225,2.1818182),(250,2.409091),(350,3.3017242),
                (375,3.5172415),(625,5.5625),(750,6.5111113),(875,7.415493),(1000,8.281879),
                (1250,9.910828),(1350,10.52439),(1375,10.676829),(1600,12.005618),(1875,13.526882),
                (1975,14.061855),(2000,14.1907215),(2250,15.460396),(2375,16.075829),(2500,16.668247),
                (2750,17.818182),(3375,20.475609),(3500,20.98374),(3625,21.47826),(3750,21.972332),
                (4750,25.665493),(5125,26.958904),(6250,30.56135),(6875,32.432747),(7125,33.16),
                (7500,34.226257),(8625,37.278645),(9000,38.249363),(10750,42.528168),(11000,43.112904),
                (12100,45.606667),(12350,46.159042),(14750,51.17729),(15225,52.121567),(17375,56.22836),
                (18375,58.051693),(21750,63.90339),(26625,71.863785),(42750,97.70513),(44182,100)
              ) AS curve(x, l)) c
     WHERE c.x0 IS NOT NULL AND v_new_xp > c.x0 AND v_new_xp <= c.x1;
    IF v_level IS NULL THEN
      RAISE EXCEPTION 'SN_SHOP_ERR no curve segment for % xp', v_new_xp;
    END IF;
    PERFORM dune.set_specialization_xp_and_level(v_ctrl,
      current_setting('ls.track')::dune.specializationtracktype, v_new_xp, v_level);
    UPDATE dune.ls_reward_claims
       SET detail = detail || jsonb_build_object('xp_before', v_xp, 'xp_after', v_new_xp, 'level_after', v_level)
     WHERE id = v_op;
    RETURN;
  END IF;

  -- One emote row per character: do not sell a duplicate (owned, or unused in the bank).
  IF current_setting('ls.dedupe') = 'emote' AND EXISTS (
       SELECT 1 FROM dune.items i
         JOIN dune.inventories inv ON inv.id = i.inventory_id
        WHERE inv.actor_id = v_pawn AND inv.inventory_type IN (14, 27, 30)
          AND i.template_id = v_items->0->>'template_id') THEN
    RAISE EXCEPTION 'SN_SHOP_FAIL:already_owned %', v_items->0->>'template_id';
  END IF;

  -- What this character has learned (every unlock id in m_UnlockedCustomizationIds).
  -- A malformed unlock list reads as empty and an entry without an id is skipped
  -- (a NULL would make <> ALL unknown), so bad data can only ever sell, never
  -- refuse or raise an ambiguous error.
  SELECT COALESCE(array_agg(e->>'m_CustomizationId') FILTER (WHERE e->>'m_CustomizationId' IS NOT NULL), '{}')
    INTO v_have
    FROM dune.actors a
    CROSS JOIN LATERAL jsonb_array_elements(
      CASE WHEN jsonb_typeof(a.properties #> '{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}') = 'array'
           THEN a.properties #> '{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}'
           ELSE '[]'::jsonb END) e
   WHERE a.id = v_pawn;
  v_have := COALESCE(v_have, '{}');

  -- Swatches, variants, weapon skins: do not sell what this character already
  -- has, either learned or as an unused copy (owned, or in the bank).
  IF current_setting('ls.dedupe') = 'unlock' THEN
    IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(current_setting('ls.unlocks')::jsonb) u
                    WHERE u <> ALL (v_have)) THEN
      RAISE EXCEPTION 'SN_SHOP_FAIL:already_owned every unlock of % is learned', current_setting('ls.sku');
    END IF;
    IF EXISTS (
         SELECT 1 FROM dune.items i
           JOIN dune.inventories inv ON inv.id = i.inventory_id
          WHERE inv.actor_id = v_pawn AND inv.inventory_type IN (14, 27, 30)
            AND i.template_id IN (SELECT value->>'template_id' FROM jsonb_array_elements(v_items))) THEN
      RAISE EXCEPTION 'SN_SHOP_FAIL:already_owned an unused copy of % is held', current_setting('ls.sku');
    END IF;
  END IF;

  -- The CHOAM bank of THIS character's pawn, exactly one row.
  SELECT inv.id, inv.max_item_count,
         (SELECT COUNT(*) FROM dune.items WHERE inventory_id = inv.id),
         COALESCE((SELECT MAX(position_index) FROM dune.items WHERE inventory_id = inv.id), -1)
    INTO v_inv, v_cap, v_used, v_pos
    FROM dune.inventories inv
   WHERE inv.actor_id = v_pawn AND inv.inventory_type = 30
   ORDER BY inv.id
   LIMIT 1;
  IF v_inv IS NULL THEN
    RAISE EXCEPTION 'SN_SHOP_FAIL:bank_unopened no bank for pawn %', v_pawn;
  END IF;
  IF v_used + jsonb_array_length(v_items) > v_cap THEN
    RAISE EXCEPTION 'SN_SHOP_FAIL:capacity used % + % > cap %', v_used, jsonb_array_length(v_items), v_cap;
  END IF;

  -- Per-item dedupe inside a bundle. An item with skip_if_unlocked is NOT minted
  -- when the character has learned every listed id, or holds an unused copy of
  -- it (inventories 14, 27, 30): the game deletes a duplicate unlock item the
  -- moment it is moved out of the bank (proven live 2026-10-24 on the Feast hook).
  -- Its fallback, checked the same way, is minted instead; when the fallback is
  -- owned too, the item is dropped and the rest of the bundle still delivers.
  -- An empty skip list never counts as learned.
  FOR v_it IN SELECT value FROM jsonb_array_elements(v_items) LOOP
    v_pick := v_it;
    v_skips := '[]'::jsonb;
    WHILE v_pick IS NOT NULL AND jsonb_typeof(v_pick) = 'object'
          AND jsonb_typeof(v_pick->'skip_if_unlocked') = 'array' LOOP
      IF jsonb_array_length(v_pick->'skip_if_unlocked') > 0
         AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(v_pick->'skip_if_unlocked') u
                          WHERE u <> ALL (v_have)) THEN
        v_skips := v_skips || jsonb_build_object('template_id', v_pick->>'template_id', 'reason', 'learned');
      ELSIF EXISTS (
          SELECT 1 FROM dune.items i
            JOIN dune.inventories inv ON inv.id = i.inventory_id
           WHERE inv.actor_id = v_pawn AND inv.inventory_type IN (14, 27, 30)
             AND i.template_id = v_pick->>'template_id') THEN
        v_skips := v_skips || jsonb_build_object('template_id', v_pick->>'template_id', 'reason', 'held');
      ELSE
        EXIT;
      END IF;
      v_pick := v_pick->'fallback';
    END LOOP;
    IF v_pick IS NOT NULL AND jsonb_typeof(v_pick) <> 'object' THEN
      v_pick := NULL;
    END IF;
    IF jsonb_array_length(v_skips) > 0 THEN
      v_subs := v_subs || jsonb_build_object('wanted', v_it->>'template_id',
                                             'delivered', v_pick->>'template_id',
                                             'skipped', v_skips);
    END IF;
    CONTINUE WHEN v_pick IS NULL;

    v_pos := v_pos + 1;
    INSERT INTO dune.items
      (inventory_id, stack_size, position_index, template_id, stats,
       quality_level, acquisition_time, is_new)
    VALUES
      (v_inv, (v_pick->>'qty')::int, v_pos, v_pick->>'template_id', '{}'::jsonb,
       (v_pick->>'grade')::int, 0, true)
    RETURNING id INTO v_item_id;
    v_ids := v_ids || to_jsonb(v_item_id);
  END LOOP;

  UPDATE dune.ls_reward_claims
     SET detail = detail || jsonb_build_object('bank_inv', v_inv, 'item_ids', v_ids)
                         || CASE WHEN jsonb_array_length(v_subs) > 0
                                 THEN jsonb_build_object('substitutions', v_subs) ELSE '{}'::jsonb END
   WHERE id = v_op;

  -- Solari carried by a bank SKU: credit only, same proc and txn as the items.
  IF v_solari > 0 THEN
    PERFORM dune.adjust_player_virtual_currency_balance(v_ctrl, 'Solaris'::dune.virtualwallettype, v_solari);
  END IF;
END
\$\$;

SELECT 'RESULT|' || g.op_id::text || '|'
       || CASE WHEN g.is_new THEN 'applied' ELSE 'replay' END || '|'
       || COALESCE(c.detail->>'sku', '') || '|' || COALESCE(c.detail->>'controller_id', '')
  FROM _reward_gate g JOIN dune.ls_reward_claims c ON c.id = g.op_id;

COMMIT;
EOF
)

  local -a vargs=(
    -v "idem=${idem}"
    -v "account_id=${account_id}"
    -v "operator=${operator}"
    -v "reward_kind=${kind}"
    -v "detail=${DETAIL_JSON}"
    -v "controller_id=${ctrl}"
    -v "profile_id=${profile_id}"
    -v "sku=${sku}"
    -v "sku_limit=${limit}"
    -v "items=${items}"
    -v "solari=${solari}"
    -v "dedupe=${dedupe}"
    -v "unlocks=${unlocks}"
    -v "solari_max=${SN_SOLARI_MAX_PER_PROFILE}"
    -v "track=${track}"
    -v "spec_xp=${spec_xp}"
    -v "char_xp=${char_xp}"
  )

  if [[ "$dry" == "1" ]]; then
    local vars_json
    vars_json=$(printf '%s\0' "${vargs[@]}" | python3 -c '
import json, sys
parts = [p for p in sys.stdin.read().split("\0") if p]
print(json.dumps(dict(p.split("=", 1) for p in parts if p != "-v")))')
    printf '{"success":true,"status":"dry-run","reward_kind":%s,"sku":%s,' "$(json_str "$kind")" "$(json_str "$sku")"
    printf '"message":"dry-run: SQL built, NOT executed","vars":%s,"sql":%s}\n' "$vars_json" "$(json_str "$TXN")"
    exit 0
  fi

  resolve_db_pod

  local result code
  if ! result=$(printf '%s\n' "$TXN" | run_psql -tA "${vargs[@]}" 2>&1); then
    if printf '%s' "$result" | grep -q 'SN_SHOP_DEFER:'; then
      printf '{"success":true,"status":"deferred","reward_kind":%s,"message":%s}\n' "$(json_str "$kind")" \
        "$(json_str "the character is in game; nothing applied, retry once it is offline")"
      exit 0
    fi
    code=$(printf '%s' "$result" | grep -oE 'SN_SHOP_FAIL:[a-z_]+' | head -n1 | cut -d: -f2)
    if [[ -n "$code" ]]; then
      sn_fail "$code" "$kind" "$(printf '%s' "$result" | grep -oE 'SN_SHOP_FAIL:[^"]*' | head -n1 | tail -c 300)"
    fi
    # Not a refusal we raised: the txn may not have run at all, or the reply was
    # lost. NO error code, so the portal re-drives the same key instead of refunding.
    printf '{"success":false,"status":"failed","reward_kind":%s,"message":%s}\n' "$(json_str "$kind")" \
      "$(json_str "$kind transaction failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)")"
    exit 6
  fi

  local row op_id state got_sku got_ctrl
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  op_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  got_sku=$(printf '%s' "$row" | cut -d'|' -f4 | tr -d '[:space:]')
  got_ctrl=$(printf '%s' "$row" | cut -d'|' -f5 | tr -d '[:space:]')
  [[ "$op_id" =~ ^[0-9]+$ && "$got_ctrl" =~ ^[0-9]+$ ]] \
    || fail_json "$kind committed but returned no RESULT row" 6
  # The echo is the ROW's sku and controller, not the request's: a replay of a key
  # that was first used for something else will not match, and the portal holds it.
  printf '{"success":true,"status":%s,"reward_kind":%s,"audit_id":%s,"delivery_key":%s,"sku":%s,"controller_id":%s}\n' \
    "$(json_str "$state")" "$(json_str "$kind")" "$op_id" "$(json_str "$idem")" "$(json_str "$got_sku")" "$got_ctrl"
  exit 0
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

  local account_id reward_kind idem amount template_id quality augments operator req_mode period_key
  account_id=$(jq_get '.account_id')
  reward_kind=$(jq_get '.reward_kind')
  idem=$(jq_get '.idempotency_key')
  amount=$(jq_get '.amount')
  template_id=$(jq_get '.template_id')
  quality=$(jq_get '.quality_level')
  augments=$(jq_get '.augments'); [[ -n "$augments" ]] || augments="[]"
  operator=$(jq_get '.operator'); [[ -n "$operator" ]] || operator="lastsietch-reward"
  req_mode=$(jq_get '.mode'); [[ -n "$req_mode" ]] || req_mode="apply"
  period_key=$(jq_get '.period_key')   # required for monthly_augment (period-scoped cap)

  validate_uuid "$idem"
  validate_posint "$account_id" "account_id"
  case "$reward_kind" in
    daily_solari|weekly_item|monthly_augment|sn_shop_bank|sn_shop_solari|sn_shop_spec|sn_shop_charxp) ;;
    *) fail_json "invalid reward_kind: $reward_kind" 2 ;;
  esac
  case "$req_mode" in apply|dry-run) ;; *) fail_json "invalid mode: $req_mode" 2 ;; esac

  local dry=0
  [[ "$DRY_RUN" == "1" || "$req_mode" == "dry-run" ]] && dry=1

  # --- Seven Nights shop: its own gate, never LASTSIETCH_REWARD_ENABLED ---------------
  if [[ "$reward_kind" == sn_shop_* ]]; then
    if ! sn_gate_open "$account_id"; then
      printf '{"success":true,"status":"deferred","reward_kind":%s,"message":%s}\n' \
        "$(json_str "$reward_kind")" "$(json_str "the Seven Nights shop is closed for this account; nothing applied")"
      exit 0
    fi
    do_sn_shop "$reward_kind" "$account_id" "$idem" "$operator" "$dry"
  fi

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
      [[ -n "$period_key" ]] || fail_json "monthly_augment requires period_key" 2
      do_monthly_augment "$account_id" "$idem" "$template_id" "$quality" "$augments" "$operator" "$dry" "$period_key"
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
