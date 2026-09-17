#!/usr/bin/env python3
"""Build the player portal's game-data sidecars and icons from public sources.

Nothing in this repository is extracted from the game's package files, so a
fresh checkout has no item icons, no item names, no map glyphs and no marker
snapshots. This script regenerates all of them from two sources you are
entitled to read: the awakening.wiki community API (item data sourced from the
game files and published as a public API) and your own game database (map
markers, exported with scripts/dune-markers-export.py).

    python3 scripts/build-portal-assets.py                    # data + icons + glyphs
    python3 scripts/build-portal-assets.py --skip-icons       # the JSON sidecars only
    python3 scripts/build-portal-assets.py --markers-export markers.json
    python3 scripts/build-portal-assets.py --out /opt/lastsietch-admin

Outputs under --out (default: the admin-backend/ directory of this checkout):

    data/dune-item-icons.json           template_id -> icon basename
    data/dune-item-name-overrides.json  template_id -> display name
    data/dune-item-durable.json         template_id -> true (item has durability)
    data/dune-item-categories.json      template_id -> exchange browse tab
    data/dune-give-item-catalog.json    grant and reward picker catalog
    data/dune-map-icons.json            marker type -> map glyph basename
    data/<map>-markers-snapshot.json    only with --markers-export (four maps)
    static/img/dune-icons/*.png         item icons and map glyphs, 64x64
    static/img/stats/*.png              currency, XP and specialisation glyphs
    portal-nextgen/static/fonts/*.woff2 the portal's fonts (OFL), from Google Fonts
    static/js/vendor/three/three.module.min.js  three.js core (MIT), from its release
    portal-nextgen/static/*.png         placeholder PWA icons and favicon (yours to replace)

Coverage is what the wiki knows: every item a player can see or trade, about
2,300 templates. Templates it does not carry (NPC-only weapons, internal
variants, some cosmetics) render with the generic unknown glyph and a name
synthesised from the id. The portal never fails on a missing icon or name.

This repository contains no binary files at all, so the fonts, the three.js
core and the app icons are also produced here rather than shipped.

The wiki's content licence (CC BY-NC-SA) binds the DOWNLOADED files. They are
for your community's non-commercial use, which is why this repository does not
ship them and why this script exists. Re-run after a game update; only files
that are missing are fetched unless --force is given.

Python 3.10+, network access, no third-party packages. Pillow is optional: with
it icons are resized to 64x64, without it they are stored at wiki size.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.awakening.wiki/items?limit=1000&page={page}"
MEDIA = "https://media.awakening.wiki/wiki/{h1}/{h2}/{name}"
UA = "LastSietch-portal-assets/0.7 (+https://github.com/n0logic/LastSietch)"
FALLBACK = "T_UI_IconItemUnknownS_D"
HERE = Path(__file__).resolve().parent

# Exchange browse tabs. The first rule whose prefix matches ANY of the item's
# tags wins, so order is specificity. Measured against the game's own category
# table on 1,174 shared items: 99.8 percent agreement.
TAB_RULES = [
    ("Items.Holsters.BuildingTools", "building"),
    ("Items.Holsters.Deployables", "vehicles"),
    ("Items.Schematics.Deployables", "vehicles"),
    ("Items.Holsters.RangedWeapons", "weapons"),
    ("Items.Holsters.MeleeWeapons", "weapons"),
    ("Items.Schematics.RangedWeapons", "weapons"),
    ("Items.Schematics.MeleeWeapons", "weapons"),
    ("Items.Ammo", "weapons"),
    ("Items.Clothes", "garments"),
    ("Items.Schematics.Clothes", "garments"),
    ("Items.Augment", "augmentations"),
    ("Items.Holsters", "tools"),
    ("Items.Schematics", "tools"),
    ("Items.Consumables", "tools"),
    ("Items.CraftedResources", "resources"),
    ("Items.RawResources", "resources"),
    ("Items.RefinedResources", "resources"),
]

# Marker type -> the in-game minimap glyph the game itself draws for it. This is
# a curated table (the portal's own), not extracted data; the glyph images are
# fetched from the wiki like any other icon. Every glyph below is available
# there (checked 2026-09-17, 59 of 59).
MAP_GLYPHS = {
    "arrakeenrecustomization": "T_UI_IconMapCharacterContractor_D",
    "atreidesfortress": "T_UI_IconMapMarkerBaseFortressBG_D",
    "atreidesvendor": "T_UI_IconsFactionsAtreidesBuilding_D",
    "azuriteore": "T_UI_IconMapMarkerMineralAzurite_D",
    "azuritepickup": "T_UI_IconMapMarkerMineralAzurite_D",
    "bank": "T_UI_IconResourceSolarisCoin_D",
    "barkeepvendor": "T_UI_IconMapVendorBarkeeper_D",
    "basaltore": "T_UI_IconMapMarkerMineralBasalt_D",
    "basaltpickup": "T_UI_IconMapMarkerMineralBasalt_D",
    "bauxiteore": "T_UI_IconMapMarkerMineralBauxite_D",
    "bauxitepickup": "T_UI_IconMapMarkerMineralBauxite_D",
    "beastsbend": "T_UI_IconMapCityBeastsBend_D",
    "brittlebush": "T_UI_IconMapPlantFiber_D",
    "cave": "T_UI_IconMapMarkerCave_D",
    "centralplaza": "T_UI_IconMapCityCentralPlaza_D",
    "choamexchange": "T_UI_IconMapCityChoamExchange_D",
    "controlpointhousetseida": "T_UI_IconMapMarkerObjective_D",
    "dolomitepickup": "T_UI_IconMapMarkerMineralDolomite_D",
    "dolomiterock": "T_UI_IconMapMarkerMineralDolomite_D",
    "duncansdojo": "T_UI_IconMapCityDuncansDojo_D",
    "ecolab": "T_UI_IconMapMarkerEcolab_D",
    "enemycamp": "T_UI_IconMapMarkerCamp_D",
    "enemylaboroutpost": "T_UI_IconMapMarkerLabor_D",
    "enemyoutpost": "T_UI_IconMapMarkerOutpost_D",
    "erythriteore": "T_UI_IconMapMarkerMineralErythrite_D",
    "erythritepickup": "T_UI_IconMapMarkerMineralErythrite_D",
    "explorationpointofinterest": "T_UI_IconMapMarkerPointofinterest_D",
    "fuelcellpart": "T_UI_IconMapMarkerSalvageFuel_D",
    "fuelcellwreckage": "T_UI_IconMapMarkerSalvageFuel_D",
    "hannivars": "T_UI_IconMapCityHannivars_D",
    "harkonnenfortress": "T_UI_IconMapMarkerBaseFortressBG_D",
    "harkonnenvendor": "T_UI_IconsFactionsHarkonnen_D",
    "harkorecustomization": "T_UI_IconMapCharacterContractor_D",
    "hazard_drumsand": "T_UI_IconMapMarkerEnvDrumsandHatching_D",
    "hazard_quicksand": "T_UI_IconMapMarkerEnvQuicksandHatching_D",
    "hazard_radiation": "T_UI_IconMapMarkerEnvRadiation_D",
    "houserepresentativealexin": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativeargosaz": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativedyvetz": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativeecaz": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativehagal": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativehurata": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativeimota": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativekenola": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativelindaren": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativemaros": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativemikarrol": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativemoritani": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativemutelli": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativenovebruns": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativerichese": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativesor": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativespinette": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativetaligari": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativethorvald": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativevarota": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativevernius": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativewallach": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativewayku": "T_UI_IconMapCharacterLandsraad_D",
    "houserepresentativewydras": "T_UI_IconMapCharacterLandsraad_D",
    "imperialconsulate": "T_UI_IconMapCityImperialConsulate_D",
    "jasmiumore": "T_UI_IconMapMarkerMineralJasmium_D",
    "jasmiumpickup": "T_UI_IconMapMarkerMineralJasmium_D",
    "landsraadvendor": "T_UI_IconMapCharacterLandsraad_D",
    "levelexit": "T_UI_IconMapMarkerLandingPLatform_D",
    "magnetiteore": "T_UI_IconMapMarkerMineralMagnetite_D",
    "magnetitepickup": "T_UI_IconMapMarkerMineralMagnetite_D",
    "neocarthagoutskirts": "T_UI_IconMapCityNeoCarthargOutskirts_D",
    "newcourtway": "T_UI_IconMapCityNewCourtWay_D",
    "primrosefield": "T_UI_IconMapPlantFiber_D",
    "residencycourtyard": "T_UI_IconMapCityResidencyApproach_D",
    "resourcevendor": "T_UI_IconPickupBlueprint_D",
    "rhyoliteore": "T_UI_IconMapMarkerMineralRhyolite_D",
    "rhyolitepickup": "T_UI_IconMapMarkerMineralRhyolite_D",
    "saguaroseed": "T_UI_IconMapMarkerSaguaro_D",
    "salusanbull": "T_UI_IconMapCitySalsanBull_D",
    "scrapmetalpart": "T_UI_IconMapMarkerSalvageScrap_D",
    "scrapmetalwreckage": "T_UI_IconMapMarkerSalvageScrap_D",
    "shipwreck": "T_UI_IconMapMarkerShipwreck_D",
    "sietch": "T_UI_IconMapLocationCity_D",
    "spicevendor": "T_UI_IconPickupSpice_D",
    "spirekeep": "T_UI_IconMapCitySpireKeep_D",
    "stravidiumore": "T_UI_IconMapMarkerMineralStravidiumOre_D",
    "stravidiumpickup": "T_UI_IconMapMarkerMineralStravidiumOre_D",
    "structurevendor": "T_UI_IconPickupBlueprint_D",
    "survivalvendor": "T_UI_IconPickupWater_D",
    "taxiservice": "T_UI_IconMapVendorTaxi_D",
    "thebaronseye": "T_UI_IconMapCityTheBaronsEye_D",
    "thewarrens": "T_UI_IconMapCityTheWarrens_D",
    "titaniumore": "T_UI_IconMapMarkerMineralTitaniumOre_D",
    "titaniumpickup": "T_UI_IconMapMarkerMineralTitaniumOre_D",
    "tradingpost": "T_UI_IconMapMarkerTradingPost_D",
    "trainerbenegesserit": "T_UI_IconMapCharacterMentorBeneGesserit_D",
    "trainermentat": "T_UI_IconMapCharacterMentorMentat_D",
    "trainerplanetologist": "T_UI_IconMapCharacterMentorPlanetologist_D",
    "trainerswordmaster": "T_UI_IconMapCharacterMentorSwordmaster_D",
    "trainertrooper": "T_UI_IconMapCharacterMentorTrooper_D",
    "vehiclevendor": "T_UI_IconPickupVehicle_D",
    "weaponsvendor": "T_UI_IconMapVendorWeapons_D",
}

# Portal stat, currency and specialisation glyphs: local slug -> wiki file.
# Stored at wiki size under static/img/stats/ (stat_icons.py reads by slug).
STAT_GLYPHS = {
    "solari": "T_UI_Icon_Currency_SolariCoins_D.png",
    "solari-bank": "T_UI_Icon_Currency_SolariCredit_D.png",
    "scrip": "T_UI_Icon_Currency_HouseCredit_D.png",
    "intel": "T_UI_IconMapIntel_D.png",
    "xp": "Stat_XP.png",
    "faction-standing": "Stat_Faction_Standing.png",
    "spec-combat": "Stat_Combat_XP.png",
    "spec-crafting": "Stat_Crafting_XP.png",
    "spec-gathering": "Stat_Gathering_XP.png",
    "spec-exploration": "Stat_Exploration_XP.png",
    "spec-sabotage": "Stat_Sabotage_XP.png",
}

# Fonts (SIL OFL 1.1) come from Google Fonts at install time: the CSS API is
# asked for the exact weights the portal's app.css declares and the latin
# subset of each family is saved under the file name app.css expects. Google
# serves these families as variable fonts, so two weights may share a file.
FONTS_CSS = ("https://fonts.googleapis.com/css2?family=Saira:wght@400;500"
             "&family=Saira+Condensed:wght@600;700&family=JetBrains+Mono:wght@400;600&display=swap")
FONT_FILES = {
    ("JetBrains Mono", "400"): "jbmono-400.woff2",
    ("JetBrains Mono", "600"): "jbmono-600.woff2",
    ("Saira", "400"): "saira-400.woff2",
    ("Saira", "500"): "saira-500.woff2",
    ("Saira Condensed", "600"): "saira-cond-600.woff2",
    ("Saira Condensed", "700"): "saira-cond-700.woff2",
}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# three.js (MIT): the minified core the classic viewers import. Pinned to the
# version the front end's lockfile carries; the helper modules beside it in
# static/js/vendor/three/ are text and ship with the repository.
THREE_VERSION = "0.160.1"
THREE_URL = f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.module.min.js"

# Marker snapshot files per map_name_id, matching admin-backend/map_model.py.
SNAPSHOTS = {
    1: "arrakeen-markers-snapshot.json",
    7: "dd-markers-snapshot.json",
    9: "harko-village-markers-snapshot.json",
    11: "hagga-markers-snapshot.json",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def media_url(basename_png: str) -> str:
    h = hashlib.md5(basename_png.encode("utf-8")).hexdigest()
    return MEDIA.format(h1=h[0], h2=h[:2], name=basename_png)


def fetch_items(cache: Path, force: bool) -> list[dict]:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and not force:
        items = json.loads(cache.read_text(encoding="utf-8"))
        log(f"items: {len(items)} from cache {cache}")
        return items
    items, page = [], 1
    while True:
        data = json.loads(http_get(API.format(page=page)))
        items.extend(data.get("list") or [])
        info = data.get("pageInfo") or {}
        log(f"items: page {page}, {len(items)} so far")
        if info.get("isLastPage", True):
            break
        page += 1
        time.sleep(0.5)
    cache.write_text(json.dumps(items), encoding="utf-8")
    return items


def tags_of(item: dict) -> list[str]:
    raw = item.get("item_tags")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        v = json.loads(raw or "[]")
        return [str(t) for t in v] if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def tab_of(tags: list[str]) -> str | None:
    for prefix, tab in TAB_RULES:
        if any(t.startswith(prefix) for t in tags):
            return tab
    return None


def is_durable(tags: list[str]) -> bool:
    for t in tags:
        if t.startswith("Items.Holsters.BuildingTools"):
            continue
        if t.startswith("Items.Holsters.") or t.startswith("Items.Clothes."):
            return True
    return False


_WIKI_LINK = re.compile(r"\[\[[^|\]]*\|([^\]]+)\]\]|\[\[([^\]]+)\]\]")


def _plain(v: str | None) -> str:
    """'[[Buggy#Engine|Buggy - Engine]]' -> 'Buggy - Engine'."""
    if not v:
        return ""
    m = _WIKI_LINK.search(v)
    return (m.group(1) or m.group(2)) if m else v


def category_of(item: dict, tags: list[str]) -> str | None:
    """Fine-grained picker category, in the vocabulary the grant and reward
    code groups by (chest/hands/feet/head/legs, lightarmor/heavyarmor/
    stillsuits, smg/pistol/heavyrifle..., engine/hull/chassis/psu/utility,
    cutteray/powerpack/watertools/bloodtools, components/rawresources/fuel).
    Best effort from the wiki's typed fields; a schematic is categorised by
    what it crafts, as the game does. None when the item has no family tag
    (contracts, blueprints, cosmetics): those are not giveable and stay out
    of the catalog."""
    # The first tag that names a family. Items.ExcludeFromExchange and the
    # like are flags, not categories, and often come first.
    families = ("Items.Holsters.", "Items.Clothes.", "Items.Schematics.", "Items.CraftedResources",
                "Items.RawResources", "Items.RefinedResources", "Items.Consumables", "Items.Ammo",
                "Items.Augment")
    itag = next((t for t in tags if t.startswith(families)), "")
    if not itag:
        return None
    parts = itag.split(".")
    if len(parts) > 1 and parts[1] == "Schematics":
        # A schematic is categorised by what it crafts. Everything but clothes
        # is a holster item, so re-root the tag there and fall through.
        parts = ["Items"] + parts[2:]
        if parts[1:2] != ["Clothes"]:
            parts = ["Items", "Holsters"] + parts[1:]
    fam = parts[1] if len(parts) > 1 else ""
    slot = (item.get("equip_slot") or "").strip()
    gtype = (item.get("garment_type") or "").strip()
    if fam == "Clothes" or slot or gtype:
        slots = {"Torso": "chest", "Top and Legs": "chest", "Hands": "hands",
                 "Feet": "feet", "Head": "head", "Legs": "legs"}
        if slot in slots:
            return slots[slot]
        gts = {"Light Armor": "lightarmor", "Heavy Armor": "heavyarmor",
               "Water Discipline": "stillsuits"}
        if gtype in gts:
            return gts[gtype]
        sub = parts[2].lower() if len(parts) > 2 else ""
        return {"scoutarmor": "lightarmor", "assaultarmor": "heavyarmor",
                "heavyarmor": "heavyarmor", "stillsuit": "stillsuits"}.get(sub, "garment")
    if fam == "Holsters" and len(parts) > 2:
        kind = parts[2]
        if kind == "RangedWeapons":
            # Items.Holsters.RangedWeapons.<Light|Heavy>.<Class>[.<Sub>]: the
            # game's picker key is the most specific class, prefixed with
            # "heavy" only for the heavy line (heavypistol, heavyrifle).
            rest = [p.lower() for p in parts[3:]]
            leaf = rest[-1] if rest else "ranged"
            if len(rest) >= 2 and rest[0] == "heavy" and not leaf.startswith("heavy"):
                return "heavy" + leaf
            return leaf
        if kind == "MeleeWeapons":
            rest = [p.lower() for p in parts[3:]]
            leaf = rest[-1] if rest else "melee"
            if any(r in ("knife", "dagger", "kindjal", "crysknife", "shortblades") for r in rest):
                return "shortblades"
            if any(r in ("sword", "longblades", "greatsword") for r in rest):
                return "longblades"
            return leaf
        if kind == "Deployables":
            mod = _plain(item.get("vehicle_module_type"))
            if mod:
                label = mod.split(" - ")[-1].strip().lower()
                for needle, cat in (("utility", "utility"), ("engine", "engine"),
                                    ("rear hull", "rear"), ("hull", "hull"), ("chassis", "chassis"),
                                    ("generator", "psu"), ("wing", "locomotion"),
                                    ("tread", "locomotion"), ("wheel", "locomotion")):
                    if needle in label:
                        return cat
                return label.replace(" ", "")
            # No module field: a whole vehicle or a base part; use the tag's
            # vehicle segment (VehicleBase.Sandbike -> sandbike).
            seg = parts[4].lower() if len(parts) > 4 else "deployables"
            for needle, cat in (("sandbike", "sandbike"), ("buggy", "buggy"),
                                ("lightorni", "lightornithopter"), ("scoutorni", "lightornithopter"),
                                ("mediumorni", "mediumornithopter"), ("assaultorni", "mediumornithopter"),
                                ("transportorni", "transportornithopter"), ("carrierorni", "transportornithopter"),
                                ("sandcrawler", "sandcrawler"), ("crawler", "sandcrawler")):
                if needle in seg:
                    return cat
            return seg
        util = _plain(item.get("utility_type")).lower()
        if util:
            for needle, cat in (("cutteray", "cutteray"), ("power pack", "powerpack"),
                                ("dew", "watertools"), ("water", "watertools"),
                                ("fluid", "bloodtools"), ("blood", "bloodtools"),
                                ("compactor", "compactor"), ("suspensor", "suspensor"),
                                ("scanner", "scanner"), ("shield", "shield"),
                                ("fuel", "fuel")):
                if needle in util:
                    return cat
        sub = parts[3].lower() if len(parts) > 3 else ""
        if kind == "HydrationTools":
            return "bloodtools" if "blood" in sub else "watertools"
        return {"utilitytools": "utility", "gatheringtools": "cutteray",
                "cartographytools": "cartographytools",
                "buildingtools": "buildingtools"}.get(kind.lower(), kind.lower())
    if fam == "CraftedResources":
        return "components"
    if fam == "RawResources":
        return "fuel" if "Fuel" in itag else "rawresources"
    if fam == "RefinedResources":
        return "fuel" if "Fuel" in itag else "refinedresources"
    if fam == "Consumables":
        return "consumables"
    if fam == "Ammo":
        return "ammo"
    if fam == "Augment":
        return "augment"
    return fam.lower() or "other"


def tier_of(tags: list[str]) -> int | None:
    for t in tags:
        if t.startswith("LootTier."):
            try:
                return int(t.split(".", 1)[1])
            except ValueError:
                return None
    return None


def rarity_of(item: dict, tags: list[str]) -> str | None:
    if str(item.get("unique_schematic") or "").strip().lower() in ("1", "true", "yes"):
        return "Unique"
    for t in tags:
        if t.startswith("Rarity."):
            return t.split(".", 1)[1].lower()
    return None


def build_data(items: list[dict], out: Path) -> dict:
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    icons, names, durable, cats, give = {}, {}, {}, {}, []
    for it in items:
        tid = (it.get("item_id") or "").strip()
        if not tid:
            continue
        key = tid.lower()
        tags = tags_of(it)
        image = (it.get("image") or "").strip()
        if image.lower().endswith(".png"):
            icons[key] = image[:-4]
        name = (it.get("name") or "").strip()
        if name:
            names[key] = name
        if is_durable(tags):
            durable[key] = True
        tab = tab_of(tags)
        if tab:
            cats[key] = tab
        tier = tier_of(tags)
        try:
            stack = int(float(it.get("max_stack") or 1))
        except ValueError:
            stack = 1
        cat = category_of(it, tags)
        if cat is None:
            continue
        entry = {"id": tid, "name": name or tid, "cat": cat, "pak_max_stack": stack}
        if tier is not None:
            entry["tier"] = tier
        rar = rarity_of(it, tags)
        if rar:
            entry["rarity"] = rar
        if tier == 6 and tab in ("garments", "weapons", "vehicles", "tools") \
                and not any(t.startswith("Items.Schematics") for t in tags):
            entry["is_gradeable"] = True
        give.append(entry)
    icons["_unknown"] = FALLBACK
    give.sort(key=lambda e: e["name"].lower())
    written = {
        "dune-item-icons.json": icons,
        "dune-item-name-overrides.json": names,
        "dune-item-durable.json": durable,
        "dune-item-categories.json": cats,
        "dune-give-item-catalog.json": {"items": give},
        "dune-map-icons.json": dict(sorted(MAP_GLYPHS.items())),
    }
    for fn, obj in written.items():
        (data / fn).write_text(json.dumps(obj, indent=0, sort_keys=(fn != "dune-give-item-catalog.json"),
                                          ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"data: icons {len(icons)}, names {len(names)}, durable {len(durable)}, "
        f"categories {len(cats)}, give catalog {len(give)}, map glyphs {len(MAP_GLYPHS)}")
    return icons


def _resize(png: bytes) -> bytes:
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return png
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    if img.size != (64, 64):
        img = img.resize((64, 64), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def fetch_icons(basenames: list[str], icons_dir: Path, force: bool, workers: int) -> tuple[int, int, list[str]]:
    icons_dir.mkdir(parents=True, exist_ok=True)
    todo = [b for b in basenames if force or not (icons_dir / f"{b}.png").exists()]
    log(f"icons: {len(basenames)} wanted, {len(todo)} to fetch into {icons_dir}")
    failed: list[str] = []

    def one(b: str) -> bool:
        url = media_url(f"{b}.png")
        for attempt in range(3):
            try:
                png = http_get(url, timeout=30)
                tmp = icons_dir / f".{b}.png.part"
                tmp.write_bytes(_resize(png))
                tmp.replace(icons_dir / f"{b}.png")
                return True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return False
                time.sleep(1 + attempt)
            except Exception:
                time.sleep(1 + attempt)
        return False

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for b, ok in zip(todo, ex.map(one, todo)):
            if ok:
                done += 1
            else:
                failed.append(b)
            if (done + len(failed)) % 200 == 0:
                log(f"icons: {done + len(failed)}/{len(todo)}")
    return done, len(todo), failed


def prune_missing(out: Path, failed: set[str]) -> None:
    """Drop icon-map entries whose PNG could not be produced, so the portal
    shows the unknown glyph instead of a broken image."""
    if not failed:
        return
    p = out / "data" / "dune-item-icons.json"
    icons = json.loads(p.read_text(encoding="utf-8"))
    before = len(icons)
    icons = {k: v for k, v in icons.items() if v not in failed or k == "_unknown"}
    p.write_text(json.dumps(icons, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    log(f"icons: pruned {before - len(icons)} entries without a PNG")


def fetch_stat_glyphs(stats_dir: Path, force: bool) -> tuple[int, list[str]]:
    stats_dir.mkdir(parents=True, exist_ok=True)
    done, failed = 0, []
    for slug, wiki_name in STAT_GLYPHS.items():
        dest = stats_dir / f"{slug}.png"
        if dest.exists() and not force:
            continue
        try:
            png = http_get(media_url(wiki_name), timeout=30)
            tmp = stats_dir / f".{slug}.png.part"
            tmp.write_bytes(png)
            tmp.replace(dest)
            done += 1
        except Exception:
            failed.append(slug)
    log(f"stat glyphs: fetched {done}, failed {len(failed)}{' ' + str(failed) if failed else ''}")
    return done, failed


def fetch_fonts(fonts_dir: Path, force: bool) -> tuple[int, list[str]]:
    fonts_dir.mkdir(parents=True, exist_ok=True)
    wanted = {k: v for k, v in FONT_FILES.items() if force or not (fonts_dir / v).exists()}
    if not wanted:
        log("fonts: all present")
        return 0, []
    req = urllib.request.Request(FONTS_CSS, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        css = r.read().decode("utf-8")
    urls: dict[tuple[str, str], str] = {}
    for subset, block in re.findall(r"/\* (\w+) \*/\s*@font-face \{(.*?)\}", css, re.S):
        if subset != "latin":
            continue
        fam = re.search(r"font-family: '([^']+)'", block)
        wgt = re.search(r"font-weight: (\d+)", block)
        url = re.search(r"url\((https://[^)]+)\)", block)
        if fam and wgt and url:
            urls[(fam.group(1), wgt.group(1))] = url.group(1)
    done, failed = 0, []
    for key, name in wanted.items():
        url = urls.get(key)
        if not url:
            failed.append(name)
            continue
        try:
            data = http_get(url, timeout=30)
            tmp = fonts_dir / f".{name}.part"
            tmp.write_bytes(data)
            tmp.replace(fonts_dir / name)
            done += 1
        except Exception:
            failed.append(name)
    log(f"fonts: fetched {done}, failed {len(failed)}{' ' + str(failed) if failed else ''}")
    return done, failed


def fetch_three(vendor_dir: Path, force: bool) -> bool:
    vendor_dir.mkdir(parents=True, exist_ok=True)
    dest = vendor_dir / "three.module.min.js"
    if dest.exists() and not force:
        log("three.js: present")
        return True
    try:
        data = http_get(THREE_URL, timeout=60)
        if b"three.js" not in data[:400].lower() and b"Three.js" not in data[:400]:
            raise ValueError("unexpected content")
        tmp = vendor_dir / ".three.module.min.js.part"
        tmp.write_bytes(data)
        tmp.replace(dest)
        log(f"three.js: fetched {THREE_VERSION} ({len(data)} bytes)")
        return True
    except Exception as exc:
        log(f"three.js: fetch failed: {exc}")
        return False


def make_pwa_icons(static_dir: Path, force: bool) -> None:
    """Placeholder app icons: concentric rings on a dark field, the shape of
    the portal's own brand mark without its art. Replace them with yours."""
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:
        log("icons: Pillow not installed, PWA icons not drawn (install pillow or add your own)")
        return
    targets = {"icon-512.png": (512, "RGBA"), "icon-512-maskable.png": (512, "RGB"),
               "icon-192.png": (192, "RGBA"), "favicon.png": (64, "RGBA")}
    if not force and all((static_dir / n).exists() for n in targets):
        log("icons: PWA icons present")
        return
    base = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((0, 0, 511, 511), radius=96, fill=(20, 17, 12, 255))
    for r, w in ((200, 10), (150, 8), (100, 6), (50, 4)):
        d.ellipse((256 - r, 256 - r, 256 + r, 256 + r), outline=(232, 168, 56, 255), width=w)
    d.ellipse((236, 236, 276, 276), fill=(232, 168, 56, 255))
    for name, (size, mode) in targets.items():
        img = base if size == 512 else base.resize((size, size), Image.LANCZOS)
        if mode == "RGB":
            flat = Image.new("RGB", img.size, (20, 17, 12))
            flat.paste(img, mask=img.split()[3])
            img = flat
        img.save(static_dir / name, format="PNG", optimize=True)
    log("icons: placeholder PWA icons drawn (replace with your own)")


def build_markers(export_path: Path, out: Path) -> None:
    """Raw rows from scripts/dune-markers-export.py -> the four compact
    snapshots the maps render. Names come from the DisplayName payload via the
    same parser the enrichment script uses."""
    spec = importlib.util.spec_from_file_location("bms", HERE / "build-markers-snapshot.py")
    bms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bms)  # type: ignore[union-attr]
    rows = json.loads(export_path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    per_map: dict[int, list] = {m: [] for m in SNAPSHOTS}
    for r in rows:
        try:
            m = int(r["map"])
            t = str(r["t"])
            x = int(round(float(r["x"])))
            y = int(round(float(r["y"])))
        except (KeyError, TypeError, ValueError):
            continue
        if m not in per_map or t == "NoIcon":
            continue
        mk = {"t": t, "x": x, "y": y}
        n = bms.friendly_name(r.get("dn"))
        if n:
            mk["n"] = n
        per_map[m].append(mk)
    for m, fn in SNAPSHOTS.items():
        markers = per_map[m]
        if not markers:
            log(f"markers: map {m} has no rows in the export, {fn} not written")
            continue
        (out / "data" / fn).write_text(
            json.dumps({"updated_utc": now, "markers": markers}, separators=(",", ":")) + "\n",
            encoding="utf-8")
        named = sum(1 for mk in markers if "n" in mk)
        log(f"markers: {fn}: {len(markers)} markers, {named} named")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=HERE.parent / "admin-backend",
                    help="admin-backend directory to write into (default: this checkout's)")
    ap.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "lastsietch-portal-assets" / "items.json",
                    help="where the wiki item list is cached")
    ap.add_argument("--skip-icons", action="store_true", help="write the JSON sidecars only")
    ap.add_argument("--markers-export", type=Path, help="raw JSON from scripts/dune-markers-export.py")
    ap.add_argument("--force", action="store_true", help="re-fetch the item list and every icon")
    ap.add_argument("--workers", type=int, default=6, help="parallel icon downloads (be polite)")
    ap.add_argument("--limit", type=int, default=0, help="testing: only the first N items")
    args = ap.parse_args()

    out: Path = args.out
    if not (out / "main.py").exists():
        log(f"warning: {out} does not look like admin-backend (no main.py); writing anyway")
    items = fetch_items(args.cache, args.force)
    if args.limit:
        items = items[: args.limit]
    icons = build_data(items, out)
    if args.markers_export:
        build_markers(args.markers_export, out)
    if args.skip_icons:
        log("icons: skipped (--skip-icons)")
        return 0
    wanted = sorted(set(icons.values()) | set(MAP_GLYPHS.values()) | {FALLBACK})
    done, todo, failed = fetch_icons(wanted, out / "static" / "img" / "dune-icons", args.force, args.workers)
    prune_missing(out, set(failed))
    fetch_stat_glyphs(out / "static" / "img" / "stats", args.force)
    fetch_fonts(out / "portal-nextgen" / "static" / "fonts", args.force)
    fetch_three(out / "static" / "js" / "vendor" / "three", args.force)
    make_pwa_icons(out / "portal-nextgen" / "static", args.force)
    log(f"icons: fetched {done} of {todo}, failed {len(failed)}"
        + (f" (e.g. {', '.join(failed[:5])})" if failed else ""))
    if FALLBACK in failed:
        log("error: the fallback glyph itself could not be fetched; check network access")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
