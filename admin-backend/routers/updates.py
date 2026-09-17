"""Steam Update endpoints. Admin-only. CSRF required for trigger.
Wraps update_orchestrator.start_update() and exposes job status polling."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import update_orchestrator
from auth import get_current_user, require_admin, require_csrf
from database import get_db
from relay import call_relay

router = APIRouter(prefix="/api/games")

# Buildwatcher state files written by /opt/lastsietch/check-<game>-build.sh on <web-host>.
# World-readable (mode 644) so lastsietch-admin can read directly without sudo.
BUILDWATCH_DIR = Path("/var/lib/lastsietch")


@router.post("/{game_id}/server/update")
async def trigger_update(request: Request, game_id: str):
    """Kick off a Steam update orchestration in the background. Admin-only.
    Returns 409 if an update is already running for this game."""
    user = require_admin(request)
    require_csrf(request, user)

    try:
        game = await call_relay(f"/games/{game_id}")
    except HTTPException as e:
        raise HTTPException(status_code=e.status_code,
                            detail=f"Failed to load game info: {e.detail}")

    if not game.get("provisioned", True):
        raise HTTPException(503, f"{game.get('display_name', game_id)} is not provisioned")
    if "update" not in game:
        raise HTTPException(400, f"{game.get('display_name', game_id)} has no update configuration")

    try:
        job = await update_orchestrator.start_update(
            game_id=game_id,
            game=game,
            user=user,
            ip=request.client.host,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"result": "started", "job": job}


@router.get("/{game_id}/server/update/status")
async def update_status(request: Request, game_id: str):
    """Return the latest job (running, completed, or failed) for this game, or null."""
    require_admin(request)
    job = update_orchestrator.get_active_job(game_id)
    return {"job": job}


def _read_buildwatch_buildid(game_id: str) -> str | None:
    """Read the latest known buildid from /var/lib/lastsietch/<game_id>-buildid.
    Returns None if the file doesn't exist (e.g., game has no buildwatcher) or is empty."""
    path = BUILDWATCH_DIR / f"{game_id}-buildid"
    try:
        text = path.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not text or not text.isdigit():
        return None
    return text


def _parse_audit_details(details: str) -> dict:
    """Parse 'key=value key=value ...' from audit_log details into a dict.
    Stops parsing a value at the first space, so values containing spaces are truncated —
    fine for our case (job_id is uuid, buildid/phase are alphanumeric)."""
    out = {}
    if not details:
        return out
    for part in details.split(" "):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k] = v
    return out


@router.get("/{game_id}/server/update/history")
async def update_history(request: Request, game_id: str, limit: int = 5):
    """Return the last N update outcomes (completed or failed) for this game.
    Sourced from audit_log; skips update_started rows since terminal events carry the outcome."""
    require_admin(request)
    limit = max(1, min(limit, 50))
    conn = get_db()
    rows = conn.execute(
        """SELECT username, action, timestamp, details, success
           FROM audit_log
           WHERE target = ? AND action IN ('update_completed', 'update_failed')
           ORDER BY id DESC LIMIT ?""",
        (game_id, limit),
    ).fetchall()
    conn.close()

    history = []
    for r in rows:
        d = _parse_audit_details(r["details"] or "")
        history.append({
            "timestamp": r["timestamp"],
            "username": r["username"],
            "action": r["action"],
            "success": bool(r["success"]),
            "buildid": d.get("buildid"),
            "phase": d.get("phase"),
            "duration_s": d.get("duration_s"),
            "job_id": d.get("job_id"),
        })
    return {"history": history}


@router.get("/{game_id}/server/update-available")
async def update_available(request: Request, game_id: str):
    """Compare installed buildid (from VM appmanifest) to latest known (from buildwatch state file).
    Returns: {installed, latest, available, source}.
    - available=true means latest is known AND differs from installed
    - available=false means up to date
    - available=null means we couldn't determine (no watcher data, or installed buildid unreadable)"""
    get_current_user(request)  # any logged-in user can see
    try:
        installed_resp = await call_relay(f"/games/{game_id}/server/installed-buildid")
    except HTTPException as e:
        # Game may not have an update config; bubble up cleanly
        raise HTTPException(e.status_code, f"installed-buildid failed: {e.detail}")

    installed = installed_resp.get("buildid")
    latest = _read_buildwatch_buildid(game_id)

    if installed is None or latest is None:
        available = None
    else:
        available = (installed != latest)

    return {
        "installed": installed,
        "latest": latest,
        "available": available,
        "watcher_state_path": str(BUILDWATCH_DIR / f"{game_id}-buildid"),
    }
