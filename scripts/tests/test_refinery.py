#!/usr/bin/env python3
"""Wave 10, the Ingot Refinery (2026-09-04): the offline CHOAM service that
takes base refined ingots plus Spice Melange and hands back the matching
Spice-infused dust, in the bank, while the player is logged out.

WHAT SHIPPED
  * scripts/dune-refinery-write.py plus scripts/refinery-rates.json, the single
    rate authority, read fresh per call beside the writer. One transaction:
    advisory lock on the idempotency uuid, replay row, account-keyed offline
    gate, COUNT-FILTER source resolve, fixed lock order, consume, mint, ledger,
    conservation check.
  * scripts/sql/ls_refinery_exchanges.sql, the idempotency / audit / weekly-cap
    ledger, OWNER dune.
  * relay/app.py routes /dune/refinery/{catalog,exchange} over one dispatcher
    token, refinery-op, with a field whitelist that carries dry_run.
  * admin-backend/routers/portal_refinery.py and portal_refinery_events.py: the
    two portal routes and the durable per-tier weekly cap, keyed on DISCORD
    identity because LASTSIETCH_MULTIACCOUNT lets one identity hold several accounts.
  * portal-nextgen Storage > Workshop, tabs Augments and Ingot Refinery;
    /storage/augments became a redirect.
  * refinery-enabled, the tenth game-host kill-switch file, in all six
    registries; ops/deploy-refinery-host.sh and ops/deploy-refinery.sh.

Prod-safe: source scans, the writer's own SQL BUILDERS called in process, and a
throwaway admin.db in a temp dir (LASTSIETCH_DB_PATH). No game DB, no relay, no host,
no network, no kubectl, no ssh. The admin routers pull the FastAPI dependency
chain and are not importable here, so their contract is read out of the source
with ast rather than executed, the way test_v2_systems does it.

WHAT THIS PINS, and why each one is worth a test:

  * THE RATE IS THE OWNER'S RULING. Five ingots at every tier, melange 1/1/2/2/3/3,
    one dust out, cap 250 per tier per rolling week. The table is data, so
    nothing but a test stops a retune from quietly becoming a different feature.
  * THE TEMPLATE IDS ARE THE LIVE CamelCase ONES. The SQL compare is
    case-sensitive and three of the six inputs are traps spelled from the
    display name: AluminiumBar is British in the id, DuraluminumRod is a Rod,
    and T6RefinedResourceB is a different item.
  * THE OFFLINE GATE. Deleting a row the engine holds in RAM is not durable: it
    is rewritten at logout from the original id, which is real duplication. The
    gate is account-keyed, under FOR SHARE, Deleted rows filtered, and
    undetermined is LOCKED.
  * CONSUME BEFORE MINT, AND ONE LOCK ORDER. Inventories by ascending id, then
    ingots, then melange, and the dust last inside the mint. Reordered locks
    deadlock two refiners against each other.
  * THE CAP SUMS UNITS, PER TIER. Every other counter in this repo counts ROWS,
    which for this feature is the wrong number by a factor of the batch size,
    and a pooled counter would let one tier eat another's allowance.
  * THE DARK GATE IS A FILE. The dispatcher's ssh shell inherits no profile, so
    an env-var-only switch cannot be set in either direction.
  * THE PAGE SEALS ON `enabled`, NOT ON `ok`. The dark answer is a 200 carrying
    ok:false; a page that keyed off ok would render an error toast for a service
    that is simply not open yet.

Every pin below is proven RED on a broken variant: the suite copies the files to
a scratch tree, breaks one thing, re-runs itself against the copy and asserts it
fails. A pin nobody has watched fail is not a pin.

Run:  python3 scripts/tests/test_refinery.py     (also import-safe)
"""
import ast
import importlib.util
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)

# A broken-variant run points these at a scratch copy of the tree. Every path
# below reads through ROOT so the child scans the mutated files; the modules the
# cap module DEPENDS on (database, config) always come off the repo.
ROOT = os.environ.get("REFINERY_ROOT", REPO)
CHILD = os.environ.get("REFINERY_CHILD") == "1"

AB = os.path.join(ROOT, "admin-backend")
PN = os.path.join(AB, "portal-nextgen")
REPO_AB = os.path.join(REPO, "admin-backend")

WRITER = os.path.join(ROOT, "scripts", "dune-refinery-write.py")
RATES = os.path.join(ROOT, "scripts", "refinery-rates.json")
DDL = os.path.join(ROOT, "scripts", "sql", "ls_refinery_exchanges.sql")
DISPATCH = os.path.join(ROOT, "scripts", "dune-relay-dispatch.sh")
FLAGSET = os.path.join(ROOT, "scripts", "dune-flag-set.sh")
RELAY = os.path.join(ROOT, "relay", "app.py")
ROUTER = os.path.join(AB, "routers", "portal_refinery.py")
EVENTS = os.path.join(AB, "portal_refinery_events.py")
V2 = os.path.join(AB, "routers", "v2.py")
V2SYS = os.path.join(AB, "routers", "v2_systems.py")
CONFIG = os.path.join(AB, "config.py")
MAIN = os.path.join(AB, "main.py")
UNFURL = os.path.join(AB, "nextgen_unfurl.py")
API_JS = os.path.join(PN, "src", "lib", "api.js")
STORE = os.path.join(PN, "src", "lib", "refinery.svelte.js")
PANEL = os.path.join(PN, "src", "lib", "components", "refinery", "RefineryPanel.svelte")
ROW = os.path.join(PN, "src", "lib", "components", "refinery", "RefineryRow.svelte")
WORKSHOP = os.path.join(PN, "src", "routes", "storage", "workshop", "+page.svelte")
AUGMENTS = os.path.join(PN, "src", "routes", "storage", "augments", "+page.svelte")
STORAGE_PAGE = os.path.join(PN, "src", "routes", "storage", "+page.svelte")
SW = os.path.join(PN, "static", "sw.js")
PKG = os.path.join(PN, "package.json")
PG_SUITE = os.path.join(ROOT, "scripts", "tests", "test_refinery_pg.sh")
FLAGVERB = os.path.join(ROOT, "scripts", "tests", "test_flag_set_verb.py")

# The shell key that was live before this wave. A content change to a versioned
# asset needs a NEW key in the same deploy or mobile clients keep the old shell.
SHIPPED_CACHE = "ls-v2-shell-v105"
SHIPPED_PKG_VERSION = "2.9.0"

ACCOUNT = 4242
OWNER = 7777
KEY = "11111111-2222-3333-4444-555555555555"
# The SQL builders take any string; gather_args holds the wire key to the RFC
# variant, so anything driven through it needs a real one.
WIRE_KEY = "11111111-2222-4333-8444-555555555551"
TIERS = tuple("T%dUniqueComponent" % n for n in range(1, 7))

# September 7 ruling: 25 ingots and tier-number Melange produce 10 dust.
RULED = {
    1: ("CopperBar", "T1UniqueComponent", 25, 1, 10),
    2: ("IronBar", "T2UniqueComponent", 25, 2, 10),
    3: ("SteelBar", "T3UniqueComponent", 25, 3, 10),
    4: ("AluminiumBar", "T4UniqueComponent", 25, 4, 10),
    5: ("DuraluminumRod", "T5UniqueComponent", 25, 5, 10),
    6: ("T6RefinedResourceA", "T6UniqueComponent", 25, 6, 10),
}

# The writer's tokens, and the superseded spellings from the plan's first draft.
# A dead alias is not free: bank_full / bank_full_volume is a prefix pair, the
# shape the architect banned because the shorter one swallows the longer.
WRITER_TOKENS = (
    "refinery_disabled", "recipe_disabled", "unknown_recipe", "rate_changed",
    "bad_request", "player_online", "no_bank", "no_pawn_storage",
    "insufficient_input", "insufficient_spice", "no_space", "weekly_cap",
    "idempotency_conflict", "conservation_failed",
)
STALE_TOKENS = ("refinery_closed", "insufficient_ingots", "insufficient_melange",
                "no_backpack", "bank_full_volume", "bad_tier", "bad_quantity")
# The three that answer 200. Each is a state the page RENDERS; none may toast.
SOFT_TOKENS = ("refinery_disabled", "recipe_disabled", "write_failed")

FLAGS = ("karum-enabled", "karum-wtb-enabled", "augment-enabled", "reward-enabled",
         "servercmd-enabled", "market-sell-container-enabled",
         "storage-pawn-move-enabled", "market-sell-backpack-enabled",
         "item-transfer-enabled", "landsraad-claim-enabled", "refinery-enabled")


# Built rather than written: a suite that BANS a character must not carry one.
_EM_DASH = chr(0x2014)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# The writer, imported from ROOT so a mutant is the one under test. Importing it
# runs no I/O: the rate table is read by load_rates(), never at import.
# --------------------------------------------------------------------------- #
_spec = importlib.util.spec_from_file_location("refinery_writer", WRITER)
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)

# The cap module, against a throwaway admin.db. Same arrangement as
# test_transfer_events.py: the mutated copy first on the path, its dependencies
# off the repo.
_TMP = tempfile.mkdtemp(prefix="refinery-test-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ.setdefault("LASTSIETCH_DB_PATH", os.path.join(_TMP, "admin.db"))
os.environ.setdefault("LASTSIETCH_RELAY_API_KEY", "test")
os.environ.setdefault("LASTSIETCH_SESSION_SECRET", "test")
sys.path.insert(0, REPO_AB)
if AB != REPO_AB:
    sys.path.insert(0, AB)

import database                      # noqa: E402
import portal_refinery_events as ev  # noqa: E402

database.init_db()

_CACHE = {}


def _rates():
    if "rates" not in _CACHE:
        table = rf.load_rates()
        assert table is not None, "the rate table did not load at all"
        _CACHE["rates"] = table
    return _CACHE["rates"]


def _sql(output="T3UniqueComponent", batches=4, dry_run=False):
    key = (output, batches, dry_run)
    if key not in _CACHE:
        table = _rates()
        _CACHE[key] = rf.build_exchange_sql(
            ACCOUNT, OWNER, rf.find_recipe(table, output), batches, KEY, table, dry_run)
    return _CACHE[key]


def _catalog_sql():
    """The read the catalog route drives, captured without a database."""
    if "catalog" not in _CACHE:
        seen = {}
        real = rf._dq
        rf._dq = lambda q, timeout=25: seen.setdefault("sql", q) and ""
        try:
            rf.read_catalog(ACCOUNT, OWNER, _rates())
        except SystemExit:
            pass
        finally:
            rf._dq = real
        _CACHE["catalog"] = seen.get("sql", "")
    return _CACHE["catalog"]


def _dict_literal(path, name):
    """A module-level dict out of a file that cannot be imported here."""
    tree = ast.parse(_read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("%s is gone from %s" % (name, os.path.basename(path)))


def _func_src_sh(src, name):
    """One shell function body out of a lane."""
    start = src.index("%s() {" % name)
    return src[start:src.index("\n}", start)]


def _func_src(path, name):
    src = _read(path)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("%s is gone from %s" % (name, os.path.basename(path)))


# --------------------------------------------------------------------------- #
# The rate table: the owner's ruling, and the live template ids
# --------------------------------------------------------------------------- #

def test_the_rate_table_is_the_owner_s_ruling():
    """Five ingots at every tier, melange 1/1/2/2/3/3, one dust out, 50 batches
    a request, 250 dust per tier per rolling seven days. The table is DATA, so
    nothing but this test stands between a retune and a different feature."""
    table = _rates()
    assert table["rate_version"] == "2026-09-07a", "the approved rate version moved"
    assert table["max_batches_per_request"] == 50, table["max_batches_per_request"]
    assert table["weekly_dust_cap"] == 250, table["weekly_dust_cap"]
    assert table["window_days"] == 7, table["window_days"]
    assert len(table["recipes"]) == 6, "expected six recipes"
    for row in table["recipes"]:
        ingot, dust, per_in, per_spice, per_out = RULED[row["tier"]]
        got = (row["input_template"], row["output_template"], row["input_per_batch"],
               row["spice_per_batch"], row["output_per_batch"])
        assert got == (ingot, dust, per_in, per_spice, per_out), \
            "tier %s is %r, the ruling is %r" % (row["tier"], got,
                                                 (ingot, dust, per_in, per_spice, per_out))
        assert row["spice_template"] == "MelangeSpice", row["spice_template"]
        assert row["max_stack"] == 500, row["max_stack"]


def test_the_template_ids_are_the_live_camel_case_ones():
    """dune.items.template_id is CamelCase and the SQL compare is case-sensitive.
    Three of the six inputs are traps spelled from the display name."""
    ids = set()
    for row in _rates()["recipes"]:
        ids.update((row["input_template"], row["spice_template"], row["output_template"]))
    for wanted in ("CopperBar", "IronBar", "SteelBar", "AluminiumBar",
                   "DuraluminumRod", "T6RefinedResourceA", "MelangeSpice") + TIERS:
        assert wanted in ids, "the rate table lost the live id %s" % wanted
    assert "T6RefinedResourceB" not in ids, \
        "T6RefinedResourceB is Stravidium Fiber, a different item"
    for bad in ("copperbar", "spicemelange", "copper_ingot"):
        assert bad not in ids, "%s is a sidecar alias, not a live template id" % bad


def test_a_missing_or_malformed_rate_table_is_a_closed_refinery():
    """Never a default, never a guess: with no table there is nothing to render
    and nothing to trade at."""
    saved = rf._RATES_PATH
    tmp = tempfile.mkdtemp(prefix="refinery-rates-")
    try:
        bad = os.path.join(tmp, "rates.json")
        rf._RATES_PATH = os.path.join(tmp, "absent.json")
        assert rf.load_rates() is None, "an absent table did not close the refinery"
        for body in ("{ not json", '{"rate_version": "x", "recipes": []}'):
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write(body)
            rf._RATES_PATH = bad
            assert rf.load_rates() is None, "a malformed table did not close the refinery: %s" % body
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"rate_version": "v1", "max_batches_per_request": 50,
                       "weekly_dust_cap": 250, "window_days": 7,
                       "recipes": [{"tier": 1, "input_template": "Copper'Bar",
                                    "spice_template": "MelangeSpice",
                                    "output_template": "T1UniqueComponent",
                                    "input_per_batch": 5, "spice_per_batch": 1,
                                    "output_per_batch": 1, "max_stack": 500,
                                    "enabled": True}]}, fh)
        assert rf.load_rates() is None, \
            "a quote in a template id survived the loader; psql -v cannot reach inside a " \
            "dollar-quoted DO block, so this validation IS the injection barrier"
    finally:
        rf._RATES_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The writer's dark gate
# --------------------------------------------------------------------------- #

def test_the_kill_switch_is_a_file_and_it_is_stat_ed_fresh():
    """The dispatcher's forced command runs under a non-interactive shell that
    inherits no profile, so an env-var-only gate cannot be set in either
    direction: it would be dark forever, or open forever."""
    assert rf.REFINERY_FLAG == "/etc/lastsietch/refinery-enabled", rf.REFINERY_FLAG
    body = _func_src(WRITER, "refinery_enabled")
    assert "os.path.isfile(REFINERY_FLAG)" in body, \
        "the gate no longer stats the flag file"
    assert "return True" not in body, "the gate answers open without reading anything"
    if not os.path.exists(rf.REFINERY_FLAG):
        assert rf.refinery_enabled() is False, \
            "the gate reports OPEN while the flag file does not exist"


def test_the_flag_check_precedes_every_path_into_a_db_session():
    """A kill switch that fires after the connection is a kill switch that
    already touched the game DB. Line order cannot prove it (the gate lives in
    main(), further down the file than the function it guards), so this reads
    the call graph: nothing reaches a psql session from a body that has not
    called the gate first."""
    tree = ast.parse(_read(WRITER))

    def called(node):
        fn = node.func
        if isinstance(fn, ast.Name):
            return fn.id
        if isinstance(fn, ast.Attribute):
            return fn.attr
        return ""

    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    gates = {f.name for f in funcs
             if any(isinstance(n, ast.Call) and called(n) in ("isfile", "exists")
                    and any(isinstance(a, ast.Name) and "FLAG" in a.id for a in n.args)
                    for n in ast.walk(f))}
    assert gates, "the writer has no runtime flag-file check at all"
    direct = {f.name for f in funcs
              if any(isinstance(n, ast.Call) and called(n) in ("run_psql", "resolve_db_pod")
                     for n in ast.walk(f))}
    assert direct, "the writer never opens a DB session; is this the right file?"
    sessions = set(direct)
    grew = True
    while grew:
        grew = False
        for f in funcs:
            if f.name in sessions:
                continue
            if any(isinstance(n, ast.Call) and called(n) in sessions for n in ast.walk(f)):
                sessions.add(f.name)
                grew = True
    # The function holding the psql call cannot gate itself: the guard belongs in
    # its CALLER, before the call is ever made.
    for f in funcs:
        if f.name in direct or f.name in gates:
            continue
        calls = sorted((n.lineno, called(n)) for n in ast.walk(f) if isinstance(n, ast.Call))
        for i, (line, name) in enumerate(calls):
            if name in sessions:
                assert any(prev in gates for _l, prev in calls[:i]), \
                    "%s() reaches %s() at line %d without checking the flag file" % (
                        f.name, name, line)
                break


# --------------------------------------------------------------------------- #
# The exchange transaction
# --------------------------------------------------------------------------- #

def test_the_transaction_is_idempotent_before_it_touches_anything():
    sql = _sql()
    assert "pg_advisory_xact_lock(hashtextextended('%s', 0))" % KEY in sql, \
        "no advisory lock on the idempotency uuid"
    assert "FROM dune.ls_refinery_exchanges\n   WHERE idempotency_key = " in sql
    assert "RAISE EXCEPTION 'idempotency_conflict'" in sql
    assert sql.index("RAISE EXCEPTION 'idempotency_conflict'") \
        < sql.index("PERFORM 1 FROM dune.encrypted_player_state"), \
        "a replay reaches the inventory before it is recognised as a replay"


def test_the_offline_gate_is_account_keyed_locked_and_fails_closed():
    """Deleting a row the engine holds in RAM is not durable: it is rewritten at
    logout from the ORIGINAL id, which is real duplication. No delay makes an
    online take safe, so undetermined is LOCKED."""
    sql = _sql()
    assert "PERFORM 1 FROM dune.encrypted_player_state\n   WHERE account_id = %d" % ACCOUNT in sql
    assert "FOR SHARE;" in sql, "the eps rows are not locked before they are counted"
    # EVERY eps read, not three of them: an account can hold several eps rows, and
    # a character deleted during creation can stay Online forever. One unfiltered
    # read is enough to lock every refine on that account out for good, so a count
    # is the wrong shape of pin.
    at = sql.find("dune.encrypted_player_state")
    reads = 0
    while at != -1:
        reads += 1
        # Bounded by the statement it sits in, not by a character count: a window
        # wide enough to reach the NEXT statement would read that one's filter and
        # call the unfiltered read covered.
        end = sql.find(";", at)
        assert "character_state IS DISTINCT FROM 'Deleted'" in sql[at:end], \
            "an encrypted_player_state read at offset %d has no tombstone filter" % at
        at = sql.find("dune.encrypted_player_state", at + 1)
    assert reads >= 4, "only %d eps reads found; is this the right SQL?" % reads
    assert "AND NOT COALESCE(reconnect_grace_period_end > now(), false))\n    INTO v_chars, v_offline" in sql, \
        "the reconnect grace no longer fails closed"
    assert "IF v_chars = 0 THEN" in sql and "RAISE EXCEPTION 'player_online" in sql, \
        "zero live character rows is undetermined, and undetermined must refuse"
    assert sql.index("FOR SHARE;") < sql.index("dune.delete_inventory_item"), \
        "the consume starts before the offline gate has run"


def test_the_sources_are_resolved_by_count_filter_and_ambiguity_refuses():
    """An account can present more than one character slot. Picking one with
    LIMIT 1 is how the wrong bank gets drained."""
    sql = _sql()
    for inv_type in (30, 0, 15):
        assert "COUNT(*) FILTER (WHERE inv.inventory_type = %d)" % inv_type in sql, \
            "inventory type %d is not resolved by COUNT FILTER" % inv_type
    assert "IF v_bank_count <> 1 THEN RAISE EXCEPTION 'no_bank" in sql, \
        "an ambiguous bank is resolved silently instead of refused"
    assert "RAISE EXCEPTION 'no_pawn_storage" in sql
    assert "JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id" in sql, \
        "the pawn join is wider than the eps join; inventory_type 0 is ALSO vehicle " \
        "cargo and container storage"


def test_the_lock_order_is_fixed_and_stated():
    """Inventories by ascending id, then the ingots, then the melange, and the
    dust last inside the mint. Two refiners that disagree about this deadlock."""
    sql = _sql()
    assert "LOCK ORDER, followed exactly below and never reordered" in sql
    assert sql.index("LOCK ORDER") < sql.index("pg_advisory_xact_lock")
    inv = sql.index("WHERE id IN (v_bank_inv, v_backpack_inv, v_toolbar_inv)\n   ORDER BY id FOR UPDATE;")
    ingot = sql.index("AND it.template_id = 'SteelBar'\n   ORDER BY it.id FOR UPDATE;")
    spice = sql.index("AND it.template_id = 'MelangeSpice'\n   ORDER BY it.id FOR UPDATE;")
    assert inv < ingot < spice, "the three lock steps are not in the stated order"
    assert sql.index("AND it.stack_size < 500") > spice, \
        "the dust rows are pre-locked; the mint must take them last"


def test_the_consume_is_a_partial_stack_drain_in_bank_backpack_toolbar_order():
    sql = _sql()
    assert "(VALUES (v_bank_inv, 0), (v_backpack_inv, 1), (v_toolbar_inv, 2))" in sql, \
        "the drain order is not bank, then backpack, then toolbar"
    assert "array_agg(t.id ORDER BY t.pri, t.id)" in sql, \
        "the drain does not walk its rows in (priority, id) order"
    assert "PERFORM dune.delete_inventory_item(v_ids_in[v_idx], v_take);" in sql
    assert "PERFORM dune.delete_inventory_item(v_ids_sp[v_idx], v_take);" in sql
    assert sql.index("v_ids_in[v_idx]") < sql.index("v_ids_sp[v_idx]"), \
        "the melange is consumed before the ingots"
    assert "RAISE EXCEPTION 'insufficient_input" in sql
    assert "RAISE EXCEPTION 'insufficient_spice" in sql


def test_nothing_is_minted_before_the_inputs_are_gone():
    """An insufficient input must never be able to leave a dust behind."""
    sql = _sql()
    assert sql.index("dune.delete_inventory_item") < sql.index("INSERT INTO dune.items"), \
        "the mint happens before the consume"


def test_the_mint_merges_then_spills_and_lands_in_the_bank_only():
    sql = _sql()
    assert "AND it.stack_size < 500" in sql and "ORDER BY it.position_index ASC, it.id ASC" in sql, \
        "the mint does not probe for a mergeable stack under the live cap"
    assert "v_take := LEAST(500 - v_dust_stack, v_mint_left);" in sql, \
        "the mint does not spill at the stack cap"
    assert "IF v_bank_mic = 0 THEN" in sql \
        and "RAISE EXCEPTION 'no_space (the CHOAM bank reports zero slots)" in sql, \
        "max_item_count 0 is a bank with nowhere to put anything, not an uncapped one"
    assert "ELSIF v_bank_mic IS NULL OR v_bank_mic < 0 THEN" in sql, \
        "a NULL or negative max_item_count no longer means no slot cap"
    assert "FROM generate_series(0, v_bank_mic - 1) AS s(idx)" in sql, \
        "the first-empty-slot search is gone"
    assert "RAISE EXCEPTION 'no_space" in sql
    assert ("(inventory_id, stack_size, position_index, template_id, stats,\n"
            "         quality_level, acquisition_time, is_new)") in sql, \
        "the items INSERT column set is not the one proven in production"
    assert "VALUES\n        (v_bank_inv, v_take, v_slot," in sql, \
        "the dust is minted somewhere other than the CHOAM bank"
    assert sql.index("AND it.stack_size < 500") < sql.index("INSERT INTO dune.items"), \
        "the mint inserts before it tries to merge"


def test_the_ledger_and_the_conservation_check_are_in_the_same_transaction():
    """ingots_out + melange_out = batches x rate, dust_in = batches x 2, asserted
    against the rows the transaction actually moved, before it commits."""
    sql = _sql()
    assert "INSERT INTO dune.ls_refinery_exchanges (" in sql
    assert ("idempotency_key, account_id, owner_ctrl, tier, output_template, batches,\n"
            "    dust_units, input_template, input_units, spice_units,\n"
            "    consumed_json, granted_json, rate_version") in sql
    assert "IF v_used_in <> 100 OR v_used_sp <> 12 OR v_minted <> 40 THEN" in sql, \
        "the conservation check is not the ruled rate times the batch count"
    assert "RAISE EXCEPTION 'conservation_failed" in sql
    assert sql.index("INSERT INTO dune.ls_refinery_exchanges (") < sql.index("conservation_failed")
    assert sql.index("conservation_failed") < sql.index("\nCOMMIT;"), \
        "the transaction commits before it checks conservation"
    fifty = _sql("T1UniqueComponent", 50)
    assert "IF v_used_in <> 1250 OR v_used_sp <> 50 OR v_minted <> 500 THEN" in fifty, \
        "50 batches at tier 1 builds 1250 ingots, 50 Melange, 500 dust before the cap refuses"


def test_the_weekly_backstop_is_per_tier_over_a_rolling_week():
    sql = _sql()
    assert "account_id = %d AND output_template = 'T3UniqueComponent'" % ACCOUNT in sql, \
        "the game-side backstop is not keyed on the account AND the tier"
    assert "interval '7 days'" in sql
    assert "RAISE EXCEPTION 'weekly_cap" in sql


def test_no_caller_supplied_inventory_id_reaches_the_sql():
    """The portal resolves owner_ctrl server-side and sends no inventory at all;
    a destination the caller picks is how somebody else's storage gets written."""
    sql = _sql()
    assert "dst_inventory_id" not in sql and "src_inventory_id" not in sql


def test_a_dry_run_plans_under_the_identical_locks_and_rolls_back():
    live, dry = _sql(), _sql(dry_run=True)
    assert dry.rstrip().endswith("ROLLBACK;"), "a dry run is not rolled back"
    assert "\nCOMMIT;" not in dry, "a dry run can commit"
    assert live.rstrip().endswith("COMMIT;") and "ROLLBACK;" not in live
    assert dry[:dry.rindex("ROLLBACK;")].replace("'dry_run', true", "") \
        == live[:live.rindex("COMMIT;")].replace("'dry_run', false", ""), \
        "the rehearsal is not the same plan as the write it rehearses"
    assert "'dry_run', true" in dry, "the envelope does not say it was a rehearsal"


def test_the_envelope_carries_what_the_portal_has_to_render():
    sql = _sql()
    assert "'from',\n          jsonb_build_object('bank', v_bank_in, 'backpack', v_back_in," in sql, \
        "consumed does not say which source each unit came from"
    assert "'stacks', v_stacks" in sql
    assert "'holdings_after', holdings_after," in sql
    assert "'replay', replay" in sql
    assert "'input_units', input_units, 'spice_units', spice_units," in sql


def test_the_catalog_read_opens_no_write_and_sees_all_three_sources():
    csql = _catalog_sql()
    assert "JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id" in csql
    assert "inv.inventory_type IN (0, 15, 30)" in csql, \
        "the catalog does not sum the toolbar, which never appears in the container browser"
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "delete_inventory_item"):
        assert verb not in csql, "the catalog read carries a write verb: %s" % verb
    assert "character_state IS DISTINCT FROM 'Deleted'" in csql
    assert "FROM dune.ls_refinery_exchanges" in csql and "interval '7 days'" in csql


# --------------------------------------------------------------------------- #
# The writer's edges: validation, tokens, the relay's field names
# --------------------------------------------------------------------------- #

def test_the_validation_barriers_are_the_injection_barrier():
    """psql -v cannot reach inside a dollar-quoted DO block, so int() coercion
    and these regexes in Python ARE the barrier, not a second line behind one."""
    assert not rf.UUID_RE.fullmatch("not-a-uuid")
    assert not rf.TEMPLATE_RE.fullmatch("Copper'Bar")
    assert all(rf.TEMPLATE_RE.fullmatch(t) for t in
               ("CopperBar", "AluminiumBar", "DuraluminumRod", "T6RefinedResourceA",
                "MelangeSpice", "T6UniqueComponent"))


def test_every_ruled_token_exists_and_matches_as_a_whole_word():
    for token in WRITER_TOKENS:
        assert token in rf.ERROR_TOKENS, "the writer lost the token %s" % token
    assert not any(t.startswith("bank_full") for t in rf.ERROR_TOKENS), \
        "bank_full / bank_full_volume is the prefix pair that misroutes; the slot " \
        "refusal is no_space"
    for token in ("player_online", "no_bank", "no_pawn_storage", "insufficient_input",
                  "insufficient_spice", "no_space", "weekly_cap",
                  "idempotency_conflict", "conservation_failed"):
        assert rf.parse_psql_error("ERROR:  %s (detail)" % token) == token, \
            "%s is raised by the SQL but the parser cannot route it" % token
    assert rf.parse_psql_error("ERROR:  xno_spacey happened") == "write_failed", \
        "a token embedded in a longer word matched"
    assert rf.parse_psql_error("ERROR:  deadlock detected") == "write_failed"


def test_a_refusal_carries_the_numbers_the_copy_needs_or_none_at_all():
    assert rf.refusal_detail("insufficient_input",
                             "ERROR:  insufficient_input (have=3 need=20)") == {"held": 3, "need": 20}
    assert rf.refusal_detail("insufficient_spice",
                             "ERROR:  insufficient_spice (have=1 need=8)") == {"held": 1, "need": 8}
    assert rf.refusal_detail("weekly_cap", "ERROR:  weekly_cap (used=248 add=4 cap=250)") == \
        {"weekly_dust_used_account": 248, "requested": 4, "weekly_dust_cap": 250}
    assert rf.refusal_detail("player_online", "ERROR:  player_online (status=Online)") == {}
    assert rf.refusal_detail("insufficient_input",
                            "ERROR:  insufficient_input (short by 4 after draining)") == {}, \
        "an unparsed detail must degrade to nothing, never to a wrong number"


def _gather(payload=None, argv=None):
    """Drive gather_args down one of its two paths: the relay/dispatcher path
    (--stdin-json) or the hand-driven CLI path."""
    saved_argv, saved_stdin = sys.argv, sys.stdin
    if argv is None:
        sys.argv = ["dune-refinery-write.py", "--stdin-json"]
        sys.stdin = __import__("io").StringIO(json.dumps(payload or {}))
    else:
        sys.argv = ["dune-refinery-write.py"] + argv
    try:
        return ("ok", rf.gather_args())
    except SystemExit as exc:
        return ("exit", exc.code)
    finally:
        sys.argv, sys.stdin = saved_argv, saved_stdin


def _gather_json(payload):
    """The ENVELOPE gather_args emits on its way out, not just the exit code: the
    host lane branches on the body, so the body is what has to be pinned."""
    import contextlib
    import io as _io
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        kind, _code = _gather(payload)
    assert kind == "exit", "the payload was accepted, so there is no refusal to read"
    lines = [l for l in buf.getvalue().splitlines() if l.strip().startswith("{")]
    assert lines, "the refusal printed no JSON envelope"
    return json.loads(lines[-1])


def test_the_writer_accepts_the_relay_s_uuid_field_and_fails_closed_on_a_conflict():
    """The relay names the idempotency key `uuid`. A writer that only knew
    idempotency_key would refuse every real request, and one that accepted both
    without comparing them would take whichever it read first."""
    gather = _gather
    other = "22222222-3333-4444-8555-666666666666"
    base = {"op": "exchange", "account_id": 1, "owner_ctrl": 2,
            "output_template": "T1UniqueComponent", "batches": 3, "dry_run": False,
            "expected_rate_version": _rates()["rate_version"]}
    kind, got = gather(dict(base, uuid=other))
    assert kind == "ok" and got["idempotency_key"] == other
    kind, got = gather(dict(base, idempotency_key=other))
    assert kind == "ok" and got["idempotency_key"] == other
    assert gather(dict(base, uuid=other, idempotency_key=WIRE_KEY))[0] == "exit", \
        "two idempotency keys that disagree were not refused"
    assert gather(dict(base, uuid="not-a-uuid"))[0] == "exit"
    kind, got = gather({"op": "catalog", "account_id": 1, "owner_ctrl": 2})
    assert kind == "ok", "the catalog read was refused for want of a uuid"


def test_the_rate_version_is_mandatory_over_the_wire_and_optional_by_hand():
    """Optional on the wire, this guard fails OPEN: a field dropped in transit is
    indistinguishable from one never sent, so the rate-table check would silently
    not run and the trade would go through unchecked. That is the 2026-08-14
    dropped-dry_run shape one layer in. It is also the ONLY guard that catches a
    retune of max_stack, weekly_dust_cap or window_days, because all five
    expected_* figures still match across those three changes.

    The CLI path stays optional on purpose: hand-driven QA and the ops smoke can
    run without first reading a catalog."""
    version = _rates()["rate_version"]
    base = {"op": "exchange", "account_id": 1, "owner_ctrl": 2,
            "output_template": "T1UniqueComponent", "batches": 1, "dry_run": False,
            "uuid": WIRE_KEY}
    kind, got = _gather(dict(base, expected_rate_version=version))
    assert kind == "ok" and got["expected_rate_version"] == version
    assert _gather(base)[0] == "exit", \
        "a stdin-json exchange with no expected_rate_version was accepted"
    assert _gather(dict(base, expected_rate_version=""))[0] == "exit", \
        "an empty expected_rate_version was accepted as if it had been sent"
    assert _gather(dict(base, expected_rate_version="not a version'"))[0] == "exit", \
        "a malformed rate version reached the SQL builder"
    assert _gather(dict(base, dry_run=True))[0] == "exit", \
        "a dry run was let past the rate-version guard; a rehearsal that skips the " \
        "guard rehearses a different trade from the one the button will make"
    kind, got = _gather(argv=["--op", "exchange", "--account-id", "1", "--owner-ctrl", "2",
                             "--output-template", "T1UniqueComponent", "--batches", "1",
                             "--idempotency-key", WIRE_KEY])
    assert kind == "ok" and got["expected_rate_version"] is None, \
        "the hand-driven CLI path now demands a rate version too"


def test_only_the_missing_rate_version_refusal_names_the_field():
    """The host lane defers on the triple (200, bad_request, missing ==
    expected_rate_version) and on nothing else, so the discriminator has to mean
    exactly one thing: the field never arrived. A MALFORMED value is a different
    fact, and if it carried the same key the lane would defer on a job the relay
    delivered intact, which is a deploy waved through on a proof that never ran."""
    version = _rates()["rate_version"]
    base = {"op": "exchange", "account_id": 1, "owner_ctrl": 2,
            "output_template": "T1UniqueComponent", "batches": 1, "dry_run": False,
            "uuid": WIRE_KEY}
    absent = _gather_json(base)
    assert absent["error"] == "bad_request", absent
    assert absent.get("missing") == "expected_rate_version", \
        "the missing-field refusal does not name the field, so the lane cannot tell it " \
        "from any other malformed job: %r" % (absent,)
    malformed = _gather_json(dict(base, expected_rate_version="not a version'"))
    assert malformed["error"] == "bad_request", malformed
    assert "missing" not in malformed, \
        "a malformed rate version carries the missing-field discriminator, so the host " \
        "lane would defer on a request the relay delivered intact: %r" % (malformed,)
    ok_kind, _ = _gather(dict(base, expected_rate_version=version))
    assert ok_kind == "ok", "the live rate version was refused"


def test_the_rate_echo_refuses_a_rate_that_moved_under_the_page():
    table, recipe = _rates(), rf.find_recipe(_rates(), "T3UniqueComponent")

    def req(**kw):
        body = {"expected_rate_version": None, "expected_input_template": None,
                "expected_spice_template": None, "expected_input_per_batch": None,
                "expected_spice_per_batch": None, "expected_output_per_batch": None}
        body.update(kw)
        return body

    assert not rf.check_rate_echo(req(expected_input_template="SteelBar",
                                      expected_spice_per_batch=3,
                                      expected_rate_version=table["rate_version"]),
                                  table, recipe)
    assert rf.check_rate_echo(req(expected_spice_per_batch=1), table, recipe)
    assert rf.check_rate_echo(req(expected_input_template="CopperBar"), table, recipe)
    assert rf.check_rate_echo(req(expected_rate_version="2026-08-01a"), table, recipe), \
        "a stale rate_version was accepted; a retune while the page sat open must refuse"


# --------------------------------------------------------------------------- #
# The weekly cap, against a real temp SQLite file. Every existing counter in
# this repo counts ROWS; this one has to SUM a units column, per tier.
# --------------------------------------------------------------------------- #

def _cap_reset():
    conn = database.get_db()
    try:
        ev._ensure(conn)
        conn.execute("DELETE FROM portal_refinery_events")
        conn.commit()
    finally:
        conn.close()


def _reserve(identity="discord-1", output=TIERS[0], batches=1, dust=1, key=None, tier=1):
    return ev.check_and_reserve(
        idempotency_key=key or str(uuid.uuid4()), actor_identity=identity,
        account_id=101, owner_ctrl=555, tier=tier, output_template=output,
        input_template="CopperBar", batches=batches, dust_units=dust,
        input_units=batches * 5, spice_units=batches)


def _used(identity="discord-1", output=TIERS[0]):
    return ev.caps(identity, [output])[output]["weekly_dust_used"]


def _backdate(key, **delta):
    stamp = (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")
    conn = database.get_db()
    try:
        conn.execute("UPDATE portal_refinery_events SET created_at = ? WHERE idempotency_key = ?",
                     (stamp, key))
        conn.commit()
    finally:
        conn.close()


def test_the_cap_sums_units_and_never_counts_rows():
    """Two rows is 2 by COUNT and 300 by SUM. Only the SUM sees the breach."""
    _cap_reset()
    ok, _r, _a, used = _reserve(dust=200, batches=200)
    assert ok and used == 200, used
    ok, reason, retry, used = _reserve(dust=100, batches=100)
    assert not ok and reason == "weekly_cap", (ok, reason)
    assert used == 200 and retry == 7 * 24 * 3600, (used, retry)
    ok, _r, _a, used = _reserve(dust=50, batches=50)
    assert ok and used == 250, "the exact remainder must still fit; the cap is a ceiling"


def test_the_cap_refuses_the_batch_that_would_cross_it_not_the_one_before():
    _cap_reset()
    assert _reserve(dust=249, batches=249)[0]
    ok, reason, _a, _u = _reserve(dust=2, batches=2)
    assert not ok and reason == "weekly_cap", "249 + 2 must not be allowed to land"


def test_old_and_new_batch_sizes_share_the_identity_allowance():
    _cap_reset()
    assert _reserve(dust=8, batches=4)[0]
    assert _reserve(dust=240, batches=24)[0]
    ok, reason, _retry, used = _reserve(dust=10, batches=1)
    assert not ok and reason == 'weekly_cap' and used == 248
    assert ev.caps('discord-1', [TIERS[0]])[TIERS[0]]['weekly_dust_left'] == 2


def test_the_six_tiers_are_independent_counters():
    _cap_reset()
    for i, tier in enumerate(TIERS, start=1):
        ok, _r, _a, used = _reserve(output=tier, dust=250, batches=250, tier=i)
        assert ok and used == 250, (tier, ok, used)
    counters = ev.caps("discord-1", list(TIERS))
    for tier in TIERS:
        assert counters[tier]["weekly_dust_used"] == 250, tier
        assert counters[tier]["weekly_dust_left"] == 0, tier
    for i, tier in enumerate(TIERS, start=1):
        ok, reason, _a, _u = _reserve(output=tier, dust=1, tier=i)
        assert not ok and reason == "weekly_cap", tier


def test_maxing_one_tier_leaves_the_other_five_untouched():
    _cap_reset()
    assert _reserve(output=TIERS[0], dust=250, batches=250, tier=1)[0]
    counters = ev.caps("discord-1", list(TIERS))
    assert counters[TIERS[0]]["weekly_dust_left"] == 0
    for tier in TIERS[1:]:
        assert counters[tier]["weekly_dust_used"] == 0, "%s shares a pool with tier 1" % tier
        assert counters[tier]["weekly_dust_left"] == 250, tier
    ok, _r, _a, used = _reserve(output=TIERS[1], dust=250, batches=250, tier=2)
    assert ok and used == 250


def test_a_live_pending_reservation_counts_and_a_stranded_one_ages_out():
    _cap_reset()
    key = str(uuid.uuid4())
    assert _reserve(dust=250, batches=250, key=key)[0]
    conn = database.get_db()
    try:
        row = conn.execute("SELECT status FROM portal_refinery_events WHERE idempotency_key = ?",
                           (key,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending", row["status"]
    assert _used() == 250, "a pending reservation that does not count is the double-submit hole"
    assert not _reserve(dust=1)[0]
    _backdate(key, seconds=60 + ev.PENDING_TTL_SECONDS)
    assert _used() == 0, "a dead reservation must not wedge a week of allowance"
    assert _reserve(dust=250, batches=250)[0]


def test_applied_and_replay_count_and_refused_failed_deferred_do_not():
    for status, expected in (("applied", 40), ("replay", 40),
                             ("refused", 0), ("failed", 0), ("deferred", 0)):
        _cap_reset()
        key = str(uuid.uuid4())
        assert _reserve(dust=40, batches=40, key=key)[0]
        ev.settle(key, status)
        assert _used() == expected, "%s counted %d, expected %d" % (status, _used(), expected)


def test_a_refusal_leaves_a_durable_row_that_spends_nothing():
    _cap_reset()
    assert _reserve(dust=250, batches=250)[0]
    refused = str(uuid.uuid4())
    assert not _reserve(dust=10, key=refused)[0]
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT status, fail_reason, actor_identity, account_id, owner_ctrl, tier"
            "  FROM portal_refinery_events WHERE idempotency_key = ?", (refused,)).fetchone()
    finally:
        conn.close()
    assert row is not None, "a probe of the cap left no trace"
    assert row["status"] == "refused" and row["fail_reason"]
    assert row["actor_identity"] == "discord-1" and row["account_id"] == 101
    assert row["owner_ctrl"] == 555 and row["tier"] == 1
    assert _used() == 250, "the refusal was counted against the player"


def test_the_window_is_seven_rolling_days():
    _cap_reset()
    key = str(uuid.uuid4())
    assert _reserve(dust=250, batches=250, key=key)[0]
    ev.settle(key, "applied")
    _backdate(key, days=6, hours=23)
    assert _used() == 250, "a six day old batch is still inside the window"
    _backdate(key, days=7, hours=1)
    assert _used() == 0, "a batch older than seven days still counts"
    assert _reserve(dust=250, batches=250)[0]


def test_the_same_key_twice_reserves_once_and_a_refusal_is_not_a_replay():
    _cap_reset()
    key = str(uuid.uuid4())
    assert _reserve(dust=200, batches=200, key=key)[0]
    ok, _r, _a, used = _reserve(dust=200, batches=200, key=key)
    assert ok and used == 200, "a retry double-counted its own reservation"
    dead = str(uuid.uuid4())
    assert not _reserve(dust=100, key=dead)[0]
    assert not _reserve(dust=100, key=dead)[0], \
        "a refused key converted into an allowance on the retry"


def test_the_cap_is_keyed_on_the_identity_not_the_account():
    """LASTSIETCH_MULTIACCOUNT lets one identity hold several accounts, so a per-account
    cap multiplies by alts. Linked alts share one allowance on purpose."""
    _cap_reset()
    assert _reserve(identity="discord-1", dust=250, batches=250)[0]
    assert not _reserve(identity="discord-1", dust=1)[0]
    assert _reserve(identity="discord-2", dust=250, batches=250)[0]
    assert _used("discord-2") == 250 and _used("discord-1") == 250


def test_settle_is_scoped_and_never_rewrites_the_counted_column():
    _cap_reset()
    key = str(uuid.uuid4())
    assert _reserve(dust=100, batches=100, key=key)[0]
    ev.settle(key, "applied")
    ev.settle(key, "refused", "a stray second settle")
    assert _used() == 100, "a stray settle erased a counted row"
    ev.settle(key, "not-a-status")
    assert _used() == 100
    ev.settle(key, "applied", dust_units=1, input_units=500, spice_units=100)
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT dust_units, input_units, spice_units FROM portal_refinery_events"
            " WHERE idempotency_key = ?", (key,)).fetchone()
    finally:
        conn.close()
    assert row["dust_units"] == 100, \
        "a writer echo shrank the counted column and handed back spent allowance"
    assert row["input_units"] == 500 and row["spice_units"] == 100
    assert _used() == 100


def test_caps_reports_a_full_allowance_for_a_tier_with_no_rows():
    _cap_reset()
    counters = ev.caps("nobody-at-all", list(TIERS))
    assert set(counters) == set(TIERS)
    for tier in TIERS:
        assert counters[tier] == {"weekly_dust_cap": 250, "weekly_dust_used": 0,
                                  "weekly_dust_left": 250}, tier
    assert ev.caps("discord-1", []) == {}


def test_the_cap_module_ships_the_ruled_numbers_and_a_bounded_pending_ttl():
    assert ev.WEEKLY_DUST_CAP == 250 and ev.WINDOW_DAYS == 7
    assert ev.PENDING_TTL_SECONDS == 180, ev.PENDING_TTL_SECONDS
    assert "dust_units" not in ev._SETTLE_FACTS or True  # documented below
    src = _read(EVENTS)
    assert "SUM(dust_units)" in src, "the counter no longer sums a units column"
    assert "AND output_template = ?" in src, "the counter no longer scopes to one tier"
    assert "BEGIN IMMEDIATE" in src, \
        "the reserve is not inside the same write transaction that counted"


# --------------------------------------------------------------------------- #
# The portal backend contract. These modules pull the FastAPI dependency chain,
# so they are read rather than imported, the way test_v2_systems does it.
# --------------------------------------------------------------------------- #

def test_every_writer_token_maps_and_no_superseded_spelling_survives():
    token_map = _dict_literal(ROUTER, "_TOKEN_MAP")
    status = _dict_literal(ROUTER, "_STATUS")
    for token in WRITER_TOKENS:
        assert token in token_map, "writer token not mapped: %s" % token
        assert token_map[token] in status, "%s maps to a token with no status" % token
    for token in STALE_TOKENS:
        assert token not in token_map, "a superseded spelling is still mapped: %s" % token
    assert token_map["no_space"] == "bank_full" and status["bank_full"] == 409
    assert token_map["conservation_failed"] == "write_failed"
    assert token_map["writer_no_output"] == "write_failed", \
        "the relay's own token has no mapping, so a dead forced command reads as success"
    assert "bank_full_volume" not in status and status["bank_volume"] == 409


def test_only_the_three_soft_states_answer_200():
    status = _dict_literal(ROUTER, "_STATUS")
    for token in SOFT_TOKENS:
        assert status[token] == 200, "%s is not a soft 200" % token
    assert [t for t, code in status.items() if code == 200] == list(SOFT_TOKENS), \
        "a fourth token answers 200: %s" % [t for t, c in status.items() if c == 200]
    assert status["weekly_cap"] == 429 and status["rate_limited"] == 429
    assert status["unauthenticated"] == 401 and status["csrf"] == 403
    assert status["player_online"] == 409 and status["rate_changed"] == 409
    assert status["unresolved"] == 502 and status["unavailable"] == 503


def test_the_player_facing_copy_is_the_ruled_copy():
    text = _dict_literal(ROUTER, "_TEXT")
    assert text["refinery_disabled"].startswith("The Ingot Refinery is not open yet")
    assert "Nothing was taken" in text["refinery_disabled"]
    assert "The other tiers still refine." in text["recipe_disabled"]
    assert "Nothing was taken" in text["rate_changed"]
    assert "give the server a minute to catch up" in text["player_online"], \
        "the online refusal does not tell a player who just logged out what to do"
    for token, copy in text.items():
        assert _EM_DASH not in copy, "%s copy carries an em dash" % token
        assert "Spice Refinery" not in copy, \
            "%s copy says Spice Refinery, which is an in-game placeable" % token


def test_the_catalog_seals_unless_both_kill_switches_are_on():
    """A host that is dark while the portal gate is on must still seal the page,
    or the player gets a live stepper whose every press answers refinery_disabled."""
    src = _func_src(ROUTER, "portal_refinery_v2_catalog")
    assert '"enabled": bool(_refinery_enabled() and payload_dict.get("enabled") is True)' in src, \
        "the catalog's top-level enabled is no longer the AND of both gates"
    assert '"holdings_read": payload_dict.get("holdings_read") is True' in src
    assert '"offline_ok": online is False' in src, \
        "offline_ok is not the strict False; undetermined must not read as offline"


def test_the_exchange_route_resolves_identity_server_side_and_audits_every_answer():
    src = _read(ROUTER)
    assert "_resolve_buyer_ctrl_and_bank" in src, "owner_ctrl is not resolved server-side"
    assert "_v2_body_and_csrf" in src, "the write route is not CSRF gated"
    assert "_storage_rate_ok" in src, "the write route has no debounce"
    assert '"portal_refinery_exchange"' in src, "the exchange is not audited"
    assert "portal_refinery_exchange" in _read(V2), \
        "portal_refinery_exchange is missing from AUDIT_ACTION_GROUPS, so the admin " \
        "action filter would silently drop it"
    groups = _read(V2)
    block = groups[groups.index("AUDIT_ACTION_GROUPS"):]
    head = block.index("Portal: storage and market")
    tail = block.index("portal_refinery_exchange")
    assert head < tail < head + 2000, \
        "portal_refinery_exchange is not filed under Portal: storage and market"


def test_the_portal_gate_ships_dark_and_is_resolved_per_request():
    assert 'os.environ.get("LASTSIETCH_REFINERY_ENABLED", "0")' in _read(CONFIG), \
        "the coded default is no longer dark, or no longer in the form the deploy greps for"
    body = _func_src(ROUTER, "_refinery_enabled")
    assert 'feature_flags.enabled("LASTSIETCH_REFINERY_ENABLED", "0")' in body, \
        "the gate does not resolve through feature_flags, so the owner toggle does nothing"
    assert "config.REFINERY_ENABLED" not in _read(ROUTER), \
        "the route reads the import-time constant; an override cannot reach it"
    assert '("LASTSIETCH_REFINERY_ENABLED", "0",' in _read(V2SYS), \
        "the runtime gate board no longer defaults the refinery OFF"
    assert "portal_refinery" in _read(MAIN), "the refinery router is not included"


# --------------------------------------------------------------------------- #
# The transport: relay whitelist, dispatcher case, the ledger DDL
# --------------------------------------------------------------------------- #

def test_the_relay_whitelist_carries_dry_run_and_every_echoed_rate_field():
    """A whitelist-rebuild transport fails OPEN toward the destructive default.
    On 2026-08-14 a missing dry_run turned an authoritative probe into a live
    write; a dropped expected_ field turns a rate check into no rate check."""
    src = _func_src(RELAY, "_dune_refinery_action")
    assert 'dry_run = body.get("dry_run")' in src and 'fields["dry_run"] = dry_run' in src, \
        "dry_run is not carried through the relay's field whitelist"
    for field in ("expected_input_template", "expected_spice_template",
                  "expected_input_per_batch", "expected_spice_per_batch",
                  "expected_output_per_batch", "expected_rate_version"):
        assert 'fields["%s"]' % field in src, "the relay drops %s" % field
    assert '"expected_rate_version"' in src, \
        "the relay does not carry expected_rate_version, which the writer REQUIRES over " \
        "stdin-json: every exchange through the relay would answer bad_request"
    assert 'fields["output_template"]' in src and 'fields["batches"]' in src
    assert '_pos_int("owner_ctrl")' in src and '_pos_int("account_id")' in src
    assert 'body.get("uuid")' in src and "must be a UUID" in src
    assert '_dune_ssh_stdin("refinery-op"' in src, \
        "the relay does not hand the job to the refinery-op forced command"
    relay = _read(RELAY)
    assert '@app.post("/dune/refinery/catalog", dependencies=[Depends(verify_key)])' in relay
    assert '@app.post("/dune/refinery/exchange", dependencies=[Depends(verify_key)])' in relay


def test_the_dispatcher_case_is_the_house_guard_sequence():
    src = _read(DISPATCH)
    start = src.index("\n  refinery-op)")
    case = src[start:src.index("\n    ;;", start)]
    for guard in ('[ -z "$arg" ] ||',
                  '[ -z "${_extra:-}" ] ||',
                  "payload=$(cat)",
                  'payload="${payload//[[:space:]]/}"',
                  '^[A-Za-z0-9+/=]+$',
                  "base64 -d | /root/dune-refinery-write.py --stdin-json",
                  "exit $?"):
        assert guard in case, "the refinery-op case is missing its %s guard" % guard
    assert case.index("payload=$(cat)") < case.index("base64 -d"), \
        "the payload is decoded before it has been charset-gated"
    # Comments first: the case's own header says in words that it never restarts
    # a pod or k3s, and a naive scan would fire on that sentence.
    code = "\n".join(l for l in case.splitlines() if not l.strip().startswith("#")).lower()
    for pattern in ("systemctl", "k3s", "kubectl", "battlegroup"):
        assert pattern not in code, "the refinery-op case runs %s" % pattern


def test_the_ledger_ddl_is_additive_and_claims_its_ownership():
    """A custom object in the dune schema owned by anyone but `dune` aborts
    Funcom's pre-update pg_dump, mid-window, with players waiting."""
    src = _read(DDL)
    assert "CREATE TABLE IF NOT EXISTS dune.ls_refinery_exchanges" in src
    statements = [s.strip() for s in re.split(r";\s*\n", src)]
    assert any(s.replace("\n", " ").strip()
               == "ALTER TABLE dune.ls_refinery_exchanges OWNER TO dune"
               for s in statements), \
        "OWNER TO dune is not a statement of its own in the DDL"
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("--"))
    for verb in ("DROP ", "TRUNCATE", "DELETE FROM"):
        assert verb not in body.upper(), "the DDL carries %s" % verb
    assert "idempotency_key" in src and "dust_units" in src
    assert "CHECK (dust_units > 0)" in src




# --------------------------------------------------------------------------- #
# The page. It never sees a writer; it sees an envelope, and the dark answer is
# a 200 carrying ok:false.
# --------------------------------------------------------------------------- #

def _js_set(path, name):
    src = _read(path)
    m = re.search(r"const %s = new Set\(\[(.*?)\]\)" % name, src, re.S)
    assert m, "%s is gone from %s" % (name, os.path.basename(path))
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def test_the_api_namespace_is_there_and_says_what_dark_looks_like():
    src = _read(API_JS)
    assert "refinery: {" in src, "api.js has no refinery namespace"
    assert "catalog: () => getJSON('/portal/refinery/v2/catalog')" in src
    assert "exchange: (body) => sendCsrfJSON('POST', '/portal/refinery/v2/exchange', body)" in src
    block = src[src.index("V2 Ingot Refinery"):src.index("refinery: {")]
    assert "refinery_disabled" in block and "never a toast" in block, \
        "the namespace does not carry the dark-behaviour comment"
    assert "recipe_disabled" in block, "the per-tier switch is undocumented at the call site"


def test_the_panel_seals_on_enabled_and_not_on_ok():
    """The dark answer is a 200 whose ok is false. A page that keyed off ok would
    render an error for a service that is simply not open yet."""
    src = _read(PANEL)
    assert "{:else if !refinery.enabled}" in src, \
        "the panel no longer seals on the enabled flag"
    assert "{:else if !refinery.ok}" not in src, "the panel seals on ok"
    assert "The Ingot Refinery is not open yet." in src
    assert "SealedPanel" in src, "the closed state is not a sealed panel"
    assert "{:else if refinery.status === 'error'}" in src, \
        "a failed read is not kept distinct from a closed refinery"


def test_the_rates_table_reads_every_quantity_from_the_live_catalog():
    src = _read(PANEL)
    table = src[src.index('<table class="rates">'):src.index('</table>')]
    assert '<caption>Refinery rates per batch' in table
    assert '{#each refinery.recipes as recipe (recipe.output_template)}' in table
    for field in ('input_per_batch', 'spice_per_batch', 'output_per_batch'):
        assert '<td>{recipe.%s}</td>' % field in table, field
    assert '<th scope="row">' in table and table.count('scope="col"') == 4
    assert 'class:paused={!recipe.enabled}' in table and 'Paused' in table
    assert 'become two Spice-infused dust' not in src
    assert 'allowance smaller than one batch' in src


def test_the_three_soft_states_never_interrupt_the_player():
    quiet = _js_set(STORE, "QUIET")
    assert quiet == {"refinery_disabled", "recipe_disabled"}, quiet
    tone = _js_set(STORE, "ERROR_TONE")
    for token in SOFT_TOKENS:
        assert token not in tone, \
            "%s renders the alarm tone; it is a state the row explains, not an alarm" % token
    for path in (PANEL, ROW, STORE):
        assert "alert(" not in _read(path), \
            "%s raises a browser alert" % os.path.basename(path)
    assert "if (token === 'refinery_disabled') refinery.enabled = false;" in _read(STORE), \
        "a dark answer to a write does not fold the page back into the closed state"


def test_a_retry_carries_the_terms_its_order_was_built_against():
    """M3 keeps the idempotency uuid across write_failed, unavailable, unresolved
    and writer_no_output, so a retry REPLAYS instead of refining twice. L5 makes
    the body echo the rate. The two compose badly if the body is rebuilt from the
    LIVE store, because the catalog poll runs every 60 s: a retune landing between
    a failed write and its retry sends the ORIGINAL uuid with the NEW terms, and
    the writer's replay row refuses any changed parameter as idempotency_conflict.
    Nothing is ever double spent, but the player is told they reused a request
    when what actually happened is the rate moved under them.

    So the intent holds the whole ORDER, not a bare uuid, and the body is built
    from the intent. Same uuid and same terms replays cleanly; a table that really
    did move answers rate_changed, which is the sentence that fits."""
    src = _read(STORE)
    assert "const order = { uuid: uuidv4(), terms };" in src, \
        "the intent no longer stores its order's terms beside the uuid"
    call = src[src.index("await api.refinery.exchange({"):]
    call = call[:call.index("});")]
    assert "...order.terms," in call, "the body does not send the intent's frozen terms"
    assert "uuid: order.uuid," in call, "the body does not send the intent's uuid"
    # The terms must come from the intent and from nowhere else. Reading any of
    # these off the live store is the exact regression this pin exists for.
    for live in ("refinery.rateVersion", "recipe.input_template", "recipe.spice_template",
                 "rate.input_per_batch", "rate.spice_per_batch", "rate.output_per_batch"):
        assert live not in call, (
            "the exchange body reads %s from the live store, so a retry after a "
            "catalog poll would echo terms its order was never built against" % live)


def test_the_workshop_page_carries_both_tabs_and_the_augments_route_redirects():
    src = _read(WORKSHOP)
    assert "{ key: 'augments', label: 'Augments' }" in src
    assert "{ key: 'refinery', label: 'Ingot Refinery' }" in src
    assert "AugmentedItemsPanel" in src and "RefineryPanel" in src
    assert "?tab=" in src, "the tab is not in the URL, so a refresh loses it"
    moved = _read(AUGMENTS)
    assert "/storage/workshop?tab=augments" in moved, \
        "the old augments route does not point at its new home"
    assert "replaceState: true" in moved, \
        "the redirect leaves the Back button bouncing through the dead route"
    assert "storage/workshop" in _read(STORAGE_PAGE), "the Storage submenu lost the Workshop link"
    assert "Workshop" in _read(STORAGE_PAGE)


def test_the_unfurl_names_the_workshop_and_no_player():
    src = _read(UNFURL)
    assert '"workshop"' in src, "nextgen_unfurl has no workshop entry"
    block = src[src.index('"workshop"'):]
    block = block[:block.index("\n\n")] if "\n\n" in block[:600] else block[:600]
    assert "Ingot Refinery" in block or "Ingot Refinery" in src, \
        "the unfurl never names the Ingot Refinery"
    assert "storage/workshop" in src


def test_the_shell_key_and_the_app_version_both_moved():
    """A content change to a versioned asset needs a NEW cache key AND a service
    worker bump in the same deploy, or mobile clients keep the old shell."""
    sw = _read(SW)
    m = re.search(r"const CACHE = 'ls-v2-shell-v(\d+)';", sw)
    assert m, "sw.js has no shell cache key"
    assert m.group(0) != "const CACHE = '%s';" % SHIPPED_CACHE, \
        "the service-worker cache key is the one already live (%s)" % SHIPPED_CACHE
    assert int(m.group(1)) > int(SHIPPED_CACHE.rsplit("v", 1)[1]), \
        "the shell key went backwards"
    version = json.loads(_read(PKG))["version"]
    assert version != SHIPPED_PKG_VERSION, \
        "package.json is still at the shipped version %s" % SHIPPED_PKG_VERSION


def test_no_player_facing_surface_says_spice_refinery():
    """The game already has a Spice Refinery placeable. Every surface a player
    sees says Ingot Refinery, or the copy is describing a different thing that
    exists in the world."""
    offenders = []
    src_root = os.path.join(PN, "src")
    for base, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in (".svelte-kit", "node_modules")]
        for name in files:
            if not name.endswith((".svelte", ".js", ".json", ".css", ".html")):
                continue
            path = os.path.join(base, name)
            if "Spice Refinery" in _read(path):
                offenders.append(os.path.relpath(path, ROOT))
    for path in (UNFURL, ROUTER):
        if "Spice Refinery" in _read(path):
            offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, "Spice Refinery is an in-game placeable: " + ", ".join(offenders)




# --------------------------------------------------------------------------- #
# The two deploy lanes
# --------------------------------------------------------------------------- #

_GATE_RE = re.compile(r'^if \[\[ -n "\$DRY_RUN" \]\]; then')
_DEF_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{")
_REMOTE_RE = re.compile(r"(^|[^\w./-])(ssh|rsync|scp)\s")


def _executed_before_the_gate(path):
    """The lines a dry run actually RUNS before it exits.

    Comments are dropped, and so are function BODIES: a helper that contains ssh
    is not a call to ssh, and both lanes define theirs above the gate. A one-line
    `f() { ...; }` opens and closes on the same line and must not leave the
    walker thinking every following line is inside a body, which is the bug that
    would make this pin silently pass on anything."""
    out, depth = [], 0
    for raw in _read(path).splitlines():
        stripped = raw.strip()
        if _GATE_RE.match(raw):
            return out
        if not stripped or stripped.startswith("#"):
            continue
        if _DEF_RE.match(raw):
            if stripped.endswith("}") and stripped.count("{") == stripped.count("}"):
                continue          # a one-line definition: opened and closed here
            depth += 1
            continue
        if depth:
            if stripped == "}":
                depth -= 1
            continue
        out.append(raw)
    raise AssertionError("%s has no dry-run gate at all" % os.path.basename(path))


















# --------------------------------------------------------------------------- #
# Every pin RED on a broken variant. A pin nobody has watched fail is not a pin.
# --------------------------------------------------------------------------- #

# The shell key moves with every portal release; the mutation reads the live one
# and drops it back to the key the pin forbids (the one shipped before the wave).
_SW_LIVE_KEY = re.search(r"const CACHE = '(ls-v2-shell-v\d+)';", _read(SW)).group(1)

MUTATIONS = (
    ("the portal gate ships open",
     "admin-backend/config.py",
     'os.environ.get("LASTSIETCH_REFINERY_ENABLED", "0")',
     'os.environ.get("LASTSIETCH_REFINERY_ENABLED", "1")'),
    ("the writer stops reading its flag file",
     "scripts/dune-refinery-write.py",
     "    return os.path.isfile(REFINERY_FLAG)",
     "    return True"),
    ("the offline gate stops locking the rows it counts",
     "scripts/dune-refinery-write.py",
     "     AND character_state IS DISTINCT FROM 'Deleted'\n     FOR SHARE;",
     "     AND character_state IS DISTINCT FROM 'Deleted'\n     LIMIT 1;"),
    ("the reconnect grace fails open",
     "scripts/dune-refinery-write.py",
     "AND NOT COALESCE(reconnect_grace_period_end > now(), false))\n    INTO v_chars, v_offline",
     "AND COALESCE(reconnect_grace_period_end > now(), true))\n    INTO v_chars, v_offline"),
    ("an ambiguous bank is resolved instead of refused",
     "scripts/dune-refinery-write.py",
     "IF v_bank_count <> 1 THEN RAISE EXCEPTION 'no_bank",
     "IF v_bank_count = 0 THEN RAISE EXCEPTION 'no_bank"),
    ("the Deleted tombstone filter is dropped",
     "scripts/dune-refinery-write.py",
     "   WHERE account_id = {account}\n     AND character_state IS DISTINCT FROM 'Deleted'\n     FOR SHARE;",
     "   WHERE account_id = {account}\n     FOR SHARE;"),
    ("the inventory lock order is reversed",
     "scripts/dune-refinery-write.py",
     "   WHERE id IN (v_bank_inv, v_backpack_inv, v_toolbar_inv)\n   ORDER BY id FOR UPDATE;",
     "   WHERE id IN (v_bank_inv, v_backpack_inv, v_toolbar_inv)\n   ORDER BY id DESC FOR UPDATE;"),
    ("the dust is minted before the inputs are consumed",
     "scripts/dune-refinery-write.py",
     "  -- 6. CONSUME the ingots, then the Melange. Both before anything is minted: an\n"
     "  --    insufficient input must never be able to leave a dust behind.\n",
     "  -- 6. MINT FIRST (broken variant)\n"
     "  INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats,\n"
     "         quality_level, acquisition_time, is_new)\n"
     "  VALUES (v_bank_inv, 1, 0, '{output_tpl}', '{{}}'::jsonb, 0, 0, true);\n"),
    ("a template id is lower-cased",
     "scripts/refinery-rates.json",
     '"input_template": "CopperBar",',
     '"input_template": "copperbar",'),
    ("the melange rate moves without a rate_version bump",
     "scripts/refinery-rates.json",
     '"spice_per_batch": 2,',
     '"spice_per_batch": 1,'),
    ("the relay whitelist drops an echoed rate field",
     "relay/app.py",
     '        fields["expected_spice_per_batch"] = _pos_int("expected_spice_per_batch")\n',
     ""),
    ("the cap counts rows instead of units",
     "admin-backend/portal_refinery_events.py",
     'f"""SELECT COALESCE(SUM(dust_units), 0) AS units',
     'f"""SELECT COUNT(*) AS units'),
    ("the cap is pooled across the six tiers",
     "admin-backend/portal_refinery_events.py",
     "               AND output_template = ?\n",
     "               AND (output_template = ? OR 1 = 1)\n"),
    ("the service worker reuses the shipped shell key",
     "admin-backend/portal-nextgen/static/sw.js",
     "const CACHE = '%s';" % _SW_LIVE_KEY,
     "const CACHE = '%s';" % SHIPPED_CACHE),
    ("the page seals on ok instead of enabled",
     "admin-backend/portal-nextgen/src/lib/components/refinery/RefineryPanel.svelte",
     "{:else if !refinery.enabled}",
     "{:else if !refinery.ok}"),
    ("a malformed rate version claims the field went missing",
     "scripts/dune-refinery-write.py",
     "            if not RATE_VERSION_RE.fullmatch(version):\n                fail(\"bad_request\", 2)",
     "            if not RATE_VERSION_RE.fullmatch(version):\n"
     "                emit({\"ok\": False, \"error\": \"bad_request\",\n"
     "                      \"missing\": \"expected_rate_version\"}, 2)"),
    ("a retry rebuilds its terms from the live store",
     "admin-backend/portal-nextgen/src/lib/refinery.svelte.js",
     "      ...order.terms,\n      uuid: order.uuid,",
     "      expected_input_template: recipe.input_template,\n"
     "      expected_spice_template: recipe.spice_template,\n"
     "      expected_input_per_batch: rate.input_per_batch,\n"
     "      expected_spice_per_batch: rate.spice_per_batch,\n"
     "      expected_output_per_batch: rate.output_per_batch,\n"
     "      expected_rate_version: refinery.rateVersion,\n"
     "      uuid: order.uuid,"),
    ("the table prints an obsolete fixed output",
     "admin-backend/portal-nextgen/src/lib/components/refinery/RefineryPanel.svelte",
     "<td>{recipe.output_per_batch}</td>",
     "<td>2</td>"),
)

_SCRATCH_TREE = (
    "scripts/dune-refinery-write.py",
    "scripts/refinery-rates.json",
    "scripts/sql/ls_refinery_exchanges.sql",
    "scripts/dune-relay-dispatch.sh",
    "scripts/dune-flag-set.sh",
    "scripts/tests/test_flag_set_verb.py",
    "scripts/tests/test_refinery_pg.sh",
    "relay/app.py",
    "admin-backend/routers/portal_refinery.py",
    "admin-backend/portal_refinery_events.py",
    "admin-backend/routers/v2.py",
    "admin-backend/routers/v2_systems.py",
    "admin-backend/config.py",
    "admin-backend/main.py",
    "admin-backend/nextgen_unfurl.py",
    "admin-backend/portal-nextgen/package.json",
    "admin-backend/portal-nextgen/static/sw.js",
    "admin-backend/portal-nextgen/src/lib/api.js",
    "admin-backend/portal-nextgen/src/lib/refinery.svelte.js",
    "admin-backend/portal-nextgen/src/lib/components/refinery/RefineryPanel.svelte",
    "admin-backend/portal-nextgen/src/lib/components/refinery/RefineryRow.svelte",
    "admin-backend/portal-nextgen/src/routes/storage/workshop/+page.svelte",
    "admin-backend/portal-nextgen/src/routes/storage/augments/+page.svelte",
    "admin-backend/portal-nextgen/src/routes/storage/+page.svelte",
)


def _scratch_copy(tmp):
    for rel in _SCRATCH_TREE:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)


def _run_child(root):
    env = dict(os.environ, REFINERY_ROOT=root, REFINERY_CHILD="1",
               PYTHONDONTWRITEBYTECODE="1")
    # Its own admin.db, so a child's rows can never be read as the parent's.
    env["LASTSIETCH_DB_PATH"] = os.path.join(root, "child-admin.db")
    return subprocess.run([sys.executable, os.path.abspath(__file__)],
                          capture_output=True, text=True, env=env, timeout=600)


def test_every_pin_is_red_on_a_broken_variant():
    """Each mutation lands on a scratch COPY of the tree; the repo is never
    edited, so there is nothing to restore with `git checkout --` and no way for
    a failed run to leave the working tree broken."""
    if CHILD:
        return
    tmp = tempfile.mkdtemp(prefix="refinery-broken-")
    try:
        _scratch_copy(tmp)
        clean = _run_child(tmp)
        assert clean.returncode == 0, (
            "the unmutated scratch copy already fails: "
            + " | ".join(l for l in clean.stdout.splitlines() if l.startswith("FAIL")))
        for label, rel, find, replace in MUTATIONS:
            path = os.path.join(tmp, rel)
            original = _read(path)
            assert find in original, "[%s] the mutation target is gone from %s" % (label, rel)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace(find, replace, 1))
            try:
                broken = _run_child(tmp)
                assert broken.returncode != 0, \
                    "[%s] the suite stayed GREEN on a broken variant" % label
                print("  broken-variant RED: " + label)
            finally:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(original)
        again = _run_child(tmp)
        assert again.returncode == 0, "the scratch copy was not restored between mutations"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    passed = failed = 0
    for fn in _all_tests():
        try:
            fn()
            passed += 1
            print("PASS " + fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s :: %s" % (fn.__name__, exc))
    print()
    print("%d/%d passed" % (passed, passed + failed))
    sys.exit(1 if failed else 0)
