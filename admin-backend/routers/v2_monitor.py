"""VC2 P1 — Server > Monitor aggregator + sub-tab shell.

Reference: docs/dune-research/VC2-P1-EXECUTION-BRIEF.md §3, §4.1, §7.1.

One JSON aggregator drives the P1 panels (Hero, Presence, World Counters,
Action Stream, Funcom Intelligence) plus the VC4 Leaderboards card on a 10s
poll. The route fans 15 reads out concurrently via
asyncio.gather(return_exceptions=True) so a single slow/dead source degrades
just one strip, not the whole page.

Per-source result lands as either the source payload (dict) or an Exception;
_assemble() maps Exceptions to {available: false, error: ...} per-key so
the JS hydrators always receive a stable envelope.

Cache: in-process 10s TTL keyed on the route (no per-user variation). 10s
matches the polling interval so the worst case is one full fan-out per
window even with N admin tabs open.

NEVER restarts game pods / BGD / k3s. Every read is either localhost HTTP
(http://127.0.0.1:8078 on lastsietch-dune via relay) or filesystem
(/var/lib/lastsietch-rmq-bridge/captures-admin/) — RAM-safe by construction.
"""
import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import get_current_user, require_admin
from database import get_db
from relay import call_relay

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# In-process aggregator cache. Mirrors VC0 cache.py shape but kept inline so
# the TTL hit doesn't depend on the decorator module — the aggregator's only
# call site is this route and the cache key is fixed.
_AGG_CACHE: dict = {"data": None, "ts": 0.0}
_AGG_TTL = 10.0

# Per-source timeout for the relay/telemetry fan-out. Each upstream call has
# its own timeout, but we cap at the gather level too so a runaway source
# can't blow past the 10s aggregator budget.
_PER_SOURCE_TIMEOUT = 8.0


def _admin_or_redirect(request: Request):
    """HTML page guard — 401 -> /admin/login, 403 -> /admin/."""
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


# --- HTML page routes (Monitor body inside server.html shell) ---

@router.get("/v2/server")
async def v2_server_root(request: Request):
    """Server hub default landing — Monitor sub-tab."""
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "v2/server.html",
        {
            "user": user,
            "current_tab": "server",
            "current_sub_tab": "monitor",
            "current_server_sub": "monitor",
            "active_subtab": "monitor",
        },
    )


@router.get("/v2/server/monitor")
async def v2_server_monitor_explicit(request: Request):
    """Explicit /server/monitor URL — same shell as /v2/server (default)."""
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "v2/server.html",
        {
            "user": user,
            "current_tab": "server",
            "current_sub_tab": "monitor",
            "current_server_sub": "monitor",
            "active_subtab": "monitor",
        },
    )


# --- Aggregator JSON route ---

async def _wrap(coro):
    """Run a coroutine with per-source timeout; return the payload OR an
    exception (gather collects the value either way when return_exceptions=True
    is set on the outer gather call, but a per-source timeout keeps the
    aggregator from blocking on a slow source past its individual budget)."""
    try:
        return await asyncio.wait_for(coro, timeout=_PER_SOURCE_TIMEOUT)
    except asyncio.TimeoutError as exc:
        return exc
    except Exception as exc:
        return exc


def _query_admin_db_stats() -> dict:
    """Local SQLite snapshot for the aggregator's `admin` key. Cheap counts
    only; no joins, no scans. Safe to call on the hot path."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log "
            "WHERE timestamp >= datetime('now', '-1 day')"
        ).fetchone()
        audit_24h = row["n"] if row else 0
        import portal_identity
        row = conn.execute(
            ("SELECT COUNT(*) AS n FROM ls_account_links "
            "WHERE revoked_at IS NULL").replace('ls_account_links', portal_identity.link_table(conn))
        ).fetchone()
        links_active = row["n"] if row else 0
        conn.close()
        return {
            "available": True,
            "audit_24h": audit_24h,
            "links_active": links_active,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _safe(result, *, default=None):
    """Map a gather result to a dict envelope. Exceptions -> available:false."""
    if isinstance(result, BaseException):
        return {"available": False, "error": f"{type(result).__name__}: {result}"}
    if result is None:
        return default if default is not None else {"available": False, "error": "no data"}
    if isinstance(result, dict):
        # Upstream payloads (telemetry, RMQ helpers) already carry their own
        # `available` key. Pass through unchanged.
        if "available" not in result:
            return {"available": True, **result}
        return result
    # Non-dict payload (e.g. a list) — wrap so consumers see a consistent shape.
    return {"available": True, "data": result}


def _safe_list(result, key: str | None = None) -> list:
    """Map a gather result to a list. Exceptions / failures -> []."""
    if isinstance(result, BaseException) or result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if key and isinstance(result.get(key), list):
            return result[key]
        # Common shapes: {"items": [...]}, {"events": [...]}, {"levelups": [...]}
        for k in ("items", "events", "rows", "results"):
            if isinstance(result.get(k), list):
                return result[k]
    return []


def _safe_field(result, key: str, *, default=0):
    """Pull a single field from a gather result; default on any failure."""
    if isinstance(result, BaseException) or not isinstance(result, dict):
        return default
    val = result.get(key)
    return val if val is not None else default


def _build_maps_summary(status, partitions) -> list:
    """Per-map summary the Hero strip renders. Joins farm_state (from
    /dune/status) with partition counts (from rmq-partition-counts) when both
    are available. Falls back to status-only if partitions failed."""
    if isinstance(status, BaseException) or not isinstance(status, dict):
        return []
    maps = status.get("maps") or []
    part_data = partitions if isinstance(partitions, dict) else {}
    part_counts = (part_data.get("partitions") or {}) if part_data.get("available", True) else {}

    out = []
    for m in maps:
        name = m.get("map") or ""
        entry = {
            "map": name,
            "ready": m.get("ready"),
            "alive": m.get("alive"),
            "players": m.get("players"),
            "partition_pve": None,
            "partition_pvp": None,
        }
        # Partition routing keys are typed as strings server-side; the helper
        # may emit them as int keys after JSON decode. Try both.
        for pid_key, count in part_counts.items():
            try:
                pid = int(pid_key)
            except (TypeError, ValueError):
                continue
            # PvE convention = dim_0, PvP = dim_1 (Deep Desert dimension convention)
            if pid == 0:
                entry["partition_pve"] = count
            elif pid == 1:
                entry["partition_pvp"] = count
        out.append(entry)
    return out


def _world_counters(world) -> dict:
    """Extract subfief / structure / vehicle counters from the telemetry world
    endpoint payload. Shape varies upstream; defensive about missing keys."""
    if isinstance(world, BaseException) or not isinstance(world, dict):
        return {"available": False, "subfiefs": 0, "structures": 0, "vehicles": 0}
    # Telemetry returns a snapshot envelope; common keys are 'subfiefs',
    # 'structures', 'vehicles' either at the top level or under 'counters'.
    counters = world.get("counters") if isinstance(world.get("counters"), dict) else world
    return {
        "available": True,
        "subfiefs": counters.get("subfiefs") or 0,
        "structures": counters.get("structures") or 0,
        "vehicles": counters.get("vehicles") or 0,
        "snapshots": world.get("snapshots") or world.get("world_snapshots") or [],
    }


def _assemble(results) -> dict:
    """Map the 15 gather results into the aggregator envelope documented in
    brief §3.2. Order MUST match the gather() call below."""
    (status, partitions, travel, funcom_push, bgd_rpc, completions,
     events, transfers, world, presence_1h, levelups, admin_stats,
     lb_pvp, lb_deaths, lb_pilots) = results

    out = {
        "generated_at": int(time.time()),
        "ttl_seconds": int(_AGG_TTL),
        "battlegroup": (
            status.get("battlegroup") if isinstance(status, dict) else None
        ) or {"available": False},
        "maps_summary": _build_maps_summary(status, partitions),
        "travel_queue": _safe(travel, default={"available": False, "depth": 0}),
        "funcom_push": _safe(funcom_push),
        "bgd_rpc": _safe(bgd_rpc),
        "presence_1h": _safe_list(presence_1h, key="samples")
                       or _safe_list(presence_1h, key="presence")
                       or _safe_list(presence_1h),
        "world_counters": _world_counters(world),
        "recent": {
            "levelups": _safe_list(levelups, key="levelups"),
            "combat": _safe_list(events, key="events"),
            # grants is P2 — kept here so the JS contract is stable across phases.
            "grants": [],
            "map_lifecycle": _safe_list(completions, key="recent"),
            "transfers": _safe_list(transfers, key="transfers"),
        },
        "admin": admin_stats if isinstance(admin_stats, dict) else {"available": False},
        # VC4 — weekly leaderboards (top-N sliced client-side). All three share
        # one ISO week; pull it from whichever board read succeeded.
        "leaderboards": {
            "week": (lb_pvp.get("week") if isinstance(lb_pvp, dict) else None)
                    or (lb_deaths.get("week") if isinstance(lb_deaths, dict) else None)
                    or (lb_pilots.get("week") if isinstance(lb_pilots, dict) else None),
            "pvp": _safe_list(lb_pvp, key="leaderboard"),
            "deaths": _safe_list(lb_deaths, key="leaderboard"),
            "pilots": _safe_list(lb_pilots, key="leaderboard"),
        },
        "sources": {
            "status": isinstance(status, dict),
            "partitions": isinstance(partitions, dict) and partitions.get("available", True),
            "travel_queue": isinstance(travel, dict) and travel.get("available", True),
            "funcom_push": isinstance(funcom_push, dict) and funcom_push.get("available", True),
            "bgd_rpc": isinstance(bgd_rpc, dict) and bgd_rpc.get("available", True),
            "completions": isinstance(completions, dict) and completions.get("available", True),
            "events": isinstance(events, dict),
            "transfers": isinstance(transfers, dict),
            "world": isinstance(world, dict),
            "presence_1h": isinstance(presence_1h, dict) or isinstance(presence_1h, list),
            "levelups": isinstance(levelups, dict),
            "admin": isinstance(admin_stats, dict) and admin_stats.get("available", True),
            "leaderboard_pvp": isinstance(lb_pvp, dict) and lb_pvp.get("available", True),
            "leaderboard_deaths": isinstance(lb_deaths, dict) and lb_deaths.get("available", True),
            "leaderboard_pilots": isinstance(lb_pilots, dict) and lb_pilots.get("available", True),
        },
    }
    return out


@router.get("/api/dune/v2/server-monitor")
async def server_monitor(request: Request):
    """Aggregator JSON for the Monitor sub-tab. 10s TTL cache; one fan-out
    per cache miss; per-source isolation. Admin-only."""
    require_admin(request)

    now = time.monotonic()
    if _AGG_CACHE["data"] is not None and (now - _AGG_CACHE["ts"]) < _AGG_TTL:
        # Return a fresh copy of the envelope with a `cached: true` marker so
        # the client can debug staleness without changing the shape. We don't
        # deep-copy — JSONResponse serializes once and consumers don't mutate.
        data = dict(_AGG_CACHE["data"])
        data["cached"] = True
        return JSONResponse(data)

    # NB: order MUST match _assemble's unpacking.
    results = await asyncio.gather(
        _wrap(call_relay("/dune/status", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/rmq/partition-counts", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/rmq/travel-queue", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/rmq/last-funcom-push", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/rmq/bgd-rpc-recent", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/rmq/completions-recent?limit=50", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/telemetry/events?limit=50", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/telemetry/transfers?limit=50", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/telemetry/world?window=24h", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/stats/presence?window=1h", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/progression/levelups?limit=50", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(asyncio.to_thread(_query_admin_db_stats)),
        # VC4 — 3 weekly leaderboards (current ISO week). Appended last so the
        # existing positional unpack order is untouched; _assemble unpacks these
        # as (lb_pvp, lb_deaths, lb_pilots) after admin_stats.
        _wrap(call_relay("/dune/telemetry/leaderboard/pvp", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/telemetry/leaderboard/deaths", timeout=_PER_SOURCE_TIMEOUT)),
        _wrap(call_relay("/dune/telemetry/leaderboard/pilots", timeout=_PER_SOURCE_TIMEOUT)),
        return_exceptions=True,
    )

    data = _assemble(results)
    data["cached"] = False
    _AGG_CACHE["data"] = data
    _AGG_CACHE["ts"] = now
    return JSONResponse(data)


# --- Fragment stubs (P2/P3/P4 — full bodies land in later phases) ---
# These keep the URL surface reserved so HTMX-driven swaps from the
# lower-frequency panels (Audit Feed, Heatmap, Economy, Coriolis, etc.)
# have a stable target while P1 ships.

_STUB_HTML = '<!-- VC2 P1 stub: this panel lands in a later phase. -->'


@router.get("/api/dune/v2/monitor/coriolis", response_class=HTMLResponse)
async def fragment_coriolis(request: Request):
    require_admin(request)
    return HTMLResponse(_STUB_HTML)


@router.get("/api/dune/v2/monitor/heatmap", response_class=HTMLResponse)
async def fragment_heatmap(request: Request):
    require_admin(request)
    return HTMLResponse(_STUB_HTML)


@router.get("/api/dune/v2/monitor/economy", response_class=HTMLResponse)
async def fragment_economy(request: Request):
    require_admin(request)
    return HTMLResponse(_STUB_HTML)


@router.get("/api/dune/v2/monitor/audit-feed", response_class=HTMLResponse)
async def fragment_audit_feed(request: Request):
    require_admin(request)
    return HTMLResponse(_STUB_HTML)


@router.get("/api/dune/v2/monitor/telemetry-health", response_class=HTMLResponse)
async def fragment_telemetry_health(request: Request):
    require_admin(request)
    return HTMLResponse(_STUB_HTML)


@router.get("/api/dune/v2/monitor/admin-db", response_class=HTMLResponse)
async def fragment_admin_db(request: Request):
    require_admin(request)
    return HTMLResponse(_STUB_HTML)


@router.get("/api/dune/v2/monitor/connections-geo", response_class=HTMLResponse)
async def fragment_connections_geo(request: Request):
    require_admin(request)
    return HTMLResponse(_STUB_HTML)
