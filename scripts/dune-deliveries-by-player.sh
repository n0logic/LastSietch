#!/usr/bin/env bash
# Fremkit wave 12: the packages the server sent one account, JSON-emitting.
#
# Invoked by the forced-command dispatcher. Args:
#   $1 = account_id (numeric, required)
#
# Read-only. Four tables, one round trip:
#   ls_welcome_pack_grants   the Welcome Package row (newest, one per account)
#   ls_item_delivery_log     its per-item lines (source='welcome_pack', cap 60)
#   ls_welcome_pack_skips    the identity-cooldown re-roll notice
#   ls_return_pack_grants    the Return Packages, newest first, LIMIT 5, with
#                             their item rows joined out of ls_progression_grants
#
# A missing table is a psql error, which is a non-zero exit and exit 3 here. It is
# NOT swallowed into an empty answer: the relay turns a failure into
# available:false, and "we could not read this" must never render as "you were
# sent nothing".
set -euo pipefail

account_id="${1:-}"

if [[ ! "$account_id" =~ ^[0-9]+$ ]]; then
  echo '{"success":false,"error":"invalid account_id"}' >&2
  exit 2
fi

sql=$(cat <<'EOF'
SELECT json_build_object(
  'account_id', :account_id::bigint,

  'welcome', (
    SELECT json_build_object(
      'granted_at',          g.granted_at,
      'version',             split_part(COALESCE(g.notes, ''), ':', 1),
      'granted_items',       g.granted_items,
      'notes',               g.notes,
      'whispered_at',        g.whispered_at,
      'rmq_delivered_at',    g.rmq_delivered_at,
      'exchange_listed_at',  g.exchange_listed_at,
      'research_applied_at', g.research_applied_at,
      'scrip_applied_at',    g.scrip_applied_at,
      'intel_applied_at',    g.intel_applied_at,
      'plan',                g.delivery_detail -> 'plan',
      'overflow',            g.delivery_detail -> 'overflow',
      'db_owed',             g.delivery_detail -> 'db_owed',
      'lines', COALESCE((
        SELECT json_agg(json_build_object(
                 'template_id',  l.template_id,
                 'quantity',     l.quantity,
                 'lane',         l.lane,
                 'status',       l.status,
                 'delivered_at', l.delivered_at,
                 'created_at',   l.created_at) ORDER BY l.created_at)
        FROM (
          SELECT template_id, quantity, lane, status, delivered_at, created_at
          FROM dune.ls_item_delivery_log
          WHERE source = 'welcome_pack' AND account_id = :account_id
          ORDER BY created_at
          LIMIT 60
        ) l), '[]'::json))
    FROM dune.ls_welcome_pack_grants g
    WHERE g.account_id = :account_id
    ORDER BY g.granted_at DESC
    LIMIT 1),

  'welcome_skip', (
    SELECT json_build_object(
      'notified_at',            s.notified_at,
      'remaining_days_at_skip', s.remaining_days_at_skip)
    FROM dune.ls_welcome_pack_skips s
    WHERE s.account_id = :account_id
    ORDER BY s.notified_at DESC
    LIMIT 1),

  'returns', COALESCE((
    SELECT json_agg(json_build_object(
             'granted_at',   r.granted_at,
             'tier',         r.tier,
             'char_level',   r.char_level,
             'days_away',    r.days_away,
             'bank_done_at', r.bank_done_at,
             'tools_done_at', r.tools_done_at,
             'whispered_at', r.whispered_at,
             'notes',        r.notes,
             -- `detail` is written by the grant writers, not by this read, so
             -- every shape assumption about it is guarded. Two of them can
             -- actually raise, and each raise is one malformed row turning into
             -- a failed read for the whole account:
             --   * jsonb_array_elements raises on a non-array `items`, so the
             --     CASE substitutes an empty array,
             --   * ::int raises on non-numeric text, so each cast is gated on a
             --     digits-only match bounded to nine digits (which also keeps a
             --     long number from overflowing a 32-bit int).
             -- The jsonb -> and ->> operators return NULL rather than raising on
             -- a shape mismatch, so the two object CASEs are belt and braces
             -- and are kept only because this read does not own the shape.
             -- Guarded, a bad row contributes NULL fields and every other row
             -- still answers. The one shape this CANNOT survive is a `detail`
             -- that is not JSON at all: ::jsonb raises on the cast itself, the
             -- handler exits 3, and the relay reports available:false, which
             -- seals the card rather than lying to it.
             'bank_items', COALESCE((
               SELECT json_agg(json_build_object(
                        'template_id', f.tid,
                        'quantity',    COALESCE(
                          CASE WHEN f.stack ~ '^-?[0-9]{1,9}$' THEN f.stack::int END,
                          CASE WHEN f.qty   ~ '^-?[0-9]{1,9}$' THEN f.qty::int END),
                        'quality',
                          CASE WHEN f.qlt   ~ '^-?[0-9]{1,9}$' THEN f.qlt::int END))
               FROM dune.ls_progression_grants bp,
                    LATERAL jsonb_array_elements(
                      CASE WHEN jsonb_typeof(bp.detail::jsonb -> 'items') = 'array'
                           THEN bp.detail::jsonb -> 'items'
                           ELSE '[]'::jsonb END) it,
                    LATERAL (SELECT CASE WHEN jsonb_typeof(it) = 'object'
                                         THEN it ELSE '{}'::jsonb END AS e) ie,
                    LATERAL (SELECT ie.e ->> 'template_id' AS tid,
                                    ie.e ->> 'stack_size'  AS stack,
                                    ie.e ->> 'quantity'    AS qty,
                                    ie.e ->> 'quality'     AS qlt) f
               WHERE bp.id = ANY(r.bank_grant_ids)), '[]'::json),
             'tool_items', COALESCE((
               SELECT json_agg(json_build_object(
                        'template_id', t.tid,
                        'quantity',
                          CASE WHEN t.qty ~ '^-?[0-9]{1,9}$' THEN t.qty::int END))
               FROM dune.ls_progression_grants tp,
                    LATERAL (SELECT CASE WHEN jsonb_typeof(tp.detail::jsonb) = 'object'
                                         THEN tp.detail::jsonb
                                         ELSE '{}'::jsonb END AS d) td,
                    LATERAL (SELECT td.d ->> 'template_id' AS tid,
                                    td.d ->> 'quantity'    AS qty) t
               WHERE tp.id = ANY(r.tools_grant_ids)), '[]'::json)
           ) ORDER BY r.granted_at DESC)
    FROM (
      SELECT granted_at, tier, char_level, days_away, bank_done_at, tools_done_at,
             whispered_at, notes, bank_grant_ids, tools_grant_ids
      FROM dune.ls_return_pack_grants
      WHERE account_id = :account_id
      ORDER BY granted_at DESC
      LIMIT 5
    ) r), '[]'::json)
);
EOF
)

# psql -v substitution requires stdin or -f (NOT -c) under kubectl-exec.
# Pipe SQL through stdin and flatten multi-line JSON output to single line.
out=$(printf '%s\n' "$sql" | /root/dq.sh -tAX -v "account_id=${account_id}" 2>&1 | tr -d '\n' | sed 's/^ *//')

if [[ -z "$out" ]] || ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
  printf '{"success":false,"error":"psql result not valid JSON","raw":"%s"}\n' \
    "$(printf '%s' "$out" | head -c 200 | tr '"' "'")" >&2
  exit 3
fi

printf '%s\n' "$out"
