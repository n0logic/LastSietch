#!/usr/bin/env python3
"""Sandstorm-position cache builder (lastsietch-dune, root, read-only). v1.1 enhanced reader.

Runs on a short systemd timer. The expensive part -- the /proc/<pid>/mem heap walk
in dune-storm-ramread.py (~1-2 min per pod) -- is the SAME read-only attach the spice
reader uses. A full heap walk is only needed to FIND the ASandStormBase actors; once
found, each actor sits at a stable address for the pod's lifetime (the pre-spawned
"ghost" actors persist between storms and a storm transitions one of them active).

v1.1 mechanism (kills the ~1-2 min birth latency + the despawn "sweep tail"):
  - LEARN: the first heap scan inside an active window records EVERY instance address
    (active + inactive ghosts) via `dune-storm-ramread.py --emit-instances`, persisted
    per dim as {instances, instances_pid}.
  - CHEAP POLL: every tick thereafter fast-reads those known addresses (a few small
    preads, NO heap walk) -- even BETWEEN storms -- so the next storm's activation is
    caught at the next 30s tick regardless of when the cadence detector opens the
    window, and a despawn flips to inactive immediately.
  - FULL SCAN is now gated to: inside an active window AND the cheap poll found no
    active storm (bootstrap / a storm that is a new instance / stale addresses after a
    pod restart). The live box is still untouched between storms (no heap walk when no
    addresses are learned and no storm is up).
  - Instance addresses are keyed to the pid and dropped on a pod restart (which
    changes the pid and invalidates every heap address); the reader also re-validates
    the vptr at each address, so a stale/reused address never emits a position.

Writes /root/dune-storm-ramcache.json:
  {"dimensions": {"0": {"active": true, "x":.., "y":.., "radius":.., "heading":..,
                        "stage":.., "sector":.., "obj":.., "vptr":.., "pid":..,
                        "fast":.., "instances":[{obj,vptr}..], "instances_pid":..,
                        "active_since_utc":.., "scanned_utc":..},
                  "1": {"active": false, "instances":[..], "instances_pid":..,
                        "ended_utc":.., "scanned_utc":..}},
   "meta": {"last_poll_utc":.., "last_scan_utc":.., "scanned_dims":[..],
            "fast_dims":[..], "learned_dims":[..], "last_error":..}}

dune-sandstorm.py (the relay /dune/sandstorm producer) reads this cache and folds
the active storm per dim into dimensions[dim].{x,y,radius,heading,stage} alongside
its ETA fields -- cheap, no scan at request time. active_since_utc / ended_utc give
downstream (the portal overlay) a precise lifecycle so it need not guess from timing.

READ-ONLY end to end: a kubectl-logs cadence read (via dune-sandstorm.py) + read-only
/proc/<pid>/mem reads (via dune-storm-ramread.py). Never writes the DB, never touches
the game pods, never PTRACE-stops (so it never pauses the game).
"""
import json
import os
import subprocess
import sys
import fcntl
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RAMREAD = os.path.join(HERE, "dune-storm-ramread.py")
SANDSTORM = os.path.join(HERE, "dune-sandstorm.py")

CACHE = "/root/dune-storm-ramcache.json"
LOCK = "/run/lastsietch-storm-ramcache.lock"

# A storm is "alive" for roughly its sweep across the map. Live-validated track
# (2026-06-27): a PvE storm crossed from the west edge to mid-map (G5) in ~6 min,
# heading ENE; a full edge-to-edge sweep is ~12-15 min. Gate scans to spawn + this
# window (over-estimate is safe: a scan after despawn just returns no active storm
# and writes inactive). Outside the window we never scan.
ACTIVE_WINDOW_SECS = 1200          # 20 min after spawn = generous active window
SCAN_TIMEOUT = 600                 # a pod-scoped heap walk can take a couple minutes
SANDSTORM_TIMEOUT = 60             # the cheap cadence read (kubectl logs tail)
FAST_TIMEOUT = 30                  # the single-address fast re-read is quick
# Bootstrap-prime: the pre-spawned ghost actors exist BETWEEN storms, so an
# UNPRIMED dim (no learned addresses -- e.g. right after a pod restart / this
# deploy, before its first storm window) is primed by one full scan so the cheap
# poll can catch its very first storm's birth too, not just later ones. Bounded by
# this backoff so a transient empty scan never loops; once primed it never re-fires.
LEARN_BACKOFF_SECS = 600           # >= scan cost; ~one bootstrap scan per unprimed dim


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_secs(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


def spawn_times() -> dict[int, str]:
    """{dim: last_spawn_utc} from the cheap cadence producer (dune-sandstorm.py log
    tail). Empty on any failure -> no active window -> no scan this tick."""
    try:
        out = subprocess.run([sys.executable, SANDSTORM], capture_output=True,
                             text=True, timeout=SANDSTORM_TIMEOUT)
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}
    res = {}
    for k, v in (data.get("dimensions") or {}).items():
        ls = v.get("last_spawn_utc") if isinstance(v, dict) else None
        if ls:
            try:
                res[int(k)] = ls
            except ValueError:
                pass
    return res


def list_pids() -> dict[int, int]:
    """{dim: pid} for the DD pods (cheap; no heap scan). Lets us scope every scan
    to the single active dim's pod and re-validate a cached pid."""
    try:
        out = subprocess.run([sys.executable, RAMREAD, "--list-pids"],
                             capture_output=True, text=True, timeout=30)
        raw = json.loads(out.stdout).get("pids", {})
        return {int(k): int(v) for k, v in raw.items()}
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}


def _first_storm(out_text: str, dim: int) -> dict | None:
    try:
        procs = json.loads(out_text).get("processes", [])
    except ValueError:
        return None
    for p in procs:
        if p.get("dimension") == dim:
            storms = p.get("storms") or []
            return storms[0] if storms else None
    return None


def fast_read(pid: int, obj: str, vptr: str, dim: int) -> dict | None:
    """v1.0.1 FAST PATH: re-read the cached actor at `obj` (the reader verifies the
    vptr + re-validates the pid). Returns the active storm dict, or None if it
    failed / despawned / went stale -> caller falls back to a pod-scoped scan."""
    try:
        out = subprocess.run([sys.executable, RAMREAD, f"--pid={pid}",
                              f"--read-obj={obj}", f"--obj-vptr={vptr}"],
                             capture_output=True, text=True, timeout=FAST_TIMEOUT)
    except (subprocess.SubprocessError, OSError):
        return None
    return _first_storm(out.stdout, dim)


def pod_scan(pid: int, dim: int) -> dict | None:
    """Full heap scan scoped to ONE DD pod (--pid) -> active storm dict or None.
    Used to first-find the actor and to confirm a despawn. Half the both-pods time."""
    try:
        out = subprocess.run([sys.executable, RAMREAD, f"--pid={pid}"],
                             capture_output=True, text=True, timeout=SCAN_TIMEOUT)
    except (subprocess.SubprocessError, OSError):
        return None
    return _first_storm(out.stdout, dim)


def scan_instances(pid: int, dim: int) -> list[dict] | None:
    """v1.1 LEARN PATH: full heap scan that returns EVERY ASandStormBase instance
    (the active storm AND the pre-spawned inactive ghosts) with obj+vptr. The cache
    persists these addresses so subsequent ticks cheap-poll them instead of heap
    walking (kills the ~1-2 min birth latency). None on failure -> caller keeps its
    previous instance list."""
    try:
        out = subprocess.run([sys.executable, RAMREAD, f"--pid={pid}",
                              "--emit-instances"], capture_output=True, text=True,
                             timeout=SCAN_TIMEOUT)
        procs = json.loads(out.stdout).get("processes", [])
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    for p in procs:
        if p.get("dimension") == dim:
            insts = p.get("instances")
            return insts if isinstance(insts, list) else None
    return None


def poll_instances(pid: int, instances: list[dict], dim: int) -> dict | None:
    """v1.1 CHEAP PATH: fast single-address re-read of each KNOWN instance address
    (no heap walk). Returns the first instance that reads ACTIVE, or None if none
    are active (storm ended / not yet spawned) or every address went stale. A few
    small preads per tick -> safe to run every tick, even between storms, so the
    next storm's activation is caught at the next 30s tick, not after a full scan."""
    for inst in instances:
        obj, vptr = inst.get("obj"), inst.get("vptr")
        if not obj or not vptr:
            continue
        st = fast_read(pid, obj, vptr, dim)   # returns the storm only if ACTIVE
        if st and st.get("active"):
            return st
    return None


def _instance_addrs(instances: list[dict] | None) -> list[dict]:
    """Reduce a full instance list to just the {obj, vptr} we re-read each tick."""
    out = []
    for inst in (instances or []):
        obj, vptr = inst.get("obj"), inst.get("vptr")
        if obj and vptr:
            out.append({"obj": obj, "vptr": vptr})
    return out


def load_cache() -> dict:
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_cache(cache: dict) -> None:
    tmp = f"{CACHE}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CACHE)


def main() -> int:
    # Single-flight: a slow scan must not overlap the next timer tick.
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0    # a previous (scanning) run still holds the lock; skip this tick

    cache = load_cache()
    dims = cache.get("dimensions") or {}
    if not isinstance(dims, dict):
        dims = {}

    spawns = spawn_times()
    pids = list_pids()                 # cheap dim->pid map every tick
    now = _now()

    scanned_dims: list[int] = []       # dims that paid a full heap walk this tick
    fast_dims: list[int] = []          # dims cheap-polled from learned addresses
    learned_dims: list[int] = []       # dims whose instance list was (re)learned

    for d in (0, 1):
        prev = dims.get(str(d)) or {}
        pid = pids.get(d)
        if pid is None:
            # Pod gone (restart in progress / partition down). Mark inactive and
            # drop the instance list (a pod restart changes the pid AND invalidates
            # every heap address). Preserve any prior ended_utc.
            entry = {"active": False, "scanned_utc": now}
            if prev.get("active"):
                entry["ended_utc"] = now
            elif prev.get("ended_utc"):
                entry["ended_utc"] = prev["ended_utc"]
            dims[str(d)] = entry
            continue

        in_window = _age_secs(spawns.get(d)) < ACTIVE_WINDOW_SECS
        # Learned instance addresses are only valid for the SAME pid.
        cached = (_instance_addrs(prev.get("instances"))
                  if prev.get("instances_pid") == pid else [])
        learned = cached

        st = None
        used_fast = False
        # CHEAP PATH: poll the known instance addresses (a few small preads, no heap
        # walk). Runs every tick we have addresses -- even BETWEEN storms -- so the
        # next storm's activation is caught at the next 30s tick regardless of when
        # the cadence detector opens the window (kills the birth latency), and a
        # despawn flips to inactive immediately (kills the sweep tail).
        if cached:
            st = poll_instances(pid, cached, d)
            used_fast = st is not None
            fast_dims.append(d)

        # FULL HEAP SCAN (learns every instance address) fires when either:
        #  - inside the active window and the cheap poll found no active storm
        #    (refresh / a storm that is a NEW instance / stale addresses), OR
        #  - the dim is UNPRIMED (no learned addresses) and the learn backoff has
        #    elapsed -- a BOUNDED bootstrap so a freshly restarted/deployed pod is
        #    primed for its FIRST storm too (the cheap poll needs learned addresses
        #    to catch a birth). Once primed, `cached` is non-empty so this never
        #    re-fires; between primed storms the box stays untouched (no heap walk).
        learn_attempt = prev.get("learn_attempt_utc")
        want_bootstrap = (not cached) and (_age_secs(learn_attempt) > LEARN_BACKOFF_SECS)
        if (in_window and st is None) or want_bootstrap:
            insts = scan_instances(pid, d)
            if insts is not None:
                learned = _instance_addrs(insts)
                st = next((i for i in insts if i.get("active")), None) or st
                learned_dims.append(d)
            elif in_window:
                # Reader lacks --emit-instances (deploy gap) -> legacy active-only
                # scan so the cache keeps working regardless of deploy order.
                st = pod_scan(pid, d)
            scanned_dims.append(d)
            learn_attempt = now

        if st and st.get("active"):
            active_since = (prev.get("active_since_utc")
                            if prev.get("active") and prev.get("active_since_utc")
                            else now)
            dims[str(d)] = {
                "active": True,
                "x": st.get("center_x"), "y": st.get("center_y"),
                "radius": st.get("radius"),          # +0x59C effect, linear cm
                "heading": st.get("heading_yaw"),    # quat yaw deg
                "stage": st.get("stage"),
                "sector": st.get("sector"),
                "obj": st.get("obj"), "vptr": st.get("vptr"), "pid": pid,
                "fast": used_fast,
                "instances": learned, "instances_pid": pid,
                "active_since_utc": active_since,
                "learn_attempt_utc": learn_attempt,
                "scanned_utc": now,
            }
        else:
            # Inactive. Keep the learned instance list so the NEXT storm cheap-polls
            # instead of heap walking. Stamp ended_utc on the active->inactive edge.
            entry = {"active": False, "scanned_utc": now,
                     "instances": learned, "instances_pid": pid,
                     "learn_attempt_utc": learn_attempt}
            if prev.get("active"):
                entry["ended_utc"] = now
            elif prev.get("ended_utc"):
                entry["ended_utc"] = prev["ended_utc"]
            dims[str(d)] = entry

    cache["dimensions"] = dims
    meta = cache.setdefault("meta", {})
    meta["last_poll_utc"] = now
    meta["scanned_dims"] = scanned_dims
    meta["fast_dims"] = fast_dims
    meta["learned_dims"] = learned_dims
    meta["spawn_times"] = {str(d): s for d, s in spawns.items()}
    if scanned_dims:
        meta["last_scan_utc"] = now
        meta.pop("last_error", None)
    write_cache(cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
