import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import metrics as metrics_mod
from auth import audit_log, get_current_user, require_admin, require_csrf
from database import get_db
from relay import call_relay

router = APIRouter()

# In-memory pending scheduled operations: {game_id: {action, delay, started_at, started_by, task}}
pending_ops = {}

# Tracks config changes pending restart: {game_id: {changed_by, changed_at, keys: [...]}}
pending_config = {}


class MessageRequest(BaseModel):
    message: str


class ConfigUpdateRequest(BaseModel):
    changes: dict


class RestoreRequest(BaseModel):
    snapshot: str


class DeleteSnapshotRequest(BaseModel):
    snapshot: str


class SnapshotScheduleRequest(BaseModel):
    schedule: str
    retention: int = 10


class ScheduledRequest(BaseModel):
    delay_minutes: int  # 1 or 5


class UserGroupsRequest(BaseModel):
    userGroups: list


async def _game_supports_rcon(game_id: str) -> bool:
    """Query relay metadata and return whether the game supports in-game RCON."""
    try:
        info = await call_relay(f"/games/{game_id}")
        return bool(info.get("supports_rcon", True))
    except Exception:
        return True  # fail-open for legacy Conan path


# --- Infra Status ---

@router.get("/api/infra/status")
async def infra_status(request: Request):
    get_current_user(request)
    return await call_relay("/infra/status")


@router.get("/api/games/status/batch")
async def games_status_batch(request: Request):
    get_current_user(request)
    return await call_relay("/games/status/batch")


# --- Game Registry ---

@router.get("/api/games")
async def list_games(request: Request):
    get_current_user(request)
    return await call_relay("/games")


@router.get("/api/games/{game_id}")
async def game_info(request: Request, game_id: str):
    get_current_user(request)
    return await call_relay(f"/games/{game_id}")


# --- VM Control ---

@router.get("/api/games/{game_id}/vm/status")
async def vm_status(request: Request, game_id: str):
    get_current_user(request)
    return await call_relay(f"/games/{game_id}/vm/status")


@router.post("/api/games/{game_id}/vm/start")
async def vm_start(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/vm/start", "POST")
    audit_log(user["id"], user["username"], "vm_start", game_id, request.client.host)
    return result


@router.post("/api/games/{game_id}/vm/stop")
async def vm_stop(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/vm/stop", "POST")
    audit_log(user["id"], user["username"], "vm_stop", game_id, request.client.host)
    return result


@router.post("/api/games/{game_id}/vm/reset")
async def vm_reset(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/vm/reset", "POST")
    audit_log(user["id"], user["username"], "vm_reset", game_id, request.client.host)
    return result


# --- Server Control ---

@router.get("/api/games/{game_id}/server/status")
async def server_status(request: Request, game_id: str):
    get_current_user(request)
    return await call_relay(f"/games/{game_id}/server/status")


@router.get("/api/games/{game_id}/server/metrics")
async def server_metrics(request: Request, game_id: str, window: str = "1h"):
    get_current_user(request)
    if window not in ("1h", "7d"):
        raise HTTPException(400, "window must be '1h' or '7d'")
    # No relay gate: keep history readable when relay is down. Unknown game_id → empty points.
    conn = get_db()
    try:
        return metrics_mod.query_window(conn, game_id, window)
    finally:
        conn.close()


@router.post("/api/games/{game_id}/server/start")
async def server_start(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/server/start", "POST")
    audit_log(user["id"], user["username"], "server_start", game_id, request.client.host)
    return result


@router.post("/api/games/{game_id}/server/stop")
async def server_stop(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/server/stop", "POST")
    audit_log(user["id"], user["username"], "server_stop", game_id, request.client.host)
    pending_config.pop(game_id, None)
    return result


@router.post("/api/games/{game_id}/server/restart")
async def server_restart(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/server/restart", "POST")
    audit_log(user["id"], user["username"], "server_restart", game_id, request.client.host)
    pending_config.pop(game_id, None)
    return result


# --- Broadcast ---

@router.post("/api/games/{game_id}/server/broadcast")
async def server_broadcast(request: Request, game_id: str, body: MessageRequest):
    user = get_current_user(request)
    require_csrf(request, user)
    if not await _game_supports_rcon(game_id):
        raise HTTPException(501, "In-game broadcast is not supported for this server")
    result = await call_relay(f"/games/{game_id}/server/broadcast", "POST", {"message": body.message})
    audit_log(user["id"], user["username"], "broadcast", body.message, request.client.host)
    return result


# --- User Groups (admin-only) ---

@router.get("/api/games/{game_id}/usergroups")
async def usergroups_read(request: Request, game_id: str):
    require_admin(request)
    return await call_relay(f"/games/{game_id}/usergroups")


@router.post("/api/games/{game_id}/usergroups")
async def usergroups_write(request: Request, game_id: str, body: UserGroupsRequest):
    user = require_admin(request)
    require_csrf(request, user)
    result = await call_relay(
        f"/games/{game_id}/usergroups", "POST", {"userGroups": body.userGroups}, timeout=60
    )
    audit_log(
        user["id"], user["username"], "usergroups_change", game_id, request.client.host,
        details=f"{len(body.userGroups)} group(s)",
    )
    pending_config.pop(game_id, None)
    return result


# --- Save Data ---

@router.get("/api/games/{game_id}/savedata")
async def game_savedata(request: Request, game_id: str):
    get_current_user(request)
    return await call_relay(f"/games/{game_id}/savedata")


# --- ZFS ---

@router.get("/api/games/{game_id}/zfs/snapshots")
async def zfs_snapshots(request: Request, game_id: str):
    get_current_user(request)
    return await call_relay(f"/games/{game_id}/zfs/snapshots")


@router.post("/api/games/{game_id}/zfs/snapshot")
async def zfs_snapshot(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/zfs/snapshot", "POST")
    audit_log(user["id"], user["username"], "zfs_snapshot", game_id, request.client.host)
    return result


@router.post("/api/games/{game_id}/zfs/restore")
async def zfs_restore(request: Request, game_id: str, body: RestoreRequest):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/zfs/restore", "POST", {"snapshot": body.snapshot}, timeout=180)
    snap_label = body.snapshot.split("@")[1] if "@" in body.snapshot else body.snapshot
    destroyed = result.get("newer_destroyed", [])
    details = f"restored to {snap_label}"
    if destroyed:
        details += f", destroyed {len(destroyed)} newer snapshot(s)"
    audit_log(user["id"], user["username"], "zfs_restore", game_id, request.client.host, details=details)
    pending_config.pop(game_id, None)
    return result


@router.post("/api/games/{game_id}/zfs/delete")
async def zfs_delete(request: Request, game_id: str, body: DeleteSnapshotRequest):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/zfs/delete", "POST", {"snapshot": body.snapshot})
    snap_label = body.snapshot.split("@")[1] if "@" in body.snapshot else body.snapshot
    audit_log(user["id"], user["username"], "zfs_delete", game_id, request.client.host, details=f"deleted {snap_label}")
    return result


@router.get("/api/games/{game_id}/zfs/schedule")
async def zfs_get_schedule(request: Request, game_id: str):
    get_current_user(request)
    return await call_relay(f"/games/{game_id}/zfs/schedule")


@router.post("/api/games/{game_id}/zfs/schedule")
async def zfs_set_schedule(request: Request, game_id: str, body: SnapshotScheduleRequest):
    user = get_current_user(request)
    require_csrf(request, user)
    result = await call_relay(f"/games/{game_id}/zfs/schedule", "POST", {"schedule": body.schedule, "retention": body.retention})
    audit_log(user["id"], user["username"], "zfs_schedule", game_id, request.client.host, details=f"set to {body.schedule}, keep {body.retention}")
    return result


# --- Config ---

@router.get("/api/games/{game_id}/config")
async def config_read(request: Request, game_id: str):
    get_current_user(request)
    return await call_relay(f"/games/{game_id}/config")


@router.post("/api/games/{game_id}/config")
async def config_write(request: Request, game_id: str, body: ConfigUpdateRequest):
    user = get_current_user(request)
    require_csrf(request, user)

    # Read current values before writing so we can track old → new
    current_config = await call_relay(f"/games/{game_id}/config")
    current_values = {s["key"]: s["current"] for s in current_config.get("settings", [])}

    result = await call_relay(f"/games/{game_id}/config", "POST", {"changes": body.changes})
    details = ", ".join(f"{k}={v}" for k, v in body.changes.items())
    audit_log(user["id"], user["username"], "config_change", game_id, request.client.host, details=details)

    # Track pending restart changes with old/new values
    if result.get("needs_restart"):
        existing_changes = pending_config.get(game_id, {}).get("changes", {})
        for change in result.get("changes", []):
            if change.get("restart_required"):
                key = change["key"]
                if key in existing_changes:
                    old_val = existing_changes[key]["old"]
                else:
                    old_val = current_values.get(key)
                existing_changes[key] = {
                    "old": old_val,
                    "new": change["value"],
                }
        pending_config[game_id] = {
            "changed_by": user["username"],
            "changed_at": datetime.now(timezone.utc).isoformat(),
            "changes": existing_changes,
        }
    return result


@router.get("/api/games/{game_id}/config/pending")
async def config_pending(request: Request, game_id: str):
    get_current_user(request)
    info = pending_config.get(game_id)
    if not info:
        return {"pending": False}
    return {"pending": True, **info}


# --- Scheduled Operations ---

@router.get("/api/games/{game_id}/server/pending")
async def get_pending(request: Request, game_id: str):
    get_current_user(request)
    op = pending_ops.get(game_id)
    if not op:
        return {"pending": False}
    elapsed = (datetime.now(timezone.utc) - op["started_at"]).total_seconds()
    remaining = max(0, op["delay"] - elapsed)
    return {
        "pending": True,
        "action": op["action"],
        "delay": op["delay"],
        "remaining": round(remaining),
        "started_by": op["started_by"],
    }


async def _countdown_task(game_id: str, action: str, delay: int, username: str):
    """Background task that sends countdown broadcasts then executes the action."""
    try:
        can_broadcast = await _game_supports_rcon(game_id)

        broadcasts = []
        if delay >= 300:
            broadcasts = [
                (0, f"Server {action} in 5 minutes. Find a safe spot!"),
                (240, f"Server {action} in 1 minute! Log out now!"),
                (270, f"Server {action} in 30 seconds!"),
                (300, None),  # execute
            ]
        elif delay >= 60:
            broadcasts = [
                (0, f"Server {action} in 1 minute! Log out now!"),
                (30, f"Server {action} in 30 seconds!"),
                (60, None),  # execute
            ]
        else:
            broadcasts = [(0, None)]  # immediate

        last_time = 0
        for wait_until, message in broadcasts:
            sleep_for = wait_until - last_time
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            last_time = wait_until

            # Check if cancelled
            if game_id not in pending_ops:
                return

            if message:
                if not can_broadcast:
                    continue
                try:
                    await call_relay(
                        f"/games/{game_id}/server/broadcast",
                        "POST",
                        {"message": message},
                    )
                except Exception:
                    pass  # broadcast failure shouldn't abort the operation
            else:
                # Execute the action
                endpoint = "stop" if action == "shutdown" else "restart"
                await call_relay(f"/games/{game_id}/server/{endpoint}", "POST")
    finally:
        pending_ops.pop(game_id, None)


@router.post("/api/games/{game_id}/server/scheduled-stop")
async def scheduled_stop(request: Request, game_id: str, body: ScheduledRequest):
    user = get_current_user(request)
    require_csrf(request, user)
    if game_id in pending_ops:
        raise HTTPException(409, "An operation is already pending for this game")
    if body.delay_minutes not in (1, 5):
        raise HTTPException(400, "delay_minutes must be 1 or 5")

    delay_sec = body.delay_minutes * 60
    task = asyncio.create_task(_countdown_task(game_id, "shutdown", delay_sec, user["username"]))
    pending_ops[game_id] = {
        "action": "shutdown",
        "delay": delay_sec,
        "started_at": datetime.now(timezone.utc),
        "started_by": user["username"],
        "task": task,
    }
    audit_log(user["id"], user["username"], "scheduled_stop", game_id, request.client.host,
              details=f"{body.delay_minutes}min countdown")
    return {"result": "scheduled", "action": "shutdown", "delay": delay_sec}


@router.post("/api/games/{game_id}/server/scheduled-restart")
async def scheduled_restart(request: Request, game_id: str, body: ScheduledRequest):
    user = get_current_user(request)
    require_csrf(request, user)
    if game_id in pending_ops:
        raise HTTPException(409, "An operation is already pending for this game")
    if body.delay_minutes not in (1, 5):
        raise HTTPException(400, "delay_minutes must be 1 or 5")

    delay_sec = body.delay_minutes * 60
    task = asyncio.create_task(_countdown_task(game_id, "restart", delay_sec, user["username"]))
    pending_ops[game_id] = {
        "action": "restart",
        "delay": delay_sec,
        "started_at": datetime.now(timezone.utc),
        "started_by": user["username"],
        "task": task,
    }
    audit_log(user["id"], user["username"], "scheduled_restart", game_id, request.client.host,
              details=f"{body.delay_minutes}min countdown")
    return {"result": "scheduled", "action": "restart", "delay": delay_sec}


@router.post("/api/games/{game_id}/server/cancel-scheduled")
async def cancel_scheduled(request: Request, game_id: str):
    user = get_current_user(request)
    require_csrf(request, user)
    op = pending_ops.pop(game_id, None)
    if not op:
        raise HTTPException(404, "No pending operation to cancel")
    op["task"].cancel()
    # Notify players if RCON supported
    if await _game_supports_rcon(game_id):
        try:
            await call_relay(
                f"/games/{game_id}/server/broadcast",
                "POST",
                {"message": f"Scheduled {op['action']} has been cancelled."},
            )
        except Exception:
            pass
    audit_log(user["id"], user["username"], "cancel_scheduled", game_id, request.client.host,
              details=f"cancelled {op['action']}")
    return {"result": "cancelled", "action": op["action"]}


# --- Backward compat routes (old /api/conan/* and /api/vm/* paths) ---

@router.get("/api/vm/status")
async def compat_vm_status(request: Request):
    return await vm_status(request, "conan")

@router.post("/api/vm/start")
async def compat_vm_start(request: Request):
    return await vm_start(request, "conan")

@router.post("/api/vm/stop")
async def compat_vm_stop(request: Request):
    return await vm_stop(request, "conan")

@router.post("/api/vm/reset")
async def compat_vm_reset(request: Request):
    return await vm_reset(request, "conan")

@router.get("/api/conan/status")
async def compat_conan_status(request: Request):
    return await server_status(request, "conan")

@router.post("/api/conan/start")
async def compat_conan_start(request: Request):
    return await server_start(request, "conan")

@router.post("/api/conan/stop")
async def compat_conan_stop(request: Request):
    return await server_stop(request, "conan")

@router.post("/api/conan/restart")
async def compat_conan_restart(request: Request):
    return await server_restart(request, "conan")

@router.post("/api/conan/broadcast")
async def compat_broadcast(request: Request, body: MessageRequest):
    return await server_broadcast(request, "conan", body)

@router.get("/api/conan/gamedb")
async def compat_gamedb(request: Request):
    return await game_savedata(request, "conan")

@router.get("/api/zfs/snapshots")
async def compat_zfs_list(request: Request):
    return await zfs_snapshots(request, "conan")

@router.post("/api/zfs/snapshot")
async def compat_zfs_create(request: Request):
    return await zfs_snapshot(request, "conan")
