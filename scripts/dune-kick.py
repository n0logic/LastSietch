#!/usr/bin/env python3
"""
Last Sietch Dune native kick wrapper.

Publishes a KickPlayer envelope to the mq-game `heartbeats` exchange via
`kubectl exec ... rabbitmqctl eval`, using the EXACT same envelope shape,
exchange, routing key, and AMQP property record as dune-service-broadcast.py.
The only difference is the inner payload's ServerCommand verb: KickPlayer
(target by PlayerId / FuncomId) instead of ServiceBroadcast.

This is the script port of Red-Blink's admin-tools.sh:353-412 kick path,
verified parity against scripts/dune-service-broadcast.py:114-213. The kick
mechanism: Funcom validates the AuthToken on fls_backend, looks up the player
by PlayerId, and dispatches a disconnect to the partition holding the actor.

Default mode: dry-run (prints envelope + Erlang, no kubectl). --send required
to actually publish. This satisfies the "ship dry-run safe first" decision in
docs/dune-research/MODERATION-AND-DRILLDOWN-DESIGN-2026-05-29.md section 2a.

Token resolution order (same as dune-service-broadcast.py):
  1. --token-file FILE (operator-staged at /etc/lastsietch/dune-command-auth-token, 0600)
  2. DUNE_COMMAND_AUTH_TOKEN env var
  3. fallback: kubectl get secret server-gateway-secret -o jsonpath=...

Target resolution: either --fls-id <funcom_id> (direct) or --account-id <id>
(resolved via dune.accounts.funcom_id join through encrypted_player_state). The
dispatcher calls the --account-id path so the relay never sees the FuncomId.

Audit log: /opt/lastsietch-rmq-bridge/kick.log
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Hardcoded paths and limits
# --------------------------------------------------------------------------

AUDIT_LOG = "/opt/lastsietch-rmq-bridge/kick.log"
NS_PREFIX = "funcom-seabass-"
MQ_POD_SUFFIX = "-mq-game-sts-0"
DB_POD_SUFFIX = "-db-dbdepl-sts-0"
DB_PORT = 15432
DB_USER = "postgres"
DB_NAME = "dune"
SECRET_NAME = "server-gateway-secret"
SECRET_KEY = "FuncomLiveServices__ServiceAuthToken"

# Funcom IDs are displayName#tag; the displayName side accepts letters, digits,
# dot, underscore, hyphen. The full string carries the '#' separator. The
# Funcom char field accepts [A-Za-z0-9._#-]+.
FLS_ID_RE = re.compile(r"^[A-Za-z0-9._#-]+$")
ACCOUNT_ID_RE = re.compile(r"^[0-9]+$")


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

def fail(msg, code=1):
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(code)


def kubectl(*args, check=True, capture=True, input_data=None):
    """Thin wrapper around `kubectl ...`. Same contract as the service-
    broadcast version: sudo is NOT auto-injected; expect the operator's
    kubectl to be on PATH and authorized."""
    cmd = ["kubectl", *args]
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=capture,
        text=True)
    if check and result.returncode != 0:
        fail("kubectl %s failed (rc=%d): %s" % (
            " ".join(args), result.returncode, result.stderr.strip()), 3)
    return result


def detect_namespace_and_pod(ns_override=None, pod_override=None):
    """Return (namespace, mq_pod_name). Verbatim from dune-service-broadcast.py."""
    if ns_override and pod_override:
        return ns_override, pod_override

    if ns_override:
        ns = ns_override
    else:
        r = kubectl(
            "get", "pods", "-A", "--no-headers",
            "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name")
        candidates = set()
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            ns_name, pod_name = parts[0], parts[1]
            if ns_name.startswith(NS_PREFIX) and pod_name.endswith(MQ_POD_SUFFIX):
                candidates.add(ns_name)
        if not candidates:
            fail("no funcom-seabass-* namespace with %s pod" % MQ_POD_SUFFIX)
        if len(candidates) > 1:
            fail("multiple funcom-seabass-* namespaces found, pass --namespace: %s"
                 % ",".join(sorted(candidates)))
        ns = candidates.pop()

    if pod_override:
        return ns, pod_override

    r = kubectl(
        "get", "pods", "-n", ns, "--no-headers",
        "-o", "custom-columns=NAME:.metadata.name")
    for line in r.stdout.splitlines():
        name = line.strip()
        if name.endswith(MQ_POD_SUFFIX):
            return ns, name
    fail("no %s pod in namespace %s" % (MQ_POD_SUFFIX, ns))


def resolve_db_pod(namespace):
    """Find the dune DB pod in the same namespace. Mirrors dune-grant.sh's
    resolve_db_pod. Used only when --account-id resolution is requested."""
    r = kubectl(
        "get", "pods", "-n", namespace, "--no-headers",
        "-o", "custom-columns=NAME:.metadata.name")
    for line in r.stdout.splitlines():
        name = line.strip()
        if name.endswith(DB_POD_SUFFIX):
            return name
    fail("no %s pod in namespace %s" % (DB_POD_SUFFIX, namespace))


def resolve_fls_id_from_account(namespace, db_pod, account_id):
    """Look up dune.accounts.funcom_id by joining encrypted_player_state.
    Read-only SELECT, runs inside the DB pod via kubectl exec. The account_id
    has already been validated against ^[0-9]+$ by the caller; we re-coerce it
    through int() and inline it (psql -v :var interpolation does not fire through
    `-c`, so the binding reached the server literally). int() is the authoritative
    guard: a non-integer raises before any SQL is built."""
    aid = int(account_id)
    pgpass_r = kubectl(
        "exec", "-n", namespace, db_pod, "--", "printenv", "POSTGRES_PASSWORD",
        check=False)
    pgpass = (pgpass_r.stdout or "").strip()
    if not pgpass:
        fail("could not read POSTGRES_PASSWORD from db pod %s" % db_pod)
    # PlayerId for RMQ ServerCommands must be accounts."user" (the hex Funcom
    # UUID, e.g. 93700FA3235F3C5A), NOT accounts.funcom_id (the display
    # "Name#tag"). Confirmed against Red-Blink dune-admin rmq_commands.go:285
    # ("flsIDFromActorID resolves the accounts.\"user\" hex Funcom UUID ... the
    # PlayerId format expected by RMQ server commands"). Using funcom_id makes
    # the game server silently no-op the kick (publish=ok, no disconnect).
    sql = (
        "SELECT acc.\"user\" "
        "FROM dune.encrypted_player_state eps "
        "JOIN dune.accounts acc ON acc.id = eps.account_id "
        "WHERE eps.account_id = %d::bigint "
        "  AND acc.\"user\" IS NOT NULL AND acc.\"user\" <> '' "
        "LIMIT 1;" % aid
    )
    r = subprocess.run(
        ["kubectl", "exec", "-i", "-n", namespace, db_pod, "--",
         "env", "PGPASSWORD=%s" % pgpass,
         "psql", "-h", "localhost", "-p", str(DB_PORT),
         "-U", DB_USER, "-d", DB_NAME,
         "-tA", "-v", "ON_ERROR_STOP=1",
         "-c", sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        fail("psql funcom_id lookup failed (rc=%d): %s"
             % (r.returncode, r.stderr.strip()), 3)
    funcom_id = (r.stdout or "").strip()
    if not funcom_id:
        fail("no funcom_id for account_id=%s (offline never-joined account or unbound)"
             % account_id, 4)
    if not FLS_ID_RE.match(funcom_id):
        fail("resolved funcom_id failed charset check: <<redacted %d chars>>"
             % len(funcom_id), 4)
    return funcom_id


def load_token(token_file, namespace):
    """Token resolution order: --token-file, env, k8s secret. Same contract
    as dune-service-broadcast.py."""
    if token_file:
        try:
            with open(token_file) as f:
                return f.read().strip()
        except OSError as e:
            fail("could not read --token-file %s: %s" % (token_file, e))

    env_token = os.environ.get("DUNE_COMMAND_AUTH_TOKEN")
    if env_token:
        return env_token.strip()

    r = kubectl(
        "-n", namespace,
        "get", "secret", SECRET_NAME,
        "-o", "jsonpath={.data.%s}" % SECRET_KEY)
    if not r.stdout.strip():
        fail("token not found at secret %s/%s in ns %s"
             % (SECRET_NAME, SECRET_KEY, namespace))
    try:
        return base64.b64decode(r.stdout.strip()).decode().strip()
    except Exception as e:
        fail("failed to base64-decode token: %s" % e)


def build_kick_inner(fls_id):
    """Inner payload: KickPlayer verb on the fls_backend command bus. The
    PlayerId is the target FuncomId; '*' (all-online) is intentionally NOT
    reachable through this builder (the --all-online CLI escape sets it
    explicitly behind a typed-confirm prompt)."""
    return {
        "ServerCommand": "KickPlayer",
        "PlayerId": fls_id,
    }


def build_envelope_b64(inner, token):
    """Outer = {Version: 2, AuthToken, MessageContent (stringified inner)}.
    Byte-identical shape with dune-service-broadcast.py."""
    outer = {
        "Version": 2,
        "AuthToken": token,
        "MessageContent": json.dumps(inner, separators=(",", ":")),
    }
    return base64.b64encode(
        json.dumps(outer, separators=(",", ":")).encode()).decode()


def build_erlang_expr(payload_b64, message_id_prefix):
    """Verbatim P_basic record from dune-service-broadcast.py: app_id=fls_backend,
    user_id=fls, content_type=Content. Exchange=heartbeats, routing=notifications.
    These are the publish-path tuples Funcom's broker accepts as 'a server-side
    admin command'; KickPlayer reuses the same tuples as ServiceBroadcast."""
    return f"""\
Outer = base64:decode(<<"{payload_b64}">>),
XName = rabbit_misc:r(<<"/">>, exchange, <<"heartbeats">>),
X = rabbit_exchange:lookup_or_die(XName),
MsgId = list_to_binary("{message_id_prefix}-" ++ integer_to_list(erlang:system_time(millisecond))),
P = {{list_to_atom("P_basic"), <<"Content">>, undefined, [], undefined,
     undefined, undefined, undefined, undefined, MsgId, undefined,
     undefined, <<"fls">>, <<"fls_backend">>, undefined}},
Content = rabbit_basic:build_content(P, Outer),
{{ok, Msg}} = rabbit_basic:message(XName, <<"notifications">>, Content),
Result = rabbit_queue_type:publish_at_most_once(X, Msg),
io:format("publish=~p exchange=heartbeats routing=notifications app_id=fls_backend user_id=fls~n", [Result]).
"""


def publish_via_kubectl(namespace, pod, erlang_expr):
    """Pipe the Erlang expression into the mq-game pod and run rabbitmqctl eval.
    Same shell harness as dune-service-broadcast.py."""
    sh_inner = (
        "set -eu\n"
        "export PATH=/opt/rabbitmq/sbin:/opt/erlang/lib/erlang/bin:"
        "/opt/erlang/lib/erlang/erts-14.2.5.12/bin:/bin:/usr/bin:/usr/local/bin:$PATH\n"
        "cat > /tmp/dune-kick.erl\n"
        "expr=$(cat /tmp/dune-kick.erl)\n"
        "/opt/rabbitmq/sbin/rabbitmqctl eval \"$expr\"\n"
        "rm -f /tmp/dune-kick.erl\n"
    )
    return kubectl(
        "exec", "-i", "-n", namespace, pod, "--", "sh", "-lc", sh_inner,
        input_data=erlang_expr, check=False)


def write_audit(record):
    """Append a single JSON line to the audit log. Best-effort; failures here
    print to stderr but do not block the publish."""
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        print("warn: audit log write failed: %s" % e, file=sys.stderr)


def redact_token(envelope_str, token):
    """Replace the token value in a JSON string with a placeholder for printing."""
    if not token:
        return envelope_str
    return envelope_str.replace(token, "<<REDACTED token: %d chars>>" % len(token))


def redact_fls_id(fls_id):
    """Coarse redaction for printable preview: keep the first two chars + tag
    suffix so an operator can sanity-check, hide the rest."""
    if not fls_id:
        return fls_id
    if "#" in fls_id:
        name, tag = fls_id.split("#", 1)
        head = name[:2] if len(name) >= 2 else name
        return "%s***#%s" % (head, tag)
    head = fls_id[:2] if len(fls_id) >= 2 else fls_id
    return "%s***" % head


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Publish a Dune KickPlayer via mq-game (heartbeats exchange).")
    p.add_argument("--namespace", help="Override auto-detected funcom-seabass-* namespace")
    p.add_argument("--mq-pod", help="Override auto-detected mq-game pod name")
    p.add_argument("--token-file", help="Read DUNE_COMMAND_AUTH_TOKEN from this file")
    p.add_argument("--operator", default=os.environ.get("USER", "unknown"),
                   help="Operator label for the audit log")
    p.add_argument("--send", action="store_true",
                   help="Actually publish. Without this flag, dry-run only.")

    sub = p.add_subparsers(dest="mode", required=True)

    k = sub.add_parser("kick", help="Kick a single player by FuncomId or account_id")
    # Mirror the top-level operational flags on the subparser so the relay
    # dispatcher and ban-watcher (which historically placed --operator / --send
    # AFTER `kick`) parse cleanly. default=SUPPRESS means the attr is only
    # written when the subparser side is explicitly used, so a top-level
    # --operator value isn't clobbered to None by the subparser's default.
    k.add_argument("--operator", default=argparse.SUPPRESS,
                   help="Operator label for the audit log (mirror of top-level)")
    k.add_argument("--send", action="store_true", default=argparse.SUPPRESS,
                   help="Actually publish (mirror of top-level)")
    target = k.add_mutually_exclusive_group(required=True)
    target.add_argument("--fls-id", help="Target FuncomId (displayName#tag)")
    target.add_argument("--account-id",
                        help="Numeric dune account_id; resolved to funcom_id via db")
    target.add_argument("--all-online", action="store_true",
                        help="PlayerId='*' broadcast kick (Red-Blink mode); "
                             "requires typed-confirm prompt, not routable via the relay")
    return p


def confirm_all_online():
    """Typed-confirm gate for the '*' all-online kick (Red-Blink parity). Stdin
    must be a TTY; the dispatcher path never reaches this branch."""
    if not sys.stdin.isatty():
        fail("--all-online refused: stdin is not a TTY (dispatcher path?)", 2)
    prompt = ("This will disconnect EVERY player on the server. Type "
              "KICK-ALL-ONLINE to confirm: ")
    answer = input(prompt).strip()
    if answer != "KICK-ALL-ONLINE":
        fail("--all-online refused: confirmation phrase mismatch", 2)


def main(argv=None):
    args = build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc).isoformat()

    # Resolve namespace + mq pod first; token resolution uses ns for the
    # secret read, and account-id resolution uses ns for the db pod.
    ns, mq_pod = detect_namespace_and_pod(args.namespace, args.mq_pod)

    # Resolve the target FuncomId (PlayerId on the wire).
    if getattr(args, "all_online", False):
        confirm_all_online()
        fls_id = "*"
        target_label = "ALL-ONLINE"
    elif args.fls_id:
        if not FLS_ID_RE.match(args.fls_id):
            fail("invalid --fls-id (allowed charset [A-Za-z0-9._#-]+)", 2)
        fls_id = args.fls_id
        target_label = redact_fls_id(fls_id)
    else:
        if not ACCOUNT_ID_RE.match(args.account_id):
            fail("invalid --account-id (must be digits)", 2)
        db_pod = resolve_db_pod(ns)
        fls_id = resolve_fls_id_from_account(ns, db_pod, args.account_id)
        target_label = "acct=%s fls=%s" % (args.account_id, redact_fls_id(fls_id))

    token = load_token(args.token_file, ns)
    inner = build_kick_inner(fls_id)
    payload_b64 = build_envelope_b64(inner, token)
    erlang_expr = build_erlang_expr(payload_b64, "lastsietch-kick")

    # The Erlang expression embeds the base64-encoded outer envelope, and the
    # outer envelope contains AuthToken. Anyone with the printed preview can
    # base64-decode that blob and recover the live token. Build a SEPARATE
    # preview where payload_b64 is replaced with a length marker; the real
    # erlang_expr (with the live b64) is what we pass to publish_via_kubectl.
    redacted_b64 = "<<REDACTED %d bytes>>" % len(payload_b64)
    erlang_preview = build_erlang_expr(redacted_b64, "lastsietch-kick")

    # --- preview ---
    print("namespace:   %s" % ns)
    print("mq pod:      %s" % mq_pod)
    print("mode:        kick")
    print("operator:    %s" % args.operator)
    print("target:      %s" % target_label)
    print("token:       %d-char (redacted)" % len(token))
    print("inner JSON (PlayerId redacted in preview):")
    inner_preview = dict(inner)
    inner_preview["PlayerId"] = target_label
    print(json.dumps(inner_preview, indent=2))
    print()
    print("Erlang expression (payload b64 REDACTED; token never echoed):")
    print(erlang_preview)

    summary = {
        "type": "KickPlayer",
        "target": target_label,
        "all_online": fls_id == "*",
    }

    if not args.send:
        print("\n--dry-run (no --send). Exiting without publishing.")
        write_audit({
            "ts": started_at, "operator": args.operator, "ns": ns, "pod": mq_pod,
            "mode": "kick", "summary": summary, "send": False, "result": "dry-run",
        })
        return 0

    print("\n--send PASSED. Publishing now...")
    r = publish_via_kubectl(ns, mq_pod, erlang_expr)
    print("rc=%d stdout=%r stderr=%r" % (r.returncode, r.stdout.strip(), r.stderr.strip()))

    write_audit({
        "ts": started_at, "operator": args.operator, "ns": ns, "pod": mq_pod,
        "mode": "kick", "summary": summary, "send": True,
        "rc": r.returncode,
        "stdout": r.stdout.strip(),
        "stderr": r.stderr.strip(),
    })

    return 0 if r.returncode == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
