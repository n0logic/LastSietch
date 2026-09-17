"""Friendly-name lookup for Dune template_ids — per-namespace sidecars.

Six sidecars under admin-backend/data/, built by
scripts/build-name-sidecars.py from the extracted pak string tables:

    dune-item-template-names.json   (ITEMS / container drill-down items)
    dune-building-names.json        (BUILDINGS / placeables + vehicles)
    dune-skill-names.json           (SKILLS / abilities/attributes/stats)
    dune-lore-names.json            (LORE_PICKUPS_AND_CONTRACTS)
    dune-progression-names.json     (PROGRESSION / keystones)
    dune-communinet-names.json      (COMMUNINET / radio channels)

Each sidecar is a flat {alias_lowercase: display_name} JSON dict. Lookup is
O(1) dict access, case-insensitive. Missing / unreadable sidecar files are
tolerated — the lookup returns None and callers fall back to the raw
template_id string.

Usage:
    from name_lookups import ITEMS, BUILDINGS, load_all
    load_all()                          # call once at startup (main.py)
    ITEMS.lookup("uniquesword_05")      # -> "Replica Pulse-sword"
    BUILDINGS.lookup("foundation")       # -> "Foundation"
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"

# Funcom uses bare PascalCase / snake_case class names as the human-facing
# string for many resource items that have no `_NAME` entry anywhere in the
# pak (e.g. IronOre, ScrapMetal, MagnetiteOre, Silicone, healthpack_channeled).
# After a dict miss we synthesize a display name by splitting the template_id
# on case + underscore boundaries — better UX than dumping the raw id.
#
# Splits we want:
#   IronOre        -> "Iron Ore"
#   ScrapMetal2    -> "Scrap Metal 2"
#   AzuriteOre     -> "Azurite Ore"
#   OldImperialComponent2 -> "Old Imperial Component 2"
#   healthpack_channeled  -> "Healthpack Channeled"
#   IronBar        -> "Iron Bar"
# Pattern: insert a space before any uppercase letter that follows a lowercase
# letter OR a digit; insert a space before any digit that follows a letter;
# then replace underscores with spaces and titlecase the result.
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def synthesize_friendly_name(template_id: str) -> str:
    """Best-effort PascalCase / snake_case -> 'Pascal Case' / 'Snake Case'.

    Used as a fallback when the pak sidecars don't have an entry for an item.
    Funcom internally uses bare class names as the player-facing string for
    raw resources (IronOre, ScrapMetal, etc.). This function makes those
    readable without round-tripping through the pak.

    Returns a string suitable for direct display. Never returns None.
    """
    if not template_id:
        return ""
    s = template_id.replace("_", " ")
    s = _CAMEL_RE.sub(" ", s)
    parts = s.split()
    return " ".join(p[:1].upper() + p[1:] for p in parts if p)


# Funcom ships dev-placeholder display strings in the pak string table for items
# they never finished naming (e.g. "XXNOTUSED_OrnithopterMediumLocomotion_6").
# Treat those as a sidecar miss so the caller falls through to a synthesized
# label instead of leaking the placeholder to players.
# Raw localization key that leaked instead of a resolved string, e.g.
# "RESOURCE_GLACIALICE_NAME". Conservative: anchored RESOURCE_..._NAME only.
_LOC_KEY_RE = re.compile(r"^RESOURCE_.*_NAME$", re.IGNORECASE)


def _is_placeholder_name(value: str) -> bool:
    v = value or ""
    # Dev-placeholder prefix "PH_" / "PH " (e.g. "PH_Glacial Ice").
    if v.startswith("PH_") or v.startswith("PH "):
        return True
    # Leaked raw loc key (e.g. "RESOURCE_GLACIALICE_NAME").
    if _LOC_KEY_RE.match(v):
        return True
    n = v.lower().replace("_", "").replace(" ", "")
    return ("notused" in n or "donotuse" in n
            or "placeholder" in n or "deprecated" in n)


class NameSidecar:
    """Single-namespace lookup wrapper around one sidecar JSON file.

    Optional curated overlay (LIFT-11 v1.1, 2026-05-25): a second JSON file
    consulted by lookup_or_synthesize() AFTER a pak-sidecar miss and BEFORE
    the camelCase synthesis fallback. Pak wins on overlap; curated fills
    pak gaps with canonical in-game names that synthesis cannot infer
    (e.g. `Stone` -> "Granite Stone", `Silicone` -> "Silicone Block").
    Build with scripts/build-curated-item-names.py.
    """

    def __init__(self, label: str, filename: str, curated_filename: str | None = None,
                 overrides_filename: str | None = None) -> None:
        self.label = label
        self.path = _DATA_DIR / filename
        self.curated_path = _DATA_DIR / curated_filename if curated_filename else None
        # Overrides WIN over the pak sidecar (unlike curated, which only fills
        # gaps). Used to correct wrong/placeholder pak names with authoritative
        # ones (e.g. from the awakening.wiki data).
        self.overrides_path = _DATA_DIR / overrides_filename if overrides_filename else None
        self._names: dict[str, str] = {}
        self._curated: dict[str, str] = {}
        self._overrides: dict[str, str] = {}

    def _load_one(self, path: Path, kind: str) -> dict[str, str]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("name_lookups[%s]: %s not found at %s (lookup empty)",
                           self.label, kind, path)
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("name_lookups[%s]: %s load failed (%s): %s",
                           self.label, kind, path, exc)
            return {}
        if not isinstance(raw, dict):
            logger.warning("name_lookups[%s]: %s root is not a dict (lookup empty)",
                           self.label, kind)
            return {}
        return {str(k).lower(): str(v) for k, v in raw.items()}

    def load(self) -> int:
        """Idempotent loader. Returns the number of pak entries loaded.
        Curated overlay (if configured) is loaded alongside; its size is
        available via curated_size()."""
        self._names = self._load_one(self.path, "sidecar")
        logger.info("name_lookups[%s]: loaded %d entries from %s",
                    self.label, len(self._names), self.path.name)
        if self.curated_path is not None:
            self._curated = self._load_one(self.curated_path, "curated overlay")
            if self._curated:
                logger.info("name_lookups[%s]: loaded %d curated entries from %s",
                            self.label, len(self._curated), self.curated_path.name)
        if self.overrides_path is not None:
            self._overrides = self._load_one(self.overrides_path, "overrides")
            if self._overrides:
                logger.info("name_lookups[%s]: loaded %d override entries from %s",
                            self.label, len(self._overrides), self.overrides_path.name)
        return len(self._names)

    def lookup(self, template_id: str) -> str | None:
        """Case-insensitive lookup. Overrides win over the pak sidecar; a
        placeholder pak value is treated as a miss. Returns None on miss —
        caller decides whether to use lookup_or_synthesize() or fall back to
        the raw template_id. Does NOT consult the curated overlay; callers who
        want curated fallback should use lookup_or_synthesize()."""
        if not template_id:
            return None
        key = template_id.lower()
        if self._overrides:
            ovr = self._overrides.get(key)
            if ovr:
                return ovr
        val = self._names.get(key)
        if val and _is_placeholder_name(val):
            return None
        return val

    def lookup_or_synthesize(self, template_id: str) -> str:
        """Four-layer lookup: overrides -> pak sidecar -> curated overlay ->
        camelCase synthesis. Never returns None. Use this in player-facing
        rendering paths where we want a readable label even for items with no
        pak entry."""
        hit = self.lookup(template_id)
        if hit:
            return hit
        if self._curated and template_id:
            curated_hit = self._curated.get(template_id.lower())
            if curated_hit:
                return curated_hit
        return synthesize_friendly_name(template_id or "")

    def size(self) -> int:
        return len(self._names)

    def curated_size(self) -> int:
        return len(self._curated)


# Module-level instances — one per namespace. Import directly:
#   from name_lookups import ITEMS, BUILDINGS, SKILLS, LORE, PROGRESSION, COMMUNINET
ITEMS = NameSidecar("items", "dune-item-template-names.json",
                    curated_filename="dune-item-template-names-curated.json",
                    overrides_filename="dune-item-name-overrides.json")
BUILDINGS = NameSidecar("buildings", "dune-building-names.json")
SKILLS = NameSidecar("skills", "dune-skill-names.json")
LORE = NameSidecar("lore", "dune-lore-names.json")
PROGRESSION = NameSidecar("progression", "dune-progression-names.json")
COMMUNINET = NameSidecar("communinet", "dune-communinet-names.json")

ALL_SIDECARS: tuple[NameSidecar, ...] = (
    ITEMS, BUILDINGS, SKILLS, LORE, PROGRESSION, COMMUNINET,
)


def load_all() -> dict[str, int]:
    """Load every sidecar; returns {label: entry_count}. Call once at
    startup from main.py lifespan — replaces the legacy
    `item_names.load_sidecar()` single-file call."""
    return {sc.label: sc.load() for sc in ALL_SIDECARS}


def total_size() -> int:
    """Total alias count across all loaded sidecars (debug / metrics)."""
    return sum(sc.size() for sc in ALL_SIDECARS)
