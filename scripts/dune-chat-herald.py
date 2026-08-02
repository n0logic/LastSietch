#!/usr/bin/env python3
"""
Last Sietch / Last Sietch in-game chat herald ("Cielago").

Publishes a native TextChat message into the Dune mq-game chat exchanges via
`kubectl exec ... rabbitmqctl eval` (broker-internal publish, no client auth -
same path as dune-service-broadcast.py). Uses the captured chat envelope format
: the message shows from
a spoofed sender name via m_bUseSpoofedUserName + m_SpoofedUserNameFrom.

Modes:
  map      - post to a map's chat (everyone on <MapName>.<dim>), e.g. HaggaBasin.0
  whisper  - per-player DM (chat.whispers, routed by recipient FuncomId)
  faction  - faction channel (chat.faction.<id>, fanout)
  guild    - guild channel  (chat.guild.<id>, fanout)
  raw      - explicit --exchange/--routing-key escape hatch (testing other channels)

CAUTION: a real --send is live - every targeted client sees it. Default is
--dry-run (prints envelope + Erlang without invoking kubectl). --send required to
publish. Audit log: /opt/lastsietch-rmq-bridge/chat-herald.log

Cleanup note: mq-game auth_backends is the FLS cache backend, so we publish
broker-internally (no AMQP/HTTP client, no temp user needed).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone

AUDIT_LOG = "/opt/lastsietch-rmq-bridge/chat-herald.log"
NS_PREFIX = "funcom-seabass-"
MQ_POD_SUFFIX = "-mq-game-sts-0"
DEFAULT_SENDER = "Cielago"
# A placeholder FuncomId for the bot. Display name comes from the spoofed name,
# so this is metadata; override with --from-id if the client filters on it.
DEFAULT_FROM_ID = "Cielago#0001"


def fail(msg, code=1):
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(code)


def kubectl(*args, check=True, input_data=None):
    result = subprocess.run(["kubectl", *args], input=input_data,
                            capture_output=True, text=True)
    if check and result.returncode != 0:
        fail("kubectl %s failed (rc=%d): %s"
             % (" ".join(args), result.returncode, result.stderr.strip()), 3)
    return result


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
            if len(parts) >= 2 and parts[0].startswith(NS_PREFIX) \
                    and parts[1].endswith(MQ_POD_SUFFIX):
                candidates.add(parts[0])
        if not candidates:
            fail("no %s* namespace with %s pod" % (NS_PREFIX, MQ_POD_SUFFIX))
        if len(candidates) > 1:
            fail("multiple namespaces; pass --namespace: %s" % ",".join(sorted(candidates)))
        ns = candidates.pop()
    if pod_override:
        return ns, pod_override
    r = kubectl("get", "pods", "-n", ns, "--no-headers",
                "-o", "custom-columns=NAME:.metadata.name")
    for line in r.stdout.splitlines():
        if line.strip().endswith(MQ_POD_SUFFIX):
            return ns, line.strip()
    fail("no %s pod in namespace %s" % (MQ_POD_SUFFIX, ns))


def new_msg_guid():
    """32 uppercase hex chars, matching the captured m_Id format."""
    return secrets.token_hex(16).upper()


def build_inner(channel_type, sender, text, from_id, to_id, origin, spoof=True):
    """The TextChat inner object (captured format)."""
    return {
        "m_Id": new_msg_guid(),
        "m_ChannelType": channel_type,
        "m_bUseSpoofedUserName": spoof,
        "m_SpoofedUserNameFrom": {"m_TableId": "", "m_Key": "", "m_UnlocalizedName": sender if spoof else ""},
        "m_FuncomIdFrom": from_id,
        "m_UserNameTo": to_id or "",
        "m_Message": {
            "m_UnlocalizedMessage": text,
            "m_LocalizedMessage": {"m_TableId": "", "m_Key": "", "m_FormatArgs": []},
        },
        "m_Timestamp": datetime.now().strftime("%Y.%m.%d-%H.%M.%S"),
        "m_OriginLocation": {"X": origin[0], "Y": origin[1], "Z": origin[2]},
        "m_HasSeenMessage": False,
    }


def build_outer_body(inner):
    """The AMQP body published to the chat exchange: {"content": <inner-str>, "Type":"TextChat"}."""
    return json.dumps(
        {"content": json.dumps(inner, separators=(",", ":")), "Type": "TextChat"},
        separators=(",", ":"),
    )


def build_erlang_expr(body_b64, redirect_exchange, routing_key, user_id, direct, msg_id_prefix):
    """Broker-internal publish (no client auth). Two modes:
      via-intercept (default): publish to chat.intercept with a `redirect_exchange` header;
        the server interceptor re-publishes to the channel. Matches the native client path.
      direct (--direct): publish straight to the channel exchange (redirect_exchange), no
        header, bypassing the interceptor and hitting client queues directly.
    Either way carries the native AMQP props (content_type=Content, type=text_chat, user_id).

    P_basic positional: content_type, content_encoding, headers, delivery_mode, priority,
    correlation_id, reply_to, expiration, message_id, timestamp, type, user_id, app_id,
    cluster_id."""
    uid = "undefined" if not user_id else f'<<"{user_id}">>'
    if direct:
        pub_exchange = redirect_exchange
        headers = f'[{{<<"redirect_exchange">>, longstr, <<"{redirect_exchange}">>}}]'
        tag = "direct=%s" % redirect_exchange
    else:
        pub_exchange = "chat.intercept"
        headers = f'[{{<<"redirect_exchange">>, longstr, <<"{redirect_exchange}">>}}]'
        tag = "via=chat.intercept redirect=%s" % redirect_exchange
    return f"""\
Body = base64:decode(<<"{body_b64}">>),
XName = rabbit_misc:r(<<"/">>, exchange, <<"{pub_exchange}">>),
X = rabbit_exchange:lookup_or_die(XName),
MsgId = list_to_binary("{msg_id_prefix}-" ++ integer_to_list(erlang:system_time(millisecond))),
Headers = {headers},
P = {{list_to_atom("P_basic"), <<"Content">>, undefined, Headers, undefined,
     undefined, undefined, undefined, undefined, MsgId, undefined,
     <<"text_chat">>, {uid}, undefined, undefined}},
Content = rabbit_basic:build_content(P, Body),
{{ok, Msg}} = rabbit_basic:message(XName, <<"{routing_key}">>, Content),
Result = rabbit_queue_type:publish_at_most_once(X, Msg),
io:format("publish=~p {tag} routing={routing_key}~n", [Result]).
"""


def publish_via_kubectl(namespace, pod, erlang_expr):
    sh_inner = (
        "set -eu\n"
        "export PATH=/opt/rabbitmq/sbin:/opt/erlang/lib/erlang/bin:"
        "/opt/erlang/lib/erlang/erts-14.2.5.12/bin:/bin:/usr/bin:/usr/local/bin:$PATH\n"
        "cat > /tmp/dune-chat-herald.erl\n"
        "expr=$(cat /tmp/dune-chat-herald.erl)\n"
        "/opt/rabbitmq/sbin/rabbitmqctl eval \"$expr\"\n"
        "rm -f /tmp/dune-chat-herald.erl\n"
    )
    return kubectl("exec", "-i", "-n", namespace, pod, "--", "sh", "-lc", sh_inner,
                   input_data=erlang_expr, check=False)


def write_audit(record):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        print("warn: audit log write failed: %s" % e, file=sys.stderr)


def resolve_target(args):
    """Return (channel_type, redirect_exchange, routing_key, to_id).
    All publishes go to chat.intercept; redirect_exchange is the real channel (a
    `redirect_exchange` header) and routing_key matches the native key per channel."""
    if args.mode == "map":
        return "Map", "chat.map", "%s.%d" % (args.map, args.dim), ""
    if args.mode == "whisper":
        return "Whispers", "chat.whispers", args.to, args.to.split("#")[0]
    if args.mode == "faction":
        return "Faction", "chat.faction.%d" % args.faction, "", ""
    if args.mode == "guild":
        return "Guild", "chat.guild.%d" % args.guild, "", ""
    if args.mode == "raw":
        return args.channel_type, args.redirect_exchange, args.routing_key, args.to or ""
    fail("unknown mode: %s" % args.mode)


def build_parser():
    p = argparse.ArgumentParser(description="Publish a Dune in-game chat message as a herald (Cielago).")
    p.add_argument("--namespace")
    p.add_argument("--mq-pod")
    p.add_argument("--sender", default=DEFAULT_SENDER, help="Spoofed display name (default Cielago)")
    p.add_argument("--from-id", default=DEFAULT_FROM_ID, help="m_FuncomIdFrom metadata")
    p.add_argument("--user-id", default="", help="AMQP user_id property (native = sender internal id; default omit)")
    p.add_argument("--direct", action="store_true", help="Publish straight to the channel exchange, bypassing chat.intercept")
    p.add_argument("--operator", default=os.environ.get("USER", "unknown"))
    p.add_argument("--send", action="store_true", help="Actually publish (default dry-run).")

    sub = p.add_subparsers(dest="mode", required=True)

    m = sub.add_parser("map", help="Post to everyone on a map")
    m.add_argument("--map", default="HaggaBasin", help="Map name (default HaggaBasin)")
    m.add_argument("--dim", type=int, default=0, help="Dimension index (default 0)")
    m.add_argument("--message", required=True)

    w = sub.add_parser("whisper", help="Per-player DM")
    w.add_argument("--to", required=True, help="Recipient FuncomId (displayName#tag)")
    w.add_argument("--message", required=True)

    f = sub.add_parser("faction", help="Faction channel")
    f.add_argument("--faction", type=int, required=True, choices=[1, 2], help="1=Atreides 2=Harkonnen")
    f.add_argument("--message", required=True)

    g = sub.add_parser("guild", help="Guild channel")
    g.add_argument("--guild", type=int, required=True)
    g.add_argument("--message", required=True)

    r = sub.add_parser("raw", help="Explicit redirect-exchange/routing-key (testing)")
    r.add_argument("--redirect-exchange", required=True, help="e.g. chat.map, chat.whispers")
    r.add_argument("--routing-key", default="")
    r.add_argument("--channel-type", default="Map")
    r.add_argument("--to", default="")
    r.add_argument("--message", required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    started_at = datetime.now(timezone.utc).isoformat()

    ns, pod = detect_namespace_and_pod(args.namespace, args.mq_pod)
    channel_type, redirect_exchange, routing_key, to_id = resolve_target(args)

    inner = build_inner(channel_type, args.sender, args.message,
                        args.from_id, to_id, (0.0, 0.0, 0.0),
                        spoof=(args.mode != "whisper"))
    body = build_outer_body(inner)
    body_b64 = base64.b64encode(body.encode()).decode()
    erlang_expr = build_erlang_expr(body_b64, redirect_exchange, routing_key,
                                    args.user_id, args.direct, "lastsietch-chat-herald")

    print("namespace:   %s" % ns)
    print("mq pod:      %s" % pod)
    print("mode:        %s" % args.mode)
    print("sender:      %s  (spoofed)" % args.sender)
    print("publish to:  chat.intercept  (redirect_exchange=%s)" % redirect_exchange)
    print("routing key: %s" % routing_key)
    print("channel:     %s%s" % (channel_type, ("  to=%s" % to_id) if to_id else ""))
    print("user_id:     %s" % (args.user_id or "(omitted)"))
    print("message:     %s" % args.message)
    print("body JSON:")
    print(body)
    print("\nErlang expression:")
    print(erlang_expr)

    summary = {"mode": args.mode, "channel": channel_type, "redirect_exchange": redirect_exchange,
               "routing_key": routing_key, "sender": args.sender, "to": to_id,
               "user_id": args.user_id, "message": args.message}

    if not args.send:
        print("\n--dry-run (no --send). Not publishing.")
        write_audit({"ts": started_at, "operator": args.operator, "ns": ns, "pod": pod,
                     "summary": summary, "send": False, "result": "dry-run"})
        return 0

    print("\n--send PASSED. Publishing now...")
    r = publish_via_kubectl(ns, pod, erlang_expr)
    print("rc=%d stdout=%r stderr=%r" % (r.returncode, r.stdout.strip(), r.stderr.strip()))
    write_audit({"ts": started_at, "operator": args.operator, "ns": ns, "pod": pod,
                 "summary": summary, "send": True, "rc": r.returncode,
                 "stdout": r.stdout.strip(), "stderr": r.stderr.strip()})
    return 0 if r.returncode == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
