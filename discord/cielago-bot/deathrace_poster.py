#!/usr/bin/env python3
import sys, os, json, urllib.request

CHANNEL_ID = "<discord-id>"  # 📢｜announcements
EVENT_URL = "https://discord.com/events/<discord-id>/<discord-id>"
HOST = "<@792150113182154803>"          # Suk Medic
CHAMP = "<@&<discord-id>>"        # Champion of the Last Sietch role
VC = "<#<discord-id>>"            # staging voice channel
TS_F = "<t:1782601200:F>"
TS_R = "<t:1782601200:R>"
TS_T = "<t:1782601200:t>"

MESSAGES = {
"t0": f"""🪱 **THE SANDS WILL RUN RED: FREMEN DEATH RACE** 🏁

@everyone The distrans carries word across Arrakis: {HOST} calls every racer to the **Fremen Death Race** in the **PvP Deep Desert**.

🗓️ **{TS_F}** ({TS_R})

Sandbike, buggy, or a beast of your own build. All are welcome to the line. Stay on the marked route or face disqualification. **Weapons are live: shoot to down your rivals, but no kill shots. Knock them out of the running, do not end them.** **Wake Shai-Hulud and be devoured, and your race ends there.**

First across the final checkpoint is crowned {CHAMP} right here on Discord, and claims rare resources, high-tier gear, and a name carved into the Sietch Hall of Legend.

Mark yourself **Interested** 👉 {EVENT_URL}

*Tune your engines. The desert does not forgive the unprepared.*""",

"t1": f"""☀️ **RACE DAY: FREMEN DEATH RACE** 🏁

Today the sands decide. {HOST} drops the flag in the **PvP Deep Desert** at **{TS_T}**, which is **{TS_R}**.

✅ Fuel topped off and a spare cell stashed
✅ Vehicle repaired and race-ready
✅ Spawn set close to the start
✅ Ears in voice {VC} for the staging call

Any vehicle runs. Off-route means DQ. **Weapons down rivals but never kill. Drop them, do not finish them.** **Wake the worm and be eaten, and you're out.** Last engine running takes the {CHAMP} title, the loot, and the glory.

Stage your interest 👉 {EVENT_URL}""",

"t2": f"""⏳ **ONE HOUR TO THE LINE** 🏁

The Fremen Death Race begins {TS_R}. Make for the **PvP Deep Desert** now and pull into voice {VC} so {HOST} can brief the route and checkpoints.

Last checks: full fuel, a spare cell, repaired hull, respawn set near the start. Latecomers race the worm alone.""",

"t3": f"""🔧 **30 MINUTES TO FINAL PREP** 🏁

Engines hot. The Death Race rolls out {TS_R}. Get into the **PvP Deep Desert** and into voice {VC} with {HOST} for the staging order. If you're not at the line when the flag drops, you start in the dust.""",

"t4": f"""🚨 **LAST CALL: 15 MINUTES TO IGNITION** 🏁

Racers to the line. The Fremen Death Race begins {TS_R}. Final boarding in voice {VC} **now**. Helmets on, throttles ready. Shoot to down, never to kill, and may Shai-Hulud take someone else. **Ride, Fedaykin.** 🪱💨""",
}

def token():
    for line in open("/opt/cielago/.env"):
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no token")

def post(key, dry=False):
    body = MESSAGES[key]
    if dry:
        print(f"--- {key} ({len(body)} chars) ---\n{body}\n")
        return
    parse = ["everyone"] if key == "t0" else []
    payload = json.dumps({"content": body, "allowed_mentions": {"parse": parse}}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
        data=payload, method="POST",
        headers={"Authorization": f"Bot {token()}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"})
    try:
        with urllib.request.urlopen(req) as r:
            msg = json.load(r)
            print(f"POSTED {key} -> message {msg['id']}")
    except urllib.error.HTTPError as e:
        print(f"FAILED {key} HTTP {e.code}: {e.read().decode()}")
        raise SystemExit(1)

if __name__ == "__main__":
    key = sys.argv[1]
    dry = "--dry" in sys.argv
    post(key, dry)
