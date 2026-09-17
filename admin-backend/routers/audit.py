from fastapi import APIRouter, Request

from auth import require_admin
from database import get_db

router = APIRouter(prefix="/api/audit")


@router.get("")
async def get_audit_log(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    username: str | None = None,
    action: str | None = None,
):
    require_admin(request)

    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)

    where = "WHERE 1=1"
    params = []

    if username:
        where += " AND username = ?"
        params.append(username)
    if action:
        where += " AND action = ?"
        params.append(action)

    conn = get_db()

    count_row = conn.execute("SELECT COUNT(*) as cnt FROM audit_log " + where, params).fetchone()
    total = count_row["cnt"] if count_row else 0

    query = "SELECT * FROM audit_log " + where + " ORDER BY id DESC LIMIT ? OFFSET ?"
    rows = conn.execute(query, params + [limit, offset]).fetchall()
    conn.close()

    return {"entries": [dict(r) for r in rows], "total": total}
