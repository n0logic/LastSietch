#!/usr/bin/env bash
# Watch for new Dune players and auto-grant the Sietch Welcome Package.
#
# Detects fresh accounts by SELECTing dune.encrypted_player_state rows
# whose account_id is NOT in dune.ls_welcome_pack_grants. For each match,
# invokes grant-welcome-pack.sh.
#
# Idempotency: dune.ls_welcome_pack_grants is the source of truth. Any
# account_id present in that table (even with granted_items=0 for
# operator-skip entries) will NEVER receive a pack from this watcher.
#
# Usage:
#   ./welcome-pack-watcher.sh once       # single sweep, exit
#   ./welcome-pack-watcher.sh loop 60    # poll every 60s forever (for systemd or tmux)
#
# Deployment options:
#   - systemd timer firing every 60s (cleanest)
#   - long-running `loop` invocation under systemd unit
#   - cron */1 * * * * (works, slight overhead per invocation)

set -euo pipefail

MODE="${1:-once}"
INTERVAL="${2:-60}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRANT_SCRIPT="$SCRIPT_DIR/grant-welcome-pack.sh"
NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
POD="${POD:-sh-<your-hostid>-<random>-db-dbdepl-sts-0}"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [welcome-watcher] $*"
}

run_psql() {
  ssh lastsietch-dune "
    PGPASS=\$(sudo kubectl exec -n $NS $POD -- printenv POSTGRES_PASSWORD)
    sudo kubectl exec -i -n $NS $POD -- env PGPASSWORD=\$PGPASS psql -h localhost -p 15432 -U postgres -d dune -t -A -F'|' -v ON_ERROR_STOP=1
  "
}

sweep() {
  # Find accounts with a playable character that haven't been granted (or pre-marked).
  # Conditions:
  #   - encrypted_player_state row exists  (an active character on the server)
  #   - last_login_time is not NULL        (they've actually played at least once)
  #   - account_id is not in sentinel       (never granted, never operator-skipped)
  local sql="
    SELECT eps.account_id, eps.player_pawn_id, eps.last_login_time
      FROM dune.encrypted_player_state eps
     WHERE eps.last_login_time IS NOT NULL
       AND eps.account_id NOT IN (SELECT account_id FROM dune.ls_welcome_pack_grants);
  "
  local rows
  rows=$(echo "$sql" | run_psql || true)

  if [[ -z "$rows" ]]; then
    return 0
  fi

  while IFS='|' read -r account_id pawn_id last_login; do
    [[ -z "$account_id" ]] && continue
    log "new account detected: account_id=$account_id pawn=$pawn_id last_login=$last_login -> granting v1 pack"
    if "$GRANT_SCRIPT" "$account_id" > /tmp/welcome-pack-grant-${account_id}.log 2>&1; then
      log "  granted ok (full log: /tmp/welcome-pack-grant-${account_id}.log)"
    else
      log "  GRANT FAILED for account_id=$account_id, see /tmp/welcome-pack-grant-${account_id}.log"
    fi
  done <<< "$rows"
}

case "$MODE" in
  once)
    sweep
    ;;
  loop)
    log "starting watcher loop (interval ${INTERVAL}s)"
    while true; do
      sweep || log "sweep error (continuing)"
      sleep "$INTERVAL"
    done
    ;;
  *)
    echo "usage: $0 {once|loop [interval-seconds]}"
    exit 1
    ;;
esac
