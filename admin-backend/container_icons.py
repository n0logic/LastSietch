# Maps a storage container or vehicle to its tile icon basename, served from
# /admin/static/img/containers/<name>.png. Returns None when nothing is mapped
# or the PNG is absent, so the tile falls back to its text label. Mirrors the
# stat_icons / item_icons helper style. Icons sourced from awakening.wiki +
# DunePakRE client-pak extraction (full-color renders).

import os

_DIR = os.path.join(os.path.dirname(__file__), "static", "img", "containers")

# Placeable building_type (and the synthetic CHOAMBank) -> icon basename.
# In-game build-menu names confirmed 2026-06-04: SpiceSilo = "Small Storage
# Container" (no spice silo exists in-game), GenericContainer = "Chest".
_CONTAINER = {
    "SpiceSilo_Placeable": "container-small",
    "GenericContainer_Placeable": "container-chest",
    "StorageContainer_Placeable": "container-storage",
    "MediumStorageContainer_Placeable": "container-medium",
    "CHOAMBank": "container-bank",
}

# Vehicle BP class -> icon basename.
_VEHICLE = {
    "BP_Buggy_CHOAM_C": "vehicle-buggy",
    "BP_Sandbike_CHOAM_C": "vehicle-sandbike",
    "BP_SandCrawler_CHOAM_C": "vehicle-sandcrawler",
    "BP_LightOrnithopter_Choam_C": "vehicle-scout",
    "BP_MediumOrnithopter_CHOAM_C": "vehicle-assault",
    "BP_TransportOrnithopter_CHOAM_C": "vehicle-carrier",
    "BP_ContainerVehicle_C": "vehicle-container",
}


def _exists(name: str) -> bool:
    return bool(name) and os.path.isfile(os.path.join(_DIR, name + ".png"))


def icon_for(class_field: str):
    """Resolve the tile icon basename for a portal container 'class' value: a
    placeable building_type, 'CHOAMBank', or 'Vehicle:<BP class>'. None if no
    icon is mapped or the file is missing."""
    if not class_field:
        return None
    if class_field.startswith("Vehicle:"):
        name = _VEHICLE.get(class_field.split(":", 1)[1])
    else:
        name = _CONTAINER.get(class_field)
    return name if _exists(name) else None
