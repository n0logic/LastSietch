"""Label map + publish gate for the live Server Rules panel (wave 5).

`data/gameplay-settings-catalog.json` is a byte-identical copy of
`docs/dune-research/gameplay-settings-catalog-2026-06-13.json` (24 knobs, the
whole `serverGameplaySettings` body). It supplies HUMAN TEXT only: the label,
the one-line "what it controls", and the unit. It never supplies a value --
`liveValue` there is a June observation and publishing it would show a number
nobody read today.

IMPORT-SAFE BY CONTRACT. main.py imports routers/dune.py unguarded and
routers/dune.py imports this module, so a raise here is not one dead panel, it
is the whole portal failing to boot. Every failure (file absent, unreadable,
not JSON, wrong shape) degrades to an empty label map and the page renders its
rows unread.

The catalog's own `do_not_expose` list is inert folklore: it names none of the
24 jsonKeys in this body, so it filters nothing. The gate that decides what the
public actually sees is PUBLISHED below, a positive allowlist. `do_not_expose`
and `not_in_this_body` are still parsed and kept as a negative filter over raw
keys, so a future build that renames a knob into one of those names can never
surface it.
"""
import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).parent / "data" / "gameplay-settings-catalog.json"

# The positive allowlist: (category, jsonKey), in the order the page reads them.
# Category is structural (it is the block the key lives in on the wire), not
# copy -- every word a player reads comes from the catalog.
PUBLISHED = (
    ("CoreSettings", "doubleDifficultyLoot"),
    ("SurvivalSettings", "hydrationEnabled"),
    ("SurvivalSettings", "sandstormEnabled"),
    ("SurvivalSettings", "sandStormAutoSpawn"),
    ("SurvivalSettings", "sandStormCoriolisAutoSpawnEnabled"),
    ("SurvivalSettings", "sandStormTreasureEnabled"),
    ("SurvivalSettings", "sandwormEnabled"),
    ("SurvivalSettings", "sandwormDangerZonesEnabled"),
    ("SurvivalSettings", "vehicleSandwormCollisionInteraction"),
    ("SurvivalSettings", "vehicleSandwormInvulnerabilitySecondsOnExit"),
    ("SurvivalSettings", "vehicleSandwormInvulnerabilitySecondsOnServerRestart"),
    ("CombatSettings", "securityZonesForceEnablePvp"),
    ("CombatSettings", "areSecurityZonesEnabled"),
    ("CombatSettings", "shouldForceEnablePvpOnAllPartitions"),
    ("HarvestingSettings", "miningOutputMultiplier"),
    ("HarvestingSettings", "vehicleMiningOutputMultiplier"),
    ("HarvestingSettings", "securityZonesPvpResourceMultiplier"),
    ("PersistenceSettings", "buildingBlueprintMaxExtensions"),
    ("PersistenceSettings", "baseBackupMaxExtensions"),
)

# The five of the 24 that stay off the public page. The reason is the point:
#   serverDisplayName                       per-partition string (Habbanya /
#                                           Kulon / ...), not a server rule, and
#                                           it is the one string value in the body
#   vehicleDurabilityDamageMultiplier       BGD reports 1.0, our ini says 0.5 in
#                                           three places; publishing a number the
#                                           config contradicts publishes a lie
#   itemDeteriorationUpdateRate             0.0 reads as "decay is off", which is
#                                           not what the knob means
#   inventoryDecayedMaxDurabilityThreshold  no honest one-line explanation yet
#   sandworm spawn type enum                an unlabelled integer enum, null on
#                                           the wire. The sample spells it
#                                           `sandwormSpawnType` and the catalog
#                                           `sandwormSpawningType`: the sample
#                                           wins, and both spellings are withheld
#                                           so neither build can surface it.
WITHHELD_KEYS = frozenset({
    "serverDisplayName",
    "vehicleDurabilityDamageMultiplier",
    "itemDeteriorationUpdateRate",
    "inventoryDecayedMaxDurabilityThreshold",
    "sandwormSpawnType",
    "sandwormSpawningType",
})

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _deny_from(doc):
    """Every identifier-shaped token named by `do_not_expose` / `not_in_this_body`.

    The members are prose ("CAT E folklore keys (SpiceTaxAmount, bServerPVE,
    ...)"), so tokens are extracted rather than matched whole. Over-collecting
    is free here: this is a deny list applied on top of a positive allowlist,
    and no published key may appear in it (pinned by the suite)."""
    deny = set()
    for section in ("do_not_expose", "not_in_this_body"):
        block = doc.get(section)
        if not isinstance(block, dict):
            continue
        for member in block.get("members") or []:
            if isinstance(member, str):
                deny.update(_TOKEN.findall(member))
    return frozenset(deny)


def _load(path):
    """(labels, deny) read from the catalog file. NEVER raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            return {}, frozenset()
        labels = {}
        for knob in doc.get("knobs") or []:
            if not isinstance(knob, dict):
                continue
            key = knob.get("jsonKey")
            if not isinstance(key, str) or not key:
                continue
            labels[key] = {
                "label": knob.get("human"),
                "controls": knob.get("controls"),
                "unit": knob.get("unit"),
                "valueType": knob.get("valueType"),
            }
        return labels, _deny_from(doc)
    except Exception:
        return {}, frozenset()


def _load_config(path):
    """(note, rows) for the "from server config" block: values that are NOT in the
    live feed (ini and client properties such as the land claim cap) but that
    players ask about. Data, never code: every row comes from the catalog file.
    NEVER raises; a malformed row is dropped, a missing block is empty."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            return "", []
        note = doc.get("config_note")
        rows = []
        for r in doc.get("config_rows") or []:
            if not isinstance(r, dict):
                continue
            key, human, value = r.get("key"), r.get("human"), r.get("value")
            if not isinstance(key, str) or not key or not isinstance(human, str) or not human:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            rows.append({
                "key": key,
                "label": human,
                "controls": r.get("controls") if isinstance(r.get("controls"), str) else None,
                "value": value,
                "unit": r.get("unit") if isinstance(r.get("unit"), str) else None,
                "basis": r.get("basis") if isinstance(r.get("basis"), str) else None,
            })
        return (note if isinstance(note, str) else ""), rows
    except Exception:
        return "", []


LABELS, RAW_DENY = _load(CATALOG_PATH)
CONFIG_NOTE, CONFIG_ROWS = _load_config(CATALOG_PATH)


def block_for(category):
    """Wire block name for a catalog category: CoreSettings -> coreSettings."""
    return category[:1].lower() + category[1:] if category else ""


def category_label(category):
    """CoreSettings -> 'Core settings'. Derived, so a new category needs no table."""
    spaced = re.sub(r"(?<!^)([A-Z])", r" \1", category or "")
    return spaced[:1].upper() + spaced[1:].lower()


def label_for(key):
    """(label, controls, unit) for one key. All three are None when the catalog
    did not load -- the row still renders, without its copy."""
    entry = LABELS.get(key) or {}
    return entry.get("label"), entry.get("controls"), entry.get("unit")


def value_type_for(key):
    """The catalog's valueType for a published key ("bool", "int", "float", ...)
    or None when the catalog is empty. Used to render int-encoded booleans
    (sandstormEnabled is 1/0 on the wire) as on/off instead of a bare 1."""
    entry = LABELS.get(key) or {}
    return entry.get("valueType") or None


def is_publishable(key):
    return (key not in WITHHELD_KEYS
            and key not in RAW_DENY
            and any(key == k for _, k in PUBLISHED))
