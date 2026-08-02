#!/usr/bin/env python3
"""Integration test for the Karum escrow audit (scripts/dune-karum-audit.py).

EXECUTES the audit's real SQL against a throwaway postgres and feeds the result to the
module's own classify(), so both halves of the audit are under test: the query and the
verdict.

This is the one test in the Karum suite that has to exist. The audit is the canary for an
offline-gate regression, and a canary nobody has verified is worse than no canary: it
produces a clean-looking report while the thing it watches for is happening. So every
paging condition here is provoked deliberately and the audit is required to catch it.

Requires docker and a local postgres image. NOT part of the fast unit suite.

  python3 scripts/tests/test_karum_audit_pg.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
AUDIT = os.path.join(SCRIPTS, "dune-karum-audit.py")
IMAGE = os.environ.get("PG_IMAGE", "postgres:16-alpine")

SELLER, BUYER = 1001, 2002
SELLER_BANK, TRADE_WIN, EXCH = 9001, 9020, 610
ITEM, LISTING = 4242, 4711

PASS = FAIL = 0
CID = None


def ok(msg):
    global PASS
    PASS += 1
    print(f"  \033[32mPASS\033[0m {msg}")


def bad(msg, detail=""):
    global FAIL
    FAIL += 1
    print(f"  \033[31mFAIL\033[0m {msg}")
    if detail:
        print(f"        {detail}")


def load_audit():
    spec = importlib.util.spec_from_file_location("karum_audit", AUDIT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def psql(sql, tuples=True):
    cmd = ["docker", "exec", "-i", CID, "psql", "-U", "postgres", "-d", "dune",
           "-v", "ON_ERROR_STOP=1"]
    if tuples:
        cmd.append("-tA")
    p = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[:600])
    return (p.stdout or "").strip()


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS dune;
CREATE TABLE dune.encrypted_player_state (
  account_id bigint, player_pawn_id bigint, player_controller_id bigint,
  online_status text, reconnect_grace_period_end timestamptz,
  character_state text, last_avatar_activity timestamptz);
CREATE TABLE dune.inventories (
  id bigserial PRIMARY KEY, actor_id bigint, inventory_type int,
  exchange_id bigint, max_item_count bigint, max_item_volume double precision);
CREATE TABLE dune.items (
  id bigint PRIMARY KEY, inventory_id bigint, stack_size bigint, position_index bigint,
  template_id text, stats jsonb, quality_level bigint, acquisition_time bigint,
  is_new boolean, volume_override double precision);
CREATE TABLE dune.dune_exchange_orders (
  id bigserial PRIMARY KEY, item_id bigint, template_id text,
  category_mask bigint, category_depth bigint);

-- 🔴 A FAITHFUL stub of the live function, not a convenient one. The first version of this
-- fixture was `LANGUAGE sql ... SELECT 610::bigint`, which is neither of the two things that
-- matter about the real one and therefore hid a bug the deploy caught on the box:
--   * the real body references `inventories` UNQUALIFIED, so it only resolves with `dune` on
--     the search_path;
--   * and it is NOT read-only: on a miss it INSERTs a row and returns it.
-- The audit no longer calls it (see the comment in AUDIT_SQL), and the assertion below keeps
-- it that way. This stub exists so that if anyone puts the call back, the test reproduces the
-- real failure instead of passing.
CREATE FUNCTION dune.get_exchange_inventory_id(in_exchange_id bigint) RETURNS bigint
  LANGUAGE plpgsql AS $fn$
DECLARE inv_id BIGINT;
BEGIN
  SELECT INTO inv_id id FROM inventories WHERE "exchange_id" = in_exchange_id;
  IF inv_id IS NULL THEN
    INSERT INTO inventories("id", exchange_id) VALUES(DEFAULT, in_exchange_id) RETURNING id INTO inv_id;
  END IF;
  RETURN inv_id;
END $fn$;
"""


def reset():
    psql(f"""
TRUNCATE dune.encrypted_player_state, dune.inventories, dune.items,
         dune.dune_exchange_orders, dune.ls_karum_escrow, dune.ls_karum_payments;

INSERT INTO dune.encrypted_player_state
  (account_id, player_pawn_id, player_controller_id, online_status, character_state,
   last_avatar_activity)
VALUES ({SELLER}, 5001, 7001, 'Offline', 'Active', now()),
       ({BUYER},  5002, 7002, 'Offline', 'Active', now());

INSERT INTO dune.inventories (id, actor_id, inventory_type, exchange_id, max_item_count, max_item_volume)
VALUES ({SELLER_BANK}, 5001, 30, NULL, 50, 500),
       ({TRADE_WIN},   5001, 20, NULL, 10, 0),
       ({EXCH},        NULL, NULL, 2,    -1, -1);

-- the escrowed stack, where the ledger says it should be
INSERT INTO dune.items
  (id, inventory_id, stack_size, position_index, template_id, stats, quality_level,
   acquisition_time, is_new, volume_override)
VALUES ({ITEM}, {EXCH}, 500, 1000000000, 'IronBar', '{{}}'::jsonb, 2, 0, false, 1.0);

INSERT INTO dune.ls_karum_escrow
  (correlation_id, listing_id, item_id, inventory_id, seller_account_id, seller_ctrl,
   template_id, stack_size, quality_level, state)
VALUES ('corr-held-1', {LISTING}, {ITEM}, {EXCH}, {SELLER}, 7001, 'IronBar', 500, 2, 'held');

-- a slice of the market bot's litter: rows in 610 with no order row and no marker
INSERT INTO dune.items
  (id, inventory_id, stack_size, position_index, template_id, stats, quality_level,
   acquisition_time, is_new, volume_override)
SELECT 900000 + g, {EXCH}, 1, g, 'BotJunk', '{{}}'::jsonb, 0, 0, false, 1.0
  FROM generate_series(1, 25) g;
""", tuples=False)


def run_audit(mod):
    sql = mod.AUDIT_SQL.format(exchange_id=mod.EXCHANGE_ID,
                               position_base=mod.KARUM_POSITION_BASE)
    raw = psql(sql)
    a = json.loads(raw)
    findings, warnings = mod.classify(a)
    a["findings"], a["warnings"], a["page"] = findings, warnings, bool(findings)
    return a


def main():
    global CID
    mod = load_audit()

    if subprocess.run(["docker", "image", "inspect", IMAGE],
                      capture_output=True).returncode != 0:
        print(f"FATAL: image {IMAGE} not present locally", file=sys.stderr)
        return 2

    print(f"\n== boot {IMAGE} ==")
    CID = subprocess.run(
        ["docker", "run", "-d", "--rm", "-e", "POSTGRES_PASSWORD=t", "-e", "POSTGRES_DB=dune",
         IMAGE, "-c", "fsync=off", "-c", "full_page_writes=off"],
        capture_output=True, text=True, check=True).stdout.strip()
    try:
        for _ in range(60):
            if subprocess.run(["docker", "exec", CID, "pg_isready", "-U", "postgres",
                               "-d", "dune"], capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        psql(SCHEMA, tuples=False)
        for f in (os.path.join(SCRIPTS, "sql", "ls_karum_escrow.sql"),
                  os.path.join(SCRIPTS, "sql", "ls_karum_payments.sql")):
            ddl = "\n".join(l for l in open(f, encoding="utf-8")
                            if "OWNER TO dune" not in l)
            psql(ddl, tuples=False)
        print("  schema + both ledgers loaded")

        # ---- the audit must not call the writing function ---------------------
        print("\n== the audit resolves the exchange inventory by READING ==")
        sql = mod.AUDIT_SQL.format(exchange_id=mod.EXCHANGE_ID,
                                   position_base=mod.KARUM_POSITION_BASE)
        code = "\n".join(l.split("--", 1)[0] for l in sql.splitlines())
        if "get_exchange_inventory_id" not in code:
            ok("does not call dune.get_exchange_inventory_id (it can INSERT, and it needs "
               "dune on the search_path)")
        else:
            bad("the audit calls get_exchange_inventory_id",
                "that function is not read-only and does not resolve without a search_path")
        reset()
        a = run_audit(mod)
        if a.get("exchange_inv") == EXCH:
            ok(f"resolved the exchange inventory to {EXCH} by direct read")
        else:
            bad("exchange inventory not resolved", f"got {a.get('exchange_inv')!r}")

        # ---- healthy baseline ------------------------------------------------
        print("\n== a healthy board must not page ==")
        reset()
        a = run_audit(mod)
        if not a["page"] and a["healthy"] == 1 and a["held_total"] == 1 and not a["findings"]:
            ok("escrow where the ledger says it is -> no findings")
        else:
            bad("healthy baseline paged", json.dumps(a["findings"]))
        if a["unmarked_orphans"] == 25:
            ok("bot litter counted and reported, not acted on (25)")
        else:
            bad("orphan count wrong", f"got {a['unmarked_orphans']}")
        if a["escrow_states"].get("held") == 1:
            ok("ledger census reported")
        else:
            bad("ledger census wrong", json.dumps(a["escrow_states"]))

        # ---- THE canary: the offline-gate regression -------------------------
        print("\n== the duplication canary (this is the whole point) ==")
        reset()
        psql(f"UPDATE dune.items SET inventory_id={SELLER_BANK} WHERE id={ITEM};", tuples=False)
        a = run_audit(mod)
        joined = " ".join(a["findings"])
        if a["page"] and "DUPLICATION SUSPECTED" in joined and str(SELLER) in joined:
            ok("escrowed row back in the seller's BANK -> duplication suspected, pages")
        else:
            bad("the canary missed a resurrection into a player bank", json.dumps(a["findings"]))
        if a["moved"] and a["moved"][0]["in_player_hands"] is True:
            ok("flagged as in_player_hands with the owning account resolved")
        else:
            bad("in_player_hands not set", json.dumps(a["moved"]))

        # ---- the worst case: a live in-person trade window -------------------
        reset()
        psql(f"UPDATE dune.items SET inventory_id={TRADE_WIN} WHERE id={ITEM};", tuples=False)
        a = run_audit(mod)
        joined = " ".join(a["findings"])
        if a["page"] and "IN-PERSON TRADE WINDOW" in joined:
            ok("escrowed row in inventory_type 20 -> named as a live trade window")
        else:
            bad("type-20 case not distinguished", json.dumps(a["findings"]))

        # ---- escrow evaporated ----------------------------------------------
        print("\n== escrow evaporated (a purge, a cull, a manual delete) ==")
        reset()
        psql(f"DELETE FROM dune.items WHERE id={ITEM};", tuples=False)
        a = run_audit(mod)
        joined = " ".join(a["findings"])
        if a["page"] and "ESCROW EVAPORATED" in joined and "refund" in joined:
            ok("missing row -> pages, and the copy points at the admin page not SQL")
        else:
            bad("evaporation not caught", json.dumps(a["findings"]))
        if a["healthy"] == 0 and len(a["evaporated"]) == 1:
            ok("counted as evaporated, not as healthy")
        else:
            bad("evaporated accounting wrong", json.dumps(a["evaporated"]))

        # ---- a marker with no ledger row ------------------------------------
        print("\n== a HolKarum marker with no ledger row ==")
        reset()
        psql(f"""INSERT INTO dune.items
                 (id, inventory_id, stack_size, position_index, template_id, stats,
                  quality_level, acquisition_time, is_new, volume_override)
                 VALUES (777777, {EXCH}, 1, 1000000500, 'MelangeSpice',
                   '{{"HolKarum":{{"listing_id":9999}}}}'::jsonb, 0, 0, false, 1.0);""",
             tuples=False)
        a = run_audit(mod)
        joined = " ".join(a["findings"])
        if a["page"] and "MARKED ROW WITH NO LEDGER" in joined and "Do NOT" in joined:
            ok("marked orphan pages and says explicitly not to clean it up")
        else:
            bad("sentinel orphan not caught", json.dumps(a["findings"]))
        if a["unmarked_orphans"] == 25:
            ok("a marked row is NOT counted as bot litter")
        else:
            bad("marked row leaked into the litter count", f"got {a['unmarked_orphans']}")

        # ---- LT-7 slot collision: a warning, not a page ----------------------
        print("\n== LT-7 slot collision (warn, do not page) ==")
        reset()
        psql(f"""INSERT INTO dune.items
                 (id, inventory_id, stack_size, position_index, template_id, stats,
                  quality_level, acquisition_time, is_new, volume_override)
                 VALUES (888888, {EXCH}, 1, 1000000000, 'BotJunk', '{{}}'::jsonb,
                         0, 0, false, 1.0);""", tuples=False)
        a = run_audit(mod)
        if a["warnings"] and "SLOT COLLISION" in a["warnings"][0]:
            ok("the bot landing on a Karum slot is reported")
        else:
            bad("slot collision missed", json.dumps(a["warnings"]))
        if not a["page"]:
            ok("and it warns rather than pages, because the effect is unproven not known-bad")
        else:
            bad("a slot collision should not page", json.dumps(a["findings"]))

        # ---- a broken audit must never read as clean ------------------------
        print("\n== a broken audit is not a clean audit ==")
        env = dict(os.environ)
        p = subprocess.run([sys.executable, AUDIT], capture_output=True, text=True,
                           env=env, timeout=60)
        # No /root/dq.sh here, so it must fail loudly rather than report success.
        out = json.loads(p.stdout or "{}")
        if p.returncode == mod.RC_BROKEN and out.get("ok") is False and out.get("page") is True:
            ok("no DB access -> ok:false, page:true, exit 3 (never mistaken for clean)")
        else:
            bad("a failed audit did not fail loudly",
                f"rc={p.returncode} out={p.stdout[:200]}")

        print(f"\n== {PASS} passed, {FAIL} failed ==")
        return 1 if FAIL else 0
    finally:
        if CID:
            subprocess.run(["docker", "rm", "-f", CID], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
