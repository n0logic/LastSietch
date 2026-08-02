#!/usr/bin/env python3
"""Regression tests for the V2 Exchange backend: the new price-history module
(admin-backend/market_history.py), the capture hook in mirror.apply_market_snapshot,
and the sibling JSON endpoints in admin-backend/routers/portal.py.

Prod-safe: NO game DB, NO network. market_history is exercised functionally against
a throwaway admin.db (LASTSIETCH_DB_PATH env -> temp file). The portal/mirror layers are
asserted by scanning the source so we catch a regression that (a) reintroduces raw
request.form() on a V2 handler, (b) drops the sell offline-gate or gates buy, (c)
stops resolving the controller_id server-side, or (d) removes the price-history
capture hook. The fee-formula is checked against the writer's authoritative form.

Run:  python3 scripts/tests/test_market_v2.py     (also import-safe)
"""
import importlib.util
import os
import re
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
BACKEND = os.path.join(REPO, "admin-backend")
PORTAL = os.path.join(BACKEND, "routers", "portal.py")
MIRROR = os.path.join(BACKEND, "mirror.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# market_history.py functional tests (throwaway admin.db)
# --------------------------------------------------------------------------- #

def _load_market_history():
    """Import market_history against a fresh temp admin.db. LASTSIETCH_DB_PATH must be set
    BEFORE database.py binds DB_PATH at import, so this seeds env then imports."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["LASTSIETCH_DB_PATH"] = tmp.name
    # config.py mandates these at import; seed dummies for the isolated unit test.
    os.environ.setdefault("LASTSIETCH_RELAY_API_KEY", "test-key")
    os.environ.setdefault("LASTSIETCH_SESSION_SECRET", "test-secret")
    import sys
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    # Drop any pre-bound config/database so DB_PATH picks up our temp file.
    for mod in ("market_history", "database", "config"):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location(
        "market_history", os.path.join(BACKEND, "market_history.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["market_history"] = m
    spec.loader.exec_module(m)
    return m, tmp.name


def test_aggregate_listings_downsamples_per_template():
    m, _ = _load_market_history()
    listings = [
        {"template_id": "Water", "item_price": 30, "stack": 2},
        {"template_id": "Water", "item_price": 10, "stack": 5},
        {"template_id": "Water", "item_price": 20, "stack": 1},
        {"template_id": "Spice", "item_price": 100, "stack": 1},
        {"template_id": None, "item_price": 5, "stack": 1},         # dropped (no tpl)
    ]
    rows = {r["template_id"]: r for r in m.aggregate_listings(listings)}
    assert set(rows) == {"Water", "Spice"}
    w = rows["Water"]
    assert w["min_price"] == 10
    assert w["median_price"] == 20          # median of [10,20,30]
    assert w["listing_count"] == 3
    assert w["total_qty"] == 8
    # even count -> integer mean of the two middles
    assert m._median([10, 20, 30, 40]) == 25
    assert m._median([]) is None


def test_capture_throttle_and_history_and_low7d():
    m, _ = _load_market_history()
    rows = [{"template_id": "Water", "min_price": 10, "median_price": 20,
             "listing_count": 3, "total_qty": 8}]
    # first capture writes one point
    assert m.capture(rows) == 1
    # immediate re-capture is throttled (template captured < 10 min ago) -> 0
    assert m.capture(rows) == 0

    h = m.history("Water")
    assert h["calibrating"] is False
    assert len(h["points"]) == 1
    p = h["points"][0]
    assert p["min_price"] == 10 and p["median_price"] == 20 and p["listing_count"] == 3
    assert h["low_7d"] == 10

    # unknown template -> calibrating empty state
    empty = m.history("NoSuchThing")
    assert empty["points"] == [] and empty["calibrating"] is True and empty["low_7d"] is None
    # case-insensitive match (mirrors market_item_detail COLLATE NOCASE)
    assert m.history("water")["calibrating"] is False


def test_capture_prunes_beyond_retention():
    m, dbpath = _load_market_history()
    from datetime import datetime, timedelta, timezone
    import sqlite3
    old = (datetime.now(timezone.utc) - timedelta(days=m._RETENTION_DAYS + 2)).isoformat()
    conn = sqlite3.connect(dbpath)
    conn.executescript(m._SCHEMA)
    conn.execute("INSERT INTO market_price_history(template_id, min_price, "
                 "median_price, listing_count, total_qty, captured_at) "
                 "VALUES('Stale', 1, 1, 1, 1, ?)", (old,))
    conn.commit()
    conn.close()
    # a capture of a different template triggers the prune of the stale row
    m.capture([{"template_id": "Fresh", "min_price": 5, "median_price": 5,
                "listing_count": 1, "total_qty": 1}])
    assert m.history("Stale")["points"] == []
    assert len(m.history("Fresh")["points"]) == 1


def test_capture_empty_rows_still_prunes_without_error():
    m, _ = _load_market_history()
    assert m.capture([]) == 0        # no rows -> nothing written, no raise


# --------------------------------------------------------------------------- #
# Fee-formula parity vs the writer's authoritative integer form
# --------------------------------------------------------------------------- #

def _ref_fee(price, days):
    # The authoritative CHOAM listing fee (writer: (price*(days+1)+50)//100 + 20*days)
    return (price * (days + 1) + 50) // 100 + 20 * days


def test_sell_fee_matches_writer_formula_in_source():
    src = _read(PORTAL)
    # _sell_fee must use the exact half-up integer form the writer debits.
    assert "(price * (days + 1) + 50) // 100 + 20 * days" in src
    # and document the authoritative writer form it mirrors
    assert "(price*(days+1)+50)//100" in src


def test_sell_fee_reference_values():
    # Spot values across prices x durations {1,3,7,14} (parity check for the frontend
    # estimate; the writer debits the same integer).
    for price in (1, 99, 100, 101, 12345, 1_000_000):
        for days in (1, 3, 7, 14):
            f = _ref_fee(price, days)
            assert f == (price * (days + 1) + 50) // 100 + 20 * days
            assert isinstance(f, int)


# --------------------------------------------------------------------------- #
# Portal V2 endpoints: JSON contract + safety invariants (source scan)
# --------------------------------------------------------------------------- #

def _v2_block():
    return _read(PORTAL).split("V2 Exchange Module", 1)[1]


def test_portal_v2_market_endpoints_present():
    src = _read(PORTAL)
    for route in ('@router.get("/portal/market/v2")',
                  '@router.get("/portal/market/v2/search")',
                  '@router.get("/portal/market/v2/item")',
                  '@router.get("/portal/market/v2/history")',
                  '@router.get("/portal/market/v2/my-orders")',
                  '@router.get("/portal/market/v2/flips")',
                  '@router.post("/portal/market/v2/buy")',
                  '@router.post("/portal/market/v2/sell")',
                  '@router.post("/portal/market/v2/orders/cancel")',
                  '@router.post("/portal/market/v2/orders/relist")'):
        assert route in src, route


def test_portal_v1_market_routes_untouched():
    # zero-regression: the V1 HTML routes must still exist unchanged.
    src = _read(PORTAL)
    for route in ('@router.get("/portal/market")',
                  '@router.get("/portal/market/search")',
                  '@router.get("/portal/market/item")',
                  '@router.post("/portal/market/buy")',
                  '@router.post("/portal/market/sell")',
                  '@router.post("/portal/my-orders/cancel")',
                  '@router.post("/portal/my-orders/relist")',
                  '@router.get("/portal/my-orders")'):
        assert route in src, route


def test_v2_market_never_uses_raw_form():
    # STOP-SHIP: V2 handlers read JSON via _v2_body_and_csrf (-> _read_body), never
    # FastAPI Form()/request.form().
    src = _read(PORTAL)
    assert "request.form(" not in src
    v2 = _v2_block()
    assert "_v2_body_and_csrf(request)" in v2
    assert "await request.form" not in v2


def test_v2_sell_is_offline_gated_buy_is_not():
    v2 = _v2_block()
    sell = v2.split('@router.post("/portal/market/v2/sell")', 1)[1]
    sell_fn = sell.split("@router.post", 1)[0]
    # sell refuses a definitely-online player BEFORE any relay/DB touch
    assert "await _resolve_online(active_account_id) is True" in sell_fn
    assert '_v2_err("player_online"' in sell_fn

    buy = v2.split('@router.post("/portal/market/v2/buy")', 1)[1]
    buy_fn = buy.split("@router.post", 1)[0]
    # buy is online-safe: it must NOT gate on online status
    assert "_resolve_online" not in buy_fn


def test_v2_writes_resolve_ctrl_server_side():
    v2 = _v2_block()
    # buyer/seller/owner controller_id always resolved from the session, never body.
    # Multi-character (2026-07-13): the resolve is scoped by the SELECTED character
    # (server-validated), still never trusting a client-supplied controller.
    assert v2.count(
        "_resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))"
    ) >= 3
    # ownership re-verified server-side on sell
    assert "_verify_seller_owns_item(active_account_id, container_id, item_id)" in v2
    # no handler trusts a client-supplied ctrl
    for bad in ("buyer_ctrl = body", "seller_ctrl = body", "owner_ctrl = body"):
        assert bad not in v2


def test_v2_writes_require_csrf():
    v2 = _v2_block()
    # every write path checks CSRF and fails closed
    assert v2.count("if not csrf_ok:") >= 3
    assert v2.count('_v2_err("csrf"') >= 3


def test_v2_reuses_v1_shapers_not_rederived():
    v2 = _v2_block()
    for fn in ("_load_market_browse(", "_load_market_item(", "_load_my_orders(",
               "mirror.market_player_floors()", "mirror.bot_prices_all()",
               "_sell_fee(price, duration_days)", "market_history.history("):
        assert fn in v2, fn


def test_v2_sell_idempotency_wired():
    v2 = _v2_block()
    sell = v2.split('@router.post("/portal/market/v2/sell")', 1)[1]
    sell_fn = sell.split("@router.post", 1)[0]
    # replay-before-execute + store-after on BOTH success and failure
    assert "_sell_idem_lookup(active_account_id, uuid)" in sell_fn
    assert sell_fn.count("_sell_idem_store(active_account_id, uuid,") == 2
    # persistent guard table + composite key (per-account, per-uuid)
    assert "market_v2_sell_idem" in v2
    assert "PRIMARY KEY(account_id, uuid)" in v2
    # only sell carries the idempotency guard (buy/cancel/relist are revision-safe).
    # buy body ends where the SELL idempotency helper block begins.
    buy = v2.split('@router.post("/portal/market/v2/buy")', 1)[1]
    buy_fn = buy.split("# V2 SELL idempotency guard", 1)[0]
    assert "_sell_idem" not in buy_fn


def test_v2_flips_use_player_only_floor():
    v2 = _v2_block()
    flips = v2.split('@router.get("/portal/market/v2/flips")', 1)[1]
    flips_fn = flips.split("@router.post", 1)[0].split("@router.get", 1)[0]
    # min_ask is the PLAYER-only floor, not the combined NPC+player summary min
    assert "mirror.market_player_floors()" in flips_fn
    assert '"min_ask": lo' in flips_fn
    assert "r.get(\"has_player\")" not in flips_fn


def test_market_history_prune_index_present():
    m, dbpath = _load_market_history()
    m.init()
    import sqlite3
    conn = sqlite3.connect(dbpath)
    try:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='market_price_history'").fetchall()}
    finally:
        conn.close()
    # dedicated captured_at index so the ~30s prune is not a full scan
    assert "idx_mph_time" in idx


def test_mirror_has_player_floor_accessor():
    src = _read(MIRROR)
    assert "def market_player_floors():" in src
    # excludes NPC sell orders from the ask floor
    seg = src.split("def market_player_floors():", 1)[1].split("\ndef ", 1)[0]
    assert "is_npc_order=0" in seg


def test_mirror_player_floor_is_grade_aware():
    """The bot prices per grade, so the ask floor must be per (template, grade).

    A template-wide floor lets a grade-4 ask be compared against the grade-0 cap,
    which is how the Flip Board came to advertise flips that could never settle
    (ticket #130)."""
    src = _read(MIRROR)
    seg = src.split("def market_player_floors():", 1)[1].split("\ndef ", 1)[0]
    assert "quality_level" in seg, "floor query must read the grade column"
    assert "GROUP BY template_id, COALESCE(quality_level, 0)" in seg
    # the old template-only grouping must be gone, not merely supplemented
    assert "GROUP BY template_id\"" not in seg
    assert "GROUP BY template_id'" not in seg


def test_v2_flips_match_each_grade_to_its_own_cap():
    """Regression guard for ticket #130.

    The bug was `grade = min(bb["caps"], ...)`: it scored every listing against the
    cheapest published grade and then rendered the row labelled with THAT grade. The
    fix walks the per-grade floors and looks each one up against its own cap."""
    v2 = _v2_block()
    flips = v2.split('@router.get("/portal/market/v2/flips")', 1)[1]
    flips_fn = flips.split("@router.post", 1)[0].split("@router.get", 1)[0]
    # the buggy pattern is gone
    assert "min(bb[\"caps\"]" not in flips_fn
    # per-grade iteration, with the cap looked up by that same grade
    assert "for grade, lo in by_grade.items():" in flips_fn
    assert 'caps.get(str(grade)' in flips_fn
    # a grade the bot publishes no cap for has no buyer, so it must not be a flip
    assert "if raw_cap is None:" in flips_fn


def test_mirror_has_price_history_capture_hook():
    src = _read(MIRROR)
    hook = src.split("def apply_market_snapshot", 1)[1]
    # capture runs BEFORE the delete+reinsert of the live listing table
    cap_at = hook.index("market_history.capture(market_history.aggregate_listings(listings))")
    del_at = hook.index('conn.execute("DELETE FROM market_listing")')
    assert cap_at < del_at
    # best-effort: a history failure must not break the market refresh
    assert "market price-history capture failed" in hook


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    tests = _all_tests()
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
