#!/usr/bin/env bash
# /opt/lastsietch-pod-watcher/watcher.sh   (repo mirror: ops/lastsietch-pod-watcher/watcher.sh)
# Monitor the configured persistent game pods; report to the configured private channel.
#
# Messages are written for NON-TECHNICAL admins but keep every technical detail.
# It now DISTINGUISHES:
#   - PLANNED RESTART  (a ServerRestart roll / admin action recreated the pod) = expected, low concern
#   - SERVER CRASH     (container died in place, restartCount++, exit 139/OOM) = unplanned, real risk
#   - SERVER REPLACED  (pod recreated with no matching planned roll) = investigate
#   - SERVER MISSING   (pod gone from cluster) = investigate now
# Saved-state and admission verification belong to the operation's recovery checks.

set -u
STATE_DIR=/var/lib/lastsietch-pod-watcher
STATE_FILE=$STATE_DIR/state.json
MAINT_FILE=$STATE_DIR/maintenance
ENV_FILE=/opt/lastsietch-pod-watcher/.discord.env
NAMESPACE=${LASTSIETCH_GAME_NAMESPACE:?set LASTSIETCH_GAME_NAMESPACE}
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

ALWAYS_ON_PODS=(
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-survival-1-pod-1
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-survival-1-pod-33
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-overmap-pod-2
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-sh-arrakeen-pod-3
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-sh-harkovillage-pod-4
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-deepdesert-1-pod-8
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-deepdesert-1-pod-31
)

# NOTE: DD labels corrected 2026-07-15 (were swapped). Canonical: DD PvE = partition 8, DD PvP = 31.
declare -A POD_LABEL=(
  [${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-survival-1-pod-1]="Hagga Basin (Survival_1)"
  [${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-survival-1-pod-33]="Amtal (Survival_1, partition 33, PvP)"
  [${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-overmap-pod-2]="Overmap"
  [${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-sh-arrakeen-pod-3]="Arrakeen hub (SH_Arrakeen)"
  [${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-sh-harkovillage-pod-4]="Harko Village hub (SH_HarkoVillage)"
  [${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-deepdesert-1-pod-8]="Deep Desert PvE (DeepDesert_1, partition 8)"
  [${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-deepdesert-1-pod-31]="Deep Desert PvP (DeepDesert_1, partition 31)"
)

mkdir -p "$STATE_DIR"
[ -f "$STATE_FILE" ] || echo '{}' > "$STATE_FILE"
source "$ENV_FILE"

post_discord() {
  local content=$1
  curl -fsS -X POST \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg c "$content" '{content:$c,allowed_mentions:{parse:[]}}')" \
    "https://discord.com/api/v10/channels/${DISCORD_CH_BOT_LOGS}/messages" >/dev/null
}

# --- maintenance window: suppress health alerts during an ANNOUNCED/planned boot ---
# Armed via maint-window.sh (writes an expiry epoch to $MAINT_FILE). Pod state is still
# tracked while muted, so the planned boot is ABSORBED into state and does not fire once
# the window ends. Self-heals: an expired (or malformed) marker auto-clears and posts a
# one-time "monitoring resumed" note, so a forgotten window can never mute forever.
maint_active() {
  [ -f "$MAINT_FILE" ] || return 1
  local exp now
  exp=$(head -n1 "$MAINT_FILE" 2>/dev/null | tr -dc '0-9')
  [ -n "$exp" ] || { rm -f "$MAINT_FILE"; return 1; }
  now=$(date -u +%s)
  [ "$now" -lt "$exp" ] && return 0
  rm -f "$MAINT_FILE"
  post_discord "🟢 **MONITORING RESUMED - Dune servers**
The maintenance window ended. Automatic server-health alerts are active again."
  return 1
}
MAINT=0; maint_active && MAINT=1

pod_index() { sed -nE 's/.*-pod-([0-9]+)$/\1/p' <<< "$1"; }

# 0 if a ServerRestart CR created in the last 180s targets this pod index (= planned roll)
planned_roll_for_index() {
  local idx="$1" crs
  crs=$(kubectl get serverrestart -n "$NAMESPACE" -o json 2>/dev/null) || return 1
  jq -e --argjson idx "$idx" '
    [ .items[]
      | select(((.metadata.creationTimestamp | fromdateiso8601) > (now - 180)))
      | select(.spec.mode=="BattleGroup"
               or (.spec.mode=="Pods" and ((.spec.indexes // []) | index($idx)) != null)) ]
    | length > 0' >/dev/null 2>&1 <<< "$crs"
}

# best-effort: distinct player accounts seen active on a pod's dead container (blast-radius hint)
recent_players() {
  local pod="$1" n
  n=$(kubectl logs -n "$NAMESPACE" "$pod" --previous --tail=4000 2>/dev/null \
        | grep -oE 'AccountId: [1-9][0-9]*' | sort -u | wc -l | tr -d ' ')
  [[ "$n" =~ ^[0-9]+$ ]] && echo "$n" || echo "?"
}

prev_state=$(cat "$STATE_FILE")
new_state='{}'
alerts=()

for pod in "${ALWAYS_ON_PODS[@]}"; do
  label="${POD_LABEL[$pod]:-$pod}"
  idx=$(pod_index "$pod")
  pod_json=$(kubectl get pod -n "$NAMESPACE" "$pod" -o json 2>/dev/null) || pod_json=""

  # --- MISSING ---
  if [ -z "$pod_json" ]; then
    new_state=$(jq --arg p "$pod" '. + {($p): {status:"MISSING"}}' <<< "$new_state")
    prev_status=$(jq -r --arg p "$pod" '.[$p].status // "INITIAL"' <<< "$prev_state")
    if [ "$prev_status" != "MISSING" ] && [ "$prev_status" != "INITIAL" ]; then
      alerts+=("$(printf '🔴 **SERVER MISSING - %s**\nThis map'\''s server is not running in the cluster at all. Anyone who was on this map is disconnected and may have lost unsaved progress (levels/skill points since the last save). Please investigate now.\n• Map: %s\n• Pod: `%s`' "$label" "$label" "$pod")")
    fi
    continue
  fi

  start_time=$(jq -r '.status.startTime // empty' <<< "$pod_json")
  pod_uid=$(jq -r '.metadata.uid // empty' <<< "$pod_json")
  restart_count=$(jq -r '.status.containerStatuses[0].restartCount // 0' <<< "$pod_json")
  phase=$(jq -r '.status.phase // "Unknown"' <<< "$pod_json")
  term_reason=$(jq -r '.status.containerStatuses[0].lastState.terminated.reason // empty' <<< "$pod_json")
  term_exit=$(jq -r '.status.containerStatuses[0].lastState.terminated.exitCode // empty' <<< "$pod_json")
  term_at=$(jq -r '.status.containerStatuses[0].lastState.terminated.finishedAt // empty' <<< "$pod_json")

  new_state=$(jq --arg p "$pod" --arg t "$start_time" --arg uid "$pod_uid" --argjson seen "$(date -u +%s)" --argjson r "$restart_count" --arg ph "$phase" \
    '. + {($p): {startTime:$t, uid:$uid, observedAt:$seen, restartCount:$r, phase:$ph}}' <<< "$new_state")

  prev_start=$(jq -r --arg p "$pod" '.[$p].startTime // empty' <<< "$prev_state")
  prev_restarts=$(jq -r --arg p "$pod" '.[$p].restartCount // 0' <<< "$prev_state")
  prev_uid=$(jq -r --arg p "$pod" '.[$p].uid // empty' <<< "$prev_state")

  if { [ "$restart_count" -gt "$prev_restarts" ] || { [ -n "$prev_uid" ] && [ "$pod_uid" != "$prev_uid" ] && [ "$restart_count" -gt 0 ]; }; } 2>/dev/null; then
    # --- CONTAINER RESTART IN PLACE = a CRASH or OOM (not a planned roll) ---
    players=$(recent_players "$pod")
    if [ "$term_reason" = "OOMKilled" ]; then
      cause="ran OUT OF MEMORY (OOM) and was killed"
      tech="OOMKilled"
    elif [ "$term_exit" = "139" ]; then
      cause="CRASHED (segfault - a Funcom server-program bug)"
      tech="exit 139 / SIGSEGV"
    elif [ -n "$term_exit" ] && [ "$term_exit" != "0" ]; then
      cause="CRASHED unexpectedly"
      tech="exit ${term_exit}"
    else
      cause="restarted unexpectedly"
      tech="reason ${term_reason:-unknown}"
    fi
    alerts+=("$(printf '🔴 **SERVER CRASH - %s**\nThe game server for this map %s and auto-restarted. This was NOT planned. About %s player(s) were active on this map recently and may have lost unsaved progress (levels/skill points since the last save - the Funcom RAM-only bug). Worth checking in with them.\n• Map: %s\n• When: %s\n• Cause: %s (%s)\n• Pod: `%s`, crash #%s' \
      "$label" "$cause" "$players" "$label" "${term_at:-just now}" "$cause" "$tech" "$pod" "$restart_count")")

  elif [ -n "$prev_start" ] && [ "$prev_start" != "$start_time" ]; then
    # --- POD RECREATED (new instance) = planned roll OR unexpected replacement ---
    rolling=$(printf '%s\n%s\n' "$(jq --arg p "$pod" '.[$p]' <<< "$prev_state")" "$pod_json" \
      | jq -s '{previous:.[0],pod:.[1]}' \
      | /usr/bin/python3 -B "$SCRIPT_DIR/planned-rolling.py" 2>/dev/null) || rolling='{}'
    if [ "$(jq -r '.status // empty' <<< "$rolling")" = "planned_rolling_replacement" ]; then
      operation=$(jq -r '.operation_id' <<< "$rolling")
      alerts+=("$(printf '🟢 **PLANNED CHAT RENEWAL - %s**\nThis replacement matches the recorded rolling renewal for this specific server. The renewal controller separately verifies its new credential and restores admission before reporting completion.\n• Map: %s\n• New start: %s (previous %s)\n• Pod: `%s`\n• Operation: `%s`' \
        "$label" "$label" "$start_time" "$prev_start" "$pod" "$operation")")
    elif planned_roll_for_index "$idx"; then
      alerts+=("$(printf '🟢 **PLANNED RESTART - %s**\nThis replacement matches a recent scheduled or administrator-requested restart. Check current map health and login availability before treating recovery as complete.\n• Map: %s\n• Restarted at: %s (previous start %s)\n• Pod: `%s`, fresh instance' \
        "$label" "$label" "$start_time" "$prev_start" "$pod")")
    else
      players=$(recent_players "$pod")
      alerts+=("$(printf '🟠 **SERVER REPLACED - %s**\nThe watcher could not verify a matching planned restart for this replacement. This can be a crash that forced a reschedule, or the cluster moving the server. About %s player(s) were active here recently and may have lost unsaved progress. Please investigate.\n• Map: %s\n• New start: %s (previous %s)\n• Pod: `%s`' \
        "$label" "$players" "$label" "$start_time" "$prev_start" "$pod")")
    fi
  fi
done

echo "$new_state" > "$STATE_FILE"

# --- DeepDesert memory watch (DD pods bumped to 22Gi on 2026-07-14; thresholds updated) ---
MEM_STATE_FILE=$STATE_DIR/mem_alert_state.json
MEM_WATCH_PODS=(
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-deepdesert-1-pod-8
  ${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}-sg-deepdesert-1-pod-31
)
MEM_WARN_MI=20000   # ~89% of 22Gi cap
MEM_CLEAR_MI=19456  # hysteresis reset (~19Gi)
[ -f "$MEM_STATE_FILE" ] || echo '{}' > "$MEM_STATE_FILE"
mem_prev=$(cat "$MEM_STATE_FILE")
mem_new="$mem_prev"
# During a maintenance window, skip the memory watch entirely: do not post, and leave the
# alerted-flags untouched so a genuinely-high pod is re-evaluated fresh once the window ends.
if [ "$MAINT" != 1 ]; then
for pod in "${MEM_WATCH_PODS[@]}"; do
  mlabel="${POD_LABEL[$pod]:-$pod}"
  raw=$(kubectl top pod -n "$NAMESPACE" "$pod" --no-headers 2>/dev/null | awk '{print $NF}')
  [ -z "$raw" ] && continue
  case "$raw" in
    *Gi) mi=$(awk -v v="${raw%Gi}" 'BEGIN{printf "%d", v*1024}') ;;
    *Mi) mi=${raw%Mi} ;;
    *)   continue ;;
  esac
  [[ "$mi" =~ ^[0-9]+$ ]] || continue
  alerted=$(jq -r --arg p "$pod" '.[$p] // false' <<< "$mem_prev")
  if [ "$mi" -ge "$MEM_WARN_MI" ] && [ "$alerted" != "true" ]; then
    alerts_mem="$(printf '🟠 **MEMORY WARNING - %s**\nThis map'\''s server is using %sMi of its 22Gi limit (over ~89%%). If it hits the limit it will be killed and restarted, which crashes progress for players on the map. Plan a memory bump next maintenance window.\n• Pod: `%s`' "$mlabel" "$mi" "$pod")"
    post_discord "$alerts_mem"
    mem_new=$(jq --arg p "$pod" '. + {($p): true}' <<< "$mem_new")
  elif [ "$mi" -lt "$MEM_CLEAR_MI" ] && [ "$alerted" = "true" ]; then
    post_discord "$(printf '🟢 **MEMORY OK - %s**\nMemory back down to %sMi (below %sMi). The earlier memory warning is cleared.\n• Pod: `%s`' "$mlabel" "$mi" "$MEM_CLEAR_MI" "$pod")"
    mem_new=$(jq --arg p "$pod" '. + {($p): false}' <<< "$mem_new")
  fi
done
fi
echo "$mem_new" > "$MEM_STATE_FILE"

# Pod state ($STATE_FILE) was written above REGARDLESS of the maintenance window, so any
# planned boot is already absorbed; here we only gate the Discord posting.
if [ "$MAINT" = 1 ]; then
  [ "${#alerts[@]}" -gt 0 ] && logger -t lastsietch-pod-watcher "maintenance window active: suppressed ${#alerts[@]} pod alert(s)"
else
  for alert in "${alerts[@]}"; do
    post_discord "$alert"
  done
fi

exit 0
