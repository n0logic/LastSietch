#!/usr/bin/env python3
# Seven Nights of the Sietch (Cielago bot, raw REST, the spec_boost_poster.py
# pattern). Message types and where each goes (CHANNELS, env-overridable):
#   default    the night post at night start (timer 00:01 UTC = 20:01 EDT)   #announcements
#   --recap    the morning recap (timer 10:00 America/New_York): the top five
#              so far (opt-out honoured by the board endpoint) and, during
#              night 6, the secret-stall tease                                #server-info
#   --oneshot launch | stall-open | back-room: one post each, never before its
#              instant (ONESHOTS)                                             #announcements
# No pings ever: allowed_mentions parse [] and the body is linted before sending.
# House style: no em dashes, "LS Tokens", never Funcom's currency name.
#
# Reads the night from lastsietch-admin's public /portal/sietch/tonight-public, which
# never carries hide-and-seek data. If that endpoint is not live yet, --dry falls
# back to the copy of record so the post can be reviewed; a real post refuses.
#
#   sietch_nights_poster.py --dry [--night N] [--recap] [--now 2026-10-27T14:00:00Z]
#   sietch_nights_poster.py --dry --oneshot launch|stall-open|back-room
#   sietch_nights_poster.py           (posts once per night; a stamp file stops repeats)
import sys, os, json, datetime, urllib.request, urllib.error

ANNOUNCEMENTS = os.environ.get("CIELAGO_ANNOUNCEMENTS_CHANNEL_ID", "")
# Per message type. Override any one with SIETCH_CHANNEL_<TYPE> (NIGHT, RECAP,
# LAUNCH, STALL_OPEN, BACK_ROOM) in the environment or /opt/cielago/.env. The
# recap defaults to #server-info, read from the bot's own
# CIELAGO_DUNE_STATUS_CHANNEL_ID so no second copy of that id can drift.
CHANNELS = {"night": ANNOUNCEMENTS, "launch": ANNOUNCEMENTS, "stall-open": ANNOUNCEMENTS,
            "back-room": ANNOUNCEMENTS, "recap": "env:CIELAGO_DUNE_STATUS_CHANNEL_ID"}
# One-shot posts: the copy key in copy["discord"] and the earliest instant (UTC).
ONESHOTS = {"launch": ("launch", "2026-10-25T16:00:00Z"),        # Sun 10-25 12:00 PM EDT
            "stall-open": ("stall_open", "2026-10-29T00:00:00Z"),  # Wed 10-28 8 PM EDT
            "back-room": ("back_room", "2026-11-01T00:00:00Z")}    # Sat 10-31 8 PM EDT
ENV_FILE = os.environ.get("CIELAGO_ENV", "/opt/cielago/.env")
PORTAL = os.environ.get("SIETCH_PORTAL", "http://127.0.0.1:8078")
STAMP_DIR = os.environ.get("SIETCH_STAMP_DIR", "/var/lib/cielago/sietch-nights")
HERE = os.path.dirname(os.path.abspath(__file__))
COPY_PATHS = [os.environ.get("SIETCH_COPY", ""),
              os.path.join(HERE, "sietch_nights_copy.json"),
              os.path.join(HERE, "..", "..", "admin-backend", "data", "sietch_nights_copy.json")]

FORBIDDEN = [("\u2014", "em dash"), ("@everyone", "@everyone"), ("@here", "@here"),
             ("blood mark", "Blood Marks"), ("<@", "a mention")]


def load_copy():
    for p in COPY_PATHS:
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    raise SystemExit("sietch_nights_copy.json not found (set SIETCH_COPY)")


def get_json(path):
    req = urllib.request.Request(PORTAL + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def night_for(copy, now):
    key = now.strftime("%Y-%m-%d")
    for n in copy["nights"]:
        if n["key"] == key:
            return n
    return None


def lint(body):
    low = body.lower()
    bad = [name for needle, name in FORBIDDEN if needle.lower() in low]
    if bad:
        raise SystemExit("refusing to post, body contains: " + ", ".join(bad))
    return body


def night_body(copy, night, spots):
    d = copy["discord"]
    lines = ["\U0001F315 **" + d["night_title"].format(n=night["n"], title=night["title"]) + "**", "",
             "*" + night["hook"] + "*", "", night["how"], ""]
    if spots:
        lines.append("**Tonight's doors:** " + ", ".join(spots))
    lines.append("**Earn:** " + night["award_text"])
    lines.append("**Runs:** " + night["runs_text"])
    stall = d.get("night_stall", {}).get(str(night["n"]))
    if stall:
        lines.append(stall)
    if night["n"] == 4:
        lines.append("**The Token Stall opens at 8 PM ET.**")
    if night["n"] == 7:
        lines.append(copy["event"]["dst_note"])
    lines += ["", d["night_footer"]]
    return "\n".join(lines)


def recap_body(copy, prev, nxt, rows, final):
    d = copy["discord"]
    lines = ["\U0001F3EE **" + d["recap_title"] + ": Night " + str(prev["n"]) + ", " + prev["title"] + "**", ""]
    # Discord-only: a hiding place is never in the night row, which players can read.
    reveal = d.get("recap_reveal", {}).get(str(prev["n"]))
    if reveal:
        lines += ["*" + reveal + "*", ""]
    if rows:
        lines.append("Brightest lanterns so far:")
        for i, r in enumerate(rows[:5], 1):
            lines.append(f"{i}. {r.get('name') or copy['board']['anon_name']}: {int(r.get('earned') or 0)} LS Tokens")
    else:
        lines.append("The lanterns are still being counted.")
    if prev["n"] == 6:
        lines += ["", "*" + d["night6_tease"] + "*"]
    if nxt:
        lines += ["", "Tonight at 8 PM ET: **" + nxt["title"] + "**."]
    elif final:
        lines += ["", d["closing"]]
    else:
        lines += ["", "The last night ends at 7 PM ET today."]
    lines += ["", d["night_footer"]]
    return "\n".join(lines)


def env_value(name):
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in open(ENV_FILE):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def token():
    t = env_value("DISCORD_BOT_TOKEN")
    if not t:
        raise SystemExit("no token")
    return t


def channel_for(kind):
    override = env_value("SIETCH_CHANNEL_" + kind.upper().replace("-", "_"))
    chan = override or CHANNELS[kind]
    if chan.startswith("env:"):
        chan = env_value(chan[4:]) or ""
    if not chan.isdigit():
        return None
    return chan


# One-shots that announce something the live event must back up. The launch is
# exempt: it goes out at noon Sunday, before the 19:30 go/no-go turns earning on,
# so the gate is legitimately off then.
GATED_ONESHOTS = ("stall-open", "back-room")


def live_refusal(pub):
    """Why a real post must not go out, from a tonight-public answer, or None.
    A NO at the go/no-go, or the kill switch on any later night, turns earning
    off; the endpoint then says enabled false and sends no night, and nothing
    may announce a night the portal is not running."""
    if not isinstance(pub, dict):
        return "tonight-public answered nothing usable"
    if pub.get("enabled") is not True:
        return "the event gate is off (tonight-public enabled is not true)"
    if not pub.get("night"):
        return "tonight-public sends no night"
    return None


def oneshot_body(copy, kind):
    return "\n".join(copy["discord"][ONESHOTS[kind][0]])


def post(body, channel, image_url=None):
    msg = {"content": body, "allowed_mentions": {"parse": []}}
    if image_url:
        msg["embeds"] = [{"image": {"url": image_url}}]
    payload = json.dumps(msg).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        data=payload, method="POST",
        headers={"Authorization": f"Bot {token()}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"})
    try:
        with urllib.request.urlopen(req) as r:
            print(f"POSTED -> message {json.load(r)['id']}")
    except urllib.error.HTTPError as e:
        print(f"FAILED HTTP {e.code}: {e.read().decode()}")
        raise SystemExit(1)


def arg_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    dry = "--dry" in sys.argv
    recap = "--recap" in sys.argv
    copy = load_copy()
    now = datetime.datetime.now(datetime.timezone.utc)
    if arg_value("--now"):
        # Review aid: render a post as it would read at another instant.
        if not dry:
            raise SystemExit("--now is for --dry review only")
        now = datetime.datetime.fromisoformat(arg_value("--now").replace("Z", "+00:00"))
    oneshot = arg_value("--oneshot")
    if "--oneshot" in sys.argv and oneshot not in ONESHOTS:
        raise SystemExit("--oneshot takes one of: " + ", ".join(ONESHOTS))
    forced = arg_value("--night")
    if forced:
        tonight = copy["nights"][int(forced) - 1]
    else:
        tonight = night_for(copy, now)

    image = None
    if oneshot:
        kind = oneshot
        not_before = datetime.datetime.fromisoformat(ONESHOTS[oneshot][1].replace("Z", "+00:00"))
        if now < not_before and not dry:
            raise SystemExit(f"{oneshot} is not due until {ONESHOTS[oneshot][1]}; not posting")
        if oneshot in GATED_ONESHOTS and not dry:
            try:
                why = live_refusal(get_json("/portal/sietch/tonight-public"))
            except Exception as e:
                why = f"tonight-public unreadable: {e}"
            if why:
                raise SystemExit(f"{oneshot}: {why}; not posting")
        body = oneshot_body(copy, oneshot)
        stamp = f"oneshot-{oneshot}"
        if oneshot == "launch":
            image = copy["discord"].get("launch_image")
    elif recap:
        kind = "recap"
        # 10:00 ET falls inside the night that began at 8 PM ET the evening before,
        # so the recap is about THAT night, and "tonight" is the one after it. The
        # morning after night 7 has no current night: recap night 7 and close.
        prev = tonight or night_for(copy, now - datetime.timedelta(days=1))
        if not prev:
            print("no recap: no night has run yet")
            return
        final = tonight is None
        nxt = copy["nights"][prev["n"]] if prev["n"] < 7 and not final else None
        try:
            rows = get_json("/portal/sietch/board").get("rows") or []
        except Exception as e:
            if not dry:
                raise SystemExit(f"board unreadable, not posting: {e}")
            print(f"(dry: board unreadable, {e})")
            rows = []
        body = recap_body(copy, prev, nxt, rows, final)
        stamp = "recap-final" if final else f"recap-{prev['key']}"
    else:
        kind = "night"
        if not tonight:
            print("no night tonight: outside the event window")
            return
        spots = []
        try:
            pub = get_json("/portal/sietch/tonight-public")
            why = live_refusal(pub)
            if why and not dry:
                raise SystemExit(f"{why}; not posting")
            if why:
                print(f"(dry: {why}; a real run would refuse)")
            pn = (pub.get("night") if isinstance(pub, dict) else None) or {}
            if pn.get("key") and pn["key"] != tonight["key"] and not forced:
                raise SystemExit(f"portal says night {pn.get('key')}, clock says {tonight['key']}; not posting")
            # tonight-public: {night: {n, key, ..., copy: {title, hook, ...}, spots: [{name, kind}]}}.
            # The server's copy wins over the file's when it is live.
            pc = pn.get("copy") if isinstance(pn.get("copy"), dict) else {}
            tonight = {**tonight, **{k: v for k, v in pc.items() if k in ("title", "hook", "how", "award_text", "runs_text") and v}}
            spots = [s.get("name") for s in (pn.get("spots") or []) if s.get("name")]
        except SystemExit:
            raise
        except Exception as e:
            if not dry:
                raise SystemExit(f"tonight-public unreadable, not posting: {e}")
            print(f"(dry: tonight-public unreadable, using the copy file: {e})")
        body = night_body(copy, tonight, spots)
        stamp = f"night-{tonight['key']}"

    lint(body)
    channel = channel_for(kind)
    if dry:
        where = channel or f"UNSET (set SIETCH_CHANNEL_{kind.upper().replace('-', '_')})"
        extra = f", image {image}" if image else ""
        print(f"--- DISCORD {stamp} -> {kind} channel {where} ({len(body)} chars, no pings{extra}) ---\n{body}\n")
        return
    if not channel:
        raise SystemExit(f"no channel id for {kind}; not posting")
    path = os.path.join(STAMP_DIR, stamp)
    if os.path.exists(path):
        print(f"already posted {stamp}")
        return
    os.makedirs(STAMP_DIR, exist_ok=True)
    post(body, channel, image)
    with open(path, "w") as fh:
        fh.write(now.isoformat() + "\n")


if __name__ == "__main__":
    main()
