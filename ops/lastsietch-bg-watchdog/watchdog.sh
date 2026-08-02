#!/usr/bin/env bash
# /opt/lastsietch-bg-watchdog/watchdog.sh   (repo mirror: ops/lastsietch-bg-watchdog/watchdog.sh)
#
# Detect + AUTO-RECOVER the known "BattleGroup comes back spec.stop=true after a power reboot"
# gotcha, which kills every game pod with NO self-recovery. We hit it twice after host
# power events; the unannounced one cost nearly two hours before anyone noticed.
# Fix is a single field: patch spec.stop=false.
#
# SAFETY (this ACTS on the live server unattended):
#   - Precise trigger: acts ONLY on status.phase == "Stopped". Any other phase, or an unreadable
#     API, is never treated as stopped (a failed/empty read does nothing).
#   - Confirm before acting: requires CONFIRM_TICKS consecutive Stopped reads, so a legitimate
#     transient (e.g. mid-update stop->start) is not fought.
#   - Own PAUSE flag (NOT the pod-watcher mute): pause this watchdog for INTENTIONAL stops
#     (our update windows). Deliberately independent of the pod-watcher maintenance mute, so
#     silencing crash-alert noise (e.g. a provider maintenance window) does NOT
#     also disable auto-recovery.
#   - Rate limited: at most MAX_ATTEMPTS auto-recoveries per ATTEMPT_WINDOW_SECS. Past that it
#     alerts for manual intervention and stops acting, so it can never flap or fight a human.
#   - The patch itself is idempotent and is the exact proven recovery command.
#
# Overridable for tests: BGW_STATE_DIR, BGW_KUBECTL, BGW_ENV_FILE, BGW_NOTIFY (cmd receiving msg
# on stdin), BGW_NOW (epoch override).
set -u

NS=${BGW_NS:-funcom-seabass-sh-<your-hostid>-<random>}
BG=${BGW_BG:-sh-<your-hostid>-<random>}
STATE_DIR=${BGW_STATE_DIR:-/var/lib/lastsietch-bg-watchdog}
STATE_FILE=$STATE_DIR/state.json
PAUSE_FILE=$STATE_DIR/pause
ENV_FILE=${BGW_ENV_FILE:-/opt/lastsietch-pod-watcher/.discord.env}
KUBECTL=${BGW_KUBECTL:-/usr/local/bin/kubectl}

CONFIRM_TICKS=${BGW_CONFIRM_TICKS:-2}          # consecutive Stopped reads before acting (~2 min @60s)
MAX_ATTEMPTS=${BGW_MAX_ATTEMPTS:-3}            # auto-recoveries allowed per window
ATTEMPT_WINDOW_SECS=${BGW_ATTEMPT_WINDOW:-21600}   # 6h

mkdir -p "$STATE_DIR"
[ -f "$STATE_FILE" ] || echo '{"consecutive":0,"acted":false,"attempts":[],"exhausted_alerted":false}' > "$STATE_FILE"

now() { echo "${BGW_NOW:-$(date -u +%s)}"; }
log() { echo "[bg-watchdog] $*"; }

notify() {
  local msg=$1
  if [ -n "${BGW_NOTIFY:-}" ]; then printf '%s' "$msg" | $BGW_NOTIFY; return 0; fi
  [ -f "$ENV_FILE" ] || { log "no env file, cannot notify"; return 0; }
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  curl -fsS -X POST \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg c "$msg" '{content:$c}')" \
    "https://discord.com/api/v10/channels/${DISCORD_CH_BOT_LOGS}/messages" >/dev/null 2>&1 \
    || log "discord post failed (non-fatal)"
}

# Pause = an INTENTIONAL stop is in progress (our update windows). Expiry epoch in the file.
# Self-heals: an expired/malformed marker is cleared so we can never stay paused forever.
paused() {
  [ -f "$PAUSE_FILE" ] || return 1
  local exp; exp=$(head -n1 "$PAUSE_FILE" 2>/dev/null | tr -dc '0-9')
  [ -n "$exp" ] || { rm -f "$PAUSE_FILE"; return 1; }
  [ "$(now)" -lt "$exp" ] && return 0
  rm -f "$PAUSE_FILE"
  return 1
}

state_get() { jq -r "$1" "$STATE_FILE" 2>/dev/null; }
state_set() { local tmp; tmp=$(mktemp); jq -c "$1" "$STATE_FILE" > "$tmp" 2>/dev/null && mv "$tmp" "$STATE_FILE" || rm -f "$tmp"; }

# --- read phase (a failed/empty read is NEVER treated as Stopped) ---
phase=$("$KUBECTL" get battlegroup -n "$NS" "$BG" -o jsonpath='{.status.phase}' 2>/dev/null)
rc=$?
if [ $rc -ne 0 ] || [ -z "$phase" ]; then
  log "phase unreadable (rc=$rc) - no action"
  exit 0
fi

consecutive=$(state_get '.consecutive // 0')
acted=$(state_get '.acted // false')

if [ "$phase" != "Stopped" ]; then
  # recovered?
  if [ "$acted" = "true" ]; then
    notify "$(printf '🟢 **SERVER RECOVERED - Dune (Last Sietch)**\nThe BattleGroup is back up (phase: %s) after the automatic recovery. Maps are coming online; players can log in again.\n• Auto-recovery worked, no action needed.' "$phase")"
    log "recovered (phase=$phase)"
  fi
  state_set '.consecutive = 0 | .acted = false | .exhausted_alerted = false'
  log "phase=$phase - healthy, nothing to do"
  exit 0
fi

# --- phase == Stopped ---
if paused; then
  log "phase=Stopped but watchdog PAUSED (intentional stop) - no action"
  state_set '.consecutive = 0'
  exit 0
fi

consecutive=$((consecutive + 1))
state_set ".consecutive = $consecutive"

if [ "$consecutive" -lt "$CONFIRM_TICKS" ]; then
  log "phase=Stopped ($consecutive/$CONFIRM_TICKS) - confirming before acting"
  exit 0
fi

# rate limit: prune old attempts, then check budget
N=$(now)
state_set ".attempts = [ .attempts[] | select(. > ($N - $ATTEMPT_WINDOW_SECS)) ]"
n_attempts=$(state_get '.attempts | length')
if [ "$n_attempts" -ge "$MAX_ATTEMPTS" ]; then
  if [ "$(state_get '.exhausted_alerted')" != "true" ]; then
    notify "$(printf '🔴 **SERVER DOWN - MANUAL ACTION NEEDED (Dune / Last Sietch)**\nThe BattleGroup is STOPPED and automatic recovery has already been tried %s times in the last %s hours, so it has STOPPED retrying to avoid fighting an intentional change.\nIf this stop was NOT intentional, run:\n`kubectl -n %s patch battlegroup %s --type=merge -p '"'"'{"spec":{"stop":false}}'"'"'`\nIf it WAS intentional, pause the watchdog: `/opt/lastsietch-bg-watchdog/pause.sh on <minutes>`' "$MAX_ATTEMPTS" "$((ATTEMPT_WINDOW_SECS/3600))" "$NS" "$BG")"
    state_set '.exhausted_alerted = true'
  fi
  log "phase=Stopped but attempt budget exhausted ($n_attempts/$MAX_ATTEMPTS) - alert only"
  exit 0
fi

# --- ACT: the documented, idempotent recovery ---
log "phase=Stopped confirmed - patching spec.stop=false (attempt $((n_attempts+1))/$MAX_ATTEMPTS)"
if "$KUBECTL" patch battlegroup -n "$NS" "$BG" --type=merge -p '{"spec":{"stop":false}}' >/dev/null 2>&1; then
  state_set ".attempts += [$N] | .acted = true"
  notify "$(printf '🛠️ **AUTO-RECOVERY - Dune server was STOPPED, restarting it**\nThe game servers were down: the BattleGroup came back `stop=true` (the known gotcha after a host power reboot - it kills every map with no self-recovery).\nI applied the documented fix automatically (`spec.stop=false`). Maps should come back within a few minutes; players can then log in.\n• Attempt %s of %s in the last %sh\n• Nothing was lost by this action; it is the same step from the maintenance runbook.' "$((n_attempts+1))" "$MAX_ATTEMPTS" "$((ATTEMPT_WINDOW_SECS/3600))")"
else
  notify "$(printf '🔴 **AUTO-RECOVERY FAILED - Dune server still STOPPED**\nThe BattleGroup is stopped and the automatic fix could NOT be applied (the patch command failed). Manual action needed:\n`kubectl -n %s patch battlegroup %s --type=merge -p '"'"'{"spec":{"stop":false}}'"'"'`' "$NS" "$BG")"
  log "patch FAILED"
fi
exit 0
