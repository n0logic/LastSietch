"""Portal link/picker/quiz router (P4).

Routes (all under /portal/, link_flow-cookie required):
- GET  /portal/link              — render picker (optionally with ?q= results)
- POST /portal/link               — POST search; renders picker w/ matches
- POST /portal/link/select        — verify pick_token, build quiz, render quiz
- POST /portal/link/quiz          — verify attempt_token + answers; success →
                                    insert ls_account_links + session cookie;
                                    fail → re-render quiz with error banner;
                                    3-fail → 24h cooldown.

CSRF: every POST validates X-Portal-CSRF-Token (header) OR form field
`csrf_token` against HMAC(PORTAL_SESSION_SECRET, link_flow_cookie_value)
before the session exists; after quiz pass we swap to session-based CSRF.

Rate limits applied at quiz-submit time:
- per-discord_id 3/day
- per-account_id 5/day
- per-(discord,account) 3-fail cooldown 24h

Hard safety:
- No raw account_id in any response body or template context. Picker matches
  carry a signed `pick_token` instead.
- Zero direct dune.* SQL.
- All live state via portal_quiz.* (which routes through routers/dune.py).
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import config
import portal_identity
from config import (
    PORTAL_LINK_FLOW_MAX_AGE,
    PORTAL_RATE_COOLDOWN_HOURS,
    PORTAL_SESSION_IDLE_MAX_AGE,
)
from database import get_db
from http_body import read_body
from portal_auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    LINK_FLOW_COOKIE,
    SELCHAR_COOKIE,
    SESSION_COOKIE,
    clear_cookie,
    client_ip,
    csrf_for_link_flow,
    csrf_for_session,
    get_portal_session,
    is_test_run,
    issue_attempt_token,
    issue_link_flow,
    issue_pick_token,
    issue_session_cookie,
    issue_switched_session_cookie,
    set_cookie,
    validate_csrf,
    verify_attempt_token,
    verify_link_flow,
    verify_pick_token,
)
from portal_host import v2_home
from portal_quiz import (
    assert_all_allowed,
    generate_quiz,
    verify_answer,
)
from portal_rate_limit import (
    check_attempts_per_account,
    check_attempts_per_discord,
    check_cooldown,
    check_selects_per_discord,
)
from routers.dune import (
    MAP_DISPLAY_NAMES,
    _cached_progression_snapshot_full,
)
from routers.portal import base_ctx

logger = logging.getLogger("portal")

router = APIRouter()
templates = Jinja2Templates(directory="templates")

SEARCH_MIN_CHARS = 3
MAX_MATCHES = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _cooldown_unlock_iso(hours: int = PORTAL_RATE_COOLDOWN_HOURS) -> str:
    """Returns the ISO timestamp at which the cooldown lifts (now + hours).
    Replaces the H-2-broken `_now_iso()` that was incorrectly being shown to
    players as the unlock time."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _require_link_flow(request: Request) -> dict:
    """Reads + verifies the link_flow cookie. Raises 302 to /portal/ if missing
    or expired (the flow cookie has a 5-min max_age — easy to time out)."""
    token = request.cookies.get(LINK_FLOW_COOKIE)
    if not token:
        raise _redirect_to_portal()
    payload = verify_link_flow(token)
    if not payload:
        raise _redirect_to_portal()
    return payload


def _redirect_to_portal() -> HTTPException:
    """We raise a custom exception that the route layer converts to 302.
    FastAPI's HTTPException doesn't natively redirect; the catch in each
    route wraps it."""
    return HTTPException(status_code=302, detail="/portal/")


def _redirect_response(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=302)


async def _validate_csrf_link_flow(request: Request) -> None:
    """Pre-session CSRF check using the link_flow cookie as the HMAC key.
    Accepts the token in either the X-Portal-CSRF-Token header OR a form
    field named csrf_token. 403 on mismatch."""
    link_flow_token = request.cookies.get(LINK_FLOW_COOKIE, "")
    if not link_flow_token:
        raise HTTPException(status_code=403, detail="Missing link_flow cookie")
    expected = csrf_for_link_flow(link_flow_token)

    header_token = request.headers.get(CSRF_HEADER, "")
    body_token = ""
    try:
        form = await request.form()
        body_token = form.get("csrf_token", "") or ""
    except Exception:
        body_token = ""
    provided = header_token or body_token
    if not validate_csrf(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _friendly_map(raw_map: Optional[str]) -> Optional[str]:
    if not raw_map:
        return None
    base = raw_map.rsplit("_", 1)[0] if raw_map[-2:-1] == "_" and raw_map[-1:].isdigit() else raw_map
    return MAP_DISPLAY_NAMES.get(raw_map) or MAP_DISPLAY_NAMES.get(base) or raw_map


async def _search_characters(query: str, discord_id: str) -> tuple[list, bool]:
    """Returns (matches, too_many). Each match is a dict with character_name,
    lvl, current_map, online, pick_token. account_id is NEVER returned —
    only embedded in the signed pick_token, which binds the picker session
    to a specific discord_id."""
    query_norm = (query or "").strip().lower()
    if len(query_norm) < SEARCH_MIN_CHARS:
        return [], False

    try:
        snapshot = await _cached_progression_snapshot_full()
    except Exception as exc:
        logger.warning("portal_link: snapshot fetch failed: %s", exc)
        return [], False

    matches = []
    for p in (snapshot.get("players") or []):
        name = p.get("char_name") or ""
        if not name:
            continue
        if query_norm not in name.lower():
            continue
        matches.append(p)
        # Hard stop at MAX_MATCHES+1 so the template can render "refine".
        if len(matches) > MAX_MATCHES:
            break

    too_many = len(matches) > MAX_MATCHES
    matches = matches[:MAX_MATCHES]

    out = []
    for p in matches:
        account_id = int(p.get("account_id") or 0)
        if account_id <= 0:
            continue
        char_name = p.get("char_name") or ""
        out.append({
            "character_name": char_name,
            "lvl": p.get("lvl"),
            "current_map": None,  # offline-friendly; live map left to /portal/account
            "online": (p.get("online_status") == "Online"),
            "pick_token": issue_pick_token(discord_id, account_id, char_name),
        })
    return out, too_many


# ---------------------------------------------------------------- routes ---


@router.api_route("/portal/link", methods=["GET", "POST"])
@router.post("/portal/link/search")
async def portal_link(request: Request):
    """Character picker. GET = empty picker; POST = search + render matches.
    Also reachable at POST /portal/link/search (dev-frontend templates post
    there). Auth: link_flow-cookie required (else 302 /portal/)."""
    try:
        flow = _require_link_flow(request)
    except HTTPException as exc:
        if exc.status_code == 302:
            return _redirect_response("/portal/")
        raise
    discord_id = flow.get("did") or ""
    discord_handle = flow.get("dh") or ""

    if request.method == "POST":
        await _validate_csrf_link_flow(request)
        form = await request.form()
        # Accept either `search_query` (my naming) or `q` (dev-frontend naming).
        search_query = (form.get("search_query") or form.get("q") or "").strip()
    else:
        search_query = (request.query_params.get("q") or "").strip()

    matches: list = []
    too_many = False
    error: Optional[str] = None
    no_match = False

    if search_query:
        if len(search_query) < SEARCH_MIN_CHARS:
            error = f"Please type at least {SEARCH_MIN_CHARS} characters of your character name."
        else:
            matches, too_many = await _search_characters(search_query, discord_id)
            no_match = (len(matches) == 0)

    # Dev-frontend's character_picker.html reads `candidates` with fields
    # `select_token` + `display_hint`. Provide both shapes for forward-compat:
    # matches[] retains the rich admin-style fields; candidates[] is the
    # lean dev-frontend shape sharing the same underlying records.
    candidates = [
        {
            "select_token": m["pick_token"],
            "display_hint": m["character_name"],
        }
        for m in matches
    ]
    for m in matches:
        m["select_token"] = m["pick_token"]
        m["display_hint"] = m["character_name"]

    ctx = {
        "discord_handle": discord_handle,
        "discord_avatar_url": None,
        "search_query": search_query,
        "matches": matches,
        "candidates": candidates or None,
        "too_many": too_many,
        "no_match": no_match,
        "error": error,
        "post_url": "/portal/link",
        # Multi-account ADD copy: add_mode retitles the picker; skip_quiz drops the
        # "three questions" promise when the added account is bound without a quiz.
        "add_mode": flow.get("md") == "add",
        "skip_quiz": (flow.get("md") == "add" and config.MULTIACCOUNT_ENABLED
                      and not config.MULTIACCOUNT_REQUIRE_QUIZ),
    }
    ctx.update(base_ctx(request, discord_handle=discord_handle))
    return templates.TemplateResponse(request, "portal/character_picker.html", ctx)


# --- multi-account helpers -------------------------------------------------

def _active_link_owner(account_id: int) -> Optional[str]:
    """discord_id of the ACTIVE link holding this account, or None if unlinked.
    Used to refuse adding an account that is already claimed (by anyone)."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT discord_id FROM ls_account_links
                WHERE account_id = ? AND revoked_at IS NULL LIMIT 1""",
            (account_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["discord_id"] if row else None


def _has_active_link(discord_id: str) -> bool:
    """True when this Discord already holds >=1 active (verified) link — the
    trust anchor that lets a multi-account ADD skip the ownership quiz."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT 1 FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL LIMIT 1""",
            (discord_id,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _commit_add_link(discord_id: str, account_id: int, character_name: str,
                     discord_handle: str) -> None:
    """Bind an additional (discord_id, account_id) link WITHOUT retiring the
    caller's other links (multi-account add). Same UPSERT shape as the quiz-pass
    commit; never touches other rows."""
    conn = get_db()
    try:
        profile_change = portal_identity.begin_legacy_links(conn, discord_id, allow_grant=True)
        conn.execute(
            """INSERT INTO ls_account_links
                 (discord_id, account_id, character_name, discord_handle)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(discord_id, account_id) DO UPDATE SET
                 character_name = excluded.character_name,
                 discord_handle = excluded.discord_handle,
                 linked_at = datetime('now'),
                 revoked_at = NULL,
                 revoked_by = NULL,
                 revoke_reason = NULL""",
            (discord_id, account_id, character_name, discord_handle),
        )
        portal_identity.finish_legacy_links(conn, profile_change)
        conn.commit()
    finally:
        conn.close()


@router.post("/portal/link/select")
async def portal_link_select(request: Request):
    """Picker submit. Verify pick_token, generate quiz, insert attempt row,
    render quiz page. Quiz rate-limit checks happen here so we never even
    render a quiz for a cooldown'd pair."""
    try:
        flow = _require_link_flow(request)
    except HTTPException as exc:
        if exc.status_code == 302:
            return _redirect_response("/portal/")
        raise
    await _validate_csrf_link_flow(request)
    discord_id = flow.get("did") or ""
    discord_handle = flow.get("dh") or ""

    form = await request.form()
    # Accept either `pick_token` (my naming) or `select_token` (dev-frontend).
    pick_token = form.get("pick_token") or form.get("select_token") or ""
    if not pick_token:
        raise HTTPException(status_code=400, detail="Missing pick_token")
    pick = verify_pick_token(pick_token)
    if not pick:
        return templates.TemplateResponse(
            request,
            "portal/error_state_expired.html",
            {"title": "Selection expired",
             "message": "Please search and pick your character again.",
             "back_url": "/portal/link"},
            status_code=400,
        )
    if pick.get("did") != discord_id:
        # Pick token was minted for a different Discord identity — refuse.
        logger.warning("portal_link: pick_token did mismatch")
        raise HTTPException(status_code=403, detail="Pick token does not match session")

    account_id = int(pick.get("aid") or 0)
    character_name = pick.get("cn") or ""
    if account_id <= 0 or not character_name:
        raise HTTPException(status_code=400, detail="Malformed pick token")

    # Burst-limit /portal/link/select per discord_id (M-4 review fix).
    # Bounds unbounded pick mints + relay snapshot reads even when no quiz
    # has been submitted yet (so the per-day attempt counters wouldn't fire).
    ok_s, retry_s = check_selects_per_discord(discord_id)
    if not ok_s:
        return templates.TemplateResponse(
            request,
            "portal/error_rate_limited.html",
            {"title": "Too many character selections",
             "message": "Please wait a few minutes before trying again.",
             "retry_after_seconds": retry_s,
             "back_url": "/portal/"},
            status_code=429,
        )

    # Rate-limit (per-discord and per-account) before issuing the quiz.
    ok_d, retry_d = check_attempts_per_discord(discord_id)
    if not ok_d:
        return templates.TemplateResponse(
            request,
            "portal/error_rate_limited.html",
            {"title": "Too many link attempts",
             "message": "You have used the maximum link attempts allowed in 24 hours.",
             "retry_after_seconds": retry_d,
             "back_url": "/portal/"},
            status_code=429,
        )
    ok_a, retry_a = check_attempts_per_account(account_id)
    if not ok_a:
        return templates.TemplateResponse(
            request,
            "portal/error_rate_limited.html",
            {"title": "Too many link attempts on this character",
             "message": "This character has had too many link attempts in the last 24 hours. Please try again later.",
             "retry_after_seconds": retry_a,
             "back_url": "/portal/"},
            status_code=429,
        )
    ok_c, retry_c = check_cooldown(discord_id, account_id)
    if not ok_c:
        return templates.TemplateResponse(
            request,
            "portal/error_cooldown.html",
            {"title": "Too many failed attempts",
             "message": "Please try again later.",
             "retry_after_iso": _cooldown_unlock_iso(),
             "hours_remaining": PORTAL_RATE_COOLDOWN_HOURS,
             "back_url": "/portal/"},
            status_code=429,
        )

    # Multi-account ADD (quiz-skip path). Only when: the feature is on, we are not
    # forcing a per-account quiz, the flow is add-mode, AND the caller ALREADY holds
    # >=1 verified link (the trust anchor — the first link passed the quiz). The
    # added account must NOT already be linked to anyone (never override a claim).
    # Otherwise we fall through to the normal ownership quiz below.
    if (flow.get("md") == "add" and config.MULTIACCOUNT_ENABLED
            and not config.MULTIACCOUNT_REQUIRE_QUIZ and _has_active_link(discord_id)):
        owner = _active_link_owner(account_id)
        if owner == discord_id:
            return templates.TemplateResponse(
                request, "portal/error_state_expired.html",
                {"title": "Already linked",
                 "message": f"{character_name} is already linked to your Discord.",
                 "back_url": "/portal/account"},
                status_code=400,
            )
        if owner:
            return templates.TemplateResponse(
                request, "portal/error_state_expired.html",
                {"title": "Account already claimed",
                 "message": "That character is already linked to another Discord account.",
                 "back_url": "/portal/account"},
                status_code=409,
            )
        _commit_add_link(discord_id, account_id, character_name, discord_handle)
        logger.info("portal: multiaccount_add did=%s aid=%s (quiz-skipped)",
                    discord_id, account_id)
        ip = client_ip(request)
        session_value = issue_session_cookie(discord_id, account_id, ip, authentication_time=flow.get('auth_at'))
        resp = _redirect_response(v2_home(request.headers.get("host", "").lower()))
        set_cookie(resp, SESSION_COOKIE, session_value,
                   max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=True)
        set_cookie(resp, CSRF_COOKIE, csrf_for_session(session_value),
                   max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=False)
        clear_cookie(resp, LINK_FLOW_COOKIE)
        return resp

    quiz = await generate_quiz(account_id)
    if quiz is None:
        # Not enough live state to verify identity. Refuse politely.
        return templates.TemplateResponse(
            request,
            "portal/error_state_expired.html",
            {"title": "Unable to build quiz",
             "message": "We could not load enough game data to verify this character right now. Please try again in a few minutes.",
             "back_url": "/portal/link"},
            status_code=503,
        )

    assert_all_allowed(quiz.kinds)

    # Persist the attempt row up front — verify step reads back from it.
    # pick_token goes into its own column with UNIQUE index, so an IntegrityError
    # here means the same pick_token was already used to mint an attempt
    # (M-5 review fix — prevents re-use of a captured pick_token for unbounded
    # attempt_row creation).
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")[:512]
    test_run = 1 if is_test_run(request) else 0
    conn = get_db()
    try:
        try:
            cur = conn.execute(
                """INSERT INTO portal_link_attempts
                     (discord_id, account_id, pick_token, state_issued_at,
                      q1_kind, q1_correct_hash, q2_kind, q2_correct_hash,
                      q3_kind, q3_correct_hash, ip_addr, user_agent, is_test_run)
                   VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    discord_id,
                    account_id,
                    pick_token,
                    quiz.kinds[0], quiz.correct_hashes[0],
                    quiz.kinds[1], quiz.correct_hashes[1],
                    quiz.kinds[2], quiz.correct_hashes[2],
                    ip, ua, test_run,
                ),
            )
            conn.commit()
            attempt_id = cur.lastrowid
        except sqlite3.IntegrityError:
            # UNIQUE pick_token violation — caller is replaying a pick_token.
            logger.warning(
                "portal_link: pick_token re-use detected did=%s", discord_id
            )
            return templates.TemplateResponse(
                request,
                "portal/error_state_expired.html",
                {"title": "Selection already used",
                 "message": "Please search and pick your character again.",
                 "back_url": "/portal/link"},
                status_code=400,
            )
    finally:
        conn.close()

    attempt_token = issue_attempt_token(attempt_id, discord_id, account_id)

    # Project quiz questions for the template (omit `correct`).
    rendered_questions = [
        {
            "index": i,
            "kind": q.kind,
            "prompt": q.prompt,
            "format": q.format,
            "choices": q.choices,
            "input_name": q.input_name,
        }
        for i, q in enumerate(quiz.questions)
    ]

    quiz_ctx = {
        "discord_handle": discord_handle,
        "character_name": character_name,
        "character_name_snapshot": character_name,  # alias for dev-frontend
        "attempt_token": attempt_token,
        "questions": rendered_questions,
        "error_banner": None,
        "show_error_banner": False,
        # We deliberately do NOT surface attempts_remaining to the player —
        # would aid bruteforce. Templates that read it should treat None as
        # "do not display".
        "attempts_remaining": None,
        "post_url": "/portal/link/quiz",
    }
    quiz_ctx.update(base_ctx(request, discord_handle=discord_handle))
    return templates.TemplateResponse(request, "portal/quiz.html", quiz_ctx)


@router.post("/portal/link/quiz")
async def portal_link_quiz_submit(request: Request):
    """Quiz submit. Verify attempt_token, re-hash submitted answers, compare
    via hmac.compare_digest. On pass → insert ls_account_links + set session
    cookie. On fail → re-render quiz with generic banner; if cumulative 3
    fails in 24h, surface cooldown page."""
    try:
        flow = _require_link_flow(request)
    except HTTPException as exc:
        if exc.status_code == 302:
            return _redirect_response("/portal/")
        raise
    await _validate_csrf_link_flow(request)
    discord_id = flow.get("did") or ""
    discord_handle = flow.get("dh") or ""

    form = await request.form()
    attempt_token = form.get("attempt_token") or ""
    if not attempt_token:
        raise HTTPException(status_code=400, detail="Missing attempt_token")
    attempt = verify_attempt_token(attempt_token)
    if not attempt:
        return templates.TemplateResponse(
            request,
            "portal/error_state_expired.html",
            {"title": "Quiz expired",
             "message": "Please start over.",
             "back_url": "/portal/link"},
            status_code=400,
        )
    if attempt.get("did") != discord_id:
        raise HTTPException(status_code=403, detail="Attempt token does not match session")

    attempt_id = int(attempt.get("a") or 0)
    account_id = int(attempt.get("aid") or 0)
    if attempt_id <= 0 or account_id <= 0:
        raise HTTPException(status_code=400, detail="Malformed attempt token")

    # Load attempt row to get stored hashes + kinds.
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT q1_kind, q1_correct_hash, q2_kind, q2_correct_hash,
                      q3_kind, q3_correct_hash, attempt_at
                 FROM portal_link_attempts WHERE id = ?""",
            (attempt_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="Unknown attempt")
    if row["attempt_at"]:
        # Already consumed — don't allow replay.
        return templates.TemplateResponse(
            request,
            "portal/error_state_expired.html",
            {"title": "Quiz already submitted",
             "message": "Please start over.",
             "back_url": "/portal/link"},
            status_code=400,
        )

    # Read 3 submitted answers; missing-input counts as wrong.
    # Accept either `qN` (my naming) or `qN_answer` (dev-frontend naming).
    submitted = [
        form.get("q0") or form.get("q0_answer") or "",
        form.get("q1") or form.get("q1_answer") or "",
        form.get("q2") or form.get("q2_answer") or "",
    ]
    expected_hashes = [row["q1_correct_hash"], row["q2_correct_hash"], row["q3_correct_hash"]]
    all_correct = all(
        verify_answer(submitted[i], expected_hashes[i])
        for i in range(3)
    )

    # Stamp the attempt row regardless.
    result = "success" if all_correct else "fail"
    conn = get_db()
    try:
        consumed = conn.execute(
            """UPDATE portal_link_attempts
                  SET attempt_at = datetime('now'),
                      state_consumed_at = datetime('now'),
                      result = ?
                WHERE id = ? AND attempt_at IS NULL""",
            (result, attempt_id),
        )
        conn.commit()
    finally:
        conn.close()

    if consumed.rowcount != 1:
        return templates.TemplateResponse(
            request, "portal/error_state_expired.html",
            {"title": "Quiz already submitted", "message": "Please start over.", "back_url": "/portal/link"},
            status_code=400,
        )

    if not all_correct:
        logger.info(
            "portal: quiz_strike did=%s aid=%s", discord_id, account_id,
        )
        # After this fail, check whether the cooldown threshold has just been
        # crossed; if so, surface the cooldown page.
        ok_c, retry_c = check_cooldown(discord_id, account_id)
        if not ok_c:
            logger.info("portal: quiz_lockout did=%s aid=%s", discord_id, account_id)
            return templates.TemplateResponse(
                request,
                "portal/error_cooldown.html",
                {"title": "Too many failed attempts",
                 "message": "Please try again later.",
                 "retry_after_iso": _cooldown_unlock_iso(),
                 "hours_remaining": PORTAL_RATE_COOLDOWN_HOURS,
                 "back_url": "/portal/"},
                status_code=429,
            )
        # Re-render the quiz with the generic banner. Reconstruct the question
        # SHAPE from the kinds stored in the attempt row — but we lost the
        # plaintext correct values, so we cannot regenerate the SAME quiz
        # cleanly without re-fetching state. For v1 simplicity: send the user
        # back to /portal/link to start a fresh attempt. This counts as one
        # strike toward the 3-fail cooldown but doesn't waste their time
        # re-rendering a half-broken quiz.
        fail_ctx = {
            "discord_handle": discord_handle,
            "character_name": "",
            "character_name_snapshot": "",
            "attempt_token": "",
            "questions": [],
            "error_banner": "One or more of your answers was incorrect. Please return to the picker and try again.",
            "show_error_banner": True,
            "attempts_remaining": None,
            "post_url": "/portal/link/quiz",
        }
        fail_ctx.update(base_ctx(request, discord_handle=discord_handle))
        return templates.TemplateResponse(request, "portal/quiz.html", fail_ctx, status_code=400)

    # PASS — insert ls_account_links and issue session.
    logger.info("portal: quiz_pass did=%s aid=%s", discord_id, account_id)

    # Look up character_name for the link row from snapshot (best-effort);
    # fall back to "(unknown)" so the insert never fails on NOT NULL.
    character_name = "(unknown)"
    try:
        snap = await _cached_progression_snapshot_full()
        for p in (snap.get("players") or []):
            if int(p.get("account_id") or 0) == account_id:
                character_name = p.get("char_name") or character_name
                break
    except Exception as exc:
        logger.warning("portal: char_name lookup failed during quiz_pass: %s", exc)

    # Multi-account ADD: never bind an account already claimed by a DIFFERENT
    # Discord, even on a quiz pass (passing proves you can read the account, but we
    # keep one account under one Discord). Same-Discord re-adds fall through to the
    # idempotent UPSERT.
    if flow.get("md") == "add":
        _owner = _active_link_owner(account_id)
        if _owner and _owner != discord_id:
            return templates.TemplateResponse(
                request, "portal/error_state_expired.html",
                {"title": "Account already claimed",
                 "message": "That character is already linked to another Discord account.",
                 "back_url": v2_home(request.headers.get("host", "").lower())},
                status_code=409,
            )

    conn = get_db()
    try:
        profile_change = portal_identity.begin_legacy_links(conn, discord_id, allow_grant=True)
        # UPSERT pattern: UNIQUE(discord_id, account_id). If a revoked row
        # exists for this pair, UPDATE clears revoked_at.
        conn.execute(
            """INSERT INTO ls_account_links
                 (discord_id, account_id, character_name, discord_handle)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(discord_id, account_id) DO UPDATE SET
                 character_name = excluded.character_name,
                 discord_handle = excluded.discord_handle,
                 linked_at = datetime('now'),
                 revoked_at = NULL,
                 revoked_by = NULL,
                 revoke_reason = NULL""",
            (discord_id, account_id, character_name, discord_handle),
        )
        # Self-serve relink: the player re-pointed their link to this character,
        # so retire any OTHER active link they hold (e.g. the now-deleted char
        # the portal was stuck on). The just-linked pair is excluded so it stays
        # active. Keyed on discord_id (from the signed flow cookie) — only ever
        # the caller's own rows.
        if flow.get("rl") == 1:
            conn.execute(
                """UPDATE ls_account_links
                      SET revoked_at = datetime('now'),
                          revoked_by = 'self',
                          revoke_reason = 'player-relinked'
                    WHERE discord_id = ? AND account_id != ?
                          AND revoked_at IS NULL""",
                (discord_id, account_id),
            )
        portal_identity.finish_legacy_links(conn, profile_change)
        conn.commit()
    finally:
        conn.close()

    ip = client_ip(request)
    session_value = issue_session_cookie(discord_id, account_id, ip, authentication_time=flow.get('auth_at'))
    # An alt-add returns to the V2 app it started from; a first link / relink lands
    # on the classic account page as before.
    resp = _redirect_response(v2_home(request.headers.get("host", "").lower())
                              if flow.get("md") == "add" else "/portal/account")
    set_cookie(resp, SESSION_COOKIE, session_value,
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=True)
    set_cookie(resp, CSRF_COOKIE, csrf_for_session(session_value),
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=False)
    # Clear the link_flow cookie; the session takes over.
    clear_cookie(resp, LINK_FLOW_COOKIE)
    return resp


# --- Player-side unlink + relink (self-serve) ------------------------------
# These routes are SESSION-authed: the player already proved ownership of their
# currently-linked character (OAuth + 3-question quiz) and holds a valid
# ls_portal_session cookie. discord_id comes from that signed session — never
# from the request body — so a player can only ever operate on their OWN
# link(s). All writes hit admin.db (ls_account_links) only; zero dune.* writes.


def _require_portal_session(request: Request) -> dict:
    """Reads + verifies the portal session cookie. Returns the payload, or
    raises a 302-to-portal HTTPException (same convention as _require_link_flow)
    when there is no valid session."""
    session = get_portal_session(request)
    if not session:
        raise _redirect_to_portal()
    return session


async def _validate_csrf_session(request: Request) -> None:
    """Session-bound CSRF check (post-link surface). Mirrors the logout route:
    the token (header X-Portal-CSRF-Token or form field csrf_token) must equal
    HMAC(session_secret, session_cookie_value). 403 on mismatch."""
    session_token = request.cookies.get(SESSION_COOKIE, "")
    if not session_token:
        raise HTTPException(status_code=403, detail="Missing session cookie")
    expected = csrf_for_session(session_token)
    header_token = request.headers.get(CSRF_HEADER, "")
    body_token = ""
    try:
        form = await request.form()
        body_token = form.get("csrf_token", "") or ""
    except Exception:
        body_token = ""
    provided = header_token or body_token
    if not validate_csrf(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


@router.post("/portal/link/unlink")
@router.post("/portal/link/revoke")
async def portal_link_unlink(request: Request):
    """Self-serve UNLINK. The logged-in player revokes THEIR OWN active link.

    Scope: discord_id comes from the signed session, so this can only ever
    revoke a row belonging to the caller. We revoke the active link the session
    is pinned to (its account_id); if the session's account_id no longer has an
    active row (e.g. operator-revoked underneath), we fall back to revoking any
    remaining active link for this discord_id so the player is never left in a
    half-linked limbo. After unlink the session cookies are cleared and the
    player lands back on the public landing, free to re-link fresh.
    """
    try:
        session = _require_portal_session(request)
    except HTTPException as exc:
        if exc.status_code == 302:
            return _redirect_response("/portal/")
        raise
    await _validate_csrf_session(request)
    if session.get('sid'):
        return _redirect_response("/settings#sign-in-security")

    discord_id = str(session.get("did") or "")
    account_id = int(session.get("aid") or 0)
    if not discord_id:
        return _redirect_response("/portal/")

    conn = get_db()
    try:
        profile_change = portal_identity.begin_legacy_links(conn, discord_id, allow_grant=False)
        cur = conn.execute(
            """UPDATE ls_account_links
                  SET revoked_at = datetime('now'),
                      revoked_by = 'self',
                      revoke_reason = 'player-unlinked'
                WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL""",
            (discord_id, account_id),
        )
        # Session's account had no active row (e.g. operator-revoked underneath) —
        # revoke ONE remaining link so the unlink button never silently no-ops.
        #
        # 🔴 BOUNDED TO A SINGLE ROW ON PURPOSE. This fallback used to be an
        # unqualified `WHERE discord_id = ?`, which revoked EVERY active link the
        # player had. That was harmless when one Discord identity meant one
        # character, but multi-account shipped 2026-07-17 and measured on live
        # 2026-08-01 there are 9 players holding more than one link, one of them
        # holding four. For them a single mis-click on V1 Classic wiped the lot,
        # each one needing a fresh OAuth plus the 3-question ownership quiz to
        # restore. Pick the most recently used link and revoke only that.
        if cur.rowcount == 0:
            conn.execute(
                """UPDATE ls_account_links
                      SET revoked_at = datetime('now'),
                          revoked_by = 'self',
                          revoke_reason = 'player-unlinked'
                    WHERE id = (
                        SELECT id FROM ls_account_links
                         WHERE discord_id = ? AND revoked_at IS NULL
                         ORDER BY last_session_at DESC NULLS LAST, linked_at DESC, id DESC
                         LIMIT 1)""",
                (discord_id,),
            )
        portal_identity.finish_legacy_links(conn, profile_change)
        conn.commit()
    finally:
        conn.close()

    logger.info("portal: self_unlink did=%s aid=%s", discord_id, account_id)

    # Tear down the session — the link it was pinned to is gone.
    resp = _redirect_response("/portal/?b=link_revoked")
    clear_cookie(resp, SESSION_COOKIE)
    clear_cookie(resp, CSRF_COOKIE)
    return resp


@router.post("/portal/link/unlink-account")
async def portal_link_unlink_account(request: Request):
    """Alt-aware self-serve UNLINK (V2 JSON; used by the account-manage dialog).

    Unlike portal_link_unlink above, the caller names WHICH linked account_id
    to drop instead of the endpoint acting on whatever the session happens to
    be pinned to. Ownership is re-proven server-side on every call: the UPDATE
    below is scoped to (discord_id FROM THE SIGNED SESSION, account_id) AND
    revoked_at IS NULL in one statement, so a row is only ever touched when it
    is both the caller's own AND currently active — there is no separate
    "check, then trust" step for a race to slip through. A request naming an
    id the caller does not hold, or one already revoked, simply matches zero
    rows and is refused.

    Never a mass-revoke: only the named account_id is ever written, full stop.
    There is no fallback to "whatever else is still active for this
    discord_id" on this path — that fallback is the footgun on the legacy
    route above, and multi-account support is exactly why it is not safe to
    reuse here.

    Session handling:
      - other links remain, the unlinked account was NOT the active one:
        the session is left untouched.
      - other links remain, the unlinked account WAS the active one: the
        session is re-pointed at another surviving link (oldest-linked
        first — the same ordering /portal/me already exposes) instead of
        logging the player out.
      - no links remain: the session is torn down, same end state as the
        legacy route above.
    """
    session = get_portal_session(request)
    if not session:
        return JSONResponse({"ok": False, "error": "unauthenticated"}, status_code=401)
    await _validate_csrf_session(request)
    if session.get('sid'):
        return JSONResponse({"ok": False, "error": "use_security_settings"}, status_code=409)

    discord_id = str(session.get("did") or "")
    if not discord_id:
        return JSONResponse({"ok": False, "error": "unauthenticated"}, status_code=401)

    body = await read_body(request)
    try:
        target_account_id = int(body.get("account_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad_account"}, status_code=400)
    if target_account_id <= 0:
        return JSONResponse({"ok": False, "error": "bad_account"}, status_code=400)

    current_account_id = int(session.get("aid") or 0)

    conn = get_db()
    try:
        profile_change = portal_identity.begin_legacy_links(conn, discord_id, allow_grant=False)
        cur = conn.execute(
            """UPDATE ls_account_links
                  SET revoked_at = datetime('now'),
                      revoked_by = 'self',
                      revoke_reason = 'player-unlinked'
                WHERE discord_id = ? AND account_id = ? AND revoked_at IS NULL""",
            (discord_id, target_account_id),
        )
        if cur.rowcount == 0:
            # Not an active link of THIS discord_id (wrong id, someone else's
            # account, or already revoked). Refuse — nothing else is touched.
            return JSONResponse({"ok": False, "error": "not_your_account"}, status_code=404)

        survivors = conn.execute(
            """SELECT account_id FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL
                ORDER BY linked_at ASC""",
            (discord_id,),
        ).fetchall()
        portal_identity.finish_legacy_links(conn, profile_change)
        conn.commit()
    finally:
        conn.close()

    logger.info("portal: self_unlink_account did=%s aid=%s", discord_id, target_account_id)
    survivor_ids = [int(r["account_id"]) for r in survivors]

    if not survivor_ids:
        resp = JSONResponse({"ok": True, "unlinked": target_account_id, "session": "cleared"})
        clear_cookie(resp, SESSION_COOKIE)
        clear_cookie(resp, CSRF_COOKIE)
        return resp

    if target_account_id != current_account_id:
        # A non-active alt dropped — the session already points somewhere valid.
        return JSONResponse({"ok": True, "unlinked": target_account_id, "session": "unchanged"})

    # The session's own account was unlinked but other links remain — re-point
    # at a survivor instead of logging the player out of a set they still hold.
    next_account_id = survivor_ids[0]
    ip = client_ip(request)
    session_value = issue_switched_session_cookie(session, next_account_id, ip)
    resp = JSONResponse({
        "ok": True, "unlinked": target_account_id,
        "session": "switched", "account_id": next_account_id,
    })
    set_cookie(resp, SESSION_COOKIE, session_value,
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=True)
    set_cookie(resp, CSRF_COOKIE, csrf_for_session(session_value),
               max_age=PORTAL_SESSION_IDLE_MAX_AGE, httponly=False)
    clear_cookie(resp, SELCHAR_COOKIE)
    return resp


@router.post("/portal/link/relink")
async def portal_link_relink(request: Request):
    """Self-serve RELINK. The logged-in player re-points their Discord link to a
    different / current character (e.g. after a Funcom character transfer left
    the portal pointed at a now-deleted character).

    SECURITY — ownership re-verification is NOT bypassed. This route does not
    bind any character itself; it only re-enters the SAME initial-link flow
    (picker -> 3-question identity quiz) by minting a fresh link_flow cookie for
    the session's discord_id. The player must search for, select, and pass the
    quiz on the target character exactly as a first-time linker would. The quiz
    UPSERT in /portal/link/quiz then binds the new (discord_id, account_id) and
    issues a fresh session pinned to the new account. We do NOT trust any client
    supplied account_id / character_name here.

    The old link is left intact for now; the new quiz-pass UPSERT supersedes the
    active session. The deleted-old-character case is handled for free — the
    relink flow is keyed only on discord_id, so it never depends on the stale
    link's character still existing.
    """
    try:
        session = _require_portal_session(request)
    except HTTPException as exc:
        if exc.status_code == 302:
            return _redirect_response("/portal/")
        raise
    await _validate_csrf_session(request)
    if session.get('sid'):
        return _redirect_response("/settings#sign-in-security")

    discord_id = str(session.get("did") or "")
    if not discord_id:
        return _redirect_response("/portal/")

    # Recover the discord_handle from the active link row for this discord_id so
    # the picker/quiz header keeps showing the right name; default to "" if the
    # only rows are revoked (the link_flow cookie just carries it for display).
    discord_handle = ""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT discord_handle FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL
                ORDER BY linked_at DESC LIMIT 1""",
            (discord_id,),
        ).fetchone()
        if not row:
            row = conn.execute(
                """SELECT discord_handle FROM ls_account_links
                    WHERE discord_id = ?
                    ORDER BY linked_at DESC LIMIT 1""",
                (discord_id,),
            ).fetchone()
        if row:
            discord_handle = row["discord_handle"] or ""
    finally:
        conn.close()

    logger.info("portal: relink_start did=%s", discord_id)

    # Drop into the picker. We hand the player a fresh link_flow cookie (the same
    # short-lived credential the OAuth callback issues for a brand-new link) and
    # swap the live session out for it, so the picker/quiz pages render in
    # link-flow mode and every existing rate-limit + CSRF + quiz check applies
    # unchanged. The session is re-minted on quiz pass.
    flow_value = issue_link_flow(discord_id, discord_handle, relink=True)
    resp = _redirect_response("/portal/link")
    set_cookie(resp, LINK_FLOW_COOKIE, flow_value,
               max_age=PORTAL_LINK_FLOW_MAX_AGE, httponly=True)
    set_cookie(resp, CSRF_COOKIE, csrf_for_link_flow(flow_value),
               max_age=PORTAL_LINK_FLOW_MAX_AGE, httponly=False)
    # Clear the live session cookie so the picker surface is unambiguously in
    # link-flow mode (base_ctx prefers a session over a link_flow cookie). The
    # player's old link row stays active in the DB; quiz-pass re-mints a session.
    clear_cookie(resp, SESSION_COOKIE)
    return resp


@router.post("/portal/link/add")
async def portal_link_add(request: Request):
    """Multi-account ADD. A logged-in player links ANOTHER game account. Mints a
    fresh add-mode link_flow for the session's discord_id and drops into the same
    picker a first-time linker uses, so every rate-limit + CSRF check applies.

    SECURITY: unlike relink, add-mode KEEPS the caller's existing links. The picked
    account is bound in /portal/link/select; when MULTIACCOUNT_REQUIRE_QUIZ=0 the
    ownership quiz is skipped ONLY because the caller already holds a verified link
    (trust anchor) — and the add path refuses any account already linked to anyone,
    so it can never override a claim. discord_id comes from the signed session,
    never the body. Gated by LASTSIETCH_MULTIACCOUNT_ENABLED.
    """
    if not config.MULTIACCOUNT_ENABLED:
        return _redirect_response("/portal/account")
    try:
        session = _require_portal_session(request)
    except HTTPException as exc:
        if exc.status_code == 302:
            return _redirect_response("/portal/")
        raise
    await _validate_csrf_session(request)
    if session.get('sid'):
        return _redirect_response("/settings#sign-in-security")

    discord_id = str(session.get("did") or "")
    if not discord_id or not _has_active_link(discord_id):
        # Must already hold a verified link to add another.
        return _redirect_response("/portal/")

    discord_handle = ""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT discord_handle FROM ls_account_links
                WHERE discord_id = ? AND revoked_at IS NULL
                ORDER BY linked_at DESC LIMIT 1""",
            (discord_id,),
        ).fetchone()
        if row:
            discord_handle = row["discord_handle"] or ""
    finally:
        conn.close()

    logger.info("portal: multiaccount_add_start did=%s", discord_id)
    flow_value = issue_link_flow(discord_id, discord_handle, add=True)
    resp = _redirect_response("/portal/link")
    set_cookie(resp, LINK_FLOW_COOKIE, flow_value,
               max_age=PORTAL_LINK_FLOW_MAX_AGE, httponly=True)
    set_cookie(resp, CSRF_COOKIE, csrf_for_link_flow(flow_value),
               max_age=PORTAL_LINK_FLOW_MAX_AGE, httponly=False)
    clear_cookie(resp, SESSION_COOKIE)
    return resp
