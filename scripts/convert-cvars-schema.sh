#!/usr/bin/env bash
# P8 — one-shot wrapper to regenerate admin-backend/data/cvars-schema.json
# from da-tweakables config-schema.kdl. Run when da-tweakables upstream is
# refreshed (or after editing the local vendored copy). Stdlib Python only.
#
# Usage:
#   ./scripts/convert-cvars-schema.sh [path/to/config-schema.kdl]
#
# Default source: ~/Tools/dune-third-party/da-tweakables/config-schema.kdl.
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-$HOME/Tools/dune-third-party/da-tweakables/config-schema.kdl}"
OUT="$REPO/admin-backend/data/cvars-schema.json"

if [ ! -f "$SRC" ]; then
  echo "error: KDL source not found at $SRC" >&2
  exit 1
fi

python3 "$REPO/admin-backend/tools/kdl_schema_to_json.py" "$SRC" > "$OUT"

# Surface the counts so the operator can sanity-check without opening the file.
python3 - "$OUT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"wrote {sys.argv[1]}: {d['kdl_field_count']} fields across {d['kdl_category_count']} categories")
print(f"source_sha256={d['source_sha256']}")
PY
