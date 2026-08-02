#!/usr/bin/env bash
# /opt/lastsietch-bg-watchdog/pause.sh   (repo mirror: ops/lastsietch-bg-watchdog/pause.sh)
# Pause the BattleGroup auto-recovery watchdog for an INTENTIONAL stop (our update windows,
# where the runbook deliberately stops the BattleGroup). While paused, the watchdog detects
# Stopped but takes NO action.
#
# Deliberately SEPARATE from the pod-watcher maintenance mute (maint-window.sh), which only
# suppresses crash ALERTS. Keeping them independent means you can silence alert noise during a
# provider window (e.g. a provider router-reboot window) WITHOUT disabling auto-recovery.
#
#   pause.sh on [minutes]   # default 120, capped at MAX_MIN
#   pause.sh off
#   pause.sh status
set -u
STATE_DIR=${BGW_STATE_DIR:-/var/lib/lastsietch-bg-watchdog}
PAUSE_FILE=$STATE_DIR/pause
MAX_MIN=${BGW_PAUSE_MAX_MIN:-360}   # never pause longer than 6h in one arm

mkdir -p "$STATE_DIR"

case "${1:-}" in
  on)
    mins=${2:-120}
    [[ "$mins" =~ ^[0-9]+$ ]] || { echo "minutes must be an integer" >&2; exit 1; }
    [ "$mins" -lt 1 ] && mins=1
    [ "$mins" -gt "$MAX_MIN" ] && { echo "capping to MAX_MIN=$MAX_MIN" >&2; mins=$MAX_MIN; }
    exp=$(( $(date -u +%s) + mins * 60 ))
    echo "$exp" > "$PAUSE_FILE"
    echo "bg-watchdog auto-recovery PAUSED for ${mins} min (until $(date -u -d "@$exp" '+%Y-%m-%dT%H:%M:%SZ'))"
    ;;
  off)
    if [ -f "$PAUSE_FILE" ]; then rm -f "$PAUSE_FILE"; echo "bg-watchdog auto-recovery RESUMED"; else echo "not paused"; fi
    ;;
  status)
    if [ -f "$PAUSE_FILE" ]; then
      exp=$(head -n1 "$PAUSE_FILE" | tr -dc '0-9'); now=$(date -u +%s)
      if [ -n "$exp" ] && [ "$now" -lt "$exp" ]; then
        echo "PAUSED: $(( (exp - now + 59) / 60 )) min left (until $(date -u -d "@$exp" '+%Y-%m-%dT%H:%M:%SZ'))"
      else
        echo "EXPIRED (marker present but past expiry; next watchdog run self-clears)"
      fi
    else
      echo "active (auto-recovery ENABLED)"
    fi
    ;;
  *)
    echo "usage: pause.sh on [minutes] | off | status" >&2; exit 1 ;;
esac
