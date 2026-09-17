"""Top-level category classifier for CHOAM exchange items.

The in-game market organises listings under a top icon rail: Garments, Weapons,
Tools, Resources, Vehicles, Augmentations, Building. We mirror that on the portal
so players browse by category instead of scrolling one flat list.

Classification is AUTHORITATIVE-FIRST: data/dune-item-categories.json (built by
scripts/build-market-categories.py from the market bot's item-data.json, which
carries the game's real per-item `category` path) maps template_id -> tab. Items
not in that map (uniques, schematic blueprints, anything missing from
item-data.json) fall back to a heuristic stem match over the template_id.

Schematics are NOT a separate tab: a schematic is categorised by what it CRAFTS
(so a garment blueprint sits under Garments) and flagged separately via
is_schematic(); the browse UI shows a "Schematic" badge + a blueprint filter.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SIDECAR = Path(__file__).parent / "data" / "dune-item-categories.json"
# Template_ids the game flags non-tradeable (cannot be listed on the CHOAM
# exchange). Lowercase. Absence => assume listable; the sell writer's
# `category_unresolved` guard is the server-side backstop for edge cases.
_NT_SIDECAR = Path(__file__).parent / "data" / "dune-item-non-tradeable.json"

# Ordered (key, label) for the UI. "all" is the default landing tab; "other" is
# the catch-all. Order here is the tab order.
CATEGORIES = [
    ("all", "All"),
    ("garments", "Garments"),
    ("weapons", "Weapons"),
    ("tools", "Tools"),
    ("resources", "Resources"),
    ("vehicles", "Vehicles"),
    ("augmentations", "Augments"),
    ("building", "Building"),
    ("other", "Other"),
]

_VALID = {k for k, _ in CATEGORIES}

# Authoritative template_id -> tab map (lowercase keys), lazy-loaded once.
_catmap: dict[str, str] = {}
_nontradeable: set[str] = set()
_loaded = False


def load() -> int:
    """Load the authoritative category sidecar once. Safe to call repeatedly."""
    global _loaded
    if _loaded:
        return len(_catmap)
    try:
        raw = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _catmap.update({str(k).lower(): str(v) for k, v in raw.items()})
    except FileNotFoundError:
        logger.warning("market_categories: sidecar missing at %s (stems only)", _SIDECAR)
    except Exception as exc:
        logger.warning("market_categories: sidecar load failed: %s", exc)
    try:
        nt = json.loads(_NT_SIDECAR.read_text(encoding="utf-8"))
        if isinstance(nt, list):
            _nontradeable.update(str(k).lower() for k in nt)
    except FileNotFoundError:
        logger.warning("market_categories: non-tradeable sidecar missing at %s "
                       "(all items treated as listable)", _NT_SIDECAR)
    except Exception as exc:
        logger.warning("market_categories: non-tradeable sidecar load failed: %s", exc)
    _loaded = True
    return len(_catmap)


# Heuristic stem fallback for template_ids absent from item-data.json (uniques,
# blueprints). Checked in order; first match wins. NOTE: there is deliberately no
# "schematics" rule -- a blueprint is bucketed by what it crafts (its stem), and
# flagged separately by is_schematic().
_RULES = [
    ("augmentations", ("augment",)),
    ("vehicles", (
        "buggy", "sandbike", "ornitho", "orni", "sandcrawler", "groundcar",
        "carryall", "carry_all", "treadwheel", "scrambler", "assault_ship",
        "vehicle", "tread", "thruster", "chassis", "boostheat", "_wing",
        "propulsion", "vehinventory", "vehengine", "spicecontainer",
    )),
    ("building", (
        "deployable", "foundation", "_wall", "_floor", "_roof", "ceiling",
        "_ramp", "_stairs", "_pillar", "_fence", "_gate", "_door", "buildable",
        "placeable", "fabricator", "refinery", "windtrap", "silo", "generator",
        "turret", "shieldwall", "sietch_", "basepiece", "constructiontool",
        "reconstruct", "brt",
    )),
    ("tools", (
        "tool", "scanner", "cutteray", "dewreaper", "bodyfluid", "extractor",
        "bloodsack", "binocular", "decajon", "pill", "consum", "kit", "torch",
        "lamp", "beacon", "sensor", "compass", "cartograph", "welder", "drill",
        "harvester_tool", "stilltent", "medkit", "bandage", "repair",
        "healthpack", "literjon", "liter", "probe", "suspensor", "powerpack",
    )),
    ("garments", (
        "cloth", "armor", "stillsuit", "helmet", "boots", "shoes", "gloves",
        "_top", "_bottom", "chest", "legs", "hands", "feet", "hood", "mask",
        "garment", "robe", "suit", "cape", "vest", "wear", "headgear",
        "backpack", "belt", "shoulder",
    )),
    ("weapons", (
        "wpn", "weapon", "sword", "dagger", "dirk", "rapier", "pistol", "rifle",
        "smg", "lmg", "dmr", "kindjal", "blade", "dart", "ammo", "lasgun",
        "spear", "_axe", "mace", "_bow", "launcher", "sidearm", "sda", "knife",
        "flamethrower", "harkar", "choamlg", "maula", "scattergun", "carbine",
        "disruptor", "cannon", "grenade", "darts", "shotgun", "smugshot", "shot",
    )),
    ("resources", (
        "ore", "ingot", "_bar", "paste", "plastone", "fiber", "spice", "sand",
        "plant", "blossom", "resource", "fuelcell", "fuel", "oil", "water",
        "flour", "meat", "powdered", "dust", "crystal", "_gel", "component",
        "compound", "scrap", "salvage", "cobalt", "silicon", "silicone",
        "aluminium", "copper", "iron", "steel", "plastanium", "duraluminum",
        "stravidium", "jasmium", "carbon", "titanium", "basalt", "dolomite",
        "granite", "sandstone", "marble", "flagstone", "calcarite", "erratic",
        "stone", "rock", "diamondine", "agave", "blood", "cell", "module",
        "actuator", "plating", "holtzman",
    )),
]


def classify(template_id: str) -> str:
    """Return a category key (one of CATEGORIES) for a template_id. Authoritative
    item-data map first, then the stem heuristic, then 'other'."""
    if not template_id:
        return "other"
    if not _loaded:
        load()
    s = template_id.lower()
    hit = _catmap.get(s)
    if hit:
        return hit
    for key, stems in _RULES:
        for stem in stems:
            if stem in s:
                return key
    return "other"


def is_schematic(template_id: str) -> bool:
    """True if the template is a schematic/blueprint (crafting recipe) rather than
    the physical item. Every schematic template_id carries the 'schematic' stem
    (e.g. ChoamHeavyLasgunSchematic, B1C4_Unique_Dirk2_Schematic, Schematic_*)."""
    return bool(template_id) and "schematic" in template_id.lower()


def is_valid(category: str) -> bool:
    return category in _VALID


def is_tradeable(template_id: str) -> bool:
    """False only for templates the game flags non-tradeable (story/soulbound
    gear, MTX cosmetics) and so cannot be listed on the CHOAM exchange. Unknown
    templates default to True (listable); the sell writer's `category_unresolved`
    guard is the server-side backstop."""
    if not template_id:
        return True
    if not _loaded:
        load()
    return template_id.lower() not in _nontradeable
