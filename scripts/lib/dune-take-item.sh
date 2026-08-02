#!/usr/bin/env bash
# shellcheck shell=bash
#
# THE SHARED GATED TAKE. Karum build contract section 3.1, owner decision D3.
#
# This is the ONE implementation in the codebase of "remove an item row from a player's
# CHOAM bank". Sourced, never executed; it emits SQL on stdout and executes nothing, so
# the caller can commit the take and its OWN ledger insert in a single transaction.
#
# WHY IT EXISTS
# -------------
# Live-tested 2026-07-26 on build 24376904 (ops/bank-online-delete-test/run.sh):
# GIVING to an online player is safe, TAKING from one is not. The client holds the whole
# item record including its primary key, and the move handler writes it to a destination
# without re-checking the row still exists, so a removal from a loaded session is
# resurrected under its ORIGINAL id and survives a full reload. That is a duplication
# path. RMQ cannot rescue the take side (its only removal verb is CleanPlayerInventory,
# which wipes everything), so a take is unavoidably a direct DB write and every direct DB
# write against a loaded session must be offline-gated.
#
# The Karum has exactly ONE take, on the seller at listing time; every other leg is a
# give. Design law: a change that introduces a second take does not ship. Rather than
# duplicate the gate into the Karum writer, both callers share this one:
#
#   scripts/dune-item-transfer-op.sh   dst=bank:<recipient_account_id>   (Tier 5, DARK)
#   scripts/dune-karum-op.sh           dst=exchange                      (Phase 1)
#
# The ONLY difference between the callers is the destination. That is a parameter, not a
# fork. A subprocess call would put the take in a different transaction from the caller's
# ledger insert, leaving a moved item with nothing recording who holds it, which is
# exactly the un-marked escrow row the contract's section 4 exists to prevent.
#
# USAGE
# -----
#   . "$(dirname "$0")/lib/dune-take-item.sh"        # /root/lib/... on the game host
#   TAKE_SQL=$(karum_take_item \
#                item_id=123 owner_account_id=456 dst=bank:789 \
#                expected_template=T6BladePart correlation_id=<uuid>) \
#     || fail_json "could not build the take"
#
# ALWAYS check the return code. A validation failure emits NOTHING on stdout and returns
# 2, and `local x=$(...)` swallows it even under `set -e`, so use a plain assignment with
# an explicit `||`.
#
# Named arguments, deliberately not positional: two of them are inventory specs and two
# are Phase 1 optionals, and a silently mis-ordered take is precisely the class of bug
# this extraction exists to prevent. (The contract sketched a 6-arg positional signature;
# this supersedes it. The source inventory is not an argument at all, because it is always
# the owner's own bank, resolved in-transaction, and the transfer's destination is only
# knowable at SQL time so it cannot be a shell literal.)
#
#   item_id=<int>              REQUIRED  the row to take
#   owner_account_id=<int>     REQUIRED  whose bank it is taken from, and who is gated
#   dst=<spec>                 REQUIRED  <int> | bank:<account_id> | exchange
#   correlation_id=<uuid>      REQUIRED  the caller's idempotency key, echoed into _take_result
#   expected_template=<tpl>    optional  identity guard; omitted = no guard
#   min_position=<int>         optional  floor for the destination slot (default 0)
#   stats_patch=<json object>  optional  merged into dune.items.stats (the Karum sentinel)
#
# WHAT THE EMITTED SQL DOES, in one transaction, in this order:
#   1. offline gate on the owner's account, under a row lock, fail-closed
#   2. resolve the SOURCE bank fresh and tombstone-safe (never trust a cached inv id)
#   3. resolve the DESTINATION per `dst`
#   4. SELECT ... FOR UPDATE the item row, pinned to the source bank
#   5. template identity guard
#   6. single-row re-home pinned with WHERE id = <item> AND inventory_id = <src>
#   7. publish the facts into the temp table _take_result for the caller's ledger
#
# CONTRACT WITH THE CALLER
# ------------------------
#   * The caller owns BEGIN / COMMIT. This emits neither.
#   * `SET LOCAL search_path TO dune, public;` must already be in effect (every object
#     here is schema-qualified anyway, but the callers' other SQL is not).
#   * REPLAY: if the caller sets `ls.take_skip` to '1' (transaction-local, via
#     `set_config('ls.take_skip','1',true)`), the take block returns without acting. An
#     idempotent replay must set it, or the take runs a second time against a source that
#     no longer holds the row and fails the exactly-one-row assertion.
#   * `_take_result` is created here as ON COMMIT DROP and holds exactly one row after a
#     take that ran. Read it BEFORE COMMIT. It is empty on a skipped replay.
#   * Capacity, rate caps, price ceilings and every other policy check stay with the
#     caller. This owns the gate and the move, nothing else.
#
# ERROR TOKENS raised (callers map these to friendly text; they are matched as substrings
# of the psql error, so keep them lowercase and unique):
#   player_online   the owner is online, in grace, or their status is undetermined
#   no_bank         no CHOAM bank inventory resolves for that account
#   item_not_found  the row is not in that bank, or the template guard failed
#   take_failed     the destination is unusable, or the re-home did not move exactly 1 row

# --- validation helpers (prefixed so a sourcing caller cannot collide) --------

_take_die() { printf 'karum_take_item: %s\n' "$1" >&2; }

_take_posint() {
  if [[ "$1" =~ ^[1-9][0-9]*$ ]]; then return 0; fi
  _take_die "$2 must be a positive integer: '$1'"
  return 2
}

_take_nonneg() {
  if [[ "$1" =~ ^(0|[1-9][0-9]*)$ ]]; then return 0; fi
  _take_die "$2 must be a non-negative integer: '$1'"
  return 2
}

# Resolve an account's CHOAM bank (inventory_type 30) fresh and tombstone-safe. Emitted
# for the source and, when dst=bank:<acct>, for the destination too, so the two can never
# disagree about what "that player's bank" means.
#
# Pawn-keyed, NOT controller-keyed: inv.actor_id points at player_pawn_id. An account can
# hold MORE THAN ONE encrypted_player_state row (create / delete / recreate each leave
# one), and a character deleted during creation can stay stuck online_status='Online'
# forever, so Deleted rows are excluded here and everywhere else this file reads eps.
# See.
_take_bank_sql() {
  local acct="$1" pawn_var="$2" inv_var="$3"
  cat <<EOF
  SELECT eps.player_pawn_id, inv.id INTO ${pawn_var}, ${inv_var}
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 30
   WHERE eps.account_id = ${acct}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1;
  IF ${inv_var} IS NULL THEN
    RAISE EXCEPTION 'no_bank (account % has no CHOAM bank inventory; never opened one?)', ${acct};
  END IF;
EOF
}

# --- the take -----------------------------------------------------------------

karum_take_item() {
  local item_id="" owner_account_id="" dst="" correlation_id="" \
        expected_template="" min_position="0" stats_patch=""
  local kv key val

  for kv in "$@"; do
    key="${kv%%=*}"
    val="${kv#*=}"
    case "$key" in
      item_id)            item_id="$val" ;;
      owner_account_id)   owner_account_id="$val" ;;
      dst)                dst="$val" ;;
      correlation_id)     correlation_id="$val" ;;
      expected_template)  expected_template="$val" ;;
      min_position)       min_position="$val" ;;
      stats_patch)        stats_patch="$val" ;;
      *) _take_die "unknown argument '$kv' (item_id= owner_account_id= dst= correlation_id= [expected_template=] [min_position=] [stats_patch=])"
         return 2 ;;
    esac
  done

  _take_posint "$item_id" item_id || return 2
  _take_posint "$owner_account_id" owner_account_id || return 2
  _take_nonneg "$min_position" min_position || return 2
  if [[ ! "$correlation_id" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    _take_die "correlation_id must be a UUID: '$correlation_id'"
    return 2
  fi

  # Charset matches the relay's own guard on expected_template (letters, digits, _ and -)
  # so a payload the relay accepted cannot be rejected here for its spelling.
  local tpl_lit="NULL"
  if [[ -n "$expected_template" ]]; then
    if [[ ! "$expected_template" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
      _take_die "expected_template must be 1-64 chars of letters, digits, _ or -: '$expected_template'"
      return 2
    fi
    tpl_lit="'${expected_template}'"
  fi

  # stats_patch is folded into a SQL literal, so it may not carry a quote or a backslash,
  # and it must be a JSON OBJECT (a scalar or array would make the `||` merge nonsense).
  local stats_clause=""
  if [[ -n "$stats_patch" ]]; then
    if [[ "$stats_patch" == *"'"* || "$stats_patch" == *\\* ]]; then
      _take_die "stats_patch must not contain a single quote or a backslash"
      return 2
    fi
    if ! printf '%s' "$stats_patch" \
         | python3 -c 'import json,sys; sys.exit(0 if isinstance(json.load(sys.stdin), dict) else 1)' \
           >/dev/null 2>&1; then
      _take_die "stats_patch must be a JSON object: '$stats_patch'"
      return 2
    fi
    stats_clause=",
         stats = COALESCE(stats, '{}'::jsonb) || '${stats_patch}'::jsonb"
  fi

  # Destination. An integer is folded as a literal; the two named modes resolve in SQL
  # because neither value is knowable when this shell function runs.
  local dst_sql=""
  case "$dst" in
    exchange)
      dst_sql="  SELECT dune.get_exchange_inventory_id(2) INTO v_dst_inv;
  IF v_dst_inv IS NULL THEN
    RAISE EXCEPTION 'take_failed (could not resolve the exchange inventory)';
  END IF;"
      ;;
    bank:*)
      local dst_acct="${dst#bank:}"
      _take_posint "$dst_acct" "dst=bank:<account_id>" || return 2
      dst_sql=$(_take_bank_sql "$dst_acct" v_dst_pawn v_dst_inv)
      ;;
    "")
      _take_die "dst is required (<inventory_id> | bank:<account_id> | exchange)"
      return 2 ;;
    *)
      _take_posint "$dst" dst || return 2
      dst_sql="  v_dst_inv := ${dst};"
      ;;
  esac

  cat <<EOF
-- ==== shared gated take (scripts/lib/dune-take-item.sh) ======================
-- item ${item_id} out of account ${owner_account_id}'s CHOAM bank into ${dst}.
-- The offline gate below is the ONLY thing standing between us and the 2026-07-26
-- resurrection bug. Do not relax it, do not make it advisory, do not move it to a
-- caller. If it fails the whole transaction rolls back and nothing moved.
DROP TABLE IF EXISTS _take_result;
CREATE TEMP TABLE _take_result(
  correlation_id uuid,
  item_id        bigint,
  src_inv        bigint,
  dst_inv        bigint,
  src_pawn_id    bigint,
  dst_pawn_id    bigint,
  position_index bigint,
  template_id    text,
  stack_size     bigint,
  quality_level  bigint
) ON COMMIT DROP;

DO \$take\$
DECLARE
  v_acct     bigint := ${owner_account_id};
  v_item     bigint := ${item_id};
  v_min_pos  bigint := ${min_position};
  v_want_tpl text   := ${tpl_lit};
  v_chars    int;
  v_offline  int;
  v_src_pawn bigint;
  v_dst_pawn bigint;
  v_src_inv  bigint;
  v_dst_inv  bigint;
  v_tpl      text;
  v_stack    bigint;
  v_quality  bigint;
  v_next_pos bigint;
  v_moved    int;
BEGIN
  -- 0. REPLAY: the caller has already decided this op ran before. Do nothing.
  IF COALESCE(current_setting('ls.take_skip', true), '') = '1' THEN
    RETURN;
  END IF;

  -- 1. OFFLINE GATE, under a row lock, fail-closed.
  --
  -- Locked FOR SHARE first so a login cannot flip online_status between the check and
  -- the re-home. It blocks the engine's own login write to these rows for the length of
  -- this transaction, which is a single fast statement sequence; a deadlock against the
  -- engine rolls US back, which is the safe direction.
  --
  -- Scoped to the ACCOUNT, not to the character owning the bank. That is deliberately
  -- stricter than the mechanism requires (only the session holding that inventory can
  -- resurrect a row) and it matches what the portal already tells the player, since
  -- _resolve_online and the storage UI offlineOk flag are both account-level. As of
  -- 2026-07-04 no account has more than one non-Deleted eps row, so in practice this
  -- reads exactly one.
  PERFORM 1 FROM dune.encrypted_player_state
   WHERE account_id = v_acct
     AND character_state IS DISTINCT FROM 'Deleted'
     FOR SHARE;

  SELECT count(*),
         count(*) FILTER (
           WHERE online_status = 'Offline'
             AND NOT COALESCE(reconnect_grace_period_end > now(), false))
    INTO v_chars, v_offline
    FROM dune.encrypted_player_state
   WHERE account_id = v_acct
     AND character_state IS DISTINCT FROM 'Deleted';

  -- No rows, a NULL status, or a NULL character_state all land here: undetermined is
  -- LOCKED, never allowed.
  IF v_chars = 0 THEN
    RAISE EXCEPTION 'player_online (account % has no live character row, status undetermined)', v_acct;
  END IF;
  IF v_offline <> v_chars THEN
    RAISE EXCEPTION 'player_online (% of % characters on account % are online or in reconnect grace)',
                    v_chars - v_offline, v_chars, v_acct;
  END IF;

  -- 2. SOURCE: the owner's own CHOAM bank, resolved fresh inside the txn.
$(_take_bank_sql v_acct v_src_pawn v_src_inv)

  -- 3. DESTINATION.
${dst_sql}
  IF v_dst_inv = v_src_inv THEN
    RAISE EXCEPTION 'take_failed (source and destination resolve to the same inventory %)', v_src_inv;
  END IF;

  -- 4. LOCK the item row, pinned to the source bank. This is the ownership AND existence
  -- gate in one: a row that is not in this bank yields nothing and we fail closed.
  SELECT it.template_id, it.stack_size, it.quality_level
    INTO v_tpl, v_stack, v_quality
    FROM dune.items it
   WHERE it.id = v_item
     AND it.inventory_id = v_src_inv
     FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'item_not_found (item % is not in bank inventory % of account %)',
                    v_item, v_src_inv, v_acct;
  END IF;

  -- 5. IDENTITY GUARD: a swapped or edited client payload fails closed.
  IF v_want_tpl IS NOT NULL AND v_tpl IS DISTINCT FROM v_want_tpl THEN
    RAISE EXCEPTION 'item_not_found (template mismatch: row is %, caller expected %)',
                    v_tpl, v_want_tpl;
  END IF;

  -- 6. Destination slot. min_position lets a caller allocate above another writer's
  -- range: the market bot caches MAX(position_index) once at init and increments
  -- locally, so anything sharing its inventory must stay out of its way (contract LT-7).
  SELECT GREATEST(COALESCE(MAX(position_index), -1) + 1, v_min_pos) INTO v_next_pos
    FROM dune.items WHERE inventory_id = v_dst_inv;

  -- 7. THE MOVE: single-row re-home, pinned to the source. Exactly one row can match, the
  -- row is moved and never copied, and a second run finds nothing in the source.
  UPDATE dune.items
     SET inventory_id   = v_dst_inv,
         position_index = v_next_pos,
         is_new         = true${stats_clause}
   WHERE id = v_item
     AND inventory_id = v_src_inv;
  GET DIAGNOSTICS v_moved = ROW_COUNT;
  IF v_moved <> 1 THEN
    RAISE EXCEPTION 'take_failed (expected to move exactly 1 item row, moved %)', v_moved;
  END IF;

  -- 8. Publish the facts. The caller reads these for its own ledger, in this txn.
  INSERT INTO _take_result VALUES (
    '${correlation_id}'::uuid, v_item, v_src_inv, v_dst_inv, v_src_pawn, v_dst_pawn,
    v_next_pos, v_tpl, v_stack, v_quality);
END
\$take\$;
-- ==== end shared gated take =================================================
EOF
}
