#!/usr/bin/env python3
"""Sandstorm ETA forecast producer for the public Deep Desert map (READ-ONLY).

Deep Desert pods log a sandstorm spawn as two lines with NO coordinates, path,
or intensity -- only a timestamp:

  LogSandStorm: Log: Sandstorm BeginPlay
  LogSandStormManager: Log: Requested a Sandstorm auto-spawn

So there is no live storm position to draw; the useful signal is the SPAWN
CADENCE. We parse the `Sandstorm BeginPlay` lines from a bounded `kubectl logs
--tail` of each DD pod, take the newest spawn as `last_spawn_utc`, derive the
mean inter-spawn interval from the in-log spawn history (falling back to a
measured ~60min baseline when the window holds fewer than 3 spawns), and emit a
TIME-ONLY ETA per Deep Desert dimension (next_eta_utc = last_spawn + mean
interval). Confidence is derived from the interval spread (tighter spacing =>
higher confidence). The two dimensions storm independently, so each is computed
on its own.

Output (stdout JSON):
  {"dimensions": {
     "0": {"label": "PvE", "last_spawn_utc": ..., "next_eta_utc": ...,
           "mean_interval_min": ..., "confidence": ..., "samples": N},
     "1": {"label": "PvP", ...}},
   "generated_utc": "...", "available": true}

Graceful degrade: {"dimensions": {}, "available": false} on any failure.

Read-only: only `kubectl logs` (container stdout, cheap -- NOT a RAM scan).
Never writes the DB, never touches the game pods. Safe to run on the live box.

Deployed to <box>:/root/dune-sandstorm.py, invoked via the relay dispatcher
action `sandstorm`.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
# dim 0 = PvE, dim 1 = PvP (same mapping the spice/worm pipelines use)
PODS = {
    0: ("PvE", "sh-<your-hostid>-<random>-sg-deepdesert-1-pod-8"),
    1: ("PvP", "sh-<your-hostid>-<random>-sg-deepdesert-1-pod-31"),
}
KUBECTL = "/usr/local/bin/kubectl"

# How many recent log lines to scan per pod. Sandstorm BeginPlay lines are rare
# (~1/hour), so a large tail is needed to capture several spawns for a cadence
# estimate. Bounded so this stays cheap.
TAIL = 200000

# Measured cadence baseline (PvE mean 60.0min, PvP mean 60.7min); used when the
# log window holds fewer than MIN_SAMPLES spawns to compute an interval.
DEFAULT_INTERVAL_MIN = 60.0
MIN_SAMPLES = 3

# Persistent spawn history. A DD pod's kubectl-logs are rotated by the container
# runtime (measured 2026-07-07: pod-8/PvE retains only ~2.7h, pod-31/PvP ~8h), so
# a single log tail sees only a few spawns -- PvE hovers at 2-3 samples, giving a
# weak cadence/confidence. We persist every detected spawn per dim and compute the
# cadence from the UNION of the live-log spawns and this history, so samples
# accumulate over time despite rotation. History only ADDS older real spawns; the
# latest spawn (and thus the active-window trigger the ram-cache reads) always
# comes from the live log, so a stale history can never fabricate an active storm.
# Bounded to HISTORY_KEEP_HOURS so it stays small and ancient intervals can't skew.
SPAWN_HISTORY = "/root/dune-sandstorm-history.json"
HISTORY_KEEP_HOURS = 48

RE_TS = re.compile(r"^\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")
RE_SPAWN = re.compile(r"LogSandStorm: Log: Sandstorm BeginPlay")


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


def load_history() -> dict:
    """{dim_str: [spawn_iso, ...]} persisted across runs. Degrades to {} on any
    failure -- history is an augmentation, never a dependency."""
    try:
        with open(SPAWN_HISTORY) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_history(hist: dict) -> None:
    """Atomic write; a persistence failure must not break the producer."""
    try:
        tmp = f"{SPAWN_HISTORY}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(hist, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SPAWN_HISTORY)
    except OSError:
        pass


def _merge_spawn_history(log_spawns: list[datetime], prior_iso: list) -> list[datetime]:
    """Union the live-log spawns with the persisted history, dedup to the second,
    and drop anything older than HISTORY_KEEP_HOURS. Returns sorted datetimes."""
    cutoff = now_utc() - timedelta(hours=HISTORY_KEEP_HOURS)
    seen: dict[str, datetime] = {}
    for ts in log_spawns:
        seen[ts.isoformat(timespec="seconds")] = ts
    for iso in (prior_iso or []):
        try:
            ts = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        seen.setdefault(ts.isoformat(timespec="seconds"), ts)
    return sorted(t for t in seen.values() if t >= cutoff)


def kubectl_logs(pod: str) -> list[str]:
    out = subprocess.run(
        [KUBECTL, "logs", "-n", NS, pod, "--tail", str(TAIL)],
        capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "kubectl logs failed").strip()[:200])
    return out.stdout.splitlines()


def confidence_from(intervals: list[float], mean: float) -> float:
    """Map the interval spread to a 0..1 confidence. Tight, consistent spacing
    => high confidence; wide scatter => low. Coefficient of variation (stddev /
    mean) drives it, clamped so a single-sample fallback still reads as moderate."""
    if len(intervals) < 2 or mean <= 0:
        return 0.5
    n = len(intervals)
    var = sum((iv - mean) ** 2 for iv in intervals) / n
    std = var ** 0.5
    cv = std / mean
    return round(max(0.0, min(1.0, 1.0 - cv)), 2)


def process_pod(label: str, pod: str, prior_iso: list) -> tuple[dict | None, list]:
    """Returns (entry, updated_history_iso). On a kubectl failure we keep the prior
    history untouched and emit nothing for this dim (so the active-window trigger
    never fires on stale data). The cadence is computed from the UNION of the live
    log and the persisted history so PvE (short log retention) accumulates samples."""
    try:
        lines = kubectl_logs(pod)
    except Exception as exc:
        # one bad pod must not sink the whole producer
        print(f"dune-sandstorm: {pod}: {exc}", file=sys.stderr)
        return None, prior_iso

    log_spawns: list[datetime] = []
    for line in lines:
        if not RE_SPAWN.search(line):
            continue
        ts = parse_ts(line)
        if ts is not None:
            log_spawns.append(ts)

    spawns = _merge_spawn_history(log_spawns, prior_iso)
    updated_iso = [t.isoformat(timespec="seconds") for t in spawns]
    if not spawns:
        return None, updated_iso

    intervals = [
        (spawns[i] - spawns[i - 1]).total_seconds() / 60.0
        for i in range(1, len(spawns))
    ]
    if len(spawns) >= MIN_SAMPLES and intervals:
        mean = sum(intervals) / len(intervals)
    else:
        mean = DEFAULT_INTERVAL_MIN

    last = spawns[-1]
    nxt = last + timedelta(minutes=mean)
    return {
        "label": label,
        "last_spawn_utc": last.isoformat(timespec="seconds"),
        "next_eta_utc": nxt.isoformat(timespec="seconds"),
        "mean_interval_min": round(mean, 1),
        "confidence": confidence_from(intervals, mean),
        "samples": len(spawns),
    }, updated_iso


# --- live storm-position merge (additive; from the ram-cache timer) ----------
# dune-storm-ramcache.py heap-scans the live storm actor during its sweep and
# writes this cache; we fold the active storm's CENTER/radius/heading/stage into
# the per-dim payload alongside the ETA. A storm moves ~47 m/s, so a stale
# position would mislead -> drop anything older than STORM_POS_STALE_SECS (scans
# take ~1-2 min, so a fresh cache is ~1-2 min old; allow headroom). Degrades
# silently to ETA-only when the cache is missing / stale / inactive.
STORM_CACHE = "/root/dune-storm-ramcache.json"
STORM_POS_STALE_SECS = 240
# v1.1: the reader stamps ended_utc when it sees a tracked storm despawn. Propagate
# a RECENT end so the portal can stop the desert-wide sweep immediately instead of
# coasting its last_spawn<18m timing window (the post-storm "sweep tail"). The portal
# only honors it when the end is at/after the latest spawn, so a stale end from a
# prior storm is naturally superseded once a new storm spawns; a generous window is
# safe. 30 min covers the whole sweep + grace.
STORM_ENDED_FRESH_SECS = 1800


def _age_secs(iso) -> float:
    if not iso:
        return float("inf")
    try:
        return (now_utc() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


def _merge_storm_position(dims_out: dict) -> None:
    try:
        with open(STORM_CACHE) as f:
            cache = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    for d, info in (cache.get("dimensions") or {}).items():
        if not isinstance(info, dict):
            continue
        ent = dims_out.get(str(d))
        if not isinstance(ent, dict):
            continue
        if not info.get("active"):
            # Storm not currently tracked. Surface a RECENT despawn so the portal
            # can end the sweep at once (the reader confirmed the storm is gone),
            # instead of coasting its last_spawn<18m window. No position folded.
            ended = info.get("ended_utc")
            if ended and _age_secs(ended) < STORM_ENDED_FRESH_SECS:
                ent["storm_ended_utc"] = ended
            continue
        if _age_secs(info.get("scanned_utc")) > STORM_POS_STALE_SECS:
            continue
        x, y = info.get("x"), info.get("y")
        if x is None or y is None:
            continue
        # Field names match what _sandstorm_overlay() projects: center_x/center_y
        # -> center_nx/center_ny, radius -> radius_nr; heading_yaw/stage pass thru.
        ent["center_x"] = x
        ent["center_y"] = y
        ent["radius"] = info.get("radius")        # +0x59C effect, linear cm
        ent["heading_yaw"] = info.get("heading")  # quat yaw deg
        ent["stage"] = info.get("stage")
        ent["storm_sector"] = info.get("sector")
        ent["storm_scanned_utc"] = info.get("scanned_utc")


def main() -> int:
    history = load_history()
    dims_out = {}
    for dim, (label, pod) in PODS.items():
        entry, updated_iso = process_pod(label, pod, history.get(str(dim), []))
        history[str(dim)] = updated_iso
        if entry is not None:
            dims_out[str(dim)] = entry
    save_history(history)
    _merge_storm_position(dims_out)
    print(json.dumps({"dimensions": dims_out,
                      "generated_utc": now_utc().isoformat(timespec="seconds"),
                      "available": bool(dims_out)},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"dimensions": {}, "available": False,
                          "error": str(exc)[:300]}))
        sys.exit(1)
