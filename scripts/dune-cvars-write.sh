#!/usr/bin/env bash
# P8 — single-target write to UserOverrides.ini on the pinned game pod plus a
# transactional row INSERT into lsadmin.cvar_changes on the dune DB pod.
#
# Deploys to lastsietch-dune:/opt/lastsietch-relay-helpers/dune-cvars-write.sh and is invoked
# only via the dispatcher action `cvars-write` (forced-command).
#
# Hard rules (P8-EXECUTION-BRIEF §10):
#   - WRITES ONLY to /home/dune/server/DuneSandbox/Saved/UserSettings/UserOverrides.ini.
#   - NEVER restarts game pods / BGD / k3s.
#   - Backup-before-write to UserOverrides.ini.bak-<UTC> on the game pod when a
#     prior file existed (UserOverrides.ini is lazy-created on first write).
#   - Atomic via kubectl cp staging + `mv` on the same pod FS.
#   - Preserves +key=, -key=, .key= array/typed-set lines and ; comments verbatim.
#   - Empty `value` = "clear" (line removed entirely, icehunter convention).
#   - INSERT row with status='applied' on success, status='failed' on file-write
#     failure (per B.5 status enum). CHECK constraint forbids 'pending' transient.
#
# Portions inspired by github.com/Icehunter/dune-admin (handlers_server_settings.go,
# control_kubectl.go: patchINI) — MIT License, Copyright (c) 2026 Ryan Wilson.
set -euo pipefail

NS="funcom-seabass-sh-<your-hostid>-<random>"
POD_GLOB="sh-*-sg-survival-1-pod-1"
TARGET="/home/dune/server/DuneSandbox/Saved/UserSettings/UserOverrides.ini"
DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

PAYLOAD_B64="${1:-}"
if [ -z "$PAYLOAD_B64" ]; then
  echo '{"status":"error","error":"missing base64 payload"}'
  exit 2
fi
if [[ ! "$PAYLOAD_B64" =~ ^[A-Za-z0-9+/=]+$ ]]; then
  echo '{"status":"error","error":"invalid base64 payload"}'
  exit 2
fi

GAME_POD="$(sudo k3s kubectl -n "$NS" get pods -o name 2>/dev/null \
              | sed 's|^pod/||' \
              | grep -E "^${POD_GLOB//\*/.*}$" | head -1)"
if [ -z "$GAME_POD" ]; then
  echo '{"status":"error","error":"no game pod matches '"$POD_GLOB"'"}'
  exit 1
fi

DB_NS="$(sudo k3s kubectl get ns -o name 2>/dev/null \
           | sed 's|^namespace/||' \
           | grep -E '^funcom-seabass-' | head -1 || true)"
DB_POD="$(sudo k3s kubectl get pods -n "$DB_NS" -o name 2>/dev/null \
            | sed 's|^pod/||' | grep -E -- '-db-dbdepl-sts-0$' | head -1 || true)"
if [ -z "$DB_NS" ] || [ -z "$DB_POD" ]; then
  echo '{"status":"error","error":"could not resolve dune DB pod"}'
  exit 3
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Snapshot current UserOverrides.ini (or empty if missing — lazy-create case).
HAD_PRIOR=0
if sudo k3s kubectl -n "$NS" exec "$GAME_POD" -- test -f "$TARGET" 2>/dev/null; then
  sudo k3s kubectl -n "$NS" exec "$GAME_POD" -- cat "$TARGET" > "$TMP/before.ini" 2>/dev/null
  HAD_PRIOR=1
else
  : > "$TMP/before.ini"
fi

# 1) Validate payload + patch into after.ini + write the audit stub. Exits
#    non-zero with a JSON error envelope on validation failure (set -e aborts).
GAME_POD="$GAME_POD" TARGET="$TARGET" PAYLOAD_B64="$PAYLOAD_B64" python3 - "$TMP" <<'PY'
import base64, hashlib, json, os, re, sys
from datetime import datetime, timezone

tmp = sys.argv[1]
before_path = os.path.join(tmp, "before.ini")
after_path = os.path.join(tmp, "after.ini")
stub_path = os.path.join(tmp, "stub.json")


def die(msg):
    print(json.dumps({"status": "error", "error": msg})); sys.exit(2)


try:
    payload = json.loads(base64.b64decode(os.environ["PAYLOAD_B64"]))
except Exception as exc:
    die(f"invalid payload: {exc}")

section = (payload.get("section") or "").strip()
key = (payload.get("key") or "").strip()
value = payload.get("value", "")
operator = (payload.get("operator") or "").strip()
operator_user_id = payload.get("operator_user_id")     # int|None — passed by admin-backend
reason = (payload.get("reason") or "").strip()
# admin-backend supplies the layer that was effective before this write (from
# the read-walk that populated the confirm modal). Fallback: "userOverrides" iff
# we find old_value in the local UserOverrides.ini, else null.
ini_layer_before_hint = (payload.get("ini_layer_before") or "").strip() or None

if not re.match(r"^[A-Za-z_/.][A-Za-z0-9_/.]*$", section): die("invalid section")
if not re.match(r"^[A-Za-z_][A-Za-z0-9_:]*$", key):         die("invalid key")
if any(ch in value for ch in ("\n", "\r", "\x00")):         die("value contains control chars")
if not operator:                                            die("operator required")

before = open(before_path, encoding="utf-8", errors="replace").read()

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
KV_RE = re.compile(r"^\s*([+\-\.]?)([A-Za-z_][A-Za-z0-9_:]*)\s*=\s*(.*?)\s*$")

old_value = None
in_section = False
for line in before.splitlines():
    m = SECTION_RE.match(line)
    if m: in_section = (m.group(1) == section); continue
    if not in_section: continue
    m = KV_RE.match(line)
    if m and m.group(1) == "" and m.group(2) == key:
        old_value = m.group(3).strip(); break

# Patcher: rewrite-in-place or append-to-section or append-new-section.
out_lines = []
patched = False
current_section = None
seen_target_section = False
last_substantive_in_section = {}
for raw in before.splitlines():
    m = SECTION_RE.match(raw)
    if m:
        current_section = m.group(1)
        if current_section == section:
            seen_target_section = True
        out_lines.append(raw); continue
    if current_section == section:
        m = KV_RE.match(raw)
        if m and m.group(1) == "" and m.group(2) == key:
            if value != "":
                out_lines.append(f"{key}={value}")
            patched = True
            continue
    out_lines.append(raw)
    if current_section is not None and raw.strip():
        last_substantive_in_section[current_section] = len(out_lines) - 1

if not patched and value != "":
    if seen_target_section:
        insert_at = last_substantive_in_section.get(section)
        new_line = f"{key}={value}"
        if insert_at is None:
            for i, line in enumerate(out_lines):
                m = SECTION_RE.match(line)
                if m and m.group(1) == section:
                    out_lines.insert(i + 1, new_line); break
        else:
            out_lines.insert(insert_at + 1, new_line)
    else:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(f"[{section}]")
        out_lines.append(f"{key}={value}")

after = "\n".join(out_lines)
if not after.endswith("\n"):
    after += "\n"
open(after_path, "w", encoding="utf-8").write(after)

source_before = ini_layer_before_hint
if source_before is None and old_value is not None:
    source_before = "userOverrides"

stub = {
    "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "pod": os.environ["GAME_POD"],
    "file": os.environ["TARGET"],
    "section": section,
    "key": key,
    "old_value": old_value,
    "new_value": value if value != "" else None,
    "source_before": source_before,
    "sha256_before": hashlib.sha256(before.encode("utf-8")).hexdigest(),
    "sha256_after": hashlib.sha256(after.encode("utf-8")).hexdigest(),
    "operator": operator,
    "operator_user_id": operator_user_id,
    "reason": reason,
}
json.dump(stub, open(stub_path, "w"))
PY

# 2) Push after.ini back. Backup BEFORE the atomic mv so .bak captures the
#    pre-write content exactly.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BAK_REMOTE="${TARGET}.bak-${STAMP}"
WRITE_OK=0
WRITE_ERR=""
if sudo k3s kubectl -n "$NS" exec "$GAME_POD" -- mkdir -p "$(dirname "$TARGET")" >/dev/null 2>&1; then
  if [ "$HAD_PRIOR" = "1" ]; then
    sudo k3s kubectl -n "$NS" exec "$GAME_POD" -- cp -p "$TARGET" "$BAK_REMOTE" >/dev/null 2>&1 || true
  fi
  STAGE="/tmp/UserOverrides.ini.staged.$$"
  if sudo k3s kubectl -n "$NS" cp "$TMP/after.ini" "$GAME_POD:$STAGE" >/dev/null 2>&1; then
    if sudo k3s kubectl -n "$NS" exec "$GAME_POD" -- sh -c "mv $(printf '%q' "$STAGE") $(printf '%q' "$TARGET")" >/dev/null 2>&1; then
      WRITE_OK=1
    else
      WRITE_ERR="kubectl exec mv failed"
    fi
  else
    WRITE_ERR="kubectl cp staging failed"
  fi
else
  WRITE_ERR="kubectl exec mkdir failed"
fi

# 3) Compose the final envelope (with status, bak_path, etc.).
HAD_PRIOR="$HAD_PRIOR" BAK_REMOTE="$BAK_REMOTE" WRITE_OK="$WRITE_OK" WRITE_ERR="$WRITE_ERR" python3 - "$TMP/stub.json" "$TMP/env.json" <<'PY'
import json, os, sys
env = json.load(open(sys.argv[1]))
env["status"] = "ok" if os.environ["WRITE_OK"] == "1" else "error"
env["bak_path"] = os.environ["BAK_REMOTE"] if os.environ["HAD_PRIOR"] == "1" else None
env["row_status"] = "applied" if os.environ["WRITE_OK"] == "1" else "failed"
if os.environ["WRITE_OK"] != "1":
    env["error"] = os.environ["WRITE_ERR"]
json.dump(env, open(sys.argv[2], "w"))
PY

# 4) INSERT the audit row. cvar_key stored as "<section>|<key>" per agreed
#    convention (recoverable client-side; relay history endpoint splits on
#    '|' for the per-row response).
PGPASS="$(sudo k3s kubectl exec -n "$DB_NS" "$DB_POD" -- printenv POSTGRES_PASSWORD 2>/dev/null || true)"
if [ -z "$PGPASS" ]; then
  CHANGE_ID=""
  INSERT_OK=false
else
  # P8.1 fix: previously the SQL was `INSERT ... RETURNING id` and the script
  # captured the id via `kubectl exec -i ... psql ... | tr -d '[:space:]'`.
  # The RETURNING line never made it through that pipe reliably so every
  # write reported change_id:null + audit_db_failed:true even though the
  # INSERT had landed. The fix is two statements in the same psql session:
  # the INSERT (no RETURNING) and a follow-up SELECT currval() that emits a
  # single integer line on stdout. currval() is session-local so concurrent
  # writes from other sessions don't pollute it.
  RAW_OUT="$(python3 - "$TMP/env.json" <<'PY' | sudo k3s kubectl exec -i -n "$DB_NS" "$DB_POD" -- env PGPASSWORD="$PGPASS" psql -h localhost -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tA -v ON_ERROR_STOP=1 2>/tmp/cvars-insert.err
import json, sys
env = json.load(open(sys.argv[1]))
section = env["section"]; key = env["key"]
old_val = env["old_value"]
new_val = env["new_value"] if env["new_value"] is not None else ""
reason  = env["reason"] or None
layer   = env["source_before"]
audit   = json.dumps(env)
operator = env["operator"]
status  = env["row_status"]


def lit(s):
    if s is None: return "NULL"
    return "'" + s.replace("'", "''") + "'"


sql = (
    "INSERT INTO lsadmin.cvar_changes\n"
    "  (operator_discord_id, cvar_key, old_value, new_value, reason, ini_layer_before, relay_audit_json, status)\n"
    "VALUES\n"
    "  (" + lit(operator) + ", " + lit(section + "|" + key) + ", "
    + lit(old_val) + ", " + lit(new_val) + ", "
    + lit(reason) + ", " + lit(layer) + ", "
    + lit(audit) + "::jsonb, " + lit(status) + ");\n"
    "SELECT currval('lsadmin.cvar_changes_id_seq');\n"
)
print(sql)
PY
)"
  PSQL_RC=$?
  # Last non-empty line of psql -tA output = the currval integer.
  CHANGE_ID="$(printf '%s' "$RAW_OUT" | awk 'NF{last=$0} END{print last}' | tr -d '[:space:]')"
  if [ "$PSQL_RC" -eq 0 ] && [ -n "$CHANGE_ID" ] && [[ "$CHANGE_ID" =~ ^[0-9]+$ ]]; then
    INSERT_OK=true
  else
    INSERT_OK=false
    CHANGE_ID=""
  fi
fi

# 5) Emit the final envelope on stdout (with change_id + audit_db_failed).
CHANGE_ID="$CHANGE_ID" INSERT_OK="$INSERT_OK" python3 - "$TMP/env.json" <<'PY'
import json, os, sys
env = json.load(open(sys.argv[1]))
ci = os.environ["CHANGE_ID"]
env["change_id"] = int(ci) if ci.isdigit() else None
env["audit_db_failed"] = (os.environ["INSERT_OK"] != "true")
print(json.dumps(env))
PY

if [ "$WRITE_OK" = "1" ]; then
  exit 0
else
  exit 1
fi
