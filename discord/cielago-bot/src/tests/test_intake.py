"""Intake normalisation tests.

The modal can only collect free text, so these functions are the only thing
standing between "whatever the player typed" and a mod queue that can be
filtered. The load-bearing property is NOT that every phrasing maps correctly;
it is that nothing is ever silently discarded or mapped into the WRONG bucket.
A guessed-wrong surface sends a mod to the wrong system; a dropped server field
loses the only location on the report.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cielago.assistant import intake as I  # noqa: E402


def test_surface_maps_the_phrasings_players_actually_use():
    for raw, want in [
        ("portal", I.SURFACE_PORTAL),
        ("Portal", I.SURFACE_PORTAL),
        ("the website", I.SURFACE_PORTAL),
        ("lastsietch.com", I.SURFACE_PORTAL),
        ("on the web site", I.SURFACE_PORTAL),
        ("in game", I.SURFACE_GAME),
        ("ingame", I.SURFACE_GAME),
        ("in-game", I.SURFACE_GAME),
        ("on the server", I.SURFACE_GAME),
        ("hagga basin", I.SURFACE_GAME),
        ("both", I.SURFACE_BOTH),
        ("Both of them", I.SURFACE_BOTH),
        ("portal and in game", I.SURFACE_BOTH),
        ("game and portal", I.SURFACE_BOTH),
    ]:
        assert I.normalise_surface(raw) == want, f"{raw!r} -> {I.normalise_surface(raw)!r}"


def test_surface_never_guesses_when_it_cannot_tell():
    """🔴 The one that matters. An unrecognised answer must land in UNKNOWN, not
    in whichever bucket happened to be tested first. Guessing sends a mod to the
    wrong system and the ticket looks authoritative while being wrong."""
    for raw in ("", None, "   ", "no idea", "asdf", "?", "everything is fine"):
        assert I.normalise_surface(raw) == I.SURFACE_UNKNOWN, raw


def test_server_canonicalises_but_keeps_what_it_cannot_place():
    assert I.normalise_server("habbanya") == "Habbanya (PvE)"
    assert I.normalise_server("Habanya basin") == "Habbanya (PvE)"
    assert I.normalise_server("kulon pvp") == "Kulon-PvP"
    assert I.normalise_server("the PvP one") == "Kulon-PvP"
    assert I.normalise_server("deep desert") == "Deep Desert"
    # Unrecognised is PRESERVED, never blanked: it is the only location we have.
    assert I.normalise_server("some cave near the sietch") == "some cave near the sietch"
    assert I.normalise_server("") == ""
    assert I.normalise_server(None) == ""


def test_ingame_name_strips_what_the_prompt_invites():
    assert I.normalise_ingame_name("IGN: SandRider") == "SandRider"
    assert I.normalise_ingame_name("name - Alphapup") == "Alphapup"
    assert I.normalise_ingame_name("@SandRider") == "SandRider"
    assert I.normalise_ingame_name('"SandRider"') == "SandRider"
    assert I.normalise_ingame_name("  SandRider  ") == "SandRider"
    assert I.normalise_ingame_name("character: Dragonlord") == "Dragonlord"
    assert I.normalise_ingame_name("") == ""
    # A name that merely CONTAINS a prefix word must survive intact.
    assert I.normalise_ingame_name("Nameless One") == "Nameless One"


def test_title_never_comes_back_empty():
    """An empty title renders as a blank embed heading in the mod queue, which
    reads as a broken bot rather than a report with no summary."""
    assert I.shape_title("bug", "") == "Bug (no summary given)"
    assert I.shape_title("feature", "   ") == "Feature request (no summary given)"
    assert I.shape_title("bug", "short one") == "short one"
    long = "x" * 200
    out = I.shape_title("bug", long)
    assert len(out) <= 90 and out.endswith("…")


def test_body_keeps_both_halves_and_never_loses_the_description():
    assert I.report_body("it broke") == "it broke"
    both = I.report_body("it broke", "log in then click")
    assert "it broke" in both and "log in then click" in both
    # Steps without a description must still carry the steps.
    assert "click" in I.report_body("", "click")


def test_mod_summary_omits_what_was_never_answered():
    """A mod must be able to tell 'not asked' from 'asked and skipped'. Empty
    fields are omitted, not rendered as blanks."""
    assert I.summarise_for_mods("SandRider", I.SURFACE_PORTAL, "Habbanya (PvE)") == \
        "**SandRider** · Player Portal · Habbanya (PvE)"
    assert I.summarise_for_mods("SandRider", I.SURFACE_UNKNOWN, "") == "**SandRider**"
    assert I.summarise_for_mods("", I.SURFACE_UNKNOWN, "") == ""
    assert "Not specified" not in I.summarise_for_mods("x", I.SURFACE_UNKNOWN, "")


def test_whitespace_and_length_are_bounded():
    """A paste out of game chat carries newlines; a griefer carries 10k of them."""
    assert I.normalise_ingame_name("a\n\n   b") == "a b"
    assert len(I.report_body("x" * 99999)) <= 3500
    assert len(I.normalise_ingame_name("y" * 999)) <= 80


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
