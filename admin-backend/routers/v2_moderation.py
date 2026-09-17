import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import blueprint_market
import player_profile
from auth import audit_log, require_admin, require_csrf
from database import get_db
from relay import call_relay

router = APIRouter(prefix="/api/dune/v2")

# Typed-CONFIRM tokens the UI must echo before a moderation action fires.
CONFIRM_KICK = "KICK"
CONFIRM_BAN = "BAN"
CONFIRM_UNBAN = "UNBAN"
# Content takedowns (wave 2 lever C). HIDE and SHOW are the reversible pair;
# REMOVE is separate because it deletes the blueprint blob and cannot be undone.
# REPUBLISH is the blueprint half of SHOW (wave 6): SHOW reopens a Signal Board
# card, and a blueprint listing needed a token of its own so a typed SHOW could
# never arm the wrong surface.
CONFIRM_HIDE = "HIDE"
CONFIRM_SHOW = "SHOW"
CONFIRM_REMOVE = "REMOVE"
CONFIRM_REPUBLISH = "REPUBLISH"

MAX_REASON = 500
MAX_NOTE = 1000
MAX_DURATION_MIN = 60 * 24 * 365  # 1 year ceiling; absent => permanent

# 30s cache on the bans list (re-kick watcher writes every 30s; UI polling cost
# does not need to outrun the source-of-truth tick).
BANS_CACHE_TTL = 15
_bans_cache: dict = {"data": None, "fetched_at": 0.0}
_bans_history_cache: dict = {"data": None, "fetched_at": 0.0}


class KickRequest(BaseModel):
    reason: str
    confirm_token: str


class BanRequest(BaseModel):
    reason: str
    note: str | None = None
    duration_minutes: int | None = None
    confirm_token: str


class UnbanRequest(BaseModel):
    unban_reason: str
    confirm_token: str


def _validate_account_id(account_id: str) -> int:
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    return int(account_id)


def _redact_reason(text: str | None) -> str:
    """Audit-safe reason: trim to 200 chars, no PII assumed but capped."""
    if not text:
        return ""
    text = text.strip()
    return text[:200]


async def _resolve_fls_id(request: Request, account_id: int) -> str | None:
    """Best-effort account_id -> fls_id resolution for the ban payload. Tries
    the offline-inclusive grant picker first (carries funcom_id for every
    account); falls back to None if absent. The on-disk ban script also
    resolves fls_id from dune.accounts as defence in depth."""
    try:
        payload = await call_relay("/dune/grant/players", timeout=30)
    except Exception:
        return None
    target = str(account_id)
    for p in (payload.get("players") or []):
        if str(p.get("account_id", "")) == target:
            fls = p.get("funcom_id") or p.get("fls_id")
            if isinstance(fls, str) and fls.strip():
                return fls.strip()
            return None
    return None


@router.post("/player/{account_id}/_kick")
async def v2_player_kick(request: Request, account_id: str, body: KickRequest):
    """Kick one player. Admin + CSRF + typed-CONFIRM gated; audit on every path.
    The relay's POST /dune/player/{aid}/kick performs the actual publish; this
    handler only validates, audits, and forwards."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    acct = _validate_account_id(account_id)

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_kick", str(acct), ip,
            details=json.dumps({
                "reason": _redact_reason(body.reason),
                "confirm_token": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        if body.confirm_token != CONFIRM_KICK:
            raise HTTPException(400, f"confirm_token must be '{CONFIRM_KICK}'")
        if not isinstance(body.reason, str) or not body.reason.strip():
            raise HTTPException(400, "reason is required")
        if len(body.reason) > MAX_REASON:
            raise HTTPException(400, f"reason exceeds {MAX_REASON} chars")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    try:
        result = await call_relay(
            f"/dune/player/{acct}/kick", "POST", {}, timeout=45,
        )
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"success": False, "detail": str(exc.detail)[:500]}
    except Exception as exc:
        _audit(False, {"result": f"error: {exc}"})
        return {"success": False, "detail": str(exc)[:500]}

    success = bool(result.get("success", False))
    _audit(success, {
        "result": result.get("status") or ("ok" if success else "failed"),
        "fls_id": result.get("fls_id"),
        "mode": result.get("mode"),
        "message": (result.get("message") or "")[:500],
    })
    return result


@router.post("/player/{account_id}/_ban")
async def v2_player_ban(request: Request, account_id: str, body: BanRequest):
    """Ban one player. Admin + CSRF + typed-CONFIRM gated. Inserts/activates a
    holadmin.bans row keyed by fls_id, then immediately kicks. duration_minutes
    null = permanent. Server resolves fls_id from account_id; the relay
    script re-resolves as defence in depth."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    acct = _validate_account_id(account_id)

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_ban", str(acct), ip,
            details=json.dumps({
                "reason": _redact_reason(body.reason),
                "note_len": len(body.note or ""),
                "duration_minutes": body.duration_minutes,
                "confirm_token": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        if body.confirm_token != CONFIRM_BAN:
            raise HTTPException(400, f"confirm_token must be '{CONFIRM_BAN}'")
        if not isinstance(body.reason, str) or not body.reason.strip():
            raise HTTPException(400, "reason is required")
        if len(body.reason) > MAX_REASON:
            raise HTTPException(400, f"reason exceeds {MAX_REASON} chars")
        if body.note is not None:
            if not isinstance(body.note, str):
                raise HTTPException(400, "note must be a string")
            if len(body.note) > MAX_NOTE:
                raise HTTPException(400, f"note exceeds {MAX_NOTE} chars")
        if body.duration_minutes is not None:
            if isinstance(body.duration_minutes, bool) or not isinstance(body.duration_minutes, int):
                raise HTTPException(400, "duration_minutes must be an integer or null")
            if body.duration_minutes < 1 or body.duration_minutes > MAX_DURATION_MIN:
                raise HTTPException(400, f"duration_minutes must be 1..{MAX_DURATION_MIN}")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    fls_id = await _resolve_fls_id(request, acct)
    idempotency_key = str(uuid.uuid4())

    # dune-grant.sh's do_grant() / build_ban_grant() reads a wrapped envelope:
    # top-level {grant_type, account_id, operator, idempotency_key, detail:{...}}.
    # The relay base64-encodes the JSON and the on-disk script jq_get_nested()
    # the detail.* fields. fls_id may be null here; build_ban_grant overrides
    # from dune.accounts as defence in depth.
    payload = {
        "grant_type": "ban",
        "account_id": acct,
        "operator": user["username"],
        "idempotency_key": idempotency_key,
        "detail": {
            "fls_id": fls_id,
            "reason": body.reason.strip(),
            "note": body.note or None,
            "duration_minutes": body.duration_minutes,
            "banned_by": user["username"],
        },
    }

    try:
        result = await call_relay(
            f"/dune/player/{acct}/ban", "POST", payload, timeout=60,
        )
    except HTTPException as exc:
        _audit(False, {
            "result": f"relay_error: {exc.detail}",
            "idempotency_key": idempotency_key,
            "fls_id_resolved": fls_id,
        })
        return {"success": False, "detail": str(exc.detail)[:500]}
    except Exception as exc:
        _audit(False, {
            "result": f"error: {exc}",
            "idempotency_key": idempotency_key,
            "fls_id_resolved": fls_id,
        })
        return {"success": False, "detail": str(exc)[:500]}

    # Invalidate the bans cache so the next GET reflects the new row.
    _bans_cache["data"] = None
    _bans_history_cache["data"] = None

    success = bool(result.get("success", False))
    _audit(success, {
        "result": result.get("status") or ("ok" if success else "failed"),
        "idempotency_key": idempotency_key,
        "fls_id_resolved": fls_id,
        "ban_id": result.get("ban_id"),
        "message": (result.get("message") or "")[:500],
    })
    return result


@router.post("/player/{account_id}/_unban")
async def v2_player_unban(request: Request, account_id: str, body: UnbanRequest):
    """Unban one player. Sets holadmin.bans.active=false for the matching
    row; the re-kick watcher stops next tick. Idempotent on the server side."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    acct = _validate_account_id(account_id)

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_unban", str(acct), ip,
            details=json.dumps({
                "unban_reason": _redact_reason(body.unban_reason),
                "confirm_token": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        if body.confirm_token != CONFIRM_UNBAN:
            raise HTTPException(400, f"confirm_token must be '{CONFIRM_UNBAN}'")
        if not isinstance(body.unban_reason, str) or not body.unban_reason.strip():
            raise HTTPException(400, "unban_reason is required")
        if len(body.unban_reason) > MAX_REASON:
            raise HTTPException(400, f"unban_reason exceeds {MAX_REASON} chars")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    fls_id = await _resolve_fls_id(request, acct)
    idempotency_key = str(uuid.uuid4())

    # Same wrapped envelope as ban; build_unban_grant() reads detail.* via
    # jq_get_nested. See ban handler above.
    payload = {
        "grant_type": "unban",
        "account_id": acct,
        "operator": user["username"],
        "idempotency_key": idempotency_key,
        "detail": {
            "fls_id": fls_id,
            "unban_reason": body.unban_reason.strip(),
            "unbanned_by": user["username"],
        },
    }

    try:
        result = await call_relay(
            f"/dune/player/{acct}/unban", "POST", payload, timeout=45,
        )
    except HTTPException as exc:
        _audit(False, {
            "result": f"relay_error: {exc.detail}",
            "idempotency_key": idempotency_key,
        })
        return {"success": False, "detail": str(exc.detail)[:500]}
    except Exception as exc:
        _audit(False, {
            "result": f"error: {exc}",
            "idempotency_key": idempotency_key,
        })
        return {"success": False, "detail": str(exc)[:500]}

    _bans_cache["data"] = None
    _bans_history_cache["data"] = None

    success = bool(result.get("success", False))
    _audit(success, {
        "result": result.get("status") or ("ok" if success else "failed"),
        "idempotency_key": idempotency_key,
        "message": (result.get("message") or "")[:500],
    })
    return result


@router.get("/bans")
async def v2_bans(request: Request, history: int = 0):
    """List bans. history=0 (default) returns active non-expired rows;
    history=1 returns the full ledger (active + expired + unbanned). Admin-only
    (no CSRF; read-only). 15s in-memory cache."""
    require_admin(request)
    cache = _bans_history_cache if history else _bans_cache
    now = time.monotonic()
    cached = cache["data"]
    if cached is not None and now - cache["fetched_at"] < BANS_CACHE_TTL:
        return cached
    path = "/dune/bans/history" if history else "/dune/bans"
    try:
        raw = await call_relay(path, timeout=30)
    except Exception as exc:
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "bans": [], "error": str(exc)[:300]}
    # dune-bans.py emits {"bans": [...]} for active and {"actions": [...]} for
    # history. Pick the right key so the history payload isn't silently empty.
    key = "actions" if history else "bans"
    items = raw.get(key) or []
    data = {
        "available": bool(raw.get("available", True)),
        "bans": items,
    }
    cache["data"] = data
    cache["fetched_at"] = now
    return data


# --------------------------------------------------------------------------- #
# Content takedowns (wave 2 lever C)
#
# Moderation is what the other admins are for, so these are require_admin, not
# require_owner. Every one of them is a portal-side admin.db write through
# database.get_db(); nothing here reaches the game database or the relay.
#
# NOTHING IS DELETED except a blueprint blob under an explicit REMOVE. A hidden
# blueprint keeps its blob, a closed recruiting card keeps its text, an expired
# seeker card keeps its row, and an unlisted profile keeps its blurb. That is
# what turns the audit row's `previous` block into an undo rather than a note.
# --------------------------------------------------------------------------- #

def _now_utc() -> str:
    """admin.db timestamp format: the portal tables all default to
    datetime('now'), which is UTC with no zone suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


TAKEDOWN_DEBOUNCE_S = 10
_last_takedown: dict = {}


def _takedown_debounce(user: dict) -> None:
    """Per-admin debounce, the same shape as v2_world._pin_debounce and the flag
    board: a double-fired fetch or a leaned-on button must not write a second
    audit row whose `previous` block describes the state the first press made."""
    key = (user["id"], "takedown")
    last = _last_takedown.get(key)
    now = time.monotonic()
    if last is not None and now - last < TAKEDOWN_DEBOUNCE_S:
        wait = int(TAKEDOWN_DEBOUNCE_S - (now - last)) + 1
        raise HTTPException(429, f"slow down: wait {wait}s before another takedown")
    _last_takedown[key] = now


def _check_takedown(reason: str, confirm: str, expected: str) -> None:
    """Shared gate: the typed token has to match the direction being asked for,
    and a reason is required. The reason is the whole value of the log six weeks
    later, which is why it is neither optional nor a checkbox."""
    if confirm != expected:
        raise HTTPException(400, f'confirm must be "{expected}"')
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(400, "reason is required")
    if len(reason) > MAX_REASON:
        raise HTTPException(400, f"reason exceeds {MAX_REASON} chars")


class BlueprintTakedownRequest(BaseModel):
    reason: str
    confirm: str


class SignalGuildRequest(BaseModel):
    recruiting: bool
    reason: str
    confirm: str


class SignalSeekerRequest(BaseModel):
    char_name: str
    active: bool
    reason: str
    confirm: str


class DirectoryListedRequest(BaseModel):
    char_name: str
    listed: bool
    reason: str
    confirm: str


@router.post("/moderation/blueprint/{publish_id}/_unpublish")
def v2_takedown_blueprint_hide(request: Request, publish_id: int,
                               body: BlueprintTakedownRequest):
    """Hide one blueprint listing. status='unpublished', the blob is kept.

    This is the DEFAULT takedown and the reversible one, but the author is NOT
    what reverses it: create_or_update matches 'published' rows only, so an
    author who publishes the same base again opens a SECOND listing with a fresh
    download counter and leaves this one orphaned. _republish below is the only
    un-hide there is."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "takedown_blueprint", str(publish_id), ip,
            details=json.dumps({
                "mode": "hide",
                "reason": _redact_reason(body.reason),
                "confirm": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        _check_takedown(body.reason, body.confirm, CONFIRM_HIDE)
        _takedown_debounce(user)
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    previous = blueprint_market.admin_state(publish_id)
    if previous is None:
        _audit(False, {"result": "not_found"})
        raise HTTPException(404, "no such listing")

    try:
        changed = blueprint_market.admin_unpublish(publish_id)
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"write_error: {exc}", "previous": previous})
        raise HTTPException(500, f"the listing could not be hidden: {str(exc)[:300]}")

    # Re-read the row rather than echo the request: only the row proves what the
    # write did, and `previous` is what an undo needs.
    after = blueprint_market.admin_state(publish_id)
    _audit(True, {"result": "hidden" if changed else "already_hidden",
                  "previous": previous, "after": after})
    return {"success": True, "changed": changed, "listing": after}


@router.post("/moderation/blueprint/{publish_id}/_republish")
def v2_takedown_blueprint_republish(request: Request, publish_id: int,
                                    body: BlueprintTakedownRequest):
    """Put one hidden blueprint listing back on the gallery.

    The inverse of HIDE, and the only one: nothing on the player side can undo a
    hide. A 'removed' row is refused rather than flipped, because REMOVE deleted
    the blueprint file and a re-published card with no file behind it is worse
    than a hidden one."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "takedown_blueprint", str(publish_id), ip,
            details=json.dumps({
                "mode": "republish",
                "reason": _redact_reason(body.reason),
                "confirm": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        _check_takedown(body.reason, body.confirm, CONFIRM_REPUBLISH)
        _takedown_debounce(user)
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    previous = blueprint_market.admin_state(publish_id)
    if previous is None:
        _audit(False, {"result": "not_found"})
        raise HTTPException(404, "no such listing")
    if previous.get("status") == "removed":
        _audit(False, {"result": "removed_is_permanent", "previous": previous})
        raise HTTPException(409, "that listing was removed; its blueprint file is gone")

    try:
        changed = blueprint_market.admin_republish(publish_id)
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"write_error: {exc}", "previous": previous})
        raise HTTPException(500, f"the listing could not be re-published: {str(exc)[:300]}")

    # Re-read the row rather than echo the request: only the row proves what the
    # write did, and `previous` is what an undo needs.
    after = blueprint_market.admin_state(publish_id)
    _audit(True, {"result": "republished" if changed else "already_published",
                  "previous": previous, "after": after})
    return {"success": True, "changed": changed, "listing": after}


@router.post("/moderation/blueprint/{publish_id}/_remove")
def v2_takedown_blueprint_remove(request: Request, publish_id: int,
                                 body: BlueprintTakedownRequest):
    """Remove one blueprint listing: status='removed' AND the blob is deleted.

    🔴 The only irreversible lever on this surface, which is why it carries its
    own typed token and its own route. Hiding is the default; this exists for
    content that must not stay on disk."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "takedown_blueprint", str(publish_id), ip,
            details=json.dumps({
                "mode": "remove",
                "reason": _redact_reason(body.reason),
                "confirm": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        _check_takedown(body.reason, body.confirm, CONFIRM_REMOVE)
        _takedown_debounce(user)
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    previous = blueprint_market.admin_state(publish_id)
    if previous is None:
        _audit(False, {"result": "not_found"})
        raise HTTPException(404, "no such listing")

    try:
        changed = blueprint_market.admin_remove(publish_id)
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"write_error: {exc}", "previous": previous})
        raise HTTPException(500, f"the listing could not be removed: {str(exc)[:300]}")

    after = blueprint_market.admin_state(publish_id)
    _audit(True, {"result": "removed" if changed else "no_change",
                  "previous": previous, "after": after,
                  "blob_deleted": bool((previous or {}).get("blob_path"))})
    return {"success": True, "changed": changed, "listing": after,
            "irreversible": True}


@router.post("/moderation/signal/guild/{guild_id}")
def v2_takedown_signal_guild(request: Request, guild_id: int, body: SignalGuildRequest):
    """Close or reopen one guild's Signal Board recruiting card.

    Writes `recruiting` and `updated_at` only. NO DELETE: the blurb, the contact
    note and the filters stay exactly as the guild wrote them, so reopening
    restores the card rather than an empty one."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    expected = CONFIRM_SHOW if body.recruiting else CONFIRM_HIDE

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "takedown_signal", f"guild:{guild_id}", ip,
            details=json.dumps({
                "kind": "guild_recruiting",
                "target_recruiting": bool(body.recruiting),
                "reason": _redact_reason(body.reason),
                "confirm": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        _check_takedown(body.reason, body.confirm, expected)
        _takedown_debounce(user)
        if guild_id <= 0:
            raise HTTPException(400, "guild_id must be a positive integer")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT guild_id, recruiting, set_by_char_name, updated_at "
            "  FROM portal_guild_recruiting WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is None:
            _audit(False, {"result": "not_found"})
            raise HTTPException(404, "this guild has no recruiting card")
        previous = {"recruiting": bool(row["recruiting"]),
                    "set_by_char_name": row["set_by_char_name"],
                    "updated_at": row["updated_at"]}
        conn.execute(
            "UPDATE portal_guild_recruiting SET recruiting = ?, updated_at = ? "
            "WHERE guild_id = ?",
            (1 if body.recruiting else 0, _now_utc(), guild_id),
        )
        conn.commit()
        after = conn.execute(
            "SELECT guild_id, recruiting, updated_at FROM portal_guild_recruiting "
            "WHERE guild_id = ?", (guild_id,)).fetchone()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"write_error: {exc}"})
        raise HTTPException(500, f"the recruiting card could not be updated: {str(exc)[:300]}")
    finally:
        conn.close()

    state = {"guild_id": after["guild_id"], "recruiting": bool(after["recruiting"]),
             "updated_at": after["updated_at"]}
    _audit(True, {"result": "reopened" if body.recruiting else "closed",
                  "previous": previous, "after": state})
    return {"success": True, "card": state}


@router.post("/moderation/signal/seeker")
def v2_takedown_signal_seeker(request: Request, body: SignalSeekerRequest):
    """Expire or restore one looking-for-guild seeker card.

    Expiring stamps `expires_at` with now, which is exactly how a card ages out
    on its own; restoring clears it. NO DELETE, so the player's note and filters
    survive in both directions.

    Keyed on char_name because that is the only handle the Social board shows.
    Two players under one name is refused, never guessed at."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    expected = CONFIRM_SHOW if body.active else CONFIRM_HIDE
    name = (body.char_name or "").strip()

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "takedown_signal", f"seeker:{name}", ip,
            details=json.dumps({
                "kind": "lfg_seeker",
                "char_name": name[:80],
                "target_active": bool(body.active),
                "reason": _redact_reason(body.reason),
                "confirm": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        _check_takedown(body.reason, body.confirm, expected)
        _takedown_debounce(user)
        if not name:
            raise HTTPException(400, "char_name is required")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT account_id, char_name, expires_at, updated_at "
            "  FROM portal_lfg_seekers WHERE char_name = ? COLLATE NOCASE "
            " ORDER BY account_id",
            (name,),
        ).fetchall()
        if not rows:
            _audit(False, {"result": "not_found"})
            raise HTTPException(404, "no seeker card under that character name")
        if len(rows) > 1:
            _audit(False, {"result": "ambiguous", "matches": len(rows)})
            raise HTTPException(409, f"{len(rows)} seeker cards share that name; "
                                     "resolve it from the seeker wall")
        row = rows[0]
        previous = {"account_id": row["account_id"], "expires_at": row["expires_at"],
                    "updated_at": row["updated_at"]}
        expires = None if body.active else _now_utc()
        conn.execute(
            "UPDATE portal_lfg_seekers SET expires_at = ?, updated_at = ? "
            "WHERE account_id = ?",
            (expires, _now_utc(), row["account_id"]),
        )
        conn.commit()
        after = conn.execute(
            "SELECT char_name, expires_at, updated_at FROM portal_lfg_seekers "
            "WHERE account_id = ?", (row["account_id"],)).fetchone()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"write_error: {exc}"})
        raise HTTPException(500, f"the seeker card could not be updated: {str(exc)[:300]}")
    finally:
        conn.close()

    state = {"char_name": after["char_name"], "expires_at": after["expires_at"],
             "updated_at": after["updated_at"],
             "active": after["expires_at"] is None}
    _audit(True, {"result": "restored" if body.active else "expired",
                  "previous": previous, "after": state})
    return {"success": True, "card": state}


@router.post("/moderation/directory")
def v2_takedown_directory(request: Request, body: DirectoryListedRequest):
    """Hide or restore one player in the public directory.

    Goes through player_profile.admin_set_listed, which writes `listed` and
    `updated_at` and nothing else. set_profile would blank the blurb, and a
    takedown that destroys what it hid cannot be undone."""
    user = require_admin(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    expected = CONFIRM_SHOW if body.listed else CONFIRM_HIDE
    name = (body.char_name or "").strip()

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "takedown_directory", f"profile:{name}", ip,
            details=json.dumps({
                "char_name": name[:80],
                "target_listed": bool(body.listed),
                "reason": _redact_reason(body.reason),
                "confirm": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        _check_takedown(body.reason, body.confirm, expected)
        _takedown_debounce(user)
        if not name:
            raise HTTPException(400, "char_name is required")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    matches = player_profile.find_by_char_name(name)
    if not matches:
        _audit(False, {"result": "not_found"})
        raise HTTPException(404, "no directory profile under that character name")
    if len(matches) > 1:
        _audit(False, {"result": "ambiguous", "matches": len(matches)})
        raise HTTPException(409, f"{len(matches)} profiles share that name; "
                                 "resolve it from the profiles card")
    target = matches[0]
    previous = {"account_id": target["account_id"], "listed": target["listed"],
                "blurb_len": len(target["blurb"] or ""),
                "updated_at": target["updated_at"]}

    try:
        after = player_profile.admin_set_listed(target["account_id"], body.listed)
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"write_error: {exc}", "previous": previous})
        raise HTTPException(500, f"the profile could not be updated: {str(exc)[:300]}")

    _audit(True, {"result": "restored" if body.listed else "hidden",
                  "previous": previous,
                  "after": {"listed": after["listed"],
                            "blurb_len": len(after["blurb"] or "")}})
    return {"success": True,
            "profile": {"char_name": after["char_name"], "listed": after["listed"],
                        "blurb_len": len(after["blurb"] or "")}}
