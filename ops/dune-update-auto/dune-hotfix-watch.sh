#!/usr/bin/env bash
# =============================================================================
# Dune hotfix watch — notify when Funcom ships a corrected build
# =============================================================================
# Context (2026-06-26): Last Sietch is LIVE and healthy on the corrected build
# 2007976-0-shipping (buildid 23894313, 1.4.10.0). This watcher polls the self-host
# server Steam app (4754530) and DMs the operator when Funcom (a) bumps the public buildid,
# or (b) silently re-pushes the same buildid with new content (timeupdated changes) —
# i.e. when a NEW self-host SERVER build ships. NOTIFY-ONLY; the update is supervised
# by hand (back up and drop any custom schema before the update and restore it after
# verify, then re-apply customisations). A client-only Steam patch does NOT
# move app 4754530, so client hotfixes (e.g. BattlEye) correctly do not fire this.
#
# Arm: systemd user timer on <orchestrator-host>, every ~10 min, linger on.
# Disarm: systemctl --user disable --now dune-hotfix-watch.timer
# =============================================================================
set -u

GAME_HOST="${DUNE_HFW_GAME_HOST:-<game-host>}"
APPID="${DUNE_HFW_APPID:-4754530}"
BASE_BUILDID="${DUNE_HFW_BASE_BUILDID:-23894313}"        # current live build (2007976-0-shipping, 1.4.10.0)
BASE_TIMEUPDATED="${DUNE_HFW_BASE_TIMEUPDATED:-1782315048}"
WORKDIR="${DUNE_HFW_WORKDIR:-$HOME/dune-update-auto}"
LOG="$WORKDIR/hotfix-watch.log"
NOTIFIED="$WORKDIR/hotfix-notified.marker"

DM_ENABLE="${DUNE_HFW_DM_ENABLE:-1}"
DM_SSH_HOST="${CIELAGO_SSH_HOST:-the web host}"
CIELAGO_VENV_PY="${CIELAGO_VENV_PY:-/opt/cielago/venv/bin/python}"
CIELAGO_ENV="${CIELAGO_ENV:-/opt/cielago/.env}"
OWNER_ID="${DUNE_HFW_OWNER_ID:-215146359479730176}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

mkdir -p "$WORKDIR"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

dm_owner() {  # dm_owner <body> -> owner DM + #server-logs mirror (shared notifier)
  [ "$DM_ENABLE" = "1" ] || { log "DM disabled; would send: $1"; return 0; }
  CIELAGO_SSH_HOST="$DM_SSH_HOST" CIELAGO_ENV="$CIELAGO_ENV" CIELAGO_VENV_PY="$CIELAGO_VENV_PY" \
    CIELAGO_OWNER_ID="$OWNER_ID" "${CIELAGO_NOTIFY:-$HOME/bin/cielago-notify.sh}" "$1" \
    >>"$LOG" 2>&1 || log "WARN DM failed"
}

[ -f "$NOTIFIED" ] && { log "already notified; no-op"; exit 0; }

read -r bid tup < <(ssh "${SSH_OPTS[@]}" "$GAME_HOST" \
  "sudo -u dune bash -lc 'steamcmd +login anonymous +app_info_update 1 +app_info_print $APPID +quit 2>/dev/null'" 2>/dev/null \
  | awk '/"public"/{p=1} p&&/"buildid"/{gsub(/[^0-9]/,"",$2); b=$2} p&&/"timeupdated"/{gsub(/[^0-9]/,"",$2); print b, $2; exit}')

if [ -z "${bid:-}" ]; then log "poll failed (no buildid)"; exit 0; fi

if [ "$bid" != "$BASE_BUILDID" ]; then
  log "NEW BUILD $bid (was $BASE_BUILDID)"
  dm_owner "🟢 Funcom shipped a NEW self-host SERVER build: $bid (was $BASE_BUILDID). You are LIVE on the old one. Plan a supervised update in an announced window, and re-apply whatever customisations an update reverts. Check the community channels first."
  touch "$NOTIFIED"
elif [ "$tup" != "$BASE_TIMEUPDATED" ]; then
  log "RE-PUSH of $bid (timeupdated $BASE_TIMEUPDATED -> $tup)"
  dm_owner "🟡 Funcom RE-PUSHED server build $bid (same buildid, new content: timeupdated $BASE_TIMEUPDATED -> $tup). Likely a silent server-side content fix. Consider a supervised re-pull/update on <game-host> in a window."
  touch "$NOTIFIED"
else
  log "no change (buildid=$bid timeupdated=$tup)"
fi
