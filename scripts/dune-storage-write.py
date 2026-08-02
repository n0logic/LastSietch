#!/usr/bin/env python3
"""Portal Storage Manager WRITE writer -- lastsietch-dune-resident psql wrapper.

Deployed to lastsietch-dune:/root/dune-storage-write.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `storage-withdraw` / `storage-deposit`
tokens, or directly with --dry-run for read-only QA validation.

A confirmed portal player moves Solari between their CHOAM bank balance (the Credit
side, player_virtual_currency_balances currency_id 0) and SolarisCoin item stacks
(the Coin side) WHILE OFFLINE:
  * WITHDRAW (Credit -> Coin): debit the bank balance by N, create/merge an N-coin
        SolarisCoin stack into the FIRST destination that can hold it, in the owner's
        priority order (delivery_dst_sql): the player's BACKPACK, then the container
        selected in the portal (--dst-inventory-id), then the remaining owned
        containers in list order, then the CHOAM bank last. N capped at 100,000 per
        transfer (the observed live SolarisCoin stack max). The chosen destination is
        returned as dst_inv/dst_kind/dst_slot so the portal can name it back.
        Before 2026-07-25 this hardcoded the bank inv30, so "Withdraw" produced a coin
        stack sitting inside the bank and nothing reached the player -- which is what
        made the UI read as lying.
  * DEPOSIT  (Coin -> Credit): two modes share one writer:
        - sweep : sum EVERY SolarisCoin row across the source set, delete them, credit
                  the sum to the bank balance.
        - amount: consume exactly N from the source set's SolarisCoin stacks (lowest id
                  first across stacks), credit N to the bank balance.
        --src-inventory-id scopes both modes to ONE owned inventory, which is the only
        way backpack coins can be deposited. Omitted => the historic owned_inv_sql set,
        so a sweep never silently starts emptying a player's pockets.

 (Phase 1, incl. the
SUPERVISOR ADDENDUM: WITHDRAW cap = 100,000; bank inv30 max_item_count varies and may be
-1/0; position_index is sparse). Mirrors dune-market-sell.py / dune-market-orders.py.

Correctness rules (non-negotiable):
  * OFFLINE-GATE (STOP-SHIP): the bank item storage (inv_type 30) is RAM-backed while
    the player is online (clobbered on save-tick). EVERY write refuses unless the
    player's encrypted_player_state.online_status='Offline' AND NOT inside
    reconnect_grace_period_end. Re-checked inside the write txn. Reads are never gated.
  * OWNERSHIP (STOP-SHIP): never trust the caller. owner_ctrl is resolved SERVER-SIDE
    by the admin-backend; deposit gathers coins only from deposit_src_sql(owner_ctrl)
    and withdraw only ever delivers into delivery_dst_sql(owner_ctrl). Both resolve to
    the player's own inventories (placed base containers + CHOAM bank inv30 + non-DD
    vehicle cargo + their own backpack), keyed by player_controller_id. DD inventories
    are auto-excluded (owned_inv_sql filters a.map <> 'DeepDesert'). A --dst/--src
    inventory id from the caller is a PREFERENCE only: it is intersected against the
    owned set, so a forged id resolves to nothing rather than to someone else's bag.
    🔴 delivery_dst_sql is deliberately NOT owned_inv_sql: that one is shared VERBATIM
    with dune-market-sell, and adding the backpack to it would hand the market-sell
    path access to players' backpacks.
  * CURRENCY CONSERVATION: WITHDRAW = bank -= N and exactly one SolarisCoin stack +N
    (merge or new). DEPOSIT = coin stacks reduced/deleted by total and bank += total.
    Both in one txn; any RAISE rolls the whole thing back.
  * The bank side uses dune.adjust_player_virtual_currency_balance(ctrl,
    dune.get_solaris_id(), delta) for engine parity. We pre-lock + verify balance>=N on
    WITHDRAW so the proc's negative-clamp / log_cheating branch is unreachable.
  * Coins are destroyed/created with the blessed procs where possible: SWEEP uses
    dune.delete_items(id[]), DEPOSIT(N) uses dune.delete_inventory_item(id, count). A new
    WITHDRAW stack is the G2 column set INSERT proven in production (dune-grant.sh G2).
  * Every numeric field is validated as a positive int in Python, then folded into the
    DO block as an integer literal (psql -v cannot reach inside dollar-quoted DO blocks,
    so int() coercion is the injection barrier). mode is an enum allowlist.

HARD CONSTRAINTS (mirror dune-market-sell.py / dune-market-buy.py):
  * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session into
    the ALREADY-RUNNING DB pod.
  * Re-validate every field here (defence in depth on top of the dispatcher gate).

  * DRAG-DROP MOVE (Tier 3, Coin/item relocation): relocate a WHOLE item stack from one
        OWNED inventory to another (first-empty slot, slot + volume gate). Offline-gated
        like withdraw/deposit. Single-row re-home UPDATE pinned to the source inventory
        (atomic, dupe/loss-proof; same mechanism as the Tier 5 transfer). Ships behind the
        LASTSIETCH_STORAGE_MOVE_ENABLED kill-switch (default OFF -> `move_disabled`, no DB touch).
        Own-account only; DD inventories are excluded as source AND destination.

Modes:
  --action withdraw --owner-ctrl N --amount N [--dry-run]
  --action deposit  --owner-ctrl N --mode sweep [--dry-run]
  --action deposit  --owner-ctrl N --mode amount --amount N [--dry-run]
  --action move     --owner-ctrl N --item-id N --dst-inventory-id N [--expected-template T] [--dry-run]
  --stdin-json   read the same fields as one JSON object on stdin (dispatcher path);
                 fields {action, owner_ctrl, amount?, mode?, item_id?, dst_inventory_id?,
                 expected_template?, dry_run?}
"""

import argparse
import json
import os
import re
import subprocess
import sys

BANK_CURRENCY_ID = 0           # player_virtual_currency_balances Solari = currency_id 0
SOLARIS_TEMPLATE = "SolarisCoin"
SOLARIS_STACK_MAX = 100000     # observed live max SolarisCoin stack (addendum Q1)
WITHDRAW_CAP = 100000          # per-transfer WITHDRAW cap (== one full stack)
VALID_MODES = ("sweep", "amount")

# ---- drag-drop MOVE (Tier 3) --------------------------------------------------
# Kill-switch (game-host env, default 0 = OFF). While off, a live MOVE refuses with
# `move_disabled` WITHOUT opening a DB session; dry-run is always allowed for QA.
MOVE_ENABLED = os.environ.get("LASTSIETCH_STORAGE_MOVE_ENABLED", "1") == "1"
# Optional swapped-item guard: an expected_template must be this safe charset.
TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
# Templates with ZERO effective volume (never volume-gated). SolarisCoin is currency.
ZERO_VOL_TEMPLATES = {SOLARIS_TEMPLATE}
# Pak-derived per-unit volume map {template_id: volume}, shipped beside the writer.
# Refresh on each Funcom content update. Absent file -> empty map (all UNKNOWN ->
# volume_unverified audit, never a hard block).
_VMAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template_volume.json")


def load_vmap():
    try:
        with open(_VMAP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def unit_volume(template, volume_override, vmap):
    """Per-unit volume for one moved item, in the contract's precedence:
    volume_override (if set, authoritative) -> 0 for currency/zero-vol -> VMAP ->
    UNKNOWN (None). Volume is PER-UNIT; the caller multiplies by stack_size."""
    if volume_override is not None:
        try:
            return float(volume_override)
        except (TypeError, ValueError):
            pass
    if template in ZERO_VOL_TEMPLATES:
        return 0.0
    if template in vmap:
        try:
            return float(vmap[template])
        except (TypeError, ValueError):
            return None
    return None

# The SolarisCoin stats jsonb, proven in production by dune-grant.sh G2. Kept as a plain
# string constant so its literal { } never collide with the write-SQL f-strings.
STATS_JSON = '{"FItemStackAndDurabilityStats": [[], {"DecayedMaxDurability": 0.0}]}'

DQ = "/root/dq.sh"             # read-only wrapper (resolves the DB pod itself)
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# Clean error tokens the relay/portal switch on. Order matters: most-specific first
# (a substring like 'bank_full' must not shadow 'dst_full_volume', so DST tokens sit
# ahead of the shorter generic ones).
ERROR_TOKENS = (
    "player_online",
    "dst_on_deep_desert",
    "dst_full_volume",
    "dst_full_slots",
    "dst_no_slots",
    "item_not_found",
    "not_owner",
    "move_failed",
    "move_disabled",
    "no_bank",
    "insufficient_bank",
    "bank_full",
    "no_coins",
    "insufficient_coins",
    "write_failed",
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


def owned_inv_sql(owner):
    """The owner's OWNED-inventory id set, keyed by player_controller_id. VERBATIM from
    dune-market-sell.owned_inv_sql (placed base containers / CHOAM bank inv_type 30 /
    non-DD vehicle cargo). DD inventories are excluded (a.map <> 'DeepDesert'). owner is
    a validated int."""
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
      AND par.player_id = {owner}
  UNION
  SELECT inv.id
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    WHERE inv.inventory_type = 30
      AND eps.player_controller_id = {owner}
  UNION
  SELECT inv.id
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN dune.inventories inv ON inv.actor_id = a.id
         AND inv.inventory_type = 0 AND inv.max_item_count > 0
    WHERE par.rank = 1
      AND par.player_id = {owner}
      AND a.map <> 'DeepDesert'
      AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
           OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
           OR a.class ILIKE '%containervehicle%')
"""


# ---------------------------------------------------------------------------
# Read-only plans (for --dry-run). The live txn re-checks all of these
# atomically under row locks, so these are advisory only.
# ---------------------------------------------------------------------------

def read_withdraw_plan(owner):
    """Read offline status + bank inv30 (id/caps/used) + bank balance + existing
    SolarisCoin stacks in the bank. owner is a validated int -> injection-safe."""
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH bank AS (\n"
        "  SELECT inv.id AS inv_id, inv.max_item_count AS mc, inv.max_item_volume AS mv\n"
        "    FROM dune.inventories inv\n"
        "    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id\n"
        f"   WHERE inv.inventory_type = 30 AND eps.player_controller_id = {owner}\n"
        "   LIMIT 1\n"
        ")\n"
        "SELECT json_build_object(\n"
        f"  'online_status', (SELECT online_status FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {owner} LIMIT 1),\n"
        f"  'in_grace', (SELECT (reconnect_grace_period_end IS NOT NULL\n"
        f"      AND reconnect_grace_period_end > NOW()) FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {owner} LIMIT 1),\n"
        "  'bank_inv', (SELECT inv_id FROM bank),\n"
        "  'bank_max_count', (SELECT mc FROM bank),\n"
        "  'bank_max_volume', (SELECT mv FROM bank),\n"
        "  'bank_used', (SELECT COUNT(*) FROM dune.items\n"
        "      WHERE inventory_id = (SELECT inv_id FROM bank)),\n"
        "  'bank', (SELECT balance FROM dune.player_virtual_currency_balances\n"
        f"      WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID}),\n"
        "  'coin_stacks', (SELECT COALESCE(json_agg(json_build_object(\n"
        "        'id', id, 'stack_size', stack_size, 'position_index', position_index)\n"
        "        ORDER BY position_index), '[]'::json)\n"
        "      FROM dune.items\n"
        "      WHERE inventory_id = (SELECT inv_id FROM bank)\n"
        f"        AND template_id = '{SOLARIS_TEMPLATE}')\n"
        ")"
    )
    return _read_json(sql)


def read_deposit_plan(owner):
    """Read offline status + bank balance + all owned SolarisCoin stacks + total. owner
    is a validated int -> injection-safe."""
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH owned AS (\n"
        f"{owned_inv_sql(owner)}"
        ")\n"
        "SELECT json_build_object(\n"
        f"  'online_status', (SELECT online_status FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {owner} LIMIT 1),\n"
        f"  'in_grace', (SELECT (reconnect_grace_period_end IS NOT NULL\n"
        f"      AND reconnect_grace_period_end > NOW()) FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {owner} LIMIT 1),\n"
        "  'bank', (SELECT balance FROM dune.player_virtual_currency_balances\n"
        f"      WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID}),\n"
        "  'coin_total', (SELECT COALESCE(SUM(stack_size), 0) FROM dune.items\n"
        "      WHERE inventory_id IN (SELECT inv_id FROM owned)\n"
        f"        AND template_id = '{SOLARIS_TEMPLATE}'),\n"
        "  'coin_count', (SELECT COUNT(*) FROM dune.items\n"
        "      WHERE inventory_id IN (SELECT inv_id FROM owned)\n"
        f"        AND template_id = '{SOLARIS_TEMPLATE}'),\n"
        "  'coin_stacks', (SELECT COALESCE(json_agg(json_build_object(\n"
        "        'id', id, 'stack_size', stack_size, 'inventory_id', inventory_id)\n"
        "        ORDER BY id), '[]'::json)\n"
        "      FROM dune.items\n"
        "      WHERE inventory_id IN (SELECT inv_id FROM owned)\n"
        f"        AND template_id = '{SOLARIS_TEMPLATE}')\n"
        ")"
    )
    return _read_json(sql)


def _read_json(sql):
    raw = _dq(sql)
    if not raw:
        fail("read_failed", 1)
    # psql may echo "SET" before the JSON; take the last non-empty non-"SET" line.
    last = ""
    for line in raw.splitlines():
        line = line.strip()
        if line and line != "SET":
            last = line
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        fail("read_failed", 1)


def _is_offline(plan):
    return plan.get("online_status") == "Offline" and not plan.get("in_grace")


def _slot_free(max_count, used):
    """Is a free slot available for a NEW stack? max_count -1 (unlimited) / 0
    (volume-gated only) => always free. Otherwise free iff used < max_count."""
    if max_count is None:
        return True
    if max_count <= 0:
        return True
    if used is None:
        return True
    return used < max_count


def preflight_withdraw(plan, amount):
    """Contract preconditions a withdraw would violate, in the live RAISE tokens."""
    errs = []
    if not _is_offline(plan):
        errs.append("player_online")
    if plan.get("bank_inv") is None:
        errs.append("no_bank")
    bank = plan.get("bank")
    if bank is None or bank < amount:
        errs.append("insufficient_bank")
    # bank_full only matters when no existing stack can absorb the amount via merge.
    stacks = plan.get("coin_stacks") or []
    mergeable = any(
        isinstance(s.get("stack_size"), int) and s["stack_size"] + amount <= SOLARIS_STACK_MAX
        for s in stacks)
    if not mergeable and not _slot_free(plan.get("bank_max_count"), plan.get("bank_used")):
        errs.append("bank_full")
    return errs


def preflight_deposit(plan, mode, amount):
    """Contract preconditions a deposit would violate, in the live RAISE tokens."""
    errs = []
    if not _is_offline(plan):
        errs.append("player_online")
    total = plan.get("coin_total") or 0
    if total <= 0:
        errs.append("no_coins")
    elif mode == "amount" and total < amount:
        errs.append("insufficient_coins")
    return errs


# ---------------------------------------------------------------------------
# Write SQL builders. All params are validated positive ints folded in as integer
# literals; mode is an enum. psql -v cannot reach inside dollar-quoted DO blocks.
# ---------------------------------------------------------------------------

def delivery_dst_sql(owner, dst_inv=None):
    """Candidate destinations for a storage DELIVERY, in the owner's priority order.

    🔴 Deliberately NOT `owned_inv_sql()`. That function is shared VERBATIM with
    dune-market-sell, and adding the pawn backpack to it would silently give the
    market-sell path the ability to source from a player's backpack. Delivery gets
    its own set; the shared one stays exactly as it is.

    Priority (owner's rule, 2026-07-25): backpack if it can take the item, else the
    container selected in the portal, else the remaining containers in list order,
    and the CHOAM bank strictly last so it is a fallback rather than the default --
    the old behaviour of always landing in the bank is what made the UI confusing.

    owner / dst_inv are validated ints -> injection-safe.
    """
    sel = "NULL::bigint" if dst_inv is None else str(dst_inv)
    return f"""
  -- 0: the player's own backpack (inventory_type 0 on their PAWN)
  SELECT inv.id AS inv_id, 0 AS pri, 'backpack'::text AS kind
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    WHERE inv.inventory_type = 0
      AND eps.player_controller_id = {owner}
  UNION ALL
  -- 1: the portal's selected container, 2: every other owned container, 3: bank last
  SELECT o.inv_id,
         CASE WHEN o.inv_id = {sel} THEN 1
              WHEN inv.inventory_type = 30 THEN 3
              ELSE 2 END,
         CASE WHEN inv.inventory_type = 30 THEN 'bank' ELSE 'container' END
    FROM ({owned_inv_sql(owner)}) o
    JOIN dune.inventories inv ON inv.id = o.inv_id
"""


def deposit_src_sql(owner, src_inv=None):
    """Where a DEPOSIT may consume SolarisCoin from.

    No selection => the historic set (`owned_inv_sql`: placed containers, bank,
    vehicle cargo), so an un-updated caller keeps its exact old behaviour and a
    sweep does NOT suddenly start emptying players' pockets.

    With a selection => that ONE inventory, and only if it is genuinely the
    player's -- verified by intersecting against `delivery_dst_sql`, which is the
    set that also includes the pawn backpack. That is what makes backpack coins
    depositable at all, and it keeps "deposit from the box I have selected"
    literal rather than a hint.

    owner / src_inv are validated ints -> injection-safe.
    """
    if src_inv is None:
        return owned_inv_sql(owner)
    return f"""
  SELECT c.inv_id FROM ({delivery_dst_sql(owner)}) c WHERE c.inv_id = {src_inv}
"""


def read_delivery_preview(owner, amount, dst_inv=None):
    """Advisory read: the full candidate list in priority order, each annotated with
    whether it could take `amount` Solari and why. The live txn re-resolves this under
    row locks, so this is for --dry-run and for explaining the choice, never a
    decision. owner / dst_inv / amount are validated ints -> injection-safe."""
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH cand AS (\n"
        f"{delivery_dst_sql(owner, dst_inv)}"
        ")\n"
        # jsonb_agg, NOT json_agg: json_agg puts a newline between array elements,
        # and _read_json only keeps the LAST line of psql output -- so a json_agg
        # result arrives truncated to its final element and fails to parse as
        # `read_failed`. jsonb serialises compactly on one line.
        "SELECT jsonb_agg(x ORDER BY x.pri, x.inv_id) FROM (\n"
        "  SELECT c.inv_id, c.pri, c.kind,\n"
        "         inv.inventory_type, inv.max_item_count AS mic,\n"
        "         (SELECT COUNT(*) FROM dune.items WHERE inventory_id = c.inv_id) AS used,\n"
        "         EXISTS (SELECT 1 FROM dune.items\n"
        f"                  WHERE inventory_id = c.inv_id AND template_id = '{SOLARIS_TEMPLATE}'\n"
        f"                    AND stack_size + {amount} <= {SOLARIS_STACK_MAX}) AS can_merge,\n"
        "         COALESCE(inv.max_item_count, 0) <= 0\n"
        "           OR (SELECT COUNT(*) FROM dune.items WHERE inventory_id = c.inv_id)\n"
        "              < inv.max_item_count AS has_free_slot,\n"
        "         COALESCE(NULLIF(pa.actor_name, 'None'), '') AS name,\n"
        "         p.building_type AS building_type\n"
        "    FROM cand c\n"
        "    JOIN dune.inventories inv ON inv.id = c.inv_id\n"
        "    LEFT JOIN dune.placeables p ON p.id = inv.actor_id\n"
        "    LEFT JOIN dune.permission_actor pa ON pa.actor_id = inv.actor_id\n"
        ") x"
    )
    return _read_json(sql)


def build_withdraw_sql(owner, amount, dst_inv=None):
    """One-transaction WITHDRAW (Credit -> Coin). Offline-gates the owner, locks +
    verifies the bank balance covers N, RESOLVES A DESTINATION in the owner's priority
    order (see delivery_dst_sql), then PREFERS a MERGE into an existing SolarisCoin
    stack there (stack_size + N <= 100000, lowest position_index) else creates a NEW
    stack in that inventory's first-empty slot (max_item_count -1/0 => no slot cap),
    and finally debits the bank via the engine currency proc. Any RAISE rolls back.

    Before 2026-07-25 this hardcoded the bank inv30 as the destination, so "Withdraw"
    converted balance into a coin stack sitting INSIDE the bank -- nothing actually
    moved to the player, which is what made the UI read as lying. The destination is
    now resolved and reported back so the portal can name where it landed.
    """
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _wd_result;
CREATE TEMP TABLE _wd_result(
  coin_id bigint, bank_inv bigint, amount bigint, merged bool,
  bank_before bigint, bank_after bigint,
  dst_inv bigint, dst_kind text, dst_slot bigint
) ON COMMIT PRESERVE ROWS;
DO $withdraw$
DECLARE
  v_status text; v_grace bool;
  v_bank_inv bigint; v_max bigint;
  v_bank bigint; v_after bigint;
  v_coin_id bigint; v_coin_stack bigint; v_first_empty bigint;
  v_merged bool := false;
  v_cand RECORD; v_dst_inv bigint; v_dst_kind text; v_dst_max bigint;
BEGIN
  -- 1. OFFLINE-GATE: the bank item storage is RAM-backed; refuse unless Offline + past grace.
  SELECT online_status,
         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())
    INTO v_status, v_grace
    FROM dune.encrypted_player_state
    WHERE player_controller_id = {owner} LIMIT 1;
  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN
    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);
  END IF;
  -- 2. resolve the bank inv30 (and its slot cap) for this owner
  SELECT inv.id, inv.max_item_count INTO v_bank_inv, v_max
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
    WHERE inv.inventory_type = 30 AND eps.player_controller_id = {owner}
    LIMIT 1;
  IF v_bank_inv IS NULL THEN RAISE EXCEPTION 'no_bank'; END IF;
  -- 3. lock + verify the bank balance can cover the withdrawal
  SELECT balance INTO v_bank FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID} FOR UPDATE;
  IF v_bank IS NULL OR v_bank < {amount} THEN
    RAISE EXCEPTION 'insufficient_bank have=% need=%', COALESCE(v_bank,0), {amount};
  END IF;
  -- 4. DESTINATION: walk the owner's priority order and take the FIRST inventory that
  --    can actually hold the coins -- either by merging into a stack there, or by
  --    having a free slot. Nothing is written during the search; the winning branch
  --    re-does the work below so the mutation stays in one place.
  FOR v_cand IN
    SELECT c.inv_id, c.pri, c.kind, inv.max_item_count AS mic
      FROM ({delivery_dst_sql(owner, dst_inv)}) c
      JOIN dune.inventories inv ON inv.id = c.inv_id
     ORDER BY c.pri ASC, c.inv_id ASC
  LOOP
    -- 4a. can we merge into a stack already sitting here?
    SELECT id, stack_size INTO v_coin_id, v_coin_stack
      FROM dune.items
      WHERE inventory_id = v_cand.inv_id AND template_id = '{SOLARIS_TEMPLATE}'
        AND stack_size + {amount} <= {SOLARIS_STACK_MAX}
      ORDER BY position_index ASC, id ASC
      LIMIT 1 FOR UPDATE;
    IF FOUND THEN
      v_dst_inv := v_cand.inv_id; v_dst_kind := v_cand.kind;
      v_merged := true;
      EXIT;
    END IF;
    -- 4b. no mergeable stack -- is there a free slot? (-1/0 => no slot cap)
    IF v_cand.mic IS NULL OR v_cand.mic <= 0 THEN
      SELECT COALESCE(MAX(position_index) + 1, 0) INTO v_first_empty
        FROM dune.items WHERE inventory_id = v_cand.inv_id;
    ELSE
      SELECT MIN(s.idx) INTO v_first_empty
        FROM generate_series(0, v_cand.mic - 1) AS s(idx)
        WHERE NOT EXISTS (SELECT 1 FROM dune.items
                          WHERE inventory_id = v_cand.inv_id AND position_index = s.idx);
    END IF;
    IF v_first_empty IS NOT NULL THEN
      v_dst_inv := v_cand.inv_id; v_dst_kind := v_cand.kind; v_dst_max := v_cand.mic;
      EXIT;
    END IF;
  END LOOP;
  IF v_dst_inv IS NULL THEN
    -- Every candidate is full. Fail rather than inventing somewhere to put it: the
    -- balance stays untouched and the player is told, which beats a silent landing.
    RAISE EXCEPTION 'no_space (backpack, containers and bank are all full)';
  END IF;
  -- 5. COIN side: merge into the stack we found, else create one in the free slot.
  IF v_merged THEN
    UPDATE dune.items SET stack_size = stack_size + {amount} WHERE id = v_coin_id;
    SELECT position_index INTO v_first_empty FROM dune.items WHERE id = v_coin_id;
  ELSE
    INSERT INTO dune.items
      (inventory_id, stack_size, position_index, template_id, stats,
       quality_level, acquisition_time, is_new)
    VALUES
      (v_dst_inv, {amount}, v_first_empty, '{SOLARIS_TEMPLATE}',
       '{STATS_JSON}'::jsonb, 0, 0, true)
    RETURNING id INTO v_coin_id;
  END IF;
  -- 6. DEBIT the bank (engine currency proc; pre-checked so the clamp is unreachable)
  PERFORM dune.adjust_player_virtual_currency_balance({owner}, dune.get_solaris_id(), -{amount});
  SELECT balance INTO v_after FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID};
  INSERT INTO _wd_result VALUES (v_coin_id, v_bank_inv, {amount}, v_merged, v_bank, v_after,
                                 v_dst_inv, v_dst_kind, v_first_empty);
END $withdraw$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'withdraw',
  'amount', amount, 'merged', merged,
  'coin_id', coin_id, 'bank_inv', bank_inv,
  'bank_before', bank_before, 'bank_after', bank_after,
  -- Where it actually landed. The portal names this back to the player; without it
  -- the message can only say "done", which is the complaint this change fixes.
  'dst_inv', dst_inv, 'dst_kind', dst_kind, 'dst_slot', dst_slot
) FROM _wd_result;
"""


def build_deposit_sql(owner, mode, amount, src_inv=None):
    """One-transaction DEPOSIT (Coin -> Credit). Offline-gates the owner, locks the bank
    vcb row, locks + gathers the SolarisCoin stacks in id order from the source set
    (see deposit_src_sql), then either SWEEPs (delete every stack, credit the sum) or
    consumes exactly N walking stacks in id order (delete_inventory_item), crediting the
    bank. Any RAISE rolls back.

    src_inv scopes the consume to ONE inventory the player owns -- including their
    backpack, whose coins were previously not depositable at all. Without it the source
    set is unchanged from before 2026-07-25, so a sweep never silently starts emptying
    a player's pockets just because this parameter now exists."""
    if mode == "sweep":
        consume = (
            "  IF v_avail = 0 OR v_ids IS NULL THEN RAISE EXCEPTION 'no_coins'; END IF;\n"
            "  PERFORM dune.delete_items(v_ids);\n"
            "  v_total := v_avail;\n"
        )
    else:
        consume = (
            "  IF v_avail = 0 OR v_ids IS NULL THEN RAISE EXCEPTION 'no_coins'; END IF;\n"
            f"  IF v_avail < {amount} THEN\n"
            f"    RAISE EXCEPTION 'insufficient_coins have=% need=%', v_avail, {amount};\n"
            "  END IF;\n"
            f"  v_remaining := {amount};\n"
            "  FOR v_idx IN 1 .. COALESCE(array_length(v_ids, 1), 0) LOOP\n"
            "    EXIT WHEN v_remaining <= 0;\n"
            "    v_take := LEAST(v_stacks[v_idx], v_remaining);\n"
            "    PERFORM dune.delete_inventory_item(v_ids[v_idx], v_take);\n"
            "    v_remaining := v_remaining - v_take;\n"
            "  END LOOP;\n"
            f"  v_total := {amount};\n"
        )
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _dep_result;
CREATE TEMP TABLE _dep_result(
  swept_total bigint, coin_count bigint, mode text,
  bank_before bigint, bank_after bigint,
  src_inv bigint, src_kind text
) ON COMMIT PRESERVE ROWS;
DO $deposit$
DECLARE
  v_status text; v_grace bool;
  v_bank bigint; v_after bigint;
  v_ids bigint[]; v_stacks bigint[]; v_avail bigint; v_count bigint;
  v_total bigint; v_remaining bigint; v_take bigint; v_idx int;
  v_src_kind text;
BEGIN
  -- 1. OFFLINE-GATE (coin stacks may live in RAM-backed inventories)
  SELECT online_status,
         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())
    INTO v_status, v_grace
    FROM dune.encrypted_player_state
    WHERE player_controller_id = {owner} LIMIT 1;
  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN
    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);
  END IF;
  -- 2. lock the bank vcb row (lock ordering symmetry with WITHDRAW: vcb first)
  SELECT balance INTO v_bank FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID} FOR UPDATE;
  -- 3. lock every owned SolarisCoin row in deterministic id order (no deadlock), then
  --    gather ids + stacks (rows already locked in this txn).
  PERFORM id FROM dune.items
    WHERE inventory_id IN (SELECT inv_id FROM (
{deposit_src_sql(owner, src_inv)}    ) owned)
      AND template_id = '{SOLARIS_TEMPLATE}'
    ORDER BY id FOR UPDATE;
  SELECT array_agg(id ORDER BY id), array_agg(stack_size ORDER BY id),
         COALESCE(SUM(stack_size), 0), COUNT(*)
    INTO v_ids, v_stacks, v_avail, v_count
    FROM dune.items
    WHERE inventory_id IN (SELECT inv_id FROM (
{deposit_src_sql(owner, src_inv)}    ) owned)
      AND template_id = '{SOLARIS_TEMPLATE}';
  -- 4. consume the coins
{consume}  -- 5. CREDIT the bank with the destroyed coin value (engine currency proc)
  PERFORM dune.adjust_player_virtual_currency_balance({owner}, dune.get_solaris_id(), v_total);
  SELECT balance INTO v_after FROM dune.player_virtual_currency_balances
    WHERE player_controller_id = {owner} AND currency_id = {BANK_CURRENCY_ID};
  -- Where the coins came from, so the portal can name it back to the player instead of
  -- just saying "deposited". NULL src_inv = the whole owned set (no selection).
  SELECT c.kind INTO v_src_kind
    FROM ({delivery_dst_sql(owner)}) c WHERE c.inv_id = {src_inv if src_inv is not None else 'NULL::bigint'} LIMIT 1;
  INSERT INTO _dep_result VALUES (v_total, v_count, '{mode}', COALESCE(v_bank, 0), v_after,
                                  {src_inv if src_inv is not None else 'NULL::bigint'}, v_src_kind);
END $deposit$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'deposit', 'mode', mode,
  'src_inv', src_inv, 'src_kind', src_kind,
  'swept_total', swept_total, 'coin_count', coin_count,
  'bank_before', bank_before, 'bank_after', bank_after
) FROM _dep_result;
"""


def read_move_item(item_id):
    """Read the moved item's identity (template/volume_override/inventory/stack) so the
    caller can resolve its per-unit volume from the VMAP. owner-independent lookup;
    the live txn re-locks the row and re-checks ownership under FOR UPDATE. Returns a
    dict (fields may be null when the item does not exist)."""
    sql = (
        "SET search_path TO dune, public;\n"
        "SELECT json_build_object(\n"
        "  'template_id', template_id, 'volume_override', volume_override,\n"
        "  'inventory_id', inventory_id, 'stack_size', stack_size)\n"
        f" FROM dune.items WHERE id = {item_id} LIMIT 1"
    )
    raw = _dq(sql)
    if not raw:
        return {}
    last = ""
    for line in raw.splitlines():
        line = line.strip()
        if line and line != "SET":
            last = line
    try:
        return json.loads(last) or {}
    except json.JSONDecodeError:
        return {}


def read_move_plan(owner, item_id, dst_inv):
    """Advisory read for --dry-run: offline status + dest caps/usage + moved-item
    identity. The live txn re-checks all of this atomically under row locks. owner /
    item_id / dst_inv are validated ints -> injection-safe."""
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH owned AS (\n"
        f"{owned_inv_sql(owner)}"
        ")\n"
        "SELECT json_build_object(\n"
        f"  'online_status', (SELECT online_status FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {owner} LIMIT 1),\n"
        f"  'in_grace', (SELECT (reconnect_grace_period_end IS NOT NULL\n"
        f"      AND reconnect_grace_period_end > NOW()) FROM dune.encrypted_player_state\n"
        f"      WHERE player_controller_id = {owner} LIMIT 1),\n"
        f"  'src_inv', (SELECT inventory_id FROM dune.items WHERE id = {item_id}),\n"
        f"  'src_owned', (SELECT EXISTS (SELECT 1 FROM owned\n"
        f"      WHERE inv_id = (SELECT inventory_id FROM dune.items WHERE id = {item_id}))),\n"
        f"  'dst_owned', (SELECT EXISTS (SELECT 1 FROM owned WHERE inv_id = {dst_inv})),\n"
        f"  'dst_mic', (SELECT max_item_count FROM dune.inventories WHERE id = {dst_inv}),\n"
        f"  'dst_miv', (SELECT max_item_volume FROM dune.inventories WHERE id = {dst_inv}),\n"
        f"  'dst_used_slots', (SELECT COUNT(*) FROM dune.items WHERE inventory_id = {dst_inv}),\n"
        f"  'dst_map', (SELECT a.map FROM dune.inventories inv\n"
        f"      LEFT JOIN dune.actors a ON a.id = inv.actor_id WHERE inv.id = {dst_inv}),\n"
        f"  'src_map', (SELECT a.map FROM dune.inventories inv\n"
        f"      LEFT JOIN dune.actors a ON a.id = inv.actor_id\n"
        f"      WHERE inv.id = (SELECT inventory_id FROM dune.items WHERE id = {item_id}))\n"
        ")"
    )
    return _read_json(sql)


def build_move_sql(owner, item_id, dst_inv, expected_template, unit_vol):
    """One-transaction drag-drop MOVE (whole-stack relocation between two OWNED
    inventories). Offline-gates, locks the SOURCE item FOR UPDATE (optional template
    identity guard), re-verifies BOTH source and destination inventories are in
    owned_inv_sql(owner) with an explicit DeepDesert hard-fail, runs the slot + volume
    gate, computes first-empty, then does the single-row re-home UPDATE pinned to the
    source inventory (atomic, dupe/loss-proof; mirrors the Tier 5 transfer mechanism).
    Any RAISE rolls the whole txn back. `unit_vol` is the moved item's per-unit volume
    resolved in Python from the VMAP (None => UNKNOWN => volume_unverified, never a
    block). owner/item_id/dst_inv are validated ints; expected_template is charset-
    checked; unit_vol is folded as a numeric literal or NULL."""
    tmpl_guard = ""
    if expected_template:
        tmpl_guard = (
            f"  IF v_src_template IS DISTINCT FROM '{expected_template}' THEN\n"
            f"    RAISE EXCEPTION 'item_not_found (template mismatch had=%)', v_src_template;\n"
            f"  END IF;\n")
    unit_vol_lit = "NULL" if unit_vol is None else repr(float(unit_vol))
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _mv_result;
CREATE TEMP TABLE _mv_result(
  item_id bigint, src_inv bigint, dst_inv bigint, first_empty bigint,
  stack_size bigint, volume_unverified bool
) ON COMMIT PRESERVE ROWS;
DO $move$
DECLARE
  v_status text; v_grace bool;
  v_src_inv bigint; v_src_template text; v_src_stack bigint;
  v_src_map text; v_dst_map text;
  v_dst_mic bigint; v_dst_miv bigint;
  v_used_slots bigint; v_used_vol double precision;
  v_first_empty bigint; v_unit_vol double precision := {unit_vol_lit};
  v_move_vol double precision; v_vol_unverified bool := false;
  v_moved bigint;
BEGIN
  -- 1. OFFLINE-GATE: the inventories are RAM-backed while online; refuse unless Offline + past grace.
  SELECT online_status,
         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())
    INTO v_status, v_grace
    FROM dune.encrypted_player_state
    WHERE player_controller_id = {owner} LIMIT 1;
  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN
    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);
  END IF;
  -- 2. lock the SOURCE item row FOR UPDATE; capture identity/stack + its inventory
  SELECT inventory_id, template_id, stack_size
    INTO v_src_inv, v_src_template, v_src_stack
    FROM dune.items WHERE id = {item_id} FOR UPDATE;
  IF v_src_inv IS NULL THEN RAISE EXCEPTION 'item_not_found'; END IF;
{tmpl_guard}  -- 3. OWNERSHIP (source): the source inventory must be in the owner's OWNED set.
  IF NOT EXISTS (SELECT 1 FROM (
{owned_inv_sql(owner)}    ) owned WHERE owned.inv_id = v_src_inv) THEN
    RAISE EXCEPTION 'not_owner (src inv % not owned)', v_src_inv;
  END IF;
  -- explicit DeepDesert hard-fail on source (placed containers are not DD-filtered in owned set)
  SELECT a.map INTO v_src_map FROM dune.inventories inv
    LEFT JOIN dune.actors a ON a.id = inv.actor_id WHERE inv.id = v_src_inv;
  IF v_src_map = 'DeepDesert' THEN RAISE EXCEPTION 'not_owner (src on DeepDesert)'; END IF;
  -- 4. OWNERSHIP (dest) + DeepDesert hard-fail
  IF NOT EXISTS (SELECT 1 FROM (
{owned_inv_sql(owner)}    ) owned WHERE owned.inv_id = {dst_inv}) THEN
    RAISE EXCEPTION 'not_owner (dst inv % not owned)', {dst_inv};
  END IF;
  SELECT a.map INTO v_dst_map FROM dune.inventories inv
    LEFT JOIN dune.actors a ON a.id = inv.actor_id WHERE inv.id = {dst_inv};
  IF v_dst_map = 'DeepDesert' THEN RAISE EXCEPTION 'dst_on_deep_desert'; END IF;
  IF v_src_inv = {dst_inv} THEN RAISE EXCEPTION 'move_failed (same inventory)'; END IF;
  -- 5. resolve dest caps + current usage (used_vol counts UNKNOWN-volume rows as 0: lower bound)
  SELECT max_item_count, max_item_volume INTO v_dst_mic, v_dst_miv
    FROM dune.inventories WHERE id = {dst_inv};
  SELECT COUNT(*), COALESCE(SUM(COALESCE(volume_override, 0) * stack_size), 0)
    INTO v_used_slots, v_used_vol
    FROM dune.items WHERE inventory_id = {dst_inv};
  -- 6. SLOT GATE + first-empty (mic 0 = no slots, -1 = unlimited -> append)
  IF v_dst_mic = 0 THEN
    RAISE EXCEPTION 'dst_no_slots';
  ELSIF v_dst_mic = -1 THEN
    SELECT COALESCE(MAX(position_index) + 1, 0) INTO v_first_empty
      FROM dune.items WHERE inventory_id = {dst_inv};
  ELSE
    SELECT MIN(s.idx) INTO v_first_empty
      FROM generate_series(0, v_dst_mic - 1) AS s(idx)
      WHERE NOT EXISTS (SELECT 1 FROM dune.items
                        WHERE inventory_id = {dst_inv} AND position_index = s.idx);
    IF v_first_empty IS NULL THEN RAISE EXCEPTION 'dst_full_slots (mic=%)', v_dst_mic; END IF;
  END IF;
  -- 7. VOLUME GATE (only when miv > 0). UNKNOWN unit volume -> allow + audit note.
  IF v_dst_miv > 0 THEN
    IF v_unit_vol IS NULL THEN
      v_vol_unverified := true;
    ELSE
      v_move_vol := v_unit_vol * v_src_stack;
      IF v_used_vol + v_move_vol > v_dst_miv THEN
        RAISE EXCEPTION 'dst_full_volume (used=% add=% cap=%)', v_used_vol, v_move_vol, v_dst_miv;
      END IF;
    END IF;
  END IF;
  -- 8. MOVE: single-row re-home pinned to the source inventory (atomic, dupe/loss-proof)
  UPDATE dune.items
     SET inventory_id = {dst_inv}, position_index = v_first_empty, is_new = true
   WHERE id = {item_id} AND inventory_id = v_src_inv;
  GET DIAGNOSTICS v_moved = ROW_COUNT;
  IF v_moved <> 1 THEN RAISE EXCEPTION 'move_failed (rows=%)', v_moved; END IF;
  INSERT INTO _mv_result VALUES ({item_id}, v_src_inv, {dst_inv}, v_first_empty, v_src_stack, v_vol_unverified);
END $move$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'move',
  'item_id', item_id, 'src_inv', src_inv, 'dst_inv', dst_inv,
  'first_empty', first_empty, 'stack_size', stack_size,
  'volume_unverified', volume_unverified
) FROM _mv_result;
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


# ---------------------------------------------------------------------------
# Dry-run + live drivers
# ---------------------------------------------------------------------------

def do_dry_run(action, owner, mode, amount, item_id=None, dst_inv=None,
               expected_template=None):
    if action == "move":
        vmap = load_vmap()
        item = read_move_item(item_id)
        uv = unit_volume(item.get("template_id"), item.get("volume_override"), vmap)
        plan = read_move_plan(owner, item_id, dst_inv)
        errs = []
        if not _is_offline(plan):
            errs.append("player_online")
        if plan.get("src_inv") is None:
            errs.append("item_not_found")
        elif not plan.get("src_owned") or plan.get("src_map") == "DeepDesert":
            errs.append("not_owner")
        if not plan.get("dst_owned"):
            errs.append("not_owner")
        if plan.get("dst_map") == "DeepDesert":
            errs.append("dst_on_deep_desert")
        mic = plan.get("dst_mic")
        if mic == 0:
            errs.append("dst_no_slots")
        emit({
            "dry_run": True, "action": "move", "owner_ctrl": owner,
            "item_id": item_id, "dst_inventory_id": dst_inv,
            "expected_template": expected_template,
            "move_enabled": MOVE_ENABLED,
            "online_status": plan.get("online_status"), "in_grace": plan.get("in_grace"),
            "src_inv": plan.get("src_inv"), "src_owned": plan.get("src_owned"),
            "dst_owned": plan.get("dst_owned"), "dst_map": plan.get("dst_map"),
            "dst_mic": mic, "dst_miv": plan.get("dst_miv"),
            "dst_used_slots": plan.get("dst_used_slots"),
            "moved_template": item.get("template_id"),
            "moved_stack_size": item.get("stack_size"),
            "unit_volume": uv, "volume_unverified": uv is None and (plan.get("dst_miv") or 0) > 0,
            "preflight_errors": errs,
        }, 0)
    elif action == "withdraw":
        plan = read_withdraw_plan(owner)
        stacks = plan.get("coin_stacks") or []
        mergeable = any(
            isinstance(s.get("stack_size"), int)
            and s["stack_size"] + amount <= SOLARIS_STACK_MAX
            for s in stacks)
        bank = plan.get("bank")
        out = {
            "dry_run": True, "action": "withdraw",
            "owner_ctrl": owner, "amount": amount,
            "online_status": plan.get("online_status"),
            "in_grace": plan.get("in_grace"),
            "bank_inv": plan.get("bank_inv"),
            "bank_max_count": plan.get("bank_max_count"),
            "bank_max_volume": plan.get("bank_max_volume"),
            "bank_used": plan.get("bank_used"),
            "bank_before": bank,
            "bank_after_projected": (bank - amount) if isinstance(bank, int) else None,
            "coin_stacks": stacks,
            "will_merge": mergeable,
            "slot_free": _slot_free(plan.get("bank_max_count"), plan.get("bank_used")),
            "preflight_errors": preflight_withdraw(plan, amount),
        }
        # Destination preview: the candidate list the live txn will walk, plus the one
        # it would pick. Advisory -- the live resolution re-runs under row locks.
        cands = read_delivery_preview(owner, amount, dst_inv) or []
        chosen = next((c for c in cands
                       if c.get("can_merge") or c.get("has_free_slot")), None)
        out["dst_selected_hint"] = dst_inv
        out["dst_candidates"] = cands
        out["dst_resolved"] = chosen
        emit(out, 0)
    else:
        plan = read_deposit_plan(owner)
        total = plan.get("coin_total") or 0
        bank = plan.get("bank")
        swept = total if mode == "sweep" else amount
        out = {
            "dry_run": True, "action": "deposit", "mode": mode,
            "owner_ctrl": owner,
            "amount": amount if mode == "amount" else None,
            "online_status": plan.get("online_status"),
            "in_grace": plan.get("in_grace"),
            "bank_before": bank,
            "coin_total": total,
            "coin_count": plan.get("coin_count"),
            "coin_stacks": plan.get("coin_stacks"),
            "swept_total_projected": swept,
            "bank_after_projected": (bank + swept) if isinstance(bank, int) else None,
            "preflight_errors": preflight_deposit(plan, mode, amount),
        }
        emit(out, 0)


def do_live(action, owner, mode, amount, item_id=None, dst_inv=None,
            expected_template=None):
    if action == "move" and not MOVE_ENABLED:
        # Kill-switch off: refuse WITHOUT opening a DB session (defense in depth; the
        # admin-backend also short-circuits). Honest, never a fake success.
        emit({"ok": False, "error": "move_disabled"}, 0)
    ns, pod = resolve_db_pod()
    if action == "withdraw":
        sql = build_withdraw_sql(owner, amount, dst_inv)
    elif action == "deposit":
        # dst_inv carries the portal's selected container for every action; for deposit
        # that means "consume the coins from THIS inventory only" (see deposit_src_sql).
        sql = build_deposit_sql(owner, mode, amount, dst_inv)
    else:  # move
        vmap = load_vmap()
        item = read_move_item(item_id)
        uv = unit_volume(item.get("template_id"), item.get("volume_override"), vmap)
        sql = build_move_sql(owner, item_id, dst_inv, expected_template, uv)
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
    """Return (action, owner, mode, amount, dry_run) from --stdin-json (dispatcher path)
    or CLI flags. withdraw requires amount (1..100000). deposit requires mode; amount is
    required only for mode=amount (any positive int; capped by available coins at write
    time)."""
    item_id = dst_inv = expected_template = None
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        action = payload.get("action")
        owner = pos_int("owner_ctrl", payload.get("owner_ctrl"))
        mode = payload.get("mode")
        amount = payload.get("amount")
        dry_run = bool(payload.get("dry_run", False))
        item_id = payload.get("item_id")
        dst_inv = payload.get("dst_inventory_id") or payload.get("src_inventory_id")
        expected_template = payload.get("expected_template")
    else:
        ap = argparse.ArgumentParser(description="Portal Storage WITHDRAW/DEPOSIT/MOVE writer")
        ap.add_argument("--action", required=True, choices=["withdraw", "deposit", "move"])
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--mode", default=None, choices=["sweep", "amount"])
        ap.add_argument("--amount", default=None)
        ap.add_argument("--item-id", default=None)
        ap.add_argument("--dst-inventory-id", default=None)
        # Alias for the same slot, so a caller can use whichever name reads right for
        # the action. The portal only ever has ONE concept -- "the container you have
        # selected" -- which is a preferred DESTINATION for withdraw, a SOURCE scope for
        # deposit, and the required destination for move.
        ap.add_argument("--src-inventory-id", default=None)
        ap.add_argument("--expected-template", default=None)
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        action = a.action
        owner = pos_int("owner_ctrl", a.owner_ctrl)
        mode = a.mode
        amount = a.amount
        dry_run = a.dry_run
        item_id = a.item_id
        dst_inv = a.dst_inventory_id or a.src_inventory_id
        expected_template = a.expected_template

    if action not in ("withdraw", "deposit", "move"):
        fail("bad_action", 2)

    if action == "withdraw":
        amount = pos_int("amount", amount)
        if amount > WITHDRAW_CAP:
            fail("bad_amount", 2)
        mode = None
        # Optional: the container selected in the portal. Only ever a PREFERENCE --
        # it sits at priority 1, behind the backpack, and the SQL still re-verifies
        # ownership through delivery_dst_sql, so a forged id cannot target someone
        # else's inventory. Absent => resolve without a selection.
        if dst_inv is not None and str(dst_inv).strip() != "":
            dst_inv = pos_int("dst_inventory_id", dst_inv)
        else:
            dst_inv = None
    elif action == "deposit":
        if mode not in VALID_MODES:
            fail("bad_mode", 2)
        if mode == "amount":
            amount = pos_int("amount", amount)
        else:
            amount = 0
        # Optional source scope. Same guarantee as withdraw's destination: a preference
        # that gets intersected against the owner's own inventories in SQL, so a forged
        # id resolves to an empty source set rather than to someone else's coins.
        if dst_inv is not None and str(dst_inv).strip() != "":
            dst_inv = pos_int("src_inventory_id", dst_inv)
        else:
            dst_inv = None
    else:  # move
        item_id = pos_int("item_id", item_id)
        dst_inv = pos_int("dst_inventory_id", dst_inv)
        mode = None
        amount = 0
        if expected_template is not None:
            expected_template = str(expected_template).strip() or None
        if expected_template is not None and not TEMPLATE_RE.match(expected_template):
            fail("bad_template", 2)
    return action, owner, mode, amount, dry_run, item_id, dst_inv, expected_template


def main():
    (action, owner, mode, amount, dry_run,
     item_id, dst_inv, expected_template) = gather_args()
    if dry_run:
        do_dry_run(action, owner, mode, amount, item_id, dst_inv, expected_template)
    else:
        do_live(action, owner, mode, amount, item_id, dst_inv, expected_template)


if __name__ == "__main__":
    main()
