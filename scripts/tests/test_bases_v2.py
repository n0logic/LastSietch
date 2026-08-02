#!/usr/bin/env python3
"""Regression tests for the V2 Bases/Solido backend: the sibling JSON endpoints
in admin-backend/routers/solido.py (market/detail/overview reads + publish /
unpublish / rename / import writes).

Prod-safe: NO real DB, NO network, NO heavy-import of solido.py (host env only).
The endpoint layer is asserted by scanning the source so we catch a regression
that (a) drops a v2 route, (b) reads a client-supplied blueprint instead of the
server-side re-export invariant, (c) drops a CSRF/cap/online-offline gate, (d)
stops honoring the client-uuid idempotency key, or (e) narrows the all-linked
scope. The V2 write handlers must never fall back to request.form().

Run:  python3 scripts/tests/test_bases_v2.py     (also import-safe)
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
SOLIDO = os.path.join(REPO, "admin-backend", "routers", "solido.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _v2_block(src):
    marker = "V2 MODULE PORT (2026-07-15)"
    assert marker in src, "V2 banner missing"
    return src.split(marker, 1)[1]


def _handler(src, defname):
    block = _v2_block(src)
    start = block.index(f"async def {defname}(")
    rest = block[start:]
    # up to the next route decorator (or EOF)
    idx = rest.find("\n@router.")
    return rest if idx < 0 else rest[:idx]


# --------------------------------------------------------------------------- #
# Route presence
# --------------------------------------------------------------------------- #

def test_all_v2_routes_present():
    src = _read(SOLIDO)
    for route in ('@router.get("/portal/bases/v2")',
                  '@router.get("/portal/solido/v2/market")',
                  '@router.get("/portal/solido/v2/{publish_id:int}")',
                  '@router.get("/portal/solido/v2/{publish_id:int}/blueprint")',
                  '@router.post("/portal/bases/v2/publish")',
                  '@router.post("/portal/bases/v2/unpublish")',
                  '@router.post("/portal/bases/v2/rename")',
                  '@router.post("/portal/bases/v2/import")'):
        assert route in src, route


def test_v1_routes_untouched():
    src = _read(SOLIDO)
    for route in ('@router.post("/portal/solido/publish")',
                  '@router.post("/portal/solido/{publish_id:int}/unpublish")',
                  '@router.post("/portal/solido/import")',
                  '@router.get("/portal/my-bases")',
                  '@router.post("/portal/my-bases/{bp_id}/rename")',
                  '@router.get("/portal/solido/{publish_id:int}/download")',
                  '@router.get("/portal/solido/{publish_id:int}/thumb.png")'):
        assert route in src, route


# --------------------------------------------------------------------------- #
# V2 writes are JSON-only (never raw form) and CSRF-gated
# --------------------------------------------------------------------------- #

def test_v2_writes_never_use_raw_form():
    block = _v2_block(_read(SOLIDO))
    assert "request.form(" not in block
    assert "await _v2_json_body(request)" in block


def test_every_v2_write_is_csrf_gated():
    src = _read(SOLIDO)
    for defname in ("bases_v2_publish", "bases_v2_unpublish",
                    "bases_v2_rename", "bases_v2_import"):
        h = _handler(src, defname)
        assert "if not _v2_csrf_ok(request, body):" in h, defname
        assert 'return _v2_err("csrf"' in h, defname


def test_every_v2_write_requires_linked_session():
    src = _read(SOLIDO)
    for defname in ("bases_v2_publish", "bases_v2_unpublish",
                    "bases_v2_rename", "bases_v2_import", "bases_v2_overview"):
        h = _handler(src, defname)
        assert "accounts = _session_accounts(request)" in h, defname
        assert 'return _v2_err("unauthenticated"' in h, defname


# --------------------------------------------------------------------------- #
# Publish: server-side re-export invariant (never trusts a client blueprint)
# --------------------------------------------------------------------------- #

def test_publish_is_server_side_reexport():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # takes only a numeric game_bp_id; the blueprint is re-exported via the
    # ownership-gated relay, never read from the request body.
    assert 'game_bp_id = str(body.get("game_bp_id"' in h
    assert "/blueprint/{game_bp_id}/export" in h
    assert 'body.get("blueprint")' not in h  # never a client-supplied blueprint


def test_publish_rolling_cap_enforced():
    src = _read(SOLIDO)
    h = _handler(src, "bases_v2_publish")
    assert "BLUEPRINT_PUBLISH_DAILY_CAP" in h
    assert "total_recent = _published_today(accounts)" in h
    assert 'return _v2_err("rate_limited"' in h
    # the shared helper is the durable publish-log COUNT.
    assert "recent_publish_count(aid, since)" in src


def test_publish_all_linked_scope():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # tries every linked account until one owns the blueprint (V1 parity).
    assert "for acct in accounts:" in h


def test_publish_size_cap():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    assert "blueprint_market.PUBLISH_BLOB_MAX" in h
    assert 'return _v2_err("too_large"' in h


# --------------------------------------------------------------------------- #
# Import: recipient forced to linked account, offline gate, cap, client uuid
# --------------------------------------------------------------------------- #

def test_import_recipient_forced_to_linked_account():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    assert "my_ids = _account_id_set(accounts)" in h
    assert "if account_id not in my_ids:" in h
    assert 'return _v2_err("bad_recipient"' in h


def test_import_backpack_online_gate():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    assert 'if delivery == "backpack" and online:' in h
    assert 'return _v2_err(\n            "online",' in h or '_v2_err("online"' in h
    assert "status=409" in h


def test_import_rolling_cap_enforced():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    assert "BLUEPRINT_IMPORT_DAILY_CAP" in h
    assert 'recent_action_count(\n        "portal_solido_import"' in h or \
           'recent_action_count("portal_solido_import"' in h
    assert 'return _v2_err("rate_limited"' in h


def test_import_honors_client_uuid_idempotency():
    src = _read(SOLIDO)
    h = _handler(src, "bases_v2_import")
    # the client idempotency key becomes the grant key.
    assert "idem_key = _v2_idem_key(body)" in h
    assert "idempotency_key=idem_key" in h
    # _v2_idem_key accepts BOTH `uuid` (frontend) and legacy `client_uuid`.
    helper = src[src.index("def _v2_idem_key("):]
    helper = helper[:helper.index("\n\n\n")] if "\n\n\n" in helper else helper[:400]
    assert 'body.get("uuid")' in helper and 'body.get("client_uuid")' in helper
    assert "_uuid.UUID(raw)" in helper


def test_import_server_side_recipient_never_arbitrary():
    h = _handler(_read(SOLIDO), "bases_v2_import")
    # recipient is forced from the session's linked set, never trusted from body
    # beyond membership check; the executor gets the validated account_id.
    assert "account_id=account_id" in h
    assert 'grant_type="import_blueprint"' in h


# --------------------------------------------------------------------------- #
# Rename: offline gate surfaced, all-linked, CSRF
# --------------------------------------------------------------------------- #

def test_rename_offline_gate_status():
    h = _handler(_read(SOLIDO), "bases_v2_rename")
    # player_online -> 409 (log out to rename); mirrors the V1 status mapping.
    assert 'status = 409 if last_err == "player_online"' in h
    assert "_RENAME_ERROR_TEXT" in h


def test_rename_name_cap():
    h = _handler(_read(SOLIDO), "bases_v2_rename")
    assert "RENAME_NAME_MAX" in h
    assert 'return _v2_err("name_too_long"' in h
    assert 'return _v2_err("empty_name"' in h


def test_rename_all_linked_scope():
    h = _handler(_read(SOLIDO), "bases_v2_rename")
    assert "for acct in accounts:" in h
    assert '"/dune/blueprint/rename"' in h


# --------------------------------------------------------------------------- #
# Reads: market paging + detail + overview
# --------------------------------------------------------------------------- #

def test_market_uses_page_and_card_shape():
    h = _handler(_read(SOLIDO), "solido_v2_market")
    assert 'q.get("page"' in h
    assert "offset = (page - 1) * limit" in h
    assert "[_card(r) for r in rows]" in h
    assert '"has_more":' in h


def test_detail_returns_canonical_card_plus_owner():
    h = _handler(_read(SOLIDO), "solido_v2_detail")
    assert "card = _card(row)" in h
    assert '"is_owner"' in h and "_my_publish_ids(accounts)" in h
    assert 'return _v2_err("not_found"' in h


def test_overview_shape_matches_frontend_contract():
    h = _handler(_read(SOLIDO), "bases_v2_overview")
    assert "/dune/player/{aid}/blueprints" in h
    # published is NESTED per-blueprint (null when unpublished), not a top-level map
    assert 'r["status"] == "published"' in h
    assert '"published": published_by_bp.get(str(bp_id))' in h
    # per-blueprint fields
    for key in ('"bp_id":', '"name":', '"piece_count":'):
        assert key in h, key
    # top-level shape
    assert '"linked": bool(accounts)' in h
    assert '"sections":' in h
    # caps -> publish_daily_cap + published_today + rename_name_max (input maxlength)
    assert '"publish_daily_cap": BLUEPRINT_PUBLISH_DAILY_CAP' in h
    assert '"published_today": _published_today(accounts)' in h
    assert '"rename_name_max": RENAME_NAME_MAX' in h
    # feature flags + renamed csrf
    assert '"publish_enabled": _publish_enabled()' in h
    assert '"import_enabled": _import_enabled()' in h
    assert '"csrf":' in h and '"csrf_token":' not in h


def test_publish_return_shape():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # publish returns publish_id + download_count + refreshed published_today
    assert '"publish_id": publish_id' in h
    assert '"download_count":' in h
    assert '"published_today": _published_today(accounts)' in h


def test_publish_derives_title_server_side():
    h = _handler(_read(SOLIDO), "bases_v2_publish")
    # client sends only game_bp_id (+ uuid); title/description are server-derived.
    assert 'body.get("title"' not in h
    assert 'body.get("description"' not in h
    assert 'title = ""' in h


def test_write_kill_switches_enforced():
    src = _read(SOLIDO)
    ph = _handler(src, "bases_v2_publish")
    assert "if not _publish_enabled():" in ph
    assert 'return _v2_err("publish_disabled"' in ph
    ih = _handler(src, "bases_v2_import")
    assert "if not _import_enabled():" in ih
    assert 'return _v2_err("import_disabled"' in ih


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
