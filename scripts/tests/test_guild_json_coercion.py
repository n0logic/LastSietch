#!/usr/bin/env python3
# Regression guard for the 2026-07-13 guild-social 500: the V2 client posts these
# forms as JSON with TYPED values (inbox role levels as ints, recruiting toggles as
# bools), but the handlers did `(value or "").strip()` -> AttributeError: 'int'/'bool'
# object has no attribute 'strip'. Every such read must coerce with str() first.
import re, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2] / "admin-backend"
def _read(p): return (ROOT / p).read_text()


def test_inbox_config_role_field_coerced():
    src = _read("routers/messages.py")
    assert 'raw = str(form.get(name, "") or "").strip()' in src, \
        "inbox-config _role_field must str()-coerce (JSON sends role levels as ints)"


def test_recruiting_typed_fields_coerced():
    src = _read("routers/portal.py")
    assert 'guild_id_raw = str(form.get("guild_id", "") or "").strip()' in src
    assert 'recruiting = str(recruiting_raw or "").strip().lower()' in src
    assert 'new_player_friendly = str(form.get("new_player_friendly", "") or "").strip().lower()' in src


def test_no_bare_strip_on_typed_guild_fields():
    for f in ("routers/portal.py", "routers/messages.py"):
        src = _read(f)
        for field in ('"guild_id"', '"view_min_role"', '"manage_min_role"',
                      '"new_player_friendly"'):
            # (?<!str) so a correctly str()-wrapped read is not flagged as bare.
            bad = re.compile(r'(?<!str)\(form\.get\(' + re.escape(field) + r'[^)]*\)\s*or\s*""\)\.strip\(\)')
            assert not bad.search(src), f"bare .strip() on typed field {field} in {f}"


def test_coercion_behavior():
    # Must not raise on any input type; truthy toggles classify correctly.
    def truthy(x): return str(x or "").strip().lower() in ("1", "true", "on", "yes")
    for v in (1, 50, 100, True, False, 0, None, "", "1", "true", "on", "member"):
        _ = str(v or "").strip()            # never raises regardless of type
    assert truthy(True) and truthy(1) and truthy("on") and truthy("yes")
    assert not truthy(False) and not truthy(0) and not truthy(None) and not truthy("member")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
    print("all guild coercion tests passed")
