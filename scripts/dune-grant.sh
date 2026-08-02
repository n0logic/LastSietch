#!/usr/bin/env bash
# Dune progression-grant tool — lastsietch-dune-resident core grant script.
#
# Deployed to lastsietch-dune:/root/dune-grant.sh (mode 0750, owner root). Invoked
# ONLY by the forced-command dispatcher /root/dune-relay-dispatch.sh, which
# itself is reached over the lastsietch-relay SSH key. This script runs as root on
# lastsietch-dune and does its own `sudo kubectl exec` into the Dune DB pod — there is
# NO nested ssh (the relay forced-command already lands here).
#
# Cross-reference: scripts/grant-welcome-pack.sh is the LEGACY workstation
# welcome-pack script (still used by the welcome-pack watcher). This file is
# the NEW parameterized tool. They are intentionally separate (plan CR-5 / R2):
# do not merge them. SQL patterns here mirror grant-welcome-pack.sh where they
# overlap (item INSERT at COALESCE(MAX(position_index),-1)+1, currency UPSERT,
# Intel jsonb_set).
#
# Negative results (adainrivers, 2026-05-26):
#   - No `AwardXP Category` field is accepted by the live server.
#   - No `AwardXPByEventTag` ServerCommand exists.
#   - Journey* ServerCommands have been broken since the 2026-05-26 patch.
# We use SQL proc dune.complete_journey_story_nodes_for_player directly
# (G12/G23/G33) and never reach for the broken Journey ServerCommand path.
#
# Modes:
#   --list-players        emit every character (incl. OFFLINE) as JSON
#   --grant-b64 <base64>  decode + validate + apply one grant
#   --dry-run             with --grant-b64: build SQL, print it, do NOT COMMIT
#
# HARD CONSTRAINTS:
#   * NEVER restart/reboot any game pod, the BGD, or k3s. This script only ever
#     opens a psql session into the ALREADY-RUNNING DB pod.
#   * Every grant runs inside BEGIN; ... COMMIT; with -v ON_ERROR_STOP=1.
#   * RAM-fragile grants (item/solari/intel/recipe/schematic_item) are
#     offline-gated: applied immediately only if the target is Offline.
#   * Every field decoded from the base64 JSON is RE-VALIDATED here (defence in
#     depth — the dispatcher's base64 regex is layer 1, this is layer 2).
#
# See docs/DUNE-PROGRESSION-GRANT-TOOL-PLAN.md sections 3, 5, 6, 8.

set -euo pipefail

# --- DB pod resolution (risk R3: never hardcode; resolve or fail loudly) -----
# The namespace + pod names are NOT hardcoded. They are resolved dynamically by
# label so a Funcom redeploy that changes the pod hash does not silently write
# to the wrong pod (or a stale one).

DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

# Sidecar JSON next to this script (deployed to /root/tags-data.json on lastsietch-dune
# alongside /root/dune-grant.sh). Pinned to icehunter dune-admin commit 9ef5a6c.
# Drives G23 (journey_node_tags), G24a/b (job_skill_blocks), G25a (job_all_modules).
TAGS_DATA_JSON="${TAGS_DATA_JSON:-$(dirname "$(readlink -f "$0")")/tags-data.json}"

# Fail-fast sidecar validator. Runs once at script-start to assert the required
# top-level keys exist; caps blast radius if a malformed tags-data.json is ever
# deployed. Per spec §Cross-cutting / Schema-validation step.
validate_tags_data() {
  [[ -f "$TAGS_DATA_JSON" ]] \
    || fail_json "tags-data.json missing at ${TAGS_DATA_JSON}" 3
  if ! jq -e '
    (type=="object")
    and has("journey_node_tags") and has("contract_tags")
    and has("contract_aliases") and has("contract_skill_grants")
    and has("job_skill_blocks") and has("job_all_modules")
    and (.job_skill_blocks | type=="object")
    and (.job_all_modules  | type=="object")
  ' "$TAGS_DATA_JSON" >/dev/null 2>&1; then
    fail_json "tags-data.json missing one or more required keys (journey_node_tags, contract_tags, contract_aliases, contract_skill_grants, job_skill_blocks, job_all_modules)" 3
  fi
}

# Emit the cross-cutting SET LOCAL search_path line that every Funcom-proc-
# calling builder MUST prepend to its GRANT_BODY (per spec §Cross-cutting /
# SET LOCAL search_path requirement + our internal notes).
# Funcom procs use UNQUALIFIED table refs; without this prelude the proc
# fails with "relation does not exist" because psql's default search_path is
# "$user", public.
funcom_proc_call_prelude() {
  printf 'SET LOCAL search_path TO dune, public;\n'
}

# The DB pod is a StatefulSet member; its name ends in -db-dbdepl-sts-0. The
# namespace is the single funcom-seabass-* namespace. We resolve both.
resolve_db_pod() {
  local ns pod
  ns=$(sudo kubectl get ns -o name 2>/dev/null \
         | sed 's#^namespace/##' \
         | grep -E '^funcom-seabass-' | head -n1 || true)
  if [[ -z "$ns" ]]; then
    echo "FATAL: could not resolve the Dune namespace (no funcom-seabass-* ns)" >&2
    exit 3
  fi
  # The DB pod name ends with -db-dbdepl-sts-0. Match on that suffix.
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

# psql runner — opens a psql session in the resolved DB pod. Extra args (e.g.
# -t -A -v key=val -f -) are passed through. stdin is forwarded so SQL can be
# piped in. This is the ONLY place a DB connection is opened.
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

# One-shot scalar SELECT helper (read-only). Returns trimmed stdout.
# Extra args after the SQL are forwarded to psql (e.g. -v key=val) so values are
# bound as psql variables — never string-concatenated into the SQL (plan §8).
psql_scalar() {
  local sql="$1"; shift
  printf '%s\n' "$sql" | run_psql -tA "$@" 2>/dev/null | tr -d '[:space:]'
}

# Canonical account -> character resolution. @COL@ is substituted with the column
# to select. dune.encrypted_player_state is keyed per character SLOT, so an
# account with an empty second slot returns 2 rows; every consumer here wants the
# ONE real character. The player_state join drops empty slots (they have no
# player_state row) and LIMIT 1 guarantees a scalar. Accounts with NO real
# character resolve to empty, which the caller turns into a loud failure rather
# than a silent write to a slot nobody plays.
readonly RESOLVE_TARGET_SQL="SELECT @COL@
  FROM dune.encrypted_player_state eps
  JOIN dune.player_state ps ON ps.player_controller_id = eps.player_controller_id
 WHERE eps.account_id = :account_id
 ORDER BY ps.last_login_time DESC NULLS LAST, ps.id DESC
 LIMIT 1;"

# Emit a JSON error object and exit non-zero. The relay surfaces stdout
# faithfully; emitting JSON keeps the admin-backend contract uniform.
fail_json() {
  local msg="$1"
  local code="${2:-1}"
  printf '{"success":false,"status":"failed","message":%s}\n' \
    "$(json_str "$msg")"
  exit "$code"
}

# JSON-quote an arbitrary string for safe embedding in our emitted JSON.
json_str() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

# =============================================================================
# MODE: --list-players
# =============================================================================
# Every character row, incl. OFFLINE, for the grant-tool player picker. PII
# (character names) — the relay route + admin-backend are auth-gated.
#
# Row set: dune.encrypted_player_state — the SAME table do_grant resolves
# accounts from, so the picker can never show an account the grant path cannot
# resolve. It carries account_id / online_status / last_login_time /
# reconnect_grace_period_end for EVERY character (online AND offline).
# Deleted-character tombstones are excluded (character_state='Deleted'):
# they linger with online_status='Online' and would otherwise show as a
# duplicate ONLINE card sharing the live character's decrypted name.
#
# Character names: encrypted_player_state stores them encrypted. dune.player_state
# is the decrypting VIEW but only exposes ONLINE rows. We LEFT JOIN it on
# account_id so online characters get a decrypted name and offline characters
# fall back to "Account <id>". The row set stays every account either way.
list_players() {
  resolve_db_pod
  # funcom_id (displayName#tag) is sourced from dune.accounts via acc.id =
  # eps.account_id. The admin-backend moderation router consumes it for ban/
  # unban fls_id resolution; LEFT JOIN keeps offline / never-bound rows.
  # build_ban_grant / build_unban_grant ALSO look up funcom_id from
  # dune.accounts as defence in depth so the wire payload's fls_id can be null
  # without breaking the writer.
  local sql
  sql=$(cat <<'EOF'
SELECT coalesce(json_agg(json_build_object(
         'account_id',      eps.account_id,
         'name',            COALESCE(ps.character_name,
                                     'Account ' || eps.account_id::text),
         'funcom_id',       acc.funcom_id,
         'online_status',   eps.online_status,
         'last_login_time', eps.last_login_time,
         'in_grace_period', (eps.reconnect_grace_period_end IS NOT NULL
                              AND eps.reconnect_grace_period_end > NOW())
       ) ORDER BY COALESCE(ps.character_name,
                           'Account ' || eps.account_id::text)), '[]'::json)
FROM dune.encrypted_player_state eps
LEFT JOIN dune.player_state ps  ON ps.account_id  = eps.account_id
LEFT JOIN dune.accounts     acc ON acc.id         = eps.account_id
WHERE eps.character_state::text <> 'Deleted';
EOF
)
  local out
  out=$(printf '%s\n' "$sql" | run_psql -tA 2>/dev/null || true)
  out=$(printf '%s' "$out" | tr -d '[:space:]')
  if [[ -z "$out" ]]; then
    printf '{"available":false,"error":"player query returned no output"}\n'
    exit 1
  fi

  # Query the current term's distinct active Landsraad houses.
  local houses_sql
  houses_sql=$(cat <<'EOF'
SELECT COALESCE(json_agg(h.house_name ORDER BY h.house_name), '[]'::json)
FROM (
  SELECT DISTINCT house_name
  FROM dune.landsraad_tasks
  WHERE term_id = (SELECT term_id FROM dune.landsraad_decree_term ORDER BY term_id DESC LIMIT 1)
) h;
EOF
)
  local houses_out
  houses_out=$(printf '%s\n' "$houses_sql" | run_psql -tA 2>/dev/null || true)
  houses_out=$(printf '%s' "$houses_out" | tr -d '[:space:]')
  [[ -n "$houses_out" ]] || houses_out="[]"

  printf '{"available":true,"players":%s,"active_houses":%s}\n' "$out" "$houses_out"
}

# =============================================================================
# MODE: --list-recent <N>
# =============================================================================
# Last N rows of dune.ls_progression_grants joined to the player-name view
# for human-readable display in the admin panel's "Recent grants" widget.
# Defaults to 20; caller-supplied N is clamped to 1..200. Read-only SELECT.
list_recent() {
  local limit="${1:-20}"
  # Clamp limit to 1..200; reject non-integers.
  if [[ ! "$limit" =~ ^[0-9]+$ ]]; then
    printf '{"available":false,"error":"invalid limit (must be a positive integer)"}\n'
    exit 2
  fi
  (( limit < 1 )) && limit=1
  (( limit > 200 )) && limit=200

  resolve_db_pod
  local sql
  sql=$(cat <<EOF
SELECT coalesce(json_agg(row_data ORDER BY id DESC), '[]'::json) FROM (
  SELECT
    g.id,
    g.granted_at,
    g.account_id,
    g.grant_type,
    g.detail,
    g.operator,
    g.status,
    COALESCE(ps.character_name, 'Account ' || g.account_id::text) AS player_name,
    json_build_object(
      'id',          g.id,
      'granted_at',  g.granted_at,
      'account_id',  g.account_id,
      'grant_type',  g.grant_type,
      'detail',      g.detail,
      'operator',    g.operator,
      'status',      g.status,
      'player_name', COALESCE(ps.character_name, 'Account ' || g.account_id::text)
    ) AS row_data
    FROM dune.ls_progression_grants g
    LEFT JOIN dune.player_state ps ON ps.account_id = g.account_id
    ORDER BY g.id DESC
    LIMIT ${limit}
) sub;
EOF
)
  local out
  out=$(printf '%s\n' "$sql" | run_psql -tA 2>/dev/null || true)
  out=$(printf '%s' "$out" | tr -d '\n')
  [[ -n "$out" ]] || out="[]"

  printf '{"available":true,"grants":%s}\n' "$out"
}

# =============================================================================
# MODE: --grant-b64 <base64>
# =============================================================================

# Hardcoded grant_type enum — the airtight allowlist (plan section 8).
is_valid_grant_type() {
  case "$1" in
    item|item_live|solari|solari_currency|house_scrip|intel|recipe|schematic_item|faction_rep|spec_xp|keystone|spec_unlock_track|spec_unlock_all|char_xp|progression_preset|teleport|reset_specs|reset_tutorials|wipe_codex|repair_all|import_blueprint|bb_handoff|bb_clone|import_solido_to_basebackup)
      return 0 ;;
    # P3a — icehunter v0.5.x parity (G23, G24a, G24b, G25a, G25b, g7b).
    main_quest_unlock|grant_full_job_tree|grant_skill_block|reset_full_skill_area|set_starter_class|align_faction)
      return 0 ;;
    # WP-C 2026-06-10 — journey/story full-unlock tag repair (online-safe,
    # tabular update_player_tags; same proc class as align_faction).
    journey_full_unlock)
      return 0 ;;
    # WP-C2 2026-06-11 — journey NODE completion pass (offline-gated Funcom
    # proc; pairs with journey_full_unlock to make the journey UI reflect it).
    journey_node_completion)
      return 0 ;;
    # 2026-06-12 — Trials-of-Aql / 4th-trial spice-addiction state-machine
    # repair (offline-gated FGL jsonb write; advances FSpiceAddictionComponent
    # to FullyEnabled so the 3rd ability slot unlocks). Journey and tag grants
    # do not fire this side effect, which is why it is a separate grant type.
    spice_addiction_enable)
      return 0 ;;
    # Phase C 2026-05-29: moderation trio (kick is direct via dune-kick.py;
    # ban + unban land here because they need durable lsadmin.bans rows + an
    # idempotency-keyed audit trail, same as any progression grant).
    ban|unban)
      return 0 ;;
    # 2026-06-26: multi-item batch grants. bank_items_batch (G29) lands in the
    # player CHOAM bank (type 30); container_items_batch lands in a chosen
    # vehicle/storage container inventory (type 0 on a BP_ContainerVehicle /
    # storage actor), resolved + validated as belonging to the target player.
    bank_items_batch|container_items_batch)
      return 0 ;;
    *) return 1 ;;
  esac
}

# Server-side caps (risk: additive double-submit / fat-finger). The admin-backend
# also caps, but this is the last line of defence before the DB.
CAP_ITEM_QTY=10000
CAP_ITEM_LIVE_QTY=9999
CAP_SOLARI=100000000
CAP_HOUSE_SCRIP=100000000
CAP_INTEL=1000000
CAP_FACTION_REP=12475  # Last Sietch-corrected 2026-07-23: live rank-20 cap is 12475 (was icehunter 12474; 12474 now displays rank 19)
CAP_SPEC_XP=44182    # icehunter parity Action #2: maxXP per track from db.go:541
CAP_SPEC_LEVEL=100   # icehunter cmdMaxSpec uses 100.0 as max level
# G11 char_xp: 344,440 = cumulative XP for character level 200 (in-game cap).
CAP_CHAR_XP=344440
# G13 solari_currency (Funcom-proc routed) — same numeric ceiling as G2 since
# both write the Solaris currency surface; difference is the storage path
# (proc + balance table vs. backpack-item stack).
CAP_SOLARI_CURRENCY=100000000
# G20 import_blueprint / G22 import_solido_to_basebackup caps. piece total =
# instances + placeables + pentashields. Byte cap is the decoded blueprint_data
# ceiling; defends the psql -v binding size and the SQL string we hand to psql.
# Piece cap bumped 5000->10000 for G22 — researcher-2 confirmed popular Solidos
# (Tippytoes large sub-fief bases, multi-level deep-desert fortresses) exceed
# 3000 pieces and several approach 5000 (G22 BUILD SPEC section 2.3).
CAP_BLUEPRINT_PIECES=10000
CAP_BLUEPRINT_BYTES=1048576
# G29 bank_items_batch — per-grant item count cap. A CHOAM bank has finite
# slots (max_item_count); even with a perfectly empty bank we hard-cap the
# batch to keep audit detail readable and prevent fat-finger oversubmits.
CAP_BATCH_ITEMS=30
# container_items_batch — per-grant item count cap for a vehicle/storage
# container (BP_ContainerVehicle holds 150 slots). Larger than the bank cap so a
# full vehicle-package fleet (e.g. 5 buggies = 65 items) lands in one grant; the
# preflight still enforces the live free-slot count so we never overflow.
CAP_CONTAINER_BATCH=150

# jq field extractor — every read of the decoded JSON goes through here so a
# missing key yields empty string, never the literal "null".
jq_get() {
  printf '%s' "$GRANT_JSON" | jq -r --arg k "$1" \
    'if has($k) and .[$k] != null then (.[$k]|tostring) else "" end'
}
jq_get_nested() {
  # $1 = parent key, $2 = child key (used for .detail.<child>)
  printf '%s' "$GRANT_JSON" | jq -r --arg p "$1" --arg c "$2" \
    'if (.[$p]? // {} | has($c)) and (.[$p][$c] != null)
     then (.[$p][$c]|tostring) else "" end'
}

# Validators -----------------------------------------------------------------
validate_account_id() {
  [[ "$1" =~ ^[0-9]+$ ]] || fail_json "invalid account_id (must be digits): $1" 2
}
validate_int_in_range() {
  # $1 value, $2 min, $3 max, $4 label
  # The value may be negative (e.g. faction_rep amounts span -MAX..MAX); the
  # range arithmetic below handles signs. min/max are caller-supplied literals.
  if [[ ! "$1" =~ ^-?[0-9]+$ ]]; then
    fail_json "invalid $4 (must be an integer): $1" 2
  fi
  if (( $1 < $2 || $1 > $3 )); then
    fail_json "$4 out of range ($2..$3): $1" 2
  fi
}
validate_template_id() {
  [[ "$1" =~ ^[A-Za-z0-9_]+$ ]] \
    || fail_json "invalid template_id (must match [A-Za-z0-9_]+): $1" 2
}
validate_quality() {
  [[ "$1" =~ ^[0-6]$ ]] || fail_json "invalid quality (must be 0-6): $1" 2
}
validate_uuid() {
  [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
    || fail_json "invalid idempotency_key (must be a UUID): $1" 2
}
validate_mode_addset() {
  case "$1" in add|set) return 0 ;; *) fail_json "invalid mode (add|set): $1" 2 ;; esac
}
validate_active_landsraad_house() {
  local h="$1"
  [[ -n "$h" ]] || fail_json "house_name required for item_live" 2
  [[ "$h" =~ ^DA_House[A-Za-z0-9_]+$ ]] \
    || fail_json "invalid house_name format: $h" 2
  local found
  found=$(psql_scalar "SELECT 1 FROM dune.landsraad_tasks WHERE term_id = (SELECT term_id FROM dune.landsraad_decree_term ORDER BY term_id DESC LIMIT 1) AND house_name = :'house_name' LIMIT 1;" -v "house_name=${h}")
  [[ "$found" == "1" ]] || fail_json "house_name not active in current Landsraad term: $h" 2
}

# A grant_type is RAM-fragile (offline-gated) if true.
# - teleport: Funcom proc itself enforces is_player_offline(), but we gate
#   client-side too for nicer UX.
# - reset_specs: specialization_tracks is loaded into RAM on login; clobber risk.
# - repair_all: backpack/hotbar items are RAM-fragile (inventory_type 14/15).
# - reset_tutorials + wipe_codex: tutorial/codex tables are read on-demand by
#   the game; safe to mutate while the player is online.
# - import_blueprint (G20): per-grant-instance — backpack delivery (RAM-fragile
#   inventory_type=0) → offline; bank delivery (inventory_type=30, online-safe
#   per our internal notes) → online OK. The function reads
#   .detail.delivery off the global GRANT_JSON for this grant type ONLY; all
#   other types ignore the body and behave per the static enum below.
requires_offline() {
  case "$1" in
    item|solari|intel|recipe|schematic_item|char_xp|keystone|spec_unlock_track|spec_unlock_all|faction_rep|progression_preset|teleport|reset_specs|repair_all) return 0 ;;
    # P3a — icehunter parity FLevelComponent writes (G24a/b, G25a, G25b) + G23
    # main_quest_unlock whose Funcom proc itself raises EXCEPTION if the player
    # is online (complete_journey_story_nodes_for_player). align_faction is
    # TABULAR (player_faction + update_player_tags) so it stays online-safe.
    main_quest_unlock|grant_full_job_tree|grant_skill_block|reset_full_skill_area|set_starter_class|journey_node_completion)
      return 0 ;;
    # spice_addiction_enable mutates FSpiceAddictionComponent + FGEPersistence-
    # Component, both loaded into RAM on character login (same RAM-fragile class
    # as char_xp / keystone FLevelComponent writes). Online write would be
    # clobbered on the next save tick.
    spice_addiction_enable)
      return 0 ;;
    import_blueprint)
      local _delivery
      _delivery=$(printf '%s' "$GRANT_JSON" | jq -r '.detail.delivery // empty')
      case "$_delivery" in
        bank) return 1 ;;
        *)    return 0 ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

do_grant() {
  local b64="$1"
  # --- layer 2: decode + parse -------------------------------------------
  if [[ ! "$b64" =~ ^[A-Za-z0-9+/=]+$ ]]; then
    fail_json "grant payload is not valid base64" 2
  fi
  local decoded
  decoded=$(printf '%s' "$b64" | base64 -d 2>/dev/null || true)
  if [[ -z "$decoded" ]]; then
    fail_json "grant payload failed base64 decode" 2
  fi
  if ! printf '%s' "$decoded" | jq -e . >/dev/null 2>&1; then
    fail_json "grant payload is not valid JSON" 2
  fi
  GRANT_JSON="$decoded"

  # --- common fields ------------------------------------------------------
  local account_id grant_type idem operator req_mode defer_if_online
  account_id=$(jq_get account_id)
  grant_type=$(jq_get grant_type)
  idem=$(jq_get idempotency_key)
  operator=$(jq_get operator)
  req_mode=$(jq_get mode);            [[ -n "$req_mode" ]] || req_mode="apply"
  defer_if_online=$(jq_get defer_if_online)

  validate_account_id "$account_id"
  is_valid_grant_type "$grant_type" || fail_json "unknown grant_type: $grant_type" 2
  validate_uuid "$idem"
  [[ -n "$operator" ]] || operator="unknown"
  case "$req_mode" in apply|dry-run) ;; *) fail_json "invalid mode: $req_mode" 2 ;; esac

  # CLI --dry-run flag overrides; either path means "build SQL, no COMMIT".
  local dry=0
  [[ "$DRY_RUN" == "1" || "$req_mode" == "dry-run" ]] && dry=1

  resolve_db_pod

  # --- account resolution -------------------------------------------------
  # pawn (actor_id) + controller + main backpack inventory + capacity.
  local actor_id controller_id inv_id inv_cap inv_used inv_vol_cap online_status
  # MULTI-SLOT: encrypted_player_state holds one row per character SLOT, not per
  # account (54 of 168 accounts have 2+). psql_scalar strips whitespace, so a
  # 2-row result CONCATENATES ("63131\n63134" -> 6313163134) and online_status
  # becomes "OfflineOffline", which never equals "Online" — the requires_offline
  # gate below would fail OPEN against a live player. Resolve the one REAL
  # character by joining player_state (empty slots have no row); ORDER BY+LIMIT 1
  # keeps it single-valued if Funcom ever ships a second live character.
  actor_id=$(psql_scalar "${RESOLVE_TARGET_SQL//@COL@/eps.player_pawn_id}" -v "account_id=${account_id}")
  controller_id=$(psql_scalar "${RESOLVE_TARGET_SQL//@COL@/eps.player_controller_id}" -v "account_id=${account_id}")
  online_status=$(psql_scalar "${RESOLVE_TARGET_SQL//@COL@/eps.online_status}" -v "account_id=${account_id}")

  if [[ -z "$actor_id" || -z "$controller_id" ]]; then
    fail_json "could not resolve actor/controller for account ${account_id}" 4
  fi

  # --- offline preflight for RAM-fragile grants (plan section 6) ----------
  # Re-checked HERE inside the script (not just the UI) immediately before any
  # write. If the target is Online and not deferring, refuse.
  if requires_offline "$grant_type"; then
    local in_grace
    in_grace=$(psql_scalar "${RESOLVE_TARGET_SQL//@COL@/(eps.reconnect_grace_period_end IS NOT NULL AND eps.reconnect_grace_period_end > NOW())::int}" -v "account_id=${account_id}")
    if [[ "$online_status" == "Online" || "$in_grace" == "1" ]]; then
      if [[ "$defer_if_online" == "true" && "$dry" == "0" ]]; then
        # Deferred-sweep: write the durable ls_progression_grants row now with
        # status='deferred'; the actual game-state write is left to Lane D's
        # sweeper. The grant body below is skipped.
        defer_grant_row "$account_id" "$grant_type" "$idem" "$operator"
        return
      fi
      local reason
      if [[ "$online_status" == "Online" ]]; then
        reason="status=Online"
      else
        # status=Offline but still in reconnect_grace_period_end window —
        # the game pod still holds the actor's RAM state during this window,
        # so a JSONB write would get clobbered when the pod flushes RAM.
        local grace_secs
        grace_secs=$(psql_scalar "${RESOLVE_TARGET_SQL//@COL@/GREATEST(0, EXTRACT(EPOCH FROM (eps.reconnect_grace_period_end - NOW())))::int}" -v "account_id=${account_id}")
        reason="in reconnect grace period (~${grace_secs}s remaining; the game pod still holds RAM state during this window — a JSONB write now would be clobbered on next RAM flush)"
      fi
      fail_json "target account ${account_id} cannot receive ${grant_type} right now: ${reason}. Wait for the player to be fully Offline + past the grace timer, then retry." 5
    fi
  fi

  # --- per-grant-type SQL build ------------------------------------------
  # GRANT_PREFLIGHT  — DO $$ ... RAISE EXCEPTION abort blocks, run before the
  #                    write; each guarded on the grant being NEW (replay-safe).
  # GRANT_BODY       — the actual write, a CTE gated WHERE EXISTS the new grant.
  # DETAIL_JSON      — compact jsonb detail stored on the audit row.
  local GRANT_PREFLIGHT="" GRANT_BODY="" DETAIL_JSON="{}"
  # Extra psql -v bindings collected as an array.
  #
  # controller_id is bound for EVERY grant. encrypted_player_state holds one row
  # per character SLOT, not per account (the script's own resolver notes 54 of
  # 168 accounts have 2+), so a bare
  #   (SELECT player_controller_id FROM encrypted_player_state WHERE account_id=…)
  # inside a grant body returns MORE THAN ONE ROW and the whole transaction
  # aborts with "more than one row returned by a subquery used as an
  # expression". Hit live on 2026-07-27 applying G7 faction_rep to account
  # 30313 (2 slots) — it failed closed, but it failed.
  # The bash layer already resolved the ONE real character above via
  # RESOLVE_TARGET_SQL (joins player_state, orders by last login, LIMIT 1), so
  # bodies must use :controller_id rather than re-deriving it.
  PSQL_VARS=( -v "controller_id=${controller_id}" )

  case "$grant_type" in
    item|schematic_item)        build_item_grant "$grant_type" ;;
    item_live)                  build_item_live_grant ;;
    solari)                     build_solari_grant ;;
    solari_currency)            build_solari_currency_grant ;;
    house_scrip)                build_house_scrip_grant ;;
    teleport)                   build_teleport_grant ;;
    reset_specs)                build_reset_specs_grant ;;
    reset_tutorials)            build_reset_tutorials_grant ;;
    wipe_codex)                 build_wipe_codex_grant ;;
    repair_all)                 build_repair_all_grant ;;
    intel)                      build_intel_grant ;;
    recipe)                     build_recipe_grant ;;
    faction_rep)                build_faction_rep_grant ;;
    spec_xp)                    build_spec_xp_grant ;;
    keystone)                   build_keystone_grant ;;
    spec_unlock_track)          build_spec_unlock_track_grant ;;
    spec_unlock_all)            build_spec_unlock_all_grant ;;
    char_xp)                    build_char_xp_grant ;;
    progression_preset)         build_progression_preset_grant ;;
    import_blueprint)           build_import_blueprint_grant ;;
    bb_handoff)                 build_bb_handoff_grant ;;
    bb_clone)                   build_bb_clone_grant ;;
    import_solido_to_basebackup) build_import_solido_to_basebackup_grant ;;
    main_quest_unlock)          build_main_quest_unlock_grant ;;
    grant_full_job_tree)        build_grant_full_job_tree_grant ;;
    grant_skill_block)          build_grant_skill_block_grant ;;
    reset_full_skill_area)      build_reset_full_skill_area_grant ;;
    set_starter_class)          build_set_starter_class_grant ;;
    align_faction)              build_align_faction_grant ;;
    journey_full_unlock)        build_journey_full_unlock_grant ;;
    journey_node_completion)    build_journey_node_completion_grant ;;
    spice_addiction_enable)     build_spice_addiction_enable_grant ;;
    bank_items_batch)           build_bank_items_batch_grant ;;
    container_items_batch)      build_container_items_batch_grant ;;
    ban)                        build_ban_grant ;;
    unban)                      build_unban_grant ;;
  esac

  # --- assemble the transaction ------------------------------------------
  # CR-4 layers 2+3: server-side atomic idempotency + grant. NO psql \if/\gset
  # client-side control flow (it is unproven under ON_ERROR_STOP). The whole
  # decision is made server-side:
  #
  #   1. The idempotency INSERT (ON CONFLICT DO NOTHING) plus a fallback lookup
  #      of any pre-existing row both land in a TEMP TABLE _grant_gate, which
  #      records the resolved grant id and an is_new flag. TEMP tables are
  #      transaction-scoped and visible to DO blocks and later statements.
  #   2. Per-grant preflight checks are DO $$ ... RAISE EXCEPTION blocks. Each
  #      is guarded so it only fires when the grant is NEW (is_new) — on a
  #      replay the preflight is a no-op. ON_ERROR_STOP makes a RAISE roll the
  #      whole transaction back and exit non-zero.
  #   3. The grant body is gated server-side: its write CTE carries
  #      WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) so on a replay
  #      no row is written.
  #   4. A single uniquely-prefixed RESULT row is emitted last; the bash layer
  #      keys ONLY on ^RESULT\| so intermediate output cannot be mis-parsed.
  #
  # status: tabular grants land 'applied' immediately; RAM-fragile grants that
  # reach here are already confirmed Offline so they also land 'applied'.
  local final_status="applied"

  # Only the recipe (G5) preflight needs item_key inside its DO block. For all
  # other grant types the gate column is NULL. :'item_key' is bound for recipe.
  local GATE_ITEM_KEY="NULL::text"
  [[ "$grant_type" == "recipe" ]] && GATE_ITEM_KEY=":'item_key'"

  local TXN
  TXN=$(cat <<EOF
BEGIN;

-- CR-4 layer 2/3: idempotency gate + durable audit row, same transaction as
-- the grant body. The INSERT uses ON CONFLICT (idempotency_key) DO NOTHING; a
-- second CTE looks up any pre-existing row. _grant_gate ends up with exactly
-- one row: (grant_id, is_new, account_id, item_key). is_new = true only when
-- this call inserted it. account_id/item_key are carried so the preflight
-- DO blocks can read them — psql does NOT substitute :vars inside a \$\$ body.
CREATE TEMP TABLE _grant_gate ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO dune.ls_progression_grants
    (idempotency_key, account_id, grant_type, detail, operator, status)
  VALUES
    (:'idem'::uuid, :account_id, :'grant_type', :'detail'::jsonb,
     :'operator', :'gstatus')
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
),
prior AS (
  SELECT id FROM dune.ls_progression_grants
   WHERE idempotency_key = :'idem'::uuid
)
SELECT
  COALESCE((SELECT id FROM ins), (SELECT id FROM prior)) AS grant_id,
  (EXISTS (SELECT 1 FROM ins))                           AS is_new,
  (:account_id)::bigint                                  AS account_id,
  ${GATE_ITEM_KEY}                                       AS item_key;

${GRANT_PREFLIGHT}
${GRANT_BODY}

-- Single uniquely-prefixed outcome row, emitted BEFORE COMMIT while the
-- ON COMMIT DROP temp table still exists. The bash layer keys ONLY on
-- ^RESULT\| so no intermediate psql output can be mis-parsed as the outcome.
SELECT 'RESULT|' || grant_id::text || '|'
       || CASE WHEN is_new THEN 'applied' ELSE 'replay' END
  FROM _grant_gate;

COMMIT;
EOF
)

  # psql variable bindings (CR-4 / section 8: parameterized -v, never string
  # concatenation of values into SQL text).
  local -a vargs=(
    -v "idem=${idem}"
    -v "account_id=${account_id}"
    -v "grant_type=${grant_type}"
    -v "operator=${operator}"
    -v "gstatus=${final_status}"
    -v "detail=${DETAIL_JSON}"
  )
  vargs+=("${PSQL_VARS[@]}")

  if [[ "$dry" == "1" ]]; then
    # Dry-run: print the fully-bound transaction text, do NOT execute.
    # We still run psql but with --set commands echoed via a no-COMMIT variant:
    # simplest faithful behaviour is to print the SQL + the bindings.
    printf '{"success":true,"status":"dry-run","grant_type":%s,"account_id":%s,' \
      "$(json_str "$grant_type")" "$account_id"
    printf '"message":"dry-run: SQL built, NOT executed","sql":%s,"bindings":%s}\n' \
      "$(json_str "$TXN")" \
      "$(json_str "idem=${idem} account_id=${account_id} grant_type=${grant_type} operator=${operator} status=${final_status} detail=${DETAIL_JSON} ${PSQL_VARS[*]}")"
    exit 0
  fi

  # --- execute ------------------------------------------------------------
  local result
  if ! result=$(printf '%s\n' "$TXN" | run_psql -tA "${vargs[@]}" 2>&1); then
    fail_json "grant transaction failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)" 6
  fi

  # The outcome is a single uniquely-prefixed row: RESULT|<grant_id>|<state>.
  # Key ONLY on ^RESULT\| — intermediate psql output can never collide with it.
  local row grant_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  grant_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$grant_id" ]] || grant_id="null"

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","grant_id":%s,"grant_type":%s,"account_id":%s,"message":"already applied — idempotent replay, no change made"}\n' \
      "$grant_id" "$(json_str "$grant_type")" "$account_id"
    exit 0
  fi

  printf '{"success":true,"status":"applied","grant_id":%s,"grant_type":%s,"account_id":%s,"message":"grant applied — the player must relog or change zone before it appears"}\n' \
    "$grant_id" "$(json_str "$grant_type")" "$account_id"
  exit 0
}

# Write only the durable audit row with status='deferred' (deferred-sweep path).
# The actual game-state write is handled later by the Lane D sweeper.
defer_grant_row() {
  local account_id="$1" grant_type="$2" idem="$3" operator="$4"
  local DETAIL_JSON_LOCAL="$DETAIL_JSON"
  [[ -n "$DETAIL_JSON_LOCAL" ]] || DETAIL_JSON_LOCAL="{}"
  local sql
  sql=$(cat <<'EOF'
BEGIN;
INSERT INTO dune.ls_progression_grants
  (idempotency_key, account_id, grant_type, detail, operator, status)
VALUES
  (:'idem'::uuid, :account_id, :'grant_type', :'detail'::jsonb, :'operator', 'deferred')
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id;
COMMIT;
EOF
)
  local gid
  gid=$(printf '%s\n' "$sql" | run_psql -tA \
          -v "idem=${idem}" -v "account_id=${account_id}" \
          -v "grant_type=${grant_type}" -v "operator=${operator}" \
          -v "detail=${DETAIL_JSON_LOCAL}" 2>&1 | grep -E '^[0-9]+$' | tail -n1 || true)
  [[ -n "$gid" ]] || gid="null"
  printf '{"success":true,"status":"deferred","grant_id":%s,"grant_type":%s,"account_id":%s,"message":"target online — grant deferred; the sweeper will apply it when the player is next offline"}\n' \
    "$gid" "$(json_str "$grant_type")" "$account_id"
  exit 0
}

# -----------------------------------------------------------------------------
# Per-grant-type builders. Each sets GRANT_BODY (SQL run between the idempotency
# gate and COMMIT), DETAIL_JSON (compact jsonb for the audit row), and appends
# any extra psql -v bindings to PSQL_VARS. Values are ALWAYS passed as psql
# variables — never concatenated into SQL text (plan section 8).
#
# Most of these reference the resolved ids via psql vars set in the txn header:
#   :account_id is the literal int; actor/controller/inventory are re-resolved
#   inside the SQL so the grant always uses live ids.
# -----------------------------------------------------------------------------

# G1 / G6 — inventory item (and schematic item). Additive INSERT into
# dune.items at position_index = COALESCE(MAX,-1)+1, with a capacity check.
build_item_grant() {
  local gt="$1"
  local template_id quantity quality
  template_id=$(jq_get_nested detail template_id)
  quantity=$(jq_get_nested detail quantity)
  quality=$(jq_get_nested detail quality)

  validate_template_id "$template_id"
  validate_int_in_range "$quantity" 1 "$CAP_ITEM_QTY" "quantity"
  validate_quality "$quality"

  # G6 sanity: a schematic_item template_id should end _Schematic. Warn-only
  # via the audit detail; do not hard-fail (catalog is authoritative).
  DETAIL_JSON=$(jq -nc --arg t "$template_id" --argjson q "$quantity" \
    --argjson ql "$quality" --arg gt "$gt" \
    '{template_id:$t,quantity:$q,quality:$ql,kind:$gt}')

  PSQL_VARS+=( -v "template_id=${template_id}" -v "quantity=${quantity}" -v "quality=${quality}" )

  # Capacity check (risk R6): abort the txn if adding this item would exceed
  # max_item_count on the main backpack (inventory_type=0). Each item grant is
  # treated as +1 slot (a single stack). Server-side RAISE EXCEPTION aborts;
  # guarded on is_new so a replay never re-evaluates the check.
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G1 preflight: resolve the main backpack and refuse if missing or full.
DO $$
DECLARE g _grant_gate%ROWTYPE; v_inv bigint; v_cap int; v_used int;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT inv.id, inv.max_item_count,
         (SELECT COUNT(*) FROM dune.items it WHERE it.inventory_id = inv.id)
    INTO v_inv, v_cap, v_used
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 0
   WHERE eps.account_id = g.account_id;
  IF v_inv IS NULL THEN
    RAISE EXCEPTION 'CAPACITY_FAIL: no main backpack inventory for this account';
  END IF;
  IF v_used >= v_cap THEN
    RAISE EXCEPTION 'CAPACITY_FAIL: backpack full (used % of %)', v_used, v_cap;
  END IF;
END $$;
EOF
)

  GRANT_BODY=$(cat <<'EOF'
-- G1 write: additive INSERT at the next free slot. Gated on a NEW grant so a
-- replay writes nothing.
WITH bp AS (
  SELECT inv.id AS inv_id
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 0
   WHERE eps.account_id = :account_id
)
INSERT INTO dune.items
  (inventory_id, stack_size, position_index, template_id, stats,
   quality_level, acquisition_time, is_new)
SELECT
  bp.inv_id, :quantity,
  COALESCE((SELECT MAX(position_index) FROM dune.items
             WHERE inventory_id = bp.inv_id), -1) + 1,
  :'template_id', '{}'::jsonb, :quality, 0, true
FROM bp
WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G2 — Solari granted as SolarisCoin stackable inventory items. UPDATE an
# existing SolarisCoin stack if present, else INSERT a new stack. Offline-gated.
build_solari_grant() {
  local amount
  amount=$(jq_get_nested detail amount)
  validate_int_in_range "$amount" 1 "$CAP_SOLARI" "amount"

  DETAIL_JSON=$(jq -nc --argjson a "$amount" '{amount:$a,template_id:"SolarisCoin"}')
  PSQL_VARS+=( -v "amount=${amount}" )

  # Preflight: resolve the backpack; refuse if missing, or if a NEW SolarisCoin
  # stack would be needed and the backpack is full. An UPDATE of an existing
  # stack consumes no slot, so the full check only matters when no stack exists.
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G2 preflight: backpack resolution + capacity (only if a new stack is needed).
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_inv bigint; v_cap int; v_used int; v_stack bigint;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT inv.id, inv.max_item_count,
         (SELECT COUNT(*) FROM dune.items it WHERE it.inventory_id = inv.id)
    INTO v_inv, v_cap, v_used
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 0
   WHERE eps.account_id = g.account_id;
  IF v_inv IS NULL THEN
    RAISE EXCEPTION 'CAPACITY_FAIL: no main backpack inventory for this account';
  END IF;
  SELECT id INTO v_stack FROM dune.items
   WHERE inventory_id = v_inv AND template_id = 'SolarisCoin'
   ORDER BY position_index LIMIT 1;
  IF v_stack IS NULL AND v_used >= v_cap THEN
    RAISE EXCEPTION 'CAPACITY_FAIL: backpack full (used % of %)', v_used, v_cap;
  END IF;
END $$;
EOF
)

  # Write: UPDATE an existing SolarisCoin stack, else INSERT a new one. Both
  # branches are gated on a NEW grant via WHERE EXISTS / WHERE NOT EXISTS so a
  # replay writes nothing.
  GRANT_BODY=$(cat <<'EOF'
-- G2 write: UPDATE existing SolarisCoin stack if present.
WITH bp AS (
  SELECT inv.id AS inv_id
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 0
   WHERE eps.account_id = :account_id
)
UPDATE dune.items it
   SET stack_size = it.stack_size + :amount
  FROM bp
 WHERE it.inventory_id = bp.inv_id
   AND it.template_id = 'SolarisCoin'
   AND it.id = (SELECT id FROM dune.items
                 WHERE inventory_id = bp.inv_id AND template_id = 'SolarisCoin'
                 ORDER BY position_index LIMIT 1)
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- G2 write: INSERT a new SolarisCoin stack only when none exists.
WITH bp AS (
  SELECT inv.id AS inv_id
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 0
   WHERE eps.account_id = :account_id
)
INSERT INTO dune.items
  (inventory_id, stack_size, position_index, template_id, stats,
   quality_level, acquisition_time, is_new)
SELECT
  bp.inv_id, :amount,
  COALESCE((SELECT MAX(position_index) FROM dune.items
             WHERE inventory_id = bp.inv_id), -1) + 1,
  'SolarisCoin',
  '{"FItemStackAndDurabilityStats": [[], {"DecayedMaxDurability": 0.0}]}'::jsonb,
  0, 0, true
FROM bp
WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  AND NOT EXISTS (SELECT 1 FROM dune.items
                   WHERE inventory_id = bp.inv_id
                     AND template_id = 'SolarisCoin');
EOF
)
}

# G13 — Solari via the Funcom adjust_player_virtual_currency_balance proc.
# Tabular, ONLINE-SAFE: writes the currency balance row directly; no backpack
# slot needed, no JSONB on the actor, no RAM cache to clobber. Distinct from
# G2 'solari' which drops a SolarisCoin stack into the backpack (offline-gated,
# slot-consuming). Use G13 for ops Solari rewards, G2 for welcome-pack-style
# "you have a coin in your bag" scenarios.
#
# Proc body confirmed 2026-05-23: INSERT ON CONFLICT DO UPDATE on
# player_virtual_currency_balances, logs to log_event_solaris when
# currency_id = get_solaris_id() (= 0), clamps negative balances to 0 and
# log_cheating's the actor. We pass a positive delta only (CAP enforced),
# so the negative-balance branch is unreachable from this path.
build_solari_currency_grant() {
  local amount
  amount=$(jq_get_nested detail amount)
  validate_int_in_range "$amount" 1 "$CAP_SOLARI_CURRENCY" "amount"

  DETAIL_JSON=$(jq -nc --argjson a "$amount" '{amount:$a,currency_id:0,via:"funcom_proc"}')
  PSQL_VARS+=( -v "amount=${amount}" )

  GRANT_PREFLIGHT=""

  # Write: route through the Funcom proc. Gated on a NEW grant via WHERE EXISTS
  # so a replay is a no-op. SET LOCAL search_path because the proc references
  # tables UNQUALIFIED (player_virtual_currency_balances, accounts, etc).
  GRANT_BODY=$(cat <<'EOF'
-- G13 write: Solari delta via Funcom proc (online-safe, no slot).
SET LOCAL search_path TO dune, public;
SELECT dune.adjust_player_virtual_currency_balance(
         :controller_id::bigint,
         dune.get_solaris_id(),
         :amount::bigint
       )
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G3 — House Scrip via dune.player_virtual_currency_balances.
# Tabular; online-safe. add (default) or set.
# Currency_id auto-detect (icehunter parity Action #6): resolves to the largest
# non-Solaris currency_id at run time (matches icehunter's resolveScripCurrencyID
# heuristic from db.go:919). With only one non-Solaris currency on our server
# (id 1, the Landsraad scrip) the resolution is stable; the SELECT in the CTE
# guards against a future second currency being introduced.
build_house_scrip_grant() {
  local amount mode scrip_id
  amount=$(jq_get_nested detail amount)
  mode=$(jq_get_nested detail mode);  [[ -n "$mode" ]] || mode="add"
  validate_int_in_range "$amount" 0 "$CAP_HOUSE_SCRIP" "amount"
  validate_mode_addset "$mode"

  # Resolve scrip currency_id (largest non-Solaris by total balance, then by id;
  # mirrors icehunter resolveScripCurrencyID at db.go:919). We resolve here in
  # bash so the audit row records the actual currency_id used; the SQL body
  # then binds it as a psql variable rather than re-querying mid-transaction.
  scrip_id=$(psql_scalar "SELECT currency_id FROM dune.player_virtual_currency_balances WHERE currency_id <> dune.get_solaris_id() GROUP BY currency_id ORDER BY SUM(balance) DESC, currency_id LIMIT 1;")
  if [[ ! "$scrip_id" =~ ^[0-9]+$ ]]; then
    fail_json "could not resolve scrip currency_id (no non-solaris currency rows found)" 2
  fi
  if (( scrip_id < 1 || scrip_id > 32767 )); then
    fail_json "resolved scrip currency_id out of smallint range: $scrip_id" 2
  fi

  DETAIL_JSON=$(jq -nc --argjson a "$amount" --arg m "$mode" --argjson c "$scrip_id" \
    '{amount:$a,mode:$m,currency_id:$c}')
  PSQL_VARS+=( -v "amount=${amount}" -v "scrip_currency_id=${scrip_id}" )

  # Tabular UPSERT, gated on a NEW grant so a replay is a no-op.
  if [[ "$mode" == "set" ]]; then
    GRANT_BODY=$(cat <<'EOF'
-- G3 write (set):
INSERT INTO dune.player_virtual_currency_balances
  (player_controller_id, currency_id, balance)
SELECT player_controller_id, :scrip_currency_id::smallint, :amount
  FROM dune.encrypted_player_state
 WHERE account_id = :account_id
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
ON CONFLICT (player_controller_id, currency_id) DO UPDATE
  SET balance = EXCLUDED.balance;
EOF
)
  else
    GRANT_BODY=$(cat <<'EOF'
-- G3 write (add):
INSERT INTO dune.player_virtual_currency_balances
  (player_controller_id, currency_id, balance)
SELECT player_controller_id, :scrip_currency_id::smallint, :amount
  FROM dune.encrypted_player_state
 WHERE account_id = :account_id
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
ON CONFLICT (player_controller_id, currency_id) DO UPDATE
  SET balance = dune.player_virtual_currency_balances.balance + :amount;
EOF
)
  fi
}

# G4 — Intel points. jsonb_set additive on the pawn actor properties. Offline
# mandatory (jsonb clobber risk if the player is online).
build_intel_grant() {
  local amount
  amount=$(jq_get_nested detail amount)
  validate_int_in_range "$amount" 1 "$CAP_INTEL" "amount"

  DETAIL_JSON=$(jq -nc --argjson a "$amount" '{amount:$a}')
  PSQL_VARS+=( -v "amount=${amount}" )

  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G4 preflight: the account must resolve to a pawn actor.
DO $$
DECLARE g _grant_gate%ROWTYPE;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  IF NOT EXISTS (SELECT 1 FROM dune.encrypted_player_state
                  WHERE account_id = g.account_id
                    AND player_pawn_id IS NOT NULL) THEN
    RAISE EXCEPTION 'RESOLVE_FAIL: no pawn for this account';
  END IF;
END $$;
EOF
)

  GRANT_BODY=$(cat <<'EOF'
-- G4 write: additive jsonb_set of the Intel point total. Gated on a NEW grant.
UPDATE dune.actors a
   SET properties = jsonb_set(
     a.properties,
     '{TechKnowledgePlayerComponent,m_TechKnowledgePoints}',
     to_jsonb(
       COALESCE((a.properties#>>'{TechKnowledgePlayerComponent,m_TechKnowledgePoints}')::int, 0)
       + :amount)
   )
  FROM dune.encrypted_player_state eps
 WHERE eps.account_id = :account_id
   AND a.id = eps.player_pawn_id
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G5 — recipe / schematic unlock (state-flip). Flips a matched ItemKey in the
# pawn's m_TechKnowledgeData array from its current state to Purchased. Only
# ItemKeys ALREADY PRESENT in the array can be flipped (plan G5 note / OI-4) —
# the SQL RETURNS the matched count and the transaction aborts if zero matched.
# Offline mandatory.
build_recipe_grant() {
  local item_key
  item_key=$(jq_get_nested detail item_key)
  [[ -n "$item_key" ]] || item_key=$(jq_get_nested detail template_id)
  validate_template_id "$item_key"

  DETAIL_JSON=$(jq -nc --arg k "$item_key" '{item_key:$k}')
  PSQL_VARS+=( -v "item_key=${item_key}" )

  # Preflight: the account must resolve to a pawn, AND the ItemKey must already
  # be present in m_TechKnowledgeData — only present keys can be flipped (OI-4).
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G5 preflight: pawn resolution + ItemKey-present check.
DO $$
DECLARE g _grant_gate%ROWTYPE; v_pawn bigint; v_matched int;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT player_pawn_id INTO v_pawn FROM dune.encrypted_player_state
   WHERE account_id = g.account_id;
  IF v_pawn IS NULL THEN
    RAISE EXCEPTION 'RESOLVE_FAIL: no pawn for this account';
  END IF;
  SELECT COUNT(*) INTO v_matched
    FROM dune.actors a,
         jsonb_array_elements(
           a.properties#>'{TechKnowledgePlayerComponent,m_TechKnowledgeData}'
         ) WITH ORDINALITY e(elem, ord)
   WHERE a.id = v_pawn
     AND e.elem->>'ItemKey' = g.item_key;
  IF v_matched = 0 THEN
    RAISE EXCEPTION 'RECIPE_NO_MATCH: ItemKey not present in this player''s m_TechKnowledgeData array';
  END IF;
END $$;
EOF
)

  # Write: rebuild m_TechKnowledgeData preserving order (WITH ORDINALITY),
  # flipping only the matched element to Purchased. Gated on a NEW grant.
  GRANT_BODY=$(cat <<'EOF'
-- G5 write: flip the matched ItemKey to Purchased.
UPDATE dune.actors a
   SET properties = jsonb_set(
     a.properties,
     '{TechKnowledgePlayerComponent,m_TechKnowledgeData}',
     (SELECT jsonb_agg(
               CASE WHEN e.elem->>'ItemKey' = :'item_key'
                    THEN e.elem
                         || jsonb_build_object('UnlockedState','Purchased')
                         || jsonb_build_object('bIsNewEntry', false)
                    ELSE e.elem END
               ORDER BY e.ord)
        FROM jsonb_array_elements(
               a.properties#>'{TechKnowledgePlayerComponent,m_TechKnowledgeData}'
             ) WITH ORDINALITY e(elem, ord))
   )
  FROM dune.encrypted_player_state eps
 WHERE eps.account_id = :account_id
   AND a.id = eps.player_pawn_id
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G7 v2 — faction reputation. Writes BOTH:
#   (1) FactionPlayerComponent JSONB on the controller actor — this is what
#       the in-game UI reads for the displayed rep + rank. Without this write
#       the grant lands silently in the table but the UI never updates.
#   (2) dune.set_player_faction_reputation proc — syncs Faction.X.Tier_N
#       gameplay tags + writes the player_faction_reputation tabular store.
#
# Source-of-truth read for ADD mode = FactionPlayerComponent JSONB (NOT the
# table — the table can be stale or empty). Goes BEYOND icehunter parity:
# icehunter's `cmdGiveFactionRep` calls only the proc and has the same
# paper-only-grant bug.
#
# OFFLINE-GATED — FactionPlayerComponent is the same RAM-fragile pattern as
# FLevelComponent (G11 char_xp). Writes get clobbered by RAM-flush on logout
# if the player is online. requires_offline() enforces.
#
# Faction.Name string mapping is hardcoded — Funcom's faction ids are fixed.
# search_path SET LOCAL needed for the proc's unqualified table refs.
build_faction_rep_grant() {
  local faction_id amount mode faction_name
  faction_id=$(jq_get_nested detail faction_id)
  amount=$(jq_get_nested detail amount)
  mode=$(jq_get_nested detail mode);  [[ -n "$mode" ]] || mode="add"
  [[ "$faction_id" =~ ^[0-9]+$ ]] || fail_json "invalid faction_id: $faction_id" 2
  validate_int_in_range "$amount" "-${CAP_FACTION_REP}" "$CAP_FACTION_REP" "amount"
  validate_mode_addset "$mode"

  case "$faction_id" in
    1) faction_name="Atreides"  ;;
    2) faction_name="Harkonnen" ;;
    3) faction_name="None"      ;;
    4) faction_name="Smuggler"  ;;
    *) fail_json "unknown faction_id: $faction_id" 2 ;;
  esac

  # P3a icehunter v0.5.8 parity: when granting rep to Atreides/Harkonnen, also
  # write the 8 alignment tags + call change_player_faction if the character is
  # still unaligned. Tier tags (Faction.X.Tier_N) are already written by the
  # set_player_faction_reputation proc below; the alignment-tag union below
  # adds the DialogueFlags.Factions.* + Contract.Tracking.* set the current G7
  # was missing. faction_id 3 (None) / 4 (Smuggler) skip the alignment step.
  local alignment_tags_json="[]"
  case "$faction_id" in
    1|2) alignment_tags_json=$(emit_alignment_sql "$faction_name") ;;
  esac

  DETAIL_JSON=$(jq -nc --argjson f "$faction_id" --argjson a "$amount" \
                       --arg m "$mode" --arg n "$faction_name" \
                       --argjson t "$alignment_tags_json" \
    '{faction_id:$f,amount:$a,mode:$m,faction_name:$n,
      alignment_tag_count:($t|length)}')
  PSQL_VARS+=( -v "faction_id=${faction_id}" -v "amount=${amount}"
               -v "faction_name=${faction_name}"
               -v "alignment_tags=${alignment_tags_json}" )

  if [[ "$mode" == "set" ]]; then
    GRANT_BODY=$(cat <<'EOF'
-- G7 v2 write (set): JSONB on actor + Funcom proc + P3a alignment step.
SET LOCAL search_path TO dune, public;

-- P3a Step A (faction_id 1/2 only): align if currently unaligned. The proc
-- is a no-op when already aligned; neutral_faction_id=3 routes the upsert
-- path. Skipped when alignment_tags is the empty array (faction_id 3/4).
SELECT dune.change_player_faction(
         :controller_id::bigint,
         (:faction_id)::smallint,
         3::smallint,
         NOW()::timestamp)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
   AND jsonb_array_length(:'alignment_tags'::jsonb) > 0;

-- P3a Step B: union the 8 alignment tags. Tabular-online-safe; tier tags
-- (Faction.X.Tier_N) are written by the set_player_faction_reputation proc
-- below, so this call carries ONLY the alignment-tag set we currently miss.
SELECT dune.update_player_tags(
         :account_id::bigint,
         (SELECT array_agg(t)
            FROM jsonb_array_elements_text(:'alignment_tags'::jsonb) t),
         ARRAY[]::text[])
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
   AND jsonb_array_length(:'alignment_tags'::jsonb) > 0;

WITH ctx AS (
  SELECT eps.player_controller_id::bigint AS ctrl_id,
         a.properties->'FactionPlayerComponent'->'m_FactionDataArray' AS arr
    FROM dune.encrypted_player_state eps
    JOIN dune.actors a ON a.id = eps.player_controller_id
   WHERE eps.player_controller_id = :controller_id
),
new_value AS (
  SELECT LEAST(GREATEST((:amount)::int, 0), 12475) AS v
),
new_arr AS (
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM ctx, jsonb_array_elements(ctx.arr) elem
     WHERE elem->'Faction'->>'Name' = :'faction_name'
  ) THEN
    (SELECT jsonb_agg(
      CASE WHEN elem->'Faction'->>'Name' = :'faction_name' THEN
        jsonb_set(
          jsonb_set(elem, '{ReputationAmount}', to_jsonb((SELECT v FROM new_value))),
          '{timestamp}', to_jsonb(EXTRACT(EPOCH FROM NOW())))
      ELSE elem END)
     FROM ctx, jsonb_array_elements(ctx.arr) elem)
  ELSE
    COALESCE((SELECT arr FROM ctx), '[]'::jsonb) ||
    jsonb_build_array(jsonb_build_object(
      'Faction', jsonb_build_object('Name', (:'faction_name')::text),
      'timestamp', EXTRACT(EPOCH FROM NOW()),
      'ReputationAmount', (SELECT v FROM new_value)))
  END AS arr_v
)
UPDATE dune.actors
   SET properties = jsonb_set(properties,
                              '{FactionPlayerComponent,m_FactionDataArray}',
                              (SELECT arr_v FROM new_arr))
 WHERE id = (SELECT ctrl_id FROM ctx)
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Proc call reads the JUST-WRITTEN JSONB value -> table + tier-tag sync.
SELECT dune.set_player_faction_reputation(
         :controller_id::bigint,
         (:faction_id)::smallint,
         COALESCE((
           SELECT (elem->>'ReputationAmount')::int
             FROM dune.actors a,
                  jsonb_array_elements(a.properties->'FactionPlayerComponent'->'m_FactionDataArray') elem
            WHERE a.id = :controller_id
              AND elem->'Faction'->>'Name' = :'faction_name'), 0)::integer)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
  else
    GRANT_BODY=$(cat <<'EOF'
-- G7 v2 write (add): JSONB on actor (read current from JSONB) + Funcom proc
-- + P3a alignment step.
SET LOCAL search_path TO dune, public;

-- P3a Step A (faction_id 1/2 only): align if currently unaligned.
SELECT dune.change_player_faction(
         :controller_id::bigint,
         (:faction_id)::smallint,
         3::smallint,
         NOW()::timestamp)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
   AND jsonb_array_length(:'alignment_tags'::jsonb) > 0;

-- P3a Step B: union the 8 alignment tags (alignment-only; tier tags handled
-- by set_player_faction_reputation below).
SELECT dune.update_player_tags(
         :account_id::bigint,
         (SELECT array_agg(t)
            FROM jsonb_array_elements_text(:'alignment_tags'::jsonb) t),
         ARRAY[]::text[])
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
   AND jsonb_array_length(:'alignment_tags'::jsonb) > 0;

WITH ctx AS (
  SELECT eps.player_controller_id::bigint AS ctrl_id,
         a.properties->'FactionPlayerComponent'->'m_FactionDataArray' AS arr
    FROM dune.encrypted_player_state eps
    JOIN dune.actors a ON a.id = eps.player_controller_id
   WHERE eps.player_controller_id = :controller_id
),
current_rep AS (
  SELECT COALESCE(
    (SELECT (elem->>'ReputationAmount')::int
       FROM ctx, jsonb_array_elements(ctx.arr) elem
      WHERE elem->'Faction'->>'Name' = :'faction_name'),
    0) AS r
),
new_value AS (
  SELECT LEAST(GREATEST((SELECT r FROM current_rep) + (:amount)::int, 0), 12475) AS v
),
new_arr AS (
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM ctx, jsonb_array_elements(ctx.arr) elem
     WHERE elem->'Faction'->>'Name' = :'faction_name'
  ) THEN
    (SELECT jsonb_agg(
      CASE WHEN elem->'Faction'->>'Name' = :'faction_name' THEN
        jsonb_set(
          jsonb_set(elem, '{ReputationAmount}', to_jsonb((SELECT v FROM new_value))),
          '{timestamp}', to_jsonb(EXTRACT(EPOCH FROM NOW())))
      ELSE elem END)
     FROM ctx, jsonb_array_elements(ctx.arr) elem)
  ELSE
    COALESCE((SELECT arr FROM ctx), '[]'::jsonb) ||
    jsonb_build_array(jsonb_build_object(
      'Faction', jsonb_build_object('Name', (:'faction_name')::text),
      'timestamp', EXTRACT(EPOCH FROM NOW()),
      'ReputationAmount', (SELECT v FROM new_value)))
  END AS arr_v
)
UPDATE dune.actors
   SET properties = jsonb_set(properties,
                              '{FactionPlayerComponent,m_FactionDataArray}',
                              (SELECT arr_v FROM new_arr))
 WHERE id = (SELECT ctrl_id FROM ctx)
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Proc call reads the JUST-WRITTEN JSONB value -> table + tier-tag sync.
SELECT dune.set_player_faction_reputation(
         :controller_id::bigint,
         (:faction_id)::smallint,
         COALESCE((
           SELECT (elem->>'ReputationAmount')::int
             FROM dune.actors a,
                  jsonb_array_elements(a.properties->'FactionPlayerComponent'->'m_FactionDataArray') elem
            WHERE a.id = :controller_id
              AND elem->'Faction'->>'Name' = :'faction_name'), 0)::integer)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
  fi
}

# =============================================================================
# P3a — icehunter v0.5.x parity helpers + new builders.
# Covers G23 main_quest_unlock, G24a grant_full_job_tree, G24b grant_skill_block,
# G25a reset_full_skill_area, G25b set_starter_class (UI disabled — see notice
# at builder header), g7b align_faction (standalone alignment-only grant).
#
# Cross-cutting:
#   * Every Funcom-proc-calling builder emits funcom_proc_call_prelude() as the
#     first SQL line (SET LOCAL search_path TO dune, public). Required because
#     Funcom procs use unqualified table refs — see
#.
#   * G24a/G24b/G25a/G25b touch FLevelComponent JSONB on the pawn actor —
#     RAM-fragile, gated offline via requires_offline().
#   * G23 main_quest_unlock is gated offline too: the Funcom proc
#     complete_journey_story_nodes_for_player raises EXCEPTION if the player
#     is online (spec §G23 lines 191-200).
#   * align_faction is TABULAR (player_faction + update_player_tags) — NOT in
#     requires_offline().
#   * tags-data.json sidecar (loaded via $TAGS_DATA_JSON) is the single source
#     of truth for journey-node tag unions + job module lists; pinned to
#     icehunter commit 9ef5a6c.
# =============================================================================

# Shared helper: emit the 8 alignment-tag union as a compact JSON array string.
# Used by build_faction_rep_grant (P3a G7 extension) and build_align_faction_grant
# (g7b standalone). Tag set is icehunter db.go:2210-2219 + spec §Faction.
emit_alignment_sql() {
  local faction_name="$1"
  local dialogue_flag aligned_flag met_recruiter_flag faction_unlocked recruitment_done
  case "$faction_name" in
    Atreides)
      dialogue_flag="DialogueFlags.Factions.SentToMeetHawat"
      aligned_flag="DialogueFlags.Factions.AlignedAtreides"
      met_recruiter_flag="DialogueFlags.Factions.MetHawat"
      faction_unlocked="Contract.Tracking.AtreidesFactionUnlocked"
      recruitment_done="Contract.Tracking.AtreidesRecruitmentCompleted"
      ;;
    Harkonnen)
      dialogue_flag="DialogueFlags.Factions.SentToPiterDeVries"
      aligned_flag="DialogueFlags.Factions.AlignedHarkonnen"
      met_recruiter_flag="DialogueFlags.Factions.MetPiterDeVries"
      faction_unlocked="Contract.Tracking.HarkonnenFactionUnlocked"
      recruitment_done="Contract.Tracking.HarkonnenRecruitmentCompleted"
      ;;
    *) printf '[]'; return 0 ;;
  esac
  jq -nc --arg a "$aligned_flag" --arg d "$dialogue_flag" \
         --arg m "$met_recruiter_flag" --arg fu "$faction_unlocked" \
         --arg rc "$recruitment_done" \
    '[$a, $d, $m, $fu, $rc,
      "DialogueFlags.Factions.FactionIntro",
      "DialogueFlags.Factions.FactionRank1",
      "DialogueFlags.Factions.FactionRank3",
      "DialogueFlags.Factions.MetARecruiter"]
     | unique'
}

# G23 — main_quest_unlock. Complete every node in ONE journey arc
# (proc complete_journey_story_nodes_for_player) + apply the union of
# m_TagsToAdd those nodes would emit (proc update_player_tags).
# Offline-gated upstream (proc raises EXCEPTION if player online). Idempotent
# via _grant_gate WHERE EXISTS; both procs are server-side idempotent too.
#
# Per-arc picker (2026-06-11): the preset is any top-level journey arc root —
# the 18 .core arcs (DA_MQ_*/DA_SQ_*/DA_Dunipedia_*/DA_DLC_LostHarvest) plus the
# Atreides faction arc DA_FQ_ClimbTheRanks. The relay-side enum
# (_MAIN_QUEST_PRESETS / catalog entries) is the authoritative allowlist; the
# box resolves node_ids from the comprehensive journey_node_completion_nodes
# set and validates the arc resolves >0 nodes.
#   node_ids ← .core prefix slice (the journey UI needs the parent/container
#              nodes; the sparse journey_node_tags set under-completes arcs).
#              DA_FQ_ClimbTheRanks comes from .faction.atreides (.core has no
#              DA_FQ), is Atreides-only, and reuses the journey faction gate.
#   tag_union ← journey_node_tags prefix slice (unchanged). Tags attach only to
#              milestone nodes, so SQ/Dunipedia arcs resolve to [] (a no-op);
#              node completion alone marks them complete.
build_main_quest_unlock_grant() {
  local preset
  preset=$(jq_get_nested detail preset)
  [[ "$preset" =~ ^DA_[A-Za-z0-9_]+$ ]] \
    || fail_json "invalid preset for main_quest_unlock: $preset" 2

  jq -e 'has("journey_node_completion_nodes")
         and (.journey_node_completion_nodes|has("core"))' \
    "$TAGS_DATA_JSON" >/dev/null 2>&1 \
    || fail_json "tags-data.json missing journey_node_completion_nodes bucket (game-box stage not yet deployed)" 3

  # FLS id (= encrypted_accounts."user", per icehunter db.go:36 + existing
  # Last Sietch patterns). The Funcom proc takes a text FLS id, NOT account_id.
  local fls_id
  fls_id=$(psql_scalar \
    "SELECT \"user\" FROM dune.encrypted_accounts WHERE id = :account_id;" \
    -v "account_id=${account_id}")
  [[ -n "$fls_id" ]] || fail_json "no FLS id for account ${account_id}" 5

  # Node ids: comprehensive .core prefix slice; the Atreides faction arc lives
  # in .faction.atreides instead (target-faction-gated).
  local node_ids_json faction_label="none"
  if [[ "$preset" == "DA_FQ_ClimbTheRanks" ]]; then
    local target_faction_id
    target_faction_id=$(psql_scalar \
      "SELECT pf.faction_id \
         FROM dune.player_faction pf \
         JOIN dune.encrypted_player_state eps \
           ON eps.player_controller_id = pf.actor_id \
        WHERE eps.account_id = :account_id;" \
      -v "account_id=${account_id}")
    case "$target_faction_id" in
      1)
        faction_label="atreides"
        node_ids_json=$(jq -c --arg p "$preset" \
          '[.journey_node_completion_nodes.faction.atreides[]
            | select(. == $p or startswith($p + "."))]' \
          "$TAGS_DATA_JSON")
        ;;
      2)
        fail_json "Harkonnen story set not yet captured — DA_FQ_ClimbTheRanks unavailable for Harkonnen targets" 2
        ;;
      *)
        fail_json "faction story arc is only available for Atreides characters (target faction_id: ${target_faction_id:-unaligned})" 2
        ;;
    esac
  else
    node_ids_json=$(jq -c --arg p "$preset" \
      '[.journey_node_completion_nodes.core[]
        | select(. == $p or startswith($p + "."))]' \
      "$TAGS_DATA_JSON")
  fi

  # Empty-result guard: an arc that resolves to 0 nodes (e.g. the retired
  # DA_MQ_TheBloodline phantom, whose nodes actually live under
  # DA_MQ_TheGreatConvention.TheBloodline.*) would be a silent no-op stamped
  # "applied". Reject it rather than report a false success.
  local node_count
  node_count=$(jq 'length' <<<"$node_ids_json")
  [[ "$node_count" =~ ^[0-9]+$ && "$node_count" -gt 0 ]] \
    || fail_json "arc '$preset' resolves to 0 journey nodes (not a top-level arc?)" 2

  local tag_union_json
  tag_union_json=$(jq -c --arg p "$preset" \
    '[(.journey_node_tags | to_entries[]
       | select(.key == $p or (.key|startswith($p + ".")))
       | .value[])] | unique' \
    "$TAGS_DATA_JSON")

  DETAIL_JSON=$(jq -nc --arg p "$preset" --arg fl "$faction_label" \
                       --argjson n "$node_ids_json" --argjson t "$tag_union_json" \
    '{preset:$p, faction:$fl, node_count:($n|length), tag_count:($t|length)}')

  PSQL_VARS+=(
    -v "fls_id=${fls_id}"
    -v "node_ids=${node_ids_json}"
    -v "tag_union=${tag_union_json}"
  )

  GRANT_PREFLIGHT=""
  local _prelude
  _prelude=$(funcom_proc_call_prelude)
  GRANT_BODY=$(cat <<EOF
-- G23 write: cascade journey-node completion + apply union of m_TagsToAdd.
-- Both procs run as TOP-LEVEL statements gated on _grant_gate.is_new so a
-- replay is a no-op. Do NOT wrap the proc calls in unreferenced CTEs: Postgres
-- prunes an unreferenced SELECT-func CTE, so the procs silently never run
-- (the 2026-07-23 main_quest_unlock no-op bug). Mirror G12's top-level pattern.
${_prelude}

-- Complete every node in the arc via Funcom's own proc.
SELECT dune.complete_journey_story_nodes_for_player(
         :'fls_id'::text,
         (SELECT COALESCE(array_agg(t), ARRAY[]::text[])
            FROM jsonb_array_elements_text(:'node_ids'::jsonb) t))
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Apply the union of m_TagsToAdd those nodes would emit.
SELECT dune.update_player_tags(
         :account_id::bigint,
         (SELECT COALESCE(array_agg(t), ARRAY[]::text[])
            FROM jsonb_array_elements_text(:'tag_union'::jsonb) t),
         ARRAY[]::text[])
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G24a — grant_full_job_tree. Unlock all 6 Skills.Key.* blocks (3 tier + 3
# capstone) for one job by direct JSONB write on FLevelComponent[1].ModuleData.
# RAM-fragile, offline-gated upstream. Uses a plpgsql DO block because
# Postgres does not support bracket-subscript multi-row jsonb_set via JOIN
# (spec §G24a NOTE lines 565-578).
build_grant_full_job_tree_grant() {
  local job
  job=$(jq_get_nested detail job)
  case "$job" in
    BeneGesserit|Mentat|Planetologist|Swordmaster|Trooper) ;;
    *) fail_json "invalid job for grant_full_job_tree: $job" 2 ;;
  esac

  # Inherit the caller's pawn instead of re-resolving it. A bare account_id
  # lookup is multi-slot-unsafe: encrypted_player_state has a row per character
  # SLOT and psql_scalar strips whitespace, so two slots CONCATENATE into a
  # garbage id rather than failing. actor_id came from RESOLVE_TARGET_SQL.
  local pawn_id="$actor_id"
  [[ -n "$pawn_id" && "$pawn_id" != "0" ]] \
    || fail_json "no pawn for account ${account_id}" 5

  local blocks_json
  blocks_json=$(jq -c --arg j "$job" \
    '.job_skill_blocks[$j] // []' "$TAGS_DATA_JSON")
  [[ "$blocks_json" != "[]" ]] \
    || fail_json "no blocks defined in tags-data.json for job ${job}" 2

  DETAIL_JSON=$(jq -nc --arg j "$job" --argjson b "$blocks_json" \
    '{job:$j, block_count:($b|length), blocks:$b}')

  PSQL_VARS+=( -v "pawn_id=${pawn_id}" -v "blocks=${blocks_json}" )

  GRANT_PREFLIGHT=""
  # plpgsql DO block: loop blocks one-at-a-time, jsonb_set each. The
  # `< 1` predicate preserves any SP the player already spent (skip set blocks).
  # psql `:` variable substitution does NOT happen inside $$...$$ dollar-quoted
  # blocks — so we hand the blocks JSON in via SET LOCAL above the DO and read
  # it back inside with current_setting(). Transaction-scoped so it auto-clears
  # on COMMIT.
  GRANT_BODY=$(cat <<'EOF'
SET LOCAL ls.blocks_json = :'blocks';
SET LOCAL ls.pawn_id = :'pawn_id';
DO $$
DECLARE
  v_pawn_id bigint := current_setting('ls.pawn_id')::bigint;
  v_entity_id bigint;
  v_block text;
  v_key text;
  v_blocks text[] := ARRAY(SELECT jsonb_array_elements_text(current_setting('ls.blocks_json')::jsonb));
BEGIN
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN
    RETURN;
  END IF;
  SELECT entity_id INTO v_entity_id
    FROM dune.actor_fgl_entities
   WHERE actor_id = v_pawn_id AND slot_name = 'DuneCharacter';
  IF v_entity_id IS NULL THEN
    RAISE EXCEPTION 'no DuneCharacter entity for pawn %', v_pawn_id;
  END IF;
  FOREACH v_block IN ARRAY v_blocks LOOP
    v_key := '(TagName="' || v_block || '")';
    UPDATE dune.fgl_entities
       SET components = jsonb_set(
             components,
             ARRAY['FLevelComponent','1','ModuleData', v_key],
             '{"SkillPointsSpent": 1}'::jsonb,
             true)
     WHERE entity_id = v_entity_id
       AND COALESCE(
             (components->'FLevelComponent'->1->'ModuleData'
               ->v_key->>'SkillPointsSpent')::int,
             0) < 1;
  END LOOP;
END$$;
EOF
)
}

# G24b — grant_skill_block. Single-block surgical version of G24a. Same
# FLevelComponent path, same `< 1` predicate. CapstoneWeaponry placeholders
# at SpSpent=0 get promoted to 1 (icehunter v0.5.1 fix).
build_grant_skill_block_grant() {
  local block
  block=$(jq_get_nested detail block)
  # Static format gate (matches the icehunter Skills.Key.* family).
  [[ "$block" =~ ^Skills\.Key\.[A-Za-z0-9_]+$ ]] \
    || fail_json "invalid block (must match Skills.Key.<id>): $block" 2
  # Membership gate against tags-data.json — block must be in some job's
  # job_skill_blocks (30 valid values across 5 jobs).
  local valid
  valid=$(jq -r --arg b "$block" \
    '[.job_skill_blocks | to_entries[] | .value[]] | any(. == $b)' \
    "$TAGS_DATA_JSON")
  [[ "$valid" == "true" ]] \
    || fail_json "invalid block (not in any job_skill_blocks): $block" 2

  # Inherit the caller's pawn instead of re-resolving it. A bare account_id
  # lookup is multi-slot-unsafe: encrypted_player_state has a row per character
  # SLOT and psql_scalar strips whitespace, so two slots CONCATENATE into a
  # garbage id rather than failing. actor_id came from RESOLVE_TARGET_SQL.
  local pawn_id="$actor_id"
  [[ -n "$pawn_id" && "$pawn_id" != "0" ]] \
    || fail_json "no pawn for account ${account_id}" 5

  DETAIL_JSON=$(jq -nc --arg b "$block" '{block:$b}')
  PSQL_VARS+=( -v "pawn_id=${pawn_id}" -v "block=${block}" )

  GRANT_PREFLIGHT=""
  # psql does NOT substitute :vars inside DO $$ ... $$ dollar-quoted blocks.
  # Fix: rewrite as a CTE-based plain UPDATE. :'block' and :pawn_id are
  # substituted in the outer SELECT/CTE context where psql variable interpolation
  # works normally. v_key CTE computes the ModuleData key string once so it is
  # reused in both the ARRAY path and the COALESCE guard without repetition.
  GRANT_BODY=$(cat <<'EOF'
-- G24b write: set SkillPointsSpent=1 for the named block in ModuleData.
-- Gated on _grant_gate.is_new; skips rows already at >= 1 (idempotent).
WITH gate AS (SELECT 1 FROM _grant_gate WHERE is_new),
entity AS (
  SELECT entity_id FROM dune.actor_fgl_entities
   WHERE actor_id = :pawn_id::bigint AND slot_name = 'DuneCharacter'
),
vkey AS (
  SELECT ('(TagName="' || :'block' || '")')::text AS k
)
UPDATE dune.fgl_entities fe
   SET components = jsonb_set(
         components,
         ARRAY['FLevelComponent', '1', 'ModuleData', vkey.k],
         '{"SkillPointsSpent": 1}'::jsonb,
         true)
  FROM entity, vkey
 WHERE fe.entity_id = entity.entity_id
   AND EXISTS (SELECT 1 FROM gate)
   AND COALESCE(
         (fe.components->'FLevelComponent'->1->'ModuleData'
           ->vkey.k->>'SkillPointsSpent')::int,
         0) < 1;
EOF
)
}

# G25a — reset_full_skill_area. Drop every ModuleData entry whose SkillArea
# matches the named job (Key + Ability + Attribute + Perk). Eliminates phantom
# refundable rows that survive a Key-only delete.
#
# HAZARD GUARD: refuses to reset the player's currently-set starter class.
# The combo (reset starter job) wipes TotalXPEarned + SP + Keystone on next
# login (actor 31 case, 2026-05-24). Operator must reassign starter class via
# G25b first (UI disabled — SQL-only recovery path).
build_reset_full_skill_area_grant() {
  local job
  job=$(jq_get_nested detail job)
  case "$job" in
    BeneGesserit|Mentat|Planetologist|Swordmaster|Trooper) ;;
    *) fail_json "invalid job for reset_full_skill_area: $job" 2 ;;
  esac

  # Inherit the caller's pawn instead of re-resolving it. A bare account_id
  # lookup is multi-slot-unsafe: encrypted_player_state has a row per character
  # SLOT and psql_scalar strips whitespace, so two slots CONCATENATE into a
  # garbage id rather than failing. actor_id came from RESOLVE_TARGET_SQL.
  local pawn_id="$actor_id"
  [[ -n "$pawn_id" && "$pawn_id" != "0" ]] \
    || fail_json "no pawn for account ${account_id}" 5

  # Starter-class hazard guard. Reads StarterSkillTreeTag.TagName off the
  # pawn's FLevelComponent[1] and refuses if it matches Skills.Key.<job>1.
  local current_starter
  current_starter=$(psql_scalar "$(cat <<'SQL'
SELECT COALESCE(
  fe.components->'FLevelComponent'->1->'StarterSkillTreeTag'->>'TagName', '')
FROM dune.fgl_entities fe
JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
WHERE afe.actor_id = :pawn_id::bigint AND afe.slot_name = 'DuneCharacter';
SQL
)" -v "pawn_id=${pawn_id}")

  if [[ "$current_starter" == "Skills.Key.${job}1" ]]; then
    fail_json "refusing to reset ${job} — it is the player's current starter class (StarterSkillTreeTag=${current_starter}). Reassign starter class via set_starter_class (SQL-only) first, then retry." 6
  fi

  local modules_json
  modules_json=$(jq -c --arg j "$job" \
    '.job_all_modules[$j] // []' "$TAGS_DATA_JSON")
  [[ "$modules_json" != "[]" ]] \
    || fail_json "no modules in tags-data.json for job ${job}" 2

  DETAIL_JSON=$(jq -nc --arg j "$job" --argjson m "$modules_json" \
    '{job:$j, module_count:($m|length)}')

  PSQL_VARS+=( -v "pawn_id=${pawn_id}" -v "modules=${modules_json}" )

  GRANT_PREFLIGHT=""
  # Single UPDATE with the `jsonb - text[]` operator. Removing keys that don't
  # exist is a no-op (already idempotent at the operator level); the
  # _grant_gate WHERE EXISTS prevents the catalog-side replay.
  GRANT_BODY=$(cat <<'EOF'
WITH gate AS (SELECT 1 FROM _grant_gate WHERE is_new),
  entity AS (
    SELECT entity_id FROM dune.actor_fgl_entities
     WHERE actor_id = :pawn_id::bigint AND slot_name = 'DuneCharacter'
  ),
  module_keys AS (
    SELECT COALESCE(array_agg('(TagName="' || k || '")'), ARRAY[]::text[]) AS keys
      FROM jsonb_array_elements_text(:'modules'::jsonb) AS k
  )
UPDATE dune.fgl_entities fe
   SET components = jsonb_set(
         fe.components,
         ARRAY['FLevelComponent','1','ModuleData'],
         (fe.components->'FLevelComponent'->1->'ModuleData')
           - (SELECT keys FROM module_keys))
  FROM gate, entity
 WHERE fe.entity_id = entity.entity_id;
EOF
)
}

# G25b — set_starter_class. **UI DISABLED** — backend plumbed for SQL-level
# recovery only. Actor 31 observed full-wipe on next login (TotalXPEarned + SP
# + Keystone -> 0) after a Set Starter -> Trooper click. Until empirical alt
# validation rules out the wipe, this builder MUST NOT be invoked from the
# admin UI; routes that allow it stamp `via_disabled_ui: true` in audit detail
# (spec §G25b line 1192-1194). See spec §G25b lines 1006-1029 for re-enable
# conditions.
#
# Mirrors icehunter cmdSetStarterClass (db.go:1690-1776). Single chained
# jsonb_set: strip old starter keys, write new tag, activate new starter
# block, grant new starter ability. The `-` operator on an empty text[] is a
# no-op so it's safe when there's no old starter (fresh character).
build_set_starter_class_grant() {
  local job
  job=$(jq_get_nested detail job)
  case "$job" in
    BeneGesserit|Mentat|Planetologist|Swordmaster|Trooper) ;;
    *) fail_json "invalid job for set_starter_class: $job" 2 ;;
  esac

  # Starter-ability map. icehunter db.go:1672-1678 is code-only (not in
  # tags-data.json). 5 entries; add a 6th here if Funcom ever ships another
  # class.
  local new_ability
  case "$job" in
    BeneGesserit)  new_ability="Skills.Ability.VoiceCompel" ;;
    Mentat)        new_ability="Skills.Ability.PoisonCapsuleLauncher" ;;
    Planetologist) new_ability="Skills.Ability.SuspensorPad" ;;
    Swordmaster)   new_ability="Skills.Ability.DeflectionSlow" ;;
    Trooper)       new_ability="Skills.Ability.SuspensorGrenade_Reduction" ;;
  esac

  # Inherit the caller's pawn instead of re-resolving it. A bare account_id
  # lookup is multi-slot-unsafe: encrypted_player_state has a row per character
  # SLOT and psql_scalar strips whitespace, so two slots CONCATENATE into a
  # garbage id rather than failing. actor_id came from RESOLVE_TARGET_SQL.
  local pawn_id="$actor_id"
  [[ -n "$pawn_id" && "$pawn_id" != "0" ]] \
    || fail_json "no pawn for account ${account_id}" 5

  # Carry a via_disabled_ui flag from the inbound detail (the admin-backend
  # route stamps it when invoking through the disabled-UI back door). We do
  # not gate the builder on it — that gate is the admin-backend's job — but
  # we preserve it on the audit row so the disabled-UI invocations are
  # grep-able after the fact.
  local via_disabled_ui
  via_disabled_ui=$(jq_get_nested detail via_disabled_ui)
  [[ -n "$via_disabled_ui" ]] || via_disabled_ui="false"

  DETAIL_JSON=$(jq -nc --arg j "$job" --arg a "$new_ability" \
                       --argjson v "$via_disabled_ui" \
    '{job:$j, starter_ability:$a, via_disabled_ui:$v}')

  PSQL_VARS+=(
    -v "pawn_id=${pawn_id}"
    -v "new_job=${job}"
    -v "new_ability=${new_ability}"
  )

  GRANT_PREFLIGHT=""
  GRANT_BODY=$(cat <<'EOF'
DO $$
DECLARE
  v_entity_id bigint;
  v_old_starter text;
  v_old_job text;
  v_old_ability text;
  v_keys_to_remove text[] := ARRAY[]::text[];
  v_new_starter_tag text := 'Skills.Key.' || :'new_job' || '1';
  v_new_starter_key text := '(TagName="' || v_new_starter_tag || '")';
  v_new_ability_key text := '(TagName="' || :'new_ability' || '")';
BEGIN
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN
    RETURN;
  END IF;
  SELECT afe.entity_id INTO v_entity_id
    FROM dune.actor_fgl_entities afe
   WHERE afe.actor_id = :pawn_id::bigint AND afe.slot_name = 'DuneCharacter';
  IF v_entity_id IS NULL THEN
    RAISE EXCEPTION 'no DuneCharacter entity for pawn %', :pawn_id;
  END IF;

  SELECT fe.components->'FLevelComponent'->1->'StarterSkillTreeTag'->>'TagName'
    INTO v_old_starter
    FROM dune.fgl_entities fe
   WHERE fe.entity_id = v_entity_id;

  IF v_old_starter LIKE 'Skills.Key.%1' THEN
    -- strip the leading "Skills.Key." (11 chars) and trailing "1" (1 char) to
    -- recover the old job name; substring is 1-indexed.
    v_old_job := substring(v_old_starter FROM 12 FOR length(v_old_starter) - 12);
    IF v_old_job <> :'new_job' THEN
      v_keys_to_remove := array_append(v_keys_to_remove,
        '(TagName="' || v_old_starter || '")');
      v_old_ability := CASE v_old_job
        WHEN 'BeneGesserit'  THEN 'Skills.Ability.VoiceCompel'
        WHEN 'Mentat'        THEN 'Skills.Ability.PoisonCapsuleLauncher'
        WHEN 'Planetologist' THEN 'Skills.Ability.SuspensorPad'
        WHEN 'Swordmaster'   THEN 'Skills.Ability.DeflectionSlow'
        WHEN 'Trooper'       THEN 'Skills.Ability.SuspensorGrenade_Reduction'
        ELSE NULL
      END;
      IF v_old_ability IS NOT NULL THEN
        v_keys_to_remove := array_append(v_keys_to_remove,
          '(TagName="' || v_old_ability || '")');
      END IF;
    END IF;
  END IF;

  UPDATE dune.fgl_entities fe
     SET components = jsonb_set(
           jsonb_set(
             jsonb_set(
               jsonb_set(
                 fe.components,
                 ARRAY['FLevelComponent','1','ModuleData'],
                 (fe.components->'FLevelComponent'->1->'ModuleData') - v_keys_to_remove),
               ARRAY['FLevelComponent','1','StarterSkillTreeTag','TagName'],
               to_jsonb(v_new_starter_tag::text)),
             ARRAY['FLevelComponent','1','ModuleData', v_new_starter_key],
             '{"SkillPointsSpent": 1}'::jsonb, true),
           ARRAY['FLevelComponent','1','ModuleData', v_new_ability_key],
           '{"SkillPointsSpent": 1}'::jsonb, true)
   WHERE fe.entity_id = v_entity_id;
END$$;
EOF
)
}

# g7b — align_faction (standalone). Pushes alignment ONLY (change_player_faction
# + the 8 alignment tags via update_player_tags). No rep bump, no tier tags.
# Tabular-online-safe. Use case: repair character where rep is at rank N but
# the alignment tags were missed (legacy G7 grants from before the P3a G7
# extension landed).
build_align_faction_grant() {
  local faction faction_id faction_name
  faction=$(jq_get_nested detail faction)
  case "$faction" in
    atreides|Atreides)   faction_id=1; faction_name="Atreides"  ;;
    harkonnen|Harkonnen) faction_id=2; faction_name="Harkonnen" ;;
    *) fail_json "invalid faction for align_faction (atreides|harkonnen): $faction" 2 ;;
  esac

  # NO local re-resolve here. This used to declare `local controller_id` and
  # re-read it with a bare account_id lookup, which SHADOWED the correctly
  # resolved outer value with a multi-slot-unsafe one: encrypted_player_state
  # has a row per character SLOT, psql_scalar strips whitespace, and two rows
  # therefore CONCATENATE into a garbage id (the same trap the resolver comment
  # describes for "OfflineOffline") rather than failing. Inherit the caller's
  # value, which came from RESOLVE_TARGET_SQL (joins player_state, newest login,
  # LIMIT 1).
  [[ -n "$controller_id" && "$controller_id" != "0" ]] \
    || fail_json "no controller for account ${account_id}" 5

  local tags_json
  tags_json=$(emit_alignment_sql "$faction_name")
  [[ "$tags_json" != "[]" ]] \
    || fail_json "could not build alignment tag set for faction: $faction_name" 2

  DETAIL_JSON=$(jq -nc --arg f "$faction_name" --argjson fi "$faction_id" \
                       --argjson t "$tags_json" \
    '{faction:$f, faction_id:$fi, tag_count:($t|length), tags:$t}')

  PSQL_VARS+=(
    -v "controller_id=${controller_id}"
    -v "faction_id=${faction_id}"
    -v "tags=${tags_json}"
  )

  GRANT_PREFLIGHT=""
  local _prelude
  _prelude=$(funcom_proc_call_prelude)
  GRANT_BODY=$(cat <<EOF
-- g7b: alignment-only. No rep changes, no tier-tag writes. Repair grant.
${_prelude}
SELECT dune.change_player_faction(
         :controller_id::bigint,
         (:faction_id)::smallint,
         3::smallint,
         NOW()::timestamp)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

SELECT dune.update_player_tags(
         :account_id::bigint,
         (SELECT array_agg(t)
            FROM jsonb_array_elements_text(:'tags'::jsonb) t),
         ARRAY[]::text[])
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# WP-C — journey/story full-unlock tag repair. Repairs the panel full-unlock
# tag gap (a panel-unlocked alt has ~68 journey tags vs a natural completer's
# 433). Writes the curated journey/story/contract tag set via the SAME proven
# online-safe tabular path align_faction uses: dune.update_player_tags(
# account_id, add[], remove[]). NO offline gate — update_player_tags is tabular
# (not RAM-fragile).
#
# Tag buckets live in the tags-data.json sidecar under journey_full_unlock_tags:
#   .core              — ~333 faction-neutral tags, ALWAYS written
#   .faction.atreides  — ~66 Atreides-path tags, only when target=Atreides AND
#                        include_faction_story=true
#   .faction.harkonnen — [] (snapshot not yet captured — Harkonnen+faction-story
#                        is rejected, never substituted)
#   .exploration_poi   — ~25 map-discovery tags, only when include_exploration_poi=true
#
# Faction is resolved from the LIVE target (dune.player_faction.faction_id;
# 1=Atreides 2=Harkonnen). This is the authoritative faction-gate — the UI note
# is advisory; this builder NEVER writes Atreides tags to a non-Atreides char.
#
# Idempotent: gated on _grant_gate.is_new so a replay never calls the proc, and
# update_player_tags itself no-ops on already-present tags.
build_journey_full_unlock_grant() {
  local include_faction_story include_exploration_poi
  include_faction_story=$(jq_get_nested detail include_faction_story)
  include_exploration_poi=$(jq_get_nested detail include_exploration_poi)
  [[ "$include_faction_story" == "true" ]] || include_faction_story="false"
  [[ "$include_exploration_poi" == "true" ]] || include_exploration_poi="false"

  # The sidecar key is OPTIONAL (validate_tags_data does not require it); fail
  # clearly here if the journey buckets were not deployed alongside this script.
  jq -e 'has("journey_full_unlock_tags")
         and (.journey_full_unlock_tags|has("core"))' \
    "$TAGS_DATA_JSON" >/dev/null 2>&1 \
    || fail_json "tags-data.json missing journey_full_unlock_tags bucket (game-box stage not yet deployed)" 3

  # CORE is always included.
  local tags_json
  tags_json=$(jq -c '.journey_full_unlock_tags.core' "$TAGS_DATA_JSON")
  [[ -n "$tags_json" && "$tags_json" != "null" ]] \
    || fail_json "journey_full_unlock_tags.core is empty or missing" 3

  # Faction-story bucket — resolved against the LIVE target faction.
  local faction_label="none"
  if [[ "$include_faction_story" == "true" ]]; then
    local target_faction_id
    target_faction_id=$(psql_scalar \
      "SELECT pf.faction_id \
         FROM dune.player_faction pf \
         JOIN dune.encrypted_player_state eps \
           ON eps.player_controller_id = pf.actor_id \
        WHERE eps.account_id = :account_id;" \
      -v "account_id=${account_id}")
    case "$target_faction_id" in
      1)
        faction_label="atreides"
        tags_json=$(jq -c \
          '(.journey_full_unlock_tags.core
            + .journey_full_unlock_tags.faction.atreides) | unique' \
          "$TAGS_DATA_JSON")
        ;;
      2)
        fail_json "Harkonnen story set not yet captured — include_faction_story unavailable for Harkonnen targets" 2
        ;;
      *)
        fail_json "faction story unlock is only available for Atreides characters (target faction_id: ${target_faction_id:-unaligned})" 2
        ;;
    esac
  fi

  # Optional map-discovery bucket (default off).
  if [[ "$include_exploration_poi" == "true" ]]; then
    tags_json=$(jq -nc --argjson cur "$tags_json" \
      --argjson poi "$(jq -c '.journey_full_unlock_tags.exploration_poi' "$TAGS_DATA_JSON")" \
      '($cur + $poi) | unique')
  fi

  DETAIL_JSON=$(jq -nc --arg fs "$include_faction_story" --arg ep "$include_exploration_poi" \
    --arg fl "$faction_label" --argjson t "$tags_json" \
    '{include_faction_story:($fs=="true"), include_exploration_poi:($ep=="true"),
      faction:$fl, tag_count:($t|length), tags:$t}')

  PSQL_VARS+=( -v "tags=${tags_json}" )

  GRANT_BODY=$(cat <<'EOF'
-- WP-C: journey/story full-unlock. Tabular, online-safe. CORE (+ optional
-- Atreides-path / exploration buckets, resolved in the bash builder against the
-- live target faction) written via Funcom's update_player_tags proc. Gated on
-- _grant_gate.is_new so a replay is a no-op; the proc itself ignores duplicate tags.
-- update_player_tags is LANGUAGE sql with unqualified player_tags refs, so it
-- needs the dune search_path (same prelude align_faction uses).
SET LOCAL search_path TO dune, public;
SELECT dune.update_player_tags(
         :account_id::bigint,
         (SELECT array_agg(t)
            FROM jsonb_array_elements_text(:'tags'::jsonb) t),
         ARRAY[]::text[])
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# WP-C2 — journey NODE completion pass. journey_full_unlock repairs the TAG
# side; this repairs the journey_story_node side so the in-game journey/mission
# UI shows the arcs complete. Calls Funcom's own
# dune.complete_journey_story_nodes_for_player(fls_id, node_ids[]) which
# INSERTs/updates journey_story_node rows (ON CONFLICT updates completion
# state) and RAISES if the player is online -> this grant type is in
# requires_offline (the panel sweeper applies it at logoff).
#
# Node buckets live in tags-data.json under journey_node_completion_nodes,
# snapshotted from the n0logic natural completer (1278 completed nodes):
#   .core              — 1150 nodes: DA_MQ_* / DA_SQ_* / DA_Dunipedia_* /
#                        DA_DLC_LostHarvest / NPE. Default-written.
#   .faction.atreides  — 65 DA_FQ_ClimbTheRanks.* nodes (the completer's
#                        Atreides path), only when target=Atreides AND
#                        include_faction_story=true
#   .faction.harkonnen — [] (not yet captured; rejected, never substituted)
# EXCLUDED from the snapshot entirely: DA_ACH_SteamAchievements.* (62 — never
# fake achievement progress) + DA_LDR_* (1 — term-bound Landsraad task node).
#
# Per-arc opt-in: include_main_quest (DA_MQ_*), include_side_quests (DA_SQ_*),
# include_dunipedia (DA_Dunipedia_*), include_dlc_lostharvest (DA_DLC_LostHarvest)
# each select their first-segment slice of .core; the selection is the union of
# the checked buckets. With NO arc bucket checked the entire .core is written
# (original behavior, back-compat). include_faction_story still adds the
# Atreides path on top, target-faction-gated.
#
# Idempotent: _grant_gate.is_new gates the call; the proc upserts.
build_journey_node_completion_grant() {
  local include_faction_story include_main_quest include_side_quests
  local include_dunipedia include_dlc_lostharvest
  include_faction_story=$(jq_get_nested detail include_faction_story)
  [[ "$include_faction_story" == "true" ]] || include_faction_story="false"
  include_main_quest=$(jq_get_nested detail include_main_quest)
  [[ "$include_main_quest" == "true" ]] || include_main_quest="false"
  include_side_quests=$(jq_get_nested detail include_side_quests)
  [[ "$include_side_quests" == "true" ]] || include_side_quests="false"
  include_dunipedia=$(jq_get_nested detail include_dunipedia)
  [[ "$include_dunipedia" == "true" ]] || include_dunipedia="false"
  include_dlc_lostharvest=$(jq_get_nested detail include_dlc_lostharvest)
  [[ "$include_dlc_lostharvest" == "true" ]] || include_dlc_lostharvest="false"

  jq -e 'has("journey_node_completion_nodes")
         and (.journey_node_completion_nodes|has("core"))' \
    "$TAGS_DATA_JSON" >/dev/null 2>&1 \
    || fail_json "tags-data.json missing journey_node_completion_nodes bucket (game-box stage not yet deployed)" 3

  # FLS id — the Funcom proc takes the text FLS id, not account_id.
  local fls_id
  fls_id=$(psql_scalar \
    "SELECT \"user\" FROM dune.encrypted_accounts WHERE id = :account_id;" \
    -v "account_id=${account_id}")
  [[ -n "$fls_id" ]] || fail_json "no FLS id for account ${account_id}" 5

  # Assemble the enabled arc-bucket prefixes. Each maps to one first-segment
  # root family in .core; a node joins the bucket when its id starts with the
  # prefix (same prefix-match rule as the G23 builder). Zero prefixes -> the
  # whole .core, preserving today's default.
  local prefixes=()
  [[ "$include_main_quest"      == "true" ]] && prefixes+=("DA_MQ_")
  [[ "$include_side_quests"     == "true" ]] && prefixes+=("DA_SQ_")
  [[ "$include_dunipedia"       == "true" ]] && prefixes+=("DA_Dunipedia_")
  [[ "$include_dlc_lostharvest" == "true" ]] && prefixes+=("DA_DLC_LostHarvest")

  local nodes_json
  if (( ${#prefixes[@]} == 0 )); then
    nodes_json=$(jq -c '.journey_node_completion_nodes.core' "$TAGS_DATA_JSON")
  else
    local prefixes_json
    prefixes_json=$(jq -nc '$ARGS.positional' --args "${prefixes[@]}")
    nodes_json=$(jq -c --argjson pf "$prefixes_json" \
      '[.journey_node_completion_nodes.core[]
        | . as $n | select(any($pf[]; . as $p | $n | startswith($p)))]' \
      "$TAGS_DATA_JSON")
  fi
  [[ -n "$nodes_json" && "$nodes_json" != "null" ]] \
    || fail_json "journey_node_completion_nodes.core is empty or missing" 3

  local faction_label="none"
  if [[ "$include_faction_story" == "true" ]]; then
    local target_faction_id
    target_faction_id=$(psql_scalar \
      "SELECT pf.faction_id \
         FROM dune.player_faction pf \
         JOIN dune.encrypted_player_state eps \
           ON eps.player_controller_id = pf.actor_id \
        WHERE eps.account_id = :account_id;" \
      -v "account_id=${account_id}")
    case "$target_faction_id" in
      1)
        faction_label="atreides"
        nodes_json=$(jq -c --argjson base "$nodes_json" \
          '($base + .journey_node_completion_nodes.faction.atreides) | unique' \
          "$TAGS_DATA_JSON")
        ;;
      2)
        fail_json "Harkonnen story set not yet captured — include_faction_story unavailable for Harkonnen targets" 2
        ;;
      *)
        fail_json "faction story unlock is only available for Atreides characters (target faction_id: ${target_faction_id:-unaligned})" 2
        ;;
    esac
  fi

  DETAIL_JSON=$(jq -nc --arg fs "$include_faction_story" --arg fl "$faction_label" \
    --arg mq "$include_main_quest" --arg sq "$include_side_quests" \
    --arg dp "$include_dunipedia" --arg dlc "$include_dlc_lostharvest" \
    --argjson n "$nodes_json" \
    '{include_faction_story:($fs=="true"),
      include_main_quest:($mq=="true"), include_side_quests:($sq=="true"),
      include_dunipedia:($dp=="true"), include_dlc_lostharvest:($dlc=="true"),
      faction:$fl, node_count:($n|length)}')

  PSQL_VARS+=(
    -v "fls_id=${fls_id}"
    -v "node_ids=${nodes_json}"
  )

  GRANT_BODY=$(cat <<'EOF'
-- WP-C2: journey-node completion pass. Funcom proc inserts/updates
-- journey_story_node rows (complete+reveal -> true); raises if player online
-- (which is why this grant type is offline-gated). Gated on _grant_gate.is_new
-- so a replay is a no-op; the proc upserts so partial overlap is safe.
SET LOCAL search_path TO dune, public;
SELECT dune.complete_journey_story_nodes_for_player(
         :'fls_id'::text,
         (SELECT COALESCE(array_agg(t), ARRAY[]::text[])
            FROM jsonb_array_elements_text(:'node_ids'::jsonb) t))
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# 2026-06-12 — spice_addiction_enable. Advances the GAS spice-addiction state
# machine to its post-Trial-4 steady state so the 3rd active-ability slot
# unlocks. This is the C++ quest-script side effect that completing the 4th
# Trial of Aql normally fires but the serialized journey/tag/item data does NOT
# carry — so a DB-side journey/tag grant (journey_full_unlock /
# journey_node_completion) leaves the addiction system stuck at AddictionDisabled
# and the slot locked. Diagnosed on a character whose third ability slot never
# unlocked despite the journey showing complete.
#
# Two surgical jsonb writes on the pawn's DuneCharacter FGL entity (same resolve
# path as build_grant_skill_block_grant / build_keystone_grant):
#   1. FSpiceAddictionComponent.SystemStatus -> "FullyEnabled". The sibling
#      SpiceVisionEnabledStatus subfield is LEFT UNTOUCHED — it is already
#      FullyEnabled on stuck characters (the red herring that fooled the prior
#      verification; icehunter's spiceVisionEnableSQL sets only that subfield and
#      is a no-op for this bug).
#   2. FGEPersistenceComponent.PersistenceData -> the 3 canonical progression GEs
#      (SpiceAbsorption + SpiceDecayRateModifier + ToleranceDecay), each Level
#      1.0 / StackCount 1 / DurationRemaining -1.0. Byte-identical across all
#      FullyEnabled players, so it is safe as a literal.
#
# Idempotent: skips the write if SystemStatus is already FullyEnabled (mirrors
# grant_skill_block's COALESCE(...) guard). Offline-required (RAM-fragile FGL
# write, same class as char_xp/keystone). Snapshots the pre-write component
# values into the audit detail for byte-exact rollback. No operator detail
# fields — the target player is the only input.
#
# NOTE (journey chaining): granting Trials-of-Aql / 4th-trial completion via
# journey_full_unlock or journey_node_completion ALONE reproduces this exact bug
# (journey/tag advances, GAS state machine does not). Chain spice_addiction_enable
# after any 4th-trial completion grant to close the gap. Auto-wiring is FLAGGED
# (not done) — see the catalog entry's help text and dune_grant.py.
SPICE_ADDICTION_PERSISTENCE_DATA='[{"Class":"/Game/Dune/Systems/SpiceAddiction/Effects/GE_SpiceAddiction_SpiceAbsorption.GE_SpiceAddiction_SpiceAbsorption_C","Level":1.0,"StackCount":1,"DurationRemaining":-1.0},{"Class":"/Game/Dune/Systems/SpiceAddiction/Effects/GE_SpiceAddiction_SpiceDecayRateModifier.GE_SpiceAddiction_SpiceDecayRateModifier_C","Level":1.0,"StackCount":1,"DurationRemaining":-1.0},{"Class":"/Game/Dune/Systems/SpiceAddiction/Effects/GE_SpiceAddiction_ToleranceDecay.GE_SpiceAddiction_ToleranceDecay_C","Level":1.0,"StackCount":1,"DurationRemaining":-1.0}]'

build_spice_addiction_enable_grant() {
  # Inherit the caller's pawn instead of re-resolving it. A bare account_id
  # lookup is multi-slot-unsafe: encrypted_player_state has a row per character
  # SLOT and psql_scalar strips whitespace, so two slots CONCATENATE into a
  # garbage id rather than failing. actor_id came from RESOLVE_TARGET_SQL.
  local pawn_id="$actor_id"
  [[ -n "$pawn_id" && "$pawn_id" != "0" ]] \
    || fail_json "no pawn for account ${account_id}" 5

  # Snapshot the current FSpiceAddictionComponent + FGEPersistenceComponent for
  # the audit row (rollback artifact). Resolve via the DuneCharacter entity, the
  # same path the write uses. If the entity is missing the snapshot is null —
  # the write CTE then matches no rows and the grant is a harmless no-op.
  local snapshot
  snapshot=$(psql_scalar "$(cat <<'SQL'
SELECT COALESCE(jsonb_build_object(
         'FSpiceAddictionComponent', fe.components->'FSpiceAddictionComponent',
         'FGEPersistenceComponent',  fe.components->'FGEPersistenceComponent'
       )::text, 'null')
  FROM dune.fgl_entities fe
  JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
 WHERE afe.actor_id = :pawn_id::bigint AND afe.slot_name = 'DuneCharacter';
SQL
)" -v "pawn_id=${pawn_id}")
  [[ -n "$snapshot" ]] || snapshot="null"

  # Current SystemStatus drives the idempotency note on the audit row (the SQL
  # body re-checks it under the row lock; this is advisory only).
  local cur_status
  cur_status=$(psql_scalar "$(cat <<'SQL'
SELECT COALESCE(
  fe.components#>>'{FSpiceAddictionComponent,1,SystemStatus}', '')
  FROM dune.fgl_entities fe
  JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
 WHERE afe.actor_id = :pawn_id::bigint AND afe.slot_name = 'DuneCharacter';
SQL
)" -v "pawn_id=${pawn_id}")

  DETAIL_JSON=$(jq -nc --arg ps "$cur_status" --argjson snap "$snapshot" \
    '{system_status_before:$ps,
      already_enabled:($ps=="FullyEnabled"),
      pre_state:$snap}')

  PSQL_VARS+=(
    -v "pawn_id=${pawn_id}"
    -v "persistence_data=${SPICE_ADDICTION_PERSISTENCE_DATA}"
  )

  GRANT_PREFLIGHT=""
  # Two jsonb_set writes in one statement chain, both inside the do_grant txn.
  # WRITE 1 sets SystemStatus, gated on the current value being below FullyEnabled
  # (COALESCE-guard idempotency: a replay or an already-enabled character writes
  # nothing). WRITE 2 replaces PersistenceData with the 3-GE literal; it is gated
  # on the same is_new + still-not-enabled predicate so the two writes move
  # together. The component array index is 1 for both (mirrors n0logic's shape
  # [0,{...SystemStatus...}] / [1,{...PersistenceData...}]; jsonb_set's path
  # element '1' addresses the object slot of that [index,{object}] pair).
  GRANT_BODY=$(cat <<'EOF'
-- spice_addiction_enable WRITE 1: FSpiceAddictionComponent.SystemStatus
-- AddictionDisabled -> FullyEnabled. SpiceVisionEnabledStatus is left as-is.
-- Idempotent: skips rows already at FullyEnabled.
WITH gate AS (SELECT 1 FROM _grant_gate WHERE is_new),
entity AS (
  SELECT entity_id FROM dune.actor_fgl_entities
   WHERE actor_id = :pawn_id::bigint AND slot_name = 'DuneCharacter'
)
UPDATE dune.fgl_entities fe
   SET components = jsonb_set(
         fe.components,
         '{FSpiceAddictionComponent,1,SystemStatus}',
         '"FullyEnabled"'::jsonb)
  FROM entity
 WHERE fe.entity_id = entity.entity_id
   AND EXISTS (SELECT 1 FROM gate)
   AND COALESCE(
         fe.components#>>'{FSpiceAddictionComponent,1,SystemStatus}', '')
       IS DISTINCT FROM 'FullyEnabled';

-- spice_addiction_enable WRITE 2: FGEPersistenceComponent.PersistenceData ->
-- the 3 canonical progression GEs. Same gate predicate as WRITE 1 (NEW grant
-- AND the character was not already enabled before this txn). The pre-write
-- SystemStatus is recaptured from the snapshot bound in DETAIL_JSON; here we
-- re-derive "was it already enabled" from the audit detail to keep both writes
-- on the same condition without depending on WRITE 1's row-update order.
WITH gate AS (SELECT 1 FROM _grant_gate WHERE is_new),
entity AS (
  SELECT entity_id FROM dune.actor_fgl_entities
   WHERE actor_id = :pawn_id::bigint AND slot_name = 'DuneCharacter'
)
UPDATE dune.fgl_entities fe
   SET components = jsonb_set(
         fe.components,
         '{FGEPersistenceComponent,1,PersistenceData}',
         :'persistence_data'::jsonb)
  FROM entity
 WHERE fe.entity_id = entity.entity_id
   AND EXISTS (SELECT 1 FROM gate)
   AND (:'detail'::jsonb ->> 'already_enabled') = 'false';
EOF
)
}

# G8 — specialization-track XP / level. Tabular upsert on
# dune.specialization_tracks keyed (player_id, track_type). add (default) or
# set. player_id is the pawn actor id. Online-safe.
# G8 v2 — specialization track XP / level. Routes through Funcom proc
# dune.set_specialization_xp_and_level(player_id, track, xp, level) so we
# inherit any Funcom-side hooks. Proc has SET semantics — for ADD mode we
# resolve current + delta + clamp inside the SQL.
#
# Caps from icehunter parity Action #2: xp <= 44182 per track, level <= 100.
#
# Actor fix: writes to player_controller_id (not pawn). Empirically the in-game
# spec data exists at the controller (verified 2026-05-23 on n0logic's
# Exploration track at actor 17 controller, not actor 19 pawn). Old G8 wrote
# to pawn — same bug class as old G7 faction_rep.
#
# search_path: Funcom's proc body uses unqualified `specialization_tracks` —
# SET LOCAL flips search_path for this transaction only (same workaround as G7).
build_spec_xp_grant() {
  local track_type xp level mode
  track_type=$(jq_get_nested detail track_type)
  xp=$(jq_get_nested detail xp);        [[ -n "$xp" ]] || xp="0"
  level=$(jq_get_nested detail level);  [[ -n "$level" ]] || level="0"
  mode=$(jq_get_nested detail mode);    [[ -n "$mode" ]] || mode="add"
  [[ "$track_type" =~ ^[A-Za-z0-9_]+$ ]] || fail_json "invalid track_type: $track_type" 2
  validate_int_in_range "$xp" 0 "$CAP_SPEC_XP" "xp"
  validate_int_in_range "$level" 0 "$CAP_SPEC_LEVEL" "level"
  validate_mode_addset "$mode"

  DETAIL_JSON=$(jq -nc --arg t "$track_type" --argjson x "$xp" \
    --argjson l "$level" --arg m "$mode" \
    '{track_type:$t,xp:$x,level:$l,mode:$m}')
  PSQL_VARS+=( -v "track_type=${track_type}" -v "xp=${xp}" -v "level=${level}" )

  if [[ "$mode" == "set" ]]; then
    GRANT_BODY=$(cat <<'EOF'
-- G8 v2 (set): pass clamped inputs straight to the proc.
-- The controller subquery is SCALAR, so it MUST return exactly one row. An
-- account with an empty second character slot has 2 encrypted_player_state
-- rows and the whole transaction aborts with "more than one row returned by a
-- subquery used as an expression". Join player_state to keep only the real
-- character (empty slots have no player_state row).
SET LOCAL search_path TO dune, public;
WITH target AS (
  SELECT eps.player_controller_id AS ctrl_id
    FROM dune.encrypted_player_state eps
    JOIN dune.player_state ps ON ps.player_controller_id = eps.player_controller_id
   WHERE eps.account_id = :account_id
   ORDER BY ps.last_login_time DESC NULLS LAST, ps.id DESC
   LIMIT 1
)
SELECT dune.set_specialization_xp_and_level(
         t.ctrl_id::bigint,
         (:'track_type')::dune.specializationtracktype,
         LEAST(GREATEST((:xp)::int, 0), 44182)::integer,
         LEAST(GREATEST((:level)::real, 0::real), 100.0)::real)
  FROM target t
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
  else
    GRANT_BODY=$(cat <<'EOF'
-- G8 v2 (add): resolve current from spec_tracks, add delta, clamp, pass to proc.
SET LOCAL search_path TO dune, public;
WITH ctx AS (
  -- One row only: the player_state join drops empty character slots, which
  -- would otherwise make the (SELECT ctrl_id FROM ctx) scalars below abort.
  SELECT eps.player_controller_id::bigint AS ctrl_id
    FROM dune.encrypted_player_state eps
    JOIN dune.player_state ps ON ps.player_controller_id = eps.player_controller_id
   WHERE eps.account_id = :account_id
   ORDER BY ps.last_login_time DESC NULLS LAST, ps.id DESC
   LIMIT 1
),
cur AS (
  SELECT COALESCE(st.xp_amount, 0) AS cur_xp,
         COALESCE(st.level, 0::real) AS cur_level
    FROM ctx
    LEFT JOIN dune.specialization_tracks st
      ON st.player_id = ctx.ctrl_id
     AND st.track_type = (:'track_type')::dune.specializationtracktype
),
new_vals AS (
  SELECT LEAST(GREATEST(cur_xp + (:xp)::int, 0), 44182)::integer AS new_xp,
         LEAST(GREATEST(cur_level + (:level)::real, 0::real), 100.0)::real AS new_level
    FROM cur
)
SELECT dune.set_specialization_xp_and_level(
         (SELECT ctrl_id FROM ctx),
         (:'track_type')::dune.specializationtracktype,
         (SELECT new_xp FROM new_vals),
         (SELECT new_level FROM new_vals))
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
  fi
}

# G9 — specialization keystone. Two writes:
#   1. INSERT into dune.purchased_specialization_keystones (player_id, keystone_id).
#   2. If the keystone has sp_bonus > 0 (catalog lookup), delta-credit
#      TotalSkillPoints + UnspentSkillPoints in the FLevelComponent JSONB by
#      the sp_bonus value. Delta-credit only — never recompute, never sum
#      ModuleData. Skips the SP write entirely when sp_bonus=0 (effect keystones).
# Idempotency: the FLevelComponent UPDATE is gated on the keystone INSERT
# actually inserting a new row (RETURNING from ON CONFLICT DO NOTHING returns
# zero rows on a replay); _grant_gate.is_new gates the INSERT itself.
# OFFLINE-REQUIRED: the FLevelComponent JSONB write is RAM-fragile per the same
# rules as G11 char_xp. requires_offline() refuses online targets.
# Catalog: dune.ls_keystone_catalog (OWNER dune), seeded from icehunter's
# keystones.go via the schema migration in dune-grant-schema.sql.
build_keystone_grant() {
  local keystone_id
  keystone_id=$(jq_get_nested detail keystone_id)
  [[ "$keystone_id" =~ ^[0-9]+$ ]] || fail_json "invalid keystone_id: $keystone_id" 2
  (( keystone_id >= 1 && keystone_id <= 205 )) \
    || fail_json "keystone_id must be 1..205: $keystone_id" 2

  DETAIL_JSON=$(jq -nc --argjson k "$keystone_id" '{keystone_id:$k}')
  PSQL_VARS+=( -v "keystone_id=${keystone_id}" )

  # Single CTE statement: tabular INSERT + conditional FLevelComponent SP credit.
  # Pattern mirrors G11 char_xp v2 — write only deltas, never recompute.
  GRANT_BODY=$(cat <<'EOF'
-- G9 write: tabular INSERT plus FLevelComponent SP delta-credit.
-- purchased_specialization_keystones is keyed by CONTROLLER id (matches the game
-- + icehunter); FLevelComponent SP lives on the PAWN's DuneCharacter entity.
WITH ids AS (
  SELECT player_controller_id AS ctrl_id, player_pawn_id AS pawn_id
    FROM dune.encrypted_player_state
   WHERE account_id = :account_id
),
inserted AS (
  INSERT INTO dune.purchased_specialization_keystones (player_id, keystone_id)
  SELECT ids.ctrl_id, :keystone_id
    FROM ids
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  ON CONFLICT (player_id, keystone_id) DO NOTHING
  RETURNING player_id
),
bonus AS (
  SELECT sp_bonus FROM dune.ls_keystone_catalog WHERE keystone_id = :keystone_id
),
target_entity AS (
  SELECT afe.entity_id, b.sp_bonus
    FROM ids
   CROSS JOIN bonus b
    JOIN dune.actor_fgl_entities afe
      ON afe.actor_id = ids.pawn_id AND afe.slot_name = 'DuneCharacter'
   WHERE b.sp_bonus > 0
     AND EXISTS (SELECT 1 FROM inserted)
)
UPDATE dune.fgl_entities fe
   SET components = jsonb_set(
         jsonb_set(
           components,
           '{FLevelComponent,1,TotalSkillPoints}',
           to_jsonb(
             COALESCE((components#>>'{FLevelComponent,1,TotalSkillPoints}')::int, 0)
             + t.sp_bonus)
         ),
         '{FLevelComponent,1,UnspentSkillPoints}',
         to_jsonb(
           COALESCE((components#>>'{FLevelComponent,1,UnspentSkillPoints}')::int, 0)
           + t.sp_bonus)
       )
  FROM target_entity t
 WHERE fe.entity_id = t.entity_id;
EOF
)
}

# G9-batch: bulk specialization keystone unlock. Two flavours share one body:
#   spec_unlock_track: all 41 keystones of one track.
#   spec_unlock_all:   all 205 keystones across the 5 tracks.
# Same two writes as build_keystone_grant, set-wise:
#   1. INSERT every target keystone_id ON CONFLICT DO NOTHING.
#   2. Delta-credit the SUMMED sp_bonus of the keystones that were ACTUALLY
#      inserted (not already owned) into FLevelComponent TotalSkillPoints +
#      UnspentSkillPoints. SP is credited per-newly-inserted-row, so a partial
#      re-grant credits only the delta and a full replay credits nothing.
# OFFLINE-REQUIRED (FLevelComponent JSONB is RAM-fragile, same as keystone/char_xp).
# Catalog: dune.ls_keystone_catalog (track + sp_bonus, OWNER dune).
#
# $1 = extra predicate on the catalog CTE ("AND k.track = :'track_type'") or "".
_emit_spec_unlock_body() {
  local track_pred="$1"
  # Unquoted heredoc so ${track_pred} expands. The SQL has no $ tokens of its
  # own (no dollar-quoted blocks); psql :vars are left for psql to bind.
  GRANT_BODY=$(cat <<EOF
-- G9-batch write: bulk INSERT plus FLevelComponent summed-SP delta-credit.
-- purchased_specialization_keystones keyed by CONTROLLER id; FLevelComponent SP on PAWN.
WITH target AS (
  -- MUST be one row. This CTE is CROSS JOINed against the 205-keystone catalog,
  -- so an account with an empty second character slot silently inserted 205
  -- keystones for BOTH controllers and then summed sp_bonus over all of them,
  -- double-crediting skill points to the one real pawn. The player_state join
  -- drops empty slots; LIMIT 1 keeps the CROSS JOIN single-target.
  SELECT eps.player_controller_id AS ctrl_id, eps.player_pawn_id AS pawn_id
    FROM dune.encrypted_player_state eps
    JOIN dune.player_state ps ON ps.player_controller_id = eps.player_controller_id
   WHERE eps.account_id = :account_id
   ORDER BY ps.last_login_time DESC NULLS LAST, ps.id DESC
   LIMIT 1
),
inserted AS (
  INSERT INTO dune.purchased_specialization_keystones (player_id, keystone_id)
  SELECT t.ctrl_id, k.keystone_id
    FROM target t
    CROSS JOIN dune.ls_keystone_catalog k
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
     ${track_pred}
  ON CONFLICT (player_id, keystone_id) DO NOTHING
  RETURNING keystone_id
),
sp AS (
  SELECT COALESCE(SUM(k.sp_bonus), 0) AS total_sp
    FROM inserted i
    JOIN dune.ls_keystone_catalog k ON k.keystone_id = i.keystone_id
),
target_entity AS (
  SELECT afe.entity_id, sp.total_sp
    FROM sp
   CROSS JOIN target t
    JOIN dune.actor_fgl_entities afe
      ON afe.actor_id = t.pawn_id AND afe.slot_name = 'DuneCharacter'
   WHERE sp.total_sp > 0
)
UPDATE dune.fgl_entities fe
   SET components = jsonb_set(
         jsonb_set(
           components,
           '{FLevelComponent,1,TotalSkillPoints}',
           to_jsonb(
             COALESCE((components#>>'{FLevelComponent,1,TotalSkillPoints}')::int, 0)
             + t.total_sp)
         ),
         '{FLevelComponent,1,UnspentSkillPoints}',
         to_jsonb(
           COALESCE((components#>>'{FLevelComponent,1,UnspentSkillPoints}')::int, 0)
           + t.total_sp)
       )
  FROM target_entity t
 WHERE fe.entity_id = t.entity_id;
EOF
)
}

build_spec_unlock_track_grant() {
  local track_type
  track_type=$(jq_get_nested detail track_type)
  case "$track_type" in
    Combat|Crafting|Exploration|Gathering|Sabotage) ;;
    *) fail_json "invalid track_type for spec_unlock_track (Combat|Crafting|Exploration|Gathering|Sabotage): $track_type" 2 ;;
  esac

  DETAIL_JSON=$(jq -nc --arg t "$track_type" '{track_type:$t,keystone_count:41}')
  PSQL_VARS+=( -v "track_type=${track_type}" )

  _emit_spec_unlock_body "AND k.track = :'track_type'"
}

build_spec_unlock_all_grant() {
  DETAIL_JSON='{"keystone_count":205}'
  _emit_spec_unlock_body ""
}

# G10 v2 — inventory item delivered LIVE via dune.landsraad_house_rewards. The
# row fires the landsraad_tasks_house_rewards_changed trigger -> pg_notify, which
# drives the in-game "Claim Rewards" prompt. ONLINE-SAFE: DB-backed, no offline
# gate. Operator selects a real active Landsraad house; the reward surfaces at
# that house's rep in-game. player_id = player_controller_id.
build_item_live_grant() {
  local template_id amount house_name
  template_id=$(jq_get_nested detail template_id)
  amount=$(jq_get_nested detail amount)
  house_name=$(jq_get_nested detail house_name)

  validate_template_id "$template_id"
  validate_int_in_range "$amount" 1 "$CAP_ITEM_LIVE_QTY" "amount"
  validate_active_landsraad_house "$house_name"

  DETAIL_JSON=$(jq -nc \
    --arg t "$template_id" --argjson a "$amount" --arg h "$house_name" \
    '{template_id:$t, amount:$a, house_name:$h}')

  PSQL_VARS+=( -v "template_id=${template_id}" \
               -v "amount=${amount}" \
               -v "house_name=${house_name}" )

  # Write: clear ONLY the matching prior unclaimed row (same player+house+item),
  # then queue the new claimable reward. Both statements are gated on a NEW grant
  # (WHERE EXISTS _grant_gate.is_new) so an idempotent replay writes nothing.
  # DELETE narrows to (player_id, house_name, template_id) to avoid clobbering
  # any other legitimate rewards the player holds from that house.
  GRANT_BODY=$(cat <<'EOF'
-- G10 v2 write: clear ONLY the matching prior unclaimed row (gated on a NEW grant).
DELETE FROM dune.landsraad_house_rewards
 WHERE player_id   = (SELECT player_controller_id
                        FROM dune.encrypted_player_state
                       WHERE account_id = :account_id)
   AND house_name  = :'house_name'
   AND template_id = :'template_id'
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- G10 v2 write: queue the claimable reward at the selected house's rep.
INSERT INTO dune.landsraad_house_rewards
  (player_id, house_name, amount, template_id, last_updated)
SELECT player_controller_id, :'house_name', :amount, :'template_id', NOW()
  FROM dune.encrypted_player_state
 WHERE account_id = :account_id
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G11 v2 — character XP + delta-credited skill points. jsonb_set additive on
# FLevelComponent[1] in dune.fgl_entities (DuneCharacter slot of the pawn).
#
# v1 (2026-05-23 PM) wrote ONLY TotalXPEarned and relied on Funcom's load path
# to recompute derived fields. Empirical test on n0logic (grant_id=10, +600
# XP, L26->L27) proved: NO wipe (UnspentSkillPoints preserved at 24 ✓) but
# also NO SP credit (Funcom's pod doesn't auto-grant SP for externally-added
# XP). HUD showed L27 but Skills UI still showed 24 unspent.
#
# v2 credits SP by delta from the observed pre-grant state — read current
# TotalSkillPoints and UnspentSkillPoints, compute levels_gained =
# ls_xp_to_level(new_xp) - ls_xp_to_level(pre_xp), increment both fields by
# that delta in the SAME transaction as the XP write. CANNOT trigger
# icehunter's wipe bug because we never sum ModuleData or guess starter
# freebie counts — we increment from the player's actual current state.
#
# Offline-gated (RAM clobbers DB writes while online). Capped at L200
# cumulative XP (344,440). Idempotent via _grant_gate. ack flag REQUIRED on
# first v2 grant (drop the gate once empirically validated per spec §8).
# Requires migration dune-grant-schema.sql (dune.ls_char_xp_curve table +
# dune.ls_xp_to_level(bigint) function).
build_char_xp_grant() {
  local amount ack target_level resolved_from
  amount=$(jq_get_nested detail amount)
  ack=$(jq_get_nested detail force_wipe_points_ack)
  target_level=$(jq_get_nested detail target_level)
  resolved_from="amount"

  if [[ -n "$target_level" && "$target_level" != "0" ]]; then
    # Level-target mode: resolve the cumulative XP for target_level from
    # dune.ls_char_xp_curve, read the character's current XP, and feed the
    # delta through the same dual write path as a raw-XP grant. XP is
    # monotonic, so reject any target at or below the current level.
    validate_int_in_range "$target_level" 1 200 "target_level"

    local target_xp pre_xp cur_level
    target_xp=$(psql_scalar \
      "SELECT cumulative_xp FROM dune.ls_char_xp_curve WHERE level = :target_level;" \
      -v "target_level=${target_level}")
    [[ "$target_xp" =~ ^[0-9]+$ ]] \
      || fail_json "no XP-curve entry for level ${target_level} (run dune-grant-schema.sql)" 3

    pre_xp=$(psql_scalar \
      "SELECT COALESCE((fe.components#>>'{FLevelComponent,1,TotalXPEarned}')::bigint, 0) \
         FROM dune.fgl_entities fe \
         JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id \
         JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = afe.actor_id \
        WHERE afe.slot_name = 'DuneCharacter' AND eps.account_id = :account_id;" \
      -v "account_id=${account_id}")
    [[ "$pre_xp" =~ ^[0-9]+$ ]] \
      || fail_json "could not resolve current XP for account ${account_id} (no DuneCharacter entity?)" 5

    cur_level=$(psql_scalar \
      "SELECT dune.ls_xp_to_level(:pre_xp::bigint);" \
      -v "pre_xp=${pre_xp}")
    [[ "$cur_level" =~ ^[0-9]+$ ]] || cur_level=0

    if (( target_level <= cur_level )); then
      fail_json "target_level ${target_level} is at or below the character's current level ${cur_level}; XP cannot de-level" 2
    fi

    amount=$(( target_xp - pre_xp ))
    resolved_from="target_level"
  fi

  validate_int_in_range "$amount" 1 "$CAP_CHAR_XP" "amount"

  # Ack flag is OPTIONAL — kept as a detail-JSON field for audit traceability
  # but no longer gates the grant. Validated empirically 2026-05-23 (grant_id
  # 10 = v1 minimal-write, grant_id 11 = v2 with delta SP credit): Funcom's
  # load path trusts the JSONB blob written by v2 and does NOT clobber
  # UnspentSkillPoints. v2 has shipped. The gate was removed in this commit.
  DETAIL_JSON=$(jq -nc --argjson a "$amount" --arg ack "$ack" \
    --arg src "$resolved_from" --arg tl "$target_level" \
    '{amount:$a, force_wipe_points_ack:($ack=="true"), resolved_from:$src}
     + (if $tl == "" or $tl == "0" then {} else {target_level:($tl|tonumber)} end)')
  PSQL_VARS+=( -v "amount=${amount}" )

  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G11 preflight: the account must resolve to a pawn actor that owns a
-- DuneCharacter fgl_entities slot with a populated FLevelComponent. Without
-- those, the write would silently no-op and the grant would land as 'applied'
-- with nothing actually changed.
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_entity_id bigint;
        v_has_lvl boolean;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;

  SELECT fe.entity_id, fe.components ? 'FLevelComponent'
    INTO v_entity_id, v_has_lvl
    FROM dune.fgl_entities fe
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = afe.actor_id
   WHERE eps.account_id = g.account_id
     AND afe.slot_name = 'DuneCharacter';

  IF v_entity_id IS NULL THEN
    RAISE EXCEPTION 'RESOLVE_FAIL: no DuneCharacter fgl entity for account %', g.account_id;
  END IF;
  IF NOT v_has_lvl THEN
    RAISE EXCEPTION 'RESOLVE_FAIL: DuneCharacter entity % has no FLevelComponent', v_entity_id;
  END IF;
END $$;
EOF
)

  GRANT_BODY=$(cat <<'EOF'
-- G11 v2 write: cap-aware additive jsonb_set on TotalXPEarned PLUS delta SP
-- credit to TotalSkillPoints and UnspentSkillPoints. The CTE reads pre-grant
-- state from the same row we're about to write, computes new_xp (capped) and
-- levels_gained, then writes all three fields in one jsonb_set chain.
-- CANNOT trigger icehunter's wipe: increments from observed values, never
-- recomputes spentSP from ModuleData. See CHAR-XP-GRANT-SPEC.md §3 / v2.
-- Gated on a NEW grant so a replay is a no-op.
WITH target AS (
  SELECT fe.entity_id,
         COALESCE((fe.components#>>'{FLevelComponent,1,TotalXPEarned}')::bigint, 0)     AS pre_xp,
         COALESCE((fe.components#>>'{FLevelComponent,1,TotalSkillPoints}')::int, 0)     AS pre_total_sp,
         COALESCE((fe.components#>>'{FLevelComponent,1,UnspentSkillPoints}')::int, 0)   AS pre_unspent_sp
    FROM dune.fgl_entities fe
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = afe.actor_id
   WHERE afe.slot_name = 'DuneCharacter'
     AND eps.account_id = :account_id
),
calc AS (
  SELECT entity_id, pre_xp, pre_total_sp, pre_unspent_sp,
         LEAST(pre_xp + :amount::bigint, 344440::bigint) AS new_xp,
         (dune.ls_xp_to_level(LEAST(pre_xp + :amount::bigint, 344440::bigint))
          - dune.ls_xp_to_level(pre_xp))                AS levels_gained
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
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- G11 v3 add-on: delta-credit intel (TechKnowledgePoints) on the player's
-- actor properties. Computes intel_delta = intel_at(new_level) - intel_at(
-- old_level) using dune.ls_intel_at_level. Delta-credit (NOT set) so any
-- intel the player earned/spent through gameplay is preserved. icehunter's
-- cmdAwardCharXP SETs to the cumulative value — would overwrite spent intel.
-- Gated on _grant_gate.is_new (replay-safe).
WITH player AS (
  SELECT a.id AS actor_id,
         COALESCE((a.properties#>>'{TechKnowledgePlayerComponent,m_TechKnowledgePoints}')::int, 0) AS cur_intel,
         (fe.components#>>'{FLevelComponent,1,TotalXPEarned}')::bigint AS post_xp
    FROM dune.encrypted_player_state eps
    JOIN dune.actors a ON a.id = eps.player_pawn_id
    JOIN dune.actor_fgl_entities afe ON afe.actor_id = eps.player_pawn_id AND afe.slot_name = 'DuneCharacter'
    JOIN dune.fgl_entities fe ON fe.entity_id = afe.entity_id
   WHERE eps.account_id = :account_id
),
delta AS (
  SELECT actor_id, cur_intel,
         GREATEST(0,
           dune.ls_intel_at_level(dune.ls_xp_to_level(post_xp)::int)
           - dune.ls_intel_at_level(dune.ls_xp_to_level(GREATEST(post_xp - :amount::bigint, 0))::int)
         ) AS intel_delta
    FROM player
)
UPDATE dune.actors a
   SET properties = jsonb_set(
         a.properties,
         '{TechKnowledgePlayerComponent,m_TechKnowledgePoints}',
         to_jsonb(delta.cur_intel + delta.intel_delta))
  FROM delta
 WHERE a.id = delta.actor_id
   AND a.properties ? 'TechKnowledgePlayerComponent'
   AND delta.intel_delta > 0
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G12 — progression preset (icehunter parity Action #4). Unbricks players who
# got admin-bumped past the normal Journey unlock chain (e.g. admin granted
# rank 17 Atreides but the in-game UI still says "advance through the faction
# journey to unlock Specializations/Landsraad"). Calls Funcom's own procs:
#
#   1. dune.complete_journey_story_nodes_for_player(fls_id, node_ids[])
#      - completes the 8 Rank5To20 ClimbTheRanks nodes that gate everything
#   2. dune.update_player_tags(account_id, add[], remove[])
#      - adds tier tags (Faction.<Name>.Tier0..N) + dialogue flag + optionally
#        Journey.LandsraadContractsUnlocked
#   3. (only for rank19_eligible) JSONB-write FactionPlayerComponent (G7 v2
#      pattern) + dune.set_player_faction_reputation, to land rep at rank-19
#      threshold so the in-game UI matches the tag state
#
# Presets are HARDCODED — never accept node lists or tag lists from the API
# (privilege-escalation-via-payload prevention).
#
# Verified pattern: applied to n0logic 2026-05-23, surfaced as Landsraad
# unlocked + Hunting Skorda revealed in-game. See.
#
# Faction.Name resolution via the hardcoded faction_id map (same as G7 v2).
# FLS ID = dune.accounts."user" (quoted — reserved word).
# Offline-gated — same RAM-fragility risk class as char_xp/faction_rep.
build_progression_preset_grant() {
  local faction preset faction_id faction_name dialogue_flag

  faction=$(jq_get_nested detail faction)
  preset=$(jq_get_nested detail preset)

  case "$faction" in
    atreides)
      faction_id=1; faction_name="Atreides"
      dialogue_flag="DialogueFlags.Factions.SentToMeetHawat"
      ;;
    harkonnen)
      faction_id=2; faction_name="Harkonnen"
      dialogue_flag="DialogueFlags.Factions.SentToPiterDeVries"
      ;;
    *) fail_json "detail.faction must be atreides or harkonnen (got: $faction)" 2 ;;
  esac

  case "$preset" in
    landsraad_unlock_only|ch3_start|rank19_eligible) ;;
    *) fail_json "detail.preset must be one of: landsraad_unlock_only, ch3_start, rank19_eligible (got: $preset)" 2 ;;
  esac

  DETAIL_JSON=$(jq -nc --arg f "$faction" --arg p "$preset" --arg n "$faction_name" --argjson fid "$faction_id" \
    '{faction:$f,preset:$p,faction_name:$n,faction_id:$fid}')
  PSQL_VARS+=( -v "faction_id=${faction_id}" -v "faction_name=${faction_name}"
               -v "dialogue_flag=${dialogue_flag}" -v "preset=${preset}" )

  # ClimbTheRanks journey nodes — icehunter v0.5.5 expanded set.
  #   - climbTheRanksNodes: 8 Rank5To20 onboarding nodes (faction-neutral baseline)
  #   - climbTheRanksStoryNodes: faction-neutral Ch2 storyline (HuntingSkorda,
  #     GatheringIntelligence, JoinAHouse, ClimbTheRanksR2)
  #   - climbTheRanksStoryNodesAtreides/Harkonnen: Ch2→Ch3 transition + Test of
  #     Loyalty(Treachery) + Investigations + PoisonedSpice
  #   - landsraadMissionNodesAtreides/Harkonnen: weekly Landsraad mission tree
  #     (rank19_eligible only)
  #
  # Both ch3_start AND rank19_eligible get climbTheRanks + Story + faction-specific
  # arcs. rank19_eligible adds Landsraad on top. Matches icehunter v0.5.5 fix
  # (ch3_start no longer truncates to just the 8 Rank5To20 nodes — characters
  # were stuck at "Test of Loyalty" rank 1 because rep stayed at zero).
  local CLIMB_RANKS_NODES="
    'DA_FQ_ClimbTheRanks.Rank5To20.MeetSponsor',
    'DA_FQ_ClimbTheRanks.Rank5To20.MeetSponsor.TalkToSponsor',
    'DA_FQ_ClimbTheRanks.Rank5To20.StartLandsraadOnboarding',
    'DA_FQ_ClimbTheRanks.Rank5To20.StartLandsraadOnboarding.ReportToMasterOfAssassins',
    'DA_FQ_ClimbTheRanks.Rank5To20.CompleteLandsraadMission',
    'DA_FQ_ClimbTheRanks.Rank5To20.CompleteLandsraadMission.CompleteOnboardingJourney1',
    'DA_FQ_ClimbTheRanks.Rank5To20.CraftAugmentation',
    'DA_FQ_ClimbTheRanks.Rank5To20.CraftAugmentation.CompleteOnboardingJourney2'"
  local CLIMB_RANKS_STORY_NODES="
    'DA_FQ_ClimbTheRanks.HuntingSkorda',
    'DA_FQ_ClimbTheRanks.HuntingSkorda.FindSkorda',
    'DA_FQ_ClimbTheRanks.HuntingSkorda.FindSkorda.SkordaInArrakeen',
    'DA_FQ_ClimbTheRanks.HuntingSkorda.FindSkorda.SkordaInMysaTarrill',
    'DA_FQ_ClimbTheRanks.HuntingSkorda.FindSkorda.SkordaInOodham',
    'DA_FQ_ClimbTheRanks.GatheringIntelligence',
    'DA_FQ_ClimbTheRanks.GatheringIntelligence.TrackDownContainer',
    'DA_FQ_ClimbTheRanks.GatheringIntelligence.TrackDownContainer.FindCanister',
    'DA_FQ_ClimbTheRanks.GatheringIntelligence.TrackDownContainer.InvestigateSandflies',
    'DA_FQ_ClimbTheRanks.GatheringIntelligence.TrackDownContainer.TrackDownPilot',
    'DA_FQ_ClimbTheRanks.GatheringIntelligence.TrackDownContainer.TrackDownRedScorpion',
    'DA_FQ_ClimbTheRanks.JoinAHouse',
    'DA_FQ_ClimbTheRanks.JoinAHouse.ProveYourself',
    'DA_FQ_ClimbTheRanks.JoinAHouse.ProveYourself.ChooseASide',
    'DA_FQ_ClimbTheRanks.JoinAHouse.ProveYourself.Rank1Contracts',
    'DA_FQ_ClimbTheRanks.JoinAHouse.StrikeADeal',
    'DA_FQ_ClimbTheRanks.JoinAHouse.StrikeADeal.FindTheSpy',
    'DA_FQ_ClimbTheRanks.JoinAHouse.StrikeADeal.GetSpyMission',
    'DA_FQ_ClimbTheRanks.JoinAHouse.StrikeADeal.TalkToARecruiter',
    'DA_FQ_ClimbTheRanks.ClimbTheRanksR2'"
  local CLIMB_RANKS_STORY_ATREIDES="
    'DA_FQ_ClimbTheRanks.TransitionToCh3_Atre',
    'DA_FQ_ClimbTheRanks.TransitionToCh3_Atre.TheCall',
    'DA_FQ_ClimbTheRanks.TransitionToCh3_Atre.TheCall.AnswerTheCall',
    'DA_FQ_ClimbTheRanks.ATestOfLoyalty',
    'DA_FQ_ClimbTheRanks.ATestOfLoyalty.GetMaximToBackOff',
    'DA_FQ_ClimbTheRanks.ATestOfLoyalty.GetMaximToBackOff.FindSemuta',
    'DA_FQ_ClimbTheRanks.InvestigateKytheria_Atreides',
    'DA_FQ_ClimbTheRanks.InvestigateKytheria_Atreides.InvestigateWreck_Atreides',
    'DA_FQ_ClimbTheRanks.InvestigateKytheria_Atreides.InvestigateWreck_Atreides.MeetAndreaGanan',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Atreides',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Atreides.DeviseAPlan_Atreides',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Atreides.DeviseAPlan_Atreides.TellThufirAboutDelphis',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Atreides.PledgeAllegiance_Atreides',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Atreides.PledgeAllegiance_Atreides.PledgeAllegiance_Atreides_Sub',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Atreides.SecureLastContainer_Atreides',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Atreides.SecureLastContainer_Atreides.RecoverSheolContainer_Atreides',
    'DA_FQ_ClimbTheRanks.PoisonedSpice_Atreides'"
  local CLIMB_RANKS_STORY_HARKONNEN="
    'DA_FQ_ClimbTheRanks.TransitionToCh3_Hark',
    'DA_FQ_ClimbTheRanks.TransitionToCh3_Hark.TheCall',
    'DA_FQ_ClimbTheRanks.TransitionToCh3_Hark.TheCall.AnswerTheCall',
    'DA_FQ_ClimbTheRanks.ATestOfTreachery',
    'DA_FQ_ClimbTheRanks.ATestOfTreachery.GetAntonToBackOff',
    'DA_FQ_ClimbTheRanks.ATestOfTreachery.GetAntonToBackOff.FindCounterfeitEvidence',
    'DA_FQ_ClimbTheRanks.InvestigateKytheria_Harkonnen',
    'DA_FQ_ClimbTheRanks.InvestigateKytheria_Harkonnen.InvestigateWreck_Harkonnen',
    'DA_FQ_ClimbTheRanks.InvestigateKytheria_Harkonnen.InvestigateWreck_Harkonnen.MeetSimoneVonKonig',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Harkonnen',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Harkonnen.DeviseAPlan_Harkonnen',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Harkonnen.DeviseAPlan_Harkonnen.TellPiterAboutEuporia',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Harkonnen.PledgeAllegiance_Harkonnen',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Harkonnen.PledgeAllegiance_Harkonnen.PledgeAllegiance_Harkonnen_Sub',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Harkonnen.SecureLastContainer_Harkonnen',
    'DA_FQ_ClimbTheRanks.InvestigateDelphis_Harkonnen.SecureLastContainer_Harkonnen.RecoverSheolContainer_Harkonnen',
    'DA_FQ_ClimbTheRanks.PoisonedSpice_Harkonnen'"

  # Assemble the node array based on preset + faction. ch3_start gets the climb
  # ranks set + story nodes + faction-specific arc. rank19_eligible adds the
  # Landsraad mission tree on top. landsraad_unlock_only is unchanged (just
  # the 8 base nodes — the existing minimal Last Sietch variant).
  local STORY_NODES_FACTION
  case "$faction" in
    atreides)  STORY_NODES_FACTION="$CLIMB_RANKS_STORY_ATREIDES"  ;;
    harkonnen) STORY_NODES_FACTION="$CLIMB_RANKS_STORY_HARKONNEN" ;;
  esac
  local JOURNEY_NODES_LITERAL
  case "$preset" in
    landsraad_unlock_only)
      JOURNEY_NODES_LITERAL="ARRAY[${CLIMB_RANKS_NODES}]::text[]"
      ;;
    ch3_start|rank19_eligible)
      JOURNEY_NODES_LITERAL="ARRAY[${CLIMB_RANKS_NODES},${CLIMB_RANKS_STORY_NODES},${STORY_NODES_FACTION}]::text[]"
      ;;
  esac

  # The tier tag generation differs by preset. landsraad_unlock_only computes
  # the player's CURRENT rank from JSONB rep and only tags up to that tier
  # (no overreach). ch3_start hard-tags Tier0..Tier5. rank19_eligible hard-
  # tags Tier0..Tier19 AND sets rep to the rank-19 threshold (11975 corrected).
  #
  # Body sequence: SET LOCAL search_path (proc unqualified refs), set tier
  # tags via a literal array, complete journey nodes via the proc, add the
  # dialogue + optional unlock + tier tags via update_player_tags. All gated
  # on _grant_gate.is_new so a replay is a no-op.
  local TIER_LIST_SQL
  case "$preset" in
    landsraad_unlock_only)
      # Compute Tier0..TierN where N = repToTier(current_jsonb_rep). Last Sietch-corrected
      # thresholds (icehunter+1 for ranks 1-19; rank 20 cap = 12475, Last Sietch-corrected 2026-07-23).
      TIER_LIST_SQL="(
        SELECT array_agg('Faction.' || :'faction_name' || '.Tier' || t::text)
          FROM generate_series(0, (
            SELECT CASE
              WHEN cur >= 12475 THEN 20  WHEN cur >= 11975 THEN 19
              WHEN cur >= 10775 THEN 18  WHEN cur >= 9650 THEN 17
              WHEN cur >= 8600 THEN 16   WHEN cur >= 7625 THEN 15
              WHEN cur >= 6725 THEN 14   WHEN cur >= 5900 THEN 13
              WHEN cur >= 5150 THEN 12   WHEN cur >= 4475 THEN 11
              WHEN cur >= 3875 THEN 10   WHEN cur >= 3350 THEN 9
              WHEN cur >= 2900 THEN 8    WHEN cur >= 2525 THEN 7
              WHEN cur >= 2225 THEN 6    WHEN cur >= 2000 THEN 5
              WHEN cur >= 1000 THEN 4    WHEN cur >= 500 THEN 3
              WHEN cur >= 250 THEN 2     WHEN cur >= 100 THEN 1
              ELSE 0 END
            FROM (
              SELECT COALESCE((
                SELECT (elem->>'ReputationAmount')::int
                  FROM dune.actors a,
                       jsonb_array_elements(a.properties->'FactionPlayerComponent'->'m_FactionDataArray') elem
                 WHERE a.id = :controller_id
                   AND elem->'Faction'->>'Name' = :'faction_name'
              ), 0) AS cur
            ) c
          )) t
      )"
      ;;
    ch3_start)
      TIER_LIST_SQL="(SELECT array_agg('Faction.' || :'faction_name' || '.Tier' || t::text)
                        FROM generate_series(0, 5) t)"
      ;;
    rank19_eligible)
      TIER_LIST_SQL="(SELECT array_agg('Faction.' || :'faction_name' || '.Tier' || t::text)
                        FROM generate_series(0, 19) t)"
      ;;
  esac

  # Optional unlock tag (only landsraad_unlock_only + rank19_eligible add it)
  local LANDSRAAD_TAG_SQL="''"
  if [[ "$preset" == "landsraad_unlock_only" || "$preset" == "rank19_eligible" ]]; then
    LANDSRAAD_TAG_SQL="'Journey.LandsraadContractsUnlocked'"
  fi

  # Rep write — icehunter v0.5.5 sets rep to the preset's tier threshold for
  # BOTH ch3_start (rank 5 = 2001) and rank19_eligible (rank 19 = 11975).
  # Last Sietch deviation: we use GREATEST(current_rep, target_rep) so the grant NEVER
  # downgrades a player who already has higher rep. This makes the grant safe
  # to re-apply to anyone (icehunter's version would downgrade a rank-17 player
  # who got a "ch3_start" retro). Threshold tags (Tier 0..N) are still set to
  # match the preset's cap regardless of rep — that's the gate-unlock effect.
  local REP_WRITE_SQL=""
  local TARGET_REP=""
  case "$preset" in
    ch3_start)       TARGET_REP="2001"  ;;   # rank 5 threshold + 1
    rank19_eligible) TARGET_REP="11975" ;;   # rank 19 threshold (Last Sietch-corrected)
  esac
  if [[ -n "$TARGET_REP" ]]; then
    REP_WRITE_SQL=$(cat <<EOF

-- ${preset}: set faction reputation to GREATEST(current, ${TARGET_REP}).
-- Dual-write JSONB on FactionPlayerComponent (so in-game UI ticks immediately)
-- + Funcom's set_player_faction_reputation proc (canonical player_faction_reputation table).
WITH ctx AS (
  SELECT eps.player_controller_id::bigint AS ctrl_id,
         a.properties->'FactionPlayerComponent'->'m_FactionDataArray' AS arr,
         COALESCE((
           SELECT (elem->>'ReputationAmount')::int
             FROM dune.actors a2, jsonb_array_elements(a2.properties->'FactionPlayerComponent'->'m_FactionDataArray') elem
            WHERE a2.id = eps.player_controller_id
              AND elem->'Faction'->>'Name' = :'faction_name'
         ), 0) AS current_rep
    FROM dune.encrypted_player_state eps
    JOIN dune.actors a ON a.id = eps.player_controller_id
   WHERE eps.player_controller_id = :controller_id
),
target AS (
  SELECT ctrl_id, arr, GREATEST(current_rep, ${TARGET_REP}) AS new_rep FROM ctx
),
new_arr AS (
  SELECT t.ctrl_id, t.new_rep, CASE WHEN EXISTS (
    SELECT 1 FROM jsonb_array_elements(t.arr) elem
     WHERE elem->'Faction'->>'Name' = :'faction_name'
  ) THEN
    (SELECT jsonb_agg(
      CASE WHEN elem->'Faction'->>'Name' = :'faction_name' THEN
        jsonb_set(
          jsonb_set(elem, '{ReputationAmount}', to_jsonb(t.new_rep)),
          '{timestamp}', to_jsonb(EXTRACT(EPOCH FROM NOW())))
      ELSE elem END)
     FROM jsonb_array_elements(t.arr) elem)
  ELSE
    COALESCE(t.arr, '[]'::jsonb) ||
    jsonb_build_array(jsonb_build_object(
      'Faction', jsonb_build_object('Name', (:'faction_name')::text),
      'timestamp', EXTRACT(EPOCH FROM NOW()),
      'ReputationAmount', t.new_rep))
  END AS arr_v
  FROM target t
)
UPDATE dune.actors a
   SET properties = jsonb_set(a.properties,
                              '{FactionPlayerComponent,m_FactionDataArray}',
                              na.arr_v)
  FROM new_arr na
 WHERE a.id = na.ctrl_id
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

SELECT dune.set_player_faction_reputation(
         :controller_id::bigint,
         (:faction_id)::smallint,
         COALESCE((
           SELECT (elem->>'ReputationAmount')::int
             FROM dune.actors a,
                  jsonb_array_elements(a.properties->'FactionPlayerComponent'->'m_FactionDataArray') elem
            WHERE a.id = :controller_id
              AND elem->'Faction'->>'Name' = :'faction_name'), 0)::integer)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
  fi

  GRANT_BODY=$(cat <<EOF
-- G12 write: complete journey nodes + update tags (+optional rep write).
SET LOCAL search_path TO dune, public;

-- Complete the 8 ClimbTheRanks journey nodes via Funcom's proc.
SELECT dune.complete_journey_story_nodes_for_player(
         (SELECT "user" FROM dune.accounts WHERE id = :account_id),
         ${JOURNEY_NODES_LITERAL})
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Add tier tags + dialogue flag + cinematic flags + optional Landsraad unlock.
-- icehunter v0.5.5 adds PlayedAllegianceCinematic + SeenAnvilCinematic for
-- ch3_start AND rank19_eligible; we follow.
SELECT dune.update_player_tags(
         :account_id::bigint,
         ${TIER_LIST_SQL}
           || ARRAY[:'dialogue_flag']::text[]
           || ARRAY['DialogueFlags.Factions.PlayedAllegianceCinematic',
                    'DialogueFlags.Factions.SeenAnvilCinematic']::text[]
           || CASE WHEN ${LANDSRAAD_TAG_SQL} = '' THEN ARRAY[]::text[]
                   ELSE ARRAY[${LANDSRAAD_TAG_SQL}]::text[] END,
         '{}'::text[])
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
${REP_WRITE_SQL}
EOF
)
}

# =============================================================================
# v2-base ops actions (icehunter parity) — teleport, resets, repair
# =============================================================================

# G15 — Teleport. Move an offline player to a named location via
# dune.admin_move_offline_player_to_partition. Offline-enforced server-side
# AND client-side (requires_offline). The proc body uses UNQUALIFIED table
# refs so SET LOCAL search_path is required.
#
# Locations are a baked-in pipe-delimited list: "name|x|y|z". Default partition
# is the player's current partition (preserves the zone server, mirrors
# icehunter); operator can override via detail.partition_id.
#
# Detail shape options:
#   { "location_name": "CrashSite" }          — picks from TELEPORT_LOCATIONS
#   { "location_name": "X", "partition_id": 1 } — override partition
#   { "x": 1.0, "y": 2.0, "z": 3.0, "partition_id": 1 }  — custom coords mode

# TELEPORT_LOCATIONS — 10 icehunter-shipped points. Add Last Sietch locations by
# appending. Format: "name|x|y|z". All Hagga-Basin compatible (partition 1).
TELEPORT_LOCATIONS=(
  "Windsack|974276.75|20084.312|5112.283"
  "EcoLabs|826879.3|-925967.2|4974.4277"
  "CrashSite|330284.22|205236.98|2251.008"
  "MediumStarter|268515.8|207559.39|5000.0"
  "ConvoyAmbush|-920080.0|909620.0|300.0"
  "SpiceRaid|271590.0|-493122.0|8471.0"
  "PS5_ESW_0|-113881.4|-305252.1|20864.5"
  "PS5_ESW_1|-109861.8|-307020.0|21192.9"
  "PS5_ESW_2|-129029.6|-312757.8|21099.6"
  "PS5_ESW_3|-117312.0|-305453.9|21649.8"
)

lookup_teleport_location() {
  # $1 = name. Echoes "x|y|z" if found; non-zero exit otherwise.
  local name="$1" entry
  for entry in "${TELEPORT_LOCATIONS[@]}"; do
    if [[ "${entry%%|*}" == "$name" ]]; then
      echo "${entry#*|}"
      return 0
    fi
  done
  return 1
}

build_teleport_grant() {
  local loc_name x y z partition_id coords
  loc_name=$(jq_get_nested detail location_name)
  partition_id=$(jq_get_nested detail partition_id)

  if [[ -n "$loc_name" ]]; then
    # Allowlisted name path. Whitelist regex so the value can never reach SQL
    # as anything other than [A-Za-z0-9_].
    [[ "$loc_name" =~ ^[A-Za-z0-9_]+$ ]] \
      || fail_json "invalid location_name format: $loc_name" 2
    coords=$(lookup_teleport_location "$loc_name") \
      || fail_json "unknown teleport location: $loc_name" 2
    IFS='|' read -r x y z <<<"$coords"
  else
    # Custom-coords mode.
    x=$(jq_get_nested detail x); y=$(jq_get_nested detail y); z=$(jq_get_nested detail z)
    [[ "$x" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || fail_json "invalid x coordinate: $x" 2
    [[ "$y" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || fail_json "invalid y coordinate: $y" 2
    [[ "$z" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || fail_json "invalid z coordinate: $z" 2
    loc_name="custom"
  fi

  # Partition default: player's current partition. Override only if provided.
  if [[ -n "$partition_id" ]]; then
    [[ "$partition_id" =~ ^[0-9]+$ ]] \
      || fail_json "invalid partition_id: $partition_id" 2
    (( partition_id >= 1 && partition_id <= 1000 )) \
      || fail_json "partition_id out of range 1..1000: $partition_id" 2
  fi

  DETAIL_JSON=$(jq -nc \
    --arg n "$loc_name" --arg x "$x" --arg y "$y" --arg z "$z" \
    --arg p "${partition_id:-}" \
    '{location_name:$n, x:($x|tonumber), y:($y|tonumber), z:($z|tonumber)} +
     (if $p == "" then {} else {partition_id:($p|tonumber)} end)')

  PSQL_VARS+=( -v "tp_x=${x}" -v "tp_y=${y}" -v "tp_z=${z}" )
  if [[ -n "$partition_id" ]]; then
    PSQL_VARS+=( -v "tp_partition_id=${partition_id}" )
  else
    # Sentinel — the SQL falls back to player-current-partition when this is 0.
    PSQL_VARS+=( -v "tp_partition_id=0" )
  fi

  # Preflight: confirm the account resolves to an FLS id (otherwise the proc
  # would fail with a less-friendly error inside the transaction).
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G15 preflight: account must have an FLS id.
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_fls text;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT convert_from(ea.encrypted_funcom_id, 'UTF8')
    INTO v_fls
    FROM dune.encrypted_accounts ea
   WHERE ea.id = g.account_id;
  IF v_fls IS NULL OR v_fls = '' THEN
    RAISE EXCEPTION 'TELEPORT_FAIL: account has no FLS id (encrypted_funcom_id missing)';
  END IF;
END $$;
EOF
)

  # Write: resolve fls_id + partition (current if override is 0/null), call the
  # Funcom proc. The proc itself checks is_player_offline() and raises if not.
  GRANT_BODY=$(cat <<'EOF'
-- G15 write: teleport via Funcom proc.
SET LOCAL search_path TO dune, public;
WITH ctx AS (
  SELECT convert_from(ea.encrypted_funcom_id, 'UTF8') AS fls_id,
         COALESCE(NULLIF(:tp_partition_id, 0)::bigint,
                  (SELECT a.partition_id FROM dune.actors a
                    JOIN dune.player_state ps ON ps.player_pawn_id = a.id
                    WHERE ps.account_id = :account_id),
                  (SELECT partition_id FROM dune.world_partition
                    WHERE blocked = false ORDER BY partition_id LIMIT 1)
         )::bigint AS partition_id
    FROM dune.encrypted_accounts ea
   WHERE ea.id = :account_id
)
SELECT dune.admin_move_offline_player_to_partition(
         ctx.fls_id,
         ctx.partition_id,
         ROW(:tp_x::float8, :tp_y::float8, :tp_z::float8)::dune.vector
       )
  FROM ctx
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G16 — Reset Specializations. Detail = { track_type?: string }.
# No track_type or "all" → call BOTH reset procs (tracks + keystones).
# With track_type → DELETE one track row (mirrors icehunter db.go:744-750).
# Offline-gated (specialization tables are loaded into RAM on login).
build_reset_specs_grant() {
  local track_type
  track_type=$(jq_get_nested detail track_type)

  if [[ -n "$track_type" ]] && [[ "$track_type" != "all" ]]; then
    [[ "$track_type" =~ ^[A-Za-z0-9_]+$ ]] \
      || fail_json "invalid track_type: $track_type" 2
    DETAIL_JSON=$(jq -nc --arg t "$track_type" '{track_type:$t,scope:"single"}')
    PSQL_VARS+=( -v "track_type=${track_type}" )
    GRANT_BODY=$(cat <<'EOF'
-- G16 write: single-track delete.
SET LOCAL search_path TO dune, public;
DELETE FROM dune.specialization_tracks
 WHERE player_id = :controller_id::bigint
   AND track_type::text = :'track_type'
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
  else
    DETAIL_JSON='{"scope":"all"}'
    GRANT_BODY=$(cat <<'EOF'
-- G16 write: reset ALL specializations (tracks + keystones).
-- Both reset_specialization_tracks AND reset_specialization_keystones take CONTROLLER id:
-- specialization_tracks.player_id = ctrl_id AND purchased_specialization_keystones.player_id
-- = ctrl_id (confirmed vs icehunter db.go:3094 + live data; the table is NOT pawn-keyed).
SET LOCAL search_path TO dune, public;
WITH ctx AS (
  SELECT player_controller_id::bigint AS ctrl_id,
         player_pawn_id::bigint       AS pawn_id
    FROM dune.encrypted_player_state WHERE player_controller_id = :controller_id
),
tracks_reset AS (
  SELECT dune.reset_specialization_tracks(ctx.ctrl_id)
    FROM ctx
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
)
SELECT dune.reset_specialization_keystones(ctx.ctrl_id)
  FROM ctx
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
   AND EXISTS (SELECT 1 FROM tracks_reset);
EOF
)
  fi
}

# G17 — Reset Tutorials. Calls dune.delete_all_tutorial_entries(player_id).
# Online-safe (tutorial table is read on-demand by the game, not cached
# per-player in RAM).
build_reset_tutorials_grant() {
  DETAIL_JSON='{}'
  GRANT_BODY=$(cat <<'EOF'
-- G17 write: delete all tutorial entries for this player.
SET LOCAL search_path TO dune, public;
WITH ctx AS (
  SELECT player_controller_id::bigint AS ctrl_id
    FROM dune.encrypted_player_state WHERE player_controller_id = :controller_id
)
SELECT dune.delete_all_tutorial_entries(ctx.ctrl_id)
  FROM ctx
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G18 — Wipe Codex (Mnemonic Recall lessons). Calls
# dune.delete_mnemonic_recall_lesson_all(account_id). Online-safe.
# Note: takes account_id directly, not player_controller_id (icehunter
# cmdWipeCodex db.go:1498-1513).
build_wipe_codex_grant() {
  DETAIL_JSON='{}'
  GRANT_BODY=$(cat <<'EOF'
-- G18 write: wipe all codex (Mnemonic Recall) entries for this account.
SET LOCAL search_path TO dune, public;
SELECT dune.delete_mnemonic_recall_lesson_all(:account_id::bigint)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G19 — Repair All. Restores CurrentDurability = MaxDurability on every
# repairable item in the player's MAIN backpack (inventory_type=0) and
# HOTBAR (inventory_type=15). Offline-gated because those inventories are
# RAM-fragile. The UPDATE only touches rows where MaxDurability > 0 (skips
# stackable non-durability items like SolarisCoin).
#
# Inventory_type reference (verified empirically 2026-05-23 against real lastsietch-dune data):
#   0  = MAIN BACKPACK (weapons, ammo, tools, blueprints — RAM-fragile)
#   1  = Equipped armor/shield slot (RAM-fragile)
#   14 = EMOTE slot (NOT durability-bearing; previous spec error)
#   15 = HOTBAR (RAM-fragile)
#   30 = CHOAM BANK (online-safe per our internal notes)
build_repair_all_grant() {
  DETAIL_JSON='{"scope":"main+hotbar"}'
  GRANT_BODY=$(cat <<'EOF'
-- G19 write: repair every repairable item in main backpack + hotbar.
SET LOCAL search_path TO dune, public;
WITH bp AS (
  SELECT inv.id AS inv_id
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id
   WHERE eps.account_id = :account_id
     AND inv.inventory_type IN (0, 15)
)
UPDATE dune.items it
   SET stats = jsonb_set(
         it.stats,
         '{FItemStackAndDurabilityStats,1,CurrentDurability}',
         (it.stats->'FItemStackAndDurabilityStats'->1->'MaxDurability')
       )
  FROM bp
 WHERE it.inventory_id = bp.inv_id
   AND it.stats->'FItemStackAndDurabilityStats'->1->>'MaxDurability' IS NOT NULL
   AND (it.stats->'FItemStackAndDurabilityStats'->1->>'MaxDurability')::float > 0
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# G20 — Import Blueprint. Materializes a BuildingBlueprint_CopyDevice item in
# either the player's MAIN backpack (inventory_type=0, offline-gated) or their
# CHOAM bank (inventory_type=30, online-safe). The item carries a PlayerBlueprintId
# reference to a master row in dune.building_blueprints; instance/placeable/
# pentashield rows are populated in-transaction so the in-game paste mechanism
# resolves the captured base on first use.
#
# Mirrors icehunter cmdImportBlueprint (db.go:1974-2129) but adapted to the
# Last Sietch _grant_gate replay-safe pattern: everything is gated WHERE EXISTS in
# _grant_gate.is_new, so a replay is a no-op.
#
# Blueprint shape (validated server-side AND in admin-backend before send):
#   { "instances": [{building_type,x,y,z,rotation}, ...],
#     "placeables": [{building_type,x,y,z,rx,ry,rz}, ...],
#     "pentashields": [{placeable_id,scale:[w,h,d]}, ...] }
# The admin-backend pre-fetches Solido Market UUIDs and inlines the data;
# the script never makes HTTPS calls.
build_import_blueprint_grant() {
  local delivery blueprint_data title source_id
  delivery=$(jq_get_nested detail delivery)
  title=$(jq_get_nested detail title)
  source_id=$(jq_get_nested detail source_blueprint_id)

  case "$delivery" in
    backpack|bank) ;;
    *) fail_json "invalid delivery (must be backpack or bank): $delivery" 2 ;;
  esac

  # Pull blueprint_data as compact JSON; preserve the object structure.
  blueprint_data=$(printf '%s' "$GRANT_JSON" \
    | jq -c '.detail.blueprint_data // empty')
  if [[ -z "$blueprint_data" || "$blueprint_data" == "null" ]]; then
    fail_json "detail.blueprint_data is required (admin-backend resolves Solido UUIDs)" 2
  fi

  # Shape check: must be a JSON object.
  if ! printf '%s' "$blueprint_data" | jq -e 'type == "object"' >/dev/null 2>&1; then
    fail_json "detail.blueprint_data must be a JSON object" 2
  fi

  # Size check (defends psql -v binding budget and SQL string length).
  local bytes
  bytes=$(printf '%s' "$blueprint_data" | wc -c)
  if (( bytes > CAP_BLUEPRINT_BYTES )); then
    fail_json "blueprint_data ${bytes} bytes exceeds cap ${CAP_BLUEPRINT_BYTES}" 2
  fi

  # Piece counts + cap + at-least-one constraint.
  local n_inst n_plac n_pent piece_total mtx_count
  n_inst=$(printf '%s' "$blueprint_data" | jq '(.instances // []) | length')
  n_plac=$(printf '%s' "$blueprint_data" | jq '(.placeables // []) | length')
  n_pent=$(printf '%s' "$blueprint_data" | jq '(.pentashields // []) | length')
  [[ "$n_inst" =~ ^[0-9]+$ ]] || fail_json "could not parse instances length" 2
  [[ "$n_plac" =~ ^[0-9]+$ ]] || fail_json "could not parse placeables length" 2
  [[ "$n_pent" =~ ^[0-9]+$ ]] || fail_json "could not parse pentashields length" 2
  piece_total=$(( n_inst + n_plac + n_pent ))
  if (( piece_total < 1 )); then
    fail_json "blueprint_data has zero pieces (need at least one instance or placeable)" 2
  fi
  if (( piece_total > CAP_BLUEPRINT_PIECES )); then
    fail_json "blueprint piece count ${piece_total} exceeds cap ${CAP_BLUEPRINT_PIECES}" 2
  fi

  # Per-element validation. Reject any building_type outside [A-Za-z0-9_],
  # any non-finite coord/rotation, any bad pentashield placeable_id/scale.
  # Done with jq predicates so the bash side stays simple.
  if ! printf '%s' "$blueprint_data" | jq -e '
        (.instances // []) | all(
          (.building_type // "" | test("^[A-Za-z0-9_-]+$"))
          and ((.x // 0) | type == "number")
          and ((.y // 0) | type == "number")
          and ((.z // 0) | type == "number")
          and ((.rotation // 0) | type == "number")
        )
      ' >/dev/null 2>&1; then
    fail_json "blueprint_data.instances has invalid building_type or non-numeric coords" 2
  fi
  if ! printf '%s' "$blueprint_data" | jq -e '
        (.placeables // []) | all(
          (.building_type // "" | test("^[A-Za-z0-9_-]+$"))
          and ((.x // 0) | type == "number")
          and ((.y // 0) | type == "number")
          and ((.z // 0) | type == "number")
          and ((.rx // 0) | type == "number")
          and ((.ry // 0) | type == "number")
          and ((.rz // 0) | type == "number")
        )
      ' >/dev/null 2>&1; then
    fail_json "blueprint_data.placeables has invalid building_type or non-numeric coords" 2
  fi
  if ! printf '%s' "$blueprint_data" | jq -e '
        (.pentashields // []) | all(
          ((.placeable_id // -1) | type == "number")
          and ((.placeable_id // -1) >= 0)
          and ((.scale // []) | type == "array")
          and ((.scale // []) | length == 3)
          and ((.scale // []) | all(type == "number" and . >= -32768 and . <= 32767))
        )
      ' >/dev/null 2>&1; then
    fail_json "blueprint_data.pentashields has invalid placeable_id or scale" 2
  fi

  # Count MTX_-prefixed pieces (audit only; the game's paste prompts entitlement
  # at runtime for these).
  mtx_count=$(printf '%s' "$blueprint_data" | jq '
    ((.instances // []) + (.placeables // []))
    | map(select(.building_type | startswith("MTX_"))) | length')
  [[ "$mtx_count" =~ ^[0-9]+$ ]] || mtx_count=0

  # Audit detail (result_* fields filled by the SQL tail-statement).
  DETAIL_JSON=$(jq -nc \
    --arg d "$delivery" \
    --arg t "$title" \
    --arg s "$source_id" \
    --argjson pt "$piece_total" \
    --argjson ic "$n_inst" \
    --argjson pc "$n_plac" \
    --argjson sc "$n_pent" \
    --argjson mc "$mtx_count" \
    '{delivery:$d, piece_count:$pt, instance_count:$ic, placeable_count:$pc,
      pentashield_count:$sc, mtx_count:$mc}
     + (if $t == "" then {} else {title:$t} end)
     + (if $s == "" then {} else {source_blueprint_id:$s} end)')

  PSQL_VARS+=( -v "delivery=${delivery}" )
  # blueprint_data is intentionally NOT passed via psql -v (200KB+ JSONs blow
  # past ARG_MAX). Instead it's inlined in the SQL via a dollar-quoted string
  # below. The jq validators above have already restricted content to a
  # well-formed JSON object with [A-Za-z0-9_] building_type strings and numeric
  # coordinates, so dollar-quoted embedding is safe.

  # Preflight: resolve target inventory by delivery mode; refuse if missing or
  # full. BOTH backpack and bank inventories are keyed on `player_pawn_id`
  # (researcher correction 2026-05-23 — bank inventory is NOT controller-keyed).
  # Only the inventory_type differs (0 vs 30). Capacity = inv.max_item_count.
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G20 preflight: resolve target inventory + capacity check.
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_delivery text; v_inv bigint; v_cap int; v_used int;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  -- Read :delivery via current_setting after we SET it below. Simpler: bind it
  -- via a single-row CTE? DO blocks cannot read psql -v vars directly, so we
  -- inline the predicates using the JSON detail stored on the audit row.
  SELECT (gp.detail->>'delivery') INTO v_delivery
    FROM dune.ls_progression_grants gp WHERE gp.id = g.grant_id;
  IF v_delivery NOT IN ('backpack','bank') THEN
    RAISE EXCEPTION 'IMPORT_BLUEPRINT_FAIL: bad delivery=%', v_delivery;
  END IF;
  SELECT inv.id, inv.max_item_count,
         (SELECT COUNT(*) FROM dune.items it WHERE it.inventory_id = inv.id)
    INTO v_inv, v_cap, v_used
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id
     AND inv.inventory_type = CASE v_delivery WHEN 'backpack' THEN 0 ELSE 30 END
   WHERE eps.account_id = g.account_id;
  IF v_inv IS NULL THEN
    RAISE EXCEPTION 'IMPORT_BLUEPRINT_FAIL: target inventory not found (delivery=%, account=%)',
                    v_delivery, g.account_id;
  END IF;
  IF v_used >= v_cap THEN
    RAISE EXCEPTION 'IMPORT_BLUEPRINT_FAIL: target inventory full (used % of %, delivery=%)',
                    v_used, v_cap, v_delivery;
  END IF;
END $$;
EOF
)

  # Body: multi-statement transactional flow. CTEs in a single statement cannot
  # see each other's writes (PG MVCC snapshot rule), so we split into:
  #   1. Capture inv + create item + create blueprint master + store IDs in a
  #      transaction-scoped TEMP TABLE (single SQL statement with CTE chain —
  #      the CTEs only forward data to a SELECT into the temp table, no
  #      cross-CTE table-state reads).
  #   2. UPDATE the item's stats (now visible) with the real PlayerBlueprintId.
  #   3-5. Bulk INSERT instances / placeables / pentashields from the temp table
  #      cross-joined with jsonb_array_elements.
  #   6. UPDATE the audit row with result_item_id + result_blueprint_db_id.
  # Every write predicates on _grant_gate.is_new for replay safety; on replay
  # the temp table comes up empty so subsequent statements all match 0 rows.
  # Array literals use PostgreSQL "[lo:hi]={...}" syntax with explicit 0-based
  # bounds, matching UE's stored convention (icehunter db.go:2065-2066, 2092-2093).
  GRANT_BODY=$(cat <<EOF
-- G20 step 1: create item + master, capture ids in a transaction-temp table.
-- The _g20_state table holds (item_id, bp_id) for the rest of the transaction.
CREATE TEMP TABLE IF NOT EXISTS _g20_state (item_id bigint, bp_id bigint) ON COMMIT DROP;
TRUNCATE _g20_state;

WITH gate AS (SELECT is_new FROM _grant_gate),
inv AS (
  -- Both backpack and bank inventories are pawn-keyed (researcher correction
  -- 2026-05-23). Only the inventory_type distinguishes them.
  SELECT i.id AS inv_id
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories i
      ON i.actor_id = eps.player_pawn_id
     AND i.inventory_type = CASE :'delivery' WHEN 'backpack' THEN 0 ELSE 30 END
   WHERE eps.account_id = :account_id
),
nextpos AS (
  SELECT COALESCE(MAX(position_index), -1) + 1 AS p
    FROM dune.items WHERE inventory_id = (SELECT inv_id FROM inv)
),
new_item AS (
  INSERT INTO dune.items
    (inventory_id, stack_size, position_index, template_id,
     quality_level, stats, acquisition_time, is_new)
  SELECT inv.inv_id, 1, nextpos.p, 'BuildingBlueprint_CopyDevice', 0,
         '{"FCustomizationStats":[[], {}],"FBuildingBlueprintItemStats":[[], {"PlayerBlueprintId":"!!bbp#0","PlayerBaseBackupId":{}}],"FItemStackAndDurabilityStats":[[], {"DecayedMaxDurability":0.0}]}'::jsonb,
         0, true
    FROM inv, nextpos
   WHERE EXISTS (SELECT 1 FROM gate WHERE is_new)
  RETURNING id
),
new_bp AS (
  INSERT INTO dune.building_blueprints (item_id, player_id, building_blueprint_map)
  SELECT id, NULL, '' FROM new_item
   WHERE EXISTS (SELECT 1 FROM gate WHERE is_new)
  RETURNING id, item_id
)
INSERT INTO _g20_state (item_id, bp_id)
SELECT new_bp.item_id, new_bp.id FROM new_bp;

-- G20 step 2: patch PlayerBlueprintId on the now-visible item row.
UPDATE dune.items
   SET stats = jsonb_set(stats,
                '{FBuildingBlueprintItemStats,1,PlayerBlueprintId}',
                to_jsonb('!!bbp#' || s.bp_id::text))
  FROM _g20_state s
 WHERE dune.items.id = s.item_id;

-- G20 step 3: bulk insert instances. `[0:3]` array bounds match UE convention.
--   - instance_id: 1-based sequential (matches icehunter v0.5.6 fix; pentashield
--     placeable_id refs in Solido JSON assume 1-based numbering).
--   - provides_stability: derived from a structural-type pattern match
--     (Foundation / Pillar / Column substrings) UNLESS the JSON explicitly
--     supplies provides_stability per-row (round-trip from an exported Last Sietch
--     blueprint preserves it verbatim). Mirrors icehunter v0.5.6.
INSERT INTO dune.building_blueprint_instances
  (building_blueprint_id, instance_id, building_type, transform,
   hologram, provides_stability, health)
SELECT s.bp_id,
       COALESCE((e.elem->>'instance_id')::int, e.ord::int),
       e.elem->>'building_type',
       ('[0:3]={'
          || COALESCE((e.elem->>'x')::real, 0)::text || ','
          || COALESCE((e.elem->>'y')::real, 0)::text || ','
          || COALESCE((e.elem->>'z')::real, 0)::text || ','
          || COALESCE((e.elem->>'rotation')::real, 0)::text
        || '}')::real[],
       true,
       COALESCE((e.elem->>'provides_stability')::bool,
                (e.elem->>'building_type') LIKE '%Foundation%'
                 OR (e.elem->>'building_type') LIKE '%Pillar%'
                 OR (e.elem->>'building_type') LIKE '%Column%'),
       1.0
  FROM _g20_state s,
       jsonb_array_elements(COALESCE(NULLIF(\$bpjson\$${blueprint_data}\$bpjson\$::jsonb->'instances', 'null'::jsonb), '[]'::jsonb))
         WITH ORDINALITY e(elem, ord);

-- G20 step 4: bulk insert placeables. `[0:5]` array bounds.
--   - placeable_id: 1-based sequential (matches icehunter v0.5.6 + Solido's
--     pentashield placeable_id refs).
INSERT INTO dune.building_blueprint_placeables
  (building_blueprint_id, placeable_id, building_type, transform, hologram)
SELECT s.bp_id,
       COALESCE((e.elem->>'placeable_id')::int, e.ord::int),
       e.elem->>'building_type',
       ('[0:5]={'
          || COALESCE((e.elem->>'x')::real, 0)::text  || ','
          || COALESCE((e.elem->>'y')::real, 0)::text  || ','
          || COALESCE((e.elem->>'z')::real, 0)::text  || ','
          || COALESCE((e.elem->>'rx')::real, 0)::text || ','
          || COALESCE((e.elem->>'ry')::real, 0)::text || ','
          || COALESCE((e.elem->>'rz')::real, 0)::text
        || '}')::real[],
       true
  FROM _g20_state s,
       jsonb_array_elements(COALESCE(NULLIF(\$bpjson\$${blueprint_data}\$bpjson\$::jsonb->'placeables', 'null'::jsonb), '[]'::jsonb))
         WITH ORDINALITY e(elem, ord);

-- G20 step 5: bulk insert pentashields (standard 1-indexed PG array).
INSERT INTO dune.building_blueprint_pentashields
  (building_blueprint_id, placeable_id, scale)
SELECT s.bp_id, (e.elem->>'placeable_id')::int,
       ARRAY[(e.elem->'scale'->>0)::smallint,
             (e.elem->'scale'->>1)::smallint,
             (e.elem->'scale'->>2)::smallint]
  FROM _g20_state s,
       jsonb_array_elements(COALESCE(NULLIF(\$bpjson\$${blueprint_data}\$bpjson\$::jsonb->'pentashields', 'null'::jsonb), '[]'::jsonb)) e(elem);

-- G20 step 6: write result_* IDs back to the audit row.
UPDATE dune.ls_progression_grants
   SET detail = detail
                || jsonb_build_object(
                     'result_item_id',         s.item_id,
                     'result_blueprint_db_id', s.bp_id)
  FROM _g20_state s,
       (SELECT grant_id FROM _grant_gate) g
 WHERE dune.ls_progression_grants.id = g.grant_id;
EOF
)
}

# G21a — bb_handoff. ONLINE-SAFE ownership transfer of an existing
# dune.base_backups row to the recipient. The source backup row is mutated
# (player_id is rewritten); the source admin no longer owns it after the
# grant. Recipient also gets an empty BaseBackupTool delivered to their CHOAM
# bank (online-safe per our internal notes) so they can
# withdraw + use it to place the saved base in Hagga Basin.
#
# Refuses if the recipient already has 3 or more base_backups rows (Funcom's
# in-game tool max is 3 stored bases per player; pushing past that may break
# the in-game slot UI). The slot cap is C++-enforced — the DB has zero
# CHECK / trigger to back it up — so this preflight is our only line of
# defence.
#
# Required detail: recipient_account_id, source_backup_id.
# Optional detail: override_name (replaces base_backup_name on transfer).
#
# The recipient_account_id is the player's account_id (matches selectedPlayer
# .account_id in the admin UI). We resolve it here to (controller_id, pawn_id)
# via dune.encrypted_player_state — same pattern as char_xp/spec_xp/other
# grants. controller_id drives dune.base_backups.player_id; pawn_id drives
# the CHOAM bank inventory lookup (inventory_type=30 is keyed on pawn actor_id).
#
# Authorization is upstream: the admin-backend's
# /api/dune/bb/available-sources endpoint filters source backups before this
# script ever sees a request, and the audit row captures the operator. The
# UPDATE here therefore matches on source_backup_id alone — no source-admin
# guard CTE (a previous version checked `player_id = src_admin.ctrl` against
# the top-level :account_id which is actually the RECIPIENT in our flow,
# guaranteeing 0 rows). We still 1/0-ROLLBACK on 0 rows updated, which now
# only fires if the source backup vanished or the id is bogus.
build_bb_handoff_grant() {
  local recipient_account_id source_backup_id override_name
  local recipient_controller_id recipient_pawn_id
  recipient_account_id=$(jq_get_nested detail recipient_account_id)
  source_backup_id=$(jq_get_nested detail source_backup_id)
  override_name=$(jq_get_nested detail override_name)

  [[ "$recipient_account_id" =~ ^[0-9]+$ ]] \
    || fail_json "invalid recipient_account_id (must be digits): $recipient_account_id" 2
  [[ "$source_backup_id" =~ ^[0-9]+$ ]] \
    || fail_json "invalid source_backup_id (must be digits): $source_backup_id" 2

  # override_name (if present) must look like a normal Funcom base name. The
  # column is text so we permit a generous charset; reject pure-control input.
  if [[ -n "$override_name" ]]; then
    if [[ ${#override_name} -gt 128 ]]; then
      fail_json "override_name too long (max 128 chars)" 2
    fi
    if [[ "$override_name" =~ [[:cntrl:]] ]]; then
      fail_json "override_name contains control characters" 2
    fi
  fi

  # Resolve recipient account_id -> (controller_id, pawn_id) the same way every
  # other grant does. Refuse loudly if either is missing — without a pawn the
  # bank inventory lookup cannot resolve, and without a controller the
  # base_backups ownership write would land on a garbage id.
  # RESOLVE_TARGET_SQL, not a bare account_id lookup: encrypted_player_state
  # has one row per character SLOT, and psql_scalar strips whitespace, so a
  # 2-slot recipient CONCATENATES both ids into a garbage number instead of
  # failing — exactly the "land on a garbage id" outcome this block warns about.
  recipient_controller_id=$(psql_scalar \
    "${RESOLVE_TARGET_SQL//@COL@/eps.player_controller_id}" \
    -v "account_id=${recipient_account_id}")
  recipient_pawn_id=$(psql_scalar \
    "${RESOLVE_TARGET_SQL//@COL@/eps.player_pawn_id}" \
    -v "account_id=${recipient_account_id}")

  if [[ -z "$recipient_controller_id" || -z "$recipient_pawn_id" ]]; then
    fail_json "could not resolve controller/pawn for recipient account ${recipient_account_id}" 4
  fi
  [[ "$recipient_controller_id" =~ ^[0-9]+$ ]] \
    || fail_json "resolved recipient controller_id not numeric: $recipient_controller_id" 4
  [[ "$recipient_pawn_id" =~ ^[0-9]+$ ]] \
    || fail_json "resolved recipient pawn_id not numeric: $recipient_pawn_id" 4

  DETAIL_JSON=$(jq -nc \
    --argjson ra "$recipient_account_id" \
    --argjson rc "$recipient_controller_id" \
    --argjson rp "$recipient_pawn_id" \
    --argjson s "$source_backup_id" \
    --arg n "$override_name" \
    '{recipient_account_id:$ra, recipient_controller_id:$rc,
      recipient_pawn_id:$rp, source_backup_id:$s}
     + (if $n == "" then {} else {override_name:$n} end)')

  PSQL_VARS+=( -v "recipient_controller_id=${recipient_controller_id}" \
               -v "recipient_pawn_id=${recipient_pawn_id}" \
               -v "source_backup_id=${source_backup_id}" \
               -v "override_name=${override_name}" )

  # Preflight: refuse if the recipient is already at the 3-slot cap. Funcom
  # enforces this client/C++-side; the DB has no CHECK constraint. Guarded on
  # is_new so a replay never re-evaluates the count. The DO block reads
  # recipient_controller_id from the just-inserted audit row's detail JSON
  # (psql -v vars are NOT substituted inside a $$ body).
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G21a preflight: (a) recipient slot cap (Funcom C++ rule: max 3 stored bases)
--                 (b) refuse no-op (recipient already owns the source)
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_recipient bigint; v_slots int;
        v_source bigint; v_current_owner bigint;
BEGIN
  SET LOCAL search_path TO dune, public;
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT (gp.detail->>'recipient_controller_id')::bigint,
         (gp.detail->>'source_backup_id')::bigint
    INTO v_recipient, v_source
    FROM dune.ls_progression_grants gp WHERE gp.id = g.grant_id;
  IF v_recipient IS NULL THEN
    RAISE EXCEPTION 'BB_HANDOFF_FAIL: recipient_controller_id missing from detail';
  END IF;
  SELECT player_id INTO v_current_owner
    FROM dune.base_backups WHERE id = v_source;
  IF v_current_owner IS NULL THEN
    RAISE EXCEPTION 'BB_HANDOFF_FAIL: source_backup_id % not found', v_source;
  END IF;
  IF v_current_owner = v_recipient THEN
    RAISE EXCEPTION 'BB_HANDOFF_FAIL: recipient already owns source backup % (would be a no-op); pick a different recipient or use bb_clone for self-clone', v_source;
  END IF;
  SELECT COUNT(*) INTO v_slots
    FROM dune.base_backups
   WHERE player_id = v_recipient;
  IF v_slots >= 3 THEN
    RAISE EXCEPTION 'BB_HANDOFF_FAIL: recipient has % stored bases (max 3)', v_slots;
  END IF;
END $$;
EOF
)

  # Write body — three logical operations, all gated on _grant_gate.is_new:
  #   1. UPDATE dune.base_backups.player_id (with override_name option). The
  #      admin-backend pre-authorizes source_backup_id, so no admin-controller
  #      guard is needed; we only ROLLBACK if 0 rows updated (source vanished
  #      or bogus id).
  #   2. Drop an empty BaseBackupTool into the recipient's CHOAM bank
  #      (inventory_type = 30, keyed on pawn actor_id).
  #   3. Write the result_item_id back onto the audit row for rollback support.
  GRANT_BODY=$(cat <<'EOF'
-- G21a write 1: transfer base_backups ownership. The admin-backend's
-- /api/dune/bb/available-sources endpoint filters source backups before this
-- script sees a request, so we don't re-check ownership here. The 1/0 guard
-- below still fires if source_backup_id is bogus or the row vanished mid-flight.
SET LOCAL search_path TO dune, public;

WITH transferred AS (
  UPDATE dune.base_backups bb
     SET player_id = :recipient_controller_id,
         base_backup_name = CASE
           WHEN COALESCE(NULLIF(:'override_name',''), '') = ''
             THEN bb.base_backup_name
           ELSE :'override_name'
         END
   WHERE bb.id = :source_backup_id
     AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  RETURNING bb.id
)
SELECT CASE
  WHEN EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
       AND NOT EXISTS (SELECT 1 FROM transferred)
    THEN 1/0  -- force ROLLBACK: source_backup_id does not exist
  ELSE 1
END;

-- G21a write 2: deliver empty BaseBackupTool to recipient's CHOAM bank. Bank
-- inventory is keyed on the pawn actor_id (researcher correction 2026-05-23);
-- we resolved pawn_id at the top of the builder and bound it as :recipient_pawn_id.
WITH bank AS (
  SELECT i.id AS inv_id
    FROM dune.inventories i
   WHERE i.actor_id = :recipient_pawn_id
     AND i.inventory_type = 30
),
nextpos AS (
  SELECT COALESCE(MAX(position_index), -1) + 1 AS p
    FROM dune.items WHERE inventory_id = (SELECT inv_id FROM bank)
),
new_tool AS (
  INSERT INTO dune.items
    (inventory_id, stack_size, position_index, template_id,
     quality_level, stats, acquisition_time, is_new)
  SELECT bank.inv_id, 1, nextpos.p, 'BaseBackupTool', 0,
         '{"FCustomizationStats":[[],{}],"FItemStackAndDurabilityStats":[[],{"DecayedMaxDurability":0.0}]}'::jsonb,
         0, true
    FROM bank, nextpos
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  RETURNING id, inventory_id
)
-- G21a write 3: stash the new item_id on the audit row for rollback.
UPDATE dune.ls_progression_grants gp
   SET detail = detail
                || jsonb_build_object('result_item_id', nt.id,
                                      'result_inventory_id', nt.inventory_id)
  FROM new_tool nt, (SELECT grant_id FROM _grant_gate) g
 WHERE gp.id = g.grant_id;
EOF
)
}

# G21b — bb_clone. ONLINE-SAFE deep-clone of a base subgraph anchored on a
# source totem actor. The 16-step procedure (see
# docs/dune-research/ITEM-G21-BUILD-SPEC.md) duplicates every dune.actors row
# in the source set, every linked dune.fgl_entities + actor_fgl_entities row,
# every dune.buildings + building_instances + placeables + inventories + items
# row, and rewrites every internal !!act#<id> reference inside the actors'
# properties JSONB. The source dune.base_backups row is left untouched, so the
# admin can clone the same reference base for multiple recipients.
#
# All work inside a single TX. The cleanup trigger on dune.actor_fgl_entities
# GCs orphaned fgl_entities on DELETE, so a mid-clone ROLLBACK leaves the DB
# clean even if we already minted new entity IDs.
#
# Required detail: recipient_account_id, source_backup_id.
# Optional detail: backup_name (label visible to the recipient; defaults to
#                                "<source_name> (Last Sietch Starter)").
#
# recipient_account_id is the player's account_id (matches the admin UI's
# selectedPlayer.account_id). We resolve it here to (controller_id, pawn_id)
# via dune.encrypted_player_state — controller_id drives base_backups.player_id
# and the slot-cap check; pawn_id drives the CHOAM bank inventory lookup
# (inventory_type=30 is keyed on pawn actor_id).
#
# Recipient slot cap (max 3 base_backups) checked in preflight, same as G21a.
build_bb_clone_grant() {
  local recipient_account_id source_backup_id backup_name
  local recipient_controller_id recipient_pawn_id
  recipient_account_id=$(jq_get_nested detail recipient_account_id)
  source_backup_id=$(jq_get_nested detail source_backup_id)
  backup_name=$(jq_get_nested detail backup_name)

  [[ "$recipient_account_id" =~ ^[0-9]+$ ]] \
    || fail_json "invalid recipient_account_id (must be digits): $recipient_account_id" 2
  [[ "$source_backup_id" =~ ^[0-9]+$ ]] \
    || fail_json "invalid source_backup_id (must be digits): $source_backup_id" 2

  if [[ -n "$backup_name" ]]; then
    if [[ ${#backup_name} -gt 128 ]]; then
      fail_json "backup_name too long (max 128 chars)" 2
    fi
    if [[ "$backup_name" =~ [[:cntrl:]] ]]; then
      fail_json "backup_name contains control characters" 2
    fi
  fi

  # Resolve recipient account_id -> (controller_id, pawn_id). Same pattern as
  # bb_handoff and the standard grant flow.
  # RESOLVE_TARGET_SQL, not a bare account_id lookup: encrypted_player_state
  # has one row per character SLOT, and psql_scalar strips whitespace, so a
  # 2-slot recipient CONCATENATES both ids into a garbage number instead of
  # failing — exactly the "land on a garbage id" outcome this block warns about.
  recipient_controller_id=$(psql_scalar \
    "${RESOLVE_TARGET_SQL//@COL@/eps.player_controller_id}" \
    -v "account_id=${recipient_account_id}")
  recipient_pawn_id=$(psql_scalar \
    "${RESOLVE_TARGET_SQL//@COL@/eps.player_pawn_id}" \
    -v "account_id=${recipient_account_id}")

  if [[ -z "$recipient_controller_id" || -z "$recipient_pawn_id" ]]; then
    fail_json "could not resolve controller/pawn for recipient account ${recipient_account_id}" 4
  fi
  [[ "$recipient_controller_id" =~ ^[0-9]+$ ]] \
    || fail_json "resolved recipient controller_id not numeric: $recipient_controller_id" 4
  [[ "$recipient_pawn_id" =~ ^[0-9]+$ ]] \
    || fail_json "resolved recipient pawn_id not numeric: $recipient_pawn_id" 4

  DETAIL_JSON=$(jq -nc \
    --argjson ra "$recipient_account_id" \
    --argjson rc "$recipient_controller_id" \
    --argjson rp "$recipient_pawn_id" \
    --argjson s "$source_backup_id" \
    --arg n "$backup_name" \
    '{recipient_account_id:$ra, recipient_controller_id:$rc,
      recipient_pawn_id:$rp, source_backup_id:$s}
     + (if $n == "" then {} else {backup_name:$n} end)')

  PSQL_VARS+=( -v "recipient_controller_id=${recipient_controller_id}" \
               -v "recipient_pawn_id=${recipient_pawn_id}" \
               -v "source_backup_id=${source_backup_id}" \
               -v "backup_name=${backup_name}" )

  # Preflight: recipient slot cap + the source backup must exist + the source
  # set must contain exactly one totem actor (v1 limitation per spec; we widen
  # later if Last Sietch ships multi-totem starter packs).
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G21b preflight: slot cap + source-backup existence + single-totem check.
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_recipient bigint; v_source bigint;
        v_slots int; v_total_actors int; v_totem_count int;
BEGIN
  SET LOCAL search_path TO dune, public;
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT (gp.detail->>'recipient_controller_id')::bigint,
         (gp.detail->>'source_backup_id')::bigint
    INTO v_recipient, v_source
    FROM dune.ls_progression_grants gp WHERE gp.id = g.grant_id;
  IF v_recipient IS NULL OR v_source IS NULL THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: recipient_controller_id or source_backup_id missing';
  END IF;

  SELECT COUNT(*) INTO v_slots
    FROM dune.base_backups WHERE player_id = v_recipient;
  IF v_slots >= 3 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: recipient has % stored bases (max 3)', v_slots;
  END IF;

  SELECT COUNT(*) INTO v_total_actors
    FROM dune.base_backup_linked_actors WHERE id = v_source;
  IF v_total_actors = 0 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: source_backup_id % has no linked actors (or does not exist)', v_source;
  END IF;

  SELECT COUNT(*) INTO v_totem_count
    FROM dune.base_backup_linked_actors bbla
    JOIN dune.actors a ON a.id = bbla.actor_id
   WHERE bbla.id = v_source
     AND a.class LIKE '%Totem%';
  IF v_totem_count <> 1 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: source_backup_id % has % totem actors (expected exactly 1)',
                    v_source, v_totem_count;
  END IF;
END $$;
EOF
)

  # The 16-step body. We use two transaction-scoped TEMP tables to hold ID
  # maps. Every write predicates on _grant_gate.is_new for replay safety; on
  # a replay the TEMP tables are repopulated empty (TRUNCATE) and every step
  # matches zero rows.
  #
  # Step-by-step:
  #   0. (preflight handled the source set existence + single-totem rule)
  #   1. Allocate ID maps:
  #      - _g21_actor_map (old_actor_id -> new_actor_id) — one row per
  #        source actor (linked via base_backup_linked_actors).
  #      - _g21_entity_map (old_entity_id -> new_entity_id) — one row per
  #        source actor_fgl_entities row.
  #   2. INSERT new dune.actors rows (copy class/transform/etc; properties is
  #      rewritten in step 2b).
  #   2b. Regex-rewrite !!act#<old>([^0-9]) -> !!act#<new>\1 across the cloned
  #       actors' properties JSONB. The [^0-9] anchor prevents !!act#66 from
  #       matching inside !!act#660.
  #   3. INSERT new dune.fgl_entities rows (components JSONB AS-IS — per spec
  #      m_ConnectedCircuit is a per-base index, NOT an entity_id).
  #   4. INSERT new dune.actor_fgl_entities rows (slot_name preserved).
  #   5. INSERT new dune.totems row (last_backup_timestamp NULL — clone is
  #      cooldown-free at restore time).
  #   6. INSERT new dune.landclaim_segments rows.
  #   7. INSERT new dune.buildings rows.
  #   8. INSERT new dune.placeables rows (owner_entity_id translated).
  #   9. INSERT new dune.building_instances rows (owner_entity_id translated;
  #      instance_id preserved — it's per-building unique).
  #  10. INSERT new dune.inventories rows.
  #  11. INSERT new dune.actor_inventories rows.
  #  12. INSERT new dune.items rows.
  #  13. INSERT dune.actor_state rows ('BaseBackup' for every cloned actor).
  #  14. (SKIP permission_actor — Funcom save already deletes those.)
  #  15. INSERT dune.base_backups + base_backup_linked_actors rows.
  #  16. Deliver empty BaseBackupTool to recipient's CHOAM bank + write all
  #      minted IDs onto the audit row for rollback.
  GRANT_BODY=$(cat <<'EOF'
-- G21b write: deep-clone the source totem subgraph. Single transaction; the
-- actor_fgl_entities_cleanup_orphaned_entities trigger GCs cloned fgl_entities
-- if we abort.
SET LOCAL search_path TO dune, public;

CREATE TEMP TABLE IF NOT EXISTS _g21_actor_map
  (old_actor_id bigint, new_actor_id bigint) ON COMMIT DROP;
CREATE TEMP TABLE IF NOT EXISTS _g21_entity_map
  (old_entity_id bigint, new_entity_id bigint) ON COMMIT DROP;
TRUNCATE _g21_actor_map;
TRUNCATE _g21_entity_map;

-- Step 1a: actor ID map. One row per source linked actor.
INSERT INTO _g21_actor_map (old_actor_id, new_actor_id)
SELECT bbla.actor_id, nextval('dune.actors_id_seq')
  FROM dune.base_backup_linked_actors bbla
 WHERE bbla.id = :source_backup_id
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 1b: entity ID map. One row per source actor_fgl_entities row across
-- the entire actor set (covers multi-slot actors like 'Actor' +
-- 'ContainerInventory').
INSERT INTO _g21_entity_map (old_entity_id, new_entity_id)
SELECT afe.entity_id, nextval('dune.ls_fgl_entity_id_seq')
  FROM dune.actor_fgl_entities afe
  JOIN _g21_actor_map m ON m.old_actor_id = afe.actor_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 1c: collision check on the minted entity IDs (sequence isolation in
-- pg gives us a fresh range per call, but the Last Sietch-reserved start at 5e18 is
-- only safe as long as no other writer has scribbled there. Belt + braces).
DO $$
DECLARE v_collisions int;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  SELECT COUNT(*) INTO v_collisions
    FROM dune.fgl_entities fe
    JOIN _g21_entity_map m ON m.new_entity_id = fe.entity_id;
  IF v_collisions > 0 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: % minted entity_ids already exist in dune.fgl_entities', v_collisions;
  END IF;
END $$;

-- Step 2: new actors. Copy class/map/transform/partition/dimension/gas AS-IS
-- and rewrite every !!act#<old_id> reference inside properties JSONB to the
-- corresponding !!act#<new_id>. The [^0-9] anchor in the regex prevents
-- !!act#66 from accidentally matching the leading bytes of !!act#660.
--
-- We do the rewrite in a PL/pgSQL DO block:
--   pass 1: for each (old,new) pair, swap !!act#<old>X -> __G21_<new>__X
--           inside every source actor's properties text. The placeholder is
--           regex-safe (no '#') so pass 2 can untangle it.
--   pass 2: swap __G21_<new>__ -> !!act#<new>. This is the single, final
--           form that gets INSERTed.
-- After both passes complete we INSERT the resulting actors. owner_account_id
-- is NULL to match Funcom's saved-state shape (the recipient repopulates
-- permissions when they interact with the totem post-restore).
DO $$
DECLARE
  v_map_row record;
  v_actor   record;
  v_new_props text;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;

  -- One row per source actor; rewrite + insert in place.
  FOR v_actor IN
    SELECT a.id           AS old_id,
           m.new_actor_id AS new_id,
           a.class, a.map, a.partition_id, a.dimension_index,
           a.transform, a.gas_attributes, a.properties
      FROM dune.actors a
      JOIN _g21_actor_map m ON m.old_actor_id = a.id
  LOOP
    v_new_props := v_actor.properties::text;

    -- Pass 1: source-id -> placeholder. Anchor: only swap !!act#<old> when
    -- followed by a non-digit (or end-of-string) so !!act#66 vs !!act#660
    -- can never collide. The replacement carries the matched non-digit byte
    -- back via \1, or for end-of-string ($) anchor we use a separate pass.
    FOR v_map_row IN SELECT old_actor_id, new_actor_id FROM _g21_actor_map LOOP
      v_new_props := regexp_replace(
        v_new_props,
        '!!act#' || v_map_row.old_actor_id::text || '([^0-9])',
        '__G21_' || v_map_row.new_actor_id::text || '__\1',
        'g');
      -- Also catch end-of-string occurrences (rare but possible if the id
      -- literally terminates a JSON string value).
      v_new_props := regexp_replace(
        v_new_props,
        '!!act#' || v_map_row.old_actor_id::text || '$',
        '__G21_' || v_map_row.new_actor_id::text || '__',
        'g');
    END LOOP;

    -- Pass 2: placeholder -> final !!act# form. This pass is unambiguous —
    -- placeholders carry the NEW id so a single regexp_replace per row
    -- collapses them, regardless of how many distinct new ids appear.
    v_new_props := regexp_replace(
      v_new_props,
      '__G21_([0-9]+)__',
      '!!act#\1',
      'g');

    INSERT INTO dune.actors
      (id, class, map, partition_id, dimension_index, transform, gas_attributes,
       properties, owner_account_id)
    VALUES
      (v_actor.new_id, v_actor.class, v_actor.map, v_actor.partition_id,
       v_actor.dimension_index, v_actor.transform, v_actor.gas_attributes,
       v_new_props::jsonb, NULL);
  END LOOP;
END $$;

-- Step 2b: post-pass invariant — no cloned actor's properties JSONB may
-- still reference a source actor id (via !!act# or via the placeholder).
-- If any survive, abort.
DO $$
DECLARE v_bad int; v_pattern text;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  v_pattern := '!!act#(' ||
               (SELECT string_agg(old_actor_id::text, '|') FROM _g21_actor_map)
               || ')([^0-9]|$)';
  SELECT COUNT(*) INTO v_bad
    FROM dune.actors a
   WHERE a.id IN (SELECT new_actor_id FROM _g21_actor_map)
     AND (
       a.properties::text ~ '__G21_'
       OR a.properties::text ~ v_pattern
     );
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: % cloned actor(s) still reference source-id strings after rewrite', v_bad;
  END IF;
END $$;

-- Step 3: new fgl_entities. components JSONB copied AS-IS (m_ConnectedCircuit
-- is a per-base index, NOT an entity_id — verified pre-build per spec).
INSERT INTO dune.fgl_entities (entity_id, components)
SELECT m.new_entity_id, fe.components
  FROM dune.fgl_entities fe
  JOIN _g21_entity_map m ON m.old_entity_id = fe.entity_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 4: new actor_fgl_entities. slot_name preserved.
INSERT INTO dune.actor_fgl_entities (actor_id, slot_name, entity_id)
SELECT am.new_actor_id, afe.slot_name, em.new_entity_id
  FROM dune.actor_fgl_entities afe
  JOIN _g21_actor_map  am ON am.old_actor_id  = afe.actor_id
  JOIN _g21_entity_map em ON em.old_entity_id = afe.entity_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 5: new totems row (for the cloned totem actor). last_backup_timestamp
-- intentionally NULL so the cooldown is fresh on restore.
--
-- G22 bundled bugfix (ITEM-G21-BUILD-SPEC §8 + ITEM-G22-BUILD-SPEC §8 / F4):
-- the original 2-column INSERT left landclaim_vertical_level,
-- landclaim_original_global_location, landclaim_original_global_yaw_rotation
-- as NULL. Game tolerates NULL there (those columns get rewritten on first
-- restore — empirically confirmed F4) but the schema's design intent is to
-- have them populated. Carry the source totem's values through, falling back
-- to a Hagga-safe anchor when NULL. The landclaim_original_global_location
-- default uses the 0-indexed '[0:2]={x,y,z}'::real[] literal — Funcom C++
-- reads arr[0..2] so a 1-indexed ARRAY[...]::real[] would surface NULL at
-- index 0 and silently corrupt world coords.
INSERT INTO dune.totems (id, landclaim_vertical_level, last_backup_timestamp,
                         landclaim_original_global_location,
                         landclaim_original_global_yaw_rotation)
SELECT am.new_actor_id,
       COALESCE(t.landclaim_vertical_level, 0),
       NULL,
       COALESCE(t.landclaim_original_global_location,
                '[0:2]={-94000.0,-379000.0,15900.0}'::real[]),
       COALESCE(t.landclaim_original_global_yaw_rotation, 0.0)
  FROM dune.totems t
  JOIN _g21_actor_map am ON am.old_actor_id = t.id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 6: new landclaim_segments rows (totem_id translated). Schema is
-- (totem_id, grid_location_x, grid_location_y). Explicit column list avoids
-- the SELECT ls.* duplicate-totem_id column-count mismatch.
INSERT INTO dune.landclaim_segments (totem_id, grid_location_x, grid_location_y)
SELECT am.new_actor_id, ls.grid_location_x, ls.grid_location_y
  FROM dune.landclaim_segments ls
  JOIN _g21_actor_map am ON am.old_actor_id = ls.totem_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 7: new buildings rows. owner_id NULL to match saved-state.
INSERT INTO dune.buildings (id, owner_id)
SELECT am.new_actor_id, NULL
  FROM dune.buildings b
  JOIN _g21_actor_map am ON am.old_actor_id = b.id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 8: new placeables rows. owner_entity_id translated via entity map
-- (points at the cloned totem's Actor-slot entity).
INSERT INTO dune.placeables (id, owner_entity_id)
SELECT am.new_actor_id,
       CASE WHEN p.owner_entity_id IS NULL THEN NULL
            ELSE (SELECT em.new_entity_id
                    FROM _g21_entity_map em
                   WHERE em.old_entity_id = p.owner_entity_id) END
  FROM dune.placeables p
  JOIN _g21_actor_map am ON am.old_actor_id = p.id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 9: new building_instances rows. building_id translated; instance_id
-- preserved (per-building unique); owner_entity_id translated.
--
-- G22 bundled bugfix (ITEM-G21-BUILD-SPEC §8 /):
-- the original column list referenced `hologram` and `provides_stability` which
-- do NOT exist on dune.building_instances (those names live on
-- dune.building_blueprint_instances, the G20 paste table — different schema).
-- Real columns per the live `\d dune.building_instances`: building_flags
-- (integer), health (real NOT NULL), shelter (smallint NOT NULL),
-- stabilization_begin_timespan/end_timespan/state (bigint/bigint/smallint),
-- sand_buildup (smallint). Carry every non-default column through; let the
-- stabilization_* and sand_buildup columns ride their schema DEFAULTs (0).
INSERT INTO dune.building_instances
  (building_id, instance_id, building_type, transform, owner_entity_id,
   building_flags, health, shelter)
SELECT am.new_actor_id, bi.instance_id, bi.building_type, bi.transform,
       CASE WHEN bi.owner_entity_id IS NULL THEN NULL
            ELSE (SELECT em.new_entity_id
                    FROM _g21_entity_map em
                   WHERE em.old_entity_id = bi.owner_entity_id) END,
       bi.building_flags, bi.health, bi.shelter
  FROM dune.building_instances bi
  JOIN _g21_actor_map am ON am.old_actor_id = bi.building_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 9b: post-step-9 invariant — every cloned placeables.owner_entity_id
-- and building_instances.owner_entity_id must point at our minted entity
-- set (no leakage to source entities).
DO $$
DECLARE v_leak int;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  SELECT COUNT(*) INTO v_leak
    FROM dune.placeables p
   WHERE p.id IN (SELECT new_actor_id FROM _g21_actor_map)
     AND p.owner_entity_id IS NOT NULL
     AND p.owner_entity_id NOT IN (SELECT new_entity_id FROM _g21_entity_map);
  IF v_leak > 0 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: % cloned placeable(s) have an owner_entity_id outside the new entity set', v_leak;
  END IF;
  SELECT COUNT(*) INTO v_leak
    FROM dune.building_instances bi
   WHERE bi.building_id IN (SELECT new_actor_id FROM _g21_actor_map)
     AND bi.owner_entity_id IS NOT NULL
     AND bi.owner_entity_id NOT IN (SELECT new_entity_id FROM _g21_entity_map);
  IF v_leak > 0 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: % cloned building_instance(s) have an owner_entity_id outside the new entity set', v_leak;
  END IF;
END $$;

-- Step 10: new inventories rows. Mint fresh IDs from the sequence; carry
-- inventory_type / max_item_count / max_item_volume; null out variant FKs
-- (exchange_id, item_id, vehicle_module_id) — clones use the actor_id path.
--
-- We do the inserts row-by-row in a PL/pgSQL loop so we can record the exact
-- (old_inventory_id -> new_inventory_id) pairing per source row. The earlier
-- CTE approach (JOIN ins ON ins.actor_id = s.new_actor) cross-products when
-- an actor owns 2+ inventories of different types (totems and silos commonly
-- carry both a container + utility slot), corrupting the items mapping at
-- step 12.
CREATE TEMP TABLE IF NOT EXISTS _g21_inv_map
  (old_inventory_id bigint, new_inventory_id bigint) ON COMMIT DROP;
TRUNCATE _g21_inv_map;

DO $$
DECLARE
  v_src record;
  v_new_inv bigint;
  v_dupe int;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;

  FOR v_src IN
    SELECT inv.id              AS old_inv,
           am.new_actor_id     AS new_actor,
           inv.inventory_type, inv.max_item_count, inv.max_item_volume
      FROM dune.inventories inv
      JOIN _g21_actor_map am ON am.old_actor_id = inv.actor_id
  LOOP
    INSERT INTO dune.inventories
      (id, actor_id, inventory_type, max_item_count, max_item_volume,
       exchange_id, item_id, vehicle_module_id)
    VALUES
      (nextval('dune.inventories_id_seq'),
       v_src.new_actor, v_src.inventory_type,
       v_src.max_item_count, v_src.max_item_volume,
       NULL, NULL, NULL)
    RETURNING id INTO v_new_inv;

    INSERT INTO _g21_inv_map (old_inventory_id, new_inventory_id)
    VALUES (v_src.old_inv, v_new_inv);
  END LOOP;

  -- Assert every old_inv maps to exactly 1 new_inv. This should be tautological
  -- given the row-by-row loop, but a duplicate would indicate something is
  -- inserting into _g21_inv_map outside this block — abort loudly.
  SELECT COUNT(*) INTO v_dupe FROM (
    SELECT old_inventory_id FROM _g21_inv_map
     GROUP BY old_inventory_id HAVING COUNT(*) > 1
  ) d;
  IF v_dupe > 0 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: % old inventory id(s) map to multiple new ids', v_dupe;
  END IF;
END $$;

-- Step 11: new actor_inventories rows. inventory_id translated;
-- component_name_hash preserved AS-IS.
--
-- actor_inventories is (inventory_id, component_name_hash) only — verified
-- against live schema 2026-05-24. The old 3-column form referenced a
-- non-existent actor_id column (the `actor_id` lives upstream on
-- dune.inventories.actor_id, not on this join table). The _g21_actor_map JOIN
-- stays in place so the FK closure (actor_id -> new_actor_id) is exercised
-- before this insert runs, but actor_id itself isn't written here.
INSERT INTO dune.actor_inventories (inventory_id, component_name_hash)
SELECT im.new_inventory_id, ai.component_name_hash
  FROM dune.actor_inventories ai
  JOIN dune.inventories inv     ON inv.id              = ai.inventory_id
  JOIN _g21_actor_map am        ON am.old_actor_id     = inv.actor_id
  JOIN _g21_inv_map  im         ON im.old_inventory_id = ai.inventory_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 12: new items rows. inventory_id translated; stats/template_id/
-- stack_size/quality_level/position_index preserved AS-IS.
INSERT INTO dune.items
  (inventory_id, stack_size, position_index, template_id,
   quality_level, stats, acquisition_time, is_new)
SELECT im.new_inventory_id, it.stack_size, it.position_index, it.template_id,
       it.quality_level, it.stats, it.acquisition_time, it.is_new
  FROM dune.items it
  JOIN _g21_inv_map im ON im.old_inventory_id = it.inventory_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 13: actor_state ('BaseBackup') for every cloned actor. ON CONFLICT
-- DO NOTHING because actor_state.actor_id is the PK — defends against any
-- duplicate id in _g21_actor_map (shouldn't happen, but cheap insurance).
INSERT INTO dune.actor_state (actor_id, state)
SELECT am.new_actor_id, 'BaseBackup'
  FROM _g21_actor_map am
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
ON CONFLICT (actor_id) DO NOTHING;

-- Step 14: skip permission_actor / permission_actor_rank (Funcom save deletes
-- these; recipient repopulates on first interaction with the new totem).

-- Step 15: new base_backups row + linked_actors fanout. The
-- base_backup_linked_actors.id column is the FK back to base_backups.id —
-- every row shares the same id value (the new backup id).
CREATE TEMP TABLE IF NOT EXISTS _g21_backup_state
  (new_backup_id bigint) ON COMMIT DROP;
TRUNCATE _g21_backup_state;

INSERT INTO _g21_backup_state (new_backup_id)
WITH src_bb AS (
  SELECT bb.base_backup_name FROM dune.base_backups bb WHERE bb.id = :source_backup_id
),
new_bb AS (
  INSERT INTO dune.base_backups (player_id, base_backup_name)
  SELECT :recipient_controller_id,
         CASE WHEN COALESCE(NULLIF(:'backup_name',''),'') = ''
              THEN src_bb.base_backup_name || ' (Last Sietch Starter)'
              ELSE :'backup_name' END
    FROM src_bb
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  RETURNING id
)
SELECT id FROM new_bb;

INSERT INTO dune.base_backup_linked_actors (id, actor_id)
SELECT bs.new_backup_id, am.new_actor_id
  FROM _g21_backup_state bs, _g21_actor_map am
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 15b: post-step-15 invariant — recipient's base_backups count must be
-- exactly the new backup row (we already gated <3 in preflight).
DO $$
DECLARE v_recipient bigint; v_slots int;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  SELECT (gp.detail->>'recipient_controller_id')::bigint INTO v_recipient
    FROM dune.ls_progression_grants gp
    JOIN _grant_gate g ON g.grant_id = gp.id;
  SELECT COUNT(*) INTO v_slots FROM dune.base_backups WHERE player_id = v_recipient;
  IF v_slots > 3 THEN
    RAISE EXCEPTION 'BB_CLONE_FAIL: recipient now has % stored bases (>3 — preflight passed but body over-inserted?)', v_slots;
  END IF;
END $$;

-- Step 16a: deliver empty BaseBackupTool to recipient's CHOAM bank.
CREATE TEMP TABLE IF NOT EXISTS _g21_item_state
  (item_id bigint, inventory_id bigint) ON COMMIT DROP;
TRUNCATE _g21_item_state;

INSERT INTO _g21_item_state (item_id, inventory_id)
WITH bank AS (
  SELECT i.id AS inv_id
    FROM dune.inventories i
   WHERE i.actor_id = :recipient_pawn_id
     AND i.inventory_type = 30
),
nextpos AS (
  SELECT COALESCE(MAX(position_index), -1) + 1 AS p
    FROM dune.items WHERE inventory_id = (SELECT inv_id FROM bank)
),
new_tool AS (
  INSERT INTO dune.items
    (inventory_id, stack_size, position_index, template_id,
     quality_level, stats, acquisition_time, is_new)
  SELECT bank.inv_id, 1, nextpos.p, 'BaseBackupTool', 0,
         '{"FCustomizationStats":[[],{}],"FItemStackAndDurabilityStats":[[],{"DecayedMaxDurability":0.0}]}'::jsonb,
         0, true
    FROM bank, nextpos
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  RETURNING id, inventory_id
)
SELECT id, inventory_id FROM new_tool;

-- Step 16b: stamp every minted ID onto the audit row so rollback is just a
-- matter of replaying the IDs from detail JSON.
UPDATE dune.ls_progression_grants gp
   SET detail = detail
                || jsonb_build_object(
                     'new_backup_id',     (SELECT new_backup_id FROM _g21_backup_state),
                     'new_actor_ids',     (SELECT jsonb_agg(new_actor_id ORDER BY new_actor_id)
                                             FROM _g21_actor_map),
                     'new_entity_ids',    (SELECT jsonb_agg(new_entity_id ORDER BY new_entity_id)
                                             FROM _g21_entity_map),
                     'new_inventory_ids', (SELECT COALESCE(jsonb_agg(new_inventory_id ORDER BY new_inventory_id), '[]'::jsonb)
                                             FROM _g21_inv_map),
                     'result_item_id',      (SELECT item_id      FROM _g21_item_state),
                     'result_inventory_id', (SELECT inventory_id FROM _g21_item_state))
  FROM (SELECT grant_id FROM _grant_gate) g
 WHERE gp.id = g.grant_id;
EOF
)
}

# G22 — import_solido_to_basebackup. ONLINE-SAFE synthesis of a placeable,
# restorable base subgraph from Solido JSON alone — no source totem required
# on the server side. Builds the actor + fgl_entity + totem + buildings +
# placeables + building_instances + inventory subgraph from scratch using a
# per-class default registry (dune.ls_solido_class_defaults), links it via
# dune.base_backups + dune.base_backup_linked_actors, and delivers an empty
# BaseBackupTool to the recipient's CHOAM bank. On first use the recipient
# opens the tool, the new slot appears, and they place the design at a
# Hagga site of their choice.
#
# Hybrid of G20 + G21: input parsing reuses G20's Solido JSON validators
# (byte/piece caps, building_type charset, numeric coords); DB write structure
# mirrors G21 deep-clone (id maps + multi-step gated INSERTs) except every
# actor is freshly minted from a registry default rather than copied from a
# source row. See docs/dune-research/ITEM-G22-BUILD-SPEC.md for the 18-step
# procedure and the empirical findings (F1-F6) backing the design.
#
# Required detail: recipient_account_id, blueprint_data (Solido JSON object),
#                  base_backup_name (the recipient-facing label).
# Optional detail: source_blueprint_id (Solido UUID, audit only — admin-backend
#                                       resolves the UUID before invocation).
#
# Constraints:
#   - Hagga-only (every minted actor: map='HaggaBasin', partition_id=1).
#   - Slot cap 3 (refuse-at-cap; v1.5 --overwrite-slot deferred per spec §7a).
#   - MTX placeables refused (full_class_path under /Game/DLC/ → caller
#     filters before invocation; class-registry miss → REFUSE here too).
#   - v1 SKIPS items population (empty containers).
#   - v1 SKIPS landclaim_segments (per F6 — game synthesizes at restore time).
#   - All Funcom proc invocations + table refs prefixed with
#     SET LOCAL search_path TO dune, public per.
build_import_solido_to_basebackup_grant() {
  local recipient_account_id base_backup_name blueprint_data source_id
  local recipient_controller_id recipient_pawn_id
  recipient_account_id=$(jq_get_nested detail recipient_account_id)
  base_backup_name=$(jq_get_nested detail base_backup_name)
  source_id=$(jq_get_nested detail source_blueprint_id)

  [[ "$recipient_account_id" =~ ^[0-9]+$ ]] \
    || fail_json "invalid recipient_account_id (must be digits): $recipient_account_id" 2

  [[ -n "$base_backup_name" ]] \
    || fail_json "detail.base_backup_name is required for G22 import_solido_to_basebackup" 2
  # Cap aligned with admin-backend/routers/dune_grant.py:753 + form hint
  # ("1..200 chars"). dune.permission_actor.actor_name is plain text (no
  # length constraint per live `\d` 2026-05-24) so 200 is safe.
  if [[ ${#base_backup_name} -gt 200 ]]; then
    fail_json "base_backup_name too long (max 200 chars)" 2
  fi
  if [[ "$base_backup_name" =~ [[:cntrl:]] ]]; then
    fail_json "base_backup_name contains control characters" 2
  fi

  # blueprint_data: same shape as G20 import_blueprint (Solido JSON object with
  # instances/placeables/pentashields arrays). The admin-backend resolves Solido
  # UUIDs before invocation; bash side only handles inline JSON.
  blueprint_data=$(printf '%s' "$GRANT_JSON" \
    | jq -c '.detail.blueprint_data // empty')
  if [[ -z "$blueprint_data" || "$blueprint_data" == "null" ]]; then
    fail_json "detail.blueprint_data is required (admin-backend resolves Solido UUIDs)" 2
  fi
  if ! printf '%s' "$blueprint_data" | jq -e 'type == "object"' >/dev/null 2>&1; then
    fail_json "detail.blueprint_data must be a JSON object" 2
  fi

  local bytes
  bytes=$(printf '%s' "$blueprint_data" | wc -c)
  if (( bytes > CAP_BLUEPRINT_BYTES )); then
    fail_json "blueprint_data ${bytes} bytes exceeds cap ${CAP_BLUEPRINT_BYTES}" 2
  fi

  local n_inst n_plac n_pent piece_total mtx_count
  n_inst=$(printf '%s' "$blueprint_data" | jq '(.instances // []) | length')
  n_plac=$(printf '%s' "$blueprint_data" | jq '(.placeables // []) | length')
  n_pent=$(printf '%s' "$blueprint_data" | jq '(.pentashields // []) | length')
  [[ "$n_inst" =~ ^[0-9]+$ ]] || fail_json "could not parse instances length" 2
  [[ "$n_plac" =~ ^[0-9]+$ ]] || fail_json "could not parse placeables length" 2
  [[ "$n_pent" =~ ^[0-9]+$ ]] || fail_json "could not parse pentashields length" 2
  piece_total=$(( n_inst + n_plac + n_pent ))
  if (( piece_total < 1 )); then
    fail_json "blueprint_data has zero pieces (need at least one instance or placeable)" 2
  fi
  if (( piece_total > CAP_BLUEPRINT_PIECES )); then
    fail_json "blueprint piece count ${piece_total} exceeds cap ${CAP_BLUEPRINT_PIECES}" 2
  fi

  # Per-element validation: building_type charset, numeric coords. Same jq
  # predicates as G20 import_blueprint — defends the dollar-quoted JSONB embed
  # below and surfaces malformed Solido payloads early.
  if ! printf '%s' "$blueprint_data" | jq -e '
        (.instances // []) | all(
          (.building_type // "" | test("^[A-Za-z0-9_-]+$"))
          and ((.x // 0) | type == "number")
          and ((.y // 0) | type == "number")
          and ((.z // 0) | type == "number")
          and ((.rotation // 0) | type == "number")
        )
      ' >/dev/null 2>&1; then
    fail_json "blueprint_data.instances has invalid building_type or non-numeric coords" 2
  fi
  if ! printf '%s' "$blueprint_data" | jq -e '
        (.placeables // []) | all(
          (.building_type // "" | test("^[A-Za-z0-9_-]+$"))
          and ((.x // 0) | type == "number")
          and ((.y // 0) | type == "number")
          and ((.z // 0) | type == "number")
          and ((.rx // 0) | type == "number")
          and ((.ry // 0) | type == "number")
          and ((.rz // 0) | type == "number")
        )
      ' >/dev/null 2>&1; then
    fail_json "blueprint_data.placeables has invalid building_type or non-numeric coords" 2
  fi

  mtx_count=$(printf '%s' "$blueprint_data" | jq '
    ((.instances // []) + (.placeables // []))
    | map(select(.building_type | startswith("MTX_"))) | length')
  [[ "$mtx_count" =~ ^[0-9]+$ ]] || mtx_count=0

  # MTX placeables are refused in v1 (per spec §6) — the class registry has no
  # entry for /Game/DLC/* paths and we cannot synthesize default_components.
  # MTX building_instances (instances list) ARE allowed — Funcom's
  # base_backup_finish_placing accepts them at restore-time with only a
  # cosmetic preview warning (empirically confirmed via G20 paste 2026-05-24).
  local mtx_placeables
  mtx_placeables=$(printf '%s' "$blueprint_data" | jq -r '
    (.placeables // [])
    | map(select(.building_type | startswith("MTX_")))
    | map(.building_type) | unique | join(", ")')
  if [[ -n "$mtx_placeables" ]]; then
    fail_json "G22_MTX_PLACEABLE_NOT_SUPPORTED: MTX placeables refused in v1: ${mtx_placeables}. Strip them from the Solido and re-submit." 2
  fi

  # Resolve recipient account_id → (controller_id, pawn_id). Same pattern as
  # bb_handoff / bb_clone — controller_id drives base_backups.player_id +
  # permission_actor_rank.player_id; pawn_id drives the CHOAM bank lookup
  # (inventory_type=30 is pawn-keyed per researcher correction 2026-05-23).
  # RESOLVE_TARGET_SQL, not a bare account_id lookup: encrypted_player_state
  # has one row per character SLOT, and psql_scalar strips whitespace, so a
  # 2-slot recipient CONCATENATES both ids into a garbage number instead of
  # failing — exactly the "land on a garbage id" outcome this block warns about.
  recipient_controller_id=$(psql_scalar \
    "${RESOLVE_TARGET_SQL//@COL@/eps.player_controller_id}" \
    -v "account_id=${recipient_account_id}")
  recipient_pawn_id=$(psql_scalar \
    "${RESOLVE_TARGET_SQL//@COL@/eps.player_pawn_id}" \
    -v "account_id=${recipient_account_id}")
  if [[ -z "$recipient_controller_id" || -z "$recipient_pawn_id" ]]; then
    fail_json "could not resolve controller/pawn for recipient account ${recipient_account_id}" 4
  fi
  [[ "$recipient_controller_id" =~ ^[0-9]+$ ]] \
    || fail_json "resolved recipient controller_id not numeric: $recipient_controller_id" 4
  [[ "$recipient_pawn_id" =~ ^[0-9]+$ ]] \
    || fail_json "resolved recipient pawn_id not numeric: $recipient_pawn_id" 4

  DETAIL_JSON=$(jq -nc \
    --argjson ra "$recipient_account_id" \
    --argjson rc "$recipient_controller_id" \
    --argjson rp "$recipient_pawn_id" \
    --arg n "$base_backup_name" \
    --arg s "$source_id" \
    --argjson pt "$piece_total" \
    --argjson ic "$n_inst" \
    --argjson pc "$n_plac" \
    --argjson sc "$n_pent" \
    --argjson mc "$mtx_count" \
    '{recipient_account_id:$ra, recipient_controller_id:$rc,
      recipient_pawn_id:$rp, base_backup_name:$n,
      piece_count:$pt, instance_count:$ic, placeable_count:$pc,
      pentashield_count:$sc, mtx_instance_count:$mc}
     + (if $s == "" then {} else {source_blueprint_id:$s} end)')

  PSQL_VARS+=( -v "recipient_controller_id=${recipient_controller_id}" \
               -v "recipient_pawn_id=${recipient_pawn_id}" \
               -v "base_backup_name=${base_backup_name}" )

  # Preflight: recipient slot cap + unknown-class refuse. Re-checked inside the
  # transaction body too (step 3) so a race between validator and apply still
  # rolls back cleanly. Both checks read recipient_controller_id from the
  # audit row's detail (psql -v vars are NOT substituted inside $$ bodies).
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G22 preflight: slot cap + class-registry coverage.
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_recipient bigint; v_slots int;
BEGIN
  SET LOCAL search_path TO dune, public;
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT (gp.detail->>'recipient_controller_id')::bigint INTO v_recipient
    FROM dune.ls_progression_grants gp WHERE gp.id = g.grant_id;
  IF v_recipient IS NULL THEN
    RAISE EXCEPTION 'G22_FAIL: recipient_controller_id missing from detail';
  END IF;
  SELECT COUNT(*) INTO v_slots
    FROM dune.base_backups WHERE player_id = v_recipient;
  IF v_slots >= 3 THEN
    RAISE EXCEPTION 'G22_SLOT_CAP_REACHED: recipient % already owns % backups (max 3)',
                    v_recipient, v_slots;
  END IF;
END $$;
EOF
)

  # Body: 14 logical steps (spec §4 steps 1-18 collapsed; steps 4/10/15 are
  # no-ops in v1). Every write predicates on _grant_gate.is_new for replay
  # safety; the TEMP tables (_g22_stage_*, _g22_*_map, _g22_backup_state,
  # _g22_item_state) drop on COMMIT.
  #
  # Synthetic local IDs (used as row keys in _g22_actor_map):
  #   0     = totem actor
  #   1     = synthetic single building shell actor (v1 rule)
  #   2..N+1 = placeable actors, one per Solido placeable (solido_index + 2)
  #
  # Array literal convention (CRITICAL): dune.totems.landclaim_original_global_location
  # MUST use '[0:2]={x,y,z}'::real[] (0-indexed). Funcom C++ reads arr[0..2];
  # a 1-indexed ARRAY[...]::real[] would surface NULL at index 0 and silently
  # corrupt world coords. Post-insert invariant asserts array_lower = 0.
  GRANT_BODY=$(cat <<EOF
-- G22 write: synthesize base subgraph from Solido JSON. Single transaction.
-- The actor_fgl_entities_cleanup_orphaned_entities trigger GCs minted
-- fgl_entities if we abort.
SET LOCAL search_path TO dune, public;

-- Step 1: stage the Solido JSON into temp tables. We use jsonb_array_elements
-- with WITH ORDINALITY to keep the source array order as solido_index.
CREATE TEMP TABLE IF NOT EXISTS _g22_stage_placeables (
  solido_index    int     NOT NULL,
  class_short     text    NOT NULL,
  rel_x           real    NOT NULL,
  rel_y           real    NOT NULL,
  rel_z           real    NOT NULL,
  pitch           real    NOT NULL,
  yaw             real    NOT NULL,
  roll            real    NOT NULL,
  health_override real
) ON COMMIT DROP;
-- N3: dropped staging .building_type column. dune.placeables.building_type
-- is now sourced from dune.ls_solido_class_defaults.placeables_building_type
-- (captured empirically per class — see scripts/capture-placeable-defaults.sh).
-- Derivation from class_short was wrong on at least Door + Choam_FloorLamp_2
-- (captured: 'Choam_Shelter_Door_Placeable', 'Choam_LightFloor_Placeable').
TRUNCATE _g22_stage_placeables;

CREATE TEMP TABLE IF NOT EXISTS _g22_stage_building_instances (
  instance_id     integer NOT NULL,
  building_type   text    NOT NULL,
  transform       real[]  NOT NULL,
  building_flags  integer NOT NULL DEFAULT 0,
  health          real    NOT NULL DEFAULT 100.0,
  shelter         smallint NOT NULL DEFAULT 0
) ON COMMIT DROP;
TRUNCATE _g22_stage_building_instances;

INSERT INTO _g22_stage_placeables
  (solido_index, class_short, rel_x, rel_y, rel_z, pitch, yaw, roll,
   health_override)
SELECT (e.ord - 1)::int,
       e.elem->>'building_type',
       COALESCE((e.elem->>'x')::real, 0),
       COALESCE((e.elem->>'y')::real, 0),
       COALESCE((e.elem->>'z')::real, 0),
       COALESCE((e.elem->>'rx')::real, 0),
       COALESCE((e.elem->>'ry')::real, 0),
       COALESCE((e.elem->>'rz')::real, 0),
       NULL::real
  FROM jsonb_array_elements(
         COALESCE(NULLIF(\$bpjson\$${blueprint_data}\$bpjson\$::jsonb->'placeables', 'null'::jsonb),
                  '[]'::jsonb))
       WITH ORDINALITY e(elem, ord)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

INSERT INTO _g22_stage_building_instances
  (instance_id, building_type, transform, building_flags, health, shelter)
SELECT COALESCE((e.elem->>'instance_id')::int, e.ord::int),
       e.elem->>'building_type',
       ('[0:3]={'
          || COALESCE((e.elem->>'x')::real, 0)::text || ','
          || COALESCE((e.elem->>'y')::real, 0)::text || ','
          || COALESCE((e.elem->>'z')::real, 0)::text || ','
          || COALESCE((e.elem->>'rotation')::real, 0)::text
        || '}')::real[],
       0,
       100.0,
       0
  FROM jsonb_array_elements(
         COALESCE(NULLIF(\$bpjson\$${blueprint_data}\$bpjson\$::jsonb->'instances', 'null'::jsonb),
                  '[]'::jsonb))
       WITH ORDINALITY e(elem, ord)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 2: class-registry coverage check. Refuse with the FULL list of missing
-- classes so the operator can capture them all in one helper run.
DO \$\$
DECLARE v_missing text;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  SELECT string_agg(DISTINCT s.class_short, ', ' ORDER BY s.class_short)
    INTO v_missing
    FROM _g22_stage_placeables s
    LEFT JOIN dune.ls_solido_class_defaults d ON d.class_short_name = s.class_short
   WHERE d.class_short_name IS NULL;
  IF v_missing IS NOT NULL THEN
    RAISE EXCEPTION 'G22_UNKNOWN_PLACEABLE_CLASS: %', v_missing;
  END IF;
END \$\$;

-- Step 2b: registry-readiness preflight (N2 + OQ2). Refuse the whole grant if
-- ANY staged class has an incomplete registry row. Catches the failure modes
-- the OQ1 empirical-capture work was designed to prevent — silently shipping
-- a base where a placeable has empty default_components (no render / no
-- interact), an unknown placeables_building_type (wrong Funcom enum), or an
-- explicitly disabled class (is_active=false, e.g. uncaptured v1.1 backlog).
-- One declarative check, single rejection message naming all bad classes.
DO \$\$
DECLARE v_bad text;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  WITH per_class AS (
    SELECT DISTINCT s.class_short, d.default_components, d.placeables_building_type, d.is_active
      FROM _g22_stage_placeables s
      JOIN dune.ls_solido_class_defaults d ON d.class_short_name = s.class_short
  ),
  bad AS (
    SELECT pc.class_short,
           string_agg(chk.reason, ',' ORDER BY chk.reason) AS reasons
      FROM per_class pc
      CROSS JOIN LATERAL (
        VALUES
          ('default_components_empty',
           pc.default_components = '{}'::jsonb OR pc.default_components IS NULL),
          ('placeables_building_type_null',
           pc.placeables_building_type IS NULL),
          ('inactive', NOT pc.is_active)
      ) AS chk(reason, isbad)
     WHERE chk.isbad
     GROUP BY pc.class_short
  )
  SELECT string_agg(class_short || ' [' || reasons || ']', ', ' ORDER BY class_short)
    INTO v_bad
    FROM bad;
  IF v_bad IS NOT NULL THEN
    RAISE EXCEPTION 'G22_REGISTRY_NOT_READY: %', v_bad;
  END IF;
END \$\$;

-- Step 3: slot-cap re-check inside the TX (defence against validator-vs-apply
-- race where the recipient saves a new base between admin-backend gate and
-- bash exec).
DO \$\$
DECLARE g _grant_gate%ROWTYPE;
        v_recipient bigint; v_count int;
BEGIN
  SET LOCAL search_path TO dune, public;
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT (gp.detail->>'recipient_controller_id')::bigint INTO v_recipient
    FROM dune.ls_progression_grants gp WHERE gp.id = g.grant_id;
  SELECT COUNT(*) INTO v_count
    FROM dune.base_backups WHERE player_id = v_recipient;
  IF v_count >= 3 THEN
    RAISE EXCEPTION 'G22_SLOT_CAP_REACHED: recipient % already owns % backups (max 3) [body recheck]',
                    v_recipient, v_count;
  END IF;
END \$\$;

-- Step 5: allocate ID maps. synth_local_id: 0=totem, 1=building shell,
-- 2..N+1=placeables.
CREATE TEMP TABLE IF NOT EXISTS _g22_actor_map
  (synth_local_id int PRIMARY KEY, new_actor_id bigint NOT NULL) ON COMMIT DROP;
CREATE TEMP TABLE IF NOT EXISTS _g22_entity_map
  (synth_local_id int NOT NULL, slot_name text NOT NULL,
   new_entity_id bigint NOT NULL,
   PRIMARY KEY (synth_local_id, slot_name)) ON COMMIT DROP;
CREATE TEMP TABLE IF NOT EXISTS _g22_inv_map
  (synth_local_id int PRIMARY KEY, new_inventory_id bigint NOT NULL) ON COMMIT DROP;
TRUNCATE _g22_actor_map;
TRUNCATE _g22_entity_map;
TRUNCATE _g22_inv_map;

-- Mint actor IDs. Totem + building shell are unconditional; placeable count
-- comes from the staged Solido rows.
INSERT INTO _g22_actor_map (synth_local_id, new_actor_id)
SELECT 0, nextval('dune.actors_id_seq')
  WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
UNION ALL
SELECT 1, nextval('dune.actors_id_seq')
  WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
UNION ALL
SELECT s.solido_index + 2, nextval('dune.actors_id_seq')
  FROM _g22_stage_placeables s
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Mint entity IDs. Every actor has an 'Actor' slot. Placeables whose registry
-- entry sets has_container_inventory=true also get a 'ContainerInventory' slot.
INSERT INTO _g22_entity_map (synth_local_id, slot_name, new_entity_id)
SELECT 0, 'Actor', nextval('dune.ls_fgl_entity_id_seq')
  WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
UNION ALL
SELECT 1, 'Actor', nextval('dune.ls_fgl_entity_id_seq')
  WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
UNION ALL
SELECT s.solido_index + 2, 'Actor', nextval('dune.ls_fgl_entity_id_seq')
  FROM _g22_stage_placeables s
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
UNION ALL
SELECT s.solido_index + 2, 'ContainerInventory', nextval('dune.ls_fgl_entity_id_seq')
  FROM _g22_stage_placeables s
  JOIN dune.ls_solido_class_defaults d ON d.class_short_name = s.class_short
 WHERE d.has_container_inventory = true
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Entity-id collision guard (same as G21 step 1c).
DO \$\$
DECLARE v_collisions int;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  SELECT COUNT(*) INTO v_collisions
    FROM dune.fgl_entities fe
    JOIN _g22_entity_map m ON m.new_entity_id = fe.entity_id;
  IF v_collisions > 0 THEN
    RAISE EXCEPTION 'G22_FAIL: % minted entity_ids already exist in dune.fgl_entities', v_collisions;
  END IF;
END \$\$;

-- Step 6: new dune.actors rows. Transform is a composite of (location vector,
-- rotation quaternion). For the synthetic anchor and the building shell we use
-- identity rotation (0,0,0,1). Placeables get their relative offset added to
-- the anchor; rotation is left identity in v1 — the game rewrites all
-- transforms on first restore (F4) so absolute coords here are throwaway.
--
-- m_SelfUniqueID = '!!act#<new_id>' is the Funcom saved-state invariant
-- (every actor's properties JSONB self-references its own ID).

-- Totem
INSERT INTO dune.actors
  (id, class, map, partition_id, dimension_index, transform, gas_attributes,
   properties, owner_account_id)
SELECT am.new_actor_id,
       'BP_Totem_C',
       'HaggaBasin',
       1, 0,
       ROW(
         ROW(-94000.0, -379000.0, 15900.0)::dune.vector,
         ROW(0, 0, 0, 1)::dune.quaternion
       )::dune.transform,
       '{}'::jsonb,
       jsonb_build_object(
         'BP_Totem_C',
         jsonb_build_object('m_SelfUniqueID', '!!act#' || am.new_actor_id::text)
       ),
       NULL
  FROM _g22_actor_map am
 WHERE am.synth_local_id = 0
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Building shell
INSERT INTO dune.actors
  (id, class, map, partition_id, dimension_index, transform, gas_attributes,
   properties, owner_account_id)
SELECT am.new_actor_id,
       'BP_DuneBuildingBase_C',
       'HaggaBasin',
       1, 0,
       ROW(
         ROW(-94000.0, -379000.0, 15900.0)::dune.vector,
         ROW(0, 0, 0, 1)::dune.quaternion
       )::dune.transform,
       '{}'::jsonb,
       jsonb_build_object(
         'BP_DuneBuildingBase_C',
         jsonb_build_object('m_SelfUniqueID', '!!act#' || am.new_actor_id::text)
       ),
       NULL
  FROM _g22_actor_map am
 WHERE am.synth_local_id = 1
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Placeables. class = registry full_class_path; properties wraps the class
-- short name (extracted from full_class_path via split_part) as the top-level
-- key and stores m_SelfUniqueID under it.
INSERT INTO dune.actors
  (id, class, map, partition_id, dimension_index, transform, gas_attributes,
   properties, owner_account_id)
SELECT am.new_actor_id,
       d.full_class_path,
       'HaggaBasin',
       1, 0,
       ROW(
         ROW(-94000.0 + s.rel_x, -379000.0 + s.rel_y, 15900.0 + s.rel_z)::dune.vector,
         ROW(0, 0, 0, 1)::dune.quaternion
       )::dune.transform,
       '{}'::jsonb,
       jsonb_build_object(
         split_part(d.full_class_path, '.', 2),
         jsonb_build_object('m_SelfUniqueID', '!!act#' || am.new_actor_id::text)
       ),
       NULL
  FROM _g22_stage_placeables s
  JOIN _g22_actor_map am ON am.synth_local_id = s.solido_index + 2
  JOIN dune.ls_solido_class_defaults d ON d.class_short_name = s.class_short
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 7: new dune.fgl_entities rows. components JSONB comes from the class
-- registry's default_components per (class, slot_name). Totem + building
-- shell components key off the registry's reserved 'Totem' / 'Building'
-- rows (seeded in dune-grant-schema.sql).
INSERT INTO dune.fgl_entities (entity_id, components)
SELECT em.new_entity_id,
       CASE
         WHEN em.synth_local_id = 0 THEN
           COALESCE((SELECT default_components
                       FROM dune.ls_solido_class_defaults
                      WHERE class_short_name = 'Totem'),
                    '{}'::jsonb)
         WHEN em.synth_local_id = 1 THEN
           COALESCE((SELECT default_components
                       FROM dune.ls_solido_class_defaults
                      WHERE class_short_name = 'Building'),
                    '{}'::jsonb)
         ELSE
           COALESCE(
             CASE WHEN em.slot_name = 'ContainerInventory'
                  THEN d.default_components->'ContainerInventory'
                  ELSE d.default_components->'Actor'
             END,
             '{}'::jsonb)
       END
  FROM _g22_entity_map em
  LEFT JOIN _g22_stage_placeables s
    ON s.solido_index + 2 = em.synth_local_id
  LEFT JOIN dune.ls_solido_class_defaults d
    ON d.class_short_name = s.class_short
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 8: new dune.actor_fgl_entities rows.
INSERT INTO dune.actor_fgl_entities (actor_id, slot_name, entity_id)
SELECT am.new_actor_id, em.slot_name, em.new_entity_id
  FROM _g22_entity_map em
  JOIN _g22_actor_map am ON am.synth_local_id = em.synth_local_id
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 9: new dune.totems row. Use the 0-indexed array literal for
-- landclaim_original_global_location so the game's arr[0..2] reads land
-- correctly. Yaw 0, vertical 0, last_backup_timestamp NULL (cooldown-free,
-- mirrors bb_clone). All four columns get rewritten on first restore (F4)
-- but we populate them at INSERT to match the schema's design intent.
INSERT INTO dune.totems
  (id, landclaim_vertical_level, last_backup_timestamp,
   landclaim_original_global_location, landclaim_original_global_yaw_rotation)
SELECT am.new_actor_id,
       0,
       NULL,
       '[0:2]={-94000.0,-379000.0,15900.0}'::real[],
       0.0
  FROM _g22_actor_map am
 WHERE am.synth_local_id = 0
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 9b: 0-indexed array invariant. If the literal syntax was wrong the
-- array lands 1-indexed and Funcom reads NULL at index 0 → silent corruption.
DO \$\$
DECLARE v_lower int; v_totem bigint;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;
  SELECT new_actor_id INTO v_totem FROM _g22_actor_map WHERE synth_local_id = 0;
  SELECT array_lower(landclaim_original_global_location, 1) INTO v_lower
    FROM dune.totems WHERE id = v_totem;
  IF v_lower IS DISTINCT FROM 0 THEN
    RAISE EXCEPTION 'G22_FAIL: landclaim_original_global_location lower bound = % (expected 0); Funcom would read NULL at index 0', v_lower;
  END IF;
END \$\$;

-- Step 10: SKIP landclaim_segments. Per F6 (spec §3): 16 of 18 live totems
-- have ZERO segment rows and function correctly; the game synthesizes
-- segments server-side at restore time as needed. v1 ships segment-less.

-- Step 11: new dune.buildings row (single synthetic building shell). owner_id
-- NULL per saved-state convention (bb_clone step 7 same).
INSERT INTO dune.buildings (id, owner_id)
SELECT am.new_actor_id, NULL
  FROM _g22_actor_map am
 WHERE am.synth_local_id = 1
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 12: new dune.placeables rows. owner_entity_id points at the totem's
-- 'Actor'-slot entity (F2 model). health falls back to the class registry's
-- default_health, then to a generic 100.0 if neither is set.
-- N3 (D1 fix): building_type sourced from the registry's empirical
-- placeables_building_type column, NOT derived from class_short — Door,
-- Choam_FloorLamp_2 and others have building_type values that don't follow
-- the simple class-short + _Placeable suffix pattern. The preflight check
-- ensures every staged class has a non-NULL placeables_building_type before
-- we reach here. (NB: no backticks in this comment — the heredoc is unquoted
-- so backticks would trigger bash command substitution.)
INSERT INTO dune.placeables
  (id, owner_entity_id, health, building_type, has_hit_ground,
   has_buildable_support, is_hologram)
SELECT am.new_actor_id,
       (SELECT em.new_entity_id FROM _g22_entity_map em
         WHERE em.synth_local_id = 0 AND em.slot_name = 'Actor'),
       COALESCE(s.health_override,
                NULLIF(d.default_properties->>'default_health','')::real,
                100.0),
       d.placeables_building_type,
       false, false, false
  FROM _g22_stage_placeables s
  JOIN _g22_actor_map am ON am.synth_local_id = s.solido_index + 2
  JOIN dune.ls_solido_class_defaults d ON d.class_short_name = s.class_short
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 13: new dune.building_instances rows. All instances live under the
-- single synthetic building actor (synth_local_id = 1). owner_entity_id
-- points at the totem's Actor-slot entity. stabilization_* + sand_buildup
-- ride their schema DEFAULT 0.
INSERT INTO dune.building_instances
  (building_id, instance_id, building_type, transform, owner_entity_id,
   building_flags, health, shelter)
SELECT (SELECT new_actor_id FROM _g22_actor_map WHERE synth_local_id = 1),
       sbi.instance_id,
       sbi.building_type,
       sbi.transform,
       (SELECT em.new_entity_id FROM _g22_entity_map em
         WHERE em.synth_local_id = 0 AND em.slot_name = 'Actor'),
       sbi.building_flags,
       sbi.health,
       sbi.shelter
  FROM _g22_stage_building_instances sbi
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 14: new dune.inventories + dune.actor_inventories rows for placeables
-- whose registry entry sets has_container_inventory=true. Row-by-row loop
-- so we can map (synth_local_id -> new_inventory_id) in _g22_inv_map.
-- v1 leaves containers EMPTY (step 15 skipped).
DO \$\$
DECLARE v_row record; v_new_inv bigint;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;

  FOR v_row IN
    SELECT am.synth_local_id, am.new_actor_id,
           d.inventory_type, d.inventory_max_count, d.inventory_max_volume,
           d.component_name_hash
      FROM _g22_stage_placeables s
      JOIN _g22_actor_map am ON am.synth_local_id = s.solido_index + 2
      JOIN dune.ls_solido_class_defaults d ON d.class_short_name = s.class_short
     WHERE d.has_container_inventory = true
  LOOP
    INSERT INTO dune.inventories
      (id, actor_id, inventory_type, max_item_count, max_item_volume,
       exchange_id, item_id, vehicle_module_id)
    VALUES
      (nextval('dune.inventories_id_seq'),
       v_row.new_actor_id, v_row.inventory_type,
       v_row.inventory_max_count, v_row.inventory_max_volume,
       NULL, NULL, NULL)
    RETURNING id INTO v_new_inv;

    INSERT INTO _g22_inv_map (synth_local_id, new_inventory_id)
    VALUES (v_row.synth_local_id, v_new_inv);

    -- actor_inventories is (inventory_id, component_name_hash) only — verified
    -- against live schema 2026-05-24. component_name_hash MUST be non-null or
    -- the in-game container UI silently fails to render.
    IF v_row.component_name_hash IS NULL THEN
      RAISE EXCEPTION 'G22_FAIL: class % has has_container_inventory=true but component_name_hash is NULL — capture from a live placement before granting',
                      (SELECT class_short FROM _g22_stage_placeables
                        WHERE solido_index + 2 = v_row.synth_local_id);
    END IF;
    INSERT INTO dune.actor_inventories (inventory_id, component_name_hash)
    VALUES (v_new_inv, v_row.component_name_hash);
  END LOOP;
END \$\$;

-- Step 15: SKIP items population. v1 ships empty containers.

-- Step 16: actor_state ('BaseBackup') for every minted actor.
INSERT INTO dune.actor_state (actor_id, state)
SELECT am.new_actor_id, 'BaseBackup'::dune.actorstate
  FROM _g22_actor_map am
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
ON CONFLICT (actor_id) DO NOTHING;

-- Step 17: permission_actor + permission_actor_rank rows (F2 model).
--   - Totem: permission_actor (actor_type=3, is_child=false) + 1 rank row
--           (rank=1 = OWNER, player_id = recipient's controller actor).
--   - Placeables: permission_actor only (actor_type=1, is_child=true).
--                 No rank rows — inherit from totem via is_child cascade.
--   - Building shells: NONE (empirically confirmed F2 table).
INSERT INTO dune.permission_actor
  (actor_id, actor_name, actor_type, access_level, is_child)
SELECT am.new_actor_id, :'base_backup_name', 3, 3, false
  FROM _g22_actor_map am
 WHERE am.synth_local_id = 0
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

INSERT INTO dune.permission_actor_rank
  (permission_actor_id, player_id, rank)
SELECT am.new_actor_id, :recipient_controller_id, 1
  FROM _g22_actor_map am
 WHERE am.synth_local_id = 0
   AND EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

INSERT INTO dune.permission_actor
  (actor_id, actor_name, actor_type, access_level, is_child)
SELECT am.new_actor_id,
       '##' || s.class_short || '_Placeable',
       1, 3, true
  FROM _g22_stage_placeables s
  JOIN _g22_actor_map am ON am.synth_local_id = s.solido_index + 2
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 18a: new dune.base_backups row + linked_actors fanout.

-- D3-b pre-step stash: snapshot recipient's base_backups count BEFORE the
-- INSERT below, so the post-step invariant can assert prev+1==post verbatim
-- per spec §4. Catches "double-write-to-dune.base_backups-outside-staged-path"
-- regressions that the _g22_backup_state row count alone would miss.
CREATE TEMP TABLE IF NOT EXISTS _g22_pre_bb
  (prev_count int) ON COMMIT DROP;
TRUNCATE _g22_pre_bb;
INSERT INTO _g22_pre_bb (prev_count)
SELECT (SELECT COUNT(*) FROM dune.base_backups
         WHERE player_id = :recipient_controller_id)
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

CREATE TEMP TABLE IF NOT EXISTS _g22_backup_state
  (new_backup_id bigint) ON COMMIT DROP;
TRUNCATE _g22_backup_state;

INSERT INTO _g22_backup_state (new_backup_id)
WITH new_bb AS (
  INSERT INTO dune.base_backups (player_id, base_backup_name)
  SELECT :recipient_controller_id, :'base_backup_name'
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  RETURNING id
)
SELECT id FROM new_bb;

INSERT INTO dune.base_backup_linked_actors (id, actor_id)
SELECT bs.new_backup_id, am.new_actor_id
  FROM _g22_backup_state bs, _g22_actor_map am
 WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);

-- Step 18b: post-step invariants — owner-entity leakage, rank-row count,
-- minted entity_id range, slot cap. D3 adds 4 more pure-read assertions
-- (actor_map size, base_backups +1 delta, actor_state count, permission_actor
-- count) so dry-run + smoke test self-verify the mint shape.
DO \$\$
DECLARE v_bad int;
        v_expected int;
        v_recipient bigint;
BEGIN
  SET LOCAL search_path TO dune, public;
  IF NOT EXISTS (SELECT 1 FROM _grant_gate WHERE is_new) THEN RETURN; END IF;

  -- Every minted entity_id must be in the Last Sietch-reserved range.
  SELECT COUNT(*) INTO v_bad
    FROM _g22_entity_map
   WHERE new_entity_id < 5000000000000000000;
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'G22_FAIL: % minted entity_id(s) below the Last Sietch-reserved 5e18 floor', v_bad;
  END IF;

  -- No placeables.owner_entity_id leakage outside our minted set.
  SELECT COUNT(*) INTO v_bad
    FROM dune.placeables p
   WHERE p.id IN (SELECT new_actor_id FROM _g22_actor_map)
     AND p.owner_entity_id IS NOT NULL
     AND p.owner_entity_id NOT IN (SELECT new_entity_id FROM _g22_entity_map);
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'G22_FAIL: % minted placeable(s) have an owner_entity_id outside the new entity set', v_bad;
  END IF;

  -- No building_instances.owner_entity_id leakage.
  SELECT COUNT(*) INTO v_bad
    FROM dune.building_instances bi
   WHERE bi.building_id IN (SELECT new_actor_id FROM _g22_actor_map)
     AND bi.owner_entity_id IS NOT NULL
     AND bi.owner_entity_id NOT IN (SELECT new_entity_id FROM _g22_entity_map);
  IF v_bad > 0 THEN
    RAISE EXCEPTION 'G22_FAIL: % minted building_instance(s) have an owner_entity_id outside the new entity set', v_bad;
  END IF;

  -- permission_actor_rank count must be exactly 1 (totem).
  SELECT COUNT(*) INTO v_bad
    FROM dune.permission_actor_rank
   WHERE permission_actor_id IN (SELECT new_actor_id FROM _g22_actor_map);
  IF v_bad <> 1 THEN
    RAISE EXCEPTION 'G22_FAIL: permission_actor_rank row count = % (expected 1 — totem owner)', v_bad;
  END IF;

  -- D3-a: actor_map size must equal staged placeable count + 2 (totem + building shell).
  SELECT COUNT(*) INTO v_bad FROM _g22_actor_map;
  SELECT COUNT(*) + 2 INTO v_expected FROM _g22_stage_placeables;
  IF v_bad <> v_expected THEN
    RAISE EXCEPTION 'G22_FAIL: _g22_actor_map size = %, expected % (staged placeables + 2)',
                    v_bad, v_expected;
  END IF;

  -- D3-b: strict +1 delta per spec §4 — recipient's post-step base_backups
  -- count == prev_count + 1. Catches both (a) silent drops in step 18a's CTE
  -- (prev+0==post) and (b) any rogue/duplicate INSERT outside the staged path
  -- (prev+2==post or more). prev_count stashed in _g22_pre_bb pre-INSERT.
  -- Belt-and-suspenders: _g22_backup_state row count + ownership trace.
  SELECT (gp.detail->>'recipient_controller_id')::bigint INTO v_recipient
    FROM dune.ls_progression_grants gp
    JOIN _grant_gate g ON g.grant_id = gp.id;

  SELECT COUNT(*) INTO v_bad FROM _g22_backup_state;
  IF v_bad <> 1 THEN
    RAISE EXCEPTION 'G22_FAIL: _g22_backup_state row count = %, expected 1 (step 18a CTE outcome)', v_bad;
  END IF;

  SELECT COUNT(*) INTO v_bad
    FROM dune.base_backups WHERE player_id = v_recipient;
  SELECT prev_count + 1 INTO v_expected FROM _g22_pre_bb;
  IF v_bad <> v_expected THEN
    RAISE EXCEPTION 'G22_BASE_BACKUP_DELTA_MISMATCH: post_count = %, expected % (prev_count + 1) for recipient %',
                    v_bad, v_expected, v_recipient;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM dune.base_backups bb
      JOIN _g22_backup_state bs ON bs.new_backup_id = bb.id
     WHERE bb.player_id = v_recipient
  ) THEN
    RAISE EXCEPTION 'G22_FAIL: new base_backups row not present for recipient %', v_recipient;
  END IF;

  -- D3-c: actor_state row count must equal _g22_actor_map size (every minted
  -- actor gets a BaseBackup state row at step 16).
  SELECT COUNT(*) INTO v_bad
    FROM dune.actor_state ast
   WHERE ast.actor_id IN (SELECT new_actor_id FROM _g22_actor_map)
     AND ast.state = 'BaseBackup'::dune.actorstate;
  SELECT COUNT(*) INTO v_expected FROM _g22_actor_map;
  IF v_bad <> v_expected THEN
    RAISE EXCEPTION 'G22_FAIL: actor_state(BaseBackup) row count = %, expected % (every minted actor)',
                    v_bad, v_expected;
  END IF;

  -- D3-d: permission_actor count must equal actor_map size - 1 (every minted
  -- actor EXCEPT the building shell gets a permission_actor row per F2).
  SELECT COUNT(*) INTO v_bad
    FROM dune.permission_actor pa
   WHERE pa.actor_id IN (SELECT new_actor_id FROM _g22_actor_map);
  SELECT COUNT(*) - 1 INTO v_expected FROM _g22_actor_map;
  IF v_bad <> v_expected THEN
    RAISE EXCEPTION 'G22_FAIL: permission_actor row count = %, expected % (actor_map size - 1, building shell excluded per F2)',
                    v_bad, v_expected;
  END IF;
END \$\$;

-- Step 18c: deliver empty BaseBackupTool to recipient's CHOAM bank
-- (inventory_type=30, pawn-keyed). Same proven path as G21 bb_clone step 16a.
CREATE TEMP TABLE IF NOT EXISTS _g22_item_state
  (item_id bigint, inventory_id bigint) ON COMMIT DROP;
TRUNCATE _g22_item_state;

INSERT INTO _g22_item_state (item_id, inventory_id)
WITH bank AS (
  SELECT i.id AS inv_id
    FROM dune.inventories i
   WHERE i.actor_id = :recipient_pawn_id
     AND i.inventory_type = 30
),
nextpos AS (
  SELECT COALESCE(MAX(position_index), -1) + 1 AS p
    FROM dune.items WHERE inventory_id = (SELECT inv_id FROM bank)
),
new_tool AS (
  INSERT INTO dune.items
    (inventory_id, stack_size, position_index, template_id,
     quality_level, stats, acquisition_time, is_new)
  SELECT bank.inv_id, 1, nextpos.p, 'BaseBackupTool', 0,
         '{"FCustomizationStats":[[],{}],"FItemStackAndDurabilityStats":[[],{"DecayedMaxDurability":0.0}]}'::jsonb,
         0, true
    FROM bank, nextpos
   WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new)
  RETURNING id, inventory_id
)
SELECT id, inventory_id FROM new_tool;

-- Step 18d: stamp every minted ID onto the audit row so rollback (spec §10)
-- and downstream telemetry can key off detail JSONB.
UPDATE dune.ls_progression_grants gp
   SET detail = detail
                || jsonb_build_object(
                     'result_backup_id',
                       (SELECT new_backup_id FROM _g22_backup_state),
                     'result_actor_ids',
                       (SELECT jsonb_agg(new_actor_id ORDER BY synth_local_id)
                          FROM _g22_actor_map),
                     'result_entity_ids',
                       (SELECT jsonb_agg(new_entity_id
                                         ORDER BY synth_local_id, slot_name)
                          FROM _g22_entity_map),
                     'result_inventory_ids',
                       (SELECT COALESCE(jsonb_agg(new_inventory_id
                                                  ORDER BY synth_local_id),
                                        '[]'::jsonb)
                          FROM _g22_inv_map),
                     'result_basebackuptool_item_id',
                       (SELECT item_id FROM _g22_item_state),
                     'result_basebackuptool_inventory_id',
                       (SELECT inventory_id FROM _g22_item_state))
  FROM (SELECT grant_id FROM _grant_gate) g
 WHERE gp.id = g.grant_id;
EOF
)
}

# G29 — Bank Items Batch. ONLINE-SAFE multi-item insert into the player's CHOAM
# bank (inventory_type=30). One audit row per panel submission with the full
# items array in detail JSONB; N item rows materialize in dune.items in a single
# transaction. Online-safe per our internal notes
# (proven 2026-05-23: PlantFiber x7 inserted live, instant render, withdrawable,
# persists across relog). Mirrors the G20 bank-delivery preflight + capacity
# pattern but batches N items in a single SQL pass.
#
# Detail shape:
#   { "items": [
#       { "template_id": "PlantFiber", "stack_size": 100, "quality": 0 },
#       ...
#     ] }
# Each item validated: template_id ^[A-Za-z0-9_]+$, stack in 1..CAP_ITEM_QTY,
# quality 0..5. Batch capped at CAP_BATCH_ITEMS.
build_bank_items_batch_grant() {
  local items_json batch_size
  items_json=$(printf '%s' "$GRANT_JSON" | jq -c '.detail.items // empty')
  if [[ -z "$items_json" || "$items_json" == "null" ]]; then
    fail_json "detail.items is required (array of {template_id, stack_size, quality})" 2
  fi
  if ! printf '%s' "$items_json" | jq -e 'type == "array"' >/dev/null 2>&1; then
    fail_json "detail.items must be a JSON array" 2
  fi
  batch_size=$(printf '%s' "$items_json" | jq 'length')
  [[ "$batch_size" =~ ^[0-9]+$ ]] || fail_json "could not parse items array length" 2
  if (( batch_size < 1 )); then
    fail_json "detail.items is empty (need at least one item)" 2
  fi
  if (( batch_size > CAP_BATCH_ITEMS )); then
    fail_json "items count ${batch_size} exceeds cap ${CAP_BATCH_ITEMS}" 2
  fi

  # Per-item validation. jq predicates fail fast if any entry is malformed.
  if ! printf '%s' "$items_json" | jq -e '
        all(
          (.template_id // "" | test("^[A-Za-z0-9_]+$"))
          and ((.stack_size // 0) | type == "number")
          and ((.stack_size // 0) >= 1)
          and ((.stack_size // 0) <= 10000)
          and ((.quality // 0) | type == "number")
          and ((.quality // 0) >= 0)
          and ((.quality // 0) <= 5)
        )
      ' >/dev/null 2>&1; then
    fail_json "detail.items has invalid template_id (alnum+_ only), stack_size (1..10000), or quality (0..5)" 2
  fi

  # Audit detail (carry the items array verbatim so a single grant_id documents
  # the whole batch — operator-friendly + replay-safe).
  DETAIL_JSON=$(jq -nc --argjson its "$items_json" --argjson bs "$batch_size" \
    '{items:$its, item_count:$bs, delivery:"bank"}')

  PSQL_VARS+=( -v "items=${items_json}" )

  # Preflight: resolve bank inventory + check capacity (used + batch_size <= cap).
  # Bank is keyed on player_pawn_id (researcher correction 2026-05-23 — bank is
  # NOT controller-keyed). inventory_type=30 differentiates from backpack=0.
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G29 preflight: resolve bank inventory + capacity check (sum batch_size).
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_inv bigint; v_cap int; v_used int; v_batch int;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT jsonb_array_length(gp.detail->'items') INTO v_batch
    FROM dune.ls_progression_grants gp WHERE gp.id = g.grant_id;
  SELECT inv.id, inv.max_item_count,
         (SELECT COUNT(*) FROM dune.items it WHERE it.inventory_id = inv.id)
    INTO v_inv, v_cap, v_used
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 30
   WHERE eps.account_id = g.account_id;
  IF v_inv IS NULL THEN
    RAISE EXCEPTION 'BANK_BATCH_FAIL: no bank inventory for account=% (player offline AND never logged in? bank is pawn-keyed)', g.account_id;
  END IF;
  IF v_used + v_batch > v_cap THEN
    RAISE EXCEPTION 'BANK_BATCH_FAIL: bank capacity exceeded (used % + batch % > cap %)',
                    v_used, v_batch, v_cap;
  END IF;
END $$;
EOF
)

  # G29 write: expand the items array via jsonb_array_elements WITH ORDINALITY
  # to compute successive position_index offsets; INSERT one row per item in a
  # single statement. Gated on _grant_gate.is_new for replay safety.
  GRANT_BODY=$(cat <<'EOF'
-- G29 write: batched INSERT into bank (inventory_type=30) at successive positions.
WITH bank AS (
  SELECT inv.id AS inv_id,
         COALESCE((SELECT MAX(position_index) FROM dune.items
                    WHERE inventory_id = inv.id), -1) AS max_pos
    FROM dune.encrypted_player_state eps
    JOIN dune.inventories inv
      ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 30
   WHERE eps.account_id = :account_id
),
new_items AS (
  SELECT (ord - 1) AS rn,
         (e->>'template_id') AS template_id,
         (e->>'stack_size')::int AS stack,
         (e->>'quality')::int AS quality
    FROM jsonb_array_elements(:'items'::jsonb)
         WITH ORDINALITY AS j(e, ord)
)
INSERT INTO dune.items
  (inventory_id, stack_size, position_index, template_id, stats,
   quality_level, acquisition_time, is_new)
SELECT
  bank.inv_id,
  ni.stack,
  bank.max_pos + ni.rn + 1,
  ni.template_id,
  '{}'::jsonb,
  ni.quality,
  0,
  true
FROM bank, new_items ni
WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# -----------------------------------------------------------------------------
# Container target (2026-06-26). A vehicle/storage cargo container is a WORLD
# actor with NO owner_account_id, so ownership is resolved via the authoritative
# permission chain (the same one dune-containers.py / the relay `containers-list`
# verb uses for the v2 admin Player Tools picker):
#   vehicle cargo : actors.id = permission_actor_rank.permission_actor_id (rank 1)
#                   -> par.player_id = eps.player_controller_id -> eps.account_id
#                   cargo inventory = inventory_type 0, max_item_count > 0
#   placed storage: placeables.owner_entity_id -> actor_fgl_entities.entity_id
#                   -> afe.actor_id = permission_actor_rank.permission_actor_id
#                   (rank 1) -> par.player_id -> eps -> account; inv on p.id type 0
# This is correct ownership (NOT proximity): it cleanly separates co-located
# allies' containers (proven 2026-06-26 — inv 36193 resolves to Medic, not the operator,
# even though their DD bases are ~6k units apart).
#
# container_ownership_guard_sql emits SQL binding :account_id + :target_inv that
# returns a single 1 iff :target_inv is a container inventory the account owns.
# The picker itself is the existing relay `containers-list` (dune-containers.py);
# we deliberately do NOT duplicate it here.
# -----------------------------------------------------------------------------
container_ownership_guard_sql() {
  cat <<'SQL'
WITH owned AS (
  -- vehicle cargo (type 0) owned via permission_actor_rank rank=1
  SELECT inv.id AS inv_id
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN dune.inventories inv
      ON inv.actor_id = a.id AND inv.inventory_type = 0 AND inv.max_item_count > 0
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
   WHERE par.rank = 1 AND eps.account_id = :account_id
     AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
          OR a.class ILIKE '%sandbike%'  OR a.class ILIKE '%crawler%'
          OR a.class ILIKE '%containervehicle%')
  UNION
  -- placed storage (type 0) owned via the placeable entity chain
  SELECT inv.id AS inv_id
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
      ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    JOIN dune.inventories inv ON inv.actor_id = p.id AND inv.inventory_type = 0
   WHERE p.is_hologram = false AND eps.account_id = :account_id
     AND p.building_type = ANY(ARRAY['SpiceSilo_Placeable','GenericContainer_Placeable',
            'StorageContainer_Placeable','MediumStorageContainer_Placeable']::text[])
)
SELECT 1 FROM owned WHERE inv_id = :target_inv LIMIT 1;
SQL
}

# G30 (2026-06-26) — container_items_batch. Batched INSERT of an items array into
# a chosen vehicle/storage container's type-0 inventory (detail.target_inv),
# guarded so the inventory must be one the target player owns (proximity). Mirror
# of build_bank_items_batch_grant but with an explicit target inventory instead
# of the pawn-keyed bank. NOT offline-gated: a world container is not held in the
# pawn's RAM; RAM-safety (commit while the container's cell is unloaded, then fly
# it out/in to surface the items) is the operator's responsibility.
build_container_items_batch_grant() {
  local items_json target_inv batch_size
  items_json=$(printf '%s' "$GRANT_JSON" | jq -c '.detail.items // empty')
  target_inv=$(jq_get_nested detail target_inv)

  if [[ -z "$items_json" || "$items_json" == "null" ]]; then
    fail_json "detail.items is required (array of {template_id, stack_size, quality})" 2
  fi
  if ! printf '%s' "$items_json" | jq -e 'type == "array"' >/dev/null 2>&1; then
    fail_json "detail.items must be a JSON array" 2
  fi
  [[ "$target_inv" =~ ^[0-9]+$ ]] || fail_json "detail.target_inv is required (a container inventory id)" 2

  batch_size=$(printf '%s' "$items_json" | jq 'length')
  [[ "$batch_size" =~ ^[0-9]+$ ]] || fail_json "could not parse items array length" 2
  (( batch_size >= 1 )) || fail_json "detail.items is empty (need at least one item)" 2
  (( batch_size <= CAP_CONTAINER_BATCH )) || fail_json "items count ${batch_size} exceeds cap ${CAP_CONTAINER_BATCH}" 2

  if ! printf '%s' "$items_json" | jq -e '
        all(
          (.template_id // "" | test("^[A-Za-z0-9_]+$"))
          and ((.stack_size // 0) | type == "number")
          and ((.stack_size // 0) >= 1) and ((.stack_size // 0) <= 10000)
          and ((.quality // 0) | type == "number")
          and ((.quality // 0) >= 0) and ((.quality // 0) <= 5)
        )' >/dev/null 2>&1; then
    fail_json "detail.items has invalid template_id (alnum+_ only), stack_size (1..10000), or quality (0..5)" 2
  fi

  # OWNERSHIP GUARD (anti-footgun): target_inv must be a container inventory the
  # account actually OWNS (permission_actor_rank rank=1 chain — same source as the
  # containers-list picker). Refuse a stranger's box or an unrelated inventory.
  local owned
  owned=$(printf '%s\n' "$(container_ownership_guard_sql)" \
            | run_psql -tA -v "account_id=${account_id}" \
                       -v "target_inv=${target_inv}" 2>/dev/null | tr -d '[:space:]')
  if [[ "$owned" != "1" ]]; then
    fail_json "target_inv ${target_inv} is not a container owned by account ${account_id} (must be one of the player's own vehicle-cargo or storage containers, as listed by containers-list). Refusing to write to an unowned inventory." 5
  fi

  DETAIL_JSON=$(jq -nc --argjson its "$items_json" --argjson bs "$batch_size" --argjson ti "$target_inv" \
    '{items:$its, item_count:$bs, target_inv:$ti, delivery:"container"}')
  PSQL_VARS+=( -v "items=${items_json}" -v "target_inv=${target_inv}" )

  # Preflight: capacity check on the target container. :target_inv cannot be read
  # inside a DO $$ body (psql does not substitute :vars there), so we read it back
  # from the audit row's detail jsonb (same trick the bank builder uses for batch).
  GRANT_PREFLIGHT=$(cat <<'EOF'
-- G30 preflight: resolve the target container + free-slot capacity check.
DO $$
DECLARE g _grant_gate%ROWTYPE;
        v_target bigint; v_cap int; v_used int; v_batch int;
BEGIN
  SELECT * INTO g FROM _grant_gate;
  IF NOT g.is_new THEN RETURN; END IF;
  SELECT (gp.detail->>'target_inv')::bigint, jsonb_array_length(gp.detail->'items')
    INTO v_target, v_batch
    FROM dune.ls_progression_grants gp WHERE gp.id = g.grant_id;
  SELECT inv.max_item_count,
         (SELECT COUNT(*) FROM dune.items it WHERE it.inventory_id = inv.id)
    INTO v_cap, v_used
    FROM dune.inventories inv WHERE inv.id = v_target AND inv.inventory_type = 0;
  IF v_cap IS NULL THEN
    RAISE EXCEPTION 'CONTAINER_BATCH_FAIL: target_inv % is not a type-0 container inventory', v_target;
  END IF;
  IF v_used + v_batch > v_cap THEN
    RAISE EXCEPTION 'CONTAINER_BATCH_FAIL: container slots exceeded (used % + batch % > cap %)',
                    v_used, v_batch, v_cap;
  END IF;
END $$;
EOF
)

  # G30 write: batched INSERT into the target container at successive positions.
  GRANT_BODY=$(cat <<'EOF'
-- G30 write: append the items array into target_inv (type-0 container) at
-- MAX(position_index)+1 successive slots. Gated on _grant_gate.is_new.
WITH tgt AS (
  SELECT :target_inv::bigint AS inv_id,
         COALESCE((SELECT MAX(position_index) FROM dune.items
                    WHERE inventory_id = :target_inv::bigint), -1) AS max_pos
),
new_items AS (
  SELECT (ord - 1) AS rn,
         (e->>'template_id') AS template_id,
         (e->>'stack_size')::int AS stack,
         (e->>'quality')::int AS quality
    FROM jsonb_array_elements(:'items'::jsonb)
         WITH ORDINALITY AS j(e, ord)
)
INSERT INTO dune.items
  (inventory_id, stack_size, position_index, template_id, stats,
   quality_level, acquisition_time, is_new)
SELECT
  tgt.inv_id, ni.stack, tgt.max_pos + ni.rn + 1, ni.template_id,
  '{}'::jsonb, ni.quality, 0, true
FROM tgt, new_items ni
WHERE EXISTS (SELECT 1 FROM _grant_gate WHERE is_new);
EOF
)
}

# =============================================================================
# Moderation trio (Phase C 2026-05-29): ban + unban builders.
#
# These DO NOT use the standard ls_progression_grants transaction template.
# Moderation writes land in lsadmin.bans + lsadmin.player_actions instead;
# the schema additions live at the bottom of dune-grant-schema.sql.
#
# Both builders run their OWN psql txn (BEGIN ... COMMIT, ON_ERROR_STOP=1) and
# `exit 0` directly; same pattern as defer_grant_row above. They never reach
# the standard TXN that follows the case dispatch.
#
# ban payload:
#   { "account_id", "fls_id", "reason", "note"?, "duration_minutes"?,
#     "banned_by", "idempotency_key" }
# unban payload:
#   { "account_id", "fls_id", "unban_reason", "unbanned_by", "idempotency_key" }
#
# fls_id charset matches dune.accounts.funcom_id (displayName#tag):
#   ^[A-Za-z0-9._#-]+$
#
# ban also triggers an immediate /root/dune-kick.py kick if the target is Online
# (or in reconnect_grace_period_end). The ban-watcher timer covers re-join
# kicks every 30s.
# =============================================================================

validate_fls_id() {
  [[ "$1" =~ ^[A-Za-z0-9._#-]+$ ]] \
    || fail_json "invalid fls_id (must match [A-Za-z0-9._#-]+): $1" 2
}

validate_admin_user() {
  # admin usernames: lowercase letters, digits, underscore, hyphen, dot. 1..64.
  [[ "$1" =~ ^[A-Za-z0-9._-]{1,64}$ ]] \
    || fail_json "invalid admin username (charset/length): $1" 2
}

# Insert (or no-op replay) a lsadmin.bans row + a matching player_actions row
# inside a single BEGIN..COMMIT. The idempotency_key serves the same role as
# in ls_progression_grants: a replay touches no data.
build_ban_grant() {
  local fls_id reason note duration_minutes banned_by
  fls_id=$(jq_get_nested detail fls_id)
  reason=$(jq_get_nested detail reason)
  note=$(jq_get_nested detail note)
  duration_minutes=$(jq_get_nested detail duration_minutes)
  banned_by=$(jq_get_nested detail banned_by)

  # Defence-in-depth fls_id resolution. The design says fls_id resolution
  # should be server-side, not via the admin-backend. If the payload omitted
  # it (admin-backend's /dune/grant/players lookup may fail or precede the
  # funcom_id column rollout), fall back to dune.accounts.funcom_id keyed by
  # account_id. This makes the builder self-healing against the FIX 4 race.
  if [[ -z "$fls_id" ]]; then
    resolve_db_pod
    fls_id=$(psql_scalar \
      "SELECT funcom_id FROM dune.accounts WHERE id = :account_id::bigint AND funcom_id IS NOT NULL AND funcom_id <> '' LIMIT 1;" \
      -v "account_id=${account_id}")
  fi

  validate_fls_id "$fls_id"
  [[ -n "$reason" ]] || fail_json "reason is required for ban" 2
  if [[ -n "$duration_minutes" ]]; then
    validate_int_in_range "$duration_minutes" 1 525600 "duration_minutes"
  else
    duration_minutes=""
  fi
  validate_admin_user "$banned_by"

  # idem + account_id + grant_type + operator already validated by do_grant().
  local dur_sql exp_sql
  if [[ -n "$duration_minutes" ]]; then
    dur_sql=":duration_minutes::int"
    exp_sql="NOW() + (:duration_minutes::int || ' minutes')::interval"
  else
    dur_sql="NULL::int"
    exp_sql="NULL::timestamptz"
  fi

  # Build the txn. INSERT ... ON CONFLICT (fls_id) DO UPDATE re-activates a
  # row whose previous active=false (i.e. unban then re-ban). For a NEW row
  # we always set active=true. The player_actions row is keyed by the
  # idempotency_key so replays are no-ops.
  local txn
  txn=$(cat <<EOF
BEGIN;
SET LOCAL search_path TO lsadmin, dune, public;

-- Idempotency: the audit row is gated on idempotency_key UNIQUE; ON CONFLICT
-- DO NOTHING makes a replay a no-op. The bans UPSERT below runs every time so
-- a re-submit with the same key still leaves the row consistent; but if the
-- audit insert was a no-op (replay), we return 'replay' instead of 'applied'.
WITH idem_check AS (
  INSERT INTO lsadmin.player_actions
    (idempotency_key, account_id, fls_id, action_type, reason, note,
     duration_minutes, admin_user)
  VALUES
    (:'idem'::uuid, :account_id::bigint, :'fls_id', 'ban', :'reason',
     NULLIF(:'note',''), ${dur_sql}, :'banned_by')
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
),
ban_upsert AS (
  INSERT INTO lsadmin.bans
    (fls_id, account_id, reason, note, duration_minutes,
     banned_at, expires_at, active, banned_by,
     unbanned_at, unban_reason, unbanned_by)
  VALUES
    (:'fls_id', :account_id::bigint, :'reason', NULLIF(:'note',''),
     ${dur_sql}, NOW(), ${exp_sql}, true, :'banned_by',
     NULL, NULL, NULL)
  ON CONFLICT (fls_id) DO UPDATE SET
    account_id        = EXCLUDED.account_id,
    reason            = EXCLUDED.reason,
    note              = EXCLUDED.note,
    duration_minutes  = EXCLUDED.duration_minutes,
    banned_at         = EXCLUDED.banned_at,
    expires_at        = EXCLUDED.expires_at,
    active            = true,
    banned_by         = EXCLUDED.banned_by,
    unbanned_at       = NULL,
    unban_reason      = NULL,
    unbanned_by       = NULL
  RETURNING id, active
)
SELECT 'RESULT|' || COALESCE((SELECT id FROM ban_upsert)::text, 'null')
       || '|' || CASE WHEN EXISTS(SELECT 1 FROM idem_check)
                       THEN 'applied' ELSE 'replay' END;

COMMIT;
EOF
)

  local -a vargs=(
    -v "idem=${idem}"
    -v "account_id=${account_id}"
    -v "fls_id=${fls_id}"
    -v "reason=${reason}"
    -v "note=${note}"
    -v "banned_by=${banned_by}"
  )
  [[ -n "$duration_minutes" ]] && vargs+=( -v "duration_minutes=${duration_minutes}" )

  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '{"success":true,"status":"dry-run","grant_type":"ban","account_id":%s,"fls_id":%s,"message":"dry-run: ban txn built, NOT executed"}\n' \
      "$account_id" "$(json_str "$fls_id")"
    exit 0
  fi

  local result
  if ! result=$(printf '%s\n' "$txn" | run_psql -tA "${vargs[@]}" 2>&1); then
    fail_json "ban transaction failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)" 6
  fi

  local row ban_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  ban_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$ban_id" ]] || ban_id="null"

  # Persistent iptables enforcement: drop every recent non-allowlisted source IP
  # for this account. The RMQ KickPlayer ServerCommand is a confirmed no-op on
  # this GA build, so a ban = a firewall drop of the player's source IP(s).
  # dune-ipban.py refreshes the IP map first, drops all recent IPs (tag
  # lsban:aid=N), and the ban-watcher re-applies on rejoin / new IP + restores
  # rules after a node reboot. Allowlisted IPs (mgmt/private/node/relay +
  # /etc/lastsietch/ipban-allowlist.txt) are never dropped. Offline target with no
  # recent IP = nothing dropped now; the watcher catches them on reconnect.
  # The kick_* response field names are retained for relay/UI contract stability.
  local kick_attempted=true kick_ok=false kick_skipped_reason="" kick_error=""
  local drop_out
  if drop_out=$(/root/dune-ipban.py drop --account-id "$account_id" 2>&1); then
    if printf '%s' "$drop_out" | grep -q '"dropped": \[\]'; then
      if printf '%s' "$drop_out" | grep -q '"skipped_allowlisted": \[\]'; then
        kick_skipped_reason="no_known_ip"
      else
        kick_skipped_reason="all_ips_allowlisted"
      fi
    else
      kick_ok=true
    fi
  else
    kick_skipped_reason="drop_failed"
    kick_error=$(printf '%s' "$drop_out" | tr '\n' ' ' | tail -c 200)
  fi

  local kick_skipped_reason_json kick_error_json
  kick_skipped_reason_json=$(json_str "$kick_skipped_reason")
  kick_error_json=$(json_str "$kick_error")

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","ban_id":%s,"grant_type":"ban","account_id":%s,"fls_id":%s,"kick_attempted":%s,"kick_ok":%s,"kick_skipped_reason":%s,"kick_error":%s,"message":"already applied: idempotent replay"}\n' \
      "$ban_id" "$account_id" "$(json_str "$fls_id")" "$kick_attempted" "$kick_ok" "$kick_skipped_reason_json" "$kick_error_json"
    exit 0
  fi
  printf '{"success":true,"status":"applied","ban_id":%s,"grant_type":"ban","account_id":%s,"fls_id":%s,"kick_attempted":%s,"kick_ok":%s,"kick_skipped_reason":%s,"kick_error":%s,"message":"ban applied; ban-watcher will re-kick on rejoin"}\n' \
    "$ban_id" "$account_id" "$(json_str "$fls_id")" "$kick_attempted" "$kick_ok" "$kick_skipped_reason_json" "$kick_error_json"
  exit 0
}
# NOTE: "kick_*" response fields above reflect the iptables source-IP DROP result
# (kick_ok=true means at least one recent IP was dropped). The ban-watcher
# re-applies the drop on rejoin / new IP; IP bans are VPN-avoidable (surfaced in
# the admin UI).

# Unban: flip lsadmin.bans.active=false + record audit row. No kick.
build_unban_grant() {
  local fls_id unban_reason unbanned_by
  fls_id=$(jq_get_nested detail fls_id)
  unban_reason=$(jq_get_nested detail unban_reason)
  unbanned_by=$(jq_get_nested detail unbanned_by)

  # Defence-in-depth fls_id resolution; see build_ban_grant comment above.
  if [[ -z "$fls_id" ]]; then
    resolve_db_pod
    fls_id=$(psql_scalar \
      "SELECT funcom_id FROM dune.accounts WHERE id = :account_id::bigint AND funcom_id IS NOT NULL AND funcom_id <> '' LIMIT 1;" \
      -v "account_id=${account_id}")
  fi

  validate_fls_id "$fls_id"
  [[ -n "$unban_reason" ]] || fail_json "unban_reason is required for unban" 2
  validate_admin_user "$unbanned_by"

  local txn
  txn=$(cat <<'EOF'
BEGIN;
SET LOCAL search_path TO lsadmin, dune, public;

-- Idempotency: same idempotency_key gate as ban above. The bans UPDATE is
-- naturally idempotent (re-running against an already-inactive row is a no-op).
WITH idem_check AS (
  INSERT INTO lsadmin.player_actions
    (idempotency_key, account_id, fls_id, action_type, reason, admin_user)
  VALUES
    (:'idem'::uuid, :account_id::bigint, :'fls_id', 'unban',
     :'unban_reason', :'unbanned_by')
  ON CONFLICT (idempotency_key) DO NOTHING
  RETURNING id
),
unban_update AS (
  UPDATE lsadmin.bans
     SET active        = false,
         unbanned_at   = NOW(),
         unban_reason  = :'unban_reason',
         unbanned_by   = :'unbanned_by'
   WHERE fls_id = :'fls_id'
     AND active = true
  RETURNING id
)
SELECT 'RESULT|' || COALESCE((SELECT id FROM unban_update)::text, 'null')
       || '|' || CASE WHEN EXISTS(SELECT 1 FROM idem_check)
                       THEN 'applied' ELSE 'replay' END;

COMMIT;
EOF
)

  local -a vargs=(
    -v "idem=${idem}"
    -v "account_id=${account_id}"
    -v "fls_id=${fls_id}"
    -v "unban_reason=${unban_reason}"
    -v "unbanned_by=${unbanned_by}"
  )

  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    printf '{"success":true,"status":"dry-run","grant_type":"unban","account_id":%s,"fls_id":%s,"message":"dry-run: unban txn built, NOT executed"}\n' \
      "$account_id" "$(json_str "$fls_id")"
    exit 0
  fi

  local result
  if ! result=$(printf '%s\n' "$txn" | run_psql -tA "${vargs[@]}" 2>&1); then
    fail_json "unban transaction failed: $(printf '%s' "$result" | tr '\n' ' ' | tail -c 400)" 6
  fi

  # Lift the iptables source-IP drops for this account (idempotent; safe on
  # replay). The RMQ kick path is dead, so unban = removing the firewall drop.
  local undrop_removed=0 undrop_out
  if undrop_out=$(/root/dune-ipban.py undrop --account-id "$account_id" 2>&1); then
    undrop_removed=$(printf '%s' "$undrop_out" | grep -oE '"rules_removed": [0-9]+' | grep -oE '[0-9]+' | head -1)
    [[ -n "$undrop_removed" ]] || undrop_removed=0
  fi

  local row ban_id state
  row=$(printf '%s' "$result" | grep -E '^RESULT\|' | tail -n1)
  ban_id=$(printf '%s' "$row" | cut -d'|' -f2 | tr -d '[:space:]')
  state=$(printf '%s' "$row" | cut -d'|' -f3 | tr -d '[:space:]')
  [[ -n "$ban_id" ]] || ban_id="null"

  if [[ "$state" == "replay" ]]; then
    printf '{"success":true,"status":"replay","ban_id":%s,"grant_type":"unban","account_id":%s,"fls_id":%s,"message":"already applied: idempotent replay"}\n' \
      "$ban_id" "$account_id" "$(json_str "$fls_id")"
    exit 0
  fi
  printf '{"success":true,"status":"applied","ban_id":%s,"grant_type":"unban","account_id":%s,"fls_id":%s,"rules_removed":%s,"message":"unban applied; %s firewall drop rule(s) removed"}\n' \
    "$ban_id" "$account_id" "$(json_str "$fls_id")" "$undrop_removed" "$undrop_removed"
  exit 0
}

# =============================================================================
# Entry point
# =============================================================================
DRY_RUN=0
GRANT_JSON=""

main() {
  if [[ $# -eq 0 ]]; then
    fail_json "usage: dune-grant.sh --list-players | --list-recent [N] | --grant-b64 <b64> [--dry-run]" 2
  fi

  # P3a: fail-fast if the tags-data.json sidecar is missing or malformed.
  # Required by G23/G24a/G24b/G25a builders; cheap to validate once up-front
  # so we never half-execute a grant against a broken sidecar.
  validate_tags_data

  # parse args (order-independent: --dry-run may precede or follow --grant-b64)
  local mode="" b64=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --list-players)  mode="list"; shift ;;
      --list-recent)
        mode="recent"
        recent_limit="${2:-20}"
        if [[ -n "${2:-}" && "$2" =~ ^[0-9]+$ ]]; then shift 2; else shift; fi
        ;;
      --grant-b64)
        mode="grant"
        b64="${2:-}"
        [[ -n "$b64" ]] || fail_json "--grant-b64 requires a base64 payload" 2
        shift 2
        ;;
      --grant-b64-stdin)
        # Read base64 payload from stdin; sidesteps ARG_MAX (~128KB on Linux)
        # for large blueprints (G20 Solido imports can exceed 200KB b64).
        mode="grant"
        b64=$(cat)
        b64="${b64//[[:space:]]/}"
        [[ -n "$b64" ]] || fail_json "--grant-b64-stdin requires a base64 payload on stdin" 2
        shift
        ;;
      --dry-run)       DRY_RUN=1; shift ;;
      *)               fail_json "unknown argument: $1" 2 ;;
    esac
  done

  case "$mode" in
    list)        list_players ;;
    recent)      list_recent "${recent_limit:-20}" ;;
    grant)
      [[ -n "$b64" ]] || fail_json "--grant-b64 requires a base64 payload" 2
      do_grant "$b64"
      ;;
    *)     fail_json "no mode given (--list-players or --grant-b64)" 2 ;;
  esac
}

main "$@"
