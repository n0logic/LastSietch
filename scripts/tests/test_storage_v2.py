#!/usr/bin/env python3
"""Regression tests for the V2 Storage backend (Tier 0-3): the drag-drop MOVE writer
action (scripts/dune-storage-write.py), the transport wiring (relay/app.py +
scripts/dune-relay-dispatch.sh), and the sibling JSON endpoints
(admin-backend/routers/portal.py).

Prod-safe: no real DB or network. Live/dry-run MOVE refusal is exercised through
both drivers and CLI/JSON parsing, with legacy flags absent, off and on. Database
helpers are tripwires. The actual portal gate/route and public capability response
are executed with isolated collaborators. Historical SQL-builder checks remain
reference coverage; the builder is no longer dispatched by the live writer.

Run:  python3 scripts/tests/test_storage_v2.py     (also import-safe)
"""
import ast
import asyncio
import base64
import re
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
# A broken-variant run points ROOT at a scratch COPY of portal.py, the only file
# the wave 7 mutations below touch. Everything else still reads the repo.
ROOT = os.environ.get("STORAGEV2_ROOT", REPO)
CHILD = os.environ.get("STORAGEV2_CHILD") == "1"
WRITER = os.path.join(SCRIPTS, "dune-storage-write.py")
DISPATCH = os.path.join(SCRIPTS, "dune-relay-dispatch.sh")
VMAP = os.path.join(SCRIPTS, "template_volume.json")
RELAY = os.path.join(REPO, "relay", "app.py")
PORTAL = os.path.join(ROOT, "admin-backend", "routers", "portal.py")
MIRROR = os.path.join(REPO, "admin-backend", "mirror.py")
CONTAINERS = os.path.join(SCRIPTS, "dune-containers.py")
CONTAINER_ITEMS = os.path.join(SCRIPTS, "dune-container-items.py")
CONTAINER_SEARCH = os.path.join(SCRIPTS, "dune-container-search.py")
PAWN_MOVE_DDL = os.path.join(SCRIPTS, "sql", "ls_storage_pawn_moves.sql")
NEXTGEN = os.path.join(REPO, "admin-backend", "portal-nextgen", "src")
STORAGE_STATE = os.path.join(NEXTGEN, "lib", "storage.svelte.js")
STORAGE_API = os.path.join(NEXTGEN, "lib", "api.js")
STORAGE_PAGE = os.path.join(NEXTGEN, "routes", "storage", "+page.svelte")
CONTAINER_TILE = os.path.join(
    NEXTGEN, "lib", "components", "storage", "ContainerTile.svelte")
ITEM_CELL = os.path.join(
    NEXTGEN, "lib", "components", "storage", "ItemCell.svelte")
ONLINE_GATE_BANNER = os.path.join(
    NEXTGEN, "lib", "components", "storage", "OnlineGateBanner.svelte")
# Wave 10b lane C (the Grid / List view). These three read from ROOT, not REPO, so
# the broken-variant child at the bottom of this file sees the mutated scratch copy.
_LANE_C_TREE = (
    os.path.join("admin-backend", "portal-nextgen", "src", "lib", "components",
                 "storage", "ItemGrid.svelte"),
    os.path.join("admin-backend", "portal-nextgen", "src", "lib", "components",
                 "storage", "ItemCell.svelte"),
    os.path.join("admin-backend", "portal-nextgen", "src", "routes", "storage",
                 "+page.svelte"),
)
_GRID_REL, _CELL_REL, _PAGE_REL = _LANE_C_TREE
ITEM_GRID_L = os.path.join(ROOT, _GRID_REL)
ITEM_CELL_L = os.path.join(ROOT, _CELL_REL)
STORAGE_PAGE_L = os.path.join(ROOT, _PAGE_REL)
# Karum Phase 0: the Tier 5 transfer writer and the shared gated take it sources.
XFER = os.path.join(SCRIPTS, "dune-item-transfer-op.sh")
TAKE_LIB = os.path.join(SCRIPTS, "lib", "dune-take-item.sh")
TRANSFER_DIALOG = os.path.join(
    REPO, "admin-backend", "portal-nextgen", "src", "lib", "components", "storage",
    "TransferDialog.svelte")
IDEM_UUID = "11111111-2222-3333-4444-555555555555"
PAWN_IDEM_UUID = "11111111-2222-4333-8444-555555555555"


def _load_writer():
    spec = importlib.util.spec_from_file_location("dune_storage_write", WRITER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Writer: unit_volume precedence + VMAP
# --------------------------------------------------------------------------- #

def test_unit_volume_precedence():
    m = _load_writer()
    # volume_override (if set) is authoritative, even over a VMAP entry
    assert m.unit_volume("Water", 7, {"Water": 3}) == 7.0
    # currency / zero-vol set resolves to 0
    assert m.unit_volume("SolarisCoin", None, {}) == 0.0
    # else the VMAP
    assert m.unit_volume("Water", None, {"Water": 3}) == 3.0
    # else UNKNOWN (None) -> caller treats as volume_unverified, never a block
    assert m.unit_volume("MysteryThing", None, {}) is None


def test_vmap_file_valid():
    data = json.load(open(VMAP, encoding="utf-8"))
    assert isinstance(data, dict)
    # currency is pinned to zero volume; metadata keys are ignored by lookup
    assert data.get("SolarisCoin") == 0
    m = _load_writer()
    assert m.unit_volume("__meta__", None, data) is None  # metadata never matched


# --------------------------------------------------------------------------- #
# Writer: MOVE SQL structure (the slot/volume/DD/ownership/atomicity gates)
# --------------------------------------------------------------------------- #

def test_move_sql_has_all_gates():
    m = _load_writer()
    sql = m.build_move_sql(1610, 42, 99, None, 2.5)
    # offline gate
    assert "player_online" in sql and "reconnect_grace_period_end" in sql
    # source locked FOR UPDATE
    assert "FROM dune.items WHERE id = 42 FOR UPDATE" in sql
    # BOTH source and dest ownership via owned_inv_sql membership
    assert sql.count("owned.inv_id") >= 2
    assert "not_owner (src" in sql and "not_owner (dst" in sql
    # explicit DeepDesert hard-fail on both sides
    assert "dst_on_deep_desert" in sql
    assert "src on DeepDesert" in sql
    # slot gate: mic 0 / -1 / bounded
    assert "dst_no_slots" in sql and "dst_full_slots" in sql
    assert "v_dst_mic = -1" in sql and "generate_series(0, v_dst_mic - 1)" in sql
    # volume gate only when miv > 0, UNKNOWN -> volume_unverified (not a block)
    assert "v_dst_miv > 0" in sql and "dst_full_volume" in sql
    assert "v_vol_unverified := true" in sql
    # atomic single-row re-home pinned to the SOURCE inventory + exactly-one-row check
    assert "UPDATE dune.items" in sql
    assert "WHERE id = 42 AND inventory_id = v_src_inv" in sql
    assert "IF v_moved <> 1 THEN RAISE EXCEPTION 'move_failed" in sql


def test_move_sql_template_guard_toggle():
    m = _load_writer()
    guarded = m.build_move_sql(1, 2, 3, "BuildingBlueprint_CopyDevice", None)
    assert "template mismatch" in guarded
    assert "'BuildingBlueprint_CopyDevice'" in guarded
    plain = m.build_move_sql(1, 2, 3, None, None)
    assert "template mismatch" not in plain
    # UNKNOWN unit volume folds to a SQL NULL literal (fail-open on volume)
    assert "v_unit_vol double precision := NULL;" in plain


def test_move_error_tokens_registered():
    m = _load_writer()
    for tok in ("dst_on_deep_desert", "dst_full_volume", "dst_full_slots",
                "dst_no_slots", "item_not_found", "not_owner", "move_failed",
                "move_disabled", "native_move_unavailable"):
        assert tok in m.ERROR_TOKENS, tok
    # the generic 'bank_full' must not shadow the longer dst_full_* tokens
    assert m.ERROR_TOKENS.index("dst_full_volume") < m.ERROR_TOKENS.index("bank_full")


def test_pawn_move_resolver_is_exact_and_separate_from_market_owned_set():
    m = _load_writer()
    sql = m.pawn_storage_inv_sql(1610)
    assert "inv.inventory_type IN (0, 30)" in sql
    assert "eps.player_controller_id = 1610" in sql
    assert "character_state IS DISTINCT FROM 'Deleted'" in sql
    assert "dune.placeables" not in sql
    assert "permission_actor_rank" not in sql
    owned = m.owned_inv_sql(1610)
    assert "inventory_type = 30" in owned
    assert "inventory_type IN (0, 30)" not in owned


def test_pawn_move_sql_has_pair_offline_lock_capacity_and_idempotency_gates():
    m = _load_writer()
    sql = m.build_pawn_move_sql(
        1610, 42, "bank", "backpack", PAWN_IDEM_UUID, "Water", 2.5)
    assert "ls_storage_pawn_moves" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "idempotency_conflict" in sql
    assert "replay', replay" in sql
    assert "FOR SHARE" in sql and "player_online" in sql
    assert "v_backpack_count <> 1" in sql and "v_bank_count <> 1" in sql
    assert "ORDER BY id FOR UPDATE" in sql
    assert "FROM dune.items WHERE id = 42 FOR UPDATE" in sql
    assert "v_actual_src_kind <> 'bank'" in sql
    assert "dst_no_slots" in sql and "dst_full_slots" in sql
    assert "dst_full_volume" in sql and "v_vol_unverified := true" in sql
    assert "WHERE id = 42 AND inventory_id = v_src_inv" in sql
    assert "dune.placeables" not in sql
    assert "permission_actor_rank" not in sql


def test_pawn_move_ledger_schema_is_uuid_unique_and_auditable():
    src = _read(PAWN_MOVE_DDL)
    assert "idempotency_key       uuid        PRIMARY KEY" in src
    assert "owner_ctrl" in src and "src_inventory_id" in src
    assert "dst_inventory_id" in src and "dst_position_index" in src
    assert "requested_src_kind" in src and "requested_dst_kind" in src
    assert "ALTER TABLE dune.ls_storage_pawn_moves OWNER TO dune" in src


def test_pawn_move_dark_file_gate_short_circuits_before_db():
    job = {
        "action": "pawn_move", "owner_ctrl": 1, "item_id": 2,
        "src_storage": "bank", "dst_storage": "backpack",
        "idempotency_key": PAWN_IDEM_UUID,
    }
    proc = subprocess.run(
        [sys.executable, WRITER, "--stdin-json"],
        input=json.dumps(job), capture_output=True, text=True, timeout=30)
    out = json.loads(proc.stdout)
    assert out == {"ok": False, "error": "pawn_move_disabled"}, out


# --------------------------------------------------------------------------- #
# Writer: kill-switch (flag OFF -> move_disabled, no DB touch)
# --------------------------------------------------------------------------- #

def test_move_requests_never_resolve_game_database():
    for enabled in (False, True):
        for driver_name in ("do_live", "do_dry_run"):
            m = _load_writer()
            m.MOVE_ENABLED = enabled

            def unexpected(*args, **kwargs):
                raise AssertionError("unsupported move reached a database helper")

            m.resolve_db_pod = m._dq = m.run_psql = unexpected
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    getattr(m, driver_name)("move", 1, None, 0, item_id=2, dst_inv=3)
                except SystemExit as exc:
                    assert exc.code == 0
            out = json.loads(buf.getvalue())
            assert out["ok"] is False and out["error"] == "native_move_unavailable", out
            if driver_name == "do_dry_run":
                assert out["dry_run"] is True and out["move_enabled"] is False, out


def test_move_cli_and_json_refuse_with_every_legacy_flag_state():
    with tempfile.TemporaryDirectory(prefix="storage-move-refusal-") as directory:
        sentinel = os.path.join(directory, "sudo")
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 93\n")
        os.chmod(sentinel, 0o755)
        bootstrap = (
            "import runpy,subprocess,sys\n"
            "def forbidden(*args, **kwargs):\n"
            "    raise AssertionError('move parsing reached external execution')\n"
            "subprocess.run=forbidden\n"
            f"sys.argv=[{WRITER!r}]+sys.argv[1:]\n"
            f"runpy.run_path({WRITER!r},run_name='__main__')\n"
        )
        for flag in (None, "0", "1"):
            for use_json in (False, True):
                for dry_run in (False, True):
                    env = dict(os.environ, PATH=directory)
                    env.pop("LASTSIETCH_STORAGE_MOVE_ENABLED", None)
                    if flag is not None:
                        env["LASTSIETCH_STORAGE_MOVE_ENABLED"] = flag
                    job = {"action": "move", "owner_ctrl": 1, "item_id": 2,
                           "dst_inventory_id": 3, "dry_run": dry_run}
                    args = [sys.executable, WRITER]
                    if use_json:
                        args.append("--stdin-json")
                    else:
                        args += ["--action", "move", "--owner-ctrl", "1",
                                 "--item-id", "2", "--dst-inventory-id", "3"]
                        if dry_run:
                            args.append("--dry-run")
                    result = subprocess.run([sys.executable, "-c", bootstrap] + args[2:],
                                            input=json.dumps(job) if use_json else None,
                                            capture_output=True, text=True, env=env, timeout=10)
                    assert result.returncode == 0, result.stderr
                    out = json.loads(result.stdout)
                    assert out["ok"] is False and out["error"] == "native_move_unavailable", out
                    if dry_run:
                        assert out["dry_run"] is True and out["move_enabled"] is False, out


def test_portal_move_cannot_open_with_all_feature_flags_on():
    node, _body = _portal_func("_v2_move_enabled")
    pawn, _body = _portal_func("_v2_pawn_move_enabled")
    scope = {}
    exec(compile(ast.Module(body=[node, pawn], type_ignores=[]), PORTAL, "exec"), scope)
    with patch.dict(sys.modules, {"feature_flags": SimpleNamespace(enabled=lambda *args: True)}):
        assert scope["_v2_move_enabled"]() is False
        assert scope["_v2_pawn_move_enabled"]() is True


def test_portal_move_refuses_before_container_reads_and_relay():
    gate, _body = _portal_func("_v2_move_enabled")
    route, _body = _portal_func("portal_storage_v2_move")
    route.decorator_list = []

    async def body_and_csrf(request):
        return {"item_id": 2, "dst_container_id": 3}, True

    async def unexpected(*args, **kwargs):
        raise AssertionError("unsupported move reached container reads or relay")

    scope = {
        "Request": object,
        "_require_linked_session_json": lambda request: (("session", 10, 11, {}), None),
        "_v2_body_and_csrf": body_and_csrf,
        "_v2_err": lambda error, message, status: {"error": error, "status": status},
        "_MOVE_ERROR_TEXT": {"native_move_unavailable": "unavailable", "move_disabled": "disabled"},
        "_storage_rate_ok": lambda account: True,
        "_load_containers": unexpected,
    }
    exec(compile(ast.Module(body=[gate, route], type_ignores=[]), PORTAL, "exec"), scope)
    with patch.dict(sys.modules, {"feature_flags": SimpleNamespace(enabled=lambda *args: True),
                                  "relay": SimpleNamespace(call_relay=unexpected)}):
        early = {"error": "auth", "status": 401}
        scope["_require_linked_session_json"] = lambda request: (None, early)
        scope["_v2_body_and_csrf"] = unexpected
        assert asyncio.run(scope["portal_storage_v2_move"](None)) == early
        scope["_require_linked_session_json"] = lambda request: (("session", 10, 11, {}), None)

        async def invalid_csrf(request):
            return {}, False

        scope["_v2_body_and_csrf"] = invalid_csrf
        assert asyncio.run(scope["portal_storage_v2_move"](None)) == {"error": "csrf", "status": 403}
        scope["_v2_body_and_csrf"] = body_and_csrf
        out = asyncio.run(scope["portal_storage_v2_move"](None))
    assert out == {"error": "native_move_unavailable", "status": 200}, out


def test_currency_drivers_still_reach_their_sql_path():
    for action, mode, amount in (("withdraw", None, 10), ("deposit", "amount", 10)):
        m = _load_writer()
        calls = []
        m.resolve_db_pod = lambda: ("synthetic-ns", "synthetic-db")

        def fake_psql(namespace, pod, sql, timeout):
            calls.append((namespace, pod, sql))
            return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "action": action}), stderr="")

        m.run_psql = fake_psql
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                m.do_live(action, 1, mode, amount)
            except SystemExit as exc:
                assert exc.code == 0
        assert json.loads(buf.getvalue()) == {"ok": True, "action": action}
        assert len(calls) == 1 and calls[0][:2] == ("synthetic-ns", "synthetic-db")
        assert "player_online" in calls[0][2] and "COMMIT;" in calls[0][2]



def test_move_unavailable_when_flag_off():
    env = dict(os.environ)
    # The legacy flag is ignored while no native backend exists.
    env["LASTSIETCH_STORAGE_MOVE_ENABLED"] = "0"
    job = {"action": "move", "owner_ctrl": 1, "item_id": 2, "dst_inventory_id": 3}
    proc = subprocess.run(
        [sys.executable, WRITER, "--stdin-json"],
        input=json.dumps(job), capture_output=True, text=True, env=env, timeout=30)
    out = json.loads(proc.stdout)
    assert out == {"ok": False, "error": "native_move_unavailable"}, out


def test_withdraw_deposit_still_build():
    # Regression guard: the pre-existing currency paths are untouched.
    m = _load_writer()
    assert "player_online" in m.build_withdraw_sql(1, 100)
    assert "delete_items" in m.build_deposit_sql(1, "sweep", 0)
    assert "delete_inventory_item" in m.build_deposit_sql(1, "amount", 50)


# --------------------------------------------------------------------------- #
# Transport: relay routes + dispatcher case
# --------------------------------------------------------------------------- #

def test_relay_has_move_and_transfer_routes():
    src = _read(RELAY)
    for route in ('@app.post("/dune/storage/move", dependencies=[Depends(verify_key)])',
                  '@app.post("/dune/item-transfer-op", dependencies=[Depends(verify_key)])'):
        assert route in src, route
    # both encode compact sorted-key JSON, guard the b64 alphabet, ssh over stdin
    assert '_dune_ssh_stdin("storage-move"' in src
    assert '_dune_ssh_stdin("item-transfer-op"' in src
    assert src.count("sort_keys=True") >= 2
    # move route forwards action:move + the required ids
    assert '"action": "move"' in src and '"dst_inventory_id": dst_inventory_id' in src
    # transfer never trusts a client sender (resolved server-side upstream); item id required
    assert 'sender_account_id = _pos_int("sender_account_id")' in src


def test_dispatcher_has_storage_move_case():
    src = _read(DISPATCH)
    assert "storage-move)" in src
    assert "/root/dune-storage-write.py --stdin-json" in src
    # the storage-move branch keeps the safe-b64 alphabet guard
    seg = src.split("storage-move)", 1)[1].split(";;", 1)[0]
    assert "[A-Za-z0-9+/=]" in seg
    assert "base64 -d | /root/dune-storage-write.py --stdin-json" in seg


def test_pawn_move_transport_carries_kinds_and_uuid_without_inventory_ids():
    src = _read(RELAY)
    route = src.split('@app.post("/dune/storage/pawn-move"', 1)[1] \
               .split("\n@app.", 1)[0]
    assert '_dune_ssh_stdin("storage-pawn-move"' in route
    assert '"action": "pawn_move"' in route
    assert '"src_storage": src_storage' in route
    assert '"dst_storage": dst_storage' in route
    assert '"idempotency_key": idem' in route
    forwarded = route.split("fields = {", 1)[1].split("payload =", 1)[0]
    assert "dst_inventory_id" not in forwarded

    dispatch = _read(DISPATCH)
    seg = dispatch.split("storage-pawn-move)", 1)[1].split(";;", 1)[0]
    assert "[A-Za-z0-9+/=]" in seg
    assert "base64 -d | /root/dune-storage-write.py --stdin-json" in seg


# --------------------------------------------------------------------------- #
# Backend endpoints: JSON contract + safety invariants (source scan)
# --------------------------------------------------------------------------- #

def test_portal_v2_endpoints_present():
    src = _read(PORTAL)
    for route in ('@router.get("/portal/storage/v2")',
                  '@router.get("/portal/containers/v2")',
                  '@router.get("/portal/containers/v2/{container_id}/items")',
                  '@router.get("/portal/containers/v2/search")',
                  '@router.post("/portal/storage/v2/withdraw")',
                  '@router.post("/portal/storage/v2/deposit")',
                  '@router.post("/portal/storage/v2/repair/box")',
                  '@router.post("/portal/storage/v2/repair/gear")',
                  '@router.post("/portal/storage/v2/repair/everything")',
                  '@router.post("/portal/storage/v2/pawn-move")',
                  '@router.post("/portal/storage/v2/move")',
                  '@router.post("/portal/storage/v2/transfer")'):
        assert route in src, route


def test_portal_never_uses_raw_form():
    # STOP-SHIP invariant: every V2 mutation (and V1) reads JSON via _read_body,
    # NEVER FastAPI Form()/request.form().
    src = _read(PORTAL)
    assert "request.form(" not in src
    assert "await _read_body(request)" in src or "_v2_body_and_csrf" in src


def test_portal_v1_storage_routes_untouched():
    # The V1 HTML routes must still exist unchanged (zero-regression governing rule).
    src = _read(PORTAL)
    assert '@router.post("/portal/storage/withdraw")' in src
    assert '@router.post("/portal/storage/deposit")' in src
    assert '@router.get("/portal/storage")' in src


def test_portal_move_behind_killswitch():
    src = _read(PORTAL)
    v2 = src.split("V2 Storage Manager", 1)[1]
    # the move endpoint honours the kill-switch and never dispatches while off
    assert "if not _v2_move_enabled():" in v2
    assert 'return _v2_err("native_move_unavailable"' in v2


def test_portal_pawn_move_is_separately_dark_and_clamps_to_storage_kinds():
    src = _read(PORTAL)
    route = src.split('@router.post("/portal/storage/v2/pawn-move")', 1)[1] \
               .split("\n@router.", 1)[0]
    assert "if not _v2_pawn_move_enabled():" in route
    assert 'return _v2_err("pawn_move_disabled"' in route
    assert "_pick_pawn_storage_pair(clist, owner_ctrl)" in route
    assert '"src_storage": src_storage' in route
    assert '"dst_storage": dst_storage' in route
    assert '"idempotency_key": idem' in route
    assert 'call_relay("/dune/storage/pawn-move"' in route
    forwarded = route.split("move_body = {", 1)[1].split("ip = client_ip", 1)[0]
    assert "dst_inventory_id" not in forwarded
    assert "await _resolve_online(active_account_id) is True" in route
    assert '"portal_storage_pawn_move"' in route


def test_portal_overview_filters_pawn_storage_to_selected_character():
    src = _read(PORTAL)
    overview = src.split('async def portal_storage_v2(request: Request):', 1)[1] \
                  .split("\n@router.", 1)[0]
    assert "_pick_backpack(clist, _ctrl)" in overview
    assert "_visible_storage_for_ctrl(clist, _ctrl)" in overview
    assert '"backpack": {' in overview
    assert '"pawn_move_enabled": _v2_pawn_move_enabled()' in overview
    assert '"containers": [_v2_container(c) for c in visible_clist]' in overview


def test_item_grid_fails_closed_without_selected_character_container_model():
    src = _read(PORTAL)
    route = src.split('async def portal_container_v2_items(', 1)[1] \
               .split("\n@router.", 1)[0]
    assert 'if not containers or not containers.get("available"):' in route
    assert 'if match is None:' in route
    assert 'if match.get("is_pawn_storage"):' in route
    assert 'str(match.get("owner_ctrl")) != str(ctrl)' in route


def test_portal_transfer_dark_never_fakes_success():
    src = _read(PORTAL)
    v2 = src.split("V2 Storage Manager", 1)[1]
    # deferred is surfaced as its own soft state, only applied/replay is a success
    assert 'if status == "deferred":' in v2
    assert '"status": "deferred"' in v2
    assert 'status in ("applied", "replay")' in v2


def test_portal_server_side_identity():
    src = _read(PORTAL)
    v2 = src.split("V2 Storage Manager", 1)[1]
    # sender/owner_ctrl come from the session, never the client body. Multi-character
    # (2026-07-13): scoped by the SELECTED character (server-validated), still never
    # trusting a client-supplied controller.
    assert '"sender_account_id": active_account_id' in v2
    assert ("owner_ctrl, _bank = await _resolve_buyer_ctrl_and_bank("
            "active_account_id, _selected_ctrl(request, active_account_id))") in v2
    # transfer resolves the recipient server-side (game names are encrypted, and a
    # code is resolved in-request), never from a client-supplied account id
    assert "_resolve_recipient(" in v2
    assert "_resolve_account_by_char_name(" in src
    assert 'body.get("recipient_account_id")' not in v2


# --------------------------------------------------------------------------- #
# Container-id NAMESPACE (fixed 2026-07-16). The read path emits `id` (a PLACEABLE
# id for a box; an INVENTORY id for the bank/vehicle) plus `inv_id` (always the real
# inventory id). Writers gate on owned_inv_sql(), which only ever returns inventory
# ids, so they MUST be sent inv_id.
#
# This is a DATA-INTEGRITY guard, not a cosmetic one: the two namespaces overlap, so
# forwarding a read id to a writer did NOT fail safe. Live (acct 1644): placeable
# 3015's inventory is 2969, and 2969 is itself a placeable id. A non-colliding id
# surfaced as `not_owner`; a COLLIDING id matched owned_inv_sql and silently moved the
# stack into the WRONG container. Never let an `id` reach a writer.
# --------------------------------------------------------------------------- #

def test_backpack_is_present_in_container_item_and_search_read_models():
    containers = _read(CONTAINERS)
    assert "'Backpack' AS class" in containers
    assert "inv.inventory_type = 0" in containers
    assert "eps.character_state IS DISTINCT FROM 'Deleted'" in containers

    items = _read(CONTAINER_ITEMS)
    assert "Backpack (inventory_type 0 on an active pawn)" in items
    assert items.count("eps.character_state IS DISTINCT FROM 'Deleted'") >= 2

    search = _read(CONTAINER_SEARCH)
    assert "'owner_ctrl', sub.owner_ctrl" in search
    assert "'Backpack' AS ctype" in search
    assert "MAX(eps.player_controller_id) AS owner_ctrl" in search


def test_backpack_shaping_and_picker_fail_closed_to_selected_controller():
    src = _read(PORTAL)
    assert 'is_backpack = cls == "Backpack"' in src
    assert '"is_pawn_storage": is_bank or is_backpack' in src
    picker = src.split("def _pick_backpack(", 1)[1].split("\ndef ", 1)[0]
    assert "if owner_ctrl is None:" in picker
    assert 'str(c["owner_ctrl"]) == str(owner_ctrl)' in picker
    assert "return None" in picker


def test_frontend_only_enables_bank_and_backpack_moves():
    state = _read(STORAGE_STATE)
    assert "pawnMoveEnabled" in state
    assert "pawnStorageKind" in state
    assert "api.storage.pawnMove(body)" in state
    assert "expected_source: srcStorage" in state
    assert "destination: dstStorage" in state
    assert "moveIntentUuid(intentKey)" in state
    assert "MOVE_INTENT_TTL_MS" in state
    assert "moveIntentKeys.delete(intentKey)" in state
    assert "storage.containers.find((c) => c.is_backpack)" in state
    assert "api.storage.move(body)" not in state

    page = _read(STORAGE_PAGE)
    assert 'storageKind="bank"' in page
    assert "writable={bankWritable}" in page
    assert "writable={selWritable}" in page
    # Live QA 2026-09-03: the flags were inverted (bank never, containers always) while
    # the writer refuses container sources and accepts the bank. Bank on, and the bank
    # lane needs no flag because it has been open since June.
    # Wave 5 (2026-09-03) opened the BACKPACK lane behind /etc/lastsietch/market-sell-backpack-enabled,
    # so the right-hand panel is no longer a flat no: it offers List on Exchange for a
    # backpack, and only while that flag is on. Containers and vehicles stay off, which
    # is what selStorageKind gives us for free (it is '' for anything but the two pawn
    # inventories). The full contract is pinned in test_market_sell_backpack.py.
    bank_grid = page[page.index('panel="bank"'):page.index("</ItemGrid>", page.index('panel="bank"')) if "</ItemGrid>" in page[page.index('panel="bank"'):] else page.index("/>", page.index('panel="bank"'))]
    assert "allowMarketSell={true}" in bank_grid, "the bank grid must offer List on Exchange"
    sel_grid = page[page.index('panel="container"'):page.index("/>", page.index('panel="container"'))]
    assert ("allowMarketSell={selStorageKind === 'backpack' && storage.marketBackpackEnabled}"
            in sel_grid), "only the backpack, and only under its flag"
    assert "{#if tradeable && allowMarketSell}" in _read(ITEM_CELL), "the cell must not re-gate on the container panel"

    tile = _read(CONTAINER_TILE)
    assert "isPawnStorage && storage.pawnMoveEnabled" in tile
    assert "Read-only" in tile
    cell = _read(ITEM_CELL)
    assert "Move to {moveTargetLabel}" in cell
    assert "storage.pawnMoveEnabled" in cell
    assert "src_storage: storageKind" in cell
    assert "allowMarketSell" in cell

    banner = _read(ONLINE_GATE_BANNER)
    assert "offlineLocked || !storage.pawnMoveEnabled" in banner
    assert "Bank and Backpack transfers are not open yet" in banner
    assert "Bank and Backpack transfers are unlocked" in banner

    api = _read(STORAGE_API)
    assert "pawnMove: (body) => sendCsrfJSON(" in api
    assert "'/portal/storage/v2/pawn-move'" in api

def test_containers_read_emits_inv_id_in_every_branch():
    src = _read(CONTAINERS)
    # exposed in the JSON payload...
    assert "'inv_id', sub.inv_id," in src
    # ...and produced by all three UNION branches: placeables aggregate the real
    # inventory id, bank + vehicle alias their own inv.id so the field is uniform.
    assert "MAX(inv.id) AS inv_id" in src, "placeables branch must emit the real inv id"
    assert src.count("inv.id AS inv_id") >= 2, "bank + vehicle branches must emit inv_id"
    # `id` semantics are UNCHANGED: V1 + the items drawer resolve a box by placeable id.
    assert "SELECT p.id," in src


def test_containers_read_keeps_placeable_id_distinct_from_inv_id():
    """Placeable: id (p.id) and inv_id (the inventory) are DIFFERENT expressions.
    Bank/vehicle: both are inv.id (identity). Aliasing the placeable's inv_id back to
    p.id would silently restore the original bug -- and its wrong-container variant."""
    src = _read(CONTAINERS)
    tpl = src.split('SQL_TEMPLATE = """', 1)[1].split('"""', 1)[0]
    clean = "\n".join(l.split("--", 1)[0] for l in tpl.splitlines())
    body = clean.split("FROM (", 1)[1].rsplit(") sub;", 1)[0]
    branches = body.split("UNION ALL")
    # Placeable, Bank, Backpack, cargo vehicle, and storage-less vehicle.
    assert len(branches) == 5, f"expected 5 UNION branches, got {len(branches)}"

    # Anchor the split to a line-initial FROM: the storage-less-vehicle branch resolves
    # its ids with a scalar subquery that carries its OWN inline "FROM dune.inventories",
    # so an unanchored split truncates that branch mid-SELECT-list.
    placeable, bank, backpack, vehicle, bare_vehicle = (
        b.split("\n    FROM dune.", 1)[0] for b in branches)
    # placeable: keyed by p.id, inv_id resolved from the inventories join -- NOT p.id
    assert "p.id" in placeable and "MAX(inv.id) AS inv_id" in placeable, \
        "placeable branch must key by p.id and resolve inv_id from the inventories join"
    assert "p.id AS inv_id" not in placeable, \
        "placeable inv_id must NOT alias p.id -- that restores the wrong-container bug"
    # Bank, Backpack, and vehicle: container id IS the inventory id.
    for name, branch in (("bank", bank), ("backpack", backpack), ("vehicle", vehicle)):
        assert "inv.id AS id" in branch, f"{name} id must be inv.id"
        assert "inv.id AS inv_id" in branch, f"{name} inv_id must be inv.id"

    # every branch must expose the same column count/order or the UNION ALL errors out
    def cols(head):
        out, depth, cur = [], 0, ""
        for ch in head:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                out.append(cur)
                cur = ""
            else:
                cur += ch
        out.append(cur)
        return [c for c in (" ".join(x.split()) for x in out) if c]

    # The storage-less-vehicle branch resolves both ids through the SAME scalar subquery
    # over dune.inventories, so `id` and `inv_id` still agree; there is no p.id-style
    # namespace to confuse, and max_item_count 0 is what flags "no cargo grid".
    assert bare_vehicle.count(
        "(SELECT MIN(inv.id) FROM dune.inventories inv WHERE inv.actor_id = a.id)") == 2, \
        "storage-less vehicle branch must resolve id and inv_id identically"
    assert "0 AS max_item_count" in bare_vehicle

    widths = {len(cols(b)) for b in (placeable, bank, backpack, vehicle, bare_vehicle)}
    # The invariant that actually breaks prod is ALIGNMENT -- a mismatched branch makes
    # the UNION ALL error at runtime -- so assert that separately from the count, which
    # is pinned only to catch an accidental column drop.
    assert len(widths) == 1, f"UNION ALL branch widths must MATCH each other, got {widths}"
    assert widths == {10}, (
        "expected 10 columns per branch (id, inv_id, owner_ctrl, name, class, label, "
        f"map, item_count, max_item_count, max_item_volume), got {widths}")


def test_move_endpoint_sends_inv_id_not_container_id():
    src = _read(PORTAL)
    # translate, with a fallback so a pre-deploy read model (no inv_id) can't hard-break
    assert 'dst_inventory_id = dst.get("inv_id") or dst_container_id' in src
    assert '"dst_inventory_id": dst_inventory_id' in src
    # the raw READ id must never be forwarded as an inventory id again
    assert '"dst_inventory_id": dst_container_id' not in src


def test_shaper_carries_inv_id_with_fallback():
    src = _read(PORTAL)
    assert '"inv_id": c.get("inv_id") or c.get("id"),' in src


def test_repair_box_translates_container_id():
    # Same defect class: the client sends storage.selectedId (a placeable id for a box)
    # and dune-repair-write.py gates it against ITS owned_inv_sql (inventory ids), so a
    # box repair silently targeted nothing and reported 0 repaired.
    src = _read(PORTAL)
    assert 'box = next((c for c in clist if str(c.get("id")) == str(inv_id)), None)' in src
    assert 'inv_id = box.get("inv_id") or inv_id' in src


def test_move_invalidates_mirror_blob_and_both_item_grids():
    src = _read(PORTAL)
    # the mirror is consulted BEFORE the relay cache, so dropping only the cache is a
    # no-op while LASTSIETCH_PORTAL_MIRROR_READS=1 -- the blob must go too
    assert "_mirror.invalidate_storage(active_account_id)" in src
    assert 'invalidate("dune.player_containers", str(active_account_id))' in src
    assert 'invalidate("dune.player_container_items",' in src
    # the grid cache is keyed by the READ id, so the writer's src_inv (an inventory id)
    # has to be mapped back or the drop misses a placeable's grid entirely
    assert 'inv_to_read = {str(c.get("inv_id") or c.get("id")): str(c.get("id"))' in src
    assert 'src_container_id = (inv_to_read.get(str(src_inv), str(src_inv))' in src


def test_move_response_ids_are_read_namespace():
    # The frontend refetches both ends with api.storage.containerItems(id), which speaks READ
    # ids. Returning the writer's src_inv (an INVENTORY id) under a *_container_id key
    # would send it probing a non-existent container for any placeable source.
    src = _read(PORTAL)
    assert '"src_container_id": src_container_id,' in src
    assert '"src_container_id": src_inv,' not in src


def test_mirror_exposes_invalidate_storage():
    src = _read(MIRROR)
    assert "def invalidate_storage(account_id):" in src
    assert "DELETE FROM player_storage WHERE account_id=?" in src


def test_move_audit_records_both_id_namespaces():
    # Logging only the client's container id is what hid the mismatch in audit_log.
    src = _read(PORTAL)
    assert '"dst_container_id": dst_container_id,' in src
    assert '"dst_inventory_id": dst_inventory_id,' in src


# --------------------------------------------------------------------------- #
# KARUM PHASE 0 (2026-07-27): the SHARED GATED TAKE.
#
# Live-tested 2026-07-26: giving to an online player is safe, TAKING from one is not (the
# client holds the whole item record including its id and writes it back to a destination
# without re-checking the row exists, so a removal is resurrected under its original id).
# The Tier 5 transfer TAKES from the sender, so the sender is now offline-gated, and the
# gate lives in ONE place that both this writer and the Phase 1 Karum writer source.
#
# These are structural guards. The gate's BEHAVIOUR is proven by executing the real SQL
# against a throwaway postgres in scripts/tests/test_take_item_pg.sh, which is where a
# regression in what the gate actually refuses will surface.
# --------------------------------------------------------------------------- #

def _transfer_dry_run_sql(template="T6BladePart"):
    """The transaction the Tier 5 writer really assembles, via its own --dry-run. Exits
    before resolve_db_pod, so this needs no game host and no DB."""
    job = {"sender_account_id": 11, "recipient_account_id": 22, "item_id": 33,
           "template_id": template, "idempotency_key": IDEM_UUID,
           "operator": "unittest", "mode": "dry-run"}
    payload = base64.b64encode(json.dumps(job).encode()).decode()
    env = dict(os.environ)
    env["LASTSIETCH_ITEM_TRANSFER_ENABLED"] = "1"     # dry-run still refuses while dark
    proc = subprocess.run(["bash", XFER, "--op-b64-stdin"], input=payload,
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out.get("status") == "dry-run", out
    return out["sql"]


def test_shared_take_library_exists_and_is_sourced_not_forked():
    """Owner decision D3: ONE gated-take implementation, shared by both callers. The
    fallback if extraction had proved invasive was 'same pattern, independently verified';
    it did not, so this asserts the shared library instead."""
    assert os.path.isfile(TAKE_LIB), TAKE_LIB
    lib = _read(TAKE_LIB)
    assert "karum_take_item()" in lib
    xfer = _read(XFER)
    # sourced, resolved relative to the writer so repo scripts/lib and host /root/lib
    # both work, and fail-closed when the library is absent
    assert "lib/dune-take-item.sh" in xfer
    assert "require_take_lib" in xfer
    assert "shared take library missing" in xfer
    # and the writer must NOT carry a take of its own any more. Comments are stripped
    # first: the header still quotes the old re-home UPDATE to explain what moved where.
    code = "\n".join(ln for ln in xfer.splitlines() if not ln.lstrip().startswith("#"))
    assert "UPDATE dune.items" not in code, "the transfer writer re-grew its own take"


def test_shared_take_sql_has_every_gate():
    lib = _read(TAKE_LIB)
    sql = _transfer_dry_run_sql()
    assert "shared gated take" in sql, "the writer is not emitting the shared take"
    # offline gate: status AND reconnect grace, under a row lock, fail-closed
    assert "player_online" in sql
    assert "reconnect_grace_period_end" in sql
    assert "FOR SHARE" in sql, "the eps rows must be locked so a login cannot race the gate"
    assert "has no live character row, status undetermined" in sql
    # tombstone-safe everywhere eps is read (a Deleted char can stick at Online forever)
    assert sql.count("character_state IS DISTINCT FROM 'Deleted'") >= 3
    # item locked FOR UPDATE, pinned to the source bank (ownership + existence in one)
    assert "AND it.inventory_id = v_src_inv\n     FOR UPDATE" in sql
    assert "item_not_found" in sql
    # template identity guard
    assert "template mismatch" in sql
    # the re-home is pinned so exactly one row can match, and it is a move not a copy
    assert "WHERE id = v_item\n     AND inventory_id = v_src_inv" in sql
    assert "expected to move exactly 1 item row" in sql
    assert "INSERT INTO dune.items" not in lib, "a take must never insert an item row"


def test_shared_take_honours_the_caller_replay_flag():
    """Without the skip flag an idempotent replay re-runs the take against a source that
    no longer holds the row and turns a harmless retry into a hard failure."""
    sql = _transfer_dry_run_sql()
    assert "set_config('ls.take_skip', '1', true)" in sql       # caller sets it
    # take, caps and post-take blocks all check it
    assert sql.count("current_setting('ls.take_skip', true), '') = '1'") >= 3


def test_shared_take_serves_the_karum_destination_without_a_fork():
    """The ONLY difference between the two callers is the destination. If dst=exchange
    stops working the Phase 1 writer has to fork the take, which breaks D3."""
    lib = _read(TAKE_LIB)
    assert "dune.get_exchange_inventory_id(2)" in lib      # exchange inv 610
    assert "bank:" in lib
    # The Karum escrow marker (a HolKarum key merged into dune.items.stats, contract
    # section 4.3b) and the LT-7 position-collision mitigation are PARAMETERS here, so the
    # library stays generic and the Karum writer does not need its own UPDATE.
    assert "stats_patch" in lib
    assert "stats = COALESCE(stats, '{}'::jsonb) || '${stats_patch}'::jsonb" in lib
    assert "min_position" in lib and "GREATEST(COALESCE(MAX(position_index)" in lib


def test_transfer_writer_still_dark_and_defers_before_touching_the_library():
    """A box with the writer but not the library must still answer 'deferred', so the
    library landing late can never turn the dark path into a new failure mode."""
    job = {"sender_account_id": 11, "recipient_account_id": 22, "item_id": 33,
           "idempotency_key": IDEM_UUID}
    payload = base64.b64encode(json.dumps(job).encode()).decode()
    env = dict(os.environ)
    env.pop("LASTSIETCH_ITEM_TRANSFER_ENABLED", None)             # default = 0 = DARK
    proc = subprocess.run(["bash", XFER, "--op-b64-stdin"], input=payload,
                          capture_output=True, text=True, env=env, timeout=30)
    out = json.loads(proc.stdout)
    assert out.get("status") == "deferred", out
    assert out.get("success") is True, out
    xfer = _read(XFER)
    # the dark gate is upstream of require_take_lib in the file, hence at runtime too
    assert xfer.index("LASTSIETCH_ITEM_TRANSFER_ENABLED\"") < xfer.index("require_take_lib\n")


def test_transfer_writer_emits_error_tokens():
    """The relay passes the writer's JSON through verbatim and the portal reads
    result["error"], so a token here is what lets the UI say 'log out first'."""
    xfer = _read(XFER)
    assert '"error":%s' in xfer
    for tok in ("player_online", "no_bank", "item_not_found", "take_failed",
                "bank_full", "rate_limited"):
        assert tok in xfer, tok
    sql = _transfer_dry_run_sql()
    # rate caps are checked BEFORE the take so a capped request never touches dune.items
    assert sql.index("rate_limited") < sql.index("shared gated take")


def test_transfer_route_offline_gates_the_sender_only():
    src = _read(PORTAL)
    route = src.split('@router.post("/portal/storage/v2/transfer")', 1)[1] \
               .split("\n@router.", 1)[0]
    # edge gate on the SENDER (session account), definitely-online only; undetermined
    # falls through to the writer, which is the authoritative fail-closed gate
    assert "await _resolve_online(active_account_id) is True" in route
    assert '_v2_err("player_online"' in route
    # the recipient is NOT gated: a give is online-safe
    assert "_resolve_online(recipient_account_id)" not in route
    # friendly text is ours; the writer's message names inventory + account ids
    assert "_TRANSFER_ERROR_TEXT" in src
    assert 'result.get("message") or "That transfer could not be completed."' not in route


def test_transfer_dialog_locks_while_online():
    src = _read(TRANSFER_DIALOG)
    assert "storage.offlineOk" in src
    assert "!locked" in src            # folded into canSend, so Send is disabled
    assert "storage.online == null" in src   # undetermined reads as locked too


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]

def test_containers_read_emits_owner_ctrl_in_every_branch():
    """The read is account-scoped, so every container must name its owning character.
    Without it `_pick_bank` cannot tell one character's bank from another's."""
    src = _read(CONTAINERS)
    assert "'owner_ctrl', sub.owner_ctrl" in src          # surfaced in the envelope
    assert "MAX(par.player_id) AS owner_ctrl" in src      # placeables + vehicles
    assert "MAX(eps.player_controller_id) AS owner_ctrl" in src  # bank


def test_pick_bank_follows_selected_character():
    """Regression: the bank pick must not just take the first is_bank row. The shaper
    sorts banks by -item_count, so an alt (or a tombstoned re-roll) holding more items
    would win and every write against it fails not_owner. Live case 2026-07-16: acct
    3563 bank 7242 (ctrl 7651, Deleted, 30 items) outranked bank 32530 (ctrl 35487,
    Active, 0 items)."""
    src = _read(PORTAL)
    assert "def _pick_bank(" in src
    # no bank pick may bypass the helper
    assert 'next((c for c in clist if c.get("is_bank")), None)' not in src
    assert '(c for c in containers["containers"] if c.get("is_bank")), None)' not in src
    body = src.split("def _pick_bank(", 1)[1].split("\nasync def ", 1)[0]
    assert 'str(c["owner_ctrl"]) == str(owner_ctrl)' in body   # matches the character
    assert "return banks[0]" in body                            # falls back, never None-crashes


def test_v1_repair_box_translates_container_id():
    """V1 repair had the same silent no-op as V2: it forwarded the READ id to a writer
    that gates on inventory ids, so a placed box repaired 0 items and reported success."""
    src = _read(PORTAL)
    v1 = src.split("async def _dispatch_repair(", 1)[1].split("\nasync def ", 1)[0]
    assert 'box.get("inv_id") or inv_id' in v1


# --------------------------------------------------------------------------- #
# Wave 7 Tier 5: the durable both-parties mirror around the relay call, the caps
# it enforces ahead of the writer, and the audit row that used to call every
# refusal a success.
#
# The rule that shapes all of it: A LOST RESPONSE IS NOT A FAILURE. Settling a
# timeout as `failed` frees the sender's allowance for a transfer that may well
# have applied, and re-driving the same key is the only thing that resolves it.
# --------------------------------------------------------------------------- #

def _portal_func(name):
    """(ast node, source without the docstring) for one portal.py function."""
    src = _read(PORTAL)
    tree = ast.parse(src, filename=PORTAL)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            stmts = list(node.body)
            if (stmts and isinstance(stmts[0], ast.Expr)
                    and isinstance(stmts[0].value, ast.Constant)
                    and isinstance(stmts[0].value.value, str)):
                stmts = stmts[1:]
            body = "\n".join(src.splitlines()[stmts[0].lineno - 1:node.end_lineno])
            return node, body
    raise AssertionError(f"{name} is gone from portal.py")


def _settle_calls(node):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "settle"]


def test_transfer_reserves_before_the_relay():
    """Two concurrent submits otherwise both read a not-yet-breached cap and both
    pass, which is the hole the identity-keyed gift caps exist to close."""
    _node, body = _portal_func("portal_storage_v2_transfer")
    assert "check_and_reserve(" in body and "call_relay(" in body
    assert body.index("check_and_reserve(") < body.index("call_relay(")


def test_transfer_never_settles_failed_on_a_lost_response():
    node, _body = _portal_func("portal_storage_v2_transfer")
    settles = _settle_calls(node)
    assert settles, "the transfer route stopped settling the mirror"
    literals = {c.args[1].value for c in settles if isinstance(c.args[1], ast.Constant)}
    names = {c.args[1].id for c in settles if isinstance(c.args[1], ast.Name)}
    assert literals == {"deferred", "refused"}, literals
    assert names == {"status"}, names        # applied / replay come off the writer
    for handler in [h for t in ast.walk(node) if isinstance(t, ast.Try)
                    for h in t.handlers]:
        assert not _settle_calls(handler), "a relay failure settled the reservation"


def test_transfer_audit_carries_the_real_outcome():
    """The row was written in a `finally` with success defaulted to True, so a
    refusal, a 502 and a dark deferred all read as completed transfers."""
    node, _body = _portal_func("portal_storage_v2_transfer")
    calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)
             and getattr(c.func, "id", None) == "audit_log"]
    assert len(calls) == 1, calls
    kwargs = {k.arg: k.value for k in calls[0].keywords}
    assert "success" in kwargs, "audit_log left success defaulted to True"
    expr = ast.unparse(kwargs["success"])
    assert "'applied', 'replay'" in expr, expr
    assert "result.get('success')" in expr, expr
    assert "deferred" not in expr, "a dark deferred was recorded as a success"


def test_transfer_cap_refusal_is_a_429_with_retry_after():
    _node, body = _portal_func("portal_storage_v2_transfer")
    assert 'cap_reason == "daily"' in body
    assert '_v2_err("rate_limited", msg, status=429)' in body
    assert 'resp.headers["Retry-After"] = str(retry_after)' in body


def test_storage_overview_carries_the_transfer_caps_additively():
    _node, overview = _portal_func("portal_storage_v2")
    assert '"caps": {"withdraw_cap": _STORAGE_WITHDRAW_CAP,' in overview
    assert "**_transfer_caps(discord_id)" in overview
    assert '"transfer_enabled": _v2_transfer_enabled(),' in overview   # flag unchanged
    _caps_node, caps = _portal_func("_transfer_caps")
    assert "portal_transfer_events.caps(sender_identity, None)" in caps
    for key in ("transfer_daily_cap", "transfer_daily_used", "transfer_pair_cap"):
        assert f'"{key}"' in caps, key


def test_transfer_history_route_is_session_scoped_and_clamped():
    _node, body = _portal_func("portal_storage_v2_transfers")
    assert "_require_linked_session_json(request)" in body
    assert "portal_codes.own_account_ids(discord_id)" in body
    assert "max(1, min(_TRANSFER_HISTORY_MAX, limit))" in body
    assert "portal_transfer_events.history(account_ids, limit)" in body
    assert '_v2_ok({"rows": rows})' in body
    # the client never names whose rows come back
    assert "query_params" not in body and "body.get(" not in body
    _gate_node, gate = _portal_func("_require_linked_session_json")
    assert '_v2_err("unauthenticated"' in gate and "status=401" in gate


# --------------------------------------------------------------------------- #
# Broken-variant proof for the wave 7 pins above. Mutations land on a scratch
# COPY of portal.py, so the repo is never edited and there is nothing to restore
# with `git checkout --`.
# --------------------------------------------------------------------------- #

MUTATIONS = (
    ("relay called before the reservation",
     "    import portal_transfer_events\n",
     "    from relay import call_relay\n"
     '    result = await call_relay("/dune/item-transfer-op", method="POST",\n'
     "                              json_body=transfer_body, timeout=45)\n"
     "    import portal_transfer_events\n"),
    ("a relay timeout settles the reservation failed",
     '        logger.warning("portal: item-transfer failed acct=%s: %s", '
     "active_account_id, exc)\n",
     '        logger.warning("portal: item-transfer failed acct=%s: %s", '
     "active_account_id, exc)\n"
     '        portal_transfer_events.settle(idem, "failed", str(exc))\n'),
    ("audit stops passing the real outcome",
     '                                      "status": relay_status}),\n'
     '                  success=bool(relay_status in ("applied", "replay")\n'
     "                               and isinstance(result, dict)\n"
     '                               and result.get("success")))',
     '                                      "status": relay_status}))'),
    ("a dark deferred counts as a success",
     '                  success=bool(relay_status in ("applied", "replay")\n',
     '                  success=bool(relay_status in ("applied", "replay", "deferred")\n'),
    ("a writer refusal settles nothing",
     '    portal_transfer_events.settle(idem, "refused", err_token)\n',
     ""),
    ("the cap refusal loses Retry-After",
     '        resp = _v2_err("rate_limited", msg, status=429)\n'
     '        resp.headers["Retry-After"] = str(retry_after)\n',
     '        resp = _v2_err("rate_limited", msg, status=429)\n'),
    ("the overview drops the transfer caps",
     "                 **_transfer_caps(discord_id)},",
     "                 },"),
    ("history is scoped to what the client names",
     "    account_ids = portal_codes.own_account_ids(discord_id) or [active_account_id]",
     '    account_ids = [int(request.query_params.get("account_id") '
     "or active_account_id)]"),
    ("history limit is unclamped",
     "    limit = max(1, min(_TRANSFER_HISTORY_MAX, limit))",
     "    limit = int(limit)"),
)

_SCRATCH_TREE = (os.path.join("admin-backend", "routers", "portal.py"),) + _LANE_C_TREE


def _scratch_copy(tmp):
    for rel in _SCRATCH_TREE:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)


def _run_child(root):
    env = dict(os.environ, STORAGEV2_ROOT=root, STORAGEV2_CHILD="1")
    return subprocess.run([sys.executable, os.path.abspath(__file__)],
                          capture_output=True, text=True, env=env, timeout=300)


def test_every_wave7_pin_is_red_on_a_broken_variant():
    if CHILD:
        return
    tmp = tempfile.mkdtemp(prefix="storagev2-broken-",
                           dir=os.environ.get("STORAGEV2_SCRATCH") or None)
    try:
        _scratch_copy(tmp)
        clean = _run_child(tmp)
        assert clean.returncode == 0, (
            "the unmutated scratch copy already fails: "
            + " | ".join(l for l in clean.stdout.splitlines() if l.startswith("FAIL")))

        path = os.path.join(tmp, _SCRATCH_TREE[0])
        for label, find, replace in MUTATIONS:
            original = _read(path)
            assert find in original, f"[{label}] the mutation target is gone"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace(find, replace, 1))
            try:
                broken = _run_child(tmp)
                assert broken.returncode != 0, \
                    f"[{label}] the suite stayed GREEN on a broken variant"
                print(f"  broken-variant RED: {label}")
            finally:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(original)

        again = _run_child(tmp)
        assert again.returncode == 0, "the scratch copy was not restored between mutations"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Wave 10b lane C: the Storage Grid / List view.
#
# The view is an ACCOUNT preference (portal_prefs `storage_view`), so these pins are
# about WHERE things are wired, not just that the words appear somewhere: the view
# has to reach BOTH grids, the list has to drop the empty slots and grow a free-slot
# footer OUTSIDE the drop zone, and the row's two inline buttons have to carry the
# exact conditions of the menu items they mirror. Every mutation below RELOCATES the
# guarded statement rather than deleting it, so a pin that only greps the whole file
# cannot survive.
# --------------------------------------------------------------------------- #

def _svelte_markup(src):
    """Template only. A pin about rendered markup must not be satisfiable by the
    same words sitting in a header comment or a CSS rule."""
    out = re.sub(r"<script[^>]*>.*?</script>", "", src, flags=re.S)
    return re.sub(r"<style>.*?</style>", "", out, flags=re.S)


def _svelte_style(src):
    """The <style> body with its comments stripped: a pin about a CSS rule must not
    be answered by the prose sitting above it."""
    m = re.search(r"<style>(.*)</style>", src, re.S)
    return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S) if m else ""


def _squash(text):
    """Collapse whitespace, so an adjacency pin survives re-indentation but still
    fails when the guarded statement MOVES to another element."""
    return re.sub(r"\s+", " ", text)


def _branch(markup, start, end):
    i = markup.index(start) + len(start)
    return markup[i:markup.index(end, i)]


# The row branch ends at the LAYOUT else. `{:else}` alone stops at the icon
# fallback nested inside the info-popover trigger, three lines in.
_CELL_TILE_ELSE = '{:else}\n    <div class="art">'


def _grid_mount(page, marker):
    """The one <ItemGrid ... /> tag carrying `marker`."""
    i = page.index(marker)
    return page[page.rindex("<ItemGrid", 0, i):page.index("/>", i)]


def test_storage_view_toggle_is_an_identity_scoped_pref():
    page = _read(STORAGE_PAGE_L)
    markup = _svelte_markup(page)
    assert "import { getPref, setPref } from '$lib/prefs.svelte.js';" in page
    # $derived, not a one-shot read: the same key is edited from the Settings page,
    # and that change has to land here without a reload.
    assert "$derived(getPref('storage_view')" in page
    assert "setPref('storage_view', 'grid')" in page
    assert "setPref('storage_view', 'list')" in page
    # Identity scope. The storage view follows the account, not one character.
    assert "getPref('storage_view', {" not in page
    assert "setPref('storage_view', 'list', {" not in page
    # A segmented control, not two loose buttons.
    assert 'role="group"' in markup
    assert markup.count("aria-pressed={storageView ===") == 2
    assert ">Grid</button>" in markup and ">List</button>" in markup


def test_storage_view_reaches_both_item_grids():
    page = _read(STORAGE_PAGE_L)
    assert "view={storageView}" in _grid_mount(page, 'panel="bank"'), \
        "the bank grid ignores the view"
    assert "view={storageView}" in _grid_mount(page, 'panel="container"'), \
        "the container grid ignores the view"


def test_item_grid_list_view_renders_rows_and_no_empty_slots():
    src = _read(ITEM_GRID_L)
    markup = _svelte_markup(src)
    assert "view = 'grid'" in src, "the view prop must default to the shipped grid"
    branch = _branch(markup, "{:else if view === 'list'}", "{:else}")
    mount = branch[branch.index("<ItemCell"):branch.index("/>", branch.index("<ItemCell"))]
    assert 'layout="list"' in mount, "the row cell is not asked for the list layout"
    assert "draggable={draggableItems}" in mount, "rows stopped being drag sources"
    assert 'role="listitem"' in branch
    assert 'class="idx mono"' not in branch, "a list must not render empty slots"
    assert "{#each rows as it (it.item_id)}" in branch


def test_item_grid_list_view_keeps_the_wrap_as_the_drop_zone():
    markup = _svelte_markup(_read(ITEM_GRID_L))
    wrap = markup[markup.index('class="grid-wrap"'):markup.index("{#if status === 'loading'}")]
    for handler in ("ondragover={onDragOver}", "ondragleave={onDragLeave}", "ondrop={onDrop}"):
        assert handler in wrap, f"the drop zone lost {handler}"
    assert 'role="list"' in wrap


def test_item_grid_list_view_footer_sits_below_the_drop_zone():
    src = _read(ITEM_GRID_L)
    markup = _svelte_markup(src)
    # Outside the role="list" wrap: a <p> is not a listitem, and a footer that
    # scrolls away with the rows is not a footer.
    assert "</div>\n\n{#if view === 'list' && mic > 0 && status === 'ready'}" in markup
    assert "{freeSlots} of {mic} slots free" in markup
    # Derived from the capacity, never a second count typed by hand.
    assert "Math.max(0, mic - items.length)" in src


def test_item_cell_menu_closes_on_an_outside_click():
    """Owner ruling 2026-09-04: every popover menu closes on an outside click.
    The action sits on the TILE (trigger and menu both inside it), so a click
    anywhere else on the page closes the actions menu; on the menu itself it
    would close on the very click that opened it."""
    src = _read(ITEM_CELL_L)
    assert "import { clickOutside } from '$lib/actions/clickOutside.js';" in src, \
        "ItemCell must use the shared clickOutside action"
    assert ('  class="cell"\n  use:clickOutside={() => { if (open) closeAll(); }}\n'
            '  class:list={layout === \'list\'}') in src, \
        "the outside-click action must sit on the .cell tile, before its layout classes"


def test_item_cell_list_row_carries_the_two_everyday_actions():
    src = _read(ITEM_CELL_L)
    markup = _svelte_markup(src)
    assert "layout = 'grid'" in src, "the layout prop must default to the shipped tile"
    row = _squash(_branch(markup, "{#if layout === 'list'}", _CELL_TILE_ELSE))
    # Move: the same condition and the same handler as the menu item it mirrors.
    assert ('{#if storageKind} <button class="row-btn" type="button" '
            'disabled={!canMove} onclick={moveToOtherPawnStorage}> '
            'Move to {moveTargetLabel}' in row)
    # List on Exchange: the same condition, and the same sell dialog.
    assert ('{#if tradeable && allowMarketSell} <button class="row-btn" type="button" '
            "onclick={() => pick('sell')}>List on Exchange</button>" in row)
    # A row button has no menu to open first, so pick() opens as well as selects.
    assert "function pick(a) { action = a; open = true; }" in src
    # ...and everything else stays behind the "..." menu.
    assert 'aria-label="Item actions"' in row
    menu = _branch(markup, "{#if action === 'none'}", "{:else if action === 'watch'}")
    assert "Add price alert" in menu and "Send from bank" in menu


def test_item_cell_list_row_is_the_hover_surface_and_shows_the_meter():
    markup = _svelte_markup(_read(ITEM_CELL_L))
    row = _squash(_branch(markup, "{#if layout === 'list'}", _CELL_TILE_ELSE))
    assert "hoverTarget={cellEl}" in row, "the whole row must open the description card"
    # The meter belongs under the name, not off in the actions.
    assert ('<span class="name">{name}</span> {#if item?.durable} '
            "<DurabilityMeter durability={item.durability} label={name} />" in row)
    assert 'class="row-qty mono"' in row, "the quantity must be mono"


def test_item_cell_list_name_wraps_over_two_lines_without_an_ellipsis():
    style = _svelte_style(_read(ITEM_CELL_L))
    rules = [b for b in style.split("}") if ".cell.list .name" in b]
    assert rules, "the row has no name rule, so it inherits the tile's truncation"
    rule = rules[0]
    assert "white-space: normal" in rule
    assert "text-overflow: clip" in rule and "ellipsis" not in rule
    assert "line-height: 1.3" in rule and "max-height: 2.6em" in rule   # two lines


def test_grid_tile_is_untouched_by_the_list_layout():
    markup = _svelte_markup(_read(ITEM_CELL_L))
    row = _branch(markup, "{#if layout === 'list'}", _CELL_TILE_ELSE)
    assert '<span class="name" title={name}>{name}</span>' in markup   # the tile's name
    assert '{#if qty > 1}<span class="qty mono">' in markup            # the tile's badge
    assert "title={name}" not in row, \
        "the row shows the whole name, so it needs no tooltip of the truncated one"
    assert 'class="qty mono"' not in row


# --------------------------------------------------------------------------- #
# Broken-variant proof for the lane C pins. Every mutation MOVES the guarded
# statement somewhere it does nothing; none of them delete it, so a pin that only
# greps for the words stays green and is caught here.
# --------------------------------------------------------------------------- #

def test_list_rows_reflow_on_the_panel_width_not_only_the_viewport():
    """2.14.2: on a wide desktop the bank and backpack panels are ~270-330px, and
    the list row's `auto` side column (two inline actions) pushed the name column
    to zero width: every name invisible. The list is now a size container and the
    row drops its side under the name below 480px of CONTAINER width."""
    grid = _read(os.path.join(ROOT, _GRID_REL)) if "_GRID_REL" in globals() else _read(os.path.join(ROOT, "admin-backend", "portal-nextgen", "src", "lib", "components", "storage", "ItemGrid.svelte"))
    cell = _read(os.path.join(ROOT, _CELL_REL))
    assert re.search(r"\.list\s*\{[^}]*container-type:\s*inline-size", grid), \
        "the list container is no longer a size container"
    m = re.search(r"@container \(max-width: 480px\) \{(.*?)\n  \}", cell, re.S)
    assert m, "the list row has no container query for a narrow panel"
    assert "grid-template-columns: 40px minmax(0, 1fr);" in m.group(1), \
        "the narrow-panel rule does not drop the side column"
    assert ".row-side { grid-column: 2; flex-wrap: wrap; }" in m.group(1), \
        "the narrow-panel rule does not move the side under the name"


LANE_C_MUTATIONS = (
    ("the narrow-panel reflow becomes a viewport rule again", _CELL_REL,
     "  @container (max-width: 480px) {\n    .cell.list { grid-template-columns: 40px minmax(0, 1fr); }",
     "  @media (max-width: 480px) {\n    .cell.list { grid-template-columns: 40px minmax(0, 1fr); }"),
    ("the outside-click action moves off the tile onto the list art", _CELL_REL,
     '  use:clickOutside={() => { if (open) closeAll(); }}\n  class:list={layout === \'list\'}\n  class:draggable={canDrag}\n  draggable={canDrag}\n  ondragstart={onDragStart}\n  data-item-id={item?.item_id}\n>\n  {#if layout === \'list\'}\n    <div class="art">',
     '  class:list={layout === \'list\'}\n  class:draggable={canDrag}\n  draggable={canDrag}\n  ondragstart={onDragStart}\n  data-item-id={item?.item_id}\n>\n  {#if layout === \'list\'}\n    <div class="art" use:clickOutside={() => { if (open) closeAll(); }}>'),
    ("the bank grid's view prop moves to the repair panel", _PAGE_REL,
     """            view={storageView}
          />
        </CarvedSlab>
        <CarvedSlab>
          <RepairControls />""",
     """          />
        </CarvedSlab>
        <CarvedSlab>
          <RepairControls view={storageView} />"""),
    ("the container grid's view prop moves to the parts panel", _PAGE_REL,
     """              view={storageView}
            />
          {/if}
        </CarvedSlab>
        {#if sel?.is_vehicle}
          <CarvedSlab sharp={true}>
            <VehicleParts containerId={storage.selectedId} />""",
     """            />
          {/if}
        </CarvedSlab>
        {#if sel?.is_vehicle}
          <CarvedSlab sharp={true}>
            <VehicleParts containerId={storage.selectedId} view={storageView} />"""),
    ("the pref read moves out of $derived into a one-shot $state", _PAGE_REL,
     "  let storageView = $derived(getPref('storage_view') === 'list' ? 'list' : 'grid');",
     "  let storageView = $state(getPref('storage_view') === 'list' ? 'list' : 'grid');"),
    ("the free-slot footer moves inside the drop zone", _GRID_REL,
     """    </div>
  {/if}
</div>

{#if view === 'list' && mic > 0 && status === 'ready'}
  <p class="slots-free mono">{freeSlots} of {mic} slots free</p>
{/if}""",
     """    </div>
  {/if}
  {#if view === 'list' && mic > 0 && status === 'ready'}
    <p class="slots-free mono">{freeSlots} of {mic} slots free</p>
  {/if}
</div>"""),
    ("the list layout prop moves from the cell to its wrapper", _GRID_REL,
     """ role="listitem">
          <ItemCell item={it} {panel} srcContainerId={containerId}
            draggable={draggableItems} {storageKind} {allowMarketSell} layout="list" />""",
     """ role="listitem" layout="list">
          <ItemCell item={it} {panel} srcContainerId={containerId}
            draggable={draggableItems} {storageKind} {allowMarketSell} />"""),
    ("the drop handler moves from the wrap onto the list", _GRID_REL,
     """  ondragleave={onDragLeave}
  ondrop={onDrop}
>
  {#if status === 'loading'}
    <div class="grid">
      {#each Array(10) as _, i (i)}<div class="slot skeleton-slot"></div>{/each}
    </div>
  {:else if status === 'error'}
    <SealedPanel status="error" errorText="Could not read this container." />
  {:else if items.length === 0 && (mic === 0 || view === 'list')}
    <SealedPanel status="empty" action="none" art="empty-vault" emptyText="Empty." />
  {:else if view === 'list'}
    <div class="list">""",
     """  ondragleave={onDragLeave}
>
  {#if status === 'loading'}
    <div class="grid">
      {#each Array(10) as _, i (i)}<div class="slot skeleton-slot"></div>{/each}
    </div>
  {:else if status === 'error'}
    <SealedPanel status="error" errorText="Could not read this container." />
  {:else if items.length === 0 && (mic === 0 || view === 'list')}
    <SealedPanel status="empty" action="none" art="empty-vault" emptyText="Empty." />
  {:else if view === 'list'}
    <div class="list" ondrop={onDrop}>"""),
    ("the tradeable guard moves off the sell button onto the menu button", _CELL_REL,
     """      {#if tradeable && allowMarketSell}
        <button class="row-btn" type="button" onclick={() => pick('sell')}>List on Exchange</button>
      {/if}
      <button class="more mono" type="button" aria-label="Item actions" aria-expanded={open} onclick={toggle}>&hellip;</button>
    </div>""",
     """      <button class="row-btn" type="button" onclick={() => pick('sell')}>List on Exchange</button>
      {#if tradeable && allowMarketSell}
        <button class="more mono" type="button" aria-label="Item actions" aria-expanded={open} onclick={toggle}>&hellip;</button>
      {/if}
    </div>"""),
    ("the durability meter moves out from under the name", _CELL_REL,
     """    <div class="row-main">
      <span class="name">{name}</span>
      {#if item?.durable}
        <DurabilityMeter durability={item.durability} label={name} />
      {/if}
    </div>""",
     """    <div class="row-main">
      <span class="name">{name}</span>
    </div>
    <div class="row-side">
      {#if item?.durable}
        <DurabilityMeter durability={item.durability} label={name} />
      {/if}
    </div>"""),
    ("the row picks up the tile's truncating name span", _CELL_REL,
     """      <span class="name">{name}</span>
      {#if item?.durable}
        <DurabilityMeter durability={item.durability} label={name} />
      {/if}
    </div>""",
     """      <span class="name" title={name}>{name}</span>
      {#if item?.durable}
        <DurabilityMeter durability={item.durability} label={name} />
      {/if}
    </div>"""),
    ("the row's name rule moves onto the quantity", _CELL_REL,
     """  .cell.list .name {
    font-size: var(--text-sm); color: var(--text);""",
     """  .cell.list .row-qty {
    font-size: var(--text-sm); color: var(--text);"""),
)


def test_every_lane_c_pin_is_red_on_a_broken_variant():
    if CHILD:
        return
    tmp = tempfile.mkdtemp(prefix="storagev2-lanec-",
                           dir=os.environ.get("STORAGEV2_SCRATCH") or None)
    try:
        _scratch_copy(tmp)
        clean = _run_child(tmp)
        assert clean.returncode == 0, (
            "the unmutated scratch copy already fails: "
            + " | ".join(l for l in clean.stdout.splitlines() if l.startswith("FAIL")))

        for label, rel, find, replace in LANE_C_MUTATIONS:
            path = os.path.join(tmp, rel)
            original = _read(path)
            assert find in original, f"[{label}] the mutation target is gone"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace(find, replace, 1))
            try:
                broken = _run_child(tmp)
                assert broken.returncode != 0, \
                    f"[{label}] the suite stayed GREEN on a broken variant"
                print(f"  broken-variant RED: {label}")
            finally:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(original)

        again = _run_child(tmp)
        assert again.returncode == 0, "the scratch copy was not restored between mutations"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
