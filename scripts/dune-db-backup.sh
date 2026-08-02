#!/usr/bin/env bash
# Daily Dune game-server backup (web-host PULL model).
#
# Runs ON the web host (systemd dune-db-backup.timer, 02:30 UTC daily). SSHes to
# <game-host> and:
#   1. pg_dump the live `dune` Postgres DB (streamed plain SQL -> gzip locally)
#   2. snapshot UserGame.ini / UserEngine.ini + the igwbg/igwbgd CRs (tar.gz)
# Output lands in /opt/backups/dune, which is ALREADY in the restic to offsite storage set
# (vps-backup.sh), so each backup is swept offsite on the next restic run.
#
# Strictly read-only against the game: pg_dump + kubectl get + cat. No DB writes,
# no pod restart. Safe to run while players are online.
#
# Restore (DB):      gunzip -c dune-db-<TS>.sql.gz | (the dq.sh psql path)
# Restore (config):  tar -xzf dune-config-<TS>.tar.gz
# Restore (tooling): tar -xzf dune-tooling-<TS>.tar.gz -C /   (scripts/units/token/telemetry DB)
set -uo pipefail

NS="${DUNE_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
DBPOD=sh-<your-hostid>-<random>-db-dbdepl-sts-0
BG="${DUNE_BG:-sh-<your-hostid>-<random>}"
PVC_INI="/var/lib/rancher/k3s/storage/pvc-b505f204-1724-4521-ba62-71b3bdb544ec_${NS}_${BG}-pvc/Saved/UserSettings"
SSH_HOST="${SSH_HOST:-<game-host>}"     # ssh alias or host for the game box
DEST="${DEST:-/opt/backups/dune}"
KEEP_DAYS="${KEEP_DAYS:-14}"
LOG="${LOG:-/var/log/dune-db-backup.log}"
MIN_DB_BYTES="${MIN_DB_BYTES:-500000}"   # healthy gz dump ~2.5MB; below 500KB = suspect
MAILTO="${MAILTO:-}"   # set to receive failure mail

UTC=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$DEST"

log(){ printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$1" | tee -a "$LOG" >&2; }
fail(){
  log "FAIL: $1"
  if command -v sendmail >/dev/null 2>&1; then
    { printf 'From: dune-backup@localhost\nSubject: [Dune Backup FAILED] %s\n\n%s\n' \
        "$UTC" "$1"; } | sendmail "$MAILTO" 2>/dev/null || true
  fi
  exit 1
}
remote(){ ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" "$@"; }

log "=== dune backup start ($UTC) ==="

# --- 1. DB dump: stream plain SQL from the pod, gzip on the web host -----------
DBFILE="$DEST/dune-db-$UTC.sql.gz"
remote "sudo kubectl exec -n $NS $DBPOD -- sh -c 'PGPASSWORD=\$POSTGRES_PASSWORD pg_dump -h localhost -p 15432 -U postgres -d dune'" \
  2>>"$LOG" | gzip > "$DBFILE"
rc=("${PIPESTATUS[@]}")
[ "${rc[0]}" -eq 0 ] || fail "pg_dump/ssh exited ${rc[0]}"
[ "${rc[1]}" -eq 0 ] || fail "gzip exited ${rc[1]}"

# --- 2. DB integrity gates ---------------------------------------------------
# Decompress ONCE to a temp file and run all content checks against the file.
# (Piping `gunzip -c | grep -q` would SIGPIPE gunzip on early match and, under
# `set -o pipefail`, falsely report failure.)
gzip -t "$DBFILE" 2>>"$LOG" || fail "gzip integrity test failed on $DBFILE"
SIZE=$(stat -c%s "$DBFILE")
[ "$SIZE" -ge "$MIN_DB_BYTES" ] || fail "dump suspiciously small ($SIZE bytes < $MIN_DB_BYTES)"
PLAIN=$(mktemp "$DEST/.verify.XXXXXX")
trap 'rm -f "$PLAIN"' EXIT
gunzip -c "$DBFILE" > "$PLAIN" 2>>"$LOG" || fail "gunzip decompress failed"
tail -5 "$PLAIN" | grep -q "PostgreSQL database dump complete" \
  || fail "dump missing completion marker (truncated?)"
# Spot-check core game + custom tables (pg_dump emits schema-qualified DDL).
grep -qE "^CREATE TABLE dune\.encrypted_accounts " "$PLAIN" \
  || fail "expected table dune.encrypted_accounts missing from dump"
grep -qE "^CREATE TABLE dune\.base_backups " "$PLAIN" \
  || fail "expected table dune.base_backups missing from dump"
grep -qE "^CREATE TABLE lsadmin\.bans " "$PLAIN" \
  || log "WARN: lsadmin.bans not found in dump (ok if schema predates moderation)"
# Table-count floor: live DB has ~170 tables; a healthy dump is well above 100.
TBL_COUNT=$(grep -cE "^CREATE TABLE " "$PLAIN")
rm -f "$PLAIN"; trap - EXIT
[ "$TBL_COUNT" -ge 100 ] || fail "dump has only $TBL_COUNT tables (<100); likely incomplete"
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
  /root/dune-*.py /root/dune-*.sh \
  /var/lib/lastsietch-telemetry/telemetry.db" \
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

# --- 4. Rotation + latest symlinks ------------------------------------------
find "$DEST" -maxdepth 1 -name 'dune-db-*.sql.gz'      -mtime +"$KEEP_DAYS" -delete 2>>"$LOG" || true
find "$DEST" -maxdepth 1 -name 'dune-config-*.tar.gz'  -mtime +"$KEEP_DAYS" -delete 2>>"$LOG" || true
find "$DEST" -maxdepth 1 -name 'dune-tooling-*.tar.gz' -mtime +"$KEEP_DAYS" -delete 2>>"$LOG" || true
ln -sf "$(basename "$DBFILE")"   "$DEST/dune-db-latest.sql.gz"
ln -sf "$(basename "$CFGFILE")"  "$DEST/dune-config-latest.tar.gz"
ln -sf "$(basename "$TOOLFILE")" "$DEST/dune-tooling-latest.tar.gz"

CFGSIZE=$(stat -c%s "$CFGFILE")
log "OK: db=$(basename "$DBFILE") ${SIZE}B; config=$(basename "$CFGFILE") ${CFGSIZE}B; tooling=$(basename "$TOOLFILE") ${TOOLSIZE}B; keep=${KEEP_DAYS}d"
log "=== dune backup done ==="
