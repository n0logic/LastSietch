#!/usr/bin/env bash
# Cross-check player presence across the BGD admin endpoint and the
# encrypted_player_state table. Exit 0 = no live players, safe to
# restart. Exit 1 = at least one player live or in grace.
#
# Run this before any operation that restarts game pods or the BGD.

set -euo pipefail

NS="${DUNE_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
POD=sh-<your-hostid>-<random>-db-dbdepl-sts-0

ssh "${GAME_HOST:-<game-host>}" "
  set -e
  echo '=== BGD endpoint ==='
  ONLINE=\$(curl -s --max-time 3 http://127.0.0.1:31282/v0/players/online)
  IN_TRANSIT=\$(curl -s --max-time 3 http://127.0.0.1:31282/v0/players/intransit)
  QUEUED=\$(curl -s --max-time 3 http://127.0.0.1:31282/v0/players/queued)
  echo \"online:     \$ONLINE\"
  echo \"intransit:  \$IN_TRANSIT\"
  echo \"queued:     \$QUEUED\"

  echo ''
  echo '=== DB encrypted_player_state (Online + grace-period stragglers) ==='
  PGPASS=\$(sudo kubectl exec -n $NS $POD -- printenv POSTGRES_PASSWORD)
  sudo kubectl exec -n $NS $POD -- env PGPASSWORD=\$PGPASS psql -h localhost -p 15432 -U postgres -d dune -c \"
    SELECT account_id, online_status, last_login_time, reconnect_grace_period_end
    FROM dune.encrypted_player_state
    WHERE (online_status = 'Online'
       OR (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW()))
      AND character_state IS DISTINCT FROM 'Deleted';\"

  # Exit code based on whether anything was returned
  if [ \"\$ONLINE\" != '[]' ] || [ \"\$IN_TRANSIT\" != '[]' ] || [ \"\$QUEUED\" != '[]' ]; then
    echo ''
    echo '!! players present per BGD. DO NOT restart pods or BGD. !!'
    exit 1
  fi

  echo ''
  echo 'safe: no live players per BGD.'
  exit 0
"
