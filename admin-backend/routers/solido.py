"""Base blueprints — own-base 3D preview, Solido-format export, and the Last
Sietch community blueprint market.

Routes (all under /portal/):
- GET  /portal/solido                       — community blueprint market (public)
- GET  /portal/api/solido/market            — gallery data JSON (public, sort/tag/page)
- GET  /portal/solido/{publish_id}          — listing detail (public)
- GET  /portal/api/solido/market/{publish_id}/blueprint — blob JSON for the viewer (public)
- GET  /portal/solido/{publish_id}/download — download the blueprint (public, counts)
- POST /portal/solido/publish               — publish one of YOUR bases (session + CSRF)
- POST /portal/solido/{publish_id}/unpublish — author self-removal (session + CSRF)
- GET  /portal/my-bases                     — player's own bases (session required)
- GET  /portal/my-bases/{bp_id}/export      — download own base as Solido JSON (session)

The one invariant that shapes the design: publish is a SERVER-SIDE re-export of the
player's own blueprint via the ownership-gated G30 relay path (exactly as
my_bases_export). The publish endpoint NEVER accepts a client-supplied blueprint
JSON — it takes only an in-game numeric bp_id plus a capped title/description, and
stores the JSON the relay returns. Everything (browse/detail/download) is public;
only publish/unpublish require a linked session. All writes are <web-host>-local
(admin.db + on-disk blobs); the game DB is read-only via the relay.
"""
import portal_identity
import functools
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("solido")


def _log_import_rejection(fn):
    """Log WHY a portal import was refused, not merely that it was.

    Every early exit in solido_import happens BEFORE the audit_log call at the
    bottom, so a player who trips one leaves no record at all: the portal logs a
    bare `400 Bad Request` and nothing about the cause. On 2026-08-18 that turned
    one player's failed blueprint import into a long guessing session that a
    single log line would have answered.

    Context is stashed on request.state as the handler learns each field, so a
    refusal at any stage reports what was known by that point. Blueprint CONTENT
    is never logged -- only its size and the source identifier.
    """
    @functools.wraps(fn)
    async def wrapper(request: Request, *args, **kwargs):
        request.state.import_ctx = {}
        try:
            return await fn(request, *args, **kwargs)
        except HTTPException as exc:
            ctx = getattr(request.state, "import_ctx", None) or {}
            logger.warning(
                "import REFUSED %s: %s | %s",
                exc.status_code,
                str(exc.detail)[:200],
                " ".join(f"{k}={v}" for k, v in ctx.items()) or "no context",
            )
            raise
    return wrapper


router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Single source of truth for the portal-solido.js cache-bust key, shared by all
# four solido templates (gallery, detail, my_bases, import) via this Jinja global
# so the keys can never drift apart again (the detail page once lagged the others
# on an old key and served stale JS without the 3D viewer chrome). BUMP THIS ONE
# VALUE whenever static/js/portal-solido.js changes.
SOLIDO_JS_V = "20260701a"
templates.env.globals["solido_js_v"] = SOLIDO_JS_V
# Same discipline for portal-solido.css. BUMP on any CSS change.
SOLIDO_CSS_V = "20260701a"
templates.env.globals["solido_css_v"] = SOLIDO_CSS_V

SOLIDO_SITE = "https://dune.layout.tools"
# Canonical public home of a listing, for the copyable share link on the card.
PORTAL_V2_SITE = "https://portal.lastsietch.com/"

GALLERY_LIMIT_MAX = 48
GALLERY_LIMIT_DEFAULT = 24

# Blueprint display-name cap; must match dune-blueprint-rename.py NAME_MAX.
RENAME_NAME_MAX = 40

# Import: extract a Solido blueprint UUID from a pasted dune.layout.tools link
# or a bare UUID. Upload byte ceiling sits just above the writer's normalized
# cap (MAX_BLUEPRINT_BYTES, 1 MiB) to allow envelope overhead before normalize.
_IMPORT_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IMPORT_UPLOAD_MAX = 1_100_000


# --------------------------------------------------------------- HTML routes ---

@router.get("/portal/solido")
async def solido_hub(request: Request):
    """Community blueprint market landing (public). Server-renders the first
    gallery page; client JS handles sort/tag/paging thereafter. Keeps the My
    Bases entry, the public-Solido link-out, and the credit footer."""
    import blueprint_market
    from routers.portal import base_ctx
    accounts = _session_accounts(request)
    sort = "new"
    # Map through _card() so the server-rendered first page carries thumb_url
    # (same shape as the JSON paging endpoint); the template renders the photo
    # when present and falls back to the 'no preview' placeholder otherwise.
    # limit+1 probe: ask for one row past the page so an exactly-full page does
    # not advertise a next page that renders empty.
    rows = blueprint_market.list_published(sort, None, GALLERY_LIMIT_DEFAULT + 1, 0)
    has_more = len(rows) > GALLERY_LIMIT_DEFAULT
    listed = {}
    listings = [_card(r, listed) for r in rows[:GALLERY_LIMIT_DEFAULT]]
    my_ids = _my_publish_ids(accounts)
    ctx = {
        "active_nav": "solido",
        "solido_site": SOLIDO_SITE,
        "listings": listings,
        "sort": sort,
        "tag": "",
        "is_linked": bool(accounts),
        "my_publish_ids": my_ids,
        "gallery_limit": GALLERY_LIMIT_DEFAULT,
        "has_more": has_more,
    }
    ctx.update(base_ctx(request))
    return templates.TemplateResponse(request, "portal/solido.html", ctx)


@router.get("/portal/api/solido/market")
async def solido_market_data(request: Request):
    """Gallery data JSON (public, read-only). Client sort/tag/paging source."""
    import blueprint_market
    q = request.query_params
    sort = q.get("sort", "new")
    if sort not in ("new", "popular"):
        sort = "new"
    tag = (q.get("tag", "") or "").strip() or None
    try:
        limit = int(q.get("limit", GALLERY_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = GALLERY_LIMIT_DEFAULT
    limit = max(1, min(GALLERY_LIMIT_MAX, limit))
    try:
        offset = int(q.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    # limit+1 probe (see solido_hub): >, not >=, so the last full page is final.
    rows = blueprint_market.list_published(sort, tag, limit + 1, offset)
    has_more = len(rows) > limit
    listed = {}
    listings = [_card(r, listed) for r in rows[:limit]]
    return JSONResponse({
        "ok": True,
        "listings": listings,
        "has_more": has_more,
    })


@router.get("/portal/solido/{publish_id:int}")
async def solido_detail(request: Request, publish_id: int):
    """Listing detail page (public). 404 (friendly) if missing/unpublished."""
    import blueprint_market
    from routers.portal import base_ctx
    row = blueprint_market.get_public(publish_id)
    if row is None:
        raise HTTPException(404, "Blueprint not found")
    accounts = _session_accounts(request)
    is_owner = publish_id in _my_publish_ids(accounts)
    ctx = {
        "active_nav": "solido",
        "solido_site": SOLIDO_SITE,
        "bp": row,
        "is_owner": is_owner,
        "is_linked": bool(accounts),
        "thumb_url": (f"/portal/solido/{publish_id}/thumb.png"
                      if _has_thumb(publish_id) else None),
    }
    ctx.update(base_ctx(request))
    return templates.TemplateResponse(request, "portal/solido_detail.html", ctx)


@router.get("/portal/api/solido/market/{publish_id:int}/blueprint")
async def solido_market_blueprint(request: Request, publish_id: int):
    """The blob JSON the 3D viewer consumes (public). Viewing != downloading,
    so this does NOT increment the counter. Soft-degrades if the blob is gone."""
    import blueprint_market
    row = blueprint_market.get_public(publish_id)
    if row is None:
        raise HTTPException(404, "Blueprint not found")
    body = blueprint_market.read_blob(row)
    if body is None:
        return JSONResponse({"available": False})
    return Response(content=body, media_type="application/json")


@router.get("/portal/solido/{publish_id:int}/download")
async def solido_download(request: Request, publish_id: int):
    """Download the blueprint JSON (public). Counts; soft-degrades if blob gone."""
    import blueprint_market
    from auth import audit_log
    row = blueprint_market.get_public(publish_id)
    if row is None:
        raise HTTPException(404, "Blueprint not found")
    body = blueprint_market.read_blob(row)
    if body is None:
        raise HTTPException(404, "Blueprint file unavailable")
    blueprint_market.increment_download(publish_id)
    ip = (request.client.host if request.client else "") or ""
    audit_log(
        None, "public", "portal_blueprint_download", str(publish_id), ip,
        details=json.dumps({"title": row.get("title"),
                            "author": row.get("author_name")}),
        success=True,
    )
    filename = _slug_filename(row.get("title") or "", f"blueprint-{publish_id}") + ".json"
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/portal/solido/publish")
async def solido_publish(request: Request):
    """Publish one of the caller's OWN bases to the community market. The
    authenticity gate: takes only game_bp_id + title/description; the blueprint
    JSON is a server-side re-export via the ownership-gated relay (never a client
    upload). Session-gated, CSRF, rate-limited."""
    import blueprint_market
    from config import BLUEPRINT_PUBLISH_DAILY_CAP
    from relay import call_relay
    from auth import audit_log
    from portal_auth import (CSRF_HEADER, SESSION_COOKIE, csrf_for_session,
                             get_portal_session, validate_csrf)

    accounts = _session_accounts(request)
    if not accounts:
        raise HTTPException(403, "Link your character to publish")

    # Read the body once per content type (never both — that double-consumes the
    # stream). The My Bases button posts JSON via PortalAPI.fetch; the no-JS
    # fallback posts a form.
    content_type = (request.headers.get("content-type", "") or "").lower()
    form, body_json = {}, {}
    if "application/json" in content_type:
        try:
            body_json = await request.json()
        except Exception:
            body_json = {}
    else:
        try:
            form = await request.form()
        except Exception:
            form = {}

    def _field(name, default=""):
        if form and name in form:
            return form.get(name, default)
        return body_json.get(name, default)

    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = (request.headers.get(CSRF_HEADER, "")
                     or (form.get("csrf_token", "") if form else "")
                     or body_json.get("csrf_token", ""))
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    wants_json = "application/json" in (request.headers.get("accept", "") or "")
    game_bp_id = str(_field("game_bp_id", "")).strip()
    if not re.match(r"^[0-9]{1,20}$", game_bp_id):
        raise HTTPException(404, "Unknown blueprint")
    title = str(_field("title", "") or "")
    description = str(_field("description", "") or "")

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ip = (request.client.host if request.client else "") or ""

    # Rate limit: rolling 24h, durable across restarts (publish-log COUNT).
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    total_recent = 0
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if aid:
            total_recent += blueprint_market.recent_publish_count(aid, since)
    if total_recent >= BLUEPRINT_PUBLISH_DAILY_CAP:
        msg = "Daily publish limit reached — try again later."
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=429)
        raise HTTPException(429, msg)

    # Server-side re-export (the invariant). Try each linked account; stop at the
    # first that owns this blueprint.
    matched_aid = 0
    author_name = ""
    blueprint = None
    last_err = "not_owned"
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if not aid:
            continue
        try:
            raw = await call_relay(
                f"/dune/player/{aid}/blueprint/{game_bp_id}/export", timeout=60)
        except Exception:
            logger.warning("publish: relay failed (aid=%s bp=%s)",
                           aid, game_bp_id, exc_info=True)
            last_err = "relay_error"
            continue
        if not raw.get("available"):
            last_err = raw.get("error") or "unavailable"
            continue
        blueprint = raw.get("blueprint") or {}
        if not title:
            title = raw.get("name") or blueprint.get("name") or ""
        matched_aid = aid
        author_name = acct.get("character_name") or f"account #{aid}"
        break

    if blueprint is None:
        audit_log(
            None, f"self:{discord_id or '?'}", "portal_blueprint_publish", "0", ip,
            details=json.dumps({"game_bp_id": game_bp_id,
                                "result": f"unavailable: {last_err}"}),
            success=False,
        )
        raise HTTPException(404, "Blueprint not found for your linked characters")

    # Size cap before any row/blob write — no orphan blob.
    body = json.dumps(blueprint, ensure_ascii=False).encode("utf-8")
    if len(body) > blueprint_market.PUBLISH_BLOB_MAX:
        msg = "Base too large to publish."
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=413)
        raise HTTPException(413, msg)

    meta = blueprint_market.derive_metadata(blueprint)
    row = blueprint_market.create_or_update(
        account_id=matched_aid, discord_id=discord_id, author_name=author_name,
        game_bp_id=int(game_bp_id), title=title, description=description,
        blueprint=blueprint, meta=meta)
    publish_id = int(row["publish_id"])

    audit_log(
        None, f"self:{discord_id or '?'}", "portal_blueprint_publish",
        str(matched_aid), ip,
        details=json.dumps({
            "publish_id": publish_id,
            "game_bp_id": game_bp_id,
            "title": row.get("title"),
            "piece_count": meta["piece_count"],
            "byte_count": len(body),
        }),
        success=True,
    )

    if wants_json:
        return JSONResponse({"ok": True, "publish_id": publish_id})
    return RedirectResponse(
        url=f"/portal/solido/{publish_id}?published=1", status_code=302)


@router.post("/portal/solido/{publish_id:int}/unpublish")
async def solido_unpublish(request: Request, publish_id: int):
    """Author self-removal of a listing. Session-gated, CSRF. Blob retained."""
    import blueprint_market
    from auth import audit_log
    from portal_auth import (CSRF_HEADER, SESSION_COOKIE, csrf_for_session,
                             get_portal_session, validate_csrf)

    accounts = _session_accounts(request)
    if not accounts:
        raise HTTPException(403, "Link your character to manage your listings")

    try:
        form = await request.form()
    except Exception:
        form = {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = (request.headers.get(CSRF_HEADER, "")
                     or (form.get("csrf_token", "") if form else ""))
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    wants_json = "application/json" in (request.headers.get("accept", "") or "")
    account_ids = _account_id_set(accounts)
    ok = blueprint_market.unpublish(publish_id, account_ids)
    if not ok:
        raise HTTPException(404, "Listing not found")

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ip = (request.client.host if request.client else "") or ""
    audit_log(
        None, f"self:{discord_id or '?'}", "portal_blueprint_unpublish",
        str(publish_id), ip, details=None, success=True,
    )
    if wants_json:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/portal/solido", status_code=302)


# ----------------------------------------------------- Import to character ---
# Materialize a Solido blueprint .json (from OUR private market, the public
# dune.layout.tools market, or an uploaded file) as a new in-game Solido tool
# (BuildingBlueprint_CopyDevice) in the caller's CHOAM bank (online-safe) or
# main backpack (offline only). Self-scoped: a player can only import to their
# OWN linked character. Reuses the admin G20 path (_execute_one_grant), so the
# heavy shape/caps validation + the live game write are shared, single-sourced.

def _unwrap_blueprint(obj):
    """Accept either the Solido envelope {"blueprint_data": {...}} or a direct
    {instances, placeables, ...} object; return the inner dict."""
    if isinstance(obj, dict) and isinstance(obj.get("blueprint_data"), dict):
        return obj["blueprint_data"]
    return obj


def _import_detail_from_source(source_type, *, publish_id=None, link=None,
                               blueprint_text=None):
    """Resolve a player-chosen source into the partial grant detail
    (``{"blueprint_data": {...}}`` for private/upload, or ``{"blueprint_id": uuid}``
    for the public market). Raises HTTPException on bad input; deep shape/caps
    validation happens downstream in dune_grant._validate_detail."""
    import blueprint_market
    if source_type == "private":
        try:
            pid = int(publish_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid private blueprint id")
        row = blueprint_market.get_public(pid)
        if row is None:
            raise HTTPException(404, "Blueprint not found")
        body = blueprint_market.read_blob(row)
        if body is None:
            raise HTTPException(404, "Blueprint file unavailable")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(502, "Stored blueprint is not valid JSON")
        return {"blueprint_data": _unwrap_blueprint(data),
                "title": (row.get("title") or "")[:200],
                "source_blueprint_id": f"private:{pid}"}
    if source_type == "public":
        m = _IMPORT_UUID_RE.search(link or "")
        if not m:
            raise HTTPException(400, "Paste a dune.layout.tools blueprint link or UUID")
        return {"blueprint_id": m.group(0)}
    if source_type == "upload":
        if not blueprint_text:
            raise HTTPException(400, "No blueprint provided")
        if len(blueprint_text.encode("utf-8")) > IMPORT_UPLOAD_MAX:
            raise HTTPException(413, "Blueprint file too large")
        try:
            data = json.loads(blueprint_text)
        except json.JSONDecodeError:
            raise HTTPException(400, "Uploaded file is not valid JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "Uploaded blueprint must be a JSON object")
        return {"blueprint_data": _unwrap_blueprint(data)}
    raise HTTPException(400, "Unknown import source")


@router.get("/portal/solido/import")
async def solido_import_page(request: Request):
    """Import page (paste public link/UUID or upload a .json). Renders the
    caller's linked characters with live online status so the UI can gate the
    backpack option. Public page; the POST is session + CSRF gated."""
    from config import BLUEPRINT_IMPORT_DAILY_CAP
    from rmq_command import is_online
    from routers.portal import base_ctx
    accounts = _session_accounts(request)
    chars = []
    for a in accounts:
        aid = int(a.get("account_id") or 0)
        if not aid:
            continue
        try:
            online = bool(await is_online(aid))
        except Exception:
            online = False
        chars.append({
            "account_id": aid,
            "character_name": a.get("character_name") or f"Account {aid}",
            "online": online,
        })
    ctx = {
        "active_nav": "solido",
        "solido_site": SOLIDO_SITE,
        "is_linked": bool(accounts),
        "characters": chars,
        "import_cap": BLUEPRINT_IMPORT_DAILY_CAP,
    }
    ctx.update(base_ctx(request))
    return templates.TemplateResponse(request, "portal/solido_import.html", ctx)


@router.post("/portal/solido/import")
@_log_import_rejection
async def solido_import(request: Request):
    """Import a Solido blueprint to the caller's OWN character as a new Solido
    tool, delivered to the CHOAM bank (online-safe) or main backpack (offline
    only). Self-scoped, session + CSRF gated, rolling-24h rate-limited. Reuses
    the admin G20 executor; recipient is forced to a linked account."""
    import uuid as _uuid
    from config import BLUEPRINT_IMPORT_DAILY_CAP
    from auth import audit_log, recent_action_count
    from portal_auth import (CSRF_HEADER, SESSION_COOKIE, csrf_for_session,
                             get_portal_session, validate_csrf)
    from rmq_command import is_online
    from routers.dune_grant import _execute_one_grant

    ctx = getattr(request.state, "import_ctx", None)
    if ctx is None:
        ctx = request.state.import_ctx = {}

    accounts = _session_accounts(request)
    ctx["linked"] = len(accounts or [])
    if not accounts:
        raise HTTPException(403, "Link your character to import blueprints")

    content_type = (request.headers.get("content-type", "") or "").lower()
    ctx["ct"] = content_type.split(";")[0] or "-"
    form, body_json, upload_text = {}, {}, None
    if "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except Exception:
            form = {}
        up = form.get("blueprint_file")
        if up is not None and hasattr(up, "read"):
            raw = await up.read()
            ctx["upload_bytes"] = len(raw)
            if len(raw) > IMPORT_UPLOAD_MAX:
                raise HTTPException(413, "Blueprint file too large")
            try:
                upload_text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(400, "Uploaded file is not UTF-8 text")
    elif "application/json" in content_type:
        try:
            body_json = await request.json()
        except Exception:
            body_json = {}
    else:
        try:
            form = await request.form()
        except Exception:
            form = {}

    def _field(name, default=""):
        if form and name in form:
            return form.get(name, default)
        return body_json.get(name, default)

    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = (request.headers.get(CSRF_HEADER, "")
                     or (form.get("csrf_token", "") if form else "")
                     or body_json.get("csrf_token", ""))
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    wants_json = "application/json" in (request.headers.get("accept", "") or "")

    # Recipient is forced to one of the caller's OWN linked accounts — never
    # read an arbitrary account from the body.
    my_ids = _account_id_set(accounts)
    try:
        account_id = int(_field("account_id", 0) or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id == 0 and len(my_ids) == 1:
        account_id = next(iter(my_ids))
    ctx["account_id"] = account_id
    if account_id not in my_ids:
        raise HTTPException(403, "Choose one of your linked characters")

    delivery = str(_field("delivery", "") or "").strip().lower()
    ctx["delivery"] = delivery or "-"
    if delivery not in ("bank", "backpack"):
        raise HTTPException(400, "Choose a destination: bank or backpack")

    source_type = str(_field("source_type", "") or "").strip().lower()
    ctx["source_type"] = source_type or "-"
    detail_src = _import_detail_from_source(
        source_type,
        publish_id=_field("publish_id", None),
        link=str(_field("link", "") or ""),
        blueprint_text=(upload_text if upload_text is not None
                        else (str(_field("blueprint_text", "") or "") or None)),
    )

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ctx["did"] = discord_id or "?"
    ip = (request.client.host if request.client else "") or ""

    # Backpack is RAM-fragile (offline only). The writer also enforces this, but
    # we give a clear up-front message and steer online players to the bank.
    try:
        online = bool(await is_online(account_id))
    except Exception:
        online = False
    ctx["online"] = online
    if delivery == "backpack" and online:
        msg = ("Log out of the game first to deliver to your backpack, or pick "
               "the CHOAM bank (works while you are online).")
        if wants_json:
            logger.warning("import REFUSED 409: backpack while online | %s",
                           " ".join(f"{k}={v}" for k, v in ctx.items()))
            return JSONResponse({"ok": False, "error": msg, "online": True}, status_code=409)
        raise HTTPException(409, msg)

    # Per-player rolling-24h import cap (counts successful imports to any of the
    # caller's linked accounts).
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    recent = recent_action_count(
        "portal_solido_import", since, targets=[str(a) for a in my_ids])
    if recent >= BLUEPRINT_IMPORT_DAILY_CAP:
        msg = "Daily import limit reached — try again later."
        if wants_json:
            logger.warning("import REFUSED 429: daily cap %s reached | %s",
                           BLUEPRINT_IMPORT_DAILY_CAP,
                           " ".join(f"{k}={v}" for k, v in ctx.items()))
            return JSONResponse({"ok": False, "error": msg}, status_code=429)
        raise HTTPException(429, msg)

    # Fire via the shared G20 executor: validates shape/caps and writes through
    # the ownership-keyed relay grant path. account_id is the (forced) recipient.
    detail = {"delivery": delivery, **detail_src}
    portal_user = {"id": None, "username": f"portal:{discord_id or '?'}"}
    result = await _execute_one_grant(
        user=portal_user, ip=ip, account_id=account_id,
        grant_type="import_blueprint", detail_in=detail,
        idempotency_key=str(_uuid.uuid4()),
    )

    ok = bool(result.get("success"))
    if not ok:
        logger.warning("import FAILED at writer: %s | status=%s grant=%s | %s",
                       (result.get("message") or "?")[:200], result.get("status"),
                       result.get("grant_id"),
                       " ".join(f"{k}={v}" for k, v in ctx.items()))
    audit_log(
        None, f"self:{discord_id or '?'}", "portal_solido_import",
        str(account_id), ip,
        details=json.dumps({
            "delivery": delivery,
            "source_type": source_type,
            "source": detail_src.get("source_blueprint_id") or detail_src.get("blueprint_id"),
            "result": result.get("status"),
            "result_message": (result.get("message") or "")[:500],
            "grant_id": result.get("grant_id"),
        }),
        success=ok,
    )

    if ok:
        if delivery == "bank":
            msg = ("Imported to your CHOAM bank. Relog or change zones in-game "
                   "and the new Solido tool will appear in your bank.")
        else:
            msg = ("Imported to your backpack. It will be waiting in your "
                   "inventory the next time you log into the game.")
    else:
        msg = result.get("message") or "Import failed."
    payload = {
        "ok": ok,
        "delivery": delivery,
        "online": online,
        "message": msg,
        "grant_id": result.get("grant_id"),
    }
    if wants_json:
        return JSONResponse(payload, status_code=(200 if ok else 400))
    if ok:
        return RedirectResponse(url="/portal/solido/import?imported=1", status_code=302)
    raise HTTPException(400, payload["message"])


_THUMB_MAX_B64 = 900_000          # ~660 KB decoded; an 800x500 viewer PNG is ~150-400 KB
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@router.post("/portal/solido/{publish_id:int}/thumbnail")
async def solido_thumbnail_upload(request: Request, publish_id: int):
    """Owner uploads the publish-time viewer snapshot (PNG, base64 JSON body).
    Session + CSRF gated, owner-checked, magic+size validated. Overwrites any
    previous thumb (re-publish refreshes the preview)."""
    import base64

    import blueprint_market
    from portal_auth import (CSRF_HEADER, SESSION_COOKIE, csrf_for_session,
                             get_portal_session, validate_csrf)

    accounts = _session_accounts(request)
    if not accounts:
        raise HTTPException(403, "Link your character to manage your listings")
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = request.headers.get(CSRF_HEADER, "")
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    # The listing's owner, or a portal session whose Discord id maps to the
    # admin role: the same server-side users.role lookup /portal/me reports,
    # never a claim the client sends. The admin lane exists for one job, the
    # backfill of previews on listings that predate publish-time thumbnails,
    # whose owners have no action left that would render one.
    as_admin = False
    discord_id = ""
    if blueprint_market.get_owned(publish_id, _account_id_set(accounts)) is None:
        from routers.portal import _roles_for_discord
        session = get_portal_session(request)
        discord_id = portal_identity.actor_key(session)
        if "admin" not in _roles_for_discord(discord_id):
            raise HTTPException(404, "Listing not found")
        admin_row = blueprint_market.get_public(publish_id)
        if admin_row is None:
            raise HTTPException(404, "Listing not found")
        # The lane is a BACKFILL, enforced here and not only by the button: an
        # admin may fill an empty preview, never replace a player's own render.
        if _has_thumb(publish_id):
            raise HTTPException(409, "That listing already has a preview")
        as_admin = True

    try:
        payload = await request.json()
        b64 = payload.get("png_base64") or ""
    except Exception:
        raise HTTPException(400, "Expected JSON body with png_base64")
    if not b64 or len(b64) > _THUMB_MAX_B64:
        raise HTTPException(413, "Thumbnail too large")
    try:
        body = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(400, "Invalid base64")
    if not body.startswith(_PNG_MAGIC):
        raise HTTPException(400, "Not a PNG")

    had_thumb = _has_thumb(publish_id)
    blueprint_market.save_thumbnail(publish_id, body)
    if as_admin:
        from auth import audit_log
        from portal_auth import client_ip
        audit_log(
            None, f"self:{discord_id}", "portal_blueprint_thumbnail_admin",
            str(publish_id), client_ip(request) or "",
            details=json.dumps({
                "bytes": len(body),
                "owner_account_id": admin_row.get("account_id"),
                "author_name": admin_row.get("author_name"),
                "had_thumb": had_thumb,
            }),
            success=True,
        )
    return JSONResponse({"ok": True})


@router.get("/portal/solido/{publish_id:int}/thumb.png")
async def solido_thumbnail(publish_id: int):
    """Public listing thumbnail. Only served while the listing is published."""
    import blueprint_market
    if blueprint_market.get_public(publish_id) is None:
        raise HTTPException(404, "Not found")
    path = blueprint_market.thumb_path(publish_id)
    if not blueprint_market.has_thumbnail(publish_id):
        raise HTTPException(404, "No thumbnail")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=600"})


# --------------------------------------------------------- shared helpers ---

def _account_id_set(accounts: list) -> set:
    return {int(a.get("account_id") or 0) for a in accounts if a.get("account_id")}


def _my_publish_ids(accounts: list) -> set:
    """Set of publish_ids owned by the caller (any non-removed status), so the
    gallery/detail can badge 'yours'."""
    if not accounts:
        return set()
    import blueprint_market
    rows = blueprint_market.list_by_accounts(_account_id_set(accounts))
    return {int(r["publish_id"]) for r in rows}


# Stand-in for a publisher who opted out of the player directory (ruling 9.2).
AUTHOR_STAND_IN = "a Fremen builder"


def _is_listed_cached(account_id, cache) -> bool:
    """The directory opt-out for one publisher, memoized for the request. A
    gallery page is 24 cards and the opt-out is honoured at READ time, so without
    the cache this would be 24 queries per page. A lookup failure returns the
    stand-in, never the name: the safe side of an opt-out is silence."""
    import player_profile
    aid = int(account_id or 0)
    if not aid:
        return False
    if cache is not None and aid in cache:
        return cache[aid]
    try:
        listed = player_profile.is_listed(aid)
    except Exception:  # noqa: BLE001
        logger.warning("card: is_listed failed (aid=%s)", aid, exc_info=True)
        listed = False
    if cache is not None:
        cache[aid] = listed
    return listed


def _card(row: dict, listed_cache: dict = None) -> dict:
    """Public card shape for the gallery JSON (no provenance/blob fields). The
    author line honours the player-directory opt-out at read time; the stored
    author_name is left alone so the audit trail still says who published it."""
    return {
        "publish_id": row.get("publish_id"),
        "title": row.get("title"),
        "author_name": (row.get("author_name")
                        if _is_listed_cached(row.get("account_id"), listed_cache)
                        else AUTHOR_STAND_IN),
        "piece_count": row.get("piece_count"),
        "instance_count": row.get("instance_count"),
        "placeable_count": row.get("placeable_count"),
        "pentashield_count": row.get("pentashield_count"),
        "faction": row.get("faction") or "neutral",
        "has_paid_pieces": bool(row.get("has_paid_pieces")),
        "tags": row.get("tags") or [],
        "user_tags": row.get("user_tags") or [],
        "download_count": row.get("download_count"),
        "created_at": row.get("created_at"),
        "share_url": f"{PORTAL_V2_SITE}bases/{row.get('publish_id')}",
        "thumb_url": (f"/portal/solido/{row.get('publish_id')}/thumb.png"
                      if _has_thumb(row.get("publish_id")) else None),
    }


def _has_thumb(publish_id) -> bool:
    import blueprint_market
    try:
        return blueprint_market.has_thumbnail(int(publish_id))
    except Exception:
        return False


# -------------------------------------------------- "My Bases" (own export) ---

def _session_accounts(request: Request) -> list:
    """The caller's own linked accounts. Empty list = not linked. Export is
    clamped to this set; a bp_id/account from the URL is never trusted."""
    from portal_auth import get_portal_session
    from routers.portal import _linked_characters
    session = get_portal_session(request)
    if session is None:
        return []
    did = portal_identity.actor_key(session)
    if not did:
        return []
    return _linked_characters(did)


@router.get("/portal/my-bases")
async def my_bases(request: Request):
    """Player's own bases (BuildingBlueprint_CopyDevice items), one section per
    linked character. Session required; lists via the ownership-gated relay for
    each linked account (never an arbitrary account_id)."""
    import blueprint_market
    from relay import call_relay
    from routers.portal import base_ctx
    accounts = _session_accounts(request)
    sections = []
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if not aid:
            continue
        try:
            raw = await call_relay(f"/dune/player/{aid}/blueprints", timeout=30)
        except Exception:
            logger.warning("my-bases: relay list failed (aid=%s)", aid, exc_info=True)
            raw = {"available": False, "blueprints": []}
        sections.append({
            "account_id": aid,
            "character_name": acct.get("character_name") or f"account #{aid}",
            "available": bool(raw.get("available")),
            "blueprints": raw.get("blueprints") or [],
        })
    # Inline publish state keyed by game_bp_id so each row can show
    # Published/Unpublish without a second round-trip.
    published = {}
    for r in blueprint_market.list_by_accounts(_account_id_set(accounts)):
        if r["status"] == "published":
            published[str(r["game_bp_id"])] = {
                "publish_id": int(r["publish_id"]),
                "download_count": r.get("download_count") or 0,
            }
    ctx = {
        "active_nav": "mybases",
        "linked": bool(accounts),
        "sections": sections,
        "published": published,
        "solido_site": SOLIDO_SITE,
    }
    ctx.update(base_ctx(request))
    return templates.TemplateResponse(request, "portal/my_bases.html", ctx)


@router.get("/portal/my-bases/{bp_id}/export")
async def my_bases_export(request: Request, bp_id: str):
    """Download one of the caller's OWN blueprints as Solido-format JSON. The
    account is resolved from the session's linked accounts (defense in depth;
    the relay+script re-verify ownership). bp_id format: the in-game item id
    (numeric), not a Solido UUID."""
    from relay import call_relay
    from auth import audit_log
    from portal_auth import get_portal_session

    if not re.match(r"^[0-9]{1,20}$", bp_id or ""):
        raise HTTPException(404, "Unknown blueprint")
    session = get_portal_session(request)
    accounts = _session_accounts(request)
    if not accounts:
        raise HTTPException(403, "Link your character to export bases")

    ip = (request.client.host if request.client else "") or ""
    actor = f"self:{portal_identity.actor_key(session) or '?'}"

    # Try each linked account; the relay returns not_owned for accounts that
    # don't hold this blueprint, so we stop at the first that does.
    last_err = "not_owned"
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if not aid:
            continue
        try:
            raw = await call_relay(
                f"/dune/player/{aid}/blueprint/{bp_id}/export", timeout=60)
        except Exception:
            logger.warning("my-bases export: relay failed (aid=%s bp=%s)",
                           aid, bp_id, exc_info=True)
            last_err = "relay_error"
            continue
        if not raw.get("available"):
            last_err = raw.get("error") or "unavailable"
            continue

        blueprint = raw.get("blueprint") or {}
        body = json.dumps(blueprint, indent=2, ensure_ascii=False).encode("utf-8")
        bp_name = raw.get("name") or blueprint.get("name") or f"Blueprint-{bp_id}"
        filename = _slug_filename(bp_name, f"blueprint-{bp_id}") + ".json"
        audit_log(
            None, actor, "portal_export_blueprint", str(aid), ip,
            details=json.dumps({
                "bp_id": bp_id,
                "bp_name": bp_name,
                "filename": filename,
                "byte_count": len(body),
                "instance_count": len(blueprint.get("instances") or []),
                "placeable_count": len(blueprint.get("placeables") or []),
                "result": "ok",
            }),
            success=True,
        )
        return Response(
            content=body, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    audit_log(
        None, actor, "portal_export_blueprint", "0", ip,
        details=json.dumps({"bp_id": bp_id, "result": f"unavailable: {last_err}"}),
        success=False,
    )
    raise HTTPException(404, "Blueprint not found for your linked characters")


# Friendly text for the rename writer's error tokens (writer/relay own these).
_RENAME_ERROR_TEXT = {
    "player_online": "Log out of the game first, then rename. Base names only "
                     "update while you are offline.",
    "no_player": "We could not find your in-game character right now. Try again shortly.",
    "not_owned": "That base does not belong to your linked characters.",
    "no_struct": "That base can't be renamed (unexpected item data).",
    "empty_name": "Enter a name.",
    "name_too_long": f"Keep the name to {RENAME_NAME_MAX} characters or fewer.",
}


@router.post("/portal/my-bases/{bp_id}/rename")
async def my_bases_rename(request: Request, bp_id: str):
    """Rename one of the caller's OWN base blueprints. Writes the in-game
    BuildingBlueprintName (propagates to this list, exports, and the publish default).
    OFFLINE-only (the writer hard-gates; the copy device is a RAM-backed inventory item).
    The account is resolved from the session's linked accounts and each is tried via the
    ownership-gated relay until one owns the blueprint (mirrors my_bases_export). Session +
    CSRF gated."""
    import blueprint_market
    from relay import call_relay
    from auth import audit_log
    from portal_auth import (CSRF_HEADER, SESSION_COOKIE, csrf_for_session,
                             get_portal_session, validate_csrf)

    if not re.match(r"^[0-9]{1,20}$", bp_id or ""):
        raise HTTPException(404, "Unknown blueprint")

    accounts = _session_accounts(request)
    if not accounts:
        raise HTTPException(403, "Link your character to rename bases")

    content_type = (request.headers.get("content-type", "") or "").lower()
    form, body_json = {}, {}
    if "application/json" in content_type:
        try:
            body_json = await request.json()
        except Exception:
            body_json = {}
    else:
        try:
            form = await request.form()
        except Exception:
            form = {}

    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided_csrf = (request.headers.get(CSRF_HEADER, "")
                     or (form.get("csrf_token", "") if form else "")
                     or body_json.get("csrf_token", ""))
    if not session_token or not validate_csrf(provided_csrf, csrf_for_session(session_token)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    raw_name = body_json.get("name") if body_json else form.get("name", "")
    name = re.sub(r"\s+", " ", str(raw_name or "")).strip()
    if not name:
        return JSONResponse({"ok": False, "error": _RENAME_ERROR_TEXT["empty_name"]},
                            status_code=400)
    if len(name) > RENAME_NAME_MAX:
        return JSONResponse({"ok": False, "error": _RENAME_ERROR_TEXT["name_too_long"]},
                            status_code=400)

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ip = (request.client.host if request.client else "") or ""

    # Try each linked account; the writer returns not_owned for accounts that don't hold
    # this blueprint, so we stop at the first that does.
    last_err = "not_owned"
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if not aid:
            continue
        try:
            result = await call_relay(
                "/dune/blueprint/rename", method="POST",
                json_body={"account_id": aid, "bp_id": int(bp_id), "name": name},
                timeout=35)
        except HTTPException as exc:
            logger.warning("my-bases rename: relay error (aid=%s bp=%s): %s",
                           aid, bp_id, exc.detail)
            last_err = "relay_error"
            continue
        except Exception:
            logger.warning("my-bases rename: relay failed (aid=%s bp=%s)",
                           aid, bp_id, exc_info=True)
            last_err = "relay_error"
            continue

        if isinstance(result, dict) and result.get("ok"):
            new_name = result.get("new_name") or name
            # Keep a still-published market listing's title in sync (only if it
            # was the default-ish title; a custom listing title is left alone).
            synced_publish_id = None
            try:
                synced_publish_id = blueprint_market.sync_listing_title_on_rename(
                    aid, int(bp_id), new_name, result.get("old_name"))
            except Exception:  # noqa: BLE001 - title sync is best-effort
                logger.warning("rename: listing title sync failed (aid=%s bp=%s)",
                               aid, bp_id, exc_info=True)
            audit_log(
                None, f"self:{discord_id or '?'}", "portal_blueprint_rename",
                str(aid), ip,
                details=json.dumps({
                    "bp_id": bp_id, "old_name": result.get("old_name"),
                    "new_name": new_name, "listing_synced": synced_publish_id,
                }),
                success=True,
            )
            return JSONResponse({"ok": True, "name": new_name,
                                 "listing_synced": synced_publish_id})

        last_err = (result.get("error") if isinstance(result, dict) else None) or "error"
        # not_owned just means "try the next linked account"; any other token is terminal.
        if last_err != "not_owned":
            break

    audit_log(
        None, f"self:{discord_id or '?'}", "portal_blueprint_rename", "0", ip,
        details=json.dumps({"bp_id": bp_id, "result": f"failed: {last_err}"}),
        success=False,
    )
    friendly = _RENAME_ERROR_TEXT.get(
        last_err, "That rename could not be completed. Please try again.")
    status = 409 if last_err == "player_online" else (
        404 if last_err == "not_owned" else 502 if last_err == "relay_error" else 400)
    return JSONResponse({"ok": False, "error": friendly}, status_code=status)


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug_filename(stem: str, default: str) -> str:
    cleaned = _FILENAME_SAFE_RE.sub("-", (stem or "").strip()).strip("-.")
    return (cleaned or default)[:120]


# ============================================================================
# V2 MODULE PORT (2026-07-15): Bases / Solido JSON siblings for the SvelteKit
# portal. NEW /v2 endpoints only; every V1 route above is byte-untouched.
# All-linked scope (V1 parity). Writes reuse the EXACT V1 flow: publish is a
# server-side re-export via the ownership-gated relay (never a client blueprint),
# import fires the shared G20 executor, rolling-24h caps + online/offline gates +
# CSRF are unchanged. Downloads + thumb.png stay on the V1 binary endpoints.
# Contract: docs/dune-research/v2-portal/V2-MODULE-PORTS-BUILD-CONTRACT-2026-07-15.md
# ============================================================================


def _v2_ok(data: dict = None, status: int = 200) -> JSONResponse:
    payload = {"ok": True}
    if data:
        payload.update(data)
    return JSONResponse(payload, status_code=status)


def _v2_err(error: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error, "message": message},
                        status_code=status)


async def _v2_json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _v2_csrf_ok(request: Request, body: dict) -> bool:
    from portal_auth import (CSRF_HEADER, SESSION_COOKIE, csrf_for_session,
                             validate_csrf)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    provided = (request.headers.get(CSRF_HEADER, "")
                or (body.get("csrf_token", "") if isinstance(body, dict) else ""))
    return bool(session_token) and validate_csrf(provided, csrf_for_session(session_token))


# Feature kill-switches (default ON). Surfaced in the overview flags so the UI can
# hide the buttons; also hard-gated server-side on the write paths.
def _publish_enabled() -> bool:
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_BLUEPRINT_PUBLISH_ENABLED", "1")


def _import_enabled() -> bool:
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_BLUEPRINT_IMPORT_ENABLED", "1")


# Public download bucket: in-process, per client IP, 30 a minute (ruling 9.6).
# Same shape as routers/dune.py's _check_dune_rate -- a counter that anyone can
# drive from a loop is a counter nobody can read.
_download_buckets: dict = defaultdict(deque)
_download_last_cleanup = 0.0
DOWNLOAD_RATE_LIMIT = 30
DOWNLOAD_RATE_WINDOW = 60

# Import history: how far back the list reaches, and how many rows it returns.
IMPORT_HISTORY_DAYS = 30
IMPORT_HISTORY_MAX = 20


def _check_download_rate(ip: str) -> bool:
    global _download_last_cleanup
    now = time.monotonic()
    cutoff = now - DOWNLOAD_RATE_WINDOW

    # Prune empty/stale buckets every 5 min so the dict can't grow unbounded
    # when the client IP varies (mirrors dune.py's sweep).
    if now - _download_last_cleanup > 300:
        _download_last_cleanup = now
        stale = [k for k, v in _download_buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _download_buckets[k]

    bucket = _download_buckets[ip]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= DOWNLOAD_RATE_LIMIT:
        return False
    bucket.append(now)
    return True


def _imported_today(accounts: list) -> int:
    """Rolling-24h import count across the caller's linked accounts: the same
    audit COUNT the import cap gates on, so the number the UI shows and the
    number that refuses the write can never disagree."""
    from auth import recent_action_count
    targets = [str(a) for a in _account_id_set(accounts)]
    if not targets:
        return 0
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return recent_action_count("portal_solido_import", since, targets=targets)


def _published_today(accounts: list) -> int:
    """Rolling-24h publish count across all the caller's linked accounts (the same
    durable publish-log COUNT the publish cap gates on)."""
    import blueprint_market
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    total = 0
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if aid:
            total += blueprint_market.recent_publish_count(aid, since)
    return total


def _v2_idem_key(body: dict) -> str:
    """Client idempotency key: accept `uuid` (frontend) or legacy `client_uuid`.
    Must be a UUID; otherwise a fresh one is minted so the write still proceeds."""
    import uuid as _uuid
    raw = str((body.get("uuid") or body.get("client_uuid") or "")).strip()
    try:
        _uuid.UUID(raw)
        return raw
    except (ValueError, AttributeError, TypeError):
        return str(_uuid.uuid4())


@router.get("/portal/bases/v2")
async def bases_v2_overview(request: Request):
    """V2 overview (JSON): the caller's linked characters, each with its own base
    blueprint section (ownership-gated relay list) + best-effort online flag, and
    per-blueprint published state nested inline. Caps + feature flags + CSRF.
    All-linked scope (V1 my_bases parity). Reads only. Auth: linked session."""
    import blueprint_market
    from config import BLUEPRINT_IMPORT_DAILY_CAP, BLUEPRINT_PUBLISH_DAILY_CAP
    from portal_auth import SESSION_COOKIE, csrf_for_session
    from relay import call_relay
    from rmq_command import is_online

    accounts = _session_accounts(request)
    if not accounts:
        return _v2_err("unauthenticated",
                       "Link your character to view your bases.", status=401)

    # Published state keyed by game_bp_id, nested per-blueprint below. Carries
    # the listing copy so the Edit form prefills from this one read.
    published_by_bp = {}
    for r in blueprint_market.list_by_accounts(_account_id_set(accounts)):
        if r["status"] == "published":
            published_by_bp[str(r["game_bp_id"])] = {
                "publish_id": int(r["publish_id"]),
                "download_count": r.get("download_count") or 0,
                "title": r.get("title") or "",
                "description": r.get("description") or "",
                "user_tags": r.get("user_tags") or [],
            }

    sections = []
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if not aid:
            continue
        try:
            raw = await call_relay(f"/dune/player/{aid}/blueprints", timeout=30)
        except Exception:
            logger.warning("bases/v2: relay list failed (aid=%s)", aid, exc_info=True)
            raw = {"available": False, "blueprints": []}
        try:
            online = bool(await is_online(aid))
        except Exception:
            online = False
        blueprints = []
        for bp in (raw.get("blueprints") or []):
            bp_id = bp.get("bp_id")
            blueprints.append({
                "bp_id": bp_id,
                "name": bp.get("name"),
                "piece_count": bp.get("piece_count"),
                "published": published_by_bp.get(str(bp_id)),
            })
        sections.append({
            "account_id": aid,
            "character_name": acct.get("character_name") or f"account #{aid}",
            "online": online,
            "available": bool(raw.get("available")),
            "blueprints": blueprints,
        })

    session_token = request.cookies.get(SESSION_COOKIE, "")
    return _v2_ok({
        "linked": bool(accounts),
        "sections": sections,
        "caps": {
            "publish_daily_cap": BLUEPRINT_PUBLISH_DAILY_CAP,
            "published_today": _published_today(accounts),
            "import_daily_cap": BLUEPRINT_IMPORT_DAILY_CAP,
            "imported_today": _imported_today(accounts),
            "rename_name_max": RENAME_NAME_MAX,
        },
        "flags": {
            "publish_enabled": _publish_enabled(),
            "import_enabled": _import_enabled(),
        },
        "csrf": csrf_for_session(session_token) if session_token else "",
    })


@router.get("/portal/solido/v2/market")
async def solido_v2_market(request: Request):
    """V2 community-market gallery (JSON, public, read-only). sort/tag/page.
    page is 1-based; maps to the same list_published offset/limit as V1."""
    import blueprint_market
    q = request.query_params
    sort = q.get("sort", "new")
    if sort not in ("new", "popular"):
        sort = "new"
    tag = (q.get("tag", "") or "").strip() or None
    try:
        page = int(q.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    limit = GALLERY_LIMIT_DEFAULT
    offset = (page - 1) * limit

    # limit+1 probe (see solido_hub): an exactly-full page must not hand the
    # client a "Load more" that fetches nothing.
    rows = blueprint_market.list_published(sort, tag, limit + 1, offset)
    has_more = len(rows) > limit
    listed = {}
    return _v2_ok({
        "listings": [_card(r, listed) for r in rows[:limit]],
        "page": page,
        "has_more": has_more,
    })


@router.get("/portal/solido/v2/tags")
async def solido_v2_tags(request: Request):
    """The closed filter vocabulary for the gallery chip rail (JSON, public):
    derived tags, size bands, and the player purpose tags. Served from the data
    layer so the rail and the tags actually stored can never drift apart."""
    import blueprint_market
    return _v2_ok(blueprint_market.tag_catalog())


@router.get("/portal/solido/v2/{publish_id:int}")
async def solido_v2_detail(request: Request, publish_id: int):
    """V2 listing detail (JSON, public). Canonical public card + description +
    owner/thumb flags. 404 envelope if missing/unpublished."""
    import blueprint_market
    row = blueprint_market.get_public(publish_id)
    if row is None:
        return _v2_err("not_found", "Blueprint not found", status=404)
    accounts = _session_accounts(request)
    card = _card(row)
    card["description"] = row.get("description") or ""
    card["is_owner"] = publish_id in _my_publish_ids(accounts)
    card["is_linked"] = bool(accounts)
    return _v2_ok({"listing": card})


@router.get("/portal/solido/v2/{publish_id:int}/blueprint")
async def solido_v2_blueprint(request: Request, publish_id: int):
    """V2 viewer blob (public). Raw blueprint JSON the 3D viewer consumes; viewing
    != downloading, so this does NOT increment the counter. Soft-degrades if the
    blob is gone. Reuses the V1 loader verbatim."""
    import blueprint_market
    row = blueprint_market.get_public(publish_id)
    if row is None:
        return _v2_err("not_found", "Blueprint not found", status=404)
    body = blueprint_market.read_blob(row)
    if body is None:
        return JSONResponse({"available": False})
    return Response(content=body, media_type="application/json")


@router.get("/portal/solido/v2/{publish_id:int}/download")
async def solido_v2_download(request: Request, publish_id: int):
    """V2 download (public, counts). The V1 body in the V2 envelope: same audit
    action name, same slugged attachment. A missing blob 404s WITHOUT counting --
    a download nobody received is not a download. The IP bucket is checked before
    the row read so a scripted run costs a dict lookup, not a query."""
    import blueprint_market
    from auth import audit_log

    # Behind Caddy the socket peer is the proxy; key the bucket (and the audit
    # row) on the forwarded client address like every other public lane.
    from portal_auth import client_ip
    ip = client_ip(request) or ""
    if not _check_download_rate(ip or "unknown"):
        return _v2_err("rate_limited",
                       "Too many downloads. Try again in a minute.", status=429)

    row = blueprint_market.get_public(publish_id)
    if row is None:
        return _v2_err("not_found", "Blueprint not found", status=404)
    body = blueprint_market.read_blob(row)
    if body is None:
        return _v2_err("not_found", "Blueprint file unavailable", status=404)

    blueprint_market.increment_download(publish_id)
    audit_log(
        None, "public", "portal_blueprint_download", str(publish_id), ip,
        details=json.dumps({"title": row.get("title"),
                            "author": row.get("author_name")}),
        success=True,
    )
    filename = _slug_filename(row.get("title") or "", f"blueprint-{publish_id}") + ".json"
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/portal/bases/v2/publish")
async def bases_v2_publish(request: Request):
    """V2 publish (JSON). Reuses the V1 authenticity gate EXACTLY: takes only
    game_bp_id + title/description; the blueprint is a server-side re-export via
    the ownership-gated relay (never a client upload). Session + CSRF, rolling-24h
    cap. create_or_update is keyed on (account_id, game_bp_id), so a re-publish is
    naturally idempotent."""
    import blueprint_market
    from config import BLUEPRINT_PUBLISH_DAILY_CAP
    from relay import call_relay
    from auth import audit_log
    from portal_auth import get_portal_session

    accounts = _session_accounts(request)
    if not accounts:
        return _v2_err("unauthenticated", "Link your character to publish.", status=401)
    if not _publish_enabled():
        return _v2_err("publish_disabled",
                       "Publishing is temporarily unavailable.", status=403)

    body = await _v2_json_body(request)
    if not _v2_csrf_ok(request, body):
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    game_bp_id = str(body.get("game_bp_id", "") or "").strip()
    if not re.match(r"^[0-9]{1,20}$", game_bp_id):
        return _v2_err("unknown_blueprint", "Unknown blueprint", status=404)
    # The publish form's own text. The BLUEPRINT is still a server-side re-export
    # and never a client upload; only the listing copy comes from the player. A
    # blank title falls back to the re-exported name below; hygiene and the caps
    # are create_or_update's. Tags are intersected with the closed vocabulary
    # there too, so an invented tag is dropped rather than refused.
    title = str(body.get("title", "") or "")
    description = str(body.get("description", "") or "")
    raw_tags = body.get("tags")
    user_tags = ([str(t) for t in raw_tags if isinstance(t, str)]
                 if isinstance(raw_tags, list) else [])

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ip = (request.client.host if request.client else "") or ""

    # Rolling-24h cap (durable publish-log COUNT), across all linked accounts.
    total_recent = _published_today(accounts)
    if total_recent >= BLUEPRINT_PUBLISH_DAILY_CAP:
        return _v2_err("rate_limited",
                       "Daily publish limit reached. Try again later.", status=429)

    # Server-side re-export (the invariant): first linked account that owns it.
    matched_aid = 0
    author_name = ""
    blueprint = None
    last_err = "not_owned"
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if not aid:
            continue
        try:
            raw = await call_relay(
                f"/dune/player/{aid}/blueprint/{game_bp_id}/export", timeout=60)
        except Exception:
            logger.warning("bases/v2 publish: relay failed (aid=%s bp=%s)",
                           aid, game_bp_id, exc_info=True)
            last_err = "relay_error"
            continue
        if not raw.get("available"):
            last_err = raw.get("error") or "unavailable"
            continue
        blueprint = raw.get("blueprint") or {}
        if not title:
            title = raw.get("name") or blueprint.get("name") or ""
        matched_aid = aid
        author_name = acct.get("character_name") or f"account #{aid}"
        break

    if blueprint is None:
        audit_log(
            None, f"self:{discord_id or '?'}", "portal_blueprint_publish", "0", ip,
            details=json.dumps({"game_bp_id": game_bp_id,
                                "result": f"unavailable: {last_err}"}),
            success=False,
        )
        return _v2_err("not_found",
                       "Blueprint not found for your linked characters", status=404)

    body_bytes = json.dumps(blueprint, ensure_ascii=False).encode("utf-8")
    if len(body_bytes) > blueprint_market.PUBLISH_BLOB_MAX:
        return _v2_err("too_large", "Base too large to publish.", status=413)

    meta = blueprint_market.derive_metadata(blueprint)
    row = blueprint_market.create_or_update(
        account_id=matched_aid, discord_id=discord_id, author_name=author_name,
        game_bp_id=int(game_bp_id), title=title, description=description,
        blueprint=blueprint, meta=meta, user_tags=user_tags)
    publish_id = int(row["publish_id"])

    audit_log(
        None, f"self:{discord_id or '?'}", "portal_blueprint_publish",
        str(matched_aid), ip,
        details=json.dumps({
            "publish_id": publish_id, "game_bp_id": game_bp_id,
            "title": row.get("title"), "piece_count": meta["piece_count"],
            "byte_count": len(body_bytes),
        }),
        success=True,
    )
    return _v2_ok({
        "publish_id": publish_id,
        "download_count": row.get("download_count") or 0,
        "published_today": _published_today(accounts),
    })


@router.post("/portal/bases/v2/unpublish")
async def bases_v2_unpublish(request: Request):
    """V2 author self-removal (JSON). Session + CSRF. Idempotent (a second call
    for an already-removed listing 404s). Blob retained. publish_id in the body."""
    import blueprint_market
    from auth import audit_log
    from portal_auth import get_portal_session

    accounts = _session_accounts(request)
    if not accounts:
        return _v2_err("unauthenticated",
                       "Link your character to manage your listings.", status=401)

    body = await _v2_json_body(request)
    if not _v2_csrf_ok(request, body):
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    try:
        publish_id = int(body.get("publish_id"))
    except (TypeError, ValueError):
        return _v2_err("bad_request", "publish_id must be an integer", status=400)

    ok = blueprint_market.unpublish(publish_id, _account_id_set(accounts))
    if not ok:
        return _v2_err("not_found", "Listing not found", status=404)

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ip = (request.client.host if request.client else "") or ""
    audit_log(
        None, f"self:{discord_id or '?'}", "portal_blueprint_unpublish",
        str(publish_id), ip, details=None, success=True,
    )
    return _v2_ok()


@router.post("/portal/bases/v2/rename")
async def bases_v2_rename(request: Request):
    """V2 rename (JSON). Writes the in-game BuildingBlueprintName (OFFLINE-only;
    the writer hard-gates). Session + CSRF. bp_id + name in the body; each linked
    account is tried via the ownership-gated relay until one owns it (mirrors V1)."""
    import blueprint_market
    from relay import call_relay
    from auth import audit_log
    from portal_auth import get_portal_session

    accounts = _session_accounts(request)
    if not accounts:
        return _v2_err("unauthenticated",
                       "Link your character to rename bases.", status=401)

    body = await _v2_json_body(request)
    if not _v2_csrf_ok(request, body):
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    bp_id = str(body.get("bp_id", "") or "").strip()
    if not re.match(r"^[0-9]{1,20}$", bp_id):
        return _v2_err("unknown_blueprint", "Unknown blueprint", status=404)
    name = re.sub(r"\s+", " ", str(body.get("name", "") or "")).strip()
    if not name:
        return _v2_err("empty_name", _RENAME_ERROR_TEXT["empty_name"], status=400)
    if len(name) > RENAME_NAME_MAX:
        return _v2_err("name_too_long", _RENAME_ERROR_TEXT["name_too_long"], status=400)

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ip = (request.client.host if request.client else "") or ""

    last_err = "not_owned"
    for acct in accounts:
        aid = int(acct.get("account_id") or 0)
        if not aid:
            continue
        try:
            result = await call_relay(
                "/dune/blueprint/rename", method="POST",
                json_body={"account_id": aid, "bp_id": int(bp_id), "name": name},
                timeout=35)
        except HTTPException as exc:
            logger.warning("bases/v2 rename: relay error (aid=%s bp=%s): %s",
                           aid, bp_id, exc.detail)
            last_err = "relay_error"
            continue
        except Exception:
            logger.warning("bases/v2 rename: relay failed (aid=%s bp=%s)",
                           aid, bp_id, exc_info=True)
            last_err = "relay_error"
            continue

        if isinstance(result, dict) and result.get("ok"):
            new_name = result.get("new_name") or name
            synced_publish_id = None
            try:
                synced_publish_id = blueprint_market.sync_listing_title_on_rename(
                    aid, int(bp_id), new_name, result.get("old_name"))
            except Exception:  # noqa: BLE001 - title sync is best-effort
                logger.warning("bases/v2 rename: listing title sync failed "
                               "(aid=%s bp=%s)", aid, bp_id, exc_info=True)
            audit_log(
                None, f"self:{discord_id or '?'}", "portal_blueprint_rename",
                str(aid), ip,
                details=json.dumps({
                    "bp_id": bp_id, "old_name": result.get("old_name"),
                    "new_name": new_name, "listing_synced": synced_publish_id,
                }),
                success=True,
            )
            return _v2_ok({"name": new_name, "listing_synced": synced_publish_id})

        last_err = (result.get("error") if isinstance(result, dict) else None) or "error"
        if last_err != "not_owned":
            break

    audit_log(
        None, f"self:{discord_id or '?'}", "portal_blueprint_rename", "0", ip,
        details=json.dumps({"bp_id": bp_id, "result": f"failed: {last_err}"}),
        success=False,
    )
    friendly = _RENAME_ERROR_TEXT.get(
        last_err, "That rename could not be completed. Please try again.")
    status = 409 if last_err == "player_online" else (
        404 if last_err == "not_owned" else 502 if last_err == "relay_error" else 400)
    return _v2_err(last_err, friendly, status=status)


@router.post("/portal/bases/v2/import")
async def bases_v2_import(request: Request):
    """V2 import (JSON). Materializes a Solido blueprint as a new in-game copy
    device in the caller's OWN character bank (online-safe) or backpack (offline
    only). Self-scoped, session + CSRF, rolling-24h cap. Reuses the shared G20
    executor; the recipient is forced to a linked account. CLIENT-UUID
    idempotency: the client `uuid` (or legacy `client_uuid`) becomes the grant
    idempotency key so a network-timeout retry never double-imports."""
    from config import BLUEPRINT_IMPORT_DAILY_CAP
    from auth import audit_log, recent_action_count
    from portal_auth import get_portal_session
    from rmq_command import is_online
    from routers.dune_grant import _execute_one_grant

    accounts = _session_accounts(request)
    if not accounts:
        return _v2_err("unauthenticated",
                       "Link your character to import blueprints.", status=401)
    if not _import_enabled():
        return _v2_err("import_disabled",
                       "Importing is temporarily unavailable.", status=403)

    body = await _v2_json_body(request)
    if not _v2_csrf_ok(request, body):
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    # Recipient is forced to one of the caller's OWN linked accounts.
    my_ids = _account_id_set(accounts)
    try:
        account_id = int(body.get("account_id", 0) or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id == 0 and len(my_ids) == 1:
        account_id = next(iter(my_ids))
    if account_id not in my_ids:
        return _v2_err("bad_recipient",
                       "Choose one of your linked characters", status=403)

    delivery = str(body.get("delivery", "") or "").strip().lower()
    if delivery not in ("bank", "backpack"):
        return _v2_err("bad_delivery",
                       "Choose a destination: bank or backpack", status=400)

    source_type = str(body.get("source_type", "") or "").strip().lower()
    try:
        detail_src = _import_detail_from_source(
            source_type,
            publish_id=body.get("publish_id"),
            link=str(body.get("link", "") or ""),
            blueprint_text=(str(body.get("blueprint_text", "") or "") or None),
        )
    except HTTPException as exc:
        return _v2_err("bad_source", str(exc.detail), status=exc.status_code)

    session = get_portal_session(request)
    discord_id = portal_identity.actor_key(session)
    ip = (request.client.host if request.client else "") or ""

    # Backpack is RAM-fragile (offline only). The writer also hard-gates.
    try:
        online = bool(await is_online(account_id))
    except Exception:
        online = False
    if delivery == "backpack" and online:
        return _v2_err(
            "online",
            "Log out of the game first to deliver to your backpack, or pick the "
            "CHOAM bank (works while you are online).", status=409)

    # Per-player rolling-24h import cap.
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    recent = recent_action_count(
        "portal_solido_import", since, targets=[str(a) for a in my_ids])
    if recent >= BLUEPRINT_IMPORT_DAILY_CAP:
        return _v2_err("rate_limited",
                       "Daily import limit reached. Try again later.", status=429)

    # Client idempotency key: accept `uuid` (frontend) or legacy `client_uuid`.
    idem_key = _v2_idem_key(body)

    detail = {"delivery": delivery, **detail_src}
    portal_user = {"id": None, "username": f"portal:{discord_id or '?'}"}
    result = await _execute_one_grant(
        user=portal_user, ip=ip, account_id=account_id,
        grant_type="import_blueprint", detail_in=detail,
        idempotency_key=idem_key,
    )

    ok = bool(result.get("success"))
    audit_log(
        None, f"self:{discord_id or '?'}", "portal_solido_import",
        str(account_id), ip,
        details=json.dumps({
            "delivery": delivery, "source_type": source_type,
            "source": detail_src.get("source_blueprint_id") or detail_src.get("blueprint_id"),
            "result": result.get("status"),
            "result_message": (result.get("message") or "")[:500],
            "grant_id": result.get("grant_id"),
        }),
        success=ok,
    )

    if ok:
        if delivery == "bank":
            msg = ("Imported to your CHOAM bank. Relog or change zones in-game "
                   "and the new Solido tool will appear in your bank.")
        else:
            msg = ("Imported to your backpack. It will be waiting in your "
                   "inventory the next time you log into the game.")
        return _v2_ok({
            "delivery": delivery, "online": online, "message": msg,
            "grant_id": result.get("grant_id"),
        })
    return _v2_err("import_failed", result.get("message") or "Import failed.",
                   status=400)


@router.get("/portal/bases/v2/imports")
async def bases_v2_imports(request: Request):
    """The caller's own recent imports (JSON, session-gated), newest first. Read
    from the same audit rows the import cap counts. The stored row also carries
    the source link/UUID and the grant id; neither leaves the server -- this is a
    "did my import land" list, not a provenance dump."""
    from auth import recent_action_rows

    accounts = _session_accounts(request)
    if not accounts:
        return _v2_err("unauthenticated",
                       "Link your character to see your imports.", status=401)

    since = (datetime.now(timezone.utc)
             - timedelta(days=IMPORT_HISTORY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in recent_action_rows("portal_solido_import",
                                sorted(_account_id_set(accounts)),
                                since, IMPORT_HISTORY_MAX):
        try:
            detail = json.loads(r.get("details") or "{}")
        except (TypeError, ValueError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        rows.append({
            "at": r.get("timestamp"),
            "delivery": detail.get("delivery") or "",
            "source_type": detail.get("source_type") or "",
            "result": detail.get("result") or "",
            "message": (detail.get("result_message") or "")[:200],
        })
    return _v2_ok({"rows": rows})
