#!/usr/bin/env python3
"""Read-only live spice-field position reader (Plan B).

Scans the Dune dedicated-server process memory (/proc/<pid>/mem, READ-ONLY, no
ptrace-stop) for ASpiceField actor instances and reads their live world transform,
turning the otherwise-RAM-only Large spice field location into a 9x9 map sector.

This is the automated complement to the survey path: the active-Large position is
never persisted server-side (field_id is a one-way hash), but the live actor's
RootComponent transform is in process RAM. Layout from
DunePakRE/findings/SPICE-RAM-READER-SPEC.md (Ghidra RE 2026-06-08).

SAFE: opens /proc/<pid>/mem O_RDONLY and preads in chunks. It never writes, never
PTRACE_ATTACHes (so it never pauses the game), never touches the DB or game state.

Usage:
  sudo python3 dune-spice-ramread.py            # JSON: active fields per process
  sudo python3 dune-spice-ramread.py --verify   # human-readable, dumps every hit
  sudo python3 dune-spice-ramread.py --system          # v2: USpiceHarvestingSystem array path
  sudo python3 dune-spice-ramread.py --system --verify # v2: dump both arrays + cross-check

The default (v1) path scans the whole heap for ASpiceField vptrs. The additive
--system path instead locates the singleton USpiceHarvestingSystem per process and
reads its candidate-field arrays directly (Ghidra RE 2026-06-09, see
DunePakRE/findings/SPICE-HARVESTING-SYSTEM-RE.md). v1 stays the default/fallback
until --system is live-validated. Both emit the same {"processes":[...]} contract.
"""
import json
import os
import re
import struct
import subprocess
import sys

# --- vtable self-location (patch-robust) -------------------------------------
# Objects store a vptr = vtable+0x10 (Itanium ABI: the first virtual fn follows the
# offset-to-top @+0 and the type_info ptr @+8). Rather than hardcode the vtable VA
# (build-specific; the 0x15946108 from the 2026-06-08 build dies on any Funcom
# patch), we self-locate at runtime via the RTTI chain that survives stripping:
#   _ZTS "<mangled>" string  ->  a ptr to it == _ZTI+8  ->  a ptr to _ZTI == vtable+8
#   ->  the vptr objects hold == vtable+0x10 == (that location)+8.
# We gather every candidate vptr (primary + interface vtables) and let the object
# scan keep whichever actually yields valid field instances (interface vtables hit 0).
MANGLED = {"spice": b"11ASpiceField", "floursand": b"15AFlourSandField",
           "system": b"22USpiceHarvestingSystem"}

# Fallback only: the 2026-06-08-build vtable+0x10 offsets (module-base-relative),
# used if the RTTI self-location finds nothing (e.g. RTTI ever gets stripped).
FALLBACK_VPTR = {"spice": 0x159472F8, "floursand": 0x15496C50}

OFF_ROOT       = 0x240               # RootComponent (USceneComponent*)
OFF_VALUE      = 0x5B8               # m_TotalValueToDistribute (read 32-bit)
COMP_TRANSLATION = 0x320             # ComponentToWorld.Translation (X,Y,Z doubles)
# --- active-field discriminator (Ghidra RE 2026-06-09) -----------------------
# value+dim is NOT enough: up to 3 Large ASpiceField actors coexist in one DD
# dim's RAM (the active blow + pre-spawned candidate craters), ALL at value=
# 2,500,000. The active-blow discriminator has been chased through three guesses;
# the LIVE ground truth (<game-host> 2026-06-09, both DD procs) settles it:
#   - bloom (+0x75C) only means "has a surfaced visual" (>=0 on several at once). NO.
#   - +0x4D0 gates HARVEST-PROGRESS (FUN_0f2a42e0 computes the distribute fraction
#     only when *(char*)(field+0x4D0)==1). On the live box with ZERO players mining,
#     0 Larges had +0x4D0==1 (it fired only on small value=5000 fields) -> +0x4D0 ==
#     "a field is being ACTIVELY MINED right now", NOT "the blow exists". NO.
#   - +0x758 (m_bSurfaced) == 1 on EXACTLY ONE Large/dim (dim0=I3, dim1=F1), held at
#     rest, == the in-game surfaced blow. YES -> this is the active-blow signal.
# So the ACTIVE Large = the resident Large whose byte @+0x758 == 1. +0x4D0 (now a
# "being mined" diagnostic), bloom, and active_seq are kept for diagnostics only.
#
# AUTHORITATIVE-FLAG LEAD CLOSED (Ghidra RE 2026-06-25, build 2007976): the reflected
# bool the RE team flagged as a cleaner "is this field active" signal,
# m_bIsActiveOnServer, resolves to THIS SAME byte @ +0x4D0 -- confirmed via its
# getter UFUNCTION GetIsActiveOnServer (exec thunk reads `*result = *(u8*)(this+0x4D0)`)
# and OnRep_IsActiveOnServer (RepNotify reads the same byte). So m_bIsActiveOnServer
# is NOT a new/cleaner offset: it is OFF_ACTIVE, already empirically known to fire only
# while a field is being mined. The current picker already ORs it with +0x758 to also
# catch the just-surfaced-at-rest state, so switching to the bool alone would REGRESS.
# No reader change warranted; keep the OR. (ESpiceFieldActivationStatus is an enum TYPE
# with no distinct per-instance reflected accessor; no separate offset exists.)
OFF_ACTIVE     = 0x4D0               # m_bIsActiveOnServer (byte; ==1 while mined; OR'd, not sole picker)
OFF_BLOOM      = 0x75C               # m_BloomVariationIndex (int32; -1=never surfaced)
OFF_ACTIVESEQ  = 0x760               # m_ActiveSequence (int32; diagnostics)
OFF_BLOOMCOUNT = 0x698               # bloom-variation count (upper bound on bloom)
OFF_SURFACED   = 0x758               # m_bSurfaced (byte; ==1 = THE active blow, the picker)
BLOOM_INACTIVE = -1
LARGE_VALUE    = 2_500_000

# --- USpiceHarvestingSystem v2 array offsets (Ghidra RE 2026-06-09) -----------
# The singleton USpiceHarvestingSystem (one per DD-dim igw process, size 0x318)
# owns two TArray<ASpiceField*>: the active-consideration pool (+0xF0 data /
# +0xF8 Num) and the staging pool (+0x100 data / +0x108 Num). A field sits in
# staging when there is no active blow for its type and in the consideration pool
# otherwise -- reading BOTH and UNIONing (dedupe by field-obj ptr) guarantees we
# see every candidate regardless of state. See SPICE-HARVESTING-SYSTEM-RE.md §2.
SYS_ARRAYS     = ((0xF0, 0xF8), (0x100, 0x108))   # (data off, Num off) pairs
SYS_NUM_MAX    = 4096                # sanity bound on each TArray Num
MED_VALUE      = 150_000
BINNAME        = "DuneSandboxServer-Linux-Shipping"

# --- DD calibration (ground-truthed) ----------------------------------------
MIN, MAX = -1270000.0, 1168400.0
SECTOR = (MAX - MIN) / 9.0
CHUNK = 64 * 1024 * 1024             # 64 MiB scan window


def sector_for(x: float, y: float) -> str:
    col = max(1, min(9, int((x - MIN) / SECTOR) + 1))
    row = max(0, min(8, int((MAX - y) / SECTOR)))
    return f"{chr(ord('A') + row)}{col}"


def _cmdline(pid: int) -> str:
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("latin1")
    except OSError:
        return ""


# PartitionIndex -> DB dimension_index. The Large spice field only lives on the
# DeepDesert partitions: index 8 = PvE (dim 0), 31 = PvP (dim 1) on our server.
PARTITION_DIM = {8: 0, 31: 1}


def game_pids() -> list[tuple[int, int | None]]:
    """Return (pid, dimension_index) for the DeepDesert_1 partition processes only
    (that is where the active Large is). Other partitions/dungeons are skipped."""
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
    """Readable, binary-backed regions (.rodata + .data.rel.ro where the RTTI
    strings, type_info objects, and vtables live). Small relative to the heap, so
    cheap to scan for the self-location chain. Includes any 'w' RELRO leftovers."""
    out = []
    for ln in open(f"/proc/{pid}/maps"):
        if BINNAME not in ln:
            continue
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4})", ln)
        if m and "r" in m.group(3):
            out.append((int(m.group(1), 16), int(m.group(2), 16)))
    return out


_RO_CHUNK = 16 * 1024 * 1024


def _scan_regions(mem, regions, needle, aligned=False):
    """Yield every address in `regions` where `needle` occurs (optionally only at
    8-aligned offsets, for pointer searches)."""
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
    """Self-locate the ASpiceField / AFlourSandField vptrs via the RTTI chain so the
    reader survives Funcom patches. Returns {vptr_addr: kind}. Falls back to the
    build-specific hardcoded offsets if the chain can't be resolved."""
    regs = ro_regions(pid)
    needles: dict[int, str] = {}
    for kind, mangled in MANGLED.items():
        # 1) the _ZTS type-name C-string, guarded so we don't match a longer name.
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
        # 2) ptr to the string == _ZTI+8;  3) ptr to _ZTI == vtable+8.
        for sa in str_addrs:
            for zti8 in _scan_regions(mem, regs, struct.pack("<Q", sa), aligned=True):
                zti = zti8 - 8
                for vt8 in _scan_regions(mem, regs, struct.pack("<Q", zti), aligned=True):
                    needles[vt8 + 8] = kind        # vptr = (vtable+8) + 8 = vtable+0x10
    if not needles:                                # RTTI gone -> build-specific fallback
        for kind, off in FALLBACK_VPTR.items():
            needles[base + off] = kind
    return needles


def rw_regions(pid: int):
    for ln in open(f"/proc/{pid}/maps"):
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+) (\S{4})", ln)
        if not m:
            continue
        perms = m.group(3)
        if "r" in perms and "w" in perms:           # heap / anon rw
            yield int(m.group(1), 16), int(m.group(2), 16)


def scan_pid(pid: int, dim: int | None = None, stop_on_large: bool = False,
             progress: bool = False) -> dict:
    base = module_base(pid)
    if base is None:
        return {"pid": pid, "dim": dim, "error": "module base not found"}
    hits = []
    regions = list(rw_regions(pid))
    done = False
    with open(f"/proc/{pid}/mem", "rb", 0) as mem:
        def rd(addr, n):
            mem.seek(addr)
            return mem.read(n)
        # Resolve the field vptrs from the live process's RTTI (patch-robust); each
        # distinct vptr becomes a search needle tagged with its field kind.
        needles = resolve_vptrs(pid, mem, base)
        resolved = "rtti" if any(v not in
                   {base + o for o in FALLBACK_VPTR.values()} for v in needles) else "fallback"
        needle_bytes: dict[str, list[bytes]] = {}
        for addr, kind in needles.items():
            needle_bytes.setdefault(kind, []).append(struct.pack("<Q", addr))
        for ri, (s, e) in enumerate(regions):
            if done:
                break
            if progress:
                print(f"  [pid {pid}] region {ri+1}/{len(regions)} "
                      f"{hex(s)} ({(e-s)//(1024*1024)} MiB) hits={len(hits)}",
                      file=sys.stderr, flush=True)
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
                for kind, nbs in needle_bytes.items():
                    for nb in nbs:
                        pos = buf.find(nb)
                        while pos != -1:
                            if (addr + pos) % 8 == 0:       # pointer must be 8-aligned
                                obj = addr + pos
                                try:
                                    val = struct.unpack("<I", rd(obj + OFF_VALUE, 4))[0]
                                    root = struct.unpack("<Q", rd(obj + OFF_ROOT, 8))[0]
                                    # bounds-guard the root ptr: a false-positive vptr match
                                    # yields garbage here and an unguarded deref overflows
                                    # C long (OverflowError) or faults mid-scan.
                                    if 0x10000 < root < 0x7FFFFFFFFFFF:
                                        x, y, z = struct.unpack("<ddd", rd(root + COMP_TRANSLATION, 24))
                                        if MIN - 5e5 < x < MAX + 5e5 and MIN - 5e5 < y < MAX + 5e5:
                                            bloom = struct.unpack("<i", rd(obj + OFF_BLOOM, 4))[0]
                                            aseq = struct.unpack("<i", rd(obj + OFF_ACTIVESEQ, 4))[0]
                                            active_flag = rd(obj + OFF_ACTIVE, 1)[0]  # byte
                                            hits.append({"kind": kind, "obj": hex(obj),
                                                         "value": val, "x": round(x, 1),
                                                         "y": round(y, 1), "z": round(z, 1),
                                                         "sector": sector_for(x, y),
                                                         "bloom": bloom, "active_seq": aseq,
                                                         "active_flag": active_flag,
                                                         "active": active_flag == 1})
                                            # THE active blow = the Large whose +0x4D0 byte == 1
                                            # (server-designated; exactly one/dim). bloom>=0 only
                                            # means "surfaced" (true on several candidates).
                                            if (stop_on_large and kind == "spice"
                                                    and val == LARGE_VALUE and active_flag == 1):
                                                done = True
                                                break
                                except (OSError, struct.error, OverflowError):
                                    pass
                            pos = buf.find(nb, pos + 8)
                        if done:
                            break
                    if done:
                        break
                if done:
                    break
                addr += size - 8 if size == CHUNK else size   # 8-byte overlap across chunks
    return {"pid": pid, "dim": dim, "pod": pod_for(pid), "module_base": hex(base),
            "vptr_source": resolved, "vptrs": len(needles), "fields": hits}


def _read_field(rd, obj: int) -> dict | None:
    """Read one ASpiceField at `obj` and return the SAME per-hit dict shape the v1
    heap scan produces (keys: kind, obj, value, x, y, z, sector, bloom, active_seq,
    active_flag, active), or None if any guarded read fails / falls out of bounds.

    Identical offsets/parse to the v1 inner loop -- the only difference is that the
    v2 caller already knows `obj` is a field (it came out of the system array),
    where v1 found it by vptr match. Every read is wrapped so a faulted page or a
    bogus pointer degrades to None (skip) rather than crashing the whole scan."""
    try:
        val = struct.unpack("<I", rd(obj + OFF_VALUE, 4))[0]
        root = struct.unpack("<Q", rd(obj + OFF_ROOT, 8))[0]
        # bounds-guard the root ptr exactly as v1 does: a stale/garbage array slot
        # yields nonsense here and an unguarded deref overflows C long or faults.
        if not (0x10000 < root < 0x7FFFFFFFFFFF):
            return None
        x, y, z = struct.unpack("<ddd", rd(root + COMP_TRANSLATION, 24))
        if not (MIN - 5e5 < x < MAX + 5e5 and MIN - 5e5 < y < MAX + 5e5):
            return None
        bloom = struct.unpack("<i", rd(obj + OFF_BLOOM, 4))[0]
        aseq = struct.unpack("<i", rd(obj + OFF_ACTIVESEQ, 4))[0]
        active_flag = rd(obj + OFF_ACTIVE, 1)[0]            # byte @ +0x4D0
        surfaced = rd(obj + OFF_SURFACED, 1)[0]             # byte @ +0x758 (diagnostic)
    except (OSError, struct.error, OverflowError):
        return None
    return {"kind": "spice", "obj": hex(obj), "value": val,
            "x": round(x, 1), "y": round(y, 1), "z": round(z, 1),
            "sector": sector_for(x, y), "bloom": bloom, "active_seq": aseq,
            "surfaced": surfaced, "active_flag": active_flag,
            # ACTIVE-BLOW signal. The active Large cycles through sub-states (live
            # <game-host> 2026-06-09, two reads ~10min apart of the SAME field):
            #   just-surfaced -> surfaced(+0x758)==1, active_seq==0, +0x4D0==0
            #   being-mined   -> surfaced==0,        active_seq==1, +0x4D0==1
            # dormant candidates stay surfaced==0/+0x4D0==0/active_seq==3 (the
            # stood-down value SetActiveSequence(p,3)). So NEITHER byte alone is
            # stable; the active field is the one that is surfaced OR being-mined
            # (equivalently active_seq != 3). +0x4D0 alone (the old picker) misses
            # the just-surfaced state; +0x758 alone misses the being-mined state.
            "active": (surfaced == 1 or active_flag == 1),
            "dormant_seq": (aseq == 3)}


def scan_pid_system(pid: int, dim: int | None = None) -> dict:
    """v2 path: locate the singleton USpiceHarvestingSystem and read its candidate
    arrays directly, instead of scanning the whole heap for ASpiceField vptrs.

    Returns the SAME process-dict shape v1 scan_pid produces (pid, dim, pod,
    module_base, vptr_source, vptrs, fields) PLUS `systems` (raw per-system array
    detail for --verify). `fields` is the UNION of both arrays, deduped by field
    obj ptr -- so the downstream large/large_resident/large_active derivation in
    main() is identical to v1's."""
    base = module_base(pid)
    if base is None:
        return {"pid": pid, "dim": dim, "error": "module base not found"}
    with open(f"/proc/{pid}/mem", "rb", 0) as mem:
        def rd(addr, n):
            mem.seek(addr)
            return mem.read(n)
        # Resolve every RTTI vptr (patch-robust); keep only the system vtable.
        needles = resolve_vptrs(pid, mem, base)
        resolved = "rtti" if any(v not in
                   {base + o for o in FALLBACK_VPTR.values()} for v in needles) else "fallback"
        sys_vptrs = [a for a, k in needles.items() if k == "system"]
        if not sys_vptrs:
            return {"pid": pid, "dim": dim, "pod": pod_for(pid),
                    "module_base": hex(base), "vptr_source": resolved,
                    "vptrs": len(needles), "systems": [], "fields": [],
                    "error": "USpiceHarvestingSystem vptr not resolved (RTTI?)"}
        sys_needles = [struct.pack("<Q", a) for a in sys_vptrs]

        # 1) Find the singleton system object(s): scan rw regions (8-aligned) for the
        #    system vptr. Expect ~1 real hit; a 2nd interface/secondary vtable hit is
        #    possible -- we keep any object whose arrays read sane and discard the rest.
        sys_objs = []
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
                for nb in sys_needles:
                    pos = buf.find(nb)
                    while pos != -1:
                        a = addr + pos
                        if a % 8 == 0:
                            sys_objs.append(a)
                        pos = buf.find(nb, pos + 8)
                addr += size - 8 if size == CHUNK else size

        # 2) Read BOTH arrays of each system object, union by field obj ptr.
        systems = []
        union: dict[int, dict] = {}
        for sysobj in sys_objs:
            arrays = []
            any_ok = False
            for data_off, num_off in SYS_ARRAYS:
                entry = {"data_off": hex(data_off), "num_off": hex(num_off),
                         "data": None, "num": None, "fields": [], "ok": False}
                try:
                    data = struct.unpack("<Q", rd(sysobj + data_off, 8))[0]
                    num = struct.unpack("<i", rd(sysobj + num_off, 4))[0]
                except (OSError, struct.error, OverflowError):
                    arrays.append(entry)
                    continue
                entry["data"] = hex(data)
                entry["num"] = num
                if not (0x10000 < data < 0x7FFFFFFFFFFF) or not (0 <= num < SYS_NUM_MAX):
                    arrays.append(entry)
                    continue
                entry["ok"] = True
                any_ok = True
                for i in range(num):
                    try:
                        field_ptr = struct.unpack("<Q", rd(data + i * 8, 8))[0]
                    except (OSError, struct.error, OverflowError):
                        continue
                    if not (0x10000 < field_ptr < 0x7FFFFFFFFFFF):
                        continue
                    f = _read_field(rd, field_ptr)
                    if f is None:
                        continue
                    entry["fields"].append(f)
                    union.setdefault(field_ptr, f)       # dedupe by field obj ptr
                arrays.append(entry)
            # Discard a system candidate whose BOTH arrays failed every bound (a
            # false-positive / secondary-vtable hit), keep the real singleton(s).
            systems.append({"obj": hex(sysobj), "valid": any_ok, "arrays": arrays})

    fields = list(union.values())
    return {"pid": pid, "dim": dim, "pod": pod_for(pid), "module_base": hex(base),
            "vptr_source": resolved, "vptrs": len(needles),
            "systems": systems, "fields": fields}


def resolve_only(pids) -> int:
    """Fast diagnostic: resolve the vptrs (small read-only-region scan, no heap
    walk) and print them next to the build-specific fallback for comparison."""
    for pid, dim in pids:
        base = module_base(pid)
        print(f"\n=== pid {pid} dim={dim} base={hex(base) if base else None} ===")
        if base is None:
            print("  module base not found"); continue
        with open(f"/proc/{pid}/mem", "rb", 0) as mem:
            needles = resolve_vptrs(pid, mem, base)
        fb = {base + o for o in FALLBACK_VPTR.values()}
        src = "rtti" if any(v not in fb for v in needles) else "fallback"
        print(f"  resolved ({src}), {len(needles)} vptr(s):")
        for addr, kind in sorted(needles.items()):
            print(f"    {kind:9} {hex(addr)}  (module-rel {hex(addr - base)})")
        print("  fallback would be:")
        for kind, off in FALLBACK_VPTR.items():
            print(f"    {kind:9} {hex(base + off)}  (module-rel {hex(off)})")
    return 0


def _candidates(fields: list) -> list:
    """Compact per-candidate view (additive output): every union field with the
    keys downstream needs to eventually drop the hardcoded F1/F9/H5/I3/I9 list."""
    return [{"sector": f["sector"], "value": f["value"], "obj": f["obj"],
             "x": f["x"], "y": f["y"], "bloom": f.get("bloom"),
             "active_seq": f.get("active_seq"), "active_flag": f.get("active_flag"),
             "active": f.get("active")}
            for f in sorted(fields, key=lambda f: (not f.get("active"), -f["value"]))]


def _mediums(fields: list) -> list:
    """Resident Medium spice fields (value == MED_VALUE) as exact-coord sites.

    The candidate medium SITES are the full per-cycle set, instantiated at once
    (static, no per-cycle accumulation needed), but only a SUBSET is active/erupted
    at any moment and that active subset ROTATES (mediums work like Larges for the
    active state; the operator 2026-06-17). So carry the per-site `active` flag (+ value).
    Co-located crater sub-actors share an exact transform, so dedupe by
    (round(x), round(y)); a site is active if ANY of its sub-actors is active.
    Additive output: feeds the portal medium-field layer (Part A)."""
    by_site: dict[tuple[int, int], dict] = {}
    order: list[tuple[int, int]] = []
    for f in sorted(fields, key=lambda f: (f["sector"], f["obj"])):
        if f.get("value") != MED_VALUE:
            continue
        k = (round(f["x"]), round(f["y"]))
        if k not in by_site:
            by_site[k] = {"sector": f["sector"], "x": f["x"], "y": f["y"],
                          "value": f["value"], "active": bool(f.get("active"))}
            order.append(k)
        elif f.get("active"):
            by_site[k]["active"] = True   # erupted in any co-located crater -> active
    return [by_site[k] for k in order]


def _system_verify(procs: list) -> int:
    """Human-readable dump of the v2 system-array path: per dim, the Num + every
    element of BOTH arrays (active-consideration +0xF0/+0xF8 and staging
    +0x100/+0x108), plus a self-cross-check that the union's active Large matches
    what the value==2.5M & active==1 filter picks. This is the offset confirmation
    the supervisor runs on <game-host>."""
    for pr in procs:
        print(f"\n=== pid {pr['pid']} dim={pr.get('dim')} pod={pr.get('pod')} "
              f"base={pr.get('module_base')} "
              f"vptr={pr.get('vptr_source')}({pr.get('vptrs')}) ===")
        if pr.get("error"):
            print("  ERROR:", pr["error"]); continue
        systems = pr.get("systems", [])
        print(f"  USpiceHarvestingSystem objects: {len(systems)} "
              f"({sum(1 for s in systems if s.get('valid'))} valid)")
        for s in systems:
            print(f"  system @ {s['obj']}  valid={s['valid']}")
            for arr in s["arrays"]:
                print(f"    array data@{arr['data_off']}/num@{arr['num_off']}: "
                      f"data={arr['data']} num={arr['num']} ok={arr['ok']} "
                      f"({len(arr['fields'])} field(s) read)")
                for f in arr["fields"]:
                    print(f"      obj={f['obj']} sector={f['sector']:3} "
                          f"value={f['value']:>9} active@0x4D0={f['active_flag']} "
                          f"surfaced@0x758={f.get('surfaced')} bloom={f['bloom']} "
                          f"active_seq={f['active_seq']}")
        # Union view + self-cross-check. ACTIVE = surfaced (+0x758) == 1.
        fields = pr.get("fields", [])
        larges = [f for f in fields if f["value"] == LARGE_VALUE]
        active = [f for f in larges if f.get("active")]
        print(f"  UNION: {len(fields)} field(s), {len(larges)} Large, "
              f"{len(active)} active Large (surfaced +0x758==1 OR mined +0x4D0==1)")
        for f in sorted(larges, key=lambda f: (not f.get("active"), f["sector"])):
            tag = "  <== ACTIVE" if f.get("active") else "  (candidate)"
            print(f"    LARGE  sector={f['sector']:3} ({f['x']:.0f},{f['y']:.0f}) "
                  f"surfaced@0x758={f.get('surfaced')} active@0x4D0={f['active_flag']} "
                  f"active_seq={f['active_seq']} bloom={f['bloom']}{tag}")
        # cross-check: the value==2.5M & active==1 filter must agree with the union
        # active-Large pick (sanity that the offsets compose correctly).
        filt = [f for f in fields if f["value"] == LARGE_VALUE and f.get("active")]
        ok = ([f["obj"] for f in active] == [f["obj"] for f in filt])
        print(f"  cross-check: union-active-Large == value/active filter -> "
              f"{'OK' if ok else 'MISMATCH'} "
              f"({[f['sector'] for f in active]} vs {[f['sector'] for f in filt]})")
    return 0


def main(argv) -> int:
    verify = "--verify" in argv
    first = "--first" in argv           # stop at the first Large (fast offset validation)
    resolve = "--resolve-only" in argv  # fast: print resolved vptrs, no heap scan
    system = "--system" in argv         # v2: read USpiceHarvestingSystem array directly
    want_pid = None
    for a in argv:
        if a.startswith("--pid="):
            want_pid = int(a.split("=", 1)[1])
    if want_pid is not None:
        m = re.search(r"-PartitionIndex=(\d+)", _cmdline(want_pid))
        pids = [(want_pid, PARTITION_DIM.get(int(m.group(1))) if m else None)]
    else:
        pids = game_pids()
    if not pids:
        print(json.dumps({"error": f"no {BINNAME} DeepDesert_1 process found"}))
        return 1
    if resolve:
        return resolve_only(pids)

    # v2 system-array path (additive; v1 heap scan stays the default until this is
    # live-validated). Locates the singleton USpiceHarvestingSystem and reads its
    # candidate arrays directly. Same output contract as v1 + a `candidates` list.
    if system:
        procs = [scan_pid_system(p, dim) for p, dim in pids]
        if verify:
            return _system_verify(procs)
        out = []
        for pr in procs:
            spice = [f for f in pr.get("fields", []) if f["kind"] == "spice"]
            larges = [f for f in spice if f["value"] == LARGE_VALUE]
            active = [f for f in larges if f.get("active")]
            large = active[0] if active else None
            out.append({"pid": pr["pid"], "dim": pr.get("dim"), "pod": pr.get("pod"),
                        "vptr_source": pr.get("vptr_source"),
                        "large": large, "actives": active, "spice_count": len(spice),
                        "large_resident": len(larges), "large_active": len(active),
                        "candidates": _candidates(spice),
                        "mediums": _mediums(spice),
                        "error": pr.get("error")})
        print(json.dumps({"processes": out}, separators=(",", ":")))
        return 0

    procs = [scan_pid(p, dim, stop_on_large=first, progress=verify) for p, dim in pids]

    if verify:
        for pr in procs:
            print(f"\n=== pid {pr['pid']} dim={pr.get('dim')} pod={pr.get('pod')} "
                  f"base={pr.get('module_base')} vptr={pr.get('vptr_source')}({pr.get('vptrs')}) ===")
            if pr.get("error"):
                print("  ERROR:", pr["error"]); continue
            fields = pr["fields"]
            larges = [f for f in fields if f["value"] == LARGE_VALUE]
            active = [f for f in larges if f.get("active")]
            print(f"  total ASpiceField/FlourSand hits: {len(fields)}")
            print(f"  LARGE (value=2,500,000): {len(larges)} resident, {len(active)} ACTIVE (+0x4D0==1)")
            # Always dump every Large with its discriminator so a single live pass
            # confirms the +0x4D0==1 picker against ground truth (active_flag is the
            # authority; bloom/active_seq shown for diagnostics).
            for f in sorted(larges, key=lambda f: (not f.get("active"), f["sector"])):
                tag = "  <== ACTIVE" if f.get("active") else "  (candidate)"
                print(f"    LARGE  sector={f['sector']:3} ({f['x']:.0f},{f['y']:.0f}) "
                      f"active@0x4D0={f.get('active_flag')} bloom={f.get('bloom')} "
                      f"active_seq={f.get('active_seq')} "
                      f"band={'OK' if f['sector'][0] in 'DEFGHI' else 'OUT'}{tag}")
            for f in sorted(fields, key=lambda f: -f["value"])[:12]:
                tag = "  <== LARGE" if f["value"] == LARGE_VALUE else ""
                print(f"    {f['kind']:9} value={f['value']:>9} sector={f['sector']:3} "
                      f"({f['x']:.0f},{f['y']:.0f}) bloom={f.get('bloom')} "
                      f"band={'OK' if f['sector'][0] in 'DEFGHI' else 'OUT'}{tag}")
        return 0

    # machine output: the active Large per process. Among the resident Larges (all
    # tie at value=2,500,000) the live one is the field whose +0x4D0 byte == 1
    # (server-designated active blow). If none is flagged we emit large=None
    # (between rotations) rather than guess a candidate.
    out = []
    for pr in procs:
        spice = [f for f in pr.get("fields", []) if f["kind"] == "spice"]
        larges = [f for f in spice if f["value"] == LARGE_VALUE]
        active = [f for f in larges if f.get("active")]
        large = active[0] if active else None
        out.append({"pid": pr["pid"], "dim": pr.get("dim"), "pod": pr.get("pod"),
                    "vptr_source": pr.get("vptr_source"),
                    "large": large, "actives": active, "spice_count": len(spice),
                    "large_resident": len(larges), "large_active": len(active),
                    "error": pr.get("error")})
    print(json.dumps({"processes": out}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
