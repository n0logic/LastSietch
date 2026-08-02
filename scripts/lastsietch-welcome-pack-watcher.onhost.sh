#!/usr/bin/env bash
# lastsietch-dune-local welcome-pack watcher. Runs sweep once per invocation.
# Designed to be triggered by a systemd timer every 60s.
#
# CANONICAL on-host copy. Deployed to lastsietch-dune:/opt/lastsietch-welcome-pack/watcher.sh
# (runs as root via lastsietch-welcome-pack-watcher.service/.timer). This is the
# variant that actually runs; scripts/welcome-pack-watcher.sh is the older
# WSL/ssh variant and has diverged.
#
# Per invocation:
#   1. intel_sweep: apply deferred +100 intel to any welcome-pack account
#      that is Offline and not yet stamped (intel-sweep.sql).
#   2. new-account sweep: for each never-granted account, run grant.sh to
#      insert the v1.4 welcome pack, resolve the display name, post a Discord
#      welcome to #dune (env file: /opt/lastsietch-welcome-pack/.discord.env), and
#      whisper an in-game Cielago welcome if the recipient is online.

set -euo pipefail

NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
POD="${POD:-sh-<your-hostid>-<random>-db-dbdepl-sts-0}"
GRANT=/opt/lastsietch-welcome-pack/grant.sh
DISCORD_ENV=/opt/lastsietch-welcome-pack/.discord.env
INTEL_SWEEP_SQL=/opt/lastsietch-welcome-pack/intel-sweep.sql

# Welcome-pack re-roll hole fix: one pack per stable identity (fls_id) per
# cooldown window. SINGLE SOURCE OF TRUTH for the cooldown; exported so grant.sh
# applies the same window. Change it here. A character delete/recreate mints a
# new account_id for the same identity, so dedup on identity, not account_id.
export WELCOME_PACK_COOLDOWN_DAYS="${WELCOME_PACK_COOLDOWN_DAYS:-30}"

# In-game welcome whisper (Cielago herald). Sender renders from the funcom_id
# of CIELAGO_HOSTID's account; works with the Cielago alt logged out.
HERALD=/opt/lastsietch-rmq-bridge/dune-chat-herald.py
CIELAGO_HOSTID=93700FA3235F3C5A
CIELAGO_FUNCOMID="Cielago#47840"
# Welcome whisper, sent as one or more sequential whispers (in case a single
# line exceeds the in-game whisper length limit). %s in the first line is the
# recipient display name. Edit these lines to change the copy; no other change
# needed. Keep each part within the whisper char limit (TBD, confirm live).
WELCOME_WHISPER_PARTS=(
  'Welcome to Last Sietch, %s. Your sietch welcome package has been delivered. Relog once and it appears in your inventory.'
  'New here? Rules, the live map, and our Discord are all at lastsietch.com.'
  'Link your character at lastsietch.com/portal to track your Landsraad rewards, storage, and progression. The spice must flow.'
)

# Cooldown-skip whisper. Sent when a new account re-rolls an identity that
# already received a welcome package inside the cooldown window. {name} = display
# name, {days} = remaining days (already pluralized by the caller). No em dashes
# in player-facing copy.
ELIGIBILITY_WHISPER_PARTS=(
  'Welcome back, {name}. Your identity already received a Last Sietch welcome package recently, so a new one was not granted.'
  'You will be eligible for another welcome package in {days}. Until then, the spice must flow.'
)

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [welcome-watcher] $*"
}

run_psql() {
  PGPASS=$(sudo kubectl exec -n $NS $POD -- printenv POSTGRES_PASSWORD)
  sudo kubectl exec -i -n $NS $POD -- env PGPASSWORD=$PGPASS psql -h localhost -p 15432 -U postgres -d dune -t -A -F'|' -v ON_ERROR_STOP=1
}

resolve_display_name() {
  # Returns the display portion of funcom_id (before the '#'), e.g. "honeybee#28357" -> "honeybee"
  local acct="$1"
  echo "SELECT funcom_id FROM dune.accounts WHERE id=$acct;" | run_psql 2>/dev/null | head -n1 | tr -d '\n' | sed 's/#.*$//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

resolve_funcom_id() {
  # Returns the full funcom_id (Display#tag) for an account id.
  local acct="$1"
  echo "SELECT funcom_id FROM dune.accounts WHERE id=$acct;" | run_psql 2>/dev/null | head -n1 | tr -d '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

is_recipient_online() {
  # True if the account currently has an Online player state. Whispers only
  # render to online clients; an offline recipient's queue expires (~30 min).
  local acct="$1" out
  out=$(echo "SELECT 1 FROM dune.encrypted_player_state WHERE account_id=$acct AND online_status='Online' AND character_state IS DISTINCT FROM 'Deleted' LIMIT 1;" \
        | run_psql 2>/dev/null | tr -d '[:space:]')
  [[ "$out" == "1" ]]
}

welcome_whisper() {
  # Fire a one-time Cielago welcome whisper to a freshly-granted account.
  # Tied to the grant event (one-shot via ls_welcome_pack_grants), gated on
  # the recipient being online. Pack is the durable part; whisper is a bonus.
  local acct="$1" name="$2"
  local fid
  fid=$(resolve_funcom_id "$acct")
  if [[ -z "$fid" || "$fid" != *"#"* ]]; then
    log "  whisper: no funcom_id for account $acct, skipping"
    return 0
  fi
  if ! is_recipient_online "$acct"; then
    log "  whisper: ${fid} offline, skipping welcome whisper (pack still granted)"
    return 0
  fi
  local disp="${name:-${fid%%#*}}"
  local logf="/tmp/welcome-whisper-${acct}.log"
  : > "$logf"
  local part msg sent=0 ok=1
  for part in "${WELCOME_WHISPER_PARTS[@]}"; do
    printf -v msg "$part" "$disp"
    if PATH="/usr/local/bin:$PATH" python3 "$HERALD" --direct \
          --user-id "$CIELAGO_HOSTID" --from-id "$CIELAGO_FUNCOMID" --send \
          whisper --to "$fid" --message "$msg" >> "$logf" 2>&1; then
      sent=$((sent + 1))
    else
      ok=0
    fi
    sleep 1   # preserve ordering between parts
  done
  if [[ $ok -eq 1 ]]; then
    log "  whisper: sent ${sent}-part Cielago welcome to ${fid}"
  else
    log "  whisper: PARTIAL/FAILED for ${fid} (sent ${sent}), see ${logf}"
  fi
}

eligibility_whisper() {
  # Whisper a cooldown-skip notice to a re-roll account. Gated on the recipient
  # being online (whispers only render to online clients). Best-effort, like the
  # welcome whisper.
  local acct="$1" name="$2" remaining_days="$3"
  local fid
  fid=$(resolve_funcom_id "$acct")
  if [[ -z "$fid" || "$fid" != *"#"* ]]; then
    log "  eligibility: no funcom_id for account $acct, skipping"
    return 0
  fi
  if ! is_recipient_online "$acct"; then
    log "  eligibility: ${fid} offline, deferring cooldown notice (retried when online)"
    return 1
  fi
  local disp="${name:-${fid%%#*}}"
  local days_phrase
  if [[ "$remaining_days" == "1" ]]; then
    days_phrase="1 day"
  else
    days_phrase="${remaining_days} days"
  fi
  local logf="/tmp/eligibility-whisper-${acct}.log"
  : > "$logf"
  local part msg sent=0
  for part in "${ELIGIBILITY_WHISPER_PARTS[@]}"; do
    msg="${part//\{name\}/$disp}"
    msg="${msg//\{days\}/$days_phrase}"
    if PATH="/usr/local/bin:$PATH" python3 "$HERALD" --direct \
          --user-id "$CIELAGO_HOSTID" --from-id "$CIELAGO_FUNCOMID" --send \
          whisper --to "$fid" --message "$msg" >> "$logf" 2>&1; then
      sent=$((sent + 1))
    fi
    sleep 1
  done
  log "  eligibility: sent ${sent}-part cooldown notice to ${fid} (eligible in ${days_phrase})"
  return 0
}

record_skip() {
  # Mark this re-roll account as cooldown-notified so we do not re-whisper it
  # every 60s sweep. One row per account_id.
  local acct="$1" fid="$2" remaining_days="$3"
  echo "INSERT INTO dune.ls_welcome_pack_skips (account_id, fls_id, remaining_days_at_skip)
        VALUES ($acct, NULLIF('$fid', ''), $remaining_days)
        ON CONFLICT (account_id) DO NOTHING;" | run_psql >/dev/null 2>&1 || true
}

discord_welcome() {
  local name="$1"
  [[ -z "$name" ]] && { log "  discord: empty display name, skipping"; return 0; }
  if [[ ! -r "$DISCORD_ENV" ]]; then
    log "  discord: $DISCORD_ENV not readable, skipping"
    return 0
  fi
  # shellcheck disable=SC1090
  source "$DISCORD_ENV"
  if [[ -z "${DISCORD_BOT_TOKEN:-}" || -z "${DISCORD_CH_DUNE:-}" ]]; then
    log "  discord: missing token or channel id, skipping"
    return 0
  fi
  local content
  content="Welcome to the Sietch, **${name}**. Your Sietch Welcome Package has been delivered. Spice flows."
  local body
  body=$(python3 -c "import json,sys; print(json.dumps({'content': sys.argv[1]}))" "$content")
  local http_code
  http_code=$(curl -sS -o /tmp/discord-welcome-${acct}.resp -w "%{http_code}" \
    -X POST "https://discord.com/api/v10/channels/${DISCORD_CH_DUNE}/messages" \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$body" || echo "curlfail")
  if [[ "$http_code" == "200" ]]; then
    log "  discord: posted welcome for ${name}"
  else
    log "  discord: post failed http=$http_code see /tmp/discord-welcome-${acct}.resp"
  fi
}

# --- 1. deferred intel sweep (runs every invocation) ---
intel_sweep() {
  # Apply deferred +100 intel to offline, unstamped welcome-pack accounts.
  if [[ ! -r "$INTEL_SWEEP_SQL" ]]; then
    log "intel-sweep: $INTEL_SWEEP_SQL not readable, skipping"
    return 0
  fi
  local out applied
  out=$(run_psql < "$INTEL_SWEEP_SQL" 2>/dev/null || true)
  applied=$(echo "$out" | grep -E '^[0-9]+$' || true)
  if [[ -n "$applied" ]]; then
    log "intel-sweep: applied +100 intel to offline accounts: $(echo "$applied" | tr '\n' ' ')"
  fi
}

intel_sweep

# --- 2. new-account sweep ---
# Dedup on stable identity (fls_id), not account_id. An account with no grant row
# of its own is either a brand-new identity (action=grant) or a re-roll of an
# identity that already got a pack inside the cooldown window (action=skip ->
# eligibility notice). Already-notified skips are excluded so we whisper once.
sql="WITH cfg AS (
  SELECT (INTERVAL '1 day') * ${WELCOME_PACK_COOLDOWN_DAYS} AS cooldown
),
cand AS (
  -- One row per account_id. An account can have multiple encrypted_player_state
  -- pawn rows (a stale orphan pawn stuck Online beside the real character); the
  -- old per-pawn select yielded N rows -> N grant.sh calls + N welcome whispers
  -- (only the first actually granted, the rest were idempotency-blocked, but each
  -- still whispered -- the 2026-06-24 Thoryn double-whisper). Pick the pawn that
  -- owns a main backpack, most-recent login, matching grant.sh's actor resolution.
  -- Dedup is by account_id ONLY; the fls_id re-roll cooldown below is unchanged.
  SELECT DISTINCT ON (eps.account_id)
         eps.account_id, eps.player_pawn_id, eps.last_login_time, a.\"user\" AS fls_id
    FROM dune.encrypted_player_state eps
    JOIN dune.accounts a ON a.id = eps.account_id
   WHERE eps.last_login_time IS NOT NULL
     AND eps.account_id NOT IN (SELECT account_id FROM dune.ls_welcome_pack_grants)
     AND eps.account_id NOT IN (SELECT account_id FROM dune.ls_welcome_pack_skips)
   ORDER BY eps.account_id,
            (EXISTS (SELECT 1 FROM dune.inventories i
                      WHERE i.actor_id = eps.player_pawn_id AND i.inventory_type = 0)) DESC,
            eps.last_login_time DESC
),
last_grant AS (
  SELECT g.fls_id, MAX(g.granted_at) AS last_at
    FROM dune.ls_welcome_pack_grants g
   WHERE g.fls_id IS NOT NULL
   GROUP BY g.fls_id
)
SELECT c.account_id, c.player_pawn_id, c.last_login_time,
       CASE WHEN lg.last_at IS NULL OR lg.last_at <= now() - cfg.cooldown
            THEN 'grant' ELSE 'skip' END AS action,
       COALESCE(c.fls_id, '') AS fls_id,
       CASE WHEN lg.last_at IS NULL THEN 0
            ELSE GREATEST(1, CEIL(EXTRACT(EPOCH FROM (lg.last_at + cfg.cooldown - now())) / 86400.0))::int
       END AS remaining_days
  FROM cand c
  CROSS JOIN cfg
  LEFT JOIN last_grant lg ON lg.fls_id = c.fls_id;"
rows=$(echo "$sql" | run_psql || true)

if [[ -z "$rows" ]]; then
  log "sweep: no new accounts"
  exit 0
fi

while IFS='|' read -r account_id pawn_id last_login action fls_id remaining_days; do
  [[ -z "$account_id" ]] && continue
  acct="$account_id"
  name=$(resolve_display_name "$account_id" || true)
  if [[ "$action" == "skip" ]]; then
    log "re-roll account: id=$account_id fls=${fls_id:-<none>} in cooldown -> skip (eligible in ${remaining_days}d)"
    if eligibility_whisper "$account_id" "$name" "$remaining_days"; then
      record_skip "$account_id" "$fls_id" "$remaining_days"
    fi
    continue
  fi
  log "new account: id=$account_id pawn=$pawn_id last_login=$last_login fls=${fls_id:-<none>} -> granting v1"
  if "$GRANT" "$account_id" > /tmp/welcome-pack-grant-${account_id}.log 2>&1; then
    log "  ok"
    log "  display_name=${name:-<unresolved>}"
    # discord_welcome "$name"  # disabled 2026-05-20: redundant w/ daily digest
    welcome_whisper "$account_id" "$name"
  else
    log "  FAILED, see /tmp/welcome-pack-grant-${account_id}.log"
  fi
done <<< "$rows"
