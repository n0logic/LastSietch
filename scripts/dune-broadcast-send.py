#!/usr/bin/env python3
"""
lastsietch-dune broadcast-send bridge: reads one JSON job from stdin and invokes the
proven dune-service-broadcast.py with a safe argv list (no shell interpolation
of the title/body text).

Invoked by the relay forced-command dispatcher (action `broadcast-send`, payload
on stdin) so arbitrary broadcast text never crosses a shell. The publisher
base64-encodes the envelope into the rabbitmqctl eval, so the text is safe end
to end. The AuthToken defaults to the build-baked BUILTIN_AUTH_TOKEN inside
dune-service-broadcast.py (override via DUNE_COMMAND_AUTH_TOKEN / --token-file).

stdin JSON:
  { "title": "...",                 # required, 1..MAX_TITLE chars
    "message": "...",               # required, 1..MAX_MSG chars
    "duration": 30,                 # banner seconds on screen, 1..600
    "mode": "apply"|"dry-run",      # dry-run omits --send (preview only)
    "operator": "username" }        # audit passthrough (publisher logs its own)

stdout JSON: { success, mode, returncode, title_len, message_len, detail }
Always exits 0 and reports status in JSON so the relay gets a clean response.
"""
import json
import subprocess
import sys

BROADCAST = "/opt/lastsietch-rmq-bridge/dune-service-broadcast.py"
MAX_TITLE = 80
MAX_MSG = 280
MIN_DURATION = 1
MAX_DURATION = 600


def respond(success, mode, returncode, title_len, message_len, detail):
    print(json.dumps({
        "success": success,
        "mode": mode,
        "returncode": returncode,
        "title_len": title_len,
        "message_len": message_len,
        "detail": detail[:2000],
    }))
    sys.exit(0)


def main():
    raw = sys.stdin.read()
    try:
        job = json.loads(raw)
    except (ValueError, TypeError):
        respond(False, None, -1, 0, 0, "invalid JSON payload")

    title = job.get("title")
    message = job.get("message")
    duration = job.get("duration", 30)
    mode = job.get("mode", "dry-run")

    if not isinstance(title, str) or not title.strip():
        respond(False, mode, -1, 0, 0, "title is required")
    if not isinstance(message, str) or not message.strip():
        respond(False, mode, -1, len(title), 0, "message is required")
    if len(title) > MAX_TITLE:
        respond(False, mode, -1, len(title), len(message), f"title exceeds {MAX_TITLE} chars")
    if len(message) > MAX_MSG:
        respond(False, mode, -1, len(title), len(message), f"message exceeds {MAX_MSG} chars")
    if isinstance(duration, bool) or not isinstance(duration, int) \
            or not (MIN_DURATION <= duration <= MAX_DURATION):
        respond(False, mode, -1, len(title), len(message),
                f"duration must be an integer {MIN_DURATION}..{MAX_DURATION}")
    if mode not in ("apply", "dry-run"):
        respond(False, mode, -1, len(title), len(message), "mode must be 'apply' or 'dry-run'")

    # The full dune-service-broadcast.py uses a `generic` subcommand; --send is a
    # top-level flag and must precede the subcommand.
    argv = [sys.executable, BROADCAST]
    if mode == "apply":
        argv.append("--send")
    argv += ["generic", "--title", title, "--message", message,
             "--duration", str(duration)]

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        respond(False, mode, -1, len(title), len(message), "broadcast invocation timed out")
    except OSError as exc:
        respond(False, mode, -1, len(title), len(message), f"broadcast exec failed: {exc}")

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    detail = out if result.returncode == 0 else (err or out or "broadcast failed")
    respond(result.returncode == 0, mode, result.returncode, len(title), len(message), detail)


if __name__ == "__main__":
    main()
