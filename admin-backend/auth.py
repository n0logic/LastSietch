import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe

import bcrypt
from fastapi import HTTPException, Request

from config import BCRYPT_ROUNDS, SESSION_LIFETIME_HOURS, SESSION_SECRET
from database import get_db


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_session(user_id: int, ip: str, user_agent: str | None) -> str:
    token = token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_LIFETIME_HOURS)
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
        (token, user_id, expires.isoformat(), ip, user_agent),
    )
    conn.commit()
    conn.close()
    return token


def generate_csrf_token(session_token: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), session_token.encode(), hashlib.sha256).hexdigest()


def get_current_user(request: Request) -> dict:
    token = request.cookies.get("ls_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    conn = get_db()
    row = conn.execute(
        """SELECT s.token, s.user_id, s.expires_at, u.username, u.role, u.is_active
           FROM sessions s JOIN users u ON s.user_id = u.id
           WHERE s.token = ?""",
        (token,),
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid session")

    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=401, detail="Session expired")

    if not row["is_active"]:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=401, detail="Account disabled")

    conn.execute(
        "UPDATE sessions SET last_activity = ? WHERE token = ?",
        (datetime.now(timezone.utc).isoformat(), token),
    )
    conn.commit()
    conn.close()

    return {
        "id": row["user_id"],
        "username": row["username"],
        "role": row["role"],
        "session_token": token,
    }


def require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_owner(request: Request) -> dict:
    """The owner's own admin login only. `users.role` knows admin and operator, so
    every admin passes require_admin; the write levers that can dark a live
    player feature (flag toggles, the Deep Desert layout pin) key on the primary
    admin row, users.id == 1, which is the owner's login. One definition, used by
    every owner-only route."""
    user = require_admin(request)
    if int(user.get("id") or 0) != 1:
        raise HTTPException(status_code=403, detail="Owner access required")
    return user


def require_csrf(request: Request, user: dict):
    csrf_header = request.headers.get("X-CSRF-Token", "")
    expected = generate_csrf_token(user["session_token"])
    if not hmac.compare_digest(csrf_header, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def audit_log(user_id: int | None, username: str | None, action: str, target: str | None, ip: str, details: str | None = None, success: bool = True):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (user_id, username, action, target, ip_address, details, success) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, action, target, ip, details, 1 if success else 0),
    )
    conn.commit()
    conn.close()


def recent_action_count(action: str, since_utc: str, targets: list | None = None,
                        success_only: bool = True) -> int:
    """Windowed audit-event count for rate limiting. ``since_utc`` is in the
    audit_log.timestamp format ('YYYY-MM-DD HH:MM:SS', UTC). ``targets`` filters
    to a set of target values (e.g. a caller's own account_ids); None = any."""
    conn = get_db()
    try:
        q = "SELECT COUNT(*) AS n FROM audit_log WHERE action = ? AND timestamp >= ?"
        params = [action, since_utc]
        if targets:
            q += " AND target IN (%s)" % ",".join("?" * len(targets))
            params.extend(str(t) for t in targets)
        if success_only:
            q += " AND success = 1"
        row = conn.execute(q, params).fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row else 0


def recent_action_rows(action: str, account_ids: list, since_utc: str,
                       limit: int = 20) -> list:
    """Windowed audit rows for one action, newest first, scoped to a caller's own
    account ids (audit_log.target). Read-only; same table and timestamp format as
    recent_action_count. Returns timestamp/target/details straight from the row --
    the caller decides what of `details` is safe to surface."""
    if not account_ids:
        return []
    conn = get_db()
    try:
        q = ("SELECT timestamp, target, details FROM audit_log "
             "WHERE action = ? AND timestamp >= ? AND target IN (%s) "
             "ORDER BY timestamp DESC, id DESC LIMIT ?"
             % ",".join("?" * len(account_ids)))
        params = [action, since_utc]
        params.extend(str(a) for a in account_ids)
        params.append(int(limit))
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
