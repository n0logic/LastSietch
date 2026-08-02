#!/usr/bin/env python3
# State-machine tests for the v1.1 enhanced storm-position cache
# (dune-storm-ramcache.py). Drives the full storm lifecycle with the reader
# subprocess calls mocked, so it runs anywhere (no live game box, no /proc/mem).
#
# Proves the two goals: (1) a learned instance list lets the cheap poll catch a
# storm's activation WITHOUT a heap walk (kills birth latency), even between
# cadence windows; (2) a despawn flips to inactive immediately with ended_utc.
import importlib.util
import os
import pathlib
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "storm_ramcache", REPO / "scripts" / "dune-storm-ramcache.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _active(obj="0xaaa", vptr="0x15a51ab0", active=True):
    return {"obj": obj, "vptr": vptr, "active": active, "center_x": 1000.0,
            "center_y": -2000.0, "radius": 450000.0, "heading_yaw": 34.5,
            "stage": 3, "sector": "H2"}


def _ghost(obj):
    return {"obj": obj, "vptr": "0x15a51ab0", "active": False, "center_x": 0.0,
            "center_y": 0.0, "radius": 0.0, "heading_yaw": 0.0, "stage": -1,
            "sector": "A1"}


class Harness:
    """Wire a fresh cache module to a temp CACHE/LOCK + swappable reader stubs."""
    def __init__(self, m, tmp):
        self.m = m
        self.tmp = tmp
        self._n = 0
        m.CACHE = os.path.join(tmp, "cache.json")
        m.LOCK = os.path.join(tmp, "lock0")
        m.spawn_times = lambda: self.spawns
        m.list_pids = lambda: self.pids
        m.poll_instances = lambda pid, insts, d: self.poll(pid, insts, d)
        m.scan_instances = lambda pid, d: self.scan(pid, d)
        m.pod_scan = lambda pid, d: self.podscan(pid, d)
        # defaults: dim0 pod present, no storm anywhere
        self.spawns = {}
        self.pids = {0: 111, 1: 222}
        self.poll = lambda pid, insts, d: None
        self.scan = lambda pid, d: None
        self.podscan = lambda pid, d: None

    def tick(self):
        # main() leaks its flock fd on purpose (production = one process per timer
        # tick, which exits). In-process we give each tick a fresh lock file so the
        # LOCK_NB acquire never collides with the previous tick's leaked fd.
        self._n += 1
        self.m.LOCK = os.path.join(self.tmp, f"lock{self._n}")
        assert self.m.main() == 0
        return self.m.load_cache()["dimensions"]


def test_lifecycle_birth_poll_despawn():
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(m, tmp)
        FRESH = m._now()  # a spawn "just now" -> in active window

        # -- S1 cold start, dim0 in window, ghosts present, one active: LEARN --
        h.spawns = {0: FRESH}
        insts = [_ghost("0xg1"), _active(obj="0xaaa"), _ghost("0xg2")]
        h.scan = lambda pid, d: insts if d == 0 else None
        h.poll = lambda pid, i, d: None            # nothing learned yet
        dims = h.tick()
        d0 = dims["0"]
        assert d0["active"] and d0["x"] == 1000.0 and d0["sector"] == "H2"
        assert d0["instances_pid"] == 111
        assert {i["obj"] for i in d0["instances"]} == {"0xg1", "0xaaa", "0xg2"}
        assert d0.get("active_since_utc")
        meta = m.load_cache()["meta"]
        assert 0 in meta["learned_dims"] and 0 in meta["scanned_dims"]

        # -- S2 next tick: CHEAP poll returns active, NO heap walk --
        since = d0["active_since_utc"]
        h.poll = lambda pid, i, d: _active(obj="0xaaa") if d == 0 else None
        h.scan = lambda pid, d: (_ for _ in ()).throw(AssertionError("must not heap-walk"))
        dims = h.tick()
        d0 = dims["0"]
        assert d0["active"] and d0["fast"] is True
        assert d0["active_since_utc"] == since            # continuity across ticks
        meta = m.load_cache()["meta"]
        assert meta["scanned_dims"] == [] and 0 in meta["fast_dims"]

        # -- S3 despawn: cheap poll None, in-window full scan finds no active --
        h.poll = lambda pid, i, d: None
        h.scan = lambda pid, d: [_ghost("0xg1"), _ghost("0xg2")] if d == 0 else None
        dims = h.tick()
        d0 = dims["0"]
        assert d0["active"] is False and d0.get("ended_utc")
        assert d0["instances_pid"] == 111 and len(d0["instances"]) == 2  # list retained

        # -- S4 BETWEEN storms (window closed) a NEW storm activates: cheap poll
        #       catches it with NO heap walk = birth latency killed --
        h.spawns = {}                                     # cadence window CLOSED
        h.poll = lambda pid, i, d: _active(obj="0xg1") if d == 0 else None
        h.scan = lambda pid, d: (_ for _ in ()).throw(AssertionError("no scan between storms"))
        dims = h.tick()
        d0 = dims["0"]
        assert d0["active"] and d0["fast"] is True
        meta = m.load_cache()["meta"]
        assert meta["scanned_dims"] == [] and 0 in meta["fast_dims"]
        print("ok lifecycle: learn -> cheap poll -> despawn(ended_utc) -> off-window birth caught cheaply")


def test_bootstrap_primes_unprimed_dim_off_window():
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(m, tmp)
        # Cold start, NO active window (no spawns), no cache -> an unprimed dim must
        # still be primed by one bootstrap scan so its FIRST storm is caught cheaply.
        h.spawns = {}
        scans = {"n": 0}
        def scan(pid, d):
            scans["n"] += 1
            return [_ghost("0xg1"), _ghost("0xg2")] if d == 0 else None
        h.scan = scan
        h.poll = lambda pid, i, d: None
        dims = h.tick()
        d0 = dims["0"]
        assert d0["active"] is False and len(d0["instances"]) == 2, d0
        assert d0["instances_pid"] == 111 and d0.get("learn_attempt_utc")
        meta = m.load_cache()["meta"]
        assert 0 in meta["learned_dims"], "unprimed dim must bootstrap-learn off-window"

        # Next tick: now primed -> NO further heap scan (cheap poll only).
        before = scans["n"]
        h.scan = lambda pid, d: (_ for _ in ()).throw(AssertionError("primed dim must not re-scan"))
        h.poll = lambda pid, i, d: None
        dims = h.tick()
        assert scans["n"] == before  # scan lambda swapped; assertion is it isn't called
        assert m.load_cache()["meta"]["scanned_dims"] == []
        print("ok bootstrap: unprimed dim learns once off-window, then never re-scans")


def test_pod_restart_drops_stale_addresses():
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(m, tmp)
        FRESH = m._now()
        # learn on pid 111
        h.spawns = {0: FRESH}
        h.scan = lambda pid, d: [_active(obj="0xaaa"), _ghost("0xg2")] if d == 0 else None
        h.tick()
        assert m.load_cache()["dimensions"]["0"]["instances_pid"] == 111

        # pod restarts -> new pid, window closed. Stale addresses must NOT be polled.
        h.pids = {0: 999, 1: 222}
        h.spawns = {}
        polled = {"n": 0}
        def poll_spy(pid, i, d):
            polled["n"] += 1
            return None
        h.poll = poll_spy
        h.scan = lambda pid, d: (_ for _ in ()).throw(AssertionError("no scan between storms"))
        dims = h.tick()
        assert polled["n"] == 0, "must not cheap-poll addresses from the OLD pid"
        assert dims["0"]["active"] is False
        print("ok pod restart: stale addresses dropped, no poll on the old pid")


def test_reader_without_emit_instances_falls_back():
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(m, tmp)
        h.spawns = {0: m._now()}
        h.poll = lambda pid, i, d: None
        h.scan = lambda pid, d: None                      # --emit-instances unsupported
        h.podscan = lambda pid, d: _active(obj="0xaaa") if d == 0 else None
        dims = h.tick()
        assert dims["0"]["active"] and dims["0"]["x"] == 1000.0
        print("ok deploy-gap fallback: legacy pod_scan used when --emit-instances absent")


def test_pod_gone_marks_inactive():
    m = _load()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(m, tmp)
        h.pids = {1: 222}                                 # dim0 pod missing
        dims = h.tick()
        assert dims["0"]["active"] is False
        print("ok pod gone: dim marked inactive, no crash")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all storm ramcache tests passed")
