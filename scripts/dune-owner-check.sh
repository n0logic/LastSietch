#!/usr/bin/env bash
# Post-Funcom-hotfix ownership assertion.
#
# Why this exists: hotfix 1.4.0.2 (2026-05-26) reverted dune DB +
# schema ownership to `postgres` for at least 2 community operators
# (magiemalone, wofnull — see DISCORD-INTEL-2026-05-27-PM.md). Funcom's
# pre-update pg_dump halts the whole update path if any custom table
# isn't OWNER=dune (see docs/12-operations.md, "the pg_dump trap"). We
# verified manually after our 14:35Z apply yesterday, but a missed
# verify on a future hotfix could brick admin tooling.
#
# This script asserts ownership across:
#   - dune database
#   - dune + lsadmin schemas
#   - all lsadmin.* tables
#   - all dune.ls_* tables (Last Sietch custom)
# Exits 0 only if EVERY check is OWNER=dune. Non-zero otherwise with a
# pointer to the offending row.
#
# Wire into the post-hotfix runbook as a HARD GATE before BG restart.

set -euo pipefail

sql=$(cat <<'EOF'
WITH checks AS (
  SELECT 'database:dune' AS kind, pg_catalog.pg_get_userbyid(d.datdba) AS owner
    FROM pg_database d WHERE d.datname = 'dune'
  UNION ALL
  SELECT 'schema:'||nspname AS kind, pg_catalog.pg_get_userbyid(nspowner) AS owner
    FROM pg_namespace WHERE nspname IN ('dune', 'lsadmin')
  UNION ALL
  SELECT 'table:'||schemaname||'.'||tablename AS kind, tableowner AS owner
    FROM pg_tables WHERE schemaname = 'lsadmin'
  UNION ALL
  SELECT 'table:'||schemaname||'.'||tablename AS kind, tableowner AS owner
    FROM pg_tables WHERE schemaname = 'dune' AND tablename LIKE 'ls\_%' ESCAPE '\'
)
SELECT kind || '|' || owner FROM checks ORDER BY kind;
EOF
)

out=$(printf '%s\n' "$sql" | /root/dq.sh -tA 2>&1)

if [[ -z "$out" ]]; then
  echo "FAIL: ownership query returned no rows" >&2
  exit 3
fi

bad=0
total=0
while IFS='|' read -r kind owner; do
  [[ -z "$kind" ]] && continue
  total=$((total + 1))
  if [[ "$owner" != "dune" ]]; then
    echo "FAIL: $kind has owner=$owner (expected dune)" >&2
    bad=$((bad + 1))
  fi
done <<<"$out"

if [[ "$bad" -gt 0 ]]; then
  echo "FAIL: $bad of $total ownership rows wrong. Run reditus's recovery:" >&2
  echo "  ALTER DATABASE dune OWNER TO dune;" >&2
  echo "  ALTER SCHEMA dune OWNER TO dune;" >&2
  echo "  REASSIGN OWNED BY postgres TO dune;" >&2
  exit 1
fi

echo "OK: $total ownership rows all OWNER=dune"
