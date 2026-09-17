#!/usr/bin/env python3
"""V2 Exchange price-history: a tiered per-template price series in its OWN SQLite
file (config.MARKET_HISTORY_DB_PATH), fed from each market snapshot and read by the
Exchange sparkline.

WHY ITS OWN FILE. The first cut lived in admin.db as one flat table at 10 min x 21 d.
Retention worked exactly as designed, but the series plateaued at 4.0M rows / ~820 MB,
which was 92% of admin.db: the auth, session, karum and rewards database. Everything
that touches admin.db paid for it -- the nightly VACUUM INTO snapshot, the restic
delta, the deploy-time cp backup, and a GROUP BY plus a range DELETE into admin.db's
WAL every 30 s. A sparkline pulled ~3k points (230 KB) for a chart 200 px wide.

THE SHAPE. Integer template keys and epoch timestamps in three tiers:

    mph_raw     10 min points, kept 48 h    (the fresh tail the sparkline needs)
    mph_hourly  1 h points,    kept 60 d    (what the 21 d window is drawn from)
    mph_daily   1 d points,    kept 365 d   (the long tail, drawn from hourly)

All three are WITHOUT ROWID with a composite PK (template, timestamp), so the PK IS
the lookup index and the prune is a range delete through it. Steady state is roughly
175 MB against 821 MB for the flat table.

Rollups and prunes run ONCE AN HOUR from the capture path, not on every 30 s tick:
the gate is `now_hour != last_rollup_hour` with last_rollup_hour durable in mph_meta,
so a restart does not re-run it, and a process that has never rolled up recomputes
every retained raw hour (cheap: 48 h of raw, idempotent INSERT OR REPLACE).

Independent of the live market path: it only appends downsampled points and prunes
old ones, so a failure here never affects the market mirror. A template with no
points yet renders as "calibrating" in the UI.

Migration from the flat admin.db table:

    python3 market_history.py --backfill-from /opt/lastsietch-admin/admin.db

reads the legacy table read-only (mode=ro, never a write to admin.db) and fills all
three tiers from the 21 d it holds. Idempotent, so it can be re-run.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

from config import MARKET_HISTORY_DB_PATH

log = logging.getLogger("lastsietch-admin.market_history")

# One captured point per template per this window. Market snapshots pull every
# ~30s (config.MIRROR_PULL_INTERVAL); without the throttle mph_raw would take
# every template on every tick. A template whose newest raw point is younger than
# this is skipped for the current snapshot.
_MIN_INTERVAL_SECONDS = 10 * 60           # ~10 min

_HOUR = 3600
_DAY = 86400
_SEVEN_DAYS_SECONDS = 7 * _DAY

# Retention per tier. _RETENTION_DAYS is also the DEFAULT history() window, which
# is what the sparkline draws; it must stay <= _HOURLY_RETENTION_DAYS for the
# window to come out of the hourly tier.
_RAW_RETENTION_HOURS = 48
_HOURLY_RETENTION_DAYS = 60
_DAILY_RETENTION_DAYS = 365
_RETENTION_DAYS = 21

# Without this the -wal keeps its high-water mark forever: a backfill or a large
# rollup writes the whole file through the WAL and SQLite never shrinks it back.
_WAL_LIMIT_BYTES = 16 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id           INTEGER PRIMARY KEY,
    template_id  TEXT    NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS mph_raw (
    tpl            INTEGER NOT NULL,
    ts             INTEGER NOT NULL,
    min_price      INTEGER,
    median_price   INTEGER,
    listing_count  INTEGER,
    total_qty      INTEGER,
    PRIMARY KEY (tpl, ts)
) WITHOUT ROWID;
-- Only the 48 h tier carries a time index: the capture throttle and the raw prune
-- are both global range scans on ts, and at 48 h the index is a few MB. The hourly
-- and daily prunes loop the templates and go through their PKs instead.
CREATE INDEX IF NOT EXISTS idx_mph_raw_ts ON mph_raw(ts, tpl);

CREATE TABLE IF NOT EXISTS mph_hourly (
    tpl                INTEGER NOT NULL,
    hour_ts            INTEGER NOT NULL,
    min_price          INTEGER,
    median_price       INTEGER,
    avg_listing_count  INTEGER,
    avg_total_qty      INTEGER,
    samples            INTEGER NOT NULL,
    PRIMARY KEY (tpl, hour_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS mph_daily (
    tpl                INTEGER NOT NULL,
    day_ts             INTEGER NOT NULL,
    min_price          INTEGER,
    median_price       INTEGER,
    avg_listing_count  INTEGER,
    avg_total_qty      INTEGER,
    samples            INTEGER NOT NULL,
    PRIMARY KEY (tpl, day_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS mph_meta (
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_ts() -> int:
    return int(_now().timestamp())


def _iso(ts: int) -> str:
    """Epoch seconds -> the same UTC ISO8601 with a +00:00 offset the flat table
    stored, because that string is the `t` field of the history() contract."""
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def _to_ts(value) -> int:
    """Epoch int/float, datetime, or ISO8601 string -> epoch seconds. None on
    anything unparseable, so one bad legacy row cannot abort a backfill."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _floor(ts: int, step: int) -> int:
    return (int(ts) // step) * step


# --------------------------------------------------------------------------- #
# Connection + schema
# --------------------------------------------------------------------------- #

def _connect(path: str = None) -> sqlite3.Connection:
    target = path or MARKET_HISTORY_DB_PATH
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA journal_size_limit={_WAL_LIMIT_BYTES}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init(path: str = None) -> None:
    """Idempotent schema creation (also done lazily in capture/history)."""
    conn = _connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _meta_get(conn, key: str):
    row = conn.execute("SELECT v FROM mph_meta WHERE k = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return int(row["v"])
    except (TypeError, ValueError):
        return None


def _meta_set(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO mph_meta(k, v) VALUES(?, ?)",
                 (key, str(value)))


def _template_map(conn) -> dict:
    """{lowercased template_id: integer id} for every interned template."""
    return {r["template_id"].lower(): r["id"]
            for r in conn.execute("SELECT id, template_id FROM templates")}


def _intern(conn, names) -> None:
    uniq = sorted({n for n in names if n})
    if uniq:
        conn.executemany("INSERT OR IGNORE INTO templates(template_id) VALUES(?)",
                         [(n,) for n in uniq])


# --------------------------------------------------------------------------- #
# Downsampling
# --------------------------------------------------------------------------- #

def _median(sorted_prices: list):
    n = len(sorted_prices)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return sorted_prices[mid]
    return (sorted_prices[mid - 1] + sorted_prices[mid]) // 2


def _mean(values):
    if not values:
        return None
    return int(round(sum(values) / len(values)))


def aggregate_listings(listings) -> list:
    """Downsample a raw market snapshot (list of listing dicts, as delivered to
    mirror.apply_market_snapshot) to ONE aggregate row per template:
    {template_id, min_price, median_price, listing_count, total_qty}.
    None/absent prices are ignored for min/median; every listing counts toward
    listing_count and total_qty."""
    by_tpl: dict = {}
    for l in listings or []:
        tpl = l.get("template_id")
        if not tpl:
            continue
        b = by_tpl.setdefault(tpl, {"prices": [], "qty": 0, "count": 0})
        b["count"] += 1
        stack = l.get("stack") or l.get("initial_stack_size") or 0
        try:
            b["qty"] += int(stack)
        except (TypeError, ValueError):
            pass
        price = l.get("item_price")
        if isinstance(price, int) and not isinstance(price, bool):
            b["prices"].append(price)
    rows = []
    for tpl, b in by_tpl.items():
        prices = sorted(b["prices"])
        rows.append({
            "template_id": tpl,
            "min_price": prices[0] if prices else None,
            "median_price": _median(prices),
            "listing_count": b["count"],
            "total_qty": b["qty"],
        })
    return rows


class _Bucket:
    """One (template, time bucket) accumulator. `samples` counts the underlying
    RAW points, so a daily row's samples is the number of 10 min captures behind
    it, not the number of hourly rows."""

    __slots__ = ("mins", "meds", "counts", "qtys", "samples")

    def __init__(self):
        self.mins = []
        self.meds = []
        self.counts = []
        self.qtys = []
        self.samples = 0

    def add(self, min_price, median_price, listing_count, total_qty, samples=1):
        if min_price is not None:
            self.mins.append(min_price)
        if median_price is not None:
            self.meds.append(median_price)
        if listing_count is not None:
            self.counts.append(listing_count)
        if total_qty is not None:
            self.qtys.append(total_qty)
        self.samples += samples

    def fold(self):
        """(min_price, median_price, avg_listing_count, avg_total_qty, samples).
        min is the true minimum of the bucket; median is the median OF THE SAMPLE
        MEDIANS (the standard downsample: the raw listing prices are gone by then);
        the two averages are unweighted means, rounded to an integer."""
        return (
            min(self.mins) if self.mins else None,
            _median(sorted(self.meds)),
            _mean(self.counts),
            _mean(self.qtys),
            self.samples,
        )


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #

def capture(rows, captured_at=None) -> int:
    """Persist one raw point per template (the aggregate_listings output),
    throttled to one point per template per _MIN_INTERVAL_SECONDS, then roll up
    and prune if the hour has turned. Best-effort: callers wrap this so a history
    failure never breaks the snapshot path. Returns the number of points written.
    `captured_at` accepts epoch seconds or an ISO8601 string."""
    stamp = _to_ts(captured_at) if captured_at is not None else _now_ts()
    if stamp is None:
        stamp = _now_ts()
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        written = 0
        if rows:
            _intern(conn, [r.get("template_id") for r in rows])
            tmap = _template_map(conn)
            cutoff = stamp - _MIN_INTERVAL_SECONDS
            recent = {r["tpl"] for r in conn.execute(
                "SELECT DISTINCT tpl FROM mph_raw WHERE ts >= ?", (cutoff,))}
            payload = []
            seen = set()
            for r in rows:
                name = r.get("template_id")
                if not name:
                    continue
                tpl = tmap.get(name.lower())
                if tpl is None or tpl in recent or tpl in seen:
                    continue
                seen.add(tpl)
                payload.append((tpl, stamp, r.get("min_price"), r.get("median_price"),
                                r.get("listing_count"), r.get("total_qty")))
            if payload:
                conn.executemany(
                    "INSERT OR REPLACE INTO mph_raw(tpl, ts, min_price, "
                    "median_price, listing_count, total_qty) VALUES(?,?,?,?,?,?)",
                    payload)
                written = len(payload)
        maybe_rollup(conn, stamp)
        conn.commit()
        return written
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Rollup + prune (hourly, from the capture path)
# --------------------------------------------------------------------------- #

def maybe_rollup(conn, now_ts: int = None) -> bool:
    """The once-an-hour gate. Returns True when the rollup + prune actually ran.
    last_rollup_hour is durable in mph_meta, so restarting lastsietch-admin does not
    re-run it; a process that has never rolled up (no meta row) recomputes every
    retained raw hour, which is idempotent and costs one pass over 48 h of raw."""
    now_ts = _now_ts() if now_ts is None else int(now_ts)
    hour = now_ts // _HOUR
    last = _meta_get(conn, "last_rollup_hour")
    if last == hour:
        return False
    rollup(conn, now_ts=now_ts, since_hour=last)
    prune(conn, now_ts=now_ts)
    _meta_set(conn, "last_rollup_hour", hour)
    return True


def rollup(conn, now_ts: int = None, since_hour: int = None) -> dict:
    """raw -> hourly -> daily. Recomputes from `since_hour - 1` (the hour that was
    still partial last time) forward, or everything retained when since_hour is
    None. INSERT OR REPLACE, so recomputing a bucket is always safe."""
    now_ts = _now_ts() if now_ts is None else int(now_ts)
    raw_floor = (int(since_hour) - 1) * _HOUR if since_hour else 0

    hourly: dict = {}
    for r in conn.execute(
            "SELECT tpl, ts, min_price, median_price, listing_count, total_qty "
            "FROM mph_raw WHERE ts >= ?", (raw_floor,)):
        key = (r["tpl"], _floor(r["ts"], _HOUR))
        b = hourly.get(key)
        if b is None:
            b = hourly[key] = _Bucket()
        b.add(r["min_price"], r["median_price"], r["listing_count"], r["total_qty"])
    if hourly:
        conn.executemany(
            "INSERT OR REPLACE INTO mph_hourly(tpl, hour_ts, min_price, "
            "median_price, avg_listing_count, avg_total_qty, samples) "
            "VALUES(?,?,?,?,?,?,?)",
            [(tpl, hour_ts) + b.fold() for (tpl, hour_ts), b in hourly.items()])

    # Daily comes from HOURLY, not raw: raw only keeps 48 h, hourly keeps 60 d.
    # Only the days containing a recomputed hour can have changed, so the pass is
    # bounded by what the hourly pass just touched. Without that bound a full
    # recompute (no meta row, i.e. the first capture of a process) would fold all
    # 2.0M hourly rows to reach the same answer.
    if not hourly:
        return {"hourly": 0, "daily": 0}
    day_floor = _floor(min(hour_ts for _, hour_ts in hourly), _DAY)
    daily: dict = {}
    for r in conn.execute(
            "SELECT tpl, hour_ts, min_price, median_price, avg_listing_count, "
            "avg_total_qty, samples FROM mph_hourly WHERE hour_ts >= ?",
            (day_floor,)):
        key = (r["tpl"], _floor(r["hour_ts"], _DAY))
        b = daily.get(key)
        if b is None:
            b = daily[key] = _Bucket()
        b.add(r["min_price"], r["median_price"], r["avg_listing_count"],
              r["avg_total_qty"], samples=r["samples"])
    if daily:
        conn.executemany(
            "INSERT OR REPLACE INTO mph_daily(tpl, day_ts, min_price, "
            "median_price, avg_listing_count, avg_total_qty, samples) "
            "VALUES(?,?,?,?,?,?,?)",
            [(tpl, day_ts) + b.fold() for (tpl, day_ts), b in daily.items()])
    return {"hourly": len(hourly), "daily": len(daily)}


def prune(conn, now_ts: int = None) -> dict:
    """Drop everything past each tier's retention. raw goes through idx_mph_raw_ts
    as one range delete; hourly and daily loop the templates so each delete is a
    range through the composite PK and neither table needs a time index."""
    now_ts = _now_ts() if now_ts is None else int(now_ts)
    raw_before = now_ts - _RAW_RETENTION_HOURS * _HOUR
    hourly_before = now_ts - _HOURLY_RETENTION_DAYS * _DAY
    daily_before = now_ts - _DAILY_RETENTION_DAYS * _DAY

    dropped = {"raw": conn.execute("DELETE FROM mph_raw WHERE ts < ?",
                                   (raw_before,)).rowcount,
               "hourly": 0, "daily": 0}
    for row in conn.execute("SELECT id FROM templates").fetchall():
        tpl = row["id"]
        dropped["hourly"] += conn.execute(
            "DELETE FROM mph_hourly WHERE tpl = ? AND hour_ts < ?",
            (tpl, hourly_before)).rowcount
        dropped["daily"] += conn.execute(
            "DELETE FROM mph_daily WHERE tpl = ? AND day_ts < ?",
            (tpl, daily_before)).rowcount
    return dropped


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

def _empty() -> dict:
    """A fresh empty series every time: a shared module-level dict would hand
    every caller the same `points` list, and one append would leak into all."""
    return {"points": [], "low_7d": None, "calibrating": True}


def _point(ts, row) -> dict:
    return {
        "t": _iso(ts),
        "min_price": row["min_price"],
        "median_price": row["median_price"],
        "listing_count": row["listing_count"],
    }


def history(template_id: str, detail: bool = True, days: int = None) -> dict:
    """The price series for one template over the retention window:
    {points:[{t, min_price, median_price, listing_count}], low_7d, calibrating}.
    Points are oldest-first. An empty series -> points=[] + calibrating=True (the
    sparkline shows a "Calibrating" state). low_7d is the lowest min_price over
    the last 7 days, or None when nothing priced in that window.

    The series is one continuous line assembled from the tiers with NO overlap:
    daily for anything older than the hourly retention, hourly up to the raw
    horizon, then the raw last 48 h. `detail=False` drops the raw tail and returns
    hourly only (a smaller payload that lags by up to one hour, because the
    current hour's bucket is not rebuilt until the hour turns)."""
    tpl_name = (template_id or "").strip()
    if not tpl_name:
        return _empty()
    window_days = int(days or _RETENTION_DAYS)
    now_ts = _now_ts()
    since = now_ts - window_days * _DAY
    hourly_floor = now_ts - _HOURLY_RETENTION_DAYS * _DAY
    # Align the handover to an hour boundary so no raw point can also be inside a
    # returned hourly bucket: the same instant must not appear at two resolutions.
    raw_cut = _floor(now_ts - _RAW_RETENTION_HOURS * _HOUR, _HOUR)
    hourly_to = raw_cut if detail else now_ts + _HOUR

    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        row = conn.execute(
            "SELECT id FROM templates WHERE template_id = ? COLLATE NOCASE",
            (tpl_name,)).fetchone()
        if row is None:
            return _empty()
        tpl = row["id"]

        # The daily-to-hourly handover sits on a DAY boundary: the day that
        # contains the hourly floor is served by its daily point alone and the
        # hourly series starts at the next midnight. Handing over at the floor
        # itself would return that day twice (daily point plus the hours after
        # the floor) whenever now is not midnight-aligned.
        handover = _floor(hourly_floor, _DAY) + _DAY
        points = []
        for r in conn.execute(
                "SELECT day_ts, min_price, median_price, "
                "avg_listing_count AS listing_count FROM mph_daily "
                "WHERE tpl = ? AND day_ts >= ? AND day_ts < ? ORDER BY day_ts",
                (tpl, since, handover)):
            points.append(_point(r["day_ts"], r))
        for r in conn.execute(
                "SELECT hour_ts, min_price, median_price, "
                "avg_listing_count AS listing_count FROM mph_hourly "
                "WHERE tpl = ? AND hour_ts >= ? AND hour_ts < ? ORDER BY hour_ts",
                (tpl, max(since, handover), hourly_to)):
            points.append(_point(r["hour_ts"], r))
        if detail:
            for r in conn.execute(
                    "SELECT ts, min_price, median_price, listing_count "
                    "FROM mph_raw WHERE tpl = ? AND ts >= ? ORDER BY ts",
                    (tpl, max(since, raw_cut))):
                points.append(_point(r["ts"], r))

        # low_7d is the hourly tier's min_price, plus the raw tail: the current
        # hour's bucket is only rebuilt when the hour turns, so a low captured in
        # the last few minutes is not in mph_hourly yet.
        low_since = now_ts - _SEVEN_DAYS_SECONDS
        lows = [conn.execute(
            "SELECT MIN(min_price) FROM mph_hourly WHERE tpl = ? AND hour_ts >= ?",
            (tpl, low_since)).fetchone()[0]]
        lows.append(conn.execute(
            "SELECT MIN(min_price) FROM mph_raw WHERE tpl = ? AND ts >= ?",
            (tpl, low_since)).fetchone()[0])
    finally:
        conn.close()

    priced = [v for v in lows if v is not None]
    return {"points": points, "low_7d": min(priced) if priced else None,
            "calibrating": len(points) == 0}


# --------------------------------------------------------------------------- #
# One-shot backfill from the flat admin.db table
# --------------------------------------------------------------------------- #

def backfill_from(admin_db_path: str, dest: str = None, now_ts: int = None) -> dict:
    """Fill the tiered file from the legacy admin.db market_price_history table.

    admin.db is opened `mode=ro` and never written. Work is chunked per template
    (~2.9k rows each) so 4M rows never land in memory at once, and every write is
    INSERT OR REPLACE, so re-running it is safe while lastsietch-admin keeps writing the
    old table. Returns the row counts written per tier."""
    src = sqlite3.connect(f"file:{admin_db_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    conn = _connect(dest)
    try:
        conn.executescript(_SCHEMA)
        now_ts = _now_ts() if now_ts is None else int(now_ts)
        raw_from = now_ts - _RAW_RETENTION_HOURS * _HOUR
        hourly_from = now_ts - _HOURLY_RETENTION_DAYS * _DAY
        daily_from = now_ts - _DAILY_RETENTION_DAYS * _DAY

        names = [r[0] for r in src.execute(
            "SELECT DISTINCT template_id FROM market_price_history "
            "WHERE template_id IS NOT NULL AND template_id <> '' "
            "ORDER BY template_id")]
        _intern(conn, names)
        tmap = _template_map(conn)

        stats = {"templates": len(names), "read": 0, "unparsed": 0,
                 "raw": 0, "hourly": 0, "daily": 0}
        for name in names:
            tpl = tmap.get(name.lower())
            if tpl is None:
                continue
            raw_rows = []
            hourly: dict = {}
            for r in src.execute(
                    "SELECT min_price, median_price, listing_count, total_qty, "
                    "captured_at FROM market_price_history "
                    "WHERE template_id = ? COLLATE NOCASE", (name,)):
                ts = _to_ts(r["captured_at"])
                if ts is None:
                    stats["unparsed"] += 1
                    continue
                stats["read"] += 1
                if ts >= raw_from:
                    raw_rows.append((tpl, ts, r["min_price"], r["median_price"],
                                     r["listing_count"], r["total_qty"]))
                # Hourly buckets are folded for the whole DAILY window, because
                # daily is a fold of hourly (as in rollup()); only the ones inside
                # the hourly retention are written to mph_hourly.
                if ts >= daily_from:
                    key = _floor(ts, _HOUR)
                    b = hourly.get(key)
                    if b is None:
                        b = hourly[key] = _Bucket()
                    b.add(r["min_price"], r["median_price"], r["listing_count"],
                          r["total_qty"])
            if raw_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO mph_raw(tpl, ts, min_price, "
                    "median_price, listing_count, total_qty) VALUES(?,?,?,?,?,?)",
                    raw_rows)
                stats["raw"] += len(raw_rows)

            hourly_rows = [(tpl, hour_ts) + b.fold()
                           for hour_ts, b in sorted(hourly.items())]
            keep_hourly = [row for row in hourly_rows if row[1] >= hourly_from]
            if keep_hourly:
                conn.executemany(
                    "INSERT OR REPLACE INTO mph_hourly(tpl, hour_ts, min_price, "
                    "median_price, avg_listing_count, avg_total_qty, samples) "
                    "VALUES(?,?,?,?,?,?,?)", keep_hourly)
                stats["hourly"] += len(keep_hourly)

            # Daily from the hourly rows just folded, exactly as rollup() does it.
            daily: dict = {}
            for _tpl, hour_ts, mn, md, lc, qty, samples in hourly_rows:
                key = _floor(hour_ts, _DAY)
                b = daily.get(key)
                if b is None:
                    b = daily[key] = _Bucket()
                b.add(mn, md, lc, qty, samples=samples)
            daily_rows = [(tpl, day_ts) + b.fold()
                          for day_ts, b in sorted(daily.items())]
            if daily_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO mph_daily(tpl, day_ts, min_price, "
                    "median_price, avg_listing_count, avg_total_qty, samples) "
                    "VALUES(?,?,?,?,?,?,?)", daily_rows)
                stats["daily"] += len(daily_rows)
            conn.commit()
        # Deliberately NOT stamping last_rollup_hour: the first capture after the
        # restart then does a full recompute over the retained raw, which lifts
        # the current hour's bucket to date immediately instead of an hour later.
        conn.commit()
        return stats
    finally:
        src.close()
        conn.close()


def counts(path: str = None) -> dict:
    """Row counts per tier plus the file size. The deploy script's verify step."""
    conn = _connect(path)
    try:
        conn.executescript(_SCHEMA)
        out = {}
        for table in ("templates", "mph_raw", "mph_hourly", "mph_daily"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out["last_rollup_hour"] = _meta_get(conn, "last_rollup_hour")
    finally:
        conn.close()
    target = path or MARKET_HISTORY_DB_PATH
    out["path"] = target
    out["bytes"] = os.path.getsize(target) if os.path.exists(target) else 0
    return out


def _main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Tiered market price-history maintenance (one-shot).")
    ap.add_argument("--backfill-from", metavar="ADMIN_DB",
                    help="legacy admin.db to read market_price_history from "
                         "(opened read-only; never written)")
    ap.add_argument("--dest", metavar="DB",
                    help=f"target file (default: {MARKET_HISTORY_DB_PATH})")
    ap.add_argument("--counts", action="store_true",
                    help="print row counts per tier and exit")
    args = ap.parse_args(argv)

    if args.counts:
        print(json.dumps(counts(args.dest), indent=2))
        return 0
    if args.backfill_from:
        if not os.path.exists(args.backfill_from):
            print(f"FATAL: no such file: {args.backfill_from}")
            return 2
        stats = backfill_from(args.backfill_from, dest=args.dest)
        print(json.dumps(stats, indent=2))
        print(json.dumps(counts(args.dest), indent=2))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
