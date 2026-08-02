#!/usr/bin/env python3
"""Build the authoritative market-category sidecar for the portal exchange browse.

The portal's top-level category tabs (Garments, Weapons, Tools, Resources,
Vehicles, Augments, Building) used to be guessed from template_id stems, which
mis-sorted items whose id happens to contain a stem (e.g. "Plasteel Composite
Armor Plating" -> garments via "armor"; "Improved Holtzman Actuator" ->
garments via "holtzman"). The market bot's item-data.json carries the game's
real per-item `category` path (extracted from the pak), so we derive an
authoritative template_id -> tab map from it and ship it to admin-backend.

Game category paths -> portal tab:
  items/garment/*            -> garments
  items/weapons/*            -> weapons
  items/vehicles/*           -> vehicles
  items/augment/*            -> augmentations
  items/utility/deployables  -> building
  items/utility/*            -> tools
  items/misc/*               -> resources   (components, raw/refined resources, fuel)

Schematics are NOT a category here: a schematic is categorised by what it CRAFTS
(item-data.json gives the crafted category), and the portal flags it separately
via market_categories.is_schematic(). Items not in item-data.json (uniques,
blueprints) fall through to the stem heuristic in market_categories.py.

Usage: scripts/build-market-categories.py
  reads  dune-market-bot/item-data.json
  writes admin-backend/data/dune-item-categories.json   (lowercase tid -> tab key)
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "dune-market-bot" / "item-data.json"
OUT = REPO / "admin-backend" / "data" / "dune-item-categories.json"
# Sidecar of items the game flags non-tradeable (cannot be listed on the CHOAM
# exchange: story/soulbound gear, MTX cosmetics, etc.). Same source field the
# market bot itself filters on (catalog.py: `if d.get("tradeable") is False`).
OUT_NT = REPO / "admin-backend" / "data" / "dune-item-non-tradeable.json"


def map_category(path: str):
    """Map a game category path to a portal tab key, or None to defer to stems."""
    p = (path or "").lower()
    if p.startswith("items/garment"):
        return "garments"
    if p.startswith("items/weapons"):
        return "weapons"
    if p.startswith("items/vehicles"):
        return "vehicles"
    if p.startswith("items/augment"):
        return "augmentations"
    if p.startswith("items/utility/deployables"):
        return "building"
    if p.startswith("items/utility"):
        return "tools"
    if p.startswith("items/misc"):
        return "resources"
    return None


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    items = data.get("items", {})
    out = {}
    skipped = 0
    non_tradeable = []
    for tid, d in items.items():
        if d.get("tradeable") is False:
            non_tradeable.append(tid.lower())
        tab = map_category(d.get("category", ""))
        if tab is None:
            skipped += 1
            continue
        out[tid.lower()] = tab
    OUT.write_text(json.dumps(out, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    OUT_NT.write_text(json.dumps(sorted(set(non_tradeable)), indent=0) + "\n", encoding="utf-8")
    # Stderr summary so it doesn't pollute any piped JSON.
    from collections import Counter
    dist = Counter(out.values())
    import sys
    print(f"wrote {len(out)} mappings to {OUT} ({skipped} uncategorised, left to stems)",
          file=sys.stderr)
    print(f"wrote {len(set(non_tradeable))} non-tradeable template_ids to {OUT_NT}",
          file=sys.stderr)
    print("  distribution: " + ", ".join(f"{k}={n}" for k, n in dist.most_common()),
          file=sys.stderr)


if __name__ == "__main__":
    main()
