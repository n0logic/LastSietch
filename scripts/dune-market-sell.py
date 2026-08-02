#!/usr/bin/env python3
"""Portal Market SELL writer -- lastsietch-dune-resident psql wrapper.

Deployed to lastsietch-dune:/root/dune-market-sell.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `market-sell` token, or directly
with --dry-run for read-only QA validation.

A confirmed portal player lists an item sitting in one of THEIR OWN persisted base
containers (or CHOAM bank item storage) on the CHOAM exchange, WHILE OFFLINE. This
is our differentiator: the game only lets you list from backpack/vehicle; we list
from a persisted base container. The engine proc dune_exchange_add_sell_order does
ALL of the heavy lifting -- fee debit, order insert, AND the escrow move (splitting
the stack). We only fund the wallet from bank and call the proc, in ONE txn.

See docs/dune-research/MARKET-SELL-BUILD-CONTRACT-2026-06-05.md.

Modes:
  --seller-ctrl N --item-id N --count N --price N --duration-days {1,3,7,14}
    [--max-orders N] [--expected-template T] [--dry-run]
  --stdin-json    read the same fields as one JSON object on stdin (dispatcher path);
                  fields {seller_ctrl, item_id, count, price, duration_days,
                          max_orders?, expected_template?, dry_run?}

Correctness rules (non-negotiable):
  * OFFLINE-GATE (STOP-SHIP): the source item lives in a base container whose
    inventory is RAM-backed while the player is online (clobbered on save-tick).
    Refuse unless the seller's encrypted_player_state.online_status='Offline' AND
    NOT inside reconnect_grace_period_end. Mirrors dune-grant.sh's do_grant gate.
  * OWNERSHIP (STOP-SHIP): never trust the caller. The locked items row's
    inventory_id MUST belong to the seller's OWNED inventory set (placed base
    containers + CHOAM bank inv_type 30 + non-DD vehicle cargo), resolved the same
    way scripts/dune-containers.py's container browser does but keyed by the
    seller's player_controller_id. REJECT otherwise (not_owner).
  * Funding the listing FEE is ONE call to dune_exchange_modify_user_solari_balance(
    seller, fee): that proc atomically debits the bank AND credits the exchange
    wallet. add_sell_order then debits the wallet by in_solari_cost=fee. NEVER also
    UPDATE player_virtual_currency_balances or the bank is double-debited.
  * expiration_time is GAME-TIME, derived exactly like the market bot's
    learn_game_epoch (/opt/lastsietch-market-bot/exchange.py): game_now =
    MAX(expiration_time) over NON-NPC, sub-1e9 orders, minus 24h. The
    is_npc_order=FALSE filter is REQUIRED -- NPC orders carry a 999999999 sentinel
    that would otherwise poison the value. in_expiration_time = game_now +
    duration_days*86400.
  * FEE formula CONFIRMED 2026-06-05 from in-game screenshots, verified across
    three prices x multiple durations (1500@1d=50/3d=120/7d=260/14d=505,
    10000@14d=1780, 500000@3d=20060/7d=40140/14d=75280):
        fee = round(0.01 * price * (days + 1)) + 20 * days
    Implemented as exact integer math (half-up on a positive integer numerator):
        fee = (price * (days + 1) + 50) // 100 + 20 * days
    Postgres ROUND(numeric) is half-away-from-zero; for the positive integer
    numerator price*(days+1) that equals (numerator + 50) // 100, so the python
    preflight and the SQL fee are byte-identical.
  * Every numeric field is validated as a positive int in Python, then folded into
    the DO block as an integer literal. psql -v variables are NOT used for the
    transaction: psql does not interpolate :vars inside dollar-quoted (DO $$..$$)
    blocks, so int() coercion is the injection barrier here. expected_template is
    validated against a strict [A-Za-z0-9_]+ allowlist before it is quoted in.

HARD CONSTRAINTS (mirror dune-market-buy.py / dune-grant.sh):
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
ACCESS_POINT_ID = 1            # HarkoVillage_AP
BANK_CURRENCY_ID = 0           # player_virtual_currency_balances Solari = currency_id 0
DEFAULT_MAX_ORDERS = 100       # safe high default if the game cap is not supplied
VALID_DURATIONS = (1, 3, 7, 14)
# V1 durability simplification: the market bot lists every order at 1.0/1.0 and
# the engine wear-adjusts via in_wear_normalized_item_price (which we pass = price).
# Refine for true durability later (true max is template-derived, not in dune.items).
DURABILITY_CUR = "1.0"
DURABILITY_MAX = "1.0"

DQ = "/root/dq.sh"             # read-only wrapper (resolves the DB pod itself)
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# Clean error tokens the relay/portal switch on. Order matters: most-specific first.
ERROR_TOKENS = (
    "player_online",
    "item_not_found",
    "not_owner",
    "count_exceeds_stack",
    "category_unresolved",
    "expiration_unresolved",
    "insufficient_bank",
    "slots_full",
    "list_failed",
)

# Matches the admin/relay expected_template charset exactly (dev-2 contract):
# [A-Za-z0-9_-]{2,64}. Hyphen is the literal last char of the class; it is only
# ever quoted into a single-quoted SQL string literal, so it is injection-safe.
TEMPLATE_RE = re.compile(r"[A-Za-z0-9_-]{2,64}")


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


def valid_template(val):
    """Optional expected_template -> strict-allowlisted string, or None."""
    if val is None:
        return None
    if not isinstance(val, str) or not TEMPLATE_RE.fullmatch(val):
        fail("bad_expected_template", 2)
    return val


def compute_fee(price, days):
    """CONFIRMED listing fee, exact integer math (half-up). See module docstring."""
    return (price * (days + 1) + 50) // 100 + 20 * days


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
    pod hash never writes to a stale pod. Mirrors dune-market-buy.resolve_db_pod."""
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
    dune-market-buy.run_psql. SQL arrives on stdin (no large argv, no shell)."""
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


def owned_inv_sql(seller):
    """The seller's OWNED-inventory id set, keyed by player_controller_id. Same three
    chains as scripts/dune-containers.py's container browser (base containers /
    CHOAM bank inv_type 30 / non-DD vehicle cargo). seller is a validated int."""
    return f"""
  SELECT inv.id AS inv_id
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN dune.inventories inv ON inv.actor_id = p.id
    WHERE p.is_hologram = false
      AND p.building_type = ANY(ARRAY[
            'SpiceSilo_Placeable','GenericContainer_Placeable',
            'StorageContainer_Placeable','MediumStorageContainer_Placeable']::text[])
      AND par.player_id = {seller}
  UNION
  SELECT inv.id
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    WHERE inv.inventory_type = 30
      AND eps.player_controller_id = {seller}
  UNION
  SELECT inv.id
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN dune.inventories inv ON inv.actor_id = a.id
         AND inv.inventory_type = 0 AND inv.max_item_count > 0
    WHERE par.rank = 1
      AND par.player_id = {seller}
      AND a.map <> 'DeepDesert'
      AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
           OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
           OR a.class ILIKE '%containervehicle%')
"""


def read_plan(seller, item_id):
    """Read-only lookup of the item + ownership + offline status + category + game
    clock + bank. seller/item_id are validated ints, so direct interpolation is
    injection-safe. Returns one JSON object (or fails read_failed)."""
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH it AS (\n"
        f"  SELECT id, inventory_id, template_id, stack_size, quality_level\n"
        f"    FROM dune.items WHERE id = {item_id}\n"
        "), owned AS (\n"
        f"{owned_inv_sql(seller)}"
        ")\n"
        "SELECT json_build_object(\n"
        "  'found', (SELECT id FROM it) IS NOT NULL,\n"
        "  'inventory_id', (SELECT inventory_id FROM it),\n"
        "  'template_id', (SELECT template_id FROM it),\n"
        "  'stack', (SELECT stack_size FROM it),\n"
        "  'quality', (SELECT quality_level FROM it),\n"
        "  'owned', EXISTS(SELECT 1 FROM owned o JOIN it ON o.inv_id = it.inventory_id),\n"
        f"  'online_status', (SELECT online_status FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {seller} LIMIT 1),\n"
        f"  'in_grace', (SELECT (reconnect_grace_period_end IS NOT NULL\n"
        f"      AND reconnect_grace_period_end > NOW()) FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {seller} LIMIT 1),\n"
        "  'cat_mask', (SELECT category_mask FROM dune.dune_exchange_orders\n"
        "      WHERE template_id = (SELECT template_id FROM it) AND category_mask <> 0 LIMIT 1),\n"
        "  'cat_depth', (SELECT category_depth FROM dune.dune_exchange_orders\n"
        "      WHERE template_id = (SELECT template_id FROM it) AND category_mask <> 0 LIMIT 1),\n"
        "  'game_now', ((SELECT MAX(expiration_time) FROM dune.dune_exchange_orders\n"
        "      WHERE is_npc_order = FALSE AND expiration_time IS NOT NULL\n"
        "        AND expiration_time < 1000000000) - 86400),\n"
        "  'bank', (SELECT balance FROM dune.player_virtual_currency_balances\n"
        f"      WHERE player_controller_id = {seller} AND currency_id = {BANK_CURRENCY_ID})\n"
        ")"
    )
    raw = _dq(sql)
    if not raw:
        fail("read_failed", 1)
    # psql may echo "SET" before the JSON; take the last non-empty line.
    last = ""
    for line in raw.splitlines():
        line = line.strip()
        if line and line != "SET":
            last = line
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        fail("read_failed", 1)


def preflight(plan, count, expected_template, fee=None):
    """Return the contract preconditions the listing would violate, in the same
    tokens the live write RAISEs. Used by --dry-run; the live txn re-checks all of
    these atomically under row locks so this is advisory only. Pass fee so the bank
    check (live DO-block step 6) is mirrored here too."""
    errs = []
    if not plan.get("found"):
        return ["item_not_found"]
    if expected_template is not None and plan.get("template_id") != expected_template:
        errs.append("item_not_found")  # identity mismatch / swapped item
    if plan.get("online_status") != "Offline" or plan.get("in_grace"):
        errs.append("player_online")
    if not plan.get("owned"):
        errs.append("not_owner")
    stack = plan.get("stack")
    if stack is None or count > stack:
        errs.append("count_exceeds_stack")
    if not plan.get("cat_mask"):
        errs.append("category_unresolved")
    if plan.get("game_now") is None:
        errs.append("expiration_unresolved")
    bank = plan.get("bank")
    if fee is not None and (bank is None or bank < fee):
        errs.append("insufficient_bank")
    return errs


def build_write_sql(seller, item_id, count, price, days, max_orders, expected_template):
    """The contract's CORRECT one-transaction write. All numeric params are validated
    positive ints folded in as integer literals (psql -v cannot reach inside the
    dollar-quoted DO block); expected_template is allowlisted then quoted. The DO
    block: offline-gates the seller, locks + verifies the item (ownership + template
    + stack), resolves category, computes the game expiration + fee, locks + verifies
    the bank, funds the exchange wallet with the ONE modify_user_solari_balance call,
    then calls add_sell_order (which debits the wallet fee, inserts the order, and
    moves the stack to escrow). Any RAISE rolls the whole txn -- incl. the funding --
    back. The success row lands in a temp table the trailing SELECT emits."""
    tpl_check = ""
    if expected_template is not None:
        tpl_check = (
            f"  IF v_tpl <> '{expected_template}' THEN\n"
            f"    RAISE EXCEPTION 'item_not_found (template mismatch have=% want=%)', "
            f"v_tpl, '{expected_template}';\n"
            "  END IF;\n"
        )
    return f"""\\set ON_ERROR_STOP on
-- The exchange procs reference their tables/functions UNQUALIFIED internally;
-- the bot connects with search_path=dune,public. dq.sh's psql does NOT, so without
-- this the qualified proc call resolves but its internal refs fail -> write_failed.
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _sell_result;
CREATE TEMP TABLE _sell_result(
  order_id bigint, item_id bigint, template_id text, count bigint,
  unit_price bigint, fee bigint, expiration_time bigint,
  bank_before bigint, bank_after bigint
) ON COMMIT PRESERVE ROWS;
DO $sell$
DECLARE
  v_status text; v_grace bool;
  v_inv bigint; v_tpl text; v_stack bigint; v_quality bigint;
  v_mask integer; v_depth smallint;
  v_game_now bigint; v_expire bigint; v_fee bigint;
  v_bank bigint; v_after bigint;
  v_res dune.duneexchangeaddsellorderresult;
BEGIN
  -- 1. OFFLINE-GATE: refuse if the seller is online or still in reconnect grace
  --    (their base-container inventory is RAM-backed and would clobber on save).
  SELECT online_status,
         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())
    INTO v_status, v_grace
    FROM dune.encrypted_player_state
    WHERE player_controller_id = {seller} LIMIT 1;
  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN
    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);
  END IF;
  -- 2. lock + verify the source item: it must exist and its inventory must be in
  --    the seller's OWNED set (defense in depth -- never trust the caller).
  SELECT inventory_id, template_id, stack_size, quality_level
    INTO v_inv, v_tpl, v_stack, v_quality
    FROM dune.items WHERE id = {item_id} FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'item_not_found'; END IF;
{tpl_check}  PERFORM 1 FROM (
{owned_inv_sql(seller)}  ) owned WHERE owned.inv_id = v_inv;
  IF NOT FOUND THEN RAISE EXCEPTION 'not_owner (inv=%)', v_inv; END IF;
  IF {count} > v_stack THEN
    RAISE EXCEPTION 'count_exceeds_stack avail=%', v_stack;
  END IF;
  -- 3. resolve category mask/depth from a live order of the same template; fail
  --    closed rather than list with mask 0 (un-findable / mis-categorised order)
  SELECT category_mask, category_depth INTO v_mask, v_depth
    FROM dune.dune_exchange_orders
    WHERE template_id = v_tpl AND category_mask <> 0 LIMIT 1;
  IF v_mask IS NULL OR v_mask = 0 THEN RAISE EXCEPTION 'category_unresolved'; END IF;
  -- 4. game-time expiration, exactly like the bot's learn_game_epoch (NON-NPC only)
  SELECT (MAX(expiration_time) - 86400) INTO v_game_now
    FROM dune.dune_exchange_orders
    WHERE is_npc_order = FALSE AND expiration_time IS NOT NULL
      AND expiration_time < 1000000000;
  IF v_game_now IS NULL THEN RAISE EXCEPTION 'expiration_unresolved'; END IF;
  v_expire := v_game_now + {days} * 86400;
  -- 5. CONFIRMED listing fee (half-up integer math; see module docstring)
  v_fee := ({price} * ({days} + 1) + 50) / 100 + 20 * {days};
  -- 6. lock + verify the bank can cover the fee (avoids the modify-clamp stranding)
  SELECT balance INTO v_bank FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {seller} AND currency_id = {BANK_CURRENCY_ID} FOR UPDATE;
  IF v_bank IS NULL OR v_bank < v_fee THEN
    RAISE EXCEPTION 'insufficient_bank have=% need=%', COALESCE(v_bank,0), v_fee;
  END IF;
  -- 7. fund the exchange wallet from the bank (ONE call: atomic bank-debit +
  --    wallet-credit inside the proc; do NOT also touch vcb here)
  PERFORM dune.dune_exchange_modify_user_solari_balance({seller}, v_fee);
  -- 8. create the listing: the proc debits the wallet fee, inserts the order, and
  --    moves {count} of the item to the global escrow inv (splitting the stack).
  SELECT * INTO v_res FROM dune.dune_exchange_add_sell_order(
    {EXCHANGE_ID}, {ACCESS_POINT_ID}, {seller}, {max_orders}, v_expire,
    {item_id}, {count}, v_mask, v_depth, {DURABILITY_CUR}, {DURABILITY_MAX},
    {price}, {price}, v_quality, v_fee);
  IF v_res.order_id IS NULL OR v_res.order_id = 0 THEN
    IF COALESCE(v_res.order_slots_used, 0) >= {max_orders} THEN
      RAISE EXCEPTION 'slots_full (used=% max=%)', COALESCE(v_res.order_slots_used,0), {max_orders};
    END IF;
    RAISE EXCEPTION 'list_failed (fee debit or escrow move rejected)';
  END IF;
  -- 9. record the outcome for the trailing SELECT (bank re-read post-funding)
  SELECT balance INTO v_after FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {seller} AND currency_id = {BANK_CURRENCY_ID};
  INSERT INTO _sell_result VALUES (v_res.order_id, v_res.order_id, v_tpl, {count},
    {price}, v_fee, v_expire, v_bank, v_after);
END $sell$;
COMMIT;
SELECT json_build_object(
  'ok', true,
  'order_id', order_id,
  'template_id', template_id,
  'count', count,
  'unit_price', unit_price,
  'fee', fee,
  'expiration_time', expiration_time,
  'expiration_days', {days},
  'bank_before', bank_before,
  'bank_after', bank_after
) FROM _sell_result;
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


def do_dry_run(seller, item_id, count, price, days, max_orders, expected_template):
    plan = read_plan(seller, item_id)
    if not plan.get("found"):
        fail("item_not_found", 1)
    fee = compute_fee(price, days)
    game_now = plan.get("game_now")
    expiration = (game_now + days * 86400) if isinstance(game_now, int) else None
    bank = plan.get("bank")
    out = {
        "dry_run": True,
        "seller_ctrl": seller,
        "item_id": item_id,
        "template_id": plan.get("template_id"),
        "expected_template": expected_template,
        "owned": plan.get("owned"),
        "online_status": plan.get("online_status"),
        "in_grace": plan.get("in_grace"),
        "stack": plan.get("stack"),
        "count": count,
        "quality": plan.get("quality"),
        "unit_price": price,
        "duration_days": days,
        "category_mask": plan.get("cat_mask"),
        "category_depth": plan.get("cat_depth"),
        "game_now": game_now,
        "expiration_time": expiration,
        "fee": fee,
        "bank_before": bank,
        "bank_after_projected": (bank - fee) if isinstance(bank, int) else None,
        "max_orders": max_orders,
        "preflight_errors": preflight(plan, count, expected_template, fee),
    }
    emit(out, 0)


def do_live(seller, item_id, count, price, days, max_orders, expected_template):
    ns, pod = resolve_db_pod()
    sql = build_write_sql(seller, item_id, count, price, days, max_orders,
                          expected_template)
    # Keep the inner psql timeout BELOW the relay's ssh abandon so a slow write
    # rolls back before the relay returns a failure (deterministic ladder; no
    # ambiguous post-return commit). A single add_sell_order is sub-second.
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
    """Return (seller, item_id, count, price, days, max_orders, expected_template,
    dry_run) from either --stdin-json (dispatcher path) or CLI flags. Every numeric
    field is validated to a positive int; duration must be one of {1,3,7,14};
    max_orders falls back to the safe default; expected_template is optional."""
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        seller = pos_int("seller_ctrl", payload.get("seller_ctrl"))
        item_id = pos_int("item_id", payload.get("item_id"))
        count = pos_int("count", payload.get("count"))
        price = pos_int("price", payload.get("price"))
        days = pos_int("duration_days", payload.get("duration_days"))
        mo = payload.get("max_orders")
        max_orders = pos_int("max_orders", mo) if mo is not None else DEFAULT_MAX_ORDERS
        expected_template = valid_template(payload.get("expected_template"))
        dry_run = bool(payload.get("dry_run", False))
    else:
        ap = argparse.ArgumentParser(description="Portal Market SELL writer")
        ap.add_argument("--seller-ctrl", required=True)
        ap.add_argument("--item-id", required=True)
        ap.add_argument("--count", required=True)
        ap.add_argument("--price", required=True)
        ap.add_argument("--duration-days", required=True)
        ap.add_argument("--max-orders", default=None)
        ap.add_argument("--expected-template", default=None)
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        seller = pos_int("seller_ctrl", a.seller_ctrl)
        item_id = pos_int("item_id", a.item_id)
        count = pos_int("count", a.count)
        price = pos_int("price", a.price)
        days = pos_int("duration_days", a.duration_days)
        max_orders = pos_int("max_orders", a.max_orders) if a.max_orders is not None \
            else DEFAULT_MAX_ORDERS
        expected_template = valid_template(a.expected_template)
        dry_run = a.dry_run

    if days not in VALID_DURATIONS:
        fail("bad_duration_days", 2)
    return seller, item_id, count, price, days, max_orders, expected_template, dry_run


def main():
    seller, item_id, count, price, days, max_orders, expected_template, dry_run = \
        gather_args()
    if dry_run:
        do_dry_run(seller, item_id, count, price, days, max_orders, expected_template)
    else:
        do_live(seller, item_id, count, price, days, max_orders, expected_template)


if __name__ == "__main__":
    main()
