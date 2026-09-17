import httpx
from fastapi import HTTPException

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


async def call_relay(path: str, method: str = "GET", json_body: dict = None, timeout: float = 15.0) -> dict:
    base = _resolve_relay_url(path)
    if not base:
        # Fail closed and say why: an unset relay URL is a configuration state,
        # not a network error, and callers should not retry it.
        raise HTTPException(status_code=503, detail="relay not configured")
    url = f"{base}{path}"
    headers = {"X-API-Key": RELAY_API_KEY}
    client = _get_client()

    if method == "GET":
        resp = await client.get(url, headers=headers, timeout=timeout)
    elif method == "POST":
        resp = await client.post(url, headers=headers, json=json_body, timeout=timeout)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return resp.json()
