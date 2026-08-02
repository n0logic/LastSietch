#!/usr/bin/env python3
# W6 Spice-spawn toggle: lastsietch-dune-resident psql wrapper for the V2 admin
# "Spice" sub-card. Deployed to lastsietch-dune:/root/dune-spice-toggle.py; invoked
# ONLY by the forced-command dispatcher /root/dune-relay-dispatch.sh via the
# `spice-types` (read) and `spice-toggle <b64>` (write) tokens.
#
#
# "W6: Spice-spawn toggle". v1 = boolean only (Decision A): flips
# dune.spicefield_types.is_spawning_active per field type and records the flip
# in lsadmin.spicefield_toggle_log.
#
# Modes:
#   --list             read-only SELECT of the 8 spicefield_types rows as JSON
#   --apply-b64 <b64>  decode {type_id,new_value,who,change_id}, UPDATE the
#                      boolean, INSERT the log row, single BEGIN..COMMIT
#
# HARD CONSTRAINTS:
#   * NEVER restart/reboot any game pod, the BGD, or k3s. This script only ever
#     opens a psql session into the ALREADY-RUNNING DB pod (mirrors dune-grant.sh).
#   * Toggling off does NOT despawn active fields; it only suppresses the next
#     spawn-tick. Online-safe per Decision A.
#   * Every field decoded from the base64 JSON is RE-VALIDATED here (defence in
#     depth; the dispatcher's base64 regex is layer 1, this is layer 2).

import base64
import json
import subprocess
import sys

DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

LIST_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(row_to_json(t) ORDER BY t.id), '[]'::json)
  FROM (
    SELECT spicefield_type_id AS id, field_type, map_name, dimension_index,
           is_spawning_active, current_globally_active
      FROM dune.spicefield_types
  ) t;
"""

# Write txn: bind every value as a psql -v variable so nothing is string-
# concatenated into the SQL. The UPDATE flips the boolean; the INSERT records
# the flip. Single BEGIN..COMMIT under ON_ERROR_STOP=1 (set by run_psql).
APPLY_SQL = """
BEGIN;
SET LOCAL search_path TO dune, public;
UPDATE dune.spicefield_types
   SET is_spawning_active = :new_value
 WHERE spicefield_type_id = :type_id;
INSERT INTO lsadmin.spicefield_toggle_log (who, type_id, new_value, change_id)
VALUES (:'who', :type_id, :new_value, :'change_id');
COMMIT;
SELECT json_build_object(
  'ok', true,
  'type_id', :type_id,
  'new_value', :new_value,
  'change_id', :'change_id'
);
"""


def fail(msg, code=1):
    print(json.dumps({"available": False, "ok": False, "error": str(msg)[:500]}))
    sys.exit(code)


def resolve_db_pod():
    # Namespace + DB pod are NOT hardcoded; resolved by label so a Funcom
    # redeploy that changes the pod hash never writes to a stale pod. Mirrors
    # dune-grant.sh resolve_db_pod().
    try:
        ns_out = subprocess.run(
            ["sudo", "kubectl", "get", "ns", "-o", "name"],
            capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        fail("timeout resolving namespace", 3)
    ns = ""
    for line in (ns_out.stdout or "").splitlines():
        name = line.strip().removeprefix("namespace/")
        if name.startswith("funcom-seabass-"):
            ns = name
            break
    if not ns:
        fail("could not resolve the Dune namespace (no funcom-seabass-* ns)", 3)

    try:
        pod_out = subprocess.run(
            ["sudo", "kubectl", "get", "pods", "-n", ns, "-o", "name"],
            capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        fail("timeout resolving DB pod", 3)
    pod = ""
    for line in (pod_out.stdout or "").splitlines():
        name = line.strip().removeprefix("pod/")
        if name.endswith("-db-dbdepl-sts-0"):
            pod = name
            break
    if not pod:
        fail(f"could not resolve the Dune DB pod in namespace {ns}", 3)
    return ns, pod


def run_psql(ns, pod, sql, extra_args, timeout=45):
    # The ONLY place a DB connection is opened. Reads POSTGRES_PASSWORD from the
    # pod env, then execs psql inside the already-running pod. Mirrors
    # dune-grant.sh run_psql().
    try:
        pw_out = subprocess.run(
            ["sudo", "kubectl", "exec", "-n", ns, pod, "--",
             "printenv", "POSTGRES_PASSWORD"],
            capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        fail("timeout reading POSTGRES_PASSWORD from DB pod", 3)
    pgpass = (pw_out.stdout or "").strip()
    if not pgpass:
        fail(f"could not read POSTGRES_PASSWORD from DB pod {pod}", 3)

    cmd = ["sudo", "kubectl", "exec", "-i", "-n", ns, pod, "--",
           "env", f"PGPASSWORD={pgpass}", "psql",
           "-h", "localhost", "-p", DB_PORT, "-U", DB_USER, "-d", DB_NAME,
           "-v", "ON_ERROR_STOP=1", *extra_args]
    try:
        return subprocess.run(cmd, input=sql, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        fail("psql timeout", 1)


def last_json_line(stdout):
    raw = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line and line not in ("SET", "BEGIN", "COMMIT", "UPDATE 1", "INSERT 0 1"):
            raw = line
    return raw


def do_list():
    ns, pod = resolve_db_pod()
    out = run_psql(ns, pod, LIST_SQL, ["-tA"], timeout=30)
    if out.returncode != 0:
        fail((out.stderr or out.stdout).strip())
    raw = last_json_line(out.stdout) or "[]"
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"parse: {e}; raw={raw[:300]}")
    print(json.dumps({"available": True, "count": len(rows), "types": rows}))


def do_apply(b64):
    if not isinstance(b64, str) or not b64.strip():
        fail("usage: dune-spice-toggle.py --apply-b64 <base64>", 2)
    try:
        payload = json.loads(base64.b64decode(b64, validate=True).decode("utf-8"))
    except Exception as e:
        fail(f"invalid base64 payload: {e}", 2)
    if not isinstance(payload, dict):
        fail("payload must be a JSON object", 2)

    type_id = payload.get("type_id")
    new_value = payload.get("new_value")
    who = payload.get("who")
    change_id = payload.get("change_id")

    # Defence-in-depth re-validation of every field.
    if isinstance(type_id, bool) or not isinstance(type_id, int) or type_id < 1:
        fail("type_id must be a positive integer", 2)
    if not isinstance(new_value, bool):
        fail("new_value must be a boolean", 2)
    if not isinstance(who, str) or not who.strip():
        fail("who must be a non-empty string", 2)
    if not isinstance(change_id, str) or not change_id.strip():
        fail("change_id must be a non-empty string", 2)

    ns, pod = resolve_db_pod()
    vargs = [
        "-tA",
        "-v", f"type_id={type_id}",
        "-v", f"new_value={'true' if new_value else 'false'}",
        "-v", f"who={who}",
        "-v", f"change_id={change_id}",
    ]
    out = run_psql(ns, pod, APPLY_SQL, vargs, timeout=45)
    if out.returncode != 0:
        fail((out.stderr or out.stdout).strip())
    raw = last_json_line(out.stdout)
    if not raw:
        fail("apply produced no result row")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"parse: {e}; raw={raw[:300]}")
    result.setdefault("available", True)
    print(json.dumps(result))


def main():
    if len(sys.argv) < 2:
        fail("usage: dune-spice-toggle.py --list | --apply-b64 <base64>", 2)
    mode = sys.argv[1]
    if mode == "--list":
        do_list()
    elif mode == "--apply-b64":
        do_apply(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        fail(f"unknown mode: {mode}", 2)


if __name__ == "__main__":
    main()
