#!/usr/bin/env python3
"""Hard-revoke a portal account link.

Usage:
    portal-revoke-link.py <discord_id> "<reason>" [--by <operator>]

Sets revoked_at=now, revoked_by, revoke_reason on every active link row for
the given discord_id. Idempotent. Next portal request from any session for
this discord_id is forced back to OAuth.

Reads LASTSIETCH_DB_PATH from env (defaults to /opt/lastsietch-admin/admin.db, matching
admin-backend config).
"""
import argparse
import os
import sqlite3
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Revoke portal account link by discord_id")
    parser.add_argument("discord_id")
    parser.add_argument("reason")
    parser.add_argument("--by", default=os.environ.get("USER", "operator"))
    args = parser.parse_args()

    db_path = os.environ.get("LASTSIETCH_DB_PATH", "/opt/lastsietch-admin/admin.db")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """UPDATE ls_account_links
                  SET revoked_at = datetime('now'),
                      revoked_by = ?,
                      revoke_reason = ?
                WHERE discord_id = ? AND revoked_at IS NULL""",
            (args.by, args.reason, args.discord_id),
        )
        conn.commit()
        print(f"revoked {cur.rowcount} active link row(s) for discord_id={args.discord_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
