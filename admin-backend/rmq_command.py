"""Shared helpers for the native RMQ server-command path.

One source of truth for resolving an account to its FLS ref and POSTing a job to
the relay `/dune/server-command` route, used by both the V2 Live Actions tab
(v2_actions.py) and online-aware Grant Bench delivery (dune_grant.py). The relay
wrapper (dune-server-command-send.py) remains authoritative — it online-gates,
charset-checks, and audits server-side; these helpers keep the admin-backend
side DRY and add a short player-list cache so batch fires don't hammer the relay.
"""
import time

from relay import call_relay

# 60s TTL cache on the offline-inclusive player list (mirrors the grant roster).
# Resolve + online checks for a whole batch fire then cost one relay round-trip.
_PLAYERS_TTL = 60
_players_cache: dict = {"data": None, "fetched_at": 0.0}


async def _players_list() -> list[dict]:
    now = time.monotonic()
    cached = _players_cache["data"]
    if cached is not None and now - _players_cache["fetched_at"] < _PLAYERS_TTL:
        return cached
    try:
        raw = await call_relay("/dune/grant/players", timeout=45)
    except Exception:
        return cached or []
    players = raw.get("players") or []
    _players_cache["data"] = players
    _players_cache["fetched_at"] = now
    return players


async def resolve_fls_ref(account_id) -> str | None:
    """account_id -> funcom_id (Name#tag) the publisher's --resolve turns into the
    hex FLS id server-side. Returns None if absent; callers MUST reject rather
    than guess an id (the raw funcom_id carries a '#' and is not a bare id)."""
    target = str(account_id)
    for p in await _players_list():
        if str(p.get("account_id", "")) == target:
            ref = p.get("funcom_id") or p.get("fls_id")
            if isinstance(ref, str) and ref.strip():
                return ref.strip()
            return None
    return None


async def is_online(account_id) -> bool:
    """True iff the account's player row reports online_status == Online. Advisory
    — the relay wrapper online-gates authoritatively (a player mid-load is Online
    in the DB but not yet spawned, which the wrapper still refuses)."""
    target = str(account_id)
    for p in await _players_list():
        if str(p.get("account_id", "")) == target:
            return str(p.get("online_status", "")).lower() == "online"
    return False


async def dispatch_server_command(
    resolve_ref: str, verb: str, *, mode: str, operator: str, reason: str,
    args: dict, timeout: int = 60,
) -> dict:
    """POST one server-command job to the relay. Returns the relay envelope
    ({success, mode, detail, verb, ...})."""
    job = {
        "verb": verb,
        "resolve": resolve_ref,
        "mode": mode,
        "operator": operator,
        "reason": reason,
        "args": args,
    }
    return await call_relay("/dune/server-command", "POST", job, timeout=timeout)
