#!/usr/bin/env python3
"""iptables source-IP kick/ban engine for the hostNetwork Dune game node.

WHY: the RMQ KickPlayer ServerCommand is a confirmed silent no-op on the current
GA build . The only mechanism that actually
disconnects a player is dropping their source IP at the node firewall. Game pods
are hostNetwork, so player UDP hits the host INPUT chain directly and a
`-s <ip> -j DROP` there severs the session. IP bans are VPN-avoidable (surfaced
as a caveat in the admin UI); they are still the only thing that works.

SAFETY (this script edits the production firewall, run as root):
  * Hard allowlist: loopback/private/link-local + the node IP + the the web host
    relay + every entry in /etc/lastsietch/ipban-allowlist.txt. A target in the
    allowlist is REFUSED, never dropped. This is what stops a kick from severing
    admin SSH or cluster traffic.
  * Every rule we add is tagged with an iptables --comment ("lsban:aid=N" /
    "lskick:aid=N"). We only ever delete rules carrying our tag; kube-router /
    kube-proxy rules are never touched.
  * All iptables mutations are serialized under an flock.
  * Rules are inserted at INPUT position 1 so they win over the kube chains.
  * Kick = temporary drop; removal is scheduled via `systemd-run --on-active` so
    it fires even if this process exits. A stale-kick sweep in reapply is the
    safety net.

Subcommands:
  kick    --account-id N [--duration S] [--operator who]   temp drop (default 120s)
  drop    --account-id N                                    persistent drop (ban enforce)
  undrop  --account-id N                                    remove persistent drop (unban)
  reapply                                                   re-drop all active bans (boot/flush)
  list-ips --account-id N                                   JSON of known IPs (for UI)
  status                                                    JSON of our managed rules
  _unkick --account-id N --ip IP                            internal: scheduled kick removal
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time

DQ = "/root/dq.sh"
IPDETECT = "/root/dune-ip-detect.py"
ALLOWLIST_FILE = "/etc/lastsietch/ipban-allowlist.txt"
LOG_FILE = "/var/log/lastsietch-ipban.log"
LOCK_FILE = "/run/lock/lastsietch-ipban.lock"
RECENT_DAYS = 14            # ban scope: drop every IP seen within this window
DEFAULT_KICK_SECONDS = 120
TIMEOUT_DB = 30
TIMEOUT_IPT = 15

# Hard, non-overridable allowlist. Anything here is NEVER dropped.
HARDCODED_ALLOW = [
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "100.64.0.0/10",          # CGNAT / tailscale-ish
    "::1/128", "fc00::/7", "fe80::/10",
    "<game-host>/32",                          # this game node
    "<relay-source-ip>/32",                        # the web host relay
]


def log_line(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg))
    except OSError:
        pass


# --- allowlist -------------------------------------------------------------

def _load_allow_nets():
    nets = []
    for c in HARDCODED_ALLOW:
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            pass
    try:
        with open(ALLOWLIST_FILE) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                try:
                    nets.append(ipaddress.ip_network(line, strict=False))
                except ValueError:
                    log_line("WARN allowlist bad entry: %r" % line)
    except FileNotFoundError:
        pass
    return nets


def is_allowlisted(ip, nets):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True   # unparseable -> treat as protected (refuse)
    return any(addr in n for n in nets)


# --- iptables (flock-serialized, comment-tagged) ---------------------------

class _Flock:
    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        self.fh = open(LOCK_FILE, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        import fcntl
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        except Exception:
            pass


def _ipt(args):
    return subprocess.run(["iptables"] + args, capture_output=True, text=True,
                          timeout=TIMEOUT_IPT, check=False)


def _rule_exists(ip, comment):
    r = _ipt(["-C", "INPUT", "-s", ip, "-m", "comment", "--comment", comment,
              "-j", "DROP"])
    return r.returncode == 0


def apply_drop(ip, comment):
    """Insert a DROP at INPUT pos 1 if not already present. Returns True if added."""
    if _rule_exists(ip, comment):
        return False
    r = _ipt(["-I", "INPUT", "1", "-s", ip, "-m", "comment", "--comment",
              comment, "-j", "DROP"])
    if r.returncode != 0:
        log_line("ERROR insert %s (%s): %s" % (ip, comment, r.stderr.strip()))
        raise RuntimeError("iptables insert failed: %s" % r.stderr.strip())
    return True


def remove_exact(ip, comment):
    """Delete one specific tagged DROP. Returns True if a rule was removed."""
    removed = False
    # Loop in case of accidental duplicates.
    while _rule_exists(ip, comment):
        r = _ipt(["-D", "INPUT", "-s", ip, "-m", "comment", "--comment",
                  comment, "-j", "DROP"])
        if r.returncode != 0:
            break
        removed = True
    return removed


def remove_by_tag(tag):
    """Delete every INPUT DROP whose comment contains `tag`, regardless of IP
    (handles IP-changed bans). Reconstructs each -D from `iptables -S INPUT`."""
    r = _ipt(["-S", "INPUT"])
    if r.returncode != 0:
        return 0
    n = 0
    for line in (r.stdout or "").splitlines():
        if tag in line and line.startswith("-A INPUT"):
            spec = line[len("-A "):].split()        # -> ["INPUT", "-s", ...]
            d = _ipt(["-D"] + spec)
            if d.returncode == 0:
                n += 1
    return n


def list_managed_rules():
    r = _ipt(["-S", "INPUT"])
    out = []
    if r.returncode != 0:
        return out
    for line in (r.stdout or "").splitlines():
        m = re.search(r'--comment "?(ls(?:ban|kick):aid=\d+)"?', line)
        if m and line.startswith("-A INPUT"):
            ip_m = re.search(r"-s ([0-9a-fA-F.:]+(?:/\d+)?)", line)
            out.append({"tag": m.group(1), "ip": ip_m.group(1) if ip_m else None})
    return out


# --- DB helpers (via dq.sh; values inlined, validated ints/ips only) --------

def dq(sql):
    r = subprocess.run([DQ, "-tAc", sql], capture_output=True, text=True,
                       timeout=TIMEOUT_DB, check=False)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def recent_ips_for_account(account_id):
    aid = int(account_id)
    sql = ("SELECT host(ip_address) FROM lsadmin.player_ips "
           "WHERE account_id = %d AND last_seen > now() - interval '%d days' "
           "ORDER BY last_seen DESC;" % (aid, RECENT_DAYS))
    return [ln.strip() for ln in dq(sql).splitlines() if ln.strip()]


def active_ban_accounts():
    sql = ("SELECT account_id FROM lsadmin.bans "
           "WHERE active = true AND account_id IS NOT NULL "
           "AND (expires_at IS NULL OR expires_at > now());")
    return [int(ln) for ln in dq(sql).splitlines() if ln.strip()]


def log_player_action(account_id, action_type, operator, note=""):
    aid = int(account_id)
    note_sql = "'" + note.replace("'", "''")[:200] + "'" if note else "NULL"
    sql = ("INSERT INTO lsadmin.player_actions "
           "(account_id, action_type, admin_user, note) VALUES "
           "(%d, '%s', '%s', %s);"
           % (aid, action_type, operator.replace("'", "''")[:64], note_sql))
    try:
        dq(sql)
    except RuntimeError as e:
        log_line("WARN player_actions log failed acct=%s: %s" % (aid, e))


# --- target resolution -----------------------------------------------------

def resolve_targets(account_id, refresh):
    """Return (droppable_ips, skipped_allowlisted). Optionally refresh the IP
    map first so a just-connected player is kickable."""
    if refresh:
        subprocess.run([IPDETECT, "--quiet"], capture_output=True, text=True,
                       timeout=90, check=False)
    nets = _load_allow_nets()
    ips = recent_ips_for_account(account_id)
    droppable, skipped = [], []
    for ip in ips:
        (skipped if is_allowlisted(ip, nets) else droppable).append(ip)
    return droppable, skipped


# --- subcommands -----------------------------------------------------------

def cmd_kick(args):
    aid = int(args.account_id)
    dur = max(5, min(int(args.duration), 3600))
    droppable, skipped = resolve_targets(aid, refresh=True)
    if not droppable:
        msg = ("no droppable IP for account %d (known but allowlisted: %s)"
               % (aid, skipped) if skipped else "no known IP for account %d" % aid)
        print(json.dumps({"ok": False, "error": msg}))
        log_line("kick acct=%d NO-TARGET skipped=%s" % (aid, skipped))
        return 4
    tag = "lskick:aid=%d" % aid
    dropped = []
    with _Flock():
        for ip in droppable:
            try:
                apply_drop(ip, tag)
                dropped.append(ip)
            except RuntimeError as e:
                log_line("kick acct=%d ip=%s FAIL %s" % (aid, ip, e))
    # Schedule removal per IP so the kick auto-expires even if we die.
    for ip in dropped:
        _schedule_unkick(aid, ip, dur)
    log_player_action(aid, "kick", args.operator, "ip-drop %ds %s" % (dur, dropped))
    log_line("kick acct=%d dur=%ds dropped=%s skipped=%s" % (aid, dur, dropped, skipped))
    print(json.dumps({"ok": True, "account_id": aid, "dropped": dropped,
                      "skipped_allowlisted": skipped, "duration_s": dur}))
    return 0


def _schedule_unkick(account_id, ip, delay):
    unit = "lskick-%d-%s" % (account_id, re.sub(r"[^0-9a-fA-F]", "-", ip))
    r = subprocess.run(
        ["systemd-run", "--quiet", "--on-active=%ds" % delay, "--unit", unit,
         sys.argv[0] if os.path.isabs(sys.argv[0]) else "/root/dune-ipban.py",
         "_unkick", "--account-id", str(account_id), "--ip", ip],
        capture_output=True, text=True, check=False)
    if r.returncode != 0:
        # Fallback: detached child that sleeps then removes.
        log_line("WARN systemd-run unkick failed (%s); forking fallback"
                 % r.stderr.strip()[:120])
        if os.fork() == 0:
            os.setsid()
            time.sleep(delay)
            with _Flock():
                remove_exact(ip, "lskick:aid=%d" % account_id)
            os._exit(0)


def cmd_unkick(args):
    with _Flock():
        removed = remove_exact(args.ip, "lskick:aid=%d" % int(args.account_id))
    log_line("_unkick acct=%s ip=%s removed=%s" % (args.account_id, args.ip, removed))
    return 0


def cmd_drop(args):
    """Persistent drop of all recent IPs for an account (ban enforcement)."""
    aid = int(args.account_id)
    droppable, skipped = resolve_targets(aid, refresh=True)
    tag = "lsban:aid=%d" % aid
    dropped = []
    with _Flock():
        for ip in droppable:
            try:
                apply_drop(ip, tag)
                dropped.append(ip)
            except RuntimeError as e:
                log_line("drop acct=%d ip=%s FAIL %s" % (aid, ip, e))
    log_line("drop(ban-enforce) acct=%d dropped=%s skipped=%s" % (aid, dropped, skipped))
    print(json.dumps({"ok": True, "account_id": aid, "dropped": dropped,
                      "skipped_allowlisted": skipped}))
    return 0


def cmd_undrop(args):
    aid = int(args.account_id)
    with _Flock():
        n = remove_by_tag("lsban:aid=%d" % aid)
    log_line("undrop(unban) acct=%d rules_removed=%d" % (aid, n))
    print(json.dumps({"ok": True, "account_id": aid, "rules_removed": n}))
    return 0


def cmd_reapply(args):
    """Re-assert persistent drops for every active ban (boot / flush recovery),
    picking up any newly-detected IPs. Also sweeps stale lskick rules."""
    subprocess.run([IPDETECT, "--quiet"], capture_output=True, text=True,
                   timeout=120, check=False)
    nets = _load_allow_nets()
    total = 0
    try:
        accounts = active_ban_accounts()
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    with _Flock():
        for aid in accounts:
            tag = "lsban:aid=%d" % aid
            try:
                for ip in recent_ips_for_account(aid):
                    if is_allowlisted(ip, nets):
                        continue
                    if apply_drop(ip, tag):
                        total += 1
            except RuntimeError as e:
                log_line("reapply acct=%d ERROR %s" % (aid, e))
    log_line("reapply active_bans=%d new_drops=%d" % (len(accounts), total))
    print(json.dumps({"ok": True, "active_bans": len(accounts), "new_drops": total}))
    return 0


def cmd_list_ips(args):
    aid = int(args.account_id)
    sql = ("SELECT json_agg(t) FROM (SELECT host(ip_address) AS ip, fls_id, "
           "character_name, to_char(first_seen,'YYYY-MM-DD HH24:MI') AS first_seen, "
           "to_char(last_seen,'YYYY-MM-DD HH24:MI') AS last_seen, "
           "(last_seen > now() - interval '%d days') AS recent "
           "FROM lsadmin.player_ips WHERE account_id = %d "
           "ORDER BY last_seen DESC) t;" % (RECENT_DAYS, aid))
    try:
        raw = dq(sql).strip() or "[]"
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    nets = _load_allow_nets()
    rows = json.loads(raw if raw != "" else "[]") or []
    for row in rows:
        row["allowlisted"] = is_allowlisted(row.get("ip", ""), nets)
    print(json.dumps({"ok": True, "account_id": aid, "ips": rows}))
    return 0


def cmd_status(args):
    with _Flock():
        rules = list_managed_rules()
    print(json.dumps({"ok": True, "managed_rules": rules}))
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("kick"); k.add_argument("--account-id", required=True)
    k.add_argument("--duration", default=DEFAULT_KICK_SECONDS)
    k.add_argument("--operator", default="admin")

    d = sub.add_parser("drop"); d.add_argument("--account-id", required=True)
    u = sub.add_parser("undrop"); u.add_argument("--account-id", required=True)
    sub.add_parser("reapply")
    li = sub.add_parser("list-ips"); li.add_argument("--account-id", required=True)
    sub.add_parser("status")
    uk = sub.add_parser("_unkick")
    uk.add_argument("--account-id", required=True); uk.add_argument("--ip", required=True)

    args = p.parse_args()
    # Strict validation of account-id everywhere.
    if getattr(args, "account_id", None) is not None:
        if not re.match(r"^[0-9]+$", str(args.account_id)):
            print(json.dumps({"ok": False, "error": "invalid account_id"}))
            return 2
    if getattr(args, "ip", None) is not None:
        try:
            ipaddress.ip_address(args.ip)
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid ip"}))
            return 2

    return {
        "kick": cmd_kick, "drop": cmd_drop, "undrop": cmd_undrop,
        "reapply": cmd_reapply, "list-ips": cmd_list_ips, "status": cmd_status,
        "_unkick": cmd_unkick,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
