"""Structured report intake — pure normalisation for the /report modal.

Kept out of the cog so it is testable without a live Discord. Everything here is
a pure function over strings the player typed.

WHY NORMALISE AT ALL. The modal can only offer free-text boxes (discord.py 2.x
modals accept TextInput and nothing else), so "portal or in-game?" comes back as
whatever the player felt like typing: "Portal", "the website", "both", "ingame",
"in game", "". A mod queue where the same answer appears six ways cannot be
filtered or counted, and asking players to match an exact spelling is a worse
trade than accepting their words and mapping them here.

🔴 NORMALISATION NEVER REJECTS. An unrecognised answer is preserved verbatim
(server) or recorded as UNKNOWN (surface), never dropped and never guessed into
a wrong bucket. A report that reached us is worth more than a tidy enum: the one
thing we must not do is silently discard the only description of a bug because
the player wrote something we did not anticipate.
"""

from __future__ import annotations

import re

SURFACE_PORTAL = "portal"
SURFACE_GAME = "game"
SURFACE_BOTH = "both"
SURFACE_UNKNOWN = "unknown"

SURFACE_LABEL = {
    SURFACE_PORTAL: "Player Portal",
    SURFACE_GAME: "In game",
    SURFACE_BOTH: "Both",
    SURFACE_UNKNOWN: "Not specified",
}

# Checked in order; "both" first because "portal and in game" contains both of
# the narrower words and would otherwise match whichever was tested first.
_BOTH = ("both", "either", "everywhere", "all of it", "portal and", "and portal",
         "game and portal", "portal and game")
_PORTAL = ("portal", "website", "web site", "site", "webpage", "web page",
           "browser", "lastsietch.com", "dashboard")
# NOT "sietch": the community itself is Last Sietch and the portal lives at
# lastsietch.com, so that token fires on portal answers too and pushed
# "lastsietch.com" into BOTH. Ambiguous words belong in neither list.
_GAME = ("game", "in-game", "ingame", "server", "client", "world",
         "hagga", "deep desert", "arrakis")

# Canonical server / sietch names, so the queue can be filtered. See
# the canonical server identity: these are THE names, and players use a
# dozen spellings of each.
_SERVERS = {
    "Habbanya (PvE)": ("habbanya", "habanya", "habbanaya", "pve", "hagga pve"),
    "Kulon-PvP": ("kulon", "pvp", "kulon pvp", "kulonpvp"),
    "Deep Desert": ("deep desert", "deepdesert", "dd", "the deep"),
}

_WS = re.compile(r"\s+")


def _tidy(raw: str | None, limit: int = 200) -> str:
    """Collapse whitespace and trim. Discord already strips most of the mess but
    a paste out of the game chat can carry newlines and doubled spaces."""
    if not raw:
        return ""
    return _WS.sub(" ", str(raw)).strip()[:limit]


def normalise_surface(raw: str | None) -> str:
    """'the website' -> portal. Unrecognised -> UNKNOWN, never a guess."""
    text = _tidy(raw).lower()
    if not text:
        return SURFACE_UNKNOWN
    for token in _BOTH:
        if token in text:
            return SURFACE_BOTH
    hit_portal = any(t in text for t in _PORTAL)
    hit_game = any(t in text for t in _GAME)
    if hit_portal and hit_game:
        return SURFACE_BOTH
    if hit_portal:
        return SURFACE_PORTAL
    if hit_game:
        return SURFACE_GAME
    return SURFACE_UNKNOWN


def normalise_server(raw: str | None) -> str:
    """Map to a canonical sietch name when recognisable, else keep the player's
    own words. An unrecognised map is still useful to a mod reading the ticket;
    blanking it would destroy the only location we have."""
    text = _tidy(raw, limit=120)
    if not text:
        return ""
    low = text.lower()
    for canonical, aliases in _SERVERS.items():
        if any(a in low for a in aliases):
            return canonical
    return text


def normalise_ingame_name(raw: str | None) -> str:
    """Strip the decorations players add: a leading @, surrounding quotes, and
    the "IGN:" / "name:" prefixes the prompt itself invites."""
    text = _tidy(raw, limit=80)
    text = re.sub(r"^(ign|in.?game name|name|character)\s*[:\-]\s*", "", text,
                  flags=re.IGNORECASE)
    return text.lstrip("@").strip("\"'").strip()


def shape_title(kind: str, summary: str, limit: int = 90) -> str:
    """One-line title for the mod queue. Falls back to a labelled placeholder
    rather than an empty string, which would render as a blank embed heading."""
    text = _tidy(summary, limit=limit * 2)
    if not text:
        return f"{'Bug' if kind == 'bug' else 'Feature request'} (no summary given)"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def report_body(description: str, steps: str = "") -> str:
    """The ticket body: description, plus repro steps when the player gave any.
    Kept as one blob because that is what the embedder and dedup already read."""
    desc = _tidy(description, limit=3500)
    steps_t = _tidy(steps, limit=1500)
    if not steps_t:
        return desc
    return f"{desc}\n\nSteps / when it happened:\n{steps_t}"


def summarise_for_mods(ingame_name: str, surface: str, server: str) -> str:
    """The one-line context strip under a ticket embed. Only says what we were
    actually told; an unanswered field is omitted rather than shown as empty, so
    a mod can tell 'not asked' from 'asked and skipped'."""
    bits = []
    if ingame_name:
        bits.append(f"**{ingame_name}**")
    if surface and surface != SURFACE_UNKNOWN:
        bits.append(SURFACE_LABEL.get(surface, surface))
    if server:
        bits.append(server)
    return " · ".join(bits)
