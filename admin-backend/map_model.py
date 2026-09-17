"""Unified Deep Desert / Hagga Basin map model for the public portal.

Both maps' static layers (caves, wrecks, ore nodes, ecolabs, enemy camps, sietches,
...) live in OUR game DB: dune.markers, dimension_index=-1 (the shared static layer;
the same terrain underlies a map's PvE and PvP instances). The marker composite
carries (marker_type, x, y, z) in world units, so every POI plots at its true
position -- no scraping, no screenshots. We own the server, so this is authoritative.

One engine, a per-map registry (calibration + backdrop + instances). The render
model it emits is map-agnostic: normalized 0..1000 coordinates the JS plots over
whatever backdrop the map declares. Designed so the public site can consume the same
/portal/maps/{map}/data endpoint later (one cached backend call, no duplication).

Calibration is authoritative (ground-truthed vs in-game survey + live player
tracking + dune.gaming.tools cross-check 2026-06-09):
  Deep Desert: bounds Min=(-1270000,-1270000) Max=(1168400,1168400); 9x9 grid,
    rows A..I SOUTH->NORTH (A=south/+y, I=north/-y), cols 1..9 WEST->EAST. No
    terrain image (procedural per 14-day Coriolis cycle) -> stylized sand backdrop.
    Large spice candidate sites are auto-discovered per cycle from the RAM
    reader and injected by the /data route (fallback list below).
  Hagga Basin: HAGGA_CAL origin -457200 span 812800 (matches the live 8K image);
    real static terrain image HaggaBasin.webp as the backdrop. One terrain, three
    sietch instances (Habbanya / Kulon / Amtal) split by partition.
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA = Path(__file__).parent / "data"
VIEW = 1000.0           # normalized viewBox the JS engine renders into

# --- per-map registry --------------------------------------------------------
MAPS = {
    "deep-desert": {
        "name": "Deep Desert",
        "map_name_id": 7,
        # `engine_map` is dune.actors.map — the string the engine itself stores,
        # which is what every live position/vehicle feed tags a row with. The
        # registry key is ours and map_name_id is the numeric proc argument;
        # neither one joins to a live row, so consumers that route live actors
        # onto a map need this third name.
        "engine_map": "DeepDesert",
        "snapshot": "dd-markers-snapshot.json",
        # cal: world -> normalized. nx=(x-originX)/spanX*VIEW, ny likewise.
        "cal": {"originX": -1270000.0, "spanX": 2438400.0,
                "originY": -1270000.0, "spanY": 2438400.0, "flipY": False},
        "grid": {"rows": 9, "cols": 9, "row_letters": "ABCDEFGHI",
                 "row_top_is": "I"},   # I (north) at top, A (south) at bottom
        "backdrop": {"type": "sand"},  # fallback until the Coriolis layout is known
        # Baked rock-island backdrops, one per Coriolis layout (ops/dd-terrain-bake/
        # bake_islands.py). portal_map_data swaps `backdrop` to this image when the
        # layout id is known (matcher or owner override). Bump ?v= on every re-bake.
        "backdrop_layouts": {"template": "/admin/static/img/maps/dd-islands/dd-layout-{id}.webp?v=20260902a"},
        "card_image": "/admin/static/img/dd-map-card.webp",  # hub card art
        "instances": [{"key": "pve", "label": "PvE", "dim": 0},
                      {"key": "pvp", "label": "PvP", "dim": 1}],
        "has_spice": True,
        # `has_storms` is deliberately SEPARATE from has_spice: sandstorms and
        # spice are unrelated systems that merely happened to coexist on DD only.
        # Hagga storms (tracked since 2026-08-27) are the counter-example.
        "has_storms": True,
        # FALLBACK ONLY. The real per-cycle Large candidate sites are auto-
        # discovered from the live RAM reader (USpiceHarvestingSystem array) and
        # injected by the /portal/maps/{map}/data route; this list is used only
        # when that live read is unavailable. Kept current as a sane default
        # (this cycle = F5/F7/I2/I5/I9, RAM-authoritative post the 2026-06-16
        # Coriolis reset). The live overlay marks which one is currently erupting.
        "spice_candidates": ["F5", "F7", "I2", "I5", "I9"],
    },
    "hagga": {
        "name": "Hagga Basin",
        "map_name_id": 11,
        "engine_map": "HaggaBasin",
        "snapshot": "hagga-markers-snapshot.json",
        "cal": {"originX": -457200.0, "spanX": 812800.0,
                "originY": -457200.0, "spanY": 812800.0, "flipY": False},
        "grid": None,                  # open world, no survey grid
        "backdrop": {"type": "image", "src": "/assets/HaggaBasin.webp",
                     "w": 8192, "h": 8192},
        # Three live Hagga sietches share the same terrain + static markers
        # (dimension_index=-1), split by partition_id: 1=Habbanya (PvE),
        # 32=Kulon (PvP), 33=Amtal (full PvP; browser name "Amtal-Full-PvP").
        # `part` is the discriminator the /me self-marker and the public
        # /api/dune/positions player layer filter on (positions carries only
        # `p`=partition, so partition is the ground truth). `dim` is the real
        # dune.actors.dimension_index, not a token: verified live 2026-08-27,
        # partition 1 -> dim 0, 32 -> dim 1, 33 -> dim 2, with no row breaking
        # the pairing. The V1 map JS has no `part` and filters its me-layer on
        # `dim` alone, so that correspondence is what keeps V1 correct.
        "instances": [
            {"key": "habbanya", "label": "Habbanya", "mode": "PvE",
             "dim": 0, "part": 1},
            {"key": "kulon", "label": "Kulon", "mode": "PvP",
             "dim": 1, "part": 32},
            {"key": "amtal", "label": "Amtal", "mode": "Full PvP",
             "dim": 2, "part": 33},
        ],
        "has_spice": False,
        # All three sietches storm independently (Amtal's autospawn is off today,
        # so its layer stays empty until it is re-enabled -- no code change needed).
        "has_storms": True,
        "spice_candidates": [],
    },
    # --- Social hubs (2026-06-12). The in-game hub maps are realtime 3D mesh
    # dioramas, not flat textures, so there is no drop-in backdrop image — the
    # AO ortho bakes do NOT align to marker space and serve as hub-card art
    # only. Authoritative world bounds (DA_<Hub>_FullscreenMapData) are the full
    # ±56750 streaming square, but the walkable hub is a small central island,
    # so the cal below is a tighter VIEW WINDOW around the marker cloud
    # (markers + live overlays stay consistent — same linear projection).
    # Detail: docs/dune-research/HUB-MAPS-EXTRACTION-2026-06-12.md.
    # Hub backdrops are OUR orthographic renders of the game's cartography
    # diorama meshes (the AO bakes were packed UV atlases — unalignable). The
    # cal below is the render_cal fit from 15 ground-truth anchors
    # (RMS 62px / 40px of 4096); see hub-map-cal-20260612.json phase 2.
    "arrakeen": {
        "name": "Arrakeen",
        "map_name_id": 1,
        "engine_map": "Arrakeen",
        "snapshot": "arrakeen-markers-snapshot.json",
        "cal": {"originX": -40545.7, "spanX": 79828.5,
                "originY": -41826.2, "spanY": 79828.5, "flipY": False},
        "grid": None,
        "backdrop": {"type": "image",
                     "src": "/admin/static/img/maps/arrakeen-map.webp?v=3",
                     "w": 4096, "h": 4096},
        "card_image": "/admin/static/img/maps/arrakeen-map.webp?v=3",
        "instances": [{"key": "main", "label": "Arrakeen", "dim": None}],
        "has_spice": False,
        "has_storms": False,
        "spice_candidates": [],
    },
    "harko-village": {
        "name": "Harko Village",
        "map_name_id": 9,
        "engine_map": "HarkoVillage",
        "snapshot": "harko-village-markers-snapshot.json",
        "cal": {"originX": -36441.3, "spanX": 73064.6,
                "originY": -30198.0, "spanY": 73064.6, "flipY": False},
        "grid": None,
        "backdrop": {"type": "image",
                     "src": "/admin/static/img/maps/harko-village-map.webp?v=3",
                     "w": 4096, "h": 4096},
        "card_image": "/admin/static/img/maps/harko-village-map.webp?v=3",
        "instances": [{"key": "main", "label": "Harko Village", "dim": None}],
        "has_spice": False,
        "has_storms": False,
        "spice_candidates": [],
    },
}

# --- marker categorisation (suffix/substring rules cover both maps) -----------
# category -> (label, color, z-order). Colors read on both portal themes.
CATEGORIES = {
    "spice":   ("Spice fields", "#b15ee0", 9),   # melange purple (matches the map markers)
    "vendor":  ("Vendors & traders", "#e8c060", 8),   # hub maps
    "service": ("City services", "#6fc7d8", 7),       # hub maps
    "poi":     ("Landmarks", "#9a86e0", 6),
    "enemy":   ("Enemy", "#e0584a", 5),
    "ore":     ("Ore & minerals", "#7fb0d8", 3),
    "salvage": ("Salvage & Plants", "#c2a878", 2),
    "hazard":  ("Hazards", "#86d84a", 4),
    "district": ("Districts", "#d695c4", 1.5),        # hub maps
    "other":   ("Other", "#6f6147", 0),
}
_CAT_ORDER = sorted(CATEGORIES, key=lambda k: -CATEGORIES[k][2])

_POI_TYPES = {
    "Cave", "Ecolab", "TaxiService", "Sietch", "TradingPost", "Shipwreck",
    "ExplorationPointOfInterest", "AtreidesFortress", "HarkonnenFortress",
}

# marker types dropped from the map entirely. SurveyPoint = the admin's own survey
# markers (not a world POI). NoIcon = empty placeholder markers (no DisplayName, no
# tags, no glyph) that appear in the live re-rolled layer; they'd render as nameless
# dots, so drop them too.
_EXCLUDE = {"SurveyPoint", "NoIcon"}


# Social-hub marker types (Arrakeen / Harko Village). Authoritative category
# assignments from DT_MapLegend{Arrakeen,HarkoVillage}; see
# docs/dune-research/hub-marker-names-20260612.json.
_HUB_SERVICES = {
    "CHOAMExchange", "Bank", "ImperialConsulate",
    "ArrakeenRecustomization", "HarkoRecustomization",
}
_HUB_DISTRICTS = {
    "CentralPlaza", "DuncansDojo", "ResidencyCourtyard", "TheWarrens",
    "SalusanBull", "NeoCarthagOutskirts", "NewCourtWay", "BeastsBend",
    "TheBaronsEye", "SpireKeep", "Hannivars",
}


def classify(t: str) -> str:
    """marker_type -> category, by rule so new types fall in sensibly."""
    if t.startswith("Hazard_"):
        return "hazard"
    if t.startswith("Enemy"):
        return "enemy"
    if t in _HUB_SERVICES:
        return "service"
    if t in _HUB_DISTRICTS:
        return "district"
    if t.endswith("Vendor"):
        return "vendor"
    if t.startswith("HouseRepresentative") or t.startswith("Trainer") \
            or t.startswith("ControlPoint") or t in _POI_TYPES:
        return "poi"
    if "ScrapMetal" in t or "FuelCell" in t:
        return "salvage"
    if t.endswith("Ore") or t.endswith("Pickup") or t.endswith("Rock"):
        return "ore"
    # Plants group with salvage ("Salvage & Plants", owner 2026-07-04); the
    # standalone Flora group is gone and Shipwreck moved to Landmarks.
    if t in ("BrittleBush", "PrimroseField", "SaguaroSeed"):
        return "salvage"
    return "other"


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|_")

# In-game display names for resource-node marker types. The raw marker_type is the
# internal geology/deposit name (RhyoliteOre, AzuriteOre, ...), which is NOT what
# players see in game -- the node yields a named material. Authoritative mapping
# from awakening.wiki item data (sourced from game files; the Ore item_ids resolve
# directly, e.g. BauxiteOre -> "Aluminum Ore", and "rhyolite" is referenced only by
# the Granite Stone item). Both the big "Ore/Rock" deposit and the small "Pickup"
# surface scatter yield the same material, so both map to the material name (per
# the Dune Awakening wiki API item data).
_RESOURCE_BASE = {
    "Azurite":    "Copper Ore",
    "Bauxite":    "Aluminum Ore",
    "Dolomite":   "Carbon Ore",
    "Rhyolite":   "Granite Stone",
    "Basalt":     "Basalt Stone",
    "Stravidium": "Stravidium Mass",
    "Titanium":   "Titanium Ore",
}
# exact-type overrides (salvage + plants + anything not a clean <Material><Suffix>
# form). Plant nodes are labeled by what they yield: Brittle Bush -> Plant Fiber
# (in-game, per server admin); SaguaroSeed -> Agave Seeds (awakening.wiki
# SaguaroResourceRaw). Salvage wrecks keep a "Wreck" tag (bigger salvage node).
_TYPE_NAMES = {
    "ScrapMetalPart":     "Salvaged Metal",
    "ScrapMetalWreckage": "Salvaged Metal Wreck",
    "FuelCellPart":       "Fuel Cell",
    "FuelCellWreckage":   "Fuel Cell Wreck",
    "BrittleBush":        "Plant Fiber",
    "SaguaroSeed":        "Agave Seeds",
    # Ecolabs are "Testing Stations" in game (each has a number + hazard variant,
    # e.g. Testing Station 136 (Fire); the per-station number is not in the
    # compact {t,x,y} marker snapshot, so the group label is used).
    "Ecolab":             "Testing Station",
    # Social-hub types (authoritative names from DT_MapLegend* + string tables).
    "ArrakeenRecustomization": "Shaffat's Clinic",
    "HarkoRecustomization":    "Phrakk's Clinic",
    "CHOAMExchange":           "CHOAM Exchange",
    "Bank":                    "Guild Bank",
    "ResourceVendor":          "Scrap Trader",
    "SurvivalVendor":          "Water Seller",
    "StructureVendor":         "Base Vendor",
    "WeaponsVendor":           "Weapons Merchant",
    "SpiceVendor":             "Spice Merchant",
    "BarkeepVendor":           "Barkeep",
    "LevelExit":               "Exit",
    "SalusanBull":             "The Salusan Bull",
    "NeoCarthagOutskirts":     "Neo-Carthag Outskirts",
    "TheBaronsEye":            "The Baron's Eye",
    "BeastsBend":              "Beast's Bend",
    "Hannivars":               "Hannivar's",
    "DuncansDojo":             "Duncan's Dojo",
    "ResidencyCourtyard":      "Residency Approach",
}
_RESOURCE_SUFFIXES = ("Ore", "Pickup", "Rock")


def humanize(t: str) -> str:
    """marker_type -> the in-game display name.

    Resource nodes get their real material name (RhyoliteOre/RhyolitePickup ->
    "Granite Stone"); a few salvage types are mapped explicitly; everything else
    falls back to a camelCase split ('Hazard_Radiation' -> 'Radiation')."""
    if t in _TYPE_NAMES:
        return _TYPE_NAMES[t]
    for suf in _RESOURCE_SUFFIXES:
        if t.endswith(suf) and t[:-len(suf)] in _RESOURCE_BASE:
            return _RESOURCE_BASE[t[:-len(suf)]]
    s = t.replace("Hazard_", "")
    return _CAMEL.sub(" ", s).strip()


# --- coordinate + sector helpers --------------------------------------------
def normalize(x: float, y: float, cal: dict) -> tuple[float, float]:
    nx = (x - cal["originX"]) / cal["spanX"] * VIEW
    ny = (y - cal["originY"]) / cal["spanY"] * VIEW
    if cal.get("flipY"):
        ny = VIEW - ny
    return round(nx, 1), round(ny, 1)


def sector_for(x: float, y: float, m: dict) -> str | None:
    """9x9 survey sector for grid maps (DD); None for open-world maps (Hagga)."""
    grid = m.get("grid")
    if not grid:
        return None
    cal = m["cal"]
    span = cal["spanX"]
    minx, maxy = cal["originX"], cal["originY"] + cal["spanY"]
    col = max(1, min(9, int((x - minx) / (span / 9)) + 1))
    row = max(0, min(8, int((maxy - y) / (span / 9))))
    return f"{chr(ord('A') + row)}{col}"


# The game's actor.map string -> our map registry key (the snapshot uses
# map_name_id; live actor rows carry the human map name).
_GAME_MAP_TO_KEY = {"DeepDesert": "deep-desert", "HaggaBasin": "hagga",
                    "Arrakeen": "arrakeen", "HarkoVillage": "harko-village"}


def game_map_to_key(game_map: str | None) -> str | None:
    """'DeepDesert' -> 'deep-desert', 'HaggaBasin' -> 'hagga'; None if unmapped."""
    return _GAME_MAP_TO_KEY.get(game_map or "")


def project(map_key: str, x: float, y: float) -> tuple[float, float] | None:
    """World (x, y) -> normalized (nx, ny) in this map's 0..VIEW frame, using the
    same calibration the static markers use (so live overlays plot in lockstep).
    None for an unknown map."""
    m = MAPS.get(map_key)
    if not m:
        return None
    return normalize(float(x), float(y), m["cal"])


def load_markers(m: dict) -> dict:
    try:
        return json.loads((_DATA / m["snapshot"]).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("map_model: snapshot missing: %s", m["snapshot"])
    except Exception as exc:
        logger.warning("map_model: load failed (%s): %s", m["snapshot"], exc)
    return {}


def map_meta(map_key: str) -> dict | None:
    """Lightweight metadata for the renderer page (no marker payload)."""
    m = MAPS.get(map_key)
    if not m:
        return None
    return {
        "key": map_key, "name": m["name"], "view": int(VIEW),
        "cal": m["cal"], "grid": m["grid"], "backdrop": m["backdrop"],
        "card_image": m.get("card_image"),
        "instances": m["instances"], "has_spice": m["has_spice"],
        "has_storms": m["has_storms"],
        "spice_candidates": m["spice_candidates"],
    }


def _icon_for_type(t: str) -> str | None:
    """Return an icon basename for a marker type.

    Prefers the authoritative in-game minimap glyph (map_icons sidecar) -- these
    are the markers the game itself draws, so they resolve every POI type
    (Shipwreck, Ecolab, Hazard_*, Trainer*, minerals, ...). Falls back to the
    awakening.wiki item-icon sidecar for anything the map sidecar misses, then to
    None (the JS draws a colored dot)."""
    try:
        import map_icons
        glyph = map_icons.icon_for(t)
        if glyph:
            return glyph
    except Exception:
        pass
    try:
        import item_icons
        # Attempt a direct match (e.g. "AzuriteOre" -> icon) and a fallback via
        # the material name. item_icons.icon_for() never returns None; it returns
        # the generic unknown-item icon when unresolved, so we gate on that.
        hit = item_icons.icon_for(t)
        if hit and hit != item_icons.FALLBACK_ICON:
            return hit
        # Try the humanised name as a template_id guess (strip spaces, lowercase)
        friendly = humanize(t).replace(" ", "").lower()
        hit2 = item_icons.icon_for(friendly)
        if hit2 and hit2 != item_icons.FALLBACK_ICON:
            return hit2
    except Exception:
        pass
    return None


def build_data(map_key: str) -> dict | None:
    """Full map data for /portal/maps/{map}/data: normalized markers + legend +
    calibration. Cached; reusable by the public site. Markers are compact arrays
    [nx, ny, catIdx, typeIdx] to keep Hagga's 17k payload small.

    Extends the taxonomy to support per-TYPE client-side filtering:
      - type_index:  list of humanised type names (positional)
      - type_icons:  parallel list of icon basenames (None when no icon resolved)
      - type_cat:    parallel list of category indices (so JS can group by cat)
      - type_counts: {typeIdx -> count} for the sidebar tree
      - legend:      category-level tree with per-type children (label/count/icon)
    """
    m = MAPS.get(map_key)
    if not m:
        return None
    raw = load_markers(m).get("markers") or []
    cal = m["cal"]
    type_index: list[str] = []
    type_raw: list[str] = []           # raw marker_type (for icon lookup + identity)
    type_pos: dict[tuple, int] = {}    # (category, display-name) -> typeIdx
    type_cat: list[int] = []           # parallel to type_index: category idx
    type_counts: dict[int, int] = {}   # typeIdx -> count
    cat_counts: dict[str, int] = {}
    out: list[list] = []
    for mk in raw:
        t = mk.get("t")
        x, y = mk.get("x"), mk.get("y")
        if t is None or x is None or y is None or t in _EXCLUDE:
            continue
        # Group by (category, display-name) so variant raw types that yield the
        # same material collapse into ONE legend row + toggle. The deposit node
        # ("...Ore" / "...Rock") and the surface scatter ("...Pickup") both
        # humanize to the same material (e.g. StravidiumOre + StravidiumPickup ->
        # "Stravidium Mass"); without this they showed as two rows with split
        # counts. The marker row carries the merged typeIdx, so the client filter
        # toggles all variants of a material as one layer.
        gkey = (classify(t), humanize(t))
        ti = type_pos.get(gkey)
        if ti is None:
            ti = len(type_index)
            type_pos[gkey] = ti
            type_index.append(humanize(t))
            type_raw.append(t)
            type_cat.append(_CAT_ORDER.index(classify(t)))
        nx, ny = normalize(float(x), float(y), cal)
        # Compact marker row: [nx, ny, catIdx, typeIdx]. Named POIs (Testing
        # Stations, Caves, Fortresses, ...) carry a 5th element = the friendly name
        # from the snapshot; unnamed markers (resource/scrap nodes) stay length-4 so
        # the ~17k Hagga payload stays lean.
        name = mk.get("n")
        out.append([nx, ny, type_cat[ti], ti, name] if name
                   else [nx, ny, type_cat[ti], ti])
        cat = _CAT_ORDER[type_cat[ti]]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        type_counts[ti] = type_counts.get(ti, 0) + 1

    # Resolve icons for each distinct type once (cache-warming item_icons).
    type_icons: list[str | None] = [_icon_for_type(r) for r in type_raw]

    # Build the legend with a child-types array for the collapsible sidebar tree.
    legend = []
    for ci, key in enumerate(_CAT_ORDER):
        # Skip empty categories so e.g. the hub-only vendor/service/district
        # entries don't clutter DD/Hagga (and wilderness cats stay off hubs).
        if not cat_counts.get(key):
            continue
        if key == "spice":          # spice is a live overlay, not a static marker
            continue
        lbl, color, _z = CATEGORIES[key]
        # Collect child types for this category, sorted by descending count.
        children = []
        for ti, ti_label in enumerate(type_index):
            if type_cat[ti] != ci:
                continue
            cnt = type_counts.get(ti, 0)
            if cnt == 0:
                continue
            children.append({
                "idx": ti, "label": ti_label,
                "count": cnt, "icon": type_icons[ti],
            })
        children.sort(key=lambda c: -c["count"])
        legend.append({"idx": ci, "key": key, "label": lbl, "color": color,
                       "count": cat_counts.get(key, 0), "types": children})

    # Spice is a live overlay (not a static marker layer), but it gets its own
    # legend category so players can toggle its sub-layers. The large- and
    # medium-field layers are child types nested under it (category->type tree).
    # Overlay children use STRING ids ("spice_large"/"spice_medium") so they never
    # collide with numeric marker-type indices; the client keys its hidden-map +
    # count on them. The large count seeds from the per-cycle candidate sites
    # (server-known); the medium count is filled in client-side from the live
    # overlay payload. Both layers are now independently toggleable.
    if m["has_spice"]:
        lbl, color, _z = CATEGORIES["spice"]
        legend.insert(0, {
            "idx": _CAT_ORDER.index("spice"), "key": "spice",
            "label": lbl, "color": color, "count": 0, "overlay": True,
            "types": [
                {"idx": "spice_large", "label": "Large fields",
                 "count": len(m["spice_candidates"]), "icon": None,
                 "overlay": True},
                {"idx": "spice_medium", "label": "Medium fields",
                 "count": 0, "icon": None, "overlay": True},
            ],
        })

    # Sandstorm tracker: a live overlay with a single toggleable layer (moving
    # storm center + heading arrow + radius ring). Same string-id pattern as spice
    # so the client keys its hidden-map on it; drawStorm() guards on
    # state.hidden["sandstorm"]. Gated on has_storms -- this was has_spice until
    # 2026-08-27, which was only ever right by coincidence (DD had both) and is
    # what kept the layer off Hagga, where storms are just as real.
    if m["has_storms"]:
        legend.insert(1, {
            "idx": 900, "key": "sandstorm",
            "label": "Sandstorm", "color": "#6cc6d6", "count": 0, "overlay": True,
            "types": [
                {"idx": "sandstorm", "label": "Storm tracker",
                 "count": 0, "icon": None, "overlay": True},
            ],
        })

    return {
        "map": map_key, "name": m["name"], "view": int(VIEW), "cal": cal,
        "grid": m["grid"], "backdrop": m["backdrop"], "instances": m["instances"],
        "has_spice": m["has_spice"], "has_storms": m["has_storms"],
        "cat_index": _CAT_ORDER,
        "cat_colors": {k: CATEGORIES[k][1] for k in _CAT_ORDER},
        "type_index": type_index,
        "type_icons": type_icons,
        "type_counts": {str(k): v for k, v in type_counts.items()},
        "markers": out,
        "total": len(out),
        "legend": legend,
        "spice_candidates": m["spice_candidates"],
        "updated_utc": load_markers(m).get("updated_utc"),
    }
