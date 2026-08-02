"""
Last Sietch Dune telemetry read API.

A small FastAPI app, localhost-bound, the single read contract over
telemetry.db. Consumers (relay, digest, admin-backend) call this instead of
opening the SQLite file directly, so PII (roster names) has one controlled
choke point and the leaderboard SQL lives in one place.

READ-ONLY: telemetry.db is opened with the SQLite `?mode=ro` URI per request.
There are no write paths and no DDL.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import load_config  # noqa: E402

app = FastAPI(title="Last Sietch Dune Telemetry API")

_CONFIG = load_config()

# Supported presence/world rolling windows.
_WINDOWS = {
    "1h": 3600, "6h": 6 * 3600, "12h": 12 * 3600,
    "24h": 24 * 3600, "7d": 7 * 24 * 3600, "30d": 30 * 24 * 3600,
}


def _connect():
    """Open telemetry.db read-only. Raises 503 if the store is missing."""
    if not os.path.exists(_CONFIG.telemetry_db):
        raise HTTPException(status_code=503, detail="telemetry store not ready")
    uri = "file:%s?mode=ro" % _CONFIG.telemetry_db
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _window_seconds(window):
    if window not in _WINDOWS:
        raise HTTPException(
            status_code=400,
            detail="window must be one of %s" % ",".join(_WINDOWS))
    return _WINDOWS[window]


def _week_start_epoch(week):
    """Epoch start of an ISO week. 'current' = the ongoing ISO week."""
    if week == "current":
        now = datetime.now(timezone.utc)
        year, wk, _ = now.isocalendar()
    else:
        try:
            year, wk = week.split("-W")
            year, wk = int(year), int(wk)
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=400, detail="week must be 'current' or 'YYYY-Www'")
    monday = datetime.fromisocalendar(year, wk, 1).replace(tzinfo=timezone.utc)
    return int(monday.timestamp())


def _iso_week_label(week):
    if week == "current":
        year, wk, _ = datetime.now(timezone.utc).isocalendar()
        return "%04d-W%02d" % (year, wk)
    return week


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/presence")
def presence(window: str = Query("24h")):
    """Online-player concurrency series + peak + play-hours for a window."""
    span = _window_seconds(window)
    since = int(time.time()) - span
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ts, count(*) AS concurrent FROM presence "
            "WHERE ts >= ? GROUP BY ts ORDER BY ts", (since,)).fetchall()
    finally:
        conn.close()
    series = [{"ts": r["ts"], "concurrent": r["concurrent"]} for r in rows]
    peak = max((r["concurrent"] for r in rows), default=0)
    # Play-hours: each sample row is one player observed for one sweep; the
    # sweep cadence is the presence interval.
    total_samples = sum(r["concurrent"] for r in rows)
    play_hours = round(total_samples * _CONFIG.presence_interval / 3600.0, 2)
    return {"window": window, "peak": peak,
            "play_hours": play_hours, "series": series}


@app.get("/world")
def world(window: str = Query("24h")):
    """world_snapshots series, grouped per metric, for a window."""
    span = _window_seconds(window)
    since = int(time.time()) - span
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ts, metric, value FROM world_snapshots "
            "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    finally:
        conn.close()
    metrics = {}
    for r in rows:
        metrics.setdefault(r["metric"], []).append(
            {"ts": r["ts"], "value": r["value"]})
    return {"window": window, "metrics": metrics}


# Live-map stream poll cadence: how often /positions/stream re-reads the store
# and looks for a new sweep. Matches the positions logger cadence.
_STREAM_POLL_S = 5


def _positions_payload(conn):
    """Build the Stream A contract from the latest player_positions sweep."""
    latest = conn.execute(
        "SELECT max(ts) AS ts FROM player_positions").fetchone()
    if latest is None or latest["ts"] is None:
        return {"available": False, "map": "HaggaBasin",
                "ts": None, "count": 0, "players": []}
    ts = latest["ts"]
    rows = conn.execute(
        "SELECT x, y, partition_id FROM player_positions WHERE ts=?", (ts,)).fetchall()
    return {"available": True, "map": "HaggaBasin", "ts": ts,
            "count": len(rows),
            "players": [{"x": r["x"], "y": r["y"], "p": r["partition_id"]} for r in rows]}


def _read_positions():
    """Open the store read-only and return the positions payload. On a missing
    store, returns the unavailable shape rather than raising."""
    try:
        conn = _connect()
    except HTTPException:
        return {"available": False, "map": "HaggaBasin",
                "ts": None, "count": 0, "players": []}
    try:
        return _positions_payload(conn)
    finally:
        conn.close()


@app.get("/positions/live")
def positions_live():
    """Latest player-position snapshot for the live Hagga map.

    Coords-only (PII-safe): no names, no account ids. Returns the most-recent
    sweep's rows. `available` is false until the positions stream has written
    at least one sweep.
    """
    return _read_positions()


@app.get("/positions/stream")
async def positions_stream(request: Request):
    """SSE stream of live Hagga player positions.

    Emits a `data:` frame on connect and again whenever the latest sweep `ts`
    changes; otherwise sends a comment keepalive every poll so the proxy hops
    do not idle-close the connection. The frame body is the same JSON shape as
    /positions/live (the Stream A data contract).
    """
    async def gen():
        payload = _read_positions()
        last_ts = payload.get("ts")
        yield "data: %s\n\n" % json.dumps(payload)
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(_STREAM_POLL_S)
            payload = _read_positions()
            if payload.get("ts") != last_ts:
                last_ts = payload.get("ts")
                yield "data: %s\n\n" % json.dumps(payload)
            else:
                yield ": keepalive\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/events")
def events(limit: int = Query(100, ge=1, le=1000),
           type: int = Query(None),
           killer_type: str = Query(None)):
    """Recent combat_events, newest-first, with optional type/killer filters."""
    clauses = []
    params = []
    if type is not None:
        clauses.append("event_type = ?")
        params.append(type)
    if killer_type:
        clauses.append("killer_type = ?")
        params.append(killer_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT occurred_at, occurred_epoch, map, partition_id, event_type, "
            "actor_id, victim_account_id, victim_name, killer_type, "
            "killer_account_id, killer_name, damage_type, x, y, z "
            "FROM combat_events %s ORDER BY occurred_epoch DESC LIMIT ?" % where,
            params).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "events": [dict(r) for r in rows]}


@app.get("/grants")
def grants(limit: int = Query(50, ge=1, le=500),
           grant_type: str = Query(None),
           account_id: str = Query(None)):
    """Recent grant_events, newest-first, with optional type/account filters."""
    clauses, params = [], []
    if grant_type:
        clauses.append("grant_type = ?")
        params.append(grant_type)
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT pg_grant_id, granted_at, granted_epoch, account_id, "
            "grant_type, detail, operator, status, harvested_at "
            "FROM grant_events %s ORDER BY granted_epoch DESC, pg_grant_id DESC "
            "LIMIT ?" % where, params).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "grants": [dict(r) for r in rows]}


@app.get("/transfers")
def transfers(limit: int = Query(100, ge=1, le=1000),
              event_type: str = Query(None),
              fls_id: str = Query(None)):
    """Recent transfer / removal events, newest-first."""
    clauses, params = [], []
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if fls_id:
        clauses.append("fls_id = ?")
        params.append(fls_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, ts, event_type, account_id, fls_id, "
            "transfer_state, raw_json "
            "FROM transfer_events %s ORDER BY ts DESC, id DESC "
            "LIMIT ?" % where, params).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "transfers": [dict(r) for r in rows]}


def _name_for_account(conn, account_id):
    """Best-effort display name: latest roster name, else combat name, else id."""
    if not account_id:
        return account_id
    row = conn.execute(
        "SELECT character_name FROM roster_snapshot "
        "WHERE account_id=? AND character_name IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (account_id,)).fetchone()
    if row and row["character_name"]:
        return row["character_name"]
    row = conn.execute(
        "SELECT killer_name FROM combat_events "
        "WHERE killer_account_id=? AND killer_name IS NOT NULL "
        "ORDER BY occurred_epoch DESC LIMIT 1", (account_id,)).fetchone()
    if row and row["killer_name"]:
        return row["killer_name"]
    return account_id


@app.get("/leaderboard/pvp")
def leaderboard_pvp(week: str = Query("current")):
    """PvP Champ: player kill counts for an ISO week."""
    week_start = _week_start_epoch(week)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT killer_account_id, count(*) AS kills FROM combat_events "
            "WHERE killer_type='Player' AND occurred_epoch >= ? "
            "GROUP BY killer_account_id ORDER BY kills DESC", (week_start,)
        ).fetchall()
        out = [{"account_id": r["killer_account_id"],
                "name": _name_for_account(conn, r["killer_account_id"]),
                "kills": r["kills"]} for r in rows if r["killer_account_id"]]
    finally:
        conn.close()
    return {"week": _iso_week_label(week), "leaderboard": out}


@app.get("/leaderboard/deaths")
def leaderboard_deaths(week: str = Query("current")):
    """Most deaths + K/D for an ISO week (derived from combat_events)."""
    week_start = _week_start_epoch(week)
    conn = _connect()
    try:
        death_rows = conn.execute(
            "SELECT victim_account_id, count(*) AS deaths FROM combat_events "
            "WHERE event_type=0 AND victim_account_id IS NOT NULL "
            "AND occurred_epoch >= ? GROUP BY victim_account_id",
            (week_start,)).fetchall()
        kill_rows = conn.execute(
            "SELECT killer_account_id, count(*) AS kills FROM combat_events "
            "WHERE killer_type='Player' AND occurred_epoch >= ? "
            "GROUP BY killer_account_id", (week_start,)).fetchall()
        kills = {r["killer_account_id"]: r["kills"] for r in kill_rows}
        out = []
        for r in death_rows:
            acct = r["victim_account_id"]
            deaths = r["deaths"]
            k = kills.get(acct, 0)
            kd = round(k / deaths, 2) if deaths else float(k)
            out.append({"account_id": acct,
                        "name": _name_for_account(conn, acct),
                        "deaths": deaths, "kills": k, "kd": kd})
        out.sort(key=lambda e: e["deaths"], reverse=True)
    finally:
        conn.close()
    return {"week": _iso_week_label(week), "leaderboard": out}


@app.get("/leaderboard/pilots")
def leaderboard_pilots(week: str = Query("current")):
    """Pilot of the Week: top air distance from flight_distance_weekly."""
    iso_week = _iso_week_label(week)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT account_id, vehicle_class, meters FROM flight_distance_weekly "
            "WHERE iso_week=? ORDER BY meters DESC", (iso_week,)).fetchall()
        out = [{"account_id": r["account_id"],
                "name": _name_for_account(conn, r["account_id"]),
                "vehicle_class": r["vehicle_class"],
                "meters": round(r["meters"], 1),
                "km": round(r["meters"] / 1000.0, 3)} for r in rows]
    finally:
        conn.close()
    return {"week": iso_week, "leaderboard": out}


@app.get("/progression/snapshot")
def progression_snapshot():
    """Latest progression sample per account (Stream D).

    online_status is computed LIVE from the presence stream's most-recent
    sweep, not from player_progression.online_status — that column is frozen
    at the value seen the last time the player's XP changed (sample_hash
    excludes online_status to avoid churn; see streams/progression.py:_hash_sample),
    so it goes stale as soon as a player stops earning XP. A player is
    considered Online if their latest presence row is within
    2 * presence_interval (one missed-sample tolerance)."""
    conn = _connect()
    try:
        online_cutoff = int(time.time()) - 2 * _CONFIG.presence_interval
        rows = conn.execute(
            "SELECT p.account_id, p.char_name, "
            "       CASE WHEN COALESCE(pres.last_ts, 0) >= ? "
            "            THEN 'Online' ELSE 'Offline' END AS online_status, "
            "       p.xp, p.lvl, "
            "       p.total_sp, p.unspent_sp, p.keystone_sp, p.intel, p.ts "
            "FROM player_progression p "
            "JOIN (SELECT account_id, MAX(id) AS max_id "
            "      FROM player_progression GROUP BY account_id) m "
            "  ON m.account_id = p.account_id AND m.max_id = p.id "
            "LEFT JOIN (SELECT account_id, MAX(ts) AS last_ts "
            "           FROM presence GROUP BY account_id) pres "
            "  ON pres.account_id = p.account_id "
            "ORDER BY p.lvl DESC, p.xp DESC",
            (online_cutoff,)).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "players": [dict(r) for r in rows]}


@app.get("/progression/{account_id}/history")
def progression_history(account_id: str,
                        since: int = Query(0, ge=0),
                        limit: int = Query(200, ge=1, le=2000)):
    """Time-series of progression snapshots for one account, oldest-first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ts, char_name, online_status, xp, lvl, total_sp, "
            "unspent_sp, keystone_sp, intel "
            "FROM player_progression WHERE account_id=? AND ts>=? "
            "ORDER BY ts ASC LIMIT ?",
            (account_id, since, limit)).fetchall()
    finally:
        conn.close()
    return {"account_id": account_id, "count": len(rows),
            "history": [dict(r) for r in rows]}


@app.get("/progression/levelups")
def progression_levelups(since: int = Query(None),
                         account_id: str = Query(None),
                         limit: int = Query(100, ge=1, le=1000)):
    """Recent level-up events, newest-first, optionally filtered by account."""
    if since is None:
        since = int(time.time()) - 7 * 24 * 3600
    clauses = ["ts >= ?"]
    params = [since]
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    where = " AND ".join(clauses)
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ts, account_id, char_name, from_lvl, to_lvl, "
            "from_xp, to_xp, crossed_lvl, detected_at "
            "FROM player_progression_levelups WHERE %s "
            "ORDER BY ts DESC, crossed_lvl DESC LIMIT ?" % where,
            params).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "levelups": [dict(r) for r in rows]}


@app.get("/read-models")
def read_models(account_id: int = Query(None)):
    """Per-account portal/admin read models for the web host local-mirror sync.

    Returns one entry per account with the section payloads (parsed from their
    stored JSON so the response is a single structured document) plus the
    denormalized scalars, and the read_models sync_meta row for the freshness
    guard. Reads player_read_model in telemetry.db (the collector full-refreshes
    it each cycle); the game DB is NOT touched on this path. The the web host bg
    loop pulls this via the relay and upserts into /opt/lastsietch-admin/mirror.sqlite."""
    conn = _connect()
    try:
        where, params = "", []
        if account_id is not None:
            where, params = "WHERE account_id = ?", [account_id]
        rows = conn.execute(
            "SELECT account_id, char_name, online, lvl, intel, current_map, "
            "progress_json, specializations_json, landsraad_json, tags_json, "
            "src_synced_at FROM player_read_model %s ORDER BY account_id" % where,
            params).fetchall()
        meta = conn.execute(
            "SELECT section, last_run_at, row_count, ok, note FROM sync_meta "
            "WHERE section='read_models'").fetchone()
    finally:
        conn.close()

    def _j(v):
        return json.loads(v) if v else None

    players = [{
        "account_id": r["account_id"],
        "char_name": r["char_name"],
        "online": r["online"],
        "lvl": r["lvl"],
        "intel": r["intel"],
        "current_map": r["current_map"],
        "progress": _j(r["progress_json"]),
        "specializations": _j(r["specializations_json"]),
        "landsraad": _j(r["landsraad_json"]),
        "tags": _j(r["tags_json"]),
        "src_synced_at": r["src_synced_at"],
    } for r in rows]
    return {"count": len(players), "players": players,
            "sync": dict(meta) if meta else None}


@app.get("/storage")
def storage_models(account_id: int = Query(None)):
    """Per-account storage snapshots for the web host local-mirror sync (Phase 2).

    Returns one entry per account with the parsed storage blob (containers +
    items_by_container + search_rows) plus the 'storage' sync_meta row for the
    freshness guard. Reads player_storage in telemetry.db (full-refreshed by the
    collector each cycle); the game DB is NOT touched on this path.
   """
    conn = _connect()
    try:
        where, params = "", []
        if account_id is not None:
            where, params = "WHERE account_id = ?", [account_id]
        rows = conn.execute(
            "SELECT account_id, storage_json, src_synced_at FROM player_storage "
            "%s ORDER BY account_id" % where, params).fetchall()
        meta = conn.execute(
            "SELECT section, last_run_at, row_count, ok, note FROM sync_meta "
            "WHERE section='storage'").fetchone()
    finally:
        conn.close()
    players = [{
        "account_id": r["account_id"],
        "storage": json.loads(r["storage_json"]) if r["storage_json"] else None,
        "src_synced_at": r["src_synced_at"],
    } for r in rows]
    return {"count": len(players), "players": players,
            "sync": dict(meta) if meta else None}


@app.get("/market")
def market_listings_all():
    """The full active CHOAM exchange listing set for the web host local-mirror
    sync (Phase 2). The mirror runs the substring search LOCALLY over these. Reads
    market_listing in telemetry.db (full-refreshed by the collector); the game DB
    is NOT touched"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT template_id, item_price, quality_level, is_npc_order, stack, "
            "owner_id, order_id, revision, src_synced_at FROM market_listing "
            "ORDER BY is_npc_order, item_price").fetchall()
        meta = conn.execute(
            "SELECT section, last_run_at, row_count, ok, note FROM sync_meta "
            "WHERE section='market'").fetchone()
    finally:
        conn.close()
    listings = [{
        "template_id": r["template_id"],
        "item_price": r["item_price"],
        "quality_level": r["quality_level"],
        "is_npc_order": bool(r["is_npc_order"]),
        "stack": r["stack"],
        "owner_id": r["owner_id"],
        "order_id": r["order_id"],
        "revision": r["revision"],
    } for r in rows]
    src = rows[0]["src_synced_at"] if rows else None
    return {"count": len(listings), "listings": listings, "src_synced_at": src,
            "sync": dict(meta) if meta else None}


@app.get("/login-days")
def login_days(account_id: str = Query(None),
               days: int = Query(60, ge=1, le=400)):
    """Per-account login-day history for the rewards streak calc (login-rewards V2).

    Returns the UTC calendar days (date_utc) an account was seen online, newest
    first, within the last `days` window. Reads portal_login_days in telemetry.db
    (written by the login_days stream); the game DB is NOT touched. Streak = the
    run of consecutive date_utc ending today, computed by the consumer. With no
    account_id, returns every account's rows in the window (the web host mirror pull)."""
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    conn = _connect()
    try:
        where, params = ["date_utc >= ?"], [cutoff]
        if account_id is not None:
            where.append("account_id = ?")
            params.append(account_id)
        rows = conn.execute(
            "SELECT account_id, date_utc, first_seen_ts FROM portal_login_days "
            "WHERE %s ORDER BY account_id, date_utc DESC" % " AND ".join(where),
            params).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "days_window": days,
            "login_days": [dict(r) for r in rows]}


@app.get("/roster/latest")
def roster_latest():
    """Most-recent roster snapshot. PII (character names) - localhost-only."""
    conn = _connect()
    try:
        latest = conn.execute(
            "SELECT max(ts) AS ts FROM roster_snapshot").fetchone()
        if latest is None or latest["ts"] is None:
            return {"ts": None, "roster": []}
        ts = latest["ts"]
        rows = conn.execute(
            "SELECT account_id, character_name, map, guild, faction "
            "FROM roster_snapshot WHERE ts=?", (ts,)).fetchall()
    finally:
        conn.close()
    return {"ts": ts, "roster": [dict(r) for r in rows]}
