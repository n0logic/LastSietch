#!/usr/bin/env bash
# Supervises N parallel mq-admin capture subscribers, one per priority exchange.
# Each subscriber gets its own SimpleShaToken, exclusive temp queue, and output
# subtree. If a subscriber dies (token expiry, broker hiccup), the supervisor
# restarts it. Loop forever until systemd stops the service.
#
# Priority exchanges (per ITEM-B-BROKER-ARCHITECTURE-2026-05-26.md):
#   settingsUpdate     fanout  -- Funcom remote config push (CONFIRMED)
#   grant              direct  -- 25 partition-keyed bindings + bgdRpc
#   rpc                direct  -- 25 partition keys + serverGuid for BGD
#   response           direct  -- 25 partition keys
#   completions        direct  -- validation.* / completion.* / server_state.*
#   travelQueueStatus  fanout  -- per-partition travel queue depth (VC2 P1)
set -u
BRIDGE=/opt/lastsietch-rmq-bridge
WINDOW=${WINDOW:-1800}

# fanout: bind empty key. direct: auto-discover via rabbitmqctl.
declare -A MODE=(
  [settingsUpdate]=fanout
  [grant]=discover
  [rpc]=discover
  [response]=discover
  [completions]=discover
  [travelQueueStatus]=fanout
)

declare -A PIDS

launch_one() {
  local ex="$1" mode="${MODE[$1]}"
  local cmd
  if [[ "$mode" == "fanout" ]]; then
    cmd=(/opt/lastsietch-rmq-bridge/capture-admin.py --exchange "$ex" --routing-keys "" --duration-seconds "$WINDOW")
  else
    cmd=(/opt/lastsietch-rmq-bridge/capture-admin.py --exchange "$ex" --auto-discover --duration-seconds "$WINDOW")
  fi
  ( "${cmd[@]}" 2>&1 | sed -u "s/^/[$ex] /" ) &
  PIDS[$ex]=$!
  echo "[supervisor] launched $ex pid=${PIDS[$ex]} mode=$mode window=${WINDOW}s"
}

for ex in "${!MODE[@]}"; do launch_one "$ex"; done

trap 'echo "[supervisor] SIGTERM, killing children"; for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; exit 0' SIGTERM SIGINT

while true; do
  for ex in "${!PIDS[@]}"; do
    p="${PIDS[$ex]}"
    if ! kill -0 "$p" 2>/dev/null; then
      echo "[supervisor] $ex (pid $p) exited; restarting in 3s"
      sleep 3
      launch_one "$ex"
    fi
  done
  sleep 5
done
