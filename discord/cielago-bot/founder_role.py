#!/usr/bin/env python3
# Founder role for every current (human) member of the Last Sietch guild, via the Cielago bot (raw REST).
#   founder_role.py --dry          permissions check + member count, no writes
#   founder_role.py --apply        create the role if missing, then add it to every current human member
# Snapshot of who got it: /opt/cielago/founder-role-snapshot.json (resumable: members already holding it are skipped).
import json, os, sys, time, urllib.request, urllib.error

GUILD = os.environ.get("LAST_SIETCH_GUILD_ID", "")
ROLE_NAME = "Founder"
ROLE_COLOR = 0x3A7BD5
SNAP = "/opt/cielago/founder-role-snapshot.json"
MANAGE_ROLES = 1 << 28
ADMINISTRATOR = 1 << 3


def token():
    for line in open("/opt/cielago/.env"):
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no token")


TOK = token()


def api(method, path, body=None, reason=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bot " + TOK, "Content-Type": "application/json",
               "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"}
    if reason:
        headers["X-Audit-Log-Reason"] = reason
    while True:
        req = urllib.request.Request("https://discord.com/api/v10/" + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = json.loads(e.read().decode() or "{}").get("retry_after", 2)
                time.sleep(float(wait) + 0.25)
                continue
            raise SystemExit(f"FAILED {method} {path}: HTTP {e.code} {e.read().decode()[:300]}")


def members():
    out, after = [], "0"
    while True:
        page = api("GET", f"guilds/{GUILD}/members?limit=1000&after={after}")
        if not page:
            return out
        out += page
        after = page[-1]["user"]["id"]
        if len(page) < 1000:
            return out


def main():
    apply = "--apply" in sys.argv
    roles = api("GET", f"guilds/{GUILD}/roles")
    me = api("GET", "users/@me")
    my_member = api("GET", f"guilds/{GUILD}/members/{me['id']}")
    by_id = {r["id"]: r for r in roles}
    perms = 0
    for rid in my_member["roles"] + [GUILD]:
        perms |= int(by_id.get(rid, {}).get("permissions", "0"))
    top = max((by_id[r]["position"] for r in my_member["roles"] if r in by_id), default=0)
    print(f"bot {me['username']}: manage_roles={bool(perms & (MANAGE_ROLES | ADMINISTRATOR))} top_role_position={top}")
    existing = [r for r in roles if r["name"] == ROLE_NAME]
    print(f"existing '{ROLE_NAME}' roles: {[(r['id'], r['position']) for r in existing]}")
    mem = members()
    humans = [m for m in mem if not m["user"].get("bot")]
    print(f"members: {len(mem)} total, {len(humans)} human, {len(mem) - len(humans)} bots")
    if not apply:
        return
    if not perms & (MANAGE_ROLES | ADMINISTRATOR):
        raise SystemExit("REFUSED: bot lacks Manage Roles")
    if len(existing) > 1:
        raise SystemExit("REFUSED: more than one existing Founder role")
    role = existing[0] if existing else api("POST", f"guilds/{GUILD}/roles", {
        "name": ROLE_NAME, "color": ROLE_COLOR, "hoist": False, "mentionable": False, "permissions": "0"},
        reason="Founder role for members who joined before the Facebook page (owner request 2026-09-29)")
    if role["position"] >= top:
        raise SystemExit(f"REFUSED: role position {role['position']} is not below the bot's top role {top}")
    print(f"role {role['id']} position {role['position']}")
    try:
        snap = json.load(open(SNAP))
    except FileNotFoundError:
        snap = {"role_id": role["id"], "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "members": [m["user"]["id"] for m in humans], "added": []}
    todo = [uid for uid in snap["members"] if uid not in snap["added"]]
    have = {m["user"]["id"] for m in humans if role["id"] in m["roles"]}
    for n, uid in enumerate(todo, 1):
        if uid not in have:
            try:
                api("PUT", f"guilds/{GUILD}/members/{uid}/roles/{role['id']}",
                    reason="Founder: member before the Facebook launch")
            except SystemExit as e:
                if "HTTP 404" in str(e):  # left the server since the snapshot
                    print(f"skip {uid}: no longer a member")
                else:
                    raise
        snap["added"].append(uid)
        if n % 25 == 0:
            json.dump(snap, open(SNAP, "w"))
            print(f"{n}/{len(todo)}")
    json.dump(snap, open(SNAP, "w"))
    final = [m for m in members() if not m["user"].get("bot")]
    holding = sum(1 for m in final if role["id"] in m["roles"])
    print(f"DONE: role {role['id']}; snapshot {len(snap['members'])}; now holding {holding} of {len(final)} human members")


if __name__ == "__main__":
    main()
