# Classifies raw Dune player tags (dune.admin_read_player_tags, exposed by the
# relay at /dune/player/{aid}/tags) into a player-facing "Journey & exploration"
# progress summary for the portal account card. Pure + read-only; mirrors the
# stat_icons / item_durable helper style. Returns None when there is nothing to
# show so the card degrades gracefully.
#
# Tag taxonomy observed on the live GA build (one owner-controlled character, 430 tags):
#   Journey.<Arc>.<...>            story journey arcs; completion flags carry a
#                                  "Completed"/"Complete" segment
#   Exploration.POI.<Group>.<Name> discovered points of interest (one per POI)
#   BigMoments.<Name>.Complete     milestone "firsts" (Base, Bike, Stillsuit, ...)
#   DunipediaFlags.<Name>          codex / lore entries unlocked

import re

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")

# POI sub-namespace -> friendly label. Unknown groups fall back to camel-split.
_POI_GROUPS = {
    "SocialHubs": "Social hubs",
    "TradePost": "Trade posts",
    "Landmark": "Landmarks",
}


def _friendly(name: str) -> str:
    """ThePawnOfPeace -> The Pawn Of Peace; Act1 -> Act 1."""
    return _CAMEL.sub(" ", name).strip()


def _is_completion(parts) -> bool:
    # A journey arc counts as completed once any tag under it carries a
    # "Completed"/"Complete" segment (Act1.Completed, SpiceDream.Completed1,
    # Chapter3.NoctuasMessage.MessageCompleted, TheJackal.Progression.Completed).
    return any("Completed" in p or p == "Complete" for p in parts[2:])


def summarize(tags):
    """Reduce a flat tag list to a portal-card summary dict, or None if empty."""
    if not tags:
        return None

    arcs = {}          # arc name -> completed?
    poi_groups = {}    # POI group -> count
    big_moments = []   # friendly names of completed milestone moments
    codex = 0

    for t in tags:
        parts = t.split(".")
        head = parts[0]
        if head == "Journey" and len(parts) >= 2:
            arc = parts[1]
            done = arcs.get(arc, False) or _is_completion(parts)
            arcs[arc] = done
        elif head == "Exploration" and len(parts) >= 3 and parts[1] == "POI":
            grp = parts[2]
            poi_groups[grp] = poi_groups.get(grp, 0) + 1
        elif head == "BigMoments" and parts[-1] == "Complete":
            big_moments.append(_friendly(parts[1]))
        elif head == "DunipediaFlags":
            codex += 1

    completed_arcs = sorted(_friendly(a) for a, done in arcs.items() if done)
    poi_total = sum(poi_groups.values())
    poi_breakdown = [
        {"label": _POI_GROUPS.get(g, _friendly(g)), "count": c}
        for g, c in sorted(poi_groups.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    if not (completed_arcs or poi_total or big_moments or codex):
        return None

    return {
        "arcs_completed": len(completed_arcs),
        "arc_names": completed_arcs,
        "poi_total": poi_total,
        "poi_breakdown": poi_breakdown,
        "big_moments": sorted(big_moments),
        "big_moments_count": len(big_moments),
        "codex_count": codex,
    }
