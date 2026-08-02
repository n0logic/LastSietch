#!/usr/bin/env python3
"""Portal Market BUY writer -- lastsietch-dune-resident psql wrapper.

Deployed to lastsietch-dune:/root/dune-market-buy.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `market-buy` token, or directly
with --dry-run for read-only QA validation.

A confirmed portal player buys a CHOAM-exchange sell listing remotely. The buy is
funded from the buyer's CHOAM bank Solari and delivered to their in-game Completed
tab (exchange storage, dst_inventory_id=NULL), so it is safe whether the player is
online or offline -- it never touches live RAM-backed inventory. No offline gate.



Modes:
  --order-id N --revision N --buyer-ctrl N --count N [--max-orders N] [--dry-run]
  --stdin-json    read the same fields as one JSON object on stdin (dispatcher path);
                  fields {order_id, revision, buyer_ctrl, count, max_orders?, dry_run?}

Correctness rules (non-negotiable):
  * Funding the buy is ONE call to dune_exchange_modify_user_solari_balance(buyer,
    total): that proc atomically debits the bank AND credits the exchange wallet.
    NEVER also UPDATE player_virtual_currency_balances or the bank is double-debited.
  * purge_time is GAME-TIME, derived exactly like the market bot's learn_game_epoch
    (/opt/lastsietch-market-bot/exchange.py): MAX(expiration_time) over NON-NPC, sub-1e9
    orders, minus 24h, plus 30d. The is_npc_order=FALSE filter is REQUIRED -- NPC
    orders carry a 999999999 sentinel that would otherwise poison the value.
  * Every numeric field is validated as a positive int in Python, then folded into
    the DO block as an integer literal. psql -v variables are NOT used for the
    transaction: psql does not interpolate :vars inside dollar-quoted (DO $$..$$)
    blocks, so int() coercion is the injection barrier here.

HARD CONSTRAINTS (mirror dune-spice-toggle.py / dune-grant.sh):
  * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session
    into the ALREADY-RUNNING DB pod.
  * Re-validate every field here (defence in depth on top of the dispatcher gate).
"""

import argparse
import base64
import json
import re
import subprocess
import sys

# Exchange / proc constants (verified against the live schema 2026-06-05).
EXCHANGE_ID = 2                 # the single global CHOAM exchange
PURCHASED_COMPLETION_TYPE = 5   # buyer-owned completed order in escrow (Completed tab)
SOLD_COMPLETION_TYPE = 4        # seller-side fulfilled marker
BUYER_FEE = 0                   # no buyer-side fee; fees are seller-side
BANK_CURRENCY_ID = 0            # player_virtual_currency_balances Solari = currency_id 0
DEFAULT_MAX_ORDERS = 100        # safe high default if the game cap is not supplied

DQ = "/root/dq.sh"             # read-only wrapper (resolves the DB pod itself)
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# Clean error tokens the relay/portal switch on. Order matters: most-specific first.
ERROR_TOKENS = (
    "order_gone",
    "revision_drift",
    "count_exceeds_stack",
    "insufficient_bank",
    "purge_time_unresolved",
    "fulfill_failed",
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


def _dq(sql, timeout=25):
    """Run one read-only SQL through dq.sh (-tAc). Returns stripped stdout or ""."""
    try:
        r = subprocess.run([DQ, "-tAc", sql], capture_output=True,
                           text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        # fail closed (-> read_failed JSON) if dq.sh is missing/non-exec/times out
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def resolve_db_pod():
    """Resolve namespace + DB pod by label so a Funcom redeploy that rotates the
    pod hash never writes to a stale pod. Mirrors dune-spice-toggle.resolve_db_pod."""
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


def run_psql(ns, pod, sql, timeout=45):
    """The ONLY place a write DB connection is opened. Reads POSTGRES_PASSWORD from
    the pod env then execs psql inside the already-running pod. Mirrors
    dune-spice-toggle.run_psql. SQL arrives on stdin (no large argv, no shell)."""
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


def read_plan(order_id, revision, buyer, count):
    """Read-only lookup of the listing + bank + game purge_time. order_id/buyer are
    validated ints, so direct interpolation is injection-safe. Always returns one
    row (LEFT JOIN on a synthetic qid) so a missing order surfaces as found=false."""
    sql = (
        "SELECT json_build_object("
        "  'found', (o.id IS NOT NULL),"
        "  'revision', o.revision,"
        "  'unit_price', o.item_price,"
        "  'is_npc', o.is_npc_order,"
        "  'template_id', o.template_id,"
        "  'stack', s.initial_stack_size,"
        "  'bank', (SELECT balance FROM dune.player_virtual_currency_balances"
        f"      WHERE player_controller_id={buyer} AND currency_id={BANK_CURRENCY_ID}),"
        "  'purge_time', ((SELECT MAX(expiration_time) FROM dune.dune_exchange_orders"
        "      WHERE is_npc_order=FALSE AND expiration_time IS NOT NULL"
        "        AND expiration_time<1000000000) - 86400 + 2592000)"
        ") "
        f"FROM (SELECT {order_id}::bigint AS qid) q "
        "LEFT JOIN dune.dune_exchange_orders o ON o.id=q.qid "
        "LEFT JOIN dune.dune_exchange_sell_orders s ON s.order_id=o.id"
    )
    raw = _dq(sql)
    if not raw:
        fail("read_failed", 1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fail("read_failed", 1)


def preflight(plan, revision, count):
    """Return the list of contract preconditions the buy would violate, in the same
    tokens the live write RAISEs. Used by --dry-run; the live txn re-checks all of
    these atomically under row locks so this is advisory only."""
    errs = []
    if not plan.get("found"):
        return ["order_gone"]
    if plan.get("revision") != revision:
        errs.append("revision_drift")
    stack = plan.get("stack")
    if stack is None or count > stack:
        errs.append("count_exceeds_stack")
    unit = plan.get("unit_price") or 0
    bank = plan.get("bank")
    if bank is None or bank < unit * count:
        errs.append("insufficient_bank")
    if plan.get("purge_time") is None:
        errs.append("purge_time_unresolved")
    return errs


def build_write_sql(order_id, revision, buyer, count, max_orders):
    """The contract's CORRECT one-transaction write. All five params are validated
    positive ints folded in as integer literals (psql -v cannot reach inside the
    dollar-quoted DO block). The DO block locks + re-reads the order, verifies
    revision + stack, locks + verifies the bank, funds the exchange wallet with the
    ONE modify_user_solari_balance call, computes the game purge_time, then fulfills
    to the Completed tab; any RAISE rolls the whole txn (incl. funding) back. The
    success row lands in a session temp table that the trailing SELECT emits."""
    return f"""\\set ON_ERROR_STOP on
-- The exchange procs reference their tables/functions UNQUALIFIED internally
-- (e.g. dune_exchange_get_user_id, player_virtual_currency_balances, get_solaris_id);
-- the bot connects with search_path=dune,public. dq.sh's psql does NOT, so without
-- this the qualified proc call resolves but its internal refs fail -> write_failed.
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _buy_result;
CREATE TEMP TABLE _buy_result(
  item_id bigint, template_id text, unit_price bigint, total bigint,
  bank_before bigint, bank_after bigint
) ON COMMIT PRESERVE ROWS;
DO $buy$
DECLARE
  v_rev bigint; v_price bigint; v_npc bool; v_avail bigint; v_tpl text;
  v_bank bigint; v_total bigint; v_purge bigint; v_after bigint;
  v_res dune.duneexchangefulfillsellorderresult;
BEGIN
  -- 1. lock + reread the listing; verify it still matches what the buyer saw
  SELECT o.revision, o.item_price, o.is_npc_order, o.template_id, s.initial_stack_size
    INTO v_rev, v_price, v_npc, v_tpl, v_avail
    FROM dune.dune_exchange_orders o
    JOIN dune.dune_exchange_sell_orders s ON s.order_id = o.id
    WHERE o.id = {order_id} FOR UPDATE OF o;
  IF NOT FOUND THEN RAISE EXCEPTION 'order_gone'; END IF;
  IF v_rev <> {revision} THEN
    RAISE EXCEPTION 'revision_drift have=% want=%', v_rev, {revision};
  END IF;
  IF {count} > v_avail THEN
    RAISE EXCEPTION 'count_exceeds_stack avail=%', v_avail;
  END IF;
  v_total := v_price * {count};   -- buyer fee 0
  -- 2. lock + verify the bank can cover it (avoids the modify-clamp stranding funds)
  SELECT balance INTO v_bank FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {buyer} AND currency_id = {BANK_CURRENCY_ID} FOR UPDATE;
  IF v_bank IS NULL OR v_bank < v_total THEN
    RAISE EXCEPTION 'insufficient_bank have=% need=%', COALESCE(v_bank,0), v_total;
  END IF;
  -- 3. fund the exchange wallet from the bank (ONE call: atomic bank-debit +
  --    wallet-credit inside the proc; do NOT also touch vcb here)
  PERFORM dune.dune_exchange_modify_user_solari_balance({buyer}, v_total);
  -- 4. game-time purge_time, exactly like the bot's learn_game_epoch (NON-NPC only)
  SELECT (MAX(expiration_time) - 86400) + 2592000 INTO v_purge
    FROM dune.dune_exchange_orders
    WHERE is_npc_order = FALSE AND expiration_time IS NOT NULL
      AND expiration_time < 1000000000;
  -- fail closed if no non-NPC order anchors the game clock (NULL would otherwise
  -- pass straight into fulfill); rolls back the funding done in step 3
  IF v_purge IS NULL THEN RAISE EXCEPTION 'purge_time_unresolved'; END IF;
  -- 5. fulfill -> Completed tab (dst NULL); engine re-verifies revision + spends wallet
  SELECT * INTO v_res FROM dune.dune_exchange_fulfill_sell_order(
    {EXCHANGE_ID}, {max_orders}, {PURCHASED_COMPLETION_TYPE}, {SOLD_COMPLETION_TYPE},
    {buyer}, {order_id}, {revision}, NULL, NULL, {count}, {BUYER_FEE}, v_purge);
  IF v_res.item_id IS NULL OR v_res.item_id = 0 THEN
    RAISE EXCEPTION 'fulfill_failed (order slots full, revision drift, or escrow move failed)';
  END IF;
  -- 6. record the outcome for the trailing SELECT (bank re-read post-funding)
  SELECT balance INTO v_after FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {buyer} AND currency_id = {BANK_CURRENCY_ID};
  INSERT INTO _buy_result VALUES (v_res.item_id, v_tpl, v_price, v_total, v_bank, v_after);
END $buy$;
COMMIT;
SELECT json_build_object(
  'ok', true,
  'order_id', {order_id},
  'item_id', item_id,
  'template_id', template_id,
  'count', {count},
  'unit_price', unit_price,
  'total_debited', total,
  'bank_before', bank_before,
  'bank_after', bank_after
) FROM _buy_result;
"""


def parse_psql_error(stderr):
    """Map a psql/plpgsql RAISE message to a clean error token."""
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
        if line.startswith(("INSERT ", "UPDATE ", "SELECT ", "NOTICE")):
            continue
        raw = line
    return raw


def do_dry_run(order_id, revision, buyer, count, max_orders):
    plan = read_plan(order_id, revision, buyer, count)
    if not plan.get("found"):
        fail("order_gone", 1)
    unit = plan.get("unit_price") or 0
    bank = plan.get("bank")
    total = unit * count
    out = {
        "dry_run": True,
        "order_id": order_id,
        "revision": revision,
        "template_id": plan.get("template_id"),
        "unit_price": unit,
        "count": count,
        "total": total,
        "bank_before": bank,
        "bank_after_projected": (bank - total) if isinstance(bank, int) else None,
        "purge_time": plan.get("purge_time"),
        "max_orders": max_orders,
        "is_npc": plan.get("is_npc"),
        "stack": plan.get("stack"),
        "preflight_errors": preflight(plan, revision, count),
    }
    emit(out, 0)


def do_live(order_id, revision, buyer, count, max_orders):
    ns, pod = resolve_db_pod()
    sql = build_write_sql(order_id, revision, buyer, count, max_orders)
    # Keep the inner psql timeout BELOW the relay's 30s ssh abandon so a slow
    # write rolls back before the relay returns a failure (deterministic ladder;
    # no ambiguous post-return commit). A single-row fulfill is sub-second.
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
    """Return (order_id, revision, buyer, count, max_orders, dry_run) from either
    --stdin-json (dispatcher path) or CLI flags. Every field is validated to a
    positive int; max_orders falls back to the safe default."""
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        order_id = pos_int("order_id", payload.get("order_id"))
        revision = pos_int("revision", payload.get("revision"))
        buyer = pos_int("buyer_ctrl", payload.get("buyer_ctrl"))
        count = pos_int("count", payload.get("count"))
        mo = payload.get("max_orders")
        max_orders = pos_int("max_orders", mo) if mo is not None else DEFAULT_MAX_ORDERS
        dry_run = bool(payload.get("dry_run", False))
        return order_id, revision, buyer, count, max_orders, dry_run

    ap = argparse.ArgumentParser(description="Portal Market BUY writer")
    ap.add_argument("--order-id", required=True)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--buyer-ctrl", required=True)
    ap.add_argument("--count", required=True)
    ap.add_argument("--max-orders", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    order_id = pos_int("order_id", a.order_id)
    revision = pos_int("revision", a.revision)
    buyer = pos_int("buyer_ctrl", a.buyer_ctrl)
    count = pos_int("count", a.count)
    max_orders = pos_int("max_orders", a.max_orders) if a.max_orders is not None \
        else DEFAULT_MAX_ORDERS
    return order_id, revision, buyer, count, max_orders, a.dry_run


def main():
    order_id, revision, buyer, count, max_orders, dry_run = gather_args()
    if dry_run:
        do_dry_run(order_id, revision, buyer, count, max_orders)
    else:
        do_live(order_id, revision, buyer, count, max_orders)


if __name__ == "__main__":
    main()
