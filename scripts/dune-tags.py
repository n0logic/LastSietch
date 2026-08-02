#!/usr/bin/env python3
# Read-only Dune player-tag dump for the Last Sietch relay. Calls
# dune.admin_read_player_tags(account_id) and emits a JSON document.
# Deployed to lastsietch-dune:/root/dune-tags.py — invoked by the relay over SSH
# via the dispatcher's `tags-read <account_id>` token.

import json
import subprocess
import sys

SQL_TEMPLATE = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(tags ORDER BY tags), '[]'::json)
  FROM dune.admin_read_player_tags({account_id}::bigint);
"""


def _query_json(sql, fallback="null"):
    """Run psql via the dq.sh wrapper, strip the SET header, return parsed JSON."""
    out = subprocess.run(
        ["/root/dq.sh", "-tAc", sql],
        capture_output=True, text=True, timeout=30, check=False)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:500])
    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    if not raw:
        raw = fallback
    return json.loads(raw)


def build(account_id, query_json):
    """Assemble the player-tags payload. `account_id` is a digit string.
    `query_json(sql, fallback)` runs the SQL and returns parsed JSON (CLI passes
    the dq.sh runner; the collector passes a psycopg2 runner). account_id is
    emitted as the string it came in as (matches the existing relay payload)."""
    tags = query_json(SQL_TEMPLATE.format(account_id=account_id), "[]")
    return {"available": True, "account_id": account_id,
            "count": len(tags), "tags": tags}


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(json.dumps({"available": False, "error": "usage: dune-tags.py <account_id>"}))
        sys.exit(2)

    account_id = sys.argv[1]
    try:
        result = build(account_id, _query_json)
    except Exception as exc:  # noqa: BLE001 - surface any DB error as JSON
        print(json.dumps({"available": False, "error": str(exc)[:500]}))
        sys.exit(1)

    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
