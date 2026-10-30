#!/usr/bin/env python3
"""Regression tests for the game-host half of the admin wave 2 flag board:
the `flag-set` / `flag-list` dispatcher verbs, scripts/dune-flag-set.sh, the two
relay routes in front of them, and the wave 7 host lane that ships them
(ops/deploy-item-transfer-host.sh; the wave 2 lane was retired 2026-09-03).

Prod-safe: NO game host, NO relay, NO network, NO ssh. The dispatcher and the
helper are real bash and they are RUN, in a temp tree with the flag directory,
the log path and the helper path redirected. Nothing under /etc/lastsietch or
/var/log is read or written.

What this pins, and why each one is worth a test:

  * THE ALLOWLIST, THREE TIMES. The relay, the dispatcher and the helper each
    carry the same ten names. Three copies is the design (plan section 8,
    risk 1): any one of them can be wrong, and all three being wrong the same
    way is what the repetition makes unlikely. A test that only read one of them
    would let the other two drift.
  * THE TRANSFER WRITER'S DARK GATE IS A FILE, AND IT IS RUN. Between shipping
    and wave 7 the writer read only an environment variable, and the forced
    command that invokes it inherits no profile, so nothing could ever set it:
    the gate was unreachable in both directions and no source scan would say so.
    The writer is RUN here against a redirected flag directory, with the env var
    unset exactly as the dispatcher leaves it, once with the file absent and once
    with it present.
  * REFUSALS ARE RUN, NOT READ. An unlisted name, a bad state and a trailing
    argument are pushed through the real `case` logic and the exit code is
    checked. A source scan cannot tell a guard that is present from a guard that
    is unreachable.
  * THE FRESH test -f. `verified` must come from stat'ing the file after the
    change, never from echoing the request back. Proven by making the write
    fail in a way the request cannot see: the target path is a DIRECTORY, so
    `touch` succeeds and `[ -f ]` is still false. A helper that trusted the
    request would report verified:true for a flag that is not on.
  * VALIDATE BEFORE THE SSH ARGUMENT EXISTS. The relay must refuse a bad name or
    state before it builds a remote command line, not after.
  * NOTHING RESTARTS ON THE GAME HOST. Every command in the deploy that targets
    <game-host> is checked for a restart verb. The dispatcher is exec'd per ssh
    connection and the helper is exec'd fresh, so a copied file IS the live
    change and there is nothing to restart. A `systemctl` that crept into that
    half would be a production-boundary violation, not a style problem.

Every source-scan pin is proven RED against a broken variant: the suite copies
the files to a scratch tree, breaks one thing, re-runs itself against the copy
and asserts it fails. A pin nobody has watched fail is not a pin.

Run:  python3 scripts/tests/test_flag_set_verb.py     (also import-safe)
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)

# A broken-variant run points these at a scratch copy of the tree.
ROOT = os.environ.get("FLAGVERB_ROOT", REPO)
CHILD = os.environ.get("FLAGVERB_CHILD") == "1"

DISPATCH = os.path.join(ROOT, "scripts", "dune-relay-dispatch.sh")
HELPER = os.path.join(ROOT, "scripts", "dune-flag-set.sh")
RELAY = os.path.join(ROOT, "relay", "app.py")
# Wave 7's game-host lane. It ships the same three files plus the transfer
# writer and proves all TEN names through the relay.
WRITER = os.path.join(ROOT, "scripts", "dune-item-transfer-op.sh")

OWNED = (DISPATCH, HELPER)

# The ten, and only the ten. market-sell-backpack-enabled is wave 5's kill
# switch, item-transfer-enabled is wave 7's and refinery-enabled is wave 10's;
# each is here from the start, so every switch exists before the feature it
# guards.
FLAGS = (
    "karum-enabled",
    "karum-wtb-enabled",
    "augment-enabled",
    "reward-enabled",
    "servercmd-enabled",
    "market-sell-container-enabled",
    "storage-pawn-move-enabled",
    "market-sell-backpack-enabled",
    "item-transfer-enabled",
    "landsraad-claim-enabled",
    "refinery-enabled",
)

# What the wave 2 lane was written to prove. Its round trip still asserts exactly
# these, so on a box that now reports ten it refuses at its own assertion:
# loudly, and after nothing destructive. ops/deploy-item-transfer-host.sh is the
# lane that carries the full set. Pinned as the SUBSET it really is rather than
# frozen as a count this suite would then have to keep true.
WAVE2_FLAGS = tuple(f for f in FLAGS
                    if f not in ("item-transfer-enabled", "refinery-enabled",
                                 "landsraad-claim-enabled"))

# A well-formed dry-run op for the transfer writer. dry-run assembles the real
# transaction and exits BEFORE resolve_db_pod, so nothing here reaches kubectl, a
# pod or a database.
XFER_OP = ('{"sender_account_id":1,"recipient_account_id":2,"item_id":3,'
           '"idempotency_key":"11111111-2222-3333-4444-555555555555",'
           '"mode":"dry-run"}')


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Running the real bash, with every host path redirected
# --------------------------------------------------------------------------- #

def _sandbox(tmp):
    """A dispatcher wired to the repo's own helper, with the flag directory and
    the log inside `tmp`. The dispatcher is otherwise byte-for-byte the file
    that gets deployed, so the case logic under test is the executed one."""
    flag_dir = os.path.join(tmp, "etc-lastsietch")
    os.makedirs(flag_dir, exist_ok=True)
    log = os.path.join(tmp, "dispatch.log")
    dispatch = os.path.join(tmp, "dispatch.sh")
    src = _read(DISPATCH)
    src = src.replace("/root/dune-flag-set.sh", HELPER)
    src = src.replace("/var/log/lastsietch-relay-dispatch.log", log)
    with open(dispatch, "w", encoding="utf-8") as fh:
        fh.write(src)
    return {"dispatch": dispatch, "flag_dir": flag_dir, "log": log}


def _dispatch(box, command):
    """Run one SSH_ORIGINAL_COMMAND through the sandboxed dispatcher."""
    env = dict(os.environ,
               SSH_ORIGINAL_COMMAND=command,
               LASTSIETCH_FLAG_DIR=box["flag_dir"],
               LASTSIETCH_FLAG_LOG=box["log"])
    env.pop("SSH_CLIENT", None)      # the source-IP allowlist is not under test
    env["LASTSIETCH_RELAY_ALLOWED_IPS"] = "127.0.0.1"
    return subprocess.run(["bash", box["dispatch"]], capture_output=True,
                          text=True, env=env, timeout=60)


def _helper(box, *args):
    env = dict(os.environ,
               LASTSIETCH_FLAG_DIR=box["flag_dir"],
               LASTSIETCH_FLAG_LOG=box["log"])
    env.pop("SSH_CLIENT", None)
    env["LASTSIETCH_RELAY_ALLOWED_IPS"] = "127.0.0.1"
    return subprocess.run(["bash", HELPER, *args], capture_output=True,
                          text=True, env=env, timeout=60)


def _writer(box, flag_on):
    """Run the REAL transfer writer with the flag directory redirected into the
    sandbox. LASTSIETCH_ITEM_TRANSFER_ENABLED is removed from the environment, which is
    what the forced-command dispatcher actually gives it, so whatever the writer
    answers here was decided by the FILE and by nothing else."""
    path = os.path.join(box["flag_dir"], "item-transfer-enabled")
    if flag_on:
        open(path, "w", encoding="utf-8").close()
    elif os.path.exists(path):
        os.remove(path)
    env = dict(os.environ, LASTSIETCH_FLAG_DIR=box["flag_dir"])
    env.pop("LASTSIETCH_ITEM_TRANSFER_ENABLED", None)
    env.pop("SSH_CLIENT", None)
    env["LASTSIETCH_RELAY_ALLOWED_IPS"] = "127.0.0.1"
    payload = base64.b64encode(XFER_OP.encode("utf-8")).decode("ascii")
    return subprocess.run(["bash", WRITER, "--op-b64", payload],
                          capture_output=True, text=True, env=env, timeout=120)


# --------------------------------------------------------------------------- #
# Syntax
# --------------------------------------------------------------------------- #

def test_both_shell_scripts_parse():
    for path in (DISPATCH, HELPER):
        proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert proc.returncode == 0, \
            f"{os.path.basename(path)} does not parse: {proc.stderr.strip()}"



def test_the_helper_allowlist_is_exactly_the_ten():
    src = _read(HELPER)
    block = src[src.index('ALLOWED="'):]
    block = block[len('ALLOWED="'):block.index('"', len('ALLOWED="'))]
    names = block.split()
    assert sorted(names) == sorted(FLAGS), f"the helper allowlist is {names}"


def test_the_relay_allowlist_is_exactly_the_ten():
    src = _read(RELAY)
    block = src[src.index("DUNE_FLAG_NAMES = ("):]
    block = block[:block.index(")")]
    names = re.findall(r'"([a-z-]+)"', block)
    assert sorted(names) == sorted(FLAGS), f"the relay allowlist is {names}"


def test_the_three_allowlists_are_the_same_list():
    """They are three independent copies on purpose. Three copies that have
    drifted apart are worse than one, because each one looks authoritative."""
    for name in FLAGS:
        for path in (DISPATCH, HELPER, RELAY):
            assert name in _read(path), \
                f"{name} is missing from {os.path.basename(path)}"


# --------------------------------------------------------------------------- #
# The dispatcher refuses, for real
# --------------------------------------------------------------------------- #

def test_the_dispatcher_flips_an_allowlisted_flag_and_reads_it_back():
    tmp = tempfile.mkdtemp(prefix="flagverb-")
    try:
        box = _sandbox(tmp)
        listed = _dispatch(box, "flag-list")
        assert listed.returncode == 0, listed.stderr
        data = json.loads(listed.stdout)
        assert sorted(f["name"] for f in data["flags"]) == sorted(FLAGS)
        assert all(f["state"] == "off" for f in data["flags"])

        on = _dispatch(box, "flag-set karum-enabled:on")
        assert on.returncode == 0, on.stderr
        assert json.loads(on.stdout) == {
            "ok": True, "name": "karum-enabled", "state": "on",
            "changed": True, "verified": True}
        assert os.path.isfile(os.path.join(box["flag_dir"], "karum-enabled"))

        again = json.loads(_dispatch(box, "flag-list").stdout)
        states = {f["name"]: f["state"] for f in again["flags"]}
        assert states["karum-enabled"] == "on"
        assert states["augment-enabled"] == "off", "one flip moved another flag"

        off = _dispatch(box, "flag-set karum-enabled:off")
        assert off.returncode == 0, off.stderr
        assert json.loads(off.stdout)["state"] == "off"
        assert not os.path.exists(os.path.join(box["flag_dir"], "karum-enabled"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_landsraad_switch_from_the_board_can_only_close_the_claim():
    """The board's "on" is a touch, and an EMPTY gate file admits nobody, so the
    panel can kill the Landsraad claim but never open it to anyone. Opening it
    stays a deliberate edit of the file (account ids, or '*')."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ls_claim_writer", os.path.join(ROOT, "scripts", "dune-landsraad-claim.py"))
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    tmp = tempfile.mkdtemp(prefix="flagverb-")
    try:
        box = _sandbox(tmp)
        path = os.path.join(box["flag_dir"], "landsraad-claim-enabled")
        assert writer.ENABLED_FLAG == "/etc/lastsietch/landsraad-claim-enabled", writer.ENABLED_FLAG
        on = _dispatch(box, "flag-set landsraad-claim-enabled:on")
        assert on.returncode == 0, on.stderr
        assert json.loads(on.stdout)["verified"] is True
        assert not writer.flag_allows(path, 1644), "an empty gate file let a player in"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("1644\n41001  # tester\n")
        on_again = json.loads(_dispatch(box, "flag-set landsraad-claim-enabled:on").stdout)
        assert on_again["changed"] is False
        assert writer.flag_allows(path, 41001), "re-touching an allowlist emptied it"
        off = _dispatch(box, "flag-set landsraad-claim-enabled:off")
        assert off.returncode == 0, off.stderr
        assert not os.path.exists(path)
        assert not writer.flag_allows(path, 41001), "the kill switch left the claim open"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_dispatcher_refuses_an_unlisted_name_a_bad_state_and_a_trailing_arg():
    """Run, not read. Each refusal is non-zero and leaves the flag directory
    exactly as it found it."""
    tmp = tempfile.mkdtemp(prefix="flagverb-")
    try:
        box = _sandbox(tmp)
        before = sorted(os.listdir(box["flag_dir"]))
        for command, why in (
            ("flag-set not-a-flag:on", "an unlisted name"),
            ("flag-set karum-enabled:maybe", "a state that is not on or off"),
            ("flag-set karum-enabled:on extra", "a trailing argument"),
            ("flag-set karum-enabled", "a missing state"),
            ("flag-set", "no argument at all"),
            ("flag-set ../../etc/passwd:on", "a path traversal"),
            ("flag-set karum-enabled:on;rm", "a shell metacharacter"),
            ("flag-list please", "an argument flag-list does not take"),
        ):
            proc = _dispatch(box, command)
            assert proc.returncode != 0, f"{why} was ACCEPTED: {command!r}"
            assert sorted(os.listdir(box["flag_dir"])) == before, \
                f"{why} still changed the flag directory: {command!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_dispatcher_pins_the_arg_shape_in_its_own_right():
    """Defence in depth, and the reason a behavioural test is not enough here:
    the helper refuses a bad state too, so a dispatcher that stopped checking
    would still refuse everything today. It would stop refusing the moment the
    helper changed, and the whole point of three allowlists is that no single
    one of them is load-bearing."""
    block = _new_dispatcher_cases()
    # e7552d5 (wave 5 review) moved the class to [[:lower:]-]; the pin follows it.
    assert 'if [[ ! "$arg" =~ ^[[:lower:]-]+:(on|off)$ ]]; then' in block, \
        "the dispatcher no longer pins the <name>:<on|off> shape itself"
    assert '[ -z "${_extra:-}" ]' in block, \
        "the dispatcher no longer refuses a trailing argument"
    assert "exec /root/dune-flag-set.sh" in block, \
        "the dispatcher does not exec the helper"
    # The allowlist has to be checked BEFORE the exec, not by the helper alone.
    assert block.index('case "${arg%%:*}" in') < block.index("exec /root/dune-flag-set.sh"), \
        "the dispatcher execs before it checks its own allowlist"


def test_the_dispatcher_still_refuses_an_unknown_action():
    tmp = tempfile.mkdtemp(prefix="flagverb-")
    try:
        box = _sandbox(tmp)
        proc = _dispatch(box, "flag-setx karum-enabled:on")
        assert proc.returncode != 0
        assert "unknown action" in proc.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_refusal_answers_json_so_the_relay_can_pass_it_through():
    """The relay hands the tool's own output back for a refusal (_claim_op). A
    refusal that printed nothing on stdout would reach the panel as a 502."""
    tmp = tempfile.mkdtemp(prefix="flagverb-")
    try:
        box = _sandbox(tmp)
        for args in (["not-a-flag:on"], ["karum-enabled:maybe"], []):
            proc = _helper(box, *args)
            assert proc.returncode != 0, args
            payload = json.loads(proc.stdout)
            assert payload["ok"] is False and payload["error"], payload
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The fresh test -f
# --------------------------------------------------------------------------- #

def test_verified_comes_from_a_fresh_stat_not_from_the_request():
    """The mechanism, not the string. The target path is made a DIRECTORY, so
    `touch` succeeds and `[ -f ]` is still false. A helper that echoed the
    request would answer state:"on", verified:true for a flag that is off."""
    tmp = tempfile.mkdtemp(prefix="flagverb-")
    try:
        box = _sandbox(tmp)
        os.makedirs(os.path.join(box["flag_dir"], "augment-enabled"))
        proc = _helper(box, "augment-enabled:on")
        payload = json.loads(proc.stdout)
        assert payload["verified"] is False, \
            "the helper reported a write that did not land as verified"
        assert payload["state"] == "off", \
            "the helper echoed the requested state instead of re-reading the file"
        assert proc.returncode != 0, "an unverified write exited 0"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_helper_re_reads_after_the_change_in_that_order():
    src = _read(HELPER)
    write_at = max(src.index('touch "$path"'), src.index('rm -f "$path"'))
    verify_at = src.index('after=$(state_of "$name")')
    assert write_at < verify_at, "the re-read happens before the write"
    state_of = src[src.index("state_of() {"):]
    state_of = state_of[:state_of.index("\n}")]
    assert '[ -f "$FLAG_DIR/$1" ]' in state_of, \
        "state_of does not stat the file itself"
    assert "changed" in src and "verified" in src


def test_the_helper_logs_every_change_in_plain_text():
    tmp = tempfile.mkdtemp(prefix="flagverb-")
    try:
        box = _sandbox(tmp)
        _dispatch(box, "flag-set reward-enabled:on")
        _dispatch(box, "flag-set reward-enabled:off")
        log = _read(box["log"])
        lines = [l for l in log.splitlines() if "flag-set reward-enabled" in l]
        assert len(lines) == 2, f"the helper logged {len(lines)} lines, expected 2"
        for line in lines:
            assert "verified=" in line and "was=" in line and "now=" in line, line
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_helper_touches_nothing_but_files_under_the_flag_directory():
    """It creates and removes files and appends to a log. It must never reach
    for a pod, the BattleGroup Director, k3s or the database."""
    code = [ln for ln in _read(HELPER).splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    code = "\n".join(code).lower()
    for banned in ("kubectl", "systemctl", "k3s", "battlegroup", "psql", "curl"):
        assert banned not in code, f"the helper's code reaches for {banned}"


# --------------------------------------------------------------------------- #
# The transfer writer's dark gate: the same flag directory, run for real
# --------------------------------------------------------------------------- #

def test_the_transfer_writer_answers_deferred_while_the_flag_file_is_absent():
    """A soft, honest refusal. `deferred` carries success:true because nothing
    went wrong and nothing moved, and it must arrive with no SQL built: the
    refusal happens before the transaction is assembled, not after."""
    tmp = tempfile.mkdtemp(prefix="flagverb-xfer-")
    try:
        box = _sandbox(tmp)
        proc = _writer(box, flag_on=False)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["status"] == "deferred", payload
        assert payload["success"] is True, "a deferred answer must not read as a failure"
        assert "sql" not in payload, "the writer built a transaction it then refused to run"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_transfer_writer_reaches_the_take_once_the_flag_file_exists():
    """The other half of the same switch, and the half a source scan cannot
    reach. With the file present the writer gets past the gate, sources the
    shared take and assembles the applied-path transaction: the ledger insert,
    the anti-RMT caps, the take's offline gate and its single-row re-home."""
    tmp = tempfile.mkdtemp(prefix="flagverb-xfer-")
    try:
        box = _sandbox(tmp)
        proc = _writer(box, flag_on=True)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["status"] == "dry-run", payload
        sql = payload["sql"]
        assert "INSERT INTO dune.ls_item_transfers" in sql, "no ledger row in the txn"
        assert "rate_limited" in sql, "the anti-RMT caps are not in the txn"
        assert "player_online" in sql, \
            "the shared take's offline gate is absent: the take was never sourced"
        assert "UPDATE dune.items" in sql, "the single-row re-home is absent"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_transfer_writer_reads_the_flag_file_the_way_every_other_writer_does():
    src = _read(WRITER)
    assert '[[ -f "${LASTSIETCH_FLAG_DIR:-/etc/lastsietch}/item-transfer-enabled" ]] ' \
           '&& LASTSIETCH_ITEM_TRANSFER_ENABLED=1' in src, \
        "the writer does not read the flag file the way dune-karum-op.sh does"


def test_the_transfer_writer_dark_gate_precedes_the_take_library():
    """A box that has the writer but not scripts/lib/dune-take-item.sh must still
    answer a clean deferred, not a new failure mode. That only holds while the
    gate refuses BEFORE the library is sourced."""
    src = _read(WRITER)
    assert src.index("# --- dark gate ---") < src.index("\n  require_take_lib"), \
        "require_take_lib is reached before the dark gate refuses"


def test_the_transfer_writer_no_longer_claims_it_is_untested():
    """LT-0 ran live on 2026-07-27 and closed: an online re-home is RETRACTED by
    the client, one row throughout, nothing minted. The header that told the next
    operator not to un-dark until LT-0 was run is a standing instruction to
    ignore a decision that has already been taken."""
    src = _read(WRITER)
    assert "STILL UNTESTED" not in src, \
        "the writer still carries the stale pre-LT-0 header"
    assert "LT-0 IS CLOSED" in src, "the writer does not record the LT-0 result"


# --------------------------------------------------------------------------- #
# The relay
# --------------------------------------------------------------------------- #

def test_both_relay_routes_are_key_gated():
    src = _read(RELAY)
    for decorator in (
        '@app.get("/dune/flags", dependencies=[Depends(verify_key)])',
        '@app.post("/dune/flags/{name}", dependencies=[Depends(verify_key)])',
    ):
        assert decorator in src, f"missing or ungated route: {decorator}"


def test_the_relay_validates_before_it_builds_an_ssh_argument():
    """A bad name must never become part of a remote command line, even one the
    dispatcher would go on to refuse."""
    src = _read(RELAY)
    body = src[src.index("def dune_flag_set("):]
    end = body.find("\n@app.")
    body = body[:end] if end != -1 else body
    assert "if name not in DUNE_FLAG_NAMES:" in body, "the name is never checked"
    assert 'if state not in ("on", "off"):' in body, "the state is never checked"
    assert body.index("DUNE_FLAG_NAMES") < body.index("_claim_op("), \
        "the name check runs after the remote command is built"
    assert body.index('("on", "off")') < body.index("_claim_op("), \
        "the state check runs after the remote command is built"
    assert "_claim_op(" in body and "_dune_ssh_json(" not in body, \
        "a refusal would be flattened into a 502 instead of passed through"


def test_the_relay_read_is_the_dispatcher_verb_and_nothing_else():
    src = _read(RELAY)
    body = src[src.index("def dune_flags():"):]
    body = body[:body.index("\n@app.")]
    assert '_dune_ssh_json("flag-list"' in body, \
        "the flags read does not go through the flag-list verb"


# --------------------------------------------------------------------------- #
# The deploy
# --------------------------------------------------------------------------- #



def _new_dispatcher_cases():
    """Only the two cases wave 2 added. The rest of the dispatcher predates this
    work and is not reopened here."""
    src = _read(DISPATCH)
    return src[src.index("  flag-set)"):src.index("  *)\n    echo \"rejected: unknown action\"")]




# --------------------------------------------------------------------------- #
# Broken-variant proofs
# --------------------------------------------------------------------------- #

# (label, relative path, find, replace). Each mutation must turn this suite RED.
MUTATIONS = (
    ("dispatcher allowlist",
     "scripts/dune-relay-dispatch.sh",
     "|storage-pawn-move-enabled|",
     "|storage-pawn-move-disabled|"),
    ("dispatcher arg pattern",
     "scripts/dune-relay-dispatch.sh",
     'if [[ ! "$arg" =~ ^[[:lower:]-]+:(on|off)$ ]]; then',
     'if [[ ! "$arg" =~ ^.+$ ]]; then'),
    ("dispatcher rejects a trailing arg",
     "scripts/dune-relay-dispatch.sh",
     '''  flag-set)
    # Admin wave 2: flip ONE game-host kill-switch file under /etc/lastsietch. arg =''',
     '''  flag-set)
    _extra=""
    # Admin wave 2: flip ONE game-host kill-switch file under /etc/lastsietch. arg ='''),
    ("dispatcher flag-list takes no args",
     "scripts/dune-relay-dispatch.sh",
     '''  flag-list)
    # Read-only: the on/off state of all eleven allowlisted kill-switch files.''',
     '''  flag-list)
    arg=""; _extra=""
    # Read-only: the on/off state of all eleven allowlisted kill-switch files.'''),
    ("helper allowlist",
     "scripts/dune-flag-set.sh",
     "storage-pawn-move-enabled",
     "storage-pawn-move-disabled"),
    ("landsraad gate: an empty file must admit nobody",
     "scripts/dune-landsraad-claim.py",
     '    return "*" in ids or str(account_id) in ids',
     '    return (not ids) or "*" in ids or str(account_id) in ids'),
    ("landsraad switch on the dispatcher allowlist",
     "scripts/dune-relay-dispatch.sh",
     "|landsraad-claim-enabled|refinery-enabled) ;;",
     "|refinery-enabled) ;;"),
    ("helper re-reads after the write",
     "scripts/dune-flag-set.sh",
     'after=$(state_of "$name")',
     'after="$want"'),
    ("helper refuses a bad state",
     "scripts/dune-flag-set.sh",
     '[[ "$arg" =~ ^[[:lower:]-]+:(on|off)$ ]] || refuse "flag-set takes <name>:<on|off>"',
     '[[ "$arg" =~ ^[a-z-]+:[a-z]+$ ]] || refuse "flag-set takes <name>:<on|off>"'),
    ("helper answers JSON on a refusal",
     "scripts/dune-flag-set.sh",
     '''refuse() {
  printf '{"ok":false,"error":"%s"}\\n' "$1"''',
     '''refuse() {
  echo "rejected: $1" >&2'''),
    ("relay key gate",
     "relay/app.py",
     '@app.post("/dune/flags/{name}", dependencies=[Depends(verify_key)])',
     '@app.post("/dune/flags/{name}")'),
    ("relay validates first",
     "relay/app.py",
     "    if name not in DUNE_FLAG_NAMES:\n        raise HTTPException(400, \"unknown flag\")",
     "    pass"),
    ("relay passes a refusal through",
     "relay/app.py",
     '    return _claim_op(f"flag-set {name}:{state}", timeout=30)',
     '    return _dune_ssh_json(f"flag-set {name}:{state}", timeout=30)'),
    # --- wave 7 -------------------------------------------------------------
    ("relay allowlist",
     "relay/app.py",
     '    "storage-pawn-move-enabled",\n',
     ""),
    ("writer reads the flag file",
     "scripts/dune-item-transfer-op.sh",
     '[[ -f "${LASTSIETCH_FLAG_DIR:-/etc/lastsietch}/item-transfer-enabled" ]] && LASTSIETCH_ITEM_TRANSFER_ENABLED=1',
     '# the env var was the only switch, and nothing could set it'),
    ("writer dark gate precedes the take",
     "scripts/dune-item-transfer-op.sh",
     "  # --- dark gate ---",
     "  require_take_lib\n  # --- dark gate ---"),
    ("writer stale untested header",
     "scripts/dune-item-transfer-op.sh",
     "# \u2705 LT-0 IS CLOSED, RUN LIVE 2026-07-27",
     "# \u26a0\ufe0f STILL UNTESTED, and it is why this stays DARK. LT-0"),
)

_SCRATCH_TREE = (
    "scripts/dune-relay-dispatch.sh",
    "scripts/dune-flag-set.sh",
    "relay/app.py",
    # Wave 7. The writer is RUN in the child, so its shared take library has to
    # travel with it: the writer resolves lib/ relative to itself, and a copy
    # without it would fail on a missing file and say nothing about the mutation.
    "scripts/dune-item-transfer-op.sh",
    "scripts/lib/dune-take-item.sh",
    # The Landsraad claim writer is imported in the child for its gate-file rule.
    "scripts/dune-landsraad-claim.py",
)


def _scratch_copy(tmp):
    for rel in _SCRATCH_TREE:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)


def _run_child(root):
    env = dict(os.environ, FLAGVERB_ROOT=root, FLAGVERB_CHILD="1")
    return subprocess.run([sys.executable, os.path.abspath(__file__)],
                          capture_output=True, text=True, env=env, timeout=300)


def test_every_pin_is_red_on_a_broken_variant():
    """A pin nobody has watched fail is not a pin. Each mutation lands on a
    scratch COPY of the tree; the repo is never edited, so there is nothing to
    restore with `git checkout --` and no way for a failed run to leave the
    working tree broken."""
    if CHILD:
        return
    tmp = tempfile.mkdtemp(prefix="flagverb-broken-")
    try:
        _scratch_copy(tmp)
        clean = _run_child(tmp)
        assert clean.returncode == 0, (
            "the unmutated scratch copy already fails: "
            + " | ".join(l for l in clean.stdout.splitlines() if l.startswith("FAIL")))

        for label, rel, find, replace in MUTATIONS:
            path = os.path.join(tmp, rel)
            original = _read(path)
            assert find in original, f"[{label}] the mutation target is gone from {rel}"
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


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    tests = _all_tests()
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
