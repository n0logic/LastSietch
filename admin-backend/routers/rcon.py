import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, get_current_user, require_admin, require_csrf
from relay import call_relay

router = APIRouter(prefix="/api/rcon")

# Rate limiting: 10 commands per minute per user
_rcon_buckets: dict[str, deque] = defaultdict(deque)
RCON_RATE_LIMIT = 10
RCON_RATE_WINDOW = 60
MAX_CMD_LENGTH = 2000

# Commands safe for operators (from `help` output)
OPERATOR_COMMANDS = {
    "help", "listplayers", "listbans", "broadcast",
    "kickplayer", "banplayer", "unbanplayer",
    "whitelistplayer", "unwhitelistplayer",
    "getserversetting", "getlandowner",
    "memreport", "memreportsilentevent", "dumpticks", "netprofile",
}

# Additional commands only for admins
ADMIN_COMMANDS = OPERATOR_COMMANDS | {
    "setserversetting", "sql", "con", "exec",
    "buildingquery", "buildingdestroy", "buildingcontribution",
    "validateallbuildings", "shutdown", "restart",
}


class RconRequest(BaseModel):
    command: str
    game_id: str = "conan"


# Commands whose ARGUMENTS can carry a secret: `setserversetting AdminPassword ...`
# is the one that actually happens, and `sql`/`con`/`exec` can carry anything. For
# those the audit row keeps the verb and drops the rest; every other command logs in
# full, because "who kicked whom" is the reason the row exists.
SECRET_ARG_COMMANDS = {"setserversetting", "sql", "con", "exec"}


def _audit_details(cmd_name, command, error=None):
    detail = cmd_name if cmd_name in SECRET_ARG_COMMANDS else command[:400]
    parts = [f"cmd={detail}"]
    if error:
        parts.append(f"error={error[:300]}")
    return " ".join(parts)


def _check_rcon_rate(username: str) -> bool:
    now = time.monotonic()
    cutoff = now - RCON_RATE_WINDOW
    bucket = _rcon_buckets[username]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= RCON_RATE_LIMIT:
        return False
    bucket.append(now)
    return True


@router.post("/execute")
async def rcon_execute(request: Request, body: RconRequest):
    user = get_current_user(request)
    require_csrf(request, user)

    command = body.command.strip()
    if not command:
        raise HTTPException(400, "Command cannot be empty")
    if len(command) > MAX_CMD_LENGTH:
        raise HTTPException(400, f"Command too long (max {MAX_CMD_LENGTH} characters)")

    # Rate limit
    if not _check_rcon_rate(user["username"]):
        raise HTTPException(429, "Rate limit exceeded (10 commands/minute)")

    # Reject when the target game doesn't expose RCON.
    try:
        info = await call_relay(f"/games/{body.game_id}")
        if info.get("supports_rcon") is False:
            raise HTTPException(501, f"RCON is not supported for {info.get('display_name', body.game_id)}")
    except HTTPException:
        raise
    except Exception:
        pass

    # Parse command name for allowlist check
    cmd_name = command.split()[0].lower()
    allowed = ADMIN_COMMANDS if user["role"] == "admin" else OPERATOR_COMMANDS
    if cmd_name not in allowed:
        if user["role"] != "admin":
            raise HTTPException(403, f"Command '{cmd_name}' requires admin role")
        # Unknown command, and admins can still try it
        pass

    # Execute via relay.
    #
    # Every path passes success= explicitly. audit_log defaults it to True, so the
    # failure path that omitted it wrote a row claiming the command RAN, and the
    # unexpected-exception path wrote no row at all: read back, the audit trail said
    # every RCON command in the panel's history had succeeded, including the ones that
    # never left the box.
    def _audit(success, error=None):
        audit_log(
            user["id"], user["username"], "rcon_command",
            body.game_id, request.client.host,
            details=_audit_details(cmd_name, command, error),
            success=success,
        )

    try:
        result = await call_relay(
            f"/games/{body.game_id}/server/rcon",
            "POST",
            {"message": command},
            timeout=20,
        )
        response_text = result.get("response", "")
        _audit(True)
        return {"success": True, "response": response_text, "command": command}
    except HTTPException as e:
        _audit(False, str(e.detail))
        return {"success": False, "response": str(e.detail), "command": command}
    except Exception as e:
        _audit(False, str(e))
        return {"success": False, "response": str(e), "command": command}


@router.get("/commands")
async def rcon_commands(request: Request):
    user = get_current_user(request)
    allowed = ADMIN_COMMANDS if user["role"] == "admin" else OPERATOR_COMMANDS
    return {"commands": sorted(allowed), "role": user["role"]}
