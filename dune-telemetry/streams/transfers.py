"""
Transfers / removals stream: dune.account_removal_log + dune.character_transfer_imports.

Watches the two Funcom-managed audit tables that fire on character delete,
account removal, and incoming character-transfer state changes. Writes a
unified, dedup'd row into the telemetry-local `transfer_events` table so the
event timeline is captured without manual psql harvests.

Cursors (two, source-specific):
  - cursors["transfer_events_removal"] : ISO event_time high-water mark
  - cursors["transfer_events_imports"] : ISO last_update high-water mark

Passive read-only against dune.* (SELECT only). Writes ONLY to local SQLite.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("telemetry.transfers")

REMOVAL_QUERY = """
SELECT event_time, fls_id, account_id, reason
FROM dune.account_removal_log
WHERE event_time > %(cursor)s::timestamptz
ORDER BY event_time ASC
"""

IMPORTS_QUERY = """
SELECT fls_id, last_update, transfer_state::text AS transfer_state
FROM dune.character_transfer_imports
WHERE last_update > %(cursor)s::timestamptz
ORDER BY last_update ASC
"""

COLUMNS = [
    "ts", "event_type", "account_id", "fls_id",
    "transfer_state", "raw_json", "dedup_key",
]

# First-time cursor clamp: account_removal_log accumulates forever; without a
# floor the initial sweep would harvest months of history. 7 days is generous
# but bounded.
INITIAL_REMOVAL_BACKFILL_DAYS = 7


def _to_iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else ""


def _to_epoch(value):
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


def _initial_removal_cursor():
    floor = datetime.now(timezone.utc) - timedelta(days=INITIAL_REMOVAL_BACKFILL_DAYS)
    return floor.isoformat()


def _harvest_removals(ctx):
    cursor = db.get_cursor(ctx.store, "transfer_events_removal") or _initial_removal_cursor()
    rows = ctx.gamedb.query(REMOVAL_QUERY, {"cursor": cursor})
    out = []
    max_seen = cursor
    for row in rows:
        et_iso = _to_iso(row.get("event_time"))
        if et_iso > max_seen:
            max_seen = et_iso
        aid = str(row["account_id"]) if row.get("account_id") is not None else None
        fls = row.get("fls_id")
        raw = json.dumps(
            {"event_time": et_iso, "fls_id": fls, "account_id": aid,
             "reason": row.get("reason")},
            sort_keys=True, separators=(",", ":"))
        dedup = "removal|%s|%s|%s" % (et_iso, fls or "", aid or "")
        out.append((
            _to_epoch(row.get("event_time")),
            "removal",
            aid,
            fls,
            None,
            raw,
            dedup,
        ))
    return out, cursor, max_seen


def _harvest_imports(ctx):
    # Empty cursor → use unix epoch so the timestamptz cast succeeds and we
    # fetch every row. character_transfer_imports is sparse; full fetch is safe.
    cursor = db.get_cursor(ctx.store, "transfer_events_imports") or ""
    query_cursor = cursor or "1970-01-01T00:00:00+00:00"
    rows = ctx.gamedb.query(IMPORTS_QUERY, {"cursor": query_cursor})
    out = []
    max_seen = cursor
    for row in rows:
        lu_iso = _to_iso(row.get("last_update"))
        if lu_iso > max_seen:
            max_seen = lu_iso
        fls = row.get("fls_id")
        state = row.get("transfer_state")
        raw = json.dumps(
            {"fls_id": fls, "last_update": lu_iso, "transfer_state": state},
            sort_keys=True, separators=(",", ":"))
        dedup = "transfer|%s|%s|%s" % (fls or "", lu_iso, state or "")
        out.append((
            _to_epoch(row.get("last_update")),
            "transfer",
            None,
            fls,
            state,
            raw,
            dedup,
        ))
    return out, cursor, max_seen


def run(ctx):
    harvested_at = int(time.time())

    removal_rows, rem_cursor, rem_max = _harvest_removals(ctx)
    written_rem = db.insert_many_ignore(
        ctx.store, "transfer_events", COLUMNS, removal_rows)
    if rem_max > rem_cursor:
        db.set_cursor(ctx.store, "transfer_events_removal", rem_max, harvested_at)

    import_rows, imp_cursor, imp_max = _harvest_imports(ctx)
    written_imp = db.insert_many_ignore(
        ctx.store, "transfer_events", COLUMNS, import_rows)
    if imp_max > imp_cursor:
        db.set_cursor(ctx.store, "transfer_events_imports", imp_max, harvested_at)

    log.info(
        "transfers: removals harvested=%d new=%d / imports harvested=%d new=%d",
        len(removal_rows), written_rem, len(import_rows), written_imp)


STREAM = {"name": "transfers",
          "interval_attr": "transfers_interval",
          "run": run}
