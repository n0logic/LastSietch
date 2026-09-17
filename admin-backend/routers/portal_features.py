"""The public feature-gate read (Fremkit wave 12, lane A).

The Help page hides an entry for a feature that has not opened yet. It needs to
know which gates are on, and it is a PUBLIC page, so this route is public too.

The legacy exposure is a fixed tuple of gate names, written out here rather than
read from anywhere: `feature_flags` can resolve any LASTSIETCH_-prefixed name, and a
route that forwarded an arbitrary name would turn a help page into a read of the
operator's whole switchboard. Nothing else about a gate is exposed either. Not
the environment value, not the override, not whether an override exists: one
boolean per named feature and no more.
Profile login availability also checks configured credentials and private storage.

Public, cacheable for a minute. A gate flip is a deliberate operator action and
sixty seconds of staleness on a help page is not worth an uncacheable read on
every page load.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import feature_flags

logger = logging.getLogger("portal")
router = APIRouter()

# Public legacy gates; unsupported storage stays closed and login readiness is computed below.
FEATURE_GATES = (
    ("chat", "LASTSIETCH_CHAT_ENABLED"),
    ("refinery", "LASTSIETCH_REFINERY_ENABLED"),
    ("storage_move", "LASTSIETCH_STORAGE_MOVE_ENABLED"),
    ("market_sell_backpack", "LASTSIETCH_MARKET_SELL_BACKPACK_ENABLED"),
    ("augment", "LASTSIETCH_AUGMENT_ENABLED"),
    ("reports", "LASTSIETCH_REPORTS_ENABLED"),
)

# --- routes ------------------------------------------------------------------


@router.get("/portal/features")
async def portal_features():
    """{ok: true, features: {name: bool}} over the five gates above. Every gate
    defaults to OFF ("0"), the same coded default its own feature carries, so a
    feature whose environment says nothing reads closed here too."""
    features = {name: bool(feature_flags.enabled(flag, "0"))
                for name, flag in FEATURE_GATES}
    # No native container-move backend exists; an operator flag is insufficient.
    features["storage_move"] = False
    features['profile_login'] = False
    features['game_login'] = False
    try:
        import config
        from database import get_db
        from routers.portal_signin import enabled
        conn = get_db()
        try:
            features['profile_login'] = enabled(conn)
            features['game_login'] = bool(enabled(conn) and config.PORTAL_GAME_AUTH_ENABLED)
        finally:
            conn.close()
    except Exception:
        pass
    return JSONResponse({"ok": True, "features": features},
                        headers={"Cache-Control": "public, max-age=60"})
