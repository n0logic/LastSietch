#!/usr/bin/env python3
# Build authoritative item icon + name + durability sidecars for the Last Sietch
# player portal from the awakening.wiki community API (data sourced from game
# files). This REPLACES the old pak-extraction fuzzy matcher, which guessed
# wrong icons (Light Darts -> blade, Locomotion -> sword) because the real
# template_id -> icon binding is C++-resident and not in the extracted paks.
#
# The wiki exposes per item: item_id (= our template_id), name, image (the real
# T_UI_Icon*_D.png basename), and item_tags (durability/rarity/category). Icons
# are downloaded from media.awakening.wiki via the MediaWiki md5 hash path.
#
# GAP-FILL (2026-06-12): templates the wiki doesn't know (NPC weapons, vehicle
# module grades, schematic blueprints, MTX/swatch variants...) are resolved
# EXACTLY from the client DataTables: scripts/data/dt-item-icon-map.json maps
# DT_BaseItems_* row_key.lower() -> Icon AssetPathName basename (no fuzzing —
# this is the same binding the game UI reads). PNGs for those come from the
# DunePakRE pak extraction (EXTRACT_ROOTS), falling back to wiki media by
# basename. Entries whose PNG cannot be produced are dropped so the portal
# falls back to the unknown glyph instead of a broken <img>.
#
# Outputs (admin-backend/):
#   data/dune-item-icons.json          {template_id_lower: icon_basename}
#   data/dune-item-name-overrides.json {template_id_lower: display_name}
#   data/dune-item-durable.json        {template_id_lower: true}
#   static/img/dune-icons/<basename>.png  (64x64 RGBA)
#
# Source data is cached at /tmp/awakening_all_items.json (re-fetch with --fetch).
# Coverage: argv[1] template list if given, else /tmp/container_template_ids.txt
# if present, else the union of the repo's own universes (existing icon sidecar
# + dune-item-categories.json (market) + dune-item-template-names-curated.json
# (storage)).

import hashlib
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent / "admin-backend"
ICONS_DIR = ROOT / "static/img/dune-icons"
WIKI_ITEMS = Path("/tmp/awakening_all_items.json")
TEMPLATES = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else Path("/tmp/container_template_ids.txt")
DT_MAP = Path(__file__).parent / "data" / "dt-item-icon-map.json"
EXTRACT_ROOTS = [
    Path.home() / "Source/Security/DunePakRE/extracted/textures",
    Path("/mnt/c/Users/the operator/Source/Security/DunePakRE/extracted/textures"),
]
FALLBACK = "T_UI_IconItemUnknownS_D"
MEDIA = "https://media.awakening.wiki/wiki"

# Explicit fixes where the DataTables disagree across tables and the BaseItems
# row carries the wrong texture (Funcom data reuse). Lowercase template_id.
OVERRIDES = {
    "smallchemicalrefinery": "T_UI_IconPlacChoamChemicalRefinerySmallI_D",  # BaseItems_BuildingSets says watercistern
    "d_compactthumper": "T_UI_IconPlacChoamCompactThumper01_D",  # BaseItems_Placeables says plain thumper
}

norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())


def default_templates():
    """Union of the portal's item universes when no explicit list is given:
    everything already mapped + the market category map + the curated storage
    name list. Lowercase, deduped."""
    ids = set()
    for rel in ("data/dune-item-icons.json",
                "data/dune-item-categories.json",
                "data/dune-item-template-names-curated.json"):
        p = ROOT / rel
        if p.exists():
            ids |= {str(k).lower() for k in json.loads(p.read_text())
                    if not str(k).startswith("_")}
    return sorted(ids)


def pak_png_index():
    """basename -> path for every PNG in the DunePakRE texture extractions."""
    idx = {}
    for root in EXTRACT_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*.png"):
            idx.setdefault(p.stem, p)
    return idx


def fetch_items():
    items = []
    for page in (1, 2, 3, 4):
        url = f"https://api.awakening.wiki/items?limit=1000&page={page}"
        with urllib.request.urlopen(url, timeout=60) as r:
            chunk = json.load(r)["list"]
        items += chunk
        if len(chunk) < 1000:
            break
    WIKI_ITEMS.write_text(json.dumps(items))
    return items


def is_durable(tags):
    t = tags or ""
    if "Items.Holsters.BuildingTools" in t:
        return False
    return ("Items.Holsters." in t) or ("Items.Clothes." in t) or ("Items.Armor" in t)


def download_icon(image):
    h = hashlib.md5(image.encode()).hexdigest()
    url = f"{MEDIA}/{h[0]}/{h[0:2]}/{image}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def main():
    if WIKI_ITEMS.exists() and "--fetch" not in sys.argv:
        items = json.loads(WIKI_ITEMS.read_text())
    else:
        items = fetch_items()
    byid = {str(it["item_id"]).lower(): it for it in items if it.get("item_id")}
    byname = {}
    for it in items:
        if it.get("name"):
            byname.setdefault(norm(it["name"]), it)

    if TEMPLATES.exists():
        templates = [l.strip() for l in TEMPLATES.read_text().splitlines() if l.strip()]
    else:
        templates = default_templates()
    resolved = {}
    for t in templates:
        it = byid.get(t.lower()) or byname.get(norm(t))
        if it and it.get("image"):
            resolved[t] = it

    # DataTable gap-fill: exact row-key match first, then punctuation-insensitive
    # (solaris_coin -> SolarisCoin). Wiki wins where both know the item.
    dt_map = {k: v for k, v in json.loads(DT_MAP.read_text()).items()
              if not k.startswith("_")} if DT_MAP.exists() else {}
    dt_norm = {}
    for k, v in dt_map.items():
        dt_norm.setdefault(norm(k), v)
    dt_fill = {}
    for t in templates:
        if t in resolved:
            continue
        hit = dt_map.get(t.lower()) or dt_norm.get(norm(t))
        if hit:
            dt_fill[t] = hit

    # Previous sidecar = last-resort fallback so a wiki/DT regression never
    # un-maps an id that already renders (entry kept only if its PNG survives).
    prev_path = ROOT / "data/dune-item-icons.json"
    prev = {k: v for k, v in json.loads(prev_path.read_text()).items()
            if not k.startswith("_")} if prev_path.exists() else {}

    from PIL import Image
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    icons, names, durable, needed = {}, {}, {}, {}
    for t, it in resolved.items():
        base = it["image"].rsplit(".", 1)[0]
        icons[t.lower()] = base
        names[t.lower()] = it["name"]
        if is_durable(it.get("item_tags")):
            durable[t.lower()] = True
        needed[base] = it["image"]
    for t, base in dt_fill.items():
        icons[t.lower()] = base
        needed.setdefault(base, base + ".png")
    for t, base in prev.items():
        if t.lower() not in icons and (ICONS_DIR / (base + ".png")).exists():
            icons[t.lower()] = base
    for t, base in OVERRIDES.items():
        if t in icons:
            icons[t] = base
            needed.setdefault(base, base + ".png")

    pak_idx = pak_png_index()
    ok = fail = 0
    for base, image in sorted(needed.items()):
        dst = ICONS_DIR / (base + ".png")
        if dst.exists():
            ok += 1
            continue
        try:
            src = pak_idx.get(base)
            data = src.read_bytes() if src else download_icon(image)
            img = Image.open(io.BytesIO(data)).convert("RGBA").resize((64, 64), Image.LANCZOS)
            img.save(dst)
            ok += 1
        except Exception as e:
            print(f"  icon FAIL {image}: {e}")
            fail += 1

    # Drop entries whose PNG never materialised (portal then falls back to the
    # unknown glyph instead of a broken <img>), then prune stale PNGs. The dir
    # is SHARED with the map-icon sidecar (build-map-icon-sidecar.py), so its
    # glyphs are kept too, and non-T_UI files (hand-added) are never touched.
    missing = {b for b in needed if not (ICONS_DIR / (b + ".png")).exists()}
    dropped = [t for t, b in icons.items() if b in missing]
    for t in dropped:
        del icons[t]
    keep = {b + ".png" for b in icons.values()} | {FALLBACK + ".png"}
    map_sidecar = ROOT / "data/dune-map-icons.json"
    if map_sidecar.exists():
        keep |= {str(v) + ".png" for k, v in json.loads(map_sidecar.read_text()).items()
                 if not str(k).startswith("_")}
    pruned = 0
    for f in ICONS_DIR.glob("T_UI_*.png"):
        if f.name not in keep:
            f.unlink()
            pruned += 1

    icons["_unknown"] = FALLBACK
    (ROOT / "data/dune-item-icons.json").write_text(json.dumps(icons, indent=0, sort_keys=True))
    (ROOT / "data/dune-item-name-overrides.json").write_text(json.dumps(names, indent=0, sort_keys=True))
    (ROOT / "data/dune-item-durable.json").write_text(json.dumps(durable, indent=0, sort_keys=True))

    print(f"resolved {len(resolved)} wiki + {len(dt_fill)} datatable / {len(templates)} templates")
    print(f"icons: {ok} present/installed, {fail} failed; {len(needed)} distinct; pruned {pruned}")
    print(f"name overrides: {len(names)} | durable: {len(durable)} | dropped (no PNG): {len(dropped)}")
    print(f"unresolved (-> unknown glyph): {len(templates) - len(resolved) - len(dt_fill)}")


if __name__ == "__main__":
    main()
