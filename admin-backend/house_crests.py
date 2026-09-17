"""House + faction crest lookup for the Landsraad board and account card.

Resolves a raw house name (e.g. `DA_HouseNovebruns`) to a crest image basename
(e.g. `novebruns`), served from /admin/static/img/houses/<basename>.png. Faction
emblems (atreides / harkonnen) resolve the same way from /img/factions/.

The sidecar data/dune-crests.json carries the real game crests (extracted from
T_UI_IconsLandsraadFactionHouse*). Join rule: strip the DA_House/DA_ prefix,
lowercase, look up in houses{} (mikkarol aliased to mikarrol). A missing sidecar
degrades to None so the board falls back to the monogram and never 500s.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SIDECAR = Path(__file__).parent / "data" / "dune-crests.json"

_houses: dict[str, str] = {}
_factions: dict[str, str] = {}
_loaded = False


def _strip_house_prefix(name: str) -> str:
    base = name or ""
    if base.startswith("DA_House"):
        base = base[len("DA_House"):]
    elif base.startswith("DA_"):
        base = base[len("DA_"):]
    return base.strip()


def load() -> int:
    """Load the crest sidecar once. Returns the house-entry count. Safe to call
    repeatedly; only the first call reads the file."""
    global _loaded
    if _loaded:
        return len(_houses)
    try:
        raw = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        for k, v in (raw.get("houses") or {}).items():
            _houses[str(k).lower()] = str(v)
        for k, v in (raw.get("factions") or {}).items():
            _factions[str(k).lower()] = str(v)
    except FileNotFoundError:
        logger.warning("house_crests: sidecar missing at %s (crests disabled)", _SIDECAR)
    except Exception as exc:
        logger.warning("house_crests: sidecar load failed: %s", exc)
    _loaded = True
    return len(_houses)


def crest_for(house_name: str) -> str | None:
    """Return the house crest basename for a raw house name, or None when no
    crest is mapped (caller falls back to the monogram)."""
    if not _loaded:
        load()
    base = _strip_house_prefix(house_name).lower()
    return _houses.get(base) if base else None


def faction_crest_for(faction: str) -> str | None:
    """Return the faction emblem basename (atreides|harkonnen) or None."""
    if not _loaded:
        load()
    if not faction:
        return None
    return _factions.get(faction.strip().lower())
