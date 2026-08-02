#!/usr/bin/env python3
"""Augment PERFECT-ROLL + any-grade SWAP writer -- lastsietch-dune-resident psql wrapper.

Phase 1: apply/replace augments on an EXISTING owned inventory item, with all
StatRolls forced to 1.0 (perfect roll) at any chosen grade (1..5). This is the
Last Sietch equivalent of RedBlink's `POST /augment-item`; mechanism + provenance
in our internal design notes

Augments live in dune.items.stats jsonb under key `FAugmentedItemStats`:
  [[], {
    "AppliedAugments":        [{"Name": "T6_Augment_Damage1"}, ...],
    "AppliedAugmentQualities":[5, ...],
    "AppliedAugmentRollData": [{"StatRolls":[1.0,...], "AppliedEffectIndices":[...]}, ...]
  }]
Perfect roll = every StatRolls value 1.0. Swap = the top-level `||` merge REPLACES
the whole FAugmentedItemStats block, so this both installs and re-rolls/replaces
(the in-game "augments can't be removed" rule does not apply to a DB edit).

Correctness rules (non-negotiable, mirror dune-storage-write.py):
  * OFFLINE-GATE (STOP-SHIP): item stats are RAM-backed while online (clobbered on
    save-tick). Refuse unless encrypted_player_state.online_status='Offline' AND NOT
    inside reconnect_grace_period_end. Re-checked inside the write txn. Player must
    relog after the edit for the game to load the new augments.
  * OWNERSHIP (STOP-SHIP): the item must sit in the caller's OWN character inventory
    (inventories.actor_id = the owner's player_pawn_id). Resolved SERVER-SIDE from the
    owner controller id; never trust the caller. Row locked FOR UPDATE; template
    re-verified against what Python validated (guards a swap between read and write).
  * SLOT CAP: clothing <= 2 augments, weapons <= 3 (game limits; hard cap 20).
  * SLOT KEYSTONES: the augment slots are unlocked by Crafting keystones
    42-49 (42/43 Armor, 44-46 Melee, 47-49 Ranged; sp_bonus=0 effect keystones).
    Ensured in-txn via INSERT ... ON CONFLICT DO NOTHING, same target table as
    dune-grant.sh build_keystone_grant (dune.purchased_specialization_keystones).
  * COMPATIBILITY: coarse kind gate (weapon augments only on weapons, armor augments
    only on clothing) from the augment's own tags vs a template-kind heuristic. A
    finer per-item tag match needs our item->tag map (follow-up); this gate still
    blocks the obviously-wrong (armor augment on a rifle).
  * ROLL SHAPE FIDELITY: the number of StatRolls + AppliedEffectIndices must match
    what the game expects or the slot renders empty. Read a REAL standalone augment
    row's FAugmentItemStats from OUR live DB for the true shape; fall back to the
    catalogue-derived roll count. Only the ROLL VALUES are overwritten (all 1.0).
  * Every field validated in Python then folded as an int / enum / charset-checked
    literal. The stats jsonb is built from validated ids + numeric grades in Python,
    json.dumps'd, and folded as a '...'::jsonb literal ('' single-quote escaped).

HARD CONSTRAINTS (mirror dune-storage-write.py):
  * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session
    into the ALREADY-RUNNING DB pod.
  * DARK by default: ships behind LASTSIETCH_AUGMENT_ENABLED (default 0 -> augment_disabled,
    no DB touch). Flip on ONLY after dune-augment-verify.sql confirms our build's
    jsonb shape. --dry-run is always allowed (read-only QA).

Modes:
  --owner-ctrl N --item-id N --augments T6_Augment_Damage1,T6_Augment_Melee1 --grade 5 [--dry-run]
  --stdin-json   {owner_ctrl, item_id, augments:[...], grade, dry_run?}
"""

import argparse
import json
import os
import re
import subprocess
import sys

AUGMENT_ENABLED = os.environ.get("LASTSIETCH_AUGMENT_ENABLED", "0") == "1"

# Augment id charset: game uses the T<digit>_Augment_ family; keep the broad item
# template charset as a backstop. Both must pass.
AUGMENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{2,240}$")
AUGMENT_FAMILY_RE = re.compile(r"^T\d+_Augment_", re.IGNORECASE)

MAX_AUGMENTS_CLOTHING = 2
MAX_AUGMENTS_WEAPON = 3
HARD_CAP = 20

# Crafting augment-slot keystones (verified in keystone-catalog.json; match RedBlink).
KEYSTONES_CLOTHING = [42, 43]
KEYSTONES_MELEE = [44, 45, 46]
KEYSTONES_RANGED = [47, 48, 49]

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "augment-compatibility.json")

DQ = "/root/dq.sh"
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

ERROR_TOKENS = (
    "player_online",
    "item_not_found",
    "not_owner",
    "template_mismatch",
    "keystone_failed",
    "augment_disabled",
    "write_failed",
)


# ---------------------------------------------------------------------------
# I/O helpers (mirror dune-storage-write.py)
# ---------------------------------------------------------------------------

def emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def fail(error, code=1):
    emit({"ok": False, "error": str(error)[:300]}, code)


def pos_int(name, val):
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


# ---------------------------------------------------------------------------
# Catalogue + kind/compat/roll logic (mirrors RedBlink duneDb.js)
# ---------------------------------------------------------------------------

_catalog_cache = None


def catalog():
    global _catalog_cache
    if _catalog_cache is None:
        try:
            with open(_CATALOG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            _catalog_cache = data.get("augments", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _catalog_cache = {}
    return _catalog_cache


def augment_entry(augment_id):
    return catalog().get(augment_id, {})


def augment_roll_count(augment_id):
    """StatRolls length fallback when no real standalone row exists. Mirrors
    RedBlink augmentRollCount: max gradeEffects length, else effectSummary ';'
    segments, else 1."""
    entry = augment_entry(augment_id)
    ge = entry.get("gradeEffects")
    if isinstance(ge, dict):
        lengths = [len(v) for v in ge.values() if isinstance(v, list) and len(v) > 0]
        if lengths:
            return max(lengths)
    es = entry.get("effectSummary")
    if isinstance(es, str) and es.strip():
        return max(1, len([p for p in es.split(";") if p.strip()]))
    return 1


def augment_tags(augment_id):
    entry = augment_entry(augment_id)
    tags = entry.get("tags")
    return [str(t) for t in tags] if isinstance(tags, list) else []


# Strip the T<digit>_Augment_ (and optional ChNN_) prefix to the descriptive core,
# e.g. T6_Augment_Ch5_Scattergun1 -> "Scattergun1". The live DB has ~64 augment ids
# (many weapon-specific: Scattergun/Shotgun/HeavyPistol/SMG/Melee/...) that are NOT in
# RedBlink's generic catalogue, so a NAME fallback is required after the tag lookup.
_AUG_CORE_RE = re.compile(r"^T\d+_Augment_(?:Ch\d+_)?", re.IGNORECASE)


def augment_core(augment_id):
    return _AUG_CORE_RE.sub("", str(augment_id or ""))


def augment_kind(augment_id):
    """weapon | clothing | unknown. Prefer the catalogue's compatibility tags; fall
    back to the augment id's own name for ids not in the catalogue."""
    tags = augment_tags(augment_id)
    if any("RangedWeapons" in t or "MeleeWeapons" in t or "Holsters" in t for t in tags):
        return "weapon"
    if any(t.startswith("Items.Clothes") for t in tags):
        return "clothing"
    # name fallback (covers the live weapon-specific + Armor ids)
    core = augment_core(augment_id)
    if re.match(r"Armor", core, re.IGNORECASE):
        return "clothing"
    if AUGMENT_FAMILY_RE.match(str(augment_id or "")):
        return "weapon"  # Melee* and every other T#_Augment_* are weapon augments
    return "unknown"


def augment_weapon_subkind(augment_ids):
    """(is_melee, is_ranged) for keystone selection. Tags first, then name fallback."""
    tags = [t for a in augment_ids for t in augment_tags(a)]
    is_melee = any("MeleeWeapons" in t for t in tags)
    is_ranged = any("RangedWeapons" in t for t in tags)  # NOT "Holsters" (matches melee too)
    for a in augment_ids:
        core = augment_core(a)
        if re.match(r"Melee", core, re.IGNORECASE):
            is_melee = True
        elif not re.match(r"Armor", core, re.IGNORECASE):
            is_ranged = True
    return is_melee, is_ranged


# Template-kind heuristic (weapon vs clothing) from the item's template id, mirroring
# RedBlink augmentItemKindForTemplate. Coarse but blocks obviously-wrong applies.
_WEAPON_RE = re.compile(
    r"weapon|lasgun|choamlg|spitdart|jabal|dmr|rifle|longrifle|karpov|battle.?rifle|"
    r"hark.?ar|unique.?ar|disruptor|smg|lmg|vulcan|drillshot|shotgun|scattergun|grda|"
    r"pyrocket|fireball|flamethrower|rocket|missile|pistol|snubnose|rafiq|maula|sda|"
    r"melee|sword|blade|knife|dirk|rapier|kindjal|minotaur|dualblades|crysknife|"
    r"dewreaper|ghola|hook|perforator|karpov|drillshot", re.IGNORECASE)
_CLOTHING_RE = re.compile(
    r"social|castoffs|garment|helmet|boots|gloves|stillsuit|still_suit|suit|top|"
    r"bottom|shirt|pants|robe|cloak|hood|wearable|clothing|armor|chest|guard|weave|"
    r"scoutarmor|heavyarmor|utility", re.IGNORECASE)


def template_kind(template_id):
    t = str(template_id or "")
    if _WEAPON_RE.search(t):
        return "weapon"
    if _CLOTHING_RE.search(t):
        return "clothing"
    return "unknown"


# ---------------------------------------------------------------------------
# Reads (dq.sh). All ids are validated ints/charset -> injection-safe.
# ---------------------------------------------------------------------------

def read_item_plan(owner, item_id):
    """Item identity + ownership + offline status. The owner's OWN character
    inventory = inventories whose actor_id equals the owner's player_pawn_id."""
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH me AS (\n"
        "  SELECT player_pawn_id, online_status,\n"
        "         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW()) AS in_grace\n"
        f"    FROM dune.encrypted_player_state WHERE player_controller_id = {owner} LIMIT 1\n"
        ")\n"
        "SELECT json_build_object(\n"
        "  'pawn', (SELECT player_pawn_id FROM me),\n"
        "  'online_status', (SELECT online_status FROM me),\n"
        "  'in_grace', (SELECT in_grace FROM me),\n"
        f"  'item_template', (SELECT template_id FROM dune.items WHERE id = {item_id}),\n"
        f"  'item_inv', (SELECT inventory_id FROM dune.items WHERE id = {item_id}),\n"
        f"  'item_quality', (SELECT quality_level FROM dune.items WHERE id = {item_id}),\n"
        "  'item_stats', (SELECT stats FROM dune.items WHERE id = "
        f"{item_id}),\n"
        "  'owned', (SELECT EXISTS (\n"
        "     SELECT 1 FROM dune.items i JOIN dune.inventories inv ON inv.id = i.inventory_id\n"
        f"      WHERE i.id = {item_id} AND inv.actor_id = (SELECT player_pawn_id FROM me)))\n"
        ")"
    )
    return _read_json(sql)


def read_roll_shapes(augment_ids):
    """Per requested augment id, the real standalone augment row's FAugmentItemStats
    shape (StatRolls length + AppliedEffectIndices) from OUR live DB, if present.
    Returns {augment_id: {"rollCount": n, "appliedEffectIndices": [...]}}. Missing
    ids fall back to the catalogue count in build_roll_payloads()."""
    ids_sql = ",".join(f"'{a}'" for a in augment_ids)  # ids are charset-validated
    sql = (
        "SET search_path TO dune, public;\n"
        "SELECT COALESCE(json_object_agg(template_id, shape), '{}'::json) FROM (\n"
        "  SELECT DISTINCT ON (template_id) template_id,\n"
        "    json_build_object(\n"
        "      'rollCount', jsonb_array_length(COALESCE(stats #> '{FAugmentItemStats,1,StatRolls}', '[]'::jsonb)),\n"
        "      'appliedEffectIndices', COALESCE(stats #> '{FAugmentItemStats,1,AppliedEffectIndices}', '[]'::jsonb)\n"
        "    ) AS shape\n"
        "  FROM dune.items\n"
        f"  WHERE template_id IN ({ids_sql}) AND stats ? 'FAugmentItemStats'\n"
        "  ORDER BY template_id, id DESC\n"
        ") s"
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


# ---------------------------------------------------------------------------
# Validation + payload build
# ---------------------------------------------------------------------------

def validate_augment_ids(raw_list):
    ids = []
    for a in raw_list:
        a = str(a).strip()
        if not a:
            continue
        if not AUGMENT_ID_RE.match(a):
            fail("bad_augment_id", 2)
        ids.append(a)
    # de-dupe preserving order, hard cap
    seen, out = set(), []
    for a in ids:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out[:HARD_CAP]


def validate_grade(val):
    if isinstance(val, bool):
        fail("bad_grade", 2)
    try:
        g = int(val)
    except (TypeError, ValueError):
        fail("bad_grade", 2)
    if g < 1 or g > 5:
        fail("bad_grade", 2)
    return g


def slot_cap_for(kind):
    return MAX_AUGMENTS_CLOTHING if kind == "clothing" else MAX_AUGMENTS_WEAPON


def keystones_for(kind, augment_ids):
    if kind == "clothing":
        return KEYSTONES_CLOTHING
    is_melee, is_ranged = augment_weapon_subkind(augment_ids)
    if is_melee and not is_ranged:
        return KEYSTONES_MELEE
    if is_ranged and not is_melee:
        return KEYSTONES_RANGED
    return KEYSTONES_MELEE + KEYSTONES_RANGED


def preflight(plan, augment_ids, grade):
    """Return (errors[], resolved_kind, roll_shapes) for dry-run + as the live
    precondition. Ownership/offline are re-checked atomically in the write txn."""
    errs = []
    if plan.get("pawn") is None:
        errs.append("not_owner")
    if plan.get("item_template") is None or plan.get("item_inv") is None:
        errs.append("item_not_found")
    if not (plan.get("online_status") == "Offline" and not plan.get("in_grace")):
        errs.append("player_online")
    if not plan.get("owned"):
        errs.append("not_owner")

    tmpl = plan.get("item_template") or ""
    item_kind = template_kind(tmpl)
    # kind gate: every augment's kind must be applicable to the item kind
    aug_kinds = {a: augment_kind(a) for a in augment_ids}
    if item_kind == "unknown":
        # can't prove the item is augmentable; block rather than risk a broken item
        errs.append("unknown_item_kind")
    else:
        mismatched = [a for a, k in aug_kinds.items() if k != "unknown" and k != item_kind]
        if mismatched:
            errs.append("incompatible_augment")
        unknown_aug = [a for a, k in aug_kinds.items() if k == "unknown"]
        if unknown_aug:
            errs.append("unknown_augment")
        cap = slot_cap_for(item_kind)
        if len(augment_ids) > cap:
            errs.append("too_many_augments")
    return errs, item_kind


def build_roll_payloads(augment_ids, grade, roll_shapes):
    """Per augment id: {StatRolls:[1.0]*n, AppliedEffectIndices:[...]} with the real
    shape where known, else catalogue-derived count. All rolls = 1.0 (perfect)."""
    payloads = []
    for a in augment_ids:
        shape = roll_shapes.get(a) or {}
        n = shape.get("rollCount")
        if not isinstance(n, int) or n <= 0:
            n = augment_roll_count(a)
        indices = shape.get("appliedEffectIndices")
        if not isinstance(indices, list):
            indices = []
        payloads.append({
            "StatRolls": [1.0] * max(1, n),
            "AppliedEffectIndices": indices,
        })
    return payloads


def build_augmented_item_stats_value(augment_ids, grade, payloads):
    """The FAugmentedItemStats VALUE (the [[], {...}] array)."""
    return [
        [],
        {
            "AppliedAugments": [{"Name": a} for a in augment_ids],
            "AppliedAugmentQualities": [grade for _ in augment_ids],
            "AppliedAugmentRollData": payloads,
        },
    ]


def sql_jsonb_literal(obj):
    """json.dumps -> SQL '...'::jsonb literal with '' single-quote escaping."""
    return "'" + json.dumps(obj, separators=(",", ":")).replace("'", "''") + "'::jsonb"


# ---------------------------------------------------------------------------
# Write SQL (DO block; re-checks offline + ownership atomically)
# ---------------------------------------------------------------------------

def build_augment_sql(owner, item_id, template_id, is_weapon, keystone_ids, faug_value):
    faug_obj = {"FAugmentedItemStats": faug_value}
    faug_lit = sql_jsonb_literal(faug_obj)
    tmpl_lit = "'" + str(template_id).replace("'", "''") + "'"
    keystones_arr = "ARRAY[" + ",".join(str(int(k)) for k in keystone_ids) + "]::bigint[]"
    weapon_flag = "true" if is_weapon else "false"
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _aug_result;
CREATE TEMP TABLE _aug_result(
  item_id bigint, template_id text, keystones_inserted int,
  augment_count int
) ON COMMIT PRESERVE ROWS;
DO $augment$
DECLARE
  v_pawn bigint; v_status text; v_grace bool;
  v_inv bigint; v_tmpl text; v_stats jsonb;
  v_ks_ins int := 0;
BEGIN
  -- 1. resolve the owner's pawn + OFFLINE-GATE (stats are RAM-backed while online)
  SELECT player_pawn_id, online_status,
         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())
    INTO v_pawn, v_status, v_grace
    FROM dune.encrypted_player_state
    WHERE player_controller_id = {owner} LIMIT 1;
  IF v_pawn IS NULL THEN RAISE EXCEPTION 'not_owner (no pawn for ctrl {owner})'; END IF;
  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN
    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);
  END IF;
  -- 2. lock the item; verify it is in the owner's OWN character inventory
  SELECT i.inventory_id, i.template_id, i.stats
    INTO v_inv, v_tmpl, v_stats
    FROM dune.items i
    JOIN dune.inventories inv ON inv.id = i.inventory_id
    WHERE i.id = {item_id} AND inv.actor_id = v_pawn
    FOR UPDATE OF i;
  IF v_inv IS NULL THEN RAISE EXCEPTION 'not_owner (item {item_id} not in pawn % inventory)', v_pawn; END IF;
  -- 3. template guard: the item must still be what Python validated (swap-proof)
  IF v_tmpl IS DISTINCT FROM {tmpl_lit} THEN
    RAISE EXCEPTION 'template_mismatch (had=% want=%)', v_tmpl, {tmpl_lit};
  END IF;
  -- 4. ensure the Crafting augment-slot keystones exist (sp_bonus=0; INSERT only).
  -- NOT EXISTS guard (not ON CONFLICT) so we do not depend on a unique constraint.
  INSERT INTO dune.purchased_specialization_keystones (player_id, keystone_id)
    SELECT {owner}, k FROM unnest({keystones_arr}) AS k
    WHERE NOT EXISTS (
      SELECT 1 FROM dune.purchased_specialization_keystones p
       WHERE p.player_id = {owner} AND p.keystone_id = k);
  GET DIAGNOSTICS v_ks_ins = ROW_COUNT;
  -- 5. build the stats: preserve existing keys, ensure base blocks, REPLACE augments
  v_stats := COALESCE(v_stats, '{{}}'::jsonb);
  IF NOT (v_stats ? 'FCustomizationStats') THEN
    v_stats := v_stats || '{{"FCustomizationStats":[[],{{}}]}}'::jsonb;
  END IF;
  IF NOT (v_stats ? 'FItemStackAndDurabilityStats') THEN
    v_stats := v_stats || '{{"FItemStackAndDurabilityStats":[[],{{"CurrentDurability":100,"MaxDurability":100,"DecayedMaxDurability":100}}]}}'::jsonb;
  END IF;
  IF {weapon_flag} AND NOT (v_stats ? 'FWeaponItemStats') THEN
    v_stats := v_stats || '{{"FWeaponItemStats":[[],{{"CurrentAmmo":0}}]}}'::jsonb;
  END IF;
  v_stats := v_stats || {faug_lit};  -- top-level || REPLACES FAugmentedItemStats (= swap)
  -- 6. write; reset is_new so the game reloads the item's stats on next login
  UPDATE dune.items SET stats = v_stats, is_new = false WHERE id = {item_id} AND inventory_id = v_inv;
  INSERT INTO _aug_result VALUES ({item_id}, v_tmpl, v_ks_ins,
    jsonb_array_length({faug_lit} #> '{{FAugmentedItemStats,1,AppliedAugments}}'));
END $augment$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'augment',
  'item_id', item_id, 'template_id', template_id,
  'keystones_inserted', keystones_inserted, 'augment_count', augment_count
) FROM _aug_result;
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
        if line in ("SET", "BEGIN", "COMMIT", "CREATE TABLE", "DROP TABLE", "DO", "INSERT 0 1"):
            continue
        if line.startswith(("INSERT ", "UPDATE ", "SELECT ", "NOTICE", "PERFORM")):
            continue
        raw = line
    return raw


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

def do_dry_run(owner, item_id, augment_ids, grade):
    plan = read_item_plan(owner, item_id)
    errs, item_kind = preflight(plan, augment_ids, grade)
    roll_shapes = read_roll_shapes(augment_ids) if augment_ids else {}
    payloads = build_roll_payloads(augment_ids, grade, roll_shapes)
    faug = build_augmented_item_stats_value(augment_ids, grade, payloads)
    kind_for_ks = item_kind if item_kind != "unknown" else "weapon"
    emit({
        "dry_run": True, "action": "augment", "augment_enabled": AUGMENT_ENABLED,
        "owner_ctrl": owner, "item_id": item_id,
        "item_template": plan.get("item_template"),
        "item_kind": item_kind, "item_quality": plan.get("item_quality"),
        "online_status": plan.get("online_status"), "in_grace": plan.get("in_grace"),
        "owned": plan.get("owned"),
        "augments": augment_ids, "grade": grade,
        "roll_shapes_from_db": roll_shapes,
        "keystones_to_ensure": keystones_for(kind_for_ks, augment_ids),
        "faugmented_item_stats": faug,
        "preflight_errors": errs,
    }, 0)


def do_live(owner, item_id, augment_ids, grade):
    if not AUGMENT_ENABLED:
        emit({"ok": False, "error": "augment_disabled"}, 0)
    plan = read_item_plan(owner, item_id)
    errs, item_kind = preflight(plan, augment_ids, grade)
    if errs:
        fail(errs[0], 1)
    template_id = plan.get("item_template")
    is_weapon = item_kind == "weapon"
    keystone_ids = keystones_for(item_kind, augment_ids)
    roll_shapes = read_roll_shapes(augment_ids)
    payloads = build_roll_payloads(augment_ids, grade, roll_shapes)
    faug = build_augmented_item_stats_value(augment_ids, grade, payloads)

    ns, pod = resolve_db_pod()
    sql = build_augment_sql(owner, item_id, template_id, is_weapon, keystone_ids, faug)
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
    result["augments"] = augment_ids
    result["grade"] = grade
    emit(result, 0)


def gather_args():
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        owner = pos_int("owner_ctrl", payload.get("owner_ctrl"))
        item_id = pos_int("item_id", payload.get("item_id"))
        augments = payload.get("augments") or []
        if not isinstance(augments, list):
            fail("bad_augments", 2)
        grade = validate_grade(payload.get("grade", 5))
        dry_run = bool(payload.get("dry_run", False))
    else:
        ap = argparse.ArgumentParser(description="Augment perfect-roll / swap writer (Phase 1)")
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--item-id", required=True)
        ap.add_argument("--augments", required=True,
                        help="comma-separated augment template ids (T6_Augment_*)")
        ap.add_argument("--grade", default="5")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        owner = pos_int("owner_ctrl", a.owner_ctrl)
        item_id = pos_int("item_id", a.item_id)
        augments = [x for x in a.augments.split(",")]
        grade = validate_grade(a.grade)
        dry_run = a.dry_run

    augment_ids = validate_augment_ids(augments)
    if not augment_ids:
        fail("no_augments", 2)
    return owner, item_id, augment_ids, grade, dry_run


def main():
    owner, item_id, augment_ids, grade, dry_run = gather_args()
    if dry_run:
        do_dry_run(owner, item_id, augment_ids, grade)
    else:
        do_live(owner, item_id, augment_ids, grade)


if __name__ == "__main__":
    main()
