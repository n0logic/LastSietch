import asyncio
import json
import logging
import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import map_model
from auth import get_current_user, require_admin
from database import get_db
from routers.dune import (
    dune_progression_levelups,
    dune_progression_snapshot,
    dune_roster,
    dune_status,
)
from routers.dune_grant import grant_recent

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Bulk fetch ceilings for the merged audit+grants view. Grants are pulled once
# per request and paginated client-side; audit_log is filtered SQL-side (user,
# action, outcome, date window), then the same filters apply post-merge so the
# grant rows, which are not in SQLite at all, obey them too.
#
# The query is local rather than routed through routers.audit.get_audit_log
# because that route clamps `limit` to 200 for its own JSON callers: asking it
# for 10000 silently returned the newest 200 rows, so every filter here only
# ever searched the tail of the table and the page said so nowhere.
AUDIT_BULK_LIMIT = 10000
GRANTS_BULK_LIMIT = 500

# Every action literal this codebase passes to auth.audit_log, grouped for the
# filter dropdown. ONE list: the page offered 28 of these while the code wrote
# 90, so two thirds of the audit trail could not be filtered for at all and
# nothing said which two thirds. scripts/tests/test_v2_audit_truth.py re-scans
# every admin-backend/**/*.py with ast and fails when this tuple and the call
# sites disagree, so a new lever cannot land without appearing here.
AUDIT_ACTION_GROUPS = (
    ("Sessions and accounts", (
        "login", "login_failed", "login_blocked", "logout", "setup",
        "change_password", "create_user", "delete_user", "change_role",
        "reset_password", "unlock_user",
    )),
    ("Server and host", (
        "vm_start", "vm_stop", "vm_reset",
        "server_start", "server_stop", "server_restart",
        "scheduled_stop", "scheduled_restart", "cancel_scheduled",
        "broadcast", "config_change", "usergroups_change", "cvar_write",
        "rcon_command", "update_started", "update_completed", "update_failed",
    )),
    ("Backups", (
        "zfs_snapshot", "zfs_restore", "zfs_delete", "zfs_schedule",
    )),
    ("Base Vault", (
        "dune_vault_history", "dune_vault_capture_requested", "dune_vault_capture",
        "dune_vault_restore_plan_requested", "dune_vault_restore_plan",
        "dune_vault_restore_apply_refused",
    )),
    ("Game levers", (
        "dune_grant", "dune_custom_grant", "dune_cart_fire", "dune_preset_fire",
        "dune_live_give_item", "dune_live_award_xp", "dune_live_refill_water",
        "dune_live_rescue_teleport", "dune_export_character",
        "dune_export_blueprint", "dune_claim_adopt", "dune_claim_backup",
        "dune_spice_toggle", "dune_chat_send", "dune_broadcast_send",
        "dune_market_service", "dune_market_policy_set",
        "dd_layout_pin", "dd_layout_clear", "feature_flag_set",
    )),
    ("Moderation", (
        "dune_kick", "dune_ban", "dune_unban",
        "takedown_blueprint", "takedown_signal", "takedown_directory",
    )),
    ("Portal: links and characters", (
        "portal_manual_link", "portal_revoke_link", "portal_unlock",
        "portal_rescue", "portal_reward_claim", "portal_character_augment",
        "portal_gift_send", "portal_gift_inbound_alert",
        "portal_event_create", "portal_event_update", "portal_event_cancel",
        "portal_event_remind_toggle",
        "portal_guild_join_invite", "portal_guild_op",
    )),
    # The repair tiers reach audit_log through a shared wrapper, so their names
    # arrive as `audit_action=` keywords rather than as a literal at the call.
    # The scan in the test follows that keyword for exactly this reason.
    ("Portal: repairs", (
        "portal_repair", "portal_repair_vehicle", "portal_repair_everything",
    )),
    ("Portal: storage and market", (
        "portal_storage_move", "portal_storage_pawn_move",
        "portal_storage_transfer", "portal_market_buy", "portal_market_sell",
        "portal_karum_list", "portal_karum_buy", "portal_karum_cancel",
        "portal_karum_request_post", "portal_karum_request_fill",
        "portal_karum_request_cancel",
        "portal_refinery_exchange",
    )),
    # Wave 11. Only the moderation half of chat writes here: sending, reading
    # and the stream are ordinary player traffic and leave no audit row, so a
    # row in this group always means somebody acted ON somebody else.
    ("Portal: chat", (
        "portal_chat_report", "portal_chat_delete", "portal_chat_mute",
        "portal_chat_unmute", "portal_chat_resolve",
    )),
    # The QA harness minting a player session over the loopback route: one row
    # per mint, actor "qa-harness", so a session that did not come from Discord
    # is always accounted for in the same ledger as everything else.
    ("Portal: QA harness", (
        "portal_qa_session",
    )),
    ("Blueprints", (
        "portal_blueprint_download", "portal_blueprint_publish",
        "portal_blueprint_unpublish", "portal_blueprint_rename",
        "portal_blueprint_thumbnail_admin", "portal_export_blueprint",
        "portal_solido_import", "solido_thumbnail_backfill",
    )),
)

AUDIT_ACTIONS = tuple(a for _label, group in AUDIT_ACTION_GROUPS for a in group)

# Five call sites across three families build their action name with an
# f-string (`karum_{action}`, `portal_market_{action}`,
# `portal_storage_{action}`), so the exact values are only knowable at runtime. They get a wildcard option each instead of being
# unreachable from the dropdown; `_action_where` turns a trailing `*` into a
# prefix match. The same test pins these prefixes against the scan.
AUDIT_ACTION_PREFIXES = ("karum_", "portal_market_", "portal_storage_")

# audit_log.timestamp is written by SQLite's datetime('now'): 'YYYY-MM-DD
# HH:MM:SS', UTC, so a plain lexicographic compare against a date is a correct
# window and the filter labels say UTC rather than implying local time.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _list_audit_users():
    """Distinct usernames seen in audit_log for the filter dropdown."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT username FROM audit_log WHERE username IS NOT NULL ORDER BY username"
    ).fetchall()
    conn.close()
    return [{"username": r["username"]} for r in rows]


def _clean_date(value: str) -> str:
    """A date filter is either YYYY-MM-DD or it is not applied. Anything else is
    dropped rather than passed to the query, so a typo narrows nothing instead
    of silently emptying the page."""
    value = (value or "").strip()
    return value if _DATE_RE.match(value) else ""


def _clean_success(value: str) -> str:
    """'1' success only, '0' failures only, '' both."""
    value = (value or "").strip()
    return value if value in ("0", "1") else ""


def _action_matches(row_action: str, wanted: str) -> bool:
    """Post-merge twin of _action_where, for the grant rows and any row that did
    not come out of SQLite."""
    row_action = row_action or ""
    if wanted.endswith("*"):
        return row_action.startswith(wanted[:-1])
    return row_action == wanted


def _action_where(wanted: str, params: list) -> str:
    """SQL for one action filter. `karum_*` is a prefix match; `_` and `%` are
    LIKE wildcards, so the prefix is escaped or `dune_ban` would also match
    `duneXban`."""
    if wanted.endswith("*"):
        prefix = wanted[:-1]
        for ch in ("\\", "%", "_"):
            prefix = prefix.replace(ch, "\\" + ch)
        params.append(prefix + "%")
        return " AND action LIKE ? ESCAPE '\\'"
    params.append(wanted)
    return " AND action = ?"


def _query_audit_rows(username: str = "", action: str = "", success: str = "",
                      date_from: str = "", date_to: str = "",
                      limit: int = AUDIT_BULK_LIMIT) -> list:
    """audit_log rows, newest first, with every filter pushed into the SQL."""
    where = "WHERE 1=1"
    params: list = []
    if username:
        where += " AND username = ?"
        params.append(username)
    if action:
        where += _action_where(action, params)
    if success in ("0", "1"):
        where += " AND success = ?"
        params.append(int(success))
    if date_from:
        where += " AND timestamp >= ?"
        params.append(date_from)
    if date_to:
        where += " AND timestamp <= ?"
        params.append(date_to + " 23:59:59")

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, user_id, username, action, target, ip_address, timestamp,"
            " details, success FROM audit_log " + where
            + " ORDER BY id DESC LIMIT ?",
            params + [int(limit)],
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _filter_qs(username: str, action: str, search: str,
               success: str = "", date_from: str = "", date_to: str = "") -> str:
    """Build the trailing `&k=v` query string used by pagination links in the
    audit fragment template (the leading `?page=N` already exists)."""
    parts = {}
    if username:
        parts["username"] = username
    if action:
        parts["action"] = action
    if search:
        parts["search"] = search
    if success:
        parts["success"] = success
    if date_from:
        parts["date_from"] = date_from
    if date_to:
        parts["date_to"] = date_to
    if not parts:
        return ""
    return "&" + urlencode(parts)


def _admin_or_redirect(request: Request):
    """For HTML page routes: 401 -> redirect to login, 403 -> redirect to /admin/."""
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


# Dashboard panel order. NOT map_model.MAPS order (which leads with Deep Desert):
# Hagga is where the population actually is, so it reads first.
_OVERVIEW_MAP_ORDER = ("hagga", "deep-desert", "arrakeen", "harko-village")


def _overview_map_panels():
    """One live-position panel per map INSTANCE, projected from map_model.

    Every panel carries its own map's calibration. Hagga's two sietches share one
    image and one cal, but Deep Desert / Arrakeen / Harko do not - a single
    HAGGA_CAL applied to all of them would silently draw every dot in the wrong
    place rather than fail, so the cal travels with the panel.

    Routing a live actor onto a panel: `engine_map` first, then the instance
    discriminator. For Hagga that is partition_id (map_model treats partition as
    ground truth there, since both sietches report dimension 0/1 but the feed's
    partition is what the public layer already filters on); for Deep Desert it is
    dimension_index; single-instance hub maps match on the map alone.
    """
    panels = []
    for key in _OVERVIEW_MAP_ORDER:
        m = map_model.MAPS.get(key)
        if not m:
            continue
        for inst in m["instances"]:
            bits = [b for b in (inst.get("mode"),
                                f"partition {inst['part']}" if inst.get("part") else None,
                                f"dim {inst['dim']}" if inst.get("part") is None
                                and inst.get("dim") is not None else None) if b]
            panels.append({
                "id": f"{key}-{inst['key']}",
                "map_key": key,
                # The pair World & Maps takes on its query string. `id` is a DOM
                # id and concatenates the two; the deep link needs them apart.
                "instance": inst["key"],
                "map_name": m["name"],
                "engine_map": m["engine_map"],
                "label": inst["label"],
                "sub": " · ".join(bits),
                "dim": inst.get("dim"),
                "part": inst.get("part"),
                "cal": m["cal"],
                "backdrop": m["backdrop"],
                "grid": m["grid"],
                # The vehicle feed covers every map since 2026-07-27 and tags
                # each row with `m` + `d`, so every panel can draw its own.
                "vehicles": True,
            })
    return panels


async def _safe_call(coro):
    """Run a router coroutine; swallow HTTPException so first-paint never crashes."""
    try:
        return await coro
    except HTTPException as exc:
        logger.warning("_safe_call: HTTPException %s: %s", exc.status_code, exc.detail)
        return None
    except Exception:
        logger.warning("_safe_call: unexpected exception", exc_info=True)
        return None


def _feed_ok(payload) -> bool:
    """Did this panel's source actually answer?

    _safe_call returns None when the handler raised, and every dune_* handler
    returns available=False when the relay is down. Either way the number behind
    it is not a measurement.
    """
    if not isinstance(payload, dict):
        return False
    return payload.get("available", True) is not False


def _stat(value, payload):
    """One Overview number plus whether its source answered.

    Coercing a missing panel to 0 painted "0 active players, 0 characters, 0
    level-ups, 0 grants" during a relay outage, which reads as a dead server
    rather than a dead feed. An unreachable source and a real zero are different
    facts and the card now says which one it is holding. `stale` rides along:
    the progression handlers serve their last cache when the relay is down, and
    a cached number is true-but-old, not wrong.
    """
    ok = _feed_ok(payload)
    return {
        "value": value if ok else None,
        "available": ok,
        "stale": bool(ok and isinstance(payload, dict) and payload.get("stale")),
    }


# --- Overview routes ---

@router.get("/v2/")
async def v2_root(request: Request):
    return RedirectResponse(url="/admin/v2/overview", status_code=302)


# --- Server-scoped pages still served from here ---
#
# /v2/server + /v2/server/monitor are owned by routers/v2_monitor.py (VC2 P1:
# render server.html with the Monitor body as default landing). The Settings and
# Updates placeholder routes that used to live here were deleted 2026-09-03: a
# page whose only content is a promise is not a page. Settings is the v1
# settings.html, linked from the Admin tab; Updates stays CLI-only.


@router.get("/v2/server/chat")
async def v2_server_chat(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "v2/chat.html",
        {
            "user": user,
            "current_tab": "server",
            "current_sub_tab": "chat",
            "title": "Chat",
        },
    )


@router.get("/v2/server/market")
async def v2_server_market(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "v2/market.html",
        {
            "user": user,
            "current_tab": "server",
            "current_sub_tab": "market",
            "title": "Market bot",
        },
    )


# The /v2/cvars* back-compat redirects lived here behind a 90-day quiet period
# that expired 2026-08-25; deleted 2026-09-03. /v2/server/cvars is the route.


# --- Players hub (VC1) ---

@router.get("/v2/players")
async def v2_players_root(request: Request):
    return RedirectResponse(url="/admin/v2/players/search", status_code=302)


# NOTE: /v2/players/bases is now served by the real catalog route in
# v2_player.py (Base-backup sources UI, 2026-05-31). The old _placeholder
# route lived here; removed so the real route (registered later) is reached.


# --- /v2/player-tools and /v2/player/{id} back-compat redirects ---

@router.get("/v2/player-tools")
async def v2_player_tools_compat(request: Request):
    qs = ("?" + str(request.url.query)) if request.url.query else ""
    return RedirectResponse(url=f"/admin/v2/players/search{qs}", status_code=302)


@router.get("/v2/player/{account_id}")
async def v2_player_drilldown_compat(request: Request, account_id: str):
    qs = ("?" + str(request.url.query)) if request.url.query else ""
    return RedirectResponse(url=f"/admin/v2/players/{account_id}{qs}", status_code=302)


# --- Portal admin (VC1 stub; real admin in VC6) ---

@router.get("/v2/portal")
async def v2_portal(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "v2/portal.html",
        {
            "user": user,
            "current_tab": "portal",
            "current_sub_tab": None,
            "title": "Discord links",
        },
    )


@router.get("/v2/overview")
async def v2_overview(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    # VC0 perf: fan the 5 panel queries out concurrently with asyncio.gather.
    # Each underlying handler is a SSH dispatch (or DB query) of ~0.5-1.5s;
    # serial they'd add up to ~5s. Parallel they finish in the slowest one.
    # _safe_call wraps each so a single panel failure doesn't kill the page.
    roster, status, snapshot, levelups, grants = await asyncio.gather(
        _safe_call(dune_roster(request)),
        _safe_call(dune_status(request)),
        _safe_call(dune_progression_snapshot(request)),
        _safe_call(dune_progression_levelups(request, limit=200)),
        _safe_call(grant_recent(request, limit=100)),
    )

    initial_roster = _roster_rows(roster)

    # Active players reads from two sources; it counts as measured if EITHER of
    # them answered, and the larger number wins (the roster only sees mapped
    # players, the battlegroup total sees everyone).
    active_players = len(initial_roster) if _feed_ok(roster) else None
    if _feed_ok(status) and isinstance(status.get("instances"), dict):
        ip_players = status["instances"].get("players")
        if isinstance(ip_players, int) and (active_players is None
                                            or ip_players > active_players):
            active_players = ip_players

    l200_club = None
    if _feed_ok(snapshot):
        l200_club = sum(1 for p in (snapshot.get("players") or [])
                        if (p.get("lvl") or 0) >= 200)

    initial_stats = {
        "active_players": {
            "value": active_players,
            "available": active_players is not None,
            "stale": bool(_feed_ok(roster) and roster.get("stale")),
        },
        "l200_club": _stat(l200_club, snapshot),
        "total_chars": _stat(snapshot.get("count") if _feed_ok(snapshot) else None,
                             snapshot),
        "levelups_24h": _stat(len(levelups.get("levelups") or [])
                              if _feed_ok(levelups) else None, levelups),
        "grants_24h": _stat(len(grants.get("grants") or [])
                            if _feed_ok(grants) else None, grants),
    }

    # VC1 (a): DLC pod stability indicator. The Lost Harvest DLC pods crashlooped
    # pre-hotfix-1.4.0.2; the v2-banner__dlc-warning pill surfaces any current
    # ready/alive false on LostHarvest* farm_state rows. Point-in-time only -
    # rate-based detection is a follow-up that needs a samples table.
    dlc_warning = None
    if status and isinstance(status.get("maps"), list):
        unstable = []
        for m in status["maps"]:
            name = (m.get("map") or "")
            if name.startswith("LostHarvest"):
                ready = m.get("ready")
                alive = m.get("alive")
                if ready is False or alive is False:
                    unstable.append(name)
        if unstable:
            dlc_warning = {"unstable_maps": unstable, "count": len(unstable)}

    return templates.TemplateResponse(
        request,
        "v2/overview.html",
        {
            "user": user,
            "current_tab": "overview",
            "initial_roster": initial_roster,
            "roster_available": _feed_ok(roster),
            "initial_stats": initial_stats,
            "dlc_warning": dlc_warning,
            # Single source for the Hagga projection. The template used to carry
            # its own hardcoded copy of these four numbers; two copies of a
            # projection is how a map quietly stops agreeing with itself.
            "cal": map_model.MAPS["hagga"]["cal"],
            "map_panels": _overview_map_panels(),
        },
    )


def _roster_rows(data):
    """Flatten the per-map roster payload into table rows. No last_login: the
    feed carries none, and a column bound to a field the route always sets to
    None is a column of blanks pretending to be data."""
    rows = []
    if not isinstance(data, dict):
        return rows
    for m in (data.get("maps") or []):
        map_name = m.get("map") or ""
        for p in (m.get("players") or []):
            rows.append({
                "name": p.get("name"),
                "map": map_name,
                "faction": p.get("faction"),
                "online": True,
            })
    return rows


@router.get("/api/dune/v2/roster-fragment")
async def v2_roster_fragment(request: Request):
    require_admin(request)
    # The roster feed goes through the relay. When it is down this used to 500
    # the poll, htmx swallowed it, and the table simply kept showing whoever was
    # online several minutes ago with nothing saying so.
    data = await _safe_call(dune_roster(request))
    return templates.TemplateResponse(
        request,
        "v2/_fragments/overview_roster.html",
        {"initial_roster": _roster_rows(data), "roster_available": _feed_ok(data)},
    )


# --- Audit routes ---

AUDIT_PAGE_SIZE = 50


async def _audit_view(request, page, username, action, search,
                      success, date_from, date_to):
    """The one merged, filtered, paginated audit read.

    Both the page route and the fragment route go through it, so a filter cannot
    exist on one and be silently absent from the other. That mismatch is what
    made the page load its rows twice: the server rendered an unfiltered first
    page, then the wrapper immediately re-fetched the filtered one on load, and
    every visit paid for the 10000-row read and the 500-grant relay call twice.
    """
    page = max(int(page or 1), 1)
    success = _clean_success(success)
    date_from = _clean_date(date_from)
    date_to = _clean_date(date_to)

    # Bulk fetch both sources once, with every filter the SQL can carry already
    # applied. The same filters run again post-merge because the grant rows come
    # from the relay and were never in SQLite.
    rows = _query_audit_rows(
        username=username, action=action, success=success,
        date_from=date_from, date_to=date_to, limit=AUDIT_BULK_LIMIT,
    )
    grants = await _safe_call(grant_recent(request, limit=GRANTS_BULK_LIMIT))
    merged = _merge_audit_and_grants(
        rows, grants.get("grants") if grants else [])

    # Apply the remaining filters across the full merged dataset BEFORE
    # pagination so they match every row and `total` reflects the filtered set.
    if username:
        merged = [e for e in merged if (e.get("username") or "") == username]
    if action:
        merged = [e for e in merged if _action_matches(e.get("action"), action)]
    if success in ("0", "1"):
        want_ok = success == "1"
        merged = [e for e in merged if bool(e.get("success")) == want_ok]
    # A row with no timestamp cannot be shown to be inside the window, so a date
    # filter drops it rather than assuming it belongs.
    if date_from:
        merged = [e for e in merged
                  if (e.get("timestamp") or "")[:10] >= date_from]
    if date_to:
        merged = [e for e in merged
                  if e.get("timestamp") and e["timestamp"][:10] <= date_to]
    if search:
        needle = search.lower()
        merged = [
            e for e in merged
            if needle in (e.get("action") or "").lower()
            or needle in (e.get("username") or "").lower()
            or needle in (e.get("target") or "").lower()
            or needle in (e.get("details") or "").lower()
        ]

    total = len(merged)
    start = (page - 1) * AUDIT_PAGE_SIZE
    return {
        "initial_entries": merged[start:start + AUDIT_PAGE_SIZE],
        "current_page": page,
        "page_size": AUDIT_PAGE_SIZE,
        "total_count": total,
        "filter_qs": _filter_qs(username, action, search,
                                success, date_from, date_to),
        # A relay that is down means the grant half of this trail is simply
        # missing. Saying so beats a page that quietly shows admin.db only.
        "grants_available": bool(grants and grants.get("available", True)),
        "initial_filters": {
            "username": username, "action": action, "search": search,
            "success": success, "date_from": date_from, "date_to": date_to,
            "page": page,
        },
    }


@router.get("/v2/audit")
async def v2_audit(
    request: Request,
    page: int = 1,
    username: str = "",
    action: str = "",
    search: str = "",
    success: str = "",
    date_from: str = "",
    date_to: str = "",
):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    # The page route takes the SAME filters as the fragment. hx-push-url puts
    # them in the address bar, so a bookmarked or refreshed filtered view has to
    # come back filtered; without this the first paint would quietly ignore them.
    ctx = await _audit_view(request, page, username, action, search,
                            success, date_from, date_to)
    ctx.update({
        "user": user,
        "current_tab": "audit",
        "users": _list_audit_users(),
        "action_groups": AUDIT_ACTION_GROUPS,
        "action_prefixes": AUDIT_ACTION_PREFIXES,
    })
    return templates.TemplateResponse(request, "v2/audit.html", ctx)


@router.get("/api/dune/v2/audit-fragment")
async def v2_audit_fragment(
    request: Request,
    page: int = 1,
    username: str = "",
    action: str = "",
    search: str = "",
    success: str = "",
    date_from: str = "",
    date_to: str = "",
):
    require_admin(request)
    ctx = await _audit_view(request, page, username, action, search,
                            success, date_from, date_to)
    return templates.TemplateResponse(
        request, "v2/_fragments/audit_rows.html", ctx)


def _details_view(raw):
    """(one line summary, expanded body) for the Details cell.

    Nearly every lever writes json.dumps(...) here: the reason, the old state and
    the new one. The page rendered none of it, so an audit row said that a thing
    happened and never what it did. Pretty-print when it parses, fall back to the
    raw string when it does not, and truncate the summary so one 4KB payload
    cannot own the table. Both halves go through Jinja autoescape."""
    if raw is None:
        return "", ""
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if not text:
        return "", ""
    body = text
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            body = json.dumps(json.loads(text), indent=2, sort_keys=True)
        except (ValueError, TypeError):
            body = text
    summary = " ".join(text.split())
    if len(summary) > 90:
        summary = summary[:90] + "..."
    return summary, body


def _merge_audit_and_grants(audit_entries, grants):
    """Merge admin-side audit_log rows and game-side grant rows, newest first.

    Audit rows shape (from SQLite audit_log): id, user_id, username, action, target,
      ip_address, details, success, timestamp. The column is `timestamp`, not
      `created_at`: reading created_at gave every admin row a null time, which
      both blanked the Time cell and sorted the whole admin half to the bottom
      under a reverse sort on ''.
    Grant rows shape (from relay): variable; normalize with sensible fallbacks.
    """
    out = []
    for e in (audit_entries or []):
        ts = e.get("timestamp") or e.get("created_at")
        summary, body = _details_view(e.get("details"))
        out.append({
            "source": "audit",
            "timestamp": ts,
            "timestamp_iso": ts,
            "timestamp_display": ts,
            "username": e.get("username"),
            "action": e.get("action"),
            "target": e.get("target"),
            "details": e.get("details"),
            "details_summary": summary,
            "details_body": body,
            "success": e.get("success"),
            "ip_address": e.get("ip_address"),
        })
    for g in (grants or []):
        ts = g.get("ts") or g.get("created_at") or g.get("granted_at")
        grant_type = g.get("grant_type") or g.get("type") or "grant"
        raw_detail = g.get("detail") or g.get("payload") or g.get("note")
        # Uniform action keeps the audit.html dropdown's 'dune_grant' value
        # matching every grant row; grant_type is preserved in the detail
        # column so per-grant context isn't lost.
        details = f"{grant_type}: {raw_detail}" if raw_detail else grant_type
        summary, body = _details_view(details)
        out.append({
            "source": "grant",
            "timestamp": ts,
            "timestamp_iso": ts,
            "timestamp_display": ts,
            "username": g.get("admin") or g.get("operator") or g.get("granted_by"),
            "action": "dune_grant",
            "target": g.get("target_name") or g.get("char_name") or g.get("account_id"),
            "details": details,
            "details_summary": summary,
            "details_body": body,
            "success": True,
            "ip_address": None,
        })
    out.sort(key=lambda r: (r.get("timestamp") or ""), reverse=True)
    return out
