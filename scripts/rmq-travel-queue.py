#!/usr/bin/env python3
# VC2 P1 — walk newest UTC-window dir under captures-admin/travelQueueStatus/
# and emit current queue depth + per-destination breakdown for the Hero
# panel.
#
# Stdlib only — deploys to lastsietch-dune:/opt/lastsietch-rmq-bridge/, invoked by the
# `rmq-travel-queue` dispatcher token (no args).
#
# Observed Funcom payload shape (captured 2026-05-27, fanout exchange):
#   {
#     "MapName": "SH_Arrakeen",            # destination map identifier
#     "QueueState": {},                    # dict of queued players -> state
#     "InGameOrInTransitPlayerCount": 0,
#     "ServerState": 3,
#     "Timestamp": "..."
#   }
# Routing key is always empty (fanout). Depth per destination = len(QueueState).
# Total depth = sum of len(QueueState) across the latest message per MapName
# in the newest UTC-window dir.
#
# Captures-dir constraint: read newest UTC-window dir ONLY.
import json
import sys
import time
from pathlib import Path

ROOT = Path('/var/lib/lastsietch-rmq-bridge/captures-admin/travelQueueStatus')


def emit(obj):
    print(json.dumps(obj))
    sys.exit(0)


def main():
    try:
        if not ROOT.is_dir():
            return emit({"available": False,
                         "error": f"captures dir missing: {ROOT}"})
        windows = sorted(p for p in ROOT.iterdir() if p.is_dir())
        if not windows:
            return emit({"available": False, "error": "no capture windows"})

        newest = windows[-1]
        files = sorted(newest.glob('*.json'),
                       key=lambda p: p.stat().st_mtime)
        if not files:
            return emit({"available": False,
                         "error": f"no capture files in {newest.name}"})

        # Latest message per MapName in this window. Funcom publishes per-map
        # state updates; the freshest one wins.
        latest_per_map = {}
        for f in files:
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            body = data.get('body_json') or data.get('body') or {}
            if not isinstance(body, dict):
                continue
            map_name = body.get('MapName')
            if not map_name:
                continue
            ts = int(f.stat().st_mtime)
            cur = latest_per_map.get(map_name)
            if cur is None or ts >= cur[0]:
                latest_per_map[map_name] = (ts, body)

        if not latest_per_map:
            return emit({"available": False,
                         "error": "no parseable messages in newest window",
                         "window_dir": newest.name})

        by_destination = {}
        total_depth = 0
        latest_ts = 0
        for map_name, (ts, body) in latest_per_map.items():
            qs = body.get('QueueState')
            qdepth = len(qs) if isinstance(qs, dict) else 0
            by_destination[map_name] = {
                "depth": qdepth,
                "in_game_or_in_transit": body.get(
                    'InGameOrInTransitPlayerCount') or 0,
                "server_state": body.get('ServerState'),
                "ts": ts,
            }
            total_depth += qdepth
            if ts > latest_ts:
                latest_ts = ts

        emit({
            "available": True,
            "generated_at": int(time.time()),
            "window_dir": newest.name,
            "depth": total_depth,
            "destinations_count": len(latest_per_map),
            "by_destination": by_destination,
            "latest_ts": latest_ts,
        })
    except Exception as e:
        emit({"available": False, "error": str(e)})


if __name__ == '__main__':
    main()
