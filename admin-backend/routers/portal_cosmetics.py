"""Settings > Appearance cosmetics picker read (COSMETICS-MAP-ICONS-PLAN, P0.3).

One route: the titles and map dots the caller owns and which of each is worn.
It exists so the picker outlives the Seven Nights page, whose nav entry closes
with LASTSIETCH_SIETCH_NIGHTS_ENABLED. Writes go through the existing prefs PUT, which
re-checks ownership (portal_prefs.SIETCH_OWNED); nothing is written here.

Dark (LASTSIETCH_COSMETIC_TITLES_ENABLED off) answers {ok:true, enabled:false} at 200,
so the picker simply does not render.
"""
from fastapi import APIRouter, Request

import cosmetics
from database import get_db
from routers.portal import _require_linked_session_json, _v2_ok

router = APIRouter()


@router.get("/portal/cosmetics")
async def portal_cosmetics(request: Request):
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    session, discord_id, account_id, _row = gate
    if not cosmetics.enabled():
        return _v2_ok({"enabled": False})
    conn = get_db()
    try:
        view = cosmetics.owned_view(conn, session, account_id, discord_id)
    finally:
        conn.close()
    return _v2_ok(dict(view, enabled=True))
