"""VC4 cart catalog, derived from the authoritative grant catalog.

Every cart-eligible grant_type's form schema is DERIVED at request time from
``data/dune-grant-catalog.json`` (grant_types[] for label/category/offline/
high_value; entries[] for the choice lists) and the numeric caps imported from
``routers.dune_grant`` (single source of truth). Nothing here hardcodes a range
or an enum that already lives in those two places.

Field objects match the Workbench renderer's contract:
  {name, type, [min], [max], [default], [required], [choices], [help],
   [widget], [allow_custom]}
  - type   : "int" | "choice" | "bool" | "text"
  - widget : "select" | "radio" (choice only). Large choice lists (item_live
             template_id, skill blocks) render as a <select>; short enums as
             radios. Emitted explicitly so the renderer stays dumb.
  - choices: list of {value, label} (or bare strings for trivial enums).

``ram_fragile`` mirrors the authoritative ``requires_offline`` flag (offline
grants queue until logout + grace: ~30s in safe zones, ~5 min from Deep Desert
or PvP partitions).

Public API kept back-compatible so the existing /_catalog route and
workbench_catalog.html keep working:
  - grant_catalog()       -> category-grouped list (legacy shape + description)
  - find_entry(grant_type)-> single flat entry dict, or None
New for the cart:
  - cart_catalog()        -> flat list of entries for the /_index endpoint
"""

from routers.dune_grant import (
    _load_catalog,
    MAX_CHAR_XP,
    MAX_FACTION_REP,
    MAX_HOUSE_SCRIP,
    MAX_INTEL,
    MAX_KEYSTONE_ID,
    MAX_QUANTITY,
    MAX_SOLARI,
    MAX_SOLARI_CURRENCY,
    MAX_SPEC_LEVEL,
    MAX_SPEC_XP,
)

# Choice lists longer than this render as a <select> instead of radios.
SELECT_WIDGET_THRESHOLD = 12

# Category that groups the specialization + skill-tree grants and the two
# picker entry tiles. Must match the category string in dune-grant-catalog.json.
PROGRESSION_OFFLINE_CATEGORY = "Progression"

# Picker entry tiles. These are NOT fireable grant_types (no builder in
# dune-grant.sh, absent from the catalog JSON, so the submit route rejects a
# direct fire). They render as tiles that carry an ``opens`` marker; the
# frontend (workbench.js) intercepts the click and loads the matching modal
# fragment instead of the generic config form. The modal then pushes the REAL
# grant lines (spec_xp / keystone / spec_unlock_* / grant_full_job_tree /
# grant_skill_block / reset_*).
PICKER_TILES = {
    "spec_picker": {"label": "Specializations", "opens": "spec_picker"},
    "skill_picker": {"label": "Skills / Abilities", "opens": "skill_picker"},
}

# Real grant_types that a picker modal owns. They stay cart-line-eligible (the
# modal pushes them and the cart renders their field schema), but they must NOT
# render as standalone generic browse tiles that open the old config form. Each
# carries a ``picker`` marker in the /_index JSON so the frontend can hide it
# from the browse grid and route it through the matching modal instead. Absent
# on every other entry.
PICKER_OWNED = {
    "spec_xp": "spec_picker",
    "keystone": "spec_picker",
    "spec_unlock_track": "spec_picker",
    "spec_unlock_all": "spec_picker",
    "reset_specs": "spec_picker",
    "grant_full_job_tree": "skill_picker",
    "grant_skill_block": "skill_picker",
    "reset_full_skill_area": "skill_picker",
}

# Cart-eligible grant types. Display order is the order presented in the browse
# accordion within each category. Deferred to v1.5: item, recipe, schematic_item,
# blueprints, base-backups, teleport. set_starter_class stays hidden.
# bank_items_batch (CHOAM bank delivery, G29) uses the items_batch field builder.
# spec_xp/keystone/spec_unlock_track/spec_unlock_all + the skill-tree grants are
# grouped under the "Progression (Offline-Only)" category (see the catalog JSON);
# the Specializations and Skills pickers (Phase 1/2) open from that category.
CART_GRANT_TYPES = [
    "solari_currency",
    "solari",
    "house_scrip",
    "intel",
    "char_xp",
    "spec_picker",
    "skill_picker",
    "spec_xp",
    "keystone",
    "spec_unlock_track",
    "spec_unlock_all",
    "faction_rep",
    "progression_preset",
    "main_quest_unlock",
    "grant_full_job_tree",
    "reset_full_skill_area",
    "grant_skill_block",
    "reset_specs",
    "reset_tutorials",
    "wipe_codex",
    "repair_all",
    "align_faction",
    "journey_full_unlock",
    "journey_node_completion",
    "item_live",
    "bank_items_batch",
]


def _entries(catalog: dict, key: str) -> list:
    return catalog.get("entries", {}).get(key, []) or []


def _confirmed(rows: list, key: str) -> list:
    """Keep only CONFIRMED rows (drops C++ enum sentinels like the 'Invalid'
    and 'Count' specialization tracks)."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("confidence", "CONFIRMED") == "CONFIRMED":
            out.append(r)
    return out


def _choices_from(rows: list, value_key: str) -> list:
    """Map entries[] rows to {value, label} choice objects."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or value_key not in r:
            continue
        out.append({"value": r[value_key], "label": r.get("label", str(r[value_key]))})
    return out


def _widget_for(choices: list) -> str:
    return "select" if len(choices) > SELECT_WIDGET_THRESHOLD else "radio"


def _choice_field(name, choices, *, required=True, default=None, help=None, allow_custom=False):
    f = {
        "name": name,
        "type": "choice",
        "choices": choices,
        "required": required,
        "widget": _widget_for(choices),
    }
    if default is not None:
        f["default"] = default
    if help:
        f["help"] = help
    if allow_custom:
        f["allow_custom"] = True
    return f


def _int_field(name, lo, hi, default, *, required=True, help=None):
    f = {"name": name, "type": "int", "min": lo, "max": hi, "default": default, "required": required}
    if help:
        f["help"] = help
    return f


_MODE_CHOICES = ["add", "set"]


def _fields_for(grant_type: str, catalog: dict) -> list:
    """Build the field schema for one cart-eligible grant_type. Caps come from
    the imported MAX_* constants; choice lists come from entries[]. Mirrors the
    per-type shape that routers.dune_grant._validate_detail accepts."""
    if grant_type == "solari":
        return [_int_field("amount", 1, MAX_SOLARI, 1000)]

    if grant_type == "solari_currency":
        return [_int_field("amount", 1, MAX_SOLARI_CURRENCY, 1000)]

    if grant_type == "intel":
        return [_int_field("amount", 1, MAX_INTEL, 100)]

    if grant_type == "house_scrip":
        return [
            _int_field("amount", 1, MAX_HOUSE_SCRIP, 100),
            _choice_field("mode", list(_MODE_CHOICES), default="add"),
        ]

    if grant_type == "char_xp":
        return [
            _int_field("target_level", 0, 200, 0, required=False,
                       help="Jump the character to this level (1-200). Recommended. "
                            "Leave 0 to grant raw XP instead. Cannot lower a level."),
            _int_field("amount", 0, MAX_CHAR_XP, 0, required=False,
                       help="Advanced: raw cumulative XP to add. Leave 0 when using "
                            "Target Level. 344440 reaches level 200."),
            {
                "name": "force_wipe_points_ack", "type": "bool", "default": False,
                "required": False,
                "help": "Acknowledge already-spent skill points may be redistributed.",
            },
        ]

    if grant_type == "spec_xp":
        tracks = _choices_from(_confirmed(_entries(catalog, "specialization_track"), "track_type"), "track_type")
        return [
            _choice_field("track_type", tracks),
            _int_field("xp", 0, MAX_SPEC_XP, 2000),
            _int_field("level", 0, MAX_SPEC_LEVEL, 0, required=False, help="Level checkpoint; usually 0."),
            _choice_field("mode", list(_MODE_CHOICES), default="set"),
        ]

    if grant_type == "keystone":
        # Single specialization trait (keystone) unlock. keystone_id resolves to
        # track/name/sp_bonus in dune.ls_keystone_catalog; the trait picker
        # drives the id selection. RAM-fragile (FLevelComponent SP credit).
        return [_int_field("keystone_id", 1, MAX_KEYSTONE_ID, 1,
                           help="Keystone id 1.." + str(MAX_KEYSTONE_ID) +
                                " (per dune.ls_keystone_catalog).")]

    if grant_type == "spec_unlock_track":
        # Bulk unlock: all 41 keystones of one track + summed sp_bonus credit.
        tracks = _choices_from(_confirmed(_entries(catalog, "specialization_track"), "track_type"), "track_type")
        return [_choice_field("track_type", tracks,
                              help="Unlock all 41 keystones of this track.")]

    if grant_type == "spec_unlock_all":
        # Bulk unlock: all 205 keystones across the 5 tracks. No inputs.
        return []

    if grant_type == "faction_rep":
        factions = _choices_from(_entries(catalog, "faction"), "faction_id")
        return [
            _choice_field("faction_id", factions),
            _int_field("amount", -MAX_FACTION_REP, MAX_FACTION_REP, 500,
                       help="Negative values penalize reputation in 'add' mode."),
            _choice_field("mode", list(_MODE_CHOICES), default="add"),
        ]

    if grant_type == "progression_preset":
        # faction/preset enums are not entries-backed; they mirror the explicit
        # allowlist enforced by _validate_detail.
        return [
            _choice_field("faction", ["atreides", "harkonnen"]),
            _choice_field("preset", ["landsraad_unlock_only", "ch3_start", "rank19_eligible"],
                          default="rank19_eligible"),
        ]

    if grant_type == "main_quest_unlock":
        presets = _choices_from(_confirmed(_entries(catalog, "main_quest_preset"), "preset"), "preset")
        return [_choice_field("preset", presets)]

    if grant_type in ("grant_full_job_tree", "reset_full_skill_area"):
        jobs = _choices_from(_entries(catalog, "job"), "job")
        return [_choice_field("job", jobs)]

    if grant_type == "grant_skill_block":
        blocks = _choices_from(_entries(catalog, "skill_block"), "block")
        return [_choice_field("block", blocks)]

    if grant_type == "reset_specs":
        tracks = _choices_from(_confirmed(_entries(catalog, "specialization_track"), "track_type"), "track_type")
        choices = [{"value": "all", "label": "All tracks"}] + tracks
        return [_choice_field("track_type", choices, required=False, default="all",
                              help="'All tracks' performs a full specialization reset.")]

    if grant_type in ("reset_tutorials", "wipe_codex", "repair_all"):
        return []

    if grant_type == "align_faction":
        return [_choice_field("faction", ["atreides", "harkonnen"])]

    if grant_type == "journey_full_unlock":
        # WP-C — two opt-in booleans; CORE is always applied by the bash builder.
        # The faction-story bucket is faction-gated server-side (bash resolves the
        # live target faction and rejects Harkonnen). The help note carries that
        # constraint into the UI.
        return [
            {
                "name": "include_faction_story", "type": "bool",
                "default": False, "required": False,
                "help": "Add the Atreides story-path tags (~66). Atreides targets only — "
                        "Harkonnen story set not yet captured; non-Atreides targets are "
                        "rejected at apply.",
            },
            {
                "name": "include_exploration_poi", "type": "bool",
                "default": False, "required": False,
                "help": "Add map-discovery / POI tags (~25). Optional, default off.",
            },
        ]

    if grant_type == "journey_node_completion":
        # WP-C2 — opt-in arc buckets + the Atreides story flag. Leave all arc
        # buckets unchecked to complete the entire journey (the bash builder's
        # default). Faction story is gated server-side like journey_full_unlock.
        return [
            {
                "name": "include_main_quest", "type": "bool",
                "default": False, "required": False,
                "help": "Complete the main-quest arcs (DA_MQ_*). Leave all "
                        "unchecked to complete the entire journey.",
            },
            {
                "name": "include_side_quests", "type": "bool",
                "default": False, "required": False,
                "help": "Complete the side-quest arcs (DA_SQ_*).",
            },
            {
                "name": "include_dunipedia", "type": "bool",
                "default": False, "required": False,
                "help": "Complete the Dunipedia codex arcs (DA_Dunipedia_*).",
            },
            {
                "name": "include_dlc_lostharvest", "type": "bool",
                "default": False, "required": False,
                "help": "Complete the Lost Harvest DLC arc (DA_DLC_LostHarvest).",
            },
            {
                "name": "include_faction_story", "type": "bool",
                "default": False, "required": False,
                "help": "Also complete the Atreides story-path nodes (65). Atreides "
                        "targets only \u2014 non-Atreides targets are rejected at apply.",
            },
        ]

    if grant_type == "item_live":
        items = _choices_from(_entries(catalog, "item"), "template_id")
        return [
            _choice_field("template_id", items, help="Item delivered to the Landsraad reward queue."),
            _int_field("amount", 1, MAX_QUANTITY, 1),
            # house_name choices are runtime state (active houses) injected by
            # the /_index endpoint; allow_custom keeps manual DA_House... entry
            # open, which _validate_detail accepts via its regex.
            _choice_field("house_name", [], allow_custom=True,
                          help="Pick an active house, or choose Other to type a DA_House... name."),
        ]

    if grant_type == "bank_items_batch":
        # No item list is sent. The Workbench row builder fetches the give-item
        # catalog itself from /api/dune/v2/catalog/give-items: those are the
        # NATIVE template ids plus is_gradeable and pak_max_stack, which the row
        # needs and entries[].item (a 2026-05-21 community cross-reference of an
        # older build) does not carry. The key stays declared so the renderer
        # contract is unchanged; the client ignores the empty list.
        return [{
            "name": "items",
            "type": "items_batch",
            "required": True,
            "max_rows": 30,
            "item_choices": [],
            "stack_min": 1, "stack_max": MAX_QUANTITY, "stack_default": 1,
            "quality_min": 0, "quality_max": 5, "quality_default": 0,
            "help": "Up to 30 items delivered straight to the recipient's CHOAM bank. "
                    "Online-safe; recipient must have logged in at least once.",
        }]

    # Not on the v1 allowlist (caller should not request these); empty schema.
    return []


def _build_entry(grant_type: str, catalog: dict, meta_by_id: dict) -> dict | None:
    # Picker entry tiles are catalog DATA defined here, not in the JSON. They
    # carry an ``opens`` marker the frontend keys on to launch a modal; they have
    # no fields and are never fired directly.
    tile = PICKER_TILES.get(grant_type)
    if tile is not None:
        return {
            "grant_type": grant_type,
            "category": PROGRESSION_OFFLINE_CATEGORY,
            "label": tile["label"],
            "ram_fragile": True,
            "high_value": False,
            "opens": tile["opens"],
            "fields": [],
        }
    meta = meta_by_id.get(grant_type)
    if meta is None:
        return None
    entry = {
        "grant_type": grant_type,
        "category": meta.get("category", ""),
        "label": meta.get("label", grant_type),
        "ram_fragile": bool(meta.get("requires_offline", False)),
        "high_value": bool(meta.get("high_value", False)),
        "fields": _fields_for(grant_type, catalog),
    }
    owner = PICKER_OWNED.get(grant_type)
    if owner is not None:
        entry["picker"] = owner
    return entry


def cart_catalog() -> list:
    """Flat list of cart-eligible entries for the /_index endpoint.
    Each: {grant_type, category, label, ram_fragile, high_value, fields[]}."""
    catalog = _load_catalog()
    meta_by_id = {g.get("id"): g for g in catalog.get("grant_types", []) if g.get("id")}
    out = []
    for gt in CART_GRANT_TYPES:
        entry = _build_entry(gt, catalog, meta_by_id)
        if entry is not None:
            out.append(entry)
    return out


def _describe(entry: dict) -> str:
    if entry.get("ram_fragile"):
        tail = "Offline-only: queued until the player logs out and grace elapses."
    else:
        tail = "Online-safe: applies immediately."
    return f"{entry['label']}. {tail}"


def grant_catalog() -> list:
    """Legacy category-grouped shape for the /_catalog route + template.
    [{category, items:[{grant_type, label, description, ram_fragile, fields}]}]."""
    grouped: list = []
    index: dict = {}
    for entry in cart_catalog():
        item = {
            "grant_type": entry["grant_type"],
            "label": entry["label"],
            "description": _describe(entry),
            "ram_fragile": entry["ram_fragile"],
            "fields": entry["fields"],
        }
        cat = entry["category"] or "Other"
        if cat not in index:
            index[cat] = {"category": cat, "items": []}
            grouped.append(index[cat])
        index[cat]["items"].append(item)
    return grouped


def find_entry(grant_type: str) -> dict | None:
    """Look up a single cart entry by grant_type (legacy flat-item shape)."""
    for entry in cart_catalog():
        if entry["grant_type"] == grant_type:
            return {
                "grant_type": entry["grant_type"],
                "label": entry["label"],
                "description": _describe(entry),
                "ram_fragile": entry["ram_fragile"],
                "fields": entry["fields"],
            }
    return None
