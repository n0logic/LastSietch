#!/usr/bin/env python3
"""Regression tests for the V2 Landsraad backend: the sibling JSON endpoint
`GET /portal/landsraad/v2` (admin-backend/routers/portal.py) and the proxy
swatch-chip attach (Option A).

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
    "_swatch_lut_by_house", "_swatch_chips_for_house",
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
        "json": json, "_os": os, "Optional": Optional,
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
# Swatch attach: only Swatch template ids get chips (exact:false)
# --------------------------------------------------------------------------- #

def test_board_swatch_attach_only_on_swatch_tids():
    ns = _load_swatch_ns()
    shaped = {"tiles": [{
        "board_index": 3, "short": "Ecaz",
        "rewards": [{"name": "Ecaz Swatch"}, {"name": "A Schematic"}],
    }]}
    raw = {"tiles": [{
        "board_index": 3, "house_name": "DA_HouseEcaz",
        "rewards": [
            {"template_id": "Dye_Ecaz_Placeables_Swatch"},
            {"template_id": "Schematic_Ecaz_Wall"},
        ],
    }]}
    ns["_attach_board_swatches"](shaped, raw)
    rows = shaped["tiles"][0]["rewards"]
    assert rows[0].get("swatch"), "swatch reward should carry chips"
    assert rows[0]["swatch"]["exact"] is False
    assert rows[0]["swatch"]["chips"] and rows[0]["swatch"]["chips"][0].startswith("#")
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
        "items": [{"template_id": "Dye_Ecaz_Placeables_Swatch"},
                  {"template_id": "Schematic_Ecaz"}],
    }]}
    ns["_attach_rewards_swatches"](rewards, raw_rewards)
    for section in ("board", "houses"):
        items = rewards[section][0]["items"]
        assert items[0].get("swatch") and items[0]["swatch"]["exact"] is False
        assert "swatch" not in items[1]


def test_rewards_half_alias_house_resolves():
    # Argosaz (house_reps spelling) must still get chips via the Agrosaz alias.
    ns = _load_swatch_ns()
    rewards = {"board": [], "houses": [{
        "raw": "DA_HouseArgosaz",
        "items": [{"name": "Argosaz Swatch"}]}]}
    raw_rewards = {"houses": [{
        "house_name": "DA_HouseArgosaz",
        "items": [{"template_id": "Dye_Argosaz_Placeables_Swatch"}]}]}
    ns["_attach_rewards_swatches"](rewards, raw_rewards)
    assert rewards["houses"][0]["items"][0].get("swatch")


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
