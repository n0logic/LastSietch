#!/usr/bin/env python3
# Pays Amtal kill-streak bounties (ops/amtal-pvp-flag/BOUNTIES-SPEC.md §3, thresholds owner 2026-10-20).
#
# The telemetry API (/amtal/pvp) already folds the Amtal death rows into "collected" entries: a player on a streak
# of 5+ kills was killed by another player, who collects 2,500 spice per 5 kills of the ended streak. Each tick
# (systemd timer, 60 s) this reads that list and, once per entry:
#   1. voids it if the collector shares a guild or a base claim with the victim (no farming a guildmate's streak)
#   2. otherwise grants MelangeSpice to the collector through deliver-item.py (RMQ, online only, confirmed
#      by reading dune.items back). Offline collectors stay pending and are retried until PENDING_DAYS.
# Entries older than the first live tick are never paid, so turning this on pays nothing retroactively.
#
# Dry run unless /etc/lastsietch/amtal-streak-payouts-live exists. Kill switch: touch /etc/lastsietch/amtal-streak-payouts-disabled
import json, os, subprocess, time, urllib.request

STATE = "/var/lib/lastsietch-amtal-streak-payouts/state.json"
LIVE_FLAG = "/etc/lastsietch/amtal-streak-payouts-live"
KILL = "/etc/lastsietch/amtal-streak-payouts-disabled"
PVP_URL = "http://127.0.0.1:8078/amtal/pvp"
DELIVER = "/opt/lastsietch-rmq-bridge/deliver-item.py"
TEMPLATE = "MelangeSpice"
PENDING_DAYS = 14
# deliver-item.py exit codes
RC_OK, RC_USAGE, RC_OFFLINE, RC_PUBLISH_FAILED, RC_UNCONFIRMED = 0, 2, 3, 4, 5


def log(msg):
    print(msg, flush=True)


def sql(q):
    out = subprocess.run(["/root/dq.sh", "-At", "-F", "|", "-c", q], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return [l.split("|") for l in out.stdout.splitlines() if l and not l.startswith(("SET", "ERROR"))]


def load():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {}


def save(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def related(a, b):
    """True when two accounts share a guild or a base claim (any rank on the same totem)."""
    a, b = int(a), int(b)
    rows = sql("with c as (select account_id, player_controller_id as ctl from dune.player_state where account_id in (%d, %d)) "
               "select (select count(*) from dune.guild_members g1 join dune.guild_members g2 on g1.guild_id = g2.guild_id "
               "where g1.player_id in (select ctl from c where account_id = %d) and g2.player_id in (select ctl from c where account_id = %d)), "
               "(select count(*) from dune.permission_actor_rank p1 join dune.permission_actor_rank p2 on p1.permission_actor_id = p2.permission_actor_id "
               "where p1.player_id in (select ctl from c where account_id = %d) and p2.player_id in (select ctl from c where account_id = %d))"
               % (a, b, a, b, a, b))
    if not rows:
        raise RuntimeError("guild/claim check returned nothing")
    return any(int(n) > 0 for n in rows[0])


def deliver(entry, live):
    cmd = ["python3", DELIVER, "--account-id", str(entry["collector"]), "--template", TEMPLATE,
           "--quantity", str(entry["bounty"]), "--source", "grant", "--operator", "amtal-streak-payouts",
           "--correlation-id", entry["correlation_id"], "--json"]
    if live:
        cmd.append("--send")
    r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    tail = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode, tail[-1] if tail else ""


def main():
    if os.path.exists(KILL):
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    live = os.path.exists(LIVE_FLAG)
    st = load()
    now = time.time()
    # The pay-from mark is set by the first LIVE tick; dry runs never move it.
    if live and "start" not in st:
        st["start"] = now
        log("first live tick: paying streaks ended from now on")
    start = st.get("start", now)
    payouts = st.setdefault("payouts", {})

    with urllib.request.urlopen(PVP_URL, timeout=10) as r:
        board = json.load(r)
    for c in board.get("collected") or []:
        if not c.get("collector_account_id") or not c.get("bounty"):
            continue
        key = "%s-%s" % (c["victim_account_id"], c["occurred_epoch"])
        if key in payouts or c["occurred_epoch"] < start:
            continue
        payouts[key] = {"victim": c["victim_account_id"], "collector": c["collector_account_id"],
                        "streak": c["streak"], "bounty": c["bounty"], "ended_at": c["occurred_at"],
                        "seen": now, "attempts": 0, "status": "pending",
                        "correlation_id": "amtal-streak-%s" % key}
        log("new: %s ended %s's %d-kill streak, %d spice" % (c["collector_account_id"], c["victim_account_id"],
                                                             c["streak"], c["bounty"]))

    for key, p in payouts.items():
        if p["status"] != "pending":
            continue
        if now - p["seen"] > PENDING_DAYS * 86400:
            p["status"] = "expired"
            log("expired %s: collector never online to receive it" % key)
            continue
        if "related" not in p:
            p["related"] = related(p["victim"], p["collector"])
            if p["related"]:
                p["status"] = "void_related"
                log("void %s: collector shares a guild or base with the victim" % key)
                continue
        if not live:
            rc, out = deliver(p, False)
            log("DRY-RUN %s: would pay %d spice to %s (rc=%d %s)" % (key, p["bounty"], p["collector"], rc, out))
            continue
        p["attempts"] += 1
        rc, out = deliver(p, True)
        p["last_rc"], p["last_out"] = rc, out[:300]
        if rc == RC_OK:
            p["status"] = "paid"
            p["paid_at"] = now
            log("PAID %s: %d spice to %s" % (key, p["bounty"], p["collector"]))
        elif rc in (RC_OFFLINE, RC_PUBLISH_FAILED):
            log("retry later %s (rc=%d %s)" % (key, rc, out))
        else:
            # Published but not confirmed, or a usage error: never resend blind, staff check by hand.
            p["status"] = "needs_review"
            log("NEEDS REVIEW %s (rc=%d %s)" % (key, rc, out))
    save(st)


if __name__ == "__main__":
    main()
