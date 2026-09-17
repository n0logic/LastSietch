import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import map_model
import mirror
from auth import audit_log, get_current_user, require_admin
from database import get_db
from name_lookups import ITEMS as _ITEM_NAMES
from relay import call_relay
from routers.dune import (
    _cached_progression_snapshot_full,
    dune_player_container_items,
    dune_player_containers,
    dune_player_tags,
)
from routers.dune_grant import grant_players

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Pagination is shared across the per-player Grants and Audit sub-tabs.
PAGE_SIZE = 50

VALID_SUBTABS = ("identity", "grants", "containers", "audit", "export", "live_actions")


def _admin_or_redirect(request: Request):
    """For HTML page routes: 401 -> redirect to login, 403 -> redirect to /admin/."""
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


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


def _validate_account_id(account_id: str) -> int:
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    return int(account_id)


def _sanitize_search(q: str) -> str:
    """Strip control chars from picker search; cap at 64 chars."""
    if not q:
        return ""
    cleaned = "".join(ch for ch in q if ch.isprintable())
    return cleaned[:64].strip()


def _filter_players(players: list, q: str, online_only: bool) -> list:
    """Apply substring + online filter, then sort online-first then name ASC."""
    rows = players or []
    if q:
        needle = q.lower()
        rows = [p for p in rows if needle in (p.get("name") or "").lower()]
    if online_only:
        rows = [p for p in rows if (p.get("online_status") or "").lower() == "online"]
    rows = sorted(
        rows,
        key=lambda p: (
            0 if (p.get("online_status") or "").lower() == "online" else 1,
            (p.get("name") or "").lower(),
        ),
    )
    return rows


def _find_player(players: list, account_id: int) -> dict | None:
    # Relay may return account_id as a string — cast both sides to be safe.
    target = str(account_id)
    for p in (players or []):
        if str(p.get("account_id", "")) == target:
            return p
    return None


async def _load_identity_data(request: Request, account_id: int) -> dict:
    """Compose Identity-tab data from three sources. Each is best-effort —
    a relay outage on any one source still renders the others with empty-state
    placeholders, so first-paint never throws."""
    players_payload = await _safe_call(grant_players(request)) or {}
    picker_row = _find_player(players_payload.get("players") or [], account_id)

    # Hit the cached snapshot helper so a second click on a different player
    # within 30s reuses the relay round-trip. The cached helper carries the
    # raw account_id-bearing payload (the public proxy strips it).
    snapshot_row = None
    snapshot_available = False
    try:
        raw_snapshot = await _cached_progression_snapshot_full()
        snapshot_available = True
        target = str(account_id)
        for p in (raw_snapshot.get("players") or []):
            if str(p.get("account_id", "")) == target:
                snapshot_row = p
                break
    except Exception:
        logger.warning("identity: progression snapshot fetch failed", exc_info=True)

    # Route the tags call through the existing admin-backend proxy so it picks
    # up the per-IP rate limit + standardized error envelope. The proxy returns
    # {available, tags, count} on failure, so a 502 here surfaces as available=False
    # rather than an exception.
    # Fast path: local mirror tags section (self-gates on flag + staleness);
    # falls back to the rate-limited proxy on miss/stale/flag-off.
    tags_payload = mirror.get_section(account_id, "tags")
    if tags_payload is None:
        tags_payload = await _safe_call(dune_player_tags(str(account_id), request)) or {}
    tags_available = bool(tags_payload.get("available", True)) if tags_payload else False

    # Specialization tracks + keystone/skill counts (existing progression_state
    # endpoint, the same one that backs the Specializations picker). Best-effort.
    # Fast path: the mirror's specializations section IS the progression_state
    # payload; fall back to the live relay on miss/stale/flag-off.
    progression = {"available": False, "spec_tracks": {}, "keystone_count": 0, "skill_count": 0}
    try:
        ps = mirror.get_section(account_id, "specializations")
        if ps is None:
            ps = await call_relay(f"/dune/player/{account_id}/progression_state")
        if ps and ps.get("available"):
            progression = {
                "available": True,
                "spec_tracks": ps.get("spec_tracks") or {},
                "keystone_count": len(ps.get("owned_keystone_ids") or []),
                "skill_count": len(ps.get("learned_blocks") or []),
            }
    except Exception:
        logger.warning("identity: progression_state fetch failed", exc_info=True)

    # Economy (Solari/Scrip) + activity (playtime/last-seen) via the vitals endpoint.
    vitals = {"available": False, "economy": {"available": False}, "activity": {"available": False}}
    try:
        v = await call_relay(f"/dune/player/{account_id}/vitals")
        if v and v.get("available"):
            vitals = {
                "available": True,
                "economy": v.get("economy") or {"available": False},
                "activity": v.get("activity") or {"available": False},
            }
    except Exception:
        logger.warning("identity: vitals fetch failed", exc_info=True)

    # Faction standing. MUST come from reputation, never from Faction.X.TierN
    # tags: `dune.set_player_faction_reputation` WRITES those tags during a G12
    # grant, so a granted player carries the full Tier0..20 set while a player
    # who earned the rank normally carries only Tier0..5. Deriving the tier from
    # tags therefore ranks them backwards, which is exactly what this card did
    # until 2026-07-27 (one player read "Tier 20" while stuck at 19; another
    # read "Tier 5" while actually rank 20).
    # Reuses the portal's threshold table rather than restating it — two copies
    # of a tier table drift, and a wrong rank here is indistinguishable from a
    # real one. Local import: portal.py is the only owner, and importing it at
    # module scope would couple two routers' import order for one helper.
    faction = {"available": False}
    try:
        prog = mirror.get_section(account_id, "progress")
        if prog is None:
            prog = await call_relay(f"/dune/player/{account_id}/progress")
        fac = (prog or {}).get("faction") or {}
        if fac.get("faction_name") is not None or fac.get("reputation") is not None:
            from routers.portal import _faction_rank
            rep = int(fac.get("reputation") or 0)
            r = _faction_rank(rep, fac.get("faction_name"))
            faction = {
                "available": True,
                "name": fac.get("faction_name"),
                "reputation": rep,
                "rank": r["rank"],
                "rank_name": r["rank_name"],
                "at_max": r["at_max"],
                "to_next": r.get("to_next"),
            }
    except Exception:
        logger.warning("identity: faction standing fetch failed", exc_info=True)

    return {
        "account_id": account_id,
        "picker_row": picker_row,
        "snapshot_row": snapshot_row,
        "snapshot_available": snapshot_available,
        "tags": tags_payload.get("tags") or [],
        "tags_available": tags_available,
        "progression": progression,
        "vitals": vitals,
        "faction": faction,
    }


def _load_grants_page(account_id: int, page: int) -> dict:
    """Read-only paginated query against the local SQLite audit_log for rows
    where this player was the target of a dune_grant. All SQL is parameterized."""
    offset = (page - 1) * PAGE_SIZE
    target = str(account_id)
    conn = get_db()
    count_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM audit_log WHERE action = ? AND target = ?",
        ("dune_grant", target),
    ).fetchone()
    total = count_row["cnt"] if count_row else 0
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE action = ? AND target = ? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        ("dune_grant", target, PAGE_SIZE, offset),
    ).fetchall()
    conn.close()

    grants = []
    for r in rows:
        row = dict(r)
        # details column is JSON written by dune_grant.py on every grant attempt.
        # A malformed row must never crash the page — keep the raw string in
        # `details_raw` so the row-expand still has something to show.
        parsed = None
        raw_details = row.get("details")
        if raw_details:
            try:
                parsed = json.loads(raw_details)
            except (TypeError, ValueError):
                parsed = None
        grants.append({
            "id": row.get("id"),
            # audit_log column is `timestamp` (database.py:35), NOT `created_at`.
            # The template reads g.timestamp directly.
            "timestamp": row.get("timestamp"),
            "username": row.get("username"),
            "ip_address": row.get("ip_address"),
            "success": bool(row.get("success")),
            "grant_type": (parsed or {}).get("grant_type") or "grant",
            "detail": (parsed or {}).get("detail"),
            "result": (parsed or {}).get("result"),
            "idempotency_key": (parsed or {}).get("idempotency_key"),
            "details_parsed": parsed,
            "details_raw": raw_details,
        })
    return {
        "account_id": account_id,
        "grants": grants,
        "page": page,
        "page_size": PAGE_SIZE,
        "total_count": total,
    }


def _load_audit_page(account_id: int, page: int) -> dict:
    """Direct paginated query against the local SQLite audit_log for any row
    targeting this player. Bypasses audit.get_audit_log because that helper
    clamps `limit` to 200 (audit.py:19), which would cap the per-player audit
    total at 200 — wrong for a forensic view. All SQL is parameterized."""
    offset = (page - 1) * PAGE_SIZE
    target = str(account_id)
    conn = get_db()
    count_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM audit_log WHERE target = ?",
        (target,),
    ).fetchone()
    total = count_row["cnt"] if count_row else 0
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE target = ? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (target, PAGE_SIZE, offset),
    ).fetchall()
    conn.close()
    entries = [dict(r) for r in rows]
    return {
        "account_id": account_id,
        "entries": entries,
        "page": page,
        "page_size": PAGE_SIZE,
        "total_count": total,
    }


async def _load_containers(request: Request, account_id: int) -> dict:
    """Delegate to the existing P3a route. Wrapped via _safe_call so a relay
    outage degrades to an empty 'unavailable' state instead of a 500."""
    raw = await _safe_call(dune_player_containers(str(account_id), request)) or {}
    return {
        "account_id": account_id,
        "available": bool(raw.get("available")),
        "containers": raw.get("containers") or [],
        "count": raw.get("count") or 0,
    }


async def _load_export(account_id: int) -> dict:
    """Order 0 / G30 v1 — list player's BuildingBlueprint_CopyDevice items
    for the Export subtab's blueprint picker. Character Export has no listing
    step (one snapshot per account)."""
    try:
        raw = await call_relay(f"/dune/player/{account_id}/blueprints")
    except HTTPException as exc:
        logger.warning("export: blueprints fetch failed: %s", exc.detail)
        raw = {"available": False, "blueprints": [], "count": 0}
    except Exception:
        logger.warning("export: blueprints fetch unexpected error", exc_info=True)
        raw = {"available": False, "blueprints": [], "count": 0}
    return {
        "account_id": account_id,
        "available": bool(raw.get("available")),
        "blueprints": raw.get("blueprints") or [],
        "count": raw.get("count") or 0,
    }


async def _load_live_actions(request: Request, account_id: int) -> dict:
    """Live Actions subtab data. Resolves is_online + char name from the same
    grant-players picker the hero strip uses. is_online is advisory only — the
    publisher refuses Offline --send authoritatively. Best-effort: a relay
    outage degrades to is_online=False so the cards render disabled."""
    players_payload = await _safe_call(grant_players(request)) or {}
    picker_row = _find_player(players_payload.get("players") or [], account_id) or {}
    status_lc = str(picker_row.get("online_status") or "").lower()
    is_online = status_lc == "online" or picker_row.get("is_online") is True
    return {
        "account_id": account_id,
        "is_online": is_online,
        "player_name": picker_row.get("name") or f"account #{account_id}",
    }


# Filename-safe slug for downloads. Used for both blueprint and character
# exports to keep filenames clean ({title}.json per Q5a icehunter style).
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(stem: str, default: str) -> str:
    cleaned = _FILENAME_SAFE_RE.sub("-", (stem or "").strip()).strip("-.")
    return (cleaned or default)[:120]


# --- Picker landing page + fragment ---

@router.get("/v2/players/search")
async def v2_player_tools(request: Request, q: str = "", online_only: int = 0):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    payload = await _safe_call(grant_players(request)) or {}
    q_clean = _sanitize_search(q)
    only_online = bool(online_only)
    players = _filter_players(payload.get("players") or [], q_clean, only_online)
    return templates.TemplateResponse(
        request,
        "v2/player_tools.html",
        {
            "user": user,
            "current_tab": "players",
            "current_sub_tab": "search",
            "players": players,
            "total_player_count": len(payload.get("players") or []),
            "active_houses": payload.get("active_houses") or [],
            "available": bool(payload.get("available", True)),
            "filter_q": q_clean,
            "filter_online_only": only_online,
        },
    )


@router.get("/api/dune/v2/players/_picker")
async def v2_players_picker(request: Request, q: str = "", online_only: int = 0):
    require_admin(request)
    payload = await _safe_call(grant_players(request)) or {}
    q_clean = _sanitize_search(q)
    only_online = bool(online_only)
    players = _filter_players(payload.get("players") or [], q_clean, only_online)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_picker_list.html",
        {
            "players": players,
            "total_player_count": len(payload.get("players") or []),
            "available": bool(payload.get("available", True)),
            "filter_q": q_clean,
            "filter_online_only": only_online,
        },
    )


# --- Bans viewer (active + full action history) ---

async def _load_bans(history: bool) -> dict:
    """Active bans (holadmin.bans) or the full action ledger
    (holadmin.player_actions: kick/ban/unban). Best-effort; a relay outage
    degrades to an unavailable state instead of a 500."""
    path = "/dune/bans/history" if history else "/dune/bans"
    try:
        raw = await call_relay(path, timeout=30)
    except Exception:
        logger.warning("bans: relay fetch failed (%s)", path, exc_info=True)
        return {"available": False, "history": history, "rows": []}
    key = "actions" if history else "bans"
    return {
        "available": bool(raw.get("available", True)),
        "history": history,
        "rows": raw.get(key) or [],
    }


@router.get("/v2/players/bans")
async def v2_players_bans(request: Request, history: int = 0):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    bans = await _load_bans(bool(history))
    return templates.TemplateResponse(
        request,
        "v2/players_bans.html",
        {
            "user": user,
            "current_tab": "players",
            "current_sub_tab": "bans",
            "bans": bans,
        },
    )


@router.get("/api/dune/v2/players/_bans")
async def v2_players_bans_fragment(request: Request, history: int = 0):
    require_admin(request)
    bans = await _load_bans(bool(history))
    return templates.TemplateResponse(
        request, "v2/_fragments/bans_table.html", {"bans": bans},
    )


# --- Base-backup sources catalog (G20/G21 handoff/clone source library) ---

async def _load_bb_sources() -> dict:
    """Read-only base_backups source catalog. Each row is a saved base totem
    snapshot usable as a G20/G21 handoff or clone source. Best-effort; a relay
    outage degrades to an unavailable state instead of a 500."""
    try:
        raw = await call_relay("/dune/bb/available-sources", timeout=30)
    except Exception:
        logger.warning("bases: relay fetch failed", exc_info=True)
        return {"available": False, "sources": [], "total_actors": 0}
    sources = raw.get("sources") or []
    total_actors = sum((s.get("linked_actor_count") or 0) for s in sources)
    return {
        "available": bool(raw.get("available", True)),
        "sources": sources,
        "total_actors": total_actors,
    }


@router.get("/v2/players/bases")
async def v2_players_bases(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    bases = await _load_bb_sources()
    return templates.TemplateResponse(
        request,
        "v2/players_bases.html",
        {
            "user": user,
            "current_tab": "players",
            "current_sub_tab": "bases",
            "bases": bases,
        },
    )


@router.get("/v2/players/vault")
async def v2_players_vault(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "v2/players_vault.html", {
        "user": user, "current_tab": "players", "current_sub_tab": "vault",
    })


@router.get("/v2/players/claims")
async def v2_players_claims(request: Request):
    """Land-claim ownership map. Distinct from /v2/players/bases, which is the
    base-BACKUP catalogue (G20/G21 sources); this one is who owns what on the
    ground, and the two share nothing but the word "base".

    The page itself carries no data: the map fetches the cached
    /api/dune/v2/bases directory client-side, so 163 claims cost the game host
    one query per 5 minutes however many admins have it open. Calibration is
    handed to the template from map_model so the projection cannot drift away
    from the portal's.
    """
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    hagga = map_model.MAPS["hagga"]
    return templates.TemplateResponse(
        request,
        "v2/players_claims.html",
        {
            "user": user,
            "current_tab": "players",
            "current_sub_tab": "claims",
            "cal": hagga["cal"],
            "game_map": "HaggaBasin",
        },
    )


@router.get("/api/dune/v2/players/_bases")
async def v2_players_bases_fragment(request: Request):
    require_admin(request)
    bases = await _load_bb_sources()
    return templates.TemplateResponse(
        request, "v2/_fragments/bases_list.html", {"bases": bases},
    )


@router.get("/api/dune/v2/players/bases/{backup_id}/_detail")
async def v2_players_bases_detail_fragment(request: Request, backup_id: str):
    require_admin(request)
    if not backup_id.isdigit():
        raise HTTPException(400, "backup_id must be a positive integer")
    bid = int(backup_id)
    try:
        raw = await call_relay(f"/dune/bb/{bid}/detail", timeout=30)
    except Exception:
        logger.warning("bases detail: relay fetch failed (id=%s)", bid, exc_info=True)
        raw = {"available": False}
    detail = raw.get("detail") if isinstance(raw, dict) else None
    available = bool(raw.get("available", True)) and isinstance(detail, dict)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/bases_detail.html",
        {"backup_id": bid, "available": available, "detail": detail or {}},
    )


# --- Per-player drill-down page + sub-tab fragments ---

@router.get("/v2/players/{account_id}")
async def v2_player_drilldown(request: Request, account_id: str, tab: str = "identity"):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    acct = _validate_account_id(account_id)

    initial_subtab = tab if tab in VALID_SUBTABS else "identity"

    # The picker payload powers the hero strip (name + presence) and confirms
    # the account_id is known to the relay — 404 if it isn't.
    players_payload = await _safe_call(grant_players(request)) or {}
    picker_row = _find_player(players_payload.get("players") or [], acct)
    if not picker_row:
        raise HTTPException(status_code=404, detail="Player not found")

    context = {
        "user": user,
        "current_tab": "players",
        "current_sub_tab": "search",
        "account_id": acct,
        "player": picker_row,
        "initial_subtab": initial_subtab,
    }

    # First-paint includes the chosen sub-tab's data so we don't HTMX-load on
    # the very first render — shareable links to ?tab=containers paint
    # immediately instead of flashing the Identity tab and then swapping.
    if initial_subtab == "identity":
        context["identity"] = await _load_identity_data(request, acct)
    elif initial_subtab == "grants":
        context["grants"] = _load_grants_page(acct, 1)
    elif initial_subtab == "containers":
        context["containers"] = await _load_containers(request, acct)
    elif initial_subtab == "audit":
        context["audit"] = _load_audit_page(acct, 1)
    elif initial_subtab == "export":
        context["export"] = await _load_export(acct)
    elif initial_subtab == "live_actions":
        context["live_actions"] = await _load_live_actions(request, acct)

    return templates.TemplateResponse(
        request,
        "v2/player_tools_drilldown.html",
        context,
    )


@router.get("/api/dune/v2/player/{account_id}/_identity")
async def v2_player_identity_fragment(request: Request, account_id: str):
    require_admin(request)
    acct = _validate_account_id(account_id)
    data = await _load_identity_data(request, acct)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_identity.html",
        {"account_id": acct, "identity": data},
    )


@router.get("/api/dune/v2/player/{account_id}/_grants")
async def v2_player_grants_fragment(request: Request, account_id: str, page: int = 1):
    require_admin(request)
    acct = _validate_account_id(account_id)
    page = max(page, 1)
    data = _load_grants_page(acct, page)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_grants.html",
        {"account_id": acct, "grants": data},
    )


@router.get("/api/dune/v2/player/{account_id}/_containers")
async def v2_player_containers_fragment(request: Request, account_id: str):
    require_admin(request)
    acct = _validate_account_id(account_id)
    data = await _load_containers(request, acct)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_containers.html",
        {"account_id": acct, "containers": data},
    )


@router.get("/api/dune/v2/player/{account_id}/_live_actions")
async def v2_player_live_actions_fragment(request: Request, account_id: str):
    require_admin(request)
    acct = _validate_account_id(account_id)
    data = await _load_live_actions(request, acct)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_live_actions.html",
        {"account_id": acct, "live_actions": data},
    )


@router.get("/api/dune/v2/player/{account_id}/_audit")
async def v2_player_audit_fragment(request: Request, account_id: str, page: int = 1):
    require_admin(request)
    acct = _validate_account_id(account_id)
    page = max(page, 1)
    data = _load_audit_page(acct, page)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_audit_rows.html",
        {"account_id": acct, "audit": data},
    )


def _validate_container_id(container_id: str) -> int:
    if not container_id.isdigit():
        raise HTTPException(400, "container_id must be a positive integer")
    return int(container_id)


def _decorate_items(items: list) -> list:
    """Attach friendly `name` to each item dict. Uses sidecar lookup first,
    falls back to PascalCase/snake_case synthesis (IronOre -> 'Iron Ore')
    for raw resource keys that have no pak entry — Funcom uses bare class
    names for those internally."""
    out = []
    for it in (items or []):
        tpl = it.get("template_id") or ""
        out.append({
            "id": it.get("id"),
            "template_id": tpl,
            "name": _ITEM_NAMES.lookup_or_synthesize(tpl),
            "stack_size": it.get("stack_size"),
            "quality": it.get("quality"),
            "cur_dur": it.get("cur_dur") or "",
            "max_dur": it.get("max_dur") or "",
        })
    return out


@router.get("/api/dune/v2/player/{account_id}/_container/{container_id}/_items")
async def v2_player_container_items_fragment(
    request: Request, account_id: str, container_id: str, page: int = 1,
):
    """Container drill-down items fragment (LIFT-10). Ownership is verified
    server-side by the helper script's JOIN chain — an `not_owned` envelope
    becomes a 404 here so a fat-fingered URL across two players can't leak
    inventory data."""
    require_admin(request)
    acct = _validate_account_id(account_id)
    cont = _validate_container_id(container_id)
    page = max(page, 1)

    raw = await dune_player_container_items(str(acct), str(cont), page, request)

    if not raw.get("available") and raw.get("error") == "not_owned":
        raise HTTPException(status_code=404, detail="Container not found for this account")

    items = _decorate_items(raw.get("items") or [])
    # Nest under `container_items` to match the existing fragment convention
    # (grants/audit/containers/identity all read from `{name}.{field}`).
    container_items = {
        "account_id": acct,
        "container_id": cont,
        "items": items,
        "count": len(items),
        "total_count": raw.get("total_count") or 0,
        "page": page,
        "page_size": raw.get("page_size") or 100,
        "available": bool(raw.get("available")),
        "error": raw.get("error"),
        "stale": bool(raw.get("stale")),
    }
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_container_items.html",
        {"account_id": acct, "container_id": cont, "container_items": container_items},
    )


# ============================================================================
# Order 0 — Data Portability: G30 v1 Blueprint Export + Character Export
#
# Two download routes (raw JSON files) + one HTMX fragment for the new
# "Export" subtab. icehunter cmdExportBlueprint MIT port with attribution
# per V2-ADMIN-CONTINUATION-PLAN-2026-05-26.md §F Q1.
# ============================================================================

def _validate_bp_id(bp_id: str) -> int:
    if not bp_id.isdigit():
        raise HTTPException(400, "bp_id must be a positive integer")
    return int(bp_id)


@router.get("/api/dune/v2/player/{account_id}/_export")
async def v2_player_export_fragment(request: Request, account_id: str):
    """Export subtab HTMX fragment. Renders Character Export card + the
    Solido Blueprint Export list (one row per BuildingBlueprint_CopyDevice)."""
    require_admin(request)
    acct = _validate_account_id(account_id)
    data = await _load_export(acct)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/player_export.html",
        {"account_id": acct, "export": data},
    )


@router.get("/v2/dune/player/{account_id}/export-blueprint/{bp_id}")
async def v2_player_export_blueprint(request: Request, account_id: str, bp_id: str):
    """G30 v1 — download a Solido-market JSON blueprint for a single
    BuildingBlueprint_CopyDevice owned by the player. Verbatim icehunter
    cmdExportBlueprint schema (name?/instances/placeables/pentashields?).
    No version field per Q5d. Filename: {bp-name}.json (icehunter style)."""
    user = get_current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "admin only")
    acct = _validate_account_id(account_id)
    bp = _validate_bp_id(bp_id)
    ip = (request.client.host if request.client else "") or ""

    try:
        raw = await call_relay(
            f"/dune/player/{acct}/blueprint/{bp}/export", timeout=60)
    except HTTPException as exc:
        audit_log(
            user["id"], user["username"], "dune_export_blueprint",
            str(acct), ip,
            details=json.dumps({"bp_id": bp, "result": f"relay_error: {exc.detail}"}),
            success=False,
        )
        raise

    if not raw.get("available"):
        err = raw.get("error") or "unknown"
        audit_log(
            user["id"], user["username"], "dune_export_blueprint",
            str(acct), ip,
            details=json.dumps({"bp_id": bp, "result": f"unavailable: {err}"}),
            success=False,
        )
        if err == "not_owned":
            raise HTTPException(404, "Blueprint not found for this account")
        raise HTTPException(502, f"Export failed: {err}")

    blueprint = raw.get("blueprint") or {}
    body = json.dumps(blueprint, indent=2, ensure_ascii=False).encode("utf-8")
    # Prefer the player-set BuildingBlueprintName from in-game; fall back to
    # bp_id when absent. Filename is {name}.json per Q5a (icehunter style).
    bp_name = raw.get("name") or blueprint.get("name") or f"Blueprint-{bp}"
    filename = _safe_filename(bp_name, f"blueprint-{bp}") + ".json"

    audit_log(
        user["id"], user["username"], "dune_export_blueprint",
        str(acct), ip,
        details=json.dumps({
            "bp_id": bp,
            "item_id": raw.get("item_id"),
            "bp_name": bp_name,
            "filename": filename,
            "byte_count": len(body),
            "instance_count":    len(blueprint.get("instances")    or []),
            "placeable_count":   len(blueprint.get("placeables")   or []),
            "pentashield_count": len(blueprint.get("pentashields") or []),
            "result": "ok",
        }),
        success=True,
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v2/dune/player/{account_id}/export-character")
async def v2_player_export_character(request: Request, account_id: str):
    """Character Export — Last Sietch-internal character snapshot JSON download.
    Carries FLevelComponent + FactionPlayerComponent + tags + spec_tracks +
    full controller/pawn actor properties + fgl_entities components."""
    user = get_current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "admin only")
    acct = _validate_account_id(account_id)
    ip = (request.client.host if request.client else "") or ""

    try:
        raw = await call_relay(
            f"/dune/player/{acct}/character/export", timeout=60)
    except HTTPException as exc:
        audit_log(
            user["id"], user["username"], "dune_export_character",
            str(acct), ip,
            details=json.dumps({"result": f"relay_error: {exc.detail}"}),
            success=False,
        )
        raise

    if not raw.get("available"):
        err = raw.get("error") or "unknown"
        audit_log(
            user["id"], user["username"], "dune_export_character",
            str(acct), ip,
            details=json.dumps({"result": f"unavailable: {err}"}),
            success=False,
        )
        if err == "account_not_found":
            raise HTTPException(404, "Account not found")
        raise HTTPException(502, f"Export failed: {err}")

    snapshot = raw.get("snapshot") or {}
    body = json.dumps(snapshot, indent=2, ensure_ascii=False).encode("utf-8")
    char_name = ((snapshot.get("character") or {}).get("character_name")) or f"account-{acct}"
    filename = _safe_filename(char_name, f"account-{acct}") + ".character.json"

    audit_log(
        user["id"], user["username"], "dune_export_character",
        str(acct), ip,
        details=json.dumps({
            "character_name": char_name,
            "filename": filename,
            "byte_count": len(body),
            "tag_count":        len((snapshot.get("character") or {}).get("tags") or []),
            "spec_track_count": len((snapshot.get("character") or {})
                                    .get("specialization_tracks") or []),
            "result": "ok",
        }),
        success=True,
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
