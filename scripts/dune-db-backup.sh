#!/usr/bin/env bash
# Daily Dune game-server backup (web-host PULL model).
#
# Runs ON the web host (systemd dune-db-backup.timer, 02:30 UTC daily). SSHes to
# <game-host> and:
#   1. pg_dump the live `dune` Postgres DB (streamed plain SQL -> gzip locally)
#   2. snapshot UserGame.ini / UserEngine.ini + the igwbg/igwbgd CRs (tar.gz)
#   3.5 host tooling (scripts/units/token/telemetry) + the k3s bootstrap kit
#        (/root/k3s-bootstrap = CRD defs/operator manifests) + /etc/rancher/k3s
#        (config.yaml + the memory-focused-scheduler profile)
#   3.6 full k8s declarative state (all igw CRs + secrets + configmaps + Saved/)
#   3.7 containerd image export (DIGEST-AWARE): the 11 IN-USE funcom images
#        (4 operators + igw-postgres + the 6 current-build game components). This
#        is DR-CRITICAL: registry.funcom.com is dead, so these tars are the ONLY
#        source for the images, AND the battlegroupoperator image carries the FLS
#        world-identity signing key. The
#        export only re-runs when the in-use image set changes (Funcom build
#        update); unchanged nights skip the ~4.7GB read entirely.
# Output lands in /opt/backups/dune, which is ALREADY in the restic to offsite storage set
# (vps-backup.sh), so each backup is swept offsite on the next restic run.
#
# Strictly read-only against the game: pg_dump + kubectl get + cat + ctr export.
# No DB writes, no pod restart. Safe to run while players are online (the image
# export is ionice/nice-throttled and only fires on a build change).
#
# Restore (DB):      gunzip -c dune-db-<TS>.sql.gz | (the dq.sh psql path)
# Restore (config):  tar -xzf dune-config-<TS>.tar.gz
# Restore (tooling): tar -xzf dune-tooling-<TS>.tar.gz -C /   (scripts/units/token/telemetry DB/k3s kit)
# Restore (images):  k3s ctr -n k8s.io images import dune-images-<TS>.tar   (per build)
set -uo pipefail
umask 077

NS="${DUNE_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
DBPOD=sh-<your-hostid>-<random>-db-dbdepl-sts-0
BG="${DUNE_BG:-sh-<your-hostid>-<random>}"
PVC_INI="/var/lib/rancher/k3s/storage/pvc-b505f204-1724-4521-ba62-71b3bdb544ec_${NS}_${BG}-pvc/Saved/UserSettings"
SSH_HOST="${SSH_HOST:-<game-host>}"     # ssh alias or host for the game box
DEST="${DEST:-/opt/backups/dune}"
KEEP_DAYS="${KEEP_DAYS:-2}"
LOG="${LOG:-/var/log/dune-db-backup.log}"
MIN_DB_BYTES="${MIN_DB_BYTES:-500000}"   # healthy gz dump ~2.5MB; below 500KB = suspect
MAILTO="${MAILTO:-}"   # set to receive failure mail
NOTIFY_FAILURE="${NOTIFY_FAILURE:-1}"

UTC=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$DEST"

log(){ printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$1" | tee -a "$LOG" >&2; }
fail(){
  log "FAIL: $1"
  if [ "$NOTIFY_FAILURE" = 1 ] && command -v sendmail >/dev/null 2>&1; then
    { printf 'From: dune-backup@localhost\nSubject: [Dune Backup FAILED] %s\n\n%s\n' \
        "$UTC" "$1"; } | sendmail "$MAILTO" 2>/dev/null || true
  fi
  exit 1
}
remote(){ ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" "$@"; }

log "=== dune backup start ($UTC) ==="

# --- 1. DB dump and integrity checks, with bounded fresh attempts -----------
# A successful exec transport can still return a truncated SQL stream. Keep
# rejected attempts and publish only a dump that passes every integrity gate.
FINAL_DBFILE="$DEST/dune-db-$UTC.sql.gz"
[ ! -e "$FINAL_DBFILE" ] && [ ! -L "$FINAL_DBFILE" ] || fail "database dump destination already exists"
PLAIN=""
trap '[ -z "$PLAIN" ] || rm -f -- "$PLAIN"' EXIT
reject_dump(){ DUMP_ERROR="$1"; return 1; }
dump_once(){
remote "sudo kubectl exec -n $NS $DBPOD -- sh -c 'PGPASSWORD=\$POSTGRES_PASSWORD pg_dump -h localhost -p 15432 -U postgres -d dune'" \
  2>>"$LOG" | gzip > "$DBFILE"
rc=("${PIPESTATUS[@]}")
[ "${rc[0]}" -eq 0 ] || { reject_dump "pg_dump/ssh exited ${rc[0]}"; return 1; }
[ "${rc[1]}" -eq 0 ] || { reject_dump "gzip exited ${rc[1]}"; return 1; }

# --- 2. DB integrity gates ---------------------------------------------------
# Decompress ONCE to a temp file and run all content checks against the file.
# (Piping `gunzip -c | grep -q` would SIGPIPE gunzip on early match and, under
# `set -o pipefail`, falsely report failure.)
gzip -t "$DBFILE" 2>>"$LOG" || { reject_dump "gzip integrity test failed"; return 1; }
SIZE=$(stat -c%s "$DBFILE") || { reject_dump "dump size check failed"; return 1; }
[ "$SIZE" -ge "$MIN_DB_BYTES" ] || { reject_dump "dump suspiciously small ($SIZE bytes < $MIN_DB_BYTES)"; return 1; }
PLAIN=$(mktemp "$DEST/.verify.XXXXXX") || { reject_dump "verification file creation failed"; return 1; }
gunzip -c "$DBFILE" > "$PLAIN" 2>>"$LOG" || { reject_dump "gunzip decompress failed"; return 1; }
tail -5 "$PLAIN" | grep -F "PostgreSQL database dump complete" > /dev/null \
  || { reject_dump "dump missing completion marker (truncated?)"; return 1; }
# Spot-check core game + custom tables (pg_dump emits schema-qualified DDL).
grep -qE "^CREATE TABLE dune\.encrypted_accounts " "$PLAIN" \
  || { reject_dump "expected table dune.encrypted_accounts missing from dump"; return 1; }
grep -qE "^CREATE TABLE dune\.base_backups " "$PLAIN" \
  || fail "expected table dune.base_backups missing from dump"
grep -qE "^CREATE TABLE lsadmin\.bans " "$PLAIN" \
  || log "WARN: lsadmin.bans not found in dump (ok if schema predates moderation)"
# Table-count floor: live DB has ~170 tables; a healthy dump is well above 100.
TBL_COUNT=$(grep -cE "^CREATE TABLE " "$PLAIN")
[ "$TBL_COUNT" -ge 100 ] || { reject_dump "dump has only $TBL_COUNT tables (<100); likely incomplete"; return 1; }
return 0
}

for ATTEMPT in 1 2 3; do
  DBFILE="$DEST/dune-db-$UTC.attempt-$ATTEMPT.sql.gz"
  [ ! -e "$DBFILE" ] && [ ! -L "$DBFILE" ] || fail "database dump attempt destination already exists"
  DUMP_ERROR=""
  if dump_once; then
    rm -f -- "$PLAIN"; PLAIN=""
    if ! mv -nT -- "$DBFILE" "$FINAL_DBFILE" || [ -e "$DBFILE" ]; then
      fail "validated database dump publication failed"
    fi
    DBFILE="$FINAL_DBFILE"
    [ "$ATTEMPT" -eq 1 ] || log "DB dump recovered on attempt $ATTEMPT"
    break
  fi
  if [ -n "$PLAIN" ]; then rm -f -- "$PLAIN"; PLAIN=""; fi
  [ "$ATTEMPT" -lt 3 ] || fail "$DUMP_ERROR after 3 attempts; rejected dumps retained"
  log "WARN: DB dump attempt $ATTEMPT rejected: $DUMP_ERROR; retaining $(basename "$DBFILE") and retrying"
  sleep 2
done
trap - EXIT
log "dump table count: $TBL_COUNT"

# --- 3. Config + CR snapshot -------------------------------------------------
CFGFILE="$DEST/dune-config-$UTC.tar.gz"
TMP=$(mktemp -d)
remote "sudo cat '$PVC_INI/UserGame.ini'"   > "$TMP/UserGame.ini"   2>>"$LOG" || fail "UserGame.ini pull failed"
remote "sudo cat '$PVC_INI/UserEngine.ini'" > "$TMP/UserEngine.ini" 2>>"$LOG" || fail "UserEngine.ini pull failed"
remote "sudo kubectl -n $NS get igwbg $BG -o yaml" > "$TMP/igwbg.yaml" 2>>"$LOG" || fail "igwbg CR pull failed"
# igwbgd is a bonus (the BG-database CR); tolerate absence.
remote "sudo kubectl -n $NS get igwbgd -o yaml" > "$TMP/igwbgd.yaml" 2>>"$LOG" || log "WARN: igwbgd CR pull failed (non-fatal)"
tar -czf "$CFGFILE" -C "$TMP" . 2>>"$LOG" || fail "config tar failed"
rm -rf "$TMP"
[ -s "$CFGFILE" ] || fail "config tar empty"
tar -xzOf "$CFGFILE" ./UserGame.ini | grep -q "m_PvpEnabledPartitions" \
  || log "WARN: UserGame.ini in tar missing expected PvP key"

# --- 3.5 Host tooling snapshot (scripts + units + token + telemetry DB) -------
# Everything needed to rebuild the <game-host> operational layer from cold, beyond
# the game itself (Steam appid 4754530) and the DB/config above. The scripts'
# source-of-truth is the House0fL0gic repo, but this captures the LIVE host so a
# rebuild never depends on the repo being reachable. Big/ephemeral trees
# (RMQ capture dirs, venvs, pycache) are excluded; tar stores paths without the
# leading '/', so restore with `tar -xzf ... -C /`.
TOOLFILE="$DEST/dune-tooling-$UTC.tar.gz"
remote "sudo tar -czf - --ignore-failed-read \
  --exclude='*/captures' --exclude='*/captures-admin' --exclude='*/captures-events' \
  --exclude='*/venv' --exclude='*/__pycache__' --exclude='*.pyc' \
  /opt/lastsietch-telemetry /opt/lastsietch-rmq-bridge /opt/lastsietch-welcome-pack /etc/lastsietch \
  /etc/systemd/system/lastsietch-*.service /etc/systemd/system/lastsietch-*.timer \
  /etc/systemd/system/dune-*.service /etc/systemd/system/dune-*.timer /etc/systemd/system/lastsietch-*.service /etc/systemd/system/lastsietch-*.timer \
  /root/dune-*.py /root/dune-*.sh /usr/local/bin/lastsietch-dd-align.sh \
  /var/lib/lastsietch-telemetry/telemetry.db \
  /root/k3s-bootstrap \
  /etc/rancher/k3s/config.yaml /etc/rancher/k3s/scheduler.yaml /etc/rancher/k3s/kubelet-config.yaml" \
  2>>"$LOG" > "$TOOLFILE"
gzip -t "$TOOLFILE" 2>>"$LOG" || fail "tooling tar gzip integrity failed"
TOOLSIZE=$(stat -c%s "$TOOLFILE")
[ "$TOOLSIZE" -ge 50000 ] || fail "tooling tar suspiciously small ($TOOLSIZE bytes)"
# Sanity: the dispatcher + a telemetry script must be present.
TOOL_LIST=$(tar -tzf "$TOOLFILE" 2>/dev/null)
printf '%s\n' "$TOOL_LIST" | grep -q "dune-relay-dispatch.sh" \
  || log "WARN: dune-relay-dispatch.sh missing from tooling tar"
printf '%s\n' "$TOOL_LIST" | grep -q "lastsietch-telemetry" \
  || log "WARN: /opt/lastsietch-telemetry missing from tooling tar"
# k3s rebuild kit: the CRD defs + the memory-focused-scheduler profile are both
# load-bearing on a cold rebuild (missing CRD -> operator crashloop; missing
# scheduler.yaml -> server pods stuck Pending). Both bit us on the EPYC migration.
printf '%s\n' "$TOOL_LIST" | grep -q "root/k3s-bootstrap/crds" \
  || log "WARN: /root/k3s-bootstrap/crds missing from tooling tar"
printf '%s\n' "$TOOL_LIST" | grep -q "rancher/k3s/scheduler.yaml" \
  || log "WARN: k3s scheduler.yaml (memory-focused-scheduler) missing from tooling tar"


# --- 3.6 Full k8s declarative state (rebuild-complete) -----------------------
# The COMPLETE operator desired+runtime state so the battlegroup rebuilds
# deterministically on a fresh box: all igw CRs (battlegroup, servergroup, the 30
# serversets + 25 serversetscales, gateway, textrouter, message queues, database,
# databasedeployment, director), the RMQ + gateway SECRETS (without which the
# stack cannot come up), the configmaps, PVC manifests, and the full Saved/ tree
# (minus UE5 logs). Read-only (kubectl get + tar); restic encrypts it offsite.
K8SFILE="$DEST/dune-k8s-$UTC.tar.gz"
KTMP=$(mktemp -d)
PVC_ROOT="${PVC_INI%/Saved/UserSettings}"
remote "sudo kubectl -n $NS get battlegroups,servergroups,serversets,serversetscales,servergateways,textrouters,messagequeues,databases,databasedeployments,battlegroupdirectors -o yaml" > "$KTMP/igw-crs.yaml" 2>>"$LOG" || fail "CR export failed"
remote "sudo kubectl -n $NS get secret rmq-game-secret server-gateway-secret -o yaml" > "$KTMP/secrets.yaml" 2>>"$LOG" || fail "secret export failed"
remote "sudo kubectl -n $NS get configmap -o yaml" > "$KTMP/configmaps.yaml" 2>>"$LOG" || log "WARN: configmap export failed (non-fatal)"
remote "sudo kubectl -n $NS get pvc -o yaml" > "$KTMP/pvcs.yaml" 2>>"$LOG" || log "WARN: pvc export failed (non-fatal)"
remote "sudo tar -czf - --ignore-failed-read --warning=no-file-changed --exclude='Logs' --exclude='Crashes' --exclude='*.log' -C '$PVC_ROOT' Saved" > "$KTMP/saved.tar.gz" 2>>"$LOG" || log "WARN: Saved tree capture failed (non-fatal)"
# Validate the staged exports directly (plain-file grep = no pipe, so no SIGPIPE
# false-failure under `set -o pipefail`) BEFORE archiving, then tar + gzip-test.
grep -q "kind: BattleGroup" "$KTMP/igw-crs.yaml" || fail "k8s export missing BattleGroup CR"
grep -q "name: rmq-game-secret" "$KTMP/secrets.yaml" || fail "k8s export missing rmq-game-secret"
SSCOUNT=$(grep -c "kind: ServerSet$" "$KTMP/igw-crs.yaml")
[ "${SSCOUNT:-0}" -ge 20 ] || log "WARN: only ${SSCOUNT:-0} ServerSet CRs captured (<20)"
tar -czf "$K8SFILE" -C "$KTMP" . 2>>"$LOG" || fail "k8s tar failed"
rm -rf "$KTMP"
gzip -t "$K8SFILE" 2>>"$LOG" || fail "k8s tar gzip integrity failed"
K8SSIZE=$(stat -c%s "$K8SFILE")
log "k8s state: ${SSCOUNT} serversets + scales/secrets/cms/pvc-manifests + Saved tree"

# --- 3.7 Containerd image export (DIGEST-AWARE, DR-critical) ------------------
# registry.funcom.com is DEAD, so the only source for the funcom images is the
# OLD box's containerd cache -> capture them. Identity note: the
# battlegroupoperator image carries the FLS world-identity signing key, so this
# is required to reproduce the world "no re-roll" on a cold rebuild.
#
# Export ONLY the images actually in use by the running stack (the 4 operators +
# igw-postgres + the 6 current-build game components = 11 of the ~41 cached refs;
# the rest are dead historical build tags). The in-use set is derived live, so it
# tracks Funcom build updates automatically.
#
# Digest-aware: fingerprint the in-use set; if it matches the last export AND the
# tar still exists, SKIP (no ~4.7GB read on quiet nights). Only Funcom-build
# nights pay the export cost. Throttled (ionice/nice) and read-only.
IMGFP="$DEST/dune-images-latest.fingerprint"
IMG_REFS=$(remote "sudo kubectl get pods -A -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{\"\n\"}{end}{range .spec.initContainers[*]}{.image}{\"\n\"}{end}{end}'" 2>>"$LOG" | grep -E 'funcom' | sort -u)
if [ -z "$IMG_REFS" ]; then
  log "WARN: no in-use funcom images enumerated; skipping image export"
else
  IMG_COUNT=$(printf '%s\n' "$IMG_REFS" | wc -l)
  # Fingerprint = sha256 over "ref digest" lines (digest from containerd), so any
  # image content change (even a re-tag to the same build) re-triggers export.
  REFLIST=$(printf '%s ' $IMG_REFS)
  IMG_DIGESTS=$(remote "sudo k3s ctr -n k8s.io images ls 2>/dev/null | awk 'NR>1{print \$1, \$3}'" 2>>"$LOG" \
    | grep -F -f <(printf '%s\n' "$IMG_REFS") | sort -u)
  FP=$(printf '%s\n' "$IMG_DIGESTS" | sha256sum | awk '{print $1}')
  PREV_FP=$(cat "$IMGFP" 2>/dev/null || echo none)
  CUR_TAR=$(readlink -f "$DEST/dune-images-latest.tar" 2>/dev/null || true)
  if [ "$FP" = "$PREV_FP" ] && [ -n "$CUR_TAR" ] && [ -s "$CUR_TAR" ]; then
    log "images unchanged (fp ${FP:0:12}, ${IMG_COUNT} refs) -> skip export; current=$(basename "$CUR_TAR")"
  else
    IMGFILE="$DEST/dune-images-$UTC.tar"
    log "image set changed (prev ${PREV_FP:0:12} -> ${FP:0:12}); exporting ${IMG_COUNT} images (throttled)..."
    # ctr export takes multiple refs into one tar; ionice/nice keeps the 4.2GB
    # seabass content-store read courteous to the live game.
    remote "sudo ionice -c3 nice -n19 k3s ctr -n k8s.io images export - $REFLIST" 2>>"$LOG" > "$IMGFILE" \
      || fail "image export (k3s ctr) failed"
    IMGSIZE=$(stat -c%s "$IMGFILE")
    # Sanity: a full export of these 11 images is ~4.7GB; floor at 1GB catches a
    # truncated/partial stream. Then validate the archive is a well-formed
    # containerd/OCI export (blobs/ layout + index.json). List the tar ONCE into a
    # plain file and grep the FILE (never `tar | grep -q`: grep -q exits on first
    # match, SIGPIPEs the long-running tar, and under `set -o pipefail` that 141
    # surfaces as a false failure -- the exact trap the DB/k8s checks avoid).
    [ "$IMGSIZE" -ge 1000000000 ] || fail "image tar suspiciously small ($IMGSIZE bytes < 1GB)"
    IMGLIST=$(mktemp)
    tar -tf "$IMGFILE" 2>>"$LOG" > "$IMGLIST" || { rm -f "$IMGLIST"; fail "image tar unreadable (tar -tf failed)"; }
    [ -s "$IMGLIST" ] || { rm -f "$IMGLIST"; fail "image tar listing empty"; }
    grep -q 'blobs/sha256/' "$IMGLIST" || { rm -f "$IMGLIST"; fail "image tar missing blobs/ layout (corrupt export)"; }
    grep -q 'index.json'    "$IMGLIST" || log "WARN: image tar missing index.json (unexpected ctr format)"
    IMG_ENTRIES=$(wc -l < "$IMGLIST")
    rm -f "$IMGLIST"
    log "image tar validated: ${IMG_ENTRIES} archive entries, blobs+index present"
    printf '%s' "$FP" > "$IMGFP"
    ln -sfn "$(basename "$IMGFILE")" "$DEST/dune-images-latest.tar"
    log "image export OK: $(basename "$IMGFILE") ${IMGSIZE}B (${IMG_COUNT} images, fp ${FP:0:12})"
    # Image tars are big and change rarely (per Funcom build) -> rotate by COUNT,
    # not by age (mtime rotation would delete a stable-build's only copy). Keep
    # the newest 1 tar on <web-host> (owner 2026-09-28: older builds live on the offsite copy via the nightly restic). List
    # real files only (-type f) so the latest-symlink is never a deletion target.
    find "$DEST" -maxdepth 1 -type f -name 'dune-images-*.tar' -printf '%T@\t%p\n' 2>/dev/null \
      | sort -rn | tail -n +2 | cut -f2- | while read -r old; do
      rm -f "$old" 2>>"$LOG" && log "rotated out old image tar: $(basename "$old")"
    done
  fi
fi

# --- 4. Rotation + latest symlinks ------------------------------------------
find "$DEST" -maxdepth 1 -name 'dune-db-*.sql.gz'      -mtime +"$KEEP_DAYS" -delete 2>>"$LOG" || true
find "$DEST" -maxdepth 1 -name 'dune-config-*.tar.gz'  -mtime +"$KEEP_DAYS" -delete 2>>"$LOG" || true
find "$DEST" -maxdepth 1 -name 'dune-tooling-*.tar.gz' -mtime +"$KEEP_DAYS" -delete 2>>"$LOG" || true
find "$DEST" -maxdepth 1 -name 'dune-k8s-*.tar.gz'     -mtime +"$KEEP_DAYS" -delete 2>>"$LOG" || true
ln -sf "$(basename "$DBFILE")"   "$DEST/dune-db-latest.sql.gz"
ln -sf "$(basename "$CFGFILE")"  "$DEST/dune-config-latest.tar.gz"
ln -sf "$(basename "$TOOLFILE")" "$DEST/dune-tooling-latest.tar.gz"
ln -sf "$(basename "$K8SFILE")" "$DEST/dune-k8s-latest.tar.gz"

CFGSIZE=$(stat -c%s "$CFGFILE")
IMG_CUR=$(basename "$(readlink -f "$DEST/dune-images-latest.tar" 2>/dev/null)" 2>/dev/null || echo none)
log "OK: db=$(basename "$DBFILE") ${SIZE}B; config=$(basename "$CFGFILE") ${CFGSIZE}B; tooling=$(basename "$TOOLFILE") ${TOOLSIZE}B; k8s=$(basename "$K8SFILE") ${K8SSIZE}B; images=${IMG_CUR}; keep=${KEEP_DAYS}d"
log "=== dune backup done ==="
