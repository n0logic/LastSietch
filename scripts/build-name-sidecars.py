#!/usr/bin/env python3
"""Build per-namespace friendly-name sidecars from the extracted pak string
tables. Each namespace ships its own JSON to keep individual files under the
500KB per-file budget called out in architect P4-EXECUTION-BRIEF §7.

Source: ~/Source/Security/DunePakRE/extracted/string_table_lookup.json
        (27k+ entries keyed like ITEMS/WEAPON_FOO_NAME).

Outputs (admin-backend/data/):
    dune-item-template-names.json       (existing, 488KB — unchanged)
    dune-building-names.json            (new, ~50KB)
    dune-skill-names.json               (new, ~25KB)
    dune-lore-names.json                (new, ~20KB)
    dune-progression-names.json         (new, ~8KB)
    dune-communinet-names.json          (new, ~12KB)

Each sidecar is a flat {alias_lowercase: display_name} dict. Runtime lookup
lives in admin-backend/name_lookups.py — one NameSidecar instance per
namespace, all loaded once at startup.

Idempotent — safe to re-run. Each invocation overwrites the sidecar in
place (no implicit backups; commit produced JSONs).

Per-namespace peel lists were derived by sampling 50 rows + leading-token
counter audit per architect §7 directive ("MUST inspect a 50-row sample
from each namespace first; do not hardcode without checking"). They differ
from the architect's first-guess lists in the brief — observed pak data
overrides the spec where it diverged.

CLI:
    python3 scripts/build-name-sidecars.py                 # all
    python3 scripts/build-name-sidecars.py --namespace items
    python3 scripts/build-name-sidecars.py --namespace buildings
    ...

LIFT-11 follow-up notes (file as v1.1 backlog):
    - ScrapMetal / MetalOre / IceBlock / etc. raw resource keys NEVER
      appear as _NAME entries in the pak. A camelCase-split fallback
      applied AFTER dict misses in name_lookups (e.g. `ScrapMetal` ->
      "Scrap Metal") would catch these — out of scope for the pak-derived
      sidecar itself.
"""
import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

SRC = Path.home() / "Source/Security/DunePakRE/extracted/string_table_lookup.json"
DATA_DIR = Path(__file__).parent.parent / "admin-backend/data"
SIZE_BUDGET_BYTES = 500_000  # hard cap per architect §5 / §7


@dataclass(frozen=True)
class NamespaceSpec:
    name: str                          # CLI handle, e.g. "items"
    src_namespace: str                  # pak prefix, e.g. "ITEMS"
    src_suffix: str                     # pak suffix, e.g. "_NAME"
    peelable_prefixes: tuple[str, ...]  # leading stem tokens to strip
    peelable_suffixes: tuple[str, ...]  # trailing stem tokens to strip
    dest_filename: str                  # output file under DATA_DIR
    # Legacy ITEMS sidecar keeps original behavior: drop XX_/WIP values
    # outright, emit compact only for shortest peeled form. New per-namespace
    # sidecars opt into the broader recovery path (strip Funcom WIP value
    # prefixes; emit dual compact for both full-stem and shortest-peel).
    strip_value_prefixes: bool = False
    emit_dual_compact: bool = False

    @property
    def dest_path(self) -> Path:
        return DATA_DIR / self.dest_filename


# Per-namespace peel lists derived from 50-row sample + leading-token
# counter audit (2026-05-25). Order within each tuple matters — longest /
# most-specific prefix first so peel-loop strips them before short
# overlapping forms (e.g. BUILDING_BLUEPRINT_ before BUILDING_).
NAMESPACES: tuple[NamespaceSpec, ...] = (
    NamespaceSpec(
        name="items",
        src_namespace="ITEMS",
        src_suffix="_NAME",
        peelable_prefixes=(
            "WEAPON_", "ARMOR_", "RESOURCE_", "SCHEMATIC_", "COMBAT_",
            "RCP_", "TOOL_", "VEHICLE_", "CONSUMABLE_", "AMMO_",
            "T1_", "T2_", "T3_", "T4_", "T5_", "T6_", "T7_",
        ),
        peelable_suffixes=("_SCHEMATIC",),
        dest_filename="dune-item-template-names.json",
        # Legacy mode — keep ITEMS sidecar bit-for-bit identical to the
        # original build-item-template-names.py output (488KB / 8221 aliases)
        # per architect §7 "(existing — unchanged)".
        strip_value_prefixes=False,
        emit_dual_compact=False,
    ),
    NamespaceSpec(
        name="buildings",
        src_namespace="BUILDINGS",
        src_suffix="_NAME",
        # Audit: VEHICLE_=354, BUILDING_=238 — vehicles ride in BUILDINGS
        # namespace because all-placeable. BUILDING_SET_MTX_ before
        # BUILDING_SET_ before BUILDING_ so peel matches longest first.
        peelable_prefixes=(
            "VEHICLE_",
            "BUILDING_BLUEPRINT_", "BUILDING_SET_MTX_",
            "BUILDING_SET_", "BUILDING_",
            "MTX_",
        ),
        peelable_suffixes=("_PLACEABLE", "_PATENT"),
        dest_filename="dune-building-names.json",
        strip_value_prefixes=True,
        emit_dual_compact=True,
    ),
    NamespaceSpec(
        name="skills",
        src_namespace="SKILLS",
        src_suffix="_NAME",
        # Audit: ABILITIES_=78, ATTRIBUTE_=72, STAT_=66, TECHNIQUE_=36,
        # SPICE_=13. Architect's PASSIVE_/MENTOR_ guesses don't appear.
        peelable_prefixes=(
            "ABILITIES_", "ATTRIBUTE_", "STAT_", "TECHNIQUE_", "SPICE_",
        ),
        peelable_suffixes=(),
        dest_filename="dune-skill-names.json",
        strip_value_prefixes=True,
        emit_dual_compact=True,
    ),
    NamespaceSpec(
        name="lore",
        src_namespace="LORE_PICKUPS_AND_CONTRACTS",
        src_suffix="_NAME",
        # Audit: CONTRACT_=201, JOURNEY_=9. Architect's BENEGESSERIT_/
        # CONDITION_/REWARD_ live on _TITLE/_CONDITION1 suffixes, not _NAME.
        peelable_prefixes=("CONTRACTS_", "CONTRACT_", "JOURNEY_"),
        peelable_suffixes=(),
        dest_filename="dune-lore-names.json",
        strip_value_prefixes=True,
        emit_dual_compact=True,
    ),
    NamespaceSpec(
        name="progression",
        src_namespace="PROGRESSION",
        src_suffix="_NAME",
        # Audit: KEYSTONE_=76 (all). ACT2_/TASK_TITLE_ live on _TITLE.
        peelable_prefixes=("KEYSTONE_",),
        peelable_suffixes=(),
        dest_filename="dune-progression-names.json",
        strip_value_prefixes=True,
        emit_dual_compact=True,
    ),
    NamespaceSpec(
        name="communinet",
        src_namespace="COMMUNINET",
        src_suffix="_NAME",
        # Audit: COMMUNINET_=98, RADIOCHANNEL_=5.
        peelable_prefixes=("COMMUNINET_", "RADIOCHANNEL_"),
        peelable_suffixes=(),
        dest_filename="dune-communinet-names.json",
        strip_value_prefixes=True,
        emit_dual_compact=True,
    ),
)


# Value-side prefixes Funcom uses on WIP / placeholder strings. We strip
# these from the display string instead of dropping the entry — most have
# a real English name after the prefix (e.g. "XX_Duraluminum Maula Pistol").
VALUE_STRIP_PREFIXES = (
    "XX_NOTUSED_",
    "XX_NOTUSED",  # trailing-letter form: "XX_NOTUSEDT6 Maula Pistol"
    "XX_Learnable Schematic Set ",
    "XX_Learnable Schematic ",
    "XX_Cut_", "XX_Cut ",
    "XX_CUT_", "XX_CUT ",
    "XX_QA_", "XX_QA ",
    "XX_NU_",
    "XX_NPC ",
    "XX_NOT_",
    "XX_",
    "PH_",
)

NOISE_VALUES = frozenset({
    "", "WIP", "XX_", "PH_", "TBD", "TODO", "REMOVE", "REMOVED",
    "DEPRECATED", "TEST", "TEST NAME", "EMPTY_TEXT",
})

# All-uppercase token (debug self-reference like "WEAPON_FOO_NAME").
_DEBUG_RE = re.compile(r"^[A-Z][A-Z0-9_\-]*$")


def _strip_value_prefixes(v: str) -> str:
    s = v
    changed = True
    while changed:
        changed = False
        for p in VALUE_STRIP_PREFIXES:
            if s.startswith(p):
                s = s[len(p):]
                changed = True
                break
    return s.strip()


def _peel_prefixes(stem: str, prefixes: tuple[str, ...]) -> list[str]:
    out = [stem]
    cur = stem
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if cur.startswith(p) and len(cur) > len(p) + 1:
                cur = cur[len(p):]
                out.append(cur)
                changed = True
                break
    return out


def _peel_suffixes(stem: str, suffixes: tuple[str, ...]) -> list[str]:
    out = [stem]
    cur = stem
    for suf in suffixes:
        if cur.endswith(suf) and len(cur) > len(suf) + 1:
            cur = cur[: -len(suf)]
            out.append(cur)
    return out


def aliases_for(stem: str, spec: NamespaceSpec) -> list[str]:
    """Cartesian peel chain + compact emit. Legacy mode (ITEMS) emits
    compact only for the shortest peeled form, matching original
    build-item-template-names.py output. New mode emits compact for both
    the full stem AND the shortest peel — catches DBs that use either
    `Augment_Armor1` (full stem) or `Armor1` (peeled) without exploding
    alias count."""
    seen: set[str] = set()
    out: list[str] = []
    peeled_chain: list[str] = []
    for after_pre in _peel_prefixes(stem, spec.peelable_prefixes):
        for variant in _peel_suffixes(after_pre, spec.peelable_suffixes):
            peeled_chain.append(variant.lower())
    for cand in peeled_chain:
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    if peeled_chain:
        pickers = (max, min) if spec.emit_dual_compact else (min,)
        for picker in pickers:
            picked = picker(peeled_chain, key=len)
            compact = picked.replace("_", "")
            if compact and compact not in seen:
                seen.add(compact)
                out.append(compact)
    return out


def _is_acceptable(stem: str, sanitized: str, spec: NamespaceSpec) -> bool:
    if not sanitized or sanitized in NOISE_VALUES:
        return False
    if spec.strip_value_prefixes:
        # Stricter filter only in new-mode namespaces. Legacy ITEMS skips
        # these — keeps bit-for-bit parity with the original sidecar.
        if _DEBUG_RE.match(sanitized):
            return False
        if not any(c.isalpha() for c in sanitized):
            return False
        # Reject values that just echo the stem (Funcom-untranslated like
        # COMMUNINET_BASEPROFILEINVITENOTIFICATION_NAME ->
        # "BaseProfileInviteNotification").
        if stem.replace("_", "").lower() == sanitized.replace(" ", "").replace("_", "").lower():
            return False
    return True


def _skip_key(ku: str) -> bool:
    # Generic noisy-key patterns; namespaces narrow on src_suffix already.
    if "LONGDESC" in ku or "SHORTDESC" in ku or "PARTSALE" in ku:
        return True
    if "SWATCH" in ku or "SETVARIANT" in ku:
        return True
    return False


def build_one(raw: dict, spec: NamespaceSpec) -> tuple[int, int, int]:
    """Returns (alias_count, source_entry_count, skipped_noise)."""
    names: dict[str, str] = {}
    seen_keys = 0
    skipped = 0
    ns_prefix = spec.src_namespace + "/"
    for k, v in raw.items():
        if not k.startswith(ns_prefix) or not k.endswith(spec.src_suffix):
            continue
        if _skip_key(k.upper()):
            continue
        stem = k[len(ns_prefix):-len(spec.src_suffix)]
        if not stem:
            continue
        display_raw = str(v).strip() if v is not None else ""
        # Legacy ITEMS mode drops XX_/WIP outright; new mode strips the
        # Funcom WIP prefix and admits the underlying real string.
        if spec.strip_value_prefixes:
            display = _strip_value_prefixes(display_raw)
        else:
            if (not display_raw or display_raw.startswith("XX_")
                    or display_raw == "WIP"):
                skipped += 1
                continue
            display = display_raw
        if not _is_acceptable(stem, display, spec):
            skipped += 1
            continue
        seen_keys += 1
        for alias in aliases_for(stem, spec):
            # Most-specific alias wins (first writer).
            names.setdefault(alias, display)

    spec.dest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(names, separators=(",", ":"), sort_keys=True)
    spec.dest_path.write_text(payload, encoding="utf-8")
    size = spec.dest_path.stat().st_size
    flag = " OVER BUDGET" if size > SIZE_BUDGET_BYTES else ""
    print(f"  [{spec.name:11}] {len(names):>6} aliases / {seen_keys:>5} entries "
          f"({size:>7} bytes; {skipped:>4} noise skipped){flag}")
    return len(names), seen_keys, skipped


def main(selected: str) -> int:
    if not SRC.exists():
        raise SystemExit(f"source not found: {SRC}")
    raw = json.loads(SRC.read_text(encoding="utf-8"))
    chosen = NAMESPACES if selected == "all" else tuple(
        s for s in NAMESPACES if s.name == selected
    )
    if not chosen:
        valid = ", ".join(s.name for s in NAMESPACES) + ", all"
        raise SystemExit(f"unknown --namespace {selected!r}; valid: {valid}")
    print(f"building {len(chosen)} sidecar(s) from {SRC}")
    total_aliases = 0
    total_size = 0
    over_budget = 0
    for spec in chosen:
        aliases, _, _ = build_one(raw, spec)
        total_aliases += aliases
        total_size += spec.dest_path.stat().st_size
        if spec.dest_path.stat().st_size > SIZE_BUDGET_BYTES:
            over_budget += 1
    print(f"total: {total_aliases} aliases across {len(chosen)} sidecar(s) "
          f"({total_size} bytes on disk; {over_budget} over per-file budget)")
    return 0 if over_budget == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--namespace", default="all",
        help="one of: " + ", ".join(s.name for s in NAMESPACES) + ", all",
    )
    args = p.parse_args()
    raise SystemExit(main(args.namespace))
