"""
World-events stream: sandstorm spawns + sandworm breaches from pod logs.

Two low-frequency map events that no DB table records, harvested straight from
the game pods' stdout via a bounded `kubectl logs --tail` (the same read-only
mechanism scripts/dune-sandstorm.py and scripts/dune-worms.py use):

  - sandstorm  : "LogSandStorm: Log: Sandstorm BeginPlay" (one per spawn)
  - worm_breach: a LogDuneSandworm line whose trailing event is the actual
                 breach eruption ("teleport and execute ... breach") -- NOT the
                 "wants to breach" intent line, which only signals the worm is
                 lining one up.

Storms are counted per map (HaggaBasin = sg-survival pods, DeepDesert =
sg-deepdesert pods); worm breaches are Deep-Desert only (no worms in Hagga).
We tail every relevant pod and parse both line kinds; pods that never emit a
given line simply contribute nothing.

Dedup: dedup_key = (event_type, dimension, log timestamp [, worm id]) so the
overlapping tails of consecutive runs are idempotent against the UNIQUE index.
The dimension is the stable `sg-...-pod-N` segment of the pod name (the sh-hash
prefix changes on a battlegroup rebuild, which is a new world anyway).

Read-only: only `kubectl get pods` + `kubectl logs` (container stdout, cheap --
NOT a RAM scan). Never writes the game DB, never mutates k8s. Writes ONLY to the
local SQLite store.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timezone

import db

log = logging.getLogger("telemetry.world_events")

# How many recent log lines to tail per pod. Matches dune-sandstorm.py: spawn /
# breach lines are rare, so a large bounded tail is needed to span several hours
# of logs. With the interval kept well under that span, consecutive tails overlap
# and the UNIQUE dedup makes re-ingest idempotent.
TAIL = 200_000

COLUMNS = ["ts", "map", "dimension", "event_type", "dedup_key"]

RE_TS = re.compile(r"^\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")
RE_SPAWN = re.compile(r"LogSandStorm: Log: Sandstorm BeginPlay")
RE_WORM_LINE = re.compile(r"LogDuneSandworm: Log: \[(?P<body>.*)\]\s*(?P<event>.*)$")
RE_ID = re.compile(r"\bID:\s*(\d+)")
# Stable, rebuild-agnostic dimension token inside the full pod name.
RE_POD_DIM = re.compile(r"(sg-(?:deepdesert|survival)-\d+-pod-\d+)")


def _kubectl(args, timeout=90):
    res = subprocess.run(
        ["kubectl"] + args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            "kubectl %s failed: %s" % (" ".join(args), res.stderr.strip()[:200]))
    return res.stdout


def _target_pods(ns):
    out = _kubectl(["get", "pods", "-n", ns, "-o", "name"], timeout=20)
    pods = [l.split("/", 1)[1] for l in out.splitlines() if l.strip()]
    return [p for p in pods if "sg-deepdesert" in p or "sg-survival" in p]


def _map_for(pod):
    return "DeepDesert" if "sg-deepdesert" in pod else "HaggaBasin"


def _dimension_for(pod):
    m = RE_POD_DIM.search(pod)
    return m.group(1) if m else pod


def _parse_ts(line):
    """Return (epoch_seconds, 'YYYYMMDDHHMMSS' token) or (None, None)."""
    m = RE_TS.match(line)
    if not m:
        return None, None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    try:
        dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        return None, None
    return int(dt.timestamp()), "%04d%02d%02d%02d%02d%02d" % (y, mo, d, h, mi, s)


def _is_breach(event):
    """A real breach eruption, not the 'wants to breach' intent line.

    Mirrors dune-worms.py classify(): the worm's routine surface lunge is
    'teleport and execute attack'; the big eruption is 'teleport and execute
    ... breach'. We count only the eruption."""
    e = event.strip()
    return e.startswith("teleport and execute") and "breach" in e


def _parse_lines(lines, mapname, dimension):
    out = []
    for line in lines:
        if RE_SPAWN.search(line):
            epoch, token = _parse_ts(line)
            if epoch is None:
                continue
            out.append((epoch, mapname, dimension, "sandstorm",
                        "sandstorm|%s|%s" % (dimension, token)))
            continue
        wm = RE_WORM_LINE.search(line)
        if wm and _is_breach(wm.group("event")):
            epoch, token = _parse_ts(line)
            if epoch is None:
                continue
            idm = RE_ID.search(wm.group("body"))
            wid = idm.group(1) if idm else "?"
            out.append((epoch, mapname, dimension, "worm_breach",
                        "worm_breach|%s|%s|%s" % (dimension, token, wid)))
    return out


def run(ctx):
    ns = ctx.config.game_ns
    if not ns:
        log.warning("world_events: GAME_NS not set, skipping")
        return
    try:
        pods = _target_pods(ns)
    except Exception as exc:  # noqa: BLE001 - one bad enum must not stop others
        log.warning("world_events: pod enumeration failed: %s", exc)
        return
    if not pods:
        log.warning("world_events: no deepdesert/survival pods in %s", ns)
        return

    rows = []
    for pod in pods:
        try:
            lines = _kubectl(["logs", "-n", ns, pod, "--tail", str(TAIL)]).splitlines()
        except Exception as exc:  # noqa: BLE001 - skip the bad pod, keep the rest
            log.warning("world_events: tail %s failed: %s", pod, exc)
            continue
        rows.extend(_parse_lines(lines, _map_for(pod), _dimension_for(pod)))

    written = db.insert_many_ignore(ctx.store, "world_events", COLUMNS, rows)
    log.info("world_events: %d pods, %d events parsed, %d new",
             len(pods), len(rows), written)


STREAM = {"name": "world_events",
          "interval_attr": "world_events_interval",
          "run": run}
