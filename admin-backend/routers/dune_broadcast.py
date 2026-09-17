import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, require_admin, require_csrf
from relay import call_relay

router = APIRouter(prefix="/api/dune")

MAX_TITLE = 80
MAX_MESSAGE = 280
MIN_DURATION = 1
MAX_DURATION = 600
CONFIRM_TOKEN = "BROADCAST"
# Per-admin debounce on LIVE sends so a stuck finger can't carpet the whole
# server with banners. Dry-run previews are never debounced.
APPLY_DEBOUNCE_S = 30
_last_apply: dict = {}  # user_id -> monotonic timestamp of last live send


class BroadcastSendRequest(BaseModel):
    title: str
    message: str
    duration: int = 30
    mode: str = "dry-run"  # apply | dry-run
    confirm: str | None = None  # must equal CONFIRM_TOKEN for apply


@router.post("/broadcast/send")
async def broadcast_send(request: Request, body: BroadcastSendRequest):
    """Publish one in-game ServiceBroadcast (Generic system banner) to every
    connected player as the fls_backend service. Admin + CSRF gated, audited on
    every path. Live sends (mode=apply) require a typed confirm token and are
    debounced 30s per admin. dry-run previews the exact envelope without
    publishing anything."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_broadcast_send", body.mode or "?", ip,
            details=json.dumps({
                "mode": body.mode,
                "duration": body.duration,
                "title_len": len(body.title or ""),
                "message_len": len(body.message or ""),
                **extra,
            }),
            success=success,
        )

    try:
        if not isinstance(body.title, str) or not body.title.strip():
            raise HTTPException(400, "title is required")
        if not isinstance(body.message, str) or not body.message.strip():
            raise HTTPException(400, "message is required")
        if len(body.title) > MAX_TITLE:
            raise HTTPException(400, f"title exceeds {MAX_TITLE} chars")
        if len(body.message) > MAX_MESSAGE:
            raise HTTPException(400, f"message exceeds {MAX_MESSAGE} chars")
        if isinstance(body.duration, bool) or not isinstance(body.duration, int) \
                or not (MIN_DURATION <= body.duration <= MAX_DURATION):
            raise HTTPException(400, f"duration must be an integer {MIN_DURATION}..{MAX_DURATION}")
        if body.mode not in ("apply", "dry-run"):
            raise HTTPException(400, "mode must be apply or dry-run")
        if body.mode == "apply":
            if body.confirm != CONFIRM_TOKEN:
                raise HTTPException(400, f'live broadcast requires confirm="{CONFIRM_TOKEN}"')
            last = _last_apply.get(user["id"])
            now = time.monotonic()
            if last is not None and now - last < APPLY_DEBOUNCE_S:
                wait = int(APPLY_DEBOUNCE_S - (now - last)) + 1
                raise HTTPException(429, f"slow down: wait {wait}s before another live broadcast")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    job = {
        "title": body.title,
        "message": body.message,
        "duration": body.duration,
        "mode": body.mode,
        "operator": user["username"],
    }

    try:
        res = await call_relay("/dune/broadcast/send", "POST", job, timeout=60)
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"success": False, "mode": body.mode, "detail": str(exc.detail)[:1000]}
    except Exception as exc:
        _audit(False, {"result": f"relay_error: {exc}"})
        return {"success": False, "mode": body.mode, "detail": str(exc)[:1000]}

    success = bool(res.get("success"))
    # Only start the debounce clock once a live send actually succeeded.
    if success and body.mode == "apply":
        _last_apply[user["id"]] = time.monotonic()
    _audit(success, {"result": res})
    return {
        "success": success,
        "mode": body.mode,
        "detail": (res.get("detail") or "")[:2000],
    }
