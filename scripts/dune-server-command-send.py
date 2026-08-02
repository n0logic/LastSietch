#!/usr/bin/env python3
"""
lastsietch-dune server-command bridge: reads one JSON job from stdin and invokes
dune-server-command.py with a safe argv list (no shell interpolation of any
field). The relay/V2 sibling of dune-broadcast-send.py.

Invoked by the relay forced-command dispatcher (action `server-command`, payload
on stdin) so admin-driven native server commands (give-item / award-xp /
teleport / refill-water) never cross a shell. dune-server-command.py owns the
publish path, the builtin AuthToken, the online-status gate, and the audit log.

SAFETY:
  * Only a fixed allow-list of NON-destructive verbs is reachable here. Destructive
    verbs (clean-inventory / reset-progression / cheat-script) and spawn-vehicle are
    intentionally NOT routable via the relay -- they stay CLI-only.
  * Real sends still require the box-side master switch (LASTSIETCH_SERVERCMD_ENABLED=1),
    which the dispatcher sets only when /etc/lastsietch/servercmd-enabled exists. dry-run
    jobs omit --send (preview only).

stdin JSON:
  { "verb": "give-item"|"award-xp"|"teleport"|"refill-water",
    "resolve": "Name#tag",          # OR "player_id": "<hexFLS>" (one required)
    "player_id": "<hexFLS>",
    "mode": "apply"|"dry-run",       # dry-run omits --send (preview only)
    "operator": "username",          # audit label (required)
    "reason": "...",                 # optional audit reason
    "args": { ... verb-specific ... } }

  give-item args:    { "item": "<template_id>", "qty": 1, "durability": 1.0 }
  award-xp args:     { "category": "<cat>", "experience": <int> }
  teleport args:     { "x": <f>, "y": <f>, "z": <f>, "exact": false }
  refill-water args: { "water_amount": <int> }

stdout JSON: { success, mode, returncode, verb, detail }
Always exits 0 and reports status in JSON so the relay gets a clean response.
"""
import json
import subprocess
import sys

CMD = "/opt/lastsietch-rmq-bridge/dune-server-command.py"

# verb -> required arg keys (validated before building argv)
ALLOWED = {
    "give-item": ["item"],
    "award-xp": ["category", "experience"],
    "teleport": ["x", "y", "z"],
    "refill-water": [],
}
MAX_STR = 200


def respond(success, mode, returncode, verb, detail):
    print(json.dumps({
        "success": success,
        "mode": mode,
        "returncode": returncode,
        "verb": verb,
        "detail": (detail or "")[:2000],
    }))
    sys.exit(0)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def main():
    raw = sys.stdin.read()
    try:
        job = json.loads(raw)
    except (ValueError, TypeError):
        respond(False, None, -1, None, "invalid JSON payload")

    verb = job.get("verb")
    mode = job.get("mode", "dry-run")
    operator = job.get("operator")
    reason = job.get("reason", "")
    args = job.get("args") or {}

    if verb not in ALLOWED:
        respond(False, mode, -1, verb, "verb not allowed via relay: %r" % verb)
    if mode not in ("apply", "dry-run"):
        respond(False, mode, -1, verb, "mode must be 'apply' or 'dry-run'")
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 64:
        respond(False, mode, -1, verb, "operator is required (<=64 chars)")
    if not isinstance(reason, str) or len(reason) > MAX_STR:
        respond(False, mode, -1, verb, "reason too long")
    if not isinstance(args, dict):
        respond(False, mode, -1, verb, "args must be an object")

    # target: exactly one of resolve / player_id
    resolve = job.get("resolve")
    player_id = job.get("player_id")
    if bool(resolve) == bool(player_id):
        respond(False, mode, -1, verb, "exactly one of resolve / player_id required")
    if resolve is not None and (not isinstance(resolve, str) or len(resolve) > 64):
        respond(False, mode, -1, verb, "invalid resolve")
    if player_id is not None and (not isinstance(player_id, str)
                                  or not player_id.isalnum() or len(player_id) > 32):
        respond(False, mode, -1, verb, "invalid player_id")

    # required arg presence
    for k in ALLOWED[verb]:
        if k not in args:
            respond(False, mode, -1, verb, "missing arg: %s" % k)

    # build the verb-specific argv tail with typed validation
    tail = []
    if verb == "give-item":
        item = args.get("item")
        qty = args.get("qty", 1)
        dur = args.get("durability", 1.0)
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_STR \
                or not item.replace("_", "").isalnum():
            respond(False, mode, -1, verb, "invalid item template")
        if not isinstance(qty, int) or isinstance(qty, bool) or not (1 <= qty <= 1000):
            respond(False, mode, -1, verb, "qty must be int 1..1000")
        if not is_num(dur) or not (0 <= dur <= 1):
            respond(False, mode, -1, verb, "durability must be 0..1")
        tail = ["--item", item, "--qty", str(qty), "--durability", str(float(dur))]
    elif verb == "award-xp":
        cat = args.get("category")
        exp = args.get("experience")
        if not isinstance(cat, str) or not cat.strip() or len(cat) > MAX_STR \
                or not cat.replace("_", "").isalnum():
            respond(False, mode, -1, verb, "invalid category")
        if not isinstance(exp, int) or isinstance(exp, bool) or not (1 <= exp <= 10_000_000):
            respond(False, mode, -1, verb, "experience must be int 1..10000000")
        tail = ["--category", cat, "--experience", str(exp)]
    elif verb == "teleport":
        x, y, z = args.get("x"), args.get("y"), args.get("z")
        if not all(is_num(v) for v in (x, y, z)):
            respond(False, mode, -1, verb, "x/y/z must be numbers")
        tail = ["--x", str(float(x)), "--y", str(float(y)), "--z", str(float(z))]
        if args.get("exact"):
            tail.append("--exact")
    elif verb == "refill-water":
        wa = args.get("water_amount", 100)
        if not isinstance(wa, int) or isinstance(wa, bool) or not (1 <= wa <= 100):
            respond(False, mode, -1, verb, "water_amount must be int 1..100")
        tail = ["--water-amount", str(wa)]

    argv = [sys.executable, CMD, "--operator", operator]
    if reason:
        argv += ["--reason", reason]
    if mode == "apply":
        argv.append("--send")
    argv.append(verb)
    if resolve:
        argv += ["--resolve", resolve]
    else:
        argv += ["--player-id", player_id]
    argv += tail

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        respond(False, mode, -1, verb, "server-command invocation timed out")
    except OSError as exc:
        respond(False, mode, -1, verb, "server-command exec failed: %s" % exc)

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    detail = out if result.returncode == 0 else (err or out or "server-command failed")
    respond(result.returncode == 0, mode, result.returncode, verb, detail)


if __name__ == "__main__":
    main()
