#!/usr/bin/env python3
"""Harvest player source IPs from game-pod LogNet lines into lsadmin.player_ips.

Each net-connection log line carries RemoteAddr + UniqueId Fls + AccountId on ONE
line, e.g.:
  LogNet: ... RemoteAddr: <your-ip>:61186, ... UniqueId: Fls:36A0226630A5875.
          NetConnectionInfo: (AccountId: 3013, TravelFlowId: ...)
so we map account_id -> ip directly (no two-line RemoteAddr/Login correlation).

Game pods are hostNetwork, so RemoteAddr is the REAL external client IP (no SNAT)
- exactly the IP an `iptables -I INPUT -s <ip> -j DROP` on this node will match.

Runs on lastsietch-dune as root. Idempotent upsert; bumps last_seen. Intended for a
short-interval systemd timer + an on-demand pre-kick refresh (dune-ipban.py calls
this before resolving a target). Read-only against the cluster; only writes
lsadmin.player_ips via dq.sh.

Usage: dune-ip-detect.py [--tail N] [--quiet]
"""
import ipaddress
import re
import subprocess
import sys

DQ = "/root/dq.sh"
DEFAULT_TAIL = 6000
TIMEOUT_KUBECTL = 60
TIMEOUT_DB = 30

RE_IP = re.compile(r"RemoteAddr:\s*([0-9.]+):\d+")
RE_AID = re.compile(r"AccountId:\s*(\d+)")
RE_FLS = re.compile(r"Fls:([0-9A-Fa-f]+)")


def sh(cmd, timeout, stdin=None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, input=stdin, check=False)


def detect_namespace():
    """Find the live battlegroup namespace by locating the running mq-game pod.
    More robust than hardcoding (the NS changes on every battlegroup rebuild)."""
    r = sh(["kubectl", "get", "pods", "-A", "--no-headers"], TIMEOUT_KUBECTL)
    for line in (r.stdout or "").splitlines():
        f = line.split()
        if len(f) >= 4 and "mq-game-sts-0" in f[1] and "Running" in line:
            return f[0]
    return None


def game_pods(ns):
    """All game-server pods (sg-* = survival / deep-desert / overmap / story /
    dungeon / ecolab instances). These are the only pods that log player conns."""
    r = sh(["kubectl", "get", "pods", "-n", ns, "--no-headers"], TIMEOUT_KUBECTL)
    pods = []
    for line in (r.stdout or "").splitlines():
        name = line.split()[0] if line.split() else ""
        if "-sg-" in name and "Running" in line:
            pods.append(name)
    return pods


def harvest(ns, pod, tail):
    """Return {(account_id, ip): fls_id} for one pod's recent log tail."""
    found = {}
    r = sh(["kubectl", "logs", "-n", ns, pod, "--tail", str(tail)],
           TIMEOUT_KUBECTL)
    for line in (r.stdout or "").splitlines():
        if "RemoteAddr:" not in line or "AccountId:" not in line:
            continue
        m_ip, m_aid = RE_IP.search(line), RE_AID.search(line)
        if not (m_ip and m_aid):
            continue
        ip = m_ip.group(1)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        # Only store routable public IPs; private/loopback/link-local cannot be
        # a remote player and must never reach the DROP path anyway.
        if not addr.is_global:
            continue
        aid = int(m_aid.group(1))
        # AccountId 0 = pre-auth / unauthenticated connection state; it is not a
        # real player and maps many unrelated IPs onto one id. Never store it.
        if aid <= 0:
            continue
        m_fls = RE_FLS.search(line)
        fls = m_fls.group(1) if m_fls else None
        # Last write wins -> newest fls label for the pair within this tail.
        found[(aid, ip)] = fls or found.get((aid, ip))
    return found


def upsert(rows):
    """rows: dict {(aid, ip): fls}. One multi-row INSERT ... ON CONFLICT.
    Values are inlined (aid is int, ip is ipaddress-validated, fls is hex) because
    psql -v binding does not interpolate through dq.sh."""
    if not rows:
        return 0, None
    values = []
    for (aid, ip), fls in rows.items():
        fls_sql = "NULL" if not fls else "'%s'" % re.sub(r"[^0-9A-Fa-f]", "", fls)
        values.append("(%d, '%s'::inet, %s, now(), now())" % (int(aid), ip, fls_sql))
    sql = (
        "INSERT INTO lsadmin.player_ips "
        "(account_id, ip_address, fls_id, first_seen, last_seen) VALUES "
        + ", ".join(values) +
        " ON CONFLICT (account_id, ip_address) DO UPDATE SET "
        "last_seen = now(), "
        "fls_id = COALESCE(lsadmin.player_ips.fls_id, EXCLUDED.fls_id);"
    )
    r = sh([DQ, "-tAc", sql], TIMEOUT_DB)
    if r.returncode != 0:
        return 0, (r.stderr or r.stdout).strip()[:300]
    return len(rows), None


def main():
    tail = DEFAULT_TAIL
    quiet = "--quiet" in sys.argv
    if "--tail" in sys.argv:
        try:
            tail = int(sys.argv[sys.argv.index("--tail") + 1])
        except (ValueError, IndexError):
            pass

    ns = detect_namespace()
    if not ns:
        print("error: could not detect live battlegroup namespace", file=sys.stderr)
        sys.exit(1)

    pods = game_pods(ns)
    if not pods:
        print("error: no running game (sg-*) pods in %s" % ns, file=sys.stderr)
        sys.exit(1)

    rows = {}
    for pod in pods:
        rows.update(harvest(ns, pod, tail))

    n, err = upsert(rows)
    if err:
        print("error: upsert failed: %s" % err, file=sys.stderr)
        sys.exit(1)
    if not quiet:
        accts = len({aid for (aid, _ip) in rows})
        print("ok: %d (account,ip) pairs across %d accounts from %d pods (ns=%s)"
              % (n, accts, len(pods), ns))


if __name__ == "__main__":
    main()
