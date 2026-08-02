#!/usr/bin/env bash
# Forced-command dispatcher for the lastsietch-relay service SSH key on the game host.
# Pinned via authorized_keys command="..." — the key can ONLY run the
# read-only Dune dashboard operations below, never an arbitrary shell.
# Deployed to <game-host>:/root/dune-relay-dispatch.sh.
set -euo pipefail

# VC0: source-IP allowlist. Defense-in-depth on top of the forced-command
# constraint. the web host is the only authorized relay caller; rotation
# happens by editing this list (or LASTSIETCH_RELAY_ALLOWED_IPS env). The
# allowlist applies to any key that's pinned to this dispatcher, so
# rotation = update list + deploy. Free-shell keys (operator@workstation etc.)
# never reach this script and are unaffected.
ALLOWED_SOURCE_IPS="${LASTSIETCH_RELAY_ALLOWED_IPS:-173.249.194.214}"
if [ -n "${SSH_CLIENT:-}" ]; then
  client_ip="${SSH_CLIENT%% *}"
  allowed=0
  for ip in $ALLOWED_SOURCE_IPS; do
    if [ "$client_ip" = "$ip" ]; then allowed=1; break; fi
  done
  if [ "$allowed" -ne 1 ]; then
    echo "$(date -u +%FT%TZ) rejected: source $client_ip not in allowlist" \
      >> /var/log/lastsietch-relay-dispatch.log 2>/dev/null || true
    echo "rejected: source IP not allowed" >&2
    exit 1
  fi
fi

cmd="${SSH_ORIGINAL_COMMAND:-}"
read -r action arg _extra <<<"$cmd"

# VC0: dispatch audit log. action + arg-hash + source IP. Forensic trail
# independent of the relay's own logs. Append-only (chattr +a recommended).
arg_hash="-"
if [ -n "${arg:-}" ]; then
  arg_hash=$(printf '%s' "$arg" | sha256sum | cut -c1-12)
fi
echo "$(date -u +%FT%TZ) ${action:-EMPTY} ${arg_hash} [${client_ip:-?}]" \
  >> /var/log/lastsietch-relay-dispatch.log 2>/dev/null || true

case "$action" in
  battlegroup)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 10 http://127.0.0.1:31282/v0/battlegroup
    ;;
  host-ops)
    # Public landing-page ops panel (lastsietch.com): game-box host telemetry
    # (cpu/mem/disk/uptime) + always-on/on-demand pod counts + BGD title/region
    # + online count. Read-only: kubectl get pods, /proc, df, curl localhost:31282.
    # Replaces an older ssh-from-the-web-host path. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-host-ops.sh
    ;;
  players-online)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 10 http://127.0.0.1:31282/v0/players/online
    ;;
  status)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-status.py
    ;;
  positions)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-positions.py
    ;;
  vehicles)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-vehicles.py
    ;;
  spice-active)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-spice-active.py
    ;;
  worms)
    # Live sandworm tracker for the Deep Desert map (read-only kubectl logs parse).
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-worms.py
    ;;
  sandstorm)
    # Time-only sandstorm ETA forecast for the Deep Desert map. The DD pod logs
    # emit only spawn-event timestamps (no coords/path/intensity), so this
    # read-only kubectl-logs parse derives per-dimension spawn cadence and the
    # next-storm ETA. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-sandstorm.py
    ;;
  positions-stream)
    # SSE live-position stream for the Hagga dashboard. Long-lived, so no
    # -m timeout; curl -N disables output buffering so frames flush at once.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -sN http://127.0.0.1:8078/positions/stream
    ;;
  fields)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-fields.py
    ;;
  roster)
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-roster.py
    ;;
  bases)
    # Base / land-claim ownership directory for the ADMIN portal: 166 claims
    # with owner, co-holders, position, piece count and condition. Ownership
    # comes from permission_actor_rank (rank 1 = owner), never from the actor,
    # which carries no owner for a totem. Read-only SELECTs; no arg accepted.
    # PII: this says who owns what. The relay endpoint is auth-gated and the
    # output must never be merged into a public map payload.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-bases.py list
    ;;
  bases-near)
    # Nearest claims to a world point, for an admin map click / tooltip.
    # ONE PACKED arg, because this dispatcher only reads three tokens:
    #   <map>:<dim>:<x>:<y>[:<limit>]   e.g. HaggaBasin:0:-95208.8:-379127.5:5
    # dim is mandatory: PvE and PvP Hagga share one coordinate space, so a
    # lookup without it silently ranks the other world's claims against the
    # point that was clicked.
    if [[ ! "$arg" =~ ^[A-Za-z0-9_]+:[0-9]{1,2}:-?[0-9]+(\.[0-9]+)?:-?[0-9]+(\.[0-9]+)?(:[0-9]{1,2})?$ ]]; then
      echo "rejected: expected <map>:<dim>:<x>:<y>[:limit]" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    IFS=: read -r _bmap _bdim _bx _by _blimit <<<"$arg"
    exec /root/dune-bases.py near "$_bmap" "$_bdim" "$_bx" "$_by" "${_blimit:-5}"
    ;;
  claim-backup)
    # WRITE. Back a base into its OWNER's reconstruction tool. The env flag is set
    # HERE and nowhere else, which makes this token the single audited doorway to
    # the operation: an admin-panel button can fire it while the script stays
    # inert to anyone who only has a shell. Every eligibility rule (orphan, owner
    # online, owner already holds a backup) lives in the script and still applies.
    #
    # ⚠️ This persists the pickup; it does NOT despawn the structures. No database
    # path can. They stand until the map reloads, and Hagga is persistent, so that
    # means a restart. Prefer adopt + remove with the in-game tool when the plot
    # has to be visibly clear.
    if [[ ! "$arg" =~ ^[0-9]{1,19}$ ]]; then
      echo "rejected: expected a numeric totem id" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec env LASTSIETCH_BASE_BACKUP_OP_ENABLED=1 \
      /root/dune-base-backup-op.py --backup "$arg" --operator admin-panel
    ;;
  claim-adopt)
    # WRITE. Adopt a claim, keeping the previous owner as a co-holder so they are
    # not locked out of their own base. ONE PACKED arg because this dispatcher
    # reads three tokens:  <totem_id>:<admin_account_id>
    # The claim cap (3) and the owned-claim rules are enforced in the script.
    if [[ ! "$arg" =~ ^[0-9]{1,19}:[0-9]{1,19}$ ]]; then
      echo "rejected: expected <totem_id>:<account_id>" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    IFS=: read -r _ctotem _cacct <<<"$arg"
    exec env LASTSIETCH_CLAIM_TAKEOVER_ENABLED=1 LASTSIETCH_CLAIM_TAKEOVER_ALLOW_OWNED=1 \
      /root/dune-claim-takeover.py --take "$_ctotem" --account "$_cacct" \
      --force-owned --keep-as-coholder --operator admin-panel
    ;;
  claim-options)
    # READ-ONLY preflight for the admin Claims map: what can be done about this
    # base. Runs both checkers and merges them, so the panel makes one call and
    # cannot show half an answer. Neither script writes without its own env flag,
    # and neither flag is set here.
    if [[ ! "$arg" =~ ^[0-9]{1,19}$ ]]; then
      echo "rejected: expected a numeric totem id" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    _adopt=$(/root/dune-claim-takeover.py --check "$arg" 2>/dev/null || echo '{}')
    _backup=$(/root/dune-base-backup-op.py --check "$arg" 2>/dev/null || echo '{}')
    # Operator roster rides along on the same call: the panel needs it at exactly
    # the moment it renders the adopt option, and a picker that cannot show who
    # has a free claim slot is just a dropdown of names.
    _ops=$(/root/dune-claim-takeover.py --operators 2>/dev/null || echo '{}')
    printf '{"adopt":%s,"backup":%s,"operators":%s}\n' "$_adopt" "$_backup" "$_ops"
    exit 0
    ;;
  guilds)
    # Public guild directory (names, members, Landsraad contributions) for the
    # portal Guild/Recruitment section. Read-only SELECTs; no arg accepted.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-guilds.py
    ;;
  guild-invites)
    # P0 read: a player's OWN pending guild invites. arg = numeric account_id;
    # dune-guilds.py resolves it to controller_id server-side (tombstone-safe)
    # and calls dune.get_player_guild_invites. Read-only.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-guilds.py pending-invites "$arg"
    ;;
  guild-census)
    # P0 read: online-state census of one guild's roster. arg = numeric
    # guild_id; dune-guilds.py calls dune.get_all_player_in_guild_online_state.
    # Caller-must-be-a-member is enforced upstream by the admin-backend.
    # Read-only.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid guild_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-guilds.py member-census "$arg"
    ;;
  presence)
    if [[ ! "$arg" =~ ^[a-zA-Z0-9]+$ ]]; then
      echo "rejected: invalid window" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-presence.py "$arg"
    ;;
  grant-players)
    # Progression-grant player picker — every character incl. OFFLINE.
    # Read-only SELECT inside dune-grant.sh; no arg accepted.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-grant.sh --list-players
    ;;
  grant-recent)
    # Recent grants — last N rows of dune.ls_progression_grants for the
    # admin panel's "Recent grants" widget. arg = optional integer limit
    # (1..200; defaults to 20 inside the script). Numeric-only allowlist.
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
        echo "rejected: invalid limit (must be a positive integer)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-grant.sh --list-recent "${arg:-20}"
    ;;
  progression-snapshot)
    # Latest per-account progression sample for the dashboard widgets
    # (histogram, top-N). Localhost call into the telemetry read API;
    # the API enforces read-only on telemetry.db.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 10 http://127.0.0.1:8078/progression/snapshot
    ;;
  read-models)
    # Per-account portal/admin read models for the web host local-mirror sync.
    # Localhost call into the telemetry read API (read-only on telemetry.db; the
    # game DB is not touched here). Optional numeric account_id narrows the pull.
    if [ -n "${arg:-}" ]; then
      case "$arg" in
        *[!0-9]*) echo "rejected: account_id must be digits" >&2; exit 2 ;;
      esac
      exec curl -s -m 15 "http://127.0.0.1:8078/read-models?account_id=$arg"
    fi
    exec curl -s -m 15 http://127.0.0.1:8078/read-models
    ;;
  storage-models)
    # Per-account storage snapshots for the web host local-mirror sync (Phase 2).
    # Localhost call into the telemetry read API (read-only on telemetry.db; the
    # game DB is not touched here). Optional numeric account_id narrows the pull.
    if [ -n "${arg:-}" ]; then
      case "$arg" in
        *[!0-9]*) echo "rejected: account_id must be digits" >&2; exit 2 ;;
      esac
      exec curl -s -m 30 "http://127.0.0.1:8078/storage?account_id=$arg"
    fi
    exec curl -s -m 30 http://127.0.0.1:8078/storage
    ;;
  login-days)
    # Per-account login-day history for the the web host rewards mirror (login-rewards
    # V2 streak calc). Localhost call into the telemetry read API (read-only on
    # telemetry.db; the game DB is not touched here). Optional numeric account_id
    # narrows the pull.
    if [ -n "${arg:-}" ]; then
      case "$arg" in
        *[!0-9]*) echo "rejected: account_id must be digits" >&2; exit 2 ;;
      esac
      exec curl -s -m 15 "http://127.0.0.1:8078/login-days?account_id=$arg"
    fi
    exec curl -s -m 15 http://127.0.0.1:8078/login-days
    ;;
  market-all)
    # Full active CHOAM exchange listing set for the web host local-mirror sync
    # (Phase 2). Localhost call into the telemetry read API (read-only on
    # telemetry.db; the game DB is not touched here). No args.
    [ -z "${arg:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 30 http://127.0.0.1:8078/market
    ;;
  market-bot-prices)
    # NPC market-maker buy-price export for the web host local-mirror sync:
    # per-item per-grade caps the bot still pays (written by lastsietch-market-bot on
    # every tick). Plain file read; neither the game DB nor the bot is touched.
    # A missing file fails loudly so the mirror keeps its last-good snapshot.
    [ -z "${arg:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec cat /var/lib/lastsietch-market-bot/bot-prices.json
    ;;
  market-bot-limits)
    # NPC market-maker per-category weekly buy/sell budget usage for the portal
    # Exchange tracker (written by lastsietch-market-bot every tick). Plain file read;
    # neither the game DB nor the bot is touched. Missing file fails loudly so
    # the mirror keeps its last-good snapshot.
    [ -z "${arg:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec cat /var/lib/lastsietch-market-bot/bot-limits.json
    ;;
  tags-read)
    # Read player tags by account_id. arg must be a numeric account_id.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-tags.py "$arg"
    ;;
  player-progress)
    # Per-account character (XP/skill points) + economy (Solari/Scrip) for the
    # public portal dashboard. arg must be a numeric account_id. Optional
    # _extra selects a multi-character mode (portal switcher):
    #   "--list"              -> every non-Deleted character on the account
    #   "--controller <N>"    -> scope the read to one character's controller id
    # Both are strictly allowlisted; anything else is rejected.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    if [ -z "${_extra:-}" ]; then
      exec /root/dune-player-progress.py "$arg"
    elif [ "$_extra" = "--list" ]; then
      exec /root/dune-player-progress.py "$arg" --list
    elif [[ "$_extra" =~ ^--controller\ [0-9]+$ ]]; then
      exec /root/dune-player-progress.py "$arg" --controller "${_extra#--controller }"
    else
      echo "rejected: unexpected args" >&2
      exit 1
    fi
    ;;
  player-equipped)
    # Per-account EQUIPPED gear (inventory_type=1) for the public portal
    # character stage. arg must be a numeric account_id; SELECT-only helper.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-player-equipped.py "$arg"
    ;;
  player-map)
    # Per-account map overlay: the player's OWN position + base totems + owned
    # vehicles (coords + map + dim). arg must be a numeric account_id. The
    # admin-backend only serves this for the caller's own session-bound account.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-player-map.py "$arg"
    ;;
  containers-list)
    # Per-player container list for v2 admin Player Tools (and P5 portal).
    # arg must be a numeric account_id; the helper script re-validates.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-containers.py "$arg"
    ;;
  container-search)
    # Cross-container item index for the portal "where is my stuff" search.
    # arg must be a numeric account_id; the helper script re-validates.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-container-search.py "$arg"
    ;;
  my-orders)
    # Read-only "My Orders" panel for the portal: a player's active sell
    # listings + Completed tab + recent market-log history. arg must be a
    # numeric account_id; the helper resolves controller_id server-side and
    # re-validates. Read-only SELECTs through dq.sh.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-my-orders.py "$arg"
    ;;
  progression-levelups)
    # Recent level-up events for the dashboard ticker. arg = optional
    # integer limit (1..1000; telemetry-api clamps and defaults to 100).
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
        echo "rejected: invalid limit (must be a positive integer)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    if [ -n "$arg" ]; then
      exec curl -s -m 10 "http://127.0.0.1:8078/progression/levelups?limit=$arg"
    else
      exec curl -s -m 10 http://127.0.0.1:8078/progression/levelups
    fi
    ;;
  grant)
    # Progression-grant WRITE path. arg is a single base64-encoded JSON blob
    # — the ONLY thing accepted. The base64 alphabet [A-Za-z0-9+/=] contains
    # no whitespace, no ';', no '$', no backtick: shell injection is
    # impossible. /root/dune-grant.sh re-decodes and re-validates every field.
    if [[ ! "$arg" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid grant payload" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-grant.sh --grant-b64 "$arg"
    ;;
  grant-stdin)
    # Same as 'grant' but the base64 payload comes via stdin instead of an
    # SSH argv positional. This sidesteps the kernel ARG_MAX limit
    # (~128KB on older kernels, 2MB on modern Linux but SSH adds overhead)
    # that breaks G20/G22 Solido imports above ~2900 pieces. The payload is
    # piped through stdin all the way to dune-grant.sh's matching
    # --grant-b64-stdin handler — no large argv anywhere in the chain.
    # Defense in depth: validate the b64 alphabet here AND in dune-grant.sh.
    [ -z "$arg" ] || { echo "rejected: grant-stdin takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid grant payload" >&2
      exit 2
    fi
    # printf is a shell builtin — no exec, no kernel argv limit on its
    # operand. /root/dune-grant.sh reads stdin for the actual payload.
    printf '%s' "$payload" | /root/dune-grant.sh --grant-b64-stdin
    exit $?
    ;;
  guild-op)
    # Guild-operations WRITE path. Base64-encoded JSON job on stdin (same safe
    # alphabet as grant-stdin -- no whitespace / ';' / '$' / backtick, so shell
    # injection is impossible). Decoded and handed to dune-guild-op.sh's
    # --op-b64-stdin mode, which re-validates every field, takes the guild lock,
    # writes dune.ls_guild_ops (idempotency + audit) in the same txn as the
    # proc call, and re-verifies is_player_guild_admin for leader ops. Invite ops
    # are dark by default (GUILD_WRITES_DARK). actor_account_id is resolved
    # server-side by the admin-backend from the session before it reaches here.
    [ -z "$arg" ] || { echo "rejected: guild-op takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid guild-op payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | /root/dune-guild-op.sh --op-b64-stdin
    exit $?
    ;;
  gift-op)
    # Solari gifting WRITE path. Same safe b64-JSON-on-stdin contract as guild-op
    # (no whitespace / ';' / '$' / backtick, so shell injection is impossible).
    # Handed to dune-gift-op.sh's --op-b64-stdin mode, which re-validates every
    # field, resolves sender+recipient controllers tombstone-safe, pre-checks the
    # sender balance INSIDE the locked txn, does the two value-conserving vcb
    # adjusts, and writes dune.ls_guild_gifts (idempotency + audit + rate-limit
    # ledger) in the same txn. DARK by default (LASTSIETCH_GIFTS_ENABLED=0): while off it
    # refuses cleanly with status:"deferred". sender/recipient account_ids are
    # resolved server-side by the admin-backend from the session before here.
    [ -z "$arg" ] || { echo "rejected: gift-op takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid gift-op payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | /root/dune-gift-op.sh --op-b64-stdin
    exit $?
    ;;
  item-transfer-op)
    # CHOAM bank item transfer WRITE path (Tier 5). Same safe b64-JSON-on-stdin
    # contract as gift-op. Handed to dune-item-transfer-op.sh's --op-b64-stdin
    # mode, which re-validates every field, resolves both banks tombstone-safe
    # (pawn-keyed, inv_type 30), locks the item row pinned to the sender's bank,
    # and does a single-row re-home UPDATE (atomic, dupe/loss-proof) writing
    # dune.ls_item_transfers (idempotency + audit + rate-limit ledger) in the
    # same txn. DARK by default (LASTSIETCH_ITEM_TRANSFER_ENABLED=0): while off it
    # refuses cleanly with status:"deferred". sender/recipient account_ids are
    # resolved server-side by the admin-backend from the session before here.
    [ -z "$arg" ] || { echo "rejected: item-transfer-op takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid item-transfer-op payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | /root/dune-item-transfer-op.sh --op-b64-stdin
    exit $?
    ;;

  karum-op)
    # The Karum (player-to-player trade venue) WRITE path. ONE dispatcher token for all
    # four writer actions -- karum-list, karum-buy, karum-cancel, karum-admin -- because
    # fewer forced-command cases is fewer places to get the safe-alphabet guard wrong. The
    # action travels INSIDE the signed b64 payload and dune-karum-op.sh re-validates it,
    # along with every other field, before opening a txn.
    #
    # Same safe b64-JSON-on-stdin contract as gift-op (no whitespace / ';' / '$' / backtick,
    # so shell injection is impossible). Every identity is resolved server-side by the
    # admin-backend from the session before it reaches here; a client can never trade AS
    # another player.
    #
    # The writer holds exactly ONE take (the seller's bank at listing time, offline-gated
    # via the shared /root/lib/dune-take-item.sh) and every other leg is a give, so the
    # dupe path is closed by construction rather than by care. Payment is gated by
    # dune.ls_karum_payments and delivery by dune.ls_item_delivery_log, both UNIQUE on
    # correlation_id, so a retry through this token is always a replay and never a second
    # effect. DARK by default (LASTSIETCH_KARUM_ENABLED=0): while off it refuses cleanly with
    # status:"deferred" without opening a DB txn.
    [ -z "$arg" ] || { echo "rejected: karum-op takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid karum-op payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | /root/dune-karum-op.sh --op-b64-stdin
    exit $?
    ;;

  karum-audit)
    # The Karum escrow audit. READ-ONLY: dune-karum-audit.py issues a single SELECT and
    # never writes, so this token is safe to call at any time, including under a change
    # freeze. Takes no arguments and no payload.
    #
    # It is the canary for an offline-gate regression, and that regression is SILENT by
    # construction: the take succeeds, the seller's loaded client resurrects the item under
    # its original id, and nothing errors. So this is exposed as a token rather than left as
    # a manual ssh, because the web host runs it nightly over the relay and the admin panel
    # runs it on demand.
    #
    # Exit codes are meaningful and MUST be surfaced, not flattened: 0 = clean,
    # 1 = at least one paging finding, 3 = the audit could not run, which is NOT clean.
    [ -z "$arg" ] || { echo "rejected: karum-audit takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    /root/dune-karum-audit.py
    exit $?
    ;;
  chat-send)
    # In-game chat WRITE path (Cielago herald). Base64-encoded JSON job on
    # stdin (same safe alphabet as grant-stdin); decoded and handed to the
    # wrapper, which invokes dune-chat-herald.py with a safe argv list so the
    # arbitrary message text never crosses a shell. dry-run jobs preview only.
    [ -z "$arg" ] || { echo "rejected: chat-send takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid chat payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /opt/lastsietch-rmq-bridge/dune-chat-send.py
    exit $?
    ;;
  broadcast-send)
    # In-game ServiceBroadcast WRITE path (Generic system banner to ALL connected
    # players). Base64-encoded JSON job on stdin (same safe alphabet as chat-send);
    # decoded and handed to the wrapper, which invokes the proven
    # dune-service-broadcast.py with a safe argv so arbitrary text never crosses a
    # shell. dry-run jobs preview only (no --send, no publish).
    [ -z "$arg" ] || { echo "rejected: broadcast-send takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid broadcast payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /opt/lastsietch-rmq-bridge/dune-broadcast-send.py
    exit $?
    ;;
  server-command)
    # Native server-command WRITE path (give-item/award-xp/teleport/refill-water to an
    # ONLINE player). Base64-encoded JSON job on stdin (same safe alphabet as broadcast-send);
    # decoded and handed to dune-server-command-send.py, which builds a safe argv for
    # dune-server-command.py (the publisher: builtin AuthToken, online-gate, audit log).
    # Destructive verbs (clean-inventory/reset-progression/cheat-script) + spawn-vehicle are
    # NOT routable here (wrapper allow-list). MASTER SWITCH: real --send sends require the
    # file /etc/lastsietch/servercmd-enabled to exist (sets LASTSIETCH_SERVERCMD_ENABLED=1); without it
    # dune-server-command.py refuses every --send. Remove the file to kill the whole path.
    [ -z "$arg" ] || { echo "rejected: server-command takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid server-command payload" >&2
      exit 2
    fi
    [ -f /etc/lastsietch/servercmd-enabled ] && export LASTSIETCH_SERVERCMD_ENABLED=1
    printf '%s' "$payload" | base64 -d | /opt/lastsietch-rmq-bridge/dune-server-command-send.py
    exit $?
    ;;
  # =============================================================================
  # Moderation trio dispatcher contract (Phase C 2026-05-29).
  # Design doc: docs/dune-research/MODERATION-AND-DRILLDOWN-DESIGN-2026-05-29.md
  #
  # Shared interface for admin-backend (dev-2):
  #   kick           <account_id>          numeric, validated here
  #   ban            <base64_json>         same shape as `grant`, routed via dune-grant.sh
  #   unban          <base64_json>         same shape as `ban`,   routed via dune-grant.sh
  #   bans-list      (no args)             read lsadmin.bans (active)
  #   bans-history   (no args)             read lsadmin.player_actions
  #
  # ban payload shape (decoded):
  #   { "grant_type":"ban", "account_id":N, "operator":"<admin>",
  #     "idempotency_key":"<uuid>",
  #     "detail":{ "fls_id":"<funcom_id>", "reason":"<text>",
  #                "note":"<text>?", "duration_minutes":<int>?,
  #                "banned_by":"<admin>" } }
  # unban payload shape (decoded):
  #   { "grant_type":"unban", "account_id":N, "operator":"<admin>",
  #     "idempotency_key":"<uuid>",
  #     "detail":{ "fls_id":"<funcom_id>", "unban_reason":"<text>",
  #                "unbanned_by":"<admin>" } }
  #
  # kick is direct: dispatcher validates account_id, dune-kick.py resolves
  # account_id -> dune.accounts.funcom_id internally (no inline psql here; the
  # python script already owns kubectl + namespace resolution, so the lookup
  # lives there rather than duplicating it in bash). Default --send-OFF in
  # the dispatcher cmd flips on for the live path.
  # =============================================================================
  kick)
    # Direct kick of a single account. arg = numeric account_id. iptables
    # source-IP drop (the RMQ KickPlayer ServerCommand is a confirmed no-op on
    # this GA build). dune-ipban.py refreshes the IP map, drops every recent
    # non-allowlisted IP for the account for ~120s, then auto-removes. Allowlist
    # (mgmt/private/node/relay + /etc/lastsietch/ipban-allowlist.txt) is never dropped.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-ipban.py kick --account-id "$arg" --operator lastsietch-relay
    ;;
  ips-list)
    # Read-only: known source IPs for an account (UI). arg = numeric account_id.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-ipban.py list-ips --account-id "$arg"
    ;;
  ip-detect)
    # On-demand refresh of the player->IP map. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-ip-detect.py
    ;;
  ban)
    # Ban WRITE path. arg = single base64-encoded JSON blob, same alphabet as
    # `grant` (no whitespace / shell metachars). dune-grant.sh re-decodes,
    # re-validates every field, writes lsadmin.bans + lsadmin.player_actions
    # inside a single BEGIN..COMMIT, and triggers an immediate kick if online.
    if [[ ! "$arg" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid ban payload" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-grant.sh --grant-b64 "$arg"
    ;;
  unban)
    # Unban WRITE path. Same b64 contract as ban; flips lsadmin.bans.active
    # to false. The ban-watcher stops re-kicking next tick. No kick triggered.
    if [[ ! "$arg" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid unban payload" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-grant.sh --grant-b64 "$arg"
    ;;
  bans-list)
    # Read-only listing of active lsadmin.bans rows, joined to
    # encrypted_player_state for online_status. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-bans.py
    ;;
  bans-history)
    # Read-only listing of lsadmin.player_actions (kick|ban|unban events).
    # No args; the helper clamps to a sensible LIMIT internally.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-bans.py --history
    ;;
  chat-players)
    # Read-only online-player list WITH funcom_id for the chat whisper picker.
    # No args; SELECT-only inside dune-chat-players.py.
    exec /root/dune-chat-players.py
    ;;
  bb-available-sources)
    # Read-only listing of dune.base_backups for the BB handoff/clone source
    # picker. No args accepted.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-bb-list.sh available-sources
    ;;
  bb-slot-count)
    # Read-only base_backups slot count for a target account. arg = numeric
    # account_id; the script re-validates and rejects non-digits.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-bb-list.sh slot-count "$arg"
    ;;
  bb-detail)
    # Read-only single base_backup detail (identity + owner + totem + per-class
    # composition) for the admin Bases detail drawer. arg = numeric backup_id;
    # the script re-validates and rejects non-digits.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid backup_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-bb-list.sh detail "$arg"
    ;;
  container-items)
    # Per-container items list for the v2 admin Player Tools drill-down
    # (LIFT-10). Multi-arg: <account_id> <container_id> [page]. Re-tokenise
    # the original command so we can grab 2-3 positionals; reject any extra.
    read -r _action ci_acct ci_container ci_page ci_extra <<<"$cmd"
    if [[ ! "$ci_acct" =~ ^[0-9]+$ ]] || [[ ! "$ci_container" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id/container_id (must be positive integers)" >&2
      exit 2
    fi
    if [ -n "${ci_page:-}" ] && [[ ! "$ci_page" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid page (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${ci_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-container-items.py "$ci_acct" "$ci_container" "${ci_page:-1}"
    ;;
  vehicle-parts)
    # Per-vehicle INSTALLED-parts durability list for the portal storage browser.
    # Multi-arg: <account_id> <container_id>. Read-only (dune.vehicle_modules).
    # Ownership pushed down to SQL; not owned => available=false/not_owned.
    read -r _action vp_acct vp_container vp_extra <<<"$cmd"
    if [[ ! "$vp_acct" =~ ^[0-9]+$ ]] || [[ ! "$vp_container" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id/container_id (must be positive integers)" >&2
      exit 2
    fi
    [ -z "${vp_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-vehicle-parts.py "$vp_acct" "$vp_container"
    ;;
  blueprints-list)
    # Order 0 / G30 v1 — per-player BuildingBlueprint_CopyDevice listing for
    # the v2 admin "Export" subtab. arg = numeric account_id; the helper
    # re-validates and rejects non-digits.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-blueprints-list.py "$arg"
    ;;
  blueprint-export)
    # Order 0 / G30 v1 — single-blueprint Solido-market JSON dump (verbatim
    # MIT port of icehunter cmdExportBlueprint). Multi-arg: <account_id> <bp_id>.
    # Ownership is re-verified inside the helper; cross-account fat-finger
    # returns "not_owned" instead of leaking data.
    read -r _action be_acct be_bp be_extra <<<"$cmd"
    if [[ ! "$be_acct" =~ ^[0-9]+$ ]] || [[ ! "$be_bp" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id/bp_id (must be positive integers)" >&2
      exit 2
    fi
    [ -z "${be_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-blueprint-export.py "$be_acct" "$be_bp"
    ;;
  character-export)
    # Last Sietch character snapshot dump. arg = numeric account_id; the helper
    # re-validates. Used by (a) the v2 admin "Export" subtab (no _extra =
    # account-level) and (b) the player portal "Download my data" (optional
    # "--controller <N>" scopes to the selected character; strictly allowlisted).
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    if [ -z "${_extra:-}" ]; then
      exec /root/dune-character-export.py "$arg"
    elif [[ "$_extra" =~ ^--controller\ [0-9]+$ ]]; then
      exec /root/dune-character-export.py "$arg" --controller "${_extra#--controller }"
    else
      echo "rejected: unexpected args" >&2
      exit 1
    fi
    ;;
  progression-state)
    # Phase 1/2: combined read backing the Specializations + Skills pickers:
    # owned keystones + per-track level/xp + learned skill blocks. arg =
    # numeric account_id; the helper re-validates. Read-only SELECTs only.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-progression-state.py "$arg"
    ;;
  cvars-read)
    # P8 — 5-layer INI merge read from the pinned game pod
    # (sg-survival-1-pod-1). Read-only kubectl exec inside the pod; no args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-relay-helpers/dune-cvars-read.sh
    ;;
  cvars-write)
    # P8 — single-target write to UserOverrides.ini on the pinned game pod.
    # arg = base64-encoded JSON payload (same alphabet allowlist as `grant`).
    # The helper re-decodes and re-validates every field; backup-before-write
    # is automatic; NEVER restarts game pods/BGD/k3s.
    if [[ ! "$arg" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid cvars-write payload" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-relay-helpers/dune-cvars-write.sh "$arg"
    ;;
  cvars-diff)
    # P8 — settingsUpdate capture diff. arg = optional ISO8601 UTC anchor
    # in compact form (`20260526T091342Z`).
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^[0-9TZ]+$ ]]; then
        echo "rejected: invalid --since anchor (UTC stamp only)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-rmq-bridge/diff-settingsupdate.sh "${arg:-}"
    ;;
  cvars-history)
    # P8 — paginated lsadmin.cvar_changes read. Multi-arg: <limit> <offset>;
    # the helper re-validates and clamps. Read-only psql SELECT.
    read -r _action ch_limit ch_offset ch_extra <<<"$cmd"
    if [ -n "${ch_limit:-}" ] && [[ ! "$ch_limit" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid limit (positive integer)" >&2
      exit 2
    fi
    if [ -n "${ch_offset:-}" ] && [[ ! "$ch_offset" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid offset (non-negative integer)" >&2
      exit 2
    fi
    [ -z "${ch_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-relay-helpers/dune-cvars-history.sh "${ch_limit:-50}" "${ch_offset:-0}"
    ;;
  preset-list)
    # VC3 — Read lsadmin.grant_presets. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-presets-list.sh
    ;;
  grant-recent-by-player)
    # VC3 — Recent grants for a single account_id. Multi-arg: <aid> [limit];
    # the helper re-validates + clamps limit to 1..50 (default 10).
    read -r _action rp_aid rp_limit rp_extra <<<"$cmd"
    if [[ ! "$rp_aid" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (positive integer)" >&2
      exit 2
    fi
    if [ -n "${rp_limit:-}" ] && [[ ! "$rp_limit" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid limit (positive integer)" >&2
      exit 2
    fi
    [ -z "${rp_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-grant-recent-by-player.sh "$rp_aid" "${rp_limit:-10}"
    ;;
  grant-postprocess)
    # VC3 — UPDATE just-applied grant row with batch_id + preset_name.
    # Multi-arg: <grant_id> <batch_id_or_dash> <preset_name_or_dash>. Each
    # field re-validated by the helper. '-' = no update for that column.
    read -r _action gp_gid gp_bid gp_preset gp_extra <<<"$cmd"
    if [[ ! "$gp_gid" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid grant_id (positive integer)" >&2
      exit 2
    fi
    if [ -n "${gp_bid:-}" ] && [[ "$gp_bid" != "-" ]] && [[ ! "$gp_bid" =~ ^[A-Fa-f0-9-]{36}$ ]]; then
      echo "rejected: invalid batch_id (UUID or '-')" >&2
      exit 2
    fi
    if [ -n "${gp_preset:-}" ] && [[ "$gp_preset" != "-" ]] && [[ ! "$gp_preset" =~ ^[a-z][a-z0-9_]{0,63}$ ]]; then
      echo "rejected: invalid preset_name (lowercase snake or '-')" >&2
      exit 2
    fi
    [ -z "${gp_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-grant-postprocess.sh "$gp_gid" "${gp_bid:--}" "${gp_preset:--}"
    ;;
  rmq-last-funcom-push)
    # VC2 P1 — read newest settingsUpdate capture, emit Funcom Intelligence
    # summary (last_ts, sha256, push_count_today). No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-rmq-bridge/rmq-last-funcom-push.py
    ;;
  rmq-bgd-rpc-recent)
    # VC2 P1 — newest UTC-window dir under captures-admin/rpc/, filtered
    # to the BGD inbox routing key. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-rmq-bridge/rmq-bgd-rpc-recent.py
    ;;
  rmq-partition-counts)
    # VC2 P1 — newest UTC-window dir under captures-admin/response/,
    # latest message per partition routing key. Aggregator joins to map
    # names via dune.world_partition. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-rmq-bridge/rmq-partition-counts.py
    ;;
  rmq-completions-recent)
    # VC2 P1 — newest UTC-window dir under captures-admin/completions/,
    # last N events for the Live Action Stream panel. arg = optional
    # limit (1-3 digits, default 50; helper clamps to <=200).
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^[0-9]{1,3}$ ]]; then
        echo "rejected: invalid limit (1-3 digits)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-rmq-bridge/rmq-completions-recent.py "${arg:-50}"
    ;;
  rmq-travel-queue)
    # VC2 P1 — newest UTC-window dir under captures-admin/travelQueueStatus/,
    # current queue depth + per-destination breakdown. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-rmq-bridge/rmq-travel-queue.py
    ;;
  telemetry-events)
    # VC2 P1 — direct proxy to localhost telemetry-api /events for the
    # Live Action Stream panel. arg = optional limit (1-4 digits, default 50).
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^[0-9]{1,4}$ ]]; then
        echo "rejected: invalid limit (1-4 digits)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 5 "http://127.0.0.1:8078/events?limit=${arg:-50}"
    ;;
  telemetry-transfers)
    # VC2 P1 — direct proxy to localhost telemetry-api /transfers (cross-
    # battlegroup transfer events). arg = optional limit (1-4 digits, default 50).
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^[0-9]{1,4}$ ]]; then
        echo "rejected: invalid limit (1-4 digits)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 5 "http://127.0.0.1:8078/transfers?limit=${arg:-50}"
    ;;
  telemetry-world)
    # VC2 P1 — direct proxy to localhost telemetry-api /world (world
    # counters series). arg = optional window (e.g. 24h, 7d, 4w; default 24h).
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^[0-9]{1,3}[hdwHDW]$ ]]; then
        echo "rejected: invalid window (e.g. 24h, 7d, 4w)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 5 "http://127.0.0.1:8078/world?window=${arg:-24h}"
    ;;
  telemetry-leaderboard-pvp|telemetry-leaderboard-deaths|telemetry-leaderboard-pilots)
    # VC4 — direct proxy to localhost telemetry-api /leaderboard/{board} for the
    # Monitor "Leaderboards" card (PvP kills / deaths+K-D / pilot air-distance).
    # board comes from the action suffix; arg = optional ISO week ("current"
    # default, or e.g. 2026-W23). Read-only; top-N slicing happens client-side.
    board="${action#telemetry-leaderboard-}"
    if [ -n "$arg" ]; then
      if [[ ! "$arg" =~ ^(current|[0-9]{4}-W[0-9]{2})$ ]]; then
        echo "rejected: invalid week (current or YYYY-Www)" >&2
        exit 2
      fi
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec curl -s -m 5 "http://127.0.0.1:8078/leaderboard/${board}?week=${arg:-current}"
    ;;
  spice-types)
    # W6: read-only listing of dune.spicefield_types (8 rows) for the V2
    # admin "Spice" sub-card. No args; SELECT-only inside dune-spice-toggle.py.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-spice-toggle.py --list
    ;;
  spice-toggle)
    # W6: flip dune.spicefield_types.is_spawning_active per field type. arg =
    # single base64-encoded JSON blob {type_id,new_value,who,change_id}, same
    # alphabet allowlist as `grant` (no whitespace / shell metachars). The
    # python helper re-decodes and re-validates every field, UPDATEs the boolean
    # + INSERTs lsadmin.spicefield_toggle_log in one BEGIN..COMMIT; NEVER
    # restarts game pods/BGD/k3s; toggling off does not despawn active fields.
    if [[ ! "$arg" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid spice-toggle payload" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-spice-toggle.py --apply-b64 "$arg"
    ;;
  stats-collect)
    # Server-stats digest data for the Cielago bot (Last Sietch). arg = period
    # ('daily' | 'weekly'). Read-only collector: SELECT-only on dune.* /
    # lsadmin.* + read-only telemetry.db; emits a structured JSON stats blob
    # on stdout. No Discord, no branding, no writes. Cielago renders + posts.
    case "$arg" in
      daily|weekly) ;;
      *) echo "rejected: period must be daily or weekly" >&2; exit 2 ;;
    esac
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /opt/lastsietch-stats/dune-stats-collect.py --period "$arg"
    ;;
  player-vitals)
    # V2 Identity tab: per-player economy (online-safe currency balances) +
    # activity (telemetry presence). arg = numeric account_id; helper
    # re-validates. Read-only SELECTs + read-only telemetry.db.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-player-vitals.py "$arg"
    ;;
  landsraad-rewards)
    # Public portal: a player's own unclaimed Landsraad rewards (per-house
    # items awaiting pickup + Solari totals). arg = numeric account_id; helper
    # re-validates and resolves account_id -> controller_id. Read-only SELECTs.
    if [[ ! "$arg" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid account_id (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-landsraad-rewards.py "$arg"
    ;;
  landsraad-board)
    # Public portal: the live Landsraad term board (term-global). 25 minor-house
    # tiles + per-faction progress + great-house score + top-guild contributors +
    # reward ladders for the CURRENT term. Read-only SELECTs; no args (the
    # admin-backend caches it for everyone, same as `guilds`).
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-landsraad-board.py
    ;;
  market-status)
    # V2 Server>Market: bot service state + balance + ls_market_log activity.
    # Read-only (SELECTs + systemctl show). No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-market-control.py status
    ;;
  market-listings)
    # Search active CHOAM exchange listings by template_id fragment. arg =
    # search term (letters/digits/_/- only; re-validated in the control script
    # since it is interpolated into a LIKE). Read-only single SELECT, LIMIT 200.
    if [[ ! "$arg" =~ ^[A-Za-z0-9_-]{2,64}$ ]]; then
      echo "rejected: search term must be 2-64 chars of letters/digits/_/-" >&2
      exit 2
    fi
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-market-control.py listings-search "$arg"
    ;;
  market-rare-recent)
    # Recently-listed rare-rotation items for the Cielago market announcer
    # (dune.ls_rare_rotation, coder-B's RareRotation.recent_listings). Multi-arg:
    # <after> [limit]. `after` = optional ISO8601 timestamptz cursor (digits, T,
    # Z, ':', '-', '+', '.'); the producer pages it as the last listed_at it
    # announced. `limit` = optional positive integer (producer defaults 50, clamps).
    # The cold-fill bootstrap is auto-excluded by the announce_after marker inside
    # the producer. Read-only SELECT.
    read -r _action rr_after rr_limit rr_extra <<<"$cmd"
    if [ -n "${rr_after:-}" ] && [[ ! "$rr_after" =~ ^[0-9TZ:+.-]+$ ]]; then
      echo "rejected: invalid after cursor (ISO8601 UTC timestamp only)" >&2
      exit 2
    fi
    if [ -n "${rr_limit:-}" ] && [[ ! "$rr_limit" =~ ^[0-9]+$ ]]; then
      echo "rejected: invalid limit (must be a positive integer)" >&2
      exit 2
    fi
    [ -z "${rr_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    # rare-recent.py reads DB_* from env. Resolve fresh from k3s (same source
    # run-market-bot.sh uses — the ClusterIP/port can change on svc recreate).
    # Absolute kubectl + explicit KUBECONFIG: the SSH forced-command env is minimal.
    export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
    _rr_ns="${DUNE_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
    _rr_svc="${DUNE_BG:-sh-<your-hostid>-<random>}-db-dbdepl-svc"
    _rr_pod="${DUNE_BG:-sh-<your-hostid>-<random>}-db-dbdepl-sts-0"
    DB_HOST=$(/usr/local/bin/kubectl get svc -n "$_rr_ns" "$_rr_svc" -o jsonpath='{.spec.clusterIP}')
    DB_PORT=$(/usr/local/bin/kubectl get svc -n "$_rr_ns" "$_rr_svc" -o jsonpath='{.spec.ports[0].port}')
    DB_PASS=$(/usr/local/bin/kubectl exec -n "$_rr_ns" "$_rr_pod" -- printenv POSTGRES_PASSWORD)
    export DB_HOST DB_PORT DB_USER=postgres DB_PASS DB_NAME=dune
    exec /opt/lastsietch-market-bot/rare-recent.py "${rr_after:--}" "${rr_limit:-50}"
    ;;
  market-policy-get)
    # Read the live market-policy.json (price floors, weekly budgets, blocked
    # sellers). Read-only. No args.
    [ -z "$arg" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-market-control.py policy-get
    ;;
  market-policy-set)
    # Write market-policy.json. base64 JSON policy on stdin (same safe alphabet
    # as chat-send); the control script base64-decodes, validates, backs up,
    # then atomically replaces. Applies on next bot restart.
    [ -z "$arg" ] || { echo "rejected: market-policy-set takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid policy payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | /root/dune-market-control.py policy-set
    exit $?
    ;;
  market-service)
    # Control the bot systemd unit. arg = start|stop|restart (validated here
    # AND in the control script). No stdin.
    case "$arg" in
      start|stop|restart) ;;
      *) echo "rejected: market-service verb must be start|stop|restart" >&2; exit 2 ;;
    esac
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-market-control.py service "$arg"
    ;;
  market-buy)
    # Portal Market BUY WRITE path. Base64-encoded JSON job on stdin (same safe
    # alphabet as grant-stdin/chat-send -- no whitespace / ';' / '$' / backtick,
    # so shell injection is impossible). Decoded JSON is piped to the writer's
    # --stdin-json mode; the writer re-validates that every field {order_id,
    # revision, buyer_ctrl, count, max_orders?} is a positive integer and runs
    # the single funding+fulfill transaction. The decoded payload never crosses
    # a shell -- it travels base64 -> stdin -> the python script. buyer_ctrl is
    # resolved server-side by the admin-backend before it reaches here; this layer
    # only relays. Source-IP allowlist is unchanged (enforced globally above).
    [ -z "$arg" ] || { echo "rejected: market-buy takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid market-buy payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /root/dune-market-buy.py --stdin-json
    exit $?
    ;;
  market-sell)
    # Portal Market SELL WRITE path. Base64-encoded JSON job on stdin (same safe
    # alphabet as market-buy/grant-stdin -- no whitespace / ';' / '$' / backtick,
    # so shell injection is impossible). Decoded JSON is piped to the writer's
    # --stdin-json mode; the writer re-validates that {seller_ctrl, item_id, count,
    # price, duration_days, max_orders?} are positive integers (duration in
    # {1,3,7,14}) and the optional expected_template is allowlisted, then runs the
    # single OFFLINE-GATED funding+add_sell_order transaction. The decoded payload
    # never crosses a shell -- base64 -> stdin -> the python script. seller_ctrl and
    # the owned-inventory check are resolved server-side by the admin-backend before
    # it reaches here; this layer only relays (the writer re-verifies ownership as
    # defense in depth). Source-IP allowlist is unchanged (enforced globally above).
    [ -z "$arg" ] || { echo "rejected: market-sell takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid market-sell payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /root/dune-market-sell.py --stdin-json
    exit $?
    ;;
  market-cancel|market-relist)
    # Portal Market CANCEL / RELIST WRITE path. Base64-encoded JSON job on stdin
    # (same safe alphabet as market-buy/sell -- no whitespace / ';' / '$' / backtick,
    # so shell injection is impossible). Decoded JSON is piped to the orders writer's
    # --stdin-json mode; the writer re-validates {action, owner_ctrl, order_id,
    # revision, price?, duration_days?} (positive ints; relist duration in {1,3,7,14})
    # and runs the single ONLINE-SAFE cancel/relist transaction (owner + revision
    # guarded). owner_ctrl is resolved server-side by the admin-backend before it
    # reaches here; the writer re-verifies ownership as defense in depth. The action
    # field inside the payload selects cancel vs relist; both tokens route here.
    # Source-IP allowlist is unchanged (enforced globally above).
    [ -z "$arg" ] || { echo "rejected: $action takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid $action payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /root/dune-market-orders.py --stdin-json
    exit $?
    ;;
  blueprint-rename)
    # Portal "My Bases" blueprint RENAME WRITE path. Base64-encoded JSON job on stdin
    # (same safe alphabet as storage-withdraw/market-sell -- no whitespace / ';' / '$' /
    # backtick, so shell injection is impossible). Decoded JSON is piped to the rename
    # writer's --stdin-json mode; the writer re-validates {account_id, bp_id, name} (ints +
    # a length/charset-capped name), OFFLINE-gates the player, re-verifies the blueprint is
    # OWNED by account_id, and jsonb_set's the one BuildingBlueprintName field. The arbitrary
    # name never crosses a shell -- it travels base64 -> stdin -> python, and the writer
    # re-encodes it to base64 before it touches SQL. account_id is resolved server-side by
    # the admin-backend; the writer re-verifies ownership as defense in depth. Source-IP
    # allowlist is unchanged (enforced globally above).
    [ -z "$arg" ] || { echo "rejected: blueprint-rename takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid blueprint-rename payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /root/dune-blueprint-rename.py --stdin-json
    exit $?
    ;;
  storage-withdraw|storage-deposit)
    # Portal Storage Manager WITHDRAW / DEPOSIT WRITE path. Base64-encoded JSON job on
    # stdin (same safe alphabet as market-buy/sell/cancel -- no whitespace / ';' / '$' /
    # backtick, so shell injection is impossible). Decoded JSON is piped to the storage
    # writer's --stdin-json mode; the writer re-validates {action, owner_ctrl, amount?,
    # mode?} (positive ints; withdraw amount capped at 100000; deposit mode in
    # {sweep,amount}) and runs the single OFFLINE-GATED currency<->coin transaction.
    # The decoded payload never crosses a shell -- base64 -> stdin -> the python script.
    # owner_ctrl is resolved server-side by the admin-backend before it reaches here; the
    # writer re-verifies offline + ownership (owned_inv_sql) as defense in depth. The
    # action field inside the payload selects withdraw vs deposit; both tokens route here.
    # Source-IP allowlist is unchanged (enforced globally above).
    [ -z "$arg" ] || { echo "rejected: $action takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid $action payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /root/dune-storage-write.py --stdin-json
    exit $?
    ;;
  storage-move)
    # Portal Storage Manager drag-drop MOVE WRITE path (Tier 3). Base64-encoded JSON job
    # on stdin (same safe alphabet as storage-withdraw/market-sell -- no whitespace / ';' /
    # '$' / backtick, so shell injection is impossible). Decoded JSON is piped to the storage
    # writer's --stdin-json mode; the writer re-validates {action:'move', owner_ctrl, item_id,
    # dst_inventory_id, expected_template?} (positive ints; template charset-capped) and runs
    # the single OFFLINE-GATED slot+volume-gated re-home transaction. The decoded payload
    # never crosses a shell -- base64 -> stdin -> the python script. owner_ctrl is resolved
    # server-side by the admin-backend before it reaches here; the writer re-verifies offline +
    # ownership (owned_inv_sql) of BOTH source and destination + the DeepDesert exclusion as
    # defense in depth. Kill-switch: the writer refuses with move_disabled unless
    # LASTSIETCH_STORAGE_MOVE_ENABLED=1. NEVER restarts game pods/BGD/k3s (psql into the running DB
    # only). Source-IP allowlist is unchanged (enforced globally above).
    [ -z "$arg" ] || { echo "rejected: storage-move takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid storage-move payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /root/dune-storage-write.py --stdin-json
    exit $?
    ;;
  repair-box|repair-gear|repair-all|repair-vehicle)
    # Portal Item Repair WRITE path. Base64-encoded JSON job on stdin (same safe
    # alphabet as storage-withdraw/market-sell -- no whitespace / ';' / '$' / backtick,
    # so shell injection is impossible). Decoded JSON is piped to the repair writer's
    # --stdin-json mode; the writer re-validates {action, owner_ctrl, inv_id?} (positive
    # ints; box requires inv_id) and runs the single OFFLINE-GATED durability repair
    # (repair-box/repair-gear = vanilla top-up; repair-all = factory refurbish). The
    # decoded payload never crosses a shell -- base64 -> stdin -> the python script.
    # owner_ctrl is resolved server-side by the admin-backend before it reaches here; the
    # writer re-verifies offline + ownership (owned_inv_sql / pawn inventories) as defense
    # in depth. The action field inside the payload selects box/gear/everything; all three
    # tokens route here. NEVER restarts game pods/BGD/k3s (psql into the running DB only).
    # Source-IP allowlist is unchanged (enforced globally above).
    [ -z "$arg" ] || { echo "rejected: $action takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid $action payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | base64 -d | /root/dune-repair-write.py --stdin-json
    exit $?
    ;;
  reward-op)
    # Login-rewards WRITE path (V2). Base64-encoded JSON job on stdin (same safe
    # alphabet as gift-op/item-transfer-op -- no whitespace / ';' / '$' / backtick,
    # so shell injection is impossible). Handed to dune-reward-op.sh's
    # --op-b64-stdin mode, which re-validates every field {idempotency_key,
    # account_id, reward_kind, amount|template_id, quality_level}, writes the
    # idempotency + audit + per-account cap ledger dune.ls_reward_claims, then:
    #   daily_solari => +credit via dune.adjust_player_virtual_currency_balance
    #                   (positive delta, online-safe, one atomic txn);
    #   weekly_item  => G29 bank mint via dune-grant.sh (inv_type 30, online-safe).
    # DARK by default (LASTSIETCH_REWARD_ENABLED=0): while off it refuses cleanly with
    # status:"deferred". account_id is resolved server-side by the admin-backend
    # from the session before it reaches here. Source-IP allowlist is unchanged.
    [ -z "$arg" ] || { echo "rejected: reward-op takes no args" >&2; exit 1; }
    [ -z "${_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    payload=$(cat)
    payload="${payload//[[:space:]]/}"
    if [[ ! "$payload" =~ ^[A-Za-z0-9+/=]+$ ]]; then
      echo "rejected: invalid reward-op payload" >&2
      exit 2
    fi
    printf '%s' "$payload" | /root/dune-reward-op.sh --op-b64-stdin
    exit $?
    ;;
  *)
    echo "rejected: unknown action" >&2
    exit 1
    ;;
esac
