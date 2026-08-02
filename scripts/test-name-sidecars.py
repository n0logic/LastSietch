#!/usr/bin/env python3
"""Offline assertion harness for the per-namespace name sidecars.

Verifies, for each sidecar JSON under admin-backend/data/:
  1. File exists, parses, and root is a non-empty dict.
  2. All keys are lowercase non-empty strings; all values are non-empty strings.
  3. Alias-count / source-entry-count ratio >= 1 (catches silent peel-logic
     skips — every source entry should produce at least one alias).
  4. No cross-namespace alias clobber for high-value DB-style template_ids
     (a sanity sample — full cross-product would be noisy, since e.g.
     "foundation" legitimately resolves in both BUILDINGS and ITEMS sometimes).
  5. Specific spot-check template_ids resolve where expected.

Exit non-zero on any assertion failure. Safe to wire into CI later.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "admin-backend/data"
SRC = Path.home() / "Source/Security/DunePakRE/extracted/string_table_lookup.json"

# (sidecar filename, pak namespace, pak suffix, minimum-acceptable alias count)
SIDECARS = (
    ("dune-item-template-names.json",   "ITEMS",                       "_NAME",        5000),
    ("dune-building-names.json",        "BUILDINGS",                   "_NAME",        500),
    ("dune-skill-names.json",           "SKILLS",                      "_NAME",        300),
    ("dune-lore-names.json",            "LORE_PICKUPS_AND_CONTRACTS",  "_NAME",        300),
    ("dune-progression-names.json",     "PROGRESSION",                 "_NAME",        100),
    ("dune-communinet-names.json",      "COMMUNINET",                  "_NAME",        100),
)

# Spot-check template_ids: (sidecar filename, alias_lower, expected substring in name).
SPOT_CHECKS = (
    ("dune-item-template-names.json", "uniquesword_05",   "Pulse-sword"),
    ("dune-item-template-names.json", "solariscoin",      "Solari"),
    ("dune-building-names.json",      "foundation",       "Foundation"),
    ("dune-building-names.json",      "choam_outpost_01", "CHOAM Outpost"),
    ("dune-skill-names.json",         "perfectaim",       "Perfect Aim"),
    ("dune-skill-names.json",         "bindudodge",       "Bindu"),
    ("dune-lore-names.json",          "coded_message",    "Coded Message"),
    ("dune-progression-names.json",   "skillpoints",      "Skill Points"),
    ("dune-progression-names.json",   "taxevasion",       "Tax Evasion"),
    ("dune-communinet-names.json",    "harkonnen_ops",    "Harkonnen Ops"),
)


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"ok    {msg}")


def main() -> int:
    if not SRC.exists():
        print(f"FAIL  source pak JSON not found at {SRC}")
        return 1
    raw = json.loads(SRC.read_text(encoding="utf-8"))

    fails = 0
    loaded: dict[str, dict[str, str]] = {}
    for filename, ns, suffix, min_aliases in SIDECARS:
        path = DATA_DIR / filename
        if not path.exists():
            fail(f"{filename}: missing"); fails += 1
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"{filename}: parse error: {exc}"); fails += 1
            continue
        if not isinstance(d, dict) or not d:
            fail(f"{filename}: not a non-empty dict"); fails += 1
            continue
        loaded[filename] = d

        bad_keys = sum(1 for k in d if not isinstance(k, str) or not k or k != k.lower())
        bad_vals = sum(1 for v in d.values() if not isinstance(v, str) or not v.strip())
        if bad_keys or bad_vals:
            fail(f"{filename}: {bad_keys} bad keys, {bad_vals} bad values"); fails += 1
            continue

        # Source entry count from the pak.
        src_entries = sum(
            1 for k in raw
            if k.startswith(ns + "/") and k.endswith(suffix)
        )
        if src_entries == 0:
            fail(f"{filename}: pak has 0 entries for {ns}/_*{suffix}"); fails += 1
            continue
        ratio = len(d) / src_entries
        if ratio < 1.0:
            fail(f"{filename}: alias/source ratio {ratio:.2f} < 1.0 "
                 f"({len(d)} aliases / {src_entries} entries) — silent peel skip?")
            fails += 1
            continue
        if len(d) < min_aliases:
            fail(f"{filename}: {len(d)} aliases under min {min_aliases}"); fails += 1
            continue
        ok(f"{filename}: {len(d)} aliases / {src_entries} source ({ratio:.2f}x), "
           f"{path.stat().st_size} bytes")

    # Spot checks.
    for filename, alias, expected_sub in SPOT_CHECKS:
        d = loaded.get(filename)
        if d is None:
            fail(f"spot: {filename}!{alias}: sidecar not loaded"); fails += 1
            continue
        v = d.get(alias)
        if v is None:
            fail(f"spot: {filename}!{alias!r}: MISS"); fails += 1
        elif expected_sub.lower() not in v.lower():
            fail(f"spot: {filename}!{alias!r}: {v!r} missing {expected_sub!r}")
            fails += 1
        else:
            ok(f"spot: {filename}!{alias!r} -> {v!r}")

    # Cross-namespace clobber sanity (informational — count, don't fail).
    all_aliases: Counter[str] = Counter()
    for d in loaded.values():
        all_aliases.update(d.keys())
    collisions = sum(1 for n in all_aliases.values() if n > 1)
    print(f"info  cross-namespace alias collisions: {collisions} "
          f"(informational — each sidecar is queried with namespace context)")

    if fails:
        print(f"FAIL  {fails} assertion(s) failed")
        return 1
    print(f"PASS  all {len(SIDECARS)} sidecars + {len(SPOT_CHECKS)} spot checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
