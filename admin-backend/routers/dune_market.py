"""V2 admin panel API for the live Last Sietch market-maker bot (lastsietch-market-bot).

Read + control surface backing /v2/server/market. All routes are admin-gated;
writes (policy edit, service control) also require CSRF and are audited. Data
flows admin-backend -> relay (/dune/market/*) -> lastsietch-dune dispatcher ->
dune-market-control.py. No DB or SSH here; the relay owns that path.
"""
import json
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, require_admin, require_csrf
from relay import call_relay

router = APIRouter(prefix="/api/dune")

SERVICE_VERBS = ("start", "stop", "restart")


class PolicyRequest(BaseModel):
    policy: dict


class ServiceRequest(BaseModel):
    verb: str


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@router.get("/market/status")
async def market_status(request: Request):
    """Bot service state + balance + ls_market_log activity. Admin-only."""
    require_admin(request)
    try:
        return await call_relay("/dune/market/status", timeout=45)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"market status unavailable: {exc}")


@router.get("/market/listings")
async def market_listings(request: Request, q: str = ""):
    """Search active CHOAM exchange listings by template_id fragment. Admin-only."""
    require_admin(request)
    q = (q or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", q):
        raise HTTPException(400, "search term must be 2-64 chars of letters, digits, _ or -")
    try:
        return await call_relay(f"/dune/market/listings?q={q}", timeout=45)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"listing search unavailable: {exc}")


@router.get("/market/policy")
async def market_policy_get(request: Request):
    """Current market-policy.json (price floors, weekly budgets, blocked sellers)."""
    require_admin(request)
    try:
        return await call_relay("/dune/market/policy", timeout=30)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"market policy unavailable: {exc}")


@router.post("/market/policy")
async def market_policy_set(request: Request, body: PolicyRequest):
    """Replace market-policy.json. Admin + CSRF + audited. Applies on next restart."""
    user = require_admin(request)
    require_csrf(request, user)
    if not isinstance(body.policy, dict):
        raise HTTPException(400, "policy must be a JSON object")
    success = False
    try:
        result = await call_relay("/dune/market/policy", method="POST",
                                  json_body=body.policy, timeout=45)
        success = isinstance(result, dict) and result.get("status") == "ok"
        return result
    finally:
        audit_log(
            user["id"], user["username"], "dune_market_policy_set",
            "market-policy.json", _ip(request),
            details=json.dumps({
                "price_overrides": len(body.policy.get("price_overrides", {}) or {}),
                "blocked_sellers": len(body.policy.get("blocked_sellers", []) or []),
                "has_weekly_budget": bool(body.policy.get("weekly_budget")),
            }),
            success=success,
        )


@router.post("/market/service")
async def market_service(request: Request, body: ServiceRequest):
    """start | stop | restart the lastsietch-market-bot service. Admin + CSRF + audited."""
    user = require_admin(request)
    require_csrf(request, user)
    if body.verb not in SERVICE_VERBS:
        raise HTTPException(400, "verb must be start|stop|restart")
    success = False
    try:
        result = await call_relay("/dune/market/service", method="POST",
                                  json_body={"verb": body.verb}, timeout=45)
        success = isinstance(result, dict) and result.get("status") == "ok"
        return result
    finally:
        audit_log(
            user["id"], user["username"], "dune_market_service",
            body.verb, _ip(request), success=success,
        )
