#!/usr/bin/env python3
"""Read-only live sandstorm-position reader (branch A).

Scans the Dune dedicated-server process memory (/proc/<pid>/mem, READ-ONLY, no
ptrace-stop) for ASandStormBase actor instances and reads each ACTIVE storm's
world center, radius, heading, and stage, so the public DD map can draw a MOVING
storm circle (consensus geometry, #5). Spatial complement to dune-sandstorm.py
(which parses the spawn-cadence ETA from the pod logs).

Semantics: DunePakRE/findings/SANDSTORM-SEMANTICS-RE-2026-06-26.md +
STORM-OFFSET-MAP (re-storm-server, build 2007976):
  - ASandStormBase is a MOVING entity. ACTIVE = u8 @ +0x1E0 == 3 (phase
    fully-active); the MovementComponent (+0x458) drives the root, so the center
    moves sample-to-sample.
  - Coriolis (ACoriolisBase) is a scheduled GLOBAL event -> countdown banner, NOT
    a circle; NOT this reader's target.

GEOMETRY (v1): single moving CIRCLE = center (Root+0x320) + radius (+0x59C, stored
LINEAR in cm -- the overlap test squares it, so read it DIRECTLY, no sqrt). The
swept "band" is a v1.1 predicted-path upgrade, not emitted here.

The reader emits ALL radius/heading CANDIDATES so QA/in-game picks the true visual
circle + travel direction; it does not hardcode the choice:
  - radius candidates: +0x59C effect(cm) / +0x5FC fade(m=9000) / +0x600 cull(m=10000)
  - heading candidates: velocity vector +0x458 (primary, true travel dir) /
                        root quat +0x300 (fallback)

SAFE: opens /proc/<pid>/mem O_RDONLY and preads in chunks. Never writes, never
PTRACE_ATTACHes (so it never pauses the game), never touches the DB or game state.
Same read-only attach the spice reader uses on the live box.

Usage:
  sudo python3 dune-storm-ramread.py            # JSON: active storms per process
  sudo python3 dune-storm-ramread.py --verify   # human-readable, dumps every hit
  sudo python3 dune-storm-ramread.py --all      # (verify) include inactive instances
  sudo python3 dune-storm-ramread.py --pid=N --emit-instances
                                                # JSON: EVERY instance (active + the
                                                # inactive ghosts) with obj+vptr, so the
                                                # cache learns the persistent actor
                                                # addresses and cheap-polls them (v1.1)
"""
import json
import math
import os
import re
import struct
import subprocess
import sys

BINNAME = "DuneSandboxServer-Linux-Shipping"

# --- ASandStormBase vptr (build 2007976) -------------------------------------
# RTTI self-location (patch-robust) is tried first: _ZTS "14ASandStormBase"
# ("ASandStormBase" = 14 chars). FALLBACK is the build-specific vptr VA
# (module-base-relative; PIE base 0 so file_VA == module offset), double-confirmed
# by re-storm-server. --verify prints both so we can confirm they agree.
MANGLED = {"storm": b"14ASandStormBase"}
FALLBACK_VPTR = {"storm": 0x15A51AB0}

# --- ASandStormBase offsets (STORM-OFFSET-MAP, re-storm-server) ---------------
OFF_ROOT         = 0x240    # AActor RootComponent (USceneComponent*)
# Within the RootComponent's ComponentToWorld FTransform:
COMP_ROTATION    = 0x300    # FQuat Rotation (X,Y,Z,W doubles) -> fallback heading
COMP_TRANSLATION = 0x320    # FVector Translation (X,Y,Z doubles) = storm CENTER
# Within the actor object:
OFF_PHASE        = 0x1E0    # u8 phase enum (0=none .. 3=fully-active). ACTIVE == 3.
OFF_STAGE        = 0x368    # int32 current-zone / stage index (1:1 with 5 stages)
OFF_VELOCITY     = 0x458    # MovementComponent velocity FVector (primary heading)
OFF_RADIUS_EFFECT = 0x59C   # float damage radius, LINEAR cm (read directly, no sqrt)
OFF_RADIUS_FADE   = 0x5FC   # float visual-fade distance (m; ~9000) -- candidate only
OFF_RADIUS_CULL   = 0x600   # float net-cull distance   (m; ~10000) -- candidate only
OBJ_SIZE         = 0x618    # ASandStormBase instance size (sanity bound)

ACTIVE_PHASE = 3            # u8 @ +0x1E0 value that means "fully-active storm"

# --- DD calibration (matches map_model deep-desert cal: origin -1270000, span
# 2438400 -> max 1168400). World units are cm. ---------------------------------
MIN, MAX = -1270000.0, 1168400.0
SECTOR = (MAX - MIN) / 9.0
CHUNK = 64 * 1024 * 1024
_RO_CHUNK = 16 * 1024 * 1024

# PartitionIndex -> DB dimension_index (canonical): pod-8 = PvE = dim 0,
# pod-31 = PvP = dim 1.
PARTITION_DIM = {8: 0, 31: 1}


def sector_for(x: float, y: float) -> str:
    col = max(1, min(9, int((x - MIN) / SECTOR) + 1))
    row = max(0, min(8, int((MAX - y) / SECTOR)))
    return f"{chr(ord('A') + row)}{col}"


def _cmdline(pid: int) -> str:
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("latin1")
    except OSError:
        return ""


def game_pids() -> list[tuple[int, int | None]]:
    """(pid, dimension_index) for the DeepDesert_1 partition processes (where the
    storm actors live). Other partitions/dungeons are skipped."""
    out = subprocess.run(["pgrep", "-f", BINNAME], capture_output=True, text=True)
    found = []
    for p in (int(x) for x in out.stdout.split()):
        cl = _cmdline(p)
        if "DeepDesert_1" not in cl:
            continue
        m = re.search(r"-PartitionIndex=(\d+)", cl)
        pidx = int(m.group(1)) if m else None
        found.append((p, PARTITION_DIM.get(pidx)))
    return found


def pod_for(pid: int) -> str:
    try:
        cg = open(f"/proc/{pid}/cgroup").read()
        m = re.search(r"(sh-[0-9a-f]+-[a-z0-9]+-sg-[a-z0-9-]+-pod-\d+)", cg)
        if m:
            return m.group(1)
        m = re.search(r"pod-?\d+|pod[0-9a-f-]{6,}", cg)
        return m.group(0) if m else "?"
    except OSError:
        return "?"


def module_base(pid: int) -> int | None:
    lo = None
    for ln in open(f"/proc/{pid}/maps"):
        if BINNAME in ln:
            start = int(ln.split("-", 1)[0], 16)
            lo = start if lo is None else min(lo, start)
    return lo


def ro_regions(pid: int):
    out = []
    for ln in open(f"/proc/{pid}/maps"):
        if BINNAME not in ln:
            continue
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4})", ln)
        if m and "r" in m.group(3):
            out.append((int(m.group(1), 16), int(m.group(2), 16)))
    return out


def rw_regions(pid: int):
    for ln in open(f"/proc/{pid}/maps"):
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4})", ln)
        if not m:
            continue
        if "r" in m.group(3) and "w" in m.group(3):
            yield int(m.group(1), 16), int(m.group(2), 16)


def _scan_regions(mem, regions, needle, aligned=False):
    nlen = len(needle)
    for s, e in regions:
        addr = s
        while addr < e:
            size = min(_RO_CHUNK, e - addr)
            try:
                mem.seek(addr)
                buf = mem.read(size)
            except (OSError, ValueError):
                addr += size
                continue
            if not buf:
                addr += size
                continue
            pos = buf.find(needle)
            while pos != -1:
                a = addr + pos
                if not aligned or a % 8 == 0:
                    yield a
                pos = buf.find(needle, pos + 1)
            addr += size - (nlen - 1) if size == _RO_CHUNK else size


def resolve_vptrs(pid: int, mem, base: int) -> dict[int, str]:
    """Self-locate the ASandStormBase vptr via the RTTI chain (patch-robust): the
    same _ZTS -> _ZTI+8 -> vtable+8 -> vptr=vtable+0x10 walk the spice reader uses.
    Falls back to FALLBACK_VPTR (build-specific) only if RTTI yields nothing."""
    regs = ro_regions(pid)
    needles: dict[int, str] = {}
    for kind, mangled in MANGLED.items():
        str_addrs = []
        for a in _scan_regions(mem, regs, mangled + b"\x00"):
            try:
                mem.seek(a - 1)
                prev = mem.read(1)
            except (OSError, ValueError):
                prev = b""
            if prev and (prev.isalnum() or prev == b"_"):
                continue
            str_addrs.append(a)
        for sa in str_addrs:
            for zti8 in _scan_regions(mem, regs, struct.pack("<Q", sa), aligned=True):
                zti = zti8 - 8
                for vt8 in _scan_regions(mem, regs, struct.pack("<Q", zti), aligned=True):
                    needles[vt8 + 8] = kind
    if not needles:
        for kind, off in FALLBACK_VPTR.items():
            needles[base + off] = kind
    return needles


def _yaw_from_quat(qx, qy, qz, qw) -> float:
    return round(math.degrees(math.atan2(2.0 * (qw * qz + qx * qy),
                                         1.0 - 2.0 * (qy * qy + qz * qz))), 1)


def _read_storm(rd, obj: int) -> dict | None:
    """Read one ASandStormBase at `obj`; None if any guarded read fails / falls out
    of bounds. Emits ALL radius + heading candidates (QA validated the picks on the
    live 01:56Z PvE storm). ACTIVE discriminator LOCKED from that capture: a real
    sweeping storm passes all of phase==3 / stage>=0 / r_eff>0 / off-origin, while
    the 2 pre-spawned ghost actors per pod fail every clause (phase==3 but
    stage==-1, r_eff==0, center==(0,0))."""
    try:
        phase = rd(obj + OFF_PHASE, 1)[0]
        root = struct.unpack("<Q", rd(obj + OFF_ROOT, 8))[0]
        if not (0x10000 < root < 0x7FFFFFFFFFFF):
            return None
        x, y, z = struct.unpack("<ddd", rd(root + COMP_TRANSLATION, 24))
        if not (MIN - 5e5 < x < MAX + 5e5 and MIN - 5e5 < y < MAX + 5e5):
            return None
        r_effect = struct.unpack("<f", rd(obj + OFF_RADIUS_EFFECT, 4))[0]
        r_fade = struct.unpack("<f", rd(obj + OFF_RADIUS_FADE, 4))[0]
        r_cull = struct.unpack("<f", rd(obj + OFF_RADIUS_CULL, 4))[0]
        stage = struct.unpack("<i", rd(obj + OFF_STAGE, 4))[0]
        # heading candidate 1: MovementComponent velocity. LIVE FINDING: this reads
        # (0,0) on the real storm (it translates directly, no populated velocity),
        # so it is NOT the heading source -- kept only as a diagnostic.
        vx, vy, vz = struct.unpack("<ddd", rd(obj + OFF_VELOCITY, 24))
        h_vel = (round(math.degrees(math.atan2(vy, vx)), 1)
                 if (abs(vx) > 1e-3 or abs(vy) > 1e-3) else None)
        # heading candidate 2: root quat yaw. LOCKED as the PRIMARY heading source.
        qx, qy, qz, qw = struct.unpack("<dddd", rd(root + COMP_ROTATION, 32))
        h_quat = _yaw_from_quat(qx, qy, qz, qw)
    except (OSError, struct.error, OverflowError):
        return None
    real_active = (phase == ACTIVE_PHASE and stage >= 0 and r_effect > 0.0
                   and not (round(x, 1) == 0.0 and round(y, 1) == 0.0))
    return {"obj": hex(obj), "phase": phase, "active": real_active,
            "center_x": round(x, 1), "center_y": round(y, 1), "center_z": round(z, 1),
            "sector": sector_for(x, y), "stage": stage,
            # radius candidates (raw). +0x59C is LINEAR cm -> the v1 visual circle
            # (LIVE: 450000cm = 4.5km on the real storm). fade/cull are config
            # constants (9000m/10000m), NOT the visual circle.
            "radius_effect": round(r_effect, 1), "radius_fade": round(r_fade, 1),
            "radius_cull": round(r_cull, 1),
            "heading_vel_yaw": h_vel, "heading_quat_yaw": h_quat,
            "vel_x": round(vx, 1), "vel_y": round(vy, 1),
            # v1 picks (relay maps these into the per-dim overlay payload):
            "radius": round(r_effect, 1),       # +0x59C effect, linear cm
            # heading = quat (+0x300) ONLY. LIVE-PROVEN: the 3-point track
            # H1->H2->G5 has bearing atan2(dy,dx)==5.5 on BOTH segments, exactly
            # equal to hdg_quat. Velocity +0x458 is always (0,0) -> diagnostic only.
            "heading_yaw": h_quat}


def scan_pid(pid: int, dim: int | None, include_inactive: bool = False) -> dict:
    base = module_base(pid)
    if base is None:
        return {"pid": pid, "dimension": dim, "error": "module base not found"}
    storms = []
    with open(f"/proc/{pid}/mem", "rb", 0) as mem:
        def rd(addr, n):
            mem.seek(addr)
            return mem.read(n)
        needles = resolve_vptrs(pid, mem, base)
        resolved = "rtti" if any(
            v not in {base + o for o in FALLBACK_VPTR.values()} for v in needles
        ) else ("fallback" if needles else "none")
        if not needles:
            return {"pid": pid, "dimension": dim, "pod": pod_for(pid),
                    "module_base": hex(base), "vptr_source": resolved,
                    "vptrs": [], "storms": [],
                    "error": "ASandStormBase vptr not resolved"}
        seen: set[int] = set()
        for vptr_addr in needles:
            nb = struct.pack("<Q", vptr_addr)
            for s, e in rw_regions(pid):
                addr = s
                while addr < e:
                    size = min(CHUNK, e - addr)
                    try:
                        mem.seek(addr)
                        buf = mem.read(size)
                    except (OSError, ValueError):
                        addr += size
                        continue
                    if not buf:
                        addr += size
                        continue
                    pos = buf.find(nb)
                    while pos != -1:
                        obj = addr + pos
                        if obj % 8 == 0 and obj not in seen:
                            seen.add(obj)
                            st = _read_storm(rd, obj)
                            if st is not None and (include_inactive or st["active"]):
                                # record the matched vptr so the cache can fast
                                # re-read this exact actor (v1.0.1 fast path).
                                st["vptr"] = hex(vptr_addr)
                                storms.append(st)
                        pos = buf.find(nb, pos + 8)
                    addr += size - 8 if size == CHUNK else size
    return {"pid": pid, "dimension": dim, "pod": pod_for(pid),
            "module_base": hex(base), "vptr_source": resolved,
            "vptrs": [hex(a) for a in needles], "storms": storms}


def read_obj(pid: int, obj: int, expected_vptr: int, dim: int | None) -> dict:
    """v1.0.1 FAST PATH: read ONE actor at a known heap address (no heap walk),
    for re-reading a storm the cache already located. Guarded per the lead's notes:
      1. PID re-validate: the pid must still be a DeepDesert_1 DD process.
      2. STALE-ADDRESS safety: the 8 bytes at `obj` (the vptr) MUST equal the
         expected ASandStormBase vptr from the prior full scan; a freed/reused
         address fails this -> we return empty (caller falls back to a full scan)
         and NEVER emit a position from an unverified address.
    Returns the same process-dict shape as scan_pid (storms=[st] if active)."""
    if "DeepDesert_1" not in _cmdline(pid):
        return {"pid": pid, "dimension": dim, "fast": True, "storms": [],
                "error": "pid no longer a DeepDesert_1 DD process"}
    try:
        with open(f"/proc/{pid}/mem", "rb", 0) as mem:
            def rd(addr, n):
                mem.seek(addr)
                return mem.read(n)
            try:
                vptr = struct.unpack("<Q", rd(obj, 8))[0]
            except (OSError, struct.error, OverflowError):
                return {"pid": pid, "dimension": dim, "fast": True, "storms": [],
                        "error": "obj address unreadable"}
            if vptr != expected_vptr:
                return {"pid": pid, "dimension": dim, "fast": True, "storms": [],
                        "error": "vptr mismatch (stale/reused address)"}
            st = _read_storm(rd, obj)
    except OSError as exc:
        return {"pid": pid, "dimension": dim, "fast": True, "storms": [],
                "error": str(exc)[:120]}
    if st is None:
        return {"pid": pid, "dimension": dim, "fast": True, "storms": []}
    st["vptr"] = hex(expected_vptr)
    return {"pid": pid, "dimension": dim, "pod": pod_for(pid), "fast": True,
            "storms": [st] if st["active"] else []}


def main(argv) -> int:
    verify = "--verify" in argv
    include_inactive = "--all" in argv
    emit_instances = "--emit-instances" in argv

    # --list-pids: cheap dim->pid map for the cache (no heap scan).
    if "--list-pids" in argv:
        print(json.dumps({"pids": {str(d): p for p, d in game_pids()
                                   if d is not None}}, separators=(",", ":")))
        return 0

    want_pid = None
    read_obj_addr = None
    obj_vptr = None
    for a in argv:
        if a.startswith("--pid="):
            want_pid = int(a.split("=", 1)[1])
        elif a.startswith("--read-obj="):
            read_obj_addr = int(a.split("=", 1)[1], 16)
        elif a.startswith("--obj-vptr="):
            obj_vptr = int(a.split("=", 1)[1], 16)
    if want_pid is not None:
        m = re.search(r"-PartitionIndex=(\d+)", _cmdline(want_pid))
        pids = [(want_pid, PARTITION_DIM.get(int(m.group(1))) if m else None)]
    else:
        pids = game_pids()
    if not pids:
        print(json.dumps({"error": f"no {BINNAME} DeepDesert_1 process found",
                          "processes": []}))
        return 1

    # --read-obj: v1.0.1 fast single-address re-read of a cached actor (no scan).
    if read_obj_addr is not None:
        if want_pid is None or obj_vptr is None:
            print(json.dumps({"error": "--read-obj requires --pid and --obj-vptr",
                              "processes": []}))
            return 1
        pr = read_obj(want_pid, read_obj_addr, obj_vptr, pids[0][1])
        print(json.dumps({"processes": [pr]}, separators=(",", ":")))
        return 0

    # --emit-instances: JSON list of EVERY ASandStormBase instance (active AND the
    # pre-spawned inactive ghosts) with its obj+vptr, so the cache can LEARN the
    # per-pod actor addresses once and then cheap fast-read them each tick (killing
    # the full-heap-walk birth latency). Purely additive; existing modes unchanged.
    if emit_instances:
        procs = [scan_pid(p, dim, include_inactive=True) for p, dim in pids]
        out = []
        for pr in procs:
            out.append({"pid": pr["pid"], "dimension": pr.get("dimension"),
                        "pod": pr.get("pod"), "vptr_source": pr.get("vptr_source"),
                        "instances": pr.get("storms", []), "error": pr.get("error")})
        print(json.dumps({"processes": out}, separators=(",", ":")))
        return 0

    procs = [scan_pid(p, dim, include_inactive=(verify and include_inactive))
             for p, dim in pids]

    if verify:
        for pr in procs:
            print(f"\n=== pid {pr['pid']} dim={pr.get('dimension')} "
                  f"pod={pr.get('pod')} base={pr.get('module_base')} "
                  f"vptr={pr.get('vptr_source')} {pr.get('vptrs')} ===")
            if pr.get("error"):
                print("  ERROR:", pr["error"]); continue
            storms = pr.get("storms", [])
            act = [s for s in storms if s["active"]]
            print(f"  ASandStormBase instances: {len(storms)} "
                  f"({len(act)} real-active; discriminator phase==3 & stage>=0 "
                  f"& r_eff>0 & off-origin)")
            for st in storms:
                tag = " <== ACTIVE" if st["active"] else ""
                print(f"    obj={st['obj']} phase={st['phase']} sector={st['sector']:3} "
                      f"center=({st['center_x']:.0f},{st['center_y']:.0f}) "
                      f"r_eff={st['radius_effect']} r_fade={st['radius_fade']} "
                      f"r_cull={st['radius_cull']} hdg_vel={st['heading_vel_yaw']} "
                      f"hdg_quat={st['heading_quat_yaw']} "
                      f"vel=({st['vel_x']},{st['vel_y']}) stage={st['stage']}{tag}")
        return 0

    out = []
    for pr in procs:
        active = [s for s in pr.get("storms", []) if s.get("active")]
        out.append({"pid": pr["pid"], "dimension": pr.get("dimension"),
                    "pod": pr.get("pod"), "vptr_source": pr.get("vptr_source"),
                    "storms": active, "error": pr.get("error")})
    print(json.dumps({"processes": out}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
