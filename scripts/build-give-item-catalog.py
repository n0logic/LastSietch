#!/usr/bin/env python3
"""Build the give-item typeahead catalog for the V2 Live Actions tab.

Source of truth = dune-market-bot/item-data.json (the game's own item table;
its keys ARE the template_ids the native give-item / AddItemToInventory
ServerCommand accepts, "Stone", "PlantFiber" verified live 2026-06-10). Every
key already passes the wrapper charset [A-Za-z0-9_], so the catalog never offers
an item the relay would reject.

Output = admin-backend/data/dune-give-item-catalog.json, a compact
{"items":[{"id","name","cat",...}...]} sorted by display name, committed to the
repo so the admin-backend has no runtime dependency on the sibling market-bot
dir. Enrichment fields (2026-06-12, only emitted when meaningful):

  pak_max_stack  - MaxStackSize from the client pak (dune-item-pak-meta.json,
                   scripts/build-item-pak-meta.py). PAK value, NOT live truth:
                   live server config can differ (SolarisCoin pak 50k vs live
                   100k). Falls back to item-data stack_max (same pak origin;
                   verified identical for all 1662 ids on build 1988751).
  mtx            - true for MTX_ -prefixed templates (event/paid cosmetics).
                   No MTX item-tag exists in the pak; prefix is the signal.
  non_tradeable  - true when the id is in the dune-item-non-tradeable.json
                   sidecar (cannot be listed on the exchange).
  tier / rarity  - from item-data where present.
  is_gradeable   - true when the template can exist at a grade above Base. Carried
                   verbatim from item-data (the canonical flag); the Karum wanted-order
                   picker uses it to decide whether to offer Base / G1..G5 at all.
                   Schematics DO carry real grades (live-verified 2026-08-25 on
                   dune.items: SMG_Unique_LargeMag_06_Schematic spans quality_level
                   0..5), so they are NOT suppressed here. Grades top out at 5.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "dune-market-bot", "item-data.json")
DATA = os.path.join(REPO, "admin-backend", "data")
PAK_META = os.path.join(DATA, "dune-item-pak-meta.json")
NON_TRADEABLE = os.path.join(DATA, "dune-item-non-tradeable.json")
OUT = os.path.join(DATA, "dune-give-item-catalog.json")


def short_category(path: str) -> str:
    """Friendly leaf from a slash category path ('items/garment/utilitywearables'
    -> 'utilitywearables'). Empty string if absent."""
    if not path:
        return ""
    return path.rstrip("/").split("/")[-1]


def main() -> None:
    with open(SRC, encoding="utf-8") as fh:
        items = json.load(fh)["items"]

    pak_meta = {}
    if os.path.exists(PAK_META):
        with open(PAK_META, encoding="utf-8") as fh:
            pak_meta = json.load(fh).get("items", {})
    with open(NON_TRADEABLE, encoding="utf-8") as fh:
        non_tradeable = {t.lower() for t in json.load(fh)}

    out = []
    for tid, meta in items.items():
        # Defensive: the wrapper rejects anything outside [A-Za-z0-9_].
        if not tid.replace("_", "").isalnum():
            continue
        name = (meta.get("name") or "").strip() or tid
        entry = {"id": tid, "name": name, "cat": short_category(meta.get("category", ""))}

        pak = pak_meta.get(tid.lower(), {})
        stack = pak.get("max_stack") or meta.get("stack_max")
        if isinstance(stack, int) and stack > 0:
            entry["pak_max_stack"] = stack
        tags = pak.get("tags", [])
        if tid.startswith("MTX_") or any(t.startswith(("MTX", "Items.MTX")) for t in tags):
            entry["mtx"] = True
        if tid.lower() in non_tradeable:
            entry["non_tradeable"] = True
        tier = meta.get("tier")
        if isinstance(tier, int):
            entry["tier"] = tier
        rarity = meta.get("rarity")
        if rarity:
            entry["rarity"] = rarity
        # Emitted only when true, matching the other enrichment fields: absent means
        # "Base only", which is what every consumer already assumes for an old catalog.
        if meta.get("is_gradeable") is True:
            entry["is_gradeable"] = True
        out.append(entry)

    out.sort(key=lambda e: (e["name"].lower(), e["id"]))
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"items": out}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(out)} items -> {OUT}")


if __name__ == "__main__":
    main()
