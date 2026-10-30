#!/usr/bin/env python3
# Read-only worn/carried gear list for the Last Sietch portal character stage.
# Deployed to <game-host>:/root/dune-player-equipped.py, invoked over SSH by the
# lastsietch-relay dispatcher via the `player-equipped <account_id>` token.
#
# Reads TWO inventories on the account's pawn (verified empirically 2026-05-23;
# see dune-grant.sh inventory_type reference):
#   inventory_type=1  -> EQUIPPED (worn armour, belt, shield, tools)
#   inventory_type=15 -> HOTBAR (weapons live here, and so do their augments;
#                        an augmented weapon is invisible to a type-1-only read)
# Each row carries `source` so the portal can group them.
#
# Each row carries the visual identity needed by the gear card / 3D viewer:
#   position_index  -> slot ordering (0=head,1=chest,2=legs,3=hands,4=feet,5=back,
#                      6+ = tools/utility; portal maps to a label via category)
#   template_id     -> item identity (portal resolves name + category + mesh)
#   FCustomizationStats.VariantId -> the equipped MESH VARIANT (e.g.
#                      "MTX_WaterS_HeavyArmor_Top_MeshVariant")
#   FCustomizationStats.SwatchId  -> the equipped DYE/colour (feeds the LUT tint)
#   FItemStackAndDurabilityStats  -> current/max durability (best-effort)
#   FAugmentedItemStats           -> installed augments, as three PARALLEL arrays
#                      (AppliedAugments / AppliedAugmentQualities /
#                      AppliedAugmentRollData) flattened here into one list of
#                      {name, grade, rolls}. Roll count VARIES per augment
#                      (Spitdart=7, Rateoffire=1, Melee6=2), so never assume 1.
#
# `item_id` is emitted because the augment reroll/swap flow has to name an exact
# dune.items row; the portal only ever serves rows for the caller's own session
# account, and the writer re-resolves ownership server-side regardless.
#
# 🔴 character_state <> 'Deleted' is REQUIRED, not cosmetic. encrypted_player_state
# holds one row per character SLOT, and a bare account_id join pulls in the pawn of
# a DELETED character too. Measured on live 2026-08-01: 59 accounts carry a Deleted
# tombstone next to their Active character, and that leaked 133 phantom equipped
# items across 14 accounts onto the Character page (plus 131 hotbar items across 18
# once this read widened).
#
# NOTE: both inventory types are RAM-fragile (like backpack=0), they reflect the
# player's last-OFFLINE loadout; while online the rows may be stale. That is fine
# for a portal character card (same offline-gated semantics as the other player
# cards). This script only SELECTs, no writes, no gating needed.

import json
import os
import subprocess
import sys

# Augment display names + per-grade effect text. Same catalogue dune-augment.py
# uses, deliberately resolved HERE rather than on <web-host> so there is exactly
# ONE copy of it (it has to live on this host for the writer anyway). Absent
# catalogue = degrade to raw template ids, never fail the gear read.
_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "augment-compatibility.json")


def _load_augment_catalog() -> dict:
    try:
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("augments") or {}
    except (OSError, json.JSONDecodeError):
        return {}

GEAR_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(json_build_object(
  'item_id',    i.id,
  'source',     CASE inv.inventory_type WHEN 1 THEN 'equipped' ELSE 'hotbar' END,
  'inv_type',   inv.inventory_type,
  'position',   i.position_index,
  'template_id', i.template_id,
  'quality',    i.quality_level,
  'variant_id', COALESCE(i.stats->'FCustomizationStats'->1->>'VariantId', ''),
  'swatch_id',  COALESCE(i.stats->'FCustomizationStats'->1->>'SwatchId', ''),
  'cur_dur',    COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability', ''),
  'max_dur',    COALESCE(i.stats->'FItemStackAndDurabilityStats'->1->>'DecayedMaxDurability', ''),
  'augments',   CASE
    WHEN jsonb_typeof(i.stats #> '{FAugmentedItemStats,1,AppliedAugments}') = 'array'
    THEN COALESCE((
      SELECT json_agg(json_build_object(
               'name',  a.aug->>'Name',
               'grade', (i.stats #> '{FAugmentedItemStats,1,AppliedAugmentQualities}') -> (a.ord - 1)::int,
               'rolls', COALESCE(
                          (i.stats #> '{FAugmentedItemStats,1,AppliedAugmentRollData}')
                            -> (a.ord - 1)::int -> 'StatRolls',
                          '[]'::jsonb)
             ) ORDER BY a.ord)
        FROM jsonb_array_elements(i.stats #> '{FAugmentedItemStats,1,AppliedAugments}')
             WITH ORDINALITY AS a(aug, ord)
    ), '[]'::json)
    ELSE '[]'::json
  END
) ORDER BY inv.inventory_type, i.position_index), '[]'::json)
FROM (
  -- Exactly ONE character. Filtering out Deleted is necessary but not sufficient:
  -- an account may hold several live characters, and joining them all merges their
  -- loadouts into one list (two Head slots, duplicate "Slot 1", inflated counts).
  -- Same default pick as the sibling reader dune-player-progress.py, so the gear
  -- card and the progress card always describe the SAME character.
  -- IS DISTINCT FROM, not <>, to stay NULL-safe and match the 16 sibling sites.
  SELECT eps.player_pawn_id
    FROM dune.encrypted_player_state eps
   WHERE eps.account_id = __ACCOUNT_ID__::bigint
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1
) me
JOIN dune.inventories inv
  ON inv.actor_id = me.player_pawn_id
 AND inv.inventory_type IN (1, 15)
JOIN dune.items i ON i.inventory_id = inv.id;
"""
# NB: substituted with .replace(), NOT .format(), the jsonb paths above are
# Postgres array literals ({FAugmentedItemStats,1,...}) and str.format reads those
# braces as placeholders and raises KeyError. The id is int()-cast, so injection-safe.


# Standalone augment consumables the player owns, for the reroll/swap dialog's
# "swap in" picker.
#
# 🔴 THE OWNERSHIP SCOPE HERE MUST MATCH dune-augment.py's CONSUME LOOP EXACTLY.
# That loop is PAWN-SIDE ONLY (`cinv.actor_id = <pawn>`), which covers backpack,
# worn, hotbar and the CHOAM bank, so this read is too. If this were WIDER the
# dialog would offer an augment the write then refuses with augment_not_owned,
# which reads to the player as the portal losing their item.
#
# Containers and vehicles are EXCLUDED on purpose: their contents live in a live
# partition's RAM, which resurrects our DELETE and duplicates the augment. Most
# augments do sit in boxes today, so this picker will look sparse; that is the
# point. Players move one into their backpack in game first.
#
# Grouped by (template_id, grade): the picker needs "you have 2 of these at G5",
# and augments of the same id at different grades are genuinely different choices.
OWNED_AUGMENTS_SQL = """
SET search_path TO dune, public;
WITH me AS (
  SELECT eps.player_pawn_id, eps.player_controller_id
    FROM dune.encrypted_player_state eps
   WHERE eps.account_id = __ACCOUNT_ID__::bigint
     AND eps.character_state IS DISTINCT FROM 'Deleted'
   ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
   LIMIT 1
), mine AS (
  SELECT i.template_id, i.quality_level
    FROM dune.items i
    JOIN dune.inventories inv ON inv.id = i.inventory_id
   WHERE i.stats ? 'FAugmentItemStats'
     AND inv.actor_id = (SELECT player_pawn_id FROM me)
)
SELECT coalesce(json_agg(json_build_object(
  'name', template_id, 'grade', quality_level, 'count', n
) ORDER BY template_id, quality_level), '[]'::json)
FROM (SELECT template_id, quality_level, count(*) AS n
        FROM mine GROUP BY template_id, quality_level) g;
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

    items_raw = _run(GEAR_SQL.replace("__ACCOUNT_ID__", str(int(account_id))))
    try:
        items = json.loads(items_raw or "[]")
    except json.JSONDecodeError as e:
        print(json.dumps({"available": False, "error": f"parse: {e}",
                          "raw": items_raw[:500]}))
        sys.exit(1)

    catalog = _load_augment_catalog()
    for it in items:
        for aug in (it.get("augments") or []):
            entry = catalog.get(aug.get("name")) or {}
            aug["label"] = entry.get("name") or aug.get("name")
            grade = aug.get("grade")
            effects = (entry.get("gradeEffects") or {}).get(str(grade)) or []
            aug["effects"] = [str(e) for e in effects]

    # The swap picker's inventory. Best-effort: a failure here must NOT take the
    # gear card down with it, so it degrades to an empty list (the dialog then
    # offers reroll only, which needs nothing owned).
    try:
        owned = json.loads(_run(
            OWNED_AUGMENTS_SQL.replace("__ACCOUNT_ID__", str(int(account_id)))) or "[]")
    except (json.JSONDecodeError, SystemExit, ValueError):
        owned = []
    for aug in owned:
        entry = catalog.get(aug.get("name")) or {}
        aug["label"] = entry.get("name") or aug.get("name")
        effects = (entry.get("gradeEffects") or {}).get(str(aug.get("grade"))) or []
        aug["effects"] = [str(e) for e in effects]

    equipped = [i for i in items if i.get("source") == "equipped"]
    hotbar = [i for i in items if i.get("source") == "hotbar"]
    print(json.dumps({
        "available": True,
        "account_id": account_id,
        "items": items,
        "count": len(items),
        "equipped_count": len(equipped),
        "hotbar_count": len(hotbar),
        "augmented_count": sum(1 for i in items if i.get("augments")),
        "owned_augments": owned,
    }))


if __name__ == "__main__":
    main()
