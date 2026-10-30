"""The v2 admin shell (H0): which chrome a request gets, and what it contains.

Presentation only. Nothing here reads or writes game state.

The shell is dark-launched. `LASTSIETCH_ADMIN_SHELL_NEXT_ENABLED` (default off, read
through feature_flags like every other gate) turns the rail on for everyone. The
owner can look at it first, the way the portal's `?shell=next` works:
`?shell=next` / `?shell=old` on any /v2 page sets a cookie for this browser, and
is honoured ONLY for the owner login (users.id == 1). Anyone else asking for it
gets whatever the flag says.

The nav rule (owner, 2026-10-30): no dropdowns, no fly-outs. A rail item is a
plain link to the section's landing page; the section's other pages are tabs
across the top of the page. Every tab is its own URL, so a deep link keeps its
tab. The same rule drives the phone tab bar.
"""
import logging
import re

import feature_flags

FLAG = "LASTSIETCH_ADMIN_SHELL_NEXT_ENABLED"
COOKIE = "ls_admin_shell"
OWNER_USER_ID = 1

# key, label, icon id (see _includes/shell_icons.html), landing href, tabs.
# The landing page is always the first tab.
_GROUPS = [
    ("today", "Today", "today", "/admin/v2/overview", [
        ("Overview", "/admin/v2/overview"),
    ]),
    ("players", "Players", "players", "/admin/v2/players/search", [
        ("Search", "/admin/v2/players/search"),
        ("Grant Bench", "/admin/v2/players/grants"),
        ("Bans", "/admin/v2/players/bans"),
        ("Requests", "/admin/v2/players/requests"),
    ]),
    ("bases", "Bases", "bases", "/admin/v2/players/bases", [
        ("Bases", "/admin/v2/players/bases"),
        ("Base Vault", "/admin/v2/players/vault"),
        ("Claims", "/admin/v2/players/claims"),
    ]),
    ("events", "Events", "events", "/admin/v2/players/events", [
        ("Events", "/admin/v2/players/events"),
    ]),
    ("economy", "Economy", "economy", "/admin/v2/server/market", [
        ("Market bot", "/admin/v2/server/market"),
        ("History", "/admin/v2/economy"),
        ("Karum", "/admin/v2/portal/karum"),
    ]),
    ("world", "World", "world", "/admin/v2/world/map", [
        ("Live Map", "/admin/v2/world/map"),
        ("CVars", "/admin/v2/server/cvars"),
    ]),
    ("community", "Community", "community", "/admin/v2/social", [
        ("Social", "/admin/v2/social"),
        ("Chat", "/admin/v2/server/chat"),
        ("Discord links", "/admin/v2/portal"),
    ]),
    ("system", "System", "system", "/admin/v2/systems", [
        ("Health", "/admin/v2/systems"),
        ("Monitor", "/admin/v2/server/monitor"),
        ("Audit", "/admin/v2/audit"),
        ("Users", "/admin/users"),
        ("RCON", "/admin/rcon"),
        ("Solido previews", "/admin/dashboard/dune/solido-thumbs"),
    ]),
]

GROUP_KEYS = tuple(g[0] for g in _GROUPS)

# Request path (prefix already stripped) -> group. Longest prefix wins, so
# /v2/portal/karum lands in Economy while /v2/portal stays in Community. Anything
# under /v2/players that is not named here is a player drilldown (Players).
_PATH_RULES = [
    ("/v2/overview", "today"),
    ("/v2/players/bases", "bases"), ("/v2/players/vault", "bases"),
    ("/v2/players/claims", "bases"),
    ("/v2/players/events", "events"),
    ("/v2/players", "players"), ("/v2/player", "players"),
    ("/v2/server/market", "economy"), ("/v2/economy", "economy"),
    ("/v2/portal/karum", "economy"),
    ("/v2/world", "world"), ("/v2/server/cvars", "world"),
    ("/v2/social", "community"), ("/v2/server/chat", "community"),
    ("/v2/portal", "community"),
    ("/v2/systems", "system"), ("/v2/server", "system"), ("/v2/audit", "system"),
]
_DRILLDOWN = re.compile(r"^/v2/players/(?!search|grants|bans|requests|bases|vault|claims|events)[^/]+")


def _strip(path: str) -> str:
    path = path or ""
    return path[6:] if path.startswith("/admin/") else path


def group_for(path: str) -> str:
    path = _strip(path)
    if path in ("/v2", "/v2/"):
        return "today"
    best = None
    for prefix, key in _PATH_RULES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, key)
    return best[1] if best else ""


def resolve_choice(flag_on: bool, query=None, cookie=None, is_owner=False) -> bool:
    """query beats cookie, both beat the flag, and both only count for the owner."""
    if is_owner:
        for value in (query, cookie):
            if value == "next":
                return True
            if value == "old":
                return False
    return bool(flag_on)


def flag_enabled() -> bool:
    return feature_flags.enabled(FLAG, "0")


def build(path: str) -> dict:
    """Everything the template needs: the eight rail groups, the active one, its
    tabs (active flagged by path) and its banner."""
    p = _strip(path)
    active = group_for(p)
    groups = []
    tabs = []
    for key, label, icon, href, group_tabs in _GROUPS:
        groups.append({"key": key, "label": label, "icon": icon, "href": href,
                       "active": key == active})
        if key != active:
            continue
        for tlabel, thref in group_tabs:
            tp = _strip(thref)
            on = p == tp or p.startswith(tp + "/") or (key == "today" and p in ("/v2", "/v2/"))
            tabs.append({"label": tlabel, "href": thref, "active": on})
        if key == "players" and _DRILLDOWN.match(p):
            tabs.append({"label": "Drilldown", "href": "/admin" + p, "active": True})
    pages = [{"label": g[1] + ": " + t[0], "href": t[1]} for g in _GROUPS for t in g[4]]
    return {"groups": groups, "active": active, "tabs": tabs, "pages": pages,
            "banner": "/admin/static/img/banners/%s.jpg" % (active or "today")}


def make_middleware(get_current_user):
    """The HTTP middleware main.py registers. Decides, per /v2 request, whether the
    shell draws and leaves the answer on request.state. Fails closed: any error
    leaves the shipped chrome in place. `get_current_user` is auth's, passed in
    so this module stays importable without the auth stack."""

    async def admin_shell_state(request, call_next):
        set_cookie = None
        try:
            path = request.url.path
            if _strip(path).startswith("/v2"):
                query = request.query_params.get("shell")
                cookie = request.cookies.get(COOKIE)
                owner = False
                if query in ("next", "old") or cookie in ("next", "old"):
                    try:
                        user = get_current_user(request)
                        owner = user["role"] == "admin" and int(user["id"]) == OWNER_USER_ID
                    except Exception:
                        owner = False
                on = resolve_choice(flag_enabled(), query, cookie, owner)
                if owner and query in ("next", "old"):
                    set_cookie = query
                if on:
                    request.state.admin_shell = build(path)
        except Exception:
            logging.getLogger(__name__).exception("admin shell decision failed; using the shipped chrome")
        response = await call_next(request)
        if set_cookie:
            response.set_cookie(COOKIE, set_cookie, max_age=31536000, path="/admin",
                                httponly=True, samesite="lax")
        return response

    return admin_shell_state
