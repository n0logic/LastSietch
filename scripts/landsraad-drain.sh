#!/usr/bin/env bash
# Last Sietch Landsraad WAL drain stopgap.
# The in-game LISTEN-driven drainer (landsraad_process_task_progress, fired via
# landsraad_notify_channel) stalled after the 2026-05-28 18:53 UTC battlegroup
# restart, freezing per-house contribution at the sum-through-cursor value while
# the WAL kept growing. See docs/dune-research/LANDSRAAD-CONTRIBUTION-DIAGNOSIS-2026-05-29.md.
#
# This calls Funcom's OWN drainer proc on a timer so contributions reach the
# ledger regardless of LISTEN health. Idempotent: the proc processes only WAL
# rows past the cursor, so a run with nothing to do is a no-op. Remove this timer
# once the native drainer is confirmed healthy.
#
# Runs as root (systemd). dq.sh uses sudo kubectl; the proc body uses unqualified
# table names so the SET LOCAL search_path is required.
set -uo pipefail

LOG=/var/log/lastsietch-landsraad-drain.log
STATE_DIR=/var/lib/lastsietch-landsraad-drain
STATE="$STATE_DIR/last_cursor"
mkdir -p "$STATE_DIR"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
before=$(cat "$STATE" 2>/dev/null || echo "")

out=$(/root/dq.sh -v ON_ERROR_STOP=1 -c \
  "BEGIN; SET LOCAL search_path TO dune, public; SELECT dune.landsraad_process_task_progress(200); COMMIT;" 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
  echo "$ts ERROR rc=$rc: $(printf '%s' "$out" | tr '\n' ' ')" >>"$LOG"
  exit $rc
fi

after=$(/root/dq.sh -tA -c \
  "SELECT last_processed_id FROM dune.landsraad_task_progress_processed;" 2>/dev/null | tr -d '[:space:]')
if [ -n "$after" ] && [ "$after" != "$before" ]; then
  echo "$ts drained cursor ${before:-?}->${after}" >>"$LOG"
  printf '%s' "$after" >"$STATE"
fi
