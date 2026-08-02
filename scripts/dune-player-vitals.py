#!/usr/bin/env python3
"""Per-player 'vitals' for the V2 admin Identity tab: economy + activity.

Read-only. Emits a small JSON blob for one account_id:
  economy  = online-safe currency balances (Solari = currency_id 0,
             Scrip = currency_id 1) from dune.player_virtual_currency_balances,
             resolved account_id -> player_controller_id via dune.player_state.
  activity = playtime/last-seen/active-days from the telemetry presence table
             (one row ~= 5 min online).

Usage: dune-player-vitals.py <account_id>
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DB_POD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"
DB_PATH = "/var/lib/lastsietch-telemetry/telemetry.db"
SAMPLE_MINUTES = 5

# currency_id -> friendly key (confirmed live: 0 = Solari, 1 = Scrip)
CURRENCY = {0: "solari", 1: "scrip"}


def kubectl(args):
    return subprocess.run(["sudo", "kubectl", *args],
                          capture_output=True, text=True, timeout=60)


def psql(sql):
    pw = kubectl(["exec", "-n", NS, DB_POD, "--", "printenv", "POSTGRES_PASSWORD"]).stdout.strip()
    r = subprocess.run(
        ["sudo", "kubectl", "exec", "-i", "-n", NS, DB_POD, "--", "env", f"PGPASSWORD={pw}",
         "psql", "-h", "localhost", "-p", "15432", "-U", "postgres", "-d", "dune",
         "-t", "-A", "-F|", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return [ln.split("|") for ln in r.stdout.splitlines() if ln.strip()]


def get_economy(account_id):
    out = {"solari": None, "scrip": None, "available": False}
    try:
        rows = psql(
            "SELECT vcb.currency_id, sum(vcb.balance) "
            "FROM dune.player_virtual_currency_balances vcb "
            "JOIN dune.player_state ps ON ps.player_controller_id = vcb.player_controller_id "
            f"WHERE ps.account_id = {account_id} "
            "GROUP BY vcb.currency_id;"
        )
        for cid, total in rows:
            key = CURRENCY.get(int(cid))
            if key:
                out[key] = int(total)
        out["available"] = True
    except Exception as e:
        print(f"economy query failed: {e}", file=sys.stderr)
    return out


def get_activity(account_id):
    out = {"total_hours": None, "first_seen_epoch": None, "last_seen_epoch": None,
           "active_days": None, "available": False}
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except Exception as e:
        print(f"activity open failed: {e}", file=sys.stderr)
        return out
    try:
        rows = con.execute(
            "SELECT ts FROM presence WHERE account_id = ?", (str(account_id),)
        ).fetchall()
    except Exception as e:
        print(f"activity query failed: {e}", file=sys.stderr)
        con.close()
        return out
    con.close()
    if not rows:
        out["available"] = True  # telemetry reachable, just no rows for this player
        out["total_hours"] = 0.0
        out["active_days"] = 0
        return out
    ts_values = [int(r[0]) for r in rows]
    days = {time.gmtime(t)[:3] for t in ts_values}  # (year, mon, mday)
    out.update({
        "total_hours": round(len(ts_values) * SAMPLE_MINUTES / 60.0, 1),
        "first_seen_epoch": min(ts_values),
        "last_seen_epoch": max(ts_values),
        "active_days": len(days),
        "available": True,
    })
    return out


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(json.dumps({"available": False, "error": "bad_account_id"}))
        return 2
    account_id = int(sys.argv[1])
    out = {
        "available": True,
        "account_id": account_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "economy": get_economy(account_id),
        "activity": get_activity(account_id),
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
