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
#
# 'transplant' is the swap path: the rolls written into a slot are the REAL rolls
# of the copy consumed to fill it, read and spliced inside the same transaction
# that locks and deletes that copy. Under 'random' a swap redrew the incoming
# augment's rolls, so a hand-picked roll was replaced by an average draw and a
# player could spend a rare augment to make their weapon WORSE (live audit rows
# 133/134: 0.263386 -> 0.252256). Transplant is what makes a swap carry what the
# player earned.
ROLL_MODE_PERFECT = "perfect"
ROLL_MODE_RANDOM = "random"
ROLL_MODE_TRANSPLANT = "transplant"
ROLL_MODES = (ROLL_MODE_PERFECT, ROLL_MODE_RANDOM, ROLL_MODE_TRANSPLANT)

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

_CATALOG_PATH = os.environ.get("DUNE_AUGMENT_CATALOG", "augment-compatibility.local.json")

DQ = "/root/dq.sh"
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# 🔴 ORDER IS LOAD-BEARING, and matching is FIRST-SUBSTRING-WINS. parse_psql_error
# walks this tuple in order and returns the first entry that appears anywhere in
# psql's stderr, so an entry placed ahead of a token it is a substring of would
# silently reroute that refusal to the wrong message. APPEND new tokens to the
# end; do not insert mid-tuple.
#
# 🔴 The near-miss, named so nobody disarms it by accident: `augment_disabled` and
# `augment_count_shrank` share an 8-character prefix with the augment_* tokens at
# the end, and are safe ONLY because a substring match needs the whole token.
# That is the exact shape of the bug this tuple already had -- `not_owner` looks
# like it covers `augment_not_owned` and does not ("not_own" + "ed" is not
# "not_own" + "er"), so nothing matched, parse_psql_error fell through to
# write_failed, and a player whose augment was not pawn-side was told "The change
# did not go through" instead of "You no longer own that augment." A careless
# rename here turns that from a near-miss back into a live defect.
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
    # The consume loop's own refusal: 'augment_not_owned (% G% not carried)'.
    # 478 of 484 standalone augments on live are not pawn-side, so this is the
    # most common refusal the writer emits, and until now it was the one nobody
    # could read -- see the prefix note above.
    "augment_not_owned",
    # transplant-path refusals. All three are FAIL-CLOSED: nothing is written and
    # nothing is consumed, because a RAISE rolls the whole DO block back.
    #
    # no_improvement -- a same-template, same-grade replacement where no copy the
    # player carries rolls higher than the one already fitted at any position.
    # This is the old portal-side 409 moved to where the rolls actually are.
    # augment_arrays_desynced -- AppliedAugments / AppliedAugmentQualities /
    # AppliedAugmentRollData disagree in length, or a transplant would write the
    # wrong number of rolls into a slot. We refuse rather than find out whether
    # the game tolerates it (unverified).
    # augment_roll_data_missing -- the source copy carries no readable StatRolls.
    # '[]'::jsonb and 'null'::jsonb are both NOT NULL, so an IS NULL guard passes
    # both, and an empty StatRolls renders an EMPTY AUGMENT SLOT in game.
    "no_improvement",
    "augment_arrays_desynced",
    "augment_roll_data_missing",
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
        auth_out = subprocess.run(
            ["sudo", "kubectl", "exec", "-n", ns, pod, "--",
             "sh", "-c", 'test -n "${POSTGRES_PASSWORD:-}"'],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout reading POSTGRES_PASSWORD", 3)
    if auth_out.returncode != 0:
        fail("no_pgpass", 3)
    cmd = ["sudo", "kubectl", "exec", "-i", "-n", ns, pod, "--",
           "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_",
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


def newly_installed_slots(augment_ids, item_stats, swap_slot=None):
    """SLOT INDICES this write installs into, and therefore must consume for.

    🔴 An augment's identity is (name, GRADE), not name. A player replacing their
    installed Damage1 G3 with a Damage1 G5 sends a list whose NAMES are unchanged,
    so the name difference below sees nothing new: the writer would consume nothing
    and print the player a free grade upgrade. `swap_slot` names the slot being
    replaced and is ALWAYS treated as installed, whatever its name.

    Indices rather than names because the caller has to pair each consumed item with
    the GRADE being written into that slot -- see build_augment_sql's consume loop."""
    have = set(installed_augment_ids(item_stats))
    return [i for i, a in enumerate(augment_ids) if i == swap_slot or a not in have]


def newly_installed(augment_ids, item_stats, swap_slot=None):
    """Requested ids that are not already installed, i.e. what must be consumed.
    Order-preserving; duplicates already removed by validate_augment_ids()."""
    return [augment_ids[i] for i in
            newly_installed_slots(augment_ids, item_stats, swap_slot)]


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


def validate_swap_slot(val, augment_ids):
    """Index into `augments` of the slot a swap replaces, or None for "infer it".

    Bounds-checked against the list it indexes and validated AFTER
    validate_augment_ids(), which drops blanks and de-dupes and so can shift
    positions. An out-of-range index would silently match no slot, consume
    nothing and hand out a free upgrade -- the exact failure this parameter
    exists to remove -- so it is a hard refusal, not a clamp."""
    if val is None:
        return None
    if isinstance(val, bool):
        fail("bad_swap_slot", 2)
    try:
        i = int(val)
    except (TypeError, ValueError):
        fail("bad_swap_slot", 2)
    if i < 0 or i >= len(augment_ids):
        fail("bad_swap_slot", 2)
    return i


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
      'transplant' -> the same fresh draw, but the value is a THROWAWAY PLACEHOLDER
                   of the correct SHAPE. build_augment_sql overwrites every
                   transplanted slot with the consumed copy's real rolls, under
                   the lock that deletes it. The placeholder must still exist and
                   still be the right length: the splice uses
                   jsonb_set(..., create_missing => false), which no-ops rather
                   than creating a slot, and the length is what the writer's own
                   shape assert compares against.

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
        if roll_mode in (ROLL_MODE_RANDOM, ROLL_MODE_TRANSPLANT):
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


def installed_grades_by_slot(item_stats):
    """Installed grades in SLOT ORDER, index-aligned with installed_augment_ids().

    installed_grades() above keys by NAME, which is what the preserve-a-reroll
    lookup wants but cannot answer "what grade is slot 1 carrying". The portal
    needs exactly that to tell a same-name/different-grade swap (a real change)
    apart from a no-op. None for a slot whose quality is missing or out of range,
    so a caller can tell "grade unknown" from "grade 1"."""
    try:
        block = (item_stats or {}).get("FAugmentedItemStats")
        applied = block[1].get("AppliedAugments") or []
        quals = block[1].get("AppliedAugmentQualities") or []
        out = []
        for i, a in enumerate(applied):
            if not (isinstance(a, dict) and a.get("Name")):
                continue
            g = quals[i] if i < len(quals) else None
            out.append(g if (isinstance(g, int) and not isinstance(g, bool)
                             and 1 <= g <= 5) else None)
        return out
    except (AttributeError, IndexError, KeyError, TypeError):
        return []


def resolve_grades(augment_ids, item_stats, default_grade, preserve, swap_slot=None):
    """Per-slot grades for the write. With preserve=True (the reroll/swap path) an
    augment that is ALREADY installed keeps the grade it has; anything newly added
    takes default_grade. With preserve=False every slot takes default_grade.

    🔴 `swap_slot` overrides the preserved grade for exactly the slot being replaced.
    The lookup below is by NAME, so on a same-name swap it finds the OUTGOING
    augment and hands back its grade -- silently discarding the grade the player
    just spent a rare item on. The slot being replaced always takes default_grade,
    which the portal has already proved the player owns."""
    if not preserve:
        return [default_grade for _ in augment_ids]
    have = installed_grades(item_stats)
    out = [have.get(a, default_grade) for a in augment_ids]
    if swap_slot is not None and 0 <= swap_slot < len(out):
        out[swap_slot] = default_grade
    return out


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
                      consume_specs=None, roll_mode=ROLL_MODE_PERFECT, reroll_only=None,
                      augment_ids_for_audit=None, operator=None, idempotency_key=None):
    faug_obj = {"FAugmentedItemStats": faug_value}
    faug_lit = sql_jsonb_literal(faug_obj)
    tmpl_lit = "'" + str(template_id).replace("'", "''") + "'"
    keystones_arr = "ARRAY[" + ",".join(str(int(k)) for k in keystone_ids) + "]::bigint[]"
    weapon_flag = "true" if is_weapon else "false"
    # Consume list: the augments being NEWLY installed, as (template, GRADE, SLOT)
    # triples (a pure reroll installs nothing new and so consumes nothing). Ids are
    # charset-validated upstream; grades are ints from resolve_grades(); SLOT is the
    # destination index from newly_installed_slots(), the one thing SQL cannot
    # derive for itself.
    #
    # 🔴 The GRADE is half the key, not decoration. Matching on template alone with
    # `ORDER BY ci.id LIMIT 1` deletes whichever copy happens to be OLDEST, and six
    # pawns on live carry the same augment at two grades right now (e.g. Damage1 at
    # G4 and G5). A player spending their G5 could lose their G4 instead -- silently,
    # permanently, and looking exactly like a successful swap. The grade paired here
    # is the one being WRITTEN into the slot, so the item destroyed is always exactly
    # the item installed.
    #
    # 🔴 No item id crosses any boundary, deliberately. One statement locks the
    # candidate, reads its rolls, applies the improvement guard and feeds the
    # DELETE, so "the row we judged is the row we locked is the row we deleted" is
    # a property of the statement rather than an agreement between two selections.
    transplant = (roll_mode == ROLL_MODE_TRANSPLANT)
    consume_specs = [(str(a), int(g), int(s)) for a, g, s in (consume_specs or [])]
    consume_arr = ("ARRAY[" + ",".join("'" + a.replace("'", "''") + "'"
                                       for a, _, _ in consume_specs)
                   + "]::text[]") if consume_specs else "ARRAY[]::text[]"
    consume_grade_arr = ("ARRAY[" + ",".join(str(g) for _, g, _ in consume_specs)
                         + "]::int[]") if consume_specs else "ARRAY[]::int[]"
    consume_slot_arr = ("ARRAY[" + ",".join(str(s) for _, _, s in consume_specs)
                        + "]::int[]") if consume_specs else "ARRAY[]::int[]"
    # op classification for the audit row: what the player actually did.
    if consume_specs:
        op = "swap"
    elif reroll_only is not None or roll_mode == ROLL_MODE_RANDOM:
        op = "reroll"
    else:
        op = "install"
    op_lit = "'" + op + "'"
    roll_mode_lit = "'" + str(roll_mode or "").replace("'", "''") + "'"
    # Nothing was rerolled on a TRANSPLANT: the draw in build_roll_payloads was a
    # throwaway placeholder and the splice below overwrote it with the consumed
    # copy's real rolls, so it never reached the item. The portal sets
    # reroll_only=[incoming] on every swap, so without this the audit row claims
    # a reroll that did not happen -- on every swap. ls_augment_audit is the
    # table this bug was PROVEN from, so it has to stay truthful about what
    # actually happened or the next investigation is blind. Same reasoning as
    # do_live's replay case; see the comment there, which names both.
    rerolled_lit = sql_jsonb_literal(
        [] if transplant else
        (sorted(reroll_only) if reroll_only is not None
         else list(augment_ids_for_audit or [])))
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
""" if (idempotency_key and not consume_specs) else ""
    # With a client key the audit INSERT is the idempotency claim, so it must be
    # able to ABORT the transaction. Without one it stays best-effort on a
    # missing table (the operator/prize paths must not fail on absent audit).
    if idempotency_key:
        audit_open, audit_close = "", ""
    else:
        audit_open = "  BEGIN\n  "
        audit_close = ("\n  EXCEPTION WHEN undefined_table THEN\n"
                       "    NULL;\n  END;")

    # ---------------------------------------------------------------------
    # TRANSPLANT-ONLY SQL: FOUR emissions, ONE condition. The improvement
    # conjunct in the WHERE, the preference in the ORDER BY, the per-slot
    # accumulation, and the splice loop. Nothing else is gated, and these four
    # are never gated separately.
    #
    # 🔴 That is the whole kill-switch. With the portal flag off it dispatches
    # roll_mode='random', none of this is emitted, and a swap behaves exactly as
    # it does today -- the revert is structural, not a label on an audit row.
    # It is also this file's own standard (see replay_block above): dead SQL in a
    # writer that edits player gear is the last place to leave something that
    # looks live and is not. Do not "harmlessly" emit the accumulation without
    # the splice; that leaves the loaded gun next to the trigger.
    #
    # 🔴 These are PLAIN strings, NOT f-strings, so their jsonb path literals
    # carry SINGLE braces. A value substituted into an f-string is not formatted
    # again. Anything written directly into the return's f-string below must
    # double them ('{{FAugmentItemStats,1,StatRolls}}'). If you convert one of
    # these to an f-string, double every brace in it or it will KeyError at
    # build time -- or worse, render '{{' and fail only at SQL parse time.
    # ---------------------------------------------------------------------

    # (c) the slot being REPLACED, read out of v_before -- the pre-mutation
    # snapshot taken under this item's OWN FOR UPDATE at step 2, so the
    # comparison cannot be fooled by a stale read anywhere upstream.
    #
    # 🔴 v_slot indexes the OUTGOING augment list, so on a fresh install v_before
    # has nothing at that index. That is NORMAL and must NOT raise: a new slot has
    # no predecessor to improve on. Bounding v_slot against v_before here would
    # reject every legitimate install, including the operator prize path that is
    # live today. The upper bound belongs in the splice loop, against the block
    # actually being written.
    #   name absent, rolls absent  -> newly added slot, no guard, no RAISE
    #   name present, rolls absent -> pre-existing array desync, RAISE
    #   both present               -> normal replacement, evaluate the guard
    #
    # Not gated on transplant: only the four emissions listed above are. This
    # block just reads and classifies, and its RAISE can only fire on an item
    # that is already corrupt.
    fitted_block = """    v_inst_name := v_before #>> ARRAY['FAugmentedItemStats','1','AppliedAugments', v_slot::text, 'Name'];
    v_inst_grade := (v_before #>> ARRAY['FAugmentedItemStats','1','AppliedAugmentQualities', v_slot::text])::int;
    v_inst_rolls := v_before #> ARRAY['FAugmentedItemStats','1','AppliedAugmentRollData', v_slot::text, 'StatRolls'];
    -- Also rejects a fitted slot whose StatRolls is a SCALAR rather than an array.
    -- That is not pedantry: past this point v_inst_rolls is passed to
    -- jsonb_array_length and jsonb_array_elements_text, both of which ERROR on a
    -- scalar, and the caller would see write_failed instead of a real token.
    IF v_inst_name IS NOT NULL
       AND (v_inst_rolls IS NULL OR jsonb_typeof(v_inst_rolls) <> 'array') THEN
      RAISE EXCEPTION 'augment_arrays_desynced (fitted slot desync)';
    END IF;
    -- The guard applies ONLY to a same-template, same-grade replacement. Roll
    -- order is the game's asset order and roll counts differ per template
    -- (Spitdart 7, Melee6 2), so comparing across templates or grades compares
    -- unrelated numbers.
    v_same_upgrade := (v_inst_name IS NOT NULL
                       AND v_inst_name = v_aug
                       AND v_inst_grade IS NOT DISTINCT FROM v_cons_grade[v_ci]);
"""

    # (d) THE IMPROVEMENT GUARD, as a WHERE conjunct of the locking SELECT rather
    # than a check after it. A post-check on an arbitrarily-chosen copy tells a
    # player "nothing you own beats it" while they are carrying something that
    # does; a filter cannot. It also means a non-improving copy is unselectable,
    # so the same-grade path can never destroy the wrong one.
    #
    # 🔴 THE RULE IS ANY-BETTER: one position strictly better admits the copy.
    # There is NO not-worse conjunct here and NONE MAY BE ADDED. Adding one
    # silently converts the rule to DOMINANCE, which refuses 16% of all genuine
    # improvements and 77-95% of them on 4+ roll augments -- measured on live --
    # because 86 of 105 catalogued augments mix rolled and FLAT effects, and a
    # flat effect still occupies a roll position the game ignores. A not-worse
    # expression DOES appear in this statement, in the ORDER BY, where it is a
    # PREFERENCE. Do not move it up here.
    #
    # 🔴 EPSILON. `::numeric` casts to exact decimal, so the comparison itself
    # carries no drift; the epsilon absorbs noise in the STORED float32 value,
    # which does not round-trip (0.178757 reads back 0.178755). Under a bare `>`
    # that ~2e-6 of storage noise counts as "strictly better" and an identical
    # copy is destroyed for nothing -- the exact no-op this guard exists to stop.
    # The boundary is EXCLUSIVE on purpose: with rolls stored at 6 decimals the
    # ten smallest increments above the fitted value are rejected, which on a G5
    # Blade Sharpener (48 + roll*22) is 0.00022 percentage points. Nobody can
    # perceive that, and "0.600010 does not beat 0.600000" is not a bug. Do not
    # loosen it to `>=`. The epsilon is per-element and INDEPENDENT by design: a
    # copy that is -2e-6 on one roll and +2e-5 on another qualifies, and that is
    # correct -- do not "fix" it into an all-or-nothing comparison.
    #
    # 🔴 Roll counts must match EXACTLY. Two independent reasons, and BOTH must
    # survive any future change to the comparison rule:
    #   1. COMPARISON. `USING (ord)` joins on ordinality, so a shorter candidate
    #      leaves the surplus positions unmatched and a longer one dereferences to
    #      SQL NULL. Either way the extra positions are silently ignored -- both
    #      directions demonstrated by execution. Never relax `=` to `>=` or `<=`.
    #   2. WRITE SHAPE. The transplant WRITES this array into the slot, so a
    #      length mismatch silently reshapes it. Per build_roll_payloads'
    #      docstring a wrong roll count RENDERS AN EMPTY AUGMENT SLOT IN GAME.
    # Reason 2 holds even if the rule changes to one where reason 1 does not apply.
    #
    # 🔴 ONE CASE, not a chain of ANDs, and not a style choice. SQL does NOT
    # short-circuit AND or OR -- PostgreSQL may evaluate either operand first --
    # while jsonb_array_length and jsonb_array_elements_text both ERROR on a
    # scalar instead of returning NULL. So a candidate whose StatRolls is a JSON
    # `null` or a bare number aborts the whole statement before any typeof guard
    # can exclude it, and a player carrying one good copy and one corrupt sibling
    # gets write_failed. A CASE evaluates its WHEN arms in order and stops at the
    # first match, so the type check genuinely gates every call beneath it; the
    # arms are non-constant, so the planner cannot fold them out of order.
    #
    # 🔴 The shape test and the improvement test are ONE CASE on purpose. Split
    # into two sibling AND conjuncts -- even with the shape half CASE-guarded --
    # the improvement half calls jsonb_array_length on the same unvalidated value
    # and is protected only by conjunct ORDERING, which is a planner decision.
    # Both halves are transplant-only, so merging them costs nothing.
    #
    # Proven, not assumed (postgres 16 and 17, 2026-08-21, and pinned as a
    # permanent case in test_augment_pg.sh): the plain conjunct chain raised
    # `cannot get array length of a scalar`. A chain that merely replaced the
    # length call survived only because the planner sorted the expensive OR last
    # -- correct by optimiser behaviour, which is what this file declines to
    # depend on. This form is correct by construction.
    guard_conjunct = """       AND CASE
             WHEN jsonb_typeof(ci.stats #> '{FAugmentItemStats,1,StatRolls}') <> 'array'
               THEN false
             WHEN jsonb_array_length(ci.stats #> '{FAugmentItemStats,1,StatRolls}') < 1
               THEN false
             WHEN NOT v_same_upgrade
               THEN true
             WHEN jsonb_array_length(ci.stats #> '{FAugmentItemStats,1,StatRolls}')
                    <> jsonb_array_length(v_inst_rolls)
               THEN false
             ELSE EXISTS (
               SELECT 1
                 FROM jsonb_array_elements_text(
                        ci.stats #> '{FAugmentItemStats,1,StatRolls}') WITH ORDINALITY c(val, ord)
                 JOIN jsonb_array_elements_text(v_inst_rolls) WITH ORDINALITY f(val, ord) USING (ord)
                WHERE c.val::numeric > f.val::numeric + 0.00001)
           END
""" if transplant else ""

    # The ORDER BY. `random`/`perfect` emit the byte-identical bare `ci.id`, so
    # ops/augment-swap-live-verify.sh's lowest-id assertions stay valid BY
    # CONSTRUCTION rather than by optimiser behaviour.
    #
    # Inside transplant mode the preference is gated AGAIN, at runtime, on
    # v_same_upgrade. Any-better admits mixed copies, so among several qualifying
    # copies a plain lowest-id pick can spend one that is a net downgrade
    # (fitted [0.50,0.90], X id100 [0.51,0.05], Y id200 [0.99,0.99] -> X wins and
    # position 1 collapses 0.90 -> 0.05). Sorting "nothing is worse" first makes
    # a dominating copy always beat a mixed one. It aggregates nothing and
    # compares no magnitudes across positions, so it is not a scalar score: it
    # can only ever refuse to put a dominated copy above a dominating one.
    #
    # The runtime CASE matters because on a fresh install v_inst_rolls is NULL and
    # on a cross-augment swap it is the OUTGOING, unrelated augment's rolls --
    # different template, usually a different roll count -- so USING (ord) would
    # compare noise and silently drop the lowest-id property that T9/T28/T34 and
    # ops tooling rely on. Where there is no meaningful predecessor every
    # candidate sorts true, ties, and falls through to ci.id.
    #
    # jsonb_array_elements_text is STRICT: NULL input yields ZERO ROWS rather than
    # erroring, which is what makes the install path safe here AND in the WHERE
    # above. 🔴 SQL `OR` is not guaranteed to short-circuit, so that NULL-safety
    # is not something the `NOT v_same_upgrade OR ...` shape merely hides -- it is
    # what stops the install path erroring at all. If T27 raises, look here first.
    order_by_clause = """(CASE WHEN v_same_upgrade
                THEN NOT EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements_text(
                                ci.stats #> '{FAugmentItemStats,1,StatRolls}') WITH ORDINALITY c(val, ord)
                         JOIN jsonb_array_elements_text(v_inst_rolls) WITH ORDINALITY f(val, ord) USING (ord)
                        WHERE c.val::numeric < f.val::numeric - 0.00001)
                ELSE true END) DESC,
          ci.id""" if transplant else "ci.id"

    # (f, second half) HOLE C.
    #
    # 🔴 A MISSING AppliedEffectIndices IS THE NORMAL CASE, NOT A DEFECT.
    # Measured on live 2026-08-21, read-only:
    #   standalone augments carrying the key ....   0 of 482
    #   weapon slots carrying the key .......... 690 of 690
    # The four key-sets that actually occur on the standalone side are
    # {StatRolls} x269, {RollBias,StatRolls} x111, {AugmentItemHash,StatRolls}
    # x75 and {AugmentItemHash,RollBias,StatRolls} x27 -- none of them contain
    # it. AppliedEffectIndices is a WEAPON-SIDE field: it describes a fitted
    # slot, not a loose augment.
    #
    # So the read COALESCEs to '[]', exactly as read_roll_shapes does on every
    # other path, and a missing key is NOT refused. Refusing it would have
    # rejected 100% of live transplants the day the flag went on, telling every
    # player their augment "carries no effect indices" -- which is true of every
    # augment in the game. Do not reinstate that as hardening.
    #
    # What remains refused is a key that is PRESENT and not an array, which is a
    # genuine corruption signal rather than the ordinary shape.
    #
    # 🔴 Transplant-only, deliberately. v_cons_idx is only ever WRITTEN on this
    # path; under random/perfect the writer supplies its own indices from
    # read_roll_shapes and never reads this value.
    # 🔴 SCOPE LIMIT ON THE CASE RULE -- read this before "tidying" the guard
    # below to match the one above it.
    #
    # The rule is: wherever jsonb_typeof GUARDS jsonb_array_length, use a CASE.
    # There is no length call here, so the rule does not reach this guard and
    # converting it would be ceremony. And the obvious way to make it "match" its
    # neighbour -- adding a length test -- would refuse an EMPTY array, which is
    # what the COALESCE above produces for all 482 live augments. That single
    # edit would refuse every swap in the game.
    #
    # This test is TOTAL and needs no ordering guarantee: jsonb_typeof is strict,
    # so it yields NULL rather than raising, and no branch can error. Verified on
    # postgres:17-alpine across every input shape, on the pre-COALESCE value:
    #   SQL NULL -> (now impossible)   'null' -> refuse   0.5 -> refuse
    #   '"x"'    -> refuse             '[]'   -> ACCEPT   '[1]' -> ACCEPT
    idx_shape_block = """    IF jsonb_typeof(v_cons_idx) <> 'array' THEN
      RAISE EXCEPTION 'augment_roll_data_missing (the consumed copy carries malformed effect indices)';
    END IF;
""" if transplant else ""

    # (f, first half) HOLES A/B, transplant-only for the same reason as hole C:
    # v_cons_rolls is only ever WRITTEN on this path. Under random/perfect the
    # source is destroyed and the slot redrawn, so its rolls are irrelevant and
    # refusing on them would make an augment that is swappable today unswappable
    # -- inside a change that is supposed to be inert when the flag is off.
    # (Whether a malformed augment should be swappable at all in random mode is a
    # fair question and a separate ticket; it narrows a live path.)
    #
    # 🔴 CASE for the same reason as the WHERE: a plpgsql IF condition is
    # evaluated as one SQL expression, so on a jsonb-null v_cons_rolls the
    # IS NULL test is false, the typeof test is true, and the length test still
    # ERRORS. Unlike the WHERE site this one is not demonstrated -- it rests on
    # PostgreSQL documenting subexpression evaluation order as undefined and
    # naming CASE as the way to force it. Same idiom at all three sites so the
    # next author gets it right at whichever one they touch.
    # 🔴 The CASE is PARENTHESISED, and it must be. plpgsql reads an IF condition
    # up to the first `THEN` at paren-depth zero -- so a bare CASE, whose own
    # arms carry `THEN`, truncates the condition and the block dies with
    # `syntax error at end of input`. Caught by executing it; nothing static
    # sees it, because the SQL is well-formed as a fragment. This is the only
    # place the CASE idiom lands inside a plpgsql IF rather than inside SQL.
    rolls_shape_block = """    IF v_cons_rolls IS NULL
       OR (CASE WHEN jsonb_typeof(v_cons_rolls) <> 'array' THEN true
                ELSE jsonb_array_length(v_cons_rolls) = 0 END) THEN
      RAISE EXCEPTION 'augment_roll_data_missing (the consumed copy carries no stat rolls)';
    END IF;
""" if transplant else ""

    # (c) The usable-copy count. Transplant-only: in other modes every owned copy
    # is usable by definition, so usable == total and the ladder's second branch
    # is unreachable. Emitting count(*) twice rather than a separate assignment
    # keeps the two builds the same shape.
    usable_count_expr = ("""count(*) FILTER (WHERE CASE
             WHEN jsonb_typeof(ci.stats #> '{FAugmentItemStats,1,StatRolls}') <> 'array'
               THEN false
             ELSE jsonb_array_length(ci.stats #> '{FAugmentItemStats,1,StatRolls}') >= 1
           END)""" if transplant else "count(*)")

    # 🔴 Per-slot ACCUMULATION, not a scalar. The consume loop genuinely runs more
    # than once (a CLI three-augment install returns slots [0,1,2]), so a scalar
    # would hold only the last iteration and every earlier slot would keep
    # build_roll_payloads' throwaway placeholder -- the original bug, in the code
    # written to fix it.
    accumulate_block = """    v_cons_rolls_arr := v_cons_rolls_arr || v_cons_rolls;
    v_cons_idx_arr := v_cons_idx_arr || v_cons_idx;
    v_cons_slot_used := v_cons_slot_used || v_slot;
""" if transplant else ""

    # THE SPLICE. Runs after the `|| faug_lit` block-replace, so it overwrites
    # build_roll_payloads' placeholder in every slot this write consumed for.
    #
    # 🔴 HOLE D, correctly bounded: against the AppliedAugmentRollData being
    # WRITTEN, not against v_before. Proven by execution before this assert
    # existed: swap_slot=5 on a 2-slot item was ACCEPTED and COMMITTED, the source
    # consumed, the arrays left 2/2/3, and the player's real roll landed on a
    # phantom index while slot 0 kept the placeholder. create_missing => false is
    # NOT the fix on its own -- it silently no-ops and leaves the placeholder,
    # reproducing the original bug while reporting success. The RAISE is what is
    # load-bearing.
    #
    # The roll-count assert covers the paths v_same_upgrade's equal-length
    # conjunct cannot reach -- cross-augment and cross-grade transplants also
    # write the candidate's array into the slot. Their placeholder length comes
    # from read_roll_shapes, which reads the newest row of that template anywhere
    # on the server, while the candidate is this player's specific copy. Those
    # agree only because roll count is constant per template, which is an
    # EMPIRICAL invariant measured on 87 of 105 catalogued templates. Fail closed.
    splice_block = """  FOR v_si IN 1 .. COALESCE(array_length(v_cons_slot_used, 1), 0) LOOP
    IF v_cons_slot_used[v_si] >= COALESCE(jsonb_array_length(
         v_stats #> '{FAugmentedItemStats,1,AppliedAugmentRollData}'), 0) THEN
      RAISE EXCEPTION 'augment_arrays_desynced (transplant slot out of range)';
    END IF;
    IF jsonb_array_length(v_cons_rolls_arr[v_si]) IS DISTINCT FROM
       COALESCE(jsonb_array_length(v_stats #> ARRAY['FAugmentedItemStats','1',
                 'AppliedAugmentRollData', v_cons_slot_used[v_si]::text, 'StatRolls']), -1) THEN
      RAISE EXCEPTION 'augment_arrays_desynced (transplanted roll count does not match the slot)';
    END IF;
    -- 🔴 Every roll must be a NUMBER. jsonb_array_elements_text yields SQL NULL
    -- for a JSON null element and NULL is never TRUE, so a null position simply
    -- cannot be "better" and the COMPARISON degrades safe. The WRITE does not:
    -- without this, a candidate like [0.9, null] is admitted on the strength of
    -- position 0 and the null is transplanted into the player's item. The
    -- hole-A/B checks test the array, not its elements.
    -- Same stage and same argument as the roll-count assert directly above: this
    -- guards the WRITE SHAPE. The admission predicate is deliberately untouched --
    -- adding an element-type conjunct there would change which copies qualify,
    -- which is a rule change, not a safety check.
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_cons_rolls_arr[v_si]) e
                WHERE jsonb_typeof(e) <> 'number') THEN
      RAISE EXCEPTION 'augment_arrays_desynced (transplanted rolls contain a non-numeric value)';
    END IF;
    v_stats := jsonb_set(v_stats,
      ARRAY['FAugmentedItemStats','1','AppliedAugmentRollData', v_cons_slot_used[v_si]::text],
      jsonb_build_object('StatRolls', v_cons_rolls_arr[v_si],
                         'AppliedEffectIndices', v_cons_idx_arr[v_si]),
      false);
  END LOOP;
""" if transplant else ""

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
  v_cons_tpl text[] := {consume_arr}; v_cons_grade int[] := {consume_grade_arr};
  v_cons_slot int[] := {consume_slot_arr};
  v_ci int; v_slot int; v_owned_cnt int; v_owned_usable_cnt int;
  v_cons_rolls jsonb; v_cons_idx jsonb;
  v_inst_rolls jsonb; v_inst_name text; v_inst_grade int; v_same_upgrade boolean;
  v_cons_rolls_arr jsonb[] := ARRAY[]::jsonb[];
  v_cons_idx_arr jsonb[] := ARRAY[]::jsonb[];
  v_cons_slot_used int[] := ARRAY[]::int[];
  v_si int;
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
  -- Indexed rather than FOREACH: each consumed item is matched on its GRADE as
  -- well as its template, and the three arrays are index-aligned triples.
  --
  -- 🔴 plpgsql returns NULL for an out-of-range subscript rather than erroring,
  -- so a short array does not fail, it silently degrades: a missing grade would
  -- match no copy and a missing slot would transplant nowhere. Assert the three
  -- lengths agree before the loop can read any of them.
  IF COALESCE(array_length(v_cons_grade, 1), 0)
       IS DISTINCT FROM COALESCE(array_length(v_cons_tpl, 1), 0)
     OR COALESCE(array_length(v_cons_slot, 1), 0)
       IS DISTINCT FROM COALESCE(array_length(v_cons_tpl, 1), 0) THEN
    RAISE EXCEPTION 'write_failed (consume arrays desynced)';
  END IF;
  FOR v_ci IN 1 .. COALESCE(array_length(v_cons_tpl, 1), 0) LOOP
    v_aug := v_cons_tpl[v_ci];
    v_slot := v_cons_slot[v_ci];
    -- HOLE D, cheap half. A NULL or negative destination slot is nonsense in any
    -- mode. The UPPER bound cannot be checked here -- v_slot indexes the block
    -- being written, which does not exist yet -- so it lives in the splice loop.
    IF v_slot IS NULL OR v_slot < 0 THEN
      RAISE EXCEPTION 'augment_arrays_desynced (transplant slot out of range)';
    END IF;
{fitted_block}    -- Unfiltered count on the same pawn/template/grade scope, taken BEFORE the
    -- filtered SELECT so a refusal can say WHICH kind of no it is: "you are not
    -- carrying one" and "you are carrying one but it is not better" are different
    -- conversations with the player. Deliberately NOT locking -- both branches
    -- RAISE and roll the transaction back, so this only chooses wording.
    SELECT count(*), {usable_count_expr}
      INTO v_owned_cnt, v_owned_usable_cnt
      FROM dune.items ci
      JOIN dune.inventories cinv ON cinv.id = ci.inventory_id
     WHERE cinv.actor_id = v_pawn
       AND ci.template_id = v_aug
       AND ci.quality_level = v_cons_grade[v_ci]
       AND ci.stats ? 'FAugmentItemStats';
    -- The rolls are read from the row this statement LOCKS and then DELETEs, so
    -- the row we judged, locked and destroyed cannot come apart. Under READ
    -- COMMITTED a row that unblocks is re-checked against this WHERE, so a copy
    -- a racing transaction already spent stops qualifying instead of being spent
    -- twice. It does NOT fall through to the next copy (ORDER BY/LIMIT are not
    -- re-evaluated) -- that refusal is intended, not a bug to build a retry
    -- loop around.
    SELECT ci.id,
           ci.stats #> '{{FAugmentItemStats,1,StatRolls}}',
           -- COALESCE because standalone augments do NOT carry this key: 0 of 482
           -- on live, against 690 of 690 weapon slots. Same default
           -- read_roll_shapes applies on every other path. Without it the guard
           -- below refuses every swap in the game.
           COALESCE(ci.stats #> '{{FAugmentItemStats,1,AppliedEffectIndices}}', '[]'::jsonb)
      INTO v_cons_id, v_cons_rolls, v_cons_idx
      FROM dune.items ci
      JOIN dune.inventories cinv ON cinv.id = ci.inventory_id
     WHERE cinv.actor_id = v_pawn
       AND ci.template_id = v_aug
       AND ci.quality_level = v_cons_grade[v_ci]
       AND ci.stats ? 'FAugmentItemStats'
{guard_conjunct}     ORDER BY {order_by_clause}
     LIMIT 1
     FOR UPDATE OF ci;
    -- 🔴 FOUR branches, because "nothing qualified" has three distinct causes and
    -- every branch has to be a TRUE statement. Two branches cannot express the
    -- inert-copy case: it would either tell a player carrying one that they are
    -- not carrying one, or reclassify corrupt augment data as "nothing better
    -- available" -- and that second one matters operationally, because
    -- augment_roll_data_missing is how we LEARN that our own grant path minted
    -- an inert augment again (ac2adb9 exists because it did once).
    -- Branch 2 is unreachable when the transplant is not emitted (usable = total).
    -- Branch 4 is unreachable by construction: it fails closed with a distinct
    -- token rather than guessing a plausible-sounding reason.
    IF v_cons_id IS NULL THEN
      IF v_owned_cnt = 0 THEN
        RAISE EXCEPTION 'augment_not_owned (% G% not carried)', v_aug, v_cons_grade[v_ci];
      ELSIF v_owned_usable_cnt = 0 THEN
        RAISE EXCEPTION 'augment_roll_data_missing (the copy carried has no stat rolls)';
      ELSIF v_same_upgrade THEN
        RAISE EXCEPTION 'no_improvement (nothing carried rolls higher than the fitted one)';
      ELSE
        RAISE EXCEPTION 'write_failed (usable copy exists but none selected)';
      END IF;
    END IF;
    -- HOLES A/B. '[]'::jsonb IS NULL and 'null'::jsonb IS NULL are BOTH FALSE, so
    -- an IS NULL guard passes both and COALESCE cannot help. Proven by execution:
    -- both were ACCEPTED, the source CONSUMED, and the slot written
    -- {{"StatRolls":[]}} / {{"StatRolls":null}} -- the player loses the augment and
    -- gets an inert slot. Redundant with the WHERE exclusion above BY DESIGN:
    -- they guard different stages, and this one still stands if the WHERE is
    -- ever relaxed.
{rolls_shape_block}{idx_shape_block}{accumulate_block}    -- standalone augments are never stacked (live: all 297 rows stack_size=1),
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
{splice_block}  -- 5b. BUG-019 INVARIANT (authoritative, atomic). A reroll/swap/install must
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
  -- 5c. THE THREE PARALLEL ARRAYS MUST STAY INDEX-ALIGNED. There was no length
  -- assertion anywhere in this writer, and block_slots() truncates to the
  -- SHORTEST array on read-back, so a desync was invisible in the response as
  -- well as in the DB. RAISE rather than repair: whether the game tolerates
  -- desynced arrays is UNVERIFIED, and jsonb_set with create_missing => true
  -- APPENDS on an out-of-range index instead of erroring. Unconditional by
  -- design -- it can only fire on a state that is already broken.
  IF jsonb_array_length(v_stats #> '{{FAugmentedItemStats,1,AppliedAugments}}')
       IS DISTINCT FROM jsonb_array_length(v_stats #> '{{FAugmentedItemStats,1,AppliedAugmentQualities}}')
     OR jsonb_array_length(v_stats #> '{{FAugmentedItemStats,1,AppliedAugments}}')
       IS DISTINCT FROM jsonb_array_length(v_stats #> '{{FAugmentedItemStats,1,AppliedAugmentRollData}}') THEN
    RAISE EXCEPTION 'augment_arrays_desynced (AppliedAugments / Qualities / RollData lengths disagree)';
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
      -- 🔴 v_stats, NOT the faug_lit literal. faug_lit is baked in Python at build
      -- time and still carries build_roll_payloads' throwaway placeholder; v_stats
      -- is what the UPDATE above actually wrote. Reading the literal here would
      -- make the audit row and the API response report rolls the item does not
      -- have -- the same silent divergence this whole change exists to kill.
      COALESCE(v_stats #> '{{FAugmentedItemStats,1,AppliedAugments}}', '[]'::jsonb),
      COALESCE(v_stats #> '{{FAugmentedItemStats,1,AppliedAugmentQualities}}', '[]'::jsonb),
      {rerolled_lit},
      -- The rolls ARE the payload of a reroll. Without them "did the values
      -- change" was unanswerable from stored data (player report 2026-08-03).
      -- On a swap, rolls_before is the only record of the destroyed slot.
      COALESCE(v_before #> '{{FAugmentedItemStats,1,AppliedAugmentRollData}}', '[]'::jsonb),
      COALESCE(v_stats #> '{{FAugmentedItemStats,1,AppliedAugmentRollData}}', '[]'::jsonb),
      to_jsonb(v_consumed_ids), {operator_lit}, {idem_lit});{audit_close}
  INSERT INTO _aug_result VALUES ({item_id}, v_tmpl, v_ks_ins,
    jsonb_array_length(v_stats #> '{{FAugmentedItemStats,1,AppliedAugments}}'),
    v_consumed, false, v_stats -> 'FAugmentedItemStats');
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
               preserve_grades=False, reroll_only=None, swap_slot=None):
    plan = read_item_plan(owner, item_id)
    errs, item_kind = preflight(plan, augment_ids, grade)
    roll_shapes = read_roll_shapes(augment_ids) if augment_ids else {}
    keep = installed_rolls(plan.get("item_stats"))
    payloads = build_roll_payloads(augment_ids, grade, roll_shapes, roll_mode,
                                   keep, reroll_only)
    grades = resolve_grades(augment_ids, plan.get("item_stats"), grade, preserve_grades,
                            swap_slot)
    faug = build_augmented_item_stats_value(augment_ids, grade, payloads, grades)
    kind_for_ks = item_kind if item_kind != "unknown" else "weapon"
    # Same pairing the live write will consume on, so a probe can be checked
    # against what actually gets destroyed rather than just which name.
    cons_slots = (newly_installed_slots(augment_ids, plan.get("item_stats"), swap_slot)
                  if consume else [])
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
        # Grades in SLOT ORDER, index-aligned with already_installed. The portal
        # cannot otherwise tell a same-name/different-grade swap (a real change)
        # from a request that changes nothing.
        "already_installed_grades": installed_grades_by_slot(plan.get("item_stats")),
        "swap_slot": swap_slot,
        "would_consume": [augment_ids[i] for i in cons_slots],
        "would_consume_grades": [grades[i] for i in cons_slots],
        "roll_shapes_from_db": roll_shapes,
        "keystones_to_ensure": keystones_for(kind_for_ks, augment_ids),
        "faugmented_item_stats": faug,
        "preflight_errors": errs,
    }, 0)


def do_live(owner, item_id, augment_ids, grade, roll_mode=ROLL_MODE_PERFECT, consume=False,
            preserve_grades=False, reroll_only=None, operator=None, idempotency_key=None,
            swap_slot=None):
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
    grades = resolve_grades(augment_ids, plan.get("item_stats"), grade, preserve_grades,
                            swap_slot)
    faug = build_augmented_item_stats_value(augment_ids, grade, payloads, grades)
    # Consume only what is NEWLY installed. A reroll of the same augments takes
    # nothing from the player; a swap costs them the augment they swapped in.
    # Paired with the grade being WRITTEN into that slot, so what is destroyed is
    # always exactly what is installed -- see build_augment_sql's consume loop.
    # The slot index rides along because it is the one thing the SQL cannot
    # derive: it is what the transplant splices the consumed copy's rolls into.
    cons_slots = (newly_installed_slots(augment_ids, plan.get("item_stats"), swap_slot)
                  if consume else [])
    # Transplant has nothing to transplant FROM without a consumed copy. Refused
    # here, before any DB work, rather than quietly degrading to a fresh draw --
    # a swap that silently stops transplanting is exactly the bug being fixed.
    if roll_mode == ROLL_MODE_TRANSPLANT and not cons_slots:
        fail("bad_roll_mode", 2)
    consume_specs = [(augment_ids[i], grades[i], i) for i in cons_slots]

    ns, pod = resolve_db_pod()
    sql = build_augment_sql(owner, item_id, template_id, is_weapon, keystone_ids, faug,
                            consume_specs, roll_mode, reroll_only, augment_ids, operator,
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
    # Nothing was rerolled on a replay, and nothing was rerolled on a transplant:
    # in both cases the draw above never reached the item. The reasons differ --
    # a replay never wrote at all, a transplant overwrote the placeholder with the
    # consumed copy's real rolls under lock -- but the claim being made to the
    # player is the same one, and on a swap it would be false. The portal sets
    # reroll_only=[incoming] on every swap, so this is not an edge case; without
    # it the response tells the player their incoming augment got fresh rolls,
    # which is exactly the confusion this whole change exists to end.
    result["rerolled"] = ([] if (replayed or roll_mode == ROLL_MODE_TRANSPLANT) else
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
        raw_swap_slot = payload.get("swap_slot")
    else:
        ap = argparse.ArgumentParser(description="Augment perfect-roll / swap writer (Phase 1)")
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--item-id", required=True)
        ap.add_argument("--augments", required=True,
                        help="comma-separated augment template ids (T6_Augment_*)")
        ap.add_argument("--grade", default="5")
        ap.add_argument("--roll-mode", default=ROLL_MODE_PERFECT, choices=list(ROLL_MODES),
                        help="perfect = every roll 1.0 (prize path); "
                             "random = a fresh uniform draw per roll slot (player reroll); "
                             "transplant = the consumed copy's REAL rolls (swap; "
                             "requires --consume and something to consume)")
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
        ap.add_argument("--swap-slot", default=None,
                        help="0-based index into --augments of the slot a swap is "
                             "REPLACING. Required when the incoming augment has the "
                             "same name as the one it replaces (a grade change), "
                             "because nothing in the name list distinguishes them.")
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
        raw_swap_slot = a.swap_slot

    augment_ids = validate_augment_ids(augments)
    if not augment_ids:
        fail("no_augments", 2)
    # AFTER validate_augment_ids, never before: it drops blanks and de-dupes, so an
    # index taken against the raw list can point at the wrong slot.
    swap_slot = validate_swap_slot(raw_swap_slot, augment_ids)
    return (owner, item_id, augment_ids, grade, dry_run, roll_mode, consume,
            preserve_grades, reroll_only, idempotency_key, swap_slot)


def main():
    (owner, item_id, augment_ids, grade, dry_run, roll_mode, consume,
     preserve_grades, reroll_only, idempotency_key, swap_slot) = gather_args()
    if dry_run:
        do_dry_run(owner, item_id, augment_ids, grade, roll_mode, consume,
                   preserve_grades, reroll_only, swap_slot)
    else:
        do_live(owner, item_id, augment_ids, grade, roll_mode, consume,
                preserve_grades, reroll_only, None, idempotency_key, swap_slot)


if __name__ == "__main__":
    main()
