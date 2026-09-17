"""Item-icon lookup for Dune template_ids.

Resolves a player-facing template_id (e.g. `MagnetiteOre`) to an extracted
client-pak icon basename (e.g. `T_UI_IconResourceMagnetite_D`), served from
/admin/static/img/dune-icons/<basename>.png.

The sidecar data/dune-item-icons.json is built by
scripts/build-item-icon-sidecar.py from the DunePakRE extraction. Lookup is
case-insensitive O(1), mirroring name_lookups.py. A missing/unreadable sidecar
degrades to the unknown-item fallback so the portal never 500s on icons.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SIDECAR = Path(__file__).parent / "data" / "dune-item-icons.json"

# Funcom's generic "unknown item" glyph — shipped alongside the mapped icons by
# the builder so the UI always has something to render.
FALLBACK_ICON = "T_UI_IconItemUnknownS_D"

_icons: dict[str, str] = {}
_loaded = False


def load() -> int:
    """Load the icon sidecar once. Returns the entry count. Safe to call
    repeatedly; only the first call reads the file."""
    global _loaded
    if _loaded:
        return len(_icons)
    try:
        raw = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _icons.update({str(k).lower(): str(v) for k, v in raw.items()})
    except FileNotFoundError:
        logger.warning("item_icons: sidecar missing at %s (icons disabled)", _SIDECAR)
    except Exception as exc:
        logger.warning("item_icons: sidecar load failed: %s", exc)
    _loaded = True
    return len(_icons)


def icon_for(template_id: str) -> str:
    """Return the icon basename for a template_id, or the unknown-item
    fallback. Never returns None, so templates can render unconditionally."""
    if not _loaded:
        load()
    if template_id:
        hit = _icons.get(template_id.lower())
        if hit:
            return hit
    return FALLBACK_ICON
