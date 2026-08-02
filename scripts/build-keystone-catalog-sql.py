#!/usr/bin/env python3
# Generates the dune.ls_keystone_catalog backfill migration from the
# PAK-derived keystone map in community-repos/dune-admin/cmd/dune-admin/keystones.go.
#
# sp_bonus derivation (icehunter keystoneSPBonus parity, already canonical in
# dune-grant-schema.sql): name suffix _SkillPoint_Super=+5, _SkillPoint_Major=+3,
# _SkillPoint=+1, otherwise 0. The Super tier is essential: without it the
# Combat track sums to 49, not the live-correct 54.
#
# Emits an idempotent migration that ALTERs the catalog to add req_level +
# spice_cost (OWNER dune) and UPSERTs all 205 rows. Run:
#   python3 scripts/build-keystone-catalog-sql.py > scripts/dune-keystone-catalog-backfill-2026-05-29.sql

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYSTONES_GO = ROOT / "community-repos/dune-admin/cmd/dune-admin/keystones.go"
EXISTING_SEED = ROOT / "scripts/dune-grant-schema.sql"
SIDECAR = ROOT / "admin-backend/data/keystone-catalog.json"

ROW_RE = re.compile(
    r'(\d+):\s*\{Track:\s*"([^"]+)",\s*Name:\s*"([^"]+)",\s*Level:\s*(-?\d+),\s*Cost:\s*(-?\d+)\}'
)


def derive_sp_bonus(name):
    if name.endswith("_SkillPoint_Super"):
        return 5
    if name.endswith("_SkillPoint_Major"):
        return 3
    if name.endswith("_SkillPoint"):
        return 1
    return 0


def parse_keystones():
    text = KEYSTONES_GO.read_text()
    rows = {}
    for m in ROW_RE.finditer(text):
        kid = int(m.group(1))
        rows[kid] = {
            "track": m.group(2),
            "name": m.group(3),
            "req_level": int(m.group(4)),
            "spice_cost": int(m.group(5)),
            "sp_bonus": derive_sp_bonus(m.group(3)),
        }
    return rows


def parse_existing_seed():
    # Existing 4-column INSERT rows: (id,'Track','Name',sp_bonus)
    text = EXISTING_SEED.read_text()
    seed = {}
    for m in re.finditer(r"\((\d+),'([^']+)','([^']+)',(\d+)\)", text):
        seed[int(m.group(1))] = {
            "track": m.group(2),
            "name": m.group(3),
            "sp_bonus": int(m.group(4)),
        }
    return seed


def validate(rows, seed):
    errors = []
    # Gate 1: exactly 205 ids, contiguous 1..205.
    if sorted(rows) != list(range(1, 206)):
        errors.append(f"expected ids 1..205, got {len(rows)} ids")
    # Gate 2: Combat sp_bonus sum must reproduce the live-correct 54.
    combat_sum = sum(r["sp_bonus"] for k, r in rows.items() if r["track"] == "Combat")
    if combat_sum != 54:
        errors.append(f"Combat sp_bonus sum = {combat_sum}, expected 54 (GATE FAIL)")
    # Gate 3: every existing Combat seed row must be reproduced exactly.
    for kid, s in seed.items():
        if s["track"] != "Combat":
            continue
        r = rows.get(kid)
        if r is None:
            errors.append(f"id {kid}: missing from generated rows")
            continue
        if r["sp_bonus"] != s["sp_bonus"]:
            errors.append(
                f"id {kid}: sp_bonus {r['sp_bonus']} != existing seed {s['sp_bonus']}"
            )
        if r["name"] != s["name"]:
            errors.append(
                f"id {kid}: name '{r['name']}' != existing seed '{s['name']}'"
            )
    return errors, combat_sum


def name_diffs(rows, seed):
    diffs = []
    for kid in sorted(rows):
        r = rows[kid]
        s = seed.get(kid)
        if s and (r["name"] != s["name"] or r["sp_bonus"] != s["sp_bonus"]):
            diffs.append(
                f"  id {kid} ({r['track']}): "
                f"name '{s['name']}'->'{r['name']}' "
                f"sp {s['sp_bonus']}->{r['sp_bonus']}"
            )
    return diffs


def emit_sql(rows):
    lines = []
    lines.append("-- dune.ls_keystone_catalog backfill: req_level + spice_cost columns")
    lines.append("-- and all 205 rows (5 tracks) with sp_bonus derived per the icehunter")
    lines.append("-- keystoneSPBonus rule (_SkillPoint_Super=+5, _SkillPoint_Major=+3,")
    lines.append("-- _SkillPoint=+1, else 0). Generated from keystones.go by")
    lines.append("-- scripts/build-keystone-catalog-sql.py. Do not hand-edit.")
    lines.append("--")
    lines.append("-- Idempotent + replay-safe. OWNER dune (custom ls_* table ownership rule:")
    lines.append("-- a table owned by postgres aborts Funcom's pre-update pg_dump).")
    lines.append("--")
    lines.append("-- Apply read/write via the dq.sh equivalent, e.g.:")
    lines.append("--   sudo kubectl exec -i -n <ns> <db-pod> -- \\")
    lines.append("--     env PGPASSWORD=<pw> psql -h localhost -p 15432 -U postgres -d dune \\")
    lines.append("--     -v ON_ERROR_STOP=1 -f dune-keystone-catalog-backfill-2026-05-29.sql")
    lines.append("-- Verify (read-only): \\d dune.ls_keystone_catalog and the validation")
    lines.append("-- SELECTs at the foot of this file.")
    lines.append("")
    lines.append("BEGIN;")
    lines.append("")
    lines.append("ALTER TABLE dune.ls_keystone_catalog")
    lines.append("  ADD COLUMN IF NOT EXISTS req_level  smallint,")
    lines.append("  ADD COLUMN IF NOT EXISTS spice_cost integer;")
    lines.append("")
    lines.append("ALTER TABLE dune.ls_keystone_catalog OWNER TO dune;")
    lines.append("")
    lines.append(
        "INSERT INTO dune.ls_keystone_catalog "
        "(keystone_id, track, keystone_name, sp_bonus, req_level, spice_cost) VALUES"
    )
    vals = []
    for kid in sorted(rows):
        r = rows[kid]
        vals.append(
            f"  ({kid},'{r['track']}','{r['name']}',{r['sp_bonus']},"
            f"{r['req_level']},{r['spice_cost']})"
        )
    lines.append(",\n".join(vals))
    lines.append("ON CONFLICT (keystone_id) DO UPDATE SET")
    lines.append("  track         = EXCLUDED.track,")
    lines.append("  keystone_name = EXCLUDED.keystone_name,")
    lines.append("  sp_bonus      = EXCLUDED.sp_bonus,")
    lines.append("  req_level     = EXCLUDED.req_level,")
    lines.append("  spice_cost    = EXCLUDED.spice_cost;")
    lines.append("")
    lines.append("-- Validation gates (must all pass before COMMIT is trusted):")
    lines.append("--   total rows = 205")
    lines.append("--   Combat sp_bonus sum = 54")
    lines.append("--   no NULL req_level / spice_cost")
    lines.append("DO $$")
    lines.append("DECLARE v_total int; v_combat int; v_nulls int;")
    lines.append("BEGIN")
    lines.append("  SELECT count(*) INTO v_total FROM dune.ls_keystone_catalog;")
    lines.append("  SELECT COALESCE(SUM(sp_bonus),0) INTO v_combat")
    lines.append("    FROM dune.ls_keystone_catalog WHERE track = 'Combat';")
    lines.append("  SELECT count(*) INTO v_nulls FROM dune.ls_keystone_catalog")
    lines.append("    WHERE req_level IS NULL OR spice_cost IS NULL;")
    lines.append("  IF v_total <> 205 THEN")
    lines.append("    RAISE EXCEPTION 'keystone catalog row count = %, expected 205', v_total;")
    lines.append("  END IF;")
    lines.append("  IF v_combat <> 54 THEN")
    lines.append("    RAISE EXCEPTION 'Combat sp_bonus sum = %, expected 54', v_combat;")
    lines.append("  END IF;")
    lines.append("  IF v_nulls <> 0 THEN")
    lines.append("    RAISE EXCEPTION '% rows have NULL req_level/spice_cost', v_nulls;")
    lines.append("  END IF;")
    lines.append("END $$;")
    lines.append("")
    lines.append("COMMIT;")
    lines.append("")
    return "\n".join(lines)


def update_sidecar(rows):
    # Merge req_level + spice_cost into the existing keystone-catalog.json
    # served by /grant/keystones. Preserves friendly_name / node_index /
    # node_total and re-asserts sp_bonus is unchanged (regression check).
    data = json.loads(SIDECAR.read_text())
    entries = data.get("keystones", [])
    if len(entries) != 205:
        sys.stderr.write(f"SIDECAR FAIL: expected 205 entries, got {len(entries)}\n")
        sys.exit(1)
    for e in entries:
        kid = e.get("keystone_id")
        r = rows.get(kid)
        if r is None:
            sys.stderr.write(f"SIDECAR FAIL: keystone_id {kid} not in keystones.go\n")
            sys.exit(1)
        if e.get("sp_bonus") != r["sp_bonus"]:
            sys.stderr.write(
                f"SIDECAR FAIL: id {kid} sp_bonus {e.get('sp_bonus')} "
                f"!= derived {r['sp_bonus']}\n"
            )
            sys.exit(1)
        e["req_level"] = r["req_level"]
        e["spice_cost"] = r["spice_cost"]
    SIDECAR.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    sys.stderr.write(
        f"sidecar updated: {SIDECAR} (req_level + spice_cost on all 205 entries, "
        f"sp_bonus unchanged)\n"
    )


def main():
    ap = argparse.ArgumentParser(
        description="Build the keystone catalog backfill SQL and/or update the sidecar.")
    ap.add_argument("--sidecar", action="store_true",
                    help="update admin-backend/data/keystone-catalog.json in place "
                         "(adds req_level + spice_cost); does NOT emit SQL")
    args = ap.parse_args()

    rows = parse_keystones()
    seed = parse_existing_seed()
    errors, combat_sum = validate(rows, seed)
    if errors:
        sys.stderr.write("VALIDATION FAILED:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(1)
    diffs = name_diffs(rows, seed)
    sys.stderr.write(f"OK: 205 rows, Combat sp_bonus sum = {combat_sum}\n")
    sys.stderr.write(f"Combat sp_bonus rows reproduce existing seed exactly.\n")
    if diffs:
        sys.stderr.write(
            f"NOTE: {len(diffs)} non-Combat name/sp_bonus diffs vs existing seed "
            f"(keystones.go is newer PAK source; grant uses keystone_id only):\n"
        )
        for d in diffs:
            sys.stderr.write(d + "\n")
    else:
        sys.stderr.write("No name/sp_bonus diffs vs existing seed across all 205.\n")

    if args.sidecar:
        update_sidecar(rows)
    else:
        sys.stdout.write(emit_sql(rows))


if __name__ == "__main__":
    main()
