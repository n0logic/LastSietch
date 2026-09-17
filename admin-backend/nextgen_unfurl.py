"""Per-route link-unfurl copy for the V2 portal shell.

The V2 app is an adapter-static SPA: every deep link is answered with the same
index.html, whose OpenGraph tags (src/app.html) describe the portal as a whole.
No unfurler (Discord, Slack, X) runs JS, so a link to portal.lastsietch.com/karum unfurled
as "The Last Sietch Portal" no matter where it landed. main.py rewrites the
title, description and canonical URL for the routes below before serving the
fallback; everything else (image, site name, card type) stays as app.html says.

Pure stdlib on purpose: scripts/tests/test_og_meta.py imports it without
fastapi. Opsec rule carried over from that suite: the copy names no player,
handle or link, ever.
"""
import html
import re

SITE = "https://portal.lastsietch.com/"
SERVERS = "the Last Sietch Dune: Awakening servers"

# first path segment -> (title, one-sentence description)
ROUTES = {
    "reports": ("Sietch Reports", "The Daily Dispatch and Sunday Chronicle from Last Sietch, with dated editions and recorded activity."),
    "start": ("Your first session",
              "Join Last Sietch, link your portal account, find your welcome package and meet the community."),
    "desert": ("Desert",
               f"Live traffic across every world on {SERVERS}: who is where, which maps are warm, and the boards behind each one."),
    "economy": ("Economy",
                f"The trading console for {SERVERS}: your CHOAM bank, the prices you watch, the alerts that fired and your open listings."),
    "rules": ("Server Rules",
              f"The gameplay settings {SERVERS} actually run under, read live from the servers themselves."),
    "maps": ("Maps",
             f"Live maps of the Deep Desert, Hagga Basin and the hubs for {SERVERS}: spice, storms, worms and bases."),
    "bases": ("Base Blueprints",
              f"The community blueprint market for {SERVERS}: browse bases, preview them in 3D and import them."),
    "karum": ("The Karum",
              f"Player-to-player trading for {SERVERS}: sell from your bank, buy, and post wanted orders."),
    "exchange": ("The Exchange",
                 f"CHOAM Exchange prices, price watches and alerts for {SERVERS}."),
    "landsraad": ("Landsraad",
                  f"The Landsraad term board for {SERVERS}: house standings, tasks and your own contribution."),
    "guilds": ("Guilds",
               f"Guild rosters, invites and the seeker wall for {SERVERS}."),
    "events": ("Events",
               f"What is coming up on {SERVERS}: gatherings, faction wars and maintenance windows, with the times and the maps."),
    # /chat/moderation deliberately gets no deeper match: it is a private admin
    # and guild-leader queue, and the section copy is the right answer for a
    # link to it, the same way /karum/orders/12 unfurls as The Karum.
    "chat": ("Chat",
             f"Live rooms for linked players on {SERVERS}: the sietch, your guild, your house and every map."),
    "rewards": ("Rewards",
                f"Daily and cycle rewards for linked players on {SERVERS}."),
    "solido": ("Solido",
               f"Preview and import base blueprints on {SERVERS}."),
    "workshop": ("Workshop",
                 f"The offline bench for {SERVERS}: the Ingot Refinery that trades refined ingots and Spice Melange for spiced dust, plus the augment reroll and swap tools."),
    "settings": ("Settings",
                 f"Your own settings for {SERVERS}: linked accounts, character selection, appearance and what the player directory shows."),
    "help": ("Help and what's new",
             f"How the portal works, and what changed in each release of {SERVERS} companion."),
}

# A market listing id in a /bases/<id> deep link. Gated here so a junk segment
# ("bases/latest", "bases/../etc") never reaches the lookup callback at all.
_LISTING_ID_RE = re.compile(r"^[0-9]{1,12}$")

_TAGS = (
    ("property", "og:title", "title"),
    ("name", "twitter:title", "title"),
    ("property", "og:description", "description"),
    ("name", "twitter:description", "description"),
    ("property", "og:url", "url"),
)


def _listing_census(info):
    """The one-sentence listing description: server-derived census only. The
    author name and the player's own description are deliberately absent -- this
    is a public surface (opsec rule for public surfaces) and a description is player free text."""
    parts = []
    pieces = info.get("pieces")
    if isinstance(pieces, int) and pieces > 0:
        parts.append(f"{pieces:,} piece" + ("" if pieces == 1 else "s"))
    for key in ("size_band", "faction"):
        value = str(info.get(key) or "").strip()
        if value:
            parts.append(value[:1].upper() + value[1:])
    census = (": " + ", ".join(parts)) if parts else ""
    return f"A shared base blueprint for {SERVERS}{census}."


def unfurl_for(path, map_names=None, listing_lookup=None, report_lookup=None):
    """{title, description, url} for a V2 route path ('' or 'maps/deep-desert'),
    or None when the shell's portal-wide copy is the right answer."""
    segs = [s for s in (path or "").strip("/").split("/") if s]
    if not segs:
        return None
    head = segs[0]
    if (head == "reports" and len(segs) == 2 and report_lookup
            and re.fullmatch(r"(?:daily|weekly)-[0-9]{4}-[0-9]{2}-[0-9]{2}", segs[1])):
        report = report_lookup(segs[1])
        if report:
            return {"title": str(report["title"]) + " | " + str(report["date"]),
                    "description": str(report["summary"]), "url": SITE + "reports/" + segs[1]}
    if (head == "bases" and len(segs) > 1 and listing_lookup
            and _LISTING_ID_RE.fullmatch(segs[1])):
        info = listing_lookup(segs[1]) or None
        title = str((info or {}).get("title") or "").strip()
        if title:
            return {
                "title": title,
                "description": _listing_census(info),
                "url": SITE + "bases/" + segs[1],
            }
        # missing, hidden or removed: the generic bases copy answers below.
    if head == "maps" and len(segs) > 1 and map_names and segs[1] in map_names:
        name = map_names[segs[1]]
        return {
            "title": f"{name} map",
            "description": f"The live {name} map for {SERVERS}: spice, storms, worms, bases and players.",
            "url": SITE + "/".join(segs[:2]),
        }
    # Storage itself is a private page and keeps the portal-wide copy. Its
    # Workshop subpage is the one storage surface worth describing on its own,
    # so it is matched explicitly rather than by opening every /storage/* child
    # to a ROUTES lookup.
    if head == "storage" and len(segs) > 1 and segs[1] == "workshop":
        title, description = ROUTES["workshop"]
        return {"title": title, "description": description,
                "url": SITE + "storage/workshop"}
    if head in ROUTES:
        title, description = ROUTES[head]
        return {"title": title, "description": description, "url": SITE + head}
    return None


def apply_unfurl(index_html, path, map_names=None, listing_lookup=None, report_lookup=None):
    """index.html with the unfurl tags rewritten for `path`; unchanged when the
    route has no copy of its own. Only the content attribute of the five tags
    moves; a tag that is not in the shell is not invented."""
    info = unfurl_for(path, map_names, listing_lookup, report_lookup)
    if not info:
        return index_html
    out = index_html
    for attr, key, field in _TAGS:
        value = html.escape(info[field], quote=True)
        out = re.sub(
            r'(<meta\s+%s="%s"\s+content=")[^"]*(")' % (attr, re.escape(key)),
            lambda m, v=value: m.group(1) + v + m.group(2),
            out, count=1)
    return out
