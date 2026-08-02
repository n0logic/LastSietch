#!/usr/bin/env python3
# Read-only equipped-gear list for the Last Sietch portal character stage.
# Deployed to <game-host>:/root/dune-player-equipped.py — invoked over SSH by the
# lastsietch-relay dispatcher via the `player-equipped <account_id>` token.
#
# Reads the player's EQUIPPED inventory: inventory_type=1 on the account's pawn
# (verified empirically 2026-05-23; see dune-grant.sh inventory_type reference).
# Each row carries the visual identity needed by the 3D gear viewer:
#   position_index  -> slot ordering (0=head,1=chest,2=legs,3=hands,4=feet,5=back,
#                      6+ = tools/utility; portal maps to a label via category)
#   template_id     -> item identity (portal resolves name + category + mesh)
#   FCustomizationStats.VariantId -> the equipped MESH VARIANT (e.g.
#                      "MTX_WaterS_HeavyArmor_Top_MeshVariant")
#   FCustomizationStats.SwatchId  -> the equipped DYE/colour (feeds the LUT tint)
#   FItemStackAndDurabilityStats  -> current/max durability (best-effort)
#
# NOTE: inventory_type=1 is RAM-fragile (like backpack=0 / hotbar=15) — it
# reflects the player's last-OFFLINE loadout; while online the rows may be stale.
# That is fine for a portal character card (same offline-gated semantics as the
# other player cards). This script only SELECTs — no writes, no gating needed.

import json
import subprocess
import sys

EQUIPPED_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'position',   i.position_index,
  'template_id', i.template_id,
  'quality',    i.quality_level,
  'variant_id', COALESCE(i.stats->'FCustomizationStats'->1->>'VariantId', ''),
  'swatch_id',  COALESCE(i.stats->'FCustomizationStats'->1->>'SwatchId', ''),
  'cur_dur',    COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability', ''),
  'max_dur',    COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability', '')
) ORDER BY i.position_index), '[]'::json)
FROM dune.encrypted_player_state eps
JOIN dune.inventories inv ON inv.actor_id = eps.player_pawn_id AND inv.inventory_type = 1
JOIN dune.items i ON i.inventory_id = inv.id
WHERE eps.account_id = {account_id}::bigint;
"""


def _run(sql: str) -> str:
    try:
        out = subprocess.run(
            ["/root/dq.sh", "-tAc", sql],
            capture_output=True, text=True, timeout=30, check=False,
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
    if len(argv) != 1 or not argv[0].isdigit():
        print(json.dumps({"available": False,
                          "error": "usage: dune-player-equipped.py <account_id>"}))
        sys.exit(2)
    account_id = argv[0]

    items_raw = _run(EQUIPPED_SQL.format(account_id=int(account_id)))
    try:
        items = json.loads(items_raw or "[]")
    except json.JSONDecodeError as e:
        print(json.dumps({"available": False, "error": f"parse: {e}",
                          "raw": items_raw[:500]}))
        sys.exit(1)

    print(json.dumps({
        "available": True,
        "account_id": account_id,
        "items": items,
        "count": len(items),
    }))


if __name__ == "__main__":
    main()
