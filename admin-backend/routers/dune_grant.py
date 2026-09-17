import json
import logging
import os
import re
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import audit_log, require_admin, require_csrf
from relay import call_relay
from rmq_command import dispatch_server_command, is_online, resolve_fls_ref

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dune")

# Catalog file shipped as a static asset by the admin-backend (Phase 0, P0-2).
_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "dune-grant-catalog.json",
)

# In-memory catalog cache. The file is version-controlled and only changes on a
# deploy, so an unbounded TTL is fine; it is reloaded when the file's mtime moves.
_catalog_cache: dict = {"data": None, "mtime": 0.0}

# Pak item-metadata sidecar (scripts/build-item-pak-meta.py): per-template
# MaxStackSize + ItemTags from the client pak. Overlaid onto the grant
# catalog's item/schematic_item entries at serve time so the Grant Bench can
# show admin-facing warnings (MTX, non-tradeable, pak max stack) without
# touching the curated catalog JSON. pak_max_stack is the PAK value — live
# server config can differ (SolarisCoin pak 50k vs live 100k).
_PAK_META_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "dune-item-pak-meta.json",
)
_NON_TRADEABLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "dune-item-non-tradeable.json",
)

# Keystone catalog sidecar (G9 / icehunter parity Action #1). Generated from
# scripts/dune-grant-schema.sql once at build time; committed to the repo so
# the admin-backend has no DB dependency for the dropdown.
_KEYSTONE_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "keystone-catalog.json",
)
_keystone_catalog_cache: dict = {"data": None, "mtime": 0.0}

# Give-item catalog (scripts/build-give-item-catalog.py, rebuilt 2026-08-25 from
# the game's own item table): 1662 native template ids carrying is_gradeable,
# pak_max_stack, tier and cat. The Grant Bench pickers read it through
# /api/dune/v2/catalog/give-items; the batch validators below read it to refuse
# a grade on a template that cannot carry one. Its own mtime-keyed cache rather
# than an import from routers.v2_actions, which would make the two routers
# import each other.
_GIVE_ITEM_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "dune-give-item-catalog.json",
)
_give_item_cache: dict = {"index": None, "mtime": 0.0}


def _give_item_index() -> dict:
    """template_id -> catalog row. A missing or unreadable file yields {} so the
    grade check degrades to the existing warn-only stance instead of blocking
    every item grant."""
    try:
        mtime = os.path.getmtime(_GIVE_ITEM_CATALOG_PATH)
    except OSError:
        return {}
    cached = _give_item_cache["index"]
    if cached is not None and mtime == _give_item_cache["mtime"]:
        return cached
    try:
        with open(_GIVE_ITEM_CATALOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    index = {}
    for row in data.get("items") or []:
        tid = row.get("id")
        if isinstance(tid, str):
            index[tid] = row
    _give_item_cache["index"] = index
    _give_item_cache["mtime"] = mtime
    return index


def _validate_item_grade(where: str, tpl: str, quality: int, stack: int,
                         give_index: dict, qty_field: str = "stack_size",
                         stack_hint: str = "") -> None:
    """Grade rules shared by every item write: the two batch grants (G29 bank,
    G30 container) and the single-item item / schematic_item grant.

    quality is the item GRADE (quality_level): Base(0) through 5, and only
    gradeable templates carry one. Base(0) is a real grade, never "unset", so
    grade 0 is always accepted. A template the catalog does not carry stays
    warn-only -- custom and DLC ids are legal and the catalog is operator
    shorthand, not a whitelist -- so only a KNOWN non-gradeable is refused.
    Graded items never stack.

    `where` is the field path this refusal names, `qty_field` the name of the
    count field on that path (stack_size in a batch, quantity on the single-item
    form) and `stack_hint` any extra sentence that path needs."""
    if quality <= 0:
        return
    row = give_index.get(tpl)
    if row is not None and not row.get("is_gradeable"):
        raise HTTPException(
            400,
            f"{where}: {tpl} carries no grades (grades exist only on "
            f"gradeable T6 templates) -- send grade 0 (Base)")
    if stack > 1:
        raise HTTPException(
            400,
            f"{where}: graded items never stack (grade {quality} "
            f"requires {qty_field} 1, got {stack}).{stack_hint}")

# 60s TTL cache on the offline-inclusive player list (mirrors dune.py's roster).
PLAYERS_CACHE_TTL = 60
_players_cache: dict = {"data": None, "fetched_at": 0.0}

# Server-side hard caps (Section 11 risks). The grant script re-validates these
# independently (defence in depth), but the API rejects out-of-range input early.
MAX_QUANTITY = 9999
# quality_level is the item GRADE: Base(0) through 5, and only on gradeable
# templates. 6 is not a grade. THIS ROUTE is the cap, not the writer:
# scripts/dune-grant.sh's validate_quality still accepts ^[0-6]$, so a 6 that
# got past here would be written, not refused. Both batch validators cap at 5
# and this was the last route path that let one through.
# TODO: tighten validate_quality to ^[0-5]$ at scripts/dune-grant.sh:451 on the
# next writer deploy (game-host file, its own lane).
MAX_QUALITY = 5
MAX_SOLARI = 10_000_000
# G13 solari_currency: online-safe Funcom-proc path. Same numeric ceiling as
# G2 'solari' since both write the Solaris surface; differs only in storage.
MAX_SOLARI_CURRENCY = 10_000_000
MAX_INTEL = 100_000
MAX_HOUSE_SCRIP = 1_000_000
MAX_FACTION_REP = 12_474  # icehunter parity Action #2: factionRepCap from db.go:986
MAX_SPEC_XP = 44_182      # icehunter parity Action #2: maxXP per track from db.go:541
MAX_SPEC_LEVEL = 100      # icehunter cmdMaxSpec uses 100.0 as max level (db.go ~588)
MAX_CHAR_XP = 344_440  # G11 char_xp cap (cumulative XP for L200)
MAX_KEYSTONE_ID = 205  # G9 catalog upper bound (icehunter keystones.go)

# G20 import_blueprint + G22 import_solido_to_basebackup caps. piece_count is
# the sum of instances + placeables + pentashields; bytes is the serialized
# blueprint_data ceiling. Bumped 5000 → 10000 on 2026-05-26 to match the
# shell helper (scripts/dune-grant.sh CAP_BLUEPRINT_PIECES=10000) and unblock
# popular Solidos (Tippytoes/deep-desert fortresses, 5000-9000 pieces). The
# server-side build cap (m_bBuildingRestrictionLimitsEnabled) is False on
# this server, so the game itself accepts large builds — the admin-side cap
# was conservative leftover from G20 v1 and didn't track G22's bump.
MAX_BLUEPRINT_PIECES = 10_000
MAX_BLUEPRINT_BYTES = 1_048_576
MAX_G22_BLUEPRINT_PIECES = 10_000

# G21 base-backup grants. Slot cap is game-server C++ side (DB has no
# constraint); we enforce defensively here so the UI never lands a 4th row.
MAX_BB_SLOTS = 3
SOLIDO_API_BASE = "https://dune.layout.tools/api"
SOLIDO_FETCH_TIMEOUT = 15
_BLUEPRINT_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Hyphen is legitimate in Dune piece names (e.g. ..._Half-Left) and must be
# preserved so the in-game paste resolves the piece. Safe in the dollar-quoted
# SQL embedding (no $). template_id checks elsewhere stay strict (no hyphen).
_BUILDING_TYPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Conservative id shape for raw template / item-key identifiers.
_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

# Keystone ids are numeric — the grant script requires ^[0-9]+$, so the API
# must validate identically or it will accept values the script rejects.
_NUMERIC_ID_RE = re.compile(r"^[0-9]+$")

RECENT_GRANTS_DEFAULT = 25
RECENT_GRANTS_MAX = 500


class GrantRequest(BaseModel):
    account_id: int
    grant_type: str            # one of the catalog grant_type ids
    idempotency_key: str       # UUIDv4, generated by the UI at the preview step
    detail: dict               # type-specific shape; validated server-side
    mode: str = "apply"        # "apply" | "dry-run"
    defer_if_online: bool = False
    # Prefer live in-world delivery via the native RMQ server-command path when
    # the player is online and the grant is RMQ-eligible (base item / char XP);
    # falls back to the offline DB write otherwise. See _rmq_plan / _maybe_fire_rmq.
    live_delivery: bool = False


# P3a icehunter v0.5.x parity — enum allowlists for the 5 new grant types
# (G23/G24a/G24b/G25a + align_faction) and the UI-disabled G25b. Block list
# mirrors tags-data.json job_skill_blocks (5 jobs * 6 blocks).
# Top-level journey arc roots selectable by the per-arc grant. The 18 .core
# arcs plus the Atreides faction arc DA_FQ_ClimbTheRanks (box resolves it from
# .faction.atreides, target-faction-gated). DA_MQ_TheBloodline is intentionally
# absent — it is not a top-level arc (its nodes live under
# DA_MQ_TheGreatConvention.TheBloodline.*) and resolved to 0 nodes.
_MAIN_QUEST_PRESETS = {
    "DA_MQ_ANewBeginning",
    "DA_MQ_AssassinsHandbook",
    "DA_MQ_FindTheFremen",
    "DA_MQ_TheGreatConvention",
    "DA_MQ_TheGreatConventionPt2",
    "DA_MQ_NPEAutocompleted",
    "DA_SQ_VermiliusGap",
    "DA_SQ_JabalEifrit",
    "DA_SQ_Oodham",
    "DA_SQ_DeepDesert",
    "DA_SQ_Taxation",
    "DA_SQ_OverlandMap",
    "DA_SQ_Sheol",
    "DA_Dunipedia_WarForArrakis",
    "DA_Dunipedia_KnownUniverse",
    "DA_Dunipedia_ManualOfTheFriendlyDesert",
    "DA_Dunipedia_Landmarks",
    "DA_DLC_LostHarvest",
    "DA_FQ_ClimbTheRanks",
}
_JOBS = {"BeneGesserit", "Mentat", "Planetologist", "Swordmaster", "Trooper"}
_SKILL_BLOCKS = {
    f"Skills.Key.{b}"
    for b in (
        "BeneGesserit1", "BeneGesserit2", "BeneGesserit3",
        "CapstoneManipulation", "CapstoneSelfControl", "CapstoneWeirdingWay",
        "Mentat1", "Mentat2", "Mentat3",
        "CapstoneAssassination", "CapstoneMentalCalculus", "CapstoneTactician",
        "Planetologist1", "Planetologist2", "Planetologist3",
        "CapstoneDriver", "CapstoneExplorer", "CapstoneScientist",
        "Swordmaster1", "Swordmaster2", "Swordmaster3",
        "CapstoneAggression", "CapstoneBlade", "CapstoneResolve",
        "Trooper1", "Trooper2", "Trooper3",
        "CapstoneGadgets", "CapstoneSuspensorTech", "CapstoneWeaponry",
    )
}
_FACTIONS = {"atreides", "harkonnen"}


def _overlay_item_meta(data: dict) -> None:
    """Attach pak metadata + admin warnings to item/schematic_item entries
    in-place: pak_max_stack, tags, mtx, non_tradeable. Best-effort — missing
    or unreadable sidecars just leave the entries bare."""
    try:
        with open(_PAK_META_PATH, encoding="utf-8") as fh:
            pak_meta = json.load(fh).get("items", {})
    except (OSError, json.JSONDecodeError):
        pak_meta = {}
    try:
        with open(_NON_TRADEABLE_PATH, encoding="utf-8") as fh:
            non_tradeable = {t.lower() for t in json.load(fh)}
    except (OSError, json.JSONDecodeError):
        non_tradeable = set()
    if not pak_meta and not non_tradeable:
        return

    entries = data.get("entries", {})
    for key in ("item", "schematic_item"):
        for e in entries.get(key, []):
            tid = e.get("template_id") or ""
            pak = pak_meta.get(tid.lower(), {})
            stack = pak.get("max_stack")
            if isinstance(stack, int) and stack > 0:
                e["pak_max_stack"] = stack
            tags = pak.get("tags", [])
            if tags:
                e["tags"] = tags
            if tid.startswith("MTX_") or any(t.startswith(("MTX", "Items.MTX")) for t in tags):
                e["mtx"] = True
            if tid.lower() in non_tradeable:
                e["non_tradeable"] = True


def _sidecar_mtimes() -> tuple:
    out = []
    for p in (_PAK_META_PATH, _NON_TRADEABLE_PATH):
        try:
            out.append(os.path.getmtime(p))
        except OSError:
            out.append(0.0)
    return tuple(out)


def _load_catalog() -> dict:
    """Load the grant catalog from disk, cached in memory until the file mtime
    (or an overlay sidecar's mtime) changes. Raises 500 if the catalog asset
    is missing or malformed."""
    try:
        mtime = os.path.getmtime(_CATALOG_PATH)
    except OSError:
        raise HTTPException(500, "Grant catalog file not found")

    cache_key = (mtime,) + _sidecar_mtimes()
    cached = _catalog_cache["data"]
    if cached is not None and cache_key == _catalog_cache["mtime"]:
        return cached

    try:
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "Grant catalog file is unreadable")

    _overlay_item_meta(data)
    _catalog_cache["data"] = data
    _catalog_cache["mtime"] = cache_key
    return data


def _grant_type_ids(catalog: dict) -> set:
    return {g.get("id") for g in catalog.get("grant_types", []) if g.get("id")}


def _grant_type_def(catalog: dict, grant_type: str) -> dict | None:
    for g in catalog.get("grant_types", []):
        if g.get("id") == grant_type:
            return g
    return None


def _require_int(detail: dict, key: str, lo: int, hi: int) -> int:
    if key not in detail:
        raise HTTPException(400, f"detail.{key} is required")
    value = detail[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(400, f"detail.{key} must be an integer")
    if value < lo or value > hi:
        raise HTTPException(400, f"detail.{key} must be between {lo} and {hi}")
    return value


def _require_id(detail: dict, key: str) -> str:
    value = detail.get(key)
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise HTTPException(400, f"detail.{key} must match ^[A-Za-z0-9_]+$")
    return value


def _require_numeric_id(detail: dict, key: str) -> str:
    value = detail.get(key)
    if not isinstance(value, str) or not _NUMERIC_ID_RE.match(value):
        raise HTTPException(400, f"detail.{key} must match ^[0-9]+$")
    return value


def _require_mode(detail: dict) -> str:
    mode = detail.get("mode", "add")
    if mode not in ("add", "set"):
        raise HTTPException(400, "detail.mode must be 'add' or 'set'")
    return mode


def _validate_detail(catalog: dict, grant_type: str, detail: dict) -> dict:
    """Validate the per-type detail shape and hard caps. Returns the normalized
    detail dict that is forwarded to the relay. Raises 400 on any violation."""
    if not isinstance(detail, dict):
        raise HTTPException(400, "detail must be an object")

    entries = catalog.get("entries", {})

    if grant_type in ("item", "schematic_item"):
        template_id = _require_id(detail, "template_id")
        give_index = _give_item_index()
        # The grant catalog's own entries for this type, widened by the
        # give-item catalog the bench picker now renders: without that every
        # native template the picker offers but the 2026-05-21 cross-reference
        # missed would 400 here. The widening is scoped to grant_type "item".
        # The give-item catalog is 1662 ids of which only the 443 ending in
        # _Schematic are schematics (it carries no schematic category), so a
        # blanket union would let the schematic grant hand out a rifle.
        valid = {e.get("template_id") for e in entries.get(grant_type, [])}
        known = template_id in valid
        if not known and template_id in give_index:
            known = grant_type == "item" or template_id.endswith("_Schematic")
        if not known:
            raise HTTPException(400, f"unknown {grant_type} template_id: {template_id}")
        quantity = _require_int(detail, "quantity", 1, MAX_QUANTITY)
        quality = _require_int(detail, "quality", 0, MAX_QUALITY)
        # The writer inserts ONE dune.items row with stack_size = quantity and
        # quality_level = quality (scripts/dune-grant.sh:900-919), so a graded
        # quantity is a graded STACK. Same rules as the batch paths.
        _validate_item_grade(
            "detail", template_id, quality, quantity, give_index,
            qty_field="quantity",
            stack_hint=" The writer inserts one row per grant, so five graded "
                       "blades are five grants.")
        return {
            "template_id": template_id,
            "quantity": quantity,
            "quality": quality,
        }

    if grant_type == "solari":
        return {"amount": _require_int(detail, "amount", 1, MAX_SOLARI)}

    if grant_type == "solari_currency":
        # G13 online-safe Solari (Funcom proc, no backpack slot). Same numeric
        # cap as item-coin G2 since both write the Solaris surface; difference
        # is the storage path.
        return {"amount": _require_int(detail, "amount", 1, MAX_SOLARI_CURRENCY)}

    if grant_type == "intel":
        return {"amount": _require_int(detail, "amount", 1, MAX_INTEL)}

    if grant_type == "house_scrip":
        return {
            "amount": _require_int(detail, "amount", 1, MAX_HOUSE_SCRIP),
            "mode": _require_mode(detail),
        }

    if grant_type == "recipe":
        item_key = _require_id(detail, "item_key")
        valid = {e.get("item_key") for e in entries.get("recipe", [])}
        if item_key not in valid:
            raise HTTPException(400, f"unknown recipe item_key: {item_key}")
        return {"item_key": item_key}

    if grant_type == "faction_rep":
        faction_id = detail.get("faction_id")
        if isinstance(faction_id, bool) or not isinstance(faction_id, int):
            raise HTTPException(400, "detail.faction_id must be an integer")
        valid = {e.get("faction_id") for e in entries.get("faction", [])}
        if faction_id not in valid:
            raise HTTPException(400, f"unknown faction_id: {faction_id}")
        return {
            "faction_id": faction_id,
            "amount": _require_int(detail, "amount", -MAX_FACTION_REP, MAX_FACTION_REP),
            "mode": _require_mode(detail),
        }

    if grant_type == "spec_xp":
        track_type = _require_id(detail, "track_type")
        # Only CONFIRMED tracks are grant targets — track_type "Invalid" is a
        # C++ enum sentinel and NEEDS-VERIFICATION tracks are not trusted (M1).
        confirmed = {
            e.get("track_type")
            for e in entries.get("specialization_track", [])
            if e.get("confidence") == "CONFIRMED"
        }
        if track_type not in confirmed:
            raise HTTPException(400, f"unknown or unconfirmed track_type: {track_type}")
        out = {"track_type": track_type, "mode": _require_mode(detail)}
        # xp and level are both optional but at least one must be present.
        if "xp" in detail:
            out["xp"] = _require_int(detail, "xp", 0, MAX_SPEC_XP)
        if "level" in detail:
            out["level"] = _require_int(detail, "level", 0, MAX_SPEC_LEVEL)
        if "xp" not in out and "level" not in out:
            raise HTTPException(400, "spec_xp requires detail.xp and/or detail.level")
        return out

    if grant_type == "char_xp":
        # G11 char_xp. Adds XP, credits TotalSkillPoints + UnspentSkillPoints
        # by levels_gained delta in the same SQL transaction. Offline-gated.
        # See docs/dune-research/CHAR-XP-GRANT-SPEC.md. The script-side gate
        # on force_wipe_points_ack was dropped 2026-05-23 after empirical
        # validation (grant_id=11); the flag is now an optional audit field.
        # target_level (1..200) takes precedence over raw amount: the bash
        # builder resolves the cumulative-XP delta from dune.ls_char_xp_curve.
        # Exactly one of target_level / amount must be set; amount stays the
        # advanced raw-XP path. 0 (or absent) means "unset" for both.
        amount = detail.get("amount", 0)
        target_level = detail.get("target_level", 0)
        for key, value in (("amount", amount), ("target_level", target_level)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise HTTPException(400, f"char_xp {key} must be an integer")
        if target_level:
            if not 1 <= target_level <= 200:
                raise HTTPException(400, "char_xp target_level must be between 1 and 200")
            amount = 0
        elif amount:
            if not 1 <= amount <= MAX_CHAR_XP:
                raise HTTPException(400, f"char_xp amount must be between 1 and {MAX_CHAR_XP}")
        else:
            raise HTTPException(400, "char_xp requires target_level (1-200) or amount")
        ack = detail.get("force_wipe_points_ack")
        if ack is not None and not isinstance(ack, bool):
            raise HTTPException(
                400,
                "char_xp force_wipe_points_ack must be boolean if provided",
            )
        out = {"amount": amount, "force_wipe_points_ack": bool(ack)}
        if target_level:
            out["target_level"] = target_level
        return out

    if grant_type == "keystone":
        # The catalog ships no keystone entry list (G9 grants individual
        # already-known keystones); validate by shape + range. keystone_id is
        # numeric and must be in the catalog range 1..MAX_KEYSTONE_ID.
        kid_str = _require_numeric_id(detail, "keystone_id")
        kid = int(kid_str)
        if not (1 <= kid <= MAX_KEYSTONE_ID):
            raise HTTPException(
                400, f"detail.keystone_id must be 1..{MAX_KEYSTONE_ID}: {kid}")
        return {"keystone_id": kid_str}

    if grant_type == "spec_unlock_track":
        # G9-batch: unlock all 41 keystones of one track + summed sp_bonus.
        # Same CONFIRMED-track gate as spec_xp (drops the Invalid/Count enum
        # sentinels). RAM-fragile (FLevelComponent write), offline-gated upstream.
        track_type = _require_id(detail, "track_type")
        confirmed = {
            e.get("track_type")
            for e in entries.get("specialization_track", [])
            if e.get("confidence") == "CONFIRMED"
        }
        if track_type not in confirmed:
            raise HTTPException(400, f"unknown or unconfirmed track_type: {track_type}")
        return {"track_type": track_type}

    if grant_type == "spec_unlock_all":
        # G9-batch: unlock all 205 keystones across the 5 tracks. No inputs.
        return {}

    if grant_type == "teleport":
        # G15: either named-location mode OR custom coords. Named takes
        # precedence. partition_id is optional (defaults to player's current).
        out: dict = {}
        loc_name = detail.get("location_name")
        if loc_name is not None:
            if not isinstance(loc_name, str) or not re.match(r"^[A-Za-z0-9_]+$", loc_name):
                raise HTTPException(400, "detail.location_name must match ^[A-Za-z0-9_]+$")
            out["location_name"] = loc_name
        else:
            for axis in ("x", "y", "z"):
                v = detail.get(axis)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise HTTPException(400, f"detail.{axis} must be a number (or supply location_name)")
                out[axis] = float(v)
        pid = detail.get("partition_id")
        if pid is not None:
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise HTTPException(400, "detail.partition_id must be an integer if provided")
            if not (1 <= pid <= 1000):
                raise HTTPException(400, f"detail.partition_id out of range 1..1000: {pid}")
            out["partition_id"] = pid
        return out

    if grant_type == "reset_specs":
        # G16: optional track_type for single-track reset; absent/empty/"all"
        # → full reset (tracks + keystones via Funcom procs).
        out = {}
        tt = detail.get("track_type")
        if tt is not None and tt != "" and tt != "all":
            if not isinstance(tt, str) or not re.match(r"^[A-Za-z0-9_]+$", tt):
                raise HTTPException(400, "detail.track_type must match ^[A-Za-z0-9_]+$ or be 'all'")
            out["track_type"] = tt
        return out

    if grant_type in ("reset_tutorials", "wipe_codex", "repair_all"):
        # G17 / G18 / G19: no parameters.
        return {}

    if grant_type == "item_live":
        template_id = _require_id(detail, "template_id")
        valid = {e.get("template_id") for e in entries.get("item", [])}
        if template_id not in valid:
            raise HTTPException(400, f"unknown item_live template_id: {template_id}")
        house_name = _require_id(detail, "house_name")
        if not re.match(r"^DA_House[A-Za-z0-9_]+$", house_name):
            raise HTTPException(400, f"invalid house_name: {house_name}")
        return {
            "template_id": template_id,
            "amount": _require_int(detail, "amount", 1, MAX_QUANTITY),
            "house_name": house_name,
        }

    if grant_type == "import_blueprint":
        # G20 — materialize a BuildingBlueprint_CopyDevice from Solido Market
        # or inline JSON. See docs/dune-research/G20-IMPORT-BLUEPRINT-BUILD-PLAN.md.
        delivery = detail.get("delivery")
        if delivery not in ("backpack", "bank"):
            raise HTTPException(400, "detail.delivery must be 'backpack' or 'bank'")

        blueprint_id = detail.get("blueprint_id")
        blueprint_data = detail.get("blueprint_data")
        has_id = isinstance(blueprint_id, str) and blueprint_id != ""
        has_data = isinstance(blueprint_data, dict)
        if has_id == has_data:
            raise HTTPException(
                400,
                "exactly one of detail.blueprint_id (Solido UUID) or "
                "detail.blueprint_data (inline JSON object) is required",
            )

        source_blueprint_id = None
        if has_id:
            if not _BLUEPRINT_UUID_RE.match(blueprint_id):
                raise HTTPException(400, "detail.blueprint_id must be a UUID")
            source_blueprint_id = blueprint_id
            url = f"{SOLIDO_API_BASE}/blueprints/{blueprint_id}"
            try:
                with httpx.Client(timeout=SOLIDO_FETCH_TIMEOUT) as client:
                    resp = client.get(url, headers={"Accept": "application/json"})
            except httpx.RequestError as exc:
                raise HTTPException(
                    502,
                    f"Solido Market fetch failed ({type(exc).__name__}); "
                    "try again or paste the JSON inline",
                )
            if resp.status_code == 404:
                raise HTTPException(404, f"Solido blueprint not found: {blueprint_id}")
            if resp.status_code == 429:
                raise HTTPException(
                    502,
                    "Solido Market rate-limited the fetch; try again or paste JSON inline",
                )
            if resp.status_code >= 500:
                raise HTTPException(
                    502,
                    f"Solido Market upstream error {resp.status_code}; "
                    "try again or paste JSON inline",
                )
            if resp.status_code != 200:
                raise HTTPException(
                    502,
                    f"Solido Market returned unexpected status {resp.status_code}",
                )
            raw = resp.content[:MAX_BLUEPRINT_BYTES + 1]
            if len(raw) > MAX_BLUEPRINT_BYTES:
                raise HTTPException(
                    400,
                    f"Solido blueprint exceeds {MAX_BLUEPRINT_BYTES} bytes",
                )
            try:
                fetched = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise HTTPException(502, "Solido Market response is not valid JSON")
            # The Solido detail endpoint returns either the blueprint envelope
            # directly OR wraps it under a "blueprint_data" key. Accept both.
            if isinstance(fetched, dict) and isinstance(fetched.get("blueprint_data"), dict):
                blueprint_data = fetched["blueprint_data"]
            elif isinstance(fetched, dict):
                blueprint_data = fetched
            else:
                raise HTTPException(502, "Solido Market response shape unexpected")

        if not isinstance(blueprint_data, dict):
            raise HTTPException(400, "detail.blueprint_data must be a JSON object")

        instances = blueprint_data.get("instances") or []
        placeables = blueprint_data.get("placeables") or []
        pentashields = blueprint_data.get("pentashields") or []
        if not isinstance(instances, list):
            raise HTTPException(400, "blueprint_data.instances must be an array")
        if not isinstance(placeables, list):
            raise HTTPException(400, "blueprint_data.placeables must be an array")
        if not isinstance(pentashields, list):
            raise HTTPException(400, "blueprint_data.pentashields must be an array")
        if len(instances) + len(placeables) < 1:
            raise HTTPException(
                400,
                "blueprint_data must contain at least one instance or placeable",
            )

        piece_count = len(instances) + len(placeables) + len(pentashields)
        if piece_count > MAX_BLUEPRINT_PIECES:
            raise HTTPException(
                400,
                f"blueprint piece count {piece_count} exceeds {MAX_BLUEPRINT_PIECES}",
            )

        # Per-element validation + MTX tally. Cheap pass; we already capped
        # the total count above.
        mtx_count = 0
        normalized_instances = []
        for idx, inst in enumerate(instances):
            if not isinstance(inst, dict):
                raise HTTPException(400, f"instances[{idx}] must be an object")
            btype = inst.get("building_type")
            if not isinstance(btype, str) or not _BUILDING_TYPE_RE.match(btype):
                raise HTTPException(
                    400, f"instances[{idx}].building_type must match ^[A-Za-z0-9_]+$"
                )
            if btype.startswith("MTX_"):
                mtx_count += 1
            try:
                x = float(inst.get("x", 0))
                y = float(inst.get("y", 0))
                z = float(inst.get("z", 0))
                rotation = float(inst.get("rotation", 0))
            except (TypeError, ValueError):
                raise HTTPException(
                    400, f"instances[{idx}] x/y/z/rotation must be numeric"
                )
            for n in (x, y, z, rotation):
                if n != n or n in (float("inf"), float("-inf")):  # NaN/Inf check
                    raise HTTPException(
                        400, f"instances[{idx}] has non-finite coordinate/rotation"
                    )
            normalized_instances.append({
                "building_type": btype, "x": x, "y": y, "z": z, "rotation": rotation,
            })

        normalized_placeables = []
        for idx, pl in enumerate(placeables):
            if not isinstance(pl, dict):
                raise HTTPException(400, f"placeables[{idx}] must be an object")
            btype = pl.get("building_type")
            if not isinstance(btype, str) or not _BUILDING_TYPE_RE.match(btype):
                raise HTTPException(
                    400, f"placeables[{idx}].building_type must match ^[A-Za-z0-9_]+$"
                )
            if btype.startswith("MTX_"):
                mtx_count += 1
            try:
                x = float(pl.get("x", 0))
                y = float(pl.get("y", 0))
                z = float(pl.get("z", 0))
                rx = float(pl.get("rx", 0))
                ry = float(pl.get("ry", 0))
                rz = float(pl.get("rz", 0))
            except (TypeError, ValueError):
                raise HTTPException(
                    400, f"placeables[{idx}] x/y/z/rx/ry/rz must be numeric"
                )
            for n in (x, y, z, rx, ry, rz):
                if n != n or n in (float("inf"), float("-inf")):
                    raise HTTPException(
                        400, f"placeables[{idx}] has non-finite coordinate/rotation"
                    )
            normalized_placeables.append({
                "building_type": btype, "x": x, "y": y, "z": z,
                "rx": rx, "ry": ry, "rz": rz,
            })

        normalized_pentashields = []
        for idx, ps in enumerate(pentashields):
            if not isinstance(ps, dict):
                raise HTTPException(400, f"pentashields[{idx}] must be an object")
            pid = ps.get("placeable_id")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
                raise HTTPException(
                    400, f"pentashields[{idx}].placeable_id must be a non-negative integer"
                )
            scale = ps.get("scale")
            if not isinstance(scale, list) or len(scale) != 3:
                raise HTTPException(
                    400, f"pentashields[{idx}].scale must be a 3-element array"
                )
            scale_ints = []
            for s in scale:
                if isinstance(s, bool) or not isinstance(s, int):
                    raise HTTPException(
                        400, f"pentashields[{idx}].scale entries must be integers"
                    )
                if s < -32768 or s > 32767:
                    raise HTTPException(
                        400, f"pentashields[{idx}].scale entry out of int16 range"
                    )
                scale_ints.append(s)
            normalized_pentashields.append({
                "placeable_id": pid, "scale": scale_ints,
            })

        normalized_data = {
            "instances": normalized_instances,
            "placeables": normalized_placeables,
            "pentashields": normalized_pentashields,
        }
        # Final size check after normalization (defends against giant strings
        # that were dropped during validation but bytes-budget still matters
        # for the script payload).
        serialized_size = len(json.dumps(normalized_data, separators=(",", ":")))
        if serialized_size > MAX_BLUEPRINT_BYTES:
            raise HTTPException(
                400,
                f"normalized blueprint payload {serialized_size} bytes exceeds "
                f"{MAX_BLUEPRINT_BYTES}",
            )

        title = detail.get("title")
        if title is not None:
            if not isinstance(title, str):
                raise HTTPException(400, "detail.title must be a string if provided")
            if len(title) > 200:
                raise HTTPException(400, "detail.title must be <=200 chars")

        out = {
            "delivery": delivery,
            "blueprint_data": normalized_data,
            "piece_count": piece_count,
            "instance_count": len(normalized_instances),
            "placeable_count": len(normalized_placeables),
            "pentashield_count": len(normalized_pentashields),
            "mtx_count": mtx_count,
        }
        if source_blueprint_id is not None:
            out["source_blueprint_id"] = source_blueprint_id
        if title is not None:
            out["title"] = title
        return out

    if grant_type == "import_solido_to_basebackup":
        # G22 — synthesize a base_backups slot + empty BaseBackupTool from a
        # Solido design. Hybrid of G20 (Solido fetch / inline JSON) and G21
        # (delivers an empty BaseBackupTool to the CHOAM bank). v1 ships
        # refuse-at-cap; --overwrite-slot is deferred to v1.5. Class registry
        # coverage and MTX placeable refuse are enforced server-side in the
        # bash builder (it has live access to dune.ls_solido_class_defaults);
        # this validator only checks shape + bytes + piece count + recipient.
        # See docs/dune-research/ITEM-G22-BUILD-SPEC.md §2.
        recipient = detail.get("recipient_account_id")
        if isinstance(recipient, bool) or not isinstance(recipient, int) or recipient <= 0:
            raise HTTPException(
                400, "detail.recipient_account_id must be a positive integer")

        blueprint_id = detail.get("blueprint_id")
        blueprint_data = detail.get("blueprint_data")
        has_id = isinstance(blueprint_id, str) and blueprint_id != ""
        has_data = isinstance(blueprint_data, dict)
        if has_id == has_data:
            raise HTTPException(
                400,
                "exactly one of detail.blueprint_id (Solido UUID) or "
                "detail.blueprint_data (inline JSON object) is required",
            )

        source_blueprint_id = None
        if has_id:
            if not _BLUEPRINT_UUID_RE.match(blueprint_id):
                raise HTTPException(400, "detail.blueprint_id must be a UUID")
            source_blueprint_id = blueprint_id
            url = f"{SOLIDO_API_BASE}/blueprints/{blueprint_id}"
            try:
                with httpx.Client(timeout=SOLIDO_FETCH_TIMEOUT) as client:
                    resp = client.get(url, headers={"Accept": "application/json"})
            except httpx.RequestError as exc:
                raise HTTPException(
                    502,
                    f"Solido Market fetch failed ({type(exc).__name__}); "
                    "try again or paste the JSON inline",
                )
            if resp.status_code == 404:
                raise HTTPException(404, f"Solido blueprint not found: {blueprint_id}")
            if resp.status_code == 429:
                raise HTTPException(
                    502,
                    "Solido Market rate-limited the fetch; try again or paste JSON inline",
                )
            if resp.status_code >= 500:
                raise HTTPException(
                    502,
                    f"Solido Market upstream error {resp.status_code}; "
                    "try again or paste JSON inline",
                )
            if resp.status_code != 200:
                raise HTTPException(
                    502,
                    f"Solido Market returned unexpected status {resp.status_code}",
                )
            raw = resp.content[:MAX_BLUEPRINT_BYTES + 1]
            if len(raw) > MAX_BLUEPRINT_BYTES:
                raise HTTPException(
                    400, f"Solido blueprint exceeds {MAX_BLUEPRINT_BYTES} bytes")
            try:
                fetched = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise HTTPException(502, "Solido Market response is not valid JSON")
            if isinstance(fetched, dict) and isinstance(fetched.get("blueprint_data"), dict):
                blueprint_data = fetched["blueprint_data"]
            elif isinstance(fetched, dict):
                blueprint_data = fetched
            else:
                raise HTTPException(502, "Solido Market response shape unexpected")

        if not isinstance(blueprint_data, dict):
            raise HTTPException(400, "detail.blueprint_data must be a JSON object")

        instances = blueprint_data.get("instances") or []
        placeables = blueprint_data.get("placeables") or []
        pentashields = blueprint_data.get("pentashields") or []
        if not isinstance(instances, list):
            raise HTTPException(400, "blueprint_data.instances must be an array")
        if not isinstance(placeables, list):
            raise HTTPException(400, "blueprint_data.placeables must be an array")
        if not isinstance(pentashields, list):
            raise HTTPException(400, "blueprint_data.pentashields must be an array")
        if len(instances) + len(placeables) < 1:
            raise HTTPException(
                400,
                "blueprint_data must contain at least one instance or placeable",
            )

        piece_count = len(instances) + len(placeables) + len(pentashields)
        if piece_count > MAX_G22_BLUEPRINT_PIECES:
            raise HTTPException(
                400,
                f"blueprint piece count {piece_count} exceeds {MAX_G22_BLUEPRINT_PIECES}",
            )

        # Shape pass mirrors G20 (defence in depth — bash re-validates from the
        # Solido staging tables). MTX placeables get refused in the bash builder
        # via class registry coverage; we let MTX_*-prefixed building_types
        # through here because Funcom accepts them as building_instances at
        # restore time (G20 paste confirmed empirically 2026-05-24).
        mtx_count = 0
        normalized_instances = []
        for idx, inst in enumerate(instances):
            if not isinstance(inst, dict):
                raise HTTPException(400, f"instances[{idx}] must be an object")
            btype = inst.get("building_type")
            if not isinstance(btype, str) or not _BUILDING_TYPE_RE.match(btype):
                raise HTTPException(
                    400, f"instances[{idx}].building_type must match ^[A-Za-z0-9_]+$"
                )
            if btype.startswith("MTX_"):
                mtx_count += 1
            try:
                x = float(inst.get("x", 0))
                y = float(inst.get("y", 0))
                z = float(inst.get("z", 0))
                rotation = float(inst.get("rotation", 0))
            except (TypeError, ValueError):
                raise HTTPException(
                    400, f"instances[{idx}] x/y/z/rotation must be numeric")
            for n in (x, y, z, rotation):
                if n != n or n in (float("inf"), float("-inf")):
                    raise HTTPException(
                        400, f"instances[{idx}] has non-finite coordinate/rotation")
            normalized_instances.append({
                "building_type": btype, "x": x, "y": y, "z": z, "rotation": rotation,
            })

        normalized_placeables = []
        for idx, pl in enumerate(placeables):
            if not isinstance(pl, dict):
                raise HTTPException(400, f"placeables[{idx}] must be an object")
            btype = pl.get("building_type")
            if not isinstance(btype, str) or not _BUILDING_TYPE_RE.match(btype):
                raise HTTPException(
                    400, f"placeables[{idx}].building_type must match ^[A-Za-z0-9_]+$"
                )
            try:
                x = float(pl.get("x", 0))
                y = float(pl.get("y", 0))
                z = float(pl.get("z", 0))
                rx = float(pl.get("rx", 0))
                ry = float(pl.get("ry", 0))
                rz = float(pl.get("rz", 0))
            except (TypeError, ValueError):
                raise HTTPException(
                    400, f"placeables[{idx}] x/y/z/rx/ry/rz must be numeric")
            for n in (x, y, z, rx, ry, rz):
                if n != n or n in (float("inf"), float("-inf")):
                    raise HTTPException(
                        400, f"placeables[{idx}] has non-finite coordinate/rotation")
            normalized_placeables.append({
                "building_type": btype, "x": x, "y": y, "z": z,
                "rx": rx, "ry": ry, "rz": rz,
            })

        normalized_pentashields = []
        for idx, ps in enumerate(pentashields):
            if not isinstance(ps, dict):
                raise HTTPException(400, f"pentashields[{idx}] must be an object")
            pid = ps.get("placeable_id")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
                raise HTTPException(
                    400, f"pentashields[{idx}].placeable_id must be a non-negative integer"
                )
            scale = ps.get("scale")
            if not isinstance(scale, list) or len(scale) != 3:
                raise HTTPException(
                    400, f"pentashields[{idx}].scale must be a 3-element array")
            scale_ints = []
            for s in scale:
                if isinstance(s, bool) or not isinstance(s, int):
                    raise HTTPException(
                        400, f"pentashields[{idx}].scale entries must be integers")
                if s < -32768 or s > 32767:
                    raise HTTPException(
                        400, f"pentashields[{idx}].scale entry out of int16 range")
                scale_ints.append(s)
            normalized_pentashields.append({
                "placeable_id": pid, "scale": scale_ints,
            })

        normalized_data = {
            "instances": normalized_instances,
            "placeables": normalized_placeables,
            "pentashields": normalized_pentashields,
        }
        serialized_size = len(json.dumps(normalized_data, separators=(",", ":")))
        if serialized_size > MAX_BLUEPRINT_BYTES:
            raise HTTPException(
                400,
                f"normalized blueprint payload {serialized_size} bytes exceeds "
                f"{MAX_BLUEPRINT_BYTES}",
            )

        base_backup_name = detail.get("base_backup_name")
        if not isinstance(base_backup_name, str) or not base_backup_name.strip():
            raise HTTPException(
                400, "detail.base_backup_name is required (string, 1..200 chars)")
        base_backup_name = base_backup_name.strip()
        if len(base_backup_name) > 200:
            raise HTTPException(
                400, "detail.base_backup_name must be <=200 chars")

        out = {
            "recipient_account_id": recipient,
            "base_backup_name": base_backup_name,
            "blueprint_data": normalized_data,
            "piece_count": piece_count,
            "instance_count": len(normalized_instances),
            "placeable_count": len(normalized_placeables),
            "pentashield_count": len(normalized_pentashields),
            "mtx_count": mtx_count,
        }
        if source_blueprint_id is not None:
            out["source_blueprint_id"] = source_blueprint_id
        return out

    if grant_type in ("bb_handoff", "bb_clone"):
        # G21 — base-backup grants. Slot-count check (refuse if recipient ≥ 3)
        # is done in the handler after shape validation so we can `await` the
        # relay; this branch only normalizes the detail.
        recipient = detail.get("recipient_account_id")
        if isinstance(recipient, bool) or not isinstance(recipient, int) or recipient <= 0:
            raise HTTPException(
                400, "detail.recipient_account_id must be a positive integer")
        source = detail.get("source_backup_id")
        if isinstance(source, bool) or not isinstance(source, int) or source <= 0:
            raise HTTPException(
                400, "detail.source_backup_id must be a positive integer")
        out = {
            "recipient_account_id": recipient,
            "source_backup_id": source,
        }
        if grant_type == "bb_handoff":
            override = detail.get("override_name")
            if override is not None:
                if not isinstance(override, str):
                    raise HTTPException(
                        400, "detail.override_name must be a string if provided")
                if len(override) > 200:
                    raise HTTPException(
                        400, "detail.override_name must be <=200 chars")
                out["override_name"] = override
        else:  # bb_clone
            name = detail.get("backup_name")
            if name is not None:
                if not isinstance(name, str):
                    raise HTTPException(
                        400, "detail.backup_name must be a string if provided")
                if len(name) > 200:
                    raise HTTPException(
                        400, "detail.backup_name must be <=200 chars")
                out["backup_name"] = name
        return out

    if grant_type == "progression_preset":
        faction = detail.get("faction")
        if faction not in {"atreides", "harkonnen"}:
            raise HTTPException(400, "detail.faction must be atreides or harkonnen")
        preset = detail.get("preset")
        if preset not in {"landsraad_unlock_only", "ch3_start", "rank19_eligible"}:
            raise HTTPException(
                400,
                "detail.preset must be one of: landsraad_unlock_only, ch3_start, rank19_eligible",
            )
        return {"faction": faction, "preset": preset}

    if grant_type == "main_quest_unlock":
        # G23 — preset enum only; node + tag union resolved offline from
        # tags-data.json sidecar in dune-grant.sh.
        preset = detail.get("preset")
        if preset not in _MAIN_QUEST_PRESETS:
            raise HTTPException(
                400,
                "detail.preset must be one of: " + ", ".join(sorted(_MAIN_QUEST_PRESETS)),
            )
        return {"preset": preset}

    if grant_type in ("grant_full_job_tree", "reset_full_skill_area"):
        # G24a / G25a — job enum only. Both are FLevelComponent writes,
        # offline-gated upstream; reset carries the starter-class hazard guard
        # in the bash builder (refuses before write if job == StarterSkillTreeTag).
        job = detail.get("job")
        if job not in _JOBS:
            raise HTTPException(
                400,
                f"detail.job must be one of: " + ", ".join(sorted(_JOBS)),
            )
        return {"job": job}

    if grant_type == "grant_skill_block":
        # G24b — single Skills.Key.* enum (30 valid values).
        block = detail.get("block")
        if block not in _SKILL_BLOCKS:
            raise HTTPException(
                400,
                "detail.block must be one of the 30 Skills.Key.* values "
                "in entries.skill_block (got: "
                f"{block!r})",
            )
        return {"block": block}

    if grant_type == "set_starter_class":
        # G25b — UI DISABLED. Backend route + bash builder remain plumbed for
        # SQL-level recovery only; gated on the ?confirm_disabled_ui=1 query
        # flag (checked at the route handler) AND stamped with via_disabled_ui
        # in the audit detail so the row is grep-able in the ledger.
        # See spec §G25b DEFERRED notice + risk register.
        job = detail.get("job")
        if job not in _JOBS:
            raise HTTPException(
                400, "detail.job must be one of: " + ", ".join(sorted(_JOBS)),
            )
        return {"job": job, "via_disabled_ui": True}

    if grant_type == "align_faction":
        # g7b — alignment-only (no rep change). Online-safe tabular grant.
        faction = detail.get("faction")
        if faction not in _FACTIONS:
            raise HTTPException(400, "detail.faction must be atreides or harkonnen")
        return {"faction": faction}

    if grant_type == "journey_full_unlock":
        # WP-C — journey/story full-unlock tag repair. Online-safe tabular grant
        # (update_player_tags, same proc class as align_faction). Two opt-in
        # booleans; CORE is always applied by the bash builder. The faction-story
        # bucket is gated on the LIVE target faction inside dune-grant.sh (it
        # resolves dune.player_faction and rejects Harkonnen+faction-story with
        # "Harkonnen story set not yet captured"), so it is NOT an operator enum
        # here — we only normalize the two flags.
        def _opt_bool(key: str) -> bool:
            value = detail.get(key, False)
            if not isinstance(value, bool):
                raise HTTPException(400, f"detail.{key} must be a boolean")
            return value
        return {
            "include_faction_story": _opt_bool("include_faction_story"),
            "include_exploration_poi": _opt_bool("include_exploration_poi"),
        }

    if grant_type == "journey_node_completion":
        # WP-C2 — journey NODE completion pass (pairs with journey_full_unlock
        # so the in-game journey UI reflects the unlock). Offline-required:
        # Funcom's complete_journey_story_nodes_for_player raises while the
        # player is online. Faction gating happens in the bash builder against
        # the live target faction, same as journey_full_unlock. The four arc
        # buckets are opt-in; none checked == whole journey (bash default).
        def _opt_bool(key: str) -> bool:
            value = detail.get(key, False)
            if not isinstance(value, bool):
                raise HTTPException(400, f"detail.{key} must be a boolean")
            return value
        return {
            "include_faction_story": _opt_bool("include_faction_story"),
            "include_main_quest": _opt_bool("include_main_quest"),
            "include_side_quests": _opt_bool("include_side_quests"),
            "include_dunipedia": _opt_bool("include_dunipedia"),
            "include_dlc_lostharvest": _opt_bool("include_dlc_lostharvest"),
        }

    if grant_type == "spice_addiction_enable":
        # Trials-of-Aql / 4th-trial spice-addiction repair. No operator detail
        # fields — the target player is the only input. The bash builder
        # resolves the pawn, snapshots the pre-state, and writes the two jsonb
        # paths. Offline-required (RAM-fragile FGL write). Reject any stray
        # detail keys so a fat-finger can't smuggle unexpected fields onto the
        # audit row.
        if detail:
            raise HTTPException(
                400, "spice_addiction_enable takes no detail fields")
        return {}

    if grant_type == "bank_items_batch":
        # G29 — multi-item insert into CHOAM bank (inventory_type=30).
        # Online-safe: bank inserts render at the next zone transition.
        items = detail.get("items")
        if not isinstance(items, list):
            raise HTTPException(400, "detail.items must be an array")
        if len(items) < 1:
            raise HTTPException(400, "detail.items is empty (need at least one item)")
        if len(items) > 30:
            raise HTTPException(400, f"detail.items count {len(items)} exceeds cap 30")
        valid_templates = {e.get("template_id") for e in entries.get("item", [])}
        give_index = _give_item_index()
        out_items = []
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                raise HTTPException(400, f"detail.items[{idx}] must be an object")
            tpl = it.get("template_id")
            if not isinstance(tpl, str) or not re.match(r"^[A-Za-z0-9_]+$", tpl):
                raise HTTPException(400, f"detail.items[{idx}].template_id invalid (alnum+_ only)")
            if valid_templates and tpl not in valid_templates:
                # Warn-only path: catalog may not list every legal template_id
                # (custom or DLC). Server-side regex + DB capacity check still
                # gate; surface a clearer 400 if catalog is the source of truth.
                # For now, allow unknowns (catalog is operator-shorthand, not
                # an authoritative whitelist).
                pass
            stack = it.get("stack_size")
            if not isinstance(stack, int) or stack < 1 or stack > MAX_QUANTITY:
                raise HTTPException(400, f"detail.items[{idx}].stack_size must be int 1..{MAX_QUANTITY}")
            quality = it.get("quality", 0)
            if not isinstance(quality, int) or quality < 0 or quality > 5:
                raise HTTPException(400, f"detail.items[{idx}].quality must be int 0..5")
            _validate_item_grade(f"detail.items[{idx}]", tpl, quality, stack, give_index)
            out_items.append({"template_id": tpl, "stack_size": stack, "quality": quality})
        return {"items": out_items}

    if grant_type == "container_items_batch":
        # G30 — multi-item insert into one of the recipient's OWN vehicle/storage
        # containers (inventory_type 0). Accepts raw items and/or vehicle-package
        # selections (expanded server-side). Ownership of target_inv is enforced
        # in dune-grant.sh via the permission_actor_rank chain; here we validate
        # shape + caps. Items appear in-game only after the container reloads
        # (fly it out of the zone and back).
        target_inv = detail.get("target_inv")
        if not isinstance(target_inv, int) or target_inv < 1:
            raise HTTPException(400, "detail.target_inv must be a positive integer (a container inventory id)")
        items = list(detail.get("items") or [])
        if not isinstance(items, list):
            raise HTTPException(400, "detail.items must be an array")
        packages = detail.get("packages") or []
        if packages:
            if not isinstance(packages, list):
                raise HTTPException(400, "detail.packages must be an array of {key, qty}")
            try:
                from vehicle_packages import expand_selection
                sel = [(p["key"], int(p.get("qty", 1))) for p in packages]
                items = items + expand_selection(sel)
            except (KeyError, ValueError, TypeError) as exc:
                raise HTTPException(400, f"invalid packages selection: {exc}")
        if len(items) < 1:
            raise HTTPException(400, "nothing to insert (add at least one vehicle package or item)")
        if len(items) > 150:
            raise HTTPException(400, f"item count {len(items)} exceeds container cap 150")
        give_index = _give_item_index()
        out_items = []
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                raise HTTPException(400, f"detail.items[{idx}] must be an object")
            tpl = it.get("template_id")
            if not isinstance(tpl, str) or not re.match(r"^[A-Za-z0-9_]+$", tpl):
                raise HTTPException(400, f"detail.items[{idx}].template_id invalid (alnum+_ only)")
            stack = it.get("stack_size")
            if not isinstance(stack, int) or stack < 1 or stack > MAX_QUANTITY:
                raise HTTPException(400, f"detail.items[{idx}].stack_size must be int 1..{MAX_QUANTITY}")
            quality = it.get("quality", 0)
            if not isinstance(quality, int) or quality < 0 or quality > 5:
                raise HTTPException(400, f"detail.items[{idx}].quality must be int 0..5")
            _validate_item_grade(f"detail.items[{idx}]", tpl, quality, stack, give_index)
            out_items.append({"template_id": tpl, "stack_size": stack, "quality": quality})
        return {"target_inv": target_inv, "items": out_items}

    raise HTTPException(400, f"unsupported grant_type: {grant_type}")


@router.get("/grant/vehicle-packages")
async def grant_vehicle_packages(request: Request):
    """Curated vehicle-build package bundles for the container_items_batch picker.
    Admin-gated read of data/vehicle-packages.json."""
    require_admin(request)
    try:
        from vehicle_packages import load_packages
        return {"available": True, "packages": load_packages()}
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the panel
        raise HTTPException(500, f"failed to load vehicle packages: {exc}")


@router.get("/grant/catalog")
async def grant_catalog(request: Request):
    """Serve the version-controlled grant catalog (file read, in-memory cache).
    Admin-only — entry labels and ids are operational, not public."""
    require_admin(request)
    return _load_catalog()


@router.get("/grant/keystones")
async def grant_keystones(request: Request):
    """Serve the keystone catalog sidecar. 205 entries grouped by track,
    SP-bonus nodes ordered first within each track. Used by the keystone
    grant form to render a human-readable dropdown instead of a bare id input."""
    require_admin(request)
    try:
        mtime = os.path.getmtime(_KEYSTONE_CATALOG_PATH)
    except OSError:
        raise HTTPException(500, "Keystone catalog file not found")
    cached = _keystone_catalog_cache["data"]
    if cached is not None and mtime == _keystone_catalog_cache["mtime"]:
        return cached
    try:
        with open(_KEYSTONE_CATALOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "Keystone catalog file is unreadable")
    _keystone_catalog_cache["data"] = data
    _keystone_catalog_cache["mtime"] = mtime
    return data


@router.get("/grant/players")
async def grant_players(request: Request):
    """Player picker source — ALL characters including OFFLINE ones. Character
    names are PII, so this is admin-only. Result cached ~60s like the roster."""
    require_admin(request)

    now = time.monotonic()
    cached = _players_cache["data"]
    if cached is not None and now - _players_cache["fetched_at"] < PLAYERS_CACHE_TTL:
        return cached

    try:
        raw = await call_relay("/dune/grant/players", timeout=45)
    except Exception:
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "players": []}

    players = []
    for p in (raw.get("players") or []):
        players.append({
            "account_id": p.get("account_id"),
            "name": p.get("name"),
            "funcom_id": p.get("funcom_id"),
            "online_status": p.get("online_status"),
            "last_login_time": p.get("last_login_time"),
            "in_grace_period": bool(p.get("in_grace_period")),
        })

    data = {
        "available": bool(raw.get("available", True)),
        "players": players,
        "active_houses": raw.get("active_houses") or [],
    }
    _players_cache["data"] = data
    _players_cache["fetched_at"] = now
    return data


# RMQ live-delivery routing. A subset of grant types map cleanly onto the native
# server-command verbs for instant in-world delivery to ONLINE players, vs the
# offline DB write (which the live RAM state clobbers). Only base items and
# character XP qualify: AddItemToInventory has no grade field (graded items stay
# on the DB path), and AwardXP on an online player runs the game's native
# leveling (credits skill points in-world, matching what the DB path simulates).
RMQ_LIVE_REASON = "panel live grant"

# Grant types the UI may offer "deliver live" for (advisory; _rmq_plan is the
# authoritative eligibility gate — it also rejects graded items at fire time).
RMQ_ELIGIBLE_GRANT_TYPES = ("item", "char_xp")


def _rmq_plan(grant_type: str, detail: dict) -> tuple[str, dict] | None:
    """(verb, args) for an RMQ-deliverable grant, or None to take the DB path.
    Defensive against missing keys (un-validated preset ops fall back to DB)."""
    # Total by construction — an unvalidated preset op must never raise out of a
    # batch loop; on any malformed field, return None to fall back to the DB path.
    try:
        if grant_type == "item":
            tpl = detail.get("template_id")
            qty = detail.get("quantity")
            if not tpl or qty is None:
                return None
            # Graded items can't be delivered live — give-item has no grade arg.
            if int(detail.get("quality", 0) or 0) != 0:
                return None
            return ("give-item", {"item": tpl, "qty": int(qty), "durability": 1.0})
        if grant_type == "char_xp":
            amt = detail.get("amount")
            if amt is None:
                return None
            # Category is ignored by the live server; experience drives native level-up.
            return ("award-xp", {"category": "Combat", "experience": int(amt)})
    except (TypeError, ValueError):
        return None
    return None


async def _maybe_fire_rmq(
    user: dict, ip: str, account_id: int, grant_type: str, detail: dict,
    idempotency_key: str, mode: str, batch_id, preset_name,
) -> dict | None:
    """Attempt live RMQ delivery iff the grant is eligible AND the player is
    online. Returns a result dict on a live attempt (success OR failure), or None
    to tell the caller to fall back to the offline DB path. Audits live attempts
    under action 'dune_grant' with via:'rmq'."""
    plan = _rmq_plan(grant_type, detail)
    if plan is None:
        return None
    if not await is_online(account_id):
        return None
    verb, args = plan

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dune_grant", str(account_id), ip,
            details=json.dumps({
                "grant_type": grant_type, "detail": detail,
                "idempotency_key": idempotency_key, "mode": mode,
                "via": "rmq", "verb": verb, "args": args,
                "batch_id": batch_id, "preset_name": preset_name, **extra,
            }),
            success=success,
        )

    resolve_ref = await resolve_fls_ref(account_id)
    if not resolve_ref:
        _audit(False, {"result": "error: could not resolve FLS id"})
        return {"success": False, "status": "failed",
                "message": "could not resolve this account's FLS id — refusing live send",
                "grant_id": None}
    try:
        res = await dispatch_server_command(
            resolve_ref, verb, mode=mode, operator=user["username"],
            reason=RMQ_LIVE_REASON, args=args,
        )
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        return {"success": False, "status": "failed",
                "message": str(exc.detail)[:2000], "grant_id": None}
    except Exception as exc:
        _audit(False, {"result": f"error: {exc}"})
        return {"success": False, "status": "failed",
                "message": str(exc)[:2000], "grant_id": None}

    success = bool(res.get("success"))
    _audit(success, {"result": (res.get("detail") or "")[:2000]})
    return {
        "success": success,
        "status": "live" if success else "failed",
        "message": (res.get("detail") or "")[:2000],
        "grant_id": None,
    }


# --- In-world Cielago whisper to an ONLINE grant recipient -------------------
# A cielago is the Fremen distrans messenger bat, so a whisper "from Cielago"
# carrying word fits the lore. Online-safe grants render only after the client
# reloads (relog / zone change); RAM-fragile grants that hit an online player
# are deferred to their next logout->login. Either way the player would not see
# it immediately, so we nudge them. resolve_fls_ref returns a FuncomId ONLY for
# an online player, so it doubles as the online gate; offline recipients get
# nothing (they see the grant on next login). Best-effort: a failure here NEVER
# affects the grant result.
_CIELAGO_GRANT_WHISPER = {
    "applied": (
        "A cielago brings word from the stewards of the sietch: a gift now rests in "
        "your keeping. Cross a threshold or return to the desert anew, by changing "
        "zones or relogging, and it shall reveal itself. The desert provides."
    ),
    "deferred": (
        "A cielago brings word from the stewards: a boon is promised to you, yet the "
        "sands will not settle while you walk them. Rest beyond the desert and return, "
        "by logging out and back in, and it shall be yours. The Maker keeps what is promised."
    ),
}


async def _cielago_grant_whisper(account_id: int, status: str, operator: str) -> None:
    """Best-effort in-game Cielago whisper telling an online recipient to relog.
    No-op (and never raises) if the player is offline or the herald is down."""
    msg = _CIELAGO_GRANT_WHISPER.get(status)
    if not msg:
        return
    try:
        funcom_id = await resolve_fls_ref(account_id)
        if not funcom_id:
            return  # offline / unresolvable -> they will see the grant on next login
        await call_relay(
            "/dune/chat/send", "POST",
            {"scope": "whisper", "recipient": funcom_id, "message": msg,
             "mode": "apply", "operator": f"cielago-grant/{operator}"},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - notification must never flip a grant
        logger.warning("cielago grant whisper failed for account %s: %s", account_id, exc)


async def _execute_one_grant(
    user: dict,
    ip: str,
    account_id: int,
    grant_type: str,
    detail_in: dict,
    idempotency_key: str,
    mode: str = "apply",
    defer_if_online: bool = False,
    confirm_disabled_ui: int = 0,
    batch_id: str | None = None,
    preset_name: str | None = None,
    live_delivery: bool = False,
) -> dict:
    """Validate + execute a single progression grant in-process and return the
    result dict ({success, status, message, grant_id}).

    This is the shared executor behind POST /grant and the V2 cart fire path.
    Validation failures raise HTTPException(4xx) (audited first); execution
    failures are audited and returned as success=False dicts (they do NOT
    raise). Callers that want continue-on-error semantics wrap the call in
    try/except HTTPException to capture the validation-failure case.

    When ``batch_id`` is provided, a best-effort postprocess call stamps
    batch_id (+ optional preset_name) onto the audit row after a successful
    grant. POST /grant passes neither, so its behavior is unchanged.

    ``confirm_disabled_ui`` is the SQL-recovery escape hatch for G25b
    (set_starter_class). UI cards never set it; only an operator running curl
    by hand may pass ``?confirm_disabled_ui=1``."""
    # --- Server-side validation (failures are audited too) ---
    try:
        if mode not in ("apply", "dry-run"):
            raise HTTPException(400, "mode must be 'apply' or 'dry-run'")
        try:
            uuid.UUID(idempotency_key)
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "idempotency_key must be a UUID")
        if account_id <= 0:
            raise HTTPException(400, "account_id must be a positive integer")

        # G25b — UI disabled per spec §G25b DEFERRED notice. The grant_type
        # ALSO is not in the catalog (the membership check below would reject
        # it after this gate), so this branch only fires for direct-curl
        # SQL-recovery use AND only succeeds with the explicit query flag.
        if grant_type == "set_starter_class":
            if confirm_disabled_ui != 1:
                raise HTTPException(
                    400,
                    "set_starter_class UI is disabled — append "
                    "?confirm_disabled_ui=1 to acknowledge SQL-recovery use",
                )

        catalog = _load_catalog()
        # set_starter_class is intentionally absent from catalog (UI hidden);
        # accept it iff the confirm flag is set so the bash builder is still
        # reachable for SQL-level recovery.
        known_ids = _grant_type_ids(catalog)
        if grant_type == "set_starter_class":
            known_ids = known_ids | {"set_starter_class"}
        if grant_type not in known_ids:
            raise HTTPException(400, f"unknown grant_type: {grant_type}")
        detail = _validate_detail(catalog, grant_type, detail_in)
        # G21 / G22 slot-count enforcement (3-cap is C++-side at runtime; we
        # refuse at API edge so the UI never lands a 4th row). v1.5 will add
        # an --overwrite-slot mode that lets G22 atomically reclaim a slot;
        # until then G22 mirrors G21's refuse-at-cap behavior.
        if grant_type in ("bb_handoff", "bb_clone", "import_solido_to_basebackup"):
            recipient = detail["recipient_account_id"]
            try:
                sc_raw = await call_relay(
                    f"/dune/bb/slot-count?account_id={recipient}", timeout=15)
            except HTTPException as exc:
                raise HTTPException(
                    502, f"unable to query recipient slot count: {exc.detail}")
            sc_result = sc_raw.get("result") if isinstance(sc_raw, dict) else None
            slot_count = (sc_result or {}).get("slot_count") if isinstance(sc_result, dict) else None
            if not isinstance(slot_count, int) or slot_count < 0:
                raise HTTPException(
                    502, "slot-count relay response shape unexpected")
            if slot_count >= MAX_BB_SLOTS:
                raise HTTPException(
                    400,
                    f"recipient already has {slot_count} base backups "
                    f"(max {MAX_BB_SLOTS}); ask them to free a slot first",
                )
            detail["recipient_prior_slot_count"] = slot_count
    except HTTPException as exc:
        audit_log(
            user["id"], user["username"], "dune_grant",
            str(account_id), ip,
            details=json.dumps({
                "grant_type": grant_type,
                "detail": detail_in,
                "idempotency_key": idempotency_key,
                "result": f"validation_error: {exc.detail}",
                "result_message": str(exc.detail)[:2000],
            }),
            success=False,
        )
        raise

    # --- Live-delivery routing: when requested, RMQ-deliver to an online player
    # for an RMQ-eligible grant; otherwise fall through to the offline DB path. ---
    if live_delivery:
        live = await _maybe_fire_rmq(
            user, ip, account_id, grant_type, detail,
            idempotency_key, mode, batch_id, preset_name,
        )
        if live is not None:
            return live

    relay_body = {
        "account_id": account_id,
        "grant_type": grant_type,
        "detail": detail,
        "idempotency_key": idempotency_key,
        "operator": user["username"],
        "mode": mode,
        "defer_if_online": defer_if_online,
    }

    # --- Execute via relay (generous timeout so a slow kubectl exec does not
    # report failure on a committed grant — Section 7 / R4). ---
    try:
        result = await call_relay("/dune/grant", "POST", relay_body, timeout=60)
        audit_log(
            user["id"], user["username"], "dune_grant",
            str(account_id), ip,
            details=json.dumps({
                "grant_type": grant_type,
                "detail": detail,
                "idempotency_key": idempotency_key,
                "mode": mode,
                "result": result.get("status", "ok"),
                "result_message": (result.get("message") or "")[:2000],
                "grant_id": result.get("grant_id"),
            }),
            success=bool(result.get("success", True)),
        )
        grant_id = result.get("grant_id")
        # Best-effort batch tagging. A failed UPDATE leaves batch_id/preset_name
        # off the audit row but the grant itself is applied; DO NOT raise.
        if batch_id and isinstance(grant_id, int):
            # Best-effort: the grant + its success audit already happened. A
            # postprocess failure (including raw httpx transport errors from
            # call_relay, not just HTTPException) must never flip the result.
            try:
                await call_relay(
                    "/dune/grant/postprocess",
                    "POST",
                    {"grant_id": grant_id, "batch_id": batch_id, "preset_name": preset_name},
                    timeout=15,
                )
            except Exception as exc:
                logger.warning("postprocess failed for grant_id=%s: %s", grant_id, exc)
        # In-world Cielago nudge to an online recipient that they must relog/zone
        # to see the grant (or that a deferred grant is queued). Best-effort: it
        # never affects the result. Skipped for dry-run and for replays.
        if mode == "apply" and result.get("success") and result.get("status") in ("applied", "deferred"):
            await _cielago_grant_whisper(account_id, result.get("status"), user["username"])
        return {
            "success": bool(result.get("success", True)),
            "status": result.get("status"),
            "message": result.get("message", ""),
            "grant_id": grant_id,
        }
    except HTTPException as exc:
        audit_log(
            user["id"], user["username"], "dune_grant",
            str(account_id), ip,
            details=json.dumps({
                "grant_type": grant_type,
                "detail": detail,
                "idempotency_key": idempotency_key,
                "mode": mode,
                "result": f"error: {exc.detail}",
                "result_message": str(exc.detail)[:2000],
            }),
            success=False,
        )
        return {"success": False, "status": "failed", "message": str(exc.detail), "grant_id": None}
    except Exception as exc:
        audit_log(
            user["id"], user["username"], "dune_grant",
            str(account_id), ip,
            details=json.dumps({
                "grant_type": grant_type,
                "detail": detail,
                "idempotency_key": idempotency_key,
                "mode": mode,
                "result": f"error: {exc}",
                "result_message": str(exc)[:2000],
            }),
            success=False,
        )
        return {"success": False, "status": "failed", "message": str(exc), "grant_id": None}


@router.post("/grant")
async def grant(request: Request, body: GrantRequest, confirm_disabled_ui: int = 0):
    """Execute one progression grant. Admin + CSRF gated; audit-logged on both
    the success and failure paths (rcon.py pattern). Thin wrapper over the
    shared in-process executor; behavior is unchanged.

    ``confirm_disabled_ui`` is the SQL-recovery escape hatch for G25b
    (set_starter_class). UI cards never set it; only an operator running curl
    by hand may pass ``?confirm_disabled_ui=1``. The audit row is stamped with
    ``via_disabled_ui: true`` so the use is grep-able in the ledger."""
    user = require_admin(request)
    require_csrf(request, user)

    ip = request.client.host if request.client else "unknown"
    return await _execute_one_grant(
        user, ip, body.account_id, body.grant_type, body.detail,
        body.idempotency_key, body.mode, body.defer_if_online, confirm_disabled_ui,
        live_delivery=body.live_delivery,
    )


@router.get("/bb/available-sources")
async def bb_available_sources(request: Request):
    """G21 — list every saved base_backup row joined to its totem actor. Drives
    the bb_handoff / bb_clone source-picker dropdown. Same auth pattern as
    /grant/keystones (admin-only; the source list is operational state)."""
    require_admin(request)
    try:
        raw = await call_relay("/dune/bb/available-sources", timeout=30)
    except HTTPException as exc:
        raise HTTPException(
            502, f"relay error fetching base backup sources: {exc.detail}")
    sources = []
    for s in (raw.get("sources") or []):
        sources.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "player_id": s.get("player_id"),
            "totem_actor_id": s.get("totem_actor_id"),
            "linked_actor_count": s.get("linked_actor_count"),
        })
    return {
        "available": bool(raw.get("available", True)),
        "sources": sources,
    }


@router.get("/bb/slot-count")
async def bb_slot_count(request: Request, account_id: int):
    """G21 — count of base_backups rows owned by an account. The slot cap (3)
    is enforced game-server C++ side; we surface this so the UI can warn at 2
    and refuse at 3 before submit."""
    require_admin(request)
    if account_id <= 0:
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        raw = await call_relay(
            f"/dune/bb/slot-count?account_id={account_id}", timeout=15)
    except HTTPException as exc:
        raise HTTPException(
            502, f"relay error fetching slot count: {exc.detail}")
    result = raw.get("result") if isinstance(raw, dict) else None
    count = (result or {}).get("slot_count") if isinstance(result, dict) else None
    if not isinstance(count, int) or count < 0:
        raise HTTPException(502, "slot-count relay response shape unexpected")
    return {"account_id": account_id, "count": count, "max": MAX_BB_SLOTS}


@router.get("/grant/recent")
async def grant_recent(request: Request, limit: int = RECENT_GRANTS_DEFAULT):
    """Recent-grants panel — last N rows of dune.ls_progression_grants, fetched
    via the relay (the grant ledger lives in the game's Postgres, not SQLite)."""
    require_admin(request)
    limit = min(max(limit, 1), RECENT_GRANTS_MAX)

    try:
        raw = await call_relay(f"/dune/grant/recent?limit={limit}", timeout=45)
    except Exception:
        return {"available": False, "grants": []}

    return {
        "available": bool(raw.get("available", True)),
        "grants": raw.get("grants", []) or [],
    }
