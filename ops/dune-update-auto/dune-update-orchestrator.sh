#!/usr/bin/env bash
# =============================================================================
# Dune (Last Sietch) — UNATTENDED Funcom GA update orchestrator
# =============================================================================
# Built 2026-06-23 for the v1.4.10.0 window (Funcom fleet down 2026-06-24
# 07:00 UTC / 03:00 EDT). the operator is asleep; this drives the maintenance warnings
# + the update end-to-end with owner DMs, reusing the same notification
# patterns and the DUNE-UPDATE-PROCEDURE.md runbook (retargeted <game-host> ->
# <game-host>). A separate live Claude session supervises and can intervene.
#
# FLOW:
#   1. Maintenance warnings anchored to TARGET_UTC (the announced window):
#        T-30m: Cielago Discord (#service-alerts) + in-game broadcast
#        T-15m: in-game broadcast
#        T-60s: in-game broadcast
#   2. From T, poll the Steam depot for appid 4754530 until the PUBLIC buildid
#      flips away from the installed build (the ONLY real go-signal; the depot
#      often flips during/just before the window, not on the clock).
#   3. PRE-FLIGHT gates: ls_* table ownership (auto-fix to dune), containerd
#      socket symlink, disk free, THEN back up + DROP the `lsadmin` schema (it
#      breaks Funcom's migration temp-replay; restored in step 9 after verify).
#   4. Snapshot the BG CR + record the rollback image tag.
#   5. `battlegroup update` -- applied EVEN IF PLAYERS ARE ONLINE (the operator's
#      explicit call 2026-06-23: once Funcom ships the client update, players
#      can't connect to an old-build server anyway; the update's rolling restart
#      is GRACEFUL = engine PreShutdown flush saves progression to DB). The
#      online count is logged + DM'd; the warnings give players time to log off.
#   6. Verify: BG healthy + DeclareBattlegroupUpdates firing (browser visible).
#   7. One auto-recovery: delete a stuck db-utils Error pod, re-verify.
#   8. RESTORE the lsadmin schema from the step-3 backup; re-assert dune
#      ownership (a superuser restore can leave objects postgres-owned, which
#      would re-break the NEXT migration).
#   9. DM the operator at every milestone + final report. Self-disarm.
#
# SAFETY:
#   * Go-signal from the depot, not the clock -> no premature no-op update.
#   * Graceful `battlegroup update` only (NEVER a SIGKILL pod-delete) -> the
#     progression-safe restart path even with players online.
#   * Idempotent ownership auto-fix + lsadmin backup/DROP are the only auto-
#     writes before the update; the DROP refuses to run without a VERIFIED dump
#     (6 tables + completion marker) so we can always restore.
#   * Verification failure after recovery: STOP + DM CRITICAL with a ready
#     rollback command; does NOT auto-rollback (no 2nd unattended restart).
#   * DEADLINE_UTC: stop + DM "not updated" rather than act late/ambiguously.
#   * Single flock; idempotent done-marker.
#
# DISARM: stop/disable the timer, `touch $DONE_MARKER`, or DUNE_UPD_ENABLE=0.
# =============================================================================

set -u

# ---- config (override via env / the systemd unit) --------------------------
ENABLE="${DUNE_UPD_ENABLE:-1}"
GAME_HOST="${DUNE_UPD_GAME_HOST:-<game-host>}"
NS="${DUNE_UPD_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
APPID="${DUNE_UPD_APPID:-4754530}"
CURRENT_BUILDID="${DUNE_UPD_CURRENT_BUILDID:-24204075}"   # flip away from this = go (Jul 23 1.4.10.4 window; 1.4.10.3 baseline, installed==public verified 2026-07-22)
BG_BIN="${DUNE_UPD_BG_BIN:-/home/dune/.dune/bin/battlegroup}"
DQ="${DUNE_UPD_DQ:-/root/dq.sh}"
DL_DIR="${DUNE_UPD_DL_DIR:-/home/dune/.dune/download}"
MIN_DISK_GB="${DUNE_UPD_MIN_DISK_GB:-10}"
BCAST="${DUNE_UPD_BCAST:-/opt/lastsietch-rmq-bridge/dune-service-broadcast.py}"
LSADMIN_ENABLE="${DUNE_UPD_LSADMIN_ENABLE:-1}"   # back up + DROP lsadmin pre-update, restore post-verify
FULLBK_ENABLE="${DUNE_UPD_FULLBK_ENABLE:-1}"       # full dune DB dump (gzip) as a HARD GATE before any change
HOLD_MINUTES="${DUNE_UPD_HOLD_MINUTES:-5}"         # abort window after the flip+backup, before the destructive update
PW_MAINT="${DUNE_UPD_PW_MAINT:-/opt/lastsietch-pod-watcher/maint-window.sh}"   # pod-watcher crash-alert mute during the rolling restart
BGW_PAUSE="${DUNE_UPD_BGW_PAUSE:-/opt/lastsietch-bg-watchdog/pause.sh}"        # bg-watchdog auto-recovery pause during the update
MUTE_MIN="${DUNE_UPD_MUTE_MIN:-90}"                                     # mute/pause duration around the update (self-caps at 360)
DBPOD="${DUNE_UPD_DBPOD:-}"                          # postgres pod; autodiscovered (*-db-dbdepl-sts-0) if empty
SNAP_REMOTE="${DUNE_UPD_SNAP_REMOTE:-/root/dune-update-snap}"   # lsadmin backup dir ON the game host

WORKDIR="${DUNE_UPD_WORKDIR:-$HOME/dune-update-auto}"
LOGDIR="$WORKDIR/logs"
STATE_FILE="$WORKDIR/state.json"
DONE_MARKER="$WORKDIR/done.marker"
HOLD_MARKER="$WORKDIR/HOLD"
LOCK_FILE="$WORKDIR/.lock"
SNAP_DIR="$WORKDIR/snapshots"

# Timing (UTC). TARGET = announced window start; warnings anchor to it.
TARGET_UTC="${DUNE_UPD_TARGET_UTC:-2026-07-23 07:00:00}"      # 03:00 EDT (Funcom HOTFIX 1.4.10.4 window open; STAY ONLINE, poll for the flip)
DEADLINE_UTC="${DUNE_UPD_DEADLINE_UTC:-2026-07-23 15:00:00}"  # 11:00 EDT; DM+stop if the build never reaches us
POLL_INTERVAL="${DUNE_UPD_POLL_INTERVAL:-120}"     # depot poll cadence after T (s)
VERIFY_TIMEOUT="${DUNE_UPD_VERIFY_TIMEOUT:-600}"
VERIFY_INTERVAL="${DUNE_UPD_VERIFY_INTERVAL:-20}"

# Owner DM + Cielago channel (via the Discord bot venv on the web host).
DM_ENABLE="${DUNE_UPD_DM_ENABLE:-1}"
BCAST_ENABLE="${DUNE_UPD_BCAST_ENABLE:-1}"   # in-game broadcasts
CIELAGO_ENABLE="${DUNE_UPD_CIELAGO_ENABLE:-1}"  # community Discord posts
DM_SSH_HOST="${CIELAGO_SSH_HOST:-the web host}"
CIELAGO_DIR="${CIELAGO_DIR:-/opt/cielago}"
CIELAGO_VENV_PY="${CIELAGO_VENV_PY:-$CIELAGO_DIR/venv/bin/python}"
CIELAGO_ENV="${CIELAGO_ENV:-$CIELAGO_DIR/.env}"
OWNER_ID="${DUNE_UPD_OWNER_ID:-215146359479730176}"
SERVICE_ALERTS_CH="${DUNE_UPD_SERVICE_ALERTS_CH:-}"   # Discord channel id, required if alerts are enabled

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)
mkdir -p "$LOGDIR" "$SNAP_DIR"
RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOGDIR/run-$RUN_TS.log"

# ---- helpers ----------------------------------------------------------------
log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$LOG" >&2; }
now_s() { date -u +%s; }
ts_s()  { date -u -d "$1" +%s; }
state() { printf '{"ts":"%s","phase":"%s","detail":"%s"}\n' "$(date -u +%FT%TZ)" "$1" "${2//\"/\'}" > "$STATE_FILE"; }
gh() { ssh "${SSH_OPTS[@]}" "$GAME_HOST" "$@"; }

# sleep until an absolute UTC time (no-op if already past)
sleep_until() {  # sleep_until <epoch>
  local target="$1" rem
  while :; do
    rem=$(( target - $(now_s) ))
    [ "$rem" -le 0 ] && return 0
    [ "$rem" -gt 300 ] && rem=300   # wake every 5m so a kill/disarm lands fast
    sleep "$rem"
    [ -f "$DONE_MARKER" ] && return 1
  done
}

# Cielago: DM the owner OR post to a channel. mode=dm uses OWNER_ID; mode=chan uses $2.
cielago_send() {  # cielago_send <dm|chan> <owner_id_or_channel_id> <body>
  local mode="$1" target="$2" body="$3"
  if [ "$mode" = "chan" ]; then [ "$CIELAGO_ENABLE" = "1" ] || { log "cielago chan disabled; skip"; return 0; }
  else [ "$DM_ENABLE" = "1" ] || { log "DM disabled; would send: $body"; return 0; }; fi
  local body_b64; body_b64="$(printf '%s' "$body" | base64 -w0)"
  ssh "${SSH_OPTS[@]}" "$DM_SSH_HOST" \
    "MODE='$mode' TARGET='$target' CIELAGO_ENV='$CIELAGO_ENV' DM_BODY_B64='$body_b64' '$CIELAGO_VENV_PY' - <<'PY'
import os, base64
import discord
from dotenv import load_dotenv
load_dotenv(os.environ[\"CIELAGO_ENV\"])
TOKEN = os.environ[\"DISCORD_BOT_TOKEN\"]
MODE = os.environ[\"MODE\"]; TARGET = int(os.environ[\"TARGET\"])
BODY = base64.b64decode(os.environ[\"DM_BODY_B64\"]).decode(\"utf-8\", \"replace\")
intents = discord.Intents.default()
client = discord.Client(intents=intents)
@client.event
async def on_ready():
    try:
        if MODE == \"chan\":
            dest = client.get_channel(TARGET) or await client.fetch_channel(TARGET)
        else:
            dest = client.get_user(TARGET) or await client.fetch_user(TARGET)
        for i in range(0, len(BODY), 1900):
            await dest.send(BODY[i:i+1900])
        print(\"sent\", MODE, TARGET)
    except Exception as e:
        print(\"send failed:\", e)
    finally:
        await client.close()
client.run(TOKEN)
PY" >>"$LOG" 2>&1 || log "WARN: cielago $mode send failed (non-fatal)."
}
dm_owner()    { cielago_send dm   "$OWNER_ID" "$1"; }
chan_alerts() { cielago_send chan "$SERVICE_ALERTS_CH" "$1"; }

# in-game broadcast (proven Generic ServiceBroadcast on <game-host>)
ingame() {  # ingame <title> <message> <duration_s>
  [ "$BCAST_ENABLE" = "1" ] || { log "in-game bcast disabled; would send: $2"; return 0; }
  local t="$1" m="$2" d="$3"
  gh "python3 $BCAST generic --title $(printf %q "$t") --message $(printf %q "$m") --duration $d --send" >>"$LOG" 2>&1 \
    && log "in-game broadcast sent: [$t] $m (${d}s)" \
    || log "WARN: in-game broadcast failed: $m"
}

# Silence the pod-watcher's crash alerts + pause the BG auto-recovery watchdog for
# the update: a `battlegroup update` rolling restart looks like pod crashes and
# would false-alarm the operator (asleep), and the watchdog must not fight the updater.
# Both self-cap (6h) so a failure path still self-heals. Non-fatal.
watchers_mute() {
  gh "$PW_MAINT on $MUTE_MIN"  >>"$LOG" 2>&1 && log "pod-watcher muted ${MUTE_MIN}m"   || log "WARN: pod-watcher mute failed (non-fatal)"
  gh "$BGW_PAUSE on $MUTE_MIN" >>"$LOG" 2>&1 && log "bg-watchdog paused ${MUTE_MIN}m"  || log "WARN: bg-watchdog pause failed (non-fatal)"
}
watchers_unmute() {
  gh "$PW_MAINT off"  >>"$LOG" 2>&1 && log "pod-watcher unmuted"  || log "WARN: pod-watcher unmute failed (non-fatal)"
  gh "$BGW_PAUSE off" >>"$LOG" 2>&1 && log "bg-watchdog resumed"  || log "WARN: bg-watchdog resume failed (non-fatal)"
}

die_dm() { log "ABORT: $1"; state "$1" "$2"; dm_owner "$2"; touch "$DONE_MARKER"; exit 1; }

depot_public_buildid() {
  gh "sudo -u dune bash -lc 'steamcmd +login anonymous +app_info_update 1 +app_info_print $APPID +quit 2>/dev/null'" 2>/dev/null \
    | awk '/"public"/{p=1} p&&/"buildid"/{gsub(/[^0-9]/,"",$2); print $2; exit}'
}
players_online() {
  # Exclude deleted-character tombstones: a char deleted during creation can stay
  # stuck online_status='Online' in encrypted_player_state (no logoff event ever
  # cleared it), which would inflate the count. character_state IS DISTINCT FROM
  # 'Deleted' keeps a hypothetical NULL state counted (conservative).
  gh "$DQ -t -c \"SELECT count(*) FROM dune.encrypted_player_state WHERE online_status='Online' AND character_state IS DISTINCT FROM 'Deleted';\"" 2>/dev/null | tr -dc '0-9'
}

# ---- lsadmin schema lifecycle around the migration ------------------------
# Funcom's pre-update migration replays the dune DB into a TEMP schema and does
# NOT recreate our separate `lsadmin` schema -> `CREATE TABLE lsadmin.*` aborts
# the replay (BG stuck Suspended). So back it up + DROP before the update and
# restore it after verify. The DROP refuses to run without a VERIFIED dump.
# (a custom schema left in place aborts Funcom's migration replay; first hit
# 2026-06-24 on the 1.4.10.0 update.)
LSADMIN_BK=""   # set to the on-host backup path once taken (drives the restore)

db_pod() {  # echo the postgres statefulset pod name (autodiscover if unset)
  [ -n "$DBPOD" ] && { echo "$DBPOD"; return 0; }
  gh "kubectl get pods -n $NS --no-headers -o custom-columns=:metadata.name 2>/dev/null | grep -E 'db-dbdepl-sts-0\$' | head -1" 2>/dev/null | tr -d '[:space:]'
}
lsadmin_exists() {
  [ "$(gh "$DQ -t -c \"SELECT count(*) FROM information_schema.schemata WHERE schema_name='lsadmin';\"" 2>/dev/null | tr -dc '0-9')" = "1" ]
}

lsadmin_backup_and_drop() {  # gate: aborts the run (die_dm) on any failure before the update
  [ "$LSADMIN_ENABLE" = "1" ] || { log "lsadmin handling disabled -> skip"; return 0; }
  if ! lsadmin_exists; then log "no lsadmin schema present -> nothing to back up/drop"; return 0; fi
  local pod; pod="$(db_pod)"
  [ -n "$pod" ] || die_dm "lsadmin-nopod" "🚨 Gate FAILED: could not find the DB pod (…-db-dbdepl-sts-0) to back up lsadmin. Did NOT update (lsadmin would abort the migration)."
  local bk="$SNAP_REMOTE/lsadmin-backup-$RUN_TS.sql"
  log "lsadmin: pg_dump -n lsadmin via $pod -> $bk"
  gh "mkdir -p $SNAP_REMOTE && kubectl exec -n $NS $pod -- sh -c 'PGPASSWORD=\$POSTGRES_PASSWORD pg_dump -h localhost -p 15432 -U postgres -d dune -n lsadmin' > $bk" >>"$LOG" 2>&1 \
    || die_dm "lsadmin-dump" "🚨 Gate FAILED: lsadmin pg_dump errored. Did NOT update (won't DROP lsadmin without a good backup). Check $GAME_HOST:$bk."
  # Verify the dump BEFORE we trust it enough to DROP: 6 tables + clean tail marker.
  local ntab marker
  ntab="$(gh "grep -cE '^CREATE TABLE lsadmin\\.' $bk 2>/dev/null" 2>/dev/null | tr -dc '0-9')"
  marker="$(gh "tail -5 $bk 2>/dev/null | grep -c 'PostgreSQL database dump complete'" 2>/dev/null | tr -dc '0-9')"
  if [ "${ntab:-0}" -lt 6 ] || [ "${marker:-0}" -lt 1 ]; then
    die_dm "lsadmin-badbackup" "🚨 Gate FAILED: lsadmin backup looks bad (tables=${ntab:-0}/6, complete-marker=${marker:-0}). Did NOT update (refuse to DROP without a verified backup). File: $GAME_HOST:$bk."
  fi
  LSADMIN_BK="$bk"
  log "lsadmin backup verified ($ntab tables) -> DROP SCHEMA lsadmin CASCADE"
  gh "$DQ -c \"DROP SCHEMA lsadmin CASCADE;\"" >>"$LOG" 2>&1 \
    || die_dm "lsadmin-drop" "🚨 lsadmin backup OK ($bk) but DROP SCHEMA failed. Did NOT update. Drop it by hand, then re-run."
  lsadmin_exists && die_dm "lsadmin-drop" "🚨 lsadmin still present after DROP. Did NOT update. Backup: $GAME_HOST:$bk."
  state "lsadmin-dropped" "backup=$bk tables=$ntab"
  dm_owner "🗄️ lsadmin backed up ($ntab tables -> $GAME_HOST:$bk) and DROPPED so the Funcom migration replays clean. Restoring after the build verifies."
}

lsadmin_restore() {  # echoes a human-readable status line; never aborts (server is already up)
  [ -n "$LSADMIN_BK" ] || { echo "lsadmin: nothing to restore (none was dropped this run)"; return 0; }
  local pod; pod="$(db_pod)"
  [ -n "$pod" ] || { echo "⚠️ lsadmin NOT restored: DB pod not found. Restore by hand from $GAME_HOST:$LSADMIN_BK."; return 1; }
  log "lsadmin: restoring from $LSADMIN_BK via $pod"
  if ! gh "kubectl exec -i -n $NS $pod -- sh -c 'PGPASSWORD=\$POSTGRES_PASSWORD psql -h localhost -p 15432 -U postgres -d dune -v ON_ERROR_STOP=1 -q' < $LSADMIN_BK" >>"$LOG" 2>&1; then
    echo "⚠️ lsadmin restore psql FAILED. By hand: kubectl exec -i -n $NS $pod -- sh -c 'PGPASSWORD=\\\$POSTGRES_PASSWORD psql -h localhost -p 15432 -U postgres -d dune' < $GAME_HOST:$LSADMIN_BK"; return 1
  fi
  # Re-assert dune ownership (a superuser restore can leave objects postgres-owned
  # -> would re-break the NEXT migration). for-loop, NOT while-read (ssh eats stdin).
  gh "$DQ -c \"ALTER SCHEMA lsadmin OWNER TO dune;\"" >>"$LOG" 2>&1
  local htabs t; htabs="$(gh "$DQ -t -c \"SELECT tablename FROM pg_tables WHERE schemaname='lsadmin';\"" 2>/dev/null | tr -d ' ' | grep -v '^$')"
  for t in $htabs; do gh "$DQ -c \"ALTER TABLE lsadmin.$t OWNER TO dune;\"" >>"$LOG" 2>&1; done
  local ntab badown presets
  ntab="$(gh "$DQ -t -c \"SELECT count(*) FROM pg_tables WHERE schemaname='lsadmin';\"" 2>/dev/null | tr -dc '0-9')"
  badown="$(gh "$DQ -t -c \"SELECT count(*) FROM pg_tables WHERE schemaname='lsadmin' AND tableowner<>'dune';\"" 2>/dev/null | tr -dc '0-9')"
  presets="$(gh "$DQ -t -c \"SELECT count(*) FROM lsadmin.grant_presets;\"" 2>/dev/null | tr -dc '0-9')"
  if [ "${ntab:-0}" -ge 6 ] && [ "${badown:-1}" = "0" ]; then
    echo "lsadmin restored OK (${ntab} tables, owner dune, grant_presets=${presets:-?})"; return 0
  fi
  echo "⚠️ lsadmin restore INCOMPLETE (tables=${ntab:-0}/6, non-dune-owned=${badown:-?}). Verify by hand; backup at $GAME_HOST:$LSADMIN_BK."; return 1
}

# ---- full dune DB backup (HARD GATE before any change) ----------------------
# pg_dump the ENTIRE dune database (all schemas incl lsadmin), gzip on the host,
# and verify it before we touch anything. This is the pre-update restore point
# the operator asked for; taken while the server is still up (pg_dump is a
# transactionally consistent snapshot, safe with players online). Aborts the run
# on any failure so we NEVER update without a good full backup. Gates mirror
# scripts/dune-db-backup.sh (gzip test + completion marker + >=100 tables).
FULLBK=""   # set to the on-host full-DB backup path once taken
full_db_backup() {
  [ "$FULLBK_ENABLE" = "1" ] || { log "full DB backup disabled -> skip"; return 0; }
  local pod bk ntab marker sz
  pod="$(db_pod)"
  [ -n "$pod" ] || die_dm "fullbk-nopod" "🚨 Gate FAILED: DB pod (…-db-dbdepl-sts-0) not found for the full backup. Did NOT update."
  bk="$SNAP_REMOTE/dune-full-$RUN_TS.sql.gz"
  log "full DB backup: pg_dump dune via $pod -> $bk"
  gh "mkdir -p $SNAP_REMOTE && set -o pipefail; kubectl exec -n $NS $pod -- sh -c 'PGPASSWORD=\$POSTGRES_PASSWORD pg_dump -h localhost -p 15432 -U postgres -d dune' | gzip > $bk" >>"$LOG" 2>&1 \
    || die_dm "fullbk-dump" "🚨 Gate FAILED: full DB pg_dump/gzip errored. Did NOT update. Check $GAME_HOST:$bk."
  gh "gzip -t $bk" >>"$LOG" 2>&1 \
    || die_dm "fullbk-gzip" "🚨 Gate FAILED: full backup gzip integrity test failed ($GAME_HOST:$bk). Did NOT update."
  marker="$(gh "gunzip -c $bk 2>/dev/null | tail -5 | grep -c 'PostgreSQL database dump complete'" 2>/dev/null | tr -dc '0-9')"
  ntab="$(gh "gunzip -c $bk 2>/dev/null | grep -cE '^CREATE TABLE '" 2>/dev/null | tr -dc '0-9')"
  if [ "${marker:-0}" -lt 1 ] || [ "${ntab:-0}" -lt 100 ]; then
    die_dm "fullbk-bad" "🚨 Gate FAILED: full backup looks bad (tables=${ntab:-0} need>=100, complete-marker=${marker:-0}). Did NOT update. File: $GAME_HOST:$bk."
  fi
  FULLBK="$bk"
  sz="$(gh "stat -c%s $bk" 2>/dev/null | tr -dc '0-9')"
  state "fullbackup-done" "file=$bk tables=$ntab bytes=${sz:-?}"
  dm_owner "💾 Full dune DB backup taken BEFORE the update: $ntab tables, ${sz:-?} bytes -> $GAME_HOST:$bk. Pre-update restore point in place."
}

# ---- abort/hold window (the asleep-hybrid gate) -----------------------------
# After the flip + full backup, pause HOLD_MINUTES before the destructive update.
# Abort by creating $HOLD_MARKER (ssh <host> 'touch <marker>'); with no abort it
# auto-proceeds. Backup is already taken and the server is still up, so a hold
# leaves everything untouched.
hold_window() {
  [ "${HOLD_MINUTES:-0}" -gt 0 ] 2>/dev/null || { log "hold window disabled (HOLD_MINUTES=$HOLD_MINUTES)"; return 0; }
  rm -f "$HOLD_MARKER"
  state "hold" "waiting ${HOLD_MINUTES}m for abort"
  dm_owner "⏸️ HOLD: updating in ${HOLD_MINUTES} min. To ABORT run:  ssh $(hostname -s) 'touch $HOLD_MARKER'  (full backup already taken; server still UP on $CURRENT_BUILDID). No action = auto-proceed."
  local end=$(( $(now_s) + HOLD_MINUTES*60 ))
  while [ "$(now_s)" -lt "$end" ]; do
    if [ -f "$HOLD_MARKER" ]; then
      rm -f "$HOLD_MARKER"
      die_dm "held" "🛑 HELD by owner: update ABORTED before the destructive step. Server UNTOUCHED, still up on $CURRENT_BUILDID. Full backup at $GAME_HOST:${FULLBK:-<none>}. To re-arm: rm $DONE_MARKER then restart dune-update.service."
    fi
    sleep 15
  done
  log "hold window elapsed; proceeding with the update"
  dm_owner "▶️ Hold elapsed with no abort. Proceeding with the lsadmin-safe graceful update now."
}

# =============================================================================
main() {
  if [ "${1:-}" = "test-notify" ]; then
    log "test-notify: sending a DM to owner only (no channel/in-game)"
    dm_owner "🔧 Test from the Last Sietch update orchestrator on $(hostname -s). If you see this, the owner-DM path works. (test-notify; nothing else fired)"
    exit 0
  fi
  [ "$ENABLE" = "1" ] || { log "disabled no-op"; exit 0; }
  [ -f "$DONE_MARKER" ] && { log "done-marker present -> no-op"; exit 0; }
  exec 9>"$LOCK_FILE"; flock -n 9 || { log "locked -> exit"; exit 0; }

  local nowu deadu tT
  nowu="$(now_s)"; deadu="$(ts_s "$DEADLINE_UTC")"; tT="$(ts_s "$TARGET_UTC")"
  [ "$nowu" -gt "$deadu" ] && { log "past deadline -> no-op"; exit 0; }

  log "=== orchestrator armed (host=$GAME_HOST target=$TARGET_UTC deadline=$DEADLINE_UTC) ==="
  state "armed" "warnings pending"
  dm_owner "🛰️ Last Sietch update-watch ARMED on $(hostname -s) for Funcom 1.4.10.4. At T=$(date -u -d "$TARGET_UTC" '+%H:%M UTC') / 03:00 ET it posts a STAY-ONLINE window-open notice, then polls the Steam depot ($APPID, cur $CURRENT_BUILDID) and keeps the server UP until the buildid flips. On the flip: 60s in-game warning, full DB backup, a ${HOLD_MINUTES}-min HOLD window (touch $HOLD_MARKER to abort), then lsadmin-safe graceful update + verify + restore. DM at each step. Deadline $(date -u -d "$DEADLINE_UTC" '+%H:%M UTC')."

  # --- phase 1: window-open notice (STAY-ONLINE plan; no countdown-to-offline) ---
  # We do NOT take the server down at T. Post a heads-up that Funcom's window is
  # open, keep running, and go offline only when the buildid actually flips (phase 2).
  if sleep_until "$tT"; then
    log "window open (T): stay-online notice"
    chan_alerts "🜂 Funcom maintenance window is open. Last Sietch is staying online for now and will keep running until the actual server update releases (often near the end of the window). When it drops, watch for the 60 second in-game warning, then we go down briefly to back up and patch. Update your Steam client before reconnecting. We will post here when we go offline and when we are back."
    ingame "Funcom maintenance window" "Funcom's maintenance window is starting. Last Sietch is staying ONLINE and will keep running until the actual update reaches us. When it drops you get a 60 second warning, then a short downtime for the patch. You will need to update your client on Steam before reconnecting." 120
    state "window-open" "stay-online notice sent"
  fi

  # --- phase 2: poll for the build flip (from T) ---
  state "polling" "watching depot from T"
  dm_owner "⏳ T reached. Polling the depot for the new build now."
  local new_build=""
  while :; do
    [ "$(now_s)" -gt "$deadu" ] && die_dm "timeout-nobuild" \
      "⏰ Deadline reached; the new Dune build never went public on $APPID (still $CURRENT_BUILDID). Server NOT updated, untouched. Players were warned — you may want to post an 'update delayed' note. Handle supervised."
    local b; b="$(depot_public_buildid)"
    if [ -n "$b" ] && [ "$b" != "$CURRENT_BUILDID" ]; then new_build="$b"; log "BUILD FLIPPED -> $new_build"; break; fi
    log "poll: public=${b:-<none>} (no flip); sleep $POLL_INTERVAL"; sleep "$POLL_INTERVAL"
  done
  local pc; pc="$(players_online)"
  state "build-live" "new=$new_build players=$pc"
  dm_owner "📦 New build PUBLIC: $new_build (was $CURRENT_BUILDID). Players online now: ${pc:-?}. Sending the 60s logout warning, taking a full backup, then a ${HOLD_MINUTES}-min hold before the update."

  # --- flip: 60s in-game warning + going-offline post, then full backup + hold ---
  ingame "Update releasing: 60 seconds" "The update is here. Last Sietch goes offline in 60 seconds for a full backup and the patch. Log off now at a safe spot. Update your client on Steam, then reconnect once we are back." 60
  chan_alerts "🔧 The update just released. Taking Last Sietch offline now for a full backup and the patch. Back shortly. Please update your game client on Steam before you try to reconnect."
  sleep 60
  full_db_backup
  hold_window
  watchers_mute   # mute false crash alerts + pause BG auto-recovery for the rolling restart

  # --- phase 3: pre-flight gates ---
  local bad
  bad="$(gh "$DQ -t -c \"SELECT tablename FROM pg_tables WHERE schemaname='dune' AND tablename LIKE 'ls\\_%' AND tableowner<>'dune';\"" 2>/dev/null | tr -d ' ' | grep -v '^$')"
  if [ -n "$bad" ]; then
    log "fixing ls_* ownership: $bad"
    # for-loop (NOT while-read): ssh in a while-read consumes the loop's stdin -> only the first
    # table got altered (the 2026-06-24 abort). for-loop over word-split avoids that entirely.
    for t in $bad; do gh "$DQ -c \"ALTER TABLE dune.$t OWNER TO dune;\"" >>"$LOG" 2>&1; done
    bad="$(gh "$DQ -t -c \"SELECT tablename FROM pg_tables WHERE schemaname='dune' AND tablename LIKE 'ls\\_%' AND tableowner<>'dune';\"" 2>/dev/null | tr -d ' ' | grep -v '^$')"
    [ -n "$bad" ] && die_dm "gate-ownership" "🚨 Gate FAILED: ls_* tables not owned by dune after auto-fix ($bad). Funcom's pre-update pg_dump would abort. Did NOT update."
  fi
  gh "test -e /run/containerd/containerd.sock" 2>/dev/null || die_dm "gate-containerd" "🚨 Gate FAILED: containerd socket missing on $GAME_HOST. Did NOT update."
  local freegb; freegb="$(gh "df -BG --output=avail $DL_DIR 2>/dev/null | tail -1 | tr -dc '0-9'" 2>/dev/null)"
  [ -n "$freegb" ] && [ "$freegb" -lt "$MIN_DISK_GB" ] && die_dm "gate-disk" "🚨 Gate FAILED: only ${freegb}G free in $DL_DIR. Did NOT update."
  log "gates passed (disk ${freegb:-?}G)"

  # lsadmin schema would abort the migration -> back it up + DROP (gate: aborts on failure).
  lsadmin_backup_and_drop

  # --- phase 4: snapshot + rollback tag ---
  local snap="$SNAP_DIR/bg-cr-$RUN_TS.yaml" prev_img
  gh "kubectl get battlegroup -n $NS -o yaml" > "$snap" 2>>"$LOG" || log "WARN: BG CR snapshot failed"
  prev_img="$(grep -oE 'seabass-server:[0-9][0-9A-Za-z._-]+' "$snap" 2>/dev/null | sort -u | head -1)"
  log "snapshot=$snap prev_image=${prev_img:-<unknown>}"
  state "updating" "new=$new_build prev=${prev_img:-?}"

  # --- phase 5: the update (graceful; exit code unreliable -> verify state) ---
  local upd_log="$LOGDIR/battlegroup-update-$RUN_TS.log"
  gh "sudo -u dune $BG_BIN update" >"$upd_log" 2>&1
  log "battlegroup update done (rc=$? ignored per runbook) -> $upd_log"

  # --- phase 6/7: verify (+ one db-utils recovery) ---
  verify() {
    local end=$(( $(now_s) + VERIFY_TIMEOUT )) oks okd bgd st
    while [ "$(now_s)" -lt "$end" ]; do
      st="$(gh "sudo -u dune $BG_BIN status" 2>/dev/null)"
      echo "$st" | grep -qiE "Director.*(Healthy|2/2)" && oks=1 || oks=""
      bgd="$(gh "POD=\$(kubectl get pods -n $NS -l role=igw-battlegroup-director -o name 2>/dev/null | head -1); kubectl logs -n $NS \"\$POD\" --tail=400 2>/dev/null | grep -c Battlegroups_DeclareBattlegroupUpdates" 2>/dev/null | tr -dc '0-9')"
      [ "${bgd:-0}" -gt 0 ] && okd=1 || okd=""
      [ -n "$oks" ] && [ -n "$okd" ] && return 0
      log "verify: status=${oks:-0} declare=${bgd:-0}; retry $VERIFY_INTERVAL"; sleep "$VERIFY_INTERVAL"
    done
    return 1
  }
  if verify; then log "VERIFY GREEN (first pass)"; else
    log "verify failed; db-utils recovery"
    local errpod; errpod="$(gh "kubectl get pods -n $NS --no-headers 2>/dev/null | awk '/db-dbdepl-util/ && (/Error/||/CrashLoop/){print \$1}' | head -1" 2>/dev/null)"
    [ -n "$errpod" ] && { log "delete stuck db-utils pod $errpod"; gh "kubectl delete pod -n $NS $errpod" >>"$LOG" 2>&1; sleep 30; }
    verify || die_dm "verify-failed" "🚨 CRITICAL: build $new_build applied but verification FAILED after recovery. Server may be DOWN/INVISIBLE. NOT auto-rolled-back. Rollback supervised: repoint worldImage to ${prev_img:-PREV_TAG} (snapshot $snap; see DUNE-UPDATE-PROCEDURE.md). Update log: $upd_log${LSADMIN_BK:+ | NOTE: lsadmin was backed up to $GAME_HOST:$LSADMIN_BK and DROPPED; restore it after recovery (see the orchestrator lsadmin_restore steps).}"
    log "VERIFY GREEN (after recovery)"
  fi

  # --- phase 8: restore lsadmin (verify is green; never aborts the run) ---
  local ls_status; ls_status="$(lsadmin_restore)"; log "lsadmin: $ls_status"
  watchers_unmute   # verify is green: restore pod-watcher + BG auto-recovery to normal

  # --- success ---
  state "done-ok" "new=$new_build"; touch "$DONE_MARKER"
  ingame "The sietch is open" "The update is applied and Last Sietch is back. Bases and progression are safe. See you on the sand." 120
  chan_alerts "✅ Last Sietch is back and updated. Bases and progression are intact. Thank you for your patience, Sleepers."
  dm_owner "✅ Server updated to $new_build, verified (Director healthy + browser-visible). ${pc:-?} players were online at update time (graceful restart). 🗄️ $ls_status. 'Back online' posted. Run your own post-update checklist now: re-apply any binary or config customisations that an update reverts, confirm your UserGame.ini overrides survived, and spot check anything that reads game memory, since offsets move between builds. Logs: $LOG"
  log "=== DONE: $new_build ==="
}
main "$@"
