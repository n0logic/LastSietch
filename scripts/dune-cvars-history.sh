#!/usr/bin/env bash
# P8 — paginated history read of lsadmin.cvar_changes.
#
# Deploys to lastsietch-dune:/opt/lastsietch-relay-helpers/dune-cvars-history.sh and is
# invoked only via the dispatcher action `cvars-history` (forced-command).
# Reads rows from the dune Postgres pod and returns a JSON envelope shaped
# for the admin v2 history sub-tab template:
#   {"rows": [{changed_at, operator_handle, operator_user_id, section, key,
#              old_value, new_value, source_before, reason, applied_at,
#              applied_observed_via, status, change_id}], "total": <int>}
set -euo pipefail

DB_PORT=15432
DB_USER=postgres
DB_NAME=dune

LIMIT="${1:-50}"
OFFSET="${2:-0}"

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]] || [ "$LIMIT" -lt 1 ] || [ "$LIMIT" -gt 200 ]; then
  echo '{"status":"error","error":"invalid limit (1..200)"}'
  exit 2
fi
if ! [[ "$OFFSET" =~ ^[0-9]+$ ]] || [ "$OFFSET" -gt 100000 ]; then
  echo '{"status":"error","error":"invalid offset"}'
  exit 2
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

PGPASS="$(sudo k3s kubectl exec -n "$DB_NS" "$DB_POD" -- printenv POSTGRES_PASSWORD 2>/dev/null || true)"
if [ -z "$PGPASS" ]; then
  echo '{"status":"error","error":"could not read POSTGRES_PASSWORD"}'
  exit 3
fi

SQL=$(cat <<SQL
WITH counted AS (
  SELECT COUNT(*)                                                      AS total,
         COUNT(*) FILTER (WHERE status = 'applied')                    AS pending_restart
  FROM lsadmin.cvar_changes
)
SELECT json_build_object(
  'rows', COALESCE((
    SELECT json_agg(row_obj)
    FROM (
      SELECT json_build_object(
        'change_id', id,
        'changed_at', to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
        'operator_handle', operator_discord_id,
        'operator_user_id', NULLIF((relay_audit_json->>'operator_user_id'), '')::int,
        'section', split_part(cvar_key, '|', 1),
        'key', split_part(cvar_key, '|', 2),
        'old_value', old_value,
        'new_value', CASE WHEN new_value = '' THEN NULL ELSE new_value END,
        'source_before', ini_layer_before,
        'reason', reason,
        'status', status,
        'applied_at', CASE WHEN status = 'applied'
                            THEN to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
                            ELSE NULL END,
        'applied_observed_via', NULL,
        'pod', relay_audit_json->>'pod',
        'file', relay_audit_json->>'file',
        'sha256_before', relay_audit_json->>'sha256_before',
        'sha256_after',  relay_audit_json->>'sha256_after',
        'bak_path', relay_audit_json->>'bak_path'
      ) AS row_obj
      FROM lsadmin.cvar_changes
      ORDER BY ts DESC
      LIMIT :'limit' OFFSET :'offset'
    ) sub
  ), '[]'::json),
  'total', (SELECT total FROM counted),
  'pending_restart', (SELECT pending_restart FROM counted),
  'limit', :'limit'::int,
  'offset', :'offset'::int
)::text;
SQL
)

# set +e around the psql call so a SQL-level failure (e.g. missing migration)
# doesn't abort the script before we emit the JSON error envelope.
set +e
OUT="$(
  printf '%s\n' "$SQL" \
    | sudo k3s kubectl exec -i -n "$DB_NS" "$DB_POD" -- \
        env PGPASSWORD="$PGPASS" psql -h localhost -p "$DB_PORT" \
        -U "$DB_USER" -d "$DB_NAME" -tA -v ON_ERROR_STOP=1 \
        -v "limit=$LIMIT" -v "offset=$OFFSET" 2>/tmp/cvars-history.err
)"
PSQL_RC=$?
set -e

if [ "$PSQL_RC" -ne 0 ] || [ -z "$OUT" ]; then
  ERR_MSG="$(sed -n '1,3p' /tmp/cvars-history.err 2>/dev/null | tr '\n' ' ' | sed 's/"/\\"/g')"
  [ -z "$ERR_MSG" ] && ERR_MSG="history query produced no output (exit $PSQL_RC)"
  printf '{"status":"error","error":"%s","exit_code":%d}\n' "$ERR_MSG" "$PSQL_RC"
  exit 1
fi

# psql -tA emits the JSON on the first line; trim any trailing blank.
printf '%s\n' "$OUT" | head -1
