#!/usr/bin/env python3
# Regression guards for portal MULTI-CHARACTER support (2026-07-13). A player
# with a legit alt (>1 non-Deleted character on one account) must be able to see
# every character, switch which one the portal acts as, and have every per-char
# write target the SELECTED character (not the resolver's single LIMIT-1 pick).
# Also closes the bank->box MOVE `not_owner` bug (same single-char mismatch).
#
# The host resolver (dune-player-progress.py) is fastapi-free so we unit-test its
# behavior directly; the backend/relay/dispatch wiring is source-scanned (the web
# stack needs fastapi + live config, exercised by the deploy smoke instead).
import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
ADMIN = REPO / "admin-backend"
SCRIPTS = REPO / "scripts"
INJECTED = re.compile(r"player_controller_id = \d+::bigint")


def _read(p):
    return (REPO / p).read_text()


def _load_resolver():
    spec = importlib.util.spec_from_file_location(
        "dpp_test", SCRIPTS / "dune-player-progress.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fake_query(rows_for_list=None, exists_row=None):
    """Return a query_json stub that answers each of the resolver's SQL shapes.
    Records every SQL string it saw in `.seen`."""
    default_list = [
        {"controller_id": 3041, "pawn_id": 9, "char_name": "the operator",
         "online": True, "lvl": 119, "last_activity": 1700},
        {"controller_id": 5000, "pawn_id": 10, "char_name": "AltGuy",
         "online": False, "lvl": 12, "last_activity": 1600},
    ]
    rows_for_list = default_list if rows_for_list is None else rows_for_list

    def q(sql, fallback="null"):
        q.seen.append(sql)
        if "json_agg" in sql:                                   # LIST_SQL
            return list(rows_for_list)
        if "'player_controller_id', eps.player_controller_id" in sql:  # EXISTS
            if exists_row is not None:
                return exists_row(sql)
            return {"player_controller_id": 3041, "player_pawn_id": 9}
        if "TotalXPEarned" in sql and "jsonb_build_object" in sql:      # CHARACTER
            return {"xp": 100, "total_sp": 5, "unspent_sp": 1, "keystone_sp": 0}
        if "currency_id::text" in sql:                          # ECONOMY
            return {"0": "12345", "1": "6"}
        if "SolarisCoin" in sql:                                # POCKET
            return 7
        if "faction_id" in sql:                                 # FACTION
            return {"faction_id": 1, "faction_name": "Atreides", "reputation": 50}
        return {}
    q.seen = []
    return q


# --------------------------------------------------------------- host resolver

def test_default_path_injects_no_controller_filter():
    m = _load_resolver()
    q = _fake_query()
    r = m.build("1644", q, controller_id=None)
    assert r["available"] and r["player_controller_id"] == 3041
    assert not any(INJECTED.search(s) for s in q.seen), \
        "default (single-account) read must be byte-identical: no ctrl filter"


def test_scoped_path_injects_filter_into_every_read():
    m = _load_resolver()
    q = _fake_query()
    m.build("1644", q, controller_id=5000)
    scoped = [s for s in q.seen if "= 5000::bigint" in s]
    # EXISTS + CHARACTER + ECONOMY + POCKET + FACTION all get the ctrl filter.
    assert len(scoped) >= 5, "scoped read must key EVERY per-char query by controller"


def test_scoped_missing_controller_falls_back_to_default():
    m = _load_resolver()
    # EXISTS returns no row for the requested (stale) controller, a row for default.
    q = _fake_query(exists_row=lambda sql: {} if "= 9999::bigint" in sql
                    else {"player_controller_id": 3041, "player_pawn_id": 9})
    r = m.build("1644", q, controller_id=9999)
    assert r["available"] and r["player_controller_id"] == 3041, \
        "a stale/forged controller must fail SAFE to the account default pick"


def test_list_mode_sorts_recent_first_and_marks_default():
    m = _load_resolver()
    q = _fake_query()
    lst = m.list_characters("1644", q)
    chars = lst["characters"]
    assert lst["available"] and len(chars) == 2
    assert chars[0]["char_name"] == "the operator" and chars[0]["is_default"] is True
    assert chars[1]["char_name"] == "AltGuy" and chars[1]["is_default"] is False
    assert chars[0]["online"] is True and isinstance(chars[0]["lvl"], int)


def test_ctrl_filters_are_int_coerced():
    m = _load_resolver()
    assert m._ctrl_filters(None) == {"ctrl_eps": "", "ctrl_ps": "", "ctrl_ids": ""}
    # int() coercion is the SQL-injection guard on the controller id.
    f = m._ctrl_filters("5000")
    assert "5000::bigint" in f["ctrl_eps"] and "5000::bigint" in f["ctrl_ps"]


# --------------------------------------------------------------- backend wiring

def test_every_resolver_call_threads_selected_ctrl():
    src = _read("admin-backend/routers/portal.py")
    # No call may pass account_id alone: every one must thread the selected ctrl.
    bare = re.findall(r"_resolve_buyer_ctrl_and_bank\(active_account_id\)", src)
    assert not bare, "found resolver call(s) not threading _selected_ctrl"
    threaded = src.count(
        "_resolve_buyer_ctrl_and_bank(active_account_id, _selected_ctrl(request, active_account_id))")
    assert threaded >= 17, "expected all write/read resolver calls to thread the selected ctrl"


def test_resolver_signature_and_scoped_relay_path():
    src = _read("admin-backend/routers/portal.py")
    assert "async def _resolve_buyer_ctrl_and_bank(account_id: int, selected_ctrl: int = None)" in src
    assert 'path += f"?ctrl={int(selected_ctrl)}"' in src, \
        "resolver must scope the relay progress read by the selected controller"


def test_selected_ctrl_helper_is_fail_safe_cheap():
    src = _read("admin-backend/routers/portal.py")
    assert "def _selected_ctrl(request: Request, account_id: int)" in src
    assert "verify_selchar_cookie(token, account_id)" in src


def test_character_endpoints_defined():
    src = _read("admin-backend/routers/portal.py")
    assert '@router.get("/portal/characters")' in src
    assert '@router.post("/portal/select-character")' in src
    # select-character must be CSRF-gated, validate ownership, and set the cookie.
    assert 'validate_csrf(provided_csrf, csrf_for_session(session_token))' in src
    assert '"not_your_character"' in src
    assert "issue_selchar_cookie(active_account_id, ctrl)" in src


def test_selchar_cookie_helpers_bind_account():
    src = _read("admin-backend/portal_auth.py")
    assert 'SELCHAR_COOKIE = "ls_portal_selchar"' in src
    assert "def issue_selchar_cookie(account_id: int, controller_id: int)" in src
    assert "def verify_selchar_cookie(token: str, account_id: int)" in src
    # The aid binding is what stops one account selecting another's character.
    assert 'if int(payload.get("aid", -1)) != int(account_id):' in src


# --------------------------------------------------------------- relay + dispatch

def test_relay_builds_list_and_controller_commands():
    src = _read("relay/app.py")
    assert 'def dune_player_progress(account_id: str, ctrl: str = "", list: str = "")' in src
    assert 'player-progress {account_id} --list' in src
    assert 'player-progress {account_id} --controller {ctrl}' in src
    assert 'raise HTTPException(400, "ctrl must be a positive integer")' in src


def test_dispatch_allowlists_multichar_modes():
    src = _read("scripts/dune-relay-dispatch.sh")
    assert 'exec /root/dune-player-progress.py "$arg" --list' in src
    assert '"$_extra" =~ ^--controller\\ [0-9]+$' in src
    # anything not in the allowlist must still be rejected.
    assert 'echo "rejected: unexpected args"' in src


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all multichar tests passed")
