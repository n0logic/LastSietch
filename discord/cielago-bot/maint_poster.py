#!/usr/bin/env python3
# Last Sietch maintenance announcements -> #announcements (Cielago bot, raw REST).
# Window: 2026-06-28 22:00 ET datacenter power maintenance. epoch(22:00 ET)=1782698400
import sys, json, urllib.request, urllib.error

CHANNEL_ID = "<discord-id>"  # 📢｜announcements
EPOCH = 1782698400
TS_T = f"<t:{EPOCH}:t>"   # viewer-local time (auto-converts per timezone)
TS_R = f"<t:{EPOCH}:R>"   # live relative countdown

MESSAGES = {
"now": f"""🛠️ **Scheduled Server Maintenance — Downtime Tonight**

Our host is performing **emergency data center power maintenance** tonight, and the server must go offline while the work is done.

🕙 **Goes offline:** {TS_T} ({TS_R})
⏳ **Expected downtime:** up to ~1 hour

We'll bring the server down cleanly beforehand to protect your characters, bases, and progress. Please wrap up and **log out safely before {TS_T}**. We'll post reminders as we approach the window and let you know the moment we're back.

Thank you for your patience, sietch. 🏜️""",

"t1h": f"""⏰ **1 Hour Until Maintenance Downtime**

The server goes offline {TS_R} ({TS_T}) for **data center power maintenance** — expected back within about an hour.

Start wrapping up: finish your run, stash your loot, and park somewhere safe. We'll bring it down cleanly at {TS_T}. 🛠️""",

"t15m": f"""🚨 **15 Minutes Until Shutdown**

The server goes offline {TS_R} for **power maintenance**. **Log out safely now** to protect your character and progress.

See you on the other side. 🏜️""",

"online": f"""✅ **Server Back Online**

Maintenance is complete and the server is back up — the data center power work finished and we're fully operational again.

Thanks for your patience, sietch. Get back out there. **The spice awaits.** 🏜️""",
}

# No role/everyone pings on Discord (user preference); the in-game banner is the attention-grabber.
PARSE = {"now": [], "t1h": [], "t15m": [], "online": []}

def token():
    for line in open("/opt/cielago/.env"):
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")

def post(key, dry=False):
    body = MESSAGES[key]
    if dry:
        print(f"--- DISCORD {key} ({len(body)} chars, parse={PARSE[key]}) ---\n{body}\n")
        return
    payload = json.dumps({"content": body,
                          "allowed_mentions": {"parse": PARSE[key]}}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
        data=payload, method="POST",
        headers={"Authorization": f"Bot {token()}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"})
    try:
        with urllib.request.urlopen(req) as r:
            print(f"POSTED {key} -> message {json.load(r)['id']}")
    except urllib.error.HTTPError as e:
        print(f"FAILED {key} HTTP {e.code}: {e.read().decode()}")
        raise SystemExit(1)

if __name__ == "__main__":
    post(sys.argv[1], dry="--dry" in sys.argv)
