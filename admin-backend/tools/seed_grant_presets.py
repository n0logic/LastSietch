#!/usr/bin/env python3
"""VC3 — Seed holadmin.grant_presets from data/grant-presets.toml.

One-shot. Reads the TOML, generates an INSERT … ON CONFLICT (name) DO UPDATE
batch, prints the SQL to stdout. Pipe into psql, or save to a file and run
via the standard kubectl cp + exec dune pg flow.

Usage:
    python3 admin-backend/tools/seed_grant_presets.py > /tmp/seed.sql
    # then kubectl cp + exec psql -f /tmp/seed.sql

Reversibility flag is preserved per-preset. operator_role defaults to 'admin'.
created_by is hardcoded to 'system-seed' for the initial 10.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


HERE = Path(__file__).resolve().parent
TOML_PATH = HERE.parent / "data" / "grant-presets.toml"


def main() -> int:
    if not TOML_PATH.is_file():
        print(f"missing TOML: {TOML_PATH}", file=sys.stderr)
        return 2

    with TOML_PATH.open("rb") as fh:
        data = tomllib.load(fh)

    presets = data.get("preset", [])
    if not presets:
        print("no presets in TOML", file=sys.stderr)
        return 2

    print("BEGIN;")
    # VC3 v1.1: clear all existing rows so the seed is authoritative.
    # ON CONFLICT alone leaves orphaned presets from previous seeds.
    print("DELETE FROM holadmin.grant_presets;")
    for p in presets:
        name = p["name"]
        display = p["display"]
        description = p.get("description", "")
        reversible = "true" if p.get("reversible", False) else "false"
        operator_role = p.get("operator_role", "admin")
        ops_json = json.dumps(p.get("ops", []))
        parameters_json = json.dumps(p.get("parameters", []))

        ops_lit = ops_json.replace("'", "''")
        params_lit = parameters_json.replace("'", "''")
        desc_lit = description.replace("'", "''")
        display_lit = display.replace("'", "''")

        print(
            "INSERT INTO holadmin.grant_presets "
            "(name, display, description, ops_json, parameters, reversible, operator_role, created_by) "
            f"VALUES ('{name}', '{display_lit}', '{desc_lit}', "
            f"'{ops_lit}'::jsonb, '{params_lit}'::jsonb, "
            f"{reversible}, '{operator_role}', 'system-seed');"
        )
    print("COMMIT;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
