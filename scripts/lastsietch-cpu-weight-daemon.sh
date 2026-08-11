#!/usr/bin/env bash
# Boot-phase-aware cgroup cpu.weight manager for the Dune game fleet (BUG-018).
#
# Supersedes the static lastsietch-cpu-weight.sh. Runs on a 30s systemd timer so it is
# both DURABLE (re-applies after any pod recreation / battlegroup update / Funcom
# build with no human step) and BOOT-AWARE (the piece the static script could not
# do): it protects players who are INSIDE an instance, not just on the persistent
# maps.
#
# The mechanism, and why each weight:
#   Every game pod is cpu.weight=1 by default (the operator sets only memory
#   requests, so k8s derives the cgroup-v2 minimum). A ~2-minute UE server BOOT
#   pegs cores; at weight 1 vs 1 a booting instance steals CPU from a populated
#   map and its 30 Hz game thread misses ticks (rubber-banding). A/B proven
#   2026-08-04: one boot = 2-6% stall-seconds / 33ms max at weight 1; with the
#   static weights, 25ms max. This daemon adds the missing case.
#
#   RUNNING persistent maps (survival/DD/hubs) : WEIGHT_MAIN  (400)
#   RUNNING instances (cb/story/dlc/overmap)   : WEIGHT_INST  (300) <- the fix:
#       an OCCUPIED dungeon/testing-station now vastly outweighs a booting sibling
#       (300 vs 50) instead of tying it (1 vs 1). Slightly below the persistent
#       maps so a full survival shard still edges a full instance under scarcity.
#   ANY game pod whose container started < BOOT_GRACE_S ago : WEIGHT_BOOT (50)
#       its own boot storm yields to everything already serving players. A pod
#       has no players during its own boot, so demoting it costs nothing and is
#       exactly what stops the stall. Restored on the first tick after the grace.
#   Infra the game depends on (db/mq-game/mq-admin/bgd) : WEIGHT_INFRA (200),
#       never demoted (a "booting" DB must stay responsive).
#
# Read-only w.r.t. the game: writes ONLY cpu.weight on cgroup slices, one
# `kubectl get pods` per tick. NEVER restarts pods / BGD / k3s. Idempotent.
#
# Usage:
#   lastsietch-cpu-weight-daemon.sh            # one tick (what the timer runs)
#   lastsietch-cpu-weight-daemon.sh --verbose  # one tick, print every change
#   lastsietch-cpu-weight-daemon.sh --revert   # set every managed pod back to weight 1
#   lastsietch-cpu-weight-daemon.sh --install  # install+enable the systemd timer (root)
set -uo pipefail

NS="${LASTSIETCH_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
WEIGHT_MAIN="${WEIGHT_MAIN:-400}"
WEIGHT_INST="${WEIGHT_INST:-300}"
WEIGHT_BOOT="${WEIGHT_BOOT:-50}"
WEIGHT_INFRA="${WEIGHT_INFRA:-200}"
BOOT_GRACE_S="${BOOT_GRACE_S:-180}"
MAIN_RE='sg-survival-1|sg-deepdesert-1|sg-sh-arrakeen|sg-sh-harkovillage'
INFRA_RE='db-dbdepl-sts-0|mq-game-sts-0|mq-admin-sts-0|bgd-deploy'

KUBECTL="kubectl"
command -v kubectl >/dev/null 2>&1 || KUBECTL="k3s kubectl"

VERBOSE=0; MODE=tick
case "${1:-}" in
  --verbose) VERBOSE=1 ;;
  --revert)  MODE=revert ;;
  --install) MODE=install ;;
  "" ) ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

log() { [ "$VERBOSE" = 1 ] && echo "$*"; return 0; }

slice_for() { # <pod-uid-underscored> -> prints cgroup slice path or nothing
  ls -d /sys/fs/cgroup/kubepods.slice/kubepods-*.slice/kubepods-*-pod"$1".slice 2>/dev/null | head -1
}

write_weight() { # <slice> <weight> <podname>
  local cur; cur=$(cat "$1/cpu.weight" 2>/dev/null || echo "?")
  [ "$cur" = "$2" ] && { log "  = $3 already $2"; return; }
  if echo "$2" > "$1/cpu.weight" 2>/dev/null; then
    log "  + $3 $cur -> $2"
  else
    echo "WARN: failed to set $3 -> $2" >&2
  fi
}

install_timer() {
  [ "$(id -u)" -eq 0 ] || { echo "--install must run as root" >&2; exit 1; }
  install -m 0755 "$0" /usr/local/bin/lastsietch-cpu-weight-daemon.sh
  cat > /etc/systemd/system/lastsietch-cpu-weight.service <<'UNIT'
[Unit]
Description=Boot-phase-aware cpu.weight manager for the Dune game fleet (BUG-018)
After=k3s.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/lastsietch-cpu-weight-daemon.sh
UNIT
  cat > /etc/systemd/system/lastsietch-cpu-weight.timer <<'UNIT'
[Unit]
Description=Run the Dune cpu.weight manager every 30s (durability + boot-demotion)
[Timer]
OnBootSec=45
OnUnitActiveSec=30
AccuracySec=5
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now lastsietch-cpu-weight.timer
  echo "installed + enabled lastsietch-cpu-weight.timer (every 30s)"
  exit 0
}
[ "$MODE" = install ] && install_timer

# One JSON pull: name, uid, running-since. jq epoch handles the RFC3339 stamp so
# the shell never parses dates (portable + no locale surprises).
NOW=$(date +%s)
rows=$($KUBECTL get pods -n "$NS" -o json 2>/dev/null | jq -r --argjson now "$NOW" '
  .items[]
  | select(.status.containerStatuses != null)
  | { name: .metadata.name,
      uid: (.metadata.uid | gsub("-";"_")),
      role: (.metadata.labels.role // ""),
      started: (.status.containerStatuses[0].state.running.startedAt // "") }
  | .age = (if .started == "" then -1
            else ($now - (.started | fromdateiso8601)) end)
  | "\(.name)\t\(.uid)\t\(.role)\t\(.age)"')

[ -n "$rows" ] || { echo "WARN: no pods from kubectl (API down?)" >&2; exit 1; }

changed=0
while IFS=$'\t' read -r name uid role age; do
  [ -n "$uid" ] || continue

  if [ "$MODE" = revert ]; then
    if [ "$role" = "igw-server" ] || [[ "$name" =~ $INFRA_RE ]]; then
      s=$(slice_for "$uid"); [ -n "$s" ] && write_weight "$s" 1 "$name"
    fi
    continue
  fi

  # Classify -> target weight
  target=""
  if [ "$role" = "igw-server" ]; then
    if [[ "$name" =~ $MAIN_RE ]]; then target=$WEIGHT_MAIN; else target=$WEIGHT_INST; fi
    # Boot demotion: a freshly (re)started container yields while it boots.
    if [ "$age" -ge 0 ] && [ "$age" -lt "$BOOT_GRACE_S" ]; then target=$WEIGHT_BOOT; fi
  elif [[ "$name" =~ $INFRA_RE ]]; then
    target=$WEIGHT_INFRA
  else
    continue
  fi

  s=$(slice_for "$uid") || true
  [ -n "$s" ] || { log "  ? $name no slice yet"; continue; }
  before=$(cat "$s/cpu.weight" 2>/dev/null || echo "?")
  write_weight "$s" "$target" "$name"
  [ "$before" != "$target" ] && changed=$((changed+1))
done <<< "$rows"

[ "$MODE" = revert ] && { echo "reverted managed pods to cpu.weight=1"; exit 0; }
log "tick complete: $changed change(s)"
exit 0
