"""Admin-only Solido thumbnail backfill.

The portal auto-generates a market thumbnail at publish time (My Bases ->
Publish), but listings published before that feature -- or where the offscreen
render failed -- have no preview photo, so the gallery shows "no preview". This
admin tool re-renders any published listing from its public blueprint blob
(client-side, in the operator's browser, via the same three.js viewer) and
uploads the snapshot. Unlike the portal upload endpoint (owner-gated), this
writes a thumbnail for ANY listing -- the admin override.

Routes (app-level; served under /admin/* by the proxy):
- GET  /dashboard/dune/solido-thumbs           -- backfill page (admin session)
- GET  /api/dune/solido/missing-thumbs          -- published listings w/o a thumb
- POST /api/dune/solido/{publish_id}/thumbnail  -- save a PNG for any listing
"""
import base64

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import audit_log, get_current_user, require_admin, require_csrf

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Match the portal thumbnail endpoint's limits (solido.py): an 800x500 viewer
# PNG is ~150-400 KB; cap the base64 body well above that.
_THUMB_MAX_B64 = 900_000
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@router.get("/dashboard/dune/solido-thumbs")
async def solido_thumbs_page(request: Request):
    try:
        user = get_current_user(request)
    except HTTPException:
        return RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return RedirectResponse(url="/admin/", status_code=302)
    return templates.TemplateResponse(request, "solido_thumbs.html", {"user": user})


@router.get("/api/dune/solido/missing-thumbs")
async def solido_missing_thumbs(request: Request):
    """Published listings that have no thumbnail file yet (admin only)."""
    require_admin(request)
    import blueprint_market

    rows = blueprint_market.list_published("new", None, 500, 0)
    missing = []
    for r in rows:
        pid = int(r.get("publish_id"))
        if not blueprint_market.has_thumbnail(pid):
            missing.append({
                "publish_id": pid,
                "title": r.get("title"),
                "author_name": r.get("author_name"),
                "piece_count": r.get("piece_count"),
            })
    return JSONResponse({
        "ok": True,
        "missing": missing,
        "total_published": len(rows),
    })


@router.get("/api/dune/solido/{publish_id:int}/blueprint")
async def solido_admin_blueprint(request: Request, publish_id: int):
    """Same-origin blob for the backfill viewer. The portal twin lives at
    /portal/api/solido/market/{id}/blueprint, but on admin.lastsietch.com that
    path 301-redirects cross-origin to lastsietch.com (blocked by the admin
    page's connect-src 'self' CSP), so the backfill must read it from here."""
    require_admin(request)
    import blueprint_market

    row = blueprint_market.get_public(publish_id)
    if row is None:
        raise HTTPException(404, "Blueprint not found or not published")
    body = blueprint_market.read_blob(row)
    if body is None:
        return JSONResponse({"available": False})
    return Response(content=body, media_type="application/json")


@router.post("/api/dune/solido/{publish_id:int}/thumbnail")
async def solido_admin_thumbnail(request: Request, publish_id: int):
    """Save a viewer snapshot as the listing's thumbnail. Admin override of the
    owner-gated portal endpoint: writes for any published listing. PNG validated
    by magic bytes + size, same as the portal path."""
    user = require_admin(request)
    require_csrf(request, user)
    import blueprint_market

    if blueprint_market.get_public(publish_id) is None:
        raise HTTPException(404, "Listing not found or not published")

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

    blueprint_market.save_thumbnail(publish_id, body)
    ip = request.client.host if request.client else ""
    audit_log(user["id"], user["username"], "solido_thumbnail_backfill",
              str(publish_id), ip, success=True)
    return JSONResponse({"ok": True})
