#!/usr/bin/env python3
"""Portal "My Bases" blueprint RENAME writer -- <game-host>-resident psql wrapper.

Deployed to <game-host>:/root/dune-blueprint-rename.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `blueprint-rename` token (base64 JSON
job on stdin), or directly with --dry-run for read-only QA validation.

A confirmed portal player renames one of their OWN base blueprints from the web UI. The
display name lives in the parent item's stats JSONB at
  stats -> 'FBuildingBlueprintItemStats' -> 1 -> 'BuildingBlueprintName'
on the BuildingBlueprint_CopyDevice item that backs dune.building_blueprints.item_id.
Funcom seeds the field "New Build" (or omits it entirely on older items, which the My
Bases list renders as "Blueprint #N"). This writer sets/replaces that one field.

Correctness rules (non-negotiable; mirror dune-storage-write.py):
  * OFFLINE-GATE (STOP-SHIP): the blueprint copy device is an INVENTORY item, RAM-backed
    while the player is online (clobbered on the next save-tick). The write refuses unless
    the player's encrypted_player_state.online_status='Offline' AND NOT inside the
    reconnect_grace_period_end. Re-checked inside the write txn under a row lock. Reads
    (dry-run) are never gated.
  * OWNERSHIP (STOP-SHIP): never trust the caller. account_id is resolved SERVER-SIDE by
    the admin-backend from the session's linked characters; the writer re-verifies the
    blueprint's backing item lives in an inventory this account owns (the same owned-
    inventory union as dune-blueprints-list.py) before it will touch a single row. A
    cross-account bp_id returns not_owned, never a write.
  * INJECTION BARRIER: account_id + bp_id are validated as positive ints and folded in as
    integer literals (psql -v cannot reach inside dollar-quoted DO blocks). The new NAME is
    arbitrary user text, so it never enters the SQL as text: Python validates it, then
    re-encodes it to base64 and the SQL decodes it via
    convert_from(decode('<b64>','base64'),'utf8'). The base64 alphabet [A-Za-z0-9+/=]
    contains no quote/metachar, so it cannot break out of the literal.
  * STRUCT GUARD: jsonb_set with create_missing only creates the leaf key, so the write is
    gated on (stats #> '{FBuildingBlueprintItemStats,1}') IS NOT NULL -- the struct object
    must already exist (it always does on a real copy device). A malformed item is refused,
    never repaired blindly.

HARD CONSTRAINTS (mirror dune-storage-write.py):
  * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session into the
    ALREADY-RUNNING DB pod.
  * Re-validate every field here (defence in depth on top of the dispatcher gate).

Modes:
  --account-id N --bp-id N --name-b64 <b64> [--dry-run]
  --stdin-json   read {account_id, bp_id, name, dry_run?} as one JSON object on stdin
                 (dispatcher path; `name` is a plain string -- it travelled inside the
                 base64 job blob and never crossed a shell).
"""

import argparse
import base64
import json
import re
import subprocess
import sys

NAME_MAX = 40                  # character cap for a blueprint display name
DQ = "/root/dq.sh"             # read-only wrapper (resolves the DB pod itself)
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")

ERROR_TOKENS = (
    "no_player",
    "player_online",
    "not_owned",
    "no_struct",
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


def clean_name(name):
    """Validate + normalise a blueprint display name. Returns the trimmed text. Rejects
    empty, over-long, or control-char names (fail-closed)."""
    if not isinstance(name, str):
        fail("bad_name", 2)
    # Collapse internal runs of whitespace to single spaces, strip the ends.
    trimmed = re.sub(r"\s+", " ", name).strip()
    if not trimmed:
        fail("empty_name", 2)
    if len(trimmed) > NAME_MAX:
        fail("name_too_long", 2)
    # No control characters survive normalisation above except none; double-check.
    if any(ord(c) < 32 or ord(c) == 127 for c in trimmed):
        fail("bad_name", 2)
    return trimmed


def name_to_b64(name):
    return base64.b64encode(name.encode("utf-8")).decode("ascii")


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


def _read_json(sql):
    raw = _dq(sql)
    if not raw:
        fail("read_failed", 1)
    last = ""
    for line in raw.splitlines():
        line = line.strip()
        if line and line != "SET":
            last = line
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        fail("read_failed", 1)


def resolve_db_pod():
    """Resolve namespace + DB pod by label so a Funcom redeploy that rotates the pod hash
    never writes to a stale pod. VERBATIM from dune-storage-write.resolve_db_pod."""
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
    """The ONLY place a write DB connection is opened. VERBATIM from
    dune-storage-write.run_psql: reads POSTGRES_PASSWORD from the pod then execs psql
    inside the already-running pod with the SQL on stdin."""
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


def owned_inv_sql(account_id):
    """The account's OWNED-inventory id set. VERBATIM ownership union from
    dune-blueprints-list.py (pawn-anchored backpack/blueprint-storage/bank + placed storage
    + vehicle cargo). account_id is a validated int -> injection-safe."""
    return f"""
  WITH eps AS (
    SELECT player_pawn_id, player_controller_id
      FROM dune.encrypted_player_state
     WHERE account_id = {account_id}::bigint
  )
  SELECT inv.id
    FROM dune.inventories inv
    JOIN eps ON inv.actor_id = eps.player_pawn_id
  UNION
  SELECT inv.id
    FROM dune.placeables p
    JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
    JOIN dune.permission_actor_rank par
         ON par.permission_actor_id = afe.actor_id AND par.rank = 1
    JOIN eps ON par.player_id = eps.player_controller_id
    JOIN dune.inventories inv ON inv.actor_id = p.id
   WHERE p.is_hologram = false
  UNION
  SELECT inv.id
    FROM dune.permission_actor_rank par
    JOIN dune.actors a ON a.id = par.permission_actor_id
    JOIN eps ON par.player_id = eps.player_controller_id
    JOIN dune.inventories inv ON inv.actor_id = a.id
   WHERE par.rank = 1
"""


NAME_PATH = "{FBuildingBlueprintItemStats,1,BuildingBlueprintName}"
STRUCT_PATH = "{FBuildingBlueprintItemStats,1}"


def read_plan(account_id, bp_id):
    """Read-only dry-run plan: account online status + whether this account owns the
    blueprint + its current name + struct presence. account_id/bp_id are validated ints."""
    sql = (
        "SET search_path TO dune, public;\n"
        "SELECT json_build_object(\n"
        f"  'online_status', (SELECT online_status FROM dune.encrypted_player_state\n"
        f"      WHERE account_id = {account_id} LIMIT 1),\n"
        f"  'in_grace', (SELECT (reconnect_grace_period_end IS NOT NULL\n"
        f"      AND reconnect_grace_period_end > NOW()) FROM dune.encrypted_player_state\n"
        f"      WHERE account_id = {account_id} LIMIT 1),\n"
        "  'item_id', sub.item_id,\n"
        "  'owned', (sub.item_id IS NOT NULL),\n"
        "  'struct_ok', COALESCE(sub.struct_ok, false),\n"
        "  'current_name', sub.current_name\n"
        ") FROM (\n"
        "  SELECT i.id AS item_id,\n"
        f"         (i.stats #> '{STRUCT_PATH}') IS NOT NULL AS struct_ok,\n"
        f"         i.stats #>> '{NAME_PATH}' AS current_name\n"
        "    FROM dune.building_blueprints bp\n"
        "    JOIN dune.items i ON i.id = bp.item_id\n"
        f"   WHERE bp.id = {bp_id}\n"
        "     AND i.template_id = 'BuildingBlueprint_CopyDevice'\n"
        "     AND i.inventory_id IN (\n"
        f"{owned_inv_sql(account_id)}     )\n"
        "   LIMIT 1\n"
        ") sub"
    )
    return _read_json(sql)


def build_rename_sql(account_id, bp_id, name_b64):
    """One-transaction RENAME. Offline-gates the account, resolves + locks the OWNED
    blueprint item (struct must exist), then jsonb_set's the BuildingBlueprintName from the
    base64-decoded text. Any RAISE rolls the whole txn back. account_id/bp_id are validated
    int literals; name_b64 is a validated base64 literal decoded in-SQL."""
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _bpr_result;
CREATE TEMP TABLE _bpr_result(
  bp_id bigint, item_id bigint, old_name text, new_name text
) ON COMMIT PRESERVE ROWS;
DO $rename$
DECLARE
  v_status text; v_grace bool;
  v_item_id bigint; v_struct bool; v_old text; v_new text;
BEGIN
  -- 1. OFFLINE-GATE: the copy device is a RAM-backed inventory item; refuse unless
  --    Offline and past the reconnect grace.
  SELECT online_status,
         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())
    INTO v_status, v_grace
    FROM dune.encrypted_player_state
    WHERE account_id = {account_id} LIMIT 1;
  IF v_status IS NULL THEN RAISE EXCEPTION 'no_player'; END IF;
  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN
    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);
  END IF;
  -- 2. the new name, base64-decoded in-SQL (validated text upstream)
  v_new := convert_from(decode('{name_b64}', 'base64'), 'utf8');
  -- 3. resolve + lock the OWNED blueprint item; struct object must already exist
  SELECT i.id, (i.stats #> '{STRUCT_PATH}') IS NOT NULL,
         i.stats #>> '{NAME_PATH}'
    INTO v_item_id, v_struct, v_old
    FROM dune.building_blueprints bp
    JOIN dune.items i ON i.id = bp.item_id
    WHERE bp.id = {bp_id}
      AND i.template_id = 'BuildingBlueprint_CopyDevice'
      AND i.inventory_id IN (
{owned_inv_sql(account_id)}      )
    LIMIT 1
    FOR UPDATE OF i;
  IF v_item_id IS NULL THEN RAISE EXCEPTION 'not_owned'; END IF;
  IF NOT v_struct THEN RAISE EXCEPTION 'no_struct'; END IF;
  -- 4. set the one field
  UPDATE dune.items
     SET stats = jsonb_set(stats, '{NAME_PATH}', to_jsonb(v_new))
   WHERE id = v_item_id;
  INSERT INTO _bpr_result VALUES ({bp_id}, v_item_id, v_old, v_new);
END $rename$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'rename',
  'bp_id', bp_id, 'item_id', item_id,
  'old_name', old_name, 'new_name', new_name
) FROM _bpr_result;
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


def do_dry_run(account_id, bp_id, name):
    plan = read_plan(account_id, bp_id)
    is_offline = plan.get("online_status") == "Offline" and not plan.get("in_grace")
    errs = []
    if plan.get("online_status") is None:
        errs.append("no_player")
    elif not is_offline:
        errs.append("player_online")
    if not plan.get("owned"):
        errs.append("not_owned")
    elif not plan.get("struct_ok"):
        errs.append("no_struct")
    emit({
        "dry_run": True, "action": "rename",
        "account_id": account_id, "bp_id": bp_id,
        "online_status": plan.get("online_status"),
        "in_grace": plan.get("in_grace"),
        "item_id": plan.get("item_id"),
        "owned": plan.get("owned"),
        "struct_ok": plan.get("struct_ok"),
        "current_name": plan.get("current_name"),
        "new_name": name,
        "preflight_errors": errs,
    }, 0)


def do_live(account_id, bp_id, name_b64):
    ns, pod = resolve_db_pod()
    sql = build_rename_sql(account_id, bp_id, name_b64)
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
    """Return (account_id, bp_id, name, name_b64, dry_run) from --stdin-json (dispatcher
    path) or CLI flags."""
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        account_id = pos_int("account_id", payload.get("account_id"))
        bp_id = pos_int("bp_id", payload.get("bp_id"))
        name = clean_name(payload.get("name"))
        dry_run = bool(payload.get("dry_run", False))
    else:
        ap = argparse.ArgumentParser(description="Portal My Bases blueprint RENAME writer")
        ap.add_argument("--account-id", required=True)
        ap.add_argument("--bp-id", required=True)
        ap.add_argument("--name-b64", required=True,
                        help="base64(utf-8) of the new display name")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        account_id = pos_int("account_id", a.account_id)
        bp_id = pos_int("bp_id", a.bp_id)
        if not B64_RE.match(a.name_b64 or ""):
            fail("bad_name_b64", 2)
        try:
            decoded = base64.b64decode(a.name_b64, validate=True).decode("utf-8")
        except Exception:
            fail("bad_name_b64", 2)
        name = clean_name(decoded)
        dry_run = a.dry_run
    return account_id, bp_id, name, name_to_b64(name), dry_run


def main():
    account_id, bp_id, name, name_b64, dry_run = gather_args()
    if dry_run:
        do_dry_run(account_id, bp_id, name)
    else:
        do_live(account_id, bp_id, name_b64)


if __name__ == "__main__":
    main()
