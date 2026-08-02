"""
Stream - market mirror: full active CHOAM exchange listing set.

Full-refresh of ALL active exchange listings each cycle into the market_listing
table. the web host pulls these (via the relay `market-all` action) into its local
mirror.sqlite and runs the substring search LOCALLY, so /portal/market browse
stops crossing the internet to the game DB on each search.


Parity by construction: the listing rows come from the SAME projection
dune-market-control.fetch_all_listings() defines (matching the live
listings-search row shape), called here over the collector's psycopg2 connection.

Passive read-only against dune.*. Writes only to the telemetry SQLite.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import time
from datetime import datetime, timezone

import db
from streams.read_models import _make_qjson

log = logging.getLogger("telemetry.market")

_MOD = None


def _load_market(script_dir):
    global _MOD
    if _MOD is not None:
        return _MOD
    path = os.path.join(script_dir, "dune-market-control.py")
    spec = importlib.util.spec_from_file_location("mk_control", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MOD = mod
    return mod


def run(ctx):
    started = time.time()
    mod = _load_market(ctx.config.read_model_script_dir)
    qjson = _make_qjson(ctx.gamedb)
    now_iso = datetime.now(timezone.utc).isoformat()

    ok = 1
    note = None
    listings = []
    try:
        listings = mod.fetch_all_listings(qjson)
    except Exception as exc:  # noqa: BLE001
        ok, note = 0, str(exc)[:200]
        log.warning("market: fetch failed: %s", exc)

    # Only replace the table on a successful fetch, so a transient error leaves
    # the last-good listing set in place (the mirror staleness guard handles it).
    inserted = 0
    if ok:
        inserted = db.replace_market_listings(ctx.store, listings, now_iso)

    db.set_sync_meta(ctx.store, "market", last_run_at=now_iso,
                     row_count=inserted, ok=ok, note=note)
    log.info("market: %d listings refreshed, ok=%d (%.0f ms)",
             inserted, ok, (time.time() - started) * 1000)


STREAM = {"name": "market",
          "interval_attr": "market_interval",
          "run": run}
