#!/bin/bash
# usage: post.sh <slug> <channel: announcements|server_info> <body>
set -o pipefail
python3 /opt/lastsietch-weekend-events/make-notice.py "$1" "$3" --minutes 10 --channel "$2" \
  | python3 "${LASTSIETCH_NOTICE_SEND:-/opt/lastsietch-notices/releases/fe92ada49a9ad0f6/send.py}" discord \
      --token-command-json '["sh","-c","grep ^DISCORD_BOT_TOKEN= /opt/cielago/.env | cut -d= -f2-"]' \
  | tee -a /var/log/lastsietch-weekend-events-discord.log
