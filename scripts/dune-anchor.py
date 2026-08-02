#!/usr/bin/env python3
"""Calibration anchor capture: read a player's live position and convert it to a
9x9 Deep Desert sector, alongside the current spice liveness. Used to tie a
human's confirmed on-field position to a sector (and, by timing, to the active
WL cell) so we can build an authoritative WL->sector catalog.

Usage: dune-anchor.py <account_id>
Read-only. Deployed to lastsietch-dune:/root/dune-anchor.py.
"""
import json
import subprocess
import sys

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DBPOD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"
MAP_MIN, MAP_MAX = -1270000.0, 1168400.0
SECTOR = (MAP_MAX - MAP_MIN) / 9.0


def _psql(sql: str) -> str:
    pw = subprocess.run(["kubectl", "exec", "-n", NS, DBPOD, "--",
                         "printenv", "POSTGRES_PASSWORD"],
                        capture_output=True, text=True, timeout=20).stdout.strip()
    out = subprocess.run(["kubectl", "exec", "-n", NS, DBPOD, "--", "env",
                          f"PGPASSWORD={pw}", "psql", "-h", "localhost", "-p",
                          "15432", "-U", "postgres", "-d", "dune", "-tAc", sql],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:300])
    return out.stdout.strip()


def sector_for(x, y):
    col = max(1, min(9, int((x - MAP_MIN) / SECTOR) + 1))
    row = max(0, min(8, int((MAP_MAX - y) / SECTOR)))     # 0=A(south/+y)..8=I(north/-y)
    return f"{chr(ord('A') + row)}{col}"


def main():
    acct = int(sys.argv[1])
    pos = _psql(
        "SELECT json_agg(json_build_object('map',map,'dim',dimension_index,"
        "'class',class,"
        "'x',split_part(split_part(transform::text,'(\"(',2),',',1)::float8,"
        "'y',split_part(split_part(transform::text,'(\"(',2),',',2)::float8)) "
        f"FROM dune.actors WHERE owner_account_id={acct} AND transform IS NOT NULL "
        "AND class LIKE '%DunePlayerCharacter%';")
    rows = json.loads(pos) or []
    # one position (character pawn); fall back to any DunePlayer row
    if not rows:
        pos = _psql(
            "SELECT json_agg(json_build_object('map',map,'dim',dimension_index,"
            "'class',class,"
            "'x',split_part(split_part(transform::text,'(\"(',2),',',1)::float8,"
            "'y',split_part(split_part(transform::text,'(\"(',2),',',2)::float8)) "
            f"FROM dune.actors WHERE owner_account_id={acct} AND transform IS NOT NULL "
            "AND class LIKE '%DunePlayer%';")
        rows = json.loads(pos) or []

    live = _psql(
        "SELECT json_agg(json_build_object('dim',dimension_index,'type',field_type,"
        "'active',current_globally_active)) FROM dune.spicefield_types "
        "WHERE map_name='DeepDesert';")

    out = {"account_id": acct, "positions": [], "spice_liveness": json.loads(live) or []}
    for r in rows:
        x, y = r.get("x"), r.get("y")
        sec = sector_for(float(x), float(y)) if (x is not None and y is not None
                                                 and r["map"] == "DeepDesert") else None
        out["positions"].append({"map": r["map"], "dim": r["dim"],
                                 "x": round(float(x), 1) if x is not None else None,
                                 "y": round(float(y), 1) if y is not None else None,
                                 "sector": sec})
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:300]}))
        sys.exit(1)
