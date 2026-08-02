#!/usr/bin/env python3
# VC2 P1 — walk newest UTC-window dir under captures-admin/response/ and
# emit per-partition player counts (InGameOrInTransitPlayerCount) for
# the Hero / World Counters panels.
#
# Stdlib only — deploys to lastsietch-dune:/opt/lastsietch-rmq-bridge/, invoked by the
# `rmq-partition-counts` dispatcher token (no args).
#
# Captures-dir constraint: read newest UTC-window dir ONLY — never walk
# the full tree. response/ is a high-frequency exchange (per-partition
# heartbeats).
#
# Aggregator side joins partition routing keys to map names via
# dune.world_partition (existing /dune/status payload); this helper
# stays exchange-local and emits the raw routing_key buckets.
import json
import sys
import time
from pathlib import Path

ROOT = Path('/var/lib/lastsietch-rmq-bridge/captures-admin/response')


def emit(obj):
    print(json.dumps(obj))
    sys.exit(0)


def extract_count(body):
    """Pull the InGameOrInTransitPlayerCount from a response body."""
    if not isinstance(body, dict):
        return None
    for k in ('InGameOrInTransitPlayerCount',
              'ingame_or_in_transit_player_count',
              'PlayerCount', 'playerCount', 'connected_players'):
        v = body.get(k)
        if isinstance(v, int):
            return v
    return None


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

        # Take the latest message per routing_key — partition heartbeats
        # repeat frequently within a window; we want the most recent.
        latest_per_key = {}
        for f in files:
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            rk = data.get('routing_key') or ''
            if not rk:
                continue
            ts = int(f.stat().st_mtime)
            cur = latest_per_key.get(rk)
            if cur is None or ts >= cur[0]:
                latest_per_key[rk] = (ts, data)

        partitions = {}
        total = 0
        for rk, (ts, data) in latest_per_key.items():
            body = data.get('body_json') or data.get('body') or {}
            count = extract_count(body)
            partitions[rk] = {
                "ts": ts,
                "count": count if count is not None else 0,
                "count_field_found": count is not None,
            }
            if count is not None:
                total += count

        emit({
            "available": True,
            "generated_at": int(time.time()),
            "window_dir": newest.name,
            "partitions": partitions,
            "total_players": total,
        })
    except Exception as e:
        emit({"available": False, "error": str(e)})


if __name__ == '__main__':
    main()
