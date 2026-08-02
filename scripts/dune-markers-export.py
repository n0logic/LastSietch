#!/usr/bin/env python3
"""Scoped, read-only export of the static map-marker layer from the live DB.

Pulls dune.markers for the shared static layer (dimension_index = -1) of the Deep
Desert (map_name_id 7) and Hagga Basin (map_name_id 11), emitting one compact JSON
row per marker: marker_type + x + y + payload_type + the human DisplayName +
StaticLocationTags. The DisplayName/tags come straight from the marker `payload`
jsonb, so the station names (e.g. "Imperial Testing Station 2") are captured in ONE
box trip -- the hazard-variant tag is on the same row for the follow-up.

Read-only by construction: a single SELECT via the established kubectl-exec-psql
path (same NS/pod/creds discovery as dune-spice-active.py). No writes, no DDL, no
service touch. Run on the game box (<game-host>), or via `ssh <game-host>` from a trusted
host. Output feeds scripts/build-markers-snapshot.py.

  sudo python3 dune-markers-export.py            # -> stdout (JSON array)
  sudo python3 dune-markers-export.py -o out.json
"""
import json
import subprocess
import sys

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DBPOD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"

# The static-layer maps to export. dimension_index = -1 is the shared static layer
# (same terrain underlies a map's PvE/PvP instances).
MAP_NAME_IDS = (7, 11)            # 7 = Deep Desert, 11 = Hagga Basin


def _db_creds() -> tuple[str, str]:
    """(password, port) read once from the db pod env -- the service port differs
    by box, so read it rather than hardcode (mirrors dune-spice-active.py)."""
    env = subprocess.run(
        ["kubectl", "exec", "-n", NS, DBPOD, "--", "printenv"],
        capture_output=True, text=True, timeout=20).stdout
    pw, port = "", "15432"
    for line in env.splitlines():
        k, _, v = line.partition("=")
        if k == "POSTGRES_PASSWORD":
            pw = v.strip()
        elif k.endswith("_DB_DBDEPL_SVC_SERVICE_PORT") and v.strip().isdigit():
            port = v.strip()
    return pw, port


def _psql(sql: str) -> str:
    pgpass, pgport = _db_creds()
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, DBPOD, "--",
         "env", f"PGPASSWORD={pgpass}", "psql", "-h", "localhost", "-p", pgport,
         "-U", "postgres", "-d", "dune", "-tAc", sql],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "psql failed").strip()[:300])
    return out.stdout.strip()


def export() -> list:
    maps = ",".join(str(m) for m in MAP_NAME_IDS)
    sql = (
        "SELECT json_agg(json_build_object("
        "'map', map_name_id, "
        "'t', (marker).marker_type, "
        "'x', round((marker).x::numeric), "
        "'y', round((marker).y::numeric), "
        "'pt', (marker).payload_type, "
        "'dn', payload->>'DisplayName', "
        "'tags', payload->>'StaticLocationTags')) "
        f"FROM dune.markers WHERE map_name_id IN ({maps}) AND dimension_index = -1;")
    return json.loads(_psql(sql) or "[]")


def main(argv) -> int:
    out_path = None
    for i, a in enumerate(argv):
        if a in ("-o", "--out") and i + 1 < len(argv):
            out_path = argv[i + 1]
    rows = export()
    text = json.dumps(rows, separators=(",", ":"))
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {len(rows)} marker rows -> {out_path}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:300]}))
        sys.exit(1)
