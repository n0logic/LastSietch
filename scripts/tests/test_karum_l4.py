#!/usr/bin/env python3
"""Regression tests for the Karum portal layer (Phase 1, L4): the admin.db schema, the
listing state machine's exits, the projection boundary, and the anti-abuse gates.

Prod-safe: NO real DB, NO network, no game host.

Two techniques, because the subject needs both:
  * the SCHEMA and the contention primitive are EXECUTED against a throwaway sqlite, since
    "does this CAS actually decide a race" and "does the partial index actually stop a
    double-listing" are not questions a grep can answer;
  * the ROUTES are source-scanned, matching every other portal test in this directory
    (routers/portal.py pulls the whole FastAPI dependency chain and is not importable
    standalone), which is still the right tool for "did someone delete the guard".

Run:  python3 scripts/tests/test_karum_l4.py     (also import-safe)
"""
import json
import os
import re
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
PORTAL = os.path.join(REPO, "admin-backend", "routers", "portal.py")
MESSAGES = os.path.join(REPO, "admin-backend", "routers", "messages.py")
DATABASE = os.path.join(REPO, "admin-backend", "database.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _schema():
    src = _read(DATABASE)
    m = re.search(r'SCHEMA\s*=\s*r?"""(.*?)"""', src, re.S)
    assert m, "could not find SCHEMA in database.py"
    return m.group(1)


def _db():
    conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    conn.executescript(_schema())
    return conn


def _karum_block():
    src = _read(PORTAL)
    start = src.index("# ======================================================= THE KARUM")
    end = src.index("# ----------------------------------------------------- rewards ---", start)
    return src[start:end]


def _route(name):
    block = _karum_block()
    start = block.index(f'@router.post("/portal/karum/{name}")')
    rest = block[start + 10:]
    nxt = rest.find("\n@router.")
    return rest[:nxt] if nxt != -1 else rest


_LISTING_INSERT = """INSERT INTO portal_karum_listings
 (seller_account_id, seller_discord_id, seller_name, seller_ctrl, template_id,
  display_name, stack_size, price, escrow_item_id, status)
 VALUES (?,?,?,?,?,?,?,?,?,?)"""


# --------------------------------------------------------------------------- #
# Schema, executed
# --------------------------------------------------------------------------- #

def test_three_tables_exist():
    conn = _db()
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("portal_karum_listings", "portal_karum_events", "portal_karum_ledger"):
        assert t in tabs, t


def test_one_live_listing_per_escrowed_stack():
    """The structural guard against double-listing one stack, and it must be PARTIAL so a
    closed row never blocks a later relisting of the same item."""
    conn = _db()
    args = (1001, "d1", "Sandrider", 7001, "IronBar", "Iron Bar", 500, 12500, 4242)
    conn.execute(_LISTING_INSERT, args + ("active",))
    try:
        conn.execute(_LISTING_INSERT, args + ("active",))
        raise AssertionError("double-listed one escrowed stack")
    except sqlite3.IntegrityError:
        pass
    # every live status must be covered, not just 'active'
    for live in ("selling", "reconciling", "returning", "paid_undelivered"):
        conn.execute("UPDATE portal_karum_listings SET status=? WHERE escrow_item_id=4242",
                     (live,))
        try:
            conn.execute(_LISTING_INSERT, args + ("active",))
            raise AssertionError(f"status {live} did not block a second live listing")
        except sqlite3.IntegrityError:
            pass
    # ...and a closed one must NOT block
    for closed in ("sold", "cancelled", "failed"):
        conn.execute("UPDATE portal_karum_listings SET status=? WHERE escrow_item_id=4242",
                     (closed,))
        conn.execute(_LISTING_INSERT, args + ("active",))
        conn.execute("DELETE FROM portal_karum_listings WHERE status='active'")
        conn.execute("UPDATE portal_karum_listings SET status='active' WHERE escrow_item_id=4242")


def test_status_and_leg_enums_are_enforced():
    conn = _db()
    args = (1001, "d1", "S", 7001, "IronBar", "Iron Bar", 1, 10, 1)
    for bad in ("live", "expired", ""):
        try:
            conn.execute(_LISTING_INSERT, args + (bad,))
            raise AssertionError(f"status {bad!r} accepted")
        except sqlite3.IntegrityError:
            pass
    for bad in ("teleport", "refund", ""):
        try:
            conn.execute("INSERT INTO portal_karum_ledger "
                         "(correlation_id, listing_id, leg, account_id, status) "
                         "VALUES (?,1,?,1,'applied')", (f"c-{bad}", bad))
            raise AssertionError(f"leg {bad!r} accepted")
        except sqlite3.IntegrityError:
            pass


def test_ledger_correlation_id_is_unique():
    conn = _db()
    conn.execute("INSERT INTO portal_karum_ledger "
                 "(correlation_id, listing_id, leg, account_id, status) "
                 "VALUES ('c1',1,'list',1,'applied')")
    try:
        conn.execute("INSERT INTO portal_karum_ledger "
                     "(correlation_id, listing_id, leg, account_id, status) "
                     "VALUES ('c1',1,'pay',1,'applied')")
        raise AssertionError("duplicate correlation_id accepted")
    except sqlite3.IntegrityError:
        pass


# --------------------------------------------------------------------------- #
# The contention primitive, executed
# --------------------------------------------------------------------------- #

def test_cas_decides_a_contested_listing():
    """Two buyers race one listing. The compare-and-set is the SINGLE point that decides it,
    and it decides it in admin.db BEFORE any game write, so the loser never reaches the
    writer and is never charged."""
    conn = _db()
    conn.execute(_LISTING_INSERT,
                 (1001, "d1", "S", 7001, "IronBar", "Iron Bar", 1, 100, 7, "active"))
    lid = conn.execute("SELECT listing_id FROM portal_karum_listings").fetchone()[0]

    cas = ("UPDATE portal_karum_listings SET status = ?, updated_at = datetime('now'), "
           "buyer_account_id = ? WHERE listing_id = ? AND status = ?")
    first = conn.execute(cas, ("selling", 2002, lid, "active")).rowcount
    second = conn.execute(cas, ("selling", 3003, lid, "active")).rowcount
    assert first == 1, "the first buyer must win"
    assert second == 0, "the second buyer must lose, and must lose BEFORE any game write"
    assert conn.execute("SELECT buyer_account_id FROM portal_karum_listings "
                        "WHERE listing_id=?", (lid,)).fetchone()[0] == 2002

    # and the clean !paid revert puts it back for anyone, including the loser
    back = conn.execute(cas, ("active", None, lid, "selling")).rowcount
    assert back == 1
    assert conn.execute(cas, ("selling", 3003, lid, "active")).rowcount == 1


def test_pair_cap_sidecar_matches_the_like_pattern():
    """🔴 The per-pair cap counts portal_karum_events with
    detail LIKE '%\"counterparty\":N%'. json.dumps' DEFAULT spacing writes
    '{"counterparty": 1001}' with a space, which that pattern misses -- silently disabling
    the cap. The event writer must therefore use compact separators."""
    src = _read(PORTAL)
    block = _karum_block()
    assert 'json.dumps(detail, separators=(",", ":"))' in block, \
        "the event sidecar must be compact or the pair cap never fires"

    # prove the pattern the counter uses actually matches the sidecar the writer produces
    conn = _db()
    detail = json.dumps({"counterparty": 1001, "price": 500}, separators=(",", ":"))
    conn.execute("INSERT INTO portal_karum_events (account_id, event, detail) "
                 "VALUES (2002,'buy_applied',?)", (detail,))
    hits = conn.execute(
        "SELECT COUNT(*) FROM portal_karum_events WHERE account_id=2002 "
        "AND event='buy_applied' AND detail LIKE ?", ('%"counterparty":1001%',)
    ).fetchone()[0]
    assert hits == 1, "the cap's LIKE pattern does not match the sidecar it counts"

    # and the DEFAULT spacing would NOT have matched, which is the bug this pins
    loose = json.dumps({"counterparty": 1001, "price": 500})
    conn.execute("INSERT INTO portal_karum_events (account_id, event, detail) "
                 "VALUES (4004,'buy_applied',?)", (loose,))
    miss = conn.execute(
        "SELECT COUNT(*) FROM portal_karum_events WHERE account_id=4004 AND detail LIKE ?",
        ('%"counterparty":1001%',)).fetchone()[0]
    assert miss == 0, "test premise wrong: default spacing would have matched after all"


# --------------------------------------------------------------------------- #
# The projection boundary
# --------------------------------------------------------------------------- #

def test_projection_emits_no_internal_identifier():
    """🔴 No account_id, no discord_id, no player_controller_id, no inventory_id, no
    escrow_item_id, no correlation_id, ever. They exist on the row for the writer's benefit
    and are stripped here."""
    block = _karum_block()
    proj = block[block.index("def _karum_public("):block.index("def _karum_event(")]
    for leaked in ("account_id", "discord_id", "seller_ctrl", "buyer_ctrl",
                   "escrow_item_id", "escrow_corr_id", "sold_corr_id", "inventory_id",
                   "correlation_id"):
        assert f'"{leaked}"' not in proj, f"projection leaks {leaked}"
    # seller_name IS emitted: listing is the opt-in (owner decision D2)
    assert '"seller_name"' in proj


def test_every_read_route_goes_through_the_projection():
    block = _karum_block()
    for route in ('@router.get("/portal/karum")',
                  '@router.get("/portal/karum/search")',
                  '@router.get("/portal/karum/listing/{listing_id}")'):
        assert route in block, route
    # no route may hand a raw row to the client
    assert "_karum_public(" in block
    reads = block[block.index('@router.get("/portal/karum")'):
                  block.index('@router.post("/portal/karum/list")')]
    assert "dict(row)" not in reads and "dict(r)" not in reads


# --------------------------------------------------------------------------- #
# LIST: the only gated leg
# --------------------------------------------------------------------------- #

def test_list_offline_gates_and_checks_ownership():
    r = _route("list")
    assert "await _resolve_online(active_account_id) is True" in r
    assert '_v2_err("player_online"' in r
    # ownership through the enforced path, and the bank picked for the SELECTED character
    assert "_verify_seller_owns_item(active_account_id, container_id, item_id)" in r
    assert "_pick_bank(clist, ctrl)" in r
    assert 'owned.get("tradeable")' in r
    assert "_KARUM_MAX_PRICE" in r


def test_list_refuses_a_provably_undeliverable_template():
    """L4's half of the no_category fix. The writer is authoritative, but refusing here
    means the player gets the real reason instead of a generic write failure."""
    r = _route("list")
    assert "_karum_no_category(tpl, _karum_categorised())" in r, \
        "the list route does not check whether the item could be given back"
    assert '_v2_err("no_category"' in r
    # and the refusal has to happen BEFORE anything is written or escrowed
    block = _karum_block()
    route_at = block.index('@router.post("/portal/karum/list")')
    check_at = block.index("_karum_no_category", route_at)
    insert_at = block.index("INSERT INTO portal_karum_listings", route_at)
    assert check_at < insert_at, "the category check runs after a pending row is written"


def test_sellable_hides_what_it_could_not_hand_back():
    block = _karum_block()
    sellable = block[block.index('@router.get("/portal/karum/sellable")'):]
    sellable = sellable[:sellable.index("\n@router.")]
    assert "_karum_no_category" in sellable, "undeliverable items are still offered"
    # Silently dropping them is how "where did my item go" tickets start.
    assert "hidden_no_category" in sellable, "withheld items are dropped without a count"


def test_unknown_deliverability_fails_open():
    """🔴 The mirror can be off, stale or empty. Treating unknown as undeliverable would
    make EVERY item unlistable the moment a sync hiccups -- a self-inflicted outage, and one
    the writer already prevents properly. Unknown must mean 'let the writer decide'."""
    block = _karum_block()
    fn = block[block.index("def _karum_no_category"):]
    fn = fn[:fn.index("\n@router.")]
    assert "if not categorised" in fn and "return False" in fn, \
        "an unavailable category set is not failing open"
    assert 'return tpl.lower() not in categorised' in fn


def test_no_category_copy_names_the_real_reason():
    """The player is being refused for a reason they cannot see in-game. Vague copy here
    reads as a bug in the Karum rather than a property of the item."""
    src = _read(PORTAL)
    txt = src[src.index("_KARUM_ERROR_TEXT = {"):]
    txt = txt[:txt.index("}")]
    assert '"no_category"' in txt, "no_category has no player-facing copy"
    lowered = txt[txt.index('"no_category"'):].lower()
    assert "exchange" in lowered and "give back" in lowered


def test_list_identity_comes_from_the_session():
    r = _route("list")
    assert '"seller_account_id": active_account_id' in r
    # the client body must not be able to name the seller
    assert 'body.get("seller_account_id")' not in r
    assert 'body.get("seller_name")' not in r
    assert 'body.get("seller_ctrl")' not in r


def test_list_dark_records_nothing_live():
    """A DARK deferred must not leave a live listing behind, and must write no ledger row."""
    r = _route("list")
    dark = r[r.index('if status == "deferred":'):r.index('if status in ("applied", "replay"):')]
    assert '"pending", "failed"' in dark
    assert "_karum_mirror" not in dark
    assert '"status": "deferred"' in dark


def test_list_mirrors_only_on_applied_or_replay():
    r = _route("list")
    assert 'if status in ("applied", "replay"):' in r
    good = r[r.index('if status in ("applied", "replay"):'):]
    assert "_karum_mirror(" in good


# --------------------------------------------------------------------------- #
# BUY: the four exits from `selling`
# --------------------------------------------------------------------------- #

def test_buy_self_trade_checks_both_keys_and_fails_closed():
    """🔴 Linked alts share a discord_id, so the account check alone is trivially defeated
    by someone who owns both sides. An unresolvable identity is a REJECT, not a pass."""
    r = _route("buy")
    assert "seller_account == active_account_id" in r
    assert "str(seller_discord) == str(discord_id)" in r
    # a missing discord_id on EITHER side rejects
    assert "not discord_id or not seller_discord" in r


def test_buy_cas_precedes_the_relay():
    r = _route("buy")
    assert r.index('_karum_cas(listing_id, "active", "selling"') < r.index("_karum_relay(")


def test_buy_never_reverts_to_active_on_no_response():
    """🔴 THE money-dupe guard. A lost response is not evidence that payment failed;
    reverting would let a second buyer purchase goods the first may already have paid for."""
    r = _route("buy")
    unknown = r[r.index('if status == "unknown":'):r.index("if paid and delivered:")]
    assert '"reconciling"' in unknown
    assert '"active"' not in unknown, "a timeout must NEVER revert the listing to active"


def test_buy_reverts_to_active_only_on_a_clean_unpaid_answer():
    r = _route("buy")
    tail = r[r.index("# Clean `paid: false`"):]
    assert '_karum_cas(listing_id, "selling", "active"' in tail
    # and it clears the buyer columns so the listing is genuinely free again
    assert "buyer_account_id=None" in tail


def test_buy_paid_undelivered_never_refunds():
    r = _route("buy")
    seg = r[r.index("if paid and not delivered:"):r.index("# Clean `paid: false`")]
    assert '"paid_undelivered"' in seg
    assert "refund" not in seg.lower() or "never refund" in seg.lower()
    # the payment IS mirrored: the money really moved
    assert '_karum_mirror(corr, listing_id, "pay"' in seg


def test_buy_branches_on_the_booleans_not_the_status():
    r = _route("buy")
    assert 'paid = result.get("paid")' in r
    assert 'delivered = result.get("delivered")' in r
    assert "if paid and delivered:" in r


def test_buy_copy_explains_the_canceled_render():
    """completion_type 3 is the only format the client renders and it reads as CANCELED. The
    game will never say 'purchase', so the copy must say so or the player thinks it failed."""
    r = _route("buy")
    assert "CANCELED" in r


# --------------------------------------------------------------------------- #
# CANCEL
# --------------------------------------------------------------------------- #

def test_cancel_is_sellers_own_and_404s_otherwise():
    r = _route("cancel")
    assert 'int(row["seller_account_id"]) != active_account_id' in r
    assert "status=404" in r, "someone else's listing must 404, never 403: no existence leak"


def test_cancel_moves_no_money_and_leaves_failures_recoverable():
    r = _route("cancel")
    # No payment leg exists on this path at all: payment only happens at buy time and a
    # cancel is only reachable from `active`.
    assert 'leg="pay"' not in r and '"pay"' not in r
    assert "amount=" not in r, "a cancel must not pass an amount to the ledger"
    assert '_karum_mirror(corr, listing_id, "return"' in r
    assert '_karum_cas(listing_id, "returning", "cancelled"' in r
    # The ONLY revert out of `returning` is the DARK deferral. A real failure leaves the
    # listing in `returning` so the retry and then admin force-return can pick it up: the
    # item is still safely in escrow and the seller has lost nothing.
    reverts = r.count('"returning", "active"')
    assert reverts == 1, f"expected exactly one revert out of returning, found {reverts}"
    dark = r[r.index('if status == "deferred":'):]
    assert '"returning", "active"' in dark[:400], \
        "the revert must be the DARK branch, not a failure branch"


# --------------------------------------------------------------------------- #
# Notification plumbing
# --------------------------------------------------------------------------- #

def test_mailbox_payload_keys_are_registered():
    """🔴 A key absent from _PAYLOAD_ALLOWED_KEYS is SILENTLY STRIPPED on read, so the card
    renders empty and looks like a bug in the sender."""
    allowed = _read(MESSAGES)
    block = _karum_block()
    # Every key the Karum actually puts in a mailbox payload, harvested from the payload
    # literals themselves so a newly added key cannot slip past this test.
    payloads = re.findall(r'\{"kind": "karum_\w+".*?\}\)', block, re.S)
    assert payloads, "found no Karum mailbox payloads; the test needs updating"
    used = set()
    for p in payloads:
        used.update(re.findall(r'"(\w+)":', p))
    for key in sorted(used):
        assert f'"{key}"' in allowed, \
            f"payload key {key!r} is not registered in _PAYLOAD_ALLOWED_KEYS and would be " \
            f"silently stripped on read"


def test_mailbox_reuses_the_notification_kind():
    """portal_messages.kind is CHECK-constrained in BOTH mailbox.py and the DDL, so a fourth
    enum value means editing two places that must agree for no player-visible benefit. The
    Karum sub-type rides in the payload's own `kind` key instead."""
    block = _karum_block()
    assert 'mailbox.post("player", int(account_id), "notification"' in block
    # No other kind is ever posted from this block. Counted rather than regex-matched on
    # the argument list, because the call carries `int(account_id)` and a lazy [^)]* cannot
    # cross that closing paren -- which made the first version of this test vacuous.
    calls = block.count("mailbox.post(")
    notifications = block.count('mailbox.post("player", int(account_id), "notification"')
    assert calls == notifications == 1, \
        f"expected exactly one mailbox.post form, found {calls} calls / {notifications} notifications"
    # and the sub-type is carried in the payload
    assert '"kind": "karum_bought"' in block
    assert '"kind": "karum_sold"' in block
    assert '"kind": "karum_returned"' in block


def test_notifications_are_not_load_bearing():
    """A trade is complete when the ledger says so. Every notify call must be best-effort."""
    block = _karum_block()
    notify = block[block.index("def _karum_notify("):block.index("async def _karum_relay(")]
    assert "except Exception" in notify
    assert "return None" in notify


def test_writer_failure_detail_keeps_the_writers_own_message():
    """🔴 Regression: the first live listing attempt (2026-07-27) failed and left only
    {"reason":"write_failed"} in portal_karum_events, because the writer's fail_json emits an
    `error` key ONLY when it was handed a token, and the portal recorded nothing but that key.
    So on precisely the failures we understand least, the one description of what went wrong
    was discarded -- nothing in admin.db, the relay log, or the portal log said why.

    Pins three things: the helper exists, it preserves `message`, and every karum failure tail
    routes through it instead of rebuilding a bare {"reason": token} dict."""
    src = _read(PORTAL)

    assert "def _karum_fail_detail(" in src, \
        "the _karum_fail_detail helper is gone; karum failures will lose the writer's message"

    body = src.split("def _karum_fail_detail(", 1)[1].split("\ndef ", 1)[0]
    for key in ('result.get("message")', '"writer_message"'):
        assert key in body, f"_karum_fail_detail no longer preserves {key}"
    assert "logger.warning" in body, \
        "_karum_fail_detail must also log, so the reason survives admin.db pruning"

    # No karum failure tail may go back to recording a bare reason-only detail.
    assert 'detail={"reason": token}' not in src, \
        "a karum failure path rebuilt a bare {'reason': token} detail and will drop the message"

    # All three karum write paths must actually call it.
    calls = src.count("_karum_fail_detail(result, token")
    assert calls >= 3, (
        f"expected list, buy and cancel to route failures through _karum_fail_detail, "
        f"found {calls} call site(s)")


def test_every_buy_failed_site_routes_through_the_fail_detail_helper():
    """Every buy_failed event must build its detail via _karum_fail_detail, so the writer's
    message survives, AND must keep `counterparty` as a top-level key, because
    _karum_count_events enforces the per-pair cap by matching the compact-JSON substring
    '"counterparty":<id>'.

    Checked per CALL SITE rather than by counting strings file-wide. Two earlier versions of
    this test were wrong for that reason: 'counterparty=seller_account' also appears in the cap
    CHECK, and the bare-dict form '"counterparty": seller_account' is legitimate in non-failure
    events like buy_attempt. Counting occurrences conflated all three."""
    src = _read(PORTAL)
    marker = '_karum_event(active_account_id, "buy_failed"'
    sites, pos = [], 0
    while True:
        i = src.find(marker, pos)
        if i == -1:
            break
        sites.append(src[i:i + 400])
        pos = i + 1

    assert len(sites) == 3, (
        f"expected 3 buy_failed sites (no_response, paid_undelivered, write_failed), "
        f"found {len(sites)}")
    for n, chunk in enumerate(sites):
        call = chunk.split("return", 1)[0]
        assert "_karum_fail_detail(" in call, (
            f"buy_failed site #{n + 1} builds its detail without _karum_fail_detail, so it "
            f"discards the writer's message on a money failure:\n{call[:200]}")
        assert "counterparty=seller_account" in call, (
            f"buy_failed site #{n + 1} lost its top-level counterparty kwarg, which silently "
            f"disables the per-pair cap")

    ev = src.split("def _karum_event(", 1)[1].split("\ndef ", 1)[0]
    assert 'separators=(",", ":")' in ev, \
        "_karum_event lost its compact separators; the per-pair cap match will break"


def test_the_money_failure_paths_preserve_the_writer_message():
    """no_response and paid_undelivered are the two failures where we either do not know if
    money moved, or know it moved without goods. Those are precisely the cases an operator
    resolves by hand, so they must carry the writer's text."""
    src = _read(PORTAL)
    assert '_karum_fail_detail(result, "no_response"' in src, \
        "the relay-timeout path no longer preserves the writer/ssh message"
    assert 'result.get("error") or "undelivered"' in src \
        and "_karum_fail_detail(result,\n" in src.replace("\r", ""), \
        "the paid_undelivered path no longer routes through _karum_fail_detail"
    assert src.count("_karum_fail_detail(") >= 6, (
        "expected the helper at 5 call sites plus its definition (list, cancel, and all three "
        f"buy paths); found {src.count('_karum_fail_detail(')}")


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
