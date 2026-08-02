#!/usr/bin/env python3
# Read-only historical player counts from the telemetry-logger SQLite store.
# No writes. Deployed to lastsietch-dune:/root/dune-presence.py — invoked by the relay
# over SSH. Phase 4: repointed off the retired lastsietch-stats sampler's stats.db;
# the telemetry logger's presence table has the identical (ts, account_id)
# schema and the same 5-minute sweep cadence.
import json
import sqlite3
import sys
import time

DB = "file:/var/lib/lastsietch-telemetry/telemetry.db?mode=ro"
WINDOWS = {"1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400,
           "7d": 604800, "30d": 2592000}


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "24h"
    secs = WINDOWS.get(window)
    if secs is None:
        print(json.dumps({"error": "invalid window",
                          "valid": sorted(WINDOWS)}))
        return
    since = int(time.time()) - secs
    con = sqlite3.connect(DB, uri=True)
    try:
        rows = con.execute(
            "SELECT ts, COUNT(*) FROM presence WHERE ts >= ? "
            "GROUP BY ts ORDER BY ts", (since,)).fetchall()
    finally:
        con.close()
    series = [{"ts": ts, "count": c} for ts, c in rows]
    peak = max((c for _, c in rows), default=0)
    # Telemetry logger sweeps every 5 min; each presence row = 5 player-minutes.
    play_hours = round(sum(c for _, c in rows) * 5 / 60, 2)
    print(json.dumps({
        "window": window, "since": since, "samples": len(series),
        "peak": peak, "play_hours": play_hours, "series": series,
    }))


if __name__ == "__main__":
    main()
