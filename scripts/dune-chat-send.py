#!/usr/bin/env python3
"""
lastsietch-dune chat-send bridge: reads one JSON job from stdin and invokes the proven
dune-chat-herald.py with a safe argv list (no shell interpolation of the message).

Invoked by the relay forced-command dispatcher (action `chat-send`, payload on
stdin) so arbitrary chat text never crosses a shell. The herald itself base64-
encodes the body into the rabbitmqctl eval, so the text is safe end to end.

stdin JSON:
  { "scope": "whisper"|"map",
    "message": "...",                 # required, 1..MAX_MSG chars
    "mode": "apply"|"dry-run",        # dry-run omits --send (preview only)
    "recipient": "Display#tag",       # whisper only
    "map": "HaggaBasin", "dim": 0,    # map only
    "operator": "username" }          # audit passthrough

stdout JSON: { success, scope, mode, returncode, message_len, detail }
Always exits 0 and reports status in JSON so the relay gets a clean response.
"""
import json
import re
import subprocess
import sys

HERALD = "/opt/lastsietch-rmq-bridge/dune-chat-herald.py"
CIELAGO_HOST_ID = "93700FA3235F3C5A"  # sender renders as "Cielago" (dune.accounts)
MAX_MSG = 1000
FUNCOM_ID_RE = re.compile(r"^[^\s#]{1,32}#[0-9]{1,10}$")
MAP_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


def respond(success, scope, mode, returncode, message_len, detail):
    print(json.dumps({
        "success": success,
        "scope": scope,
        "mode": mode,
        "returncode": returncode,
        "message_len": message_len,
        "detail": detail[:2000],
    }))
    sys.exit(0)


def main():
    raw = sys.stdin.read()
    try:
        job = json.loads(raw)
    except (ValueError, TypeError):
        respond(False, None, None, -1, 0, "invalid JSON payload")

    scope = job.get("scope")
    message = job.get("message")
    mode = job.get("mode", "dry-run")

    if scope not in ("whisper", "map"):
        respond(False, scope, mode, -1, 0, "scope must be 'whisper' or 'map'")
    if not isinstance(message, str) or not message.strip():
        respond(False, scope, mode, -1, 0, "message is required")
    if len(message) > MAX_MSG:
        respond(False, scope, mode, -1, len(message), f"message exceeds {MAX_MSG} chars")
    if mode not in ("apply", "dry-run"):
        respond(False, scope, mode, -1, len(message), "mode must be 'apply' or 'dry-run'")

    argv = [sys.executable, HERALD, "--direct", "--user-id", CIELAGO_HOST_ID]
    if mode == "apply":
        argv.append("--send")

    if scope == "whisper":
        recipient = job.get("recipient", "")
        if not isinstance(recipient, str) or not FUNCOM_ID_RE.match(recipient):
            respond(False, scope, mode, -1, len(message), "recipient must be a FuncomId (Display#tag)")
        argv += ["whisper", "--to", recipient, "--message", message]
    else:  # map
        map_name = job.get("map", "HaggaBasin")
        dim = job.get("dim", 0)
        if not isinstance(map_name, str) or not MAP_NAME_RE.match(map_name):
            respond(False, scope, mode, -1, len(message), "map must match ^[A-Za-z0-9_]{1,40}$")
        if isinstance(dim, bool) or not isinstance(dim, int) or not (0 <= dim <= 1000):
            respond(False, scope, mode, -1, len(message), "dim must be an integer 0..1000")
        argv += ["map", "--map", map_name, "--dim", str(dim), "--message", message]

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        respond(False, scope, mode, -1, len(message), "herald invocation timed out")
    except OSError as exc:
        respond(False, scope, mode, -1, len(message), f"herald exec failed: {exc}")

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    detail = out if result.returncode == 0 else (err or out or "herald failed")
    respond(result.returncode == 0, scope, mode, result.returncode, len(message), detail)


if __name__ == "__main__":
    main()
