#!/usr/bin/env python3
"""Build the curated ITEMS overlay sidecar.

Layered name-lookup architecture (LIFT-11 v1.1 follow-up, 2026-05-25):

    1. ITEMS sidecar (pak-derived, dune-item-template-names.json) — wins when pak has _NAME
    2. THIS curated sidecar (manual + cross-referenced)            — wins when pak misses
    3. camelCase synthesis (name_lookups.synthesize_friendly_name)  — last-resort fallback

The synthesis fallback ships incorrect-but-plausible labels for resource
template_ids that have no pak _NAME entry (e.g. `Stone` -> "Stone" when the
canonical in-game name is "Granite Stone"; `Silicone` -> "Silicone" when the
in-game name is "Silicone Block"). This script bootstraps the curated
overlay from icehunter's `dune-admin` repo (~/Tools/dune-third-party/), which
maintains its own player-facing mapping of ~3,500 template_ids.

Filtering:
  - keep only entries NOT already present in the pak ITEMS sidecar (gap-fillers)
  - drop entries whose value is a Funcom dev/placeholder marker (PH_*, XX_*,
    XXNOTUSED_*, "CUT", empty strings)

Idempotent. Re-run when icehunter publishes updated names or after the pak
sidecar regenerates. Output is alphabetized for git-friendly diffs.

Authoritative mode (2026-06-12): --authoritative <join.json> merges the
game-data join (template_id -> ITEMS/<...>_NAME localized string, built from
the DT_BaseItems_* datatables + client string table on <orchestrator-host>'s DunePakRE
tree) INTO the existing curated sidecar. Existing curated entries WIN over
generated ones unless identical — differing values are reported as conflicts
and left untouched. Generated entries are gap-fillers only: ids the pak
sidecar already resolves (with a non-placeholder value) are skipped.

Join file format: {"map": {template_id: {"key": <loc key>, "name": <display>,
"table": <DT filename>}, ...}, "conflicts": [...]}

Usage:
    python3 scripts/build-curated-item-names.py
    python3 scripts/build-curated-item-names.py --dry-run
    python3 scripts/build-curated-item-names.py --source <path/to/icehunter-item-data.json>
    python3 scripts/build-curated-item-names.py --authoritative /tmp/dune-template-authoritative-names.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "admin-backend/data"
PAK_SIDECAR = DATA_DIR / "dune-item-template-names.json"
CURATED_OUT = DATA_DIR / "dune-item-template-names-curated.json"

DEFAULT_SOURCE = Path.home() / "Tools/dune-third-party/dune-admin/item-data.json"

# Funcom in-development markers — never player-facing.
JUNK_VALUE_PREFIXES = ("PH_", "XX_", "XXNOTUSED", "NOTUSED", "NOT_USED", "Placeholder", "TEST_", "DEV_", "WIP_")
JUNK_VALUE_EXACT = {"", "CUT", "XX_CUT"}


def is_junk_value(v: str) -> bool:
    if v.strip() in JUNK_VALUE_EXACT:
        return True
    return any(v.startswith(p) for p in JUNK_VALUE_PREFIXES)


def _pak_resolves(pak: dict, template_id_lower: str) -> bool:
    """True when the pak sidecar already serves a real (non-placeholder)
    name for this id — mirrors name_lookups.lookup(), which treats
    placeholder pak values as a miss that falls through to curated."""
    val = pak.get(template_id_lower)
    if val is None:
        return False
    sys.path.insert(0, str(REPO_ROOT / "admin-backend"))
    from name_lookups import _is_placeholder_name  # noqa: PLC0415
    return not _is_placeholder_name(val)


def merge_authoritative(auth_path: Path, dry_run: bool) -> int:
    """Merge game-data-join names into the existing curated sidecar.

    Layer rules:
      - pak sidecar wins at runtime -> skip ids it already resolves
        (unless the pak value is a placeholder, which lookup() ignores)
      - existing curated entries WIN over generated; conflicts reported
      - junk/placeholder authoritative values are never emitted
    """
    if not auth_path.exists():
        print(f"ERROR: authoritative join not found: {auth_path}", file=sys.stderr)
        return 1
    if not PAK_SIDECAR.exists():
        print(f"ERROR: pak sidecar not found: {PAK_SIDECAR}", file=sys.stderr)
        return 1

    pak = json.loads(PAK_SIDECAR.read_text(encoding="utf-8"))
    auth = json.loads(auth_path.read_text(encoding="utf-8"))["map"]
    existing = {}
    if CURATED_OUT.exists():
        existing = json.loads(CURATED_OUT.read_text(encoding="utf-8"))
    existing_by_lower = {k.lower(): (k, v) for k, v in existing.items()}

    merged = dict(existing)
    added = 0
    identical = 0
    ws_only = 0
    conflicts = []
    skipped_pak = 0
    skipped_junk = 0
    for tid, rec in auth.items():
        name = (rec.get("name") or "").strip()
        if is_junk_value(name):
            skipped_junk += 1
            continue
        tid_l = tid.lower()
        if _pak_resolves(pak, tid_l):
            skipped_pak += 1
            continue
        hit = existing_by_lower.get(tid_l)
        if hit is not None:
            ex_key, ex_val = hit
            if ex_val == name:
                identical += 1
            elif ex_val.strip() == name:
                # Whitespace-only divergence (icehunter trailing spaces) —
                # not a real conflict; existing entry kept untouched.
                ws_only += 1
            else:
                conflicts.append((ex_key, ex_val, name, rec.get("key", "")))
            continue  # existing wins either way
        merged[tid] = name
        added += 1

    merged_sorted = dict(sorted(merged.items(), key=lambda kv: kv[0].lower()))

    print(f"authoritative join:    {len(auth)} ids ({auth_path})")
    print(f"junk filtered:         -{skipped_junk} (PH_/XX_/empty values)")
    print(f"pak sidecar resolves:  -{skipped_pak} (pak wins at runtime)")
    print(f"existing curated:      {len(existing)} entries "
          f"({identical} identical, {ws_only} whitespace-only diffs, "
          f"{len(conflicts)} conflicts kept as-is)")
    print(f"new entries added:     +{added}")
    print(f"curated output:        {len(merged_sorted)} entries")
    if conflicts:
        print("\nconflicts (existing curated WINS — review manually):")
        for ex_key, ex_val, gen_val, loc_key in sorted(conflicts, key=lambda c: c[0].lower()):
            print(f"  {ex_key}: curated={ex_val!r} vs game-data={gen_val!r} [{loc_key}]")

    if dry_run:
        print(f"\n[dry-run] would write {CURATED_OUT}")
        return 0

    CURATED_OUT.write_text(json.dumps(merged_sorted, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    size_kb = CURATED_OUT.stat().st_size / 1024
    print(f"\nwrote {CURATED_OUT} ({size_kb:.1f} KB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"icehunter item-data.json path (default: {DEFAULT_SOURCE})")
    ap.add_argument("--authoritative", type=Path, default=None,
                    help="game-data join file (template_id -> localized name); "
                         "merges INTO the existing curated sidecar instead of "
                         "rebuilding from the icehunter source")
    ap.add_argument("--dry-run", action="store_true", help="print stats, do not write output")
    args = ap.parse_args()

    if args.authoritative is not None:
        return merge_authoritative(args.authoritative, args.dry_run)

    if not args.source.exists():
        print(f"ERROR: source not found: {args.source}", file=sys.stderr)
        print(f"  Clone: cd ~/Tools/dune-third-party && git clone https://github.com/icehunter/dune-admin",
              file=sys.stderr)
        return 1
    if not PAK_SIDECAR.exists():
        print(f"ERROR: pak sidecar not found: {PAK_SIDECAR}", file=sys.stderr)
        return 1

    pak = json.loads(PAK_SIDECAR.read_text(encoding="utf-8"))
    pak_keys = {k.lower() for k in pak}
    ice = json.loads(args.source.read_text(encoding="utf-8"))
    ice_names = ice.get("names", {})
    if not isinstance(ice_names, dict):
        print(f"ERROR: source missing top-level 'names' dict", file=sys.stderr)
        return 1

    kept = {}
    skipped_pak_overlap = 0
    skipped_junk = 0
    for k, v in ice_names.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.lower() in pak_keys:
            skipped_pak_overlap += 1
            continue
        if is_junk_value(v):
            skipped_junk += 1
            continue
        kept[k] = v

    # Alphabetize for git-friendly diffs (case-insensitive sort).
    kept_sorted = dict(sorted(kept.items(), key=lambda kv: kv[0].lower()))

    print(f"icehunter source:      {len(ice_names)} entries")
    print(f"pak sidecar overlap:   -{skipped_pak_overlap} entries (pak wins)")
    print(f"junk filtered:         -{skipped_junk} entries (PH_/XX_/empty)")
    print(f"curated output:        {len(kept_sorted)} entries")

    if args.dry_run:
        print(f"\n[dry-run] would write {CURATED_OUT}")
        return 0

    CURATED_OUT.write_text(json.dumps(kept_sorted, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    size_kb = CURATED_OUT.stat().st_size / 1024
    print(f"\nwrote {CURATED_OUT} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
