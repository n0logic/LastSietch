"""
Stream - storage mirror: per-account container/item snapshots.

Full-refresh of every account's storage snapshot each cycle into the
player_storage table. the web host pulls these (via the relay `storage-models`
action) into its local mirror.sqlite so the portal storage pages (container
list, per-container item drilldown, cross-container "find an item" search) read
locally instead of crossing the internet to the game DB on each hit.
See docs/dune-research/PHASE-2-MIRROR-CONTRACT-2026-06-05.md.

Parity by construction: containers + search_rows come from the SAME build()
functions the relay's dispatcher scripts use (dune-containers / -container-search),
called here over the collector's psycopg2 connection. The per-container item
buckets come from dune-container-items.build_all(), whose per-item projection +
ordering match the live ITEMS_SQL so the mirror paginates them identically.

Passive read-only against dune.*. Writes only to the telemetry SQLite.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import time
from datetime import datetime, timezone

import db
from streams.read_models import ACCOUNTS_SQL, _make_qjson

log = logging.getLogger("telemetry.storage")

_BUILDERS = None
_BUILDERS_MTIME = None

_BUILDER_FILES = (
    ("containers", "dune-containers.py"),
    ("items", "dune-container-items.py"),
    ("search", "dune-container-search.py"),
)


def _load_builders(script_dir):
    """Import the container/search/items builder scripts from script_dir.

    Reloads whenever any script's mtime advances. The collector is a long-running
    process, so a plain import-once cache keeps serving the OLD build() after the
    scripts are redeployed until someone restarts the service -- which is exactly
    what silently re-armed the container-id namespace bug (the mirror blob emitted
    no inv_id for weeks after the read script was fixed). Making the reload
    mtime-driven removes the manual-restart footgun. See
    the container-id versus inventory-id distinction: a container's own id is not its inventory id, and the two namespaces overlap.
    """
    global _BUILDERS, _BUILDERS_MTIME
    paths = {key: os.path.join(script_dir, fname) for key, fname in _BUILDER_FILES}
    try:
        newest = max(os.path.getmtime(p) for p in paths.values())
    except OSError:
        newest = None
    if _BUILDERS is not None and (newest is None or newest == _BUILDERS_MTIME):
        return _BUILDERS
    mods = {}
    for key, path in paths.items():
        spec = importlib.util.spec_from_file_location("st_" + key, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[key] = mod
    _BUILDERS = mods
    _BUILDERS_MTIME = newest
    if newest is not None:
        log.info("storage: (re)loaded container builders from %s (mtime=%.0f)",
                 script_dir, newest)
    return mods


def run(ctx):
    started = time.time()
    b = _load_builders(ctx.config.read_model_script_dir)
    qjson = _make_qjson(ctx.gamedb)

    accounts = [int(r["account_id"]) for r in ctx.gamedb.query(ACCOUNTS_SQL, {})]
    now_iso = datetime.now(timezone.utc).isoformat()

    rows = []
    errors = 0
    for aid in accounts:
        try:
            containers = b["containers"].build(aid, qjson)
            search = b["search"].build(aid, qjson)
            items_by = b["items"].build_all(aid, qjson)
            blob = {
                "available": True,
                "containers": containers.get("containers") or [],
                "items_by_container": items_by,
                "search_rows": search.get("rows") or [],
            }
            rows.append({"account_id": aid, "storage_json": json.dumps(blob),
                         "src_synced_at": now_iso})
        except Exception as exc:  # noqa: BLE001 - one bad account never kills the sweep
            errors += 1
            log.warning("storage: account %s failed: %s", aid, exc)

    # Prune only against the FULL current account set so a transiently-failing
    # account keeps its last-good row (and the mirror staleness guard catches it).
    upserted, pruned = db.upsert_storage(ctx.store, rows, prune_keep=accounts)
    note = None if errors == 0 else f"{errors} account error(s)"
    db.set_sync_meta(ctx.store, "storage", last_run_at=now_iso,
                     row_count=upserted, ok=1 if errors == 0 else 0, note=note)

    log.info("storage: %d accounts, %d upserted, %d pruned, %d errors (%.0f ms)",
             len(accounts), upserted, pruned, errors, (time.time() - started) * 1000)


STREAM = {"name": "storage",
          "interval_attr": "storage_interval",
          "run": run}
