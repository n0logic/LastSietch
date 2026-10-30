#!/usr/bin/env bash
# Re-sends SecurityZones.UsePvPOverrideTable 1 to Amtal (partition 33) after every Amtal process start.
#
# WHY: the cvar's OnChanged callback is the only thing that sets the replicated GameState flag
# m_bUseSecurityZonePvPOverrideTable, which makes players' clients use DT_SecurityZones_PvPOverride
# (open desert = PvP, abilities can target players). An ini/boot value never fires it, and every
# restart resets the flag to 0. Without this, an Amtal restart
# silently brings back "abilities won't target players".
#
# HOW: each tick resolves Amtal's current server_id; when it differs from the last one applied and
# the current container has logged "Server farm is READY", it sends the set to that server_id only
# (targeted routing key, never the fleet-wide default), reads it back, and records the id.
# Kill switch: touch /etc/lastsietch/amtal-pvp-flag-disabled
set -u
NS=${LASTSIETCH_GAME_NAMESPACE:?set LASTSIETCH_GAME_NAMESPACE}
# Partition and pod suffix of one map instance, from the map-instance config (LASTSIETCH_MAP_INSTANCES_FILE); prints
# the defaults passed in when the file is absent or the entry is unusable.
instance_target() {
  python3 -c '
import json, re, sys
path, key, part, pod = sys.argv[1:5]
try:
    for row in json.load(open(path))["instances"]:
        if row.get("key") == key:
            p, s = row.get("partition"), row.get("pod")
            if type(p) is int and 0 <= p <= 9999 and isinstance(s, str) and re.fullmatch(r"-[a-z0-9-]{1,63}", s):
                part, pod = p, s
            break
except Exception:
    pass
print(part, pod)' "${LASTSIETCH_MAP_INSTANCES_FILE:-/etc/lastsietch/map-instances.json}" "$1" "$2" "$3" 2>/dev/null || echo "$2 $3"
}
read -r PARTITION POD_SUFFIX <<< "$(instance_target "${LASTSIETCH_MAP_INSTANCE_KEY:-amtal}" 33 -sg-survival-1-pod-33)"
POD=${NS#funcom-seabass-}$POD_SUFFIX
STATE_DIR=/var/lib/lastsietch-amtal-pvp-flag
SEND=/root/dune-service-broadcast.py
ENV_FILE=/opt/lastsietch-pod-watcher/.discord.env
mkdir -p "$STATE_DIR"
[ -e /etc/lastsietch/amtal-pvp-flag-disabled ] && exit 0

post_discord() {
  [ -r "$ENV_FILE" ] || return 0
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  [ -n "${DISCORD_BOT_TOKEN:-}" ] && [ -n "${DISCORD_CH_BOT_LOGS:-}" ] || return 0
  curl -fsS -m 10 -X POST -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" -H "Content-Type: application/json" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1], "allowed_mentions": {"parse": []}}))' "$1")" \
    "https://discord.com/api/v10/channels/${DISCORD_CH_BOT_LOGS}/messages" >/dev/null || true
}

sid=$(/root/dq.sh -At -c "select f.server_id from dune.farm_state f join dune.world_partition w on w.server_id = f.server_id where w.partition_id = $PARTITION;" 2>/dev/null | head -1)
[ -n "$sid" ] || exit 0
[ "$(cat "$STATE_DIR/applied" 2>/dev/null)" = "$sid" ] && exit 0
[ "$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)" = "true" ] || exit 0
kubectl -n "$NS" logs "$POD" 2>/dev/null | grep -aq "Server farm is READY" || exit 0

python3 "$SEND" --routing-key "$sid" --operator amtal-pvp-flag --send serverexec \
  --exec "SecurityZones.UsePvPOverrideTable 1" >/dev/null 2>&1
sleep 3
python3 "$SEND" --routing-key "$sid" --operator amtal-pvp-flag --send serverexec \
  --exec "SecurityZones.UsePvPOverrideTable" >/dev/null 2>&1
sleep 4
if kubectl -n "$NS" logs "$POD" --since=30s 2>/dev/null | grep -aq 'UsePvPOverrideTable = "1"'; then
  echo "$sid" > "$STATE_DIR/applied"
  rm -f "$STATE_DIR/failed-$sid"
  echo "applied to $sid"
  post_discord "🟢 Amtal PvP rules flag re-applied after a restart (server $sid): open desert PvP and ability targeting restored."
else
  echo "readback failed for $sid"
  if [ ! -e "$STATE_DIR/failed-$sid" ]; then
    touch "$STATE_DIR/failed-$sid"
    post_discord "⚠️ Amtal PvP rules flag could NOT be confirmed after a restart (server $sid). Abilities may not target players in the open desert until it is re-sent. Retrying every minute."
  fi
fi
