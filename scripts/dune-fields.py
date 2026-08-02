#!/usr/bin/env python3
# Read-only resource + spice field aggregates for the Last Sietch relay. No writes.
# Deployed to lastsietch-dune:/root/dune-fields.py — invoked by the relay over SSH.
import json
import subprocess

# resourcefield_state / spicefield_types use friendly map names (HaggaBasin,
# DeepDesert); world_partition uses internal names. Resolve partition_id per
# (friendly map, dimension_index) via this internal-name lookup.
INTERNAL_MAP = {
    ("HaggaBasin", 0): "Survival_1",
    ("DeepDesert", 0): "DeepDesert_1",
    ("DeepDesert", 1): "DeepDesert_1",
}

FIELDS_SQL = """
SELECT json_build_object(
  'resource', (SELECT coalesce(json_agg(row_to_json(r)),'[]'::json) FROM (
     SELECT map, dimension_index, field_kind_id,
            count(*) AS fields, sum(value_remaining) AS total_value
     FROM dune.resourcefield_state GROUP BY 1,2,3 ORDER BY 1,2,3) r),
  'spice', (SELECT coalesce(json_agg(row_to_json(s)),'[]'::json) FROM (
     SELECT spicefield_type_id, field_type, map_name, dimension_index,
            max_globally_active, current_globally_active, is_spawning_active
     FROM dune.spicefield_types
     ORDER BY map_name, dimension_index, spicefield_type_id) s),
  'partitions', (SELECT coalesce(json_agg(row_to_json(w)),'[]'::json) FROM (
     SELECT partition_id, map, dimension_index
     FROM dune.world_partition) w)
);
"""


def main():
    out = subprocess.run(["/root/dq.sh", "-tAc", FIELDS_SQL],
                         capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        print(json.dumps({"available": False, "error": "db query failed",
                          "detail": (out.stderr or out.stdout).strip()[:300]}))
        return
    try:
        data = json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        print(json.dumps({"available": False, "error": "db returned non-JSON",
                          "detail": out.stdout.strip()[:300]}))
        return

    # partition_id lookup: internal map name -> partition_id (lowest dimension)
    wp = {(r["map"], r["dimension_index"]): r["partition_id"]
          for r in data.get("partitions", [])}

    parts = {}  # (friendly_map, dimension_index) -> partition dict

    def get_part(map_name, dim):
        key = (map_name, dim)
        if key not in parts:
            internal = INTERNAL_MAP.get(key)
            pid = wp.get((internal, dim)) if internal else None
            parts[key] = {
                "map": map_name,
                "partition_id": pid,
                "dimension_index": dim,
                "resource_fields": [],
                "spice_fields": [],
            }
        return parts[key]

    for r in data.get("resource", []):
        p = get_part(r["map"], r["dimension_index"])
        p["resource_fields"].append({
            "kind_id": r["field_kind_id"],
            "fields": r["fields"],
            "value_remaining": r["total_value"],
        })

    for s in data.get("spice", []):
        p = get_part(s["map_name"], s["dimension_index"])
        p["spice_fields"].append({
            "type_id": s["spicefield_type_id"],
            "size": s["field_type"],
            "max_active": s["max_globally_active"],
            "current_active": s["current_globally_active"],
            "spawning": s["is_spawning_active"],
        })

    ordered = sorted(parts.values(),
                     key=lambda p: (p["map"], p["dimension_index"]))
    print(json.dumps({"available": True, "partitions": ordered}))


if __name__ == "__main__":
    main()
