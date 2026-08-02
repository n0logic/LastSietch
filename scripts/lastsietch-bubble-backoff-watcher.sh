#!/usr/bin/env bash
# /opt/lastsietch-bubble-backoff-watcher/watcher.sh
# Bubble-map (journey/story) backoff reaper.
#
# sg-cb-* bubble servers exit BY DESIGN when their last player leaves
# ("Shutting down server for journey story map"). Under Landsraad-contract
# traffic the rapid clean-exit cycles trip k8s CrashLoopBackOff (10s -> ... ->
# 300s; the backoff only resets after ~10 min of uptime). While a pod sits in
# BackOff, players hitting that map are stuck on multi-minute load screens.
#
# Fix: any sg-cb-* pod in CrashLoopBackOff/BackOff => delete it. The operator
# recreates it within seconds with FRESH backoff state. The container is
# already down when this triggers, so deletion has zero player impact.
#
# Namespace is auto-detected (matches sg-cb pods across all namespaces) so this
# survives BattleGroup rolls that rotate the namespace suffix.

set -u

STATE_DIR=/var/lib/lastsietch-bubble-backoff-watcher
LOG_FILE=$STATE_DIR/reaped.log
ENV_FILE=/opt/lastsietch-bubble-backoff-watcher/.discord.env

mkdir -p "$STATE_DIR"

log() {
  # timestamped line to the reap log (kept small; this only fires on a reap)
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_FILE"
}

post_discord() {
  # best-effort alert to #bot-logs; silently skip if no creds present
  [ -f "$ENV_FILE" ] || return 0
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  [ -n "${DISCORD_BOT_TOKEN:-}" ] && [ -n "${DISCORD_CH_BOT_LOGS:-}" ] || return 0
  curl -fsS -X POST \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg c "$1" '{content:$c}')" \
    "https://discord.com/api/v10/channels/${DISCORD_CH_BOT_LOGS}/messages" >/dev/null 2>&1 || true
}

# All pods, every namespace. Select bubble-server (sg-cb-*) pods whose container
# is waiting in a backoff state. Emit "<namespace> <pod> <reason>" per line.
stuck=$(kubectl get pods -A -o json 2>/dev/null | jq -r '
  .items[]
  | select(.metadata.name | test("-sg-cb-"))
  | . as $p
  | ($p.status.containerStatuses // [])[]
  | select(.state.waiting.reason // "" | test("BackOff"))
  | "\($p.metadata.namespace) \($p.metadata.name) \(.state.waiting.reason)"
' 2>/dev/null | sort -u)

[ -n "$stuck" ] || exit 0

while read -r ns pod reason; do
  [ -n "$pod" ] || continue
  if kubectl delete pod -n "$ns" "$pod" --wait=false >/dev/null 2>&1; then
    log "reaped $pod ($reason) in $ns"
    post_discord "[bubble-backoff] reaped \`${pod}\` (${reason}) — operator will recreate with fresh backoff. Container was already down; zero player impact."
  else
    log "FAILED to delete $pod ($reason) in $ns"
  fi
done <<< "$stuck"

exit 0
