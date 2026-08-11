#!/usr/bin/env bash
# Pin three PvP/safety settings in the shared UserGame.ini on the Hagga PVC.
#
# Context: 2026-08-03 research pass (docs/dune-research + red-blink-research
# FINDINGS-2026-08-03-portal-pvp.md). The live Kulon config was already correct
# in the two ways that matter (m_bShouldForceEnablePvpOnAllPartitions=False, and
# security zones effectively True via stock DefaultGame.ini:1410). These three
# edits close the remaining "running on an unread default" gaps.
#
# WHAT IT CHANGES (all three are assertions of the already-effective behaviour,
# except the cooldown which is a deliberate tuning change):
#   1. +m_PveEnabledPartitions=1        assert Habbanya as PvE instead of relying
#                                       on the stock NullSec->Security PveFallback
#   2. m_bSecurityZonesForceEnablePvp=False
#                                       Funcom ships no default for this key, so it
#                                       ran on an unread code default. True would
#                                       force PvP INSIDE security zones, i.e. make
#                                       the tradeposts hostile.
#   3. s_RepeatedKillCooldown=600.0     anti-spawncamp, raised from stock 300
#
# HARD RULES:
#   - NEVER restarts game pods / BGD / k3s. These are startup-read INI keys, so
#     they take effect at the NEXT restart (ride an already-planned window).
#   - Backup-before-write to UserGame.ini.bak-lastsietch-<UTC> on the pod.
#   - Atomic: write staging file on the pod FS, then mv onto the target.
#   - Idempotent: re-running is a no-op once all three keys are present.
#   - Refuses to run if the file does not look like the file we analysed.
#
# SCOPE: /home/dune/server/DuneSandbox/Saved/UserSettings/UserGame.ini lives on
# the SHARED PVC (sh-*-pvc) mounted by every sg-survival-1 pod, verified
# 2026-08-03 by comparing the claimName on pod-1 and pod-32. One write therefore
# covers Habbanya (1), Kulon (32) and Amtal (33). Per-partition divergence needs
# -ini:game: pod args, which is how Amtal disables security zones.
#
# Usage: ./apply-pvp-pins.sh [--dry-run]
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

SSH_HOST="${GAME_HOST:-game-host}"
NS="funcom-seabass-sh-<your-hostid>-<random>"
POD="sh-<your-hostid>-<random>-sg-survival-1-pod-1"
DIR="/home/dune/server/DuneSandbox/Saved/UserSettings"
TARGET="$DIR/UserGame.ini"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

# Simple argv passthrough. Do NOT use this for compound remote shell commands:
# $* flattens quoting, which silently split a `sh -c "chmod .. && mv .."` into
# separate words and produced `chmod: missing operand` on 2026-08-03. For
# anything with quotes or &&, use kx() below.
k() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" "sudo k3s kubectl -n $NS $*"; }

# Run a single-quoted shell snippet inside the pod, quoting preserved.
kx() {
  local snippet="$1"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" \
    "sudo k3s kubectl -n $NS exec $POD -- sh -c '$snippet'"
}

echo "== preflight =="
# The pod must exist and be the one we think it is.
if ! k exec "$POD" -- test -f "$TARGET"; then
  echo "FATAL: $TARGET not found on $POD" >&2; exit 1
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
k exec "$POD" -- cat "$TARGET" > "$TMP/cur.ini"
BEFORE_MD5="$(md5sum < "$TMP/cur.ini" | cut -d' ' -f1)"
echo "current md5: $BEFORE_MD5  bytes: $(wc -c < "$TMP/cur.ini")"

# Guard: the anchors we edit against must be present, or the file has drifted
# from what the analysis was based on and a blind edit would land wrong.
grep -qF '+m_PveEnabledPartitions=8' "$TMP/cur.ini" \
  || { echo "FATAL: PvE anchor '+m_PveEnabledPartitions=8' missing" >&2; exit 2; }
grep -qF '[/Script/DuneSandbox.SecurityZonesSubsystem]' "$TMP/cur.ini" \
  || { echo "FATAL: SecurityZonesSubsystem section missing" >&2; exit 2; }
# Guard: never silently flip the two settings that define the design.
grep -qE '^m_bShouldForceEnablePvpOnAllPartitions=False' "$TMP/cur.ini" \
  || { echo "FATAL: ForceEnablePvpOnAllPartitions is not False; investigate before editing" >&2; exit 2; }

python3 "$(dirname "$0")/edit-usergame.py" "$TMP/cur.ini" "$TMP/new.ini"
echo "== diff =="
diff -u "$TMP/cur.ini" "$TMP/new.ini" || true

if ! diff -q "$TMP/cur.ini" "$TMP/new.ini" >/dev/null; then
  :
else
  echo "== already pinned, nothing to do =="; exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "== DRY RUN, no write =="; exit 0
fi

echo "== backup =="
k exec "$POD" -- cp "$TARGET" "$TARGET.bak-lastsietch-$TS"
k exec "$POD" -- ls -la "$TARGET.bak-lastsietch-$TS"

echo "== write (atomic) =="
# Preserve the target's existing owner and mode. The game server runs as `dune`
# and rewrites this file on shutdown; landing it root-owned 644 would leave a
# file the server cannot write. Capture before staging, re-apply before the mv.
OWNER="$(k exec "$POD" -- stat -c '%U:%G' "$TARGET" | tr -d '\r')"
MODE="$(k exec "$POD" -- stat -c '%a' "$TARGET" | tr -d '\r')"
echo "preserving owner=$OWNER mode=$MODE"
[ -n "$OWNER" ] && [ -n "$MODE" ] || { echo "FATAL: could not read target owner/mode" >&2; exit 3; }

ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" \
  "sudo k3s kubectl -n $NS exec -i $POD -- sh -c 'cat > $DIR/.UserGame.ini.staging'" < "$TMP/new.ini"
kx "chown $OWNER $DIR/.UserGame.ini.staging && chmod $MODE $DIR/.UserGame.ini.staging && mv $DIR/.UserGame.ini.staging $TARGET"

echo "== verify =="
k exec "$POD" -- cat "$TARGET" > "$TMP/after.ini"
if diff -q "$TMP/new.ini" "$TMP/after.ini" >/dev/null; then
  echo "OK: on-pod content matches intended content"
else
  echo "FATAL: readback mismatch. Restore: mv $TARGET.bak-lastsietch-$TS $TARGET" >&2
  diff -u "$TMP/new.ini" "$TMP/after.ini" | head -40 >&2
  exit 3
fi
k exec "$POD" -- grep -nE 'm_PveEnabledPartitions|m_bSecurityZonesForceEnablePvp|s_RepeatedKillCooldown' "$TARGET"
echo
echo "DONE. Startup-read keys: they take effect at the NEXT pod restart."
echo "Rollback: sudo k3s kubectl -n $NS exec $POD -- mv $TARGET.bak-lastsietch-$TS $TARGET"
