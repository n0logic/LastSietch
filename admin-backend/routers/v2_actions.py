import json
import os
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, require_admin, require_csrf
from rmq_command import dispatch_server_command, resolve_fls_ref

router = APIRouter(prefix="/api/dune/v2")

# Give-item typeahead catalog (built by scripts/build-give-item-catalog.py from
# item-data.json; committed so the admin-backend has no runtime market-bot dep).
# Maps the native template_ids the give-item verb accepts to friendly names so
# admins search "Granite Stone" instead of guessing the template `Stone`.
_GIVE_ITEM_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "dune-give-item-catalog.json",
)
# Version-controlled asset; reload only when the file mtime moves (deploy).
_give_item_catalog_cache: dict = {"data": None, "mtime": 0.0}


def _load_give_item_catalog() -> dict:
    try:
        mtime = os.path.getmtime(_GIVE_ITEM_CATALOG_PATH)
    except OSError:
        raise HTTPException(500, "give-item catalog not found")
    cached = _give_item_catalog_cache["data"]
    if cached is not None and mtime == _give_item_catalog_cache["mtime"]:
        return cached
    try:
        with open(_GIVE_ITEM_CATALOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "give-item catalog is unreadable")
    _give_item_catalog_cache["data"] = data
    _give_item_catalog_cache["mtime"] = mtime
    return data


@router.get("/catalog/give-items")
async def v2_give_item_catalog(request: Request):
    """Item-template typeahead catalog for the Live Actions give-item field.
    Admin-gated (no CSRF — read-only GET). Returns {items:[{id,name,cat}...]},
    sorted by display name; the UI fetches it once and filters client-side."""
    require_admin(request)
    return _load_give_item_catalog()

# Typed-CONFIRM tokens the UI must echo before an apply fires. Mirrors the
# moderation trio (CONFIRM_KICK/BAN/UNBAN) and broadcast (CONFIRM_TOKEN).
CONFIRM_RESCUE = "RESCUE"
CONFIRM_GIVE = "GIVE"
CONFIRM_XP = "XP"
CONFIRM_WATER = "WATER"

MAX_REASON = 200  # the relay wrapper caps reason at 200 chars (MAX_STR)

# Per-admin debounce on LIVE applies so a stuck finger can't carpet a player
# with give/teleport spam. Dry-run previews are never debounced. Mirrors
# dune_broadcast._last_apply.
APPLY_DEBOUNCE_S = 10
_last_apply: dict = {}  # (user_id, verb) -> monotonic timestamp of last live apply

# UI-side validation bounds. The relay wrapper (dune-server-command-send.py) is
# authoritative; these keep errors friendly and reject obviously bad input early.
QTY_MIN, QTY_MAX = 1, 1000
DUR_MIN, DUR_MAX = 0.0, 1.0
XP_MIN, XP_MAX = 1, 10_000_000
WATER_MIN, WATER_MAX = 1, 100


class RescueTeleportRequest(BaseModel):
    x: float
    y: float
    z: float
    exact: bool = False  # admins are trusted; exact coords allowed (vs portal snap)
    reason: str
    mode: str = "dry-run"  # apply | dry-run
    confirm: str | None = None  # must equal CONFIRM_RESCUE for apply


class GiveItemRequest(BaseModel):
    item: str
    qty: int = 1
    durability: float = 1.0
    reason: str
    mode: str = "dry-run"
    confirm: str | None = None


class AwardXpRequest(BaseModel):
    category: str
    experience: int
    reason: str
    mode: str = "dry-run"
    confirm: str | None = None


class RefillWaterRequest(BaseModel):
    water_amount: int = 100
    reason: str
    mode: str = "dry-run"
    confirm: str | None = None


def _validate_account_id(account_id: str) -> int:
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    return int(account_id)


def _redact_reason(text: str | None) -> str:
    """Audit-safe reason: trim, cap at 200 chars. Mirrors v2_moderation."""
    if not text:
        return ""
    return text.strip()[:200]


def _check_mode_and_confirm(body, confirm_token: str, verb: str, user: dict) -> None:
    """Shared apply-gate: mode must be valid; apply requires the typed confirm
    token + per-admin/verb debounce. Raises HTTPException on any failure."""
    if body.mode not in ("apply", "dry-run"):
        raise HTTPException(400, "mode must be apply or dry-run")
    if body.mode == "apply":
        if body.confirm != confirm_token:
            raise HTTPException(400, f'live action requires confirm="{confirm_token}"')
        key = (user["id"], verb)
        last = _last_apply.get(key)
        now = time.monotonic()
        if last is not None and now - last < APPLY_DEBOUNCE_S:
            wait = int(APPLY_DEBOUNCE_S - (now - last)) + 1
            raise HTTPException(429, f"slow down: wait {wait}s before another live {verb}")


async def _dispatch(
    verb: str, account_id: int, mode: str, operator: str, reason: str, args: dict,
) -> dict:
    """Resolve the hex FLS id and POST the job to the relay server-command
    route. Returns the relay envelope ({success, mode, detail, verb, ...}).
    Raises HTTPException on resolution failure (never sends a guessed id)."""
    resolve_ref = await resolve_fls_ref(account_id)
    if not resolve_ref:
        raise HTTPException(
            422, "could not resolve this account's FLS id — refusing to send"
        )
    return await dispatch_server_command(
        resolve_ref, verb, mode=mode, operator=operator, reason=reason, args=args,
    )


def _finish(
    user: dict, verb: str, mode: str, success: bool, res: dict,
    audit_fn, extra: dict | None = None,
) -> dict:
    """Start the debounce clock on a successful live apply, audit, and shape
    the response. Mirrors dune_broadcast's tail."""
    if success and mode == "apply":
        _last_apply[(user["id"], verb)] = time.monotonic()
    audit_fn(success, {"result": res, **(extra or {})})
    return {
        "success": success,
        "mode": mode,
        "detail": (res.get("detail") or "")[:2000],
    }


@router.post("/player/{account_id}/_rescue_teleport")
async def v2_rescue_teleport(request: Request, account_id: str, body: RescueTeleportRequest):
    """Teleport one ONLINE player to chosen coordinates. Admin + CSRF gated;
    apply requires a typed confirm + per-admin debounce; audited on every path.
    Admins are trusted, so exact coords (args.exact) are allowed here (the
    portal self-rescue is the constrained, snap-only variant)."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    acct = _validate_account_id(account_id)

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_live_rescue_teleport", str(acct), ip,
            details=json.dumps({
                "mode": body.mode,
                "x": body.x, "y": body.y, "z": body.z,
                "exact": bool(body.exact),
                "reason": _redact_reason(body.reason),
                **extra,
            }),
            success=success,
        )

    try:
        for coord in (body.x, body.y, body.z):
            if isinstance(coord, bool) or not isinstance(coord, (int, float)):
                raise HTTPException(400, "x/y/z must be numbers")
        if not isinstance(body.reason, str) or not body.reason.strip():
            raise HTTPException(400, "reason is required")
        if len(body.reason) > MAX_REASON:
            raise HTTPException(400, f"reason exceeds {MAX_REASON} chars")
        _check_mode_and_confirm(body, CONFIRM_RESCUE, "teleport", user)
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    args = {"x": float(body.x), "y": float(body.y), "z": float(body.z), "exact": bool(body.exact)}
    try:
        res = await _dispatch("teleport", acct, body.mode, user["username"], body.reason.strip(), args)
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"success": False, "mode": body.mode, "detail": str(exc.detail)[:1000]}
    except Exception as exc:
        _audit(False, {"result": f"error: {exc}"})
        return {"success": False, "mode": body.mode, "detail": str(exc)[:1000]}

    return _finish(user, "teleport", body.mode, bool(res.get("success")), res, _audit)


@router.post("/player/{account_id}/_give_item")
async def v2_give_item(request: Request, account_id: str, body: GiveItemRequest):
    """Give one item template to an ONLINE player. Admin + CSRF gated; apply
    requires a typed confirm + debounce; audited on every path. Charset/qty/
    durability bounds mirror the relay wrapper so errors are friendly; the
    wrapper is authoritative."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    acct = _validate_account_id(account_id)

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_live_give_item", str(acct), ip,
            details=json.dumps({
                "mode": body.mode,
                "item": (body.item or "")[:200],
                "qty": body.qty,
                "durability": body.durability,
                "reason": _redact_reason(body.reason),
                **extra,
            }),
            success=success,
        )

    try:
        if not isinstance(body.item, str) or not body.item.strip():
            raise HTTPException(400, "item template is required")
        # Wrapper charset: [A-Za-z0-9_] (item.replace("_","").isalnum()).
        if len(body.item) > MAX_REASON or not body.item.replace("_", "").isalnum():
            raise HTTPException(400, "item template must be alphanumeric/underscore")
        if isinstance(body.qty, bool) or not isinstance(body.qty, int) \
                or not (QTY_MIN <= body.qty <= QTY_MAX):
            raise HTTPException(400, f"qty must be an integer {QTY_MIN}..{QTY_MAX}")
        if isinstance(body.durability, bool) or not isinstance(body.durability, (int, float)) \
                or not (DUR_MIN <= body.durability <= DUR_MAX):
            raise HTTPException(400, f"durability must be {DUR_MIN}..{DUR_MAX}")
        if not isinstance(body.reason, str) or not body.reason.strip():
            raise HTTPException(400, "reason is required")
        if len(body.reason) > MAX_REASON:
            raise HTTPException(400, f"reason exceeds {MAX_REASON} chars")
        _check_mode_and_confirm(body, CONFIRM_GIVE, "give-item", user)
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    args = {"item": body.item.strip(), "qty": body.qty, "durability": float(body.durability)}
    try:
        res = await _dispatch("give-item", acct, body.mode, user["username"], body.reason.strip(), args)
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"success": False, "mode": body.mode, "detail": str(exc.detail)[:1000]}
    except Exception as exc:
        _audit(False, {"result": f"error: {exc}"})
        return {"success": False, "mode": body.mode, "detail": str(exc)[:1000]}

    return _finish(user, "give-item", body.mode, bool(res.get("success")), res, _audit)


@router.post("/player/{account_id}/_award_xp")
async def v2_award_xp(request: Request, account_id: str, body: AwardXpRequest):
    """Award experience in one category to an ONLINE player. Admin + CSRF
    gated; apply requires a typed confirm + debounce; audited on every path."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    acct = _validate_account_id(account_id)

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_live_award_xp", str(acct), ip,
            details=json.dumps({
                "mode": body.mode,
                "category": (body.category or "")[:200],
                "experience": body.experience,
                "reason": _redact_reason(body.reason),
                **extra,
            }),
            success=success,
        )

    try:
        if not isinstance(body.category, str) or not body.category.strip():
            raise HTTPException(400, "category is required")
        # Wrapper charset: [A-Za-z0-9_] (cat.replace("_","").isalnum()).
        if len(body.category) > MAX_REASON or not body.category.replace("_", "").isalnum():
            raise HTTPException(400, "category must be alphanumeric/underscore")
        if isinstance(body.experience, bool) or not isinstance(body.experience, int) \
                or not (XP_MIN <= body.experience <= XP_MAX):
            raise HTTPException(400, f"experience must be an integer {XP_MIN}..{XP_MAX}")
        if not isinstance(body.reason, str) or not body.reason.strip():
            raise HTTPException(400, "reason is required")
        if len(body.reason) > MAX_REASON:
            raise HTTPException(400, f"reason exceeds {MAX_REASON} chars")
        _check_mode_and_confirm(body, CONFIRM_XP, "award-xp", user)
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    args = {"category": body.category.strip(), "experience": body.experience}
    try:
        res = await _dispatch("award-xp", acct, body.mode, user["username"], body.reason.strip(), args)
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"success": False, "mode": body.mode, "detail": str(exc.detail)[:1000]}
    except Exception as exc:
        _audit(False, {"result": f"error: {exc}"})
        return {"success": False, "mode": body.mode, "detail": str(exc)[:1000]}

    return _finish(user, "award-xp", body.mode, bool(res.get("success")), res, _audit)


@router.post("/player/{account_id}/_refill_water")
async def v2_refill_water(request: Request, account_id: str, body: RefillWaterRequest):
    """Refill an ONLINE player's water. Admin + CSRF gated; apply requires a
    typed confirm + debounce; audited on every path."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    acct = _validate_account_id(account_id)

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_live_refill_water", str(acct), ip,
            details=json.dumps({
                "mode": body.mode,
                "water_amount": body.water_amount,
                "reason": _redact_reason(body.reason),
                **extra,
            }),
            success=success,
        )

    try:
        if isinstance(body.water_amount, bool) or not isinstance(body.water_amount, int) \
                or not (WATER_MIN <= body.water_amount <= WATER_MAX):
            raise HTTPException(400, f"water_amount must be an integer {WATER_MIN}..{WATER_MAX}")
        if not isinstance(body.reason, str) or not body.reason.strip():
            raise HTTPException(400, "reason is required")
        if len(body.reason) > MAX_REASON:
            raise HTTPException(400, f"reason exceeds {MAX_REASON} chars")
        _check_mode_and_confirm(body, CONFIRM_WATER, "refill-water", user)
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    args = {"water_amount": body.water_amount}
    try:
        res = await _dispatch("refill-water", acct, body.mode, user["username"], body.reason.strip(), args)
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"success": False, "mode": body.mode, "detail": str(exc.detail)[:1000]}
    except Exception as exc:
        _audit(False, {"result": f"error: {exc}"})
        return {"success": False, "mode": body.mode, "detail": str(exc)[:1000]}

    return _finish(user, "refill-water", body.mode, bool(res.get("success")), res, _audit)
