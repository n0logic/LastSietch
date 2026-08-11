#!/bin/bash
set -euo pipefail

source /opt/backup/vps-backup.env
export RESTIC_SFTP_COMMAND="ssh -i /root/.ssh/backup_ed25519 backup@backup-host -s sftp"

LOG="/var/log/vps-backup.log"
STAMP=$(date "+%Y-%m-%d %H:%M:%S")
HOST=$(hostname -f)
mkdir -p /opt/backups/famplan /opt/backups/n8n /opt/backups/openscores

log() { echo "[$STAMP] $1" >> "$LOG"; }

fail() {
  log "FAILED: $1"
  {
    printf 'From: vps-backup@%s\n' "$HOST"
    printf 'To: root@localhost\n'
    printf 'Subject: [VPS Backup FAILED] %s\n' "$1"
    printf 'Content-Type: text/plain; charset=UTF-8\n'
    printf '\n'
    printf 'VPS backup failed on %s at %s\n\n' "$HOST" "$STAMP"
    printf 'Error: %s\n\n' "$1"
    printf 'Last 40 log lines:\n'
    tail -n 40 "$LOG"
  } | msmtp -t
  exit 1
}

STATE_DIR=/opt/backup/state
mkdir -p "$STATE_DIR"

# Non-fatal alert: emails WITHOUT aborting. For steps that must not stop a good
# backup but must not rot silently either.
alert() {
  log "ALERT: $1"
  {
    printf 'From: vps-backup@%s\n' "$HOST"
    printf 'To: root@localhost\n'
    printf 'Subject: [VPS Backup] %s\n' "$1"
    printf 'Content-Type: text/plain; charset=UTF-8\n'
    printf '\n'
    printf '%s\n\n' "$2"
    printf 'Host: %s\nTime: %s\n\n' "$HOST" "$STAMP"
    printf 'Last 30 log lines:\n'
    tail -n 30 "$LOG"
  } | msmtp -t || log "WARN: alert email failed"
}

# Consecutive-failure tracking for the non-fatal steps.
#
# WHY THIS EXISTS. restic prune and restic check failed on a stale lock EVERY
# night from 2026-07-11 to 2026-08-04 -- 25 consecutive runs -- and only ever
# logged "WARN:". Nothing was emailed, so repo integrity went unverified for
# almost a month and nothing was pruned. A step that can fail forever without
# telling anyone is not monitored, it is decorative.
#
# Deliberately NOT fatal: a failed prune must not throw away a good backup. The
# alert fires on the ALERT_STREAK-th consecutive failure and on every run after
# that until it succeeds, plus once more on recovery so the thread closes.
ALERT_STREAK=3
streak_record() {   # $1=name  $2=ok|fail  $3=human label
  local f="$STATE_DIR/$1.failures" n=0
  [ -f "$f" ] && n=$(cat "$f" 2>/dev/null || echo 0)
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  if [ "$2" = "ok" ]; then
    if [ "$n" -ge "$ALERT_STREAK" ]; then
      alert "$3 recovered after $n failed runs" \
        "$3 succeeded again after failing $n consecutive runs."
    fi
    echo 0 > "$f"
  else
    n=$((n + 1))
    echo "$n" > "$f"
    log "WARN: $3 failed ($n consecutive)"
    if [ "$n" -ge "$ALERT_STREAK" ]; then
      alert "$3 has failed $n runs in a row" \
        "$3 has now failed $n consecutive backup runs.

The backup itself still succeeded, so data IS being archived. But this step is
not running, which means the repository is not being verified or pruned.

Most common cause: a stale restic lock left by a killed process. Check with:
  restic snapshots     (reports the lock and its age)
  restic unlock        (safe when no restic process is running)"
    fi
  fi
}

exec 200>/var/run/vps-backup.lock
flock -n 200 || { log "Another backup is running, skipping"; exit 0; }

log "=== Backup starting ==="

# Probe backup host SFTP readiness directly instead of tailscale ping.
# tailscale ping answers at L3 even while SFTP/SSH is asleep under DSM
# power-save, so we need to actually touch port 22 and see the SSH banner.
# Cold-storage wake can take 60-120s, so we allow up to 8 attempts x 30s.
BACKUP_TS_IP="${BACKUP_HOST_IP:-100.64.0.2}"
reachable=0
for attempt in 1 2 3 4 5 6 7 8; do
  if timeout 30 bash -c "exec 3<>/dev/tcp/${BACKUP_TS_IP}/22 && read -t 10 -u 3 banner && [[ \$banner == SSH-* ]]" 2>/dev/null; then
    reachable=1
    log "backup host SFTP reachable on attempt $attempt"
    break
  fi
  log "SFTP probe attempt $attempt failed, retrying in 30s..."
  sleep 30
done
[ "$reachable" = "1" ] || fail "Cannot reach backup host SFTP after 8 attempts (4 minutes)"

log "Dumping databases..."
# FamPlan is handled by sqlite-snapshot.py below, NOT by docker cp. famplan.db is
# in WAL mode and docker cp of the main file alone leaves the WAL behind, which
# is the same stale-restore bug documented there.

# OpenScores SQLite (no MySQL on new server)
docker cp openscores-api-1:/app/data/openscores.db /opt/backups/openscores/openscores.db 2>/dev/null || log "WARN: OpenScores dump failed"

# n8n PostgreSQL
docker exec incomeops-postgres-1 pg_dump -U n8n n8n > /opt/backups/n8n/n8n.sql 2>/dev/null || log "WARN: n8n PostgreSQL dump failed"

# Export crontab
crontab -l > /opt/backups/crontab.txt 2>/dev/null

# --- Stalwart mail: consistent backup (RocksDB single-process lock => brief stop) ---
# Mirror the two bind-mount dirs to /opt/backups/stalwart; the restic /opt/backups line
# below sweeps them to the backup host. ~10-15s mail blip; SMTP senders auto-retry.
log "Backing up Stalwart mail store (brief stop)..."
SW_BAK="/opt/backups/stalwart"
mkdir -p "$SW_BAK/stalwart-data" "$SW_BAK/stalwart-mail-data"
if docker stop stalwart-mail >/dev/null 2>&1; then
  rsync -a --delete /opt/stalwart-data/      "$SW_BAK/stalwart-data/"      2>>"$LOG" || log "WARN: stalwart data rsync failed"
  rsync -a --delete /opt/stalwart-mail-data/ "$SW_BAK/stalwart-mail-data/" 2>>"$LOG" || log "WARN: stalwart certs rsync failed"
  sw_up=0
  for i in 1 2 3; do docker start stalwart-mail >/dev/null 2>&1 && { sw_up=1; break; }; sleep 5; done
  [ "$sw_up" = "1" ] && log "Stalwart store backed up + restarted" || fail "CRITICAL: stalwart-mail did NOT restart after backup"
else
  log "WARN: docker stop stalwart-mail failed; skipped mail store backup (mail still running)"
fi

# --- consistent SQLite snapshots (MUST run before restic) --------------------
# restic excludes *.sqlite-wal/-shm, which is right, but in WAL mode the main
# file is stale until a checkpoint -- so without this restic archived databases
# frozen at their last checkpoint. Measured 2026-08-04: support.sqlite'''s main
# file was 3 days old while a 4.1 MB WAL held the real data, and a restore would
# have silently lost 6 tickets. VACUUM INTO writes a consistent single file even
# while the database is being written.
# Fatal on purpose: archiving a stale snapshot while logging OK is the bug.
log "Snapshotting SQLite databases..."
/opt/backup/sqlite-snapshot.py >> "$LOG" 2>&1 \
  || fail "SQLite snapshot failed - refusing to archive stale databases (see $LOG)"

log "Running restic backup..."

set +e
restic -o sftp.command="$RESTIC_SFTP_COMMAND" backup \
  /opt/famplan/docker-compose.yml \
  /opt/famplan/.env \
  /opt/famplan/server/.env \
  /opt/openscores/docker-compose.yml \
  /opt/openscores/.env \
  /opt/incomeops/ \
  /opt/stalwart/ \
  /opt/jtc-bot/ \
  /opt/lastsietch/ \
  /opt/lastsietch-admin/ \
  /opt/lastsietch-relay/ \
  /opt/cielago/ \
  /opt/backup/ \
  /opt/backups/ \
  /etc/caddy/Caddyfile \
  /etc/claude-automation/ \
  /etc/msmtprc \
  /etc/cron.d/ \
  /root/.ssh/ \
  /root/.namecheap-api.env \
  /usr/local/bin/claude-dispatch.sh \
  /usr/local/bin/claude-report-email.sh \
  /usr/local/bin/receipt-scanner.sh \
  /var/www/ \
  --exclude="*.pyc" \
  --exclude="__pycache__" \
  --exclude="node_modules" \
  --exclude="venv" \
  --exclude=".git" \
  --exclude="*.sqlite-shm" \
  --exclude="*.sqlite-wal" \
  --exclude="*-journal" \
  >> "$LOG" 2>&1
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
  log "restic backup OK"
elif [ "$rc" -eq 3 ]; then
  log "WARN: restic exit 3 (some source files unreadable, e.g. transient SQLite WAL/SHM); snapshot saved, continuing to prune/check"
else
  fail "restic backup failed (exit $rc)"
fi

log "Pruning old snapshots..."
restic -o sftp.command="$RESTIC_SFTP_COMMAND" forget \
  --keep-last 4 \
  --keep-weekly 4 \
  --keep-monthly 6 \
  --prune \
  >> "$LOG" 2>&1 && streak_record prune ok "restic prune" \
                 || streak_record prune fail "restic prune"

if restic -o sftp.command="$RESTIC_SFTP_COMMAND" check >> "$LOG" 2>&1; then
  streak_record check ok "restic check"
else
  streak_record check fail "restic check"
fi

log "=== Backup completed successfully ==="
