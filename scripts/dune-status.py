#!/usr/bin/env python3
# Read-only Dune self-host status for the Last Sietch relay. Performs no writes.
# Deployed to lastsietch-dune:/root/dune-status.py — invoked by the relay over SSH.
import json
import subprocess
import urllib.request

STATUS_SQL = """
SELECT json_build_object(
  'maps', (SELECT coalesce(json_agg(row_to_json(m)),'[]'::json) FROM (
     SELECT map, count(*) pods, coalesce(sum(connected_players),0) players,
            -- A map is up if it has ANY live+ready server. bool_and broke on the
            -- on-demand hubs (Arrakeen/Harko/content instances): a recycled pod leaves
            -- a stale ready=f/alive=f farm_state row, and bool_and(ready) then reads the
            -- whole map OFFLINE even while a live ready server exists. bool_or of
            -- (ready AND alive) ignores the dead rows.
            bool_or(ready AND alive) ready, bool_or(ready AND alive) alive
     FROM dune.farm_state GROUP BY map ORDER BY map) m),
  'partitions', (SELECT coalesce(json_agg(row_to_json(p)),'[]'::json) FROM (
     SELECT partition_id, server_id, map, label, blocked
     FROM dune.world_partition ORDER BY map) p),
  'online_players', (SELECT count(*) FROM dune.player_state
                     WHERE online_status='Online')
);
"""


def main():
    out = subprocess.run(["/root/dq.sh", "-tAc", STATUS_SQL],
                         capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        print(json.dumps({"error": "db query failed",
                          "detail": (out.stderr or out.stdout).strip()[:300]}))
        return
    try:
        data = json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        print(json.dumps({"error": "db returned non-JSON",
                          "detail": out.stdout.strip()[:300]}))
        return

    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:31282/v0/battlegroup", timeout=10) as r:
            full = json.loads(r.read())
        ssm = full.get("singleServerMaps") or {}
        bg = {
            "bgTitle": full.get("bgTitle"),
            "bgName": full.get("bgName"),
            "bgId": full.get("bgId"),
            "bgRegion": full.get("bgRegion"),
            "hasIGWO": full.get("hasIGWO"),
            "singleServerMaps": sorted(ssm.keys()),
        }
    except Exception as e:  # noqa: BLE001 - BG is best-effort
        bg = {"error": str(e)}
    data["battlegroup"] = bg
    print(json.dumps(data))


if __name__ == "__main__":
    main()
