#!/usr/bin/env bash
# the web host-side notifier: after the 2026-06-09 Landsraad term rollover, run the
# read-only goal verification on lastsietch-dune and post the verdict to the private
# Cielago audit channel (no public noise). Fired once by
# cielago-landsraad-verify.timer. Reuses the existing cielago-announce poster.
set -uo pipefail

source /opt/cielago/.env 2>/dev/null || true
CHAN="${CIELAGO_AUDIT_CHANNEL_ID:-}"
LOG=/var/log/cielago-landsraad-verify.log

OUT=$(ssh -o BatchMode=yes -o ConnectTimeout=15 lastsietch-dune '/root/dune-verify-landsraad-goal.py' 2>/dev/null)
VERDICT=$(printf '%s\n' "$OUT" | head -1)
[ -n "$VERDICT" ] || VERDICT="[ERROR] could not retrieve the verdict from lastsietch-dune"

TMPF=$(mktemp)
{
  echo "**Landsraad weekly-goal verification — term-4 rollover (2026-06-09)**"
  echo "$VERDICT"
  echo "_(auto-check: confirms the m_TaskGoalAmount 70000 -> 20000 change took effect for the new term)_"
} > "$TMPF"

if [ -n "$CHAN" ]; then
  /opt/cielago/cielago-announce.sh "$TMPF" "$CHAN" >> "$LOG" 2>&1 || true
fi
{ echo "=== $(date -u +%FT%TZ) ==="; cat "$TMPF"; } >> "$LOG"
rm -f "$TMPF"
