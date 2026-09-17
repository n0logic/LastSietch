"""VC3 — Player Workbench routes (grants hub).

`/admin/v2/players/grants?aid={N}` is the player-pivot workbench. Live state
cards + recent grants + preset library + multi-recipient preset fire.

The grant FIRE path reuses the existing relay POST /dune/grant per-op; each
op gets a UUID idempotency_key and the relay's grant-postprocess endpoint
stamps batch_id + preset_name onto the audit row after success.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from auth import audit_log, get_current_user, require_admin, require_csrf
from data.grant_catalog import cart_catalog, find_entry, grant_catalog
from relay import call_relay
from routers.dune_grant import (
    _execute_one_grant,
    _load_catalog,
    _maybe_fire_rmq,
    _validate_detail as _authoritative_validate_detail,
    grant_players,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Per-batch cap. Brief §2.2 + A05 — 25 hard ceiling for multi-recipient preset
# fires. The preset itself can have N ops, so true ceiling = 25 × N grants.
MAX_RECIPIENTS_PER_BATCH = 25

# VC4 cart caps. MAX_CART_LINES bounds the cart size; MAX_TOTAL_FIRES is the
# hard backstop on recipients × (sum of ops across all lines). The client warns
# above 200; the server hard-rejects above 500.
MAX_CART_LINES = 25
MAX_TOTAL_FIRES = 500

# Landsraad house -> in-game rep location. Single source of truth now lives in
# data/house_reps.py (shared with the public portal landsraad page); imported
# here to annotate the item_live house_name dropdown so the operator sees where
# each house's rep is. DA_HouseArgosaz (Griffins Reach) is the Hagga-south
# starter rep and is the default pick when active.
from data.house_reps import HOUSE_REP_LOCATIONS

_LANDSRAAD_REP_HELP = (
    "Queued as a Landsraad reward at the selected house's rep; the player travels "
    "to that rep to claim. Online-safe. Rep locations: "
    "method.gg/dune-awakening/all-landsraad-house-representative-locations-in-dune-awakening"
)

# Preset cache — relay-side helper hits PG, hot path doesn't need it every render.
_preset_cache: dict[str, Any] = {"ts": 0.0, "payload": None}
PRESET_CACHE_TTL = 60.0


def _admin_or_redirect(request: Request):
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


async def _safe_call(coro):
    try:
        return await coro
    except HTTPException as exc:
        logger.warning("_safe_call: HTTPException %s: %s", exc.status_code, exc.detail)
        return None
    except Exception:
        logger.warning("_safe_call: unexpected exception", exc_info=True)
        return None


async def _load_presets() -> list[dict[str, Any]]:
    """Read preset library with a 60s TTL cache. Falls back to empty list on relay error."""
    now = time.monotonic()
    if _preset_cache["payload"] is not None and (now - _preset_cache["ts"]) < PRESET_CACHE_TTL:
        return _preset_cache["payload"]
    try:
        data = await call_relay("/dune/preset/list", timeout=15)
    except HTTPException as exc:
        logger.warning("preset list relay failed: %s", exc.detail)
        return _preset_cache["payload"] or []
    presets = (data or {}).get("presets") or []
    _preset_cache["ts"] = now
    _preset_cache["payload"] = presets
    return presets


async def _load_player_summary(request: Request, account_id: int) -> dict[str, Any] | None:
    """Pull the player picker row (online flag + grace + character + faction).
    Reuses grant_players() — already used by the v1 grant tool."""
    payload = await _safe_call(grant_players(request))
    if not payload or not isinstance(payload, dict):
        return None
    for p in (payload.get("players") or []):
        try:
            if int(p.get("account_id", -1)) == account_id:
                return p
        except (TypeError, ValueError):
            continue
    return None


# --- Workbench shell page ---------------------------------------------------

@router.get("/v2/players/grants")
async def v2_players_workbench(request: Request, aid: int | None = None):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    presets = await _load_presets()
    player = None
    recent = None
    if aid is not None and aid > 0:
        player = await _load_player_summary(request, aid)
        try:
            recent = await call_relay(f"/dune/player/{aid}/recent_grants?limit=10", timeout=15)
        except HTTPException as exc:
            logger.warning("recent_grants relay failed: %s", exc.detail)
            recent = {"account_id": aid, "grants": [], "error": exc.detail}

    return templates.TemplateResponse(
        request,
        "v2/players_workbench.html",
        {
            "user": user,
            "current_tab": "grants",
            "current_sub_tab": "grants",
            "aid": aid,
            "player": player,
            "presets": presets,
            "recent": recent or {"grants": []},
            "max_batch": MAX_RECIPIENTS_PER_BATCH,
        },
    )


# --- HTMX fragments (30s poll) ----------------------------------------------

@router.get("/v2/players/grants/_recent")
async def v2_players_recent_fragment(request: Request, aid: int):
    require_admin(request)
    if aid < 1:
        raise HTTPException(400, "aid must be a positive integer")
    try:
        recent = await call_relay(f"/dune/player/{aid}/recent_grants?limit=10", timeout=15)
    except HTTPException as exc:
        recent = {"account_id": aid, "grants": [], "error": exc.detail}
    return templates.TemplateResponse(
        request,
        "v2/_fragments/workbench_recent.html",
        {"recent": recent or {"grants": []}, "aid": aid},
    )


@router.get("/v2/players/grants/_live_state")
async def v2_players_live_state_fragment(request: Request, aid: int):
    require_admin(request)
    if aid < 1:
        raise HTTPException(400, "aid must be a positive integer")
    player = await _load_player_summary(request, aid)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/workbench_live_state.html",
        {"player": player, "aid": aid},
    )


@router.get("/v2/players/grants/_presets")
async def v2_players_presets_fragment(request: Request):
    require_admin(request)
    presets = await _load_presets()
    return templates.TemplateResponse(
        request,
        "v2/_fragments/workbench_presets.html",
        {"presets": presets},
    )


# --- Catalog (VC3 v1.2) -----------------------------------------------------

@router.get("/v2/players/grants/_catalog")
async def v2_players_catalog_fragment(request: Request):
    """Single-grant catalog rendered as a category accordion. Backs the
    Custom Grants tab on the Workbench."""
    require_admin(request)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/workbench_catalog.html",
        {"catalog": grant_catalog()},
    )


# --- Preset fire (multi-recipient batch) ------------------------------------

class PresetFireRequest(BaseModel):
    preset_name: str
    recipients: list[int]
    parameters: dict[str, str] = {}     # runtime params (e.g. {"faction": "atreides"})
    confirm_token: str | None = None
    live_delivery: bool = False         # prefer RMQ live delivery to online recipients


# Derived params: when a preset takes `faction` as a choice, we auto-expose
# `faction_id` so ops_json can reference both `{{faction}}` and `{{faction_id}}`.
_FACTION_ID_MAP = {"atreides": 1, "harkonnen": 2}


def _derive_params(values: dict[str, str]) -> dict[str, Any]:
    """Augment user-provided param values with derivable companions."""
    out: dict[str, Any] = dict(values)
    if "faction" in values and "faction_id" not in out:
        fid = _FACTION_ID_MAP.get(values["faction"])
        if fid is not None:
            out["faction_id"] = fid
    return out


def _substitute(node: Any, params: dict[str, Any]) -> Any:
    """Recursively walk a JSON-shaped value, replacing string leaves of form
    '{{name}}' with params[name]. Non-string leaves and non-template strings
    pass through. Raises ValueError on a placeholder we can't resolve."""
    if isinstance(node, str):
        s = node.strip()
        if s.startswith("{{") and s.endswith("}}"):
            key = s[2:-2].strip()
            if key not in params:
                raise ValueError(f"unresolved template parameter: {{{{{key}}}}}")
            return params[key]
        return node
    if isinstance(node, list):
        return [_substitute(x, params) for x in node]
    if isinstance(node, dict):
        return {k: _substitute(v, params) for k, v in node.items()}
    return node


def _validate_params(preset: dict[str, Any], provided: dict[str, str]) -> None:
    """Raise HTTPException if the provided param values don't match the
    preset's declared parameters schema (required-fields + choice-validity)."""
    schema = preset.get("parameters") or []
    declared_names = {p.get("name") for p in schema if isinstance(p, dict)}
    for name in declared_names:
        if name not in provided:
            raise HTTPException(400, f"missing required parameter: {name}")
    extras = set(provided.keys()) - declared_names
    if extras:
        raise HTTPException(400, f"unknown parameters: {sorted(extras)}")
    for p in schema:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        ptype = p.get("type")
        if ptype == "choice":
            choices = p.get("choices") or []
            if provided.get(name) not in choices:
                raise HTTPException(
                    400,
                    f"parameter {name!r} must be one of {choices} (got {provided.get(name)!r})",
                )


@router.post("/v2/players/grants/preset/fire")
async def v2_players_preset_fire(request: Request, body: PresetFireRequest):
    user = require_admin(request)
    require_csrf(request, user)

    ip = request.client.host if request.client else "unknown"

    if not body.preset_name or len(body.preset_name) > 64:
        raise HTTPException(400, "invalid preset_name")
    if not body.recipients:
        raise HTTPException(400, "at least one recipient is required")
    if len(body.recipients) > MAX_RECIPIENTS_PER_BATCH:
        raise HTTPException(400, f"max {MAX_RECIPIENTS_PER_BATCH} recipients per batch")
    for r in body.recipients:
        if not isinstance(r, int) or r < 1:
            raise HTTPException(400, "recipient ids must be positive integers")
    if body.confirm_token != "FIRE":
        raise HTTPException(400, "confirm_token must be the literal string 'FIRE'")

    presets = await _load_presets()
    preset = next((p for p in presets if p.get("name") == body.preset_name), None)
    if preset is None:
        raise HTTPException(404, f"preset not found: {body.preset_name}")

    ops = preset.get("ops") or []
    if not ops:
        raise HTTPException(400, "preset has no ops")

    # Validate + substitute templated parameters (e.g. {{faction}}).
    _validate_params(preset, body.parameters)
    derived = _derive_params(body.parameters)
    try:
        ops = _substitute(ops, derived)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    batch_id = str(uuid.uuid4())
    ops_fired = 0
    ops_failed = 0
    errors: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []

    for aid in body.recipients:
        for op in ops:
            idem = str(uuid.uuid4())
            # Prefer live RMQ delivery for eligible ops to online recipients;
            # _maybe_fire_rmq returns None to fall back to the offline DB path.
            if body.live_delivery:
                live = await _maybe_fire_rmq(
                    user, ip, aid, op.get("grant_type"), op.get("detail") or {},
                    idem, "apply", batch_id, body.preset_name,
                )
                if live is not None:
                    if live.get("success"):
                        ops_fired += 1
                        grants.append({
                            "account_id": aid, "grant_type": op.get("grant_type"),
                            "grant_id": None, "status": live.get("status"),
                        })
                    else:
                        ops_failed += 1
                        errors.append({
                            "account_id": aid, "grant_type": op.get("grant_type"),
                            "error": live.get("message") or "live fire failed",
                        })
                    continue
            relay_body = {
                "account_id": aid,
                "grant_type": op.get("grant_type"),
                "detail": op.get("detail") or {},
                "idempotency_key": idem,
                "operator": user["username"],
                "mode": "apply",
                "defer_if_online": True,
            }
            try:
                result = await call_relay("/dune/grant", "POST", relay_body, timeout=60)
            except HTTPException as exc:
                ops_failed += 1
                errors.append({
                    "account_id": aid,
                    "grant_type": op.get("grant_type"),
                    "error": exc.detail[:200] if isinstance(exc.detail, str) else str(exc.detail),
                })
                continue

            if not bool(result.get("success", False)):
                ops_failed += 1
                errors.append({
                    "account_id": aid,
                    "grant_type": op.get("grant_type"),
                    "error": result.get("message") or "fire returned success=false",
                })
                continue

            ops_fired += 1
            grant_id = result.get("grant_id")
            grants.append({
                "account_id": aid,
                "grant_type": op.get("grant_type"),
                "grant_id": grant_id,
                "status": result.get("status"),
            })

            # Best-effort postprocess. A failed UPDATE means batch_id/preset_name
            # aren't recorded, but the grant itself is applied — DO NOT raise.
            if isinstance(grant_id, int):
                try:
                    await call_relay(
                        "/dune/grant/postprocess",
                        "POST",
                        {
                            "grant_id": grant_id,
                            "batch_id": batch_id,
                            "preset_name": body.preset_name,
                        },
                        timeout=15,
                    )
                except HTTPException as exc:
                    logger.warning("postprocess failed for grant_id=%s: %s", grant_id, exc.detail)

    audit_log(
        user["id"], user["username"], "dune_preset_fire",
        f"{body.preset_name} x{len(body.recipients)}", ip,
        details=json.dumps({
            "preset_name": body.preset_name,
            "recipients": body.recipients,
            "parameters": body.parameters,
            "batch_id": batch_id,
            "ops_fired": ops_fired,
            "ops_failed": ops_failed,
            "error_count": len(errors),
        }),
        success=(ops_failed == 0),
    )

    return {
        "success": ops_failed == 0,
        "batch_id": batch_id,
        "preset_name": body.preset_name,
        "ops_fired": ops_fired,
        "ops_failed": ops_failed,
        "errors": errors,
        "grants": grants,
    }


# --- Custom single-grant fire (VC3 v1.2) ------------------------------------

class CustomGrantFireRequest(BaseModel):
    grant_type: str
    detail: dict[str, Any]
    recipients: list[int]
    confirm_token: str | None = None
    live_delivery: bool = False         # prefer RMQ live delivery to online recipients


def _validate_detail(entry: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Validate operator-provided detail against the catalog field schema.
    Returns the (possibly defaulted) detail dict ready for the relay payload.
    Raises HTTPException(400) on any violation."""
    fields = entry.get("fields") or []
    cleaned: dict[str, Any] = {}
    declared = {f["name"] for f in fields}

    extras = set(detail.keys()) - declared
    if extras:
        raise HTTPException(400, f"unknown detail fields for {entry['grant_type']}: {sorted(extras)}")

    for f in fields:
        name = f["name"]
        ftype = f["type"]
        required = bool(f.get("required", False))
        provided = name in detail
        value: Any
        if provided:
            value = detail[name]
        elif "default" in f:
            value = f["default"]
        elif required:
            raise HTTPException(400, f"missing required field: {name}")
        else:
            continue  # optional, no default — leave it out

        if ftype == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"field {name!r} must be an integer")
            if "min" in f and value < f["min"]:
                raise HTTPException(400, f"field {name!r} below min ({f['min']})")
            if "max" in f and value > f["max"]:
                raise HTTPException(400, f"field {name!r} above max ({f['max']})")
        elif ftype == "choice":
            choices = f.get("choices") or []
            valid_values = [
                (c["value"] if isinstance(c, dict) else c) for c in choices
            ]
            if value not in valid_values:
                raise HTTPException(
                    400,
                    f"field {name!r} must be one of {valid_values} (got {value!r})",
                )
        elif ftype == "bool":
            if not isinstance(value, bool):
                if isinstance(value, str) and value.lower() in ("true", "false"):
                    value = value.lower() == "true"
                else:
                    raise HTTPException(400, f"field {name!r} must be boolean")
        elif ftype == "text":
            if not isinstance(value, str) or not value:
                raise HTTPException(400, f"field {name!r} must be a non-empty string")
            if len(value) > 256:
                raise HTTPException(400, f"field {name!r} exceeds 256 chars")
        else:
            raise HTTPException(500, f"unsupported field type {ftype!r} in catalog")

        cleaned[name] = value

    return cleaned


@router.post("/v2/players/grants/custom/fire")
async def v2_players_custom_fire(request: Request, body: CustomGrantFireRequest):
    """Single-grant fire from the Custom Grants catalog. One op × N recipients,
    each row tagged with batch_id (no preset_name for custom fires)."""
    user = require_admin(request)
    require_csrf(request, user)

    ip = request.client.host if request.client else "unknown"

    entry = find_entry(body.grant_type)
    if entry is None:
        raise HTTPException(404, f"grant_type not in catalog: {body.grant_type}")

    if not body.recipients:
        raise HTTPException(400, "at least one recipient is required")
    if len(body.recipients) > MAX_RECIPIENTS_PER_BATCH:
        raise HTTPException(400, f"max {MAX_RECIPIENTS_PER_BATCH} recipients per batch")
    for r in body.recipients:
        if not isinstance(r, int) or r < 1:
            raise HTTPException(400, "recipient ids must be positive integers")
    if body.confirm_token != "FIRE":
        raise HTTPException(400, "confirm_token must be the literal string 'FIRE'")

    cleaned_detail = _validate_detail(entry, body.detail)

    batch_id = str(uuid.uuid4()) if len(body.recipients) > 1 else None
    ops_fired = 0
    ops_failed = 0
    errors: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []

    for aid in body.recipients:
        idem = str(uuid.uuid4())
        # Prefer live RMQ delivery for eligible grants to online recipients;
        # _maybe_fire_rmq returns None to fall back to the offline DB path.
        if body.live_delivery:
            live = await _maybe_fire_rmq(
                user, ip, aid, body.grant_type, cleaned_detail,
                idem, "apply", batch_id, None,
            )
            if live is not None:
                if live.get("success"):
                    ops_fired += 1
                    grants.append({
                        "account_id": aid, "grant_type": body.grant_type,
                        "grant_id": None, "status": live.get("status"),
                    })
                else:
                    ops_failed += 1
                    errors.append({
                        "account_id": aid, "grant_type": body.grant_type,
                        "error": live.get("message") or "live fire failed",
                    })
                continue
        relay_body = {
            "account_id": aid,
            "grant_type": body.grant_type,
            "detail": cleaned_detail,
            "idempotency_key": idem,
            "operator": user["username"],
            "mode": "apply",
            "defer_if_online": True,
        }
        try:
            result = await call_relay("/dune/grant", "POST", relay_body, timeout=60)
        except HTTPException as exc:
            ops_failed += 1
            errors.append({
                "account_id": aid,
                "grant_type": body.grant_type,
                "error": exc.detail[:200] if isinstance(exc.detail, str) else str(exc.detail),
            })
            continue

        if not bool(result.get("success", False)):
            ops_failed += 1
            errors.append({
                "account_id": aid,
                "grant_type": body.grant_type,
                "error": result.get("message") or "fire returned success=false",
            })
            continue

        ops_fired += 1
        grant_id = result.get("grant_id")
        grants.append({
            "account_id": aid,
            "grant_type": body.grant_type,
            "grant_id": grant_id,
            "status": result.get("status"),
        })

        if batch_id and isinstance(grant_id, int):
            try:
                await call_relay(
                    "/dune/grant/postprocess",
                    "POST",
                    {"grant_id": grant_id, "batch_id": batch_id, "preset_name": None},
                    timeout=15,
                )
            except HTTPException as exc:
                logger.warning("postprocess failed for grant_id=%s: %s", grant_id, exc.detail)

    audit_log(
        user["id"], user["username"], "dune_custom_grant",
        f"{body.grant_type} x{len(body.recipients)}", ip,
        details=json.dumps({
            "grant_type": body.grant_type,
            "detail": cleaned_detail,
            "recipients": body.recipients,
            "batch_id": batch_id,
            "ops_fired": ops_fired,
            "ops_failed": ops_failed,
            "error_count": len(errors),
        }),
        success=(ops_failed == 0),
    )

    return {
        "success": ops_failed == 0,
        "batch_id": batch_id,
        "grant_type": body.grant_type,
        "ops_fired": ops_fired,
        "ops_failed": ops_failed,
        "errors": errors,
        "grants": grants,
    }


# --- VC4 multi-grant cart (index + batch fire) ------------------------------

async def _active_houses(request: Request) -> list[str]:
    """Active house names from the player picker payload (best-effort, []
    on relay failure). Used to populate the item_live house_name dropdown."""
    payload = await _safe_call(grant_players(request))
    if not isinstance(payload, dict):
        return []
    houses: list[str] = []
    for h in (payload.get("active_houses") or []):
        if isinstance(h, str):
            houses.append(h)
        elif isinstance(h, dict):
            name = h.get("house_name") or h.get("name")
            if isinstance(name, str):
                houses.append(name)
    return houses


def _preset_index_entry(preset: dict[str, Any], offline_by_type: dict[str, bool]) -> dict[str, Any]:
    ops = preset.get("ops") or []
    ram_fragile = any(
        offline_by_type.get((op or {}).get("grant_type"), False) for op in ops
    )
    return {
        "name": preset.get("name"),
        "display": preset.get("display") or preset.get("name"),
        "description": preset.get("description") or "",
        "ram_fragile": ram_fragile,
        "parameters": preset.get("parameters") or [],
        "ops": ops,
    }


@router.get("/v2/players/grants/_index")
async def v2_players_grants_index(request: Request):
    """Unified JSON catalog for the cart Workbench: derived cart catalog +
    the preset library. The client builds its search index and config forms
    from this; no HTML scraping. Admin-only."""
    require_admin(request)

    catalog = cart_catalog()

    # item_live house_name choices are runtime state; inject the active houses
    # so the dropdown is populated; allow_custom keeps manual entry available.
    houses = await _active_houses(request)
    if houses:
        house_choices = [
            {
                "value": h,
                "label": (h + " (" + HOUSE_REP_LOCATIONS[h] + ")") if h in HOUSE_REP_LOCATIONS else h,
            }
            for h in houses
        ]
        default_house = "DA_HouseArgosaz" if "DA_HouseArgosaz" in houses else None
        for entry in catalog:
            if entry.get("grant_type") != "item_live":
                continue
            for field in entry.get("fields", []):
                if field.get("name") == "house_name":
                    field["choices"] = house_choices
                    field["widget"] = "select"
                    field["help"] = _LANDSRAAD_REP_HELP
                    if default_house:
                        field["default"] = default_house

    # Authoritative requires_offline for ALL grant types (presets may reference
    # types outside the cart allowlist).
    try:
        full = _load_catalog()
        offline_by_type = {
            g.get("id"): bool(g.get("requires_offline"))
            for g in full.get("grant_types", []) if g.get("id")
        }
    except HTTPException:
        offline_by_type = {}

    presets = await _load_presets()
    preset_entries = [_preset_index_entry(p, offline_by_type) for p in presets]

    return {"presets": preset_entries, "catalog": catalog}


class CartLine(BaseModel):
    kind: str                              # "preset" | "custom"
    preset_name: str | None = None
    grant_type: str | None = None
    parameters: dict[str, str] = {}
    detail: dict[str, Any] = {}


class CartFireRequest(BaseModel):
    recipients: list[int]
    lines: list[CartLine]
    confirm_token: str | None = None
    live_delivery: bool = False         # prefer RMQ live delivery to online recipients


def _expand_cart_lines(
    lines: list[CartLine], presets: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any], str | None]]:
    """Expand cart lines into a flat list of (grant_type, detail, preset_name)
    ops for a single recipient. Malformed input raises HTTPException(4xx) so the
    whole batch is rejected before any grant fires (no partial cart fires).

    Custom lines are gated for cart-eligibility via find_entry, then validated
    with the SAME authoritative validator the executor uses (no drift); preset
    lines are templated via the existing param substitution helpers."""
    catalog = _load_catalog()
    expanded: list[tuple[str, dict[str, Any], str | None]] = []
    for idx, line in enumerate(lines):
        if line.kind == "preset":
            if not line.preset_name:
                raise HTTPException(400, f"lines[{idx}]: preset_name is required for a preset line")
            preset = next((p for p in presets if p.get("name") == line.preset_name), None)
            if preset is None:
                raise HTTPException(404, f"lines[{idx}]: preset not found: {line.preset_name}")
            ops = preset.get("ops") or []
            if not ops:
                raise HTTPException(400, f"lines[{idx}]: preset has no ops")
            _validate_params(preset, line.parameters)
            derived = _derive_params(line.parameters)
            try:
                ops = _substitute(ops, derived)
            except ValueError as exc:
                raise HTTPException(400, f"lines[{idx}]: {exc}")
            for op in ops:
                expanded.append((op.get("grant_type"), op.get("detail") or {}, line.preset_name))
        elif line.kind == "custom":
            if not line.grant_type:
                raise HTTPException(400, f"lines[{idx}]: grant_type is required for a custom line")
            if find_entry(line.grant_type) is None:
                raise HTTPException(404, f"lines[{idx}]: grant_type not cart-eligible: {line.grant_type}")
            try:
                cleaned = _authoritative_validate_detail(catalog, line.grant_type, line.detail)
            except HTTPException as exc:
                raise HTTPException(400, f"lines[{idx}]: {exc.detail}")
            expanded.append((line.grant_type, cleaned, None))
        else:
            raise HTTPException(400, f"lines[{idx}]: kind must be 'preset' or 'custom'")
    return expanded


@router.post("/v2/players/grants/cart/fire")
async def v2_players_cart_fire(request: Request, body: CartFireRequest):
    """Fire a multi-grant cart at multiple recipients under one batch_id. Each
    (recipient, op) is one in-process _execute_one_grant call with its own
    idempotency_key and defer_if_online=True. Continue-on-error; aggregate."""
    user = require_admin(request)
    require_csrf(request, user)

    ip = request.client.host if request.client else "unknown"

    if body.confirm_token != "FIRE":
        raise HTTPException(400, "confirm_token must be the literal string 'FIRE'")
    if not body.recipients:
        raise HTTPException(400, "at least one recipient is required")
    if len(body.recipients) > MAX_RECIPIENTS_PER_BATCH:
        raise HTTPException(400, f"max {MAX_RECIPIENTS_PER_BATCH} recipients per batch")
    for r in body.recipients:
        if not isinstance(r, int) or r < 1:
            raise HTTPException(400, "recipient ids must be positive integers")
    if not body.lines:
        raise HTTPException(400, "cart is empty")
    if len(body.lines) > MAX_CART_LINES:
        raise HTTPException(400, f"max {MAX_CART_LINES} cart lines")

    presets = await _load_presets()
    expanded = _expand_cart_lines(body.lines, presets)
    if not expanded:
        raise HTTPException(400, "cart expanded to zero ops")

    total_fires = len(body.recipients) * len(expanded)
    if total_fires > MAX_TOTAL_FIRES:
        raise HTTPException(
            400,
            f"batch would fire {total_fires} grants (max {MAX_TOTAL_FIRES}); "
            "reduce recipients or cart lines",
        )

    batch_id = str(uuid.uuid4())
    ops_fired = 0
    ops_failed = 0
    errors: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []

    for aid in body.recipients:
        for grant_type, detail, preset_name in expanded:
            idem = str(uuid.uuid4())
            try:
                result = await _execute_one_grant(
                    user, ip, aid, grant_type, detail, idem,
                    mode="apply", defer_if_online=True,
                    batch_id=batch_id, preset_name=preset_name,
                    live_delivery=body.live_delivery,
                )
            except HTTPException as exc:
                ops_failed += 1
                errors.append({
                    "account_id": aid,
                    "grant_type": grant_type,
                    "error": exc.detail[:200] if isinstance(exc.detail, str) else str(exc.detail),
                })
                continue

            if not bool(result.get("success", False)):
                ops_failed += 1
                errors.append({
                    "account_id": aid,
                    "grant_type": grant_type,
                    "error": result.get("message") or "fire returned success=false",
                })
                continue

            ops_fired += 1
            grants.append({
                "account_id": aid,
                "grant_type": grant_type,
                "grant_id": result.get("grant_id"),
                "status": result.get("status"),
            })

    audit_log(
        user["id"], user["username"], "dune_cart_fire",
        f"{len(body.recipients)}r x {len(expanded)}ops", ip,
        details=json.dumps({
            "recipients": body.recipients,
            "line_count": len(body.lines),
            "ops_per_recipient": len(expanded),
            "batch_id": batch_id,
            "ops_fired": ops_fired,
            "ops_failed": ops_failed,
            "error_count": len(errors),
        }),
        success=(ops_failed == 0),
    )

    return {
        "success": ops_failed == 0,
        "batch_id": batch_id,
        "ops_fired": ops_fired,
        "ops_failed": ops_failed,
        "errors": errors,
        "grants": grants,
    }


# --- Specialization + Skill grant pickers (Phase 1 / Phase 2) ----------------
#
# These render rich configurator modals that the workbench loads via htmx when
# an operator clicks the "Specializations" or "Skills / Abilities" catalog tile
# (marked with opens="spec_picker"/"skill_picker" by grant_catalog.py). The
# modals push the SAME custom cart lines the rest of the cart already fires
# (keystone, spec_xp, spec_unlock_track, spec_unlock_all, grant_full_job_tree,
# grant_skill_block, reset_full_skill_area); nothing here fires a grant.

# Display order for the 5 specialization tracks (track_type values match the
# grant-catalog choice lists exactly: Combat/Crafting/Exploration/Gathering/Sabotage).
SPEC_TRACKS = ["Combat", "Crafting", "Exploration", "Gathering", "Sabotage"]

# 5 trainer schools. Internal job key (matches grant_full_job_tree / reset_full_skill_area
# choices) paired with the friendly display label.
SKILL_JOBS = [
    ("Trooper", "Trooper"),
    ("Mentat", "Mentat"),
    ("Planetologist", "Planetologist"),
    ("BeneGesserit", "Bene Gesserit"),
    ("Swordmaster", "Swordmaster"),
]
_JOB_LABELS = dict(SKILL_JOBS)

# Spec XP caps (mirror routers.dune_grant; "Max track" preset is the only
# validated XP target, OQ-3). spec_xp itself is online-safe (Funcom proc).
SPEC_XP_MAX = 44182
SPEC_LEVEL_MAX = 100

# OQ-8: friendly labels for the 30 real skill blocks. Block ids come ONLY from
# scripts/tags-data.json job_skill_blocks (QA correction E1): per job the 3
# Skills.Key.<Job>1/2/3 modules plus 3 FLAT, non-job-prefixed Skills.Key.Capstone*.
# Never invent Trooper4/5/6 or job-prefixed capstones; build_grant_skill_block
# hard-fails any name not in that file.
SKILL_BLOCK_LABELS = {
    # Trooper (branches Gunnery / Suspensor Training / Tactical Tech)
    "Skills.Key.Trooper1": "Trooper Module I",
    "Skills.Key.Trooper2": "Trooper Module II",
    "Skills.Key.Trooper3": "Trooper Module III",
    "Skills.Key.CapstoneWeaponry": "Weaponry (capstone)",
    "Skills.Key.CapstoneSuspensorTech": "Suspensor Tech (capstone)",
    "Skills.Key.CapstoneGadgets": "Gadgets (capstone)",
    # Mentat (Mental Calculus / Assassination / Tactician)
    "Skills.Key.Mentat1": "Mentat Module I",
    "Skills.Key.Mentat2": "Mentat Module II",
    "Skills.Key.Mentat3": "Mentat Module III",
    "Skills.Key.CapstoneMentalCalculus": "Mental Calculus (capstone)",
    "Skills.Key.CapstoneAssassination": "Assassination (capstone)",
    "Skills.Key.CapstoneTactician": "Tactician (capstone)",
    # Planetologist (Scientist / Explorer / Mechanic)
    "Skills.Key.Planetologist1": "Planetologist Module I",
    "Skills.Key.Planetologist2": "Planetologist Module II",
    "Skills.Key.Planetologist3": "Planetologist Module III",
    "Skills.Key.CapstoneScientist": "Scientist (capstone)",
    "Skills.Key.CapstoneExplorer": "Explorer (capstone)",
    "Skills.Key.CapstoneDriver": "Driver (capstone)",
    # Bene Gesserit (Weirding Way / The Voice / Body Control)
    "Skills.Key.BeneGesserit1": "Bene Gesserit Module I",
    "Skills.Key.BeneGesserit2": "Bene Gesserit Module II",
    "Skills.Key.BeneGesserit3": "Bene Gesserit Module III",
    "Skills.Key.CapstoneWeirdingWay": "Weirding Way (capstone)",
    "Skills.Key.CapstoneManipulation": "Manipulation (capstone)",
    "Skills.Key.CapstoneSelfControl": "Self Control (capstone)",
    # Swordmaster (The Blade / The Will / The Way)
    "Skills.Key.Swordmaster1": "Swordmaster Module I",
    "Skills.Key.Swordmaster2": "Swordmaster Module II",
    "Skills.Key.Swordmaster3": "Swordmaster Module III",
    "Skills.Key.CapstoneBlade": "Blade (capstone)",
    "Skills.Key.CapstoneResolve": "Resolve (capstone)",
    "Skills.Key.CapstoneAggression": "Aggression (capstone)",
}

# Keystone catalog sidecar (committed, all 205 ids / 5 tracks, with req_level +
# spice_cost + sp_bonus + friendly_name). Authoritative trait-grid source; no DB
# hit per modal open. Mirrors the path dune_grant.py uses for /grant/keystones.
_KEYSTONE_SIDECAR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "keystone-catalog.json",
)
_TAGS_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "tags-data.json",
)
_keystone_sidecar_cache: dict[str, Any] = {"data": None, "mtime": 0.0}
_tags_data_cache: dict[str, Any] = {"data": None, "mtime": 0.0}


def _load_keystone_sidecar() -> list[dict[str, Any]]:
    """Read the keystone catalog sidecar (mtime-cached). Returns [] on error so
    the picker degrades to an empty grid rather than 500ing the whole modal."""
    try:
        mtime = os.path.getmtime(_KEYSTONE_SIDECAR_PATH)
    except OSError:
        return []
    cached = _keystone_sidecar_cache["data"]
    if cached is not None and mtime == _keystone_sidecar_cache["mtime"]:
        return cached
    try:
        with open(_KEYSTONE_SIDECAR_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning("keystone sidecar unreadable: %s", _KEYSTONE_SIDECAR_PATH)
        return cached or []
    rows = data.get("keystones") if isinstance(data, dict) else None
    rows = rows or []
    _keystone_sidecar_cache["data"] = rows
    _keystone_sidecar_cache["mtime"] = mtime
    return rows


def _traits_for_track(track: str) -> list[dict[str, Any]]:
    """All keystone rows for one track, sorted by required level then node index."""
    rows = [r for r in _load_keystone_sidecar() if r.get("track") == track]
    rows.sort(key=lambda r: (r.get("req_level") or 0, r.get("node_index") or 0,
                             r.get("keystone_id") or 0))
    return rows


def _load_job_skill_blocks() -> dict[str, list[str]]:
    """Read job_skill_blocks from scripts/tags-data.json (mtime-cached). This is
    the ONLY source of real block ids (E1)."""
    try:
        mtime = os.path.getmtime(_TAGS_DATA_PATH)
    except OSError:
        return {}
    cached = _tags_data_cache["data"]
    if cached is not None and mtime == _tags_data_cache["mtime"]:
        return cached
    try:
        with open(_TAGS_DATA_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning("tags-data.json unreadable: %s", _TAGS_DATA_PATH)
        return cached or {}
    blocks = data.get("job_skill_blocks") if isinstance(data, dict) else None
    blocks = blocks or {}
    _tags_data_cache["data"] = blocks
    _tags_data_cache["mtime"] = mtime
    return blocks


def _blocks_for_job(job: str) -> list[dict[str, str]]:
    """Ordered display blocks for a job: the 3 job modules first, then the 3 flat
    capstones. Each: {block, label, is_capstone}."""
    raw = _load_job_skill_blocks().get(job) or []
    modules, capstones = [], []
    for b in raw:
        is_capstone = ".Capstone" in b
        item = {
            "block": b,
            "label": SKILL_BLOCK_LABELS.get(b, b),
            "is_capstone": is_capstone,
        }
        (capstones if is_capstone else modules).append(item)
    modules.sort(key=lambda x: x["block"])
    capstones.sort(key=lambda x: x["label"])
    return modules + capstones


async def _progression_state(aid: int | None) -> dict[str, Any] | None:
    """Per-player progression state (owned keystones + track levels + learned
    skill blocks) for owned/available/locked/learned rendering. Returns None when
    no single-player context or the relay read is unavailable, so the pickers
    degrade to '--' per the design. Never raises."""
    if not aid or aid < 1:
        return None
    data = await _safe_call(call_relay(f"/dune/player/{aid}/progression_state", timeout=15))
    if not isinstance(data, dict) or not data.get("available"):
        return None
    return data


def _focal_aid(aid: int | None) -> int | None:
    return aid if (aid and aid > 0) else None


@router.get("/v2/players/grants/_spec_picker")
async def v2_spec_picker_fragment(request: Request, aid: int | None = None):
    """Specializations grant picker modal body (centered, htmx-loaded)."""
    require_admin(request)
    focal = _focal_aid(aid)
    state = await _progression_state(focal)
    spec_tracks = (state or {}).get("spec_tracks") or {}

    tracks = []
    for t in SPEC_TRACKS:
        lvl = None
        info = spec_tracks.get(t)
        if isinstance(info, dict):
            lvl = info.get("level")
        tracks.append({"key": t, "label": t, "level": lvl})

    default_track = SPEC_TRACKS[0]
    return templates.TemplateResponse(
        request,
        "v2/_fragments/spec_picker.html",
        {
            "aid": focal,
            "tracks": tracks,
            "default_track": default_track,
            "state_available": state is not None,
            **_spec_traits_context(default_track, focal, state),
        },
    )


def _spec_traits_context(track: str, aid: int | None,
                         state: dict[str, Any] | None) -> dict[str, Any]:
    """Build the per-track trait grid context shared by the picker body and the
    _spec_traits swap. Computes owned/available/locked per row when state exists."""
    owned = set((state or {}).get("owned_keystone_ids") or [])
    spec_tracks = (state or {}).get("spec_tracks") or {}
    info = spec_tracks.get(track) if isinstance(spec_tracks, dict) else None
    track_level = info.get("level") if isinstance(info, dict) else None
    track_xp = info.get("xp") if isinstance(info, dict) else None
    state_available = state is not None

    rows = []
    for r in _traits_for_track(track):
        kid = r.get("keystone_id")
        req_level = r.get("req_level") or 0
        sp_bonus = r.get("sp_bonus") or 0
        if not state_available:
            row_state = "unknown"
        elif kid in owned:
            row_state = "owned"
        elif track_level is not None and req_level > track_level:
            row_state = "locked"
        else:
            row_state = "available"
        rows.append({
            "keystone_id": kid,
            "name": r.get("friendly_name") or r.get("keystone_name") or str(kid),
            "req_level": req_level,
            "spice_cost": r.get("spice_cost") or 0,
            "sp_bonus": sp_bonus,
            "type": "Skill Point" if sp_bonus > 0 else "Trait",
            "state": row_state,
        })
    return {
        "track": track,
        "track_label": track,
        "track_level": track_level,
        "track_xp": track_xp,
        "state_available": state_available,
        "traits": rows,
        "spec_xp_max": SPEC_XP_MAX,
        "spec_level_max": SPEC_LEVEL_MAX,
        "aid": aid,
    }


@router.get("/v2/players/grants/_spec_traits")
async def v2_spec_traits_fragment(request: Request, track: str, aid: int | None = None):
    """Per-track trait grid + track XP controls (htmx tab swap)."""
    require_admin(request)
    if track not in SPEC_TRACKS:
        raise HTTPException(400, f"unknown track: {track}")
    focal = _focal_aid(aid)
    state = await _progression_state(focal)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/_spec_traits.html",
        _spec_traits_context(track, focal, state),
    )


@router.get("/v2/players/grants/_skill_picker")
async def v2_skill_picker_fragment(request: Request, aid: int | None = None):
    """Skills / Abilities grant picker modal body (centered, htmx-loaded)."""
    require_admin(request)
    focal = _focal_aid(aid)
    state = await _progression_state(focal)
    default_job = SKILL_JOBS[0][0]
    return templates.TemplateResponse(
        request,
        "v2/_fragments/skill_picker.html",
        {
            "aid": focal,
            "jobs": [{"key": k, "label": v} for k, v in SKILL_JOBS],
            "default_job": default_job,
            "state_available": state is not None,
            **_skill_blocks_context(default_job, focal, state),
        },
    )


def _skill_blocks_context(job: str, aid: int | None,
                          state: dict[str, Any] | None) -> dict[str, Any]:
    learned = set((state or {}).get("learned_blocks") or [])
    state_available = state is not None
    blocks = []
    for b in _blocks_for_job(job):
        blocks.append({**b, "learned": (b["block"] in learned) if state_available else None})
    learned_count = sum(1 for b in blocks if b["learned"]) if state_available else None
    return {
        "job": job,
        "job_label": _JOB_LABELS.get(job, job),
        "blocks": blocks,
        "state_available": state_available,
        "learned_count": learned_count,
        "block_total": len(blocks),
        "aid": aid,
    }


@router.get("/v2/players/grants/_skill_blocks")
async def v2_skill_blocks_fragment(request: Request, job: str, aid: int | None = None):
    """Per-job skill block list + grant/reset actions (htmx tab swap)."""
    require_admin(request)
    if job not in _JOB_LABELS:
        raise HTTPException(400, f"unknown job: {job}")
    focal = _focal_aid(aid)
    state = await _progression_state(focal)
    return templates.TemplateResponse(
        request,
        "v2/_fragments/_skill_blocks.html",
        _skill_blocks_context(job, focal, state),
    )
