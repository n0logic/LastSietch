"""The player's own deliveries read (Fremkit wave 12, lane A).

One route. It answers with the packages the server sent the SIGNED-IN account:
the Welcome Package on a new account, the Return Package after 28 days away, and
for each of them which legs have landed and which are still owed.

Three properties matter more than the feature:

  * IDENTITY IS THE SESSION'S. The account comes off `_require_linked_session_json`
    and nothing else. There is no account parameter, no body, and no way to ask
    about somebody else's packages.
  * A DEAD RELAY IS A 200. `available:false` with empty everything, so the panel
    seals in place and the page never renders a toast for a read it makes on
    every load. A 502 here would be a broken Mailbox page.
  * NO INTERNAL IDENTIFIER LEAVES. The shaping module drops the account id, the
    actor, the controller, the grant ids and the raw notes text before this route
    ever sees the payload, and the suite re-checks every key of the real output.

Read-only, no CSRF, no writes. Auth: linked session (JSON 401).
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import portal_deliveries
from routers.portal import _require_linked_session_json

logger = logging.getLogger("portal")
router = APIRouter()

# --- routes ------------------------------------------------------------------


@router.get("/portal/deliveries/v2")
async def portal_deliveries_v2(request: Request):
    """{ok, available, read_at, packages, skip, summary} for the active account.

    private, no-store: this is per-player state behind a session cookie, and a
    shared cache holding one player's packages is the whole hazard."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _session, _discord_id, account_id, _row = gate

    shaped = await portal_deliveries.load(account_id)
    if shaped is None:
        payload = {"ok": True, "available": False, "packages": [],
                   "skip": None, "summary": None}
    else:
        payload = dict(shaped)
        payload["ok"] = True
    return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})
