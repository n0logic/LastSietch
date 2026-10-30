import time

import httpx
from fastapi import HTTPException

import games_offline
from config import RELAY_API_KEY, RELAY_URL, RELAY_URL_GAMES


def _resolve_relay_url(path: str) -> str:
    # VC0: Dune lives on the localhost relay (<web-host>), Conan/Enshrouded
    # admin still goes through the relay host until VC9. Path prefix is
    # the router: anything Dune-shaped -> Dune relay; everything else (the
    # /games/{conan,enshrouded}/* + /infra/status surface) -> games relay.
    # /server/cvars/* is the P8 CVar surface and currently only Dune ships it.
    if (
        path.startswith("/dune/")
        or path.startswith("/games/dune")
        or path.startswith("/server/cvars")
    ):
        return RELAY_URL
    return RELAY_URL_GAMES


# VC0 perf: shared httpx.AsyncClient. Replaces per-call AsyncClient() context
# managers, so the TCP+TLS connection pool is reused across requests. Created
# lazily on first call and closed in main.py lifespan shutdown.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# The games relay lives on the home Proxmox host, which can drop off the
# tailnet for days (2026-10-16: logged out for 8 days). Without this every
# panel hung for the full timeout and then 500'd with an HTML page the
# frontend couldn't parse. Connect fast, and after a connect failure skip the
# games relay for a short back-off so a page load costs one timeout, not ten.
GAMES_CONNECT_TIMEOUT = 3.0
GAMES_DOWN_BACKOFF = 30.0
_games_down_until = 0.0


async def call_relay(path: str, method: str = "GET", json_body: dict = None, timeout: float = 15.0) -> dict:
    # Games listed in /etc/lastsietch/games-offline never reach the games relay: its
    # host is dark for a planned move and every call would only time out.
    if path == "/infra/status" or path == "/games" or path.startswith("/games/"):
        offline, note = games_offline.read()
        if offline:
            return await _call_relay_offline(path, method, json_body, timeout, offline, note)
    return await _call_relay(path, method, json_body, timeout)


async def _call_relay_offline(path, method, json_body, timeout, offline, note):
    whole_host = games_offline.whole_host_offline(offline)
    if path == "/infra/status":
        if whole_host:
            return games_offline.host_status(note)
        return await _call_relay(path, method, json_body, timeout)
    if path == "/games":
        if whole_host:
            return games_offline.registry()
        return await _call_relay(path, method, json_body, timeout)
    if path == "/games/status/batch":
        if whole_host:
            return games_offline.batch(note)
        result = await _call_relay(path, method, json_body, timeout)
        for gid in offline:
            if gid in result or gid in games_offline.GAMES_RELAY:
                result[gid] = games_offline.status_entry(note)
        return result

    game_id, sep, rest = path[len("/games/"):].partition("/")
    if game_id not in offline:
        return await _call_relay(path, method, json_body, timeout)
    return games_offline.answer(game_id, sep + rest, method, note)


async def _call_relay(path: str, method: str, json_body: dict, timeout: float) -> dict:
    global _games_down_until
    base = _resolve_relay_url(path)
    if not base:
        # Fail closed and say why: an unset relay URL is a configuration state,
        # not a network error, and callers should not retry it.
        raise HTTPException(status_code=503, detail="relay not configured")
    url = f"{base}{path}"
    headers = {"X-API-Key": RELAY_API_KEY}
    client = _get_client()

    is_games = base == RELAY_URL_GAMES and base != RELAY_URL
    if is_games:
        if time.monotonic() < _games_down_until:
            raise HTTPException(status_code=503, detail="game host relay unreachable")
        req_timeout = httpx.Timeout(timeout, connect=GAMES_CONNECT_TIMEOUT)
    else:
        req_timeout = timeout

    if method not in ("GET", "POST"):
        raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")
    try:
        if method == "GET":
            resp = await client.get(url, headers=headers, timeout=req_timeout)
        else:
            resp = await client.post(url, headers=headers, json=json_body, timeout=req_timeout)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # Dune callers (dune_grant, portal) catch raw httpx errors themselves,
        # so only the games relay is translated here.
        if not is_games:
            raise
        _games_down_until = time.monotonic() + GAMES_DOWN_BACKOFF
        raise HTTPException(status_code=503, detail="game host relay unreachable") from e
    except httpx.TimeoutException as e:
        if not is_games:
            raise
        raise HTTPException(status_code=504, detail="game host relay timed out") from e

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return resp.json()
