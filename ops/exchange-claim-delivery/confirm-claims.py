#!/usr/bin/env python3
"""Flip exchange-lane delivery traces from `pending` to `delivered`.

The RMQ lane confirms inline: publish, then poll dune.items for a couple of
seconds. The exchange lane cannot -- the item sits in the player's Completed tab
until they walk to a tradepost, which may be days. So confirmation is a sweep, and
without it every exchange trace row stays `pending` forever and the log quietly
claims nothing was ever delivered.

For each pending row, the item it is waiting on (observed_item_id, recorded at
list time) is in exactly one of three states:

  in a PLAYER inventory   -> delivered. Stamp delivered_at and where it landed.
  gone from dune.items    -> undelivered. The purge deleted the order and the item
                             with it, which is what a lapsed expiry looks like.
  still in exchange 610   -> genuinely still waiting. Leave it alone.

Read-only unless --apply.
"""
from __future__ import annotations

import argparse
import os
import json
import subprocess
import sys

NS = os.environ.get("LASTSIETCH_GAME_NAMESPACE", "")
DB_POD = os.environ.get("LASTSIETCH_GAME_BATTLEGROUP", "") + "-db-dbdepl-sts-0"
EXCHANGE_INV = 610


def psql(sql):
    args = ["-v", "ON_ERROR_STOP=1", "-t", "-A", "-F", "|"]
    r = subprocess.run(
        ["kubectl", "exec", "-i", "-n", NS, DB_POD, "--",
         "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_",
         "-h", "localhost", "-p", "15432", "-U", "postgres", "-d", "dune", *args], input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        print("psql failed: %s" % r.stderr.strip(), file=sys.stderr)
        sys.exit(2)
    return r.stdout.strip()


def main(argv=None):
    p = argparse.ArgumentParser(description="Confirm exchange-lane deliveries.")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    rows = psql("""
        SELECT l.id, l.template_id, l.observed_item_id, l.account_id,
               CASE
                 WHEN it.id IS NULL THEN 'gone'
                 WHEN inv.exchange_id IS NOT NULL THEN 'waiting'
                 ELSE 'collected'
               END AS state,
               COALESCE(it.position_index, -1), COALESCE(inv.actor_id, 0)
          FROM dune.ls_item_delivery_log l
          LEFT JOIN dune.items it ON it.id = l.observed_item_id
          LEFT JOIN dune.inventories inv ON inv.id = it.inventory_id
         WHERE l.lane = 'exchange' AND l.status = 'pending'
           AND l.observed_item_id IS NOT NULL
         ORDER BY l.id;
    """)

    out = {"collected": [], "gone": [], "waiting": []}
    for line in [x for x in rows.splitlines() if x.strip()]:
        tid, tmpl, item, acct, state, pos, actor = line.split("|")
        out[state].append({"trace_id": int(tid), "template": tmpl,
                           "item_id": int(item), "account_id": int(acct),
                           "slot": int(pos), "actor_id": int(actor)})

    if args.apply:
        for r in out["collected"]:
            psql("""UPDATE dune.ls_item_delivery_log
                       SET status='delivered', delivered_at=now(),
                           observed_stack_delta=%d
                     WHERE id=%d;""" % (r["slot"], r["trace_id"]))
        for r in out["gone"]:
            psql("""UPDATE dune.ls_item_delivery_log
                       SET status='undelivered',
                           error='item no longer exists -- the order expiry lapsed '
                                 'and the purge removed it before the player claimed'
                     WHERE id=%d;""" % r["trace_id"])

    summary = {"collected": len(out["collected"]), "gone": len(out["gone"]),
               "still_waiting": len(out["waiting"]), "applied": args.apply}
    if args.json:
        print(json.dumps({**summary, "detail": out}))
    else:
        print("collected=%(collected)d gone=%(gone)d still_waiting=%(still_waiting)d "
              "applied=%(applied)s" % summary)
        for r in out["collected"]:
            print("  delivered  trace %-4d %-34s -> slot %d"
                  % (r["trace_id"], r["template"], r["slot"]))
        for r in out["gone"]:
            print("  EXPIRED    trace %-4d %s" % (r["trace_id"], r["template"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
