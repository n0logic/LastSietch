"""Seven Nights of the Sietch: the portal page API and its admin lane.

Player routes (/portal/sietch/*) and admin routes (/v2/sietch/*). All state is
admin.db; the only game reads are the existing Tier A relay verbs (the caller's
OWN position for a Knock, the caller's own login days for a visit), and nothing
here writes dune.*.

Three properties matter more than the feature:

  * THE GATE IS READ PER REQUEST. LASTSIETCH_SIETCH_NIGHTS_ENABLED closes earning (a
    Knock answers `closed`, /state stops refreshing evidence) and the public
    reads go empty; LASTSIETCH_SIETCH_SHOP_ENABLED is the shop's own gate. Both default
    OFF and both read through feature_flags, so an override flip needs no restart.
  * NO COORDINATE LEAVES THE SERVER ON A PLAYER ROUTE. Spots project through
    sietch_nights.public_spots (id, name, kind, done) and a Knock returns a
    token, a band or a rounded distance to a PUBLIC spot only. Only the admin
    spots route returns x/y.
  * IDENTITY IS SERVER-SIDE. The profile comes from the session (pid) or from
    the session account's active link, never from the body; the Knock reads the
    session account's position, never one the client names.
"""
import asyncio
import json
import logging
import os
import re

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import feature_flags
import map_model
import sietch_nights as sn
from cache import ttl_cache
from auth import audit_log, get_current_user, require_admin, require_csrf
from database import get_db
from portal_auth import client_ip
from routers.portal import (
    _require_linked_session_json,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()

EARN_FLAG = "LASTSIETCH_SIETCH_NIGHTS_ENABLED"
SHOP_FLAG = "LASTSIETCH_SIETCH_SHOP_ENABLED"
PRIVATE_CACHE = "private, no-store"
PUBLIC_CACHE = "public, max-age=60"
# /state refreshes at most this many of the viewer's accounts per load; each is a
# cached (60 s) relay read, so a reload storm costs one hop per account a minute.
STATE_REFRESH_ACCOUNTS = 4
RELAY_WAIT_S = 6
ORDER_CHARACTER_WAIT_S = 2
_COPY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "sietch_nights_copy.json")
_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_CATALOG_PATH = os.path.join(_DATA, "sietch_nights_catalog.json")
_DENY_PATH = os.path.join(_DATA, "sietch_nights_deny.json")
_CATALOG_CACHE = {}
DELIVER_TIMEOUT_S = 20
_CASE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
AWARD_MAX = 50


def _earning():
    return bool(feature_flags.enabled(EARN_FLAG, "0"))


def _shop():
    return _earning() and bool(feature_flags.enabled(SHOP_FLAG, "0"))


def _copy():
    # Read per call: dev copy edits ship as a data file, and a missing or broken
    # file costs the copy block, never the page.
    try:
        with open(_COPY_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog():
    """The loaded catalog, re-read when either file or LASTSIETCH_SIETCH_SKU_OFF changes.
    Refused rows are logged once per load; they never reach a player."""
    off = tuple(sorted(x.strip() for x in os.environ.get("LASTSIETCH_SIETCH_SKU_OFF", "").split(",") if x.strip()))
    try:
        key = (os.path.getmtime(_CATALOG_PATH), os.path.getmtime(_DENY_PATH), off)
    except OSError:
        return {}
    if _CATALOG_CACHE.get("key") != key:
        try:
            rows, refused = sn.load_catalog(_CATALOG_PATH, _DENY_PATH, off)
        except (OSError, ValueError) as exc:
            logger.error("sietch: catalog load failed: %s", exc)
            return _CATALOG_CACHE.get("rows") or {}
        for sku, why in refused:
            logger.error("sietch: catalog row %r refused: %s", sku, why)
        _CATALOG_CACHE.update(key=key, rows=rows)
    return _CATALOG_CACHE["rows"]


def pref_owned(session, account_id, slot, key):
    """The prefs PUT's ownership check for sietch_flair / sietch_mapdot: the key
    must belong to a delivered order of that slot on this profile (a prefs row,
    or a bank row that also grants a pref_key once its game leg delivers)."""
    rows = catalog()
    if not any(r.get("kind") == slot and r.get("pref_key") == key for r in rows.values()):
        return False
    conn = get_db()
    try:
        pid = _profile_id(conn, session, account_id)
        return pid is not None and key in sn.owned_pref_keys(conn, pid, rows, slot)
    finally:
        conn.close()


def _night_copy(copy, n):
    nights = copy.get("nights")
    if isinstance(nights, list):
        for row in nights:
            if isinstance(row, dict) and row.get("n") == n:
                return row
    return None


def _profile_id(conn, session, account_id):
    pid = (session or {}).get("pid")
    if sn.valid_profile_id(pid):
        return pid
    pid = sn.profile_for_account(conn, account_id)
    return pid if sn.valid_profile_id(pid) else None


async def _gather_evidence(accounts):
    """The viewer's own levels and login days, read with NO database connection
    held: a write lock must never be open across a relay await."""
    from routers.dune import cached_reward_login_days
    import mirror
    out = []
    for aid in accounts[:STATE_REFRESH_ACCOUNTS]:
        scal = mirror.get_scalars(aid)
        dates = None
        try:
            payload = await asyncio.wait_for(cached_reward_login_days(aid), timeout=RELAY_WAIT_S)
            days = payload.get("login_days") if isinstance(payload, dict) else None
            if isinstance(days, list):
                dates = [r.get("date_utc") for r in days if isinstance(r, dict)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("sietch: login-days refresh failed acct=%s: %s", aid, exc)
        out.append((aid, scal.get("lvl") if scal else None, dates))
    return out


def _fold_and_credit(conn, pid, accounts, evidence):
    """One short write txn: the evidence just gathered, then the profile's credit,
    so a visit credits on the first page load instead of the next worker tick."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for aid, lvl, dates in evidence:
            sn.record_level(conn, aid, lvl)
            if dates is not None:
                sn.record_login_days(conn, aid, dates)
        sn.credit_profile(conn, pid, accounts)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _night_block(conn, night, done, copy):
    spots = sn.spots_for(conn, night["key"])
    clue = sn.current_clue(spots, done)
    return spots, dict(night, copy=_night_copy(copy, night["n"]),
                       spots_public=sn.public_spots(spots, done, map_model.project),
                       clue_current={k: v for k, v in clue.items() if not k.startswith("_")} if clue else None)


# --- player routes -----------------------------------------------------------


@router.get("/portal/sietch/state")
async def portal_sietch_state(request: Request):
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    session, discord_id, account_id, _row = gate
    earning, shop = _earning(), _shop()
    copy = _copy()
    conn = get_db()
    try:
        pid = _profile_id(conn, session, account_id)
        accounts = (sn.profile_accounts(conn, pid) or [int(account_id)]) if pid else []
    finally:
        conn.close()
    if pid is None:
        return _v2_err("unlinked", "Link a game character to join the Seven Nights.", status=409)
    if _shop():
        _warm_characters(account_id)   # so a buy from this page resolves from cache
    evidence = await _gather_evidence(accounts) if earning and sn.credit_open() else None
    conn = get_db()
    try:
        if evidence is not None:
            _fold_and_credit(conn, pid, accounts, evidence)
        # Dark: no spot names, riddles or copy for tonight leave the server.
        night = sn.current_night() if earning else None
        night_out, progress = None, None
        if night:
            done = sn._hit_refs(conn, pid, night["key"])
            spots, night_out = _night_block(conn, night, done, copy)
            progress = sn.progress(conn, pid, night["key"], spots)
        payload = {
            "night": night_out,
            "next_night_starts_at": sn.next_night_start(),
            "progress": progress,
            "eligible": sn.profile_eligible(conn, pid, accounts),
            "balance": sn.balance(conn, pid),
            "earned_total": sn.earned_total(conn, pid),
            "calendar": sn.calendar(conn, pid),
            "catalog": sn.catalog_view(conn, pid, catalog()),
            "orders": sn.orders_view(conn, pid, catalog()),
            "ledger": sn.ledger_rows(conn, pid),
            "prefs": sn.sietch_prefs(sn.read_prefs(conn, discord_id)),
            "flags": {"earning": earning, "shop": shop},
        }
    finally:
        conn.close()
    resp = _v2_ok(payload)
    resp.headers["Cache-Control"] = PRIVATE_CACHE
    return resp


@router.post("/portal/sietch/knock")
async def portal_sietch_knock(request: Request):
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    session, discord_id, account_id, _row = gate
    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    if not _earning() or sn.current_night() is None:
        return _v2_ok({"result": "closed"})
    spot_id = body.get("spot_id")
    if not (isinstance(spot_id, str) and sn._SPOT_ID_RE.fullmatch(spot_id)):
        spot_id = None

    # The caller's OWN position, read server-side. The cache marks a value it
    # served past its ttl after a failed fetch as stale, and a stale read pays nothing.
    pos = None
    try:
        from routers.dune import cached_player_map
        raw = await asyncio.wait_for(cached_player_map(int(account_id)), timeout=RELAY_WAIT_S)
        if isinstance(raw, dict) and raw.get("available") and isinstance(raw.get("self"), dict):
            pos = dict(raw["self"], stale=bool(raw.get("stale")))
    except Exception as exc:  # noqa: BLE001
        # A failed read is "stand a moment and knock again", not "you are offline".
        pos = {"stale": True}
        logger.warning("sietch knock: position read failed acct=%s: %s", account_id, exc)

    conn = get_db()
    try:
        pid = _profile_id(conn, session, account_id)
        if pid is None:
            return _v2_err("unlinked", "Link a game character to Knock.", status=409)
        out = sn.knock(conn, pid, int(account_id), pos, spot_id=spot_id, storm_live=False)
        out["balance"] = sn.balance(conn, pid)
    finally:
        conn.close()
    resp = _v2_ok(out)
    resp.headers["Cache-Control"] = PRIVATE_CACHE
    return resp


@ttl_cache("sietch.characters", ttl=120, max_stale=600)
async def _character_list(account_id: int) -> dict:
    # Cached: a stale entry can at worst name a character deleted since, and the
    # writer refuses that one as not_owner, which refunds.
    from relay import call_relay
    return await call_relay("/dune/player/%d/progress?list=1" % int(account_id), timeout=15)


_WARM_TASKS = set()   # strong refs, as for _DELIVER_TASKS


def _warm_characters(account_id):
    task = asyncio.ensure_future(_character_list(int(account_id)))
    _WARM_TASKS.add(task)

    def _done(t):
        _WARM_TASKS.discard(t)
        t.cancelled() or t.exception()
    task.add_done_callback(_done)
    return task


async def _resolve_character(request, account_id, wanted):
    """The controller to deliver to: the one asked for, else the selected one,
    else the account default. Always one of the account's live, non-Deleted
    characters; the writer re-checks ownership regardless. Waits at most
    ORDER_CHARACTER_WAIT_S: a cold, slow relay read keeps going in the
    background and warms the cache, and this order is refused with no debit."""
    from routers.portal import _selected_ctrl
    payload = await asyncio.wait_for(asyncio.shield(_warm_characters(account_id)),
                                     timeout=ORDER_CHARACTER_WAIT_S)
    chars = (payload or {}).get("characters") or []
    ids = [c.get("controller_id") for c in chars if isinstance(c.get("controller_id"), int)]
    if wanted is not None:
        return wanted if wanted in ids else None
    sel = _selected_ctrl(request, int(account_id))
    if sel in ids:
        return sel
    for c in chars:
        if c.get("is_default") and c.get("controller_id") in ids:
            return c["controller_id"]
    return ids[0] if len(ids) == 1 else None


async def deliver(order):
    """Leg 2, inline with a short timeout. Any exception is an ambiguous result:
    the order goes to reconciling and the sweep re-sends the same key. Returns
    None when the order could not be claimed (another sender holds it)."""
    from relay import call_relay
    conn = get_db()
    try:
        order = sn.begin_delivery(conn, order["id"])
    finally:
        conn.close()
    if order is None:
        return None
    outcome = None
    try:
        outcome = await asyncio.wait_for(
            call_relay("/dune/reward-op", method="POST", json_body=sn.writer_payload(order),
                       timeout=DELIVER_TIMEOUT_S), timeout=DELIVER_TIMEOUT_S + 5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sietch deliver: order %s ambiguous: %s", order["id"], exc)
    conn = get_db()
    try:
        return sn.apply_outcome(conn, order["id"], outcome)
    finally:
        conn.close()


_DELIVER_TASKS = set()   # strong refs: the loop keeps only weak ones to a bare task


def _deliver_in_background(order):
    """One delivery try after the order POST has answered, so no request waits
    on the writer. The begin_delivery claim keeps it from racing the sweep; if
    the process dies first, the sweep sends the same key."""
    task = asyncio.ensure_future(deliver(order))
    _DELIVER_TASKS.add(task)

    def _done(t):
        _DELIVER_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("sietch deliver: order %s background try failed: %s", order["id"], t.exception())
    task.add_done_callback(_done)


@router.post("/portal/sietch/order")
async def portal_sietch_order(request: Request):
    """{sku, idem_key, character_id?} -> {order, balance}. The debit and the order
    row are one local txn; a game-lane order answers 'queued' at once and gets one
    delivery try in the background. The page polls /state for the outcome."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    session, discord_id, account_id, _row = gate
    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)
    if not _shop():
        return _v2_err("closed", "The Stall is closed for now.", status=409)
    rows = catalog()
    sku = body.get("sku")
    row = rows.get(sku) if isinstance(sku, str) else None
    if row is None:
        return _v2_err("unknown_sku", "That is not on the Stall.", status=404)
    wanted = body.get("character_id")
    if wanted is not None and (isinstance(wanted, bool) or not isinstance(wanted, int)):
        return _v2_err("bad_request", "character_id must be an integer.")
    controller = None
    if row["lane"] in sn.GAME_LANES:
        try:
            controller = await _resolve_character(request, account_id, wanted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sietch order: character list failed acct=%s: %s", account_id, exc)
            return _v2_err("unavailable", "The desert is busy. Try again in a moment.", status=503)
        if controller is None:
            return _v2_err("no_character", "Pick the character who should receive it.", status=409)
    conn = get_db()
    try:
        pid = _profile_id(conn, session, account_id)
        if pid is None:
            return _v2_err("unlinked", "Link a game character to buy from the Stall.", status=409)
        order, err = sn.place_order(conn, pid, sku, body.get("idem_key"), rows,
                                    discord_id=discord_id, account_id=int(account_id),
                                    controller_id=controller)
    finally:
        conn.close()
    if err:
        return _v2_err(err, _ORDER_REFUSALS.get(err, "That order cannot be placed."),
                       status=400 if err == "bad_idem_key" else 409)
    if order["lane"] in sn.GAME_LANES and order["status"] == "queued" and order["attempts"] == 0:
        _deliver_in_background(order)
    conn = get_db()
    try:
        out = {"order": next((o for o in sn.orders_view(conn, pid, rows) if o["id"] == order["id"]), None),
               "balance": sn.balance(conn, pid)}
    finally:
        conn.close()
    resp = _v2_ok(out)
    resp.headers["Cache-Control"] = PRIVATE_CACHE
    return resp


_ORDER_REFUSALS = {
    "bad_idem_key": "Refresh the page and try again.",
    "closed": "That is not for sale right now.",
    "limit": "You already have as many of these as the Stall allows.",
    "sold_out": "The Stall has run out of that one.",
    "no_character": "Pick the character who should receive it.",
    "balance": "You need more LS Tokens for that.",
    "unknown_sku": "That is not on the Stall.",
}


@router.get("/portal/sietch/board")
async def portal_sietch_board():
    if not _earning():
        return JSONResponse({"ok": True, "enabled": False, "rows": []},
                            headers={"Cache-Control": PUBLIC_CACHE})
    anon = (_copy().get("board") or {}).get("anon_name") or "A wanderer"
    conn = get_db()
    try:
        rows = sn.board(conn, anon_name=anon)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "enabled": True, "rows": rows},
                        headers={"Cache-Control": PUBLIC_CACHE})


@router.get("/portal/sietch/tonight-public")
async def portal_sietch_tonight_public():
    """For the Discord poster: tonight's copy and public spot names. Never a seek
    row, never a riddle answer, never a coordinate."""
    night = sn.current_night()
    if not _earning() or night is None:
        return JSONResponse({"ok": True, "enabled": _earning(), "night": None,
                             "next_night_starts_at": sn.next_night_start()},
                            headers={"Cache-Control": PUBLIC_CACHE})
    conn = get_db()
    try:
        spots = sn.spots_for(conn, night["key"])
    finally:
        conn.close()
    clue = sn.current_clue(spots, set())
    out = dict(night, copy=_night_copy(_copy(), night["n"]),
               spots=[{"name": s["name"], "kind": s["kind"]} for s in sn.public_spots(spots, set())],
               first_riddle=clue["riddle"] if clue else None)
    return JSONResponse({"ok": True, "enabled": True, "night": out,
                         "next_night_starts_at": sn.next_night_start()},
                        headers={"Cache-Control": PUBLIC_CACHE})


# --- admin routes ------------------------------------------------------------


async def _admin_json(request):
    user = require_admin(request)
    require_csrf(request, user)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return user, body


def _resolve_profile(conn, body):
    pid = body.get("profile_id")
    if pid is not None:
        if not sn.valid_profile_id(pid):
            raise HTTPException(400, "profile_id must be 32 hex characters")
        if not sn.profile_accounts(conn, pid):
            raise HTTPException(404, "no active profile with that id")
        return pid
    aid = body.get("account_id")
    if isinstance(aid, bool) or not isinstance(aid, int) or aid <= 0:
        raise HTTPException(400, "send profile_id or a positive integer account_id")
    pid = sn.profile_for_account(conn, aid)
    if pid is None:
        raise HTTPException(409, "that account is not linked to a profile")
    return pid


@router.post("/v2/sietch/award")
async def v2_sietch_award(request: Request):
    """E7: {profile_id|account_id, amount, reason, case_id}. The case id makes the
    award idempotent: the same case twice is a replay, not a second award."""
    user, body = await _admin_json(request)
    amount = body.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= AWARD_MAX:
        raise HTTPException(400, "amount must be an integer 1 to %d" % AWARD_MAX)
    reason = (body.get("reason") or "").strip() if isinstance(body.get("reason"), str) else ""
    if not 3 <= len(reason) <= 200:
        raise HTTPException(400, "reason is required (3 to 200 chars)")
    case_id = body.get("case_id")
    if not isinstance(case_id, str) or not _CASE_RE.fullmatch(case_id):
        raise HTTPException(400, "case_id is required (1-64 of A-Z a-z 0-9 _ . : -)")
    conn = get_db()
    try:
        pid = _resolve_profile(conn, body)
        conn.execute("BEGIN IMMEDIATE")
        ledger_id, replay = sn.admin_award(conn, pid, amount, reason, case_id, user["username"])
        conn.commit()
        bal = sn.balance(conn, pid)
    finally:
        conn.close()
    audit_log(user["id"], user["username"], "sietch_award", pid, client_ip(request),
              details=json.dumps({"amount": amount, "case_id": case_id, "reason": reason,
                                  "ledger_id": ledger_id, "replay": replay}, separators=(",", ":")))
    return {"ok": True, "ledger_id": ledger_id, "replay": replay, "balance": bal}


@router.get("/v2/sietch/ledger")
async def v2_sietch_ledger(request: Request, profile_id: str = None, account_id: int = None):
    require_admin(request)
    conn = get_db()
    try:
        pid = _resolve_profile(conn, {"profile_id": profile_id} if profile_id else {"account_id": account_id})
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM portal_sn_ledger WHERE profile_id = ? ORDER BY id DESC LIMIT 500", (pid,))]
        accounts = sn.profile_accounts(conn, pid)
        marks = ",".join("?" * len(accounts)) or "NULL"
        earn = [dict(r) for r in conn.execute(
            "SELECT * FROM portal_sn_earn WHERE account_id IN (%s) ORDER BY id DESC LIMIT 500" % marks,
            accounts)]
        out = {"ok": True, "profile_id": pid, "accounts": accounts,
               "eligible": sn.profile_eligible(conn, pid, accounts),
               "balance": sn.balance(conn, pid), "earned_total": sn.earned_total(conn, pid),
               "ledger": rows, "evidence": earn}
    finally:
        conn.close()
    return JSONResponse(out, headers={"Cache-Control": PRIVATE_CACHE})


@router.get("/v2/sietch/spots")
async def v2_sietch_spots(request: Request, night: str = None):
    require_admin(request)
    conn = get_db()
    try:
        if night:
            rows = sn.spots_for(conn, night)
        else:
            rows = [dict(r) for r in conn.execute("SELECT * FROM portal_sn_spots ORDER BY night, kind, seq, id")]
    finally:
        conn.close()
    return JSONResponse({"ok": True, "spots": rows}, headers={"Cache-Control": PRIVATE_CACHE})


@router.put("/v2/sietch/spots")
async def v2_sietch_spots_put(request: Request):
    """{spots: [spot, ...], delete: [id, ...]}. Every spot is validated before
    anything is written, so one bad row refuses the whole request. The audit row
    names spot ids only; coordinates stay in admin.db."""
    user, body = await _admin_json(request)
    raw = body.get("spots") or []
    deletes = body.get("delete") or []
    if not isinstance(raw, list) or not isinstance(deletes, list) or len(raw) > 50 or len(deletes) > 50:
        raise HTTPException(400, "spots and delete must be lists of at most 50")
    spots = []
    for item in raw:
        spot, err = sn.validate_spot(item)
        if err:
            raise HTTPException(400, "spot %r: %s" % (item.get("id") if isinstance(item, dict) else None, err))
        spots.append(spot)
    for sid in deletes:
        if not isinstance(sid, str) or not sn._SPOT_ID_RE.fullmatch(sid):
            raise HTTPException(400, "delete ids must be spot ids")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for spot in spots:
            sn.upsert_spot(conn, spot)
        for sid in deletes:
            conn.execute("DELETE FROM portal_sn_spots WHERE id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()
    audit_log(user["id"], user["username"], "sietch_spots", None, client_ip(request),
              details=json.dumps({"upserted": [s["id"] for s in spots], "deleted": deletes},
                                 separators=(",", ":")))
    return {"ok": True, "upserted": len(spots), "deleted": len(deletes)}


_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
templates = Jinja2Templates(directory=_TEMPLATES)


@router.get("/admin/sietch-nights")
async def sietch_nights_page(request: Request):
    # Same shape as v2_amtal_ledger: the HTML page redirects, the JSON routes 401/403.
    try:
        user = get_current_user(request)
    except HTTPException:
        return RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return RedirectResponse(url="/admin/", status_code=302)
    if not os.path.exists(os.path.join(_TEMPLATES, "sietch_nights.html")):
        raise HTTPException(404, "the Seven Nights console is not deployed")
    return templates.TemplateResponse("sietch_nights.html", {"request": request})


@router.get("/v2/sietch/orders")
async def v2_sietch_orders(request: Request, status: str = None):
    require_admin(request)
    conn = get_db()
    try:
        if status:
            rows = conn.execute("SELECT * FROM portal_sn_orders WHERE status = ? ORDER BY id DESC LIMIT 500",
                                (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM portal_sn_orders ORDER BY id DESC LIMIT 500").fetchall()
        out = {"ok": True, "orders": [dict(r) for r in rows],
               "stale_reconciling": sn.stale_reconciling(conn)}
    finally:
        conn.close()
    return JSONResponse(out, headers={"Cache-Control": PRIVATE_CACHE})


@router.post("/v2/sietch/orders/{order_id}/redrive")
async def v2_sietch_redrive(request: Request, order_id: int):
    """Re-send the SAME delivery_key for a queued or reconciling game-lane order."""
    user, _body = await _admin_json(request)
    conn = get_db()
    try:
        order = sn.order_row(conn, order_id)
    finally:
        conn.close()
    if order is None or order["lane"] not in sn.GAME_LANES or order["status"] not in ("queued", "reconciling"):
        raise HTTPException(409, "only a queued or reconciling game-lane order can be re-driven")
    status = await deliver(order)
    if status is None:
        raise HTTPException(409, "the order is being delivered right now; try again in a minute")
    audit_log(user["id"], user["username"], "sietch_order_redrive", str(order_id), client_ip(request),
              details=json.dumps({"status": status}, separators=(",", ":")))
    return {"ok": True, "status": status}


@router.post("/v2/sietch/orders/{order_id}/refund")
async def v2_sietch_refund(request: Request, order_id: int):
    """Operator refund, with a reason. Refused for a delivered order, and, unless
    `confirm_not_delivered` is true, for any order whose grant may have landed
    (sn.refund_needs_confirm): the operator must have checked the game side first."""
    user, body = await _admin_json(request)
    reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
    if not 3 <= len(reason.strip()) <= 200:
        raise HTTPException(400, "reason is required (3 to 200 chars)")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = sn.order_row(conn, order_id)
        if order is None:
            conn.rollback()
            raise HTTPException(404, "no such order")
        if order["status"] in ("delivered", "refunded"):
            conn.rollback()
            raise HTTPException(409, "order is %s" % order["status"])
        if sn.refund_needs_confirm(order) and body.get("confirm_not_delivered") is not True:
            conn.rollback()
            raise HTTPException(409, "order is %s after %d attempts: a grant may have landed. Confirm it "
                                     "did not, then send confirm_not_delivered=true"
                                     % (order["status"], order["attempts"]))
        sn.refund(conn, order_id, "admin: " + reason.strip(), user["username"])
        conn.commit()
    finally:
        conn.close()
    audit_log(user["id"], user["username"], "sietch_order_refund", str(order_id), client_ip(request),
              details=json.dumps({"reason": reason.strip(), "was": order["status"]}, separators=(",", ":")))
    return {"ok": True, "status": "refunded"}


@router.get("/v2/sietch/review")
async def v2_sietch_review(request: Request):
    """Review-only flags, never acted on automatically: profiles with several
    linked accounts, and earning profiles that share a connection IP."""
    require_admin(request)
    conn = get_db()
    try:
        profiles = sn.linked_profiles(conn)
        multi = [{"profile_id": p, "accounts": a} for p, a in profiles.items() if len(a) > 1]
        shared = []
        if sn._has(conn, "connections"):
            cols = {r[1] for r in conn.execute("PRAGMA table_info(connections)")}
            if {"account_id", "ip"} <= cols:
                earners = {r[0] for r in conn.execute(
                    "SELECT DISTINCT profile_id FROM portal_sn_ledger WHERE kind = 'earn'")}
                by_ip = {}
                for aid, ip in conn.execute("SELECT DISTINCT account_id, ip FROM connections WHERE ip IS NOT NULL"):
                    pid = sn.profile_for_account(conn, aid)
                    if pid in earners:
                        by_ip.setdefault(ip, set()).add(pid)
                shared = [{"ip_group": i, "profiles": sorted(p)}
                          for i, p in enumerate(v for v in by_ip.values() if len(v) > 1)]
    finally:
        conn.close()
    return JSONResponse({"ok": True, "multi_account": multi, "shared_ip": shared},
                        headers={"Cache-Control": PRIVATE_CACHE})


# --- internal (Cielago role sync; localhost, its own key) -----------------------


def _require_key(provided):
    # A dedicated secret: holding the market-alert poll key must not let anyone
    # ack (and so refund) role orders. Read per call, like the other internal keys.
    import secrets
    expected = os.environ.get("LASTSIETCH_SIETCH_ROLE_KEY", "")
    if not expected:
        raise HTTPException(503, "Role delivery not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(401, "Invalid key")


@router.get("/_internal/sietch/role-queue")
async def sietch_role_queue(x_sietch_role_key: Optional[str] = Header(default=None)):
    """Queued role orders: {id, discord_id, role_key}. Only real Discord ids."""
    _require_key(x_sietch_role_key)
    rows_by_sku = catalog()
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, sku, discord_id FROM portal_sn_orders WHERE lane = 'role' "
                            "AND status IN ('queued', 'reconciling') ORDER BY id LIMIT 100").fetchall()
    finally:
        conn.close()
    out = [{"id": r["id"], "discord_id": r["discord_id"],
            "role_key": (rows_by_sku.get(r["sku"]) or {}).get("role_key")}
           for r in rows if sn.re.fullmatch(r"[1-9][0-9]{0,19}", r["discord_id"] or "")]
    return {"count": len(out), "orders": out}


@router.post("/_internal/sietch/role-queue/{order_id}/ack")
async def sietch_role_ack(order_id: int, request: Request,
                          x_sietch_role_key: Optional[str] = Header(default=None)):
    """{ok: true} after a 204 from Discord; {ok: false, error} after a definite 4xx.
    Anything uncertain (a 429, a 5xx) is not acked at all; the order stays queued."""
    _require_key(x_sietch_role_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
        raise HTTPException(400, "body must be {ok: bool, error?: str}")
    conn = get_db()
    try:
        status = sn.ack_role(conn, order_id, body["ok"], str(body.get("error") or "")[:64] or None)
    finally:
        conn.close()
    if status is None:
        raise HTTPException(404, "no such order")
    return {"ok": True, "status": status}


# --- internal (Cielago event contests; localhost, its own key) -----------------


def _require_award_key(provided):
    # Its own secret, off unless set: the role key must not mint tokens, and this
    # key can only pay contest:* case ids up to AWARD_MAX each.
    import secrets
    expected = os.environ.get("LASTSIETCH_SIETCH_AWARD_KEY", "")
    if not expected:
        raise HTTPException(503, "Contest awards not configured")
    # Bytes, not str: compare_digest raises TypeError on a non-ASCII str, which
    # would answer 500 to a bad header instead of 401.
    if not provided or not secrets.compare_digest(provided.encode("utf-8", "surrogateescape"),
                                                  expected.encode("utf-8", "surrogateescape")):
        raise HTTPException(401, "Invalid key")


@router.get("/_internal/sietch/contest-link/{discord_id}")
async def sietch_contest_link(discord_id: str, x_sietch_award_key: Optional[str] = Header(default=None)):
    """{linked: bool} so Cielago can tell an entrant to link before the payout."""
    _require_award_key(x_sietch_award_key)
    conn = get_db()
    try:
        linked = sn.profile_for_discord(conn, discord_id) is not None
    finally:
        conn.close()
    return {"linked": linked}


@router.post("/_internal/sietch/contest-award")
async def sietch_contest_award(request: Request, x_sietch_award_key: Optional[str] = Header(default=None)):
    """{discord_id, amount, reason, case_id} -> {ok, ledger_id, replay}. 409
    `unlinked` when the member has no portal profile; the case id is idempotent."""
    _require_award_key(x_sietch_award_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    discord_id = body.get("discord_id")
    if not isinstance(discord_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", discord_id):
        raise HTTPException(400, "discord_id must be a Discord id string")
    amount = body.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= AWARD_MAX:
        raise HTTPException(400, "amount must be an integer 1 to %d" % AWARD_MAX)
    reason = (body.get("reason") or "").strip() if isinstance(body.get("reason"), str) else ""
    if not 3 <= len(reason) <= 200:
        raise HTTPException(400, "reason is required (3 to 200 chars)")
    case_id = body.get("case_id")
    if not isinstance(case_id, str) or not sn.CONTEST_CASE_RE.fullmatch(case_id):
        raise HTTPException(400, "case_id must be contest:<id>:entry:<discord_id> or contest:<id>:place:<1-3>")
    allowed = sn.contest_ids(os.environ.get("LASTSIETCH_SIETCH_CONTEST_IDS", ""))
    if not allowed:
        raise HTTPException(503, "No contest ids enabled (LASTSIETCH_SIETCH_CONTEST_IDS)")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            status, ledger_id, replay = sn.contest_award(conn, discord_id, amount, reason, case_id,
                                                         allowed)
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(400, str(exc))
        conn.commit()
    finally:
        conn.close()
    if status != "ok":
        raise HTTPException(409, status)
    audit_log(None, sn.CONTEST_OPERATOR, "sietch_contest_award", discord_id, client_ip(request),
              details=json.dumps({"amount": amount, "case_id": case_id, "reason": reason,
                                  "ledger_id": ledger_id, "replay": replay}, separators=(",", ":")))
    return {"ok": True, "ledger_id": ledger_id, "replay": replay}
