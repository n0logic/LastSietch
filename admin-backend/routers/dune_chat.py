import json
import re
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, require_admin, require_csrf
from relay import call_relay

router = APIRouter(prefix="/api/dune")

MAX_MESSAGE = 500
# 30s cache on the online-player picker source (presence changes fast).
PLAYERS_CACHE_TTL = 30
_players_cache: dict = {"data": None, "fetched_at": 0.0}
# FuncomId is Display#tag (display before '#', numeric tag after).
FUNCOM_ID_RE = re.compile(r"^[^\s#]{1,32}#[0-9]{1,10}$")

# Instance -> chat.map routing key (<map>.<dim>). The dim values are UNVERIFIED:
# only HaggaBasin.0 has rendered in a live test, and even its partition scoping
# is unconfirmed (chat dimension != m_PartitionId). Until a per-partition capture
# confirms each routing key (after the 2026-05-29 Funcom window), every map and
# server send is forced to dry-run so a wrong guess can never publish to players.
# Flip "verified" to True per row once the real <map>.<dim> is captured.
CHAT_INSTANCES = {
    "habbanya": {"label": "Habbanya (Hagga Basin, partition 1)", "map": "HaggaBasin", "dim": 0, "verified": False},
    "kulon": {"label": "Kulon (Hagga Basin, partition 32)", "map": "HaggaBasin", "dim": 0, "verified": False},
    # dim from the DB dimension_index pairing verified 2026-08-27 (1->0, 32->1, 33->2);
    # whether chat routing keys follow dimension_index is still uncaptured, so this row
    # stays verified: False (dry-run-forced) like its siblings.
    "amtal": {"label": "Amtal (Hagga Basin, partition 33, full PvP)", "map": "HaggaBasin", "dim": 2, "verified": False},
    "dd_pvp": {"label": "Deep Desert PvP (partition 8)", "map": "DeepDesert", "dim": 0, "verified": False},
    "dd_pve": {"label": "Deep Desert PvE (partition 31)", "map": "DeepDesert", "dim": 0, "verified": False},
}


class ChatSendRequest(BaseModel):
    scope: str  # whisper | map | server
    message: str
    mode: str = "dry-run"  # apply | dry-run
    recipient: str | None = None  # whisper: FuncomId (Display#tag)
    instance: str | None = None  # map: a CHAT_INSTANCES key


@router.get("/chat/players")
async def chat_players(request: Request):
    """Online players (name + FuncomId) for the whisper picker. Admin-only;
    character names + FuncomIds are PII. 30s cache, stale-on-error fallback.
    Only online players are returned since whisper delivers to online recipients."""
    require_admin(request)
    now = time.monotonic()
    cached = _players_cache["data"]
    if cached is not None and now - _players_cache["fetched_at"] < PLAYERS_CACHE_TTL:
        return cached
    try:
        raw = await call_relay("/dune/chat/players", timeout=45)
    except Exception:
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "players": []}
    players = [
        {"name": p.get("name"), "funcom_id": p.get("funcom_id"), "faction": p.get("faction")}
        for p in (raw.get("players") or [])
        if p.get("funcom_id")
    ]
    data = {"available": bool(raw.get("available", True)), "players": players}
    _players_cache["data"] = data
    _players_cache["fetched_at"] = now
    return data


@router.get("/chat/instances")
async def chat_instances(request: Request):
    """Map-target picker source for the Chat panel. Admin-only. The verified flag
    tells the UI which instances can send live vs preview-only."""
    require_admin(request)
    return {
        "instances": [
            {"id": k, "label": v["label"], "verified": v["verified"]}
            for k, v in CHAT_INSTANCES.items()
        ],
        "max_message": MAX_MESSAGE,
    }


@router.post("/chat/send")
async def chat_send(request: Request, body: ChatSendRequest):
    """Send one in-game chat message as the Cielago herald. Admin + CSRF gated,
    audited on every path. Whisper honors the requested mode; map/server sends to
    unverified instances are forced to dry-run (preview-only) so a wrong routing
    key can never publish live."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_chat_send", body.scope or "?", ip,
            details=json.dumps({
                "scope": body.scope,
                "mode": body.mode,
                "instance": body.instance,
                "recipient": body.recipient,
                "message_len": len(body.message or ""),
                **extra,
            }),
            success=success,
        )

    try:
        if body.scope not in ("whisper", "map", "server"):
            raise HTTPException(400, "scope must be whisper, map, or server")
        if not isinstance(body.message, str) or not body.message.strip():
            raise HTTPException(400, "message is required")
        if len(body.message) > MAX_MESSAGE:
            raise HTTPException(400, f"message exceeds {MAX_MESSAGE} chars")
        if body.mode not in ("apply", "dry-run"):
            raise HTTPException(400, "mode must be apply or dry-run")
        if body.scope == "whisper":
            if not body.recipient or not FUNCOM_ID_RE.match(body.recipient):
                raise HTTPException(400, "recipient must be a FuncomId (Display#tag)")
        elif body.scope == "map":
            if body.instance not in CHAT_INSTANCES:
                raise HTTPException(400, "instance must be one of: " + ", ".join(CHAT_INSTANCES))
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    # Build one relay job per target. The relay/herald speak scope whisper|map
    # (a single routing key per call); server fans out to every instance here.
    targets: list[tuple[str, dict, bool]] = []  # (label, job, forced_preview)
    if body.scope == "whisper":
        targets.append((
            body.recipient,
            {"scope": "whisper", "recipient": body.recipient, "message": body.message,
             "mode": body.mode, "operator": user["username"]},
            False,
        ))
    else:
        inst_ids = [body.instance] if body.scope == "map" else list(CHAT_INSTANCES)
        for iid in inst_ids:
            inst = CHAT_INSTANCES[iid]
            forced = body.mode == "apply" and not inst["verified"]
            eff_mode = "dry-run" if forced else body.mode
            targets.append((
                inst["label"],
                {"scope": "map", "map": inst["map"], "dim": inst["dim"],
                 "message": body.message, "mode": eff_mode, "operator": user["username"]},
                forced,
            ))

    results = []
    any_forced = False
    for label, job, forced in targets:
        any_forced = any_forced or forced
        try:
            res = await call_relay("/dune/chat/send", "POST", job, timeout=60)
            results.append({
                "target": label, "mode": job["mode"],
                "success": bool(res.get("success")), "detail": (res.get("detail") or "")[:500],
            })
        except HTTPException as exc:
            results.append({"target": label, "mode": job["mode"], "success": False,
                            "detail": str(exc.detail)[:500]})
        except Exception as exc:
            results.append({"target": label, "mode": job["mode"], "success": False,
                            "detail": str(exc)[:500]})

    overall = bool(results) and all(r["success"] for r in results)
    _audit(overall, {"results": results, "forced_preview": any_forced})

    note = None
    if any_forced:
        note = ("One or more map targets are unverified, so they were sent as a PREVIEW only "
                "(nothing was published). Verify the routing keys to enable live map sends.")
    return {"success": overall, "scope": body.scope, "results": results, "note": note}
