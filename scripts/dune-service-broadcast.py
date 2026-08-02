#!/usr/bin/env python3
"""
Last Sietch Dune service-broadcast wrapper.

Publishes a ServiceBroadcast envelope (Generic or ServerShutdown) to the
mq-game `heartbeats` exchange via `kubectl exec ... rabbitmqctl eval`,
mirroring adain_gt/dune-server-management-service's verified-working publish
path. The envelope shape, AMQP properties, and exchange/routing-key pair are
identical to adain's send-dune-broadcast / send-dune-shutdown-broadcast
scripts; this is the Python port recommended by the icehunter parity audit.

CAUTION: this IS a live broadcast. Every connected client receives whatever
LocalizedText / ShutdownPayload you pass. There is NO dry-run at the broker
level - the script's --dry-run prints the envelope + Erlang expression
without invoking kubectl. Default mode is --dry-run; --send is required to
actually publish.

The inner AuthToken is a BUILD-BAKED constant (BUILTIN_AUTH_TOKEN) Funcom ships
in every Dune dedicated server -- NOT the per-server FuncomLiveServices__ServiceAuthToken
JWT (that JWT is the FLS gateway auth and NotificationSystem rejects it with
"Invalid Auth Token"). Token resolution order:
  1. --token-file FILE (operator-staged file, mode 0600)
  2. DUNE_COMMAND_AUTH_TOKEN env var
  3. BUILTIN_AUTH_TOKEN (the correct default for every self-host deployment)

Audit log: /opt/lastsietch-rmq-bridge/service-broadcast.log
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Hardcoded paths and limits
# --------------------------------------------------------------------------

AUDIT_LOG = "/opt/lastsietch-rmq-bridge/service-broadcast.log"
NS_PREFIX = "funcom-seabass-"
MQ_POD_SUFFIX = "-mq-game-sts-0"
# The NotificationSystem validates the inner AuthToken against a BUILD-BAKED
# constant Funcom ships in every Dune dedicated server (same for all self-host),
# NOT the per-server FuncomLiveServices__ServiceAuthToken JWT. Using the JWT is
# rejected with "Invalid Auth Token" -- which is why broadcasts published OK at
# the broker but never actually displayed in-game. Proven live 2026-06-10.
# Override via --token-file / DUNE_COMMAND_AUTH_TOKEN if a future build rotates it.
BUILTIN_AUTH_TOKEN = "Nu6VmPWUMvdPMeB7qErr"
SHUTDOWN_TYPES = ("Restart", "Maintenance", "Update")
DEFAULT_FREQUENCY_S = 600


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
        cmd,
        input=input_data,
        capture_output=capture,
        text=True)
    if check and result.returncode != 0:
        fail("kubectl %s failed (rc=%d): %s" % (
            " ".join(args), result.returncode, result.stderr.strip()), 3)
    return result


def detect_namespace_and_pod(ns_override=None, pod_override=None):
    """Return (namespace, mq_pod_name). Mirrors adain's dune_detect_game_mq."""
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


def load_token(token_file, namespace):
    """Token resolution order: --token-file, env, k8s secret."""
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


def build_generic_inner(title, message, duration):
    return {
        "ServerCommand": "ServiceBroadcast",
        "BroadcastType": "Generic",
        "BroadcastPayload": {
            "BroadcastDuration": duration,
            "LocalizedText": [
                {"Key": "en", "Title": title, "Body": message},
                {"Key": "en-US", "Title": title, "Body": message},
            ],
        },
    }


def build_shutdown_inner(shutdown_type, target_epoch, frequency_s,
                         should_cancel=False, broadcast_duration=30):
    # Field shape mirrors the live-tested Icehunter/dune-admin shutdownPayload
    # (2026-07-13 comparison): ShouldCancel aborts a pending shutdown;
    # ShutdownDuration is the LEAD time in seconds (timestamp - now), not 0, so the
    # client renders the countdown correctly; BroadcastDuration is the on-screen
    # pulse length. DateTimestamp must be the broadcast (now) time.
    now = int(time.time())
    lead = max(0, int(target_epoch) - now)
    return {
        "ServerCommand": "ServiceBroadcast",
        "BroadcastType": "ServerShutdown",
        "BroadcastPayload": {
            "ShutdownType": shutdown_type,
            "ShouldCancel": bool(should_cancel),
            "DateTimestamp": now,
            "ShutdownDuration": lead,
            "ShutdownTimestamp": int(target_epoch),
            "BroadcastFrequency": int(frequency_s),
            "BroadcastDuration": int(broadcast_duration),
        },
    }


def build_item_grant_inner(fls_id, template_id, quantity, durability=1.0,
                           quality=0):
    """AddItemToInventory: hand an item to an ONLINE player, instantly.

    The running game server executes this through its own inventory code, so
    unlike a direct dune.items INSERT there is no position_index to collide and
    no RAM-clobber. It is the exact inverse of the welcome-pack DB path: this
    one REQUIRES the player online, that one requires them offline, so the two
    cover each other.

    Same envelope, exchange, routing key and auth as ServiceBroadcast above;
    only the inner payload differs. Field shape verified against two independent
    ports of dune-admin's publishServerCommand (Icehunter's own, and the
    Red-Blink shop system's rmq.go), which agree exactly.

    PlayerId is the Funcom hex id, dune.accounts."user" (the same fls_id the
    welcome-pack watcher already resolves). ItemName is the short template id.

    QUALITY IS UNPROVEN ON THIS BUILD. Icehunter's rmqAddItemToInventory sends no
    grade field at all and their code says the command "has no quality/grade
    field"; their dispatcher only takes the RMQ lane when quality == 0. Red-Blink's
    port hedges by sending the grade under three names at once (Quality / Grade /
    ItemQuality) "because the accepted name varies by build", and ships the error
    string "Item Grades 1-5 or Augment Grades 1-5 require the player to be offline."
    We reproduce the three-key hedge so it can be tested against OUR build rather
    than assumed. quality=0 (the default) emits the exact 5-key payload proven live
    2026-07-25 -- do not let the untested path perturb the proven one.
    """
    inner = {
        "ServerCommand": "AddItemToInventory",
        "PlayerId": fls_id,
        "ItemName": template_id,
        "Quantity": int(quantity),
        "Durability": float(durability),
    }
    if int(quality) > 0:
        inner["Quality"] = int(quality)
        inner["Grade"] = int(quality)
        inner["ItemQuality"] = int(quality)
    return inner


def build_spawn_vehicle_inner(fls_id, class_name, x, y, z, rotation=0.0,
                              template_name=None, persistent=False, faction=None):
    """SpawnVehicleAt: create a vehicle at world coordinates.

    Field shape taken from DST's Invoke-DuneRmqSpawnVehicleAt. Note DST sends
    PlayerId even though the decompiled arg list does not name one -- that is what
    scopes the spawn to a single player's world. There is no partition or map
    argument anywhere in the verb, so without PlayerId there is no way to say
    WHERE, and this is the only vehicle-touching verb in the 19.

    Persistent is sent as a float 1.0/0.0, matching DST.

    🔴 There is no inverse. The RMQ surface has no despawn verb, and the Destroy*
    cheat family takes a transaction id and an EDestroyVehicleInventoryOperation
    and runs as a DB coroutine -- it is DATA LOSS, not an actor-only despawn, and
    it is not RMQ-reachable anyway. So a persistent spawn is permanent. Default to
    non-persistent and only opt in deliberately.
    """
    inner = {
        "ServerCommand": "SpawnVehicleAt",
        "PlayerId": fls_id,
        "ClassName": class_name,
        "X": float(x),
        "Y": float(y),
        "Z": float(z),
    }
    if rotation:
        inner["Rotation"] = float(rotation)
    if template_name:
        inner["TemplateName"] = template_name
    inner["Persistent"] = 1.0 if persistent else 0.0
    if faction:
        inner["Faction"] = faction
    return inner


def build_cheat_script_inner(fls_id, script_name):
    """CheatScript: run a NAMED sequence of console commands against a player.

    Payload shape from DST's Invoke-DuneRmqCheatScript. Scripts are `[CheatScript.X]`
    INI sections whose `+Cmd=` lines each run one console command.

    🔴 THIS IS THE CHEAT-MANAGER SURFACE, and it is far sharper than the other verbs.
    The shipped scripts are NOT safe to fire casually at a live player:
      * LeaveMeAlone      runs `ServerExec sandworm.dune.Enabled 0` -- SERVER-WIDE
      * Start/StopHitchVehicleTest deliberately tank server FPS
      * PlaytestSetup(Admin) open with ResetProgression + CleanPlayerInventory
    Read the script body before sending the name. There is no dry-run on the game side.

    Why it matters: cheat scripts mix bare console commands (DestroyAllNpcs,
    JourneyCompleteTaskByName) with explicitly-prefixed `ServerExec` ones, which means
    the bare lines execute in a PLAYER context that generic ServerExec lacks -- exactly
    the limitation the 2026-07-23 probe hit. `JourneyCompleteTaskByName <Task>` is a
    force-complete of a quest task through the engine's own flow, which is the primitive
    we concluded did not exist when DragonLord's rank-20 was parked as unfixable.
    """
    return {
        "ServerCommand": "CheatScript",
        "PlayerId": fls_id,
        "ScriptName": script_name,
    }


# Any exec that is destructive, server-wide-disruptive, or a known crash hook.
# Matched case-insensitively against the whole exec string. --i-mean-it overrides.
SERVEREXEC_DENY = (
    "destroy", "resetprogression", "cleanplayerinventory", "crash",
    "forceservercrash", "quit", "exit", "shutdown", "restart",
    "sandworm.dune.enabled", "hitchvehicletest", "playtestsetup",
)


def build_serverexec_inner(exec_str):
    """ServerExec (verb 19): a single `Exec` field, NO PlayerId, NO online gate,
    dispatched to `GEngine->Exec(World, <str>, OutputDevice)`.

    This is the CVAR surface, and that is the honest limit of it. Funcom's own
    shipped `[CheatScript.LeaveMeAlone]` uses exactly this shape
    (`ServerExec sandworm.dune.Enabled 0`) to flip a cvar server-wide, which is
    what makes cvar sets here well-founded rather than speculative.

    🔴 It does NOT reach player-scoped cheat verbs. Our 2026-07-23 probe hit that
    wall: a dedicated server has no local PlayerController for `ProcessConsoleExec`,
    so bare player-context console lines (the ones cheat scripts CAN run, because
    they execute in a player context) are unavailable through generic ServerExec.
    Do not expect `ScheduleMTXEvent` or similar cheat-manager UFunctions to work.

    Reading a cvar back is free and self-verifying: pass the cvar name with NO
    value and the current value is written to the OutputDevice, i.e. the game-pod
    log. So the safe sequence is read, set, read.

    Output lands in the game pod stdout, not in this script's output. Watch it with
      kubectl logs -n <ns> <game-pod> --since=2m | grep -iE 'ServerCommand|<cvar>'
    """
    return {
        "ServerCommand": "ServerExec",
        "Exec": exec_str,
    }


def build_envelope_b64(inner, token):
    """Outer = {Version: 2, AuthToken, MessageContent (stringified inner)}.
    The whole outer is base64-encoded for the Erlang base64:decode call."""
    outer = {
        "Version": 2,
        "AuthToken": token,
        "MessageContent": json.dumps(inner, separators=(",", ":")),
    }
    return base64.b64encode(
        json.dumps(outer, separators=(",", ":")).encode()).decode()


def build_erlang_expr(payload_b64, message_id_prefix, message_id=None):
    """Verbatim port of adain's Erlang expression. Properties record matches
    P_basic positional layout: content_type, content_encoding, headers,
    delivery_mode, priority, correlation_id, reply_to, expiration, message_id,
    timestamp, type, user_id, app_id, cluster_id.

    message_id: pass an explicit MsgId to make the published id knowable to the
    caller. The default computes it inside Erlang, which means the publisher
    never learns the value it sent -- fine for broadcasts, useless as a
    correlation key for a delivery trace."""
    if message_id:
        msg_id_expr = 'MsgId = <<"%s">>,' % message_id
    else:
        msg_id_expr = ('MsgId = list_to_binary("%s-" ++ '
                       'integer_to_list(erlang:system_time(millisecond))),'
                       % message_id_prefix)
    return f"""\
Outer = base64:decode(<<"{payload_b64}">>),
XName = rabbit_misc:r(<<"/">>, exchange, <<"heartbeats">>),
X = rabbit_exchange:lookup_or_die(XName),
{msg_id_expr}
P = {{list_to_atom("P_basic"), <<"Content">>, undefined, [], undefined,
     undefined, undefined, undefined, undefined, MsgId, undefined,
     undefined, <<"fls">>, <<"fls_backend">>, undefined}},
Content = rabbit_basic:build_content(P, Outer),
{{ok, Msg}} = rabbit_basic:message(XName, <<"notifications">>, Content),
Result = rabbit_queue_type:publish_at_most_once(X, Msg),
io:format("publish=~p exchange=heartbeats routing=notifications app_id=fls_backend user_id=fls~n", [Result]).
"""


def publish_via_kubectl(namespace, pod, erlang_expr):
    """Pipe the Erlang expression into the mq-game pod and run rabbitmqctl eval."""
    sh_inner = (
        "set -eu\n"
        "export PATH=/opt/rabbitmq/sbin:/opt/erlang/lib/erlang/bin:"
        "/opt/erlang/lib/erlang/erts-14.2.5.12/bin:/bin:/usr/bin:/usr/local/bin:$PATH\n"
        "cat > /tmp/dune-service-broadcast.erl\n"
        "expr=$(cat /tmp/dune-service-broadcast.erl)\n"
        "/opt/rabbitmq/sbin/rabbitmqctl eval \"$expr\"\n"
        "rm -f /tmp/dune-service-broadcast.erl\n"
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Publish a Dune ServiceBroadcast via mq-game (heartbeats exchange).")
    p.add_argument("--namespace", help="Override auto-detected funcom-seabass-* namespace")
    p.add_argument("--mq-pod", help="Override auto-detected mq-game pod name")
    p.add_argument("--token-file", help="Read DUNE_COMMAND_AUTH_TOKEN from this file")
    p.add_argument("--operator", default=os.environ.get("USER", "unknown"),
                   help="Operator label for the audit log")
    p.add_argument("--send", action="store_true",
                   help="Actually publish. Without this flag, dry-run only.")

    sub = p.add_subparsers(dest="mode", required=True)

    g = sub.add_parser("generic", help="Generic title/body/duration broadcast")
    g.add_argument("--title", required=True)
    g.add_argument("--message", required=True)
    g.add_argument("--duration", type=int, default=30)

    s = sub.add_parser("shutdown", help="ServerShutdown countdown broadcast")
    s.add_argument("--type", dest="shutdown_type", required=True,
                   choices=list(SHUTDOWN_TYPES))
    s.add_argument("--target-ts", type=int,
                   help="Unix epoch (seconds) when shutdown will execute "
                        "(required unless --cancel)")
    s.add_argument("--frequency", type=int, default=DEFAULT_FREQUENCY_S,
                   help="Repeat-warning frequency in seconds (default 600)")
    s.add_argument("--broadcast-duration", type=int, default=30,
                   help="On-screen pulse length in seconds (default 30)")
    s.add_argument("--cancel", action="store_true",
                   help="Abort a pending shutdown (ShouldCancel=true)")

    i = sub.add_parser("item-grant",
                       help="AddItemToInventory: give an item to an ONLINE player instantly")
    i.add_argument("--fls-id", required=True,
                   help="Funcom hex id, dune.accounts.\"user\" (NOT the account id)")
    i.add_argument("--template", required=True,
                   help="Short game template id, e.g. T6BladePart")
    i.add_argument("--quantity", type=int, default=1)
    i.add_argument("--durability", type=float, default=1.0)
    i.add_argument("--quality", type=int, default=0,
                   help="Grade 0-5. UNPROVEN on this build: >0 adds the "
                        "Quality/Grade/ItemQuality hedge. 0 = the proven payload.")
    i.add_argument("--message-id",
                   help="Explicit AMQP MsgId, so a caller can use it as a "
                        "delivery-trace correlation_id. [A-Za-z0-9._-] only.")

    v = sub.add_parser("spawn-vehicle",
                       help="SpawnVehicleAt: spawn a vehicle at world coordinates")
    v.add_argument("--fls-id", required=True,
                   help="Funcom hex id. The RE'd arg list omits PlayerId, but DST "
                        "sends it and that is what scopes the spawn to one player's "
                        "world -- without it there is no partition selector at all.")
    v.add_argument("--class-name", required=True,
                   help="Full BP path, e.g. /Game/Dune/Systems/Vehicles/Blueprints/"
                        "FlyingVehicles/BP_LightOrnithopter_Choam.BP_LightOrnithopter_Choam_C")
    v.add_argument("--x", type=float, required=True)
    v.add_argument("--y", type=float, required=True)
    v.add_argument("--z", type=float, required=True)
    v.add_argument("--rotation", type=float, default=0.0)
    v.add_argument("--template-name")
    v.add_argument("--faction")
    v.add_argument("--persistent", action="store_true",
                   help="Default OFF, and leave it off unless you mean it. There is "
                        "NO safe removal verb: the RMQ surface has no despawn, and "
                        "the Destroy* cheat family is persistence-affecting DATA LOSS "
                        "rather than an actor-only despawn. A non-persistent spawn "
                        "cleans itself up; a persistent one is yours forever.")

    c = sub.add_parser("cheat-script",
                       help="CheatScript: run a named [CheatScript.X] command sequence "
                            "against a player. READ THE SCRIPT BODY FIRST.")
    c.add_argument("--fls-id", required=True)
    c.add_argument("--script-name", required=True,
                   help="Section suffix, e.g. LeaveMeAlone. Shipped scripts include "
                        "server-wide and destructive effects; see build_cheat_script_inner.")

    e = sub.add_parser("serverexec",
                       help="ServerExec: run one console command server-side. CVAR "
                            "surface only; player-scoped cheat verbs do NOT work here.")
    e.add_argument("--exec", dest="exec_str", required=True,
                   help="The console command. A bare cvar name READS it (value goes to "
                        "the game-pod log); 'name value' SETS it server-wide.")
    e.add_argument("--i-mean-it", action="store_true",
                   help="Override the destructive-exec denylist. Needed for anything "
                        "matching Destroy/Reset/Crash/Shutdown/etc.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc).isoformat()

    # Resolve namespace + pod first; token resolution uses ns for the secret read.
    ns, pod = detect_namespace_and_pod(args.namespace, args.mq_pod)
    token = load_token(args.token_file, ns)

    if args.mode == "generic":
        inner = build_generic_inner(args.title, args.message, args.duration)
        msg_id_prefix = "lastsietch-service-broadcast"
        summary = {
            "type": "Generic",
            "title": args.title,
            "duration": args.duration,
        }
    elif args.mode == "shutdown":
        if not args.cancel and args.target_ts is None:
            fail("shutdown requires --target-ts (or use --cancel to abort a pending one)")
        target = args.target_ts if args.target_ts is not None else int(time.time())
        inner = build_shutdown_inner(args.shutdown_type, target, args.frequency,
                                     should_cancel=args.cancel,
                                     broadcast_duration=args.broadcast_duration)
        msg_id_prefix = "lastsietch-server-shutdown-cancel" if args.cancel else "lastsietch-server-shutdown"
        summary = {
            "type": "ServerShutdown",
            "shutdown_type": args.shutdown_type,
            "target_ts": target,
            "frequency": args.frequency,
            "broadcast_duration": args.broadcast_duration,
            "should_cancel": args.cancel,
        }
    elif args.mode == "item-grant":
        if args.quantity < 1:
            fail("item-grant requires --quantity >= 1")
        if not (0 <= args.quality <= 5):
            fail("item-grant --quality must be 0-5")
        inner = build_item_grant_inner(args.fls_id, args.template,
                                       args.quantity, args.durability,
                                       args.quality)
        msg_id_prefix = "lastsietch-item-grant"
        summary = {
            "type": "AddItemToInventory",
            "fls_id": args.fls_id,
            "template": args.template,
            "quantity": args.quantity,
            "durability": args.durability,
            "quality": args.quality,
        }
    elif args.mode == "spawn-vehicle":
        inner = build_spawn_vehicle_inner(
            args.fls_id, args.class_name, args.x, args.y, args.z,
            rotation=args.rotation, template_name=args.template_name,
            persistent=args.persistent, faction=args.faction)
        msg_id_prefix = "lastsietch-spawn-vehicle"
        summary = {
            "type": "SpawnVehicleAt",
            "fls_id": args.fls_id,
            "class_name": args.class_name,
            "at": [args.x, args.y, args.z],
            "rotation": args.rotation,
            "template_name": args.template_name,
            "persistent": args.persistent,
            "faction": args.faction,
        }
    elif args.mode == "cheat-script":
        inner = build_cheat_script_inner(args.fls_id, args.script_name)
        msg_id_prefix = "lastsietch-cheat-script"
        summary = {
            "type": "CheatScript",
            "fls_id": args.fls_id,
            "script_name": args.script_name,
        }
    elif args.mode == "serverexec":
        exec_str = args.exec_str.strip()
        if not exec_str:
            fail("serverexec requires a non-empty --exec")
        if "\n" in exec_str or '"' in exec_str:
            fail("serverexec --exec must not contain newlines or double quotes "
                 "(it is JSON-embedded then interpolated into an Erlang binary)")
        lowered = exec_str.lower()
        hit = next((w for w in SERVEREXEC_DENY if w in lowered), None)
        if hit and not args.i_mean_it:
            fail("serverexec refused: --exec matches denylisted token %r. "
                 "This verb is server-wide with no undo. Re-run with --i-mean-it "
                 "only if that is genuinely what you want." % hit)
        inner = build_serverexec_inner(exec_str)
        msg_id_prefix = "lastsietch-server-exec"
        summary = {
            "type": "ServerExec",
            "exec": exec_str,
            "denylist_overridden": bool(hit and args.i_mean_it),
        }
    else:
        fail("unknown mode: %s" % args.mode)

    explicit_msg_id = getattr(args, "message_id", None)
    if explicit_msg_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", explicit_msg_id):
        fail("--message-id must match [A-Za-z0-9._-]{1,120} "
             "(it is interpolated into an Erlang binary literal)")
    payload_b64 = build_envelope_b64(inner, token)
    erlang_expr = build_erlang_expr(payload_b64, msg_id_prefix, explicit_msg_id)
    if explicit_msg_id:
        summary["message_id"] = explicit_msg_id

    # --- preview ---
    print("namespace:   %s" % ns)
    print("mq pod:      %s" % pod)
    print("mode:        %s" % args.mode)
    print("operator:    %s" % args.operator)
    print("token:       %d-char token (redacted)" % len(token))
    print("inner JSON:")
    print(json.dumps(inner, indent=2))
    print()
    print("Erlang expression (payload b64'd; token NOT echoed):")
    print(erlang_expr)

    if not args.send:
        print("\n--dry-run (no --send). Exiting without publishing.")
        write_audit({
            "ts": started_at, "operator": args.operator, "ns": ns, "pod": pod,
            "mode": args.mode, "summary": summary, "send": False, "result": "dry-run",
        })
        return 0

    print("\n--send PASSED. Publishing now...")
    r = publish_via_kubectl(ns, pod, erlang_expr)
    print("rc=%d stdout=%r stderr=%r" % (r.returncode, r.stdout.strip(), r.stderr.strip()))

    write_audit({
        "ts": started_at, "operator": args.operator, "ns": ns, "pod": pod,
        "mode": args.mode, "summary": summary, "send": True,
        "rc": r.returncode,
        "stdout": r.stdout.strip(),
        "stderr": r.stderr.strip(),
    })

    return 0 if r.returncode == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
