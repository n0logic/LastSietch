#!/usr/bin/env python3
"""Config watch for the Dune game config (v2, 2026-10-22). Read-only on the game.

v1 sha256'd six files against a baseline captured once on 2026-06-10 and never
re-blessed, so it failed every hour from 1.4.10.3 to 1.5.15.2 and could not show
a real drift. Two design faults, both fixed here:

1. Funcom's stock files (Default*.ini) change with every build. They are now
   tracked PER BUILD (the pinned pod's image tag): a new build records the new
   hashes and posts one info line; a stock change within one build is drift.
2. A whole-file hash cannot say whether OUR values survived, and it fires on a
   comment edit. Our layers (UserSettings/*.ini on the PVC) are now compared key
   by key against ops/game-config/values-of-record.json in the repo: a changed,
   missing or unexpected key is drift. A deliberate change means updating that
   manifest (--print-manifest), the same "mirror live fixes to the repo" rule as
   everywhere else.

Plus: the generated ServerCustomSettings.ini must not carry a preset
DifficultyLevel (a preset overrides every key in the user file).

Alerts post to the staff log channel only when the state changes (new drift, a
different drift, resolved, new build). Secret keys (admin password) are checked
for presence only; their values are never read into output.

Exit: 0 clean, 2 drift, 1 unavailable.

  dune-config-hash.py                  check (and alert on change)
  dune-config-hash.py --dry-run        check, print, no alert, no state write
  dune-config-hash.py --print-manifest print the live values in manifest form
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

NS = os.environ.get("DUNE_NAMESPACE", "funcom-seabass-sh-<your-hostid>-<random>")  # your battlegroup namespace
POD_SUFFIX = "sg-survival-1-pod-1"
STOCK = {
    "defaultEngine_linux": "/home/dune/server/DuneSandbox/Config/Linux/DefaultEngine.ini",
    "defaultEngine": "/home/dune/server/DuneSandbox/Config/DefaultEngine.ini",
    "defaultGame": "/home/dune/server/DuneSandbox/Config/DefaultGame.ini",
}
PVC_GLOB = "/var/lib/rancher/k3s/storage/*funcom*pvc"
USER_DIR = "Saved/UserSettings"
GENERATED = "Config/LinuxServer/ServerCustomSettings.ini"
MANIFEST = "/etc/lastsietch/config-values-of-record.json"
# A running restart effect (ops/weekend-events, xp or crafting) publishes the
# settings value it holds here, so an event weekend is not reported as drift.
EFFECT_EXPECTED = "/etc/lastsietch/event-effects/config-expected.json"
STATE = "/var/lib/lastsietch-config-watch/state.json"
ENV_FILE = "/opt/lastsietch-pod-watcher/.discord.env"
SECRET_RE = re.compile(r"password|token|secret", re.I)


def parse_ini(text):
    """section -> key -> sorted list of values. Comments and blank lines ignored;
    repeated sections merge; repeated keys (+m_Array=) keep every value."""
    out = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.setdefault(section, {}).setdefault(key.strip(), []).append(value.strip())
    for keys in out.values():
        for key in keys:
            keys[key].sort()
    return out


def redact(parsed):
    """Drop secret values, keeping only that the key is set."""
    clean = {}
    for section, keys in parsed.items():
        for key, values in keys.items():
            clean.setdefault(section, {})[key] = (
                {"secret": True} if SECRET_RE.search(key) else values)
    return clean


def compare_layer(name, want, live):
    findings = []
    for section in sorted(set(want) | set(live)):
        w, l = want.get(section, {}), live.get(section, {})
        for key in sorted(set(w) | set(l)):
            where = f"{name} [{section}] {key}"
            secret = SECRET_RE.search(key) is not None
            if key not in l:
                findings.append({"kind": "missing", "where": where,
                                 "want": None if secret else w[key]})
            elif key not in w:
                findings.append({"kind": "unexpected", "where": where,
                                 "got": None if secret else l[key]})
            elif not secret and w[key] != l[key]:
                findings.append({"kind": "changed", "where": where,
                                 "want": w[key], "got": l[key]})
    return findings


def apply_effects(manifest, text):
    """The manifest with a running event effect's values laid over it. Only keys
    the manifest already carries can be overridden: an effect never makes a new
    key expected, and an unreadable overlay changes nothing."""
    try:
        overlay = json.loads(text) if text else {}
    except ValueError:
        return manifest
    out = json.loads(json.dumps(manifest))
    for name, sections in (overlay or {}).items():
        for section, keys in (sections or {}).items():
            have = out.get("files", {}).get(name, {}).get(section, {})
            for key, value in (keys or {}).items():
                if key in have and isinstance(value, list):
                    have[key] = sorted(str(v) for v in value)
    return out


def check_generated(text):
    if text is None:
        return []
    values = {}
    for section_keys in parse_ini(text).values():
        values.update(section_keys)
    level = values.get("DifficultyLevel")
    if level and level != ["Custom"]:
        return [{"kind": "changed", "where": f"generated {GENERATED} DifficultyLevel",
                 "want": ["Custom"], "got": level}]
    return []


def check_stock(state, build, stock):
    """Returns (findings, info). A new build re-keys the stock hashes. A pod that is
    mid-restart (no build or hashes) is skipped this run, not reported as drift."""
    if not build or len(stock) != len(STOCK):
        return [], None
    prev = state.get("stock", {})
    if state.get("build") != build:
        changed = sorted(k for k in stock if prev.get(k) != stock[k])
        info = f"new Funcom build {state.get('build') or '(none recorded)'} -> {build}; stock files recorded" + (
            f" ({', '.join(changed)} changed)" if changed else "")
        state["build"], state["stock"] = build, stock
        return [], info
    return [{"kind": "changed", "where": f"stock {k} within build {build}",
             "want": [prev.get(k)], "got": [stock[k]]}
            for k in sorted(stock) if prev.get(k) != stock[k]], None


def fingerprint(findings):
    return hashlib.sha256(json.dumps(findings, sort_keys=True).encode()).hexdigest()[:16]


def alerts(state, findings, info):
    """Messages to post for this run; updates state['alerted']."""
    out = []
    if info:
        out.append("ℹ️ **Config watch:** " + info)
    fp = fingerprint(findings) if findings else None
    if fp and fp != state.get("alerted"):
        lines = []
        for f in findings[:15]:
            detail = ""
            if f.get("want") is not None or f.get("got") is not None:
                detail = f": want `{f.get('want')}` got `{f.get('got')}`"
            lines.append(f"• {f['kind']} {f['where']}{detail}")
        more = f"\n• ...and {len(findings) - 15} more" if len(findings) > 15 else ""
        out.append("🟠 **Config watch: live game config differs from the values of record** "
                   "(ops/game-config/values-of-record.json)\n" + "\n".join(lines) + more +
                   "\nIf deliberate, update the manifest in the repo; if not, re-apply from the checklist.")
    elif not fp and state.get("alerted"):
        out.append("🟢 **Config watch:** live game config matches the values of record again.")
    state["alerted"] = fp
    return out


# ── I/O (everything above is pure and unit-tested) ────────────────────────────

def kubectl(*args):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=60)


def read_stock():
    r = kubectl("get", "pods", "-n", NS, "-o", "jsonpath={.items[*].metadata.name}")
    pod = next((n for n in r.stdout.split() if n.endswith(POD_SUFFIX)), None)
    if not pod:
        return None, {}
    img = kubectl("get", "pod", "-n", NS, pod, "-o", "jsonpath={.spec.containers[0].image}").stdout
    build = img.rsplit(":", 1)[-1] if ":" in img else None
    r = kubectl("exec", "-n", NS, pod, "--", "sha256sum", *STOCK.values())
    by_path = {line.split()[1]: line.split()[0] for line in r.stdout.splitlines() if len(line.split()) == 2}
    return build, {k: by_path[p] for k, p in STOCK.items() if p in by_path}


def pvc_root():
    # The glob also matches the database PVC; the game PVC is the one with UserSettings.
    roots = [r for r in sorted(glob.glob(PVC_GLOB)) if os.path.isdir(os.path.join(r, USER_DIR))]
    if len(roots) != 1:
        raise RuntimeError(f"expected one Funcom game PVC, found {len(roots)}")
    return roots[0]


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None


def notify(messages):
    env = {}
    for line in (read_text(ENV_FILE) or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    token, channel = env.get("DISCORD_BOT_TOKEN"), env.get("DISCORD_CH_BOT_LOGS")
    if not token or not channel:
        print("notify: no bot env, not posted", file=sys.stderr)
        return
    for msg in messages:
        body = json.dumps({"content": msg[:1990], "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel}/messages", data=body, method="POST",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                     "User-Agent": "LastSietchConfigWatch (https://lastsietch.com, 2)"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:  # non-fatal: the exit code still carries the result
            print(f"notify failed: {e}", file=sys.stderr)


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-manifest", action="store_true")
    ap.add_argument("--manifest", default=MANIFEST)
    args = ap.parse_args()
    try:
        root = pvc_root()
        layers = {}
        for name in ("UserEngine.ini", "UserGame.ini", "UserOverrides.ini", "UserServerCustomSettings.ini"):
            text = read_text(os.path.join(root, USER_DIR, name))
            layers[name] = redact(parse_ini(text)) if text is not None else None
        if args.print_manifest:
            print(json.dumps({"files": {k: v for k, v in layers.items() if v is not None}},
                             indent=2, sort_keys=True, ensure_ascii=False))
            return 0
        with open(args.manifest, encoding="utf-8") as f:
            manifest = apply_effects(json.load(f), read_text(EFFECT_EXPECTED))
        build, stock = read_stock()
        generated = read_text(os.path.join(root, GENERATED))
    except Exception as e:
        print(json.dumps({"available": False, "error": str(e)}))
        return 1

    state = json.loads(read_text(STATE) or "{}")
    findings = []
    for name, want in manifest["files"].items():
        if layers.get(name) is None:
            findings.append({"kind": "missing", "where": f"{name} (whole file)"})
        else:
            findings += compare_layer(name, want, layers[name])
    findings += check_generated(generated)
    stock_findings, info = check_stock(state, build, stock)
    findings += stock_findings
    messages = alerts(state, findings, info)

    print(json.dumps({"available": True, "drift": bool(findings), "build": build,
                      "stock_checked": bool(build) and len(stock) == len(STOCK),
                      "generated_utc": datetime.now(timezone.utc).isoformat(),
                      "findings": findings, "alerts": len(messages)}))
    if not args.dry_run:
        save_state(state)
        if messages:
            notify(messages)
    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
