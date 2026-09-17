from fastapi import APIRouter, HTTPException, Request

from auth import audit_log, hash_password, require_admin, require_csrf
from config import MIN_PASSWORD_LENGTH
from database import get_db

router = APIRouter(prefix="/api/users")


@router.get("")
async def list_users(request: Request):
    user = require_admin(request)
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, role, created_at, last_login, is_active, locked_until FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("")
async def create_user(request: Request):
    user = require_admin(request)
    require_csrf(request, user)

    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "operator")

    if not username:
        raise HTTPException(status_code=400, detail="Username required")
    if role not in ("admin", "operator"):
        raise HTTPException(status_code=400, detail="Role must be admin or operator")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    pw_hash = hash_password(password)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_by) VALUES (?, ?, ?, ?)",
            (username, pw_hash, role, user["id"]),
        )
        conn.commit()
    except Exception:
        conn.close()
        raise HTTPException(status_code=409, detail="Username already exists")
    conn.close()

    audit_log(user["id"], user["username"], "create_user", username, request.client.host, f"role={role}")
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(request: Request, user_id: int):
    user = require_admin(request)
    require_csrf(request, user)

    if user_id == 1:
        raise HTTPException(status_code=403, detail="Cannot delete the primary admin account")
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    conn = get_db()
    target = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    audit_log(user["id"], user["username"], "delete_user", target["username"], request.client.host)
    return {"ok": True}


@router.patch("/{user_id}/role")
async def change_role(request: Request, user_id: int):
    user = require_admin(request)
    require_csrf(request, user)

    if user_id == 1:
        raise HTTPException(status_code=403, detail="Cannot change the primary admin's role")

    body = await request.json()
    role = body.get("role", "")
    if role not in ("admin", "operator"):
        raise HTTPException(status_code=400, detail="Role must be admin or operator")

    conn = get_db()
    target = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    conn.close()

    audit_log(user["id"], user["username"], "change_role", target["username"], request.client.host, f"new_role={role}")
    return {"ok": True}


@router.post("/{user_id}/reset-password")
async def reset_password(request: Request, user_id: int):
    user = require_admin(request)
    require_csrf(request, user)

    if user_id == 1 and user["id"] != 1:
        raise HTTPException(status_code=403, detail="Only the primary admin can reset their own password")

    body = await request.json()
    new_password = body.get("new_password", "")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    conn = get_db()
    target = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    conn.execute(
        "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    conn.commit()
    conn.close()

    audit_log(user["id"], user["username"], "reset_password", target["username"], request.client.host)
    return {"ok": True}


@router.post("/{user_id}/unlock")
async def unlock_user(request: Request, user_id: int):
    user = require_admin(request)
    require_csrf(request, user)

    conn = get_db()
    target = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    conn.execute("UPDATE users SET locked_until = NULL, failed_attempts = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    audit_log(user["id"], user["username"], "unlock_user", target["username"], request.client.host)
    return {"ok": True}
