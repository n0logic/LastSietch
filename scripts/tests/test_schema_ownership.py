#!/usr/bin/env python3
"""Repo-wide guard: every DDL that creates a `dune.ls_*` table must also claim ownership.

🔴 WHY THIS IS A TEST AND NOT A CONVENTION. Funcom's pre-update `pg_dump` ABORTS if any object
in the `dune` schema is owned by a role other than `dune`. The abort happens mid-update, inside
an announced window, with players waiting, and the cause is a line somebody forgot months
earlier. It is the highest-consequence, lowest-visibility mistake available in this codebase.

It has already happened. `/root/dune-owner-check.sh` on 2026-07-27 reported 5 of 33 ownership
rows wrong: ls_collision_fix_log, ls_exchange_order_watch, ls_inventory_capacity_log,
ls_market_bot_limits, ls_solari_ledger, all owned by `postgres`. They were fixed in place, and
the three DDL files responsible were patched, but nothing stopped the next one from repeating it
until this test existed.

Prod-safe: reads repo files only. It does NOT check the live database -- that is
`ssh <game-host> /root/dune-owner-check.sh`, which is the authority and should be run before any
Funcom update. This checks the SOURCE, so drift cannot be re-introduced by a deploy.

Run:  python3 scripts/tests/test_schema_ownership.py     (also import-safe)
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)

# Files that create a dune.ls_* table but deliberately do not own it. Empty, and it should
# stay that way: if you are adding an entry here, you are almost certainly about to break a
# Funcom update instead.
ALLOWED_WITHOUT_OWNER = set()

SKIP_DIRS = {".git", "node_modules", "__pycache__", "build", ".svelte-kit", "venv"}

# A table we create in the GAME database. The schema qualifier is REQUIRED here and that is
# the whole point of the pattern: admin-backend/database.py creates `ls_account_links` and
# `ls_reward_claims` in admin.db, which is SQLite and has no concept of ownership. Those are
# different tables that merely share a name with Postgres ones, so an unqualified `ls_*`
# CREATE is out of scope rather than an exception to be allow-listed.
#
# Funcom's own tables are never in scope either: we do not create them and must never re-own
# them.
CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:dune\.ls_|lsadmin\.)[A-Za-z0-9_]+)",
    re.I)
# The templated form some python tools use: CREATE TABLE IF NOT EXISTS {LOG_TABLE}, where the
# variable holds a qualified name. Only counted in a file that mentions `dune.` at all, so a
# SQLite tool using the same idiom is not dragged in.
CREATE_TEMPLATED_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\{[A-Za-z0-9_]+\})", re.I)
OWNER_RE = re.compile(r"OWNER\s+TO\s+dune", re.I)


def _walk():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".sql", ".sh", ".py")):
                yield os.path.join(root, f)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def test_every_ls_table_ddl_claims_ownership():
    offenders = []
    for path in _walk():
        rel = os.path.relpath(path, REPO)
        if rel in ALLOWED_WITHOUT_OWNER:
            continue
        # This test file names the tables in prose; do not lint the linter.
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue
        src = _read(path)
        if "CREATE TABLE" not in src.upper():
            continue
        created = set(m.group(1) for m in CREATE_RE.finditer(src))
        if "dune." in src:
            created |= set(m.group(1) for m in CREATE_TEMPLATED_RE.finditer(src))
        if not created:
            continue
        if not OWNER_RE.search(src):
            offenders.append((rel, sorted(created)))

    assert not offenders, (
        "these files create dune.ls_* tables without claiming ownership.\n"
        "Funcom's pre-update pg_dump ABORTS on any dune-schema object not owned by `dune`, "
        "mid-update and inside a window. Add `ALTER TABLE <t> OWNER TO dune;` to each:\n"
        + "\n".join(f"  {rel}: {', '.join(t)}" for rel, t in offenders))


def test_the_five_known_offenders_are_fixed():
    """Pins the specific 2026-07-27 regression so it cannot silently come back."""
    expect = {
        "ops/exchange-ledger/schema.sql": ["ls_exchange_order_watch", "ls_solari_ledger"],
        "dune-market-bot/bot-limits-schema.sql": ["ls_market_bot_limits"],
        "ops/inventory-base-capacity/retrofit-capacity.py": ["LOG_TABLE"],
    }
    for rel, tables in expect.items():
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue          # tool moved or retired; the repo-wide test still covers it
        src = _read(path)
        assert OWNER_RE.search(src), f"{rel} lost its OWNER TO dune ({', '.join(tables)})"


def test_the_live_gate_is_documented_not_replaced():
    """This test checks SOURCE. The live database is checked by the game-host script, and the
    two are not interchangeable: source drift and live drift have different causes."""
    found = False
    for path in _walk():
        if "dune-owner-check" in _read(path):
            found = True
            break
    assert found, ("nothing in the repo references /root/dune-owner-check.sh, which is the "
                   "authority on LIVE ownership and must be run before a Funcom update")


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
