#!/usr/bin/env python3
# DM the owner (Cielago bot) when the spec-XP backfill finishes both stragglers.
# Deployed to the web host:/opt/cielago/spec_backfill_dm.py; called by the <inference-host>
# spec-backfill timer once Lara + Magreeth are both boosted. House style: no em dashes.
import sys, json, urllib.request, urllib.error
RECIPIENT = "215146359479730176"  # the owner
BODY = ("✅ Spec-XP backfill complete: **Lara** and **Magreeth** have been boosted "
        "(+5000 across their trained tracks). All 23 characters are now done. The "
        "#announcements post is already live; the backfill timer has self-disabled.")

def token():
    for line in open("/opt/cielago/.env"):
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")

def req(url, data=None, method="GET"):
    r = urllib.request.Request(url, data=(json.dumps(data).encode() if data else None),
        method=method, headers={"Authorization": f"Bot {token()}", "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"})
    with urllib.request.urlopen(r) as resp:
        return json.load(resp)

def main():
    if "--dry" in sys.argv:
        print(f"--- DM to {RECIPIENT} ---\n{BODY}"); return
    try:
        ch = req("https://discord.com/api/v10/users/@me/channels",
                 {"recipient_id": RECIPIENT}, "POST")
        msg = req(f"https://discord.com/api/v10/channels/{ch['id']}/messages",
                  {"content": BODY}, "POST")
        print(f"DM-SENT -> message {msg['id']}")
    except urllib.error.HTTPError as e:
        print(f"DM-FAILED HTTP {e.code}: {e.read().decode()}"); raise SystemExit(1)

if __name__ == "__main__":
    main()
