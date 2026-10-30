#!/usr/bin/env bash
# Flip Deleted-character tombstones stuck at online_status='Online' back to
# Offline in dune.encrypted_player_state. Such rows have no live session (the
# logoff path that clears the flag never fires for characters deleted during
# creation) and pollute every consumer that reads eps presence raw: grew
# 6 -> 19 in the week before 2026-07-04. Deploys to <game-host>:/root/, driven
# by lastsietch-tombstone-sweep.timer (15 min).
#
# Settle guard: only rows whose reconnect grace window ended >10 minutes ago,
# so a character mid-deletion is never touched. Grace timestamps are stored
# as UTC in a timestamp-without-time-zone column, hence the AT TIME ZONE.
set -euo pipefail

ns=$(sudo kubectl get ns -o name 2>/dev/null \
       | sed 's#^namespace/##' \
       | grep -E '^funcom-seabass-' | head -n1 || true)
if [[ -z "$ns" ]]; then
  echo "tombstone-sweep: FATAL could not resolve the Dune namespace" >&2
  exit 3
fi
pod=$(sudo kubectl get pods -n "$ns" -o name 2>/dev/null \
        | sed 's#^pod/##' \
        | grep -E -- '-db-dbdepl-sts-0$' | head -n1 || true)
if [[ -z "$pod" ]]; then
  echo "tombstone-sweep: FATAL could not resolve the db pod in $ns" >&2
  exit 3
fi

sql="UPDATE dune.encrypted_player_state
     SET online_status='Offline'
     WHERE character_state='Deleted'
       AND online_status='Online'
       AND (reconnect_grace_period_end IS NULL
            OR reconnect_grace_period_end < (now() AT TIME ZONE 'utc') - interval '10 minutes');"

out=$(printf '%s\n' "$sql" | sudo kubectl exec -i -n "$ns" "$pod" -- \
        sh -c '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"' _ -h localhost -p 15432 -U postgres -d dune -tA)
echo "tombstone-sweep: $out"
