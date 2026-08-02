"""
Item C - grant_events stream: dune.ls_progression_grants harvest.

Copies every grant-ledger row written by dune-grant.sh into the telemetry
SQLite grant_events table, using the source bigserial id as a monotonic
cursor. Passive read-only: the logger never writes to the game DB.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import db

log = logging.getLogger("telemetry.grant_events")

HARVEST_QUERY = """
SELECT id, granted_at, account_id, grant_type, detail, operator, status
FROM dune.ls_progression_grants
WHERE id > %(cursor)s
ORDER BY id ASC
"""

COLUMNS = [
    "pg_grant_id", "granted_at", "granted_epoch", "account_id",
    "grant_type", "detail", "operator", "status", "harvested_at",
]


def _to_iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _to_epoch(value):
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


def _detail_text(detail):
    """ls_progression_grants.detail is jsonb -> dict via RealDictCursor."""
    if detail is None:
        return None
    if isinstance(detail, (dict, list)):
        return json.dumps(detail, sort_keys=True, separators=(",", ":"))
    return str(detail)


def run(ctx):
    cursor = int(db.get_cursor(ctx.store, "grant_events") or 0)
    rows = ctx.gamedb.query(HARVEST_QUERY, {"cursor": cursor})
    harvested_at = int(time.time())

    out = []
    max_id = cursor
    for row in rows:
        pg_id = row["id"]
        if pg_id > max_id:
            max_id = pg_id
        granted_at = row.get("granted_at")
        out.append((
            pg_id,
            _to_iso(granted_at),
            _to_epoch(granted_at),
            str(row.get("account_id")) if row.get("account_id") is not None else None,
            row.get("grant_type"),
            _detail_text(row.get("detail")),
            row.get("operator"),
            row.get("status"),
            harvested_at,
        ))

    written = db.insert_many_ignore(ctx.store, "grant_events", COLUMNS, out)

    if max_id > cursor:
        db.set_cursor(ctx.store, "grant_events", str(max_id), harvested_at)

    log.info("grant_events: %d rows harvested, %d new", len(rows), written)


STREAM = {"name": "grant_events",
          "interval_attr": "grant_events_interval",
          "run": run}
