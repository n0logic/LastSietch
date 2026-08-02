#!/usr/bin/env python3
# Last Sietch -> #announcements (Cielago bot, raw REST). Coriolis reset apology +
# one-time specialization XP boost. No pings (Variant B). House style: no em dashes.
# Deployed to the web host:/opt/cielago/spec_boost_poster.py; fired by the <inference-host>
# spec-backfill timer once both stragglers (Lara/Magreeth) are backfilled.
import sys, json, urllib.request, urllib.error
CHANNEL_ID = "<discord-id>"  # announcements
BODY = """\U0001F3DC️ **Coriolis Reset + a Thank-You Boost**

Quick note on last night: the Deep Desert ran its scheduled Coriolis reset around 1:00 AM ET. Resource fields and points of interest re-rolled into fresh positions, the map fog reset, and the portal map is already updated. It also came with a couple of fast server restarts, and a few of you got kicked mid-activity. Apologies, especially to anyone who lost progress mid-mission.

As a thank-you for riding it out, every character has received a one-time **specialization XP boost**: a flat **+5,000 XP** across the specializations you have trained. It may require a relog to apply, so if you do not see it yet, log out and back in. \U0001F3DC️"""

def token():
    for line in open("/opt/cielago/.env"):
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")

def main():
    dry = "--dry" in sys.argv
    if dry:
        print(f"--- DISCORD ({len(BODY)} chars, no pings) ---\n{BODY}\n"); return
    payload = json.dumps({"content": BODY, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
        data=payload, method="POST",
        headers={"Authorization": f"Bot {token()}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"})
    try:
        with urllib.request.urlopen(req) as r:
            print(f"POSTED -> message {json.load(r)['id']}")
    except urllib.error.HTTPError as e:
        print(f"FAILED HTTP {e.code}: {e.read().decode()}"); raise SystemExit(1)

if __name__ == "__main__":
    main()
