import os

RELAY_URL = os.environ.get("LASTSIETCH_RELAY_URL", "")  # set to your relay base URL; empty = disabled
# VC0: separate relay for Conan/Enshrouded (+ the generic /games/* + /infra/*
# surface) while Dune relocates to the localhost relay on <web-host>. Defaults
# to the relay host Tailscale URL so existing deploys keep working.
RELAY_URL_GAMES = os.environ.get("LASTSIETCH_RELAY_URL_GAMES", "")  # set to your relay base URL; empty = disabled
RELAY_API_KEY = os.environ["LASTSIETCH_RELAY_API_KEY"]
SESSION_SECRET = os.environ["LASTSIETCH_SESSION_SECRET"]
DB_PATH = os.environ.get("LASTSIETCH_DB_PATH", "/opt/lastsietch-admin/admin.db")
# Sietch community blueprint market: published blueprint JSON blobs live on disk
# (off-row, one file per publish '<publish_id>.json') so admin.db stays small and
# the ~448 KB-class JSON streams straight from disk on download. Metadata lives in
# admin.db (portal_blueprint_market). Filename is always integer-derived; request
# input never enters the path.
BLUEPRINT_BLOB_DIR = os.environ.get(
    "LASTSIETCH_BLUEPRINT_BLOB_DIR",
    os.path.join(os.path.dirname(DB_PATH), "blueprints"))
# Per-account rolling-24h publish cap (windowed COUNT over the durable
# portal_blueprint_publish_log; survives an lastsietch-admin restart).
BLUEPRINT_PUBLISH_DAILY_CAP = int(os.environ.get("LASTSIETCH_BLUEPRINT_PUBLISH_DAILY_CAP", "10"))
# Per-player rolling-24h Solido import cap (windowed COUNT over the audit_log
# portal_solido_import rows; each import materializes a new in-game item).
BLUEPRINT_IMPORT_DAILY_CAP = int(os.environ.get("LASTSIETCH_BLUEPRINT_IMPORT_DAILY_CAP", "10"))
# Portal item-repair caps (windowed COUNT over audit_log rows, per account_id).
# The per-box "Repair Items" and "Repair Backpack & Equipped" tiers SHARE one
# rolling bucket (action portal_repair); the once-per-day "Repair & Refurbish
# Everything" tier is its own bucket (action portal_repair_everything). Advisory
# convenience gates, not security — the writer enforces ownership + offline.
REPAIR_BOX_WINDOW_MIN = int(os.environ.get("LASTSIETCH_REPAIR_BOX_WINDOW_MIN", "120"))
REPAIR_BOX_CAP = int(os.environ.get("LASTSIETCH_REPAIR_BOX_CAP", "3"))
REPAIR_ALL_WINDOW_HR = int(os.environ.get("LASTSIETCH_REPAIR_ALL_WINDOW_HR", "24"))
REPAIR_ALL_CAP = int(os.environ.get("LASTSIETCH_REPAIR_ALL_CAP", "1"))
# Kill-switch for the once-per-day "Repair & Refurbish Everything" tier.
# Re-enabled 2026-07-01 after verifying (live, all 21,982 durability items) that the
# refurbish GREATEST(MaxDurability|100, Current, Decayed) NEVER lowers a ceiling (0
# lowered, 0 of 74 high-durability 200-tier items lowered) and never touches augment
# data. The only artifact is a cosmetic ceiling bump on augmented items (96->100 in DB)
# which the engine re-caps on load. Set LASTSIETCH_REPAIR_ALL_ENABLED=0 to kill-switch again.
REPAIR_ALL_ENABLED = os.environ.get("LASTSIETCH_REPAIR_ALL_ENABLED", "1") == "1"
# Per-vehicle in-place "Refurbish Vehicle" tier: reverses max-durability decay on the
# mounted parts of ONE owned vehicle (no dismounting). Same refurbish SQL as
# Repair-Everything but scoped to a single vehicle's inventories; own rolling cap
# matching the box-repair cadence. Set LASTSIETCH_REPAIR_VEHICLE_ENABLED=0 to kill-switch.
REPAIR_VEHICLE_ENABLED = os.environ.get("LASTSIETCH_REPAIR_VEHICLE_ENABLED", "1") == "1"
# Per-container "Repair box" tier. PULLED 2026-08-03 (LASTSIETCH_REPAIR_BOX_ENABLED=0 on
# <web-host>): a container's contents are held in the server's memory for as long as
# the base is loaded, so every box repair lands in Postgres and is invisible in-game
# until the next server restart. The button reported success while nothing changed for
# the player. Set LASTSIETCH_REPAIR_BOX_ENABLED=1 to restore it.
REPAIR_BOX_ENABLED = os.environ.get("LASTSIETCH_REPAIR_BOX_ENABLED", "1") == "1"
REPAIR_VEHICLE_WINDOW_MIN = int(os.environ.get("LASTSIETCH_REPAIR_VEHICLE_WINDOW_MIN", "120"))
REPAIR_VEHICLE_CAP = int(os.environ.get("LASTSIETCH_REPAIR_VEHICLE_CAP", "3"))
# Player self-service "Download my data" export (portal). Ships DARK: the route
# returns {ok:true, status:"deferred"} until LASTSIETCH_EXPORT_ENABLED=1. Read-only; the
# player only ever exports their OWN selected character (account + controller are
# resolved server-side from the session, never trusted from the client).
EXPORT_ENABLED = os.environ.get("LASTSIETCH_EXPORT_ENABLED", "0") == "1"
# Multi-account linking (portal): one Discord links + switches between several game
# accounts. Ships DARK behind LASTSIETCH_MULTIACCOUNT_ENABLED (gates the "Link another
# account" entrypoint + the V2 account switcher). MULTIACCOUNT_REQUIRE_QUIZ=0
# (default: "trust after first link") lets an already-verified Discord add another
# account WITHOUT re-running the 3-question ownership quiz; set to 1 to force the
# quiz on every added account. The add path ALWAYS refuses an account already
# actively linked (to this Discord or anyone else), so it can never steal a claimed
# account; with REQUIRE_QUIZ=0 an added account is only guaranteed to be UNCLAIMED,
# not proven-owned (residual: a verified user could claim an unclaimed character).
MULTIACCOUNT_ENABLED = os.environ.get("LASTSIETCH_MULTIACCOUNT_ENABLED", "0") == "1"
MULTIACCOUNT_REQUIRE_QUIZ = os.environ.get("LASTSIETCH_MULTIACCOUNT_REQUIRE_QUIZ", "0") == "1"
# Requires the separately rehearsed profile schema and backfill before activation.
PORTAL_PROFILES_ENABLED = os.environ.get("LASTSIETCH_PORTAL_PROFILES_ENABLED", "0") == "1"
PORTAL_AUTH_ENABLED = os.environ.get("LASTSIETCH_PORTAL_AUTH_ENABLED", "0") == "1"
PORTAL_GAME_AUTH_ENABLED = os.environ.get("LASTSIETCH_PORTAL_GAME_AUTH_ENABLED", "0") == "1"
PORTAL_AUTH_ORIGIN = os.environ.get("LASTSIETCH_PORTAL_AUTH_ORIGIN", "")  # https://<your portal hostname>
PORTAL_AUTH_RP_ID = os.environ.get("LASTSIETCH_PORTAL_AUTH_RP_ID", "")  # your portal hostname, e.g. portal.example.org; empty = passkeys disabled
# Ingot Refinery (portal Storage > Workshop): the offline ingot-plus-melange to
# spiced-dust trade. Ships DARK. Both switches must be on for a trade to land:
# this one decides whether the portal offers the door, and
# /etc/lastsietch/refinery-enabled on the game host is what the writer itself reads.
#
# routers/portal_refinery.py resolves the gate through
# feature_flags.enabled("LASTSIETCH_REFINERY_ENABLED", "0") on EVERY request, so the
# owner's Systems toggle lands without a restart. This constant is the coded
# default in the form the deploy guards grep for; do not wire it into the route
# or the live toggle stops working.
REFINERY_ENABLED = os.environ.get("LASTSIETCH_REFINERY_ENABLED", "0") == "1"
SESSION_LIFETIME_HOURS = 8
BCRYPT_ROUNDS = 12
MIN_PASSWORD_LENGTH = 16
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 60
LOCKOUT_THRESHOLD = 10
LOCKOUT_MINUTES = 15
# Read-model mirror (portal/admin fast local read layer). The bg sync loop pulls
# /dune/read-models via the relay into MIRROR_DB_PATH; PORTAL_MIRROR_READS gates
# whether the read path consults it (default off until parity-verified); a row
# older than MIRROR_MAX_STALE falls back to the live relay.
MIRROR_DB_PATH = os.environ.get(
    "LASTSIETCH_MIRROR_DB_PATH",
    os.path.join(os.path.dirname(DB_PATH), "mirror.sqlite"))  # beside admin.db, like the others
MIRROR_PULL_INTERVAL = int(os.environ.get("LASTSIETCH_MIRROR_PULL_INTERVAL", "30"))
MIRROR_MAX_STALE = int(os.environ.get("LASTSIETCH_MIRROR_MAX_STALE", "300"))
PORTAL_MIRROR_READS = os.environ.get("LASTSIETCH_PORTAL_MIRROR_READS", "0") == "1"

# V2 Exchange price-history: its OWN SQLite file, not admin.db. The flat 21 d x
# 10 min series plateaued at 4.0M rows / ~820 MB, which was 92% of admin.db, so
# every nightly VACUUM INTO snapshot, restic delta and deploy-time cp backup of
# the auth/session database paid for a sparkline. market_history.py keeps three
# tiers (48 h raw, 60 d hourly, 365 d daily) in this file instead, ~175 MB steady
# state. Defaults beside DB_PATH so a host only has to set LASTSIETCH_DB_PATH.
MARKET_HISTORY_DB_PATH = os.environ.get(
    "LASTSIETCH_MARKET_HISTORY_DB_PATH",
    os.path.join(os.path.dirname(DB_PATH), "market_history.db"))

# Market price-alert watchlist (portal feature). The watcher diffs the local
# market mirror for player-set thresholds and fires alerts (portal bell + a
# Cielago DM). MARKET_WATCH_ENABLED gates the background loop (default off until
# verified, mirroring the mirror-reads rollout); PORTAL_ALERT_POLL_KEY guards the
# /_internal/market-alerts endpoint Cielago polls for pending DMs.
MARKET_WATCH_ENABLED = os.environ.get("LASTSIETCH_MARKET_WATCH_ENABLED", "0") == "1"
MARKET_WATCH_INTERVAL = int(os.environ.get("LASTSIETCH_MARKET_WATCH_INTERVAL", "60"))
MARKET_WATCH_MAX_PER_ACCOUNT = int(os.environ.get("LASTSIETCH_MARKET_WATCH_MAX_PER_ACCOUNT", "25"))
PORTAL_ALERT_POLL_KEY = os.environ.get("LASTSIETCH_PORTAL_ALERT_POLL_KEY", "")

# Event reminders (portal events, wave 9). An in-process loop mails a T-1h
# mailbox notification for every due portal_event_reminders row. Default ON: the
# feature is worthless without delivery, and a tick with no rows costs one
# indexed SELECT. EVENT_REMINDERS_ENABLED is the kill switch.
EVENT_REMINDERS_ENABLED = os.environ.get("LASTSIETCH_EVENT_REMINDERS_ENABLED", "1") == "1"
EVENT_REMINDER_INTERVAL = int(os.environ.get("LASTSIETCH_EVENT_REMINDER_INTERVAL", "60"))

SAMPLE_INTERVAL_SEC = int(os.environ.get("LASTSIETCH_SAMPLE_INTERVAL_SEC", "60"))
FAST_INTERVAL_SEC = int(os.environ.get("LASTSIETCH_FAST_INTERVAL_SEC", "10"))
FAST_DURATION_SEC = int(os.environ.get("LASTSIETCH_FAST_DURATION_SEC", "3600"))
METRICS_RETENTION_DAYS = int(os.environ.get("LASTSIETCH_METRICS_RETENTION_DAYS", "7"))

# Discord posting (orchestrator-only, optional; orchestrator skips Discord if missing).
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CH_BOTLOGS = os.environ.get("DISCORD_CH_BOTLOGS", "")
# Fremkit wave 9 events channel. Cross-posting a published event is opt-in: when
# this is empty nothing is posted, and it NEVER falls back to botlogs (botlogs is
# an operator channel, the event announcement is player-facing copy).
DISCORD_CH_EVENTS = os.environ.get("DISCORD_CH_EVENTS", "")

# Portal (P4) — Discord OAuth + identity-quiz link surface. Required for
# /portal/* routes. Empty strings are tolerated at import time so the admin
# surface keeps booting; the portal routes themselves 503 if anything is
# missing at request time.
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_OAUTH_REDIRECT_URI = os.environ.get("DISCORD_OAUTH_REDIRECT_URI", "")  # https://<your portal hostname>/portal/oauth/callback
# Hosts that serve the V2 portal at their ROOT (wave 13a). Compared against the
# Host header exactly, so a port or a different case is a different host; the
# comma list exists so a staging address can be added without a code change.
PORTAL_ROOT_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get("PORTAL_ROOT_HOSTS", "").split(",")
    if h.strip()
)
PORTAL_OAUTH_STATE_SECRET = os.environ.get("PORTAL_OAUTH_STATE_SECRET", "")
PORTAL_SESSION_SECRET = os.environ.get("PORTAL_SESSION_SECRET", "")
PORTAL_DISCORD_INVITE_URL = os.environ.get(
    "PORTAL_DISCORD_INVITE_URL", "https://discord.gg/your-invite"
)

PORTAL_OAUTH_STATE_MAX_AGE = 600          # 10 minutes
PORTAL_LINK_FLOW_MAX_AGE = 300            # 5 minutes between OAuth and quiz pass
PORTAL_SESSION_IDLE_MAX_AGE = 7 * 86400   # 7 days idle
PORTAL_SESSION_ABS_MAX_AGE = 30 * 86400   # 30 days absolute
PORTAL_OAUTH_HTTP_TIMEOUT = 10.0          # httpx timeout for Discord exchange + identity

# Rate-limit tiers (rolling-window counts via SQLite query on portal_link_attempts).
PORTAL_RATE_STARTS_PER_IP_PER_HOUR = 5
PORTAL_RATE_CALLBACKS_PER_IP_PER_HOUR = 5
PORTAL_RATE_ATTEMPTS_PER_DISCORD_PER_DAY = 3
PORTAL_RATE_ATTEMPTS_PER_ACCOUNT_PER_DAY = 5
PORTAL_RATE_FAILS_TO_COOLDOWN = 3
PORTAL_RATE_COOLDOWN_HOURS = 24
