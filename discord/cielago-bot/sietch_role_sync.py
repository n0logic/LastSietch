#!/usr/bin/env python3
# Seven Nights of the Sietch: deliver the "Seven Nights Survivor" role bought at
# the Token Stall (Cielago bot, raw REST, the founder_role.py pattern).
#   sietch_role_sync.py --dry      permissions + hierarchy check, list the queue, no writes
#   sietch_role_sync.py            create the role if missing, PUT it on each queued buyer, ack
# Owner ruling 2026-10-24: name "Seven Nights Survivor", burnt orange, permanent
# keepsake (never removed after the event).
#
# Queue: GET /_internal/sietch/role-queue -> {count, orders:[{id, discord_id,
# role_key}]}, authenticated with X-Sietch-Role-Key = SIETCH_ROLE_KEY (its own
# key; lastsietch-admin checks LASTSIETCH_SIETCH_ROLE_KEY). An empty key disables the sync. Ack: POST .../{id}/ack with {ok: true} or
# {ok: false, error}; {ok: false} refunds the buyer's LS Tokens.
#
# Ack rules (PLAN §7): Discord 204 -> {ok: true}. A 404 with code 10007 (the buyer
# left the guild, Unknown Member) is the one definite failure -> {ok: false}, which
# refunds. Any other 404 (a deleted role, a wrong guild) is held, never refunded.
# Any other 4xx (403 is a role-hierarchy or permission problem on OUR
# side), a 5xx or a network error is NOT acked, so the order stays queued for the
# next tick; 429 is waited out here. Never ack a result we did not see.
import json, os, sys, time, urllib.request, urllib.error

GUILD = os.environ.get("LAST_SIETCH_GUILD_ID", "")
ROLE_NAME = "Seven Nights Survivor"
ROLE_COLOR = 0xCC5500  # burnt orange
ROLE_KEY = "seven_nights_survivor"
# Discord JSON error code for a member who is not in the guild. Every other 404
# (Unknown Role 10011, Unknown Guild 10004, ...) is our side and is held.
UNKNOWN_MEMBER = 10007
MANAGE_ROLES = 1 << 28
ADMINISTRATOR = 1 << 3
PORTAL = os.environ.get("SIETCH_PORTAL", "http://127.0.0.1:8078")
ENV = "/opt/cielago/.env"


def env_value(name, required=True):
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in open(ENV):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        if required:
            raise SystemExit(f"cannot read {ENV}")
        return None
    if required:
        raise SystemExit(f"no {name} in {ENV}")
    return None


class Transient(Exception):
    pass


def discord(method, path, body=None, reason=None):
    """Returns (status, json). 429 is retried here; any other HTTP error comes back
    as its status so the caller decides definite vs transient."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bot " + env_value("DISCORD_BOT_TOKEN"), "Content-Type": "application/json",
               "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"}
    if reason:
        headers["X-Audit-Log-Reason"] = reason
    while True:
        req = urllib.request.Request("https://discord.com/api/v10/" + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            if e.code == 429:
                time.sleep(float(json.loads(text or "{}").get("retry_after", 2)) + 0.25)
                continue
            try:
                code = (json.loads(text) or {}).get("code")
            except ValueError:
                code = None
            return e.code, {"message": text[:300], "code": code}
        except (urllib.error.URLError, TimeoutError) as e:
            raise Transient(str(e))


def must(status, body, what):
    if status >= 300:
        raise SystemExit(f"FAILED {what}: HTTP {status} {body}")
    return body


def portal(method, path, body=None):
    req = urllib.request.Request(PORTAL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "X-Sietch-Role-Key": env_value("SIETCH_ROLE_KEY") or ""})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def ack(order_id, ok, error=None):
    body = {"ok": True} if ok else {"ok": False, "error": error or "failed"}
    return portal("POST", f"/_internal/sietch/role-queue/{order_id}/ack", body)


def ensure_role(apply):
    roles = must(*discord("GET", f"guilds/{GUILD}/roles"), "list roles")
    me = must(*discord("GET", "users/@me"), "whoami")
    mine = must(*discord("GET", f"guilds/{GUILD}/members/{me['id']}"), "bot member")
    by_id = {r["id"]: r for r in roles}
    perms = 0
    for rid in mine["roles"] + [GUILD]:
        perms |= int(by_id.get(rid, {}).get("permissions", "0"))
    top = max((by_id[r]["position"] for r in mine["roles"] if r in by_id), default=0)
    can = bool(perms & (MANAGE_ROLES | ADMINISTRATOR))
    existing = [r for r in roles if r["name"] == ROLE_NAME]
    print(f"bot {me['username']}: manage_roles={can} top_role_position={top}")
    print(f"existing '{ROLE_NAME}' roles: {[(r['id'], r['position']) for r in existing]}")
    if not can:
        raise SystemExit("REFUSED: bot lacks Manage Roles")
    if len(existing) > 1:
        raise SystemExit(f"REFUSED: more than one '{ROLE_NAME}' role")
    if not existing:
        if not apply:
            print("role missing: a real run creates it")
            return None, top
        role = must(*discord("POST", f"guilds/{GUILD}/roles", {
            "name": ROLE_NAME, "color": ROLE_COLOR, "hoist": False, "mentionable": False, "permissions": "0"},
            reason="Seven Nights of the Sietch keepsake role (owner ruling 2026-10-24)"), "create role")
    else:
        role = existing[0]
    if role["position"] >= top:
        raise SystemExit(f"REFUSED: role position {role['position']} is not below the bot's top role {top}")
    return role, top


def main():
    apply = "--dry" not in sys.argv
    if not env_value("SIETCH_ROLE_KEY", required=False):
        # Self-disable: with no key the queue would only answer 401/503.
        print("SIETCH_ROLE_KEY is not set: role sync disabled, nothing done")
        return
    role, _ = ensure_role(apply)
    try:
        rows = (portal("GET", "/_internal/sietch/role-queue") or {}).get("orders") or []
    except Exception as e:
        raise SystemExit(f"role queue unreadable, nothing done: {e}")
    print(f"queued role orders: {len(rows)}")
    if not apply:
        for r in rows:
            print(f"  would deliver order {r.get('id')} to discord {r.get('discord_id')}")
        return
    done = failed = held = 0
    for r in rows:
        oid, uid = r.get("id"), str(r.get("discord_id") or "")
        if r.get("role_key") != ROLE_KEY:
            # A role this script does not know: leave it queued for a human.
            print(f"hold order {oid}: unknown role_key {r.get('role_key')!r}")
            held += 1
            continue
        if not oid or not uid.isdigit():
            # The queue already filters to real Discord ids, so this is unexpected:
            # hold it for a human rather than refund (or ack a missing id).
            print(f"hold order {oid!r}: no usable discord id")
            held += 1
            continue
        try:
            status, body = discord("PUT", f"guilds/{GUILD}/members/{uid}/roles/{role['id']}",
                                   reason=f"Seven Nights: Token Stall order {oid}")
        except Transient as e:
            print(f"hold order {oid}: {e}")
            held += 1
            continue
        if status == 204:
            ack(oid, True)
            done += 1
        elif status == 404 and (body or {}).get("code") == UNKNOWN_MEMBER:
            # The buyer left the guild: the one definite failure, which refunds.
            ack(oid, False, "discord_404_member")
            failed += 1
        else:
            print(f"hold order {oid}: Discord HTTP {status}")
            held += 1
    print(f"DONE: delivered {done}, failed {failed}, held for retry {held}")


if __name__ == "__main__":
    main()
