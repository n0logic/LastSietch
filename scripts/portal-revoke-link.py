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
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


def disable_profile(conn, identity, profile_id, reason, operator):
    if re.fullmatch('[a-f0-9]{32}', profile_id) is None:
        raise ValueError('invalid profile identity')
    try:
        conn.execute('BEGIN IMMEDIATE')
        identity._schema(conn, create=False)
        if not identity.credentials_installed(conn):
            raise RuntimeError('credential schema required for profile disable')
        if conn.execute('SELECT 1 FROM portal_profiles WHERE id=?', (profile_id,)).fetchone() is None:
            raise ValueError('profile not found')
        changed = conn.execute("UPDATE portal_profiles SET disabled_at=datetime('now') WHERE id=? AND disabled_at IS NULL", (profile_id,)).rowcount
        if changed:
            conn.execute('UPDATE portal_profile_accounts SET session_version=session_version+1 WHERE profile_id=?', (profile_id,))
            conn.execute('UPDATE portal_auth_sessions SET revoked_at=? WHERE profile_id=? AND revoked_at IS NULL', (time.time(), profile_id))
            conn.execute("INSERT INTO audit_log(username,action,target,ip_address,details,success) VALUES (?,'portal_profile_disable',?,'local',?,1)",
                         (operator, profile_id, json.dumps({'reason': reason})))
        conn.commit()
        print('disabled profile=' + profile_id + ' changed=' + str(changed))
    except BaseException:
        conn.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Revoke legacy links by Discord ID, or disable a whole profile with profile:<32 hex ID>")
    parser.add_argument("discord_id")
    parser.add_argument("reason")
    parser.add_argument("--by", default=os.environ.get("USER", "operator"))
    args = parser.parse_args()

    db_path = os.environ.get("LASTSIETCH_DB_PATH", "/opt/lastsietch-admin/admin.db")
    conn = sqlite3.connect(db_path)
    try:
        candidates = (Path(__file__).resolve().parent / 'portal_identity.py',
                      Path(__file__).resolve().parents[1] / 'admin-backend/portal_identity.py')
        source = next((path for path in candidates if path.is_file()), None)
        identity = None
        if source is not None:
            spec = importlib.util.spec_from_file_location('revoke_profile_identity', source)
            identity = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(identity)
            if args.discord_id.startswith('profile:'):
                disable_profile(conn, identity, args.discord_id[8:], args.reason, args.by)
                return 0
            profile_change = identity.begin_legacy_links(conn, args.discord_id, require_profiles=False)
        else:
            conn.execute('BEGIN IMMEDIATE')
            present = conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN "
                                   "('portal_profiles','portal_profile_providers','portal_profile_accounts')").fetchone()[0]
            if present:
                raise RuntimeError('profile support is required for this database')
        cur = conn.execute(
            """UPDATE ls_account_links
                  SET revoked_at = datetime('now'),
                      revoked_by = ?,
                      revoke_reason = ?
                WHERE discord_id = ? AND revoked_at IS NULL""",
            (args.by, args.reason, args.discord_id),
        )
        if identity is not None:
            identity.finish_legacy_links(conn, profile_change)
        conn.commit()
        print(f"revoked {cur.rowcount} active link row(s) for discord_id={args.discord_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
