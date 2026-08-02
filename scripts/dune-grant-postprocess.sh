#!/usr/bin/env bash
# VC3 — Post-process a just-applied grant row to record batch_id + preset_name.
#
# Invoked by the forced-command dispatcher AFTER the relay sees a successful
# grant fire. Args:
#   $1 = grant_id     (numeric, required)
#   $2 = batch_id     (UUID, or '-' for null)
#   $3 = preset_name  (text, or '-' for null)
#
# This script is intentionally minimal — single UPDATE, no business logic.
# All validation happens here AND the dispatcher pre-validates the args.
set -euo pipefail

grant_id="${1:-}"
batch_id="${2:-}"
preset_name="${3:-}"

if [[ ! "$grant_id" =~ ^[0-9]+$ ]]; then
  echo '{"success":false,"error":"invalid grant_id"}' >&2
  exit 2
fi

# Allow '-' as the explicit "no update" sentinel for each optional field.
batch_clause=""
preset_clause=""
declare -a vargs=( -v "grant_id=${grant_id}" )

if [[ -n "$batch_id" && "$batch_id" != "-" ]]; then
  if [[ ! "$batch_id" =~ ^[A-Fa-f0-9-]{36}$ ]]; then
    echo '{"success":false,"error":"invalid batch_id (must be UUID)"}' >&2
    exit 2
  fi
  batch_clause="batch_id = :'batch_id'::uuid"
  vargs+=( -v "batch_id=${batch_id}" )
fi

if [[ -n "$preset_name" && "$preset_name" != "-" ]]; then
  if [[ ! "$preset_name" =~ ^[a-z][a-z0-9_]{0,63}$ ]]; then
    echo '{"success":false,"error":"invalid preset_name"}' >&2
    exit 2
  fi
  preset_clause="preset_name = :'preset_name'"
  vargs+=( -v "preset_name=${preset_name}" )
fi

if [[ -z "$batch_clause" && -z "$preset_clause" ]]; then
  echo '{"success":true,"updated":false,"message":"no-op (both fields blank)"}'
  exit 0
fi

set_clause=""
if [[ -n "$batch_clause" && -n "$preset_clause" ]]; then
  set_clause="${batch_clause}, ${preset_clause}"
elif [[ -n "$batch_clause" ]]; then
  set_clause="${batch_clause}"
else
  set_clause="${preset_clause}"
fi

sql="UPDATE dune.ls_progression_grants
        SET ${set_clause}
      WHERE id = :grant_id
  RETURNING id;"

result=$(printf '%s\n' "$sql" | /root/dq.sh -tA "${vargs[@]}" 2>&1 || true)
matched=$(printf '%s\n' "$result" | grep -E '^[0-9]+$' | tail -n1 || true)

if [[ -z "$matched" ]]; then
  printf '{"success":false,"error":"no row matched grant_id=%s","psql_output":"%s"}\n' \
    "$grant_id" "$(printf '%s' "$result" | tr '"\n' "' ")" >&2
  exit 3
fi

printf '{"success":true,"updated":true,"grant_id":%s}\n' "$matched"
