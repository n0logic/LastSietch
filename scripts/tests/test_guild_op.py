#!/usr/bin/env python3
"""Mocked-DB unit tests for the guild write path (scripts/dune-guild-op.sh) and
the read subcommands (scripts/dune-guilds.py).

Prod-safe: NO real DB. A fake `sudo` shim is put on PATH that emulates the
`kubectl get ns/pods`, `kubectl exec ... printenv`, and `kubectl exec -i ... psql`
calls the script makes. The psql shim captures the SQL from stdin and returns a
canned RESULT row, so we can assert on the transaction the script builds.

Covers the brief's five assertions:
  * unauthorized caller rejected      (proc RAISE -> psql non-zero -> failed)
  * dry-run does NOT COMMIT           (psql never invoked; status == dry-run)
  * replay (duplicate idem) no-op     (RESULT|..|replay -> status == replay)
  * controller resolution uses the Deleted-excluding deterministic query
  * advisory lock present in the write SQL

Run:  python3 scripts/tests/test_guild_op.py     (also pytest-compatible)
"""
import base64
import json
import os
import subprocess
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
GUILD_OP = os.path.join(SCRIPTS, "dune-guild-op.sh")
GIFT_OP = os.path.join(SCRIPTS, "dune-gift-op.sh")
GUILDS = os.path.join(SCRIPTS, "dune-guilds.py")

VALID_UUID = str(uuid.uuid4())


def _b64(obj):
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def _fake_sudo(capture_path):
    """A `sudo` shim: dispatches the kubectl calls dune-guild-op.sh makes."""
    return f"""#!/usr/bin/env bash
# invoked as: sudo kubectl <subcommand> ...
shift  # drop 'kubectl'
case "$1" in
  get)
    case "$2" in
      ns)   echo "namespace/funcom-seabass-test-ns" ;;
      pods) echo "pod/foo-db-dbdepl-sts-0" ;;
    esac
    ;;
  exec)
    if printf '%s ' "$@" | grep -q "printenv POSTGRES_PASSWORD"; then
      echo "testpw"
    else
      # psql: capture the SQL piped on stdin, then emit the canned outcome.
      cat > "{capture_path}"
      if [ -n "${{MOCK_FAIL:-}}" ]; then
        echo "ERROR:  $MOCK_FAIL" >&2
        exit 1
      fi
      echo "${{MOCK_RESULT:-RESULT|42|applied}}"
    fi
    ;;
esac
"""


def _run(args, payload_b64, env_extra=None, capture_path=None):
    """Run dune-guild-op.sh with the fake sudo on PATH. Returns (proc, sql)."""
    tmp = tempfile.mkdtemp()
    cap = capture_path or os.path.join(tmp, "captured.sql")
    sudo_path = os.path.join(tmp, "sudo")
    with open(sudo_path, "w") as fh:
        fh.write(_fake_sudo(cap))
    os.chmod(sudo_path, 0o755)

    env = dict(os.environ)
    env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)

    proc = subprocess.run(
        ["bash", GUILD_OP] + args + ["--op-b64", payload_b64],
        capture_output=True, text=True, env=env, timeout=60)
    sql = ""
    if os.path.exists(cap):
        with open(cap) as fh:
            sql = fh.read()
    return proc, sql


def _edit_payload(idem=VALID_UUID, guild_id=7, actor=1001, desc="hello"):
    return _b64({
        "op": "edit_description",
        "guild_id": guild_id,
        "actor_account_id": actor,
        "idempotency_key": idem,
        "operator": "portal:test",
        "requested_by_discord_id": "123",
        "detail": {"description": desc},
    })


# --- advisory lock + idempotency + controller resolution (dry-run SQL) --------
def test_advisory_lock_present():
    proc, _ = _run(["--dry-run"], _edit_payload())
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run", proc.stdout
    assert "guilds_get_exclusive_operation_lock" in out["sql"]


def test_idempotency_on_conflict_present():
    proc, _ = _run(["--dry-run"], _edit_payload())
    out = json.loads(proc.stdout)
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in out["sql"]


def test_controller_resolution_deleted_excluding_deterministic():
    proc, _ = _run(["--dry-run"], _edit_payload())
    out = json.loads(proc.stdout)
    sql = out["sql"]
    assert "character_state IS DISTINCT FROM 'Deleted'" in sql
    assert ("ORDER BY eps.last_avatar_activity DESC NULLS LAST, "
            "eps.player_controller_id DESC") in sql


def test_edit_description_reverifies_admin():
    proc, _ = _run(["--dry-run"], _edit_payload())
    out = json.loads(proc.stdout)
    assert "is_player_guild_admin(v_ctrl" in out["sql"]


# --- dry-run does NOT COMMIT (psql never runs) -------------------------------
def test_dry_run_does_not_execute():
    tmp = tempfile.mkdtemp()
    cap = os.path.join(tmp, "captured.sql")
    proc, sql = _run(["--dry-run"], _edit_payload(), capture_path=cap)
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run"
    # The psql shim writes the capture file ONLY when actually invoked. Dry-run
    # must not reach it.
    assert not os.path.exists(cap), "dry-run must not execute psql"
    assert sql == ""


# --- replay via duplicate idempotency_key is a no-op -------------------------
def test_replay_is_noop():
    proc, sql = _run([], _edit_payload(),
                     env_extra={"MOCK_RESULT": "RESULT|42|replay"})
    out = json.loads(proc.stdout)
    assert out["success"] is True
    assert out["status"] == "replay", proc.stdout
    assert "replay" in out["message"]
    # The replay decision is server-side: the gate temp-table + is_new guard.
    assert "is_new" in sql


def test_applied_ok():
    proc, sql = _run([], _edit_payload(),
                     env_extra={"MOCK_RESULT": "RESULT|99|applied"})
    out = json.loads(proc.stdout)
    assert out["success"] is True
    assert out["status"] == "applied"
    assert out["audit_id"] == 99


# --- unauthorized caller rejected (proc RAISE -> failed) ---------------------
def test_unauthorized_rejected():
    proc, _ = _run([], _edit_payload(),
                   env_extra={"MOCK_FAIL": "actor is not a guild admin"})
    out = json.loads(proc.stdout)
    assert out["success"] is False
    assert out["status"] == "failed", proc.stdout


# --- invite ops are dark by default ------------------------------------------
def test_invite_op_dark_when_flagged():
    # Invite ops went LIVE by default 2026-07-06; the dark gate still works when
    # GUILD_WRITES_DARK=1 is set explicitly (re-dark kill-switch).
    payload = _b64({
        "op": "reject_invite",
        "actor_account_id": 1001,
        "idempotency_key": VALID_UUID,
        "detail": {"invite_id": 5},
    })
    proc, sql = _run([], payload, env_extra={"GUILD_WRITES_DARK": "1"})
    out = json.loads(proc.stdout)
    assert out["status"] == "deferred", proc.stdout
    # Dark gate short-circuits before any DB txn.
    assert sql == ""


def test_invite_op_enabled_when_flag_off():
    payload = _b64({
        "op": "reject_invite",
        "actor_account_id": 1001,
        "idempotency_key": VALID_UUID,
        "detail": {"invite_id": 5},
    })
    proc, sql = _run(["--dry-run"], payload,
                     env_extra={"GUILD_WRITES_DARK": "0"})
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run", proc.stdout
    assert "reject_guild_invite" in out["sql"]


# --- input validation --------------------------------------------------------
def test_bad_uuid_rejected():
    payload = _edit_payload(idem="not-a-uuid")
    proc, _ = _run([], payload)
    out = json.loads(proc.stdout)
    assert out["success"] is False
    assert "idempotency_key" in out["message"]


def test_unknown_op_rejected():
    payload = _b64({"op": "delete_guild", "actor_account_id": 1,
                    "idempotency_key": VALID_UUID})
    proc, _ = _run([], payload)
    out = json.loads(proc.stdout)
    assert out["success"] is False
    assert "unknown op" in out["message"]


# --- read subcommand arg validation (no DB needed) ---------------------------
def test_guilds_pending_invites_requires_account():
    proc = subprocess.run(["python3", GUILDS, "pending-invites"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "usage" in (proc.stderr + proc.stdout).lower()


def test_guilds_unknown_subcommand():
    proc = subprocess.run(["python3", GUILDS, "bogus"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "unknown subcommand" in (proc.stderr + proc.stdout).lower()


# --- Tier 1: member management (promote/demote/remove) -----------------------
def _member_payload(op, idem=None, guild_id=7, actor=1001, target_ctrl=555,
                    new_role=None):
    body = {
        "op": op,
        "guild_id": guild_id,
        "actor_account_id": actor,
        "target_player_controller_id": target_ctrl,
        "idempotency_key": idem or str(uuid.uuid4()),
        "operator": "portal:test",
        "requested_by_discord_id": "123",
    }
    if new_role is not None:
        body["detail"] = {"new_role": new_role}
    return _b64(body)


def test_member_op_dark_when_flagged():
    # Member ops went LIVE by default 2026-07-06; GUILD_WRITES_DARK=1 re-darks
    # (kill-switch) -> promote is deferred, no DB txn.
    proc, sql = _run([], _member_payload("promote", new_role=50),
                     env_extra={"GUILD_WRITES_DARK": "1"})
    out = json.loads(proc.stdout)
    assert out["status"] == "deferred", proc.stdout
    assert sql == ""


def test_promote_builds_hierarchy_gate_not_admin_gate():
    proc, _ = _run(["--dry-run"], _member_payload("promote", new_role=50),
                   env_extra={"GUILD_WRITES_DARK": "0"})
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run", proc.stdout
    sql = out["sql"]
    # Officers-capable hierarchy gate, NOT the Leader-only is_player_guild_admin.
    assert "v_actor_role" in sql and "v_target_role" in sql
    assert "is_player_guild_admin" not in sql
    assert "promote_guild_member" in sql
    assert "cannot target self" in sql
    assert "ls.target_ctrl" in sql


def test_promote_transfer_requires_leader_guard():
    proc, _ = _run(["--dry-run"], _member_payload("promote", new_role=100),
                   env_extra={"GUILD_WRITES_DARK": "0"})
    out = json.loads(proc.stdout)
    assert "transfer leadership" in out["sql"], proc.stdout


def test_promote_clamp_rejects_bad_role():
    proc, _ = _run([], _member_payload("promote", new_role=1),
                   env_extra={"GUILD_WRITES_DARK": "0"})
    out = json.loads(proc.stdout)
    assert out["success"] is False and "new_role" in out["message"]


def test_demote_clamp_rejects_100():
    proc, _ = _run([], _member_payload("demote", new_role=100),
                   env_extra={"GUILD_WRITES_DARK": "0"})
    out = json.loads(proc.stdout)
    assert out["success"] is False and "new_role" in out["message"]


def test_member_op_requires_target_controller():
    body = {
        "op": "promote", "guild_id": 7, "actor_account_id": 1001,
        "idempotency_key": str(uuid.uuid4()), "detail": {"new_role": 50},
    }
    proc, _ = _run([], _b64(body), env_extra={"GUILD_WRITES_DARK": "0"})
    out = json.loads(proc.stdout)
    assert out["success"] is False
    assert "target_player_controller_id" in out["message"]


def test_remove_rejects_noninteger_reason():
    # remove default reason is "1" (go-live 2026-07-06). The reason gate still
    # rejects a NON-INTEGER value (bash `:-` folds empty to the default, so the
    # meaningful guard now is the integer validation), no txn.
    proc, sql = _run([], _member_payload("remove"),
                     env_extra={"GUILD_WRITES_DARK": "0", "GUILD_REMOVE_REASON": "abc"})
    out = json.loads(proc.stdout)
    assert out["success"] is False and "GUILD_REMOVE_REASON" in out["message"]
    assert sql == ""


def test_remove_builds_single_element_array():
    proc, _ = _run(["--dry-run"], _member_payload("remove"),
                   env_extra={"GUILD_WRITES_DARK": "0", "GUILD_REMOVE_REASON": "1"})
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run", proc.stdout
    sql = out["sql"]
    assert "remove_guild_members(ARRAY[v_target]::bigint[]" in sql
    assert "v_actor_role" in sql  # still hierarchy-gated


# --- send_invite timespan basis (regression, live-caught 2026-07-06) ---------
def _invite_payload(idem=None, guild_id=7, actor=1001, target=2002):
    return _b64({
        "op": "send_invite",
        "guild_id": guild_id,
        "actor_account_id": actor,
        "target_account_id": target,
        "idempotency_key": idem or str(uuid.uuid4()),
        "operator": "portal:test",
        "requested_by_discord_id": "123",
    })


def test_send_invite_uses_universe_time_basis():
    # Regression: invite_sent_timespan MUST be the game universe-time basis
    # (seconds since farm_variables.universe_time_timestamp), NOT epoch seconds.
    # A live test 2026-07-06 proved an epoch value makes add_guild_invite create
    # NO invite row (the game clock rejects a far-future timespan). The built SQL
    # must compute the basis inline in the add_guild_invite call.
    proc, _ = _run(["--dry-run"], _invite_payload(),
                   env_extra={"GUILD_WRITES_DARK": "0"})
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run", proc.stdout
    sql = out["sql"]
    assert "add_guild_invite(" in sql
    assert "universe_time_timestamp" in sql, "invite timespan must use universe-time basis"
    assert "dune.farm_variables" in sql
    # the old epoch placeholder must no longer feed add_guild_invite
    assert "current_setting('ls.timespan')" not in sql


# --- Tier 4: Solari gifting (dune-gift-op.sh) --------------------------------
def _run_gift(args, payload_b64, env_extra=None):
    tmp = tempfile.mkdtemp()
    cap = os.path.join(tmp, "captured.sql")
    sudo_path = os.path.join(tmp, "sudo")
    with open(sudo_path, "w") as fh:
        fh.write(_fake_sudo(cap))
    os.chmod(sudo_path, 0o755)
    env = dict(os.environ)
    env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", GIFT_OP] + args + ["--op-b64", payload_b64],
        capture_output=True, text=True, env=env, timeout=60)
    sql = ""
    if os.path.exists(cap):
        with open(cap) as fh:
            sql = fh.read()
    return proc, sql


def _gift_payload(sender=1001, recipient=2002, amount=500, idem=None):
    return _b64({
        "sender_account_id": sender, "recipient_account_id": recipient,
        "amount": amount, "idempotency_key": idem or str(uuid.uuid4()),
        "requested_by_discord_id": "123",
    })


def test_gift_dark_when_flagged_off():
    # Gifting was un-darked on 2026-07-07 (5de9755): dune-gift-op.sh now defaults
    # LASTSIETCH_GIFTS_ENABLED=1. This test still asserted the old dark-by-default and had
    # been failing ever since; it now pins the kill-switch path instead, which is
    # the behaviour actually worth guarding.
    proc, sql = _run_gift([], _gift_payload(), env_extra={"LASTSIETCH_GIFTS_ENABLED": "0"})
    out = json.loads(proc.stdout)
    assert out["status"] == "deferred", proc.stdout
    assert sql == ""


def test_gift_live_by_default():
    proc, _ = _run_gift(["--dry-run"], _gift_payload())
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run", proc.stdout


def test_gift_precheck_and_two_adjusts():
    proc, _ = _run_gift(["--dry-run"], _gift_payload(),
                        env_extra={"LASTSIETCH_GIFTS_ENABLED": "1"})
    out = json.loads(proc.stdout)
    assert out["status"] == "dry-run", proc.stdout
    sql = out["sql"]
    # D5 pre-check + value-conserving two adjusts + deterministic lock.
    assert "insufficient balance" in sql
    assert sql.count("adjust_player_virtual_currency_balance") == 2
    assert "-v_amount" in sql
    assert "ORDER BY player_controller_id" in sql and "FOR UPDATE" in sql
    assert "dune.ls_guild_gifts" in sql
    # Anti-RMT rate caps (sender/day + per-pair/day) present in the txn.
    assert "sender daily cap" in sql
    assert "sender->recipient daily cap" in sql


def test_gift_self_send_rejected():
    proc, _ = _run_gift([], _gift_payload(sender=1001, recipient=1001),
                        env_extra={"LASTSIETCH_GIFTS_ENABLED": "1"})
    out = json.loads(proc.stdout)
    assert out["success"] is False and "yourself" in out["message"]


def test_gift_amount_cap():
    proc, _ = _run_gift([], _gift_payload(amount=10 ** 12),
                        env_extra={"LASTSIETCH_GIFTS_ENABLED": "1"})
    out = json.loads(proc.stdout)
    assert out["success"] is False and "ceiling" in out["message"]


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
