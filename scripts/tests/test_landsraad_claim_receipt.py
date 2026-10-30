#!/usr/bin/env python3
"""Tests for the portal side of the Landsraad claim (admin-backend/landsraad_claim.py).

The writer's transaction is covered by test_landsraad_claim_pg.sh. This covers what the
player sees and what Cielago DMs:
  * stacks of one reward are summed (1200 parts arrive as 500/500/200, read as x1200);
  * Solari in the backpack is pulled out as one number;
  * lines left at the rep say why;
  * NOTHING IDENTIFYING LEAVES: no order or item ids reach the receipt;
  * the DM queue hands a receipt out once and an ack is idempotent.

The writer result below has the exact key set dune-landsraad-claim.py emits.
Prod-safe: no host, no network, no game DB; admin.db is an in-memory sqlite built from
the real CREATE TABLE in database.py.

Run:  python3 scripts/tests/test_landsraad_claim_receipt.py
"""
import json
import os
import re
import sqlite3
import sys
import types

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BACKEND = os.path.join(REPO, "admin-backend")
sys.path.insert(0, BACKEND)

# admin.db: the real portal_landsraad_claim DDL, pulled from database.py's SCHEMA text.
_schema = open(os.path.join(BACKEND, "database.py")).read()
_ddl = re.search(r"CREATE TABLE IF NOT EXISTS portal_landsraad_claim \(.*?\);", _schema, re.S).group(0)
_conn = sqlite3.connect(":memory:")
_conn.row_factory = sqlite3.Row
_conn.execute(_ddl)


class _NoClose:
    def __init__(self, c):
        self._c = c

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        pass


sys.modules["database"] = types.SimpleNamespace(get_db=lambda: _NoClose(_conn))
sys.modules["portal_identity"] = types.SimpleNamespace(credentials_installed=lambda conn: False)

import landsraad_claim as lc  # noqa: E402

PASS = FAIL = 0


async def _true():
    return True


async def _is(account_id, want):
    return account_id == want


async def _count(calls, account_id):
    calls.append(account_id)
    return False


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


RESULT = {
    "ok": True, "mode": "apply", "online": False, "withdrawn_lines": 5, "swatches_enabled": True,
    "lines": [
        {"lane": "backpack", "house": "DA_HouseArgosaz", "template": "SolarisCoin", "qty": 70000, "order_id": None, "note": "merged"},
        {"lane": "backpack", "house": "DA_HouseTseida", "template": "SolarisCoin", "qty": 10000, "order_id": None, "note": None},
        {"lane": "backpack", "house": "DA_HouseHagal", "template": "RepairTool5", "qty": 1, "order_id": None, "note": None},
        {"lane": "exchange", "house": "DA_HouseArgosaz", "template": "T6GunPart", "qty": 500, "order_id": 9001, "note": None},
        {"lane": "exchange", "house": "DA_HouseArgosaz", "template": "T6GunPart", "qty": 500, "order_id": 9002, "note": None},
        {"lane": "exchange", "house": "DA_HouseSpinette", "template": "T6GunPart", "qty": 200, "order_id": 9003, "note": None},
        {"lane": "unlock", "house": "DA_HouseEcaz", "template": "Ecaz_Placeables_Swatch", "qty": 1, "order_id": None, "note": "PlaceableDyePackEcaz"},
        {"lane": "unlock", "house": "DA_HouseImota", "template": "Imota_Placeables_Swatch", "qty": 1, "order_id": None, "note": "already_unlocked"},
        {"lane": "rep", "house": "DA_HouseHagal", "template": "T6Watertube", "qty": 25, "order_id": None, "note": "no_room"},
    ],
}

print("== shape_receipt ==")
r = lc.shape_receipt(RESULT)
check("Solari summed across houses", r["solari"] == 80000, r["solari"])
check("Solari not repeated as a backpack row", all(x["name"] != "Solari" for x in r["backpack"]))
check("three exchange stacks read as one row", len(r["exchange"]) == 1 and r["exchange"][0]["qty"] == 1200, r["exchange"])
check("both houses named on the merged row", r["exchange"][0]["houses"] == ["House Argosaz", "House Spinette"], r["exchange"][0]["houses"])
check("exchange note present", bool(r["notes"]["exchange"]) and "CANCELED" in r["notes"]["exchange"])
check("rep line says why", r["at_rep"] and r["at_rep"][0]["reason"] == "no room", r["at_rep"])
check("no-room rep note says it did not fit", r["notes"]["at_rep"] == lc.NOTE_REP, r["notes"])
sw = lc.shape_receipt({"lines": [{"lane": "rep", "house": "DA_HouseEcaz", "template": "Ecaz_Placeables_Swatch", "qty": 1, "note": "swatch_at_rep"},
                                 {"lane": "rep", "house": "DA_HouseHagal", "template": "Hagal_Placeables_Swatch", "qty": 1, "note": "swatch_at_rep"}]})
check("swatch-only plan is empty and never says 'did not fit'", sw["empty"] and sw["notes"]["at_rep"] == lc.NOTE_REP_SWATCH
      and "did not fit" not in sw["notes"]["at_rep"], sw["notes"])
mx = lc.shape_receipt({"lines": [{"lane": "rep", "house": "DA_HouseEcaz", "template": "Ecaz_Placeables_Swatch", "qty": 1, "note": "swatch_at_rep"},
                                 {"lane": "rep", "house": "DA_HouseHagal", "template": "T6GunPart", "qty": 500, "note": "no_room"}]})
check("mixed rep reasons get the neutral note", mx["notes"]["at_rep"] == lc.NOTE_REP_MIXED, mx["notes"])
check("already-unlocked swatch flagged", any(x.get("already") for x in r["unlocked"]))
check("not empty", r["empty"] is False)
blob = json.dumps(r)
check("no order ids leak", all(str(o) not in blob for o in (9001, 9002, 9003)), blob)
check("no internal lane or unlock ids leak", "PlaceableDyePack" not in blob and "\"lane\"" not in blob, blob)
check("no raw DA_ house names leak", "DA_House" not in blob, blob)

empty = lc.shape_receipt({"ok": True, "lines": [
    {"lane": "rep", "house": "DA_HouseHagal", "template": "RepairTool5", "qty": 1, "note": "no_room"}]})
check("a claim that delivered nothing is empty", empty["empty"] is True)

print("== DM queue ==")
lc.record_claim(1644, "9000000000000001", r)
lc.record_claim(1644, "9000000000000001", empty)
pending = lc.pending_dms()
check("only the non-empty claim is queued", len(pending) == 1, pending)
check("receipt round-trips", pending and pending[0]["receipt"]["solari"] == 80000)
cid = pending[0]["id"] if pending else -1
check("ack marks it", lc.ack_dms([cid], "delivered") == 1)
check("ack is idempotent", lc.ack_dms([cid], "delivered") == 0)
check("nothing pending after ack", lc.pending_dms() == [])

print("== DM queue: settle delay and no head-of-line blocking ==")
import asyncio  # noqa: E402
_conn.execute("DELETE FROM portal_landsraad_claim")
lc.record_claim(1644, "9000000000000001", r)
check("a claim younger than the mirror can see is held back",
      asyncio.run(lc.ready_dms(lambda a: _true(), 50, settle_s=360)) == [])
_conn.execute("UPDATE portal_landsraad_claim SET created_at = datetime('now', '-10 minutes')")
check("released once the online reading postdates the claim",
      len(asyncio.run(lc.ready_dms(lambda a: _true(), 50, settle_s=360))) == 1)
_conn.execute("DELETE FROM portal_landsraad_claim")
for _ in range(60):   # 60 players who claimed and have not come back
    lc.record_claim(7, "111", r)
lc.record_claim(8, "222", r)
_conn.execute("UPDATE portal_landsraad_claim SET created_at = datetime('now', '-10 minutes')")
online8 = asyncio.run(lc.ready_dms(lambda a: _is(a, 8), 50, settle_s=360))
check("a returning player is not starved behind 60 who never came back",
      [x["discord_id"] for x in online8] == ["222"], online8)
check("the DM row carries no account id", online8 and "account_id" not in online8[0], online8)
calls = []
asyncio.run(lc.ready_dms(lambda a: _count(calls, a), 50, settle_s=360))
check("one online check per account per poll, not per row", sorted(calls) == [7, 8], calls)
_conn.execute("DELETE FROM portal_landsraad_claim")

print("== claim errors: never say 'nothing was taken' when we do not know ==")
for tok in ("writer_no_output", "psql_timeout", "result_unreadable", None):
    code, msg, status = lc.claim_error("apply", tok)
    check(f"apply {tok!r} -> unconfirmed", code == "unconfirmed" and "Nothing was taken" not in msg, (code, msg))
for tok in ("player_online", "busy", "withdraw_rejected", "write_failed", "stack_catalog_missing", "disabled"):
    code, msg, status = lc.claim_error("apply", tok)
    check(f"apply {tok!r} is a definite refusal", code != "unconfirmed", (code, msg))
code, msg, status = lc.claim_error("preview", "writer_no_output")
check("a preview failure is never 'unconfirmed' (it always rolls back)", code != "unconfirmed", code)
check("player_online is a 409", lc.claim_error("apply", "player_online")[2] == 409)
check("busy is a 409", lc.claim_error("apply", "busy")[2] == 409)
for tok in ("player_online", "busy", "unconfirmed", "failed"):
    m = lc.claim_error("apply", tok)[1]
    check(f"copy for {tok!r} has no em dash", "\u2014" not in m and "--" not in m, m)

print("== rate limit ==")
store = {}
check("first apply allowed", lc.take_slot(store, 1644, "apply", 100.0))
check("second apply inside 10s refused", not lc.take_slot(store, 1644, "apply", 105.0))
check("apply after 10s allowed", lc.take_slot(store, 1644, "apply", 110.5))
check("preview is limited too", lc.take_slot(store, 1644, "preview", 100.0) and not lc.take_slot(store, 1644, "preview", 101.0))
check("preview does not consume the apply slot", lc.take_slot(store, 2036, "preview", 200.0) and lc.take_slot(store, 2036, "apply", 200.1))
check("another account is independent", lc.take_slot(store, 9, "apply", 105.0))

print("== portal gate ==")
check("off is off", not lc.enabled_for(False, frozenset(), 2036))
check("on + no allowlist = nobody (fail closed)", not lc.enabled_for(True, frozenset(), 1644))
check("on + '*' = everyone", lc.enabled_for(True, lc.parse_accounts("*"), 1644))
check("on + allowlist admits the test account", lc.enabled_for(True, frozenset({2036}), 2036))
check("on + allowlist hides it from everyone else", not lc.enabled_for(True, frozenset({2036}), 1644))
check("allowlist parses commas and spaces", lc.parse_accounts("2036, 30313 x 7") == frozenset({2036, 30313, 7}))
check("'*' parses as the wildcard, '**' does not", "*" in lc.parse_accounts("2036,*") and lc.parse_accounts("**") == frozenset())

print("== the real route, stubbed session + relay ==")
import ast  # noqa: E402
import time as _time  # noqa: E402
from unittest.mock import patch  # noqa: E402

_PORTAL = os.path.join(BACKEND, "routers", "portal.py")
_tree = ast.parse(open(_PORTAL).read())
_route = next(n for n in _tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "portal_landsraad_claim")
_route.decorator_list = []
_relay_calls, _audits, _invalidated = [], [], []


def _scope(enabled=True, accounts=frozenset({"*"}), relay=None):
    async def body_and_csrf(request):
        return dict(request["body"]), True

    async def resolve(account_id, selected):
        await asyncio.sleep(0)            # a real await between the gate and the relay
        return 3041, 0

    sc = {
        "Request": object, "time": _time, "json": json, "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
        "config": types.SimpleNamespace(LANDSRAAD_CLAIM_ENABLED=enabled, LANDSRAAD_CLAIM_ACCOUNTS=accounts),
        "landsraad_claim": lc,
        "_require_linked_session_json": lambda request: (("s", "9000000000000001", request["acct"], {}), None),
        "_v2_body_and_csrf": body_and_csrf,
        "_selected_ctrl": lambda request, acct: None,
        "_resolve_buyer_ctrl_and_bank": resolve,
        "_v2_err": lambda error, message, status=400: {"ok": False, "error": error, "message": message, "status": status},
        "_v2_ok": lambda data: {"ok": True, **data},
        "client_ip": lambda request: "127.0.0.1",
        "_last_landsraad_claim_at": {},
    }
    exec(compile(ast.Module(body=[_route], type_ignores=[]), _PORTAL, "exec"), sc)

    async def call_relay(path, method="GET", json_body=None, timeout=0):
        _relay_calls.append((path, dict(json_body or {})))
        await asyncio.sleep(0)
        if relay is None:
            raise TimeoutError("relay timed out")
        return relay

    mods = {"relay": types.SimpleNamespace(call_relay=call_relay),
            "auth": types.SimpleNamespace(audit_log=lambda *a, **k: _audits.append((a, k))),
            "cache": types.SimpleNamespace(invalidate=lambda *a: _invalidated.append(a))}
    return sc, mods


def _req(acct, mode):
    return {"acct": acct, "body": {"mode": mode, "csrf_token": "x"}}


_conn.execute("DELETE FROM portal_landsraad_claim")
sc, mods = _scope(enabled=True, accounts=frozenset({2036}))
with patch.dict(sys.modules, mods):
    out = asyncio.run(sc["portal_landsraad_claim"](_req(1644, "preview")))
check("allowlist: a non-test account is refused before the relay", out.get("error") == "disabled" and not _relay_calls, out)

sc, mods = _scope(relay=RESULT)
with patch.dict(sys.modules, mods):
    async def _two():
        return await asyncio.gather(sc["portal_landsraad_claim"](_req(2036, "apply")),
                                    sc["portal_landsraad_claim"](_req(2036, "apply")))
    a, b = asyncio.run(_two())
check("two racing applies: exactly one reaches the relay", len(_relay_calls) == 1, _relay_calls)
check("the other is rate limited", sorted([a.get("error"), b.get("error")], key=str) == [None, "rate_limited"], (a, b))
check("relay job: server-resolved controller, explicit mode",
      _relay_calls and _relay_calls[0][1]["owner_ctrl"] == 3041 and _relay_calls[0][1]["mode"] == "apply", _relay_calls)
check("an applied claim is queued for the DM", len(lc.pending_dms()) == 1)
check("an applied claim is audited", len(_audits) == 1 and _audits[0][1].get("success") is True, _audits)
check("the rewards cache is dropped under the key it was stored with (int account id)",
      _invalidated == [("dune.landsraad_rewards", 2036)], _invalidated)

_relay_calls.clear(); _audits.clear()
_conn.execute("DELETE FROM portal_landsraad_claim")
sc, mods = _scope(relay=None)
with patch.dict(sys.modules, mods):
    out = asyncio.run(sc["portal_landsraad_claim"](_req(2036, "apply")))
check("a relay timeout on apply is 'unconfirmed', not 'nothing was taken'",
      out.get("error") == "unconfirmed" and "Nothing was taken" not in out.get("message", ""), out)
check("an unconfirmed claim queues no DM", lc.pending_dms() == [])
check("an unconfirmed claim is still audited as a failure", len(_audits) == 1 and _audits[0][1].get("success") is False, _audits)

sc, mods = _scope(relay={"ok": False, "error": "player_online"})
with patch.dict(sys.modules, mods):
    out = asyncio.run(sc["portal_landsraad_claim"](_req(2036, "apply")))
check("player_online comes back as a 409 with the log-out copy", out.get("status") == 409 and "Log out" in out.get("message", ""), out)

print("== overview: claim_enabled is false for anyone off the allowlist ==")
_ov = next(n for n in _tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "portal_landsraad_v2")
_ov.decorator_list = []


def _overview(acct, enabled, accounts):
    async def none(*a, **k):
        return None
    sc = {
        "Request": object, "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
        "config": types.SimpleNamespace(LANDSRAAD_CLAIM_ENABLED=enabled, LANDSRAAD_CLAIM_ACCOUNTS=accounts),
        "landsraad_claim": lc,
        "_require_linked_session_json": lambda request: (("s", "d", acct, {"character_name": "Tester"}), None),
        "_touch_last_session": lambda a: None,
        "mirror": types.SimpleNamespace(get_section=lambda a, s, newer_than=None: {"houses": []}),
        "_shape_landsraad_for_render": lambda raw: {"summary": {"total_lines": 3}, "board": []},
        "_attach_rewards_swatches": lambda r, raw: None,
        "_load_progress": none,
        "_shape_landsraad_board": lambda *a: {"available": False},
        "_attach_board_swatches": lambda *a: None,
        "_v2_ok": lambda data: {"ok": True, **data},
    }
    exec(compile(ast.Module(body=[_ov], type_ignores=[]), _PORTAL, "exec"), sc)
    mods = {"routers.dune": types.SimpleNamespace(cached_landsraad_board=none, cached_landsraad_rewards=none)}
    with patch.dict(sys.modules, mods):
        return asyncio.run(sc["portal_landsraad_v2"](None))["claim_enabled"]


LAUNCH = lc.parse_accounts("1644 41001 41002 2036")
check("owner on the launch allowlist sees the panel", _overview(1644, True, LAUNCH) is True)
check("A tester (main and alt) sees the panel", _overview(41001, True, LAUNCH) and _overview(41002, True, LAUNCH))
check("an ordinary player does NOT see the panel", _overview(31337, True, LAUNCH) is False)
check("flag off: nobody sees it, allowlist or not", _overview(1644, False, LAUNCH) is False)
check("CANARY: flag on with an EMPTY allowlist shows it to nobody", _overview(31337, True, frozenset()) is False)
check("open to everyone: '*' shows it to all", _overview(31337, True, lc.parse_accounts("*")) is True)

print("== writer gate file: allowlist vs everyone ==")
import importlib.util  # noqa: E402
import tempfile  # noqa: E402
_spec = importlib.util.spec_from_file_location("ls_claim_writer", os.path.join(REPO, "scripts", "dune-landsraad-claim.py"))
_w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_w)
with tempfile.TemporaryDirectory() as d:
    f = os.path.join(d, "landsraad-claim-enabled")
    check("missing file refuses everyone", not _w.flag_allows(f, 1644))
    open(f, "w").write("1644\n41001  # tester\n41002  # tester alt\n2036\n")
    check("allowlisted ids pass", all(_w.flag_allows(f, a) for a in (1644, 41001, 41002, 2036)))
    check("anyone else is refused", not _w.flag_allows(f, 31337))
    open(f, "w").write("")
    check("CANARY: an empty file admits NOBODY (fail closed)", not _w.flag_allows(f, 1644))
    open(f, "w").write("\n  \n# only a comment\n")
    check("whitespace and comments only admit nobody", not _w.flag_allows(f, 1644))
    open(f, "w").write("*\n")
    check("'*' admits everyone (the open-to-all step)", _w.flag_allows(f, 31337))
    open(f, "w").write("2036\n*  # opened 2026-10-xx\n")
    check("'*' alongside ids still admits everyone", _w.flag_allows(f, 31337))
    open(f, "w").write("**\n1644*\n")
    check("'**' or '1644*' is not a wildcard", not _w.flag_allows(f, 31337) and not _w.flag_allows(f, 1644))
    os.chmod(f, 0)
    if os.geteuid() != 0:
        check("unreadable file refuses", not _w.flag_allows(f, 1644))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
