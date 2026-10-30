#!/usr/bin/env bash
# Game-host kill-switch files: read them all, or flip exactly one.
# Deployed to <game-host>:/root/dune-flag-set.sh and reached ONLY through the
# forced-command dispatcher's `flag-set` / `flag-list` actions.
#
# These files under /etc/lastsietch are what the WRITERS read. The portal-side env gate
# only decides whether the door is offered; this is the switch that actually
# darks or lights a live player feature, which is why the allowlist is repeated
# here rather than trusted from the caller. The relay validated the name and the
# state, the dispatcher re-validated both, and this script validates once more
# before it touches a path. Any one of the three can be wrong; all three being
# wrong the same way is the thing worth making unlikely.
#
# It creates and removes files under /etc/lastsietch and appends to a log. It NEVER
# restarts a game pod, the BattleGroup Director, the BattleGroup or k3s, and it
# never touches the database.
#
# Usage:
#   dune-flag-set.sh --list                 JSON state of every allowlisted flag
#   dune-flag-set.sh <name>:<on|off>        flip one, then re-read it
#
# Exit: 0 fine, 2 refused (bad name, bad state, bad arg), 4 the write did not
# take (the fresh re-read disagreed with what was asked for). Stdout is JSON on
# every path including a refusal, so the relay hands the caller a structured
# answer rather than a flattened 502.
set -euo pipefail

FLAG_DIR="${LASTSIETCH_FLAG_DIR:-/etc/lastsietch}"
LOG_FILE="${LASTSIETCH_FLAG_LOG:-/var/log/lastsietch-relay-dispatch.log}"

# The eleven, and only the eleven. Wave 5's market-sell-backpack-enabled, wave 7's
# item-transfer-enabled, wave 10's refinery-enabled and the Landsraad claim's
# landsraad-claim-enabled are all here from the start, so each kill switch exists
# before the feature it guards. For landsraad-claim-enabled "on" only creates an
# EMPTY file, which its writer reads as nobody: the board can close it, not open it.
ALLOWED="karum-enabled karum-wtb-enabled augment-enabled reward-enabled
servercmd-enabled market-sell-container-enabled storage-pawn-move-enabled
market-sell-backpack-enabled item-transfer-enabled landsraad-claim-enabled
refinery-enabled"

refuse() {
  printf '{"ok":false,"error":"%s"}\n' "$1"
  exit "${2:-2}"
}

is_allowed() {
  local candidate="$1" n
  for n in $ALLOWED; do
    if [ "$n" = "$candidate" ]; then return 0; fi
  done
  return 1
}

# on|off for one flag, from a fresh stat every time it is called.
state_of() {
  if [ -f "$FLAG_DIR/$1" ]; then echo on; else echo off; fi
}

list_all() {
  local first=1 n
  printf '{"ok":true,"dir":"%s","read_utc":"%s","flags":[' \
    "$FLAG_DIR" "$(date -u +%FT%TZ)"
  for n in $ALLOWED; do
    [ "$first" -eq 1 ] || printf ','
    first=0
    printf '{"name":"%s","state":"%s"}' "$n" "$(state_of "$n")"
  done
  printf ']}\n'
}

arg="${1:-}"

if [ "$arg" = "--list" ]; then
  list_all
  exit 0
fi

[ -n "$arg" ] || refuse "flag-set needs <name>:<on|off>"
[[ "$arg" =~ ^[[:lower:]-]+:(on|off)$ ]] || refuse "flag-set takes <name>:<on|off>"

name="${arg%%:*}"
want="${arg##*:}"
is_allowed "$name" || refuse "flag not in the allowlist"

path="$FLAG_DIR/$name"
before=$(state_of "$name")

if [ "$want" = "on" ]; then
  mkdir -p "$FLAG_DIR"
  touch "$path"
else
  rm -f "$path"
fi

# 🔴 The RE-READ, not the request. `verified` is a fresh test -f taken after the
# change, so a write that silently did not land (read-only mount, a directory
# where a file was expected) reports false instead of echoing back what was
# asked for.
after=$(state_of "$name")
changed=false
if [ "$before" != "$after" ]; then changed=true; fi
verified=false
if [ "$after" = "$want" ]; then verified=true; fi

client="${SSH_CLIENT:-}"
echo "$(date -u +%FT%TZ) flag-set ${name}=${want} was=${before} now=${after} changed=${changed} verified=${verified} [${client%% *}]" \
  >> "$LOG_FILE" 2>/dev/null || true

printf '{"ok":%s,"name":"%s","state":"%s","changed":%s,"verified":%s}\n' \
  "$verified" "$name" "$after" "$changed" "$verified"

[ "$verified" = true ] || exit 4
exit 0
