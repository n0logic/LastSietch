#!/usr/bin/env bash
# VC3 — Recent grants for a single account_id, JSON-emitting.
#
# Invoked by the forced-command dispatcher. Args:
#   $1 = account_id (numeric, required)
#   $2 = limit      (numeric 1..50, default 10)
#
# Returns: {"account_id": N, "grants": [ { id, granted_at, grant_type, status,
#                                          operator, batch_id, preset_name,
#                                          reverted_by_grant_id, detail } ]}
set -euo pipefail

account_id="${1:-}"
limit="${2:-10}"

if [[ ! "$account_id" =~ ^[0-9]+$ ]]; then
  echo '{"success":false,"error":"invalid account_id"}' >&2
  exit 2
fi
if [[ ! "$limit" =~ ^[0-9]+$ ]] || [ "$limit" -lt 1 ] || [ "$limit" -gt 50 ]; then
  limit=10
fi

sql=$(cat <<'EOF'
SELECT json_build_object(
  'account_id', :account_id::bigint,
  'limit',      :limit::int,
  'grants',     COALESCE(json_agg(g ORDER BY g_granted_at DESC), '[]'::json)
)
FROM (
  SELECT
    id, granted_at AS g_granted_at, granted_at, grant_type, status, operator,
    batch_id, preset_name, reverted_by_grant_id, detail
  FROM dune.ls_progression_grants
  WHERE account_id = :account_id
  ORDER BY granted_at DESC
  LIMIT :limit
) g;
EOF
)

# psql -v substitution requires stdin or -f (NOT -c) under kubectl-exec.
# Pipe SQL through stdin and flatten multi-line JSON output to single line.
out=$(printf '%s\n' "$sql" | /root/dq.sh -tAX -v "account_id=${account_id}" -v "limit=${limit}" 2>&1 | tr -d '\n' | sed 's/^ *//')

if [[ -z "$out" ]] || ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
  printf '{"success":false,"error":"psql result not valid JSON","raw":"%s"}\n' \
    "$(printf '%s' "$out" | head -c 200 | tr '"' "'")" >&2
  exit 3
fi

printf '%s\n' "$out"
