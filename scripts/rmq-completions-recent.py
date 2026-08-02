#!/usr/bin/env python3
# VC2 P1 — walk newest UTC-window dir under captures-admin/completions/
# and emit the last N events for the Live Action Stream panel.
#
# Stdlib only — deploys to lastsietch-dune:/opt/lastsietch-rmq-bridge/, invoked by the
# `rmq-completions-recent` dispatcher token. Optional arg: limit (1..200,
# default 50). Dispatcher pre-validates limit regex `^[0-9]{1,3}$`.
#
# Routing-key prefixes seen on this exchange: validation.*, completion.*,
# server_state.* (per brief §5.4). We surface topic (prefix) + detail
# (suffix) so the panel can filter/render distinctly.
#
# Captures-dir constraint: read newest UTC-window dir ONLY.
import json
import sys
import time
from pathlib import Path

ROOT = Path('/var/lib/lastsietch-rmq-bridge/captures-admin/completions')
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def emit(obj):
    print(json.dumps(obj))
    sys.exit(0)


def main():
    try:
        # Dispatcher already validated; clamp defensively.
        limit = DEFAULT_LIMIT
        if len(sys.argv) > 1 and sys.argv[1]:
            try:
                limit = int(sys.argv[1])
            except ValueError:
                limit = DEFAULT_LIMIT
        limit = max(1, min(MAX_LIMIT, limit))

        if not ROOT.is_dir():
            return emit({"available": False,
                         "error": f"captures dir missing: {ROOT}",
                         "items": []})
        windows = sorted(p for p in ROOT.iterdir() if p.is_dir())
        if not windows:
            return emit({"available": False, "error": "no capture windows",
                         "items": []})

        newest = windows[-1]
        files = sorted(newest.glob('*.json'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return emit({"available": False,
                         "error": f"no capture files in {newest.name}",
                         "items": []})

        items = []
        # Walk a bounded extra in case some files fail to parse.
        max_walk = min(len(files), limit * 4)
        for f in files[:max_walk]:
            if len(items) >= limit:
                break
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            rk = data.get('routing_key') or ''
            if '.' in rk:
                topic, _, detail = rk.partition('.')
            else:
                topic, detail = rk, ''
            body = data.get('body_json') or data.get('body') or {}
            if not isinstance(body, dict):
                body = {"raw": str(body)[:200]}
            items.append({
                "ts": int(f.stat().st_mtime),
                "routing_key": rk,
                "topic": topic,
                "detail": detail,
                "body": body,
            })

        emit({
            "available": True,
            "generated_at": int(time.time()),
            "window_dir": newest.name,
            "limit": limit,
            "count": len(items),
            "items": items,
        })
    except Exception as e:
        emit({"available": False, "error": str(e), "items": []})


if __name__ == '__main__':
    main()
