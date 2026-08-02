#!/usr/bin/env bash
# The Karum WRITE tool — game-host resident. L1 of the four-layer contract, and the ONLY
# thing that writes game state for the player-to-player trade venue.
#
# Deployed to the game host:/root/dune-karum-op.sh (mode 0750, owner root). Invoked ONLY by
# the forced-command dispatcher (a 'karum-op' action reading --op-b64-stdin), reached over
# the relay SSH key. Runs as root and does its own `sudo kubectl exec` into the Dune DB pod.
#
#
#
# ── THE ONE THING TO UNDERSTAND ────────────────────────────────────────────────
# There is EXACTLY ONE TAKE in this system: the seller, at listing time, offline-gated.
# Every other leg is a give. That is not a style preference, it is why the dupe path is
# closed by construction:
#
#   karum-list    take from the seller's bank   🔒 OFFLINE-GATED, the only gate
#   (escrow)      park the row in exchange 610     give-shaped, no player session involved
#   karum-buy A   two-sided balance adjust         tabular, proc-routed, no item row
#   karum-buy B   hand the item to the buyer       give, engine places it on claim
#   karum-cancel  hand the item back to the seller give, same mechanism
#
# The take is NOT written here. It lives in scripts/lib/dune-take-item.sh and is shared
# verbatim with dune-item-transfer-op.sh (owner decision D3: one gated-take implementation
# in the codebase). **If you find yourself writing `UPDATE dune.items ... SET inventory_id`
# in this file, stop: that is a second take and it does not ship.**
#
# Why the gate matters: live-tested 2026-07-26 on build 24376904, removing an item from an
# ONLINE player's bank is not durable. The client holds the whole item record including its
# primary key and writes it back to a destination without re-checking the row still exists,
# so the row is restored under its ORIGINAL id and survives a full reload. Taking from a
# loaded session is a duplication path. Giving to one is fine.
#
# ── ORDERING LAW (karum-buy) ───────────────────────────────────────────────────
# PAYMENT FIRST, THEN DELIVERY. The irreversible step goes last. A payment is a two-sided
# balance adjust and can be compensated with the same primitive that made it; a delivery
# cannot be cleanly reversed once the buyer can walk up and claim it. So there is never a
# state where the item is claimable but the money has not moved.
#
# The two are SEPARATE TRANSACTIONS, deliberately. That is what makes paid_undelivered a
# real state, and it is why `paid` and `delivered` are returned as separate booleans that
# are ALWAYS both present. L4 branches on those booleans, never on `status` alone.
#
# 🔴 Because the legs are split, a retried karum-buy re-enters transaction A. Every
# mutating leg therefore has its own UNIQUE correlation_id gate, checked FIRST, in the same
# transaction as the mutation, and the mutation only runs when the insert was new:
#
#   list          dune.ls_karum_escrow.correlation_id       (text, UNIQUE)
#   buy txn A     dune.ls_karum_payments.correlation_id     (uuid, UNIQUE)
#   buy txn B     dune.ls_item_delivery_log.correlation_id  (text, UNIQUE)
#   cancel        dune.ls_item_delivery_log.correlation_id  (text, UNIQUE)
#   refund        dune.ls_karum_payments, a NEW row with its own id
#
# The Funcom currency proc provides atomicity, NOT idempotency. dune-gift-op.sh is
# idempotent because it owns dune.ls_guild_gifts and gates on the insert being new
# (:158-172 opens the gate, :190-193 returns before touching a balance). Assuming the proc
# self-guards is the trap that nearly shipped a money dupe here; see contract 8.1b.
#
# 🔴 A REFUND IS A NEW PAYMENTS ROW, never an UPDATE of the original. A retried refund that
# was implemented as an UPDATE would double-credit.
#
# ── WHAT THIS SCRIPT DOES NOT DECIDE ───────────────────────────────────────────
#   * Contention. Two buyers racing one listing is settled by the admin.db compare-and-set
#     `active` -> `selling` in L4, BEFORE any game write. The loser never reaches here and
#     is never charged. Do not try to resolve contention in the writer.
#   * The alt self-trade check. Linked alts share a discord_id and the listing row carries
#     both discord ids precisely so L4 can compare columns. This script can only compare
#     account ids and controllers, which it does, but it is the second line, not the first.
#   * Listing state. Karum's own state lives in admin.db per the custom-table-ownership
#     rule; only settlement crosses into dune.*, and only with a ledger row.
#
# LASTSIETCH_KARUM_ENABLED (env, default "0" = OFF/DARK): while off every action is code-complete
# but refuses, returning {"status":"deferred"} WITHOUT opening a DB txn. Never a fake
# success. Un-dark only after QA plus a two-account owner self-test, and mirror the flag
# default into the repo the SAME session or the next deploy silently reverts it.
#
# HARD CONSTRAINTS (same as dune-gift-op.sh / dune-item-transfer-op.sh):
#   * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session into
#     the ALREADY-RUNNING DB pod.
#   * Every op runs inside BEGIN; ... COMMIT; with -v ON_ERROR_STOP=1.
#   * Every field decoded from the base64 JSON is RE-VALIDATED here.
#   * Identities are resolved FRESH inside the txn, tombstone-safe.

set -euo pipefail

# --- caps / tunables ---------------------------------------------------------
LASTSIETCH_KARUM_ENABLED="${LASTSIETCH_KARUM_ENABLED:-1}"                    # 0 = dark/refuse; 1 = live
# UN-DARKED 2026-07-28 (owner-authorized) for the two-account owner self-test the header
# above requires. Default mirrored into the repo the SAME session, per that same note --
# leaving it at 0 here would have had the next deploy silently re-dark the venue.
KARUM_MAX_LISTINGS_PER_DAY="${KARUM_MAX_LISTINGS_PER_DAY:-20}" # per seller / 24h
KARUM_MAX_PAIR_PER_DAY="${KARUM_MAX_PAIR_PER_DAY:-5}"          # per buyer->seller / 24h
# Per-listing ceiling. RAISED 1,000,000 -> 900,000,000 on 2026-07-27 (owner call), because
# the original number was inherited from GIFT_MAX_AMOUNT and that was the wrong precedent: a
# gift is a one-sided transfer, so a tight cap is its anti-RMT brake. A Karum sale is
# value-conserving -- goods one way, Solari the other -- so the cap's real job here is bounding
# a fat-finger, and the anti-laundering control is the same-discord self-trade check.
#
# Grounded in live balances (2026-07-27, 94 real characters): median 334k, p75 19.8M,
# p95 2.95B. So 1M was below the median holding times three and blocked almost everyone;
# 900M sits above all but ~7 players.
KARUM_MAX_PRICE="${KARUM_MAX_PRICE:-900000000}"                # Solari, per listing
KARUM_CLAIM_EXPIRY_DAYS="${KARUM_CLAIM_EXPIRY_DAYS:-365}"

# The in-row escrow sentinel (a HolKarum key merged into dune.items.stats) is REQUIRED by
# contract section 4.3b **once LT-5 passes**, and LT-5 has not been run: dune.items.stats is
# a real engine-deserialised structure, not a free-text sidecar, so an unknown top-level key
# may be ignored or may fail deserialisation on load. Until then the ledger
# (dune.ls_karum_escrow) is the sole marker, which the contract explicitly permits for
# Phase 1. Flip to 1 only after LT-5 passes.
LASTSIETCH_KARUM_STATS_SENTINEL="${LASTSIETCH_KARUM_STATS_SENTINEL:-0}"

# 🔴 LT-7 mitigation, and read the caveat before changing it. The market bot reads
# `SELECT COALESCE(MAX(position_index), -1) + 1 FROM dune.items WHERE inventory_id = 610`
# ONCE at init (dune-market-bot/exchange.py:775-777), caches it, and increments locally as
# it inserts. A Karum row written while the bot is running is invisible to that cached
# value, so the bot can later insert at a position_index Karum already occupies. Allocating
# Karum escrow from a high base keeps the two allocators apart while the bot is running.
#
# ⚠️ IT DOES NOT CLOSE THE HOLE, and the contract's "cannot overlap by construction" wording
# is too strong. Karum rows become the MAX in 610, so the NEXT bot restart caches a value
# just above them and both allocators climb from adjacent points again. Consequences of a
# duplicate position_index in an exchange inventory are UNPROVEN (INFERRED harmless, because
# the exchange addresses items through orders rather than by slot, which is also why the
# 368k orphans are invisible to the bot). The nightly audit MUST therefore count Karum rows
# in 610 sharing a position_index with any other row. The real fix is making the bot re-read
# MAX per insert; that touches a live, recently-stabilised service and was deferred.
KARUM_POSITION_BASE="${KARUM_POSITION_BASE:-1000000000}"

DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

# Claim-lane constants, PROVEN live 2026-07-25 (ops/exchange-claim-delivery/deliver-claim.py).
EXCHANGE_ID=2
ACCESS_POINT=1
COMPLETION_TYPE=3          # the ONLY completion format the client renders; reads as CANCELED

# ── A PSQL FOOTGUN, STATED ONCE ────────────────────────────────────────────────
# psql does NOT interpolate :vars inside dollar-quoted strings, and a plpgsql DO block IS a
# dollar-quoted string. A `:amount` inside DO $x$ ... $x$ is sent to the server verbatim and
# fails as a syntax error. That is exactly why dune-gift-op.sh sets `SET LOCAL ls.amount`
# outside its block and reads `current_setting('ls.amount')` inside.
#
# The rule in this file, and do not break it:
#   * plain SQL statements       -> psql :vars are fine
#   * inside any DO $x$ block    -> either a shell-folded literal (only for values already
#                                   validated as integers, so injection is impossible) or
#                                   current_setting() off a SET LOCAL made outside the block
#
# --- helpers (mirror dune-gift-op.sh / dune-item-transfer-op.sh) --------------
json_str() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

# The optional 3rd arg is a stable error token, emitted as "error". The relay passes this
# JSON through verbatim and the portal reads result["error"], so a token here is what lets
# the UI say "log out of the game first" instead of a generic failure.
fail_json() {
  local msg="$1" code="${2:-1}" token="${3:-}"
  if [[ -n "$token" ]]; then
    printf '{"success":false,"status":"failed","error":%s,"message":%s}\n' \
      "$(json_str "$token")" "$(json_str "$msg")"
  else
    printf '{"success":false,"status":"failed","message":%s}\n' "$(json_str "$msg")"
  fi
  exit "$code"
}

# Every RAISE on these paths leads with a stable lowercase token. Order matters only in that
# no token may be a substring of another.
KARUM_ERROR_TOKENS=(player_online no_bank item_not_found take_failed self_trade
                    insufficient_funds rate_limited price_too_high escrow_missing
                    no_category no_game_clock no_character already_settled)

error_token_of() {
  local blob token
  blob=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  for token in "${KARUM_ERROR_TOKENS[@]}"; do
    case "$blob" in *"$token"*) printf '%s' "$token"; return 0 ;; esac
  done
  printf 'write_failed'
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

# The shared gated take. Sourced lazily, AFTER the dark gate, so a box that has this writer
# but not yet the library still returns a clean deferred instead of a new failure mode.
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

OP_JSON=""
jq_get() { printf '%s' "$OP_JSON" | jq -r "$1 // empty"; }

validate_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || fail_json "$2 must be a UUID: $1" 2
}
validate_posint() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]] || fail_json "$2 must be a positive integer: $1" 2
}
validate_nonneg() {
  [[ "$1" =~ ^(0|[1-9][0-9]*)$ ]] || fail_json "$2 must be a non-negative integer: $1" 2
}
validate_template() {
  [[ "$1" =~ ^[A-Za-z0-9_-]{2,64}$ ]] \
    || fail_json "$2 must be 2-64 chars of letters, digits, _ or -: $1" 2
}

# Emit-or-execute. Every action builds its SQL first and runs it only when not in dry-run,
# which also means --dry-run works off-box: no resolve_db_pod, no kubectl, no DB.
#
# Results come back in the GLOBAL $RUN_OUT rather than on stdout, and callers must NOT wrap
# this in a command substitution: $(...) is a subshell, so the DRY_SQL accumulator and
# LAST_ERR would be written in the child and lost in the parent. That produced an empty
# --dry-run and, worse, swallowed the error text the token parser reads.
# 🔴 resolve_db_pod is called HERE, lazily, and nowhere else.
#
# It was originally never called at all. The function was defined, set DB_NS and DB_POD, and no
# code path invoked it -- so under `set -u` the very first run_psql died with
# "DB_NS: unbound variable" (exit 6) before opening a transaction. Every apply-mode write failed
# 100% of the time, and it survived QA because the writer shipped DARK: the 22-test suite and
# the deploy smoke test both exercise --dry-run, which returns above this line and never needs a
# pod. Found on the first real listing attempt, 2026-07-27, by which point the feature was live.
#
# Resolving inside emit_or_run rather than in main() means:
#   * all six write call sites are covered by construction, so a seventh cannot forget;
#   * --dry-run still works off-box (it returns before this), which the test suite depends on;
#   * a DARK writer never shells out to kubectl, because it refuses upstream of any emit_or_run.
# Guarded on DB_POD being unset/empty so a multi-statement action (karum-buy runs two
# transactions) resolves once, not once per transaction.
emit_or_run() {
  local label="$1" sql="$2"
  RUN_OUT=""
  if [[ "$DRY_RUN" == "1" ]]; then
    DRY_SQL+=("$label"$'\n'"$sql")
    return 0
  fi
  if [[ -z "${DB_POD:-}" ]]; then
    resolve_db_pod
  fi
  if ! RUN_OUT=$(printf '%s\n' "$sql" | run_psql -tA "${VARGS[@]}" 2>&1); then
    LAST_ERR="$RUN_OUT"
    return 1
  fi
  return 0
}

dry_run_json() {
  local joined="" chunk
  for chunk in "${DRY_SQL[@]}"; do joined+="$chunk"$'\n\n'; done
  printf '{"success":true,"status":"dry-run","message":"dry-run: SQL built, NOT executed","sql":%s}\n' \
    "$(json_str "$joined")"
  exit 0
}

# --- the delivery / return leg (shared by buy txn B, cancel, and admin) -------
#
# ADOPTS the escrowed row. It is already in inventory 610, so nothing is inserted into
# dune.items and NOTHING IS TAKEN: we wrap the existing row in the claim structure the
# exchange already understands. That is what preserves a T6's grade and durability, and it
# is why this must not call deliver-claim.py, which MINTS a fresh item at quality_level 0.
#
# $1 listing id | $2 target account id | $3 target ctrl role (buyer|seller)
# $4 escrow end state | $5 listing price for the order's cosmetic item_price
#
# listing_id is an explicit parameter rather than read from the caller's scope: bash would
# have resolved it dynamically and silently, and a delivery leg that settles the wrong
# listing is not a failure mode worth leaving to scoping luck.
build_delivery_txn() {
  local listing_id="$1" target_acct="$2" role="$3" end_state="$4" price="$5"
  local buyer_expr="NULL"
  [[ "$role" != "buyer" ]] || buyer_expr="v_ctrl"

  cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- The two non-integer values this transaction needs inside its DO block. Set out here
-- because psql will not interpolate into a dollar-quoted body (see the footgun note above).
SET LOCAL ls.karum_corr     = :'corr';
SET LOCAL ls.karum_operator = :'operator';

DROP TABLE IF EXISTS _dlv_result;
CREATE TEMP TABLE _dlv_result(
  state text, order_id bigint, item_id bigint, escrow_id bigint, stack_size bigint
) ON COMMIT DROP;

DO \$dlv\$
DECLARE
  v_ctrl     bigint;
  v_fls      text;
  v_actor    bigint;
  v_esc      record;
  v_log_id   bigint;
  v_order    bigint;
  v_mask     bigint;
  v_depth    bigint;
  v_expiry   bigint;
  v_stack    bigint;
BEGIN
  -- 1. The recipient of the give, resolved FRESH and tombstone-safe. owner_id on an
  -- exchange order is the player_controller_id (confirmed live against a real claim).
  SELECT eps.player_controller_id, acc."user", eps.player_pawn_id
    INTO v_ctrl, v_fls, v_actor
    FROM dune.encrypted_player_state eps
    JOIN dune.accounts acc ON acc.id = eps.account_id
   WHERE eps.account_id = ${target_acct}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
     AND eps.player_controller_id IS NOT NULL
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1;
  IF v_ctrl IS NULL THEN
    RAISE EXCEPTION 'no_character (account % has no live character to deliver to)', ${target_acct};
  END IF;

  -- 2. The escrowed row, LOCKED. Read from OUR ledger, never from a client-supplied item
  -- id: the ledger is the authoritative marker and the client has no business naming a
  -- row in inventory 610.
  SELECT * INTO v_esc FROM dune.ls_karum_escrow
   WHERE listing_id = ${listing_id} AND state = 'held'
   ORDER BY id DESC
   LIMIT 1
   FOR UPDATE;

  IF NOT FOUND THEN
    -- Either this already settled (the ordinary replay: escrow left 'held' and the delivery
    -- log row exists, both committed together) or nothing was ever escrowed.
    IF EXISTS (SELECT 1 FROM dune.ls_item_delivery_log
                 WHERE correlation_id = current_setting('ls.karum_corr')) THEN
      INSERT INTO _dlv_result
        SELECT 'replay', e.order_id, e.item_id, e.id, e.stack_size
          FROM dune.ls_karum_escrow e
         WHERE e.listing_id = ${listing_id}
         ORDER BY e.id DESC LIMIT 1;
      RETURN;
    END IF;
    RAISE EXCEPTION 'escrow_missing (no held escrow row for listing %)', ${listing_id};
  END IF;

  -- 3. Is the item actually still there? If not we must NOT raise: the contract wants the
  -- escrow state recorded as reconciled_missing and COMMITTED, so L4 can refund the buyer
  -- and the nightly audit can see it. Raising would roll that record back.
  SELECT stack_size INTO v_stack FROM dune.items
   WHERE id = v_esc.item_id AND inventory_id = v_esc.inventory_id;
  IF v_stack IS NULL THEN
    UPDATE dune.ls_karum_escrow
       SET state = 'reconciled_missing', closed_at = now(),
           operator = current_setting('ls.karum_operator')
     WHERE id = v_esc.id;
    INSERT INTO _dlv_result VALUES ('escrow_missing', NULL, v_esc.item_id, v_esc.id, NULL);
    RETURN;
  END IF;

  -- 4. Category + expiry. category_mask/depth are NOT NULL on an exchange order and drive
  -- the client's category header, so a real order for this template is the only reliable
  -- source -- but that source is TRANSIENT.
  --
  -- 🔴 PREFER THE SNAPSHOT the listing leg took. dune_exchange_orders holds currently-live
  -- orders, not history, so resolving here means a template that was categorisable when the
  -- seller listed can be uncategorisable by the time they cancel. That stranded a real item
  -- on 2026-07-27: all three hand-over legs land in this builder, including the operator
  -- page's force-return, so a failure here leaves NO sanctioned route out.
  --
  -- The live lookup stays as a fallback, for escrow rows written before the snapshot columns
  -- existed. It now requires a non-zero mask, matching dune-market-sell.py: a mask of 0
  -- yields an order the client files under no category header, so the player cannot find the
  -- item in the Completed tab -- worse than a refusal, because it looks like a loss.
  v_mask  := v_esc.category_mask;
  v_depth := v_esc.category_depth;
  IF v_mask IS NULL OR v_mask = 0 THEN
    SELECT category_mask, category_depth INTO v_mask, v_depth
      FROM dune.dune_exchange_orders
     WHERE template_id = v_esc.template_id
       AND category_mask <> 0
     GROUP BY category_mask, category_depth
     ORDER BY count(*) DESC, category_mask
     LIMIT 1;
  END IF;
  IF v_mask IS NULL OR v_mask = 0 THEN
    RAISE EXCEPTION 'no_category (no snapshot on escrow % and no live exchange order for % to copy a category from)',
                    v_esc.id, v_esc.template_id;
  END IF;

  -- Engine-authoritative active universe seconds, the clock exchange expiries are measured
  -- against. The bot's calibrated ~24k s offset is deliberately not replicated: at a
  -- 365-day expiry a 7-hour error is 0.08% and duplicating the calibration would mean two
  -- things to keep in step.
  SELECT (EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - universe_time_timestamp))
          - COALESCE(down_time_accumulation, 0) / 1000000.0)::bigint
    INTO v_expiry
    FROM dune.farm_variables
   WHERE universe_time_timestamp IS NOT NULL
   ORDER BY universe_time_timestamp
   LIMIT 1;
  IF v_expiry IS NULL THEN
    RAISE EXCEPTION 'no_game_clock (could not read dune.farm_variables)';
  END IF;
  v_expiry := v_expiry + ${KARUM_CLAIM_EXPIRY_DAYS} * 86400;

  -- 5. GATE for this transaction: dune.ls_item_delivery_log.correlation_id is UNIQUE, so
  -- the insert being new is what licenses the claim structure below. observed_item_id is
  -- recorded UP FRONT (an exchange claim can sit for days, and the confirmation sweep needs
  -- to know which item it is waiting on, or the row stays 'pending' forever and the log
  -- quietly lies about completed deliveries).
  INSERT INTO dune.ls_item_delivery_log
    (correlation_id, source, lane, account_id, fls_id, actor_id, template_id, quantity,
     quality_level, status, operator, observed_item_id)
  VALUES
    (current_setting('ls.karum_corr'), 'auction', 'exchange', ${target_acct}, v_fls,
     v_actor, v_esc.template_id, v_stack, v_esc.quality_level, 'pending',
     current_setting('ls.karum_operator'), v_esc.item_id)
  ON CONFLICT (correlation_id) DO NOTHING
  RETURNING id INTO v_log_id;
  IF v_log_id IS NULL THEN
    -- Unreachable in practice (the log insert and the escrow transition commit together, so
    -- a log row implies escrow already left 'held' and step 2 would have taken the replay
    -- branch). Kept because the alternative to a defensive check here is a second order row.
    INSERT INTO _dlv_result VALUES ('replay', v_esc.order_id, v_esc.item_id, v_esc.id, v_stack);
    RETURN;
  END IF;

  -- 6. THE CLAIM STRUCTURE. Order owns the EXISTING item row; the fulfilled row is what
  -- makes it render as claimable.
  --
  -- 🔴 expiration_time MUST NOT BE NULL. The purge only deletes rows where it IS NOT NULL,
  -- so NULL looks like the safe "never expires" choice, but the client's Completed tab has
  -- an EXPIRATION column it sorts by and a NULL row does not render at all. PROVEN live:
  -- NULL gave "no completed orders available"; a real timestamp rendered immediately.
  --
  -- quality_level carries the ESCROWED row's real grade, not 0. deliver-claim.py passes 0
  -- because it mints a fresh item; we are adopting a real one and a T6 must still read as
  -- a T6.
  INSERT INTO dune.dune_exchange_orders
    (revision, exchange_id, owner_id, item_id, template_id, category_mask, category_depth,
     access_point_id, is_npc_order, item_price, expiration_time,
     durability_cur, durability_max, quality_level)
  VALUES
    (1, ${EXCHANGE_ID}, v_ctrl, v_esc.item_id, v_esc.template_id, v_mask, v_depth,
     ${ACCESS_POINT}, false, ${price}, v_expiry, 0, 0, v_esc.quality_level)
  RETURNING id INTO v_order;

  -- stack_size comes from the LIVE item row, not from the ledger's copy of it. This is the
  -- LT-3 answer: the fulfilled row and the item row must agree or the player is handed the
  -- wrong quantity, and the live row is the one the engine will place.
  INSERT INTO dune.dune_exchange_fulfilled_orders
    (order_id, completion_type, stack_size, source_order_id, original_order_id)
  VALUES
    (v_order, ${COMPLETION_TYPE}, v_stack, NULL, v_order);

  -- 7. Close the escrow. state='delivered' or 'returned' means handed off; the delivery log
  -- stays 'pending' until confirm-claims.py sees the player actually collect it, which is
  -- the honest distinction between claimable and claimed.
  UPDATE dune.ls_karum_escrow
     SET state     = '${end_state}',
         buyer_ctrl = ${buyer_expr},
         order_id  = v_order,
         closed_at = now(),
         operator  = COALESCE(current_setting('ls.karum_operator'), operator)
   WHERE id = v_esc.id;

  -- 8. Drop the in-row sentinel now the ledger row is closed. Unconditional and idempotent:
  -- removing an absent jsonb key is a no-op, so this is also self-healing if the sentinel
  -- flag was toggled between listing and settlement.
  UPDATE dune.items
     SET stats = COALESCE(stats, '{}'::jsonb) - 'HolKarum'
   WHERE id = v_esc.item_id;

  INSERT INTO _dlv_result VALUES ('applied', v_order, v_esc.item_id, v_esc.id, v_stack);
END
\$dlv\$;

SELECT 'DLV|' || state || '|' || COALESCE(order_id::text,'') || '|'
       || COALESCE(item_id::text,'') || '|' || COALESCE(stack_size::text,'')
  FROM _dlv_result;

COMMIT;
EOF
}

# --- karum-list --------------------------------------------------------------
op_list() {
  local listing_id seller_account_id item_id template_id
  listing_id=$(jq_get '.listing_id')
  seller_account_id=$(jq_get '.seller_account_id')
  item_id=$(jq_get '.item_id')
  template_id=$(jq_get '.template_id')

  validate_posint "$listing_id" "listing_id"
  validate_posint "$seller_account_id" "seller_account_id"
  validate_posint "$item_id" "item_id"
  validate_template "$template_id" "template_id"

  require_take_lib

  # The escrow sentinel is gated on LT-5 (see LASTSIETCH_KARUM_STATS_SENTINEL above).
  local -a take_args=(
    item_id="$item_id"
    owner_account_id="$seller_account_id"
    dst=exchange
    correlation_id="$IDEM"
    expected_template="$template_id"
    min_position="$KARUM_POSITION_BASE"
  )
  if [[ "$LASTSIETCH_KARUM_STATS_SENTINEL" == "1" ]]; then
    take_args+=(stats_patch="{\"HolKarum\":{\"listing_id\":${listing_id}}}")
  fi
  local TAKE_SQL
  TAKE_SQL=$(karum_take_item "${take_args[@]}") \
    || fail_json "could not build the gated take for item $item_id" 2 "write_failed"

  local SQL
  SQL=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- template_id inside a DO block. psql will not interpolate into a dollar-quoted body, so
-- the value crosses on a GUC, the same way build_delivery_txn passes corr and operator.
SET LOCAL ls.karum_tpl = :'template_id';

-- The seller's controller, resolved fresh and tombstone-safe. Resolved BEFORE the gate
-- insert only because seller_ctrl is NOT NULL there and a constraint violation is a much
-- worse error message than a named one.
CREATE TEMP TABLE _karum_seller ON COMMIT DROP AS
SELECT eps.player_controller_id AS ctrl
  FROM dune.encrypted_player_state eps
 WHERE eps.account_id = :seller_account_id
   AND eps.character_state IS DISTINCT FROM 'Deleted'
   AND eps.player_controller_id IS NOT NULL
 ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
 LIMIT 1;

DO \$chk\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM _karum_seller) THEN
    RAISE EXCEPTION 'no_character (account % has no live character)', ${seller_account_id};
  END IF;
END
\$chk\$;

-- 🔴 EXCHANGE CATEGORY, RESOLVED AND SNAPSHOTTED HERE -- the prevention half of the
-- no_category defect found on the first live cancel (2026-07-27).
--
-- Every leg that hands the item back out builds a dune_exchange_orders row, whose
-- category_mask/depth are NOT NULL, and the only reliable source for them is a real order
-- for the same template. dune_exchange_orders is TRANSIENT, so resolving at delivery time
-- means a template that was categorisable at listing can be uncategorisable at cancel --
-- stranding the item with no route out, including via the operator page. Resolving here
-- does two things at once: it lets the LISTING refuse when no category exists (so an
-- undeliverable item is never escrowed), and it stores what every later leg needs.
--
-- category_mask <> 0 is REQUIRED, matching the proven dune-market-sell.py guard. A mask of
-- 0 does not fail, it produces an order the client cannot file under any category header,
-- so the player cannot find the item in the Completed tab. Measured 2026-07-27: over player
-- bank templates, 29/280 have no order at all and a further 5 have only mask-0 orders.
--
-- MODAL mask, not an arbitrary LIMIT 1: 104 templates carry more than one distinct non-zero
-- mask, so an unordered pick is a coin toss between real values. The most-used one is the
-- best-evidenced choice and, unlike an ordering-free pick, it is reproducible.
CREATE TEMP TABLE _karum_cat ON COMMIT DROP AS
SELECT category_mask AS mask, category_depth AS depth
  FROM dune.dune_exchange_orders
 WHERE template_id = :'template_id'
   AND category_mask <> 0
 GROUP BY category_mask, category_depth
 ORDER BY count(*) DESC, category_mask
 LIMIT 1;

-- GATE: dune.ls_karum_escrow.correlation_id is UNIQUE and this insert being new is what
-- licenses the take below. stack_size and quality_level are placeholders here because they
-- are only knowable after the take locks the row; they are backfilled in the post block.
CREATE TEMP TABLE _karum_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_karum_escrow
    (correlation_id, listing_id, item_id, inventory_id, seller_account_id, seller_ctrl,
     template_id, stack_size, quality_level, category_mask, category_depth, state, operator)
  SELECT :'corr', :listing_id, :item_id, dune.get_exchange_inventory_id(${EXCHANGE_ID}),
         :seller_account_id, (SELECT ctrl FROM _karum_seller), :'template_id',
         0, 0, (SELECT mask FROM _karum_cat), (SELECT depth FROM _karum_cat),
         'held', :'operator'
  ON CONFLICT (correlation_id) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_karum_escrow WHERE correlation_id = :'corr'
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS op_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new;

-- REFUSE an item that could not be handed back. Rolls the gate insert back with it, so a
-- refused listing leaves no escrow row and burns no correlation_id.
--
-- Only on a NEW row. A replay must not start failing because the template's last exchange
-- order was culled in between: that row already carries the snapshot it was written with,
-- and re-deciding a settled question is how a harmless retry turns into a hard error.
DO \$cat\$
BEGIN
  IF (SELECT is_new FROM _karum_gate) AND NOT EXISTS (SELECT 1 FROM _karum_cat) THEN
    RAISE EXCEPTION 'no_category (% has no live exchange order with a usable category; refusing to escrow an item that could not be returned)',
                    current_setting('ls.karum_tpl');
  END IF;
END
\$cat\$;

-- REPLAY SHORT-CIRCUIT. The shared take and the post block both check ls.take_skip and
-- no-op, so an exact retry re-reads its own escrow row and mutates nothing. Without this
-- the take would run a second time against a bank that no longer holds the row and abort on
-- its exactly-one-row assertion, turning a harmless retry into a hard failure.
DO \$gate\$
BEGIN
  IF NOT (SELECT is_new FROM _karum_gate) THEN
    PERFORM set_config('ls.take_skip', '1', true);
  END IF;
END
\$gate\$;

-- Durable per-seller listing cap. Counted over our OWN ledger rather than admin.db so a
-- deploy cannot reset it, and excluding this txn's own gate row.
DO \$caps\$
DECLARE
  v_cnt bigint;
BEGIN
  IF COALESCE(current_setting('ls.take_skip', true), '') = '1' THEN RETURN; END IF;
  SELECT count(*) INTO v_cnt FROM dune.ls_karum_escrow
   WHERE seller_account_id = ${seller_account_id}
     AND id <> (SELECT op_id FROM _karum_gate)
     AND held_at >= now() - interval '1 day';
  IF v_cnt >= ${KARUM_MAX_LISTINGS_PER_DAY} THEN
    RAISE EXCEPTION 'rate_limited (seller daily listing cap: % in 24h, max %)',
                    v_cnt, ${KARUM_MAX_LISTINGS_PER_DAY};
  END IF;
END
\$caps\$;

${TAKE_SQL}

-- Backfill the escrow row from the facts the shared take published. Nothing here
-- re-derives an inventory id.
DO \$post\$
DECLARE
  r record;
BEGIN
  IF COALESCE(current_setting('ls.take_skip', true), '') = '1' THEN RETURN; END IF;
  SELECT * INTO r FROM _take_result;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'take_failed (the shared take published no result row)';
  END IF;
  UPDATE dune.ls_karum_escrow
     SET item_id       = r.item_id,
         inventory_id  = r.dst_inv,
         seller_ctrl   = COALESCE(seller_ctrl, r.src_pawn_id),
         template_id   = r.template_id,
         stack_size    = r.stack_size,
         quality_level = r.quality_level
   WHERE id = (SELECT op_id FROM _karum_gate);
END
\$post\$;

SELECT 'RESULT|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _karum_gate;

COMMIT;
EOF
)

  VARGS=(
    -v "corr=${IDEM}"
    -v "listing_id=${listing_id}"
    -v "seller_account_id=${seller_account_id}"
    -v "item_id=${item_id}"
    -v "template_id=${template_id}"
    -v "operator=${OPERATOR}"
  )

  if ! emit_or_run "-- karum-list" "$SQL"; then
    fail_json "karum-list failed: $(printf '%s' "$LAST_ERR" | tr '\n' ' ' | tail -c 400)" \
              6 "$(error_token_of "$LAST_ERR")"
  fi
  [[ "$DRY_RUN" != "1" ]] || dry_run_json
  local out="$RUN_OUT"

  local row escrow_id state
  row=$(printf '%s' "$out" | grep -E '^RESULT\|' | tail -n1)
  escrow_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$escrow_id" ]] || escrow_id="null"

  printf '{"success":true,"status":%s,"escrow_id":%s,"listing_id":%s,"item_id":%s,"correlation_id":%s,"message":%s}\n' \
    "$(json_str "$state")" "$escrow_id" "$listing_id" "$item_id" "$(json_str "$IDEM")" \
    "$(json_str "item escrowed for listing ${listing_id}")"
  exit 0
}

# --- karum-buy ---------------------------------------------------------------
op_buy() {
  local listing_id buyer_account_id seller_account_id amount
  listing_id=$(jq_get '.listing_id')
  buyer_account_id=$(jq_get '.buyer_account_id')
  seller_account_id=$(jq_get '.seller_account_id')
  amount=$(jq_get '.amount')

  validate_posint "$listing_id" "listing_id"
  validate_posint "$buyer_account_id" "buyer_account_id"
  validate_posint "$seller_account_id" "seller_account_id"
  validate_posint "$amount" "amount"
  [[ "$buyer_account_id" != "$seller_account_id" ]] \
    || fail_json "self_trade: buyer and seller are the same account" 2 "self_trade"
  [[ "$amount" -le "$KARUM_MAX_PRICE" ]] \
    || fail_json "price_too_high: $amount exceeds the ceiling $KARUM_MAX_PRICE" 2 "price_too_high"

  local DETAIL_JSON
  DETAIL_JSON=$(printf '%s' "$OP_JSON" \
    | jq -c '{listing_id: (.listing_id|tostring), kind: "karum_buy"}')

  VARGS=(
    -v "corr=${IDEM}"
    -v "listing_id=${listing_id}"
    -v "buyer_account_id=${buyer_account_id}"
    -v "seller_account_id=${seller_account_id}"
    -v "amount=${amount}"
    -v "detail=${DETAIL_JSON}"
    -v "operator=${OPERATOR}"
  )

  # ---- TRANSACTION A: payment. Gate FIRST, mutate only when the insert was new. ----
  local PAY_SQL
  PAY_SQL=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- 🔴 THE PAYMENT GATE. dune.ls_karum_payments.correlation_id is UNIQUE, the insert is in
-- the SAME transaction as the balance adjust, and the adjust only runs when it was new.
-- The Funcom proc gives atomicity, not idempotency. Copied in shape from dune.ls_guild_gifts
-- which is the real mechanism behind dune-gift-op.sh.
CREATE TEMP TABLE _pay_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_karum_payments
    (correlation_id, listing_id, buyer_account_id, seller_account_id, amount, currency_id,
     detail, operator, status, applied_at)
  VALUES
    (:'corr'::uuid, :listing_id, :buyer_account_id, :seller_account_id, :amount,
     dune.get_solaris_id(), :'detail'::jsonb, :'operator', 'applied', now())
  ON CONFLICT (correlation_id) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_karum_payments WHERE correlation_id = :'corr'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS op_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new;

DO \$pay\$
DECLARE
  v_is_new     boolean;
  v_buyer      bigint;
  v_seller     bigint;
  v_buyer_bal  bigint;
  v_seller_bal bigint;
  v_cnt_pair   bigint;
BEGIN
  SELECT is_new FROM _pay_gate INTO v_is_new;
  IF NOT v_is_new THEN
    RETURN;  -- idempotent replay: NOT ONE BALANCE IS TOUCHED
  END IF;

  -- Both controllers FRESH, tombstone-safe.
  SELECT eps.player_controller_id INTO v_buyer
    FROM dune.encrypted_player_state eps
   WHERE eps.account_id = ${buyer_account_id}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1;
  IF v_buyer IS NULL THEN
    RAISE EXCEPTION 'no_character (could not resolve a controller for buyer account %)', ${buyer_account_id};
  END IF;

  SELECT eps.player_controller_id INTO v_seller
    FROM dune.encrypted_player_state eps
   WHERE eps.account_id = ${seller_account_id}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1;
  IF v_seller IS NULL THEN
    RAISE EXCEPTION 'no_character (could not resolve a controller for seller account %)', ${seller_account_id};
  END IF;

  -- Second line of defence on self-dealing. The FIRST line is L4 comparing both account_id
  -- AND discord_id off the listing row, because linked alts share a discord_id and the
  -- account check alone is trivially defeated by a player who owns both sides.
  IF v_buyer = v_seller THEN
    RAISE EXCEPTION 'self_trade (buyer and seller resolve to the same controller)';
  END IF;

  -- Lock both balance rows in deterministic controller order (deadlock-safe).
  PERFORM 1 FROM dune.player_virtual_currency_balances
   WHERE currency_id = dune.get_solaris_id()
     AND player_controller_id IN (v_buyer, v_seller)
   ORDER BY player_controller_id
   FOR UPDATE;

  SELECT balance INTO v_buyer_bal FROM dune.player_virtual_currency_balances
   WHERE player_controller_id = v_buyer AND currency_id = dune.get_solaris_id();
  SELECT balance INTO v_seller_bal FROM dune.player_virtual_currency_balances
   WHERE player_controller_id = v_seller AND currency_id = dune.get_solaris_id();

  -- PRE-CHECK so the buyer-side negative delta never drives the proc into its
  -- clamp / log_cheating / undefined branch. An insufficient balance rolls this whole
  -- transaction back BEFORE anything moves, which is what makes the L4 revert to 'active'
  -- safe: payment provably did not happen.
  IF COALESCE(v_buyer_bal, 0) < ${amount} THEN
    RAISE EXCEPTION 'insufficient_funds (buyer has %, needs %)', COALESCE(v_buyer_bal, 0), ${amount};
  END IF;

  -- Durable per-pair cap over our own ledger, excluding this txn's own gate row.
  SELECT count(*) INTO v_cnt_pair FROM dune.ls_karum_payments
   WHERE buyer_account_id = ${buyer_account_id}
     AND seller_account_id = ${seller_account_id}
     AND status = 'applied'
     AND id <> (SELECT op_id FROM _pay_gate)
     AND applied_at >= now() - interval '1 day';
  IF v_cnt_pair >= ${KARUM_MAX_PAIR_PER_DAY} THEN
    RAISE EXCEPTION 'rate_limited (buyer->seller daily cap: % in 24h, max %)',
                    v_cnt_pair, ${KARUM_MAX_PAIR_PER_DAY};
  END IF;

  UPDATE dune.ls_karum_payments
     SET buyer_balance_before  = COALESCE(v_buyer_bal, 0),
         seller_balance_before = COALESCE(v_seller_bal, 0)
   WHERE id = (SELECT op_id FROM _pay_gate);

  -- Two adjusts, value-conserving, same primitive a refund would use to compensate.
  PERFORM dune.adjust_player_virtual_currency_balance(v_buyer,  dune.get_solaris_id(), -${amount});
  PERFORM dune.adjust_player_virtual_currency_balance(v_seller, dune.get_solaris_id(),  ${amount});
END
\$pay\$;

SELECT 'PAID|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _pay_gate;

COMMIT;
EOF
)

  if ! emit_or_run "-- karum-buy transaction A (payment)" "$PAY_SQL"; then
    # Transaction A rolled back whole, so payment provably did not happen. paid:false with a
    # clean token is what licenses L4 to revert the CAS to 'active'. A TIMEOUT is a different
    # animal: it never reaches here, and L4 must send that listing to 'reconciling' instead.
    local token
    token=$(error_token_of "$LAST_ERR")
    printf '{"success":false,"status":"failed","error":%s,"paid":false,"delivered":false,"correlation_id":%s,"message":%s}\n' \
      "$(json_str "$token")" "$(json_str "$IDEM")" \
      "$(json_str "payment failed: $(printf '%s' "$LAST_ERR" | tr '\n' ' ' | tail -c 400)")"
    exit 6
  fi

  local pay_out="$RUN_OUT"
  local pay_state=""
  if [[ "$DRY_RUN" != "1" ]]; then
    pay_state=$(printf '%s' "$pay_out" | grep -E '^PAID\|' | tail -n1 | cut -d'|' -f3 \
                  | tr -d '[:space:]')
    if [[ "$pay_state" != "applied" && "$pay_state" != "replay" ]]; then
      printf '{"success":false,"status":"failed","error":"write_failed","paid":false,"delivered":false,"correlation_id":%s,"message":"payment produced no usable result"}\n' \
        "$(json_str "$IDEM")"
      exit 6
    fi
  fi

  # ---- TRANSACTION B: delivery. Entered ONLY because paid is true. ----
  local DLV_SQL
  DLV_SQL=$(build_delivery_txn "$listing_id" "$buyer_account_id" buyer delivered "$amount")

  if ! emit_or_run "-- karum-buy transaction B (delivery)" "$DLV_SQL"; then
    # Money moved, goods did not: paid_undelivered. RETRY the SAME correlation_id, never
    # refund-and-return, because if the delivery actually landed and only the response was
    # lost, a refund double-satisfies the trade and creates the item twice.
    printf '{"success":false,"status":"paid_undelivered","error":%s,"paid":true,"delivered":false,"correlation_id":%s,"message":%s}\n' \
      "$(json_str "$(error_token_of "$LAST_ERR")")" "$(json_str "$IDEM")" \
      "$(json_str "paid, delivery failed: $(printf '%s' "$LAST_ERR" | tr '\n' ' ' | tail -c 400)")"
    exit 7
  fi
  [[ "$DRY_RUN" != "1" ]] || dry_run_json
  local dlv_out="$RUN_OUT"

  local drow dstate order_id
  drow=$(printf '%s' "$dlv_out" | grep -E '^DLV\|' | tail -n1)
  dstate=$(printf '%s' "$drow" | cut -d'|' -f2 | tr -d '[:space:]')
  order_id=$(printf '%s' "$drow" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$order_id" ]] || order_id="null"

  if [[ "$dstate" == "escrow_missing" ]]; then
    # The one case where a refund IS correct: the goods provably are not there to deliver.
    # L4 drives it as a NEW payments row with its own correlation_id (karum-admin refund).
    printf '{"success":false,"status":"paid_undelivered","error":"escrow_missing","paid":true,"delivered":false,"correlation_id":%s,"message":"paid, but the escrowed item is gone; escrow marked reconciled_missing and a refund is owed"}\n' \
      "$(json_str "$IDEM")"
    exit 7
  fi
  if [[ "$dstate" != "applied" && "$dstate" != "replay" ]]; then
    printf '{"success":false,"status":"paid_undelivered","error":"write_failed","paid":true,"delivered":false,"correlation_id":%s,"message":"paid, delivery produced no usable result"}\n' \
      "$(json_str "$IDEM")"
    exit 7
  fi

  # The buyer sees status CANCELED in their Completed tab: completion_type 3 is the only
  # format the client renders and it reads as "your listing was cancelled, take your item
  # back". The game will never say "purchase", so the copy MUST explain it.
  printf '{"success":true,"status":%s,"paid":true,"delivered":true,"order_id":%s,"correlation_id":%s,"collect_at":"any CHOAM Exchange terminal","message":"paid and delivered; collect it from the Completed tab (it shows as CANCELED)"}\n' \
    "$(json_str "$([[ "$pay_state" == "replay" && "$dstate" == "replay" ]] && echo replay || echo applied)")" \
    "$order_id" "$(json_str "$IDEM")"
  exit 0
}

# --- karum-cancel ------------------------------------------------------------
op_cancel() {
  local listing_id seller_account_id price
  listing_id=$(jq_get '.listing_id')
  seller_account_id=$(jq_get '.seller_account_id')
  price=$(jq_get '.price'); [[ -n "$price" ]] || price=0

  validate_posint "$listing_id" "listing_id"
  validate_posint "$seller_account_id" "seller_account_id"
  validate_nonneg "$price" "price"

  VARGS=(
    -v "corr=${IDEM}"
    -v "listing_id=${listing_id}"
    -v "operator=${OPERATOR}"
  )

  # A cancel is give-only and reaches the seller through the SAME claim lane as a buyer.
  # No money moves: payment only happens at buy time and a cancel is only reachable from
  # 'active'. Deliberately NOT a re-home back into the seller's bank, which would be a
  # second take-shaped write for no benefit.
  local SQL
  SQL=$(build_delivery_txn "$listing_id" "$seller_account_id" seller returned "$price")

  if ! emit_or_run "-- karum-cancel (return via the claim lane)" "$SQL"; then
    fail_json "karum-cancel failed: $(printf '%s' "$LAST_ERR" | tr '\n' ' ' | tail -c 400)" \
              6 "$(error_token_of "$LAST_ERR")"
  fi
  [[ "$DRY_RUN" != "1" ]] || dry_run_json
  local out="$RUN_OUT"

  local row state order_id
  row=$(printf '%s' "$out" | grep -E '^DLV\|' | tail -n1)
  state=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  order_id=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$order_id" ]] || order_id="null"

  if [[ "$state" == "escrow_missing" ]]; then
    printf '{"success":false,"status":"failed","error":"escrow_missing","returned":false,"correlation_id":%s,"message":"the escrowed item is gone; escrow marked reconciled_missing"}\n' \
      "$(json_str "$IDEM")"
    exit 7
  fi
  printf '{"success":true,"status":%s,"returned":true,"order_id":%s,"correlation_id":%s,"message":"returned to the seller; collect it from the Completed tab (it shows as CANCELED)"}\n' \
    "$(json_str "$state")" "$order_id" "$(json_str "$IDEM")"
  exit 0
}

# --- karum-admin -------------------------------------------------------------
# Operator recovery for the ONE state that can need a human: paid_undelivered. Exists so
# nobody resolves a stuck trade with ad-hoc SQL, which is exactly what it is here to prevent.
op_admin() {
  local admin_action listing_id target_account_id price original_corr
  admin_action=$(jq_get '.admin_action')
  listing_id=$(jq_get '.listing_id')
  target_account_id=$(jq_get '.target_account_id')
  price=$(jq_get '.price'); [[ -n "$price" ]] || price=0
  original_corr=$(jq_get '.original_correlation_id')

  case "$admin_action" in
    force-deliver|force-return|refund) ;;
    *) fail_json "admin_action must be force-deliver, force-return or refund: $admin_action" 2 ;;
  esac

  if [[ "$admin_action" == "refund" ]]; then
    local buyer_account_id seller_account_id amount
    buyer_account_id=$(jq_get '.buyer_account_id')
    seller_account_id=$(jq_get '.seller_account_id')
    amount=$(jq_get '.amount')
    validate_posint "$listing_id" "listing_id"
    validate_posint "$buyer_account_id" "buyer_account_id"
    validate_posint "$seller_account_id" "seller_account_id"
    validate_posint "$amount" "amount"
    validate_uuid "$original_corr" "original_correlation_id"

    local DETAIL_JSON
    DETAIL_JSON=$(printf '{"kind":"karum_refund","listing_id":"%s","reverses":"%s"}' \
                    "$listing_id" "$original_corr")

    VARGS=(
      -v "corr=${IDEM}"
      -v "orig_corr=${original_corr}"
      -v "listing_id=${listing_id}"
      -v "buyer_account_id=${buyer_account_id}"
      -v "seller_account_id=${seller_account_id}"
      -v "amount=${amount}"
      -v "detail=${DETAIL_JSON}"
      -v "operator=${OPERATOR}"
    )

    # 🔴 A refund is a NEW payments row with its OWN correlation_id, never an UPDATE of the
    # original. That is what makes a retried refund a no-op instead of a double-credit. The
    # original row is stamped reversed in the same transaction.
    local SQL
    SQL=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

CREATE TEMP TABLE _refund_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_karum_payments
    (correlation_id, listing_id, buyer_account_id, seller_account_id, amount, currency_id,
     detail, operator, status, applied_at)
  VALUES
    (:'corr'::uuid, :listing_id, :buyer_account_id, :seller_account_id, :amount,
     dune.get_solaris_id(), :'detail'::jsonb, :'operator', 'applied', now())
  ON CONFLICT (correlation_id) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_karum_payments WHERE correlation_id = :'corr'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS op_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new;

DO \$refund\$
DECLARE
  v_is_new boolean;
  v_buyer  bigint;
  v_seller bigint;
BEGIN
  SELECT is_new FROM _refund_gate INTO v_is_new;
  IF NOT v_is_new THEN
    RETURN;  -- idempotent replay: no balance touched
  END IF;

  SELECT eps.player_controller_id INTO v_buyer
    FROM dune.encrypted_player_state eps
   WHERE eps.account_id = ${buyer_account_id}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1;
  SELECT eps.player_controller_id INTO v_seller
    FROM dune.encrypted_player_state eps
   WHERE eps.account_id = ${seller_account_id}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1;
  IF v_buyer IS NULL OR v_seller IS NULL THEN
    RAISE EXCEPTION 'no_character (could not resolve both controllers for the refund)';
  END IF;

  PERFORM 1 FROM dune.player_virtual_currency_balances
   WHERE currency_id = dune.get_solaris_id()
     AND player_controller_id IN (v_buyer, v_seller)
   ORDER BY player_controller_id
   FOR UPDATE;

  -- Reverse direction: the seller gives it back, the buyer is made whole. Same
  -- value-conserving primitive that made the original payment.
  PERFORM dune.adjust_player_virtual_currency_balance(v_seller, dune.get_solaris_id(), -${amount});
  PERFORM dune.adjust_player_virtual_currency_balance(v_buyer,  dune.get_solaris_id(),  ${amount});

  -- Both uuids are folded as literals, having been validated as UUIDs in the shell.
  UPDATE dune.ls_karum_payments
     SET status = 'reversed', reversed_at = now(), reversal_corr_id = '${IDEM}'::uuid
   WHERE correlation_id = '${original_corr}'::uuid;
END
\$refund\$;

SELECT 'REFUND|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _refund_gate;

COMMIT;
EOF
)
    if ! emit_or_run "-- karum-admin refund" "$SQL"; then
      fail_json "karum refund failed: $(printf '%s' "$LAST_ERR" | tr '\n' ' ' | tail -c 400)" \
                6 "$(error_token_of "$LAST_ERR")"
    fi
    [[ "$DRY_RUN" != "1" ]] || dry_run_json
    local out="$RUN_OUT"
    local state
    state=$(printf '%s' "$out" | grep -E '^REFUND\|' | tail -n1 | cut -d'|' -f3 \
              | tr -d '[:space:]')
    printf '{"success":true,"status":%s,"refunded":true,"correlation_id":%s,"message":"refund applied as a new payments row; the original is stamped reversed"}\n' \
      "$(json_str "$state")" "$(json_str "$IDEM")"
    exit 0
  fi

  # force-deliver / force-return: the same give as buy txn B or cancel, operator-recorded.
  validate_posint "$listing_id" "listing_id"
  validate_posint "$target_account_id" "target_account_id"
  validate_nonneg "$price" "price"

  local role end_state
  if [[ "$admin_action" == "force-deliver" ]]; then role=buyer; end_state=delivered
  else role=seller; end_state=returned; fi

  VARGS=(
    -v "corr=${IDEM}"
    -v "listing_id=${listing_id}"
    -v "operator=${OPERATOR}"
  )

  local SQL
  SQL=$(build_delivery_txn "$listing_id" "$target_account_id" "$role" "$end_state" "$price")

  if ! emit_or_run "-- karum-admin $admin_action" "$SQL"; then
    fail_json "karum-admin $admin_action failed: $(printf '%s' "$LAST_ERR" | tr '\n' ' ' | tail -c 400)" \
              6 "$(error_token_of "$LAST_ERR")"
  fi
  [[ "$DRY_RUN" != "1" ]] || dry_run_json
  local out="$RUN_OUT"

  local row state order_id
  row=$(printf '%s' "$out" | grep -E '^DLV\|' | tail -n1)
  state=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  order_id=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$order_id" ]] || order_id="null"

  printf '{"success":true,"status":%s,"admin_action":%s,"order_id":%s,"correlation_id":%s,"message":%s}\n' \
    "$(json_str "$state")" "$(json_str "$admin_action")" "$order_id" "$(json_str "$IDEM")" \
    "$(json_str "$admin_action complete for listing $listing_id")"
  exit 0
}

# --- op dispatch -------------------------------------------------------------
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

  local action req_mode
  action=$(jq_get '.action')
  IDEM=$(jq_get '.correlation_id')
  OPERATOR=$(jq_get '.operator'); [[ -n "$OPERATOR" ]] || OPERATOR="unknown"
  req_mode=$(jq_get '.mode'); [[ -n "$req_mode" ]] || req_mode="apply"

  validate_uuid "$IDEM" "correlation_id"
  case "$req_mode" in apply|dry-run) ;; *) fail_json "invalid mode: $req_mode" 2 ;; esac
  [[ "$req_mode" != "dry-run" ]] || DRY_RUN=1

  # --- dark gate ------------------------------------------------------------
  # Upstream of require_take_lib and of every DB touch, so a box that has this writer but
  # not yet its library still answers deferred rather than a new failure mode.
  if [[ "$LASTSIETCH_KARUM_ENABLED" != "1" ]]; then
    printf '{"success":true,"status":"deferred","paid":false,"delivered":false,"message":%s}\n' \
      "$(json_str "The Karum is not open yet (LASTSIETCH_KARUM_ENABLED=${LASTSIETCH_KARUM_ENABLED}); nothing was written")"
    exit 0
  fi

  case "$action" in
    karum-list)   op_list ;;
    karum-buy)    op_buy ;;
    karum-cancel) op_cancel ;;
    karum-admin)  op_admin ;;
    *) fail_json "unknown action: $action (karum-list|karum-buy|karum-cancel|karum-admin)" 2 ;;
  esac
}

# =============================================================================
# Entry point
# =============================================================================
DRY_RUN=0
IDEM=""
OPERATOR=""
LAST_ERR=""
RUN_OUT=""
declare -a VARGS=()
declare -a DRY_SQL=()

main() {
  [[ $# -gt 0 ]] || fail_json "usage: dune-karum-op.sh --op-b64 <b64> [--dry-run] | --op-b64-stdin [--dry-run]" 2
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
