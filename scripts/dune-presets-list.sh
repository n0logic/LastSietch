#!/usr/bin/env bash
# VC3 — List all grant presets from lsadmin.grant_presets, JSON-emitting.
#
# Invoked by the forced-command dispatcher. No args.
#
# Returns: {"presets": [ { name, display, description, ops, ops_count,
#                          reversible, operator_role, created_by, created_at,
#                          updated_at } ]}
set -euo pipefail

sql=$(cat <<'EOF'
SELECT json_build_object(
  'presets', COALESCE(json_agg(p ORDER BY p_id), '[]'::json)
)
FROM (
  SELECT
    id AS p_id,
    name, display, description, ops_json AS ops,
    jsonb_array_length(ops_json) AS ops_count,
    parameters,
    reversible, operator_role, created_by,
    created_at, updated_at
  FROM lsadmin.grant_presets
  ORDER BY id
) p;
EOF
)

# psql -tA wraps multi-element JSON arrays across lines. Pipe SQL via stdin
# (-v substitution doesn't work with -c under kubectl-exec) and flatten.
out=$(printf '%s\n' "$sql" | /root/dq.sh -tAX 2>&1 | tr -d '\n' | sed 's/^ *//')

if [[ -z "$out" ]] || ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
  printf '{"success":false,"error":"psql result not valid JSON","raw":"%s"}\n' \
    "$(printf '%s' "$out" | head -c 200 | tr '"' "'")" >&2
  exit 3
fi

printf '%s\n' "$out"
