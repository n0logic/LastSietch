#!/usr/bin/env python3
# VC2 P1 — read newest settingsUpdate capture and emit summary for the
# Funcom Intelligence panel of the Server > Monitor dashboard.
#
# Stdlib only — deploys to lastsietch-dune:/opt/lastsietch-rmq-bridge/, invoked by the
# `rmq-last-funcom-push` dispatcher token (no args).
#
# Output (success):
#   {"available": true, "last_ts": <epoch>, "last_sha256": "<12-hex>",
#    "last_routing_key": "<str>", "push_count_today": <int>,
#    "window_dir": "<UTC stamp>"}
# Output (failure):
#   {"available": false, "error": "<msg>"}
#
# Exit code is 0 on both success and failure — the envelope carries the
# state. SettingsUpdate is a low-volume exchange (Funcom config pushes
# at coriolis cycle / hot-patch cadence), so a bounded multi-window
# scan for push_count_today is safe: capped to the newest <=24 dirs.
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path('/var/lib/lastsietch-rmq-bridge/captures-admin/settingsUpdate')
MAX_WINDOWS_FOR_TODAY = 24


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

        latest = files[-1]
        body = latest.read_text(encoding='utf-8')
        sha = hashlib.sha256(body.encode('utf-8')).hexdigest()[:12]

        try:
            payload = json.loads(body)
            routing_key = payload.get("routing_key") or ""
        except Exception:
            routing_key = ""

        # push_count_today: bounded to the newest <=24 window dirs, mtime
        # cutoff 24h. Cheap stat-only walk; no JSON parse.
        cutoff = time.time() - 86400
        push_count_today = 0
        for w in windows[-MAX_WINDOWS_FOR_TODAY:]:
            for f in w.glob('*.json'):
                try:
                    if f.stat().st_mtime >= cutoff:
                        push_count_today += 1
                except OSError:
                    continue

        emit({
            "available": True,
            "last_ts": int(latest.stat().st_mtime),
            "last_sha256": sha,
            "last_routing_key": routing_key,
            "push_count_today": push_count_today,
            "window_dir": newest.name,
        })
    except Exception as e:
        emit({"available": False, "error": str(e)})


if __name__ == '__main__':
    main()
