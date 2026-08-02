#!/usr/bin/env python3
# Read-only per-vehicle INSTALLED-PARTS durability list for the portal storage
# browser. Deployed to lastsietch-dune:/root/dune-vehicle-parts.py -- invoked over SSH by
# the lastsietch-relay dispatcher via `vehicle-parts <account_id> <container_id>`.
#
# INSTALLED parts are NOT items: they live in dune.vehicle_modules(vehicle_id,
# template_id, stats) with durability under FVehicleModuleDurabilityStats
# (Current/Decayed/Max, all OPTIONAL). The item inventories on a vehicle hold only
# cargo/spares. See our internal notes.
#
# The portal's vehicle container_id IS the vehicle cargo inventory id (vehicles are
# inventory-keyed, per our internal notes), whose
# inv.actor_id = dune.vehicles.id = vehicle_modules.vehicle_id. Ownership is pushed
# down to SQL: the account must hold ANY permission rank on that vehicle. Not owned
# / not a vehicle => available=false, error=not_owned (FastAPI maps to 404).
#
# Factory max per part = the max durability ever observed for that template across
# ALL vehicles (a decayed value is always <= factory; a fresh module carries true
# Max) -- DB-derived, no pak data needed.

import json
import subprocess
import sys

_MOD = "stats->'FVehicleModuleDurabilityStats'->1"

OWNS_SQL = """
SET search_path TO dune, public;
SELECT EXISTS (
  SELECT 1
    FROM dune.inventories inv
    JOIN dune.vehicles v ON v.id = inv.actor_id
    JOIN dune.permission_actor_rank par ON par.permission_actor_id = v.id
    JOIN dune.encrypted_player_state eps ON eps.player_controller_id = par.player_id
    WHERE inv.id = {container_id}::bigint
      AND eps.account_id = {account_id}::bigint
) AS owns;
"""

# Per-part durability with the factory max joined in. cur/cap coalesce the sparse
# fields to an effective value; the caller renders health (cur/fmax) and integrity
# (cap/fmax, where < 100% = permanent decay the refurbish reverses).
PARTS_SQL = """
SET search_path TO dune, public;
WITH veh AS (SELECT actor_id AS vid FROM dune.inventories WHERE id = {container_id}::bigint),
fmax AS (
  SELECT template_id, GREATEST(
    COALESCE(MAX(({mod}->>'MaxDurability')::float8),0),
    COALESCE(MAX(({mod}->>'DecayedMaxDurability')::float8),0),
    COALESCE(MAX(({mod}->>'CurrentDurability')::float8),0)
  ) AS fmax
  FROM dune.vehicle_modules GROUP BY template_id
)
SELECT coalesce(json_agg(json_build_object(
  'template_id', vm.template_id,
  'current', d.cur,
  'cap', d.cap,
  'factory_max', f.fmax,
  'health_pct', CASE WHEN f.fmax > 0 THEN round(100.0 * d.cur / f.fmax)::int ELSE NULL END,
  'integrity_pct', CASE WHEN f.fmax > 0 THEN round(100.0 * d.cap / f.fmax)::int ELSE NULL END
) ORDER BY vm.template_id), '[]'::json)
FROM dune.vehicle_modules vm
JOIN veh ON veh.vid = vm.vehicle_id
JOIN fmax f ON f.template_id = vm.template_id
CROSS JOIN LATERAL (
  SELECT COALESCE((vm.{mod2}->>'CurrentDurability')::float8,
                  (vm.{mod2}->>'DecayedMaxDurability')::float8, f.fmax) AS cur,
         COALESCE((vm.{mod2}->>'DecayedMaxDurability')::float8, f.fmax) AS cap
) d
WHERE vm.stats ? 'FVehicleModuleDurabilityStats'
  AND (vm.{mod}) ?| array['CurrentDurability','DecayedMaxDurability','MaxDurability'];
"""


def _run(sql: str) -> str:
    try:
        out = subprocess.run(
            ["/root/dq.sh", "-tAc", sql],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(json.dumps({"available": False, "error": "timeout"}))
    if out.returncode != 0:
        raise SystemExit(json.dumps({"available": False,
                                     "error": (out.stderr or out.stdout).strip()[:500]}))
    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    return raw


def main():
    argv = sys.argv[1:]
    if len(argv) != 2:
        print(json.dumps({"available": False,
                          "error": "usage: dune-vehicle-parts.py <account_id> <container_id>"}))
        sys.exit(2)
    account_id, container_id = argv[0], argv[1]
    if not account_id.isdigit() or not container_id.isdigit():
        print(json.dumps({"available": False, "error": "args must be digits"}))
        sys.exit(2)

    owns_raw = _run(OWNS_SQL.format(account_id=account_id, container_id=container_id))
    if owns_raw.lower() not in ("t", "true", "1"):
        print(json.dumps({"available": False, "error": "not_owned",
                          "account_id": account_id, "container_id": container_id,
                          "parts": []}))
        return

    parts_raw = _run(PARTS_SQL.format(account_id=account_id, container_id=container_id,
                                      mod=_MOD, mod2=_MOD))
    try:
        parts = json.loads(parts_raw or "[]")
    except json.JSONDecodeError as e:
        print(json.dumps({"available": False, "error": f"parse: {e}",
                          "raw": parts_raw[:500]}))
        sys.exit(1)

    print(json.dumps({
        "available": True,
        "account_id": account_id,
        "container_id": container_id,
        "parts": parts,
        "count": len(parts),
    }))


if __name__ == "__main__":
    main()
