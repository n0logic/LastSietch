#!/usr/bin/env bash
# Boost cgroup cpu.weight for player-facing Dune pods (BUG-018 mitigation).
#
# Why: the Funcom operator sets only MEMORY requests, so k8s gives every game pod
# cpu.weight=1 (the cgroup v2 minimum; default is 100). Under contention a BOOTING
# instance pod (dungeon/ecolab recycle after every party) gets the same CPU share
# as a survival shard full of players; the shard's 30 Hz game thread stalls 10-33ms
# and players rubber-band. A/B measured 2026-08-04 (lastsietch-sched-sampler): one instance
# boot = base maps 2-6% stall-seconds, max 33ms at weight 1; with weights applied,
# worst stall 25ms (under the 33ms tick budget) and populated hubs 23ms -> 8ms.
#
# Weights: player-facing persistent maps 400, infra the game depends on 200,
# instance/story pods stay at 1 (they boot slower under load, protecting live maps).
#
# DURABILITY: weights live on the pod cgroup slice and RESET when a pod is
# recreated (restart, battlegroup update, Funcom build). Re-run this script after
# any of those. Safe to re-run any time (idempotent). Revert: WEIGHT_MAIN=1
# WEIGHT_INFRA=1 ./lastsietch-cpu-weight.sh
#
# Candidate for a systemd timer on the game host (lastsietch-bg-watchdog pattern) — not
# installed yet; run by hand or from the post-update checklist.
set -uo pipefail

NS=funcom-seabass-sh-<your-hostid>-<random>
WEIGHT_MAIN="${WEIGHT_MAIN:-400}"
WEIGHT_INFRA="${WEIGHT_INFRA:-200}"
MAIN_RE='sg-survival-1|sg-deepdesert-1|sg-sh-arrakeen|sg-sh-harkovillage'
INFRA_RE='db-dbdepl-sts-0|mq-game-sts-0|mq-admin-sts-0|bgd-deploy'

set_weight() { # <pod> <weight>
  local uid path
  uid=$(kubectl get pod -n "$NS" "$1" -o jsonpath='{.metadata.uid}' 2>/dev/null | tr - _)
  [ -n "$uid" ] || { echo "SKIP $1 (no uid)"; return; }
  path=$(ls -d /sys/fs/cgroup/kubepods.slice/kubepods-*.slice/kubepods-*-pod${uid}.slice 2>/dev/null | head -1)
  [ -n "$path" ] || { echo "SKIP $1 (no cgroup slice)"; return; }
  echo "$2" > "$path/cpu.weight" && echo "$1 -> cpu.weight=$(cat "$path/cpu.weight")"
}

kubectl get pods -n "$NS" -o name | sed 's|pod/||' | while read -r pod; do
  if [[ "$pod" =~ $MAIN_RE ]]; then set_weight "$pod" "$WEIGHT_MAIN"
  elif [[ "$pod" =~ $INFRA_RE ]]; then set_weight "$pod" "$WEIGHT_INFRA"
  fi
done
