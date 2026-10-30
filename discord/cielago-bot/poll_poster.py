#!/usr/bin/env python3
# Last Sietch community polls (Cielago bot, raw REST): posts one native Discord poll to #announcements, reads it back,
# then posts a short link to it in #help-and-feedback and #general. No pings. House style: no em dashes.
# Poll texts are the owner-approved set in ops/rollback-post-1.5/POLLS.md (2026-09-25: poll A keeps its original four
# answers). Deployed to <web-host>:/opt/cielago/poll_poster.py; fired once per poll by <re-host> timers.
#
#   poll_poster.py <KEY> --dry     print what would be posted
#   poll_poster.py <KEY>           post (refuses a poll already recorded in the state file)
import json, os, sys, urllib.request, urllib.error

GUILD = os.environ.get("LAST_SIETCH_GUILD_ID", "")
POLL_CHANNEL = (os.environ.get("CIELAGO_POLL_CHANNEL_ID", ""), "\U0001F5F3️｜polls")
LINK_CHANNELS = [(os.environ.get("CIELAGO_FEEDBACK_CHANNEL_ID", ""), "\U0001F4A1｜help-and-feedback"),
                 (os.environ.get("CIELAGO_GENERAL_CHANNEL_ID", ""), "\U0001F4AC｜general")]
STATE = "/opt/cielago/poll-state.json"
HOURS = 24

POLLS = {
    "A": ("XP is back to stock, with double XP events instead. How often should they run?",
          ["Every weekend", "Every other weekend", "One weekend a month", "Holidays and special events only"]),
    "B": ("Inventory volume is currently 2x on top of our larger base inventory. Keep it?",
          ["Keep 2x", "1.5x", "Stock volume on our base inventory"]),
    "C": ("Enemies are back to stock. How should they feel?",
          ["Stock is right", "Slightly easier", "Harder"]),
    "D": ("Water, stamina and the sandworm are back to stock. Keep it that way?",
          ["Keep stock survival", "Faster water extraction only", "Easier survival overall"]),
    "E": ("Crafting and building costs are back to stock (Funcom already cut crafting costs in 1.5.3). Change them?",
          ["Keep stock", "0.8x crafting and building cost"]),
    "D2": ("Tiebreaker: the survival poll tied 6-6. Which should it be?",
           ["Faster water extraction only", "Easier survival overall"]),
    "F": ("Our Landsraad board: 6 contracts per block, 15 min repeat, 10 sec abandon, 35 charges a day (140 max), "
          "20,000 house goal. Keep it?",
          ["Keep all of it", "Keep it, but a higher house goal", "Back to stock"]),
    # E closed 7-6 for 0.8x; the server holds CraftingCost and BuildingCostMultiplier separately, so it is re-run
    # as two polls (owner 2026-09-30)
    "EB": ("Building costs (base pieces) are at stock. Change them?",
           ["Keep stock", "0.8x building cost", "0.6x building cost"]),
    "EC": ("Crafting costs (items, gear, components) are at stock, and Funcom already cut them in 1.5.3. Change them?",
           ["Keep stock", "0.8x crafting cost", "0.6x crafting cost"]),
    # Amtal wave (Medic, 2026-09-28): his questions 5-7 first; 1-3 (raiding) wait on the zone/mitigation test
    "AM5": ("Amtal PvP loot: what drops when you die in PvP?",
            ["Full loot, everything drops", "Inventory drops, equipped gear stays", "Resources only", "Nothing drops"]),
    "AM6": ("Amtal: if we go full loot, how much should we raise resource rates?",
            ["No boost, keep it scarce", "1.5x", "2x", "3x or more"]),
    "AM7": ("Amtal: what should you get for PvP besides loot? Pick as many as you like.",
            ["Server leaderboards and titles", "Weekly rewards for top PvPers", "Contested zones with bonus resources",
             "Bounties on players on a kill streak", "Guild vs guild territory control", "Loot is enough"]),
    # Amtal follow-up (owner 2026-10-06): absolute numbers against stock, after players read AM6 as x stock
    "AM8": ("Amtal resource rate in the open, x stock (Amtal is PvP outside Riftwatch and Helius Gate, and PvP zones "
            "add Funcom's 2.5x, included here). Covers hand gathering, cutteray and vehicle mining.",
            ["2.5x in the open (1x base, stock)", "3.75x in the open (1.5x base)", "5x in the open (2x base)",
             "6.25x in the open (2.5x base, current plan)"]),
    "AM9": ("Amtal backpack size. Your inventory specializations stack on top of any choice.",
            ["4x volume + 100 slots (current)", "3x volume + 75 slots", "Same as other sietches (2x volume, 50 slots)"]),
    # AM9 closed 4-4 (owner 2026-10-07): two-way rerun of the tied answers, same as D2
    "AM9B": ("Tiebreaker: the Amtal backpack poll tied 4-4. Which should it be?",
             ["4x volume + 100 slots (current)", "Same as other sietches (2x volume, 50 slots)"]),
    # Abandoned bases (Medic + owner, 2026-10-07)
    "BASE1": ("What should happen to bases whose owner has been inactive for 30+ days? Pick as many as you like.",
              ["Sealed-bid auction (Storage Wars style)", "Guildmates get first claim, then auction",
               "Timed public salvage event, then demolish", "Demolish and refund materials to the owner",
               "Leave them alone"]),
    # Kulon close (owner 2026-10-24): two answers only, context line under the intro
    "KULON1": ("Should we close Kulon? Amtal is our full PvP Hagga. Kulon is the original launch Hagga with PvP only in "
               "some areas. If it closes, every Kulon base, vehicle and container (with contents) moves to Amtal at the "
               "same spot, after 72 hours notice on Discord and to anyone joining Kulon.",
               ["Yes, close Kulon and move everything to Amtal", "No, keep Kulon open"]),
    "BLD1": ("Builders: how many staking units should one sub-fief be allowed to claim? Today it is 5 (plus the console's "
             "own block). Anything above 5 only works for players who add one line to their own Game.ini.",
             ["Keep 5 (nothing to change on your PC)", "8 staking units (one Game.ini line)",
              "11 staking units (one Game.ini line)", "15 staking units (one Game.ini line)"]),
    "LAND1": ("Landsraad: you can hold 3 contracts at once. Raise that to 6?",
              ["Yes, hold up to 6", "No, keep 3"]),
}
MULTISELECT = {"AM7", "BASE1"}
INTRO = "\U0001F5F3️ **Community poll**: you decide the server settings from here. Open for 24 hours."
# Optional per-poll line posted under INTRO, for context that does not fit the 300-char question
CONTEXT = {
    "KULON1": ("Closing Kulon also frees server capacity for upcoming survival maps. If a Kulon base overlaps an Amtal "
               "base, the less active owner's base is saved to a Base Reconstruction Tool backup, so nothing is lost. "
               "Our scan found only one overlap, and it's already handled."),
    "BLD1": ("Bigger claims give you room to spread out, but the building piece limit stays the same, so it is more ground, "
             "not more pieces. Bigger bases also sit closer to neighbours and points of interest. Players who skip the "
             "Game.ini line keep today's size. Whatever wins, blueprints and base backups are raised to match, so a full "
             "size base can always be saved. Deep Desert claims stay as they are.\nWe test every change on our side before the window. If one turns out to need a line in your own game settings file, we will post the exact line; if one does not work at all, we will say so instead of shipping it."),
    "LAND1": ("Holding more contracts may need a client setting, or may not work on a self-hosted server at all.\nWe test every change on our side before the window. If one turns out to need a line in your own game settings file, we will post the exact line; if one does not work at all, we will say so instead of shipping it."),
}


def token():
    for line in open("/opt/cielago/.env"):
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no token")


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://discord.com/api/v10/" + path, data=data, method=method,
                                 headers={"Authorization": "Bot " + token(), "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"FAILED {method} {path}: HTTP {e.code} {e.read().decode()[:300]}")


def load_state():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in POLLS:
        raise SystemExit("usage: poll_poster.py <" + "|".join(POLLS) + "> [--dry]")
    key, dry = sys.argv[1], "--dry" in sys.argv
    question, answers = POLLS[key]
    intro = INTRO + ("\n" + CONTEXT[key] if key in CONTEXT else "")
    assert len(question) <= 300 and all(len(a) <= 55 for a in answers) and 2 <= len(answers) <= 10
    for text in [intro, question, *answers]:
        assert not any(bad in text for bad in ("@everyone", "@here", "<@"))
    if dry:
        print(f"--- poll {key} -> #{POLL_CHANNEL[1]} ({HOURS} h, multiselect={key in MULTISELECT})\n{intro}\nQ: {question}")
        for a in answers:
            print(f"  - {a}")
        print("links -> " + ", ".join(n for _, n in LINK_CHANNELS))
        return
    state = load_state()
    if key in state:
        raise SystemExit(f"REFUSED: poll {key} already posted as message {state[key]['message_id']}")
    for cid, name in [POLL_CHANNEL, *LINK_CHANNELS]:
        ch = api("GET", "channels/" + cid)
        if ch.get("name") != name or ch.get("guild_id") != GUILD:
            raise SystemExit(f"REFUSED: channel {cid} is {ch.get('name')!r}, expected {name!r}")
    msg = api("POST", f"channels/{POLL_CHANNEL[0]}/messages", {
        "content": intro, "allowed_mentions": {"parse": []},
        "poll": {"question": {"text": question}, "answers": [{"poll_media": {"text": a}} for a in answers],
                 "duration": HOURS, "allow_multiselect": key in MULTISELECT}})
    back = api("GET", f"channels/{POLL_CHANNEL[0]}/messages/{msg['id']}")
    got = [a["poll_media"]["text"] for a in (back.get("poll") or {}).get("answers", [])]
    if ((back.get("poll") or {}).get("question", {}).get("text") != question or got != answers
            or (back.get("poll") or {}).get("allow_multiselect") != (key in MULTISELECT)):
        raise SystemExit(f"READBACK MISMATCH on message {msg['id']}; links not posted")
    state[key] = {"message_id": msg["id"]}
    json.dump(state, open(STATE, "w"))
    url = f"https://discord.com/channels/{GUILD}/{POLL_CHANNEL[0]}/{msg['id']}"
    links = []
    for cid, _ in LINK_CHANNELS:
        m = api("POST", f"channels/{cid}/messages", {
            "content": f"\U0001F5F3️ New community poll in <#{POLL_CHANNEL[0]}>: **{question}** Vote here: {url}",
            "allowed_mentions": {"parse": []}})
        links.append(m["id"])
    state[key]["links"] = links
    json.dump(state, open(STATE, "w"))
    print(f"POSTED poll {key}: message {msg['id']} (readback OK), links {links}")


if __name__ == "__main__":
    main()
