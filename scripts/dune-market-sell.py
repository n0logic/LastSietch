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



Modes:
  --seller-ctrl N --item-id N --count N --price N --duration-days {1,3,7,14}
    [--max-orders N] [--expected-template T] [--expected-src bank|backpack] [--dry-run]
  --stdin-json    read the same fields as one JSON object on stdin (dispatcher path);
                  fields {seller_ctrl, item_id, count, price, duration_days,
                          max_orders?, expected_template?, expected_src?, dry_run?}

Correctness rules (non-negotiable):
  * SOURCE-GATE (STOP-SHIP, added 2026-08-03): container- and vehicle-sourced
    listings are refused unless /etc/lastsietch/market-sell-container-enabled exists.
    The offline gate below covers the SELLER, but a base container is held in its
    partition server's memory for as long as that PARTITION is up, which the
    seller's session has nothing to do with. Listing moves the item into escrow, so
    a later container save re-materialises it in the box while the escrow copy still
    backs a live order. Bank-sourced listings are unaffected: inv_type 30 re-hydrates
    from the DB at login. Enforced in BOTH preflight() and do_live() -- preflight
    alone would only cover --dry-run.
  * BACKPACK-GATE (added 2026-09-03): the pawn's own backpack IS covered by the
    offline gate (it re-hydrates from the DB at login like the bank), so it is a safe
    source mechanically. It still gets its own kill switch,
    /etc/lastsietch/market-sell-backpack-enabled, so the new lane can be closed without
    also closing the bank lane that has been live since June. Same enforcement as the
    container gate: BOTH preflight() and do_live().
  * OFFLINE-GATE (STOP-SHIP): the source item lives in an inventory that is RAM-backed
    while the player is online (clobbered on save-tick). Refuse unless EVERY non-Deleted
    character on the seller's ACCOUNT is 'Offline' and outside reconnect grace, read
    under FOR SHARE so a login cannot flip the answer between the check and the write.
    No row, a NULL status or a NULL character_state all read as ONLINE: undetermined is
    locked, never allowed. Mirrors lib/dune-take-item.sh's account-wide gate rather than
    the older per-character one, and matches what the portal already tells the player
    (its own online check is account-level).
  * EXPECTED_SRC (optional): the portal resolves which of the seller's two pawn
    inventories the item sits in BEFORE it calls, and passes 'bank' or 'backpack'. The
    write txn re-derives the source from the LOCKED items row's inventory and RAISEs
    item_not_found on a mismatch, so an item that moved between the portal's read and
    our write is refused instead of listed out of the wrong bag. Absent = no check
    (the pre-2026-09-03 contract); the value is a closed set, never free text.
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
import os
import re
import subprocess
import sys

# Exchange / proc constants (verified against the live schema 2026-06-05).
EXCHANGE_ID = 2                 # the single global CHOAM exchange
ACCESS_POINT_ID = 1            # HarkoVillage_AP
BANK_CURRENCY_ID = 0           # player_virtual_currency_balances Solari = currency_id 0
DEFAULT_MAX_ORDERS = 100       # safe high default if the game cap is not supplied
VALID_DURATIONS = (1, 3, 7, 14)
# The only two inventories a portal listing may come from. Both are pawn-side, so both
# are covered by the seller-offline gate; the portal names one and we verify it.
VALID_SOURCES = ("bank", "backpack")
# V1 durability simplification: the market bot lists every order at 1.0/1.0 and
# the engine wear-adjusts via in_wear_normalized_item_price (which we pass = price).
# Refine for true durability later (true max is template-derived, not in dune.items).
DURABILITY_CUR = "1.0"
DURABILITY_MAX = "1.0"

DQ = "/root/dq.sh"             # read-only wrapper (resolves the DB pod itself)
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# Container-sourced listing kill-switch. PRESENCE of the file = ENABLED, matching
# /etc/lastsietch/reward-enabled and /etc/lastsietch/augment-enabled; `rm` is the kill-switch
# and absence is the safe default. A FLAG FILE rather than an env var on purpose: the
# dispatcher is invoked as `ssh host market-sell`, a non-interactive non-login shell
# that inherits no profile, so an exported variable would never reach this process
# (the trap that made LASTSIETCH_AUGMENT_ENABLED a no-op until 2026-08-02).
#
# Dark since 2026-08-03: listing MOVES the item into the exchange escrow via the
# engine proc, and a base container or spawned vehicle is held in its partition
# server's memory for as long as that partition is up -- independent of whether the
# SELLER is offline. If that partition later saves over the container, the box gets
# the item back while the escrow copy still backs a live order: one item, two places.
# The CHOAM bank is exempt because it re-hydrates from the DB at login.
CONTAINER_SOURCE_FLAG = "/etc/lastsietch/market-sell-container-enabled"

# Backpack-sourced listing kill-switch, same file-presence semantics and the same
# reason for being a file rather than an env var. Default OFF at ship: the writer has
# admitted the backpack since 2026-08-25 but nothing offered it, so this flag is what
# actually opens the lane, one deliberate `touch` at a time. The backpack is NOT here
# for the container's reason -- it re-hydrates from the DB at login exactly like the
# bank, so the seller-offline gate does cover it. It is here so the new lane can be
# shut without shutting the bank lane with it.
BACKPACK_SOURCE_FLAG = "/etc/lastsietch/market-sell-backpack-enabled"

# Clean error tokens the relay/portal switch on. Order matters: most-specific first.
ERROR_TOKENS = (
    "backpack_source_disabled",
    "container_source_disabled",
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


def valid_src(val):
    """Optional expected_src -> 'bank' or 'backpack', or None. CLOSED SET: anything
    else fails closed rather than being dropped, because a dropped source pin would
    let a write land against whichever inventory the item happens to be in now."""
    if val is None:
        return None
    if not isinstance(val, str) or val not in VALID_SOURCES:
        fail("bad_expected_src", 2)
    return val


def plan_src(plan):
    """Classify the RESOLVED source inventory from a read_plan() row: 'bank',
    'backpack', or None for anything else (container / vehicle / not ours)."""
    if plan.get("src_is_bank"):
        return "bank"
    if plan.get("src_is_backpack"):
        return "backpack"
    return None


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
    """The ONLY place a write DB connection is opened. Uses POSTGRES_PASSWORD inside the pod shell, which execs psql inside the already-running pod. Mirrors
    dune-market-buy.run_psql. SQL arrives on stdin; the pod shell only forwards psql arguments."""
    try:
        auth_out = subprocess.run(
            ["sudo", "kubectl", "exec", "-n", ns, pod, "--",
             "sh", "-c", 'test -n "${POSTGRES_PASSWORD:-}"'],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout reading POSTGRES_PASSWORD", 3)
    if auth_out.returncode != 0:
        fail("no_pgpass", 3)
    cmd = ["sudo", "kubectl", "exec", "-i", "-n", ns, pod, "--",
           "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_",
           "-h", "localhost", "-p", DB_PORT, "-U", DB_USER, "-d", DB_NAME,
           "-tA", "-v", "ON_ERROR_STOP=1"]
    try:
        return subprocess.run(cmd, input=sql, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        fail("psql_timeout", 1)


def owned_inv_sql(seller):
    """The seller's OWNED-inventory id set, keyed by player_controller_id. Four chains:
    base containers / CHOAM bank inv_type 30 / non-DD vehicle cargo (the three from
    scripts/dune-containers.py's container browser) / the pawn's own backpack, added
    2026-08-25. seller is a validated int.

    🔴 Membership here is NOT permission to sell. `src_is_pawn` below decides that, and
    containers + vehicles stay behind CONTAINER_SOURCE_FLAG because the seller-offline
    gate does not cover them (they live in the partition's RAM). The backpack IS covered
    by that gate, which is why it sits with the bank rather than behind the flag."""
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
  UNION
  -- The pawn's OWN backpack. 🔴 inventory_type 0 is ALSO vehicle cargo, which the chain
  -- directly above reaches through a VEHICLE actor; this one joins through
  -- encrypted_player_state so it can only ever resolve the player's own backpack.
  -- Do not merge the two chains, and do not relax this join to reach an actor.
  SELECT inv.id
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    WHERE inv.inventory_type = 0
      AND eps.player_controller_id = {seller}
      AND eps.character_state IS DISTINCT FROM 'Deleted'
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
        "), acct AS (\n"
        f"  SELECT account_id FROM dune.encrypted_player_state\n"
        f"    WHERE player_controller_id = {seller}\n"
        "      AND character_state IS DISTINCT FROM 'Deleted' LIMIT 1\n"
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
        f"  'src_is_bank', EXISTS(SELECT 1 FROM dune.inventories inv\n"
        f"      JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id\n"
        f"      WHERE inv.id = (SELECT inventory_id FROM it)\n"
        f"        AND inv.inventory_type = 30\n"
        f"        AND eps.player_controller_id = {seller}\n"
        # Same tombstone filter as the pawn and backpack chains: a Deleted re-roll's
        # bank must not classify as the source (2026-07-16, acct 3563 shape).
        f"        AND eps.character_state IS DISTINCT FROM 'Deleted'),\n"
        # PAWN-side source = bank OR the player's own backpack. Both are covered by the
        # seller-offline gate because the pawn re-hydrates from the DB; containers and
        # vehicles are not, and stay behind CONTAINER_SOURCE_FLAG.
        f"  'src_is_pawn', EXISTS(SELECT 1 FROM dune.inventories inv\n"
        f"      JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id\n"
        f"      WHERE inv.id = (SELECT inventory_id FROM it)\n"
        f"        AND inv.inventory_type IN (0, 30)\n"
        f"        AND eps.character_state IS DISTINCT FROM 'Deleted'\n"
        f"        AND eps.player_controller_id = {seller}),\n"
        # Backpack = inv_type 0 reached THROUGH encrypted_player_state, so it can only
        # ever be this controller's own bag; vehicle cargo is also type 0 but hangs off a
        # vehicle actor and never resolves here. Its own kill switch reads this.
        f"  'src_is_backpack', EXISTS(SELECT 1 FROM dune.inventories inv\n"
        f"      JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id\n"
        f"      WHERE inv.id = (SELECT inventory_id FROM it)\n"
        f"        AND inv.inventory_type = 0\n"
        f"        AND eps.character_state IS DISTINCT FROM 'Deleted'\n"
        f"        AND eps.player_controller_id = {seller}),\n"
        f"  'online_status', (SELECT online_status FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {seller} LIMIT 1),\n"
        f"  'in_grace', (SELECT (reconnect_grace_period_end IS NOT NULL\n"
        f"      AND reconnect_grace_period_end > NOW()) FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {seller} LIMIT 1),\n"
        # ACCOUNT-WIDE offline read, matching the write txn's gate. online_status /
        # in_grace above stay for the dry-run report; these two are what preflight
        # actually judges on. An unresolvable account reads 0 characters, which is the
        # locked answer.
        f"  'acct_chars', (SELECT count(*) FROM dune.encrypted_player_state\n"
        f"      WHERE account_id = (SELECT account_id FROM acct)\n"
        f"        AND character_state IS DISTINCT FROM 'Deleted'),\n"
        f"  'acct_offline', (SELECT count(*) FILTER (WHERE online_status = 'Offline'\n"
        f"        AND NOT COALESCE(reconnect_grace_period_end > NOW(), false))\n"
        f"      FROM dune.encrypted_player_state\n"
        f"      WHERE account_id = (SELECT account_id FROM acct)\n"
        f"        AND character_state IS DISTINCT FROM 'Deleted'),\n"
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


def preflight(plan, count, expected_template, fee=None, expected_src=None):
    """Return the contract preconditions the listing would violate, in the same
    tokens the live write RAISEs. Used by --dry-run; the live txn re-checks all of
    these atomically under row locks so this is advisory only. Pass fee so the bank
    check (live DO-block step 6) is mirrored here too."""
    errs = []
    if not plan.get("found"):
        return ["item_not_found"]
    if expected_template is not None and plan.get("template_id") != expected_template:
        errs.append("item_not_found")  # identity mismatch / swapped item
    if expected_src is not None and plan_src(plan) != expected_src:
        errs.append("item_not_found")  # source mismatch / item moved between read+write
    # Source gates BEFORE the offline gate: seller-offline is the check we now know is
    # not sufficient for a container, so refusing on source first gives the player the
    # accurate reason instead of "log out and try again", which would not have helped.
    # The backpack refusal is separate so its copy can name the bank as the way through.
    if plan.get("src_is_backpack") and not os.path.exists(BACKPACK_SOURCE_FLAG):
        errs.append("backpack_source_disabled")
    if not plan.get("src_is_pawn") and not os.path.exists(CONTAINER_SOURCE_FLAG):
        errs.append("container_source_disabled")
    # ACCOUNT-WIDE, fail-closed: 0 characters (unresolvable account, all tombstoned)
    # and a partial count both read as online. Same shape as the write txn's gate.
    chars = plan.get("acct_chars")
    if not chars or plan.get("acct_offline") != chars:
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


def build_write_sql(seller, item_id, count, price, days, max_orders, expected_template,
                    expected_src=None):
    """The contract's CORRECT one-transaction write. All numeric params are validated
    positive ints folded in as integer literals (psql -v cannot reach inside the
    dollar-quoted DO block); expected_template is allowlisted then quoted, and
    expected_src comes from a closed two-value set. The DO block: offline-gates the
    seller's whole ACCOUNT under FOR SHARE, locks + verifies the item (ownership +
    template + stack + source), resolves category, computes the game expiration + fee,
    locks + verifies the bank, funds the exchange wallet with the ONE
    modify_user_solari_balance call, then calls add_sell_order (which debits the wallet
    fee, inserts the order, and moves the stack to escrow). Any RAISE rolls the whole
    txn -- incl. the funding -- back. The success row lands in a temp table the trailing
    SELECT emits."""
    src_check = ""
    if expected_src is not None:
        # Re-derive the source from the LOCKED items row rather than taking the caller's
        # word or reading the item a second time: v_inv is already pinned FOR UPDATE, so
        # classifying THAT inventory is the only reading that cannot race the write.
        # inv_type 30 is the bank; type 0 reached through encrypted_player_state is this
        # controller's own backpack. Anything else leaves v_src NULL, which mismatches.
        src_check = (
            "  SELECT CASE WHEN inv.inventory_type = 30 THEN 'bank' ELSE 'backpack' END\n"
            "    INTO v_src\n"
            "    FROM dune.inventories inv\n"
            "    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id\n"
            "   WHERE inv.id = v_inv\n"
            "     AND inv.inventory_type IN (0, 30)\n"
            "     AND eps.character_state IS DISTINCT FROM 'Deleted'\n"
            f"     AND eps.player_controller_id = {seller}\n"
            "   LIMIT 1;\n"
            f"  IF v_src IS DISTINCT FROM '{expected_src}' THEN\n"
            f"    RAISE EXCEPTION 'item_not_found (source mismatch have=% want=%)', "
            f"COALESCE(v_src, '?'), '{expected_src}';\n"
            "  END IF;\n"
        )
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
  v_acct bigint; v_chars int; v_offline int;
  v_inv bigint; v_tpl text; v_stack bigint; v_quality bigint; v_src text;
  v_mask integer; v_depth smallint;
  v_game_now bigint; v_expire bigint; v_fee bigint;
  v_bank bigint; v_after bigint;
  v_res dune.duneexchangeaddsellorderresult;
BEGIN
  -- 1. OFFLINE-GATE, ACCOUNT-WIDE and fail-closed. The source inventory is RAM-backed
  --    while a session holds it and would clobber on save, so every non-Deleted
  --    character on the seller's account must be Offline and out of reconnect grace.
  --    Locked FOR SHARE first so a login cannot flip online_status between the check
  --    and the escrow move; that blocks the engine's own login write to these rows for
  --    the length of this transaction, and a deadlock against the engine rolls US back,
  --    which is the safe direction. Scoped to the ACCOUNT rather than the selling
  --    character deliberately: it matches lib/dune-take-item.sh and it matches what the
  --    portal already tells the player, whose own online check is account-level.
  SELECT account_id INTO v_acct FROM dune.encrypted_player_state
   WHERE player_controller_id = {seller}
     AND character_state IS DISTINCT FROM 'Deleted' LIMIT 1;
  IF v_acct IS NULL THEN
    RAISE EXCEPTION 'player_online (controller % has no live character row, status undetermined)', {seller};
  END IF;
  PERFORM 1 FROM dune.encrypted_player_state
   WHERE account_id = v_acct
     AND character_state IS DISTINCT FROM 'Deleted'
     FOR SHARE;
  SELECT count(*),
         count(*) FILTER (
           WHERE online_status = 'Offline'
             AND NOT COALESCE(reconnect_grace_period_end > NOW(), false))
    INTO v_chars, v_offline
    FROM dune.encrypted_player_state
   WHERE account_id = v_acct
     AND character_state IS DISTINCT FROM 'Deleted';
  -- No rows, a NULL status, or a NULL character_state all land here: undetermined is
  -- LOCKED, never allowed.
  IF v_chars = 0 THEN
    RAISE EXCEPTION 'player_online (account % has no live character row, status undetermined)', v_acct;
  END IF;
  IF v_offline <> v_chars THEN
    RAISE EXCEPTION 'player_online (% of % characters on account % are online or in reconnect grace)',
                    v_chars - v_offline, v_chars, v_acct;
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
{src_check}  IF {count} > v_stack THEN
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


def do_dry_run(seller, item_id, count, price, days, max_orders, expected_template,
               expected_src=None):
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
        "expected_src": expected_src,
        "src": plan_src(plan),
        "owned": plan.get("owned"),
        "online_status": plan.get("online_status"),
        "in_grace": plan.get("in_grace"),
        "acct_chars": plan.get("acct_chars"),
        "acct_offline": plan.get("acct_offline"),
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
        "preflight_errors": preflight(plan, count, expected_template, fee, expected_src),
    }
    emit(out, 0)


def do_live(seller, item_id, count, price, days, max_orders, expected_template,
            expected_src=None):
    ns, pod = resolve_db_pod()
    # SOURCE GATES (live path). preflight() only runs under --dry-run, and the write's
    # own DO block gates ownership + offline but knows nothing about which chain the
    # item came from, so the refusals have to happen here or a container listing would
    # sail straight through. One extra read before a write that moves an item into
    # escrow is a cheap price for not duplicating it; ONE read serves both gates.
    container_ok = os.path.exists(CONTAINER_SOURCE_FLAG)
    backpack_ok = os.path.exists(BACKPACK_SOURCE_FLAG)
    if not container_ok or not backpack_ok:
        plan = read_plan(seller, item_id)
        if plan.get("found"):
            if not backpack_ok and plan.get("src_is_backpack"):
                fail("backpack_source_disabled", 1)
            if not container_ok and not plan.get("src_is_pawn"):
                fail("container_source_disabled", 1)
    sql = build_write_sql(seller, item_id, count, price, days, max_orders,
                          expected_template, expected_src)
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
    expected_src, dry_run) from either --stdin-json (dispatcher path) or CLI flags.
    Every numeric field is validated to a positive int; duration must be one of
    {1,3,7,14}; max_orders falls back to the safe default; expected_template and
    expected_src are optional, and expected_src is a closed set."""
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
        expected_src = valid_src(payload.get("expected_src"))
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
        ap.add_argument("--expected-src", default=None)
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
        expected_src = valid_src(a.expected_src)
        dry_run = a.dry_run

    if days not in VALID_DURATIONS:
        fail("bad_duration_days", 2)
    return (seller, item_id, count, price, days, max_orders, expected_template,
            expected_src, dry_run)


def main():
    (seller, item_id, count, price, days, max_orders, expected_template,
     expected_src, dry_run) = gather_args()
    if dry_run:
        do_dry_run(seller, item_id, count, price, days, max_orders, expected_template,
                   expected_src)
    else:
        do_live(seller, item_id, count, price, days, max_orders, expected_template,
                expected_src)


if __name__ == "__main__":
    main()
