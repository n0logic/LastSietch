"""
Stream - read-model mirror: per-account portal/admin read models.

Full-refresh of every account's flattened read models each cycle, written to the
player_read_model table in the collector store. the web host pulls these (via the
relay `read-models` action) into its local mirror.sqlite so portal/admin page
loads read locally instead of crossing the internet to the game DB on each hit.


Parity by construction: each section blob is produced by the SAME build()
function the relay's dispatcher scripts use (dune-player-progress / -progression-
state / -tags / -landsraad-rewards), called here over the collector's fast
psycopg2 connection instead of dq.sh/kubectl. One source of truth, two transports.

The denormalized scalars (char_name / online / lvl / intel) come from the
progression HARVEST_QUERY (one set-based read). current_map (v2, 2026-07-24) is
the live map each online player's pawn is on, from the same actors.map join the
roster stream uses; offline players stay NULL. Populating it in the mirror lets
per-map questions (e.g. crash-impact attribution) be answered from the read
model instead of a separate live roster call.

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
from streams import progression

log = logging.getLogger("telemetry.read_models")

ACCOUNTS_SQL = (
    "SELECT account_id FROM dune.encrypted_player_state "
    "WHERE account_id <> 0 ORDER BY account_id"
)

# current_map for ONLINE players: the map their pawn is on. Same join the roster
# stream uses (dune.actors.map via player_state.player_pawn_id). Offline players
# are not on a map, so they simply won't appear here and stay NULL. Raw map value
# (e.g. "Survival_1") is stored, not a friendly name -> precise for per-map
# telemetry / crash-impact attribution; display can friendly-map downstream.
CURRENT_MAP_SQL = (
    "SELECT ps.account_id, a.map "
    "FROM dune.player_state ps "
    "JOIN dune.actors a ON a.id = ps.player_pawn_id "
    "WHERE ps.online_status='Online'"
)

# Builder modules are loaded lazily once (the script dir is only known at run
# time from ctx.config). Hyphenated filenames -> importlib from absolute path.
_BUILDERS = None


def _load_builders(script_dir):
    global _BUILDERS
    if _BUILDERS is not None:
        return _BUILDERS
    mods = {}
    for key, fname in (
        ("progress", "dune-player-progress.py"),
        ("specializations", "dune-progression-state.py"),
        ("tags", "dune-tags.py"),
        ("landsraad", "dune-landsraad-rewards.py"),
    ):
        path = os.path.join(script_dir, fname)
        spec = importlib.util.spec_from_file_location("rm_" + key, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[key] = mod
    _BUILDERS = mods
    return mods


def _make_qjson(gamedb):
    """psycopg2 runner matching the dispatcher scripts' _query_json(sql, fallback)
    -> parsed JSON. Strips the SET search_path header (the connection already sets
    it; the SQL is dune.-qualified anyway). Returns the parsed fallback on no row."""
    def qjson(sql, fallback="null"):
        clean = "\n".join(
            ln for ln in sql.splitlines()
            if not ln.strip().upper().startswith("SET "))
        val = gamedb.query_scalar(clean)
        if val is None:
            return json.loads(fallback)
        if isinstance(val, (str, bytes, bytearray)):
            return json.loads(val)
        return val  # psycopg2 already parsed json/jsonb -> dict/list (or a scalar)
    return qjson


def _make_qrows(gamedb):
    """psycopg2 runner matching the landsraad script's psql(sql) -> positional rows."""
    def qrows(sql):
        return gamedb.query_rows(sql)
    return qrows


def _section(fn, *args):
    """Run a builder; return (json_text, ok). On error return (None, False) so the
    the web host read path falls back to the live relay for just that section."""
    try:
        return json.dumps(fn(*args)), True
    except Exception as exc:  # noqa: BLE001 - one bad section never kills the sweep
        log.warning("read_models: section %s failed: %s", getattr(fn, "__module__", "?"), exc)
        return None, False


def run(ctx):
    started = time.time()
    builders = _load_builders(ctx.config.read_model_script_dir)
    qjson = _make_qjson(ctx.gamedb)
    qrows = _make_qrows(ctx.gamedb)

    # Denormalized scalars from the progression snapshot (one set-based read).
    snap = {}
    for r in ctx.gamedb.query(progression.HARVEST_QUERY, {}):
        aid = int(r.get("account_id"))
        snap[aid] = (
            r.get("char_name"),
            1 if r.get("online_status") == "Online" else 0,
            int(r.get("lvl") or 0),
            int(r.get("intel") or 0),
        )

    accounts = [int(r["account_id"]) for r in ctx.gamedb.query(ACCOUNTS_SQL, {})]

    # account_id -> current map, online players only.
    map_by_aid = {}
    for r in ctx.gamedb.query(CURRENT_MAP_SQL, {}):
        aid = r.get("account_id")
        if aid is not None:
            map_by_aid[int(aid)] = r.get("map")

    now_iso = datetime.now(timezone.utc).isoformat()

    rows = []
    errors = 0
    for aid in accounts:
        said = str(aid)
        progress_json, ok1 = _section(builders["progress"].build, said, qjson)
        spec_json, ok2 = _section(builders["specializations"].build, said, qjson)
        tags_json, ok3 = _section(builders["tags"].build, said, qjson)
        landsraad_json, ok4 = _section(builders["landsraad"].build, aid, qrows)
        errors += (not ok1) + (not ok2) + (not ok3) + (not ok4)

        char_name, online, lvl, intel = snap.get(aid, (None, 0, None, None))
        rows.append({
            "account_id": aid,
            "char_name": char_name,
            "online": online,
            "lvl": lvl,
            "intel": intel,
            "current_map": map_by_aid.get(aid),  # live map for online players; NULL if offline
            "progress_json": progress_json,
            "specializations_json": spec_json,
            "landsraad_json": landsraad_json,
            "tags_json": tags_json,
            "src_synced_at": now_iso,
        })

    upserted, pruned = db.upsert_read_models(ctx.store, rows)
    note = None if errors == 0 else f"{errors} section error(s)"
    db.set_sync_meta(ctx.store, "read_models", last_run_at=now_iso,
                     row_count=upserted, ok=1 if errors == 0 else 0, note=note)

    log.info("read_models: %d accounts, %d upserted, %d pruned, %d section errors (%.0f ms)",
             len(accounts), upserted, pruned, errors, (time.time() - started) * 1000)


STREAM = {"name": "read_models",
          "interval_attr": "read_models_interval",
          "run": run}
