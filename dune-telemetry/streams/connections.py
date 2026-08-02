"""
Phase 1 - connections stream.

Parses the survival game pod's logs for inbound player connection events,
geolocates each new IP, and stores (conn_epoch, ip, country, country_code).
Mirrors the old lastsietch-stats sampler so a parallel-run diffs cleanly against
stats.db.

Connection events are `NotifyAcceptingConnection accepted from: <IP>:` lines.
On the GA build most of those carry the server's own IP (see DUNE_SERVER_IP) -
inter-pod UDuneS2sIpConnection traffic, not players - so SERVER_IP is filtered
alongside the RFC1918 private ranges. conn_epoch is parsed from the log line's
own timestamp (UTC), which keeps the UNIQUE(conn_epoch, ip) dedup working
across the overlapping 6-minute log windows.
"""

from __future__ import annotations
import os

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
import re

log = logging.getLogger("telemetry.connections")

# Server's own public IP - `accepted from:` lines for this address are
# server-to-server connections, not players. Mirrors SERVER_IP in the old
# lastsietch-stats sampler.
SERVER_IP = os.environ.get("DUNE_SERVER_IP", "")   # your game host public IP

# Funcom log connection-accept line:
#   [YYYY.MM.DD-HH.MM.SS:nnn]...NotifyAcceptingConnection accepted from: <IP>:
_CONN_RE = re.compile(
    r"\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2}):\d+\]"
    r".*accepted from:\s+(\d+\.\d+\.\d+\.\d+):"
)

_PRIVATE_PREFIXES = ("10.", "127.", "192.168.", "0.")


def _is_private(ip):
    if ip == SERVER_IP:
        return True
    if ip.startswith(_PRIVATE_PREFIXES):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            return 16 <= second <= 31
        except (ValueError, IndexError):
            return False
    return False


def _parse_conns(logtext):
    """Return a set of (conn_epoch, ip) for public player connection events."""
    found = set()
    for line in logtext.splitlines():
        m = _CONN_RE.search(line)
        if not m:
            continue
        y, mo, d, h, mi, s, ip = m.groups()
        if _is_private(ip):
            continue
        epoch = int(datetime(int(y), int(mo), int(d), int(h), int(mi), int(s),
                             tzinfo=timezone.utc).timestamp())
        found.add((epoch, ip))
    return found


def _geoip_lookup(ctx, ip):
    """Resolve an IP to (country, country_code). Failures are non-fatal."""
    try:
        url = ctx.config.geoip_api + ip
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("country"), data.get("countryCode")
    except Exception as exc:  # noqa: BLE001 - geoip failure must not break the stream
        log.warning("geoip lookup for %s failed: %s", ip, exc)
        return None, None


def _cached_geoip(ctx, ip):
    row = ctx.store.execute(
        "SELECT country, country_code FROM geoip_cache WHERE ip=?", (ip,)
    ).fetchone()
    return row


def run(ctx):
    if not ctx.config.game_pod:
        log.warning("connections: GAME_POD not set, skipping")
        return
    now = int(time.time())
    logtext = ctx.gamedb.kubectl_logs(ctx.config.game_pod, since="6m")
    conns = _parse_conns(logtext)

    new_conns = 0
    for epoch, ip in conns:
        cached = _cached_geoip(ctx, ip)
        if cached is not None:
            country, country_code = cached["country"], cached["country_code"]
        else:
            country, country_code = _geoip_lookup(ctx, ip)
            ctx.store.execute(
                "INSERT OR REPLACE INTO geoip_cache"
                "(ip, country, country_code, resolved_ts) VALUES(?,?,?,?)",
                (ip, country, country_code, now))
        cur = ctx.store.execute(
            "INSERT OR IGNORE INTO connections"
            "(conn_epoch, ip, country, country_code) VALUES(?,?,?,?)",
            (epoch, ip, country, country_code))
        new_conns += cur.rowcount

    log.info("connections: %d events seen, %d new rows", len(conns), new_conns)


STREAM = {"name": "connections", "interval_attr": "connections_interval", "run": run}
