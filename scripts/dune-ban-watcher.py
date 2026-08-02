#!/usr/bin/env python3
# Last Sietch Dune ban-watcher (single-shot). systemd timer fires this every 30s
# (see scripts/lastsietch-ban-watcher.timer); the script is NOT a daemon loop.
#
# Per tick:
#   1. exec /root/dune-ipban.py reapply -- refresh the player->IP map and
#      (re)apply an iptables source-IP DROP for every recent IP of every active,
#      non-expired ban. This both enforces bans on rejoin/new-IP and restores
#      rules after a node reboot (iptables rules are not persistent). Replaces
#      the dead RMQ KickPlayer path (no-op on this GA build).
#   2. UPDATE lsadmin.bans SET active=false WHERE active AND expires_at < NOW();
#      for each newly-expired ban, exec /root/dune-ipban.py undrop --account-id
#      <aid> to lift its firewall drops.
#
# Logs to /var/log/lastsietch-ban-watcher.log ONLY on action (drops applied or a row
# expired). Quiet ticks emit nothing; keeps the log readable as a moderation
# trail rather than a heartbeat.
#
#
# section 2b (kick mechanism revised 2026-06-02 from RMQ -> iptables IP-drop;
#).
import json
import subprocess
import sys
from datetime import datetime, timezone

LOG = "/var/log/lastsietch-ban-watcher.log"
DQ = "/root/dq.sh"
IPBAN = "/root/dune-ipban.py"
TIMEOUT_DB_S = 30
TIMEOUT_IPBAN_S = 150   # reapply may run dune-ip-detect (kubectl logs fan-out)

EXPIRE_BANS_SQL = """
SET search_path TO lsadmin, dune, public;
WITH expired AS (
  UPDATE lsadmin.bans
     SET active        = false,
         unbanned_at   = NOW(),
         unban_reason  = 'auto-expired',
         unbanned_by   = 'ban-watcher'
   WHERE active = true
     AND expires_at IS NOT NULL
     AND expires_at <= NOW()
  RETURNING id, fls_id, account_id
)
SELECT coalesce(json_agg(json_build_object(
  'id',         id,
  'fls_id',     fls_id,
  'account_id', account_id
)), '[]'::json) FROM expired;
"""


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_line(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (now_ts(), msg))
    except OSError as e:
        print("warn: log write failed: %s" % e, file=sys.stderr)


def dq_json(sql):
    try:
        out = subprocess.run(
            [DQ, "-tAc", sql],
            capture_output=True, text=True, timeout=TIMEOUT_DB_S, check=False)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if out.returncode != 0:
        return None, (out.stderr or out.stdout).strip()[:300]
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
        return None, "parse: %s; raw=%s" % (e, raw[:200])


def ipban(cmd_args):
    """Run dune-ipban.py with args; return (ok, detail)."""
    try:
        r = subprocess.run([IPBAN] + cmd_args, capture_output=True, text=True,
                           timeout=TIMEOUT_IPBAN_S, check=False)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return r.returncode == 0, (r.stdout or r.stderr).strip()[:300]


def main():
    # 1. Re-assert drops for all active bans (enforces on rejoin / new IP and
    #    restores rules after a node reboot). reapply is quiet on a no-op.
    ok, detail = ipban(["reapply"])
    if not ok:
        log_line("ERROR reapply: %s" % detail)
    else:
        try:
            d = json.loads(detail)
            if d.get("new_drops"):
                log_line("reapply active_bans=%s new_drops=%s"
                         % (d.get("active_bans"), d.get("new_drops")))
        except (json.JSONDecodeError, AttributeError):
            pass

    # 2. Expire bans past their deadline and lift their firewall drops.
    expired, err = dq_json(EXPIRE_BANS_SQL)
    if err:
        log_line("ERROR expire-bans: %s" % err)
        sys.exit(1)
    for e in expired:
        aid = e.get("account_id")
        uok, udetail = (ipban(["undrop", "--account-id", str(aid)])
                        if aid is not None else (False, "no account_id"))
        log_line("expire ban_id=%s acct=%s undrop_ok=%s %s"
                 % (e.get("id"), aid, uok, udetail))


if __name__ == "__main__":
    main()
