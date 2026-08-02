#!/usr/bin/env python3
"""Regression tests for the V2 Storage backend (Tier 0-3): the drag-drop MOVE writer
action (scripts/dune-storage-write.py), the transport wiring (relay/app.py +
scripts/dune-relay-dispatch.sh), and the sibling JSON endpoints
(admin-backend/routers/portal.py).

Prod-safe: NO real DB, NO network. The writer's MOVE logic is tested by monkeypatching
its read helper (`_dq`) and by driving the kill-switch path as a subprocess with the
flag OFF (which short-circuits BEFORE any kubectl/psql). The transport + endpoint layers
are asserted by scanning the source so we catch a regression that (a) reintroduces raw
request.form() on a V2 endpoint, (b) drops the MOVE kill-switch, (c) fakes a Tier 5
success while dark, or (d) breaks the slot/volume/DD gates in the writer SQL.

Run:  python3 scripts/tests/test_storage_v2.py     (also import-safe)
"""
import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
WRITER = os.path.join(SCRIPTS, "dune-storage-write.py")
DISPATCH = os.path.join(SCRIPTS, "dune-relay-dispatch.sh")
VMAP = os.path.join(SCRIPTS, "template_volume.json")
RELAY = os.path.join(REPO, "relay", "app.py")
PORTAL = os.path.join(REPO, "admin-backend", "routers", "portal.py")
MIRROR = os.path.join(REPO, "admin-backend", "mirror.py")
CONTAINERS = os.path.join(SCRIPTS, "dune-containers.py")
# Karum Phase 0: the Tier 5 transfer writer and the shared gated take it sources.
XFER = os.path.join(SCRIPTS, "dune-item-transfer-op.sh")
TAKE_LIB = os.path.join(SCRIPTS, "lib", "dune-take-item.sh")
TRANSFER_DIALOG = os.path.join(
    REPO, "admin-backend", "portal-nextgen", "src", "lib", "components", "storage",
    "TransferDialog.svelte")
IDEM_UUID = "11111111-2222-3333-4444-555555555555"


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
                "move_disabled"):
        assert tok in m.ERROR_TOKENS, tok
    # the generic 'bank_full' must not shadow the longer dst_full_* tokens
    assert m.ERROR_TOKENS.index("dst_full_volume") < m.ERROR_TOKENS.index("bank_full")


# --------------------------------------------------------------------------- #
# Writer: kill-switch (flag OFF -> move_disabled, no DB touch)
# --------------------------------------------------------------------------- #

def test_move_disabled_when_flag_off():
    env = dict(os.environ)
    # MOVE went live 2026-07-13 (writer default flipped to ON), so the disabled
    # path must be exercised by setting the kill-switch OFF explicitly.
    env["LASTSIETCH_STORAGE_MOVE_ENABLED"] = "0"
    job = {"action": "move", "owner_ctrl": 1, "item_id": 2, "dst_inventory_id": 3}
    proc = subprocess.run(
        [sys.executable, WRITER, "--stdin-json"],
        input=json.dumps(job), capture_output=True, text=True, env=env, timeout=30)
    out = json.loads(proc.stdout)
    # short-circuits BEFORE resolve_db_pod -> clean move_disabled, never a DB error
    assert out == {"ok": False, "error": "move_disabled"}, out


# --------------------------------------------------------------------------- #
# Writer: MOVE dry-run gate detection (mocked reads, no DB)
# --------------------------------------------------------------------------- #

def _dry_run_move(m, plan, item):
    """Drive do_dry_run('move', ...) with mocked _dq; return the emitted dict."""
    def fake_dq(sql, timeout=25):
        if "'template_id', template_id" in sql:      # read_move_item
            return json.dumps(item)
        return json.dumps(plan)                       # read_move_plan (via _read_json)
    m._dq = fake_dq
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            m.do_dry_run("move", 1610, None, 0, item_id=42, dst_inv=99,
                         expected_template=None)
    except SystemExit:
        pass
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_move_dry_run_flags_offline():
    m = _load_writer()
    plan = {"online_status": "Online", "in_grace": False, "src_inv": 100,
            "src_owned": True, "dst_owned": True, "dst_mic": 20, "dst_miv": 0,
            "dst_used_slots": 1, "dst_map": "Hagga Basin", "src_map": "Hagga Basin"}
    item = {"template_id": "Water", "volume_override": None, "inventory_id": 100,
            "stack_size": 5}
    out = _dry_run_move(m, plan, item)
    assert "player_online" in out["preflight_errors"]


def test_move_dry_run_flags_dst_no_slots_and_dd():
    m = _load_writer()
    # dst_map is the RAW dune.actors.map value the writer compares against ('DeepDesert'),
    # not the portal's friendly-shaped label.
    plan = {"online_status": "Offline", "in_grace": False, "src_inv": 100,
            "src_owned": True, "dst_owned": True, "dst_mic": 0, "dst_miv": 0,
            "dst_used_slots": 0, "dst_map": "DeepDesert", "src_map": "Hagga Basin"}
    item = {"template_id": "Water", "volume_override": None, "inventory_id": 100,
            "stack_size": 5}
    out = _dry_run_move(m, plan, item)
    assert "player_online" not in out["preflight_errors"]
    assert "dst_no_slots" in out["preflight_errors"]
    assert "dst_on_deep_desert" in out["preflight_errors"]


def test_move_dry_run_flags_not_owner():
    m = _load_writer()
    plan = {"online_status": "Offline", "in_grace": False, "src_inv": 100,
            "src_owned": True, "dst_owned": False, "dst_mic": 20, "dst_miv": 0,
            "dst_used_slots": 0, "dst_map": "Hagga Basin", "src_map": "Hagga Basin"}
    item = {"template_id": "Water", "volume_override": None, "inventory_id": 100,
            "stack_size": 5}
    out = _dry_run_move(m, plan, item)
    assert "not_owner" in out["preflight_errors"]


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
    assert 'return _v2_err("move_disabled"' in v2


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
    # transfer resolves recipient server-side by char name (game names are encrypted)
    assert "_resolve_account_by_char_name(" in v2


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
    # 4 since 6fc3ffb added the storage-less-vehicle branch (Scout/Carrier/Sandbike:
    # installed parts, no cargo module). This test was pinned at 3 and had been red ever
    # since, which silently aborted the deploy preflight in ops/deploy-portal-storage.sh.
    assert len(branches) == 4, f"expected 4 UNION branches, got {len(branches)}"

    # Anchor the split to a line-initial FROM: the storage-less-vehicle branch resolves
    # its ids with a scalar subquery that carries its OWN inline "FROM dune.inventories",
    # so an unanchored split truncates that branch mid-SELECT-list.
    placeable, bank, vehicle, bare_vehicle = (
        b.split("\n    FROM dune.", 1)[0] for b in branches)
    # placeable: keyed by p.id, inv_id resolved from the inventories join -- NOT p.id
    assert "p.id" in placeable and "MAX(inv.id) AS inv_id" in placeable, \
        "placeable branch must key by p.id and resolve inv_id from the inventories join"
    assert "p.id AS inv_id" not in placeable, \
        "placeable inv_id must NOT alias p.id -- that restores the wrong-container bug"
    # bank + vehicle: container id IS the inventory id, so the two are identical
    for name, branch in (("bank", bank), ("vehicle", vehicle)):
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

    widths = {len(cols(b)) for b in (placeable, bank, vehicle, bare_vehicle)}
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
    # The frontend refetches both ends with api.containerItems(id), which speaks READ
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
