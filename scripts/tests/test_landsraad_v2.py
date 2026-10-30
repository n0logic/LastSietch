#!/usr/bin/env python3
"""Regression tests for the V2 Landsraad backend: the sibling JSON endpoint
`GET /portal/landsraad/v2` (admin-backend/routers/portal.py) and the swatch-chip
attach (exact placeable dyes from the LUT, house palette proxy as the fallback).

Prod-safe: NO real DB, NO network. The endpoint layer is asserted by scanning the
source. The swatch logic is exercised for real by extracting the pure swatch
functions from portal.py via AST and exec-ing them with a minimal set of globals
(so we test the SHIPPING code, not a copy) against the REAL static/data/
swatch-lut.json + data.house_reps.

Run:  python3 scripts/tests/test_landsraad_v2.py     (also import-safe)
"""
import ast
import json
import logging
import os
import re
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
ADMIN = os.path.join(REPO, "admin-backend")
PORTAL = os.path.join(ADMIN, "routers", "portal.py")
LUT = os.path.join(ADMIN, "static", "data", "swatch-lut.json")

if ADMIN not in sys.path:
    sys.path.insert(0, ADMIN)
from data.house_reps import HOUSE_REP_LOCATIONS  # noqa: E402  (pure data module)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Extract the real swatch functions from portal.py (no heavy import)
# --------------------------------------------------------------------------- #

_SWATCH_NAMES = {
    "_house_base", "_LANDSRAAD_SWATCH_ALIAS", "_SWATCH_LUT_CACHE",
    "_LANDSRAAD_SWATCH_RE", "_LANDSRAAD_SWATCH_PREFIX", "_swatch_lut_hexes",
    "_swatch_lut_by_house", "_swatch_chips_for_house", "_swatch_for_reward",
    "_attach_board_swatches", "_attach_rewards_swatches",
}


def _load_swatch_ns():
    tree = ast.parse(_read(PORTAL))

    def _named(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
        if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        return None

    wanted = [n for n in tree.body if _named(n) in _SWATCH_NAMES]
    got = {_named(n) for n in wanted}
    missing = _SWATCH_NAMES - got
    assert not missing, f"missing swatch defs in portal.py: {missing}"
    mod = ast.Module(body=wanted, type_ignores=[])
    ns = {
        "json": json, "_os": os, "re": re, "Optional": Optional,
        "logger": logging.getLogger("test"),
        "HOUSE_REP_LOCATIONS": HOUSE_REP_LOCATIONS,
        # __file__ must resolve to admin-backend/routers/portal.py so the LUT
        # path (dirname(dirname(__file__))/static/data/...) lands on the real file.
        "__file__": PORTAL,
    }
    exec(compile(mod, PORTAL, "exec"), ns)
    return ns


# --------------------------------------------------------------------------- #
# Endpoint presence + shape
# --------------------------------------------------------------------------- #

def test_landsraad_v2_route_present():
    assert '@router.get("/portal/landsraad/v2")' in _read(PORTAL)


def test_landsraad_standings_untouched():
    # The public V2-dashboard dependency must stay a sibling, unchanged.
    src = _read(PORTAL)
    assert '@router.get("/portal/landsraad/standings")' in src


def test_landsraad_v2_envelope_keys():
    src = _read(PORTAL)
    block = src.split("V2 MODULE PORTS (2026-07-15)", 1)[1]
    h = block[block.index("async def portal_landsraad_v2("):]
    assert "return _v2_ok({" in h
    for key in ('"board":', '"rewards":', '"active_character_name":',
                '"rewards_error":'):
        assert key in h, key


def test_landsraad_v2_board_rewards_independence():
    # Board must degrade to None without taking down the rewards half (two
    # separate try/except blocks; rewards is computed first and returned even if
    # the board raises).
    src = _read(PORTAL)
    h = src.split("async def portal_landsraad_v2(", 1)[1].split("\n@router", 1)[0]
    assert "board = None" in h
    assert 'logger.warning("portal: landsraad/v2 board failed' in h
    assert 'logger.warning("portal: landsraad/v2 rewards failed' in h
    # rewards_error is keyed to the rewards half only.
    assert '"rewards_error": rewards is None' in h


def test_landsraad_v2_preserves_viewer_faction_scoping():
    src = _read(PORTAL)
    h = src.split("async def portal_landsraad_v2(", 1)[1].split("\n@router", 1)[0]
    # viewer faction resolved from progress and threaded into the board shaper
    # (opposing faction's exact numbers never leave the server).
    assert 'fac.get("faction_id") in (1, 2)' in h
    assert "viewer_faction_id = int(fac[\"faction_id\"])" in h
    assert "my_contrib_by_raw or None, viewer_faction_id)" in h


# --------------------------------------------------------------------------- #
# Swatch LUT join: all 25 Landsraad houses resolve to hex chips
# --------------------------------------------------------------------------- #

def test_all_houses_resolve_to_hex_chips():
    ns = _load_swatch_ns()
    lut = ns["_swatch_lut_by_house"]()
    base = ns["_house_base"]
    unresolved = []
    for raw in HOUSE_REP_LOCATIONS:
        chips = lut.get(base(raw).lower())
        if not chips:
            unresolved.append(base(raw))
            continue
        for c in chips:
            assert c.startswith("#") and len(c) == 7, (raw, c)
    assert not unresolved, f"houses with no proxy palette: {unresolved}"
    assert len(lut) == 25


def test_spelling_drift_aliases_present():
    ns = _load_swatch_ns()
    alias = ns["_LANDSRAAD_SWATCH_ALIAS"]
    # The 3 known drifts between data.house_reps and the dyepack LUT.
    assert alias["Argosaz"] == "Agrosaz"
    assert alias["Mikkarol"] == "Mikarrol"
    assert alias["Taligari"] == "Talgari"


# --------------------------------------------------------------------------- #
# Swatch attach: only Swatch template ids get chips; exact dyes when the LUT has
# the dyepack the template unlocks, house palette proxy otherwise
# --------------------------------------------------------------------------- #

CURATED = os.path.join(ADMIN, "data", "dune-item-template-names-curated.json")


def _lut_hex(key):
    with open(LUT, "r", encoding="utf-8") as fh:
        entry = json.load(fh)["swatches"][key]
    return ["#" + h for h in entry["hex"]]


def test_board_swatch_attach_only_on_swatch_tids():
    ns = _load_swatch_ns()
    shaped = {"tiles": [{
        "board_index": 3, "short": "Ecaz",
        "rewards": [{"name": "Ecaz Swatch"}, {"name": "A Schematic"}],
    }]}
    raw = {"tiles": [{
        "board_index": 3, "house_name": "DA_HouseEcaz",
        "rewards": [
            {"template_id": "Ecaz_Placeables_Swatch"},
            {"template_id": "Schematic_Ecaz_Wall"},
        ],
    }]}
    ns["_attach_board_swatches"](shaped, raw)
    rows = shaped["tiles"][0]["rewards"]
    assert rows[0].get("swatch"), "swatch reward should carry chips"
    assert rows[0]["swatch"]["exact"] is True
    assert rows[0]["swatch"]["chips"] == _lut_hex("PlaceableDyePackEcaz")
    assert "swatch" not in rows[1], "non-swatch reward must not carry chips"


def test_board_swatch_attach_matches_by_board_index():
    ns = _load_swatch_ns()
    shaped = {"tiles": [{"board_index": 9, "short": "Ecaz",
                         "rewards": [{"name": "x"}]}]}
    raw = {"tiles": [{"board_index": 1, "house_name": "DA_HouseEcaz",
                      "rewards": [{"template_id": "X_Placeables_Swatch"}]}]}
    ns["_attach_board_swatches"](shaped, raw)   # no matching board_index
    assert "swatch" not in shaped["tiles"][0]["rewards"][0]


def test_rewards_half_swatch_attach():
    ns = _load_swatch_ns()
    rewards = {
        "board": [{"raw": "DA_HouseEcaz",
                   "items": [{"name": "Ecaz Swatch"}, {"name": "Schem"}]}],
        "houses": [{"raw": "DA_HouseEcaz",
                    "items": [{"name": "Ecaz Swatch"}, {"name": "Schem"}]}],
    }
    raw_rewards = {"houses": [{
        "house_name": "DA_HouseEcaz",
        "items": [{"template_id": "Ecaz_Placeables_Swatch"},
                  {"template_id": "Schematic_Ecaz"}],
    }]}
    ns["_attach_rewards_swatches"](rewards, raw_rewards)
    for section in ("board", "houses"):
        items = rewards[section][0]["items"]
        assert items[0].get("swatch") and items[0]["swatch"]["exact"] is True
        assert "swatch" not in items[1]


def test_rewards_half_alias_house_resolves():
    # The placeable dyepack is spelled Mikarrol in the LUT and Mikkarol in
    # house_reps; a template in either spelling lands on the exact entry.
    ns = _load_swatch_ns()
    for tid in ("Mikarrol_Placeables_Swatch", "Mikkarol_Placeables_Swatch"):
        rewards = {"board": [], "houses": [{
            "raw": "DA_HouseMikkarol",
            "items": [{"name": "Mikarrol Swatch"}]}]}
        raw_rewards = {"houses": [{
            "house_name": "DA_HouseMikkarol",
            "items": [{"template_id": tid}]}]}
        ns["_attach_rewards_swatches"](rewards, raw_rewards)
        sw = rewards["houses"][0]["items"][0]["swatch"]
        assert sw["exact"] is True, tid
        assert sw["chips"] == _lut_hex("PlaceableDyePackMikarrol"), tid


def test_unknown_swatch_template_falls_back_to_house_palette():
    # Wakyu_Placeables_Swatch is a misspelt game template with no dyepack of its
    # own; it keeps the honest house-palette proxy rather than a guessed exact.
    ns = _load_swatch_ns()
    sw = ns["_swatch_for_reward"]("Wakyu_Placeables_Swatch", "Wayku")
    assert sw["exact"] is False
    assert sw["chips"] == _lut_hex("HArmCharDyepackWayku")


def test_swatch_with_no_house_palette_is_omitted():
    ns = _load_swatch_ns()
    assert ns["_swatch_for_reward"]("Nobody_Placeables_Swatch", "Nobody") is None


def test_armor_and_stillsuit_swatches_are_exact():
    ns = _load_swatch_ns()
    for tid, key in (("Ecaz_LightArmor_Swatch", "LArmCharDyepackEcaz"),
                     ("Ecaz_HeavyArmor_Swatch", "HArmCharDyepackEcaz"),
                     ("Taligari_Stillsuit_Swatch", "StillSCharDyepackTalgari")):
        sw = ns["_swatch_for_reward"](tid, "Ecaz")
        assert sw["exact"] is True, tid
        assert sw["chips"] == _lut_hex(key), tid


def test_every_house_placeable_swatch_template_is_exact():
    # Every <House>_Placeables_Swatch template the game ships resolves to its own
    # LUT entry. Wakyu is the one misspelt template and stays a proxy.
    ns = _load_swatch_ns()
    with open(CURATED, "r", encoding="utf-8") as fh:
        tids = [k for k in json.load(fh) if k.endswith("_Placeables_Swatch")]
    assert len(tids) >= 25
    inexact = [t for t in tids
               if not ns["_swatch_for_reward"](t, "Ecaz")["exact"]]
    assert inexact == ["Wakyu_Placeables_Swatch"], inexact


def test_lut_carries_build_stamp_and_five_dyes():
    with open(LUT, "r", encoding="utf-8") as fh:
        lut = json.load(fh)
    meta = lut["_meta"]
    assert meta["build"] == "2168446"
    assert meta["count"] == len(lut["swatches"]) == 317
    for key, entry in lut["swatches"].items():
        assert len(entry["rgb"]) == 5 and len(entry["hex"]) == 5, key
        # portal-gear-viewer.js reads repr_rgb, falling back to primary_rgb.
        assert len(entry["repr_rgb"]) == 3 and len(entry["primary_rgb"]) == 3, key


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    for fn in _all_tests():
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(_all_tests()) - failures}/{len(_all_tests())} passed")
    raise SystemExit(1 if failures else 0)
