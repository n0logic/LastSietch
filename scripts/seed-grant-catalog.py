#!/usr/bin/env python3
# Read-only seed enumerator for the Dune progression-grant catalog. NO WRITES.
#
# Runs from a workstation. Every DB call goes through `ssh lastsietch-dune
# '/root/dq.sh -tAc "SELECT ..."'` (the existing read-only query helper).
# It only ever issues SELECT statements and never restarts anything.
#
# It emits a DRAFT catalog JSON in the structure documented in
# docs/DUNE-PROGRESSION-GRANT-TOOL-PLAN.md section 4. Every enumerated entry is
# stamped confidence "NEEDS-VERIFICATION" with `label` defaulted to the raw id.
# A human enriches the draft into admin-backend/data/dune-grant-catalog.json
# (task P0-2).
#
# Usage:
#   scripts/seed-grant-catalog.py            # draft JSON to stdout
#   scripts/seed-grant-catalog.py -o FILE    # draft JSON written to FILE
import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone

GAME_BUILD = "4754530"
CATALOG_VERSION = "1.0.0-draft"

# P3a — icehunter v0.5.x parity grant_types. The admin-backend catalog
# (admin-backend/data/dune-grant-catalog.json, v1.9.0 -> v1.10.0) is the
# authoritative consumer; these entries seed the workstation draft so the
# enumerator output is in sync with the new grant ids/inputs (input shapes
# §G23-G25b + §Faction).
#
# set_starter_class is intentionally included here for completeness — the
# admin UI keeps it disabled (spec §G25b lines 1006-1029), but the backend
# catalog must still know the input shape for the SQL-recovery back door.
ICEHUNTER_PARITY_GRANT_TYPES = [
    {
        "grant_type": "main_quest_unlock",
        "label": "Main Quest Unlock (G23)",
        "offline_required": True,
        "high_value": False,
        "detail_schema": {
            "preset": {
                "type": "enum",
                "values": [
                    "DA_MQ_ANewBeginning",
                    "DA_MQ_AssassinsHandbook",
                    "DA_MQ_FindTheFremen",
                    "DA_MQ_TheGreatConvention",
                    "DA_MQ_TheGreatConventionPt2",
                    "DA_MQ_TheBloodline",
                ],
            },
        },
        "source": "ICEHUNTER-V05X-PARITY-BUILD-SPEC §G23",
    },
    {
        "grant_type": "grant_full_job_tree",
        "label": "Grant Full Job Tree (G24a)",
        "offline_required": True,
        "high_value": True,
        "detail_schema": {
            "job": {
                "type": "enum",
                "values": ["BeneGesserit", "Mentat", "Planetologist",
                           "Swordmaster", "Trooper"],
            },
        },
        "source": "ICEHUNTER-V05X-PARITY-BUILD-SPEC §G24a",
    },
    {
        "grant_type": "grant_skill_block",
        "label": "Grant Skill Block (G24b)",
        "offline_required": True,
        "high_value": False,
        "detail_schema": {
            # 30 valid values total = union of job_skill_blocks across 5 jobs
            # in tags-data.json. The admin UI enumerates lazily from the
            # sidecar; the backend re-validates against tags-data.json on
            # every grant.
            "block": {
                "type": "string",
                "pattern": r"^Skills\.Key\.[A-Za-z0-9_]+$",
                "lookup": "tags-data.json:job_skill_blocks (union)",
            },
        },
        "source": "ICEHUNTER-V05X-PARITY-BUILD-SPEC §G24b",
    },
    {
        "grant_type": "reset_full_skill_area",
        "label": "Reset Full Skill Area (G25a)",
        "offline_required": True,
        "high_value": True,
        "hazard": "Refuses to reset the player's CURRENT starter class — "
                  "operator must reassign first.",
        "detail_schema": {
            "job": {
                "type": "enum",
                "values": ["BeneGesserit", "Mentat", "Planetologist",
                           "Swordmaster", "Trooper"],
            },
        },
        "source": "ICEHUNTER-V05X-PARITY-BUILD-SPEC §G25a",
    },
    {
        "grant_type": "set_starter_class",
        "label": "Set Starter Class (G25b — UI DISABLED)",
        "offline_required": True,
        "ui_disabled": True,
        "high_value": True,
        "hazard": "Observed actor-31 full-wipe on next login. Admin UI route "
                  "stamps via_disabled_ui in detail; SQL-level recovery only "
                  "until empirical alt validation lands.",
        "detail_schema": {
            "job": {
                "type": "enum",
                "values": ["BeneGesserit", "Mentat", "Planetologist",
                           "Swordmaster", "Trooper"],
            },
        },
        "source": "ICEHUNTER-V05X-PARITY-BUILD-SPEC §G25b",
    },
    {
        "grant_type": "align_faction",
        "label": "Align Faction (g7b — alignment-only)",
        "offline_required": False,
        "high_value": False,
        "detail_schema": {
            "faction": {
                "type": "enum",
                "values": ["atreides", "harkonnen"],
            },
        },
        "source": "ICEHUNTER-V05X-PARITY-BUILD-SPEC §Faction (g7b)",
    },
]

# ItemKey families decoded from the live m_TechKnowledgeData arrays. Anything
# else falls through to "other".
RECIPE_PREFIXES = ("RCP_", "BLD_", "DA_")

# Item template families that are corpses / loot / quest artifacts. They are
# excluded from the draft `item` list (they are not grantable picker entries).
ITEM_EXCLUDE_SUBSTRINGS = ("ContractItem", "Corpse", "Bloodsack_", "Component")


def dq(sql):
    """Run one read-only SELECT via the lastsietch-dune dq.sh helper. SELECT only."""
    stripped = sql.lstrip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        raise ValueError("seed-grant-catalog issues SELECT statements only")
    # ssh space-joins argv and hands the result to the remote shell, so the SQL
    # must be quoted for that remote shell as a single token.
    remote = "/root/dq.sh -tAc " + shlex.quote(sql)
    out = subprocess.run(
        ["ssh", "lastsietch-dune", remote],
        capture_output=True, text=True, timeout=90,
    )
    if out.returncode != 0:
        sys.stderr.write((out.stderr or out.stdout).strip()[:500] + "\n")
        raise SystemExit(f"db query failed (exit {out.returncode})")
    return out.stdout.strip()


def dq_json(sql, default):
    raw = dq(sql)
    if not raw:
        return default
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("db returned non-JSON: " + raw[:300] + "\n")
        raise SystemExit("db returned non-JSON")
    return val if val is not None else default


def enumerate_items():
    """Distinct dune.items.template_id with row counts. SELECT only."""
    rows = dq_json(
        "SELECT coalesce(json_agg(json_build_object("
        "'template_id', template_id, 'rows', n) ORDER BY template_id), "
        "'[]'::json) FROM ("
        "  SELECT template_id, count(*) AS n FROM dune.items GROUP BY 1) s",
        [],
    )
    items = []
    excluded = 0
    for r in rows:
        tid = r["template_id"]
        if any(sub in tid for sub in ITEM_EXCLUDE_SUBSTRINGS):
            excluded += 1
            continue
        items.append({
            "template_id": tid,
            "label": tid,
            "family": "other",
            "rows": r["rows"],
            "confidence": "NEEDS-VERIFICATION",
            "source": "dune.items enumeration",
        })
    return items, excluded


def enumerate_item_keys():
    """Distinct ItemKeys from m_TechKnowledgeData. SELECT only."""
    keys = dq_json(
        "SELECT coalesce(json_agg(DISTINCT elem->>'ItemKey'), '[]'::json) "
        "FROM dune.actors a, jsonb_array_elements("
        "a.properties#>'{TechKnowledgePlayerComponent,m_TechKnowledge,"
        "m_TechKnowledgeData}') elem "
        "WHERE elem->>'ItemKey' IS NOT NULL",
        [],
    )
    return sorted(k for k in keys if k)


def enumerate_factions():
    """dune.factions rows. The live source of truth for G7. SELECT only."""
    rows = dq_json(
        "SELECT coalesce(json_agg(json_build_object("
        "'faction_id', id, 'name', name) ORDER BY id), '[]'::json) "
        "FROM dune.factions",
        [],
    )
    return [{
        "faction_id": r["faction_id"],
        "label": r["name"],
        "confidence": "NEEDS-VERIFICATION",
        "source": "dune.factions",
    } for r in rows]


def enumerate_track_types():
    """Distinct specialization_tracks.track_type values. SELECT only.

    track_type is a Postgres enum; rows may be absent if no player has
    specialization XP yet, so the enum definition is read as the fallback.
    """
    rows = dq_json(
        "SELECT coalesce(json_agg(DISTINCT track_type::text), '[]'::json) "
        "FROM dune.specialization_tracks",
        [],
    )
    if not rows:
        rows = dq_json(
            "SELECT coalesce(json_agg(e.enumlabel ORDER BY e.enumsortorder), "
            "'[]'::json) FROM pg_type t JOIN pg_enum e ON e.enumtypid=t.oid "
            "WHERE t.typname=(SELECT udt_name FROM information_schema.columns "
            "WHERE table_schema='dune' AND table_name='specialization_tracks' "
            "AND column_name='track_type')",
            [],
        )
    return [{
        "track_type": t,
        "label": t,
        "confidence": "NEEDS-VERIFICATION",
        "source": "dune.specialization_tracks track_type enum",
    } for t in rows]


def enumerate_journey_roots():
    """Journey root prefixes from dune.journey_story_node. SELECT only."""
    rows = dq_json(
        "SELECT coalesce(json_agg(json_build_object("
        "'root', root, 'nodes', n) ORDER BY n DESC), '[]'::json) FROM ("
        "  SELECT split_part(story_node_id,'_',1)||'_'||"
        "         split_part(story_node_id,'_',2) AS root, count(*) AS n "
        "  FROM dune.journey_story_node GROUP BY 1) s",
        [],
    )
    return [{
        "root": r["root"],
        "label": r["root"],
        "nodes": r["nodes"],
        "confidence": "NEEDS-VERIFICATION",
        "source": "dune.journey_story_node enumeration",
    } for r in rows]


def build_draft():
    items, items_excluded = enumerate_items()
    item_keys = enumerate_item_keys()
    factions = enumerate_factions()
    track_types = enumerate_track_types()
    journey_roots = enumerate_journey_roots()

    recipe = [{
        "item_key": k,
        "label": k,
        "confidence": "NEEDS-VERIFICATION",
        "source": "m_TechKnowledgeData enumeration",
    } for k in item_keys if k.startswith(RECIPE_PREFIXES)]

    schematic_items = [{
        "template_id": it["template_id"],
        "label": it["template_id"],
        "category": "schematic_item",
        "confidence": "NEEDS-VERIFICATION",
        "source": "dune.items enumeration",
    } for it in items if it["template_id"].endswith("_Schematic")]

    catalog = {
        "catalog_version": CATALOG_VERSION,
        "game_build": GAME_BUILD,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "grant_types": list(ICEHUNTER_PARITY_GRANT_TYPES),
        "entries": {
            "item": items,
            "recipe": recipe,
            "schematic_item": schematic_items,
            "faction": factions,
            "specialization_track": track_types,
            "journey_root": journey_roots,
        },
        "quality_tiers": [
            {"value": 0, "label": "Common"},
            {"value": 1, "label": "Uncommon"},
            {"value": 2, "label": "Rare"},
            {"value": 3, "label": "Epic"},
            {"value": 4, "label": "Exotic"},
            {"value": 5, "label": "Legendary"},
            {"value": 6, "label": "Unique"},
        ],
        "_draft_stats": {
            "item_templates": len(items),
            "item_templates_excluded": items_excluded,
            "schematic_items": len(schematic_items),
            "item_keys": len(item_keys),
            "recipe_entries": len(recipe),
            "factions": len(factions),
            "specialization_tracks": len(track_types),
            "journey_roots": len(journey_roots),
        },
    }
    return catalog


def main():
    ap = argparse.ArgumentParser(
        description="Read-only Dune grant-catalog draft enumerator.")
    ap.add_argument("-o", "--output",
                    help="write the draft JSON to this file (default stdout)")
    args = ap.parse_args()

    catalog = build_draft()
    text = json.dumps(catalog, indent=2)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(text + "\n")
        stats = catalog["_draft_stats"]
        sys.stderr.write(
            f"draft written to {args.output}: "
            f"{stats['item_templates']} items "
            f"({stats['item_templates_excluded']} excluded), "
            f"{stats['recipe_entries']} recipes, "
            f"{stats['factions']} factions, "
            f"{stats['specialization_tracks']} tracks, "
            f"{stats['journey_roots']} journey roots\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
