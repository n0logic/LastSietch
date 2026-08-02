#!/usr/bin/env python3
# Read-only moderation listing for the Last Sietch admin v2 Bans tab. Deployed to
# lastsietch-dune:/root/dune-bans.py and invoked by the relay over SSH via the
# dispatcher tokens `bans-list` (default mode, active bans) and
# `bans-history` (--history, lsadmin.player_actions).
#
# PII: emits FuncomIds + character names. The relay endpoint that proxies this
# is admin-auth-gated.
#
# Active bans default: lsadmin.bans WHERE active=true, joined to
# dune.encrypted_player_state (online_status, last_login_time) and
# dune.player_state (decrypted character_name, online-only view) so the UI can
# show "<name> [Online|Offline] banned by <admin>".
#
# History: lsadmin.player_actions ordered DESC. action_type IN
# ('kick','ban','unban').
import argparse
import json
import subprocess
import sys

DQ = "/root/dq.sh"
TIMEOUT_S = 45

ACTIVE_BANS_SQL = """
SET search_path TO lsadmin, dune, public;
SELECT coalesce(json_agg(json_build_object(
  'id',                b.id,
  'fls_id',            b.fls_id,
  'account_id',        b.account_id,
  'reason',            b.reason,
  'note',              b.note,
  'duration_minutes',  b.duration_minutes,
  'banned_at',         b.banned_at,
  'expires_at',        b.expires_at,
  'banned_by',         b.banned_by,
  'character_name',    COALESCE(ps.character_name,
                                'Account ' || COALESCE(b.account_id::text, '?')),
  'online_status',     COALESCE(eps.online_status::text, 'Unknown'),
  'last_login_time',   eps.last_login_time,
  'expired',           (b.expires_at IS NOT NULL AND b.expires_at <= NOW())
) ORDER BY b.banned_at DESC), '[]'::json)
FROM lsadmin.bans b
LEFT JOIN dune.encrypted_player_state eps ON eps.account_id = b.account_id
LEFT JOIN dune.player_state          ps   ON ps.account_id  = b.account_id
WHERE b.active = true;
"""

HISTORY_SQL = """
SET search_path TO lsadmin, dune, public;
SELECT coalesce(json_agg(json_build_object(
  'id',               t.id,
  'account_id',       t.account_id,
  'fls_id',           t.fls_id,
  'action_type',      t.action_type,
  'reason',           t.reason,
  'note',             t.note,
  'duration_minutes', t.duration_minutes,
  'admin_user',       t.admin_user,
  'created_at',       t.created_at,
  'character_name',   t.character_name
) ORDER BY t.created_at DESC), '[]'::json)
FROM (
  SELECT pa.id, pa.account_id, pa.fls_id, pa.action_type, pa.reason, pa.note,
         pa.duration_minutes, pa.admin_user, pa.created_at,
         COALESCE(ps.character_name,
                  'Account ' || COALESCE(pa.account_id::text, '?')) AS character_name
    FROM lsadmin.player_actions pa
    LEFT JOIN dune.player_state ps ON ps.account_id = pa.account_id
   ORDER BY pa.created_at DESC
   LIMIT {limit}
) t;
"""


def run_psql_json(sql, *extra_args):
    """Run a single read-only SELECT via dq.sh; return parsed JSON or fail
    JSON. dq.sh emits "SET" on its own line before the result; pick the last
    non-empty non-SET line. extra_args are forwarded to psql (e.g. -v key=val)
    so values are bound as variables instead of string-concatenated into SQL."""
    try:
        out = subprocess.run(
            [DQ, "-tA", *extra_args, "-c", sql],
            capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return None, "timeout"

    if out.returncode != 0:
        return None, (out.stderr or out.stdout).strip()[:500]

    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    if not raw:
        raw = "[]"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, "parse: %s; raw=%s" % (e, raw[:300])


def main():
    p = argparse.ArgumentParser(
        description="Read-only listing of lsadmin.bans + lsadmin.player_actions")
    p.add_argument("--history", action="store_true",
                   help="Emit lsadmin.player_actions rows (kick|ban|unban events)")
    p.add_argument("--limit", type=int, default=200,
                   help="Max rows for --history (clamped to 1..1000; default 200)")
    args = p.parse_args()

    if args.history:
        # limit is clamped to a 1..1000 int here, so inlining it into the SQL
        # is injection-safe. psql :var interpolation does not survive the
        # dq.sh / kubectl-exec path, so .format() is the reliable bind.
        limit = max(1, min(1000, args.limit))
        rows, err = run_psql_json(HISTORY_SQL.format(limit=limit))
        if err:
            print(json.dumps({"available": False, "error": err}))
            sys.exit(1)
        print(json.dumps({"available": True, "count": len(rows), "actions": rows}))
        return

    rows, err = run_psql_json(ACTIVE_BANS_SQL)
    if err:
        print(json.dumps({"available": False, "error": err}))
        sys.exit(1)
    print(json.dumps({"available": True, "count": len(rows), "bans": rows}))


if __name__ == "__main__":
    main()
