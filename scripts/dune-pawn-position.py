#!/usr/bin/env python3
"""Read a player's world position and PROVE it is current. Read-only.

Deployed to <game-host>:/root/dune-pawn-position.py.

WHY THIS EXISTS
---------------
`dune.actors.transform` for a player pawn is not live. The server rewrites the
row on a fixed ~60 second heartbeat, and the write is unconditional: measured
2026-07-27 across 5 online players over 244 seconds, every pawn bumped `serial`
4 times at 57-63 second intervals (median 62), including two players who did not
move at all. So `serial` is a save tick, not a change tick.

That makes a naive read wrong by up to a full heartbeat. Same measurement, the
displacement between consecutive writes:

    median 26 m | max 1413 m | 6 of 20 writes over 100 m | 3 over 500 m

A player standing in a base reads correctly. A player in an ornithopter can be a
kilometre and a half from where the row says, and nothing in the row admits it.
That is how a "which base am I standing at" lookup ends up naming a neighbour.

THE GUARD
---------
Because the heartbeat is unconditional, the row's age is exactly the time since
its last write, and a write is observable: `serial` changes. So rather than read
once and hope, this polls a cheap SELECT until it SEES the next write land, then
returns that sample. The position it reports is at most one poll interval old
(default 4 s) instead of up to 63.

The cost is waiting: up to one heartbeat, ~30 s on average. That is the honest
price of a position you can act on. Nothing here can make the game write sooner.

An OFFLINE player is a different case entirely and is not polled: their last
saved position is final, not stale, so it is returned immediately and labelled
as such.

freshness values:
  fresh               a write was observed; age_s <= poll interval
  offline_last_saved  player is offline; the position is their final resting one
  unconfirmed         no write seen inside max_wait. Do NOT treat the
                      coordinates as current. This is not necessarily a fault:
                      observed 2026-07-27 on a player sitting on map `Overland`,
                      who did not tick once in 75 s and then resumed the normal
                      ~60 s cadence after arriving on another map. Reading that
                      as "in transit, position meaningless" fits, and a
                      transition is exactly when you least want to trust a
                      coordinate. Other known causes: they logged out during the
                      wait, or `online_status` is stuck at Online for a player
                      who already left, which this server is known to do.
                      Retrying a few seconds later is the right response.

Usage:
  dune-pawn-position.py <account_id> [--poll N] [--max-wait N] [--bases K]

  --bases K  after confirming the position, also return the K nearest land
             claims, in the pawn's OWN dimension. Passing the dimension is not
             optional in dune-bases.py and this is why: PvE and PvP Hagga share
             a coordinate space, so a lookup that guesses can name the owner of
             a base in a world the player is not standing in.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASES = os.path.join(HERE, "dune-bases.py")

POLL_DEFAULT = 4
MAX_WAIT_DEFAULT = 75          # one heartbeat (63 s observed worst case) + slack

# One row, keyed on the pawn the player_state row points at. serial is the save
# tick we watch; everything else is what the caller actually wants.
POS_SQL = """
SELECT json_build_object(
         'account_id', ps.account_id,
         'character_name', ps.character_name,
         'online', ps.online_status = 'Online',
         'online_status', ps.online_status,
         'last_login', ps.last_login_time,
         'pawn_id', a.id,
         'serial', a.serial,
         'map', a.map,
         'dimension_index', a.dimension_index,
         'partition_id', a.partition_id,
         'x', round((((a.transform).location).x)::numeric, 1),
         'y', round((((a.transform).location).y)::numeric, 1),
         'z', round((((a.transform).location).z)::numeric, 1))
FROM dune.player_state ps
JOIN dune.actors a ON a.id = ps.player_pawn_id
WHERE ps.account_id = %(acct)s;
"""


def run_sql(sql, timeout=30):
    out = subprocess.run(["/root/dq.sh", "-tAc", sql],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        return None, (out.stderr or out.stdout).strip()[:300]
    text = (out.stdout or "").strip()
    # dq.sh can emit its session SET as a leading command tag. Harmless with -t,
    # but strip it defensively: a stray first line makes json.loads fail in a
    # way that reads like a database problem.
    if text.startswith("SET\n"):
        text = text[4:]
    return text.strip(), None


def read_pos(acct):
    raw, err = run_sql(POS_SQL.replace("%(acct)s", str(acct)))
    if err is not None:
        return None, err
    if not raw:
        return None, f"no pawn row for account {acct} (never spawned?)"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, f"bad json from db: {raw[:200]}"


def emit(obj):
    print(json.dumps(obj))


def main():
    args = sys.argv[1:]
    if not args:
        emit({"available": False,
              "error": "usage: dune-pawn-position.py <account_id> "
                       "[--poll N] [--max-wait N] [--bases K]"})
        return 2

    def opt(name, default):
        if name in args:
            try:
                return int(args[args.index(name) + 1])
            except (IndexError, ValueError):
                return None
        return default

    try:
        acct = int(args[0])
    except ValueError:
        emit({"available": False, "error": "account_id must be numeric"})
        return 2

    poll = opt("--poll", POLL_DEFAULT)
    max_wait = opt("--max-wait", MAX_WAIT_DEFAULT)
    want_bases = opt("--bases", 0)
    if None in (poll, max_wait, want_bases):
        emit({"available": False, "error": "--poll, --max-wait and --bases take integers"})
        return 2
    poll = max(1, min(poll, 30))
    max_wait = max(poll, min(max_wait, 300))
    want_bases = max(0, min(want_bases, 25))

    first, err = read_pos(acct)
    if err:
        emit({"available": False, "error": err})
        return 1

    # Offline is not stale. The pawn stopped moving when they logged out, so the
    # saved row IS the answer and waiting for a heartbeat that will never come
    # would just burn 75 seconds.
    if not first["online"]:
        out = {"available": True, "freshness": "offline_last_saved",
               "waited_s": 0, "position": first}
        if want_bases:
            out["bases"] = nearest(first, want_bases)
        emit(out)
        return 0

    started = time.monotonic()
    baseline = first["serial"]
    latest = first
    while time.monotonic() - started < max_wait:
        time.sleep(poll)
        cur, err = read_pos(acct)
        if err:
            emit({"available": False, "error": err,
                  "note": "position read failed while waiting for a save tick"})
            return 1
        latest = cur
        if cur["serial"] != baseline:
            waited = round(time.monotonic() - started, 1)
            out = {"available": True, "freshness": "fresh", "age_s_max": poll,
                   "waited_s": waited, "serial_from": baseline,
                   "serial_to": cur["serial"], "position": cur}
            if want_bases:
                out["bases"] = nearest(cur, want_bases)
            emit(out)
            return 0
        # An online player who goes offline mid-wait stops ticking. Say so
        # rather than sitting here until max_wait for a save that is not coming.
        if not cur["online"]:
            out = {"available": True, "freshness": "offline_last_saved",
                   "waited_s": round(time.monotonic() - started, 1),
                   "note": "player logged out during the wait",
                   "position": cur}
            if want_bases:
                out["bases"] = nearest(cur, want_bases)
            emit(out)
            return 0

    emit({"available": True, "freshness": "unconfirmed",
          "waited_s": round(time.monotonic() - started, 1),
          "serial": baseline,
          "note": "no save tick inside max_wait; the pawn heartbeat is ~60 s, so "
                  "this is abnormal. Do NOT treat these coordinates as current.",
          "position": latest})
    return 0


def nearest(pos, k):
    """Nearest claims at a CONFIRMED position, in the pawn's own dimension."""
    try:
        out = subprocess.run(
            [BASES, "near", pos["map"], str(pos["dimension_index"]),
             str(pos["x"]), str(pos["y"]), str(k)],
            capture_output=True, text=True, timeout=45)
        if out.returncode != 0:
            return {"available": False,
                    "error": (out.stderr or out.stdout).strip()[:200]}
        return json.loads(out.stdout or "{}")
    except Exception as exc:                      # never lose the position over this
        return {"available": False, "error": str(exc)[:200]}


if __name__ == "__main__":
    sys.exit(main())
