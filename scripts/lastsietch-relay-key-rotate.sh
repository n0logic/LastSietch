#!/usr/bin/env bash
# Quarterly rotation helper for the lastsietch-relay -> lastsietch-dune SSH key on
# the web host. RUN BY HAND (no timer) because the forced-command pin on
# lastsietch-dune intentionally does not expose a "write to authorized_keys"
# action — so the operator pastes the new pubkey manually as part of the
# rotation. Auto-activation lands in VC9 when we add a sidecar admin SSH
# path that bypasses the dispatcher.
#
# Workflow:
#   1. Operator runs this with --stage to generate id_ed25519.next + pub.
#   2. Operator pastes APPEND_LINE into lastsietch-dune /root/.ssh/authorized_keys.
#   3. Operator runs this with --activate to smoke + atomic-swap to new key,
#      archive the previous keypair under .ssh/archive/, and print the line
#      that should be REMOVED from lastsietch-dune authorized_keys.
#
# All actions log to /var/log/lastsietch-relay-key-rotate.log (root-owned).
# Reminder: schedule a calendar nudge quarterly; do not run from cron.

set -euo pipefail

KEY_DIR="/opt/lastsietch-relay/.ssh"
LIVE_KEY="${KEY_DIR}/id_ed25519"
LIVE_PUB="${LIVE_KEY}.pub"
NEXT_KEY="${KEY_DIR}/id_ed25519.next"
NEXT_PUB="${NEXT_KEY}.pub"
ARCHIVE_DIR="${KEY_DIR}/archive"
LOG_FILE="/var/log/lastsietch-relay-key-rotate.log"
DUNE_HOST="<game-host>"
KNOWN_HOSTS="${KEY_DIR}/known_hosts"
PIN='command="/root/dune-relay-dispatch.sh",restrict'

log() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | sudo tee -a "$LOG_FILE" >/dev/null
  printf '%s\n' "$*"
}

require_user() {
  if [ "$(id -un)" != "lastsietch-relay" ]; then
    echo "must run as lastsietch-relay (got $(id -un)); use 'sudo -u lastsietch-relay $0 $*'" >&2
    exit 2
  fi
}

stage() {
  require_user "$@"
  mkdir -p "$ARCHIVE_DIR"; chmod 0700 "$ARCHIVE_DIR"
  if [ -e "$NEXT_KEY" ]; then
    log "stage: $NEXT_KEY already exists (abort prior run?); refusing to overwrite"
    exit 3
  fi
  ssh-keygen -q -t ed25519 -f "$NEXT_KEY" -N '' \
    -C "lastsietch-relay@the web host staged $(date -u +%F)"
  local pub_line; pub_line="$(cat "$NEXT_PUB")"
  log "stage: generated next key fp=$(ssh-keygen -lf "$NEXT_PUB" | awk '{print $2}')"
  cat <<EOF

==== ACTION REQUIRED ===========================================================
Paste this line into lastsietch-dune /root/.ssh/authorized_keys (next free row):

${PIN} ${pub_line}

After paste, run:  sudo -u lastsietch-relay $0 --activate
================================================================================
EOF
}

activate() {
  require_user "$@"
  if [ ! -e "$NEXT_KEY" ]; then
    log "activate: no $NEXT_KEY — run --stage first"
    exit 4
  fi
  # Smoke the new key.
  if ! ssh -o BatchMode=yes -o ConnectTimeout=15 \
         -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=${KNOWN_HOSTS}" \
         -i "$NEXT_KEY" -l root "$DUNE_HOST" "status" >/dev/null 2>&1; then
    log "activate: new-key smoke failed; aborting (old key intact)"
    exit 5
  fi
  log "activate: new-key smoke ok"

  local old_pub_line; old_pub_line="$(cat "$LIVE_PUB")"
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$LIVE_KEY" "${ARCHIVE_DIR}/id_ed25519.${stamp}"
  mv "$LIVE_PUB" "${ARCHIVE_DIR}/id_ed25519.pub.${stamp}"
  mv "$NEXT_KEY" "$LIVE_KEY"
  mv "$NEXT_PUB" "$LIVE_PUB"
  log "activate: swapped (old archived as id_ed25519.${stamp})"

  cat <<EOF

==== ACTION REQUIRED ===========================================================
Remove this stale line from lastsietch-dune /root/.ssh/authorized_keys:

${PIN} ${old_pub_line}

(or any line whose pubkey matches the .pub above)
================================================================================
EOF
}

revoke_emergency() {
  log "revoke-emergency:"
  cat <<'EOF'

Run on lastsietch-dune (via operator@wsl2 free-shell key):

  ssh lastsietch-dune "sed -i '/lastsietch-relay@the web host/d' /root/.ssh/authorized_keys"

Then on the web host:
  sudo systemctl stop lastsietch-relay

EOF
}

case "${1:-}" in
  --stage)    shift; stage "$@" ;;
  --activate) shift; activate "$@" ;;
  --revoke-help) revoke_emergency ;;
  *)
    cat <<EOF
Usage: $0 {--stage|--activate|--revoke-help}
  --stage        Generate next keypair + print APPEND_LINE for lastsietch-dune.
  --activate     Smoke next keypair, atomic-swap into live, archive old,
                 print REMOVE line for lastsietch-dune authorized_keys cleanup.
  --revoke-help  Show the emergency revoke runbook.
EOF
    exit 1
    ;;
esac
