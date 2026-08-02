#!/usr/bin/env python3
# VC2 P1 — walk newest UTC-window dir under captures-admin/rpc/ and emit
# recent bgdRpc messages for the Funcom Intelligence panel.
#
# Stdlib only — deploys to lastsietch-dune:/opt/lastsietch-rmq-bridge/, invoked by the
# `rmq-bgd-rpc-recent` dispatcher token (no args).
#
# Filter: envelope.routing_key matches the BGD inbox serverGuid for the
# active battlegroup. Override via env LASTSIETCH_BGD_RPC_SERVER_GUID. Default
# matches the live BG documented in MEMORY (sh-...-nhzgrx, 2026-05-19).
#
# Captures-dir constraint: read at most MAX_WINDOWS_WALKBACK newest dirs.
# BGD RPC is *sparse* (~1-2/day) and rpc/ is rotated to a new 30-min UTC
# window dir every 30 min, so the newest dir frequently has zero matches.
# We walk back up to MAX_WINDOWS_WALKBACK to find a non-empty window.
# Bounded so a long-empty period (e.g. maintenance) can't blow out the
# read budget — at 48 windows we cover the last ~24h.
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path('/var/lib/lastsietch-rmq-bridge/captures-admin/rpc')
BGD_GUID = os.environ.get('LASTSIETCH_BGD_RPC_SERVER_GUID',
                          'sh-<your-hostid>-<random>')
RECENT_CAP = 50
MAX_WINDOWS_WALKBACK = 48


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

        # Walk back through the newest MAX_WINDOWS_WALKBACK dirs collecting
        # files until we have at least one. BGD RPC is sparse; the newest
        # 30-min dir is frequently empty.
        candidate_windows = windows[-MAX_WINDOWS_WALKBACK:]
        files = []
        scanned_windows = []
        for w in reversed(candidate_windows):
            scanned_windows.append(w.name)
            files.extend(sorted(w.glob('*.json'),
                                key=lambda p: p.stat().st_mtime))
            if files:
                break
        if not files:
            return emit({"available": False,
                         "error": f"no capture files in newest "
                                  f"{len(scanned_windows)} window(s)",
                         "windows_scanned": scanned_windows[:5]})

        newest = Path(scanned_windows[-1])
        cutoff_1h = time.time() - 3600
        last_ts = 0
        count_window = 0
        count_1h = 0
        recent = []

        for f in files:
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            if data.get('routing_key') != BGD_GUID:
                continue

            ts = int(f.stat().st_mtime)
            count_window += 1
            if ts >= cutoff_1h:
                count_1h += 1
            if ts > last_ts:
                last_ts = ts

            body = data.get('body_json') or data.get('body') or {}
            if isinstance(body, dict):
                # surface the top-level RPC method name when present
                preview = (body.get('method') or body.get('Method')
                           or body.get('action') or '')
                if not preview and body:
                    preview = next(iter(body))
            else:
                preview = str(body)[:80]

            recent.append({
                "ts": ts,
                "routing_key": data.get('routing_key') or '',
                "body_preview": str(preview)[:120],
            })

        recent.sort(key=lambda r: r['ts'], reverse=True)

        emit({
            "available": True,
            "last_ts": last_ts if last_ts else None,
            "count_1h": count_1h,
            "count_today": count_window,
            "window_dir": newest.name,
            "windows_scanned": len(scanned_windows),
            "server_guid": BGD_GUID,
            "recent": recent[:RECENT_CAP],
        })
    except Exception as e:
        emit({"available": False, "error": str(e)})


if __name__ == '__main__':
    main()
