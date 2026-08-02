#!/usr/bin/env python3
"""One-shot investigation probe: which RAM Large ASpiceField actor is the ACTIVE one?

The DB (dune.resourcefield_state) holds exactly ONE Large (value=2,500,000) per dim
= the active field, identified by its int64 field_id. RAM holds N Large actors (the
active crater + pre-spawned dormant candidates), all tying on value and (on a fresh
boot) all bloom>=0, so value/bloom can't isolate the active one.

This probe tests the clean hypothesis: the active actor stores its own field_id in
memory (it must, to persist UpdateResourceFieldStates). For each Large actor it
searches the object window for the DB's active field_id int64. If exactly one actor
per dim contains it, that actor is the active field and the byte offset is the
permanent discriminator.

Also dumps a structural diff of every int64 lane across the coexisting Larges so any
OTHER distinguishing field (not just field_id) surfaces.

Read-only. Run as root on the box hosting the DD server processes.
  sudo python3 dune-spice-fieldid-probe.py
"""
import importlib.util
import json
import struct
import subprocess
import sys

READER = "/root/spice/dune-spice-ramread.py"
NS = "funcom-seabass-sh-<your-hostid>-<random>"
DBPOD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"
WINDOW = 0x1000          # bytes of object memory to scan from obj base
LARGE_VALUE = 2_500_000


def load_reader():
    spec = importlib.util.spec_from_file_location("ramread", READER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def db_active_fieldids() -> dict:
    """{dim: field_id_int} for the single active Large per dim, live from the DB."""
    pw = subprocess.run(["kubectl", "exec", "-n", NS, DBPOD, "--",
                         "printenv", "POSTGRES_PASSWORD"],
                        capture_output=True, text=True, timeout=20).stdout.strip()
    sql = ("SELECT dimension_index||','||field_id FROM dune.resourcefield_state "
           "WHERE map='DeepDesert' AND field_kind_id=1 AND value_remaining=2500000 "
           "ORDER BY dimension_index;")
    out = subprocess.run(["kubectl", "exec", "-n", NS, DBPOD, "--", "env",
                          f"PGPASSWORD={pw}", "psql", "-h", "localhost", "-p", "15432",
                          "-U", "postgres", "-d", "dune", "-tAc", sql],
                         capture_output=True, text=True, timeout=30).stdout
    res = {}
    for ln in out.splitlines():
        ln = ln.strip()
        if "," in ln:
            d, fid = ln.split(",", 1)
            res[int(d)] = int(fid)
    return res


def main():
    rr = load_reader()
    active = db_active_fieldids()
    print(f"DB active Large field_ids per dim: {active}\n")

    pids = rr.game_pids()
    if not pids:
        print("no DeepDesert game process found"); return 1

    for pid, dim in pids:
        res = rr.scan_pid(pid, dim, progress=False)
        larges = [f for f in res.get("fields", []) if f["value"] == LARGE_VALUE]
        print(f"=== pid {pid} dim={dim} pod={res.get('pod')} : "
              f"{len(larges)} Large actor(s) ===")
        if not larges:
            continue
        target = active.get(dim)
        target_bytes = struct.pack("<q", target) if target is not None else None
        print(f"  looking for DB active field_id {target} "
              f"(hex {target:#018x})" if target else "  (no DB target for this dim)")

        # read each actor's object window
        windows = {}
        with open(f"/proc/{pid}/mem", "rb", 0) as mem:
            for f in larges:
                obj = int(f["obj"], 16)
                try:
                    mem.seek(obj)
                    windows[f["sector"]] = (obj, mem.read(WINDOW))
                except OSError as e:
                    windows[f["sector"]] = (obj, b"")
                    print(f"  {f['sector']}: read error {e}")

        # 1) field_id search per actor
        for f in larges:
            sec = f["sector"]
            obj, buf = windows[sec]
            note = f"bloom={f.get('bloom')} active_seq={f.get('active_seq')}"
            hit = ""
            if target_bytes:
                idx = buf.find(target_bytes)
                # report every occurrence + offset
                offs = []
                while idx != -1:
                    offs.append(idx)
                    idx = buf.find(target_bytes, idx + 1)
                hit = (f"  <== CONTAINS field_id @offset(s) "
                       f"{[hex(o) for o in offs]}" if offs else "")
            print(f"    {sec:3} obj={f['obj']} ({f['x']:.0f},{f['y']:.0f}) {note}{hit}")

        # 2) structural int64-lane diff across the coexisting Larges (catch any
        #    other discriminator: a lane that differs and tracks the active one)
        if len(larges) > 1 and len(set(w[1] and len(w[1]) for w in windows.values())) <= 2:
            secs = [f["sector"] for f in larges]
            print(f"  --- int64 lanes that DIFFER across {secs} (offset: values) ---")
            n = min(len(windows[s][1]) for s in secs) // 8
            diffs = []
            for i in range(n):
                off = i * 8
                vals = []
                ok = True
                for s in secs:
                    b = windows[s][1][off:off+8]
                    if len(b) < 8:
                        ok = False; break
                    vals.append(struct.unpack("<q", b)[0])
                if not ok:
                    continue
                if len(set(vals)) > 1:
                    diffs.append((off, vals))
            # print compactly, skip the obvious pointer lanes (vptr, root) for signal
            shown = 0
            for off, vals in diffs:
                # heuristic: highlight lanes that are small ints or look like ids,
                # not heap pointers (0x5xxx.. / 0x7fxx.. big addresses)
                small = all(-1 < v < 1_000_000_000 or v == -1 for v in vals)
                idlike = any(v == target for v in vals) if target else False
                tag = ""
                if idlike:
                    tag = "  <== field_id"
                elif small:
                    tag = "  (small/int)"
                if small or idlike:
                    print(f"    +{off:#06x}: {[hex(v & 0xFFFFFFFFFFFFFFFF) for v in vals]}{tag}")
                    shown += 1
            print(f"  ({len(diffs)} differing lanes total, {shown} small/id-like shown)")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
