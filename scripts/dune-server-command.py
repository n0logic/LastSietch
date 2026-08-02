#!/usr/bin/env python3
"""
Last Sietch Dune native server-command wrapper.

Publishes a native server command (AddItemToInventory, TeleportTo, AwardXP,
KickPlayer, etc.) to the mq-game `heartbeats` exchange via
`kubectl exec ... rabbitmqctl eval`, as the trusted `fls` system user.

This is the player-targeted sibling of dune-service-broadcast.py: same
envelope, same AMQP P_basic tuple (user_id="fls"), same publish path -- only
the inner ServerCommand + args differ. The inner shapes are byte-for-byte the
shapes Icehunter's dune-admin rmq_commands.go sends to the SAME native
UDuneServerCommandSubsystem (verified 2026-06-10: subsystem +
UDuneServerCommandsCheatManager + all 15 verbs present in our shipping binary;
ServiceBroadcast already drives live through this exact path).

AuthToken is PER-SERVER (our 352-byte JWT in server-gateway-secret), NOT
Icehunter's hardcoded constant -- read from the secret like the broadcast tool.

PlayerId is the hex Funcom UUID from dune.accounts."user" (NOT controller_id).
Resolve it with --resolve <FuncomId|name> or pass --player-id directly.

SAFETY SPINE:
  * Default mode is dry-run; --send is required to publish.
  * Kill-switch: --send is refused unless LASTSIETCH_SERVERCMD_ENABLED=1.
  * Player verbs act only on ONLINE players (RMQ has no effect offline); the
    online check runs unless --no-online-check is passed.
  * Destructive verbs (CleanPlayerInventory, ResetProgression, CheatScript)
    additionally require --i-understand-destructive and a --reason.
  * Every invocation appends a JSON line to the audit log.

Audit log: /opt/lastsietch-rmq-bridge/server-command.log
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Hardcoded paths and limits
# --------------------------------------------------------------------------

AUDIT_LOG = "/opt/lastsietch-rmq-bridge/server-command.log"
NS_PREFIX = "funcom-seabass-"
MQ_POD_SUFFIX = "-mq-game-sts-0"
DQ = "/root/dq.sh"  # DB query helper (kubectl exec into postgres pod)

# The NotificationSystem validates the inner envelope AuthToken against a
# BUILD-BAKED constant that Funcom ships in EVERY Dune dedicated server -- it is
# the same for all self-host deployments, NOT per-server. Proven live on build
# 1988751 (2026-06-10) and used identically by six community projects (Icehunter
# dune-admin, adain send-dune-broadcast, the docker selfhost BUILTIN_COMMAND_AUTH_TOKEN,
# dune-dashboard, dune-dedicated-server-manager, Simple-Dune tool).
# NOTE: this is NOT the per-server FuncomLiveServices__ServiceAuthToken JWT -- that
# JWT is the FLS *gateway* auth and is REJECTED here ("Invalid Auth Token").
# Override via --token-file / DUNE_COMMAND_AUTH_TOKEN only if a future build rotates it.
BUILTIN_AUTH_TOKEN = "Nu6VmPWUMvdPMeB7qErr"

DESTRUCTIVE = {"CleanPlayerInventory", "ResetProgression", "CheatScript"}
KILL_SWITCH_ENV = "LASTSIETCH_SERVERCMD_ENABLED"


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------

def fail(msg, code=1):
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(code)


def kubectl(*args, check=True, capture=True, input_data=None):
    """Thin wrapper around `kubectl ...`. Sudo is NOT auto-injected -
    expect the operator's `kubectl` to be on PATH and authorized."""
    cmd = ["kubectl", *args]
    result = subprocess.run(
        cmd, input=input_data, capture_output=capture, text=True)
    if check and result.returncode != 0:
        fail("kubectl %s failed (rc=%d): %s" % (
            " ".join(args), result.returncode, result.stderr.strip()), 3)
    return result


def dq(sql):
    """Run a single SQL statement via dq.sh, returning stripped stdout.
    Returns None if dq.sh is unavailable or the query errors."""
    if not os.path.exists(DQ):
        return None
    try:
        r = subprocess.run([DQ, "-tAc", sql], capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# --------------------------------------------------------------------------
# Namespace / pod / token  (identical resolution to dune-service-broadcast.py)
# --------------------------------------------------------------------------

def detect_namespace_and_pod(ns_override=None, pod_override=None):
    if ns_override and pod_override:
        return ns_override, pod_override
    if ns_override:
        ns = ns_override
    else:
        r = kubectl("get", "pods", "-A", "--no-headers",
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
            fail("multiple funcom-seabass-* namespaces, pass --namespace: %s"
                 % ",".join(sorted(candidates)))
        ns = candidates.pop()
    if pod_override:
        return ns, pod_override
    r = kubectl("get", "pods", "-n", ns, "--no-headers",
                "-o", "custom-columns=NAME:.metadata.name")
    for line in r.stdout.splitlines():
        name = line.strip()
        if name.endswith(MQ_POD_SUFFIX):
            return ns, name
    fail("no %s pod in namespace %s" % (MQ_POD_SUFFIX, ns))


def load_token(token_file, namespace):
    """Resolution order: --token-file, DUNE_COMMAND_AUTH_TOKEN env, then the
    build-baked BUILTIN_AUTH_TOKEN (the correct default for every self-host)."""
    if token_file:
        try:
            with open(token_file) as f:
                return f.read().strip()
        except OSError as e:
            fail("could not read --token-file %s: %s" % (token_file, e))
    env_token = os.environ.get("DUNE_COMMAND_AUTH_TOKEN")
    if env_token:
        return env_token.strip()
    return BUILTIN_AUTH_TOKEN


# --------------------------------------------------------------------------
# Player ID resolution + online check
# --------------------------------------------------------------------------

def resolve_hex_id(token_in):
    """Resolve a hex Funcom UUID (accounts.\"user\") from a FuncomId display
    name (e.g. 'Icehunter#55381') or character name. Returns hex or None."""
    safe = token_in.replace("'", "''")
    # Try encrypted_funcom_id display-name match first, then character name.
    sql = (
        "SELECT ac.\"user\" FROM dune.accounts ac "
        "JOIN dune.encrypted_accounts e ON e.id = ac.id "
        "WHERE convert_from(e.encrypted_funcom_id,'UTF8') = '%s' LIMIT 1" % safe)
    hex_id = dq(sql)
    return hex_id or None


def online_status(hex_id):
    """Return the player_state.online_status text for a hex FLS id, or None."""
    safe = hex_id.replace("'", "''")
    sql = ("SELECT COALESCE(ps.online_status::text,'Offline') "
           "FROM dune.accounts ac "
           "JOIN dune.player_state ps ON ps.account_id = ac.id "
           "WHERE ac.\"user\" = '%s' LIMIT 1" % safe)
    return dq(sql)


# --------------------------------------------------------------------------
# Inner command builders  (exact field shapes from Icehunter rmq_commands.go)
# --------------------------------------------------------------------------

def b_add_item(pid, a):
    return {"ServerCommand": "AddItemToInventory", "PlayerId": pid,
            "ItemName": a.item, "Quantity": a.qty, "Durability": a.durability}


def b_kick(pid, a):
    return {"ServerCommand": "KickPlayer", "PlayerId": pid}


def b_award_xp(pid, a):
    return {"ServerCommand": "AwardXP", "PlayerId": pid,
            "Category": a.category, "Experience": a.experience}


def b_set_sp(pid, a):
    return {"ServerCommand": "SkillsSetUnspentSkillPoints", "PlayerId": pid,
            "SkillPoints": a.skill_points}


def b_set_module(pid, a):
    return {"ServerCommand": "SkillsSetModuleLevel", "PlayerId": pid,
            "Module": a.module, "Level": a.level}


def b_teleport(pid, a):
    return {"ServerCommand": "TeleportToExact" if a.exact else "TeleportTo",
            "PlayerId": pid, "X": a.x, "Y": a.y, "Z": a.z}


def b_water(pid, a):
    return {"ServerCommand": "UpdateAllWaterFillables", "PlayerId": pid,
            "WaterAmount": a.water_amount}


def b_spawn_vehicle(pid, a):
    f = {"ServerCommand": "SpawnVehicleAt", "PlayerId": pid,
         "ClassName": a.class_name, "X": a.x, "Y": a.y, "Z": a.z}
    if a.rotation:
        f["Rotation"] = a.rotation
    if a.template:
        f["TemplateName"] = a.template
    f["Persistent"] = 1.0 if a.persistent else 0.0
    if a.faction:
        f["Faction"] = a.faction
    return f


def b_cheat_script(pid, a):
    return {"ServerCommand": "CheatScript", "PlayerId": pid,
            "ScriptName": a.script_name}


def b_clean_inv(pid, a):
    return {"ServerCommand": "CleanPlayerInventory", "PlayerId": pid}


def b_reset_prog(pid, a):
    return {"ServerCommand": "ResetProgression", "PlayerId": pid}


# --------------------------------------------------------------------------
# Envelope / Erlang / publish  (verbatim from dune-service-broadcast.py)
# --------------------------------------------------------------------------

def build_envelope_b64(inner, token):
    outer = {"Version": 2, "AuthToken": token,
             "MessageContent": json.dumps(inner, separators=(",", ":"))}
    return base64.b64encode(
        json.dumps(outer, separators=(",", ":")).encode()).decode()


def build_erlang_expr(payload_b64, message_id_prefix):
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
    sh_inner = (
        "set -eu\n"
        "export PATH=/opt/rabbitmq/sbin:/opt/erlang/lib/erlang/bin:"
        "/opt/erlang/lib/erlang/erts-14.2.5.12/bin:/bin:/usr/bin:/usr/local/bin:$PATH\n"
        "cat > /tmp/dune-server-command.erl\n"
        "expr=$(cat /tmp/dune-server-command.erl)\n"
        "/opt/rabbitmq/sbin/rabbitmqctl eval \"$expr\"\n"
        "rm -f /tmp/dune-server-command.erl\n"
    )
    return kubectl("exec", "-i", "-n", namespace, pod, "--", "sh", "-lc",
                   sh_inner, input_data=erlang_expr, check=False)


def write_audit(record):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        print("warn: audit log write failed: %s" % e, file=sys.stderr)


def redact_token(s, token):
    if not token:
        return s
    return s.replace(token, "<<REDACTED token: %d chars>>" % len(token))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

# verb name -> (builder, needs_player_id, [extra-arg adders])
def add_player_arg(sp):
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--player-id", help="Target hex Funcom UUID (accounts.\"user\")")
    g.add_argument("--resolve", help="Resolve target by FuncomId, e.g. 'Cielago#1234'")


def build_parser():
    p = argparse.ArgumentParser(
        description="Publish a native Dune server command via mq-game (heartbeats exchange).")
    p.add_argument("--namespace", help="Override auto-detected funcom-seabass-* namespace")
    p.add_argument("--mq-pod", help="Override auto-detected mq-game pod name")
    p.add_argument("--token-file", help="Read the auth token from this file")
    p.add_argument("--operator", default=os.environ.get("USER", "unknown"),
                   help="Operator label for the audit log")
    p.add_argument("--reason", default="", help="Reason string (required for destructive verbs)")
    p.add_argument("--no-online-check", action="store_true",
                   help="Skip the online-status gate (RMQ has no effect on offline players)")
    p.add_argument("--i-understand-destructive", action="store_true",
                   help="Required acknowledgement for destructive verbs")
    p.add_argument("--send", action="store_true",
                   help="Actually publish. Without this flag, dry-run only.")
    sub = p.add_subparsers(dest="verb", required=True)

    s = sub.add_parser("give-item"); add_player_arg(s)
    s.add_argument("--item", required=True, help="ItemName (FName / template id)")
    s.add_argument("--qty", type=int, default=1)
    s.add_argument("--durability", type=float, default=1.0)
    s.set_defaults(builder=b_add_item)

    s = sub.add_parser("kick"); add_player_arg(s)
    s.set_defaults(builder=b_kick)

    s = sub.add_parser("award-xp"); add_player_arg(s)
    s.add_argument("--category", required=True)
    s.add_argument("--experience", type=int, required=True)
    s.set_defaults(builder=b_award_xp)

    s = sub.add_parser("set-skill-points"); add_player_arg(s)
    s.add_argument("--skill-points", type=int, required=True)
    s.set_defaults(builder=b_set_sp)

    s = sub.add_parser("set-module-level"); add_player_arg(s)
    s.add_argument("--module", required=True)
    s.add_argument("--level", type=int, required=True)
    s.set_defaults(builder=b_set_module)

    s = sub.add_parser("teleport"); add_player_arg(s)
    s.add_argument("--x", type=float, required=True)
    s.add_argument("--y", type=float, required=True)
    s.add_argument("--z", type=float, required=True)
    s.add_argument("--exact", action="store_true", help="TeleportToExact (no safe-snap)")
    s.set_defaults(builder=b_teleport)

    s = sub.add_parser("refill-water"); add_player_arg(s)
    s.add_argument("--water-amount", type=int, default=100)
    s.set_defaults(builder=b_water)

    s = sub.add_parser("spawn-vehicle"); add_player_arg(s)
    s.add_argument("--class-name", required=True)
    s.add_argument("--x", type=float, required=True)
    s.add_argument("--y", type=float, required=True)
    s.add_argument("--z", type=float, required=True)
    s.add_argument("--rotation", type=float, default=0.0)
    s.add_argument("--template", default="")
    s.add_argument("--persistent", action="store_true")
    s.add_argument("--faction", default="")
    s.set_defaults(builder=b_spawn_vehicle)

    s = sub.add_parser("cheat-script"); add_player_arg(s)
    s.add_argument("--script-name", required=True)
    s.set_defaults(builder=b_cheat_script)

    s = sub.add_parser("clean-inventory"); add_player_arg(s)
    s.set_defaults(builder=b_clean_inv)

    s = sub.add_parser("reset-progression"); add_player_arg(s)
    s.set_defaults(builder=b_reset_prog)

    return p


def server_command_name(inner):
    return inner.get("ServerCommand", "?")


def main(argv=None):
    args = build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc).isoformat()

    ns, pod = detect_namespace_and_pod(args.namespace, args.mq_pod)

    # --- resolve target player id ---
    pid = args.player_id
    resolved_from = None
    if not pid and getattr(args, "resolve", None):
        pid = resolve_hex_id(args.resolve)
        resolved_from = args.resolve
        if not pid:
            fail("could not resolve hex FLS id for %r (check dq.sh / spelling)" % args.resolve)

    inner = args.builder(pid, args)
    cmd_name = server_command_name(inner)

    # --- destructive gate ---
    if cmd_name in DESTRUCTIVE:
        if not args.i_understand_destructive:
            fail("%s is destructive; pass --i-understand-destructive" % cmd_name)
        if not args.reason.strip():
            fail("%s is destructive; a --reason is required" % cmd_name)

    # --- online gate (player-targeted verbs only) ---
    # Status is always computed for the preview; the offline-abort only hard-stops
    # a real --send (a dry-run should preview against any target, online or not).
    online = None
    if pid and not args.no_online_check:
        online = online_status(pid)
        if online is None:
            print("warn: could not determine online status (dq.sh unavailable?); "
                  "use --no-online-check to bypass", file=sys.stderr)
        elif online == "Offline" and args.send:
            fail("target %s is Offline; RMQ commands have no effect offline "
                 "(use a DB proc, or --no-online-check to force-send)" % pid)

    token = load_token(args.token_file, ns)
    payload_b64 = build_envelope_b64(inner, token)
    erlang_expr = build_erlang_expr(payload_b64, "lastsietch-server-command")

    # --- preview ---
    inner_str = json.dumps(inner, indent=2)
    print("namespace:   %s" % ns)
    print("mq pod:      %s" % pod)
    print("verb:        %s" % cmd_name)
    if resolved_from:
        print("resolved:    %s -> %s" % (resolved_from, pid))
    print("online:      %s" % (online if online is not None else "(unchecked)"))
    print("inner:       %s" % inner_str.replace("\n", "\n             "))

    record = {
        "ts": started_at, "operator": args.operator, "verb": cmd_name,
        "player_id": pid, "resolved_from": resolved_from, "online": online,
        "reason": args.reason, "namespace": ns, "sent": False,
        "inner": inner,
    }

    if not args.send:
        print("\n[dry-run] not published. Re-run with --send to publish.")
        record["mode"] = "dry-run"
        write_audit(record)
        return 0

    # --- kill switch (only gates real sends) ---
    if os.environ.get(KILL_SWITCH_ENV) != "1":
        record["mode"] = "blocked-killswitch"
        write_audit(record)
        fail("%s != 1; live sends are disabled. Export %s=1 to enable."
             % (KILL_SWITCH_ENV, KILL_SWITCH_ENV))

    result = publish_via_kubectl(ns, pod, erlang_expr)
    ok = result.returncode == 0 and "publish=" in (result.stdout or "")
    record["mode"] = "send"
    record["sent"] = ok
    record["broker_stdout"] = (result.stdout or "").strip()
    record["broker_stderr"] = (result.stderr or "").strip()
    write_audit(record)

    print("\nbroker stdout: %s" % (result.stdout or "").strip())
    if result.stderr.strip():
        print("broker stderr: %s" % result.stderr.strip(), file=sys.stderr)
    if not ok:
        fail("publish did not confirm (rc=%d)" % result.returncode, 4)
    print("published OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
