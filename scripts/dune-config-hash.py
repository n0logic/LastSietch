#!/usr/bin/env python3
"""Config-drift hash watch for the pinned game pod's INI/cvar files.

Read-only. For each path in admin-backend/data/cvars-paths.json it runs
sha256sum inside the pinned pod via kubectl exec and compares against a stored
baseline. First run writes the baseline; later runs diff and, on any drift,
print a JSON alert blob to stdout AND exit nonzero.

v1 alert transport = stdout JSON + exit code only. Cielago/Discord routing is a
deferred fast-follow and is intentionally NOT wired here.

Usage:
  dune-config-hash.py                 # check against baseline (exit 2 on drift)
  dune-config-hash.py --update-baseline   # re-bless the current state
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import subprocess

CVARS_PATHS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "admin-backend", "data", "cvars-paths.json",
)
BASELINE_PATH = "/var/lib/lastsietch-config-watch/baseline.json"


def kubectl(args):
    return subprocess.run(["sudo", "kubectl", *args],
                          capture_output=True, text=True, timeout=60)


def load_paths():
    with open(CVARS_PATHS, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["namespace"], cfg["pod_pinned_for_v1"], cfg["container_paths"]


def resolve_pod(namespace, pinned):
    """Resolve the pinned pod's full name via its stable suffix."""
    r = kubectl(["get", "pods", "-n", namespace, "-o",
                 "jsonpath={.items[*].metadata.name}"])
    if r.returncode != 0:
        raise RuntimeError(f"kubectl get pods failed: {r.stderr.strip()}")
    for name in r.stdout.split():
        if name.endswith(pinned):
            return name
    raise RuntimeError(f"no pod matching '*{pinned}' in {namespace}")


def hash_file(namespace, pod, container_path):
    """Return (sha, size) for one file, or (None, None) if unreadable."""
    r = kubectl(["exec", "-n", namespace, pod, "--",
                 "sha256sum", container_path])
    if r.returncode != 0:
        return None, None
    sha = r.stdout.split()[0] if r.stdout.strip() else None
    s = kubectl(["exec", "-n", namespace, pod, "--",
                 "stat", "-c", "%s", container_path])
    size = int(s.stdout.strip()) if s.returncode == 0 and s.stdout.strip() else None
    return sha, size


def capture(namespace, pod, paths):
    now = datetime.now(timezone.utc).isoformat()
    state = {}
    for key, container_path in paths.items():
        sha, size = hash_file(namespace, pod, container_path)
        state[key] = {"path": container_path, "sha": sha, "size": size,
                      "captured_utc": now}
    return state


def write_baseline(state):
    os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
    tmp = BASELINE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BASELINE_PATH)


def load_baseline():
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


def diff(baseline, current):
    """Return a list of drift records for keys whose sha changed."""
    drifts = []
    for key, cur in current.items():
        base = baseline.get(key)
        if base is None:
            drifts.append({"key": key, "path": cur["path"], "reason": "new",
                           "old_sha": None, "new_sha": cur["sha"]})
            continue
        if cur["sha"] != base.get("sha"):
            drifts.append({
                "key": key, "path": cur["path"], "reason": "changed",
                "old_sha": base.get("sha"), "new_sha": cur["sha"],
                "old_size": base.get("size"), "new_size": cur["size"],
                "baseline_captured_utc": base.get("captured_utc"),
            })
    for key in baseline:
        if key not in current:
            drifts.append({"key": key, "path": baseline[key].get("path"),
                           "reason": "missing", "old_sha": baseline[key].get("sha"),
                           "new_sha": None})
    return drifts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true",
                    help="re-bless the current state as the new baseline")
    args = ap.parse_args()

    try:
        namespace, pinned, paths = load_paths()
        pod = resolve_pod(namespace, pinned)
        current = capture(namespace, pod, paths)
    except Exception as e:
        print(json.dumps({"available": False, "error": str(e)}))
        return 1

    if args.update_baseline or not os.path.exists(BASELINE_PATH):
        write_baseline(current)
        print(json.dumps({
            "available": True, "drift": False,
            "baseline_written": True, "pod": pod,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "tracked": len(current),
        }))
        return 0

    try:
        baseline = load_baseline()
    except Exception as e:
        print(json.dumps({"available": False, "error": f"baseline load failed: {e}"}))
        return 1

    drifts = diff(baseline, current)
    blob = {
        "available": True,
        "drift": bool(drifts),
        "pod": pod,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "tracked": len(current),
        "drifts": drifts,
    }
    print(json.dumps(blob))
    return 2 if drifts else 0


if __name__ == "__main__":
    sys.exit(main())
