#!/usr/bin/env python3
"""Portal "My Orders" read-only view -- lastsietch-dune-resident psql wrapper.

Deployed to lastsietch-dune:/root/dune-my-orders.py. Invoked over SSH by the lastsietch-relay
dispatcher via the `my-orders <account_id>` token (read-only). Mirrors the
in-game CHOAM exchange "My Orders" panel for the signed-in portal player:

  * active     -> their current sell listings (orders with a live sell_orders row)
  * completed  -> their Completed tab (orders with a fulfilled_orders row):
                  completion_type 5 = purchased (awaiting Take, item in escrow),
                                   4 = sold (proceeds awaiting bank-claim),
                                   3 = canceled (awaiting Take / Relist).
  * history    -> recent realised trades from dune.ls_market_log (retained
                  permanent seller/buyer history, owner_id = controller_id).

The player's controller_id is resolved HERE from the account_id (server-trusted)
via encrypted_player_state, so a player can only ever see their own orders. The
account_id is validated numeric here AND by the dispatcher allowlist.

Read-only: every statement is a SELECT through /root/dq.sh (no game-pod write
session is ever opened). All refs are dune.-qualified so no SET is needed (a
leading SET would print a 'SET' command tag and corrupt the JSON output, exactly
as noted in dune-market-control.py).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

DQ = "/root/dq.sh"
ACTIVE_LIMIT = 100
COMPLETED_LIMIT = 100
HISTORY_LIMIT = 50


def emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def _dq(sql: str, timeout: int = 30) -> str:
    """Run one read-only SQL through dq.sh (-tAc). Returns stripped stdout or ""."""
    try:
        r = subprocess.run([DQ, "-tAc", sql], capture_output=True,
                           text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def resolve_ctrl(account_id: int):
    """Resolve the account's player_controller_id (= dune_exchange order owner_id).
    account_id is a validated int, so direct interpolation is injection-safe."""
    sql = (
        "SELECT player_controller_id FROM dune.encrypted_player_state"
        f" WHERE account_id = {account_id}::bigint"
        " AND player_controller_id IS NOT NULL LIMIT 1"
    )
    raw = _dq(sql)
    if raw and re.fullmatch(r"[0-9]+", raw.strip()):
        return int(raw.strip())
    return None


def load_orders(ctrl: int) -> dict:
    """One round-trip JSON build of the three sections, all keyed by owner_id=ctrl.

      active    : orders that still have a dune_exchange_sell_orders row (live
                  listing); stack from items.stack_size (escrowed) or the sell
                  order's initial_stack_size; carries order_id + revision so the
                  Cancel/Relist writers can revision-guard later.
      completed : orders with a dune_exchange_fulfilled_orders row; status from
                  completion_type (5 purchased / 4 sold / 3 canceled).
      history   : recent dune.ls_market_log rows (realised trades).
    """
    sql = (
        "SELECT json_build_object("
        "  'active', (SELECT COALESCE(json_agg(r),'[]'::json) FROM ("
        "      SELECT o.id AS order_id, o.revision, o.template_id, o.item_price,"
        "             o.quality_level, o.expiration_time,"
        "             o.durability_cur, o.durability_max,"
        "             COALESCE(i.stack_size, s.initial_stack_size) AS stack"
        "      FROM dune.dune_exchange_orders o"
        "      JOIN dune.dune_exchange_sell_orders s ON s.order_id = o.id"
        "      LEFT JOIN dune.items i ON i.id = o.item_id"
        f"      WHERE o.owner_id = {ctrl} AND o.is_npc_order IS NOT TRUE"
        "      ORDER BY o.id DESC LIMIT " + str(ACTIVE_LIMIT) + ") r),"
        "  'completed', (SELECT COALESCE(json_agg(r),'[]'::json) FROM ("
        "      SELECT o.id AS order_id, o.revision, o.template_id, o.item_price,"
        "             o.quality_level, f.completion_type, f.stack_size AS stack"
        "      FROM dune.dune_exchange_orders o"
        "      JOIN dune.dune_exchange_fulfilled_orders f ON f.order_id = o.id"
        f"      WHERE o.owner_id = {ctrl}"
        "      ORDER BY o.id DESC LIMIT " + str(COMPLETED_LIMIT) + ") r),"
        "  'history', (SELECT COALESCE(json_agg(r),'[]'::json) FROM ("
        "      SELECT template_id, item_price, stack_size AS stack,"
        "             completion_type, bot_trade, logged_at"
        "      FROM dune.ls_market_log"
        f"      WHERE owner_id = {ctrl}"
        "      ORDER BY logged_at DESC LIMIT " + str(HISTORY_LIMIT) + ") r)"
        ")"
    )
    raw = _dq(sql)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main():
    if len(sys.argv) < 2 or not re.fullmatch(r"[0-9]+", (sys.argv[1] or "").strip()):
        emit({"available": False, "error": "bad_account_id",
              "active": [], "completed": [], "history": []}, 0)
    account_id = int(sys.argv[1].strip())
    ctrl = resolve_ctrl(account_id)
    if ctrl is None:
        # No resolvable character for this account (never logged in / no pawn).
        emit({"available": False, "error": "no_character",
              "active": [], "completed": [], "history": []}, 0)
    data = load_orders(ctrl)
    emit({
        "available": True,
        "account_id": account_id,
        "controller_id": ctrl,
        "active": data.get("active") or [],
        "completed": data.get("completed") or [],
        "history": data.get("history") or [],
    }, 0)


if __name__ == "__main__":
    main()
