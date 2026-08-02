#!/usr/bin/env bash
# P8 — 5-layer INI merge reader.
#
# Deploys to lastsietch-dune:/opt/lastsietch-relay-helpers/dune-cvars-read.sh and is invoked
# only via the dispatcher action `cvars-read` (forced-command). Reads the six
# INI files from the pinned game pod (sg-survival-1-pod-1) via kubectl exec,
# parses them in shell + python3 stdlib, emits the merged-walk JSON envelope on
# stdout per P8-EXECUTION-BRIEF Appendix B.9.
#
# Read-only on the pod side. NEVER restarts game pods / BGD / k3s.
#
# Portions inspired by github.com/Icehunter/dune-admin (handlers_server_settings.go,
# control_kubectl.go) — MIT License, Copyright (c) 2026 Ryan Wilson.
# See $HOME/Source/Personal/House0fL0gic/admin-backend/THIRD_PARTY_LICENSES/dune-admin-MIT.txt
set -euo pipefail

NS="funcom-seabass-sh-<your-hostid>-<random>"
POD_GLOB="sh-*-sg-survival-1-pod-1"
KUBECTL=(sudo k3s kubectl -n "$NS")

# Layer order low→high (matches admin-backend/data/cvars-paths.json).
declare -a LAYERS=(
  "defaultEngine_linux:/home/dune/server/DuneSandbox/Config/Linux/DefaultEngine.ini"
  "defaultEngine:/home/dune/server/DuneSandbox/Config/DefaultEngine.ini"
  "defaultGame:/home/dune/server/DuneSandbox/Config/DefaultGame.ini"
  "userEngine:/home/dune/server/DuneSandbox/Saved/UserSettings/UserEngine.ini"
  "userGame:/home/dune/server/DuneSandbox/Saved/UserSettings/UserGame.ini"
  "userOverrides:/home/dune/server/DuneSandbox/Saved/UserSettings/UserOverrides.ini"
)

# Resolve the actual pod name from the glob — pod names carry a random suffix.
POD="$("${KUBECTL[@]}" get pods -o name | sed 's|^pod/||' | grep -E "^${POD_GLOB//\*/.*}$" | head -1)"
if [ -z "$POD" ]; then
  echo '{"status":"error","error":"no game pod matches '"$POD_GLOB"'"}' >&2
  exit 1
fi

# Slurp each layer; an empty/missing file becomes an empty string (UserOverrides
# is lazy-created on first write so this is the normal case before any edit).
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

for spec in "${LAYERS[@]}"; do
  layer="${spec%%:*}"
  path="${spec#*:}"
  if "${KUBECTL[@]}" exec "$POD" -- test -f "$path" 2>/dev/null; then
    "${KUBECTL[@]}" exec "$POD" -- cat "$path" > "$TMPDIR/$layer.ini" 2>/dev/null || : > "$TMPDIR/$layer.ini"
  else
    : > "$TMPDIR/$layer.ini"
  fi
done

# Merge + emit JSON via stdlib Python3.
POD="$POD" TMPDIR="$TMPDIR" python3 - <<'PY'
import json
import os
import re
import sys
from datetime import datetime, timezone

LAYERS = [
    ("defaultEngine_linux", "/home/dune/server/DuneSandbox/Config/Linux/DefaultEngine.ini"),
    ("defaultEngine",       "/home/dune/server/DuneSandbox/Config/DefaultEngine.ini"),
    ("defaultGame",         "/home/dune/server/DuneSandbox/Config/DefaultGame.ini"),
    ("userEngine",          "/home/dune/server/DuneSandbox/Saved/UserSettings/UserEngine.ini"),
    ("userGame",            "/home/dune/server/DuneSandbox/Saved/UserSettings/UserGame.ini"),
    ("userOverrides",       "/home/dune/server/DuneSandbox/Saved/UserSettings/UserOverrides.ini"),
]

SECTION_RE = re.compile(r'^\s*\[([^\]]+)\]\s*$')
# UE5 ini lines accept '+key=val' (append-array), '-key=val' (remove-array),
# '.key=val' (typed-set), plain 'key=val'. ';' begins a comment.
KV_RE = re.compile(r'^\s*([+\-\.]?)([A-Za-z_][A-Za-z0-9_:]*)\s*=\s*(.*?)\s*$')
COMMENT_RE = re.compile(r'^\s*[;#]')


def parse_ini(text):
    """Return (typed_kv, raw_array_lines, section_order):
        typed_kv         = {(section, key): value}   plain 'key=value' only
        raw_array_lines  = {section: [verbatim lines for + - . prefixed entries]}
        section_order    = [section, ...] in declaration order
    """
    typed = {}
    arrays = {}
    section = None
    order = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if COMMENT_RE.match(raw):
            continue
        m = SECTION_RE.match(raw)
        if m:
            section = m.group(1)
            if section not in arrays:
                arrays[section] = []
                order.append(section)
            continue
        if section is None:
            continue
        m = KV_RE.match(raw)
        if not m:
            continue
        prefix, key, value = m.group(1), m.group(2), m.group(3)
        if prefix == "":
            typed[(section, key)] = value
        else:
            arrays[section].append(raw.rstrip("\r"))
    return typed, arrays, order


def infer_type(v):
    if v in ("True", "False", "true", "false"):
        return "bool"
    try:
        int(v); return "int"
    except ValueError:
        pass
    try:
        float(v); return "float"
    except ValueError:
        pass
    return "string"


tmpdir = os.environ["TMPDIR"]
pod = os.environ["POD"]

layer_typed = {}
layer_arrays = {}
for name, _ in LAYERS:
    with open(os.path.join(tmpdir, f"{name}.ini"), encoding="utf-8", errors="replace") as f:
        text = f.read()
    typed, arrays, _ = parse_ini(text)
    layer_typed[name] = typed
    layer_arrays[name] = arrays

# Walk every (section, key) seen in any layer.
seen = {}
for name, _ in LAYERS:
    for (sec, key), val in layer_typed[name].items():
        seen.setdefault((sec, key), {})[name] = val

settings = []
for (sec, key), per_layer in sorted(seen.items()):
    chain = []
    current_value = None
    current_source = None
    for layer_name, layer_path in LAYERS:
        present = layer_name in per_layer
        value = per_layer.get(layer_name)
        chain.append({
            "layer": layer_name,
            "present": present,
            "value": value if present else None,
            "source_path": layer_path,
        })
        if present:
            current_value = value
            current_source = layer_name
    settings.append({
        "section": sec,
        "key": key,
        "current_value": current_value,
        "source": current_source,
        "is_override": current_source == "userOverrides",
        "inferred_type": infer_type(current_value) if current_value is not None else "string",
        "layers": chain,
    })

# Raw + - . prefixed lines: preserve verbatim per top-most layer that has them.
# (We expose them grouped by (section, source) so the UI can render arrays per-layer.)
raw_sections = []
for name, _ in LAYERS:
    for sec, lines in layer_arrays[name].items():
        if not lines:
            continue
        raw_sections.append({"section": sec, "source": name, "lines": lines})

paths = {name: path for name, path in LAYERS}
out = {
    "version": 1,
    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "pod": pod,
    "paths": paths,
    "settings": settings,
    "raw_sections": raw_sections,
    "warnings": [],
}
json.dump(out, sys.stdout, ensure_ascii=False)
sys.stdout.write("\n")
PY
