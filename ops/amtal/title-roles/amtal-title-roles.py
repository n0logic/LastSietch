#!/usr/bin/env python3
# Mirrors the Amtal PvP titles (ops/amtal-pvp-flag/TITLES-DRAFT.md) onto Discord roles, via the Cielago bot.
#
# Titles come from the same routers.portal_amtal.pvp_titles the portal board uses, over the same relay read, so the
# board and Discord can never disagree. Holders are mapped to Discord through ls_account_links (active links only).
# Each run: make sure the four title roles exist (cosmetic: no permissions, not hoisted, not mentionable, below the
# bot's top role), then add and remove ONLY those four roles so each matches the computed holders.
#
# A failed or empty read changes nothing: a title is never stripped because the board could not be read.
# Runs under the lastsietch-admin venv with PYTHONPATH=/opt/lastsietch-admin and its environment (relay key). Timer: 15 min.
#   amtal-title-roles.py            dry run: print the plan, no writes
#   amtal-title-roles.py --apply    create missing roles, then sync holders
# Kill switch: touch /etc/lastsietch/amtal-title-roles-disabled
import asyncio, json, os, sqlite3, sys, time, urllib.error, urllib.request

GUILD = os.environ.get("LAST_SIETCH_GUILD_ID", "")
DB = os.environ.get("LASTSIETCH_DB_PATH", "/opt/lastsietch-admin/admin.db")
CIELAGO_ENV = "/opt/cielago/.env"
KILL = "/etc/lastsietch/amtal-title-roles-disabled"
MANAGE_ROLES = 1 << 28
ADMINISTRATOR = 1 << 3
# Discord role name -> colour. Names match the portal titles exactly.
ROLES = {
    "Sardaukar of the Week": 0xB8442C,
    "Kanly": 0x8E2F4F,
    "Headtaker": 0xD9822B,
    "Blooded": 0x9C6B3C,
}
REASON = "Amtal PvP title (portal board sync)"


def log(msg):
    print(msg, flush=True)


_TOKEN = []


def token():
    if _TOKEN:
        return _TOKEN[0]
    for line in open(CIELAGO_ENV):
        if line.startswith("DISCORD_BOT_TOKEN="):
            _TOKEN.append(line.split("=", 1)[1].strip().strip('"'))
            return _TOKEN[0]
    raise SystemExit("no Cielago token")


def api(method, path, body=None, reason=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bot " + token(), "Content-Type": "application/json",
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
                time.sleep(float(json.loads(e.read().decode() or "{}").get("retry_after", 2)) + 0.25)
                continue
            raise RuntimeError("%s %s: HTTP %d %s" % (method, path, e.code, e.read().decode()[:300]))


def members():
    out, after = [], "0"
    while True:
        page = api("GET", "guilds/%s/members?limit=1000&after=%s" % (GUILD, after))
        out += page or []
        if not page or len(page) < 1000:
            return out
        after = page[-1]["user"]["id"]


def read_titles():
    """{account_id(str): [title, ...]} from the live board, or None when it cannot be read."""
    from relay import call_relay
    from routers.portal_amtal import pvp_titles
    try:
        raw = asyncio.run(call_relay("/dune/telemetry/amtal-pvp"))
    except Exception as exc:  # noqa: BLE001
        log("board read failed: %s" % exc)
        return None
    if not isinstance(raw, dict) or raw.get("available") is False or not raw.get("season"):
        log("board unavailable or empty; changing nothing")
        return None
    held, _weekly = pvp_titles(raw)
    return {str(k): v for k, v in held.items()}


def links():
    """{account_id(str): {discord_id, ...}} for active links."""
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        rows = conn.execute("SELECT account_id, discord_id FROM ls_account_links WHERE revoked_at IS NULL").fetchall()
    finally:
        conn.close()
    out = {}
    for acct, did in rows:
        out.setdefault(str(acct), set()).add(str(did))
    return out


def plan(held, linked, role_ids, current):
    """Role changes as (op, discord_id, role_name). current = {discord_id: {role_name, ...}} for the managed roles."""
    want = {}
    for acct, titles in held.items():
        for did in linked.get(acct, ()):
            want.setdefault(did, set()).update(t for t in titles if t in role_ids)
    ops = []
    for did in sorted(set(want) | set(current)):
        have, need = current.get(did, set()), want.get(did, set())
        ops += [("add", did, t) for t in sorted(need - have)]
        ops += [("remove", did, t) for t in sorted(have - need)]
    return ops


def main():
    if os.path.exists(KILL):
        return 0
    apply = "--apply" in sys.argv
    held = read_titles()
    if held is None:
        return 0
    linked = links()

    roles = api("GET", "guilds/%s/roles" % GUILD)
    me = api("GET", "users/@me")
    mine = api("GET", "guilds/%s/members/%s" % (GUILD, me["id"]))
    by_id = {r["id"]: r for r in roles}
    perms = 0
    for rid in mine["roles"] + [GUILD]:
        perms |= int(by_id.get(rid, {}).get("permissions", "0"))
    top = max((by_id[r]["position"] for r in mine["roles"] if r in by_id), default=0)
    if not perms & (MANAGE_ROLES | ADMINISTRATOR):
        raise SystemExit("REFUSED: Cielago lacks Manage Roles")

    role_ids = {}
    for name, colour in ROLES.items():
        found = [r for r in roles if r["name"] == name]
        if len(found) > 1:
            raise SystemExit("REFUSED: more than one role named %r" % name)
        if found:
            role_ids[name] = found[0]["id"]
            if found[0]["position"] >= top:
                raise SystemExit("REFUSED: role %r sits at or above Cielago's top role" % name)
        elif apply:
            r = api("POST", "guilds/%s/roles" % GUILD, {"name": name, "color": colour, "hoist": False,
                                                        "mentionable": False, "permissions": "0"}, reason=REASON)
            role_ids[name] = r["id"]
            log("created role %r (%s)" % (name, r["id"]))
        else:
            role_ids[name] = "new:" + name
            log("DRY-RUN would create role %r" % name)

    by_role = {rid: name for name, rid in role_ids.items()}
    current = {}
    guild_ids = set()
    for m in members():
        uid = m["user"]["id"]
        guild_ids.add(uid)
        names = {by_role[r] for r in m["roles"] if r in by_role}
        if names:
            current[uid] = names
    ops = [op for op in plan(held, linked, role_ids, current) if op[1] in guild_ids]
    log("holders: %d accounts with titles, %d linked members with title roles now, %d changes"
        % (len(held), len(current), len(ops)))
    for op, did, name in ops:
        if not apply:
            log("DRY-RUN %s %s %s" % (op, name, did))
            continue
        method = "PUT" if op == "add" else "DELETE"
        api(method, "guilds/%s/members/%s/roles/%s" % (GUILD, did, role_ids[name]), reason=REASON)
        log("%s %s %s" % (op, name, did))
    return 0


if __name__ == "__main__":
    sys.exit(main())
