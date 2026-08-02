#!/usr/bin/env bash
# Dune guild-operations WRITE tool — lastsietch-dune-resident.
#
# Deployed to lastsietch-dune:/root/dune-guild-op.sh (mode 0750, owner root). Invoked
# ONLY by the forced-command dispatcher /root/dune-relay-dispatch.sh (guild-op
# action), which is reached over the lastsietch-relay SSH key. Runs as root on lastsietch-dune
# and does its own `sudo kubectl exec` into the Dune DB pod — no nested ssh.
#
# Mirrors scripts/dune-grant.sh: same DB-pod resolution, same psql runner, same
# server-side idempotency gate + durable audit row, same RESULT-row contract,
# same --dry-run (build SQL, never COMMIT). It is intentionally a SEPARATE file
# from dune-grant.sh (guild ops are their own surface with their own audit table
# dune.ls_guild_ops); do not merge them.
#
# Ops:
#   edit_description   P1 — LIVE. dune.edit_guild_description(guild_id, desc).
#                      Admin-gated: re-verifies dune.is_player_guild_admin for
#                      the acting player INSIDE the txn, AFTER the guild lock.
#   accept_invite      P2 — DARK by default. dune.accept_guild_invite(...).
#   reject_invite      P2 — DARK by default. dune.reject_guild_invite(...).
#   send_invite        P3 — DARK by default. dune.add_guild_invite(...).
#
# GUILD_WRITES_DARK (env, default "1" = dark): while dark, the three invite ops
# are code-complete but refuse — they return {"status":"deferred","..."} without
# opening a DB txn. Set GUILD_WRITES_DARK=0 to enable them. edit_description is
# always LIVE regardless of the flag (it is the Phase 1 scope).
#
# HARD CONSTRAINTS (same as dune-grant.sh):
#   * NEVER restart/reboot any game pod, the BGD, or k3s. Only ever opens a psql
#     session into the ALREADY-RUNNING DB pod.
#   * Every op runs inside BEGIN; ... COMMIT; with -v ON_ERROR_STOP=1.
#   * Every field decoded from the base64 JSON is RE-VALIDATED here (the
#     dispatcher's base64 alphabet check is layer 1; this is layer 2).
#   * The acting player's controller is resolved FRESH inside the txn with the
#     tombstone-safe query (excludes 'Deleted' characters).

set -euo pipefail

# --- caps / tunables (owner confirms the live values) ------------------------
# Passed to the Funcom procs. Defaults match the documented guild limits; expose
# them as env so a maintenance window can override without editing the script.
GUILD_MAX_INVITES="${GUILD_MAX_INVITES:-10}"      # max pending invites per guild
GUILD_MAX_MEMBERS="${GUILD_MAX_MEMBERS:-32}"      # max members per guild
GUILD_MAX_GUILDS="${GUILD_MAX_GUILDS:-3}"         # max guilds a player may join
GUILD_NEUTRAL_FACTION="${GUILD_NEUTRAL_FACTION:-3}"  # faction 3 = None/unaligned
GUILD_JOIN_ROLE_ID="${GUILD_JOIN_ROLE_ID:-1}"     # a new joiner enters as Member
GUILD_DESC_MAX_LEN="${GUILD_DESC_MAX_LEN:-2000}"  # description length ceiling

# remove op: the smallint remove-reason passed to remove_guild_members.
# reason 1 confirmed valid via live 2-account test 2026-07-06 (clean removal).
GUILD_REMOVE_REASON="${GUILD_REMOVE_REASON:-1}"

# Feature flag: invite + member-management ops. Went LIVE 2026-07-06 after the
# full 2-account test (promote/demote/remove/invite/accept/reject all verified,
# incl. the send_invite universe-time timespan fix). Gifts stay dark separately
# (LASTSIETCH_GIFTS_ENABLED in dune-gift-op.sh). Set GUILD_WRITES_DARK=1 to re-dark.
GUILD_WRITES_DARK="${GUILD_WRITES_DARK:-0}"

DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

# --- helpers (mirrors dune-grant.sh) -----------------------------------------
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

# --- field extraction + validation -------------------------------------------
OP_JSON=""
jq_get() { printf '%s' "$OP_JSON" | jq -r "$1 // empty"; }

validate_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || fail_json "invalid idempotency_key (must be a UUID): $1" 2
}
validate_posint() {
  [[ "$1" =~ ^[0-9]+$ && "$1" != "0" ]] || fail_json "$2 must be a positive integer: $1" 2
}

# Roles (canonical): 100=Leader, 50=Officer, 1=Member.
# Ops requiring the actor to be a guild admin re-verify is_player_guild_admin
# (Leader-only, per the Funcom proc) INSIDE the txn after the lock.
# Leader-only admin gate (is_player_guild_admin). ONLY edit_description +
# send_invite use it. Member-management ops (promote/demote/remove) are
# officers-capable and use a DIFFERENT hierarchy gate (see op_is_member_op / the
# member-role gate assembled below) — do NOT add them here (contract D1).
op_requires_admin() {
  case "$1" in edit_description|send_invite) return 0 ;; *) return 1 ;; esac
}
# Officers-capable member-management ops: gated by the actor>target role
# hierarchy resolved LIVE inside the txn, not by is_player_guild_admin.
op_is_member_op() {
  case "$1" in promote|demote|remove) return 0 ;; *) return 1 ;; esac
}
op_is_dark_gated() {
  case "$1" in
    accept_invite|reject_invite|send_invite|promote|demote|remove) return 0 ;;
    *) return 1 ;;
  esac
}

do_op() {
  local b64="$1"
  # --- layer 2: decode + parse ------------------------------------------------
  if [[ ! "$b64" =~ ^[A-Za-z0-9+/=]+$ ]]; then
    fail_json "op payload is not valid base64" 2
  fi
  local decoded
  decoded=$(printf '%s' "$b64" | base64 -d 2>/dev/null || true)
  [[ -n "$decoded" ]] || fail_json "op payload failed base64 decode" 2
  printf '%s' "$decoded" | jq -e . >/dev/null 2>&1 || fail_json "op payload is not valid JSON" 2
  OP_JSON="$decoded"

  # --- common fields ----------------------------------------------------------
  # target_account_id: account-scoped target (send_invite only).
  # target_player_controller_id: controller-scoped target (member ops; this is
  #   dune.guild_members.player_id straight from the portal roster — contract D2).
  local op guild_id actor_account_id target_account_id target_ctrl idem operator req_by req_mode
  op=$(jq_get '.op')
  guild_id=$(jq_get '.guild_id')
  actor_account_id=$(jq_get '.actor_account_id')
  target_account_id=$(jq_get '.target_account_id')
  target_ctrl=$(jq_get '.target_player_controller_id')
  idem=$(jq_get '.idempotency_key')
  operator=$(jq_get '.operator'); [[ -n "$operator" ]] || operator="unknown"
  req_by=$(jq_get '.requested_by_discord_id')
  req_mode=$(jq_get '.mode'); [[ -n "$req_mode" ]] || req_mode="apply"

  case "$op" in
    edit_description|accept_invite|reject_invite|send_invite|promote|demote|remove) ;;
    *) fail_json "unknown op: $op" 2 ;;
  esac
  validate_uuid "$idem"
  validate_posint "$actor_account_id" "actor_account_id"
  case "$req_mode" in apply|dry-run) ;; *) fail_json "invalid mode: $req_mode" 2 ;; esac

  local dry=0
  [[ "$DRY_RUN" == "1" || "$req_mode" == "dry-run" ]] && dry=1

  # --- dark gate --------------------------------------------------------------
  # Invite ops refuse while GUILD_WRITES_DARK != 0. Code-complete below, but no
  # DB txn is opened until the flag is flipped.
  if op_is_dark_gated "$op" && [[ "$GUILD_WRITES_DARK" != "0" ]]; then
    printf '{"success":true,"status":"deferred","op":%s,"message":%s}\n' \
      "$(json_str "$op")" \
      "$(json_str "guild invite writes are dark (GUILD_WRITES_DARK=${GUILD_WRITES_DARK}); op not applied")"
    exit 0
  fi

  # --- per-op validation + DO-block body --------------------------------------
  # OP_BODY is the op-specific tail of the DO block (runs after the actor's
  # controller v_ctrl is resolved and the admin gate, if any). Extra -v bindings
  # and SET LOCAL lines are collected per op.
  local OP_BODY="" DETAIL_JSON="{}"
  local -a VARGS=()
  local -a SETLOCAL=()
  local admin_gate=""
  local member_sql=""     # officers-capable hierarchy gate (member ops only)
  op_requires_admin "$op" && admin_gate=1

  case "$op" in
    edit_description)
      validate_posint "$guild_id" "guild_id"
      local desc
      desc=$(jq_get '.detail.description')
      (( ${#desc} <= GUILD_DESC_MAX_LEN )) \
        || fail_json "description too long (max ${GUILD_DESC_MAX_LEN})" 2
      VARGS+=(-v "guild_desc=${desc}")
      SETLOCAL+=("SET LOCAL ls.guild_id = :'guild_id';"
                 "SET LOCAL ls.guild_desc = :'guild_desc';")
      OP_BODY="PERFORM dune.edit_guild_description(current_setting('ls.guild_id')::bigint, current_setting('ls.guild_desc'));"
      DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c '{description: (.detail.description // "")}')
      ;;

    accept_invite|reject_invite)
      local invite_id
      invite_id=$(jq_get '.detail.invite_id')
      validate_posint "$invite_id" "invite_id"
      VARGS+=(-v "invite_id=${invite_id}")
      SETLOCAL+=("SET LOCAL ls.invite_id = :'invite_id';")
      # Self-action: the invite must belong to the acting player.
      local guard="IF NOT EXISTS (SELECT 1 FROM dune.get_player_guild_invites(v_ctrl) WHERE invite_id = current_setting('ls.invite_id')::bigint) THEN RAISE EXCEPTION 'invite % does not belong to actor', current_setting('ls.invite_id'); END IF;"
      if [[ "$op" == "accept_invite" ]]; then
        VARGS+=(-v "join_role=${GUILD_JOIN_ROLE_ID}" -v "max_guilds=${GUILD_MAX_GUILDS}"
                -v "max_members=${GUILD_MAX_MEMBERS}" -v "neutral=${GUILD_NEUTRAL_FACTION}")
        SETLOCAL+=("SET LOCAL ls.join_role = :'join_role';"
                   "SET LOCAL ls.max_guilds = :'max_guilds';"
                   "SET LOCAL ls.max_members = :'max_members';"
                   "SET LOCAL ls.neutral = :'neutral';")
        OP_BODY="${guard} PERFORM dune.accept_guild_invite(current_setting('ls.invite_id')::bigint, current_setting('ls.join_role')::smallint, current_setting('ls.max_guilds')::int, current_setting('ls.max_members')::int, current_setting('ls.neutral')::smallint);"
      else
        OP_BODY="${guard} PERFORM dune.reject_guild_invite(current_setting('ls.invite_id')::bigint);"
      fi
      DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c '{invite_id: (.detail.invite_id)}')
      ;;

    send_invite)
      validate_posint "$guild_id" "guild_id"
      validate_posint "$target_account_id" "target_account_id"
      # invite_sent_timespan is a game universe-time bigint: seconds since
      # dune.farm_variables.universe_time_timestamp (NOT epoch). Computed inline
      # in the OP_BODY below so it is always correct at write time. Verified live
      # 2026-07-06: an epoch-seconds value makes add_guild_invite silently create
      # no row (the game clock rejects a far-future timespan).
      local timespan
      timespan=$(date -u +%s)  # legacy, unused (kept so SET LOCAL below stays valid)
      VARGS+=(-v "timespan=${timespan}" -v "max_invites=${GUILD_MAX_INVITES}")
      SETLOCAL+=("SET LOCAL ls.guild_id = :'guild_id';"
                 "SET LOCAL ls.target_account_id = :'target_account_id';"
                 "SET LOCAL ls.timespan = :'timespan';"
                 "SET LOCAL ls.max_invites = :'max_invites';")
      OP_BODY="SELECT eps.player_controller_id INTO v_target FROM dune.encrypted_player_state eps WHERE eps.account_id = current_setting('ls.target_account_id')::bigint AND eps.character_state IS DISTINCT FROM 'Deleted' ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC LIMIT 1; IF v_target IS NULL THEN RAISE EXCEPTION 'could not resolve target controller for account %', current_setting('ls.target_account_id'); END IF; PERFORM dune.add_guild_invite(v_target, current_setting('ls.guild_id')::bigint, v_ctrl, (SELECT FLOOR(EXTRACT(EPOCH FROM now() - fv.universe_time_timestamp))::bigint FROM dune.farm_variables fv LIMIT 1), current_setting('ls.max_invites')::int);"
      DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c '{target_account_id: (.target_account_id)}')
      ;;

    promote|demote|remove)
      # Member-management ops (contract Tier 1). Target is a CONTROLLER id
      # (dune.guild_members.player_id), taken straight from the portal roster;
      # this differs from send_invite which resolves a controller from an
      # account_id. All authz is the actor>target role hierarchy resolved LIVE
      # inside the txn (contract 2.2); the procs themselves enforce nothing.
      validate_posint "$guild_id" "guild_id"
      validate_posint "$target_ctrl" "target_player_controller_id"
      VARGS+=(-v "target_ctrl=${target_ctrl}")
      SETLOCAL+=("SET LOCAL ls.guild_id = :'guild_id';"
                 "SET LOCAL ls.target_ctrl = :'target_ctrl';")

      # Shared hierarchy gate: resolve both roles from the live membership and
      # enforce contract rules 1-4. v_target is bound to the target controller.
      member_sql="
  v_target := current_setting('ls.target_ctrl')::bigint;
  v_actor_role := (SELECT role_id FROM dune.guild_members
                     WHERE guild_id = current_setting('ls.guild_id')::bigint
                       AND player_id = v_ctrl);
  v_target_role := (SELECT role_id FROM dune.guild_members
                      WHERE guild_id = current_setting('ls.guild_id')::bigint
                        AND player_id = v_target);
  IF v_actor_role IS NULL OR v_actor_role NOT IN (50, 100) THEN
    RAISE EXCEPTION 'actor is not an officer/leader of guild % (role %)',
      current_setting('ls.guild_id'), COALESCE(v_actor_role, -1);
  END IF;
  IF v_target_role IS NULL THEN
    RAISE EXCEPTION 'target controller % is not a member of guild %',
      v_target, current_setting('ls.guild_id');
  END IF;
  IF v_actor_role <= v_target_role THEN
    RAISE EXCEPTION 'actor role % cannot act on equal/higher target role %',
      v_actor_role, v_target_role;
  END IF;
  IF v_ctrl = v_target THEN
    RAISE EXCEPTION 'cannot target self for a member-management op';
  END IF;"

      if [[ "$op" == "promote" ]]; then
        local new_role
        new_role=$(jq_get '.detail.new_role')
        # Clamp (contract 2.3): promote may set 50 (Officer) or 100 (Leader =
        # transfer leadership). 100 is additionally guarded to actor-is-Leader.
        case "$new_role" in
          50|100) ;;
          *) fail_json "promote new_role must be 50 or 100: $new_role" 2 ;;
        esac
        VARGS+=(-v "new_role=${new_role}")
        SETLOCAL+=("SET LOCAL ls.new_role = :'new_role';")
        # Rule 5: promote to/through 100 (transfer) requires the actor be Leader.
        member_sql="${member_sql}
  IF current_setting('ls.new_role')::smallint = 100 AND v_actor_role <> 100 THEN
    RAISE EXCEPTION 'only a Leader may transfer leadership (promote to 100)';
  END IF;"
        OP_BODY="PERFORM dune.promote_guild_member(current_setting('ls.guild_id')::bigint, v_target, current_setting('ls.new_role')::smallint);"
        DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c \
          --argjson tc "$target_ctrl" --argjson nr "$new_role" \
          '{target_player_controller_id: $tc, new_role: $nr}')

      elif [[ "$op" == "demote" ]]; then
        local new_role
        new_role=$(jq_get '.detail.new_role')
        # Clamp (contract 2.3): demote may set 1 (Member) or 50 (Officer). The
        # proc itself RAISEs on 100; this is layer 1.
        case "$new_role" in
          1|50) ;;
          *) fail_json "demote new_role must be 1 or 50: $new_role" 2 ;;
        esac
        VARGS+=(-v "new_role=${new_role}")
        SETLOCAL+=("SET LOCAL ls.new_role = :'new_role';")
        OP_BODY="PERFORM dune.demote_guild_member(current_setting('ls.guild_id')::bigint, v_target, current_setting('ls.new_role')::smallint);"
        DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c \
          --argjson tc "$target_ctrl" --argjson nr "$new_role" \
          '{target_player_controller_id: $tc, new_role: $nr}')

      else  # remove
        # EXTRA gate: refuse (no txn) unless the owner has set GUILD_REMOVE_REASON.
        [[ -n "$GUILD_REMOVE_REASON" ]] \
          || fail_json "remove is disabled until GUILD_REMOVE_REASON is set" 2
        [[ "$GUILD_REMOVE_REASON" =~ ^[0-9]+$ ]] \
          || fail_json "GUILD_REMOVE_REASON must be an integer: $GUILD_REMOVE_REASON" 2
        VARGS+=(-v "remove_reason=${GUILD_REMOVE_REASON}")
        SETLOCAL+=("SET LOCAL ls.remove_reason = :'remove_reason';")
        # remove_guild_members takes the ids FIRST and as an ARRAY (single elem).
        OP_BODY="PERFORM dune.remove_guild_members(ARRAY[v_target]::bigint[], current_setting('ls.guild_id')::bigint, current_setting('ls.remove_reason')::smallint);"
        DETAIL_JSON=$(printf '%s' "$OP_JSON" | jq -c \
          --argjson tc "$target_ctrl" --arg rr "$GUILD_REMOVE_REASON" \
          '{target_player_controller_id: $tc, remove_reason: ($rr|tonumber)}')
      fi
      ;;
  esac

  # target_account_id / guild_id SQL bindings (NULL when absent — accept/reject
  # carry neither; the proc derives the guild from the invite). Bound ONCE here.
  local target_sql="NULL"
  if [[ -n "$target_account_id" && "$target_account_id" =~ ^[0-9]+$ ]]; then
    target_sql=":target_account_id"
    VARGS+=(-v "target_account_id=${target_account_id}")
  fi
  local guild_sql="NULL"
  if [[ -n "$guild_id" && "$guild_id" =~ ^[0-9]+$ ]]; then
    guild_sql=":guild_id"
    VARGS+=(-v "guild_id=${guild_id}")
  fi

  resolve_db_pod

  # --- admin gate SQL (inside the DO block, after v_ctrl resolves) ------------
  local admin_sql=""
  if [[ -n "$admin_gate" ]]; then
    admin_sql="IF NOT dune.is_player_guild_admin(v_ctrl, current_setting('ls.guild_id')::bigint) THEN RAISE EXCEPTION 'actor is not a guild admin (guild %, controller %)', current_setting('ls.guild_id'), v_ctrl; END IF;"
  fi

  # --- assemble the transaction ----------------------------------------------
  # One server-side txn: guild lock -> idempotency gate (temp table) -> resolve
  # controller FRESH -> admin re-verify -> proc call. Everything after the gate
  # is skipped on a replay (is_new = false). RESULT row is emitted before COMMIT.
  local setlocal_joined=""
  local line
  for line in "${SETLOCAL[@]}"; do setlocal_joined+="${line}"$'\n'; done

  local TXN
  TXN=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO dune, public;

-- Serialize against concurrent guild mutations (Funcom's own advisory lock).
SELECT dune.guilds_get_exclusive_operation_lock();

-- Idempotency gate + durable audit row, same txn as the mutation. A second call
-- with the same idempotency_key inserts nothing (ON CONFLICT DO NOTHING) and the
-- op body below is skipped (is_new = false) -> a no-op replay.
CREATE TEMP TABLE _op_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_guild_ops
    (idempotency_key, op, guild_id, actor_account_id, target_account_id,
     detail, operator, requested_by_discord_id, status, applied_at)
  VALUES
    (:'idem'::uuid, :'op', ${guild_sql}, :actor_account_id, ${target_sql},
     :'detail'::jsonb, :'operator', :'req_by', 'applied', now())
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_guild_ops WHERE idempotency_key = :'idem'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS op_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new;

-- Values for the DO block (psql does NOT substitute :vars inside a \$\$ body).
SET LOCAL ls.actor_account_id = :'actor_account_id';
${setlocal_joined}
DO \$\$
DECLARE
  v_ctrl        bigint;
  v_target      bigint;
  v_actor_role  smallint;
  v_target_role smallint;
  v_is_new      boolean;
BEGIN
  SELECT is_new FROM _op_gate INTO v_is_new;
  IF NOT v_is_new THEN
    RETURN;  -- idempotent replay: no mutation
  END IF;

  -- Resolve the acting player's controller FRESH, tombstone-safe.
  SELECT eps.player_controller_id INTO v_ctrl
  FROM dune.encrypted_player_state eps
  WHERE eps.account_id = current_setting('ls.actor_account_id')::bigint
    AND eps.character_state IS DISTINCT FROM 'Deleted'
  ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
  LIMIT 1;
  IF v_ctrl IS NULL THEN
    RAISE EXCEPTION 'could not resolve controller for account %', current_setting('ls.actor_account_id');
  END IF;

  ${admin_sql}
  ${member_sql}
  ${OP_BODY}
END
\$\$;

-- Single uniquely-prefixed outcome row; the bash layer keys ONLY on ^RESULT\|.
SELECT 'RESULT|' || op_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _op_gate;

COMMIT;
EOF
)

  local -a vargs=(
    -v "idem=${idem}"
    -v "op=${op}"
    -v "actor_account_id=${actor_account_id}"
    -v "operator=${operator}"
    -v "req_by=${req_by}"
    -v "detail=${DETAIL_JSON}"
  )
  vargs+=("${VARGS[@]}")

  if [[ "$dry" == "1" ]]; then
    printf '{"success":true,"status":"dry-run","op":%s,' "$(json_str "$op")"
    printf '"message":"dry-run: SQL built, NOT executed","sql":%s}\n' "$(json_str "$TXN")"
    exit 0
  fi

  local result
  if ! result=$(printf '%s\n' "$TXN" | run_psql -tA "${vargs[@]}" 2>&1); then
    fail_json "guild op transaction failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)" 6
  fi

  local row op_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  op_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$op_id" ]] || op_id="null"

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","audit_id":%s,"op":%s,"message":"already applied, idempotent replay, no change made"}\n' \
      "$op_id" "$(json_str "$op")"
    exit 0
  fi
  printf '{"success":true,"status":"applied","audit_id":%s,"op":%s,"message":"guild op applied"}\n' \
    "$op_id" "$(json_str "$op")"
  exit 0
}

# =============================================================================
# Entry point
# =============================================================================
DRY_RUN=0

main() {
  [[ $# -gt 0 ]] || fail_json "usage: dune-guild-op.sh --op-b64 <b64> [--dry-run] | --op-b64-stdin [--dry-run]" 2
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
