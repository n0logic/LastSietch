#!/usr/bin/env bash
# Deep Desert unpark window lane (VAULT-04 mode A). Runs from the operator box.
#
# Input: a plan made on <web-host> by `unpark.py plan` (root, from two verified vault
# archives): /root/base-vault-staging/unpark-<backup>.{json,sql,dryrun.sql}.
#
#   --dry-run   pod UP. Runs the whole transaction and ROLLBACK: proves every guard and
#               the read-back pass against the live DB and changes nothing.
#   --apply --i-have-owner-approval
#               ANNOUNCED WINDOW ONLY (a DD pod bounce disconnects that partition).
#               Deletes the target DD pod with --wait=true (shutdown-save completes),
#               commits the transaction while the pod is down, waits for Ready, then proves
#               ordering from the log: the "Initial actor load: Gathered N actors" line must
#               be stamped AFTER the commit, or the write is inert for this load and the lane
#               prints the re-bounce command (it never bounces twice on its own).
#
# The transaction itself refuses on any drift (header, links, packed state, totem
# permissions, target partition identity, occupancy) and reads back before COMMIT.
#
# Usage: ./ops/base-vault/unpark-window.sh --backup 112 --dry-run
#        ./ops/base-vault/unpark-window.sh --backup 112 --apply --i-have-owner-approval
set -euo pipefail
BACKUP=""; MODE=""; APPROVED=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP="$2"; shift 2;;
    --dry-run) MODE=dry; shift;;
    --apply) MODE=apply; shift;;
    --i-have-owner-approval) APPROVED=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
[[ "$BACKUP" =~ ^[1-9][0-9]{0,18}$ && -n "$MODE" ]] || { echo "usage: $0 --backup N --dry-run | --apply --i-have-owner-approval" >&2; exit 2; }
[[ "$MODE" != apply || -n "$APPROVED" ]] || { echo "REFUSE: --apply needs --i-have-owner-approval" >&2; exit 2; }

STAGE=/root/base-vault-staging/unpark-$BACKUP
REMOTE_DIR=/root/base-vault-unpark
SQL=$STAGE$([[ $MODE == dry ]] && echo .dryrun.sql || echo .sql)
step() { echo; echo "-- $* --"; }
# Each argument shell-quoted: ssh joins argv into one string.
dq() { ssh <game-host> "bash -s -- $(printf '%q ' "$@")"; }

step "Plan ($SQL on <web-host>)"
PLAN=$(ssh <web-host> "cat $STAGE.json")
PARTITION=$(python3 -c 'import json,sys; p=json.loads(sys.argv[1]); print(p["target"]["partition_id"])' "$PLAN")
python3 -c 'import json,sys; p=json.loads(sys.argv[1]); t=p["target"]; print("  backup %s totem %s -> %s partition %s dim %s sector %s; %d actors, %d ranks" % (p["backup_id"], p["totem_id"], t["map"], t["partition_id"], t["dimension_index"], t["sector"], len(p["linked_actor_ids"]), len(p["ranks"])))' "$PLAN"
SUM=$(ssh <web-host> "sha256sum $SQL" | cut -c1-64)
ssh <web-host> "cat $SQL" | ssh <game-host> "set -e; umask 077; install -d -m 700 $REMOTE_DIR; cat > $REMOTE_DIR/$(basename $SQL)"
[[ "$(ssh <game-host> "sha256sum $REMOTE_DIR/$(basename $SQL)" | cut -c1-64)" == "$SUM" ]] || { echo "REFUSE: SQL hash changed in transit" >&2; exit 3; }
echo "  sql sha256 ${SUM:0:16} staged on <game-host>"

REMOTE_LIB='ns=$(sudo kubectl get ns -o name | sed "s#^namespace/##" | grep -E "^funcom-seabass-" | head -n1)
db=$(sudo kubectl get pods -n "$ns" -o name | sed "s#^pod/##" | grep -E -- "-db-dbdepl-sts-0$" | head -n1)
psql_file() { sudo kubectl exec -i -n "$ns" "$db" -- sh -c "PGPASSWORD=\$POSTGRES_PASSWORD psql -h localhost -p 15432 -U postgres -d dune -X -q -t -A -v ON_ERROR_STOP=1" < "$1"; }
psql_q() { sudo kubectl exec -n "$ns" "$db" -- sh -c "PGPASSWORD=\$POSTGRES_PASSWORD psql -h localhost -p 15432 -U postgres -d dune -X -q -t -A -c \"\$1\"" _ "$1"; }
game_pod() { sudo kubectl get pods -n "$ns" -o name | sed "s#^pod/##" | grep -E -- "-sg-deepdesert-1-pod-$1\$" | head -n1; }'

step "Target pod and occupancy"
dq "$PARTITION" <<EOF
set -euo pipefail
$REMOTE_LIB
pod=\$(game_pod "\$1"); [[ -n "\$pod" ]] || { echo "REFUSE: no deepdesert pod for partition \$1" >&2; exit 4; }
echo "  pod \$pod: \$(sudo kubectl get pod -n "\$ns" "\$pod" -o jsonpath='{.status.phase} ready={.status.conditions[?(@.type=="Ready")].status}')"
players=\$(psql_q "SELECT coalesce(sum(f.connected_players),0) FROM dune.world_partition w JOIN dune.farm_state f ON f.server_id=w.server_id WHERE w.partition_id=\$1 AND f.ready AND f.alive;")
echo "  connected players on partition \$1: \$players"
[[ "\$players" == 0 ]] || { echo "REFUSE: partition occupied" >&2; exit 5; }
EOF

if [[ $MODE == dry ]]; then
  step "Dry run (pod up; the transaction ends in ROLLBACK)"
  dq "$REMOTE_DIR/$(basename $SQL)" <<EOF
set -euo pipefail
$REMOTE_LIB
psql_file "\$1"
EOF
  echo; echo "dry run complete: nothing was changed."
  exit 0
fi

step "WINDOW: bounce, commit while down, prove ordering"
dq "$PARTITION" "$REMOTE_DIR/$(basename $SQL)" <<EOF
set -euo pipefail
$REMOTE_LIB
pod=\$(game_pod "\$1")
echo "  deleting \$pod (--wait=true: shutdown-save completes first)"
sudo kubectl delete pod -n "\$ns" "\$pod" --wait=true >/dev/null
out=\$(psql_file "\$2")
committed=\$(date -u +%s)
echo "  \$out"
echo "  committed at \$(date -u -d @\$committed +%H:%M:%SZ)"
for i in \$(seq 1 120); do
  new=\$(game_pod "\$1")
  ready=\$(sudo kubectl get pod -n "\$ns" "\$new" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  [[ "\$ready" == True ]] && break; sleep 5
done
at=\$(sudo kubectl get pod -n "\$ns" "\$new" -o jsonpath='{.status.conditions[?(@.type=="Ready")].lastTransitionTime}')
started=\$(sudo kubectl get pod -n "\$ns" "\$new" -o jsonpath='{.status.startTime}')
echo "  new pod \$new started \$started, ready \$at"
# The proof is the engine's own load line: its DB read must come after the commit.
# startTime alone is not enough (it can share the commit second). Only these fixed
# lines are printed: the pod log's command line carries credentials.
load=\$(sudo kubectl logs -n "\$ns" "\$new" --since=15m 2>/dev/null | grep -m1 -o -E "^\[[0-9.:-]+\].*Initial actor load: Gathered [0-9]+ actors" || true)
if [[ -z "\$load" ]]; then
  echo "  (no load line yet; recheck: kubectl logs | grep 'Initial actor load: Gathered')"
else
  echo "  \$load"
  stamp=\$(sed -E 's/^\[([0-9]{4})\.([0-9]{2})\.([0-9]{2})-([0-9]{2})\.([0-9]{2})\.([0-9]{2}).*/\1-\2-\3 \4:\5:\6/' <<< "\$load")
  if [[ \$(date -u -d "\$stamp" +%s) -le \$committed ]]; then
    echo "WARNING: the actor load read the DB before the commit; this load does not include the base."
    echo "If missing in game: sudo kubectl delete pod -n \$ns \$new --wait=true"
  else
    echo "  ordering proven: actor load at \$stamp UTC, after the commit"
  fi
fi
sudo kubectl logs -n "\$ns" "\$new" --since=15m 2>/dev/null | grep -c -E "SIGSEGV|Fatal error" | sed "s/^/  crash lines: /" || true
EOF
echo; echo "unpark applied. Owner and Medic: check the base in game at the planned sector."
