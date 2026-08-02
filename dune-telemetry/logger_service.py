#!/usr/bin/env python3
"""
Last Sietch Dune telemetry logger - service entrypoint.

A passive, read-only collector for the Last Sietch Dune Awakening server.
It polls the game DB (dune.*) with SELECT-only queries and writes the results
to its own SQLite store at /var/lib/lastsietch-telemetry/telemetry.db. It never writes
to the game DB and never mutates k8s.

A single-threaded synchronous scheduler runs each registered stream/job on its
own env-tunable cadence. Every stream is wrapped so a failure is logged and
retried next interval - one stream failing never stops the others.

Run modes:
  default   run the scheduler loop until SIGTERM/SIGINT
  --once    run one sweep of every stream + job, then exit (parallel-run diff)
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import time

import psycopg2

import db
from config import load_config
from gamedb import GameDB
from jobs import JOBS
from streams import STREAMS

log = logging.getLogger("telemetry.service")


class Context:
    """Carried into every stream/job run: config, game DB, telemetry SQLite."""

    def __init__(self, config, gamedb, store):
        self.config = config
        self.gamedb = gamedb
        self.store = store


def parse_args():
    p = argparse.ArgumentParser(description="Last Sietch Dune telemetry logger")
    p.add_argument("--once", action="store_true",
                   help="run one sweep of every stream/job then exit")
    return p.parse_args()


def run_unit(unit, ctx):
    """Run one stream/job with error isolation. NEVER lets a failure escape."""
    name = unit["name"]
    start = time.time()
    try:
        unit["run"](ctx)
        ctx.store.commit()
        log.info("stream %s ok (%.0f ms)", name, (time.time() - start) * 1000)
        return True
    except Exception as exc:  # noqa: BLE001 - keep the loop alive
        try:
            ctx.store.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.exception("stream %s failed: %s", name, exc)
        # After a DB-shaped error, prime a reconnect before the next tick so a
        # moved ClusterIP self-heals.
        if isinstance(exc, (psycopg2.Error, RuntimeError)):
            try:
                ctx.gamedb.ensure_connected()
            except Exception as rexc:  # noqa: BLE001
                log.warning("reconnect after %s failure deferred: %s", name, rexc)
        return False


def main():
    args = parse_args()
    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="telemetry %(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    if not config.db_host or not config.db_pass:
        log.error("DB_HOST and DB_PASS are required (the launcher resolves them)")
        raise SystemExit(1)

    os.makedirs(os.path.dirname(config.telemetry_db), exist_ok=True)
    store = db.open_db(config.telemetry_db)
    db.init_schema(store)
    log.info("telemetry store ready at %s", config.telemetry_db)

    gamedb = GameDB(config)
    ctx = Context(config, gamedb, store)

    units = list(STREAMS) + list(JOBS)

    if args.once:
        log.info("--once: running one sweep of %d units", len(units))
        for unit in units:
            run_unit(unit, ctx)
        store.close()
        gamedb.close()
        return

    running = {"stop": False}

    def handle_signal(signum, frame):  # noqa: ARG001
        log.info("shutting down (signal %d received)", signum)
        running["stop"] = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Run every unit once immediately for fast first data + parallel-run diffing,
    # then schedule each on its own cadence.
    next_run = {}
    for unit in units:
        run_unit(unit, ctx)
        interval = getattr(config, unit["interval_attr"])
        next_run[unit["name"]] = time.time() + interval

    while not running["stop"]:
        time.sleep(min(5, max(1, _shortest_wait(next_run))))
        if running["stop"]:
            break
        now = time.time()
        for unit in units:
            name = unit["name"]
            if now >= next_run[name]:
                run_unit(unit, ctx)
                interval = getattr(config, unit["interval_attr"])
                next_run[name] = now + interval

    store.commit()
    store.close()
    gamedb.close()
    log.info("stopped cleanly")


def _shortest_wait(next_run):
    now = time.time()
    return min((t - now for t in next_run.values()), default=5)


if __name__ == "__main__":
    main()
