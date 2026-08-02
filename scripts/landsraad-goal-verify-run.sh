#!/usr/bin/env bash
# One-shot wrapper for the post-rollover Landsraad-goal verification.
# Runs the read-only check, records the verdict to a result file + log, and
# (best-effort) posts it to Discord if a notify hook is present. Fired by
# lastsietch-landsraad-goal-verify.timer ~after the 2026-06-09 04:55 UTC term rollover.
set -uo pipefail

RESULT=/root/jun3/landsraad-goal-verify-result.txt
LOG=/var/log/lastsietch-landsraad-goal-verify.log
NOTIFY=/usr/local/bin/lastsietch-landsraad-verify-notify.sh   # optional Discord hook

mkdir -p /root/jun3
TS=$(date -u +%FT%TZ)

OUT=$(/root/dune-verify-landsraad-goal.py 2>&1)
RC=$?
VERDICT_LINE=$(printf '%s\n' "$OUT" | head -1)

{
  echo "=== $TS (exit $RC) ==="
  printf '%s\n' "$OUT"
} | tee "$RESULT" >> "$LOG"

# Best-effort notify (only if the operator dropped a notify hook in place).
if [ -x "$NOTIFY" ]; then
  "$NOTIFY" "$RC" "$VERDICT_LINE" >> "$LOG" 2>&1 || true
fi

exit "$RC"
