#!/usr/bin/env python3
"""Augment PERFECT-ROLL + any-grade SWAP writer -- game-host-resident psql wrapper.

Phase 1: apply/replace augments on an EXISTING owned inventory item, with all
StatRolls forced to 1.0 (perfect roll) at any chosen grade (1..5). This is the
Last Sietch equivalent of RedBlink's `POST /augment-item`; mechanism + provenance
in docs/dune-research/AUGMENT-PERFECT-ROLL-RE-2026-07-14.md.

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
import random
import re
import subprocess
import sys

# Roll modes. 'perfect' is the prize path (every StatRolls value 1.0); 'random'
# is the player-facing reroll, which must be able to come out worse or it is not
# a reroll at all. Default stays 'perfect' so existing callers are unchanged.
ROLL_MODE_PERFECT = "perfect"
ROLL_MODE_RANDOM = "random"
ROLL_MODES = (ROLL_MODE_PERFECT, ROLL_MODE_RANDOM)

# Go-live gate. Mirrors dune-reward-op.sh: an env var for ad-hoc operator runs,
# PLUS a flag FILE, because the env var alone can never reach a player-initiated
# call. The dispatcher is invoked as `ssh host augment-op`, a non-interactive
# non-login shell that inherits no profile, so nothing a shell rc exports is
# visible here. The file is the only channel that works for the real path, and
# `rm /etc/lastsietch/augment-enabled` is an instant kill-switch.
# Reroll cap: N rerolls per ITEM per window (owner call 2026-08-03). Applies ONLY
# to player-initiated rerolls, which are identified by carrying an idempotency
# key; the operator/prize paths pass none and are deliberately uncapped so an
# admin can always fix something.
#
# WHY A CAP AT ALL. A reroll is free, so without one a patient player just spams
# until every roll is perfect. That does not merely trivialise the feature, it
# makes our own copy untrue: "it is a true reroll, not an upgrade, it can land
# worse" only means something if you cannot simply try again forever.
#
# SWAP is NOT capped: it already costs a rare augment, which is a far harder
# limit than a timer.
REROLL_CAP = 3
REROLL_WINDOW_HOURS = 4

AUGMENT_FLAG_FILE = "/etc/lastsietch/augment-enabled"
AUGMENT_ENABLED = (os.environ.get("LASTSIETCH_AUGMENT_ENABLED", "0") == "1"
                   or os.path.isfile(AUGMENT_FLAG_FILE))

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
    # preflight tokens. unknown_item_tags is deliberately distinct from
    # incompatible_augment: the first means we have never catalogued this item,
    # the second means the augment genuinely does not fit. Different messages.
    "unknown_item_kind",
    "unknown_item_tags",
    "unknown_augment",
    "incompatible_augment",
    "too_many_augments",
    "bad_roll_mode",
    # An idempotency key was supplied but the ledger cannot honour it. Distinct
    # from write_failed on purpose: nothing was attempted, and the caller may
    # safely retry once the migration is applied. NEVER silently proceed -- a
    # swap without a working key can double-consume a rare augment.
    "idempotency_unavailable",
    # player-facing reroll allowance for THIS item is spent for now
    "reroll_capped",
    # the write would have REDUCED the item's augment count (a reroll/swap/install
    # never legitimately shrinks it). Caused BUG-019: a short `augments` list
    # REPLACES the whole block and silently deletes the omitted slots. This token
    # means the writer refused rather than destroying a slot. See build_augment_sql.
    "augment_count_shrank",
)


# ---------------------------------------------------------------------------
# I/O helpers (mirror dune-storage-write.py)
# ---------------------------------------------------------------------------

def emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def fail(error, code=1, **extra):
    """Emit a refusal. `extra` carries structured detail the caller needs to write
    a useful message (e.g. how long until a capped reroll frees up); without it
    the portal can only say "no" with no idea when to come back."""
    payload = {"ok": False, "error": str(error)[:300]}
    payload.update(extra)
    emit(payload, code)


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
_aliases_cache = None


def _catalog_raw():
    try:
        with open(_CATALOG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog():
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = _catalog_raw().get("augments", {}) or {}
    return _catalog_cache


def item_aliases():
    """template_id -> [item tags]. The catalogue has shipped this all along
    (itemAliases, 172 entries); the RE writeup's claim that we lack an item->tag
    map is wrong, and the coarse weapon-vs-clothing gate below was built on that
    wrong belief."""
    global _aliases_cache
    if _aliases_cache is None:
        _aliases_cache = _catalog_raw().get("itemAliases", {}) or {}
    return _aliases_cache


_ITEM_TAGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "item-tags.json")
_item_tags_cache = None


def item_tag_map():
    """template_id -> taxonomy tags, MERGED from the method.gg-derived itemAliases
    and our own client-pak extraction (dune-item-pak-meta.json). 1,159 entries vs
    the catalogue's 170, because the catalogue alone left real gear uncovered
    (Combat_Choam_Light06_*, ChoamSda5) and this gate fails CLOSED, so a gap is a
    refused write. Regenerated from both sources; the pak half is keyed LOWERCASE.
    Where both sources have an item they agree on the taxonomy, and the merged map
    still accepts all 492 real in-game installs with zero false rejections."""
    global _item_tags_cache
    if _item_tags_cache is None:
        try:
            with open(_ITEM_TAGS_PATH, "r", encoding="utf-8") as fh:
                _item_tags_cache = (json.load(fh) or {}).get("tags") or {}
        except (OSError, ValueError):
            _item_tags_cache = {}
    return _item_tags_cache


_TAXONOMY_PREFIXES = ("Items.Holsters", "Items.Clothes")


def item_tags(template_id):
    """Taxonomy tags only. Non-taxonomy tags (Items.ExcludeFromLootSystem and the
    like) are filtered out: they carry no compatibility meaning and an augment tag
    could never match them anyway."""
    tid = str(template_id or "")
    for source in (item_aliases().get(tid),
                   item_tag_map().get(tid),
                   item_tag_map().get(tid.lower())):
        if isinstance(source, list):
            keep = [str(t) for t in source if str(t).startswith(_TAXONOMY_PREFIXES)]
            if keep:
                return keep
    return []


def augment_fits_item(template_id, augment_id):
    """The catalogue's own documented matchRule: compatible when any ITEM tag
    starts with any AUGMENT tag. Returns True/False, or None when either side is
    absent from the catalogue (caller must fail closed, NOT guess).

    VALIDATED 2026-08-01 against every augment install players made in game:
    492 real (item, augment) pairs over 279 augmented items, 0 rejections and 0
    undecidables. So this rule reproduces the game's own gating exactly, which is
    what makes it safe to enforce on a player-facing swap. The coarse kind gate
    stays as a second, independent check."""
    it_tags = item_tags(template_id)
    aug_tags = augment_tags(augment_id)
    if not it_tags or not aug_tags:
        return None
    return any(i.startswith(a) for i in it_tags for a in aug_tags)


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
    """Item identity + ownership + offline status.

    OWNERSHIP = the pawn's own inventories ONLY: backpack, worn, hotbar and the
    CHOAM bank, all of which hang off the pawn actor.

    🔴 NARROWED BACK TO PAWN-SIDE 2026-08-03. Storage was briefly in scope, but a
    container's or vehicle's contents live in its PARTITION server's memory for as
    long as that partition is up, and 28 of 30 partitions are alive at any moment.
    Against a live partition our UPDATE is RETRACTED (the reroll silently does not
    stick) and our DELETE is RESURRECTED (the consumed augment comes back, so a
    swap DUPLICATES a rare item). Gating on the player being offline does not help:
    the partition is a separate process. Hub maps do not help either -- Arrakeen,
    Harko and Overland vehicles sit on live partitions too.

    Pawn side is the safe surface because it re-hydrates from the DB at login, and
    `inv.actor_id = pawn` already covers backpack, worn, hotbar AND the CHOAM bank
    (its type-30 inventory hangs off the pawn actor). Players move an augment into
    their backpack in game, which the server does safely, then log out and swap.
    See the partition RAM residency constraint."""
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH me AS (\n"
        "  SELECT player_pawn_id, online_status,\n"
        "         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW()) AS in_grace\n"
        f"    FROM dune.encrypted_player_state WHERE player_controller_id = {owner}\n"
        "       AND character_state IS DISTINCT FROM 'Deleted'\n"
        "     ORDER BY player_controller_id DESC LIMIT 1\n"
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
        f"      WHERE i.id = {item_id}\n"
        "        AND inv.actor_id = (SELECT player_pawn_id FROM me)))\n"
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


def installed_augment_ids(item_stats):
    """The augment ids currently on the item, from its FAugmentedItemStats block.
    Used to work out what a request actually INSTALLS: a pure reroll keeps the same
    ids (installs nothing, consumes nothing) while a swap brings in a new one."""
    try:
        block = (item_stats or {}).get("FAugmentedItemStats")
        applied = block[1].get("AppliedAugments") or []
        return [str(a.get("Name")) for a in applied if isinstance(a, dict) and a.get("Name")]
    except (AttributeError, IndexError, KeyError, TypeError):
        return []


def newly_installed(augment_ids, item_stats):
    """Requested ids that are not already installed, i.e. what must be consumed.
    Order-preserving; duplicates already removed by validate_augment_ids()."""
    have = set(installed_augment_ids(item_stats))
    return [a for a in augment_ids if a not in have]


_IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def validate_idem_key(val):
    """Charset-capped idempotency key, or None. Deliberately narrow: the value
    is folded into a SQL literal, and while it is quote-escaped like every other
    literal here, a key that cannot contain a quote in the first place is one
    less thing to reason about. uuid4 from the portal fits comfortably."""
    if val is None or val == "":
        return None
    s = str(val)
    if not _IDEM_RE.match(s):
        fail("bad_idempotency_key", 2)
    return s


def validate_roll_mode(val):
    mode = str(val or ROLL_MODE_PERFECT).strip().lower()
    if mode not in ROLL_MODES:
        fail("bad_roll_mode", 2)
    return mode


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
    # gate 1 (coarse): every augment's kind must be applicable to the item kind
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

    # BUG-019 guard: a reroll/swap/install must never REDUCE the augment count.
    # The writer REPLACES the item's whole augment block from `augment_ids`, so a
    # list shorter than what the item carries silently deletes the omitted slots
    # (this destroyed a player's Melee9 + Melee6 augments on 2026-08-06). Refuse. This is the
    # ADVISORY copy (Python read, outside the row lock); the authoritative check
    # runs inside the write transaction (see build_augment_sql). Unconditional on
    # item_kind: truncation must be blocked even for an item we cannot classify.
    installed_now = installed_augment_ids(plan.get("item_stats"))
    if len(augment_ids) < len(installed_now):
        errs.append("augment_count_shrank")

    # gate 2 (exact): the catalogue's tag rule, which distinguishes per WEAPON
    # TYPE. The coarse gate above happily allows a Lasgun augment onto a
    # scattergun; this one does not. FAIL CLOSED when the catalogue cannot decide:
    # a distinct token, because "we have never catalogued this item" is a
    # different conversation with the player than "that augment does not fit".
    # Known gaps in itemAliases as of 2026-08-01: the Combat_Choam_Light06_* armor
    # set and ChoamSda5. Extend the catalogue rather than loosening this.
    for a in augment_ids:
        verdict = augment_fits_item(tmpl, a)
        if verdict is None:
            errs.append("unknown_item_tags" if not item_tags(tmpl) else "unknown_augment")
        elif not verdict:
            errs.append("incompatible_augment")

    # de-dupe, preserving first-seen order, so one bad augment does not emit the
    # same token three times
    seen, deduped = set(), []
    for e in errs:
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped, item_kind


def build_roll_payloads(augment_ids, grade, roll_shapes, roll_mode=ROLL_MODE_PERFECT,
                        keep_rolls=None, reroll_only=None):
    """Per augment id: {StatRolls:[...]*n, AppliedEffectIndices:[...]} with the real
    shape where known, else catalogue-derived count.

    roll_mode:
      'perfect' -> every StatRolls value 1.0. This is a PRIZE mode. Handing it to
                   players deletes augment rolls as a source of variance, so it is
                   deliberately not the reroll default.
      'random'  -> a fresh uniform draw in [0.0, 1.0] per roll slot. This is what
                   makes a reroll an actual reroll: it can come out worse. Matches
                   the observed live distribution, where real rolls span the full
                   0.0-1.0 range (n=1053, min 0.0, max 1.0).

    Roll COUNT varies per augment (live: Spitdart 7, Melee6 2, Rateoffire1 1), so
    the shape is always taken from a real standalone augment row where one exists
    and only the VALUES are replaced. Getting the count wrong renders an empty
    augment slot in game."""
    rng = random.SystemRandom()
    keep_rolls = keep_rolls or {}
    payloads = []
    for a in augment_ids:
        # PER-SLOT reroll: anything not named in reroll_only keeps the rolls it
        # already has, byte-for-byte. reroll_only=None means whole-item (every slot
        # redrawn), which is the original behaviour and what the prize path wants.
        if reroll_only is not None and a not in reroll_only and a in keep_rolls:
            payloads.append({
                "StatRolls": list(keep_rolls[a]["StatRolls"]),
                "AppliedEffectIndices": list(keep_rolls[a]["AppliedEffectIndices"]),
            })
            continue
        shape = roll_shapes.get(a) or {}
        n = shape.get("rollCount")
        if not isinstance(n, int) or n <= 0:
            n = augment_roll_count(a)
        indices = shape.get("appliedEffectIndices")
        if not isinstance(indices, list):
            indices = []
        n = max(1, n)
        if roll_mode == ROLL_MODE_RANDOM:
            rolls = [round(rng.random(), 6) for _ in range(n)]
        else:
            rolls = [1.0] * n
        payloads.append({
            "StatRolls": rolls,
            "AppliedEffectIndices": indices,
        })
    return payloads


def build_augmented_item_stats_value(augment_ids, grade, payloads, grades=None):
    """The FAugmentedItemStats VALUE (the [[], {...}] array).

    `grades` is a PER-SLOT list. Applying one uniform grade to every slot is wrong:
    grade is a property of the individual augment the player earned, not of the
    item. Measured on live 2026-08-01: 34 items carry slots at DIFFERENT grades
    (e.g. the owner's Power Harness runs Armor14 at G5 next to Armor10 at G4), so a
    reroll that collapsed them to a single grade would silently buff or nerf gear
    the player never asked us to touch. `grade` remains the fallback for callers
    that genuinely want one value everywhere (the prize path)."""
    if not grades or len(grades) != len(augment_ids):
        grades = [grade for _ in augment_ids]
    return [
        [],
        {
            "AppliedAugments": [{"Name": a} for a in augment_ids],
            "AppliedAugmentQualities": list(grades),
            "AppliedAugmentRollData": payloads,
        },
    ]


def block_slots(faug_block):
    """(ids, grades, stat_rolls) read back out of a raw FAugmentedItemStats VALUE
    (the [[], {...}] array the writer stores), in SLOT ORDER.

    The installed_* helpers above key by augment id, which is right for their
    jobs but loses order and collapses an item carrying the same augment twice.
    The three arrays here are positional and must stay index-aligned, exactly as
    the game stores them. Returns (None, None, None) on anything malformed so
    callers fall back rather than emitting a half-parsed answer."""
    try:
        slot = (faug_block or [])[1]
        applied = slot.get("AppliedAugments") or []
        quals = slot.get("AppliedAugmentQualities") or []
        rolldata = slot.get("AppliedAugmentRollData") or []
        ids = [str(a.get("Name")) for a in applied if isinstance(a, dict) and a.get("Name")]
        if not ids:
            return None, None, None
        grades = [int(q) for q in quals[:len(ids)]]
        rolls = [list((rolldata[i] or {}).get("StatRolls") or [])
                 for i in range(min(len(ids), len(rolldata)))]
        return ids, grades, rolls
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None, None, None


def installed_rolls(item_stats):
    """{augment_id: [StatRolls]} currently on the item. Lets a PER-SLOT reroll carry
    the untouched slots' rolls through byte-for-byte, instead of redrawing the whole
    item. Without this a player chasing one bad roll has to gamble their good ones."""
    try:
        block = (item_stats or {}).get("FAugmentedItemStats")
        applied = block[1].get("AppliedAugments") or []
        rolldata = block[1].get("AppliedAugmentRollData") or []
        out = {}
        for i, a in enumerate(applied):
            if not (isinstance(a, dict) and a.get("Name")) or i >= len(rolldata):
                continue
            entry = rolldata[i] or {}
            rolls = entry.get("StatRolls")
            if isinstance(rolls, list) and rolls:
                out[str(a["Name"])] = {
                    "StatRolls": [float(r) for r in rolls],
                    "AppliedEffectIndices": entry.get("AppliedEffectIndices") or [],
                }
        return out
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return {}


def installed_grades(item_stats):
    """{augment_id: grade} currently on the item, so a reroll can PRESERVE the
    grade of each slot instead of overwriting it."""
    try:
        block = (item_stats or {}).get("FAugmentedItemStats")
        applied = block[1].get("AppliedAugments") or []
        quals = block[1].get("AppliedAugmentQualities") or []
        out = {}
        for i, a in enumerate(applied):
            if isinstance(a, dict) and a.get("Name") and i < len(quals):
                g = quals[i]
                if isinstance(g, int) and not isinstance(g, bool) and 1 <= g <= 5:
                    out[str(a["Name"])] = g
        return out
    except (AttributeError, IndexError, KeyError, TypeError):
        return {}


def resolve_grades(augment_ids, item_stats, default_grade, preserve):
    """Per-slot grades for the write. With preserve=True (the reroll/swap path) an
    augment that is ALREADY installed keeps the grade it has; anything newly added
    takes default_grade. With preserve=False every slot takes default_grade."""
    if not preserve:
        return [default_grade for _ in augment_ids]
    have = installed_grades(item_stats)
    return [have.get(a, default_grade) for a in augment_ids]


def sql_jsonb_literal(obj):
    """json.dumps -> SQL '...'::jsonb literal with '' single-quote escaping."""
    return "'" + json.dumps(obj, separators=(",", ":")).replace("'", "''") + "'::jsonb"


# ---------------------------------------------------------------------------
# Write SQL (DO block; re-checks offline + ownership atomically)
# ---------------------------------------------------------------------------

def idempotency_ledger_ready():
    """True when dune.ls_augment_audit carries every column the player-path
    audit INSERT names (idempotency_key + the two rolls columns). Checked BEFORE
    any write so a missing migration fails closed with its own token rather than
    being swallowed: silently losing the key would let a retried swap consume a
    second augment."""
    out = _dq(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema='dune' AND table_name='ls_augment_audit' "
        "AND column_name IN ('idempotency_key', 'rolls_before', 'rolls_after');")
    # All three or refuse: the player-path audit INSERT names every one of these
    # columns and has no exception wrapper (it IS the idempotency claim), so a
    # partially-migrated table would abort the player's write mid-transaction
    # with a raw SQL error instead of this clean token.
    return out.strip() == "3"


def read_reroll_window(item_id):
    """(used, seconds_until_a_slot_frees) for player rerolls of this item inside
    the cap window. Counted straight off dune.ls_augment_audit, which already
    records op + item_id + created_at on every write, so the cap needs no new
    table and no new state to drift out of sync.

    Only rows carrying an idempotency_key count: those are the player-initiated
    ones. Operator/prize installs pass no key and must not burn a player's
    allowance. The wait is measured from the OLDEST row still inside the window,
    because that is the one that ages out first."""
    sql = (
        "SET search_path TO dune, public;\n"
        "SELECT json_build_object("
        "  'used', count(*),"
        "  'wait', COALESCE(CEIL(EXTRACT(EPOCH FROM ("
        f"     MIN(created_at) + interval '{int(REROLL_WINDOW_HOURS)} hours' - now()))), 0)"
        ") FROM dune.ls_augment_audit "
        f" WHERE item_id = {int(item_id)} AND op = 'reroll'"
        "   AND idempotency_key IS NOT NULL"
        f"   AND created_at > now() - interval '{int(REROLL_WINDOW_HOURS)} hours'"
    )
    out = _read_json(sql) or {}
    try:
        return int(out.get("used") or 0), max(0, int(out.get("wait") or 0))
    except (TypeError, ValueError):
        return 0, 0


def build_augment_sql(owner, item_id, template_id, is_weapon, keystone_ids, faug_value,
                      consume_ids=None, roll_mode=ROLL_MODE_PERFECT, reroll_only=None,
                      augment_ids_for_audit=None, operator=None, idempotency_key=None):
    faug_obj = {"FAugmentedItemStats": faug_value}
    faug_lit = sql_jsonb_literal(faug_obj)
    tmpl_lit = "'" + str(template_id).replace("'", "''") + "'"
    keystones_arr = "ARRAY[" + ",".join(str(int(k)) for k in keystone_ids) + "]::bigint[]"
    weapon_flag = "true" if is_weapon else "false"
    # Consume list: the augment ids being NEWLY installed (a pure reroll installs
    # nothing new and so consumes nothing). Ids are charset-validated upstream.
    consume_ids = list(consume_ids or [])
    consume_arr = ("ARRAY[" + ",".join("'" + a.replace("'", "''") + "'" for a in consume_ids)
                   + "]::text[]") if consume_ids else "ARRAY[]::text[]"
    # op classification for the audit row: what the player actually did.
    if consume_ids:
        op = "swap"
    elif reroll_only is not None or roll_mode == ROLL_MODE_RANDOM:
        op = "reroll"
    else:
        op = "install"
    op_lit = "'" + op + "'"
    roll_mode_lit = "'" + str(roll_mode or "").replace("'", "''") + "'"
    rerolled_lit = sql_jsonb_literal(sorted(reroll_only) if reroll_only is not None
                                     else list(augment_ids_for_audit or []))
    operator_lit = ("'" + str(operator).replace("'", "''") + "'") if operator else "NULL"
    idem_lit = (("'" + str(idempotency_key).replace("'", "''") + "'")
                if idempotency_key else "NULL")
    # Emitted ONLY when there is a key to check. Emitting `IF NULL IS NOT NULL`
    # for the operator path would be dead SQL sitting in a writer that edits
    # player gear, which is the last place to leave something that looks live
    # and is not.
    replay_block = f"""  -- 0. IDEMPOTENCY REPLAY. Checked FIRST, before the offline gate and before
  -- anything is read or locked: a retry of an already-honoured intent must be
  -- a no-op even if the player has since logged back in. Reports the item's
  -- CURRENT augment block so the caller shows reality, not this attempt's
  -- freshly-drawn rolls, which were never written. The unique partial index on
  -- idempotency_key is what stops two concurrent retries from both passing this
  -- check and both consuming an augment.
  SELECT id INTO v_prior FROM dune.ls_augment_audit
   WHERE idempotency_key = {idem_lit};
  IF v_prior IS NOT NULL THEN
    INSERT INTO _aug_result
    SELECT i.id, i.template_id, 0,
           COALESCE(jsonb_array_length(
             i.stats #> '{{FAugmentedItemStats,1,AppliedAugments}}'), 0),
           0, true, i.stats -> 'FAugmentedItemStats'
      FROM dune.items i WHERE i.id = {item_id};
    RETURN;
  END IF;
""" if idempotency_key else ""
    # AUTHORITATIVE reroll cap, inside the transaction. The Python pre-check
    # exists to build a useful message; THIS is what actually holds, because two
    # requests racing the pre-check would both pass it.
    #
    # Deliberately placed AFTER the replay guard: a retry of an already-counted
    # reroll must replay, not be refused for hitting the cap it itself set.
    # Only applies to player rerolls (an idempotency key, nothing consumed).
    cap_block = f"""  SELECT count(*) INTO v_recent FROM dune.ls_augment_audit
   WHERE item_id = {item_id} AND op = 'reroll' AND idempotency_key IS NOT NULL
     AND created_at > now() - interval '{int(REROLL_WINDOW_HOURS)} hours';
  IF v_recent >= {int(REROLL_CAP)} THEN
    RAISE EXCEPTION 'reroll_capped (% in the last {int(REROLL_WINDOW_HOURS)}h)', v_recent;
  END IF;
""" if (idempotency_key and not consume_ids) else ""
    # With a client key the audit INSERT is the idempotency claim, so it must be
    # able to ABORT the transaction. Without one it stays best-effort on a
    # missing table (the operator/prize paths must not fail on absent audit).
    if idempotency_key:
        audit_open, audit_close = "", ""
    else:
        audit_open = "  BEGIN\n  "
        audit_close = ("\n  EXCEPTION WHEN undefined_table THEN\n"
                       "    NULL;\n  END;")
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _aug_result;
CREATE TEMP TABLE _aug_result(
  item_id bigint, template_id text, keystones_inserted int,
  augment_count int, consumed int,
  -- replayed = this exact idempotency key was already honoured; NOTHING was
  -- written or consumed this time round. faug_after is the item's REAL
  -- augment block afterwards, so the caller reports what the item actually
  -- carries rather than what this attempt intended to write (on a replay
  -- those differ: the fresh random draw never landed).
  replayed boolean, faug_after jsonb
) ON COMMIT PRESERVE ROWS;
DO $augment$
DECLARE
  v_pawn bigint; v_status text; v_grace bool;
  v_inv bigint; v_tmpl text; v_stats jsonb;
  v_ks_ins int := 0;
  v_aug text; v_cons_id bigint; v_consumed int := 0;
  v_before jsonb; v_consumed_ids bigint[] := ARRAY[]::bigint[];
  v_prior bigint; v_recent int;
  v_before_cnt int; v_after_cnt int;
BEGIN
{replay_block}{cap_block}  -- 1. resolve the owner's pawn + OFFLINE-GATE (stats are RAM-backed while online)
  SELECT player_pawn_id, online_status,
         (reconnect_grace_period_end IS NOT NULL AND reconnect_grace_period_end > NOW())
    INTO v_pawn, v_status, v_grace
    FROM dune.encrypted_player_state
    WHERE player_controller_id = {owner}
      AND character_state IS DISTINCT FROM 'Deleted'
    ORDER BY player_controller_id DESC LIMIT 1;
  IF v_pawn IS NULL THEN RAISE EXCEPTION 'not_owner (no pawn for ctrl {owner})'; END IF;
  IF v_status IS DISTINCT FROM 'Offline' OR COALESCE(v_grace, true) THEN
    RAISE EXCEPTION 'player_online (status=% grace=%)', COALESCE(v_status,'?'), COALESCE(v_grace,true);
  END IF;
  -- 2. lock the item; verify the player OWNS it. PAWN-SIDE ONLY, same scope as
  -- the consume loop below and as read_item_plan(): backpack / worn / hotbar /
  -- CHOAM bank, all keyed off the pawn actor. Containers and vehicles are
  -- deliberately excluded -- their contents live in a live partition's RAM and
  -- our UPDATE there is retracted. This is the authoritative check; the
  -- Python-side plan read is advisory and runs outside this lock.
  SELECT i.inventory_id, i.template_id, i.stats
    INTO v_inv, v_tmpl, v_stats
    FROM dune.items i
    JOIN dune.inventories inv ON inv.id = i.inventory_id
    WHERE i.id = {item_id}
      AND inv.actor_id = v_pawn
    FOR UPDATE OF i;
  IF v_inv IS NULL THEN RAISE EXCEPTION 'not_owner (item {item_id} not owned by ctrl {owner})'; END IF;
  v_before := COALESCE(v_stats, '{{}}'::jsonb);  -- snapshot BEFORE any mutation, for the audit row
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
  -- 4b. CONSUME one standalone augment item per NEWLY installed augment, in this
  -- same transaction. Without this a swap is an item printer: the block-replace
  -- in step 5 would conjure an augment the player never earned. A pure reroll
  -- (same augments, new rolls) passes an empty array and consumes nothing.
  -- 🔴 Scope = PAWN-SIDE ONLY (backpack / worn / hotbar / CHOAM bank). Consuming
  -- out of a container or vehicle is an item DUPLICATION bug, not a nicety: the
  -- partition holding it is almost always alive, it resurrects our DELETE, and the
  -- player keeps the augment AND wears it. 335 of the 341 standalone augments on
  -- live currently sit in exactly those unsafe places, so this refuses most of
  -- them on purpose. Players carry the augment in game first, which is safe.
  FOREACH v_aug IN ARRAY {consume_arr} LOOP
    SELECT ci.id INTO v_cons_id
      FROM dune.items ci
      JOIN dune.inventories cinv ON cinv.id = ci.inventory_id
     WHERE cinv.actor_id = v_pawn
       AND ci.template_id = v_aug
       AND ci.stats ? 'FAugmentItemStats'
     ORDER BY ci.id
     LIMIT 1
     FOR UPDATE OF ci;
    IF v_cons_id IS NULL THEN
      RAISE EXCEPTION 'augment_not_owned (% not carried)', v_aug;
    END IF;
    -- standalone augments are never stacked (live: all 297 rows stack_size=1),
    -- so consuming one is a row delete, not a decrement
    DELETE FROM dune.items WHERE id = v_cons_id;
    v_consumed_ids := v_consumed_ids || v_cons_id;
    v_consumed := v_consumed + 1;
  END LOOP;
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
  -- 5b. BUG-019 INVARIANT (authoritative, atomic). A reroll/swap/install must
  -- NEVER reduce the augment-slot count -- the || above REPLACES the whole block,
  -- so a caller that sends a short `augments` list would silently delete the
  -- omitted slots (this destroyed two of a player's augments on 2026-08-06). v_before
  -- is the pre-mutation snapshot from step 2, read under this row's lock, so the
  -- check cannot be fooled by a stale/short read anywhere upstream. Any shrink
  -- RAISEs, which rolls back the ENTIRE transaction: nothing is written to the
  -- item and any augment consumed in step 4b is restored.
  v_before_cnt := COALESCE(jsonb_array_length(v_before #> '{{FAugmentedItemStats,1,AppliedAugments}}'), 0);
  v_after_cnt  := COALESCE(jsonb_array_length(v_stats  #> '{{FAugmentedItemStats,1,AppliedAugments}}'), 0);
  IF v_after_cnt < v_before_cnt THEN
    RAISE EXCEPTION 'augment_count_shrank (before=% after=% -- refusing to drop an augment slot)', v_before_cnt, v_after_cnt;
  END IF;
  -- 6. write; reset is_new so the game reloads the item's stats on next login
  UPDATE dune.items SET stats = v_stats, is_new = false WHERE id = {item_id} AND inventory_id = v_inv;
  -- 6b. AUDIT, in this same transaction so the row and its effect cannot disagree.
  -- v_before was captured at step 2, ahead of the overwrite. A random reroll can
  -- legitimately make a player's gear worse and a swap destroys an item they
  -- owned, so "what did it look like before" has to be answerable afterwards.
  -- Best-effort ONLY on the table being absent: if ls_augment_audit has not been
  -- created yet the write still succeeds rather than failing a player's action on
  -- a missing audit table. Any OTHER error propagates and rolls the write back.
{audit_open}INSERT INTO dune.ls_augment_audit (
      owner_ctrl, item_id, template_id, op, roll_mode,
      augments_before, augments_after, grades_after, rerolled,
      rolls_before, rolls_after,
      consumed_item_ids, operator, idempotency_key)
    VALUES (
      {owner}, {item_id}, v_tmpl, {op_lit}, {roll_mode_lit},
      COALESCE(v_before #> '{{FAugmentedItemStats,1,AppliedAugments}}', '[]'::jsonb),
      COALESCE({faug_lit} #> '{{FAugmentedItemStats,1,AppliedAugments}}', '[]'::jsonb),
      COALESCE({faug_lit} #> '{{FAugmentedItemStats,1,AppliedAugmentQualities}}', '[]'::jsonb),
      {rerolled_lit},
      -- The rolls ARE the payload of a reroll. Without them "did the values
      -- change" was unanswerable from stored data (player report 2026-08-03).
      -- On a swap, rolls_before is the only record of the destroyed slot.
      COALESCE(v_before #> '{{FAugmentedItemStats,1,AppliedAugmentRollData}}', '[]'::jsonb),
      COALESCE({faug_lit} #> '{{FAugmentedItemStats,1,AppliedAugmentRollData}}', '[]'::jsonb),
      to_jsonb(v_consumed_ids), {operator_lit}, {idem_lit});{audit_close}
  INSERT INTO _aug_result VALUES ({item_id}, v_tmpl, v_ks_ins,
    jsonb_array_length({faug_lit} #> '{{FAugmentedItemStats,1,AppliedAugments}}'),
    v_consumed, false, {faug_lit} -> 'FAugmentedItemStats');
END $augment$;
COMMIT;
SELECT json_build_object(
  'ok', true, 'action', 'augment',
  'item_id', item_id, 'template_id', template_id,
  'keystones_inserted', keystones_inserted, 'augment_count', augment_count,
  'consumed', consumed,
  'replayed', COALESCE(replayed, false), 'faug_after', faug_after
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

def do_dry_run(owner, item_id, augment_ids, grade, roll_mode=ROLL_MODE_PERFECT, consume=False,
               preserve_grades=False, reroll_only=None):
    plan = read_item_plan(owner, item_id)
    errs, item_kind = preflight(plan, augment_ids, grade)
    roll_shapes = read_roll_shapes(augment_ids) if augment_ids else {}
    keep = installed_rolls(plan.get("item_stats"))
    payloads = build_roll_payloads(augment_ids, grade, roll_shapes, roll_mode,
                                   keep, reroll_only)
    grades = resolve_grades(augment_ids, plan.get("item_stats"), grade, preserve_grades)
    faug = build_augmented_item_stats_value(augment_ids, grade, payloads, grades)
    kind_for_ks = item_kind if item_kind != "unknown" else "weapon"
    emit({
        "dry_run": True, "action": "augment", "augment_enabled": AUGMENT_ENABLED,
        "owner_ctrl": owner, "item_id": item_id,
        "item_template": plan.get("item_template"),
        "item_kind": item_kind, "item_quality": plan.get("item_quality"),
        "item_tags": item_tags(plan.get("item_template") or ""),
        "online_status": plan.get("online_status"), "in_grace": plan.get("in_grace"),
        "owned": plan.get("owned"),
        "augments": augment_ids, "grade": grade, "grades": grades,
        "preserve_grades": preserve_grades, "roll_mode": roll_mode,
        "rerolled": sorted(reroll_only) if reroll_only is not None else augment_ids,
        "consume": consume,
        "already_installed": installed_augment_ids(plan.get("item_stats")),
        "would_consume": newly_installed(augment_ids, plan.get("item_stats")) if consume else [],
        "roll_shapes_from_db": roll_shapes,
        "keystones_to_ensure": keystones_for(kind_for_ks, augment_ids),
        "faugmented_item_stats": faug,
        "preflight_errors": errs,
    }, 0)


def do_live(owner, item_id, augment_ids, grade, roll_mode=ROLL_MODE_PERFECT, consume=False,
            preserve_grades=False, reroll_only=None, operator=None, idempotency_key=None):
    if not AUGMENT_ENABLED:
        emit({"ok": False, "error": "augment_disabled"}, 0)
    # Fail CLOSED before touching anything: a caller that asked for idempotency
    # and cannot get it must not fall through to an unprotected write. A swap
    # destroys a rare item, so "we tried our best" is the wrong posture here.
    if idempotency_key and not idempotency_ledger_ready():
        fail("idempotency_unavailable", 1)
    plan = read_item_plan(owner, item_id)
    # Reroll allowance. Checked here purely so the refusal can say WHEN rather
    # than just no; the transaction re-checks it authoritatively. A swap is never
    # capped, and the operator path (no key) is never capped.
    is_player_reroll = bool(idempotency_key) and not consume
    if is_player_reroll:
        used, wait = read_reroll_window(item_id)
        if used >= REROLL_CAP:
            fail("reroll_capped", 1, used=used, cap=REROLL_CAP,
                 window_hours=REROLL_WINDOW_HOURS, retry_after_seconds=wait)
    errs, item_kind = preflight(plan, augment_ids, grade)
    if errs:
        fail(errs[0], 1)
    template_id = plan.get("item_template")
    is_weapon = item_kind == "weapon"
    keystone_ids = keystones_for(item_kind, augment_ids)
    roll_shapes = read_roll_shapes(augment_ids)
    keep = installed_rolls(plan.get("item_stats"))
    payloads = build_roll_payloads(augment_ids, grade, roll_shapes, roll_mode,
                                   keep, reroll_only)
    grades = resolve_grades(augment_ids, plan.get("item_stats"), grade, preserve_grades)
    faug = build_augmented_item_stats_value(augment_ids, grade, payloads, grades)
    # Consume only what is NEWLY installed. A reroll of the same augments takes
    # nothing from the player; a swap costs them the augment they swapped in.
    consume_ids = newly_installed(augment_ids, plan.get("item_stats")) if consume else []

    ns, pod = resolve_db_pod()
    sql = build_augment_sql(owner, item_id, template_id, is_weapon, keystone_ids, faug,
                            consume_ids, roll_mode, reroll_only, augment_ids, operator,
                            idempotency_key)
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
    # Report what the item ACTUALLY carries, read back out of the row we just
    # wrote, rather than what this attempt intended to write. The two are the
    # same on a normal write and DIFFERENT on an idempotent replay, where the
    # freshly-drawn rolls above were never persisted. Echoing intent there would
    # tell a player they rolled numbers their weapon does not have.
    replayed = bool(result.get("replayed"))
    after_ids, after_grades, after_rolls = block_slots(result.get("faug_after"))
    result["replayed"] = replayed
    result["grade"] = grade
    result["roll_mode"] = roll_mode
    result["augments"] = after_ids or augment_ids
    result["grades"] = after_grades or grades
    result["stat_rolls"] = after_rolls or [p["StatRolls"] for p in payloads]
    # Nothing was rerolled on a replay: the draw above never reached the item.
    result["rerolled"] = ([] if replayed else
                          (sorted(reroll_only) if reroll_only is not None else augment_ids))
    result.pop("faug_after", None)
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
        roll_mode = validate_roll_mode(payload.get("roll_mode", ROLL_MODE_PERFECT))
        consume = bool(payload.get("consume", False))
        preserve_grades = bool(payload.get("preserve_grades", False))
        _ro = payload.get("reroll_only")
        reroll_only = set(validate_augment_ids(_ro)) if isinstance(_ro, list) else None
        idempotency_key = validate_idem_key(payload.get("idempotency_key"))
    else:
        ap = argparse.ArgumentParser(description="Augment perfect-roll / swap writer (Phase 1)")
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--item-id", required=True)
        ap.add_argument("--augments", required=True,
                        help="comma-separated augment template ids (T6_Augment_*)")
        ap.add_argument("--grade", default="5")
        ap.add_argument("--roll-mode", default=ROLL_MODE_PERFECT, choices=list(ROLL_MODES),
                        help="perfect = every roll 1.0 (prize path); "
                             "random = a fresh uniform draw per roll slot (player reroll)")
        ap.add_argument("--consume", action="store_true",
                        help="require the player to OWN each newly installed augment "
                             "and destroy it in the same transaction (player-facing "
                             "swaps must set this or the tool prints items)")
        ap.add_argument("--preserve-grades", action="store_true",
                        help="keep each already-installed augment's own grade "
                             "(REQUIRED for reroll/swap: 34 live items run mixed "
                             "per-slot grades and one uniform grade silently "
                             "rewrites them)")
        ap.add_argument("--reroll-only", default=None,
                        help="comma-separated augment ids whose rolls to redraw; "
                             "every other slot keeps its CURRENT rolls. Omit for a "
                             "whole-item reroll.")
        ap.add_argument("--idempotency-key", default=None,
                        help="client key making a retry of the SAME intent a "
                             "no-op replay. REQUIRED on any player-facing swap: "
                             "without it a dropped response plus a retry "
                             "consumes a second augment.")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        owner = pos_int("owner_ctrl", a.owner_ctrl)
        item_id = pos_int("item_id", a.item_id)
        augments = [x for x in a.augments.split(",")]
        grade = validate_grade(a.grade)
        dry_run = a.dry_run
        roll_mode = validate_roll_mode(a.roll_mode)
        consume = a.consume
        preserve_grades = a.preserve_grades
        reroll_only = (set(validate_augment_ids(a.reroll_only.split(",")))
                       if a.reroll_only else None)
        idempotency_key = validate_idem_key(a.idempotency_key)

    augment_ids = validate_augment_ids(augments)
    if not augment_ids:
        fail("no_augments", 2)
    return (owner, item_id, augment_ids, grade, dry_run, roll_mode, consume,
            preserve_grades, reroll_only, idempotency_key)


def main():
    (owner, item_id, augment_ids, grade, dry_run, roll_mode, consume,
     preserve_grades, reroll_only, idempotency_key) = gather_args()
    if dry_run:
        do_dry_run(owner, item_id, augment_ids, grade, roll_mode, consume,
                   preserve_grades, reroll_only)
    else:
        do_live(owner, item_id, augment_ids, grade, roll_mode, consume,
                preserve_grades, reroll_only, None, idempotency_key)


if __name__ == "__main__":
    main()
