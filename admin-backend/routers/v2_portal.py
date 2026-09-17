"""V2 admin panel API for the Discord-OAuth player portal (VC6).

Operator surface for the identity-quiz portal: view link activity, manually
link a Discord account to a game account (support override that bypasses the
quiz), clear a locked-out user's rate-limit strikes, and revoke a link.

All data lives in the local admin.db (ls_account_links + portal_link_attempts)
so this router talks to get_db() directly -- no relay. Every write is
admin-gated, CSRF-checked, and audited. Character-name resolution for a manual
link is a best-effort read of the progression snapshot (via routers.dune).
"""
import json
import re
import portal_identity

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, require_admin, require_csrf
from config import (
    PORTAL_RATE_ATTEMPTS_PER_DISCORD_PER_DAY,
    PORTAL_RATE_COOLDOWN_HOURS,
    PORTAL_RATE_FAILS_TO_COOLDOWN,
)
from database import get_db

router = APIRouter(prefix="/api/dune/v2/portal")

# Discord snowflakes are 17-20 digit ints; be permissive but bounded.
_DISCORD_ID_RE = re.compile(r"[0-9]{15,25}")
_HANDLE_RE = re.compile(r"[^\x20-\x7e]")  # strip non-printable-ASCII from handles
MAX_REASON = 300


class LinkRequest(BaseModel):
    discord_id: str
    account_id: int
    discord_handle: str | None = None
    character_name: str | None = None


class UnlockRequest(BaseModel):
    discord_id: str
    account_id: int | None = None


class RevokeRequest(BaseModel):
    discord_id: str
    account_id: int
    reason: str | None = None


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _valid_discord_id(discord_id: str) -> str:
    discord_id = (discord_id or "").strip()
    if not _DISCORD_ID_RE.fullmatch(discord_id):
        raise HTTPException(400, "discord_id must be a 15-25 digit Discord ID")
    return discord_id


def _valid_account_id(account_id: int) -> int:
    if not isinstance(account_id, int) or account_id <= 0:
        raise HTTPException(400, "account_id must be a positive integer")
    return account_id


def _clean_handle(handle: str | None) -> str:
    if not handle:
        return ""
    return _HANDLE_RE.sub("", handle).strip()[:100]


async def _resolve_character_name(account_id: int) -> str:
    """Best-effort char name from the progression snapshot (same source the
    quiz-pass path uses). Falls back to '(unknown)' so a link never fails on a
    snapshot hiccup; the operator can override via the character_name field."""
    try:
        from routers.dune import _cached_progression_snapshot_full
        snap = await _cached_progression_snapshot_full()
        for p in (snap.get("players") or []):
            if int(p.get("account_id") or 0) == account_id:
                return p.get("char_name") or p.get("name") or "(unknown)"
    except Exception:
        pass
    return "(unknown)"


def _lockout_state(conn, discord_id: str) -> dict:
    """Compute the rate-limit picture for a discord_id: countable attempts in
    the 24h window (drives the per-discord cap) plus per-account fail counts in
    the cooldown window (drives the 24h cooldown). 'countable' == is_test_run=0,
    matching every check in portal_rate_limit."""
    attempts_24h = conn.execute(
        """SELECT COUNT(*) AS c FROM portal_link_attempts
           WHERE discord_id = ? AND is_test_run = 0
             AND attempt_at >= datetime('now', '-24 hours')""",
        (discord_id,),
    ).fetchone()["c"]
    pairs = conn.execute(
        f"""SELECT account_id, COUNT(*) AS fails FROM portal_link_attempts
            WHERE discord_id = ? AND result = 'fail' AND is_test_run = 0
              AND account_id IS NOT NULL
              AND attempt_at >= datetime('now', '-{PORTAL_RATE_COOLDOWN_HOURS} hours')
            GROUP BY account_id HAVING fails > 0
            ORDER BY fails DESC""",
        (discord_id,),
    ).fetchall()
    cooldowns = [
        {"account_id": r["account_id"], "fails_24h": r["fails"],
         "cooldown_active": r["fails"] >= PORTAL_RATE_FAILS_TO_COOLDOWN}
        for r in pairs
    ]
    return {
        "discord_attempts_24h": attempts_24h,
        "discord_cap": PORTAL_RATE_ATTEMPTS_PER_DISCORD_PER_DAY,
        "discord_capped": attempts_24h >= PORTAL_RATE_ATTEMPTS_PER_DISCORD_PER_DAY,
        "cooldowns": cooldowns,
        "locked": attempts_24h >= PORTAL_RATE_ATTEMPTS_PER_DISCORD_PER_DAY
                  or any(c["cooldown_active"] for c in cooldowns),
    }


def _links_for(conn, discord_id: str) -> list:
    rows = conn.execute(
        """SELECT discord_id, account_id, character_name, discord_handle,
                  datetime(linked_at) AS linked_at,
                  datetime(last_session_at) AS last_session_at,
                  datetime(revoked_at) AS revoked_at, revoke_reason
             FROM ls_account_links WHERE discord_id = ?
            ORDER BY revoked_at IS NOT NULL, linked_at DESC""",
        (discord_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/player-search")
async def portal_player_search(request: Request, q: str = ""):
    """Search players by in-game character name for the manual-link picker.
    Reuses the offline-inclusive grant player list (admin-gated, ~60s cached),
    so operators link by name instead of guessing a DB account_id. Returns the
    account_id + funcom handle so the right character is unambiguous."""
    require_admin(request)
    q = "".join(ch for ch in (q or "") if ch.isprintable())[:64].strip()
    if len(q) < 2:
        return {"available": True, "players": [], "too_short": True}
    try:
        from routers.dune_grant import grant_players
        payload = await grant_players(request)
    except HTTPException:
        raise
    except Exception:
        return {"available": False, "players": []}
    needle = q.lower()
    rows = [p for p in (payload.get("players") or [])
            if needle in (p.get("name") or "").lower()]
    rows.sort(key=lambda p: (0 if (p.get("online_status") or "").lower() == "online" else 1,
                             (p.get("name") or "").lower()))
    players = [{
        "account_id": p.get("account_id"),
        "name": p.get("name"),
        "funcom_id": p.get("funcom_id"),
        "online_status": p.get("online_status"),
        "last_login": p.get("last_login_time"),
    } for p in rows[:25]]
    return {"available": bool(payload.get("available", True)),
            "count": len(players), "players": players}


@router.get("/links")
async def portal_links(request: Request, q: str = ""):
    """Recent account links, or a search by discord_id / account_id / character.
    Admin-only, read-only."""
    require_admin(request)
    q = (q or "").strip()
    conn = get_db()
    try:
        if not q:
            rows = conn.execute(
                """SELECT discord_id, account_id, character_name, discord_handle,
                          datetime(linked_at) AS linked_at,
                          datetime(last_session_at) AS last_session_at,
                          datetime(revoked_at) AS revoked_at, revoke_reason
                     FROM ls_account_links
                    ORDER BY revoked_at IS NOT NULL, linked_at DESC LIMIT 100"""
            ).fetchall()
        else:
            like = f"%{q}%"
            rows = conn.execute(
                """SELECT discord_id, account_id, character_name, discord_handle,
                          datetime(linked_at) AS linked_at,
                          datetime(last_session_at) AS last_session_at,
                          datetime(revoked_at) AS revoked_at, revoke_reason
                     FROM ls_account_links
                    WHERE discord_id = ? OR CAST(account_id AS TEXT) = ?
                          OR character_name LIKE ? OR discord_handle LIKE ?
                    ORDER BY revoked_at IS NOT NULL, linked_at DESC LIMIT 100""",
                (q, q, like, like),
            ).fetchall()
    finally:
        conn.close()
    return {"available": True, "count": len(rows), "links": [dict(r) for r in rows]}


@router.get("/lookup")
async def portal_lookup(request: Request, discord_id: str = ""):
    """Full picture for one Discord ID: its link(s), lockout/rate-limit state,
    and recent attempt history. Drives the manual-link + unlock cards."""
    require_admin(request)
    discord_id = _valid_discord_id(discord_id)
    conn = get_db()
    try:
        links = _links_for(conn, discord_id)
        lockout = _lockout_state(conn, discord_id)
        attempts = conn.execute(
            """SELECT id, account_id, result, is_test_run,
                      datetime(attempt_at) AS attempt_at,
                      datetime(callback_hit_at) AS callback_hit_at,
                      q1_kind, q2_kind, q3_kind
                 FROM portal_link_attempts
                WHERE discord_id = ?
                ORDER BY COALESCE(attempt_at, callback_hit_at) DESC LIMIT 15""",
            (discord_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "available": True,
        "discord_id": discord_id,
        "links": links,
        "lockout": lockout,
        "recent_attempts": [dict(r) for r in attempts],
    }


@router.post("/link")
async def portal_link_create(request: Request, body: LinkRequest):
    """Manually link a Discord account to a game account (support override that
    bypasses the identity quiz). Mirrors the quiz-pass UPSERT exactly, so an
    existing/revoked row for the pair is re-activated. Admin + CSRF + audited."""
    user = require_admin(request)
    require_csrf(request, user)
    discord_id = _valid_discord_id(body.discord_id)
    account_id = _valid_account_id(body.account_id)
    handle = _clean_handle(body.discord_handle)
    character_name = _clean_handle(body.character_name) if body.character_name else ""
    if not character_name:
        character_name = await _resolve_character_name(account_id)

    success = False
    try:
        conn = get_db()
        try:
            profile_change = portal_identity.begin_legacy_links(conn, discord_id, allow_grant=True)
            conn.execute(
                """INSERT INTO ls_account_links
                     (discord_id, account_id, character_name, discord_handle, linked_at)
                   VALUES (?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(discord_id, account_id) DO UPDATE SET
                     character_name = excluded.character_name,
                     discord_handle = excluded.discord_handle,
                     linked_at = datetime('now'),
                     revoked_at = NULL, revoked_by = NULL, revoke_reason = NULL""",
                (discord_id, account_id, character_name, handle or "(operator-linked)"),
            )
            portal_identity.finish_legacy_links(conn, profile_change)
            conn.commit()
            links = _links_for(conn, discord_id)
        finally:
            conn.close()
        success = True
        return {"status": "ok", "discord_id": discord_id, "account_id": account_id,
                "character_name": character_name, "links": links}
    finally:
        audit_log(
            user["id"], user["username"], "portal_manual_link",
            f"{discord_id}->{account_id}", _ip(request),
            details=json.dumps({"character_name": character_name, "handle": handle}),
            success=success,
        )


@router.post("/unlock")
async def portal_unlock(request: Request, body: UnlockRequest):
    """Clear a user's rate-limit strikes so they can retry. Flips is_test_run=1
    on their countable attempt rows in the last 24h (every rate-limit check
    excludes is_test_run=1), clearing both the cooldown and the per-day cap.
    Scoped to one account if account_id is given, else all of the discord's
    recent attempts. Admin + CSRF + audited."""
    user = require_admin(request)
    require_csrf(request, user)
    discord_id = _valid_discord_id(body.discord_id)
    account_id = _valid_account_id(body.account_id) if body.account_id else None

    success = False
    cleared = 0
    try:
        conn = get_db()
        try:
            if account_id is not None:
                cur = conn.execute(
                    """UPDATE portal_link_attempts SET is_test_run = 1
                        WHERE discord_id = ? AND account_id = ? AND is_test_run = 0
                          AND (attempt_at >= datetime('now', '-24 hours')
                               OR callback_hit_at >= datetime('now', '-24 hours'))""",
                    (discord_id, account_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE portal_link_attempts SET is_test_run = 1
                        WHERE discord_id = ? AND is_test_run = 0
                          AND (attempt_at >= datetime('now', '-24 hours')
                               OR callback_hit_at >= datetime('now', '-24 hours'))""",
                    (discord_id,),
                )
            cleared = cur.rowcount
            conn.commit()
            lockout = _lockout_state(conn, discord_id)
        finally:
            conn.close()
        success = True
        return {"status": "ok", "discord_id": discord_id, "cleared": cleared,
                "lockout": lockout}
    finally:
        audit_log(
            user["id"], user["username"], "portal_unlock",
            discord_id if account_id is None else f"{discord_id}->{account_id}",
            _ip(request), details=json.dumps({"cleared": cleared}), success=success,
        )


@router.post("/revoke")
async def portal_revoke(request: Request, body: RevokeRequest):
    """Revoke a link (sets revoked_at/by/reason). The player loses portal access
    for that account and must re-link. Admin + CSRF + audited."""
    user = require_admin(request)
    require_csrf(request, user)
    discord_id = _valid_discord_id(body.discord_id)
    account_id = _valid_account_id(body.account_id)
    reason = (body.reason or "").strip()[:MAX_REASON] or "operator-revoked"

    success = False
    try:
        conn = get_db()
        try:
            profile_change = portal_identity.begin_legacy_links(conn, discord_id, allow_grant=False)
            cur = conn.execute(
                """UPDATE ls_account_links
                      SET revoked_at = datetime('now'), revoked_by = ?,
                          revoke_reason = ?
                    WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL""",
                (user["username"], reason, discord_id, account_id),
            )
            portal_identity.finish_legacy_links(conn, profile_change)
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(404, "no active link for that discord_id + account_id")
            links = _links_for(conn, discord_id)
        finally:
            conn.close()
        success = True
        return {"status": "ok", "discord_id": discord_id, "account_id": account_id,
                "links": links}
    finally:
        audit_log(
            user["id"], user["username"], "portal_revoke_link",
            f"{discord_id}->{account_id}", _ip(request),
            details=json.dumps({"reason": reason}), success=success,
        )
