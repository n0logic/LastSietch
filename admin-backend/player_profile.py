"""Public player directory + DM visibility (social layer follow-up).

Default PUBLIC: every linked player (ls_account_links) is discoverable and
cold-DMable UNLESS they opted out (portal_player_profile.listed = 0). All state
lives in admin.db; account_id comes from the session and is NEVER emitted to
clients (char_name is the only public handle). Zero game-DB touch.
"""
from datetime import datetime, timezone

from database import get_db

BLURB_MAX = 140


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_profile(account_id: int) -> dict:
    """The caller's own profile. Defaults to listed=True (public) when no row."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT listed, blurb, char_name FROM portal_player_profile "
            "WHERE account_id = ?",
            (int(account_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"listed": True, "blurb": "", "char_name": None}
    return {
        "listed": bool(row["listed"]),
        "blurb": row["blurb"] or "",
        "char_name": row["char_name"],
    }


def set_profile(account_id: int, *, char_name, listed: bool, blurb) -> dict:
    blurb = (blurb or "").strip()[:BLURB_MAX] or None
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO portal_player_profile
                   (account_id, char_name, listed, blurb, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                   char_name  = excluded.char_name,
                   listed     = excluded.listed,
                   blurb      = excluded.blurb,
                   updated_at = excluded.updated_at""",
            (int(account_id), char_name, 1 if listed else 0, blurb, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_profile(account_id)


def find_by_char_name(char_name: str) -> list[dict]:
    """Directory rows whose character name matches, case-insensitively.

    The admin surfaces show char_name and never an account_id (same rule as the
    portal), so a moderator can only name a player by their handle. The caller
    refuses an ambiguous match rather than guessing which one was meant."""
    name = (char_name or "").strip()
    if not name:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT account_id, char_name, listed, blurb, updated_at "
            "  FROM portal_player_profile WHERE char_name = ? COLLATE NOCASE "
            " ORDER BY account_id",
            (name,),
        ).fetchall()
    finally:
        conn.close()
    return [{"account_id": r["account_id"], "char_name": r["char_name"],
             "listed": bool(r["listed"]), "blurb": r["blurb"] or "",
             "updated_at": r["updated_at"]} for r in rows]


def admin_set_listed(account_id: int, listed: bool) -> dict:
    """Moderation visibility switch. Writes ONLY `listed` and `updated_at`.

    Deliberately not set_profile: that one writes char_name and blurb from its
    arguments, so a moderator hiding a profile through it would blank the blurb
    the player wrote, and the takedown would be unrecoverable rather than
    reversible. Plain UPDATE: the caller resolves an existing row first (the
    takedown route 404s on none and 409s on more than one), so there is nothing
    to insert here and a row without char_name must never be created."""
    conn = get_db()
    try:
        conn.execute(
            """UPDATE portal_player_profile
                  SET listed = ?, updated_at = ?
                WHERE account_id = ?""",
            (1 if listed else 0, _now(), int(account_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return get_profile(account_id)


def is_listed(account_id: int) -> bool:
    """False ONLY if the player explicitly opted out (listed=0). No row = public."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT listed FROM portal_player_profile WHERE account_id = ?",
            (int(account_id),),
        ).fetchone()
    finally:
        conn.close()
    return True if row is None else bool(row["listed"])


def list_public(q=None, limit: int = 100) -> list:
    """Public directory: one row per linked, not-opted-out player (their newest
    linked character). Returns char_name + blurb only, NEVER account_id. Optional
    case-insensitive name search."""
    q = (q or "").strip()
    conn = get_db()
    try:
        sql = """
            SELECT l.character_name AS char_name,
                   COALESCE(p.blurb, '') AS blurb
            FROM ls_account_links l
            JOIN (SELECT account_id, MAX(linked_at) AS mx
                    FROM ls_account_links
                   WHERE revoked_at IS NULL
                   GROUP BY account_id) nk
              ON nk.account_id = l.account_id AND nk.mx = l.linked_at
            LEFT JOIN portal_player_profile p ON p.account_id = l.account_id
            WHERE l.revoked_at IS NULL
              AND COALESCE(p.listed, 1) = 1
        """
        args = []
        if q:
            sql += " AND l.character_name LIKE ? COLLATE NOCASE"
            args.append(f"%{q}%")
        sql += (" GROUP BY l.account_id"
                " ORDER BY char_name COLLATE NOCASE LIMIT ?")
        args.append(int(limit))
        import portal_identity
        rows = conn.execute(sql.replace('ls_account_links', portal_identity.link_table(conn)), args).fetchall()
    finally:
        conn.close()
    return [{"char_name": r["char_name"], "blurb": r["blurb"]} for r in rows]
