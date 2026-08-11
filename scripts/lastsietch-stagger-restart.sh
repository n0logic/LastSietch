#!/usr/bin/env bash
# Staggered rolling restart of EMPTY game pods to desynchronize fleet-wide timer bursts.
#
# Why (BUG-018): the 2026-08-04 host reboot started all 30 game pods within ~2 min,
# aligning their internal periodic work. The fleet now bursts in lockstep every ~3-12s
# (ctxt 850k-1.5M/s vs a smooth ~575k before), and every map hitches at the same
# moment = the "worse rubber-banding" reports. Pre-reboot start times were spread by
# July's ROLLING battlegroup update; this restores that spread the same way.
#
# Safety:
#   - NEVER touches populated pods: skips survival + social hubs entirely (checked
#     against the live pod list by prefix), and they can be done later if empty.
#   - One graceful delete (120s grace = engine PreShutdown flush) every SPACING_S.
#   - Pauses if more than MAX_BOOTING pods are not ready (no self-made boot storm).
#   - Logs every action; idempotent to re-run (already-restarted pods just restart
#     again, spreading further).
set -uo pipefail

NS=funcom-seabass-sh-<your-hostid>-<random>
SPACING_S=45
MAX_BOOTING=4
SKIP_PREFIX='sg-survival-1|sg-sh-arrakeen|sg-sh-harkovillage'
LOG=/root/lastsietch-stagger-restart.log

log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$LOG"; }

mapfile -t PODS < <(kubectl get pods -n "$NS" -l role=igw-server -o name \
  | sed 's|pod/||' | grep -Ev "$SKIP_PREFIX" | sort)

log "stagger start: ${#PODS[@]} pods, spacing ${SPACING_S}s, skipping populated prefixes"

for pod in "${PODS[@]}"; do
  # backpressure: wait until fewer than MAX_BOOTING pods are not-ready
  for i in $(seq 1 40); do
    booting=$(kubectl get pods -n "$NS" -l role=igw-server --no-headers 2>/dev/null \
      | awk '$2 != "1/1" || $3 != "Running"' | wc -l)
    [ "$booting" -lt "$MAX_BOOTING" ] && break
    log "backpressure: $booting pods not ready, waiting"
    sleep 30
  done
  log "deleting $pod (grace 120)"
  kubectl delete pod -n "$NS" "$pod" --grace-period=120 --wait=false >>"$LOG" 2>&1
  sleep "$SPACING_S"
done

log "stagger complete: all deletes issued"
