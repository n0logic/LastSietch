from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import (
    audit_log,
    create_session,
    generate_csrf_token,
    get_current_user,
    hash_password,
    require_csrf,
    verify_password,
)
from config import LOCKOUT_MINUTES, LOCKOUT_THRESHOLD, MIN_PASSWORD_LENGTH, SESSION_LIFETIME_HOURS
from database import get_db, has_users
from rate_limit import check_rate_limit

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/login")
async def login_page(request: Request):
    try:
        get_current_user(request)
        return RedirectResponse(url="/admin/", status_code=302)
    except HTTPException:
        pass
    return templates.TemplateResponse(request, "login.html")


@router.post("/api/auth/login")
async def login(request: Request):
    ip = request.client.host
    if not check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts")

    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()

    if not user:
        conn.close()
        audit_log(None, username, "login_failed", None, ip, "Unknown user", success=False)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check lockout
    if user["locked_until"]:
        locked = datetime.fromisoformat(user["locked_until"])
        if locked > datetime.now(timezone.utc):
            conn.close()
            audit_log(user["id"], username, "login_blocked", None, ip, "Account locked", success=False)
            raise HTTPException(status_code=423, detail="Account locked. Try again later.")
        else:
            conn.execute("UPDATE users SET locked_until = NULL, failed_attempts = 0 WHERE id = ?", (user["id"],))
            conn.commit()

    if not verify_password(password, user["password_hash"]):
        failed = user["failed_attempts"] + 1
        if failed >= LOCKOUT_THRESHOLD:
            lock_until = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            from datetime import timedelta
            lock_until = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
            conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (failed, lock_until, user["id"]))
        else:
            conn.execute("UPDATE users SET failed_attempts = ? WHERE id = ?", (failed, user["id"]))
        conn.commit()
        conn.close()
        audit_log(user["id"], username, "login_failed", None, ip, f"Bad password (attempt {failed})", success=False)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Success — reset failed attempts, update last_login
    conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login = ? WHERE id = ?",
                 (datetime.now(timezone.utc).isoformat(), user["id"]))
    conn.commit()
    conn.close()

    token = create_session(user["id"], ip, request.headers.get("user-agent"))
    audit_log(user["id"], username, "login", None, ip)

    response = JSONResponse({"username": user["username"], "role": user["role"]})
    response.set_cookie(
        key="ls_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=SESSION_LIFETIME_HOURS * 3600,
        path="/admin",
    )
    return response


@router.post("/api/auth/logout")
async def logout(request: Request):
    user = get_current_user(request)
    require_csrf(request, user)

    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token = ?", (user["session_token"],))
    conn.commit()
    conn.close()

    audit_log(user["id"], user["username"], "logout", None, request.client.host)

    response = JSONResponse({"ok": True})
    response.delete_cookie("ls_session", path="/admin")
    return response


@router.get("/api/auth/me")
async def me(request: Request):
    user = get_current_user(request)
    return {"username": user["username"], "role": user["role"]}


@router.get("/api/auth/csrf")
async def csrf(request: Request):
    user = get_current_user(request)
    return {"token": generate_csrf_token(user["session_token"])}


@router.get("/setup")
async def setup_page(request: Request):
    if has_users():
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(request, "setup.html")


@router.post("/api/setup")
async def setup(request: Request):
    if has_users():
        raise HTTPException(status_code=403, detail="Setup already completed")

    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username:
        raise HTTPException(status_code=400, detail="Username required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    pw_hash = hash_password(password)
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
        (username, pw_hash),
    )
    conn.commit()
    conn.close()

    ip = request.client.host
    audit_log(1, username, "setup", None, ip, "Initial admin account created")
    return {"ok": True, "message": "Admin account created. Please log in."}


@router.post("/api/auth/change-password")
async def change_password(request: Request):
    user = get_current_user(request)
    require_csrf(request, user)

    body = await request.json()
    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")

    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not verify_password(current_password, row["password_hash"]):
        conn.close()
        audit_log(user["id"], user["username"], "change_password", None, request.client.host, "Bad current password", success=False)
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user["id"]))
    conn.commit()
    conn.close()

    audit_log(user["id"], user["username"], "change_password", None, request.client.host)
    return {"ok": True}
