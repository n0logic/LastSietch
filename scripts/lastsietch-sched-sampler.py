#!/usr/bin/env python3
# Per-second game-thread scheduling-delay recorder for the base-map pods.
#
# Why: rubber-banding = the 30 Hz game thread missing its tick deadline when other
# threads preempt it for 10-50 ms. Average tools (top/vmstat, 15s schedstat means)
# dilute those stalls to nothing; the harm lives in the WORST second. This records
# per-second run-queue wait per base-map game process plus the host ctxt rate, so a
# player report with a timestamp ("stutter about a minute ago") has matching data.
#
# Method matches arrakis-command-nexus cpu-pin.sh --measure (main-thread
# /proc/<pid>/schedstat: field1=on-cpu ns, field2=runqueue-wait ns), taken per
# second instead of once over 15s. Needs kernel.sched_schedstats=1.
#
# Read-only on the cluster; writes one CSV/day under /root/lastsietch-sched-sampler/.
# Deployed via systemd-run transient unit, RuntimeMaxSec-capped. Kill any time.

import json, os, subprocess, time

NS = "funcom-seabass-sh-<your-hostid>-<random>"
OUT_DIR = "/root/lastsietch-sched-sampler"
POD_MATCH = ("sg-survival", "sg-deepdesert", "sg-sh-arrakeen", "sg-sh-harkovillage")
REFRESH_S = 60
# static column order — a pod restarting must not shift columns
COLUMNS = ["sg-survival-1-pod-1", "sg-survival-1-pod-32", "sg-survival-1-pod-33",
           "sg-deepdesert-1-pod-8", "sg-deepdesert-1-pod-31",
           "sg-sh-arrakeen-pod-3", "sg-sh-harkovillage-pod-4"]


def pod_uid_map():
    out = subprocess.run(
        ["kubectl", "get", "pods", "-n", NS, "-o", "json"],
        capture_output=True, text=True, timeout=30).stdout
    m = {}
    for it in json.loads(out)["items"]:
        name = it["metadata"]["name"]
        if any(s in name for s in POD_MATCH):
            m[it["metadata"]["uid"].replace("-", "_")] = name.split("nhzgrx-")[-1]
    return m


def game_pids(uidmap):
    pids = {}
    for pid in subprocess.run(["pgrep", "-x", "DuneSandboxServ"],
                              capture_output=True, text=True).stdout.split():
        try:
            cg = open(f"/proc/{pid}/cgroup").read()
        except OSError:
            continue
        for uid, name in uidmap.items():
            if uid in cg or uid.replace("_", "-") in cg:
                # two DuneSandboxServ per pod: keep the one accruing CPU (the game)
                st = open(f"/proc/{pid}/schedstat").read().split()
                if int(st[0]) > 10_000_000_000:  # >10s cpu since start = the real one
                    pids[name] = pid
    return pids


def read_stats(pids):
    ctxt = 0
    for line in open("/proc/stat"):
        if line.startswith("ctxt"):
            ctxt = int(line.split()[1])
            break
    per = {}
    for name, pid in pids.items():
        try:
            r, w, _ = open(f"/proc/{pid}/schedstat").read().split()
            per[name] = (int(r), int(w))
        except OSError:
            per[name] = None
    return ctxt, per


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uidmap = pod_uid_map()
    pids = game_pids(uidmap)
    last_refresh = time.time()
    ctxt_p, per_p = read_stats(pids)
    fh, fh_day = None, None
    while True:
        time.sleep(1)
        ctxt_n, per_n = read_stats(pids)
        day = time.strftime("%Y%m%d", time.gmtime())
        if day != fh_day:
            if fh:
                fh.close()
            fh = open(f"{OUT_DIR}/sched-{day}.csv", "a", buffering=1)
            if fh.tell() == 0:
                fh.write("ts,ctxt_per_sec," +
                         ",".join(f"{n}_wait_ms" for n in COLUMNS) + "\n")
            fh_day = day
        row = [time.strftime("%H:%M:%S", time.gmtime()), str(ctxt_n - ctxt_p)]
        for name in COLUMNS:
            a, b = per_p.get(name), per_n.get(name)
            row.append(f"{(b[1]-a[1])/1e6:.1f}" if a and b else "")
        fh.write(",".join(row) + "\n")
        ctxt_p, per_p = ctxt_n, per_n
        if time.time() - last_refresh > REFRESH_S:
            try:
                uidmap = pod_uid_map()
                new = game_pids(uidmap)
                if new:
                    pids = new
            except Exception:
                pass
            last_refresh = time.time()
            ctxt_p, per_p = read_stats(pids)


if __name__ == "__main__":
    main()
