#!/usr/bin/env python3
"""Portal Item Repair WRITE writer -- lastsietch-dune-resident psql wrapper.

Deployed to <game-host>:/root/dune-repair-write.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `repair-box` / `repair-gear` /
`repair-all` tokens, or directly with --dry-run for read-only QA validation.

A confirmed portal player restores the DURABILITY of their own gear WHILE OFFLINE.
Three actions, two semantics:
  * box        : VANILLA repair of every durable row in one owned container. Tops
                 CurrentDurability up to the row's current DecayedMaxDurability.
  * gear       : VANILLA repair of the player's CARRIED inventories (backpack inv 0,
                 worn armor inv 1, hotbar/weapons inv 15) on the pawn.
  * everything : REFURBISH (factory). All accessible storage (own + clan/shared, any
                 map incl. Deep Desert) + all vehicles/storage-modules + bank + the
                 carried pawn inventories. Writes BOTH CurrentDurability AND
                 DecayedMaxDurability up to the factory MaxDurability (wipes decay).

 (Confirmed
durability model). Mirrors dune-storage-write.py / dune-market-sell.py.

Correctness rules (non-negotiable):
  * OFFLINE-GATE (STOP-SHIP): backpack/equipped/hotbar/bank and base-container
    inventories are RAM-backed while the player is online (clobbered on save-tick).
    EVERY write refuses unless the owner's encrypted_player_state.online_status =
    'Offline' AND NOT inside reconnect_grace_period_end. Re-checked inside the write
    txn under a row lock on the owner's encrypted_player_state. Reads/dry-run never
    gated.
  * OWNERSHIP (STOP-SHIP): never trust the caller. owner_ctrl is resolved SERVER-SIDE
    by the admin-backend. box validates inv_id is a member of owned_inv_sql(owner_ctrl)
    (-> not_owned). gear/everything target only inventories on the owner's own pawn /
    owned_inv_sql. owned_inv_sql is whitelist-gated by permission_actor_rank (any rank)
    and now spans clan/shared bases, all vehicles + storage modules, and Deep Desert
    (widened 2026-07-03). The caller-offline gate covers the CALLER only: for a shared
    base/vehicle another ranked member may be online, in which case their save-tick can
    overwrite the durability bump (benign no-op, self-heals when the base unloads; the
    GREATEST clamp never lowers a value).
  * NEVER LOWER A VALUE: the GREATEST(...) clamp keeps high-scale items (MaxDurability
    absent but values exceed the ceiling) from being reduced. Already-at-ceiling rows
    are skipped (abs(delta) <= 0.01). Rows with no FItemStackAndDurabilityStats are
    skipped (the jsonb `?` filter is self-selecting).
  * REFURBISH writes BOTH Current AND Decayed: writing Current alone gets clamped back
    down to the surviving Decayed value on reload.
  * Every numeric field is validated as a positive int in Python, then folded into the
    DO block as an integer literal (psql -v cannot reach inside dollar-quoted DO blocks,
    so int() coercion is the injection barrier). action is an enum allowlist.

HARD CONSTRAINTS (mirror dune-storage-write.py):
  * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session into
    the ALREADY-RUNNING DB pod.
  * Re-validate every field here (defence in depth on top of the dispatcher gate).

Modes:
  --action box        --owner-ctrl N --inv-id N [--dry-run]
  --action gear       --owner-ctrl N            [--dry-run]
  --action everything --owner-ctrl N            [--dry-run]
  --stdin-json   read the same fields as one JSON object on stdin (dispatcher path);
                 fields {action, owner_ctrl, inv_id?, dry_run?}
"""

import argparse
import json
import re
import subprocess
import sys

# Carried inventories on the pawn: 0=backpack, 1=worn armor, 15=hotbar/weapons.
CARRIED_INV_TYPES = "(0, 1, 15)"
VALID_ACTIONS = ("box", "gear", "everything", "vehicle")

DQ = "/root/dq.sh"             # read-only wrapper (resolves the DB pod itself)
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# Clean error tokens the relay/portal switch on. Order matters: most-specific first.
ERROR_TOKENS = (
    "player_online",
    "not_owned",
    "no_inv",
    "write_failed",
)

# jsonb durability accessors (alias `i`). Kept brace-free so they are f-string safe;
# only the jsonb_set PATH literals carry { } and live in the plain UPDATE constants.
CUR_I = "(i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability')::float8"
DEC_I = "(i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability')::float8"
MAX_I = "(i.stats->'FItemStackAndDurabilityStats'->1->>'MaxDurability')::float8"

# VANILLA repair (box / gear): top Current up to Decayed, never lower. Ends at the WHERE
# clause; the caller appends RETURNING. Plain string (the { } path literals are literal).
VANILLA_UPDATE = r"""    UPDATE dune.items i
    SET stats = jsonb_set(
          i.stats,
          '{FItemStackAndDurabilityStats,1,CurrentDurability}',
          to_jsonb( GREATEST(
            (i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability')::float8,
            (i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability')::float8
          ) ), true)
    WHERE i.inventory_id = ANY(v_target)
      AND i.stats ? 'FItemStackAndDurabilityStats'
      AND COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability')::float8,0)
        < COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability')::float8,0) - 0.01"""

# REFURBISH (everything): Current AND Decayed up to MaxDurability (never lower). Ports
# repairItemDurability from community dune-admin/cmd/dune-admin/db.go. Ends at the WHERE
# clause; the caller appends RETURNING.
REFURBISH_UPDATE = r"""    UPDATE dune.items i
    SET stats = jsonb_set(
          jsonb_set(i.stats,
            '{FItemStackAndDurabilityStats,1,CurrentDurability}', to_jsonb(t.val), true),
          '{FItemStackAndDurabilityStats,1,DecayedMaxDurability}', to_jsonb(t.val), true)
    FROM (
      SELECT i2.id, i2.inventory_id,
             GREATEST(
               COALESCE((i2.stats->'FItemStackAndDurabilityStats'->1->>'MaxDurability')::float8, 100.0),
               COALESCE((i2.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability')::float8, 0),
               COALESCE((i2.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability')::float8, 0)
             ) AS val
      FROM dune.items i2
      WHERE i2.inventory_id = ANY(v_target)
        AND i2.stats ? 'FItemStackAndDurabilityStats'
    ) AS t
    WHERE i.id = t.id
      AND ( abs(COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability')::float8,0) - t.val) > 0.01
         OR abs(COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability')::float8,0) - t.val) > 0.01 )"""


# ---------------------------------------------------------------------------
# VEHICLE MODULE refurbish (action 'vehicle'). INSTALLED parts are NOT items --
# they live in dune.vehicle_modules(vehicle_id, template_id, stats) with durability
# under FVehicleModuleDurabilityStats (Current/Decayed/Max, all optional). The item
# inventories on a vehicle actor hold only cargo/spares, so the item path never
# touches mounted parts. The refurbish ceiling is PER-INSTANCE: each module's own
# MaxDurability (the factory value, with any craft-time spec bonus already baked in),
# floored by its own Decayed/Current so it never lowers a value. Fields are sparse ->
# jsonb_set uses create_missing=false to lift only fields that already exist (never
# fabricates a field).
#
# HISTORY: this used to derive a per-TEMPLATE ceiling = MAX durability observed across
# ALL vehicles server-wide. That laundered every player's part up to the single
# best-crafted copy of that template on the server (Lvl-100 crafting spec gives up to
# +100% durability, so a spec part legitimately reads 200% of base) -- the "repaired to
# 200% integrity" bug. Per-instance clamp is the fix; it also no longer rewrites
# MaxDurability, mirroring the item REFURBISH path.
_MODDUR = "stats->'FVehicleModuleDurabilityStats'->1"


def _resolve_vehicle_sql(owner, inv_id):
    """A scalar subquery: the vehicle_id the seed inv_id belongs to, gated on the caller
    holding ANY permission rank on that vehicle. NULL if not a vehicle inv or not owned."""
    return (
        f"(SELECT v.id FROM dune.inventories seed "
        f"JOIN dune.vehicles v ON v.id = seed.actor_id "
        f"JOIN dune.permission_actor_rank par ON par.permission_actor_id = v.id "
        f"WHERE seed.id = {inv_id} AND par.player_id = {owner} LIMIT 1)"
    )


def _mod_ceil(alias):
    """Per-INSTANCE factory ceiling for one module row: its own MaxDurability, floored
    by its own Decayed/Current so a refurbish never lowers a value. Replaces the old
    cross-vehicle per-template MAX that caused the 200% laundering bug."""
    d = f"{alias}.{_MODDUR}"
    return (
        "GREATEST("
        f"COALESCE(({d}->>'MaxDurability')::float8, 0), "
        f"COALESCE(({d}->>'DecayedMaxDurability')::float8, 0), "
        f"COALESCE(({d}->>'CurrentDurability')::float8, 0))"
    )


def _mod_below(alias):
    """A module has Current or Decayed below its own factory ceiling (MaxDurability is
    the ceiling, so it is never below itself -> not checked). Only decayed rows lift."""
    d = f"{alias}.{_MODDUR}"
    ceil = _mod_ceil(alias)
    return (
        f"{alias}.stats ? 'FVehicleModuleDurabilityStats'\n"
        f"     AND ( COALESCE(({d}->>'CurrentDurability')::float8, {ceil}) < {ceil} - 0.01\n"
        f"        OR COALESCE(({d}->>'DecayedMaxDurability')::float8, {ceil}) < {ceil} - 0.01 )"
    )


def build_vehicle_dry_sql(owner, inv_id):
    """Read-only plan for the vehicle-module refurbish: offline status, ownership, and the
    count of installed modules the live write WOULD lift."""
    return (
        "SET search_path TO dune, public;\n"
        f"WITH veh AS (SELECT {_resolve_vehicle_sql(owner, inv_id)} AS vid),\n"
        "eps AS (\n"
        "  SELECT online_status,\n"
        "         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW()) AS in_grace\n"
        f"    FROM dune.encrypted_player_state WHERE player_controller_id = {owner} LIMIT 1\n"
        "),\n"
        "cand AS (\n"
        "  SELECT m.id FROM dune.vehicle_modules m\n"
        "   WHERE m.vehicle_id = (SELECT vid FROM veh)\n"
        f"     AND {_mod_below('m')}\n"
        ")\n"
        "SELECT json_build_object(\n"
        "  'online_status', (SELECT online_status FROM eps),\n"
        "  'in_grace', (SELECT in_grace FROM eps),\n"
        "  'owned', ((SELECT vid FROM veh) IS NOT NULL),\n"
        "  'target_invs', (SELECT COUNT(*) FROM dune.vehicle_modules WHERE vehicle_id = (SELECT vid FROM veh)),\n"
        "  'repaired_count', (SELECT COUNT(*) FROM cand)\n"
        ");\n"
    )


def build_vehicle_write_sql(owner, inv_id):
    """One-transaction in-place refurbish of a single vehicle's installed modules: lift
    each module's Current/Decayed up to its OWN factory MaxDurability (per-instance
    ceiling; MaxDurability itself is not rewritten). Offline-gated under a row lock;
    RAISE not_owned if the seed inv does not resolve to a vehicle the caller owns.
    owner/inv_id are validated ints folded in as literals."""
    return (
        "\\set ON_ERROR_STOP on\n"
        "SET search_path TO dune, public;\n"
        "BEGIN;\n"
        "DROP TABLE IF EXISTS _rep_result;\n"
        "CREATE TEMP TABLE _rep_result(\n"
        "  action text, semantic text, repaired_count bigint, inventories bigint\n"
        ") ON COMMIT PRESERVE ROWS;\n"
        "DO $repair$\n"
        "DECLARE\n"
        "  v_status text; v_grace bool; v_vid bigint;\n"
        "  v_repaired bigint := 0;\n"
        "BEGIN\n"
        "  SELECT online_status,\n"
        "         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())\n"
        "    INTO v_status, v_grace\n"
        f"    FROM dune.encrypted_player_state WHERE player_controller_id = {owner} LIMIT 1\n"
        "    FOR UPDATE;\n"
        "  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN\n"
        "    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);\n"
        "  END IF;\n"
        f"  v_vid := {_resolve_vehicle_sql(owner, inv_id)};\n"
        "  IF v_vid IS NULL THEN RAISE EXCEPTION 'not_owned'; END IF;\n"
        "  WITH upd AS (\n"
        "    UPDATE dune.vehicle_modules m\n"
        f"    SET stats = jsonb_set(jsonb_set(m.stats,\n"
        f"          '{{FVehicleModuleDurabilityStats,1,CurrentDurability}}', to_jsonb({_mod_ceil('m')}), false),\n"
        f"          '{{FVehicleModuleDurabilityStats,1,DecayedMaxDurability}}', to_jsonb({_mod_ceil('m')}), false)\n"
        "    WHERE m.vehicle_id = v_vid\n"
        f"      AND {_mod_below('m')}\n"
        "    RETURNING 1\n"
        "  )\n"
        "  SELECT COUNT(*) INTO v_repaired FROM upd;\n"
        "  INSERT INTO _rep_result VALUES ('vehicle', 'refurbish', v_repaired, 1);\n"
        "END $repair$;\n"
        "COMMIT;\n"
        "SELECT json_build_object(\n"
        "  'ok', true, 'action', action, 'semantic', semantic,\n"
        "  'repaired_count', repaired_count, 'inventories', inventories\n"
        ") FROM _rep_result;\n"
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
    hash never writes to a stale pod. Mirrors dune-storage-write.resolve_db_pod."""
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


def run_psql(ns, pod, sql, timeout=30):
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
    """The owner's ACCESSIBLE-inventory id set, keyed by player_controller_id. Widened
    2026-07-03 for the 'repair all once/24h' feature: every placeable the player holds
    ANY permission rank on (clan/shared storage included, rank 1/2/3), any building type
    (storage + refineries + fabricators + generators, whatever holds durable rows), ANY
    map INCLUDING Deep Desert; the CHOAM bank; and every vehicle the player holds any
    rank on -- all classes (ornithopters/sandbike/buggy/ContainerVehicle/SandCrawler) and
    all their storage-module inventories, any map. Ownership is still whitelist-gated: the
    player must appear in permission_actor_rank for the actor. owner is a validated int.
    The durable-row `?` filter downstream self-selects, so unlisted inv types are no-ops."""
    return f"""
  SELECT inv.id AS inv_id
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id
    JOIN dune.inventories inv ON inv.actor_id = p.id
    WHERE p.is_hologram = false
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
    JOIN dune.vehicles v ON v.id = par.permission_actor_id
    JOIN dune.inventories inv ON inv.actor_id = v.id
    WHERE par.player_id = {owner}
"""


def carried_inv_sql(owner):
    """Carried inventories on the owner's pawn: backpack/worn/hotbar. Resolves the pawn
    from owner_ctrl via encrypted_player_state. owner is a validated int."""
    return (
        "    SELECT inv.id AS inv_id\n"
        "      FROM dune.inventories inv\n"
        "      WHERE inv.actor_id = (SELECT player_pawn_id FROM dune.encrypted_player_state\n"
        f"                            WHERE player_controller_id = {owner} LIMIT 1)\n"
        f"        AND inv.inventory_type IN {CARRIED_INV_TYPES}"
    )


def target_inv_cte(action, owner, inv_id):
    """SQL fragment producing an `inv_id` column = the target inventory set for the
    action (box/gear/everything -- item inventories). The 'vehicle' action does NOT use
    this: installed parts are dune.vehicle_modules rows, handled by build_vehicle_*_sql."""
    if action == "box":
        return (
            f"    SELECT {inv_id}::bigint AS inv_id\n"
            "      WHERE EXISTS (SELECT 1 FROM (" + owned_inv_sql(owner)
            + f"    ) o WHERE o.inv_id = {inv_id})"
        )
    if action == "gear":
        return carried_inv_sql(owner)
    # everything: owned storage/bank/cargo UNION the carried pawn inventories
    return (
        "    SELECT inv_id FROM (" + owned_inv_sql(owner) + "    ) o\n"
        "    UNION\n" + carried_inv_sql(owner)
    )


# ---------------------------------------------------------------------------
# Read-only plan (for --dry-run). The live txn re-checks all of this atomically
# under a row lock, so these counts are advisory only.
# ---------------------------------------------------------------------------

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


def build_dry_sql(action, owner, inv_id):
    """Read-only plan: offline status + the count of rows the live write WOULD change,
    using the exact predicate the live UPDATE uses. owner/inv_id are validated ints."""
    if action == "vehicle":
        return build_vehicle_dry_sql(owner, inv_id)
    target_cte = target_inv_cte(action, owner, inv_id)
    if action == "everything":
        val = f"GREATEST(COALESCE({MAX_I},100.0), COALESCE({CUR_I},0), COALESCE({DEC_I},0))"
        pred = (f"i.stats ? 'FItemStackAndDurabilityStats'\n"
                f"     AND ( abs(COALESCE({CUR_I},0) - {val}) > 0.01\n"
                f"        OR abs(COALESCE({DEC_I},0) - {val}) > 0.01 )")
    else:
        pred = (f"i.stats ? 'FItemStackAndDurabilityStats'\n"
                f"     AND COALESCE({CUR_I},0) < COALESCE({DEC_I},0) - 0.01")
    if action == "box":
        owned_expr = ("EXISTS (SELECT 1 FROM (" + owned_inv_sql(owner)
                      + f"    ) o WHERE o.inv_id = {inv_id})")
    else:
        owned_expr = "NULL"
    return (
        "SET search_path TO dune, public;\n"
        "WITH eps AS (\n"
        "  SELECT online_status,\n"
        "         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW()) AS in_grace\n"
        f"    FROM dune.encrypted_player_state WHERE player_controller_id = {owner} LIMIT 1\n"
        "),\n"
        "target AS (\n" + target_cte + "\n),\n"
        "cand AS (\n"
        "  SELECT i.inventory_id FROM dune.items i\n"
        "   WHERE i.inventory_id IN (SELECT inv_id FROM target)\n"
        f"     AND {pred}\n"
        ")\n"
        "SELECT json_build_object(\n"
        "  'online_status', (SELECT online_status FROM eps),\n"
        "  'in_grace', (SELECT in_grace FROM eps),\n"
        f"  'owned', {owned_expr},\n"
        "  'target_invs', (SELECT COUNT(*) FROM target),\n"
        "  'repaired_count', (SELECT COUNT(*) FROM cand),\n"
        "  'inventories', (SELECT COUNT(DISTINCT inventory_id) FROM cand)\n"
        ")"
    )


# ---------------------------------------------------------------------------
# Write SQL builder. owner/inv_id are validated positive ints folded in as integer
# literals; action is an enum. psql -v cannot reach inside dollar-quoted DO blocks.
# ---------------------------------------------------------------------------

def build_repair_sql(action, owner, inv_id):
    """One-transaction repair/refurbish. Offline-gates the owner under FOR UPDATE on the
    encrypted_player_state row, builds the target inventory set, runs the vanilla (box/
    gear) or refurbish (everything) UPDATE over it, and reports rows + inventories
    touched. Any RAISE rolls the whole txn back. Brace-bearing UPDATE SQL is concatenated
    (never f-string-formatted) so the jsonb path literals survive intact."""
    if action == "vehicle":
        return build_vehicle_write_sql(owner, inv_id)
    target_cte = target_inv_cte(action, owner, inv_id)
    empty_token = "not_owned" if action == "box" else "no_inv"
    update_sql = REFURBISH_UPDATE if action == "everything" else VANILLA_UPDATE
    semantic = "refurbish" if action == "everything" else "repair"
    return (
        "\\set ON_ERROR_STOP on\n"
        "SET search_path TO dune, public;\n"
        "BEGIN;\n"
        "DROP TABLE IF EXISTS _rep_result;\n"
        "CREATE TEMP TABLE _rep_result(\n"
        "  action text, semantic text, repaired_count bigint, inventories bigint\n"
        ") ON COMMIT PRESERVE ROWS;\n"
        "DO $repair$\n"
        "DECLARE\n"
        "  v_status text; v_grace bool;\n"
        "  v_target bigint[];\n"
        "  v_repaired bigint := 0; v_invs bigint := 0;\n"
        "BEGIN\n"
        "  -- 1. OFFLINE-GATE under a row lock: refuse unless Offline + past grace.\n"
        "  SELECT online_status,\n"
        "         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())\n"
        "    INTO v_status, v_grace\n"
        "    FROM dune.encrypted_player_state\n"
        f"    WHERE player_controller_id = {owner} LIMIT 1\n"
        "    FOR UPDATE;\n"
        "  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN\n"
        "    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);\n"
        "  END IF;\n"
        "  -- 2. build the target inventory set (ownership re-verified here).\n"
        "  SELECT array_agg(inv_id) INTO v_target FROM (\n"
        + target_cte + "\n  ) t;\n"
        f"  IF v_target IS NULL THEN RAISE EXCEPTION '{empty_token}'; END IF;\n"
        "  -- 3. repair/refurbish every qualifying durable row in the target set.\n"
        "  WITH upd AS (\n"
        + update_sql + "\n"
        "    RETURNING i.inventory_id AS inv_id\n"
        "  )\n"
        "  SELECT COUNT(*), COUNT(DISTINCT inv_id) INTO v_repaired, v_invs FROM upd;\n"
        f"  INSERT INTO _rep_result VALUES ('{action}', '{semantic}', v_repaired, v_invs);\n"
        "END $repair$;\n"
        "COMMIT;\n"
        "SELECT json_build_object(\n"
        "  'ok', true, 'action', action, 'semantic', semantic,\n"
        "  'repaired_count', repaired_count, 'inventories', inventories\n"
        ") FROM _rep_result;\n"
    )


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

def do_dry_run(action, owner, inv_id):
    plan = _read_json(build_dry_sql(action, owner, inv_id))
    online = plan.get("online_status")
    in_grace = bool(plan.get("in_grace"))
    offline_ok = online == "Offline" and not in_grace
    preflight = []
    if not offline_ok:
        preflight.append("player_online")
    if action in ("box", "vehicle") and not plan.get("owned"):
        preflight.append("not_owned")
    if action in ("gear", "everything") and (plan.get("target_invs") or 0) == 0:
        preflight.append("no_inv")
    semantic = "refurbish" if action in ("everything", "vehicle") else "repair"
    out = {
        "ok": True, "dry_run": True, "action": action, "semantic": semantic,
        "owner_ctrl": owner,
        "repaired_count": plan.get("repaired_count"),
        "inventories": plan.get("inventories"),
        "online_status": online,
        "in_grace": in_grace,
        "offline_ok": offline_ok,
        "target_invs": plan.get("target_invs"),
        "preflight_errors": preflight,
    }
    if action in ("box", "vehicle"):
        out["inv_id"] = inv_id
        out["owned"] = plan.get("owned")
    emit(out, 0)


def do_live(action, owner, inv_id):
    ns, pod = resolve_db_pod()
    sql = build_repair_sql(action, owner, inv_id)
    out = run_psql(ns, pod, sql, timeout=30)
    if out.returncode != 0:
        fail(parse_psql_error(out.stderr or out.stdout), 1)
    raw = last_json_line(out.stdout)
    if not raw:
        fail("write_failed", 1)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        fail("write_failed", 1)
    if not isinstance(result, dict):
        fail("write_failed", 1)
    result["dry_run"] = False
    emit(result, 0)


def gather_args():
    """Return (action, owner, inv_id, dry_run) from --stdin-json (dispatcher path) or CLI
    flags. box requires inv_id (positive int); gear/everything ignore it (inv_id=None)."""
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        action = payload.get("action")
        owner_raw = payload.get("owner_ctrl")
        inv_raw = payload.get("inv_id")
        dry_run = bool(payload.get("dry_run", False))
    else:
        ap = argparse.ArgumentParser(description="Portal Item Repair writer")
        ap.add_argument("--action", required=True, choices=list(VALID_ACTIONS))
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--inv-id", default=None)
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        action = a.action
        owner_raw = a.owner_ctrl
        inv_raw = a.inv_id
        dry_run = a.dry_run

    if action not in VALID_ACTIONS:
        fail("bad_action", 2)
    owner = pos_int("owner_ctrl", owner_raw)
    if action in ("box", "vehicle"):
        inv_id = pos_int("inv_id", inv_raw)
    else:
        inv_id = None
    return action, owner, inv_id, dry_run


def main():
    action, owner, inv_id, dry_run = gather_args()
    if dry_run:
        do_dry_run(action, owner, inv_id)
    else:
        do_live(action, owner, inv_id)


if __name__ == "__main__":
    main()
