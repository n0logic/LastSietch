#!/usr/bin/env python3
"""Build the map-icon sidecar: marker_type -> authoritative in-game map glyph
(T_UI_IconMap*_D), from the DunePakRE client-pak extraction.

The portal maps plot dune.markers POIs. Most marker types are NOT items, so the
item-icon sidecar can't resolve them (Shipwreck, Cave, Ecolab, Hazard_*,
Trainer*, HouseRepresentative*, fortresses) and they fell back to colored dots.
These ARE the in-game minimap markers, so the correct icon is the T_UI_IconMap*
glyph the game itself draws. This builder maps every live marker type to its map
glyph by rule + explicit override, verifies the extracted PNG is present, writes
data/dune-map-icons.json, and copies the needed PNGs into static/img/dune-icons/
(shared with the item icons, same URL convention).

Offline only: reads the DunePakRE extraction, writes into the portal repo.
"""
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXTRACT = Path("/mnt/c/Users/the operator/Source/Security/DunePakRE/extracted/textures")
STATIC_ICONS = REPO / "admin-backend" / "static" / "img" / "dune-icons"
SIDECAR = REPO / "admin-backend" / "data" / "dune-map-icons.json"
SNAPSHOTS = REPO / "admin-backend" / "data"

PFX = "T_UI_IconMap"  # full basename = PFX + <glyph> + "_D"

# Explicit marker_type -> glyph stem (without the T_UI_IconMap prefix / _D suffix).
OVERRIDES = {
    # salvage / wrecks
    "ScrapMetalPart": "MarkerSalvageScrap", "ScrapMetalWreckage": "MarkerSalvageScrap",
    "FuelCellPart": "MarkerSalvageFuel",    "FuelCellWreckage": "MarkerSalvageFuel",
    "Shipwreck": "MarkerShipwreck",
    # flora
    "BrittleBush": "PlantFiber", "PrimroseField": "PlantFiber", "SaguaroSeed": "MarkerSaguaro",
    # POIs / landmarks
    "Cave": "MarkerCave", "EnemyCamp": "MarkerCamp", "EnemyOutpost": "MarkerOutpost",
    "EnemyLaborOutpost": "MarkerLabor", "Ecolab": "MarkerEcolab",
    "ExplorationPointOfInterest": "MarkerPointofinterest", "Sietch": "LocationCity",
    "TradingPost": "MarkerTradingPost", "TaxiService": "VendorTaxi",
    "AtreidesFortress": "MarkerBaseFortressBG", "HarkonnenFortress": "MarkerBaseFortressBG",
    "ControlPointHouseTseida": "MarkerObjective",
    # hazards
    "Hazard_Quicksand": "MarkerEnvQuicksandHatching",
    "Hazard_Drumsand": "MarkerEnvDrumsandHatching",
    "Hazard_Radiation": "MarkerEnvRadiation",
    # trainers (mentor glyphs)
    "TrainerSwordmaster": "CharacterMentorSwordmaster", "TrainerMentat": "CharacterMentorMentat",
    "TrainerBeneGesserit": "CharacterMentorBeneGesserit",
    "TrainerPlanetologist": "CharacterMentorPlanetologist",
    "TrainerTrooper": "CharacterMentorTrooper",
}

# Mineral nodes: <Material>{Ore,Pickup,Rock} -> MarkerMineral<glyph>. Glyph stem
# differs from material for two (Stravidium/Titanium carry the "Ore" suffix in the
# texture name).
MINERAL_GLYPH = {
    "Azurite": "MarkerMineralAzurite", "Basalt": "MarkerMineralBasalt",
    "Bauxite": "MarkerMineralBauxite", "Dolomite": "MarkerMineralDolomite",
    "Erythrite": "MarkerMineralErythrite", "Jasmium": "MarkerMineralJasmium",
    "Magnetite": "MarkerMineralMagnetite", "Rhyolite": "MarkerMineralRhyolite",
    "Stravidium": "MarkerMineralStravidiumOre", "Titanium": "MarkerMineralTitaniumOre",
}
MINERAL_SUFFIXES = ("Ore", "Pickup", "Rock")


def glyph_for(t: str) -> str | None:
    if t in OVERRIDES:
        return OVERRIDES[t]
    if t.startswith("HouseRepresentative"):
        return "CharacterLandsraad"
    for suf in MINERAL_SUFFIXES:
        if t.endswith(suf) and t[: -len(suf)] in MINERAL_GLYPH:
            return MINERAL_GLYPH[t[: -len(suf)]]
    return None


def find_png(basename: str) -> Path | None:
    hits = list(EXTRACT.rglob(f"{basename}.png"))
    return hits[0] if hits else None


def main():
    types = set()
    for snap in ("dd-markers-snapshot.json", "hagga-markers-snapshot.json"):
        d = json.loads((SNAPSHOTS / snap).read_text())
        for mk in d.get("markers", []):
            if mk.get("t"):
                types.add(mk["t"])
    types.discard("SurveyPoint")

    sidecar: dict[str, str] = {}
    copied, missing_png, unmapped = [], [], []
    STATIC_ICONS.mkdir(parents=True, exist_ok=True)

    for t in sorted(types):
        glyph = glyph_for(t)
        if not glyph:
            unmapped.append(t)
            continue
        basename = f"{PFX}{glyph}_D"
        png = find_png(basename)
        if not png:
            missing_png.append((t, basename))
            continue
        dest = STATIC_ICONS / f"{basename}.png"
        if not dest.exists():
            shutil.copy2(png, dest)
            copied.append(basename)
        sidecar[t.lower()] = basename

    SIDECAR.write_text(json.dumps(dict(sorted(sidecar.items())), indent=1) + "\n")

    print(f"types (excl SurveyPoint): {len(types)}")
    print(f"mapped:   {len(sidecar)}")
    print(f"copied PNGs (new): {len(copied)}")
    print(f"unmapped: {len(unmapped)} -> {unmapped}")
    print(f"missing PNG: {len(missing_png)} -> {missing_png}")
    print(f"sidecar: {SIDECAR}")


if __name__ == "__main__":
    main()
