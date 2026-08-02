#!/usr/bin/env bash
# Passive, supervised capture of the mq-game `notifications` exchange, armed to
# catch the Dune MTX live-ops event traffic (XPBonus / SpecializationXPBonus
# deactivation at 2026-06-01, and any re-activation).
#
# SAFE BY CONSTRUCTION:
#  - reuses the proven /opt/lastsietch-rmq-bridge/capture.py (exclusive, auto_delete
#    temp queue on a TOPIC exchange => copy delivery, never competes with the
#    game pod's own consumer; queue vanishes the moment each window ends).
#  - does NOT publish, does NOT touch game pods / BGD / mq-admin capture.
#  - disk-bounded: keeps ALL messages (so we cannot miss the event by a bad
#    filter), but prunes oldest run dirs if the capture tree exceeds CAP_MB.
#
# This is separate from lastsietch-rmq-capture-admin.service (mq-admin). Do not confuse.
set -u

BRIDGE=/opt/lastsietch-rmq-bridge
OUT=/var/lib/lastsietch-rmq-bridge/captures
MQGIP=${MQGIP:-10.43.248.252}     # mq-game svc ClusterIP
RK=${RK:-#}
WINDOW=${WINDOW:-1800}
CAP_MB=${CAP_MB:-500}             # hard ceiling for the mq-game captures tree
EVENT_RE='XPBonus|SpecializationXP|MTXEvent|ScaleXP|XPMod|Bonus|[Ee]vent|[Ee]xpire|[Mm]odifier'

prune_to_cap() {
  # prune oldest run dirs while the tree is over CAP_MB
  while [ "$(du -sm "$OUT" 2>/dev/null | awk '{print $1}')" -gt "$CAP_MB" ]; do
    oldest=$(ls -1dt "$OUT"/*/ 2>/dev/null | tail -1)
    [ -n "$oldest" ] || break
    echo "[events-loop] CAP_MB exceeded, pruning oldest: $oldest"
    rm -rf -- "$oldest"
  done
}

echo "[events-loop] start $(date -u +%FT%TZ); mq-game=$MQGIP rk=$RK window=${WINDOW}s cap=${CAP_MB}MB"
while true; do
  python3 "$BRIDGE/capture.py" --host "$MQGIP" --routing-key "$RK" \
      --duration-seconds "$WINDOW" 2>&1
  run=$(ls -1dt "$OUT"/*/ 2>/dev/null | head -1)
  if [ -n "$run" ]; then
    n=$(ls -1 "$run"*.json 2>/dev/null | wc -l | tr -d ' ')
    if [ "${n:-0}" -gt 0 ] && grep -rqlE "$EVENT_RE" "$run" 2>/dev/null; then
      echo "[events-loop] *** EVENT-RELEVANT messages captured in $run (n=$n) ***"
    elif [ "${n:-0}" -eq 0 ]; then
      rm -rf -- "$run"   # discard empty windows (no players / no traffic)
    fi
  fi
  prune_to_cap
  echo "[events-loop] window ended $(date -u +%FT%TZ), restart in 3s"
  sleep 3
done
