"""Land-claim ownership lookup for admins: who owns this base.

Ownership is not stored on the actor. A totem carries no owner_account_id and
the buildings around it are owned by the totem's OWN entity, so the only route
to a person is dune.permission_actor_rank (rank 1 = owner). The game host does
that resolution; this router is a cached, admin-gated read of it.

Read-only end to end. Nothing here writes to the game DB.

PII: this says who owns what, server-wide. Every route is require_admin, and
the payload must never be folded into /portal/maps/{key}/data — that one is
designed for the public site to consume.
"""
import json
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from auth import audit_log, require_admin, require_csrf
import base_vault
from cache import ttl_cache
from relay import call_relay

router = APIRouter(prefix="/api/dune/v2")

# The full directory is the heaviest read in the dune-* family: it aggregates
# across every row of building_instances to derive per-piece condition. Claims
# change on the order of hours, and the owner_activity bands are 14/30/60 DAYS,
# so a 5-minute TTL costs the game host at most one query per 5 minutes no
# matter how many admins have the page open. max_stale keeps the page serving
# (flagged stale) for another 30 minutes if the relay is briefly unreachable,
# which is the right failure mode for a lookup nobody is paged by.
_BASES_TTL = 300
_BASES_MAX_STALE = 1800

# One totem per claim, ~166 live. Bounded by the dispatcher at 25.
_NEAR_MAX = 25

_MAP_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


@ttl_cache("dune.bases", ttl=_BASES_TTL, max_stale=_BASES_MAX_STALE)
async def _cached_bases() -> dict:
    # 70s: the relay itself allows the game-host command 60s.
    return await call_relay("/dune/bases", timeout=70)


@router.get("/bases")
async def v2_bases(request: Request):
    """Full claim directory: owner, co-holders, position, size, condition.

    Each row carries `ownership` (owned / orphaned / stored_backup) and, when
    there is an owner, `owner_activity` (active / quiet / dormant / abandoned).
    Those two are deliberately separate: an orphan is a base whose owner record
    is gone for good, an abandoned base still has a real person to contact.

    A `legend` block travels with the payload — render the help tooltip from it
    rather than restating the definitions in a template, so the wording cannot
    drift away from the logic that produced the labels.

    Admin-only, read-only, no CSRF (nothing mutates). 166 rows: search and sort
    client-side rather than adding query params here.
    """
    require_admin(request)
    try:
        return await _cached_bases()
    except Exception as exc:
        # Never 500 a panel over a lookup. The map still works without it.
        return {"available": False, "count": 0, "bases": [],
                "error": str(exc)[:300]}


@router.get("/bases/near")
async def v2_bases_near(request: Request, map_name: str, dim: int, x: float,
                        y: float, limit: int = 5):
    """Nearest claims to a world point — the map click/tooltip lookup.

    Uncached: it is a cheap bounded lookup and the whole point is answering for
    an arbitrary point the admin just clicked. Stored backups are excluded on
    the game host; they keep a world transform but nothing stands there.

    `dim` is required. A Hagga map view is always showing ONE instance, so the
    caller already knows which (0 = Habbanya PvE, 1 = Kulon PvP) and must say
    so: the two share a coordinate space, and a lookup without the dimension
    will happily return a PvP base as the nearest thing to a PvE click. The
    response echoes `dimension_index` back for the same reason.
    """
    require_admin(request)
    if not _MAP_RE.match(map_name or ""):
        raise HTTPException(400, "map_name must be alphanumeric")
    if not 0 <= int(dim) <= 99:
        raise HTTPException(400, "dim out of range")
    limit = max(1, min(int(limit), _NEAR_MAX))
    try:
        return await call_relay(
            f"/dune/bases/near?map_name={map_name}&dim={int(dim)}"
            f"&x={x}&y={y}&limit={limit}",
            timeout=35)
    except Exception as exc:
        return {"available": False, "count": 0, "bases": [],
                "error": str(exc)[:300]}


def _ip(request: Request) -> str:
    return (request.client.host if request.client else "?")


@router.get("/claims/{totem_id}/vault")
async def v2_claim_vault(request: Request, totem_id: int):
    user = require_admin(request)
    base_vault.validate_totem(totem_id)
    result = await base_vault.call({'action': 'history', 'totem_id': totem_id})
    audit_log(user['id'], user['username'], 'dune_vault_history', str(totem_id),
              _ip(request), details=json.dumps({'snapshots': result.get('total_snapshots', 0)}))
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post("/claims/{totem_id}/vault/snapshots")
async def v2_claim_vault_capture(request: Request, totem_id: int):
    user = require_admin(request)
    require_csrf(request, user)
    base_vault.validate_totem(totem_id)
    raw = b''
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > 1024:
            raise HTTPException(400, 'Capture request is too large')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid capture request')
    identity = base_vault.validate_capture(body)
    audit_log(user['id'], user['username'], 'dune_vault_capture_requested', str(totem_id),
              _ip(request), details=json.dumps({'request_id': identity}))
    try:
        result = await base_vault.call({'action': 'capture', 'totem_id': totem_id,
                                        'request_id': identity, 'admin_id': user['id']})
    except HTTPException as error:
        audit_log(user['id'], user['username'], 'dune_vault_capture', str(totem_id),
                  _ip(request), details=json.dumps({'request_id': identity, 'status': error.status_code}), success=False)
        raise
    audit_log(user['id'], user['username'], 'dune_vault_capture', str(totem_id), _ip(request),
              details=json.dumps({'request_id': identity, 'status': result.get('operation', {}).get('status')}),
              success=result.get('operation', {}).get('status') in ('pending', 'running', 'completed'))
    return JSONResponse(result, status_code=202, headers={'Cache-Control': 'no-store'})


@router.post("/claims/{totem_id}/vault/restore-plans")
async def v2_claim_vault_restore_plan(request: Request, totem_id: int):
    user = require_admin(request)
    require_csrf(request, user)
    base_vault.validate_totem(totem_id)
    raw = b''
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > 1024:
            raise HTTPException(400, 'Restore plan request is too large')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {'request_id', 'snapshot_id'}:
            raise ValueError()
        identity = base_vault.validate_capture({'request_id': body['request_id']})
        snapshot = base_vault.validate_capture({'request_id': body['snapshot_id']})
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid restore plan request')
    audit_log(user['id'], user['username'], 'dune_vault_restore_plan_requested', str(totem_id), _ip(request),
              details=json.dumps({'request_id': identity, 'snapshot_id': snapshot}))
    try:
        result = await base_vault.call({'action': 'restore_plan', 'totem_id': totem_id,
            'request_id': identity, 'snapshot_id': snapshot, 'admin_id': user['id']})
    except HTTPException as error:
        audit_log(user['id'], user['username'], 'dune_vault_restore_plan', str(totem_id), _ip(request),
                  details=json.dumps({'request_id': identity, 'status': error.status_code}), success=False)
        raise
    audit_log(user['id'], user['username'], 'dune_vault_restore_plan', str(totem_id), _ip(request),
              details=json.dumps({'plan_id': result['plan_id'], 'apply_allowed': False}))
    return JSONResponse(result, status_code=201, headers={'Cache-Control': 'no-store'})


@router.get("/claims/{totem_id}/vault/restore-plans/{plan_id}")
async def v2_claim_vault_restore_read(request: Request, totem_id: int, plan_id: str):
    require_admin(request)
    base_vault.validate_totem(totem_id)
    base_vault.validate_capture({'request_id': plan_id})
    result = await base_vault.call({'action': 'restore_read', 'totem_id': totem_id, 'plan_id': plan_id})
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post("/claims/{totem_id}/vault/restore-plans/{plan_id}/apply")
async def v2_claim_vault_restore_apply(request: Request, totem_id: int, plan_id: str):
    user = require_admin(request)
    require_csrf(request, user)
    base_vault.validate_totem(totem_id)
    base_vault.validate_capture({'request_id': plan_id})
    audit_log(user['id'], user['username'], 'dune_vault_restore_apply_refused', str(totem_id), _ip(request),
              details=json.dumps({'plan_id': plan_id, 'reason': 'restore_apply_disabled'}), success=False)
    raise HTTPException(409, {'code': 'restore_apply_disabled', 'reasons': [
        'Production restore requires a separately approved target and maintenance window.',
        'Shutdown-save completion, an unloaded map and a startup fence are not yet proven.',
        'Game-build, terrain, overlap and in-game compatibility remain unverified.',
    ]})


@router.post("/claims/{totem_id}/backup")
async def v2_claim_backup(request: Request, totem_id: int):
    """Back a base into its OWNER's own reconstruction tool.

    The plot frees, the owner keeps every container and item, and no admin claim
    slot is spent. Every eligibility rule lives on the game host and still
    applies; this route cannot override any of them.

    ⚠️ IRREVERSIBLE, and it does NOT despawn the structures. Nothing in the
    database can: the instant removal you see in game is the engine acting when
    an OWNER uses their tool. The buildings stand until the map reloads, and
    persistent Hagga only reloads on a restart. If the plot has to be visibly
    clear, adopt instead and clear it with your own tool in game.
    """
    user = require_admin(request)
    require_csrf(request, user)
    if not 0 < totem_id < 10 ** 19:
        raise HTTPException(400, "totem_id out of range")
    try:
        res = await call_relay(f"/dune/claims/{totem_id}/backup",
                               method="POST", timeout=190)
    except Exception as exc:
        audit_log(user["id"], user["username"], "dune_claim_backup", str(totem_id),
                  _ip(request), details=str(exc)[:400], success=False)
        return {"ok": False, "error": str(exc)[:300]}
    audit_log(user["id"], user["username"], "dune_claim_backup", str(totem_id),
              _ip(request), details=json.dumps(res)[:1500],
              success=bool(res.get("ok")))
    return res


@router.post("/claims/{totem_id}/adopt")
async def v2_claim_adopt(request: Request, totem_id: int, account_id: int):
    """Adopt a claim, keeping the previous owner as a rank-2 co-holder.

    Gentler than a plain takeover: the player keeps build and access rights on
    their own base rather than being locked out. Reversible — the audit row
    records the prior owner and --revert restores them.

    Costs the admin a claim slot; the engine caps a holder at 3 and the game host
    refuses past it.
    """
    user = require_admin(request)
    require_csrf(request, user)
    if not 0 < totem_id < 10 ** 19:
        raise HTTPException(400, "totem_id out of range")
    if not 0 < account_id < 10 ** 19:
        raise HTTPException(400, "account_id out of range")
    try:
        res = await call_relay(
            f"/dune/claims/{totem_id}/adopt?account_id={account_id}",
            method="POST", timeout=130)
    except Exception as exc:
        audit_log(user["id"], user["username"], "dune_claim_adopt", str(totem_id),
                  _ip(request), details=str(exc)[:400], success=False)
        return {"ok": False, "error": str(exc)[:300]}
    audit_log(user["id"], user["username"], "dune_claim_adopt", str(totem_id),
              _ip(request), details=json.dumps(res)[:1500],
              success=bool(res.get("ok")))
    return res


@router.get("/claims/{totem_id}/options")
async def v2_claim_options(request: Request, totem_id: int):
    """What an admin can do about one base, and what stands in the way.

    Two courses of action, deliberately surfaced together so the gentler one is
    seen first:

      backup  put the base into the OWNER's own reconstruction tool. The plot
              frees, they keep everything, no admin claim slot is consumed. This
              is the humane option and the one to reach for.
      adopt   take rank 1 on the totem. Costs the admin a claim slot, and the
              engine caps a holder at 3.

    Uncached and read-only: an admin clicking a base wants the state now, and
    both underlying checkers are cheap. Nothing here writes; the write paths live
    behind their own environment flags on the game host and are not reachable
    from this endpoint.
    """
    require_admin(request)
    if not 0 < totem_id < 10 ** 19:
        raise HTTPException(400, "totem_id out of range")
    try:
        raw = await call_relay(f"/dune/claims/{totem_id}/options", timeout=65)
    except Exception as exc:
        return {"available": False, "error": str(exc)[:300]}

    adopt = (raw or {}).get("adopt") or {}
    backup = (raw or {}).get("backup") or {}
    bclaim = backup.get("claim") or {}

    # The claim cap is the engine's, not ours: 3, verified across every holder on
    # the server. Surface the admin's own headroom next to the adopt option so
    # the warning arrives before the click, not after.
    return {
        "available": True,
        "totem_id": totem_id,
        "owner": bclaim.get("owner_name"),
        "owner_account_id": bclaim.get("owner_account_id"),
        "owner_days_away": bclaim.get("owner_days_away"),
        "owner_online": bclaim.get("owner_online"),
        "size": {"pieces": bclaim.get("pieces"),
                 "placeables": bclaim.get("placeables"),
                 "containers": bclaim.get("containers"),
                 "stored_items": bclaim.get("stored_items")},
        "backup": {
            "eligible": backup.get("eligible"),
            "blockers": backup.get("blockers") or [],
            "warnings": backup.get("warnings") or [],
            "owner_has_tool": bool(bclaim.get("owner_backup_tools")),
            "owner_existing_backups": bclaim.get("owner_existing_backups"),
        },
        "adopt": {
            "eligible": adopt.get("eligible"),
            "refusal": adopt.get("refusal"),
        },
        # Who a claim may be adopted onto, with live headroom. Shipped with the
        # options rather than as its own call because the panel needs it at
        # exactly the moment it draws the adopt control, and a picker that cannot
        # show who has a free slot is just a list of names.
        "operators": ((raw or {}).get("operators") or {}).get("operators") or [],
    }
