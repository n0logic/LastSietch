#!/usr/bin/env bash
# <web-host> disk-stall watch. Runs every minute from io-watch.timer.
#
# WHY: on 2026-09-28 10:43-11:08 ET <web-host>'s disk stalled (kernel IO pressure "full" ~93% over 5 min,
# load 162 on 3 cores, memory fine). The portal hung, bot prices went stale and hid, cron skipped runs, and
# the only signal was a player report. The provider panel shows CPU and memory, neither of which moved.
#
# WHAT: reads /proc/pressure/io ("full avg60": the share of the last minute in which every runnable task was
# blocked on IO) and times one local portal request. BAD_MIN consecutive bad minutes -> one alert; the first
# good minute after an alert -> one recovery note. Notifies via karum-notify-local.sh (#server-logs + owner DM).
#
# LIMIT: it runs on the box it watches. During a hard stall this timer is delayed too, so the alert lands when
# the stall eases or persists, not at its first second. The recovery note carries how long it lasted.
set -u
STATE_DIR=${STATE_DIR:-/var/lib/lastsietch-io-watch}
FULL_PCT=${FULL_PCT:-30}        # full avg60 above this = a bad minute
PORTAL_SLOW_S=${PORTAL_SLOW_S:-10}
BAD_MIN=${BAD_MIN:-3}
NOTIFY=${NOTIFY:-/opt/lastsietch/karum-notify-local.sh}
mkdir -p "$STATE_DIR"

full=$(awk '/^full/{for(i=1;i<=NF;i++) if($i ~ /^avg60=/){sub("avg60=","",$i); print $i}}' /proc/pressure/io)
full_i=${full%%.*}
read -r code t < <(curl -s -o /dev/null -w '%{http_code} %{time_total}' -m "$PORTAL_SLOW_S" http://127.0.0.1:8078/api/dune/rules; true)
code=${code:-000}; t=${t:-?}
load=$(cut -d' ' -f1 /proc/loadavg)

bad=0; why=""
[ "${full_i:-0}" -ge "$FULL_PCT" ] && { bad=1; why="disk stall"; }
[ "$code" != "200" ] && { bad=1; why="${why:+$why + }portal not answering"; }

streak=$(cat "$STATE_DIR/streak" 2>/dev/null || echo 0)
alerted=$(cat "$STATE_DIR/alerted" 2>/dev/null || echo "")
now=$(date -u +%s)

if [ "$bad" = 1 ]; then
  streak=$((streak + 1)); echo "$streak" > "$STATE_DIR/streak"
  echo "bad minute $streak: io_full_avg60=${full}% portal=${code} ${t}s load=${load}"
  if [ "$streak" -ge "$BAD_MIN" ] && [ -z "$alerted" ]; then
    echo "$now" > "$STATE_DIR/alerted"
    "$NOTIFY" "⚠️ <web-host> ${why}: IO pressure (full, 1 min) ${full}%, portal local check ${code} in ${t}s, load ${load}. The portal, Cielago and the relay may be hanging. The game server (<game-host>) is separate. A recovery note follows when it clears."
  fi
else
  echo 0 > "$STATE_DIR/streak"
  if [ -n "$alerted" ]; then
    mins=$(( (now - alerted) / 60 ))
    rm -f "$STATE_DIR/alerted"
    "$NOTIFY" "✅ <web-host> recovered (alert was ${mins} min ago). IO pressure ${full}%, portal ${code} in ${t}s, load ${load}. Stale-data features such as bot prices refill on their next sync."
  fi
fi
