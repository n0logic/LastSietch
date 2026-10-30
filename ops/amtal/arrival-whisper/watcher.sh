#!/usr/bin/env bash
# Amtal arrival whisper. Runs one sweep per invocation, fired by a systemd timer
# every 60s. Same idiom as /opt/lastsietch-welcome-pack/watcher.sh.
#
# CANONICAL copy. Deploys to <game-host>:/opt/lastsietch-amtal-arrival-whisper/watcher.sh
# (runs as root via amtal-arrival-whisper.service/.timer).
#
# Why it exists: Amtal (partition 33, "Amtal-Full-PvP") is full PvP in the open
# world, but the client-rendered zone banner out there still reads PvE. A player
# who trusts the banner gets killed by a rule the HUD told them did not apply.
# This whispers every player each time they arrive (Medic 2026-10-17: every
# login, base owners included).
#
# Per invocation:
#   1. Query the online players whose pawn sits in partition 33.
#   2. Forget anyone in the state file who is no longer here (logged off or
#      left the partition), so their next arrival is warned again.
#   3. Whisper everyone present who is not in the state file, and record them.

set -euo pipefail

# Overridable only so the sweep can be exercised off-host with a stub DQ before
# deploy; the deployed unit uses these defaults.
DQ="${AMTAL_DQ:-/root/dq.sh}"
HERALD="${AMTAL_HERALD:-/opt/lastsietch-rmq-bridge/dune-chat-herald.py}"
CIELAGO_HOSTID=${CIELAGO_HOSTID:?set CIELAGO_HOSTID}
CIELAGO_FUNCOMID="Cielago#47840"

STATE_DIR="${AMTAL_STATE_DIR:-/var/lib/amtal-arrival-whisper}"
STATE_FILE="$STATE_DIR/warned.tsv"          # controller_id<TAB>epoch_seconds, one row per player present
LOG_FILE="${AMTAL_LOG_FILE:-/var/log/amtal-arrival-whisper.log}"
DISABLE_FLAG=/opt/lastsietch-amtal-arrival-whisper/DISABLED

# Kill switch and knobs. AMTAL_WHISPER_ENABLED=0 in the systemd drop-in stops
# delivery without redeploying; the DISABLE_FLAG file stops it without a
# daemon-reload. Either one is enough.
AMTAL_WHISPER_ENABLED="${AMTAL_WHISPER_ENABLED:-1}"
# Partition to watch. Overridable so a dry run can be smoke-tested against a
# populated partition without whispering anyone on Amtal.
AMTAL_PARTITION="${AMTAL_PARTITION:-33}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# Player-facing copy, Medic's wording verbatim (2026-10-17). A %s in a part would
# be the recipient display name; this copy has none. Parts are sent in order.
AMTAL_WHISPER_PARTS=(
  "Welcome to Amtal! 🏜️ Sandworms aren't the only thing hunting you out here. Stay safe in Helius Gate and Riftwatch — everywhere else, it's every Sleeper for themselves."
)

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [amtal-arrival] $*"
}

run_psql() {
  "$DQ" -t -A -F'|' -v ON_ERROR_STOP=1 "$@"
}

if [[ "$AMTAL_WHISPER_ENABLED" != "1" ]]; then
  log "disabled via AMTAL_WHISPER_ENABLED, nothing to do"
  exit 0
fi
if [[ -e "$DISABLE_FLAG" ]]; then
  log "disabled via $DISABLE_FLAG, nothing to do"
  exit 0
fi

mkdir -p "$STATE_DIR"
touch "$STATE_FILE"

now=$(date +%s)

prune_state() {
  # Keep only players still present this sweep ($1 = newline-separated controller
  # ids). Anyone who logged off or left drops out and is warned on next arrival.
  local present="$1" tmp
  tmp=$(mktemp "$STATE_DIR/.warned.XXXXXX")
  # printf always emits at least one line, so NR==FNR never spills into the state file.
  awk -F'\t' 'NR==FNR {if ($0 != "") keep[$0]=1; next} $1 in keep' \
    <(printf '%s\n' "$present") "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

already_warned() {
  # True if this controller id was warned during its current stay.
  local cid="$1"
  awk -F'\t' -v cid="$cid" 'BEGIN{hit=1} $1 == cid {hit=0} END{exit hit}' "$STATE_FILE"
}

record_warned() {
  local cid="$1"
  printf '%s\t%s\n' "$cid" "$now" >> "$STATE_FILE"
}

whisper_arrival() {
  local fid="$1" disp="$2" cid="$3"
  local ok=1 sent=0 msg
  for part in "${AMTAL_WHISPER_PARTS[@]}"; do
    printf -v msg "$part" "$disp"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "  DRY RUN would whisper ${fid}: ${msg}"
      sent=$((sent + 1))
      continue
    fi
    if PATH="/usr/local/bin:$PATH" python3 "$HERALD" --direct \
          --user-id "$CIELAGO_HOSTID" --from-id "$CIELAGO_FUNCOMID" --send \
          whisper --to "$fid" --message "$msg" >> "$LOG_FILE" 2>&1; then
      sent=$((sent + 1))
    else
      ok=0
    fi
    sleep 1   # preserve ordering between parts
  done
  if [[ $ok -eq 1 ]]; then
    log "  sent ${sent}-part Amtal warning to ${fid}"
    # Record on dry run too, and the operator clears the state file after the
    # smoke test; recording on failure would silently swallow the warning.
    record_warned "$cid"
  else
    log "  PARTIAL/FAILED for ${fid} (sent ${sent}), will retry next sweep"
  fi
}

# Online players standing in the watched partition. character_state filter keeps
# deleted characters out, matching the welcome-pack watcher's online check.
arrivals=$(run_psql -c "
  SELECT eps.player_controller_id, a.funcom_id
    FROM dune.encrypted_player_state eps
    JOIN dune.actors act ON act.id = eps.player_pawn_id
    JOIN dune.accounts a ON a.id = eps.account_id
   WHERE act.partition_id = ${AMTAL_PARTITION}
     AND eps.online_status <> 'Offline'
     AND eps.character_state IS DISTINCT FROM 'Deleted';") || {
  log "query failed, skipping this sweep"
  exit 0
}

# Only prune on a successful query, so a DB blip cannot re-warn everyone.
prune_state "$(cut -d'|' -f1 <<< "$arrivals")"

count=0
while IFS='|' read -r cid fid; do
  [[ -z "${cid// }" ]] && continue
  if [[ -z "$fid" || "$fid" != *"#"* ]]; then
    log "  no usable funcom_id for controller $cid, skipping"
    continue
  fi
  if already_warned "$cid"; then
    continue
  fi
  count=$((count + 1))
  log "new arrival on partition ${AMTAL_PARTITION}: ${fid} (controller ${cid})"
  whisper_arrival "$fid" "${fid%%#*}" "$cid"
done <<< "$arrivals"

[[ $count -gt 0 ]] && log "sweep done, ${count} arrival(s) warned"
exit 0
