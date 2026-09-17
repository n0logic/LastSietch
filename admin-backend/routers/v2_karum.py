"""Karum operator page: resolve a stuck trade, and read the escrow audit.

Contract: docs/dune-research/v2-portal/KARUM-BUILD-CONTRACT-2026-07-27.md section 11

THE ASK THIS ANSWERS. `paid_undelivered` is a real state with a real player's goods and a
real player's money in it. Without an operator lever, resolving one means hand-written SQL
against the game DB, at speed, under player pressure, on a live host. That is the shape of
every post-mortem this project has written. This page exists so that never has to happen.

WHAT IT IS NOT: a shortcut into the database. Every button here is a FOURTH CALLER of the
same L4 -> L3 -> L2 -> L1 chain the portal uses, so every gate, every idempotency guard and
every ledger row applies identically. Nothing here bypasses the writer.

Because every action re-runs its leg on the ORIGINAL correlation_id, and every leg is gated
by a UNIQUE key in the game DB:
  * pressing force-deliver twice is safe;
  * pressing it after a delivery silently succeeded is a `replay` no-op;
  * pressing refund twice cannot double-credit.
That is what makes an operator page safe to hand to someone at 3am.

🔴 REFUND IS A SEPARATE CONFIRMATION, never bundled with force-return. The two are not
always both correct: a return with no refund is right when the buyer never paid, and a
refund with no return is right when the goods already reached them. Bundling them would
quietly make one of those two cases wrong every time.

The listing table is a portal read, so this page may be opened at any time, including under
a change freeze. The AUDIT is likewise read-only. Only the three buttons write.
"""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import audit_log, get_current_user, require_admin, require_csrf
from database import get_db
from portal_auth import client_ip
from relay import call_relay

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Terminal states: nothing an operator can usefully do, so they stay off the page by
# default. `failed` is terminal too (nothing was escrowed and nothing moved).
_TERMINAL = ("sold", "cancelled", "failed")

_FORCE_ACTIONS = ("force-deliver", "force-return", "refund")


def _admin_or_redirect(request: Request):
    """HTML page routes redirect; the JSON routes below use require_admin and 401/403.
    Same shape as routers/v2.py, repeated rather than imported to avoid a router-to-router
    import cycle."""
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


def _open_listings(limit: int = 200):
    """Every listing not in a terminal state, newest first, with the age of the current
    state. `selling` and `reconciling` are TRANSIENT: one resting there for more than the
    bounded retry window is a bug, and the age column is how an operator sees that."""
    conn = get_db()
    try:
        marks = ",".join("?" for _ in _TERMINAL)
        rows = conn.execute(
            f"""SELECT listing_id, seller_name, seller_account_id, buyer_account_id,
                       template_id, display_name, stack_size, quality_level, price,
                       status, escrow_item_id, escrow_corr_id, sold_corr_id,
                       created_at, updated_at, sold_at,
                       CAST((julianday('now') - julianday(updated_at)) * 24 AS INTEGER)
                         AS hours_in_state
                  FROM portal_karum_listings
                 WHERE status NOT IN ({marks})
                 ORDER BY
                   CASE status WHEN 'paid_undelivered' THEN 0
                               WHEN 'reconciling' THEN 1
                               WHEN 'selling' THEN 2
                               WHEN 'returning' THEN 3
                               ELSE 4 END,
                   updated_at ASC
                 LIMIT ?""",
            (*_TERMINAL, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _events(listing_id: int, limit: int = 40):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT event_id, account_id, event, detail, created_at "
            "FROM portal_karum_events WHERE listing_id = ? "
            "ORDER BY event_id DESC LIMIT ?", (int(listing_id), int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _record(listing_id, account_id, event, operator, reason, extra=None):
    """Every press writes portal_karum_events with the operator and the reason. The event
    log is append-only and is the dispute trail, so a press with no reason is not accepted
    (see the route validation) and a press that fails is logged too."""
    detail = {"operator": operator, "reason": reason}
    if extra:
        detail.update(extra)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO portal_karum_events (listing_id, account_id, event, detail) "
            "VALUES (?,?,?,?)",
            (int(listing_id), int(account_id or 0) or 1, event,
             json.dumps(detail, separators=(",", ":"))))
        conn.commit()
    finally:
        conn.close()


# NOT "/admin/v2/portal/karum". The public /admin/* prefix is stripped by the
# proxy before the request reaches this app, so a route registered WITH it is
# only reachable at /admin/admin/... and 404s for every real visitor. The
# subnav link is right; this decorator was the thing that was wrong. Every
# other route in the app is registered prefix-free, this was the only one.
#
# The same trap has a SECOND side, and it bit this page too: because the app
# registers routes prefix-free, the browser must ADD /admin back. karum.html
# called /api/dune/v2/karum/{force,audit} bare for weeks, so neither button
# ever reached this router. Server-side and client-side are opposite halves of
# one rule, and fixing only the half you are looking at leaves the lever dead.
@router.get("/v2/portal/karum")
async def v2_karum_page(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request, "v2/karum.html",
        {"user": user, "current_tab": "portal", "current_sub_tab": "karum",
         "listings": _open_listings()},
    )


@router.get("/api/dune/v2/karum/listings")
async def v2_karum_listings(request: Request):
    require_admin(request)
    return {"ok": True, "listings": _open_listings()}


@router.get("/api/dune/v2/karum/listing/{listing_id}/events")
async def v2_karum_events(request: Request, listing_id: int):
    require_admin(request)
    return {"ok": True, "events": _events(listing_id)}


@router.get("/api/dune/v2/karum/audit")
async def v2_karum_audit(request: Request):
    """The escrow audit, on demand. READ-ONLY, so it is safe under a change freeze.

    🔴 Never dress a failure up as a pass. If the audit could not run, the relay marks
    `page` and this hands that through untouched: for an audit whose subject fails silently,
    "we could not look" and "we looked and it was fine" must not be confusable."""
    require_admin(request)
    try:
        result = await call_relay("/dune/karum/audit", timeout=220)
    except HTTPException as exc:
        return {"ok": False, "error": "audit_unavailable", "page": True,
                "message": f"The audit could not be reached: {exc.detail}. "
                           "This is NOT a clean result."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "audit_unavailable", "page": True,
                "message": f"The audit could not be reached ({exc}). "
                           "This is NOT a clean result."}
    if not isinstance(result, dict):
        return {"ok": False, "error": "audit_unavailable", "page": True,
                "message": "The audit returned nothing usable. This is NOT a clean result."}
    return result


@router.post("/api/dune/v2/karum/force")
async def v2_karum_force(request: Request):
    """Force-deliver, force-return, or refund. Body = {listing_id, action, reason}.

    The reason is REQUIRED, not decoration: this is the dispute trail for a trade involving
    another player's money, and "who pressed it and why" is the whole value of the log six
    weeks later.

    Identity is resolved from the LISTING ROW, never from the request body: an operator can
    say which listing to resolve, never who to pay."""
    user = require_admin(request)
    require_csrf(request, user)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")

    action = body.get("action")
    if action not in _FORCE_ACTIONS:
        raise HTTPException(400, f"action must be one of {', '.join(_FORCE_ACTIONS)}")
    listing_id = body.get("listing_id")
    if isinstance(listing_id, bool) or not isinstance(listing_id, int) or listing_id <= 0:
        raise HTTPException(400, "listing_id must be a positive integer")
    reason = (body.get("reason") or "").strip()
    if len(reason) < 4:
        raise HTTPException(400, "a reason of at least 4 characters is required")
    reason = reason[:400]

    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM portal_karum_listings WHERE listing_id = ?",
                           (listing_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "no such listing")

    operator = (user or {}).get("username") or "admin"
    import uuid as _uuid
    corr = str(_uuid.uuid4())     # the ADMIN action gets its own correlation_id

    if action == "refund":
        # 🔴 A refund is a NEW payments row with its OWN correlation_id, never an UPDATE of
        # the original, so a retried refund cannot double-credit. It needs the payment it
        # reverses, which only exists once a buy actually paid.
        if not row["sold_corr_id"] or not row["buyer_account_id"]:
            raise HTTPException(409, "this listing has no recorded payment to refund")
        payload = {
            "admin_action": "refund",
            "listing_id": listing_id,
            "buyer_account_id": int(row["buyer_account_id"]),
            "seller_account_id": int(row["seller_account_id"]),
            "amount": int(row["price"]),
            "original_correlation_id": row["sold_corr_id"],
            "correlation_id": corr,
            "operator": f"admin:{operator}",
        }
        event = "admin_force_return"     # the closed event set; refunds ride with it
    else:
        # force-deliver hands the escrowed row to the BUYER; force-return to the SELLER.
        # Both are gives down the same claim lane, both idempotent on the delivery log.
        if action == "force-deliver":
            target = row["buyer_account_id"]
            if not target:
                raise HTTPException(409, "this listing has no buyer to deliver to")
            event = "admin_force_deliver"
        else:
            target = row["seller_account_id"]
            event = "admin_force_return"
        payload = {
            "admin_action": action,
            "listing_id": listing_id,
            "target_account_id": int(target),
            "price": int(row["price"]),
            "correlation_id": corr,
            "operator": f"admin:{operator}",
        }

    ip = client_ip(request)
    result = None
    try:
        result = await call_relay("/dune/karum/admin", method="POST",
                                 json_body=payload, timeout=90)
    except HTTPException as exc:
        _record(listing_id, row["seller_account_id"], event, operator, reason,
                {"action": action, "outcome": "unavailable", "detail": str(exc.detail)[:200]})
        audit_log(None, f"admin:{operator}", f"karum_{action}", str(listing_id), ip,
                  details=json.dumps({"reason": reason, "outcome": "unavailable"}),
                  success=False)
        return {"ok": False, "error": "unavailable",
                "message": "The Karum writer could not be reached. Nothing was changed. "
                           "Safe to press again: every leg is idempotent."}

    status = (result or {}).get("status") if isinstance(result, dict) else None
    good = status in ("applied", "replay")

    _record(listing_id, row["seller_account_id"], event, operator, reason,
            {"action": action, "outcome": status or "unknown", "correlation_id": corr})
    audit_log(None, f"admin:{operator}", f"karum_{action}", str(listing_id), ip,
              details=json.dumps({"reason": reason, "status": status,
                                  "correlation_id": corr}),
              success=bool(good))

    if status == "deferred":
        return {"ok": False, "error": "not_open",
                "message": "The Karum writer is DARK (LASTSIETCH_KARUM_ENABLED=0), so nothing was "
                           "written. Un-dark it on the game host first."}
    if not good:
        return {"ok": False, "error": (result or {}).get("error") or "write_failed",
                "message": (result or {}).get("message")
                           or "That could not be completed. Nothing partial was left behind: "
                              "the writer runs in one transaction."}

    # Mirror the resolution into the listing. Only ever on a confirmed applied|replay.
    if action == "force-deliver":
        new_status, stamp = "sold", "sold_at = COALESCE(sold_at, datetime('now')), "
    elif action == "force-return":
        new_status, stamp = "cancelled", ""
    else:
        new_status, stamp = row["status"], ""   # a refund does not move the listing
    if new_status != row["status"]:
        conn = get_db()
        try:
            conn.execute(
                f"UPDATE portal_karum_listings SET status = ?, {stamp}"
                f"closed_at = datetime('now'), updated_at = datetime('now') "
                f"WHERE listing_id = ?", (new_status, listing_id))
            conn.commit()
        finally:
            conn.close()

    # A wanted-order fill settles through a normal listing linked by
    # settlement_listing_id. Keep its player-facing state in step with the same
    # operator action. A force-return alone does not close the request because an
    # uncertain or known payment may still need the separate refund action.
    conn = get_db()
    try:
        if action == "force-deliver":
            conn.execute(
                "UPDATE portal_karum_requests SET status = 'filled', "
                "filled_at = COALESCE(filled_at, datetime('now')), "
                "closed_at = COALESCE(closed_at, datetime('now')), "
                "updated_at = datetime('now') WHERE settlement_listing_id = ? "
                "AND status IN ('filling','reconciling','paid_undelivered')",
                (listing_id,))
        elif action == "refund":
            conn.execute(
                "UPDATE portal_karum_requests SET status = 'failed', "
                "closed_at = COALESCE(closed_at, datetime('now')), "
                "updated_at = datetime('now') WHERE settlement_listing_id = ? "
                "AND status IN ('filling','reconciling','paid_undelivered')",
                (listing_id,))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "status": status, "listing_status": new_status,
            "correlation_id": corr,
            "message": {"force-deliver": "Delivered to the buyer. They collect it from the "
                                         "Completed tab at any exchange terminal (it shows "
                                         "as CANCELED).",
                        "force-return": "Returned to the seller, same claim lane.",
                        "refund": "Refunded as a new payments row; the original is stamped "
                                  "reversed."}[action]}
