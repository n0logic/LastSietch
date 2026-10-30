#!/usr/bin/env python3
# Read-only Dune self-host status for the Last Sietch relay. Performs no writes.
# Deployed to lastsietch-dune:/root/dune-status.py, invoked by the relay over SSH.
import json
import subprocess
import urllib.request

STATUS_SQL = """
SELECT json_build_object(
  'maps', (SELECT coalesce(json_agg(row_to_json(m)),'[]'::json) FROM (
     SELECT map,
            -- The same stale-row trap documented below applies to the AGGREGATES:
            -- unfiltered count(*)/sum() read the ghost rows too. A dead instance
            -- carrying connected_players=1 put a phantom occupant on the public
            -- /portal/server page (found 2026-08-27, 84 farm_state rows vs 30 live).
            count(*) FILTER (WHERE ready AND alive) pods,
            coalesce(sum(connected_players) FILTER (WHERE ready AND alive),0) players,
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
  -- Per-DIMENSION player counts. The 'maps' aggregate above GROUPs BY map only,
  -- which collapses every dimension of a multi-world map into one number: DD
  -- dim0+dim1 both reported the map total, and Hagga reported dim0+dim1+dim2 on
  -- all three instance cards (found 2026-08-29 -- portal showed "2 here" on both
  -- the DD PvE and PvP cards while only dim0 held those 2 players). farm_state is
  -- per-SERVER and world_partition maps server_id -> dimension_index, so join
  -- them to recover the split. Additive: 'maps' is unchanged for its consumers.
  'map_dims', (SELECT coalesce(json_agg(row_to_json(d)),'[]'::json) FROM (
     SELECT w.map, w.dimension_index,
            coalesce(sum(CASE WHEN f.ready AND f.alive
                              THEN f.connected_players ELSE 0 END), 0) players
     FROM dune.world_partition w
     LEFT JOIN dune.farm_state f ON f.server_id = w.server_id
     GROUP BY w.map, w.dimension_index
     ORDER BY w.map, w.dimension_index) d),
  'online_players', (SELECT count(*) FROM dune.player_state
                     WHERE online_status='Online')
);
"""


# --- BGD queue sampler -------------------------------------------------------
# Queue depth is NOT a documented field of the BGD payload: numPlayersInQueue
# appears nowhere in this repo and nowhere in the community dump, so both its
# spelling and its nesting are UNVERIFIED. Probe for it rather than assert it --
# every known spelling, on every dict in the payload -- and report null when
# nothing matches. Null means "we did not observe a queue"; zero would be a claim
# that the queue was empty, which is a different and unearned statement.
#
# Never key a total by MAP: Hagga and Deep Desert share a map name across
# dimensions, so a per-map sum double-counts. The dimension split lives in
# 'map_dims', which is sourced from world_partition.dimension_index.
QUEUE_KEYS = ("numPlayersInQueue", "playersInQueue", "queue")
# Walk bound. A payload deep enough to hit this is not one we understand, and the
# partial result is reported as unknown rather than summed (see queue_snapshot).
VISIT_CAP = 5000
SERVER_ID_KEYS = ("serverId", "server_id", "serverName", "instanceName", "name", "id")


def queue_value(node):
    """The queue depth a dict declares, as a non-negative int, or None. A list is
    read as its length (a queue rendered as its members)."""
    if not isinstance(node, dict):
        return None
    for key in QUEUE_KEYS:
        if key not in node:
            continue
        val = node[key]
        if isinstance(val, bool):
            continue
        if isinstance(val, int) and val >= 0:
            return val
        if isinstance(val, list):
            return len(val)
    return None


def server_label(node, parent_key):
    for key in SERVER_ID_KEYS:
        val = node.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, int):
            return str(val)
    return parent_key if isinstance(parent_key, str) else None


def queue_snapshot(payload):
    """{'total': int|None, 'servers': [{'server': str|None, 'queued': int}]} read
    out of a BGD battlegroup payload. Tolerates any shape, including one with no
    queue field at all, and never raises."""
    if not isinstance(payload, dict):
        return {"total": None, "servers": [], "capped": False}

    servers = []
    declared = queue_value(payload)
    stack = list(payload.items())
    visited = 0
    while stack and visited < VISIT_CAP:
        parent_key, node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            queued = queue_value(node)
            if queued is None:
                stack.extend(node.items())
            else:
                servers.append({"server": server_label(node, parent_key),
                                "queued": queued})
        elif isinstance(node, list):
            stack.extend((parent_key, item) for item in node)

    # Hitting the cap means the walk stopped early, so anything summed from it is
    # a partial count of an unknown fraction of the payload. A confident undercount
    # is worse than no number: it reads as "3 waiting" when 30 are. The payload's
    # OWN declared total is still trustworthy (it was read before the walk).
    capped = bool(stack)
    if declared is not None:
        total = declared
    elif capped:
        total = None
    elif servers:
        total = sum(s["queued"] for s in servers)
    else:
        total = None
    return {"total": total, "servers": servers, "capped": capped}


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
            "queue": queue_snapshot(full),
        }
    except Exception as e:  # noqa: BLE001 - BG is best-effort
        bg = {"error": str(e)}
    data["battlegroup"] = bg
    print(json.dumps(data))


if __name__ == "__main__":
    main()
