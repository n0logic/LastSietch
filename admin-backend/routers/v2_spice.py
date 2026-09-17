import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, require_admin, require_csrf
from relay import call_relay

router = APIRouter(prefix="/api/dune/v2")

# 15s cache on the spice-types list (8 rows; the live counts move slowly and
# the toggle re-fetches on its own). Same shape as the bans-list cache in
# v2_moderation.
SPICE_CACHE_TTL = 15
_spice_cache: dict = {"data": None, "fetched_at": 0.0}


class SpiceToggleRequest(BaseModel):
    new_value: bool


@router.get("/spice/types")
async def v2_spice_types(request: Request):
    """List dune.spicefield_types (8 rows) for the Spice sub-card. Admin-only
    (no CSRF; read-only). 15s in-memory cache."""
    require_admin(request)
    now = time.monotonic()
    cached = _spice_cache["data"]
    if cached is not None and now - _spice_cache["fetched_at"] < SPICE_CACHE_TTL:
        return cached
    try:
        raw = await call_relay("/server/spice/types", timeout=30)
    except Exception as exc:
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "types": [], "error": str(exc)[:300]}
    data = {
        "available": bool(raw.get("available", True)),
        "types": raw.get("types") or [],
    }
    _spice_cache["data"] = data
    _spice_cache["fetched_at"] = now
    return data


@router.post("/spice/types/{type_id}/spawning")
async def v2_spice_set_spawning(request: Request, type_id: str, body: SpiceToggleRequest):
    """Flip is_spawning_active for one spicefield type. Admin + CSRF gated;
    audit on every path. v1 = boolean only (Decision A). Online-safe: toggling
    off does not despawn active fields, only suppresses the next spawn-tick.
    The relay performs the actual write; this handler validates, mints the
    change_id, audits, and forwards the full envelope (incl. who)."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    if not type_id.isdigit():
        raise HTTPException(400, "type_id must be a positive integer")
    type_id_int = int(type_id)
    change_id = str(uuid.uuid4())

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_spice_toggle", str(type_id_int), ip,
            details=json.dumps({
                "new_value": body.new_value,
                "change_id": change_id,
                **extra,
            }),
            success=success,
        )

    payload = {
        "new_value": body.new_value,
        "who": user["username"],
        "change_id": change_id,
    }

    try:
        result = await call_relay(
            f"/server/spice/types/{type_id_int}/spawning", "POST", payload, timeout=45,
        )
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"ok": False, "detail": str(exc.detail)[:500]}
    except Exception as exc:
        _audit(False, {"result": f"error: {exc}"})
        return {"ok": False, "detail": str(exc)[:500]}

    # Invalidate the cache so the next GET reflects the new boolean.
    _spice_cache["data"] = None

    success = bool(result.get("ok", False))
    _audit(success, {
        "result": "ok" if success else "failed",
        "error": (result.get("error") or "")[:500],
    })
    return result
