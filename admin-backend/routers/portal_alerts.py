"""Internal market price-alert delivery endpoint (Cielago DM pull).

Cielago (the Last Sietch Discord bot) runs on the same box as this admin
backend, so it polls these routes on localhost to pull undelivered market price
alerts and DM the player, then acks the delivered ids. The alert data lives in
admin.db (not the game DB), so this does NOT go through the lastsietch-relay.

These routes are mounted at /_internal/* which Caddy does NOT proxy on the public
lastsietch.com host. As defence in depth they are also guarded by a shared key
(PORTAL_ALERT_POLL_KEY) compared in constant time: no key configured -> 503; bad
key -> 401. Nothing here reads or writes the game DB.
"""
import logging
import os
import secrets

from fastapi import APIRouter, Header, HTTPException, Request
from typing import Optional

import config
import market_watch

logger = logging.getLogger("portal.alerts")

router = APIRouter()


def _require_key(provided: Optional[str]) -> None:
    expected = config.PORTAL_ALERT_POLL_KEY
    if not expected:
        raise HTTPException(status_code=503, detail="Alert delivery not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid alert key")


@router.get("/_internal/market-alerts/pending")
async def market_alerts_pending(
    limit: int = 50,
    x_alert_key: Optional[str] = Header(default=None),
):
    """Undelivered (dm_sent=0) market price alerts for Cielago to DM. Each row
    carries the recipient discord_id + the item/threshold/match for the message."""
    _require_key(x_alert_key)
    limit = max(1, min(int(limit or 50), 200))
    alerts = market_watch.pending_dms(limit)
    return {"count": len(alerts), "alerts": alerts}


@router.post("/_internal/market-alerts/ack")
async def market_alerts_ack(
    request: Request,
    x_alert_key: Optional[str] = Header(default=None),
):
    """Mark alert ids as DM-delivered (or permanently given up). Body:
    {"ids": [int,...], "note": "optional outcome"}. Idempotent."""
    _require_key(x_alert_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    note = body.get("note") if isinstance(body, dict) else None
    if note is not None:
        note = str(note)[:120]
    updated = market_watch.ack_dms(ids, note)
    return {"acked": updated}


# ------------------------------------------- Landsraad claim receipts (DM) --
#
# Same transport, same key as the price alerts. A receipt is only handed out once
# the player is ONLINE again: a claim requires them offline, so being online now is
# what "when they return" means. Mirror scalars first (cheap), then the live
# advisory check; an undetermined state waits for the next poll rather than guessing.

async def _online_now(account_id: int):
    import mirror
    try:
        scalars = mirror.get_scalars(account_id)
        if scalars is not None:
            return bool(scalars["online"])
        from rmq_command import is_online
        return await is_online(account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("landsraad claim dm: online check failed acct=%s: %s", account_id, exc)
        return None


@router.get("/_internal/landsraad-claims/pending")
async def landsraad_claims_pending(
    limit: int = 50,
    x_alert_key: Optional[str] = Header(default=None),
):
    """Undelivered Landsraad claim receipts whose player is online now. Each row
    carries the recipient discord_id and the friendly receipt (no internal ids)."""
    _require_key(x_alert_key)
    import landsraad_claim
    limit = max(1, min(int(limit or 50), 200))
    ready = await landsraad_claim.ready_dms(
        _online_now, limit, settle_s=config.MIRROR_MAX_STALE + 60)
    return {"count": len(ready), "claims": ready}


@router.post("/_internal/landsraad-claims/ack")
async def landsraad_claims_ack(
    request: Request,
    x_alert_key: Optional[str] = Header(default=None),
):
    """Mark claim receipt ids as DM-delivered (or permanently given up). Body:
    {"ids": [int,...], "note": "optional outcome"}. Idempotent."""
    _require_key(x_alert_key)
    import landsraad_claim
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    note = body.get("note") if isinstance(body, dict) else None
    if note is not None:
        note = str(note)[:120]
    return {"acked": landsraad_claim.ack_dms(ids, note)}


# ------------------------------------ Portal events -> Discord scheduled events --
#
# Same transport, same key. event_discord decides what Discord should show; Cielago
# makes it so and acks. LASTSIETCH_EVENT_DISCORD_ENABLED=0 (default) hands out nothing, so
# the cog idles until the owner-watched test.

@router.get("/_internal/events/discord/pending")
async def events_discord_pending(
    limit: int = 50,
    x_alert_key: Optional[str] = Header(default=None),
):
    _require_key(x_alert_key)
    if not config.EVENT_DISCORD_ENABLED:
        return {"enabled": False, "count": 0, "events": []}
    import event_discord
    limit = max(1, min(int(limit or 50), 200))
    events = event_discord.pending(limit)
    return {"enabled": True, "count": len(events), "events": events}


@router.post("/_internal/events/discord/ack")
async def events_discord_ack(
    request: Request,
    x_alert_key: Optional[str] = Header(default=None),
):
    """Body: {"results": [{id, rev, ok, discord_event_id?, discord_status?, error?}]}."""
    _require_key(x_alert_key)
    import event_discord
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        raise HTTPException(status_code=400, detail="results must be a list")
    return {"acked": event_discord.ack(results[:200])}


@router.get("/_internal/events/discord/banner/{slug}")
async def events_discord_banner(slug: str, x_alert_key: Optional[str] = Header(default=None)):
    """The shipped banner webp, for the Discord cover image (Cielago cannot read
    lastsietch-admin's build directory)."""
    _require_key(x_alert_key)
    from fastapi.responses import FileResponse
    from routers import portal_events as store
    if not store.valid_banner(slug):
        raise HTTPException(status_code=404, detail="No such banner")
    return FileResponse(os.path.join(store.BANNER_DIR, slug + ".webp"), media_type="image/webp")
