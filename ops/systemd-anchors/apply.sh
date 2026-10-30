#!/usr/bin/env bash
# Install the OnActiveSec anchor drop-in on every timer listed in timers.txt (<game-host>).
# Idempotent. Run as root on the game host after any maintenance hold that stops and
# restarts timers without a reboot. Background: AGENTS.md section 1, "The status field that lies".
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
while read -r t; do
  [ -z "$t" ] && continue
  install -D -m 0644 "$here/50-onactivesec-anchor.conf" "/etc/systemd/system/$t.timer.d/50-onactivesec-anchor.conf"
done < "$here/timers.txt"
systemctl daemon-reload
while read -r t; do [ -n "$t" ] && systemctl restart "$t.timer"; done < "$here/timers.txt"
systemctl list-timers --all | grep -E "$(paste -sd'|' "$here/timers.txt")" | awk '{print $NF, $1, $2, $3}' | sort
