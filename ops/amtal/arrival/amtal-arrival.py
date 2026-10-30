#!/usr/bin/env python3
# Places characters arriving on Amtal (partition 33) from another map at an Amtal base they OWN (claim rank 1;
# member rights do not count, Medic 2026-10-06), or, if they own none, at their faction's safe area: Harkonnen -> Riftwatch, Atreides -> Helius Gate, anyone else -> coin flip
# (Medic, 2026-10-06).
#
# An "arrival" is a character whose last map seen online was not Amtal and who is now fully in the world on
# Amtal. A relog on Amtal is not an arrival (the last map is still Amtal), so it can't be used to escape a fight.
# Each tick (systemd timer, 3 s; was 15 s until 2026-10-18):
#   1. read every online character's current map from the DB; a change to Amtal from another map (the overmap
#      in between is ignored) marks the character pending
#   2. read Amtal's log since the last tick for "LoadingInvulnerability removed for player ... (FLS: <hex>)",
#      the moment the player is in control (a command sent during the loading screen is dropped)
#   3. for each such pending character: skip if still in the opening tutorial or if they flew/drove in (below),
#      else teleport
#
# Vehicle arrivals are NOT moved (n0logic 2026-10-20): there is no admin verb to move a vehicle, so teleporting the
# pilot strands their thopter at the entry slot where anyone can destroy it. Amtal's log names the vehicle on
# arrival: "TravelingActorsPersistenceIdsChanged ... (pawn=<pawn id>, vehicle=<vehicle id>" (vehicle=NONE on foot),
# except that a thopter stored in the vehicle backup tool is named there too (2026-10-22, see the check below).
#
# Dry run unless /etc/lastsietch/amtal-arrival-live exists; the teleport itself also needs LASTSIETCH_SERVERCMD_ENABLED=1,
# which only this script sets, for this one verb. Kill switch: touch /etc/lastsietch/amtal-arrival-disabled
import json, os, random, re, subprocess
from datetime import datetime, timezone

NS = os.environ.get("LASTSIETCH_GAME_NAMESPACE", "")
POD = os.environ.get("LASTSIETCH_GAME_BATTLEGROUP", "") + "-sg-survival-1-pod-33"
AMTAL = 33
STATE = "/var/lib/lastsietch-amtal-arrival/state.json"
LIVE_FLAG = "/etc/lastsietch/amtal-arrival-live"
KILL = "/etc/lastsietch/amtal-arrival-disabled"
CMD = "/root/dune-server-command.py"
INTRO_NODE = "DA_MQ_AssassinsHandbook.Intro.FindGreatHouseRepresentatives.EnterHarkoVillage"
# Standing spots chosen in game on 2026-10-06 (n0logic at Riftwatch, Medic at Helius Gate): open floor inside each
# safe area near the CHOAM terminal. These are pawn origins, so they are sent as TeleportToExact (no surface snap;
# the snapping version put arrivals on the roof or on top of the terminal).
SAFE = {
    "Riftwatch": (271083.9, -25643.1, 9273.7),
    "Helius Gate": (-39618.1, -252547.5, 22954.7),
}
FACTION_DEST = {2: "Riftwatch", 1: "Helius Gate"}  # 2 Harkonnen, 1 Atreides
SLACK = 120  # seconds between the in-world log line and the DB map change
VEHICLE_NEAR = 5000  # cm: a backup-tool vehicle this close to the arriving pawn is taken as flown in
BASE_Z_OFFSET = 150  # land just above the totem, inside the claim
VEHICLE = re.compile(r"^\[(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d):\d{3}\].*TravelingActorsPersistenceIdsChanged.*\(pawn=(\d+), vehicle=(\d+)")
EVENT = re.compile(r"^\[(\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d):\d{3}\].*LoadingInvulnerability removed for player .* \(FLS: ([0-9A-F]{16})\)")


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
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE)


def main():
    if os.path.exists(KILL):
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    live = os.path.exists(LIVE_FLAG)
    st = load()
    players = st.setdefault("players", {})  # fls -> {"cur": partition, "pending": bool, "pid": player_state id, "ctl": controller id, "pawn": pawn id}

    rows = sql("select a.\"user\", p.id, p.player_controller_id, coalesce(w.partition_id, -1), w.map, p.online_status::text, "
               "coalesce(p.previous_server_partition_id, -1), coalesce(p.player_pawn_id, 0) from dune.player_state p join dune.accounts a on a.id = p.account_id "
               "left join dune.world_partition w on w.server_id = p.server_id")
    first_run = not players
    now = datetime.now(timezone.utc).timestamp()
    for fls, pid, ctl, part, mp, status, prev, pawn in rows:
        part, prev = int(part), int(prev)
        rec = players.get(fls)
        if rec is not None:
            rec["pawn"] = int(pawn or 0)
        if rec is None:
            # Seed: where they are now if online on a real map, else the last partition the DB remembers.
            seed = part if (status == "Online" and mp != "Overmap" and part >= 0) else prev
            players[fls] = rec = {"cur": seed, "pending": False, "pid": int(pid), "ctl": int(ctl or 0), "pawn": int(pawn or 0)}
            if first_run or seed == AMTAL:
                continue
        if status != "Online" or mp == "Overmap" or part < 0:
            continue
        if part != rec["cur"]:
            if part == AMTAL:
                rec["pending"] = True
                rec["pending_since"] = now
                log("pending: %s arrived on Amtal from partition %s" % (pid, rec["cur"]))
            else:
                rec["pending"] = False
            rec["cur"] = part

    # In-world events can land in the log a tick before or after the DB shows the map change, so keep the latest
    # event time per player and match it to a pending arrival within SLACK seconds either way.
    since = st.get("since", "120s")
    logs = subprocess.run(["kubectl", "-n", NS, "logs", POD, "--since-time" if "T" in since else "--since", since],
                          capture_output=True, text=True).stdout
    st["since"] = datetime.fromtimestamp(now - 5, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inworld = st.setdefault("inworld", {})
    vehicles = st.setdefault("vehicles", {})  # pawn id -> [log time, vehicle id] of the latest vehicle arrival
    for line in logs.splitlines():
        m = EVENT.match(line)
        if m:
            t = datetime.strptime(m.group(1), "%Y.%m.%d-%H.%M.%S").replace(tzinfo=timezone.utc).timestamp()
            inworld[m.group(2)] = max(t, inworld.get(m.group(2), 0))
            continue
        m = VEHICLE.match(line)
        if m:
            t = datetime.strptime(m.group(1), "%Y.%m.%d-%H.%M.%S").replace(tzinfo=timezone.utc).timestamp()
            if t >= vehicles.get(m.group(2), [0])[0]:
                vehicles[m.group(2)] = [t, m.group(3)]
    for fls in [f for f, t in inworld.items() if t < now - 3600]:
        del inworld[fls]
    for pawn in [p for p, v in vehicles.items() if v[0] < now - 3600]:
        del vehicles[pawn]
    for fls, rec in players.items():
        if not rec.get("pending"):
            continue
        if now - rec.get("pending_since", now) > 900:
            rec["pending"] = False
            log("expired: %s never showed as in-world on Amtal" % rec["pid"])
            continue
        if inworld.get(fls, 0) < rec.get("pending_since", now) - SLACK:
            continue
        rec["pending"] = False
        pid, ctl = rec["pid"], rec.get("ctl", 0)
        done = sql("select count(*) from dune.journey_story_node where character_id = %d and story_node_id = '%s' "
                   "and complete_condition_state = 'true'::jsonb" % (pid, INTRO_NODE))
        if not done or done[0][0] == "0":
            log("skip %s (%s): opening tutorial not complete" % (pid, fls))
            continue
        veh = vehicles.get(str(rec.get("pawn", 0)))
        if veh and abs(veh[0] - inworld.get(fls, 0)) <= SLACK:
            # The vehicle backup tool's thopter is named on every arrival, even when stored and the player walked in.
            # A stored one keeps the actor row of wherever it was last deployed, so it counts as flown in only while
            # that row sits on the same partition as the pawn and within VEHICLE_NEAR of it.
            stored = sql("select v.partition_id, p.partition_id, sqrt(power(((v.transform).location).x - "
                         "((p.transform).location).x, 2) + power(((v.transform).location).y - ((p.transform).location).y, 2)) "
                         "from dune.backup_vehicles b join dune.actors v on v.id = b.vehicle_id "
                         "join dune.actors p on p.id = %d where b.vehicle_id = %d and b.character_id = %d"
                         % (rec.get("pawn", 0), int(veh[1]), pid))
            if stored and (stored[0][0] != stored[0][1] or float(stored[0][2]) > VEHICLE_NEAR):
                log("%s (%s): vehicle %s is their backup-tool vehicle, away from them (partition %s, %.0f), "
                    "treated as on foot" % (pid, fls, veh[1], stored[0][0], float(stored[0][2])))
            else:
                log("skip %s (%s): arrived in vehicle %s, not moved" % (pid, fls, veh[1]))
                continue
        # An Amtal base they own (rank 1 only; guild bases will need an explicit marking first), else the safe area.
        base = sql("select ((a.transform).location).x, ((a.transform).location).y, ((a.transform).location).z, t.id "
                   "from dune.permission_actor_rank par join dune.totems t on t.id = par.permission_actor_id "
                   "join dune.actors a on a.id = t.id where par.player_id = %d and par.rank = 1 and a.partition_id = %d "
                   "order by t.id limit 1" % (ctl, AMTAL))
        if base:
            x, y, z = float(base[0][0]), float(base[0][1]), float(base[0][2]) + BASE_Z_OFFSET
            dest = "own base (totem %s)" % base[0][3]
            exact = []  # a base has no surveyed spot: let the game snap to the nearest floor by the totem
            fid = -1
        else:
            fac = sql("select faction_id from dune.player_faction where actor_id = %d" % ctl)
            fid = int(fac[0][0]) if fac and fac[0][0] else 3
            dest = FACTION_DEST.get(fid) or random.choice(sorted(SAFE))
            x, y, z = SAFE[dest]
            exact = ["--exact"]
        log("%s %s (%s) faction %d -> %s (%d, %d, %d)" % ("TELEPORT" if live else "DRY-RUN would teleport", pid, fls, fid, dest, x, y, z))
        if not live:
            continue
        env = dict(os.environ, LASTSIETCH_SERVERCMD_ENABLED="1")
        r = subprocess.run(["python3", CMD, "--operator", "amtal-arrival", "--send", "teleport", "--player-id", fls,
                            "--x", str(x), "--y", str(y), "--z", str(z)] + exact, capture_output=True, text=True, env=env)
        tail = (r.stdout + r.stderr).strip().splitlines()
        log("  teleport rc=%d %s" % (r.returncode, tail[-1] if tail else ""))
    save(st)


if __name__ == "__main__":
    main()
