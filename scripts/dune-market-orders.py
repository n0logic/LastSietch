#!/usr/bin/env python3
"""Portal Market CANCEL + RELIST writer -- lastsietch-dune-resident psql wrapper.

Deployed to lastsietch-dune:/root/dune-market-orders.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `market-cancel` / `market-relist`
tokens, or directly with --dry-run for read-only QA validation.

A confirmed portal player manages their OWN CHOAM exchange orders from the My Orders
page:
  * CANCEL an ACTIVE sell listing  -> dune_exchange_cancel_order(order, purge, 3).
        The engine forfeits the listing fee (already debited at list time) and moves
        the item to the player's Completed tab (exchange escrow inv 610). It NEVER
        touches live RAM-backed inventory, so this is ONLINE-SAFE -- no offline gate.
  * RELIST a CANCELED order from the Completed tab -> dune_exchange_relist_order(
        order, expiration, price, wear_price, fee). The item is already in exchange
        escrow, so this is ALSO online-safe. The proc debits the EXCHANGE WALLET by
        the new listing fee, so we fund the wallet from bank Solari first (the exact
        BUY/SELL funding pattern), then relist debits it back -- net bank -= fee.

Engine procs read live 2026-06-06; archived at
our internal design notes

Correctness rules (non-negotiable):
  * OWNERSHIP (STOP-SHIP): never trust the caller. The locked order's owner_id MUST
    equal the resolved controller_id, and is_npc_order MUST be FALSE. REJECT (not_owner)
    otherwise. The relist/cancel procs take only an order_id and do NOT verify the
    caller, so this guard lives here.
  * REVISION GUARD: the order's revision MUST equal the revision the portal showed,
    or we bail (revision_drift) -- so a stale My Orders page can't act on a changed
    order. Cancel bumps revision; the portal re-reads after either action.
  * CANCEL applies only to an ACTIVE order (has a dune_exchange_sell_orders row and NO
    dune_exchange_fulfilled_orders row). RELIST applies only to a CANCELED order
    (fulfilled_orders.completion_type = 3) whose escrow item_id IS NOT NULL.
  * RELIST funding: ONE call to dune_exchange_modify_user_solari_balance(owner, fee)
    atomically debits the bank AND credits the exchange wallet; the relist proc then
    debits the wallet by in_solari_cost=fee. NEVER also UPDATE
    player_virtual_currency_balances or the bank is double-debited.
  * expiration_time / purge_time are GAME-TIME, derived exactly like the market bot's
    learn_game_epoch: game_now = MAX(expiration_time) over NON-NPC, sub-1e9 orders,
    minus 24h. RELIST expiration = game_now + duration_days*86400. CANCEL purge_time =
    game_now + 30d (so the canceled item sits in the Completed tab long enough to
    Take or Relist).
  * FEE formula (CONFIRMED, same as SELL): fee = round(0.01*price*(days+1)) + 20*days,
    exact integer half-up = (price*(days+1) + 50) // 100 + 20*days.
  * Every numeric field is validated as a positive int in Python, then folded into the
    DO block as an integer literal (psql -v cannot reach inside dollar-quoted blocks).

HARD CONSTRAINTS (mirror dune-market-sell.py / dune-market-buy.py):
  * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session into
    the ALREADY-RUNNING DB pod.
  * Re-validate every field here (defence in depth on top of the dispatcher gate).

Modes:
  --action cancel --owner-ctrl N --order-id N --revision N [--dry-run]
  --action relist --owner-ctrl N --order-id N --revision N --price N
        --duration-days {1,3,7,14} [--dry-run]
  --stdin-json   read the same fields as one JSON object on stdin (dispatcher path);
                 fields {action, owner_ctrl, order_id, revision, price?, duration_days?,
                         dry_run?}
"""

import argparse
import json
import re
import subprocess
import sys

BANK_CURRENCY_ID = 0           # player_virtual_currency_balances Solari = currency_id 0
CANCELED_COMPLETION_TYPE = 3   # dune_exchange_cancel_order completion_type for a cancel
PURGE_WINDOW_SECS = 2592000    # 30 days the canceled item stays in the Completed tab
VALID_DURATIONS = (1, 3, 7, 14)
# V1 durability simplification (mirrors dune-market-sell.py): pass price for the
# wear-normalized price too; refine for true durability later.

DQ = "/root/dq.sh"
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# Clean error tokens the relay/portal switch on. Order matters: most-specific first.
ERROR_TOKENS = (
    "order_gone",
    "not_owner",
    "revision_drift",
    "not_active",
    "not_canceled",
    "insufficient_bank",
    "clock_unresolved",
    "cancel_failed",
    "relist_failed",
)


def emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def fail(error, code=1):
    emit({"ok": False, "error": str(error)[:300]}, code)


def pos_int(name, val):
    """Coerce val (int or all-digit str) to a positive int, or fail closed."""
    if isinstance(val, bool):
        fail(f"bad_{name}", 2)
    if isinstance(val, int):
        n = val
    elif isinstance(val, str) and re.fullmatch(r"[0-9]+", val.strip()):
        n = int(val.strip())
    else:
        fail(f"bad_{name}", 2)
    if n < 1:
        fail(f"bad_{name}", 2)
    return n


def compute_fee(price, days):
    """CONFIRMED listing fee, exact integer math (half-up). Same as dune-market-sell."""
    return (price * (days + 1) + 50) // 100 + 20 * days


def _dq(sql, timeout=25):
    """Run one read-only SQL through dq.sh (-tAc). Returns stripped stdout or ""."""
    try:
        r = subprocess.run([DQ, "-tAc", sql], capture_output=True,
                           text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def resolve_db_pod():
    """Resolve namespace + DB pod by label so a Funcom redeploy that rotates the pod
    hash never writes to a stale pod. Mirrors dune-market-sell.resolve_db_pod."""
    try:
        ns_out = subprocess.run(["sudo", "kubectl", "get", "ns", "-o", "name"],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout resolving namespace", 3)
    ns = ""
    for line in (ns_out.stdout or "").splitlines():
        name = line.strip().removeprefix("namespace/")
        if name.startswith("funcom-seabass-"):
            ns = name
            break
    if not ns:
        fail("ns_unresolved", 3)
    try:
        pod_out = subprocess.run(["sudo", "kubectl", "get", "pods", "-n", ns, "-o", "name"],
                                 capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout resolving DB pod", 3)
    pod = ""
    for line in (pod_out.stdout or "").splitlines():
        name = line.strip().removeprefix("pod/")
        if name.endswith("-db-dbdepl-sts-0"):
            pod = name
            break
    if not pod:
        fail("db_pod_unresolved", 3)
    return ns, pod


def run_psql(ns, pod, sql, timeout=20):
    """The ONLY place a write DB connection is opened. Reads POSTGRES_PASSWORD from the
    pod env then execs psql inside the already-running pod. SQL arrives on stdin."""
    try:
        pw_out = subprocess.run(
            ["sudo", "kubectl", "exec", "-n", ns, pod, "--",
             "printenv", "POSTGRES_PASSWORD"],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout reading POSTGRES_PASSWORD", 3)
    pgpass = (pw_out.stdout or "").strip()
    if not pgpass:
        fail("no_pgpass", 3)
    cmd = ["sudo", "kubectl", "exec", "-i", "-n", ns, pod, "--",
           "env", f"PGPASSWORD={pgpass}", "psql",
           "-h", "localhost", "-p", DB_PORT, "-U", DB_USER, "-d", DB_NAME,
           "-tA", "-v", "ON_ERROR_STOP=1"]
    try:
        return subprocess.run(cmd, input=sql, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        fail("psql_timeout", 1)


def read_plan(owner, order_id):
    """Read-only lookup of the order + its lifecycle state + game clock + bank.
    owner/order_id are validated ints, so direct interpolation is injection-safe.
    Always returns one row (LEFT JOIN on a synthetic qid) so a missing order surfaces
    as found=false."""
    sql = (
        "SELECT json_build_object("
        "  'found', (o.id IS NOT NULL),"
        "  'owner_id', o.owner_id,"
        "  'revision', o.revision,"
        "  'is_npc', o.is_npc_order,"
        "  'template_id', o.template_id,"
        "  'has_item', (o.item_id IS NOT NULL),"
        "  'is_active', (s.order_id IS NOT NULL),"
        "  'completion_type', (SELECT f.completion_type FROM dune.dune_exchange_fulfilled_orders f"
        "      WHERE f.order_id = o.id LIMIT 1),"
        "  'game_now', ((SELECT MAX(expiration_time) FROM dune.dune_exchange_orders"
        "      WHERE is_npc_order = FALSE AND expiration_time IS NOT NULL"
        "        AND expiration_time < 1000000000) - 86400),"
        "  'bank', (SELECT balance FROM dune.player_virtual_currency_balances"
        f"      WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID})"
        ") "
        f"FROM (SELECT {order_id}::bigint AS qid) q "
        "LEFT JOIN dune.dune_exchange_orders o ON o.id = q.qid "
        "LEFT JOIN dune.dune_exchange_sell_orders s ON s.order_id = o.id"
    )
    raw = _dq(sql)
    if not raw:
        fail("read_failed", 1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fail("read_failed", 1)


def preflight_cancel(plan, owner, revision):
    """Contract preconditions a cancel would violate, in the live RAISE tokens."""
    if not plan.get("found"):
        return ["order_gone"]
    errs = []
    if plan.get("owner_id") != owner or plan.get("is_npc"):
        errs.append("not_owner")
    if plan.get("revision") != revision:
        errs.append("revision_drift")
    # ACTIVE = has a sell_orders row AND no fulfilled_orders row.
    if not plan.get("is_active") or plan.get("completion_type") is not None:
        errs.append("not_active")
    if plan.get("game_now") is None:
        errs.append("clock_unresolved")
    return errs


def preflight_relist(plan, owner, revision, fee):
    """Contract preconditions a relist would violate, in the live RAISE tokens."""
    if not plan.get("found"):
        return ["order_gone"]
    errs = []
    if plan.get("owner_id") != owner or plan.get("is_npc"):
        errs.append("not_owner")
    if plan.get("revision") != revision:
        errs.append("revision_drift")
    # RELISTABLE = canceled (completion_type 3) with the escrow item still present.
    if plan.get("completion_type") != CANCELED_COMPLETION_TYPE or not plan.get("has_item"):
        errs.append("not_canceled")
    if plan.get("game_now") is None:
        errs.append("clock_unresolved")
    bank = plan.get("bank")
    if bank is None or bank < fee:
        errs.append("insufficient_bank")
    return errs


def build_cancel_sql(owner, order_id, revision):
    """One-transaction CANCEL. Locks + verifies the order (owner + revision + active),
    derives the game purge_time, calls dune_exchange_cancel_order(order, purge, 3),
    and asserts it flipped to a completion_type-3 fulfilled order. No bank/fee/escrow
    move: the fee was already paid at list time (forfeited), the item just moves to
    the Completed tab. Any RAISE rolls the txn back."""
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _cancel_result;
CREATE TEMP TABLE _cancel_result(order_id bigint, template_id text, purge_time bigint)
  ON COMMIT PRESERVE ROWS;
DO $cancel$
DECLARE
  v_owner bigint; v_rev bigint; v_npc bool; v_tpl text;
  v_active bool; v_completed bool; v_game_now bigint; v_purge bigint;
BEGIN
  -- 1. lock + verify the order: must be the caller's own, non-NPC, current revision
  SELECT owner_id, revision, is_npc_order, template_id
    INTO v_owner, v_rev, v_npc, v_tpl
    FROM dune.dune_exchange_orders WHERE id = {order_id} FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'order_gone'; END IF;
  IF v_owner IS DISTINCT FROM {owner} OR v_npc IS TRUE THEN
    RAISE EXCEPTION 'not_owner (owner=% npc=%)', COALESCE(v_owner,0), COALESCE(v_npc,true);
  END IF;
  IF v_rev <> {revision} THEN
    RAISE EXCEPTION 'revision_drift have=% want=%', v_rev, {revision};
  END IF;
  -- 2. must be ACTIVE: a live sell_orders row and no fulfilled_orders row yet
  SELECT EXISTS(SELECT 1 FROM dune.dune_exchange_sell_orders WHERE order_id = {order_id}),
         EXISTS(SELECT 1 FROM dune.dune_exchange_fulfilled_orders WHERE order_id = {order_id})
    INTO v_active, v_completed;
  IF NOT v_active OR v_completed THEN
    RAISE EXCEPTION 'not_active (active=% completed=%)', v_active, v_completed;
  END IF;
  -- 3. game-time purge: keep the canceled item in the Completed tab ~30 days
  SELECT (MAX(expiration_time) - 86400) INTO v_game_now
    FROM dune.dune_exchange_orders
    WHERE is_npc_order = FALSE AND expiration_time IS NOT NULL
      AND expiration_time < 1000000000;
  IF v_game_now IS NULL THEN RAISE EXCEPTION 'clock_unresolved'; END IF;
  v_purge := v_game_now + {PURGE_WINDOW_SECS};
  -- 4. cancel: deletes the sell_orders row, inserts fulfilled_orders(type 3),
  --    bumps revision, sets expiration_time = purge. Fee already gone (forfeited).
  PERFORM dune.dune_exchange_cancel_order({order_id}, v_purge, {CANCELED_COMPLETION_TYPE});
  IF NOT EXISTS(SELECT 1 FROM dune.dune_exchange_fulfilled_orders
                WHERE order_id = {order_id} AND completion_type = {CANCELED_COMPLETION_TYPE}) THEN
    RAISE EXCEPTION 'cancel_failed';
  END IF;
  INSERT INTO _cancel_result VALUES ({order_id}, v_tpl, v_purge);
END $cancel$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'cancel',
  'order_id', order_id, 'template_id', template_id, 'purge_time', purge_time
) FROM _cancel_result;
"""


def build_relist_sql(owner, order_id, revision, price, days):
    """One-transaction RELIST from the Completed tab. Locks + verifies the order
    (owner + revision + canceled + escrow item present), derives the game expiration +
    fee, funds the exchange wallet from bank Solari (ONE modify_user_solari_balance
    call), then calls dune_exchange_relist_order (which debits the wallet fee, deletes
    the fulfilled_orders row, and re-inserts a sell_orders row). relist_order returns 0
    on underfunding -> we RAISE relist_failed so the funding rolls back. Net bank -=
    fee. Any RAISE rolls the whole txn back."""
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _relist_result;
CREATE TEMP TABLE _relist_result(
  order_id bigint, template_id text, stack bigint, unit_price bigint,
  fee bigint, expiration_time bigint, bank_before bigint, bank_after bigint
) ON COMMIT PRESERVE ROWS;
DO $relist$
DECLARE
  v_owner bigint; v_rev bigint; v_npc bool; v_tpl text; v_item bigint;
  v_ctype int; v_game_now bigint; v_expire bigint; v_fee bigint;
  v_bank bigint; v_after bigint; v_stack bigint;
BEGIN
  -- 1. lock + verify the order: caller's own, non-NPC, current revision, escrow item
  SELECT owner_id, revision, is_npc_order, template_id, item_id
    INTO v_owner, v_rev, v_npc, v_tpl, v_item
    FROM dune.dune_exchange_orders WHERE id = {order_id} FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'order_gone'; END IF;
  IF v_owner IS DISTINCT FROM {owner} OR v_npc IS TRUE THEN
    RAISE EXCEPTION 'not_owner (owner=% npc=%)', COALESCE(v_owner,0), COALESCE(v_npc,true);
  END IF;
  IF v_rev <> {revision} THEN
    RAISE EXCEPTION 'revision_drift have=% want=%', v_rev, {revision};
  END IF;
  -- 2. must be a CANCELED completed order (type 3) whose escrow item is still present
  SELECT completion_type INTO v_ctype FROM dune.dune_exchange_fulfilled_orders
    WHERE order_id = {order_id} LIMIT 1;
  IF v_item IS NULL OR v_ctype IS DISTINCT FROM {CANCELED_COMPLETION_TYPE} THEN
    RAISE EXCEPTION 'not_canceled (item=% ctype=%)', COALESCE(v_item,0), COALESCE(v_ctype,-1);
  END IF;
  -- 3. game-time expiration for the new listing
  SELECT (MAX(expiration_time) - 86400) INTO v_game_now
    FROM dune.dune_exchange_orders
    WHERE is_npc_order = FALSE AND expiration_time IS NOT NULL
      AND expiration_time < 1000000000;
  IF v_game_now IS NULL THEN RAISE EXCEPTION 'clock_unresolved'; END IF;
  v_expire := v_game_now + {days} * 86400;
  -- 4. CONFIRMED listing fee (half-up integer math)
  v_fee := ({price} * ({days} + 1) + 50) / 100 + 20 * {days};
  -- 5. lock + verify the bank can cover the relist fee
  SELECT balance INTO v_bank FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID} FOR UPDATE;
  IF v_bank IS NULL OR v_bank < v_fee THEN
    RAISE EXCEPTION 'insufficient_bank have=% need=%', COALESCE(v_bank,0), v_fee;
  END IF;
  -- 6. fund the exchange wallet from the bank (ONE call: atomic bank-debit +
  --    wallet-credit); relist then debits the wallet fee back -> net bank -= fee
  PERFORM dune.dune_exchange_modify_user_solari_balance({owner}, v_fee);
  -- 7. relist: deletes fulfilled_orders, re-inserts sell_orders, updates price/exp;
  --    returns the stack size, or 0 if the wallet could not cover the fee
  v_stack := dune.dune_exchange_relist_order({order_id}, v_expire, {price}, {price}, v_fee);
  IF v_stack IS NULL OR v_stack = 0 THEN
    RAISE EXCEPTION 'relist_failed (wallet debit or order update rejected)';
  END IF;
  SELECT balance INTO v_after FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID};
  INSERT INTO _relist_result VALUES ({order_id}, v_tpl, v_stack, {price}, v_fee,
    v_expire, v_bank, v_after);
END $relist$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'relist',
  'order_id', order_id, 'template_id', template_id, 'stack', stack,
  'unit_price', unit_price, 'fee', fee, 'expiration_time', expiration_time,
  'expiration_days', {days}, 'bank_before', bank_before, 'bank_after', bank_after
) FROM _relist_result;
"""


def parse_psql_error(stderr):
    blob = (stderr or "").lower()
    for tok in ERROR_TOKENS:
        if tok in blob:
            return tok
    return "write_failed"


def last_json_line(stdout):
    raw = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line in ("SET", "BEGIN", "COMMIT", "CREATE TABLE", "DROP TABLE", "DO"):
            continue
        if line.startswith(("INSERT ", "UPDATE ", "SELECT ", "NOTICE", "PERFORM")):
            continue
        raw = line
    return raw


def do_dry_run(action, owner, order_id, revision, price, days):
    plan = read_plan(owner, order_id)
    if not plan.get("found"):
        fail("order_gone", 1)
    game_now = plan.get("game_now")
    out = {
        "dry_run": True, "action": action,
        "owner_ctrl": owner, "order_id": order_id, "revision": revision,
        "order_revision": plan.get("revision"),
        "owner_id": plan.get("owner_id"),
        "template_id": plan.get("template_id"),
        "is_active": plan.get("is_active"),
        "completion_type": plan.get("completion_type"),
        "has_item": plan.get("has_item"),
        "game_now": game_now,
        "bank_before": plan.get("bank"),
    }
    if action == "cancel":
        out["preflight_errors"] = preflight_cancel(plan, owner, revision)
        out["purge_time"] = (game_now + PURGE_WINDOW_SECS) if isinstance(game_now, int) else None
    else:
        fee = compute_fee(price, days)
        bank = plan.get("bank")
        out["unit_price"] = price
        out["duration_days"] = days
        out["fee"] = fee
        out["expiration_time"] = (game_now + days * 86400) if isinstance(game_now, int) else None
        out["bank_after_projected"] = (bank - fee) if isinstance(bank, int) else None
        out["preflight_errors"] = preflight_relist(plan, owner, revision, fee)
    emit(out, 0)


def do_live(action, owner, order_id, revision, price, days):
    ns, pod = resolve_db_pod()
    if action == "cancel":
        sql = build_cancel_sql(owner, order_id, revision)
    else:
        sql = build_relist_sql(owner, order_id, revision, price, days)
    out = run_psql(ns, pod, sql, timeout=20)
    if out.returncode != 0:
        fail(parse_psql_error(out.stderr or out.stdout), 1)
    raw = last_json_line(out.stdout)
    if not raw:
        fail("write_failed", 1)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        fail("write_failed", 1)
    emit(result, 0)


def gather_args():
    """Return (action, owner, order_id, revision, price, days, dry_run) from either
    --stdin-json (dispatcher path) or CLI flags. price/days are required + validated
    only for relist."""
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        action = payload.get("action")
        owner = pos_int("owner_ctrl", payload.get("owner_ctrl"))
        order_id = pos_int("order_id", payload.get("order_id"))
        revision = pos_int("revision", payload.get("revision"))
        price = payload.get("price")
        days = payload.get("duration_days")
        dry_run = bool(payload.get("dry_run", False))
    else:
        ap = argparse.ArgumentParser(description="Portal Market CANCEL/RELIST writer")
        ap.add_argument("--action", required=True, choices=["cancel", "relist"])
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--order-id", required=True)
        ap.add_argument("--revision", required=True)
        ap.add_argument("--price", default=None)
        ap.add_argument("--duration-days", default=None)
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        action = a.action
        owner = pos_int("owner_ctrl", a.owner_ctrl)
        order_id = pos_int("order_id", a.order_id)
        revision = pos_int("revision", a.revision)
        price = a.price
        days = a.duration_days
        dry_run = a.dry_run

    if action not in ("cancel", "relist"):
        fail("bad_action", 2)
    if action == "relist":
        price = pos_int("price", price)
        days = pos_int("duration_days", days)
        if days not in VALID_DURATIONS:
            fail("bad_duration_days", 2)
    else:
        price, days = 0, 0
    return action, owner, order_id, revision, price, days, dry_run


def main():
    action, owner, order_id, revision, price, days, dry_run = gather_args()
    if dry_run:
        do_dry_run(action, owner, order_id, revision, price, days)
    else:
        do_live(action, owner, order_id, revision, price, days)


if __name__ == "__main__":
    main()
