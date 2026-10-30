#!/bin/bash
# Dune Awakening Steam watcher.
# Watches:
#   4754530 (Dune: Awakening Self-Hosted Server, GA) mode=buildid -- patch detector for the
#                                                    self-host server appid Last Sietch actually runs
#   1172710 (Dune: Awakening Live Client)    mode=buildid  -- live patch detector for TLDR workflow
#
# Removed appids (history):
#   2995470 (Dune PTC Client)        -- removed 2026-05-04, PTC dev-cycle spam, not actionable for Last Sietch
#   1192040 mode=full → mode=buildid -- switched 2026-05-04, change_number ticks were noise.
#   3104830 (Dune PTC Self-Host Server) -- removed 2026-05-20. Funcom retired PTC: the self-hosted
#                                       servers page no longer references a PTC product, and Last Sietch
#                                       runs the GA appid 4754530. The old 3104830) case branch
#                                       (with the now-moot "release canary" sub-branch) was
#                                       repointed to 4754530.
#
# State files for removed appids left in place across changes.
#
# Modes:
#   full     : alert on any change to the SUMMARY blob. Currently unused.
#   buildid  : alert ONLY when the public buildid actually changes. State file is still
#              refreshed on every poll so we don't re-alert on metadata churn.
#
# State: /var/lib/lastsietch/dune-app-<appid>.json (full snapshot)
# Posts to the staff log channel (DISCORD_CH_BOTLOGS).

set -uo pipefail
source /opt/lastsietch/.env

STATE_DIR=/var/lib/lastsietch
mkdir -p "$STATE_DIR"

APPS=(
  "4754530:buildid:Dune Awakening Self-Hosted Server (GA)"
  "1172710:buildid:Dune Awakening Live Client"
)

post_discord() {
  local content="$1"
  curl -sf -X POST "https://discord.com/api/v10/channels/${DISCORD_CH_BOTLOGS}/messages" \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "User-Agent: Last Sietch-BuildWatch/1.0" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"content": sys.stdin.read()}))' <<<"$content")" \
    > /dev/null
}

extract_buildid() {
  python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('buildid', 'none'))" <<<"$1"
}

for entry in "${APPS[@]}"; do
  APPID=$(echo "$entry" | cut -d: -f1)
  MODE=$(echo "$entry" | cut -d: -f2)
  LABEL=$(echo "$entry" | cut -d: -f3-)
  STATE_FILE="$STATE_DIR/dune-app-$APPID.json"

  RESP=$(curl -sf --max-time 10 "https://api.steamcmd.net/v1/info/$APPID" 2>/dev/null || echo "")
  if [ -z "$RESP" ]; then
    logger -t lastsietch-dune-buildwatch "appid $APPID query failed"
    continue
  fi

  SUMMARY=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)['data'].get('$APPID', {})
buildid = d.get('depots', {}).get('branches', {}).get('public', {}).get('buildid', 'none')
print(json.dumps({
  'change_number': d.get('_change_number'),
  'public_only': d.get('public_only', '0'),
  'missing_token': d.get('_missing_token', False),
  'buildid': buildid,
}))
" 2>/dev/null) || { logger -t lastsietch-dune-buildwatch "parse failed appid $APPID"; continue; }

  if [ ! -f "$STATE_FILE" ]; then
    echo "$SUMMARY" > "$STATE_FILE"
    logger -t lastsietch-dune-buildwatch "initialized appid $APPID -> $SUMMARY"
    continue
  fi

  OLD=$(cat "$STATE_FILE")
  if [ "$SUMMARY" = "$OLD" ]; then
    continue
  fi

  # buildid-only mode: silently refresh state on metadata-only changes
  if [ "$MODE" = "buildid" ]; then
    BID_NEW=$(extract_buildid "$SUMMARY")
    BID_OLD=$(extract_buildid "$OLD")
    if [ "$BID_OLD" = "$BID_NEW" ]; then
      echo "$SUMMARY" > "$STATE_FILE"
      logger -t lastsietch-dune-buildwatch "metadata-only tick appid=$APPID buildid unchanged ($BID_NEW)"
      continue
    fi
  fi

  DIFF=$(python3 -c "
import json
old, new = json.loads('''$OLD'''), json.loads('''$SUMMARY''')
out=[]
for k in sorted(set(old)|set(new)):
  if old.get(k)!=new.get(k):
    out.append(f'  - {k}: {old.get(k)!r} -> {new.get(k)!r}')
print('\n'.join(out))
")

  case "$APPID" in
    4754530)
      BUILDID_NEW=$(extract_buildid "$SUMMARY")
      BUILDID_OLD=$(extract_buildid "$OLD")
      MSG=":dna: :rotating_light: **Dune self-host server build changed** (\`$LABEL\`, appid \`$APPID\`)
\`buildid\`: \`$BUILDID_OLD\` -> \`$BUILDID_NEW\`
This is the appid Last Sietch runs. Review the update procedure before pulling.
Cross-check: https://duneawakening.com/news/ and https://steamdb.info/app/$APPID/"
      ;;
    1172710)
      BUILDID_NEW=$(extract_buildid "$SUMMARY")
      BUILDID_OLD=$(extract_buildid "$OLD")
      MSG=":mag: **Dune live client patched** (\`$LABEL\`, appid \`$APPID\`)
\`buildid\`: \`$BUILDID_OLD\` -> \`$BUILDID_NEW\`
Funcom typically posts patch notes after deploy. When they land, drop a TLDR in #dune.
Patch notes feed: https://duneawakening.com/news/
SteamDB: https://steamdb.info/app/$APPID/patchnotes/"
      ;;
    *)
      MSG=":eyes: **Steam app changed** (\`$LABEL\`, appid \`$APPID\`)
$DIFF"
      ;;
  esac

  post_discord "$MSG"
  echo "$SUMMARY" > "$STATE_FILE"
  logger -t lastsietch-dune-buildwatch "ALERT appid=$APPID old=$OLD new=$SUMMARY"
done
