#!/usr/bin/env python3
"""Leave an item waiting for a player in their CHOAM Exchange "Completed" tab.

The third delivery lane, and the only UNCONDITIONAL one. Proven live 2026-07-25
(order 426629 -> n0logic's backpack slot 36). Unlike the other two it has:
  * no capacity ceiling      - the item sits in the exchange, not their bag
  * no position collision    - nothing is written into an occupied inventory
  * no online/offline gate   - works either way, waits as long as needed
The player collects it at any exchange terminal and the ENGINE places it, so
placement is Funcom's code, not ours.

Cost: they must walk to a tradepost. For the welcome pack's bulky tail that is a
fair trade, and it teaches a new player where the exchange is.

🔴 expiration_time MUST NOT BE NULL. The purge only deletes rows where it IS NOT
NULL, so NULL looked like the safe "never expires" choice -- but the client's
Completed tab has an EXPIRATION column it sorts by, and a NULL row does not render
at all. Tested live: NULL -> "no completed orders available"; a real timestamp ->
renders immediately. So we set a long expiry and accept that the claim lives on
the game clock. At a year out, the ~32-day clock skew that culled the BUG-010
payouts is not a threat.

The player sees status CANCELED (completion_type 3 reads as "your listing was
cancelled, take your item back"). Functional, but it will never say "gift" -- so
whatever grants this must ALSO tell the player it is waiting for them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

NS = os.environ.get("LASTSIETCH_GAME_NAMESPACE", "")
DB_POD = os.environ.get("LASTSIETCH_GAME_BATTLEGROUP", "") + "-db-dbdepl-sts-0"

EXCHANGE_ID = 2
EXCHANGE_INV = 610          # where exchange-held items live (verified live)
ACCESS_POINT = 1
COMPLETION_TYPE = 3         # renders as CANCELED + "Take item"
DEFAULT_EXPIRY_DAYS = 365
DEFAULT_PRICE = 250

TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
RC_OK, RC_USAGE, RC_FAILED = 0, 2, 5


def fail(msg, code=RC_USAGE):
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(code)


def sql_lit(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def psql(sql, tuples=True):
    args = ["-v", "ON_ERROR_STOP=1"] + (["-t", "-A", "-F", "|"] if tuples else [])
    r = subprocess.run(
        ["kubectl", "exec", "-i", "-n", NS, DB_POD, "--",
         "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_",
         "-h", "localhost", "-p", "15432", "-U", "postgres", "-d", "dune", *args], input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        fail("psql failed: %s" % r.stderr.strip())
    return r.stdout.strip()


def resolve_player(account_id):
    """(controller_id, fls_id, actor_id). owner_id on an exchange order is the
    player_controller_id -- confirmed live against n0logic's own pending claim."""
    row = psql("""
        SELECT eps.player_controller_id, acc."user", eps.player_pawn_id
          FROM dune.encrypted_player_state eps
          JOIN dune.accounts acc ON acc.id = eps.account_id
         WHERE eps.account_id = %d
           AND eps.character_state IS DISTINCT FROM 'Deleted'
           AND eps.player_controller_id IS NOT NULL
         ORDER BY eps.last_login_time DESC LIMIT 1;
    """ % account_id)
    if not row:
        fail("no controller for account %d" % account_id)
    ctrl, fls, actor = row.split("|")
    return int(ctrl), fls.strip(), int(actor)


def game_now():
    """Engine-authoritative active universe seconds, the clock exchange expiries
    are measured against. Mirrors the market bot's farm_variables anchor:
    (now - universe_start) - downtime, downtime being MICROseconds.

    The bot additionally applies a calibrated offset (~24k s, measured empirically
    against the client's own countdown). Deliberately not replicated here: at a
    365-day expiry a 7-hour error is 0.08%, and duplicating the calibration would
    mean two things to keep in step.
    """
    v = psql("""
        SELECT EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - universe_time_timestamp))
               - COALESCE(down_time_accumulation, 0) / 1000000.0
          FROM dune.farm_variables
         WHERE universe_time_timestamp IS NOT NULL
         ORDER BY universe_time_timestamp LIMIT 1;
    """)
    if not v:
        fail("could not read the game clock from dune.farm_variables")
    return int(float(v))


def category_for(template):
    """Reuse the category_mask/depth the exchange already uses for this template.
    Both columns are NOT NULL and drive the client's category header, so copying a
    real listing is more reliable than deriving one."""
    row = psql("""
        SELECT category_mask, category_depth FROM dune.dune_exchange_orders
         WHERE template_id = %s LIMIT 1;
    """ % sql_lit(template))
    if not row:
        return None, None
    m, d = row.split("|")
    return int(m), int(d)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Leave an item in a player's exchange Completed tab.")
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--quantity", type=int, default=1)
    p.add_argument("--price", type=int, default=DEFAULT_PRICE)
    p.add_argument("--expiry-days", type=int, default=DEFAULT_EXPIRY_DAYS)
    p.add_argument("--source", default="welcome_pack",
                   choices=["welcome_pack", "grant", "storage", "auction", "test"])
    p.add_argument("--operator", default=os.environ.get("USER", "unknown"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--send", action="store_true", help="Actually create the rows.")
    args = p.parse_args(argv)

    if not TEMPLATE_RE.match(args.template):
        fail("template must match [A-Za-z0-9_]{1,64}")
    if args.quantity < 1:
        fail("--quantity must be >= 1")
    if args.expiry_days < 1:
        fail("--expiry-days must be >= 1 (NULL/absent expiry does not render)")

    ctrl, fls, actor = resolve_player(args.account_id)
    mask, depth = category_for(args.template)
    if mask is None:
        fail("no existing exchange listing for %s to copy a category from; "
             "add one by hand or extend this tool" % args.template)

    expiry = game_now() + args.expiry_days * 86400
    corr = "lastsietch-exchange-claim-%d-%s-%d" % (
        args.account_id, args.template[:20], int(time.time() * 1000))

    out = {"account_id": args.account_id, "owner_ctrl": ctrl, "fls_id": fls,
           "template": args.template, "quantity": args.quantity,
           "price": args.price, "expiry_game_time": expiry,
           "expiry_days": args.expiry_days, "correlation_id": corr,
           "category_mask": mask, "category_depth": depth}

    if not args.send:
        out["verdict"] = "dry_run"
        print(json.dumps(out) if args.json else
              "dry-run: would list %s x%d for ctrl %d, expiry +%dd (game %d)"
              % (args.template, args.quantity, ctrl, args.expiry_days, expiry))
        return RC_OK

    # One transaction: item into the exchange inventory, the order that owns it,
    # and the fulfilled row that makes it show as claimable. A partial write here
    # would either orphan an item or list an order pointing at nothing.
    res = psql("""
BEGIN;
WITH newpos AS (
  SELECT COALESCE(MAX(position_index), 0) + 1 AS p
    FROM dune.items WHERE inventory_id = {inv}
), newitem AS (
  INSERT INTO dune.items
    (inventory_id, stack_size, position_index, template_id, stats, quality_level,
     acquisition_time, is_new)
  SELECT {inv}, {qty}, newpos.p, {tmpl}, '{{}}'::jsonb, 0, 0, true FROM newpos
  RETURNING id
), neworder AS (
  INSERT INTO dune.dune_exchange_orders
    (revision, exchange_id, owner_id, item_id, template_id, category_mask,
     category_depth, access_point_id, is_npc_order, item_price, expiration_time,
     durability_cur, durability_max, quality_level)
  SELECT 1, {xid}, {ctrl}, newitem.id, {tmpl}, {mask}, {depth}, {ap}, false,
         {price}, {expiry}, 0, 0, 0
    FROM newitem
  RETURNING id, item_id
), newfulfilled AS (
  INSERT INTO dune.dune_exchange_fulfilled_orders
    (order_id, completion_type, stack_size, source_order_id, original_order_id)
  SELECT neworder.id, {ctype}, {qty}, NULL, neworder.id FROM neworder
  RETURNING order_id
), trace AS (
  -- observed_item_id is recorded UP FRONT here, unlike the RMQ lane where it is
  -- discovered by polling. An exchange claim can sit for days, so the confirmation
  -- sweep needs to know which item it is waiting on; without it the row stays
  -- 'pending' forever and the log quietly lies about completed deliveries.
  INSERT INTO dune.ls_item_delivery_log
    (correlation_id, source, lane, account_id, fls_id, actor_id, template_id,
     quantity, quality_level, status, operator, observed_item_id)
  SELECT {corr}, {src}, 'exchange', {acct}, {fls}, {actor}, {tmpl}, {qty}, 0,
         'pending', {op}, (SELECT item_id FROM neworder)
  RETURNING id
)
SELECT json_build_object('order_id', neworder.id, 'item_id', neworder.item_id,
                         'trace_id', (SELECT id FROM trace))
  FROM neworder JOIN newfulfilled ON newfulfilled.order_id = neworder.id;
COMMIT;
""".format(inv=EXCHANGE_INV, qty=args.quantity, tmpl=sql_lit(args.template),
           xid=EXCHANGE_ID, ctrl=ctrl, mask=mask, depth=depth, ap=ACCESS_POINT,
           price=args.price, expiry=expiry, ctype=COMPLETION_TYPE,
           corr=sql_lit(corr), src=sql_lit(args.source), acct=args.account_id,
           fls=sql_lit(fls), actor=actor, op=sql_lit(args.operator)))

    line = [l for l in res.splitlines() if l.strip().startswith("{")]
    if not line:
        out["verdict"] = "failed"
        out["raw"] = res[-400:]
        print(json.dumps(out) if args.json else "failed: %s" % res[-200:])
        return RC_FAILED
    out.update(json.loads(line[-1]), verdict="listed")
    print(json.dumps(out) if args.json else
          "listed order=%s item=%s expiry=+%dd (waiting in their Completed tab)"
          % (out["order_id"], out["item_id"], args.expiry_days))
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
