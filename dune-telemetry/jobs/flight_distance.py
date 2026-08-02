"""
Phase 3 - flight-distance integration job.

Walks new vehicle_positions samples, sums the XY distance flown per
(pilot, vehicle_class, ISO-week), and upserts into flight_distance_weekly.

A teleport filter discards implausible segments (respawn / fast-travel / map
transfer / long idle gaps) so they do not inflate "distance flown". The job is
correct only if its bookmark advances exactly once per run after a successful
upsert - it sums into a running total and must never double-count.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

import db

log = logging.getLogger("telemetry.flight_distance")

# A segment longer than this gap (seconds) is treated as idle/unsampled, not
# credible continuous flight.
MAX_SEGMENT_GAP = 600


def _iso_week(epoch):
    """ISO 'YYYY-Www' label for an epoch-seconds timestamp."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    year, week, _ = dt.isocalendar()
    return "%04d-W%02d" % (year, week)


def run(ctx):
    store = ctx.store
    bookmark = int(db.get_cursor(store, "flight_distance") or 0)
    max_speed = ctx.config.teleport_max_speed_cms

    # New samples since the bookmark, plus the carry-over anchor per vehicle
    # (the single most-recent sample at or before the bookmark) so a flight
    # segment is not lost across job runs.
    new_rows = store.execute(
        "SELECT ts, vehicle_id, vehicle_class, pilot_account_id, x, y "
        "FROM vehicle_positions WHERE ts > ? ORDER BY vehicle_id, ts",
        (bookmark,)).fetchall()

    if not new_rows:
        log.info("flight_distance: no new positions since bookmark %d", bookmark)
        return

    vehicle_ids = sorted({r["vehicle_id"] for r in new_rows})
    anchors = {}
    for vid in vehicle_ids:
        anchor = store.execute(
            "SELECT ts, vehicle_id, vehicle_class, pilot_account_id, x, y "
            "FROM vehicle_positions WHERE vehicle_id=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 1",
            (vid, bookmark)).fetchone()
        if anchor is not None:
            anchors[vid] = anchor

    # Group new samples by vehicle, prepend the carry-over anchor.
    by_vehicle = {}
    for r in new_rows:
        by_vehicle.setdefault(r["vehicle_id"], []).append(r)
    for vid, anchor in anchors.items():
        if vid in by_vehicle:
            by_vehicle[vid].insert(0, anchor)

    # credit[(iso_week, account_id, vehicle_class)] = metres
    credit = {}
    max_ts = bookmark
    segments_kept = segments_dropped = 0

    for vid, samples in by_vehicle.items():
        for prev, cur in zip(samples, samples[1:]):
            max_ts = max(max_ts, cur["ts"])
            dt = cur["ts"] - prev["ts"]
            if dt <= 0 or dt > MAX_SEGMENT_GAP:
                segments_dropped += 1
                continue
            if prev["x"] is None or prev["y"] is None \
                    or cur["x"] is None or cur["y"] is None:
                segments_dropped += 1
                continue
            dist_cm = math.hypot(cur["x"] - prev["x"], cur["y"] - prev["y"])
            if dist_cm / dt > max_speed:
                segments_dropped += 1
                continue
            account_id = cur["pilot_account_id"]
            if not account_id:
                segments_dropped += 1
                continue
            segments_kept += 1
            key = (_iso_week(cur["ts"]), account_id, cur["vehicle_class"])
            credit[key] = credit.get(key, 0.0) + dist_cm

    now = int(time.time())
    for (iso_week, account_id, vehicle_class), dist_cm in credit.items():
        meters = dist_cm / 100.0
        store.execute(
            "INSERT INTO flight_distance_weekly"
            "(iso_week, account_id, vehicle_class, meters, updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(iso_week, account_id, vehicle_class) "
            "DO UPDATE SET meters = meters + excluded.meters, "
            "updated_at = excluded.updated_at",
            (iso_week, account_id, vehicle_class, meters, now))

    # Advance the bookmark exactly once, after the upsert succeeded.
    db.set_cursor(store, "flight_distance", max_ts, now)
    log.info("flight_distance: %d segments kept, %d dropped, %d player-weeks "
             "credited, bookmark -> %d",
             segments_kept, segments_dropped, len(credit), max_ts)


JOB = {"name": "flight_distance", "interval_attr": "flight_job_interval", "run": run}
