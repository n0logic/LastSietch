import asyncio
import time
from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import chat_retention
import event_reminders
import market_watch
import metrics
import mirror
import name_lookups
import portal_identity
import relay
from auth import get_current_user
from database import has_users, init_db
from portal_host import PortalHostMiddleware
from routers import audit, auth_routes, cvars, dune, dune_broadcast, dune_chat, dune_grant, dune_market, messages, portal, portal_alerts, portal_link, rcon, servers, solido, solido_admin, updates, users, v2, v2_actions, v2_bases, v2_karum, v2_moderation, v2_monitor, v2_player, v2_players, v2_portal, v2_spice


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not has_users():
        print("No users found. Visit /admin/setup to create the initial admin account.")
    # LIFT-11 — load the per-namespace friendly-name sidecars (items,
    # buildings, skills, lore, progression, communinet). Missing files are
    # non-fatal; each lookup returns None and callers fall back to raw IDs.
    name_lookups.load_all()
    # Without a relay URL there is nothing to sample or mirror: an unconfigured
    # host stays quiet instead of logging a failed tick every interval.
    from config import RELAY_URL as _relay_url
    if _relay_url:
        sampler_task = asyncio.create_task(metrics.start_sampler(app))
        # Read-model mirror sync loop: pulls /dune/read-models into the local mirror
        # so portal/admin reads stay local. Always runs; PORTAL_MIRROR_READS gates the
        # read side, not the sync, so the mirror is warm before reads are flipped on.
        mirror_task = asyncio.create_task(mirror.sync_loop(app))
    else:
        print("relay not configured: metrics sampler and read-model mirror disabled.")
        sampler_task = asyncio.create_task(asyncio.sleep(0))
        mirror_task = asyncio.create_task(asyncio.sleep(0))
    # Market price-alert watcher: diffs the local market mirror for player-set
    # watchlist thresholds and fires portal/Discord alerts. Self-disables when
    # MARKET_WATCH_ENABLED is off, so this task just returns immediately then.
    market_watch_task = asyncio.create_task(market_watch.sync_loop(app))
    # Portal event reminders: mails the T-1h notification for every due
    # portal_event_reminders row. Self-disables when EVENT_REMINDERS_ENABLED is
    # off, so this task just returns immediately then.
    event_reminders_task = asyncio.create_task(event_reminders.sync_loop(app))
    # Portal chat retention: hourly sweep of messages past 30 days, long-resolved
    # reports and ended mutes. Deliberately has NO kill switch and does not read
    # LASTSIETCH_CHAT_ENABLED: a switched-off feature still holds what it already took,
    # and gating retention on a feature flag is how that becomes an archive.
    chat_retention_task = asyncio.create_task(chat_retention.sync_loop(app))
    try:
        yield
    finally:
        sampler_task.cancel()
        mirror_task.cancel()
        market_watch_task.cancel()
        event_reminders_task.cancel()
        chat_retention_task.cancel()
        for _task in (sampler_task, mirror_task, market_watch_task,
                      event_reminders_task, chat_retention_task):
            try:
                await _task
            except asyncio.CancelledError:
                pass
        # VC0 perf: close the shared httpx client opened lazily on first
        # call_relay(). Idempotent — safe even if no relay call happened.
        await relay.close_client()


# No interactive docs and no schema endpoint: the admin host strips /admin straight
# into this app, so /openapi.json was a public route inventory (every admin and
# portal path with its parameters) until 2026-09-06. Nothing consumes it.
app = FastAPI(title="Last Sietch Admin", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.exception_handler(portal_identity.ProfileMigrationError)
async def profile_access_error(request, error):
    logging.getLogger(__name__).warning('portal profile operation refused: %s', error)
    return JSONResponse({'detail': 'Account access changed. Please sign in again.'}, status_code=409)


_ADMIN_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data:; font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'"
)

# Portal CSP — stricter than admin (no inline script). Adds cdn.discordapp.com
# to img-src for Discord avatars. (dune.layout.tools was removed 2026-06-12
# with the embedded-gallery retirement; the public market is a link-out now.)
_PORTAL_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data: https://cdn.discordapp.com; "
    "font-src 'self' https://fonts.gstatic.com; connect-src 'self'"
)

# V2 next-gen (SvelteKit) sets its own policy under /portal/v2 only: SvelteKit
# emits a small inline bootstrap + inline critical CSS, so 'unsafe-inline' is
# allowed for script/style here (the rest of /portal keeps the stricter
# _PORTAL_CSP). Everything is self-hosted; worker-src covers the PWA service
# worker. Hardening to hash/nonce-based CSP is a later pass.
# blob: on connect-src + img-src so GLTFLoader can fetch the blob: URLs it mints for
# GLB-embedded textures (container 3D heroes, holo terrain). Blobs are page-created
# and same-origin, so this stays low-risk. child-src blob: covers three's worker path.
_NEXTGEN_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://cdn.discordapp.com; "
    "font-src 'self'; connect-src 'self' blob:; worker-src 'self' blob:; child-src blob:; manifest-src 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith("/portal/v2"):
        response.headers["Content-Security-Policy"] = _NEXTGEN_CSP
    elif request.url.path.startswith("/portal"):
        response.headers["Content-Security-Policy"] = _PORTAL_CSP
    else:
        response.headers["Content-Security-Policy"] = _ADMIN_CSP
    return response


# --- V2 next-gen portal (SvelteKit SPA static artifact) ---------------------
# Served under /portal/v2 so the ls_portal_session cookie scope applies. Real
# built files are returned directly; any other sub-path returns index.html so
# client-side routing works on deep links / refresh. Registered before the portal
# router so /portal/v2/* never falls through to a /portal handler.
_NEXTGEN_BUILD = Path(__file__).parent / "portal-nextgen" / "build"

# Deep links unfurl as the page they land on (nextgen_unfurl.py): the shell is
# re-read only when a deploy replaces it (mtime), and the map names come from the
# same table the maps render from, so a new board unfurls without a code change.
from nextgen_unfurl import apply_unfurl  # noqa: E402
_NEXTGEN_INDEX = {"mtime": None, "text": ""}


def _nextgen_index_text(index: Path) -> str:
    mtime = index.stat().st_mtime
    if _NEXTGEN_INDEX["mtime"] != mtime:
        _NEXTGEN_INDEX["text"] = index.read_text(encoding="utf-8")
        _NEXTGEN_INDEX["mtime"] = mtime
    return _NEXTGEN_INDEX["text"]


def _nextgen_map_names() -> dict:
    try:
        from map_model import MAPS
        return {k: v.get("name", k) for k, v in MAPS.items()}
    except Exception:  # noqa: BLE001
        return {}


# One market listing's unfurl census, cached briefly so an unfurler hammering a
# hot link is not a query per crawl. Bounded: the whole cache is dropped once it
# outgrows the ceiling rather than evicted row by row.
_NEXTGEN_LISTING_TTL = 60
_NEXTGEN_LISTING_MAX = 256
_NEXTGEN_LISTINGS: dict = {}


def _nextgen_listing_lookup(publish_id):
    """{title, pieces, size_band, faction} for a published listing, or None when
    it is missing, hidden or removed (the generic bases copy then answers). The
    author name and the player's description are NOT in this shape: an unfurl is
    a public surface and carries server-derived census only."""
    key = str(publish_id)
    now = time.monotonic()
    hit = _NEXTGEN_LISTINGS.get(key)
    if hit is not None and now - hit[0] < _NEXTGEN_LISTING_TTL:
        return hit[1]
    try:
        import blueprint_market
        row = blueprint_market.get_public(int(publish_id))
        info = None
        if row:
            pieces = int(row.get("piece_count") or 0)
            faction = str(row.get("faction") or "").strip().lower()
            info = {
                "title": row.get("title") or "",
                "pieces": pieces,
                "size_band": blueprint_market._size_band(pieces),
                "faction": "" if faction in ("", "neutral") else faction,
            }
    except Exception:  # noqa: BLE001
        return None
    if len(_NEXTGEN_LISTINGS) >= _NEXTGEN_LISTING_MAX:
        _NEXTGEN_LISTINGS.clear()
    _NEXTGEN_LISTINGS[key] = (now, info)
    return info


def _nextgen_report_lookup(edition_id):
    try:
        from routers.portal_reports import enabled, store
        if not enabled():
            return None
        row = store().get(edition_id)
        if row is None:
            return None
        return {key: row['edition'][key] for key in ('title', 'date', 'summary')}
    except Exception:
        return None


@app.get("/portal/v2")
@app.get("/portal/v2/{path:path}")
async def portal_v2_spa(path: str = "") -> Response:
    base = _NEXTGEN_BUILD
    if path:
        try:
            resolved = (base / path).resolve()
            resolved.relative_to(base.resolve())
        except (ValueError, RuntimeError, OSError):
            resolved = None
        if resolved and resolved.is_file():
            if resolved.suffix == ".webmanifest":
                return FileResponse(resolved, media_type="application/manifest+json")
            return FileResponse(resolved)
    index = base / "index.html"
    if index.is_file():
        try:
            shell = _nextgen_index_text(index)
            unfurled = apply_unfurl(shell, path, _nextgen_map_names(), _nextgen_listing_lookup, _nextgen_report_lookup)
        except Exception:  # noqa: BLE001
            unfurled = shell = None
        if unfurled is not None and unfurled != shell:
            return HTMLResponse(unfurled)
        return FileResponse(index, media_type="text/html")
    return PlainTextResponse("portal v2 not deployed", status_code=503)


app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(servers.router)
app.include_router(audit.router)
app.include_router(rcon.router)
app.include_router(updates.router)
app.include_router(dune.router)
app.include_router(dune_grant.router)
app.include_router(dune_chat.router)
app.include_router(dune_broadcast.router)
app.include_router(dune_market.router)
app.include_router(solido_admin.router)
app.include_router(v2.router)
# VC2 P1: v2_monitor owns /v2/server + /v2/server/monitor + the JSON
# aggregator at /api/dune/v2/server-monitor. Registered after v2.router so
# the placeholder routes there (Settings/Updates) keep their handlers.
app.include_router(v2_monitor.router)
# VC3: v2_players (with literal /v2/players/grants*) MUST register before
# v2_player (which has the /v2/players/{account_id} catch-all) — otherwise
# the path param swallows "grants" and raises 400 "account_id must be a
# positive integer".
app.include_router(v2_players.router)
app.include_router(v2_player.router)
# Phase C moderation trio (kick/ban/unban + bans list). Lives under
# /api/dune/v2/* alongside the player drilldown JSON endpoints.
app.include_router(v2_moderation.router)
# Phase C Live Actions — give-item/award-xp/teleport/refill-water at an online
# player via the relay server-command path. Lives under /api/dune/v2/* alongside
# the player drilldown JSON endpoints; registers after v2_player (whose
# /v2/players/{account_id} catch-all does not collide with these /player/ POSTs).
app.include_router(v2_actions.router)
# W6 Spice-spawn toggle (VC2 P2). Lives under /api/dune/v2/spice/* alongside
# the other Server Health JSON endpoints.
app.include_router(v2_spice.router)
# Land-claim ownership lookup (admin read-only) under /api/dune/v2/bases*.
app.include_router(v2_bases.router)
# The Karum operator page: resolve a stuck trade, and read the escrow audit.
# Every write goes through the same relay -> dispatcher -> writer chain the
# portal uses; nothing here reaches the game DB directly.
app.include_router(v2_karum.router)
# Wave 1 Set A (World & Maps) and Set B (Systems & Health, Economy, Social).
# Guarded one at a time: the three wave-1 branches merge separately, and a
# module that has not landed yet must cost its own pages, not the whole panel.
try:
    from routers import v2_world
    app.include_router(v2_world.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.v2_world absent; /v2/world/* not registered")
try:
    from routers import v2_systems
    app.include_router(v2_systems.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.v2_systems absent; /v2/systems, /v2/economy, /v2/social not registered")
app.include_router(cvars.router)
# VC6 Portal admin — manual link / unlock / revoke under /api/dune/v2/portal/*.
app.include_router(v2_portal.router)
app.include_router(portal.router)
from routers import portal_signin
app.include_router(portal_signin.router)
app.include_router(messages.router)
app.include_router(portal_link.router)
app.include_router(portal_alerts.router)
# Fremkit wave 4 identity codes + own-activity log under /portal/settings/*.
# Guarded like the wave-1 admin routers: this module lands on its own branch, and
# its absence must cost the Settings panel only, never the whole portal.
try:
    from routers import portal_codes
    app.include_router(portal_codes.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_codes absent; /portal/settings/* not registered")
# Wave 10b account preferences under /portal/settings/prefs. Guarded the same
# way: the module lands on its own branch, and its absence must cost the saved
# preference only, never the whole portal (the portal falls back to the
# browser-local mirror).
try:
    from routers import portal_prefs
    app.include_router(portal_prefs.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_prefs absent; /portal/settings/prefs not registered")
# Fremkit wave 9 events under /portal/events/*. Guarded the same way: the module
# lands on its own branch, and its absence must cost the Events page only, never
# the whole portal.
try:
    from routers import portal_events
    app.include_router(portal_events.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_events absent; /portal/events/* not registered")
# Dated public report editions; collection and publishing use restricted routes.
from routers import portal_reports
app.include_router(portal_reports.router)

# Wave 10 Ingot Refinery under /portal/refinery/*. Guarded the same way: the
# module lands on its own branch, and its absence must cost the Workshop page
# only, never the whole portal.
try:
    from routers import portal_refinery
    app.include_router(portal_refinery.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_refinery absent; /portal/refinery/* not registered")
# Wave 11 portal chat under /portal/chat/*: the reading and posting half. Guarded
# the same way as its neighbours, and separately from the moderation half below,
# so a missing module costs the Chat page only and never the whole portal.
try:
    from routers import portal_chat
    app.include_router(portal_chat.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_chat absent; /portal/chat/* not registered")
# Wave 11 chat moderation: report, delete, mute and the moderator queue. Its own
# guard rather than a shared one, because it imports the membership helper the
# reading half also uses: if that helper is missing, both fail and the operator
# should see both lines rather than one that only names the first.
try:
    from routers import portal_chat_mod
    app.include_router(portal_chat_mod.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_chat_mod absent; chat moderation not registered")
# Wave 12 deliveries under /portal/deliveries/v2: the Welcome and Return packages
# the server sent the signed-in player. Guarded the same way as its neighbours,
# so a missing module costs the Deliveries card and nothing else (Home seals that
# one sub-object and the Mailbox panel falls back to its sealed state).
try:
    from routers import portal_deliveries
    app.include_router(portal_deliveries.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_deliveries absent; /portal/deliveries/v2 not registered")
# Wave 12 public feature gates under /portal/features: the five booleans the Help
# page needs to hide an entry for a feature that has not opened. Its own guard,
# because it shares nothing with the deliveries half and an operator reading the
# log should see which of the two is missing.
try:
    from routers import portal_features
    app.include_router(portal_features.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_features absent; /portal/features not registered")
# QA harness minted sessions under /portal/qa/*. Guarded like its neighbours, and
# the guard matters more here than anywhere else: the module answers a bare 404
# unless LASTSIETCH_QA_SESSIONS is on AND the caller is pure loopback, so a box that
# never registers it is in exactly the state a box that does registers itself
# into by default. Caddy denies /portal/qa/* at the edge as well.
try:
    from routers import portal_qa
    app.include_router(portal_qa.router)
except ImportError:
    logging.getLogger(__name__).warning("routers.portal_qa absent; /portal/qa/* not registered")
# Solido base-blueprint integration (proxy + browse/3D + own-base export).
app.include_router(solido.router)


# --- portal.lastsietch.com: the SPA at that host's root ---------------------
# THE POSITION MATTERS. Starlette inserts each add_middleware at the FRONT of
# the user list, so the last one registered is the outermost one. This must be
# outside security_headers above: that middleware picks the CSP off
# request.url.path, and a page served at / on the portal host only looks like
# the SPA (and so only gets _NEXTGEN_CSP, which its inline bootstrap needs)
# once this one has rewritten the path to /portal/v2/. Nothing below may add
# another middleware, and no add_middleware call may move above this line.
app.add_middleware(PortalHostMiddleware)


# Portal-scoped 500 handler — renders the player-friendly error.html for any
# unhandled exception under /portal/*. Admin paths (and everything else) keep
# Starlette's default plain-text 500 so admin behavior is unchanged.
_portal_logger = logging.getLogger("portal")


@app.exception_handler(Exception)
async def portal_unhandled_500(request: Request, exc: Exception) -> Response:
    is_portal = request.url.path.startswith("/portal")
    _portal_logger.exception(
        "unhandled exception path=%s portal=%s", request.url.path, is_portal
    )
    if not is_portal:
        return PlainTextResponse("Internal Server Error", status_code=500)
    return templates.TemplateResponse(
        request,
        "portal/error.html",
        {
            "title": "Something went wrong",
            "message": "An unexpected error occurred. Please try again in a moment.",
            "error_title": "Something went wrong",
            "error_message": "An unexpected error occurred. Please try again in a moment.",
            "back_url": "/portal/",
        },
        status_code=500,
    )


def _get_user_or_redirect(request: Request):
    try:
        return get_current_user(request)
    except HTTPException:
        return None


@app.get("/")
async def grid(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(request, "grid.html", {"user": user})


@app.get("/dashboard/{game_id}")
async def dashboard_game(request: Request, game_id: str):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    if game_id == "dune":
        # Dune dashboard retired — v2 overview is the live page (Phase B).
        return RedirectResponse(url="/admin/v2/overview", status_code=302)
    return templates.TemplateResponse(request, "dashboard.html", {"user": user, "game_id": game_id})


@app.get("/users")
async def users_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return RedirectResponse(url="/admin/", status_code=302)
    return templates.TemplateResponse(request, "users.html", {"user": user})


@app.get("/audit")
async def audit_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return RedirectResponse(url="/admin/", status_code=302)
    return templates.TemplateResponse(request, "audit.html", {"user": user})


@app.get("/dashboard/dune/grant")
async def dune_grant_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return RedirectResponse(url="/admin/", status_code=302)
    # Standalone dune grant page retired — v2 player search is the grant entry (Phase B).
    return RedirectResponse(url="/admin/v2/players/search", status_code=302)


@app.get("/settings")
@app.get("/settings/{game_id}")
async def settings_page(request: Request, game_id: str = "conan"):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(request, "settings.html", {"user": user, "game_id": game_id})


@app.get("/settings/{game_id}/usergroups")
async def usergroups_page(request: Request, game_id: str):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return RedirectResponse(url="/admin/", status_code=302)
    return templates.TemplateResponse(request, "usergroups.html", {"user": user, "game_id": game_id})


@app.get("/rcon")
@app.get("/rcon/{game_id}")
async def rcon_page(request: Request, game_id: str = "conan"):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(request, "rcon.html", {"user": user, "game_id": game_id})


@app.get("/change-password")
async def change_password_page(request: Request):
    user = _get_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(request, "change_password.html", {"user": user})
