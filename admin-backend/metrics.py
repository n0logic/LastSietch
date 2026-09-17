import asyncio
import sys
import time

from config import FAST_DURATION_SEC, FAST_INTERVAL_SEC, METRICS_RETENTION_DAYS, SAMPLE_INTERVAL_SEC
from database import get_db
from relay import call_relay


# In-memory: {game_id: first_sample_unix_ts}. Used for fast-bootstrap interval gating.
_first_seen = {}


def record_sample(conn, game_id, proc_mb, vm_used_mb, vm_total_mb):
    ts = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO metrics (game_id, ts, proc_mb, vm_used_mb, vm_total_mb) VALUES (?, ?, ?, ?, ?)",
        (game_id, ts, proc_mb, vm_used_mb, vm_total_mb),
    )
    conn.commit()


def query_window(conn, game_id, window):
    now = int(time.time())
    if window == "1h":
        cutoff = now - 3600
        rows = conn.execute(
            "SELECT ts, proc_mb, vm_used_mb FROM metrics WHERE game_id=? AND ts > ? ORDER BY ts",
            (game_id, cutoff),
        ).fetchall()
        points = [{"ts": r[0], "proc_mb": r[1], "vm_used_mb": r[2]} for r in rows]
    else:
        cutoff = now - 7 * 86400
        rows = conn.execute(
            "SELECT (ts/3600)*3600 AS bucket, AVG(proc_mb), AVG(vm_used_mb) "
            "FROM metrics WHERE game_id=? AND ts > ? GROUP BY bucket ORDER BY bucket",
            (game_id, cutoff),
        ).fetchall()
        points = [
            {
                "ts": int(r[0]),
                "proc_mb": round(r[1], 1) if r[1] is not None else None,
                "vm_used_mb": round(r[2], 1) if r[2] is not None else None,
            }
            for r in rows
        ]

    # Aggregates pulled from raw rows in the window so 7d peaks aren't lost to bucketing.
    agg_row = conn.execute(
        "SELECT COUNT(*), AVG(proc_mb), MAX(proc_mb), AVG(vm_used_mb), MAX(vm_used_mb), "
        "MIN(ts), MAX(ts), MAX(vm_total_mb) FROM metrics WHERE game_id=? AND ts > ?",
        (game_id, cutoff),
    ).fetchone()

    samples_count = agg_row[0] or 0
    return {
        "window": window,
        "samples_count": samples_count,
        "vm_total_mb": agg_row[7],
        "avg_proc_mb": round(agg_row[1], 1) if agg_row[1] is not None else None,
        "peak_proc_mb": agg_row[2],
        "avg_vm_used_mb": round(agg_row[3], 1) if agg_row[3] is not None else None,
        "peak_vm_used_mb": agg_row[4],
        "first_sample_ts": agg_row[5],
        "last_sample_ts": agg_row[6],
        "points": points,
    }


def prune_old(conn):
    conn.execute(
        "DELETE FROM metrics WHERE ts < strftime('%s','now') - ?",
        (METRICS_RETENTION_DAYS * 86400,),
    )
    conn.commit()


async def _sample_one(game_id):
    try:
        mem = await asyncio.wait_for(call_relay(f"/games/{game_id}/server/memory"), timeout=12)
    except Exception as e:
        print(f"[metrics] {game_id} memory fetch failed: {e}", file=sys.stderr)
        return
    if not mem.get("vm_running"):
        return
    proc_mb = mem.get("proc_mb")
    vm_used_mb = mem.get("vm_used_mb")
    vm_total_mb = mem.get("vm_total_mb")
    # If the guest agent timed out, all values come back null — skip the noise row.
    if proc_mb is None and vm_used_mb is None:
        return
    conn = get_db()
    try:
        record_sample(
            conn,
            game_id,
            int(proc_mb) if proc_mb is not None else None,
            int(vm_used_mb) if vm_used_mb is not None else None,
            int(vm_total_mb) if vm_total_mb is not None else None,
        )
        _first_seen.setdefault(game_id, int(time.time()))
    finally:
        conn.close()


def _next_sleep_seconds():
    if not _first_seen:
        return FAST_INTERVAL_SEC
    now = int(time.time())
    any_fast = any((now - t) < FAST_DURATION_SEC for t in _first_seen.values())
    return FAST_INTERVAL_SEC if any_fast else SAMPLE_INTERVAL_SEC


async def start_sampler(app):
    while True:
        try:
            tick = 0
            while True:
                try:
                    # Use /games/status/batch — it carries the `provisioned` flag, /games doesn't.
                    games = await asyncio.wait_for(call_relay("/games/status/batch"), timeout=12)
                    game_ids = [
                        gid for gid, info in (games or {}).items()
                        if isinstance(info, dict) and info.get("provisioned") is True
                    ]
                    # Drop any un-provisioned games that previously slipped into _first_seen.
                    for stale in list(_first_seen.keys()):
                        if stale not in game_ids:
                            _first_seen.pop(stale, None)
                    if game_ids:
                        await asyncio.gather(*[_sample_one(gid) for gid in game_ids], return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[metrics] sampler tick failed: {e}", file=sys.stderr)

                tick += 1
                if tick % 60 == 0:
                    try:
                        conn = get_db()
                        try:
                            prune_old(conn)
                        finally:
                            conn.close()
                    except Exception as e:
                        print(f"[metrics] prune failed: {e}", file=sys.stderr)

                await asyncio.sleep(_next_sleep_seconds())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[metrics] start_sampler crashed: {e!r}; restarting in 60s", file=sys.stderr, flush=True)
            await asyncio.sleep(60)
