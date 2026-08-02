#!/usr/bin/env python3
"""Regression tests for the V2 Character backend: the sibling JSON endpoint
`GET /portal/character/v2` (admin-backend/routers/portal.py) and the relay
ctrl-support facts it depends on (relay/app.py).

Prod-safe: NO real DB, NO network, NO heavy-import of portal.py (which needs the
host env: httpx/config/mirror/...). The endpoint layer is asserted by scanning the
source so we catch a regression that (a) drops the session gate, (b) stops
threading the selected controller into the ctrl-scoped progress read, (c) forgets
the graceful ctrl_scoped:false fallback flags for the relay reads that cannot be
ctrl-scoped without a game-box change, or (d) leaks a *_display string into the
raw-int JSON contract. A second block pins the RELAY ctrl-support verification
result: /progress accepts ?ctrl=, /progression_state and /equipped do NOT.

Run:  python3 scripts/tests/test_character_v2.py     (also import-safe)
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
PORTAL = os.path.join(REPO, "admin-backend", "routers", "portal.py")
RELAY = os.path.join(REPO, "relay", "app.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _v2_block(src):
    """The appended V2 module-ports section of portal.py (everything after the
    banner), so V1 handlers can't accidentally satisfy an assertion."""
    marker = "V2 MODULE PORTS (2026-07-15)"
    assert marker in src, "V2 banner missing"
    return src.split(marker, 1)[1]


def _char_handler(src):
    """Just the portal_character_v2 handler body (up to the next @router)."""
    block = _v2_block(src)
    start = block.index("async def portal_character_v2(")
    rest = block[start:]
    nxt = rest.index("\n@router.get(\"/portal/landsraad/v2\")")
    return rest[:nxt]


# --------------------------------------------------------------------------- #
# Endpoint presence + auth gate
# --------------------------------------------------------------------------- #

def test_character_v2_route_present():
    src = _read(PORTAL)
    assert '@router.get("/portal/character/v2")' in src


def test_character_v2_session_gated_json_401():
    h = _char_handler(_read(PORTAL))
    # JSON 401 gate (never a 302 to HTML) — the shared linked-session JSON gate.
    assert "gate, early = _require_linked_session_json(request)" in h
    assert "if early is not None:" in h
    assert "return early" in h


def test_character_v2_touches_session():
    h = _char_handler(_read(PORTAL))
    assert "_touch_last_session(active_account_id)" in h


# --------------------------------------------------------------------------- #
# Per-character ctrl threading + mirror bypass
# --------------------------------------------------------------------------- #

def test_character_v2_resolves_selected_ctrl():
    h = _char_handler(_read(PORTAL))
    assert "sel = _selected_ctrl(request, active_account_id)" in h


def test_character_v2_ctrl_scoped_progress_branch():
    h = _char_handler(_read(PORTAL))
    # non-default selected char -> ctrl-scoped fresh read; default -> mirror-first.
    assert "_load_progress_ctrl(active_account_id, sel) if sel is not None" in h
    assert "else _load_progress(active_account_id)" in h


def test_progress_ctrl_loader_bypasses_mirror_and_threads_ctrl():
    src = _read(PORTAL)
    # The ctrl-scoped loader must hit the relay directly (bypassing the account-
    # keyed mirror/ttl cache, which only holds the DEFAULT character) and thread
    # ?ctrl= into the progress path.
    start = src.index("async def _load_progress_ctrl(")
    body = src[start:src.index("async def _resolve_char_identity(")]
    assert "/progress?ctrl=" in body
    assert "call_relay(" in body
    # no account-keyed fast-path: must not consult the mirror at all.
    assert "mirror.get" not in body
    assert "get_section" not in body
    assert "get_scalars" not in body


# --------------------------------------------------------------------------- #
# Graceful fallback flags for relay reads that can't be ctrl-scoped
# --------------------------------------------------------------------------- #

def test_character_v2_ctrl_scoped_flag_definition():
    h = _char_handler(_read(PORTAL))
    # spec/equipped are correct only when the resolved character is the default:
    # no cookie, or the switcher points at the default (is_default).
    assert 'ctrl_scoped = (sel is None) or bool(ident and ident.get("is_default"))' in h


def test_character_v2_specializations_flagged():
    h = _char_handler(_read(PORTAL))
    assert 'specializations["ctrl_scoped"] = ctrl_scoped' in h


def test_character_v2_equipped_flagged():
    h = _char_handler(_read(PORTAL))
    assert '"ctrl_scoped": ctrl_scoped' in h


# --------------------------------------------------------------------------- #
# Frozen JSON contract
# --------------------------------------------------------------------------- #

def test_character_v2_envelope_and_top_level_keys():
    h = _char_handler(_read(PORTAL))
    assert "return _v2_ok({" in h
    for key in ('"character": {', '"vitals": {', '"faction_rep":',
                '"specializations":', '"journey":', '"landsraad_teaser":',
                '"equipped":', '"csrf_token":', '"character_name":'):
        assert key in h, key


def test_character_block_fields():
    h = _char_handler(_read(PORTAL))
    for key in ('"name":', '"level":', '"online":', '"current_map":',
                '"faction":', '"faction_crest":', '"last_online":'):
        assert key in h, key


def test_vitals_block_fields():
    h = _char_handler(_read(PORTAL))
    for key in ('"intel":', '"xp":', '"unspent_sp":', '"bank_solari":',
                '"pocket_solari":', '"scrip":'):
        assert key in h, key


def test_equipped_remaps_template_id_to_template():
    h = _char_handler(_read(PORTAL))
    # _load_equipped yields template_id; the frozen contract exposes it as "template".
    assert '"template": it.get("template_id")' in h


def test_character_v2_returns_raw_ints_not_display_strings():
    h = _char_handler(_read(PORTAL))
    # The UI formats client-side; no _display keys/values may leak into this JSON.
    assert "_display" not in h
    # faction_rep carries the raw standing int (not the formatted string).
    assert '"standing": rk["standing"]' in h


def test_character_v2_faction_rep_only_for_great_houses():
    h = _char_handler(_read(PORTAL))
    assert 'fac.get("faction_id") in (1, 2)' in h


# --------------------------------------------------------------------------- #
# RELAY ctrl-support verification (the fact the fallback design rests on)
# --------------------------------------------------------------------------- #

def test_relay_progress_accepts_ctrl():
    src = _read(RELAY)
    m = re.search(r"def dune_player_progress\(([^)]*)\)", src)
    assert m and "ctrl" in m.group(1), "progress must accept ?ctrl="
    assert "--controller {ctrl}" in src


def test_relay_progression_state_has_no_ctrl():
    src = _read(RELAY)
    m = re.search(r"def dune_player_progression_state\(([^)]*)\)", src)
    assert m, "progression_state handler missing"
    assert "ctrl" not in m.group(1), (
        "progression_state must NOT accept ?ctrl= (would need a game-box change); "
        "the V2 fallback flag spec_ctrl_scoped:false depends on this")


def test_relay_equipped_has_no_ctrl():
    src = _read(RELAY)
    m = re.search(r"def dune_player_equipped\(([^)]*)\)", src)
    assert m, "equipped handler missing"
    assert "ctrl" not in m.group(1), (
        "equipped must NOT accept ?ctrl= (would need a game-box change); "
        "the V2 fallback flag equipped_ctrl_scoped:false depends on this")


# --------------------------------------------------------------------------- #
# Zero-regression: V1 account handler untouched
# --------------------------------------------------------------------------- #

def test_v1_account_route_untouched():
    src = _read(PORTAL)
    assert '@router.get("/portal/account")' in src
    assert 'async def portal_account(request: Request):' in src


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
