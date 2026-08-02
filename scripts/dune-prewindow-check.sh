#!/usr/bin/env bash
# Dune pre-window readiness check — 100% READ-ONLY. Restarts nothing, deploys
# nothing. Run when Funcom announces a patch to confirm we can execute the
# maintenance window without hitting a permission/access wall.
# Usage: /root/dune-prewindow-check.sh
set -u
NS="${DUNE_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
BG="${DUNE_BG:-sh-<your-hostid>-<random>}"
ok(){ printf '  \033[32m[OK]\033[0m %s\n' "$1"; }
no(){ printf '  \033[31m[!!]\033[0m %s\n' "$1"; }

echo "=== Dune pre-window readiness ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="

echo "-- kubectl update permissions --"
for vr in 'patch igwbg' 'get igwbg' 'get pods' 'create pods/exec' 'get secrets' \
          'patch serversets' 'get configmaps' 'patch deployments'; do
  set -- $vr
  a=$(kubectl auth can-i "$1" "$2" -n "$NS" 2>/dev/null)
  [ "$a" = yes ] && ok "can-i $vr" || no "can-i $vr ($a)"
done

echo "-- system access --"
sudo -n true 2>/dev/null && ok "sudo -n passwordless" || no "sudo -n denied"
[ "$(kubectl get --raw=/readyz 2>/dev/null)" = ok ] && ok "cluster /readyz ok" || no "cluster not ready"

echo "-- deploy rails --"
for f in /root/dune-grant.sh /root/dune-relay-dispatch.sh /root/apply-dune-memory-limits.sh; do
  [ -x "$f" ] && ok "$(basename "$f") executable" || no "$(basename "$f") missing/not-exec"
done

echo "-- staged changes (dry-run, no apply) --"
if grep -q 'DeepDesert_1\]="4 22"' /root/apply-dune-memory-limits.sh 2>/dev/null; then
  ok "DD 22Gi staged in apply-dune-memory-limits.sh"
else
  no "DD 22Gi NOT staged (check /root/PENDING-NEXT-WINDOW.md)"
fi
bash /root/apply-dune-memory-limits.sh >/tmp/prewin-ddlimits.out 2>&1
grep -q '"22Gi"' /tmp/prewin-ddlimits.out && ok "DD-limits dry-run produces valid 22Gi patch" \
  || no "DD-limits dry-run unexpected (see /tmp/prewin-ddlimits.out)"

echo "-- backups (rollback readiness) --"
# Authoritative game-DB dumps live on the web host (pg_dump feeder, every 6h) and
# are swept offsite to the network storage. From <game-host> we verify the pre-update
# snapshot staging dir; offsite dump freshness is the web host's job + the DR drill.
if [ -d /root/dune-update-snap ] && [ -w /root/dune-update-snap ]; then
  ok "pre-update snapshot dir /root/dune-update-snap writable"
else
  no "/root/dune-update-snap missing or not writable"
fi
echo "     (offsite game-DB dumps: the web host /opt/backups/dune + network storage restic)"

echo "-- pending window queue --"
[ -f /root/PENDING-NEXT-WINDOW.md ] && ok "PENDING-NEXT-WINDOW.md present ($(grep -c '^## ' /root/PENDING-NEXT-WINDOW.md) item(s))" \
  || echo "  (no pending-window queue file)"

echo "=== done — all ✔ = clear to execute a window on permissions ==="
