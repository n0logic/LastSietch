"""Stat + currency glyph lookup for the portal vitals card.

Maps a stat slug (solari, solari-bank, scrip, skill-point, intel, xp, level) to
a glyph served from /admin/static/img/stats/<slug>.png. Returns None when no
glyph file is present so the card simply omits the icon and never renders a
broken image. Currency glyphs come from the awakening.wiki Currency textures;
HUD glyphs (skill points, intel, XP) are dropped in from game files as they
become available, with no code change needed (the scan picks them up).
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent / "static" / "img" / "stats"
_present: set[str] | None = None


def _scan() -> set[str]:
    global _present
    if _present is None:
        try:
            _present = {p.stem for p in _DIR.glob("*.png")}
        except Exception as exc:
            logger.warning("stat_icons: scan failed: %s", exc)
            _present = set()
    return _present


def icon_for(slug: str) -> str | None:
    """Return the glyph basename for a stat slug if its PNG exists, else None."""
    if not slug:
        return None
    return slug if slug in _scan() else None
