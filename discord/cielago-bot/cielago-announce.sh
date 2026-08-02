#!/usr/bin/env bash
# Post a message (read from a file) to one or more Discord channels as Cielago.
# Token comes from /opt/cielago/.env (never echoed). Usage:
#   cielago-announce.sh <msgfile> <channel_id> [channel_id ...]
set -euo pipefail
MSGFILE="${1:?usage: cielago-announce.sh <msgfile> <channel_id> [...]}"; shift
[ -r "$MSGFILE" ] || { echo "cannot read $MSGFILE" >&2; exit 1; }
set -a; . /opt/cielago/.env; set +a
: "${DISCORD_BOT_TOKEN:?DISCORD_BOT_TOKEN not set in /opt/cielago/.env}"
PAYLOAD=$(jq -Rs '{content: .}' < "$MSGFILE")
rc=0
for CH in "$@"; do
  resp=$(mktemp)
  code=$(curl -s -o "$resp" -w '%{http_code}' \
    -X POST "https://discord.com/api/v10/channels/$CH/messages" \
    -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")
  if [ "$code" = "200" ]; then
    mid=$(jq -r '.id // "?"' < "$resp")
    echo "channel $CH -> OK (message $mid)"
  else
    echo "channel $CH -> HTTP $code"; cat "$resp"; echo; rc=1
  fi
  rm -f "$resp"
done
exit $rc
