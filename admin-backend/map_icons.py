"""Map-glyph lookup for dune.markers POI types.

Resolves a raw marker_type (e.g. `Shipwreck`, `Ecolab`, `RhyoliteOre`) to the
authoritative in-game minimap glyph basename (`T_UI_IconMapMarkerShipwreck_D`),
served from /admin/static/img/dune-icons/<basename>.png (shared with item icons).

These are the markers the game itself draws on its map, so they are the correct
icon for every POI type -- unlike item icons, which only resolve the handful of
marker types that happen to be items. The sidecar data/dune-map-icons.json is
built by scripts/build-map-icon-sidecar.py from the DunePakRE extraction.

Returns None when a type has no map glyph, so the caller can fall back to the
item-icon sidecar and then to a colored dot.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SIDECAR = Path(__file__).parent / "data" / "dune-map-icons.json"
_icons: dict[str, str] = {}
_loaded = False


def load() -> int:
    global _loaded
    if _loaded:
        return len(_icons)
    try:
        raw = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _icons.update({str(k).lower(): str(v) for k, v in raw.items()})
    except FileNotFoundError:
        logger.warning("map_icons: sidecar missing at %s (map glyphs disabled)", _SIDECAR)
    except Exception as exc:
        logger.warning("map_icons: sidecar load failed: %s", exc)
    _loaded = True
    return len(_icons)


def icon_for(marker_type: str) -> str | None:
    """Return the map-glyph basename for a marker_type, or None if unmapped."""
    if not _loaded:
        load()
    if marker_type:
        return _icons.get(marker_type.lower())
    return None
