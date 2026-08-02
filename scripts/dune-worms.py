#!/usr/bin/env python3
"""Live sandworm tracker producer for the public Deep Desert map (READ-ONLY).

Deep Desert pods broadcast full per-worm state to stdout continuously
(`LogDuneSandworm`), so we do not need RAM scans or RE for this: a bounded
`kubectl logs --tail` of each DD pod carries every active worm's latest position
and threat state. We parse the bracket that every line shares:

  [BP_Crea_SandwormArrakis_C_..., ID: 255, V(X=-1239514.01, Y=-1024690.36,
   Z=-1485.50), RootBoneZ: 77, ESandwormSteeringMode::Roam, ...,
   InSafeZone:0, Enraged:1, ...] <event>

and the trailing <event> (is shown / is hidden / wants to breach / teleport and
execute attack|breach / Enrage changed / ...) to derive a surface + threat state.
We keep the LATEST line per worm ID within the tail window and emit, per Deep
Desert dimension, the worms with a position + danger flags. The portal normalizes
the world (x, y) to map coordinates with the same calibration the static markers
use, and draws a worm layer with a danger pulse on surfaced/enraged worms.

Output (stdout JSON):
  {"dimensions": {"0": {"label": "PvE", "worms": [ {worm}, ... ]},
                  "1": {"label": "PvP", "worms": [ ... ]}},
   "generated_utc": "..."}
  worm = {id, x, y, sector, surfaced, enraged, wants_breach, attacking,
          steering, in_safe_zone, threat, last_seen_utc, age_s}

Read-only: only `kubectl logs` (container stdout, cheap — NOT a RAM scan). Never
writes the DB, never touches the game pods. Safe to run on the live box.

Deployed to <box>:/root/dune-worms.py, invoked via the relay dispatcher action
`worms`.
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
# dim 0 = PvE, dim 1 = PvP (same mapping the spice pipeline uses)
PODS = {
    0: ("PvE", "sh-<your-hostid>-<random>-sg-deepdesert-1-pod-8"),
    1: ("PvP", "sh-<your-hostid>-<random>-sg-deepdesert-1-pod-31"),
}
KUBECTL = "/usr/local/bin/kubectl"

# How many recent log lines to scan per pod. Worm lines are ~35% of DD log
# volume and all ~9 worms chatter frequently (is shown/hidden every few sec), so
# 3000 lines covers a few minutes = every active worm's latest position with room
# to spare. Bounded so this stays cheap.
TAIL = 3000

# A worm whose last log line is older than this is treated as gone (pod just
# started, worm despawned, or the tail window did not reach it). Generous on
# purpose: with players on the map worms chatter constantly, but they go quiet
# during lulls (and on an empty server), so we keep the last-known position and
# let the UI FADE it by age_s rather than dropping the pin too eagerly.
MAX_AGE_S = 900

# Momentary threat states (attacking / wants-to-breach) are point-in-time events;
# if the worm has gone quiet for longer than this we no longer trust the flag and
# fall back to its surface/enrage state (otherwise a worm frozen mid-attack on a
# quiet server reads "attacking" forever). Surfaced/enraged are steadier states.
TRANSIENT_TTL_S = 90

# Authoritative DD calibration (matches dune-spice-active.py / map_model):
# world bounds Min=(-1270000,-1270000) Max=(1168400,1168400); 9x9 grid,
# rows A..I SOUTH->NORTH (A=south/+y), cols 1..9 WEST->EAST.
MAP_MIN = -1270000.0
MAP_MAX = 1168400.0
SECTOR = (MAP_MAX - MAP_MIN) / 9.0

# --- line parsing ------------------------------------------------------------
RE_TS = re.compile(r"^\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")
RE_WORM_LINE = re.compile(r"LogDuneSandworm: Log: \[(?P<body>.*)\]\s*(?P<event>.*)$")
RE_ID = re.compile(r"\bID:\s*(\d+)")
RE_V = re.compile(r"V\(X=(-?[\d.]+),\s*Y=(-?[\d.]+),\s*Z=(-?[\d.]+)\)")
RE_STEER = re.compile(r"ESandwormSteeringMode::(\w+)")
RE_SAFE = re.compile(r"InSafeZone:\s*(\d)")
RE_ENRAGED = re.compile(r"Enraged:\s*(\d)")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(line: str) -> datetime | None:
    m = RE_TS.match(line)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        return None


def sector_for(x: float, y: float) -> str:
    col = max(1, min(9, int((x - MAP_MIN) / SECTOR) + 1))
    row_idx = max(0, min(8, int((MAP_MAX - y) / SECTOR)))   # 0=A(south) .. 8=I(north)
    return f"{chr(ord('A') + row_idx)}{col}"


def kubectl_logs(pod: str) -> list[str]:
    out = subprocess.run(
        [KUBECTL, "logs", "-n", NS, pod, "--tail", str(TAIL)],
        capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "kubectl logs failed").strip()[:200])
    return out.stdout.splitlines()


def classify(event: str) -> dict:
    """Trailing-event -> partial state delta. Lines are processed in order so the
    last delta per field wins.

    NOTE on the two attack types: a "vertical attack" is the worm's routine
    surface lunge (it fires constantly at wrecks/thumpers and is NOT a special
    danger), so it only marks the worm surfaced. A "breach" (the worm wants to
    breach / executes a breach) is the big eruption that swallows everything in
    the area -- THAT is the real harvester danger, so it sets wants_breach."""
    e = event.strip()
    if e == "is shown":
        return {"surfaced": True}
    if e == "is hidden":
        return {"surfaced": False, "wants_breach": False}
    if e == "wants to breach":
        return {"wants_breach": True, "surfaced": True}
    if e == "no longer wants to breach":
        return {"wants_breach": False}
    if "breach" in e and e.startswith("teleport and execute"):
        return {"wants_breach": True, "surfaced": True}     # breach eruption NOW
    if e.startswith("teleport and execute"):
        return {"surfaced": True}                            # routine vertical attack
    return {}


def threat_of(w: dict) -> str:
    """Coarse danger tier for the UI, most to least dangerous:
      breaching - erupting / about to erupt (the real harvester danger)
      enraged   - surfaced + aggressive
      surfaced  - up and roaming
      submerged - under the sand."""
    if w.get("wants_breach"):
        return "breaching"
    if w.get("surfaced") and w.get("enraged"):
        return "enraged"
    if w.get("surfaced"):
        return "surfaced"
    return "submerged"


def process_pod(dim: int, label: str, pod: str) -> list[dict]:
    try:
        lines = kubectl_logs(pod)
    except Exception as exc:
        # one bad pod must not sink the whole producer
        print(f"dune-worms: {pod}: {exc}", file=sys.stderr)
        return []
    worms: dict[str, dict] = {}
    for line in lines:
        lm = RE_WORM_LINE.search(line)
        if not lm:
            continue
        body, event = lm.group("body"), lm.group("event")
        idm = RE_ID.search(body)
        vm = RE_V.search(body)
        if not idm or not vm:
            continue
        wid = idm.group(1)
        ts = parse_ts(line)
        x, y = float(vm.group(1)), float(vm.group(2))
        w = worms.setdefault(wid, {"id": wid, "surfaced": False, "enraged": False,
                                   "wants_breach": False, "attacking": False})
        w["x"], w["y"] = x, y
        w["ts"] = ts
        sm = RE_STEER.search(body)
        if sm:
            w["steering"] = sm.group(1)
        safe = RE_SAFE.search(body)
        if safe:
            w["in_safe_zone"] = safe.group(1) == "1"
        enr = RE_ENRAGED.search(body)
        if enr:
            w["enraged"] = enr.group(1) == "1"
        w.update(classify(event))

    now = now_utc()
    out = []
    for w in worms.values():
        ts = w.get("ts")
        age = (now - ts).total_seconds() if ts else None
        if age is not None and age > MAX_AGE_S:
            continue
        # decay the momentary breach flag on a stale read (a breach is a
        # point-in-time eruption; an old reading should not stay "breaching")
        if age is not None and age > TRANSIENT_TTL_S:
            w["wants_breach"] = False
        out.append({
            "id": w["id"],
            "x": round(w["x"], 1),
            "y": round(w["y"], 1),
            "sector": sector_for(w["x"], w["y"]),
            "surfaced": bool(w.get("surfaced")),
            "enraged": bool(w.get("enraged")),
            "wants_breach": bool(w.get("wants_breach")),
            "steering": w.get("steering"),
            "in_safe_zone": bool(w.get("in_safe_zone")),
            "threat": threat_of(w),
            "last_seen_utc": ts.isoformat(timespec="seconds") if ts else None,
            "age_s": round(age) if age is not None else None,
        })
    # most-dangerous first (nice for any list rendering)
    tier = {"breaching": 0, "enraged": 1, "surfaced": 2, "submerged": 3}
    out.sort(key=lambda w: (tier.get(w["threat"], 9), w["id"]))
    return out


def main() -> int:
    dims_out = {}
    for dim, (label, pod) in PODS.items():
        dims_out[str(dim)] = {"label": label, "worms": process_pod(dim, label, pod)}
    print(json.dumps({"dimensions": dims_out,
                      "generated_utc": now_utc().isoformat(timespec="seconds")},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:300], "dimensions": {}}))
        sys.exit(1)
