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
