#!/usr/bin/env bash
# P8 — settingsUpdate capture diff helper.
#
# Deploys to lastsietch-dune:/opt/lastsietch-rmq-bridge/diff-settingsupdate.sh and is invoked
# via the dispatcher action `cvars-diff`. Compares the two most recent capture
# files under /var/lib/lastsietch-rmq-bridge/captures-admin/settingsUpdate/<UTC>/*.json
# (deployed 2026-05-26). Emits the diff envelope per P8-EXECUTION-BRIEF §6 + B.9.
#
# Stdlib only — shell + python3 (no jq).
#
# Exit codes (matches brief §6 + B.9):
#   0 = no diff (identical sorted payloads)
#   1 = diff found
#   2 = error (helper missing, no captures, etc.)
set -euo pipefail

CAPTURES_DIR="/var/lib/lastsietch-rmq-bridge/captures-admin/settingsUpdate"
SINCE="${1:-}"   # optional ISO8601 UTC anchor: only consider captures older than this

CAPTURES_DIR="$CAPTURES_DIR" SINCE="$SINCE" python3 - <<'PY'
import difflib
import json
import os
import sys
from pathlib import Path


def err(msg, code=2):
    json.dump({"status": "error", "exit_code": code, "error": msg}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(code)


root = Path(os.environ["CAPTURES_DIR"])
since = os.environ.get("SINCE") or None

if not root.is_dir():
    err(f"captures dir missing: {root}")

# Each capture is <UTC>/<seq>.json — sort by full path which puts them in
# chronological order (UTC stamps sort lexicographically).
all_files = sorted(p for p in root.glob("*/*.json") if p.is_file())
if len(all_files) < 2:
    err(f"need at least 2 capture files; have {len(all_files)}")

newer = all_files[-1]
older = all_files[-2]
if since:
    # since-filter: newer = most-recent; older = most-recent file whose parent
    # dir name (the UTC stamp) sorts strictly before SINCE.
    older = None
    for p in all_files:
        if p.parent.name < since:
            older = p
    if older is None:
        err(f"no capture older than --since {since}")

try:
    older_data = json.loads(older.read_text(encoding="utf-8"))
    newer_data = json.loads(newer.read_text(encoding="utf-8"))
except Exception as exc:
    err(f"failed to parse capture JSON: {exc}")

# The capture files are full RMQ frames. The substantive Funcom payload is
# under `body_json` — everything else (body_len, delivery_tag, exchange,
# properties, redelivered, routing_key, seq, ts_utc) is frame metadata that
# churns on every capture. Drop the frame metadata so the diff actually
# reflects Funcom-side configuration changes.
older_payload = older_data.get("body_json", older_data)
newer_payload = newer_data.get("body_json", newer_data)

older_text = json.dumps(older_payload, sort_keys=True, indent=2)
newer_text = json.dumps(newer_payload, sort_keys=True, indent=2)

diff_text = "".join(difflib.unified_diff(
    older_text.splitlines(keepends=True),
    newer_text.splitlines(keepends=True),
    fromfile=str(older),
    tofile=str(newer),
))
exit_code = 0 if diff_text == "" else 1

changed_keys = sorted({
    k for k in set(older_payload) | set(newer_payload)
    if older_payload.get(k) != newer_payload.get(k)
})

out = {
    "status": "ok",
    "exit_code": exit_code,
    "compared": {
        "older": {"path": str(older), "captured_at": older.parent.name},
        "newer": {"path": str(newer), "captured_at": newer.parent.name},
    },
    "diff_text": diff_text,
    "changed_keys": changed_keys,
}
json.dump(out, sys.stdout)
sys.stdout.write("\n")
sys.exit(exit_code)
PY
