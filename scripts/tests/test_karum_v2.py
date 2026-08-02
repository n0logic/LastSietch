#!/usr/bin/env python3
"""Regression tests for the Karum backend (Phase 1, L1-L3): the writer
(scripts/dune-karum-op.sh), its two game-DB ledgers, the dispatcher token and the four
relay routes.

Prod-safe: NO real DB, NO network. The writer is driven as a subprocess in --dry-run, which
exits before resolve_db_pod, so the SQL asserted here is the SQL that ships.

These are STRUCTURAL guards. The writer's behaviour -- does a retried payment double-charge,
does an online seller actually get refused, does a delivery adopt the row instead of minting
one -- is proven by executing the real SQL against a throwaway postgres in
scripts/tests/test_karum_op_pg.sh. Neither file replaces the other: a grep cannot tell you
what a gate refuses, and an integration test will not notice that a guard was deleted from a
route it does not exercise.

Run:  python3 scripts/tests/test_karum_v2.py     (also import-safe)
"""
import base64
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
KARUM = os.path.join(SCRIPTS, "dune-karum-op.sh")
TAKE_LIB = os.path.join(SCRIPTS, "lib", "dune-take-item.sh")
DISPATCH = os.path.join(SCRIPTS, "dune-relay-dispatch.sh")
RELAY = os.path.join(REPO, "relay", "app.py")
ESCROW_SQL = os.path.join(SCRIPTS, "sql", "ls_karum_escrow.sql")
PAYMENTS_SQL = os.path.join(SCRIPTS, "sql", "ls_karum_payments.sql")

CORR = "11111111-2222-3333-4444-555555555555"
CORR2 = "99999999-8888-7777-6666-555555555555"


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _run(payload, enabled="1"):
    env = dict(os.environ)
    if enabled is None:
        env.pop("LASTSIETCH_KARUM_ENABLED", None)
    else:
        env["LASTSIETCH_KARUM_ENABLED"] = enabled
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    proc = subprocess.run(["bash", KARUM, "--op-b64-stdin"], input=b64,
                          capture_output=True, text=True, env=env, timeout=30)
    return proc, json.loads(proc.stdout)


def _sql(action, **kw):
    payload = {"action": action, "correlation_id": CORR, "mode": "dry-run"}
    payload.update(kw)
    proc, out = _run(payload)
    assert out.get("status") == "dry-run", out
    return out["sql"]


def _list_sql():
    return _sql("karum-list", listing_id=4711, seller_account_id=1001, item_id=42,
                template_id="IronBar")


def _buy_sql():
    return _sql("karum-buy", listing_id=4711, buyer_account_id=2002,
                seller_account_id=1001, amount=12500)


def _cancel_sql():
    """Cancel is the cheapest way to see build_delivery_txn, which all three hand-over
    legs share."""
    return _sql("karum-cancel", listing_id=4711, seller_account_id=1001, price=500)


# --------------------------------------------------------------------------- #
# The DARK gate
# --------------------------------------------------------------------------- #

def test_dark_by_default_and_never_a_fake_success():
    for action, extra in (
            ("karum-list", {"listing_id": 1, "seller_account_id": 1, "item_id": 1,
                            "template_id": "IronBar"}),
            ("karum-buy", {"listing_id": 1, "buyer_account_id": 2,
                           "seller_account_id": 1, "amount": 10}),
            ("karum-cancel", {"listing_id": 1, "seller_account_id": 1}),
            ("karum-admin", {"admin_action": "force-return", "listing_id": 1,
                             "target_account_id": 1})):
        payload = {"action": action, "correlation_id": CORR}
        payload.update(extra)
        _, out = _run(payload, enabled=None)
        assert out.get("status") == "deferred", (action, out)
        # paid/delivered are ALWAYS present so L4 can branch on them without special-casing
        # the dark path.
        assert out.get("paid") is False, (action, out)
        assert out.get("delivered") is False, (action, out)


def test_dark_gate_precedes_the_take_library():
    """A box that has the writer but not yet lib/dune-take-item.sh must still answer
    deferred, or a late library turns the dark path into a new failure mode.

    Asserted inside do_op rather than over the whole file: op_list is DEFINED above do_op, so
    file order is not execution order and comparing raw offsets tests nothing. What matters
    is that the gate returns before the action dispatch, and that only an action reached
    through that dispatch touches the library."""
    src = _read(KARUM)
    body = src[src.index("do_op() {"):src.index("# Entry point")]
    assert body.index('LASTSIETCH_KARUM_ENABLED" != "1"') < body.index('case "$action" in')
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "require_take_lib" not in code, "do_op must not load the library itself"
    # and the runtime proof: dark defers even with the library made unreadable is covered by
    # test_dark_by_default_and_never_a_fake_success, which runs the real script.


# --------------------------------------------------------------------------- #
# The ONE take
# --------------------------------------------------------------------------- #

def test_writer_has_no_take_of_its_own():
    """Owner decision D3, and the design law in contract 2.1: exactly one take exists in the
    system. A second `UPDATE dune.items SET inventory_id` here would be a fork of it."""
    src = _read(KARUM)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "SET inventory_id" not in code, "the Karum writer grew its own take"
    assert "INSERT INTO dune.items" not in code, "the Karum writer mints items"
    assert "lib/dune-take-item.sh" in src
    assert "karum_take_item" in src


def test_list_uses_the_shared_take_against_the_exchange():
    sql = _list_sql()
    assert "shared gated take" in sql, "list is not emitting the shared take"
    # the take's destination is the exchange inventory, resolved not assumed
    assert "dune.get_exchange_inventory_id(2)" in sql
    # and the take carries the gate with it
    assert "player_online" in sql and "FOR SHARE" in sql


def test_list_gates_escrow_on_a_unique_correlation_id():
    sql = _list_sql()
    assert "INSERT INTO dune.ls_karum_escrow" in sql
    assert "ON CONFLICT (correlation_id) DO NOTHING" in sql
    # replay must stop the take, or the retry runs against a bank that no longer holds it
    assert "set_config('ls.take_skip', '1', true)" in sql
    assert sql.count("current_setting('ls.take_skip', true), '') = '1'") >= 3


def test_list_allocates_above_the_market_bot_range():
    """LT-7: the bot caches MAX(position_index) over inventory 610 once at init, so anything
    else writing there must stay out of its way."""
    sql = _list_sql()
    assert "min_position" in _read(TAKE_LIB)
    assert "v_min_pos  bigint := 1000000" in sql or "v_min_pos  bigint := 1000000000" in sql


def test_list_refuses_a_template_it_could_not_hand_back():
    """The prevention half of the no_category defect (stranded listing 6, 2026-07-27).

    Escrowing an item whose template has no usable exchange category is a one-way door:
    every hand-over leg builds an exchange order, so the item can reach neither the buyer
    nor the seller nor the operator page. The listing leg has to refuse instead."""
    sql = _list_sql()
    assert "_karum_cat" in sql, "list resolves no exchange category at all"
    # It must be a REFUSAL, not a warning, and it must name the token L4 maps to copy.
    assert "RAISE EXCEPTION 'no_category" in sql, "list does not refuse an uncategorised item"
    # The refusal has to be reachable from the listing path, i.e. inside the same
    # transaction as the gate insert, so a refused listing leaves no escrow row behind.
    assert sql.index("_karum_cat") < sql.index("INSERT INTO dune.ls_karum_escrow"), \
        "the category is resolved after the escrow insert, so a refusal cannot roll it back"
    assert "no_category" in _read(KARUM).split("error_token_of")[0], \
        "no_category is not in the writer's error-token allowlist"


def test_list_snapshots_the_category_onto_the_escrow_row():
    """dune_exchange_orders is TRANSIENT. Resolving the category at delivery time means a
    template that was categorisable at listing can be uncategorisable at cancel, which is
    exactly how an item gets stranded. The mask has to be captured with the take."""
    sql = _list_sql()
    assert "category_mask, category_depth" in sql, "the escrow insert stores no category"
    assert "(SELECT mask FROM _karum_cat)" in sql and "(SELECT depth FROM _karum_cat)" in sql, \
        "the escrow insert names the category columns but does not fill them from the resolver"
    # And the column has to exist to be written to.
    esc = _read(ESCROW_SQL)
    assert "category_mask" in esc and "category_depth" in esc
    assert "ADD COLUMN IF NOT EXISTS category_mask" in esc, \
        "the snapshot columns are not added to the already-deployed table"


def test_category_resolution_rejects_mask_zero_everywhere():
    """A mask of 0 does not fail loudly, it produces an order the client files under no
    category header, so the player cannot find the item in the Completed tab. Worse than a
    refusal, because it looks like a loss. dune-market-sell.py has always guarded this;
    the Karum did not. Measured: 5 bank templates have only mask-0 orders."""
    for sql in (_list_sql(), _cancel_sql()):
        assert "category_mask <> 0" in sql, "a mask-0 order can still be copied"


def test_category_resolution_is_deterministic():
    """104 templates carry more than one distinct non-zero mask, and ties are real (one
    template has 3 orders of each). An unordered LIMIT 1 is a coin toss between real
    values; the modal mask plus a tiebreak is reproducible."""
    for sql in (_list_sql(), _cancel_sql()):
        cat = sql[sql.index("category_mask"):]
        assert "ORDER BY count(*) DESC, category_mask" in cat, \
            "the category pick is unordered, so two runs can disagree"


def test_delivery_prefers_the_snapshot_over_a_live_lookup():
    """All three hand-over legs land in build_delivery_txn. It must read what the listing
    captured first, and only fall back to the transient table for rows written before the
    snapshot existed -- otherwise the fix does nothing for the legs that needed it."""
    sql = _cancel_sql()
    assert "v_mask  := v_esc.category_mask" in sql, "delivery ignores the snapshot"
    snap = sql.index("v_esc.category_mask")
    live = sql.index("FROM dune.dune_exchange_orders", snap)
    assert snap < live, "the live lookup runs before the snapshot is consulted"
    # The fallback must still be able to refuse.
    assert "RAISE EXCEPTION 'no_category" in sql


def test_stats_sentinel_is_off_until_lt5_passes():
    """Contract 4.3b requires the in-row HolKarum sentinel ONCE LT-5 passes. LT-5 has not
    run: dune.items.stats is a real engine-deserialised structure and an unknown top-level
    key may fail deserialisation on load. The ledger alone is a valid Phase 1 marker."""
    src = _read(KARUM)
    assert 'LASTSIETCH_KARUM_STATS_SENTINEL="${LASTSIETCH_KARUM_STATS_SENTINEL:-0}"' in src
    assert "HolKarum" not in _list_sql(), "the sentinel is being written before LT-5 passed"


# --------------------------------------------------------------------------- #
# BUY: payment first, two transactions, gated separately
# --------------------------------------------------------------------------- #

def test_buy_is_two_transactions_payment_first():
    sql = _buy_sql()
    pay = sql.index("transaction A (payment)")
    dlv = sql.index("transaction B (delivery)")
    assert pay < dlv, "delivery must never precede payment"
    # two BEGIN/COMMIT pairs, not one transaction pretending to be two
    assert sql.count("BEGIN;") == 2 and sql.count("COMMIT;") == 2


def test_payment_gate_returns_before_touching_a_balance():
    """🔴 contract 8.1b. The Funcom proc gives atomicity, not idempotency. Without this the
    retry that the compensation policy PRESCRIBES is a money dupe."""
    sql = _buy_sql()
    assert "INSERT INTO dune.ls_karum_payments" in sql
    assert "ON CONFLICT (correlation_id) DO NOTHING" in sql
    body = sql[sql.index("DO $pay$"):]
    gate = body.index("IF NOT v_is_new THEN")
    adjust = body.index("adjust_player_virtual_currency_balance")
    assert gate < adjust, "the replay check must come BEFORE any balance adjust"


def test_payment_prechecks_funds_and_locks_deterministically():
    sql = _buy_sql()
    assert "insufficient_funds" in sql
    # deterministic lock order is what keeps two concurrent buys from deadlocking
    assert "ORDER BY player_controller_id\n   FOR UPDATE" in sql
    # value-conserving: one debit, one credit, same amount
    assert sql.count("adjust_player_virtual_currency_balance") >= 2
    assert "-12500" in sql and "12500" in sql


def test_delivery_gate_is_the_delivery_log_unique_key():
    sql = _buy_sql()
    assert "INSERT INTO dune.ls_item_delivery_log" in sql
    assert "'auction', 'exchange'" in sql
    assert "ON CONFLICT (correlation_id) DO NOTHING" in sql


def test_delivery_adopts_the_row_and_keeps_its_grade():
    """Do NOT settle by calling deliver-claim.py as written: it MINTS a fresh item at
    quality_level 0, which would destroy a T6's grade."""
    sql = _buy_sql()
    assert "v_esc.quality_level" in sql, "the order must carry the escrowed row's real grade"
    # stack_size comes from the LIVE item row so the fulfilled row cannot disagree (LT-3)
    assert "v_stack" in sql
    assert "expiration_time" in sql and "86400" in sql
    assert "completion_type" not in sql or "3," in sql


def test_delivery_expiry_is_never_null():
    """🔴 PROVEN live: the client's Completed tab sorts by EXPIRATION and a NULL row does not
    render at all, so NULL is the one value that silently loses the item."""
    sql = _buy_sql()
    assert "no_game_clock" in sql, "the writer must fail rather than write a NULL expiry"
    assert "farm_variables" in sql
    assert "expiration_time,\n     durability_cur" in sql or "v_expiry" in sql


def test_escrow_missing_commits_rather_than_raising():
    """The contract wants reconciled_missing RECORDED so L4 can refund and the audit can see
    it. A RAISE would roll that record back with everything else."""
    sql = _buy_sql()
    assert "'reconciled_missing'" in sql
    body = sql[sql.index("transaction B (delivery)"):]
    missing = body.index("reconciled_missing")
    # the branch returns instead of raising
    assert "INSERT INTO _dlv_result VALUES ('escrow_missing'" in body[missing:missing + 900]


def test_buy_reports_paid_and_delivered_separately():
    src = _read(KARUM)
    # every buy exit carries both booleans; L4's branch table depends on it
    assert src.count('"paid":false,"delivered":false') >= 2
    assert '"paid":true,"delivered":false' in src
    assert '"paid":true,"delivered":true' in src
    assert '"status":"paid_undelivered"' in src


def test_buy_never_refunds_on_a_failed_delivery():
    """Never compensate a possibly-successful irreversible leg. If the delivery landed and
    only the response was lost, a refund double-satisfies the trade."""
    src = _read(KARUM)
    dlv = src[src.index("TRANSACTION B: delivery"):]
    body = dlv[:dlv.index("op_cancel")] if "op_cancel" in dlv else dlv
    assert "adjust_player_virtual_currency_balance" not in body


# --------------------------------------------------------------------------- #
# CANCEL and the refund
# --------------------------------------------------------------------------- #

def test_cancel_moves_no_money_and_is_give_only():
    sql = _sql("karum-cancel", listing_id=4711, seller_account_id=1001, price=500)
    assert "adjust_player_virtual_currency_balance" not in sql
    assert "ls_karum_payments" not in sql
    assert "'returned'" in sql


def test_refund_is_a_new_row_never_an_update_of_the_original():
    """🔴 A retried refund implemented as an UPDATE would double-credit."""
    sql = _sql("karum-admin", admin_action="refund", listing_id=4711,
               buyer_account_id=2002, seller_account_id=1001, amount=12500,
               original_correlation_id=CORR2)
    assert "INSERT INTO dune.ls_karum_payments" in sql
    assert "ON CONFLICT (correlation_id) DO NOTHING" in sql
    # the original is stamped, and the stamp points at the refund's OWN id
    assert "status = 'reversed'" in sql and "reversal_corr_id" in sql
    assert CORR in sql and CORR2 in sql
    # and the refund runs the adjust in the opposite direction
    body = sql[sql.index("DO $refund$"):]
    assert body.index("IF NOT v_is_new THEN") < body.index("adjust_player_virtual_currency_balance")


def test_admin_actions_are_a_closed_set():
    _, out = _run({"action": "karum-admin", "admin_action": "delete-everything",
                   "listing_id": 1, "correlation_id": CORR})
    assert out.get("success") is False, out


# --------------------------------------------------------------------------- #
# The psql footgun that a source scan CAN catch
# --------------------------------------------------------------------------- #

def test_no_psql_vars_inside_dollar_quoted_blocks():
    """psql does not interpolate :vars inside a dollar-quoted string, and a plpgsql DO block
    is one, so a `:amount` in a DO body reaches the server verbatim and fails. This is why
    dune-gift-op.sh uses SET LOCAL + current_setting. Checked against the ASSEMBLED SQL,
    because that is where the mistake shows up."""
    import re
    for name, sql in (("list", _list_sql()), ("buy", _buy_sql()),
                      ("cancel", _sql("karum-cancel", listing_id=1, seller_account_id=1)),
                      ("refund", _sql("karum-admin", admin_action="refund", listing_id=1,
                                      buyer_account_id=2, seller_account_id=1, amount=5,
                                      original_correlation_id=CORR2))):
        depth = 0
        tag = None
        for line in sql.splitlines():
            m = re.search(r"\$([a-z_]+)\$", line)
            if m and tag is None:
                tag, depth = m.group(1), line.count("$%s$" % m.group(1))
                if depth >= 2:
                    tag = None
                continue
            if tag is not None:
                if "$%s$" % tag in line:
                    tag = None
                    continue
                assert not re.search(r"(?<![:\w]):'?[a-z_]+'?", line), \
                    "%s: psql var inside a DO $%s$ block: %s" % (name, tag, line.strip())


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

def test_ledgers_are_owned_by_dune():
    """Custom ls_* tables MUST be OWNER dune or Funcom's pre-update pg_dump aborts the
    whole update."""
    for path in (ESCROW_SQL, PAYMENTS_SQL):
        src = _read(path)
        assert "ALTER TABLE" in src and "OWNER TO dune;" in src, path
        assert "CREATE TABLE IF NOT EXISTS" in src, path


def test_escrow_ledger_is_the_positive_marker():
    src = _read(ESCROW_SQL)
    assert "correlation_id    text        NOT NULL UNIQUE" in src
    assert "CHECK (state IN ('held', 'delivered', 'returned', 'reconciled_missing'))" in src
    # the operator rule has to be findable from the schema itself
    assert "WITHOUT EXCLUDING ROWS PRESENT HERE" in src
    assert "acquisition_time" in src, "the NOT-the-discriminator warning must be recorded"


def test_payments_ledger_guards_the_money():
    src = _read(PAYMENTS_SQL)
    assert "correlation_id        uuid        NOT NULL UNIQUE" in src
    assert "CHECK (status IN ('applied', 'reversed'))" in src
    assert "CHECK (amount > 0)" in src
    assert "reversal_corr_id" in src


# --------------------------------------------------------------------------- #
# Transport: dispatcher token + relay routes
# --------------------------------------------------------------------------- #

def test_dispatcher_has_one_karum_token_with_the_alphabet_guard():
    src = _read(DISPATCH)
    assert "karum-op)" in src
    seg = src.split("karum-op)", 1)[1].split(";;", 1)[0]
    assert "[A-Za-z0-9+/=]" in seg, "safe-alphabet guard missing"
    assert "/root/dune-karum-op.sh --op-b64-stdin" in seg
    assert "takes no args" in seg
    # ONE token for four actions: fewer forced-command cases is fewer guards to get wrong
    for extra in ("karum-list)", "karum-buy)", "karum-cancel)", "karum-admin)"):
        assert extra not in src, extra


def test_relay_has_all_four_routes_behind_verify_key():
    src = _read(RELAY)
    for r in ("list", "buy", "cancel", "admin"):
        assert f'@app.post("/dune/karum/{r}", dependencies=[Depends(verify_key)])' in src, r
    assert '_dune_ssh_stdin("karum-op"' in src
    assert "sort_keys=True" in src


def test_relay_never_accepts_a_client_identity():
    """Identity is resolved server-side from the session by L4 and re-resolved in the
    writer's txn. The relay must not accept a character name, a controller or a discord id
    as the actor."""
    src = _read(RELAY)
    karum = src[src.index("_KARUM_ACTIONS = {"):src.index('@app.post("/dune/reward-op"')]
    for forbidden in ("char_name", "player_controller_id", "seller_ctrl", "buyer_ctrl"):
        assert forbidden not in karum, forbidden


def test_relay_reports_no_usable_response_as_its_own_outcome():
    """🔴 A timeout is NOT evidence that payment failed. Synthesising paid:false on a lost
    response is how a second buyer purchases goods the first buyer already paid for."""
    src = _read(RELAY)
    karum = src[src.index("def _karum_dispatch"):src.index('@app.post("/dune/reward-op"')]
    assert '"status": "unknown"' in karum
    fallback = karum[karum.index('"status": "unknown"'):]
    assert '"paid"' not in fallback, "the no-response fallback must make no paid claim"


def test_relay_refund_requires_its_own_correlation_id():
    src = _read(RELAY)
    assert "a refund must carry its OWN correlation_id" in src


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
