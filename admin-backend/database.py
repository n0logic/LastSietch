import sqlite3
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'operator')) DEFAULT 'operator',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by INTEGER REFERENCES users(id),
    locked_until TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    last_login TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    discord_id TEXT                      -- portal role mapping; set by an operator, never inferred
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    user_agent TEXT,
    last_activity TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    username TEXT,
    action TEXT NOT NULL,
    target TEXT,
    ip_address TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    details TEXT,
    success INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS metrics (
    game_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    proc_mb INTEGER,
    vm_used_mb INTEGER,
    vm_total_mb INTEGER,
    PRIMARY KEY (game_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_metrics_game_ts ON metrics(game_id, ts DESC);

-- Portal P4 (Discord OAuth + identity-quiz link surface). Lives in admin-backend
-- SQLite (NOT dune.*) per the custom-table ownership rule. Soft-delete via
-- revoked_at; every active-link read MUST include WHERE revoked_at IS NULL.
CREATE TABLE IF NOT EXISTS ls_account_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id      TEXT    NOT NULL,
    account_id      INTEGER NOT NULL,
    fls_id          TEXT,
    character_name  TEXT    NOT NULL,
    discord_handle  TEXT    NOT NULL,
    linked_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_session_at TEXT,
    revoked_at      TEXT,
    revoked_by      TEXT,
    revoke_reason   TEXT,
    UNIQUE (discord_id, account_id),
    CHECK (discord_id GLOB '[0-9]*'),
    CHECK (account_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_ls_account_links_discord
    ON ls_account_links(discord_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ls_account_links_account
    ON ls_account_links(account_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS portal_link_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id        TEXT,
    account_id        INTEGER,
    state_token       TEXT,
    state_issued_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    state_consumed_at TEXT,
    callback_hit_at   TEXT,
    pick_token        TEXT,
    q1_kind           TEXT,
    q1_correct_hash   TEXT,
    q2_kind           TEXT,
    q2_correct_hash   TEXT,
    q3_kind           TEXT,
    q3_correct_hash   TEXT,
    attempt_at        TEXT,
    result            TEXT,
    ip_addr           TEXT,
    user_agent        TEXT,
    is_test_run       INTEGER NOT NULL DEFAULT 0
);
-- Partial UNIQUE indexes prevent state_token / pick_token re-use while still
-- allowing many NULL rows (different attempt families coexist in one table).
CREATE UNIQUE INDEX IF NOT EXISTS uq_portal_attempts_state_token
    ON portal_link_attempts(state_token) WHERE state_token IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_portal_attempts_pick_token
    ON portal_link_attempts(pick_token) WHERE pick_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_portal_attempts_discord_recent
    ON portal_link_attempts(discord_id, state_issued_at);
CREATE INDEX IF NOT EXISTS idx_portal_attempts_ip_recent
    ON portal_link_attempts(ip_addr, state_issued_at);
CREATE INDEX IF NOT EXISTS idx_portal_attempts_ip_callback
    ON portal_link_attempts(ip_addr, callback_hit_at)
    WHERE callback_hit_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_portal_attempts_account_recent
    ON portal_link_attempts(account_id, attempt_at)
    WHERE attempt_at IS NOT NULL;

-- Market price-alert watchlist (portal feature; read-only on the exchange).
-- A player sets "notify me when <item> is listed at/below <max_price>". The
-- background watcher (market_watch.py) diffs the local market mirror and, when
-- the cheapest current listing crosses the threshold, fires one alert row and
-- disarms the watch; it re-arms once the cheapest price rises back above the
-- threshold. Lives in admin.db (NOT dune.*) per the custom-table-ownership rule.
CREATE TABLE IF NOT EXISTS portal_market_watch (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id        TEXT    NOT NULL,
    account_id        INTEGER NOT NULL,
    template_id       TEXT    NOT NULL,
    name_cached       TEXT,
    max_price         INTEGER NOT NULL CHECK (max_price > 0),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    armed             INTEGER NOT NULL DEFAULT 1,   -- 1 = waiting to fire; 0 = fired, awaiting re-arm
    last_match_price  INTEGER,                      -- cheapest price observed at last fire
    last_checked_at   TEXT,
    last_triggered_at TEXT,
    UNIQUE (account_id, template_id),
    CHECK (account_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_pmw_template ON portal_market_watch(template_id);
CREATE INDEX IF NOT EXISTS idx_pmw_account ON portal_market_watch(account_id);

-- Fired alerts: one row per crossing. seen_in_portal drives the portal bell;
-- dm_sent drives the Cielago Discord DM delivery (pulled via /_internal/market-alerts).
CREATE TABLE IF NOT EXISTS portal_market_alert (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id        INTEGER NOT NULL,
    discord_id      TEXT    NOT NULL,
    account_id      INTEGER NOT NULL,
    template_id     TEXT    NOT NULL,
    name_cached     TEXT,
    threshold_price INTEGER NOT NULL,
    match_price     INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    seen_in_portal  INTEGER NOT NULL DEFAULT 0,
    dm_sent         INTEGER NOT NULL DEFAULT 0,
    dm_note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pma_account_unseen
    ON portal_market_alert(account_id) WHERE seen_in_portal = 0;
CREATE INDEX IF NOT EXISTS idx_pma_dm_pending
    ON portal_market_alert(id) WHERE dm_sent = 0;

-- Guild recruiting flag + portal-authored blurb. OUR metadata (NOT game-state):
-- a guild Leader/Officer (verified via their linked controller_id holding
-- role_id 1/50 in dune.guild_members) marks their guild "recruiting" and writes
-- a short recruitment blurb + contact note. Surfaced read-only to all players on
-- the portal Guild directory. The actual guild join still happens in-game; this
-- is signalling only. One row per guild. Lives in admin.db (NOT dune.*) per the
-- custom-table-ownership rule.
CREATE TABLE IF NOT EXISTS portal_guild_recruiting (
    guild_id     INTEGER PRIMARY KEY,            -- game-side dune.guilds.guild_id
    recruiting   INTEGER NOT NULL DEFAULT 0,     -- 1 = open / listed; 0 = closed
    blurb        TEXT,                           -- portal-authored "about us" (RP, playstyle, etc.)
    contact_note TEXT,                           -- optional extra contact (Discord invite, etc.)
    set_by_account_id INTEGER,                   -- who last edited (controller_id of the editor)
    set_by_discord_id TEXT,
    set_by_char_name  TEXT,
    -- Structured Signal-Board filter columns (social layer Tier 2). All optional.
    playstyle    TEXT,                           -- e.g. 'PvE' | 'PvP' | 'RP' | 'casual'
    timezone     TEXT,                           -- e.g. 'NA-East', 'EU', 'OCE'
    language     TEXT,                           -- primary guild language
    new_player_friendly INTEGER NOT NULL DEFAULT 0,  -- 1 = welcomes new players
    discord_url  TEXT,                           -- optional guild Discord invite
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (guild_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_pgr_recruiting
    ON portal_guild_recruiting(recruiting) WHERE recruiting = 1;

-- Solo LFG "Seeker Wall" (social layer Tier 2). A player without a guild posts a
-- self-authored "looking for a guild" card. Self-post / self-edit / expiry; one
-- row per player (account_id PK). account_id comes from the session, never the
-- client, and is NEVER emitted to other players (char_name is the display key).
-- Lives in admin.db (NOT dune.*) per the custom-table-ownership rule.
CREATE TABLE IF NOT EXISTS portal_lfg_seekers (
    account_id  INTEGER PRIMARY KEY,             -- ls_account_links.account_id (one post per player)
    char_name   TEXT,
    playstyle   TEXT,
    timezone    TEXT,
    role        TEXT,                            -- free-text playstyle/role tag
    note        TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT,                            -- NULL or UTC 'YYYY-MM-DD HH:MM:SS'
    CHECK (account_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_lfg_active ON portal_lfg_seekers(expires_at);

-- Guild join-requests (social layer Tier 2). The CANONICAL join-request record
-- lives ONLY here (the mailbox stores no join-request lifecycle state). A linked
-- player raises a request against a guild; an Officer/Leader of that guild sees
-- it and fires the (dark) send_invite op. Lives in admin.db.
CREATE TABLE IF NOT EXISTS portal_guild_join_requests (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_account_id  INTEGER NOT NULL,
    requester_char_name   TEXT,
    requester_discord_id  TEXT,
    guild_id              INTEGER NOT NULL,
    note                  TEXT,
    status                TEXT NOT NULL DEFAULT 'pending',  -- pending|invited|dismissed|expired
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at            TEXT,
    acted_by_account_id   INTEGER,
    CHECK (requester_account_id > 0),
    CHECK (guild_id > 0)
);
-- One live pending request per (guild, requester); invited/dismissed/expired don't block a re-request.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pgjr_one_pending
    ON portal_guild_join_requests(guild_id, requester_account_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_pgjr_guild_pending
    ON portal_guild_join_requests(guild_id, status);

-- Portal mailbox (social layer Tier 3). Player + guild inbox surface for
-- notifications, player-to-player DMs, and gift receipts. Sender is ALWAYS
-- derived from the session (spoofed-sender defense); account_id is never emitted
-- to the client. Guild-inbox read-state is GUILD-LEVEL (one shared row per guild
-- message). Lives in admin.db.
CREATE TABLE IF NOT EXISTS portal_messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_kind         TEXT NOT NULL CHECK (sender_kind IN ('player','guild','system')),
    sender_account_id   INTEGER,                 -- NULL for system/guild
    sender_char_name    TEXT,
    recipient_kind      TEXT NOT NULL CHECK (recipient_kind IN ('player','guild')),
    recipient_id        INTEGER NOT NULL,        -- account_id (player) OR guild_id (guild)
    subject             TEXT,
    body                TEXT,
    kind                TEXT NOT NULL CHECK (kind IN ('notification','user','gift')),
    payload             TEXT NOT NULL DEFAULT '{}',
    state               TEXT NOT NULL DEFAULT 'unread'
                          CHECK (state IN ('unread','read','actioned','claimed','declined')),
    read_at             TEXT,
    read_by_account_id  INTEGER,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT,
    deleted_at          TEXT,
    CHECK (recipient_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_pmsg_inbox
    ON portal_messages(recipient_kind, recipient_id, created_at)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_pmsg_unread
    ON portal_messages(recipient_kind, recipient_id)
    WHERE state = 'unread' AND deleted_at IS NULL;

-- Per-guild inbox visibility/management thresholds. Only a Leader (100) edits it.
CREATE TABLE IF NOT EXISTS portal_guild_inbox_config (
    guild_id            INTEGER PRIMARY KEY,
    view_min_role       INTEGER NOT NULL DEFAULT 50,   -- min role to LIST/READ the guild inbox
    manage_min_role     INTEGER NOT NULL DEFAULT 100,  -- min role to accept/decline/delete
    set_by_account_id   INTEGER,
    set_by_char_name    TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (guild_id > 0)
);

-- Player DM block list. (recipient has blocked sender): a DM from
-- blocked_account_id to account_id is refused.
CREATE TABLE IF NOT EXISTS portal_message_block (
    account_id          INTEGER NOT NULL,        -- who set the block
    blocked_account_id  INTEGER NOT NULL,        -- who is blocked from DMing them
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, blocked_account_id)
);

-- Public player directory + DM visibility. Default PUBLIC: every LINKED player
-- (ls_account_links) is listed + cold-DMable UNLESS they have a row here with
-- listed=0 (the one-click opt-out). No row = public. blurb is an optional short
-- tagline. account_id from the session, NEVER emitted to clients (char_name is
-- the only public handle). admin.db only (NOT dune.*).
CREATE TABLE IF NOT EXISTS portal_player_profile (
    account_id  INTEGER PRIMARY KEY,
    char_name   TEXT,
    listed      INTEGER NOT NULL DEFAULT 1,      -- 1 = public/discoverable, 0 = opted out
    blurb       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Private per-player map waypoints (notes/pins). Keyed to the portal account_id
-- (from ls_account_links) and the map key (e.g. 'deep-desert', 'hagga'). Coords
-- stored as normalized 0..1000 VIEW units (same system as the static markers) so
-- the JS can plot them without an extra projection step. note is optional.
-- Lives in admin.db (NOT dune.*) per the custom-table-ownership rule.
CREATE TABLE IF NOT EXISTS portal_map_waypoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,                  -- ls_account_links.account_id
    map_key     TEXT    NOT NULL,                  -- 'deep-desert' | 'hagga'
    nx          REAL    NOT NULL,                  -- normalized x 0..1000
    ny          REAL    NOT NULL,                  -- normalized y 0..1000
    note        TEXT,                              -- optional player note
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (nx >= 0 AND nx <= 1000 AND ny >= 0 AND ny <= 1000)
);
CREATE INDEX IF NOT EXISTS idx_pmw_account_map
    ON portal_map_waypoints(account_id, map_key);

-- Self-rescue ("I'm stuck") teleport log. One row per successful portal rescue,
-- keyed to the portal account_id. Backs the DURABLE 1/hour cooldown via a windowed
-- COUNT(*) WHERE account_id=? AND used_at >= now-1h (mirrors portal_rate_limit's
-- windowed idiom). Durable across restarts so a deploy can't reset the cooldown
-- and let a player re-rescue immediately. Lives in admin.db (NOT dune.*) per the
-- custom-table-ownership rule. used_at stored as UTC 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS portal_rescue_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,                  -- ls_account_links.account_id
    used_at     TEXT    NOT NULL,                  -- UTC timestamp of the rescue
    from_x      INTEGER,                           -- player position before teleport
    from_y      INTEGER,
    from_map    TEXT,                              -- map the player was on
    to_base_x   INTEGER,                           -- chosen destination base totem
    to_base_y   INTEGER,
    CHECK (account_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_prl_account_used
    ON portal_rescue_log(account_id, used_at);

-- Per-Coriolis-cycle accumulation of Large spice candidate sectors observed from
-- the live RAM reader. The on-box ramcache only instantiates ~3 candidate fields
-- at a time (1 active + 2 dormant) and rotates them across the cycle's sites, so
-- a single read sees a small subset. The portal already polls the live spice feed
-- (~90s); we union each poll's candidate sectors here, keyed by cycle, so the map
-- can plot EVERY candidate site seen this cycle rather than just the current few.
-- Freeze-safe: pure <web-host>-side accumulation of data the relay already
-- returns (no game-box change). Lives in admin.db (NOT dune.*) per the
-- custom-table-ownership rule. cycle_key = Coriolis cycle index (see
-- spice_candidates_acc.py). first/last_utc stored as ISO8601 UTC.
CREATE TABLE IF NOT EXISTS spice_candidate_acc (
    cycle_key   TEXT NOT NULL,                        -- Coriolis cycle bucket
    dim         TEXT NOT NULL,                        -- DD dimension ('0'/'1')
    sector      TEXT NOT NULL,                        -- 9x9 grid sector, e.g. 'F1'
    first_utc   TEXT NOT NULL,                        -- first time this site was seen
    last_utc    TEXT NOT NULL,                        -- most recent sighting
    x           REAL,                                 -- exact world X (from reader; NULL until Phase 2)
    y           REAL,                                 -- exact world Y (from reader; NULL until Phase 2)
    PRIMARY KEY (cycle_key, dim, sector)
);
CREATE INDEX IF NOT EXISTS idx_sca_cycle
    ON spice_candidate_acc(cycle_key);

-- Last Sietch community blueprint market. A player publishes one of their OWN
-- bases (a BuildingBlueprint_CopyDevice item) to a public gallery. The blueprint
-- JSON is a SERVER-SIDE re-export via the ownership-gated relay (never client
-- upload), so every listing is authentic. The big JSON blob lives on disk at
-- BLUEPRINT_BLOB_DIR/<publish_id>.json; this row is metadata only. Author
-- identity + ownership are captured from the publisher's linked session at
-- publish time. Lives in admin.db (NOT dune.*) per the custom-table-ownership
-- rule. All timestamps UTC 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS portal_blueprint_market (
    publish_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    -- provenance (clamped from the session at publish; never client-trusted)
    account_id     INTEGER NOT NULL,            -- ls_account_links.account_id of publisher
    discord_id     TEXT,                         -- publisher's discord id (moderation trail)
    author_name    TEXT    NOT NULL,             -- character name = display author (snapshot)
    game_bp_id     INTEGER NOT NULL,             -- in-game blueprint/item id re-exported
    -- display metadata (server-derived + length-capped author text only)
    title          TEXT    NOT NULL,             -- defaults to blueprint name; editable, capped
    description    TEXT,                          -- optional, length-capped
    tags           TEXT    NOT NULL DEFAULT '[]', -- JSON array of derived tag strings
    faction        TEXT,                          -- dominant faction tint key for the card
    has_paid_pieces INTEGER NOT NULL DEFAULT 0,   -- any MTX_ piece present (display chip)
    -- piece census (from the re-exported blueprint; drives card + sort)
    instance_count   INTEGER NOT NULL DEFAULT 0,
    placeable_count  INTEGER NOT NULL DEFAULT 0,
    pentashield_count INTEGER NOT NULL DEFAULT 0,
    piece_count      INTEGER NOT NULL DEFAULT 0,  -- instances+placeables+pentashields
    -- blob bookkeeping
    blob_path      TEXT    NOT NULL DEFAULT '',  -- relative filename '<publish_id>.json'
    blob_bytes     INTEGER NOT NULL DEFAULT 0,
    -- engagement + lifecycle
    download_count INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'published', -- 'published' | 'unpublished' | 'removed'
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (account_id > 0),
    CHECK (game_bp_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_pbm_status_created
    ON portal_blueprint_market(status, created_at);
CREATE INDEX IF NOT EXISTS idx_pbm_status_downloads
    ON portal_blueprint_market(status, download_count);
CREATE INDEX IF NOT EXISTS idx_pbm_account
    ON portal_blueprint_market(account_id);
-- One LIVE listing per (account, game blueprint): re-publishing the same base
-- updates the existing row rather than duplicating. Partial unique index so
-- unpublished/removed rows don't block a fresh publish.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pbm_one_live
    ON portal_blueprint_market(account_id, game_bp_id)
    WHERE status = 'published';

-- Append-only publish-event log: backs the per-account daily publish rate limit
-- via COUNT(*) WHERE account_id=? AND published_at >= now-24h. Durable across
-- restarts (a deploy cannot reset the cap). Lives in admin.db.
CREATE TABLE IF NOT EXISTS portal_blueprint_publish_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    publish_id   INTEGER,                       -- resulting portal_blueprint_market row
    game_bp_id   INTEGER,
    published_at TEXT    NOT NULL,              -- UTC timestamp of the publish event
    CHECK (account_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_pbpl_account_time
    ON portal_blueprint_publish_log(account_id, published_at);

-- Login-rewards V2 read-side mirror of the game-host dune.ls_reward_claims
-- ledger. The GAME DB is the source of truth for idempotency (UNIQUE
-- idempotency_key) + the grant itself; this local table exists only so the
-- portal can render streak / calendar claim-state without a per-request
-- game-host read. A row is written here only after the writer confirms a real
-- grant (status applied/replay); a DARK 'deferred' never records a claim.
-- period_key is date_utc for daily_solari and ISO 'yyyy-Www' for weekly_item;
-- the composite UNIQUE index is a local double-claim pre-check (the deterministic
-- idempotency_key keyed on the same period is the real guard).
CREATE TABLE IF NOT EXISTS ls_reward_claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,
    account_id      INTEGER NOT NULL,
    reward_kind     TEXT    NOT NULL,      -- daily_solari | weekly_item
    period_key      TEXT    NOT NULL,      -- date_utc (daily) | yyyy-Www (weekly)
    amount          INTEGER,               -- Solari credited (daily_solari)
    template_id     TEXT,                  -- minted item (weekly_item)
    quality_level   INTEGER,               -- grade of the minted item
    status          TEXT    NOT NULL,      -- applied | replay
    detail          TEXT,                  -- json sidecar
    created_at      TEXT    NOT NULL,      -- iso-8601 UTC
    CHECK (account_id > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hrc_acct_kind_period
    ON ls_reward_claims(account_id, reward_kind, period_key);
CREATE INDEX IF NOT EXISTS idx_hrc_acct_created
    ON ls_reward_claims(account_id, created_at);

-- ==========================================================================
-- THE KARUM: player-to-player trade venue (SB-006 + SB-007).
--
-- A listing is a fixed-ask offer of ONE item stack the seller actually holds,
-- taken into escrow at listing time WHILE THE SELLER IS OFFLINE. Karum's own
-- state lives here; the game DB holds only the settlement effects, and it owns
-- idempotency through UNIQUE correlation_id keys on its own ledgers. This side
-- MIRRORS, and only after the writer confirms 'applied' or 'replay'. A DARK
-- 'deferred' writes nothing here, ever.
--
-- Structure copied in shape from portal_blueprint_market above: portal-owned
-- marketplace table, provenance clamped from the session, a snapshot author
-- name, a status enum, a partial UNIQUE index so a closed row cannot block a
-- fresh one, plus a separate append-only log backing a rate limit that a deploy
-- cannot reset. All timestamps UTC 'YYYY-MM-DD HH:MM:SS'.
-- ==========================================================================
CREATE TABLE IF NOT EXISTS portal_karum_listings (
    listing_id      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- provenance (clamped from the seller's linked session; never client-trusted)
    seller_account_id  INTEGER NOT NULL,   -- ls_account_links.account_id
    seller_discord_id  TEXT,               -- moderation trail + the alt/self-trade check
    seller_name        TEXT    NOT NULL,   -- character-name snapshot. The ONLY identity ever
                                           -- rendered, and only on this seller's own
                                           -- listings (owner decision D2)
    seller_ctrl        INTEGER NOT NULL,   -- player_controller_id, server-side only,
                                           -- NEVER emitted to any client

    -- the goods (display metadata is server-derived; no free author text in v1)
    template_id     TEXT    NOT NULL,
    display_name    TEXT    NOT NULL,      -- resolved item-name snapshot
    stack_size      INTEGER NOT NULL,
    quality_level   INTEGER NOT NULL DEFAULT 0,
    durability_cur  REAL,
    durability_max  REAL,

    -- the offer
    price           INTEGER NOT NULL,      -- whole Solari, BANK BALANCE (currency 0), not coins

    -- escrow linkage (server-side only; never emitted to any client)
    escrow_item_id  INTEGER,               -- dune.items.id now sitting in inventory 610
    escrow_corr_id  TEXT,                  -- correlation_id of the LIST write

    -- buyer, populated at sale
    buyer_account_id INTEGER,
    buyer_discord_id TEXT,
    buyer_ctrl       INTEGER,
    sold_corr_id     TEXT,                 -- correlation_id of the BUY write

    -- lifecycle. 'selling' and 'reconciling' are TRANSIENT: a listing resting in
    -- either one past the bounded retry window is a bug, and the nightly audit
    -- reports it as one. Listings have NO TTL of their own in Phase 1 (deliberate:
    -- SB-007's complaint is that the CHOAM Exchange takes goods AWAY from players,
    -- so a venue answering it with a second expiry clock inherits the problem).
    status          TEXT    NOT NULL DEFAULT 'pending',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    sold_at         TEXT,
    closed_at       TEXT,

    CHECK (seller_account_id > 0),
    CHECK (price > 0),
    CHECK (stack_size > 0),
    CHECK (status IN ('pending','active','selling','reconciling','sold','returning',
                      'cancelled','paid_undelivered','failed'))
);
CREATE INDEX IF NOT EXISTS idx_pkl_status_created
    ON portal_karum_listings(status, created_at);
CREATE INDEX IF NOT EXISTS idx_pkl_status_price
    ON portal_karum_listings(status, price);
CREATE INDEX IF NOT EXISTS idx_pkl_template_status
    ON portal_karum_listings(template_id, status);
CREATE INDEX IF NOT EXISTS idx_pkl_seller
    ON portal_karum_listings(seller_account_id, status);
CREATE INDEX IF NOT EXISTS idx_pkl_buyer
    ON portal_karum_listings(buyer_account_id, sold_at);

-- One LIVE listing per escrowed item. Partial so a cancelled or sold row never
-- blocks a fresh listing of the same item later. This is the structural guard
-- against double-listing one stack.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pkl_one_live_item
    ON portal_karum_listings(escrow_item_id)
    WHERE status IN ('active','selling','reconciling','returning','paid_undelivered');

-- Whole-order wanted listings. Kept separate from portal_karum_listings because a
-- request has no seller and no escrowed item until another player fills it. A fill
-- creates a normal settlement listing and links it here, so the existing escrow,
-- payment, delivery and operator-recovery machinery remains the only settlement path.
CREATE TABLE IF NOT EXISTS portal_karum_requests (
    request_id          INTEGER PRIMARY KEY AUTOINCREMENT,

    -- requester provenance, clamped from the linked session at post time
    requester_account_id INTEGER NOT NULL,
    requester_discord_id TEXT,
    requester_name       TEXT    NOT NULL,
    requester_ctrl       INTEGER NOT NULL,

    -- exact whole-order goods. No partial fill and no per-unit price in v1.
    template_id       TEXT    NOT NULL,
    display_name      TEXT    NOT NULL,
    category          TEXT,
    stack_size        INTEGER NOT NULL,
    price             INTEGER NOT NULL,
    post_corr_id      TEXT    NOT NULL UNIQUE,

    -- Requested grade. NULL is LEGACY ANY-GRADE and must keep meaning exactly that:
    -- rows posted before 2026-08-25 carry no grade and any grade still fills them.
    -- 0 is Base and is a REAL request, not "unset" -- never conflate the two, which is
    -- why this is nullable rather than `NOT NULL DEFAULT 0`. Range 0..5 (live-verified
    -- on dune.items 2026-08-25: quality_level tops out at 5).
    quality_level     INTEGER,
    -- 'exact' | 'min', NULL only when quality_level is NULL. Validated in Python rather
    -- than by a CHECK so the fresh-CREATE and the ALTER upgrade path below cannot drift.
    quality_mode      TEXT,

    -- populated only after one filler wins the active -> filling compare-and-set
    filler_account_id INTEGER,
    filler_discord_id TEXT,
    filler_name       TEXT,
    filler_ctrl       INTEGER,
    fill_item_id      INTEGER,
    settlement_listing_id INTEGER UNIQUE,
    take_corr_id      TEXT UNIQUE,
    fill_corr_id      TEXT UNIQUE,

    status            TEXT    NOT NULL DEFAULT 'active',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    filled_at         TEXT,
    closed_at         TEXT,

    CHECK (requester_account_id > 0),
    CHECK (requester_ctrl > 0),
    CHECK (stack_size > 0),
    CHECK (price > 0),
    CHECK (status IN ('active','filling','reconciling','paid_undelivered',
                      'filled','cancelled','failed'))
);
CREATE INDEX IF NOT EXISTS idx_pkr_status_created
    ON portal_karum_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_pkr_status_price
    ON portal_karum_requests(status, price);
CREATE INDEX IF NOT EXISTS idx_pkr_template_status
    ON portal_karum_requests(template_id, status);
CREATE INDEX IF NOT EXISTS idx_pkr_requester
    ON portal_karum_requests(requester_account_id, status);
CREATE INDEX IF NOT EXISTS idx_pkr_filler
    ON portal_karum_requests(filler_account_id, filled_at);

-- Append-only event log. Two jobs: it backs the per-account rate limits via
-- COUNT(*) over a time window (durable across restarts, so a deploy cannot reset
-- a cap), and it is the moderation and dispute trail. NOTHING is ever updated or
-- deleted here.
CREATE TABLE IF NOT EXISTS portal_karum_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id    INTEGER,
    account_id    INTEGER NOT NULL,        -- the actor
    discord_id    TEXT,
    event         TEXT    NOT NULL,
                  -- list_attempt | list_applied | list_failed | buy_attempt
                  -- | buy_applied | buy_failed | cancel_attempt | cancel_applied
                  -- | request_post_applied | request_fill_attempt
                  -- | request_fill_applied | request_fill_failed | request_cancelled
                  -- | deliver_retry | admin_force_deliver | admin_force_return
                  -- | audit_mismatch
    detail        TEXT,                    -- json sidecar, NO account_id keys
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (account_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_pke_account_time
    ON portal_karum_events(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pke_event_time
    ON portal_karum_events(event, created_at);
CREATE INDEX IF NOT EXISTS idx_pke_listing
    ON portal_karum_events(listing_id, created_at);

-- Read-side mirror of CONFIRMED game-host settlement effects. The GAME DB is the
-- source of truth for idempotency and for the effect itself; this table exists so
-- the portal can render trade history and drive the notification fan-out without a
-- per-request game-host read. Its UNIQUE correlation_id is a local double-write
-- pre-check, NOT the real guard. Modelled on ls_reward_claims above.
CREATE TABLE IF NOT EXISTS portal_karum_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id  TEXT    NOT NULL UNIQUE,  -- same key as dune.ls_karum_escrow
                                              -- and dune.ls_item_delivery_log
    listing_id      INTEGER NOT NULL,
    leg             TEXT    NOT NULL,      -- list | pay | deliver | return
    account_id      INTEGER NOT NULL,      -- who the leg acted on
    counterparty_id INTEGER,               -- the other side, where there is one
    template_id     TEXT,
    stack_size      INTEGER,
    quality_level   INTEGER,
    amount          INTEGER,               -- Solari moved (pay legs only)
    game_item_id    INTEGER,               -- dune.items.id
    game_order_id   INTEGER,               -- dune.dune_exchange_orders.id (deliver)
    status          TEXT    NOT NULL,      -- applied | replay
    detail          TEXT,                  -- json sidecar

    -- notification fan-out flags (contract section 10). NULL = not yet done. No
    -- channel is load-bearing for correctness: a trade is complete when the ledger
    -- says so, notifications are only how the player finds out.
    mailbox_msg_id   INTEGER,              -- portal_messages.id, primary channel
    discord_post_at  TEXT,                 -- secondary channel
    whispered_at     TEXT,                 -- tertiary, set by the deferred sweep
    notify_attempts  INTEGER NOT NULL DEFAULT 0,

    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (account_id > 0),
    CHECK (leg IN ('list','pay','deliver','return')),
    CHECK (status IN ('applied','replay'))
);
CREATE INDEX IF NOT EXISTS idx_pkld_listing
    ON portal_karum_ledger(listing_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pkld_account
    ON portal_karum_ledger(account_id, created_at);
-- Drives the deferred whisper sweep: legs that landed but were never whispered.
CREATE INDEX IF NOT EXISTS idx_pkld_whisper_pending
    ON portal_karum_ledger(whispered_at, created_at)
    WHERE whispered_at IS NULL;

-- ==========================================================================
-- Fremkit wave 4: identity codes.
--
-- A short typeable handle for one portal identity (the Discord id behind the
-- session), so one player can hand another something to say out loud without
-- either side ever seeing a Discord id or an account_id. 8 symbols from a
-- 32-symbol alphabet with 0/1/I/O removed; a code is a VALUE, never a URL.
--
-- History is kept: rotation retires the current row (active=0, rotated_at set)
-- and inserts a new one, so a code handed out last month can still be explained
-- to a player who asks. Lives in admin.db (NOT dune.*) per the
-- custom-table-ownership rule.
--
-- Upgrade path for an existing admin.db: init_db re-executes SCHEMA on every
-- start, so CREATE TABLE IF NOT EXISTS is itself the migration. _PORTAL_ALTERS
-- carries only statements SQLite has no IF NOT EXISTS form for (ADD COLUMN),
-- which is why this wave adds nothing there.
-- ==========================================================================
CREATE TABLE IF NOT EXISTS portal_identity_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity    TEXT    NOT NULL,           -- ls_account_links.discord_id
    code        TEXT    NOT NULL,           -- ^[A-Z2-9]{8}$
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    rotated_at  TEXT,                       -- set when this row is retired; NULL while active
    active      INTEGER NOT NULL DEFAULT 1,
    CHECK (active IN (0, 1)),
    CHECK (length(code) = 8)
);
-- One live code per identity AND one identity per live code. Both partial, so a
-- retired row never blocks a fresh mint of the same identity or the same code.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pic_active_identity
    ON portal_identity_codes(identity) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_pic_active_code
    ON portal_identity_codes(code) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_pic_identity
    ON portal_identity_codes(identity, created_at);

-- Append-only lookup log. Backs the per-identity lookup throttle by COUNT(*)
-- over a time window (mirrors portal_rate_limit), so a restart cannot reset the
-- cap. No looked-up code is stored: the log answers "how many", never "which".
CREATE TABLE IF NOT EXISTS portal_code_lookups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity    TEXT    NOT NULL,
    looked_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    found       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pcl_identity_time
    ON portal_code_lookups(identity, looked_at);

-- ==========================================================================
-- Fremkit wave 9: portal events + reminders.
--
-- An event is written by an admin in the portal and read by everyone. `status`
-- is the whole visibility model: a draft is invisible outside the admin lane, a
-- published event is public, a cancelled one stays public so a player who
-- followed a link learns it was cancelled. `created_by_discord_id` is
-- server-only and is never part of the public projection.
--
-- Reminders are one row per (event, player), keyed by account_id, with
-- notified_at NULL meaning "still due": the delivery loop claims a row by
-- stamping it, and a cancel stamps every row of the event in the same
-- transaction that cancels it, so a cancelled event can never send a
-- "starting soon" notice afterwards.
--
-- Timestamps are the portal's own 'YYYY-MM-DD HH:MM:SS' UTC. Additive only:
-- init_db re-executes SCHEMA on every start, so CREATE TABLE IF NOT EXISTS is
-- itself the migration and this wave adds nothing to _PORTAL_ALTERS.
-- ==========================================================================
CREATE TABLE IF NOT EXISTS portal_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    title                  TEXT    NOT NULL,
    kind                   TEXT    NOT NULL DEFAULT 'community',
    banner                 TEXT,                  -- shipped banner slug, never a path
    starts_utc             TEXT    NOT NULL,
    ends_utc               TEXT,
    map_name               TEXT,
    description            TEXT,
    host                   TEXT,                  -- a handle, free text
    status                 TEXT    NOT NULL DEFAULT 'draft',
    created_by_discord_id  TEXT,                  -- server-only; never projected
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    published_at           TEXT,                  -- stamped once, on first publish
    cancelled_at           TEXT,
    cancel_reason          TEXT,
    CHECK (kind IN ('community','faction_war','maintenance','other')),
    CHECK (status IN ('draft','published','cancelled')),
    CHECK (length(title) <= 200),
    CHECK (description IS NULL OR length(description) <= 4000)
);
-- Both public reads are (status, starts_utc): upcoming ascending, past descending.
CREATE INDEX IF NOT EXISTS idx_pev_status_starts
    ON portal_events(status, starts_utc);

CREATE TABLE IF NOT EXISTS portal_event_reminders (
    event_id    INTEGER NOT NULL REFERENCES portal_events(id),
    account_id  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    notified_at TEXT,                             -- NULL = still due
    PRIMARY KEY (event_id, account_id),
    CHECK (account_id > 0)
);
-- The delivery loop reads only what is still due; the partial index keeps that
-- scan proportional to the backlog rather than to every reminder ever set.
CREATE INDEX IF NOT EXISTS idx_pevr_due
    ON portal_event_reminders(event_id, account_id) WHERE notified_at IS NULL;
-- "What am I reminded about" is an account-first read the PK cannot serve.
CREATE INDEX IF NOT EXISTS idx_pevr_account
    ON portal_event_reminders(account_id, event_id);

-- ==========================================================================
-- Wave 10b: account preferences.
--
-- One JSON document per (identity, scope). Scope '' is identity-wide and
-- 'char:<controller_id>' is one character, so a map default saved on one
-- character does not follow another. The document is a WHITELIST, not a
-- schema: routers/portal_prefs.py owns the key set and the value shapes, and
-- an unknown key refuses the whole write rather than being stored.
--
-- Preferences are a convenience, never a gate: nothing here authorises
-- anything, so there are no audit rows, and a row whose JSON no longer parses
-- is read as an empty document rather than failing the page.
--
-- The primary key is the only read this table has (every scope for one
-- identity), so it carries no separate index. Timestamps are the portal's own
-- 'YYYY-MM-DD HH:MM:SS' UTC. Additive only: init_db re-executes SCHEMA on every
-- start, so CREATE TABLE IF NOT EXISTS is itself the migration and this wave
-- adds nothing to _PORTAL_ALTERS.
-- ==========================================================================
CREATE TABLE IF NOT EXISTS portal_prefs (
    discord_id  TEXT NOT NULL,                  -- ls_account_links.discord_id
    scope       TEXT NOT NULL DEFAULT '',       -- '' identity-wide | 'char:<controller_id>'
    prefs       TEXT NOT NULL DEFAULT '{}',     -- JSON object, whitelisted keys only
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (discord_id, scope)
);

-- ==========================================================================
-- Fremkit wave 11: portal chat.
--
-- Four tables, all admin.db, all linked-players-only. `channel` is the whole
-- membership model and it is a STRING, not a foreign key: 'sietch',
-- 'guild:<guild_id>', 'faction:atreides'|'faction:harkonnen' and the five fixed
-- 'map:<board>:<instance>' rooms. Guilds and factions live in the GAME database
-- and are read through the relay, so there is nothing here to join to; who may
-- read a channel is decided per request in portal_chat_channels.py and is never
-- stored beside a message.
--
-- A deleted message KEEPS ITS ROW. deleted_utc plus deleted_by is the whole
-- delete: the body is blanked on the way out, not in the table, so the list a
-- player is reading does not jump under them and moderation can still see what
-- was said. Retention (30 days) is what actually removes rows.
--
-- client_key is the client's own uuid for one intended send, unique per account
-- so a retry after a lost response replays the SAME message instead of posting a
-- second one. The index is partial: a message posted without a key (an older
-- client) must not collide with every other keyless message on that account.
--
-- portal_chat_mutes.channel = '' means everywhere. A mute is never deleted when
-- it ends: until_utc expiring and lifted_utc being stamped are different facts
-- and a moderator needs to be able to tell them apart.
--
-- `kind` (wave 11.1) is how the row was produced, not what it says: 'say' is a
-- typed message, 'emote' is /me and renders without a colon, 'roll' is a /roll
-- the SERVER resolved. It carries no CHECK constraint on purpose. A future kind
-- would otherwise need a table rebuild on a live admin.db, and an unknown kind
-- renders as 'say' on the page, which is the failure a chat message should have.
--
-- account_id is server-side only and is never projected; char_name is the only
-- public handle. Timestamps are the portal's own 'YYYY-MM-DD HH:MM:SS' UTC.
-- Additive only: init_db re-executes SCHEMA on every start, so CREATE TABLE IF
-- NOT EXISTS is itself the migration for a NEW admin.db. `kind` is the one
-- statement that needs more than that, because the table already exists on the
-- live box: it rides _PORTAL_ALTERS as well, and the two paths have to agree.
-- ==========================================================================
CREATE TABLE IF NOT EXISTS portal_chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT    NOT NULL,
    account_id    INTEGER NOT NULL,
    controller_id INTEGER,                       -- selected character, advisory
    char_name     TEXT    NOT NULL,              -- the only public handle
    body          TEXT    NOT NULL,              -- already cleaned; never logged
    kind          TEXT    NOT NULL DEFAULT 'say',-- 'say' | 'emote' | 'roll'
    client_key    TEXT,                          -- client uuid; NULL = no replay key
    created_utc   TEXT    NOT NULL,
    deleted_utc   TEXT,
    deleted_by    TEXT,                          -- 'author' | 'leader' | 'admin'
    CHECK (deleted_by IS NULL OR deleted_by IN ('author','leader','admin'))
);
-- The only read the message list has: one channel, descending id.
CREATE INDEX IF NOT EXISTS idx_pcm_channel_id
    ON portal_chat_messages(channel, id);
-- The rate windows COUNT this account's own recent rows, which the channel
-- index cannot serve and the partial client_key index does not cover.
CREATE INDEX IF NOT EXISTS idx_pcm_account_id
    ON portal_chat_messages(account_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pcm_client_key
    ON portal_chat_messages(account_id, client_key) WHERE client_key IS NOT NULL;
-- The live stream notices a delete by polling for freshly stamped rows, which is
-- a scan of the whole retention window without this.
CREATE INDEX IF NOT EXISTS idx_pcm_deleted
    ON portal_chat_messages(deleted_utc) WHERE deleted_utc IS NOT NULL;

CREATE TABLE IF NOT EXISTS portal_chat_reads (
    account_id INTEGER NOT NULL,
    channel    TEXT    NOT NULL,
    last_id    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, channel)
);

CREATE TABLE IF NOT EXISTS portal_chat_mutes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    channel     TEXT    NOT NULL DEFAULT '',     -- '' = everywhere
    by_kind     TEXT    NOT NULL,                -- 'admin' | 'leader'
    by_discord  TEXT    NOT NULL,                -- server-only; never projected
    reason      TEXT,
    until_utc   TEXT,                            -- NULL = until lifted
    created_utc TEXT    NOT NULL,
    lifted_utc  TEXT,
    lifted_by   TEXT,
    CHECK (by_kind IN ('admin','leader'))
);
-- "is this player muted here" runs on every send.
CREATE INDEX IF NOT EXISTS idx_pcmute_account
    ON portal_chat_mutes(account_id, channel);

CREATE TABLE IF NOT EXISTS portal_chat_reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          INTEGER NOT NULL,
    channel             TEXT    NOT NULL,
    reporter_account_id INTEGER NOT NULL,
    reason              TEXT,
    created_utc         TEXT    NOT NULL,
    state               TEXT    NOT NULL DEFAULT 'open',
    resolved_by         TEXT,
    resolved_utc        TEXT,
    -- One report per message per reporter: a second one is an idempotent
    -- already:true, not a second row in the queue.
    UNIQUE (message_id, reporter_account_id),
    CHECK (state IN ('open','dismissed','actioned'))
);
-- The queue reads open-first, oldest-first. message_id is NOT a foreign key on
-- purpose: retention deletes messages and their reports in its own order, and a
-- constraint here would decide that order for it.
CREATE INDEX IF NOT EXISTS idx_pcr_state
    ON portal_chat_reports(state, created_utc);
CREATE INDEX IF NOT EXISTS idx_pcr_message
    ON portal_chat_reports(message_id);
"""

# Idempotent ALTER paths for in-place upgrade of an admin.db that pre-dates the
# review-fix columns. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we catch the
# "duplicate column" error per-column. New deploys hit the CREATE TABLE above
# and never enter this path.
_PORTAL_ALTERS = (
    "ALTER TABLE portal_link_attempts ADD COLUMN callback_hit_at TEXT",
    "ALTER TABLE portal_link_attempts ADD COLUMN pick_token TEXT",
    "ALTER TABLE portal_link_attempts ADD COLUMN is_test_run INTEGER NOT NULL DEFAULT 0",
    # Exact per-candidate Large coords (Part B); NULL on rows recorded before the
    # reader emitted coords / before this upgrade.
    "ALTER TABLE spice_candidate_acc ADD COLUMN x REAL",
    "ALTER TABLE spice_candidate_acc ADD COLUMN y REAL",
    # Social layer Tier 2: structured Signal-Board filter columns on the existing
    # recruiting table (upgrade path for an admin.db that pre-dates them).
    "ALTER TABLE portal_guild_recruiting ADD COLUMN playstyle TEXT",
    "ALTER TABLE portal_guild_recruiting ADD COLUMN timezone TEXT",
    "ALTER TABLE portal_guild_recruiting ADD COLUMN language TEXT",
    "ALTER TABLE portal_guild_recruiting ADD COLUMN new_player_friendly INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE portal_guild_recruiting ADD COLUMN discord_url TEXT",
    # Karum wanted-order exact/minimum grade (2026-08-25). Both stay NULL on rows posted
    # before the upgrade, which is the legacy any-grade contract, so there is no backfill.
    "ALTER TABLE portal_karum_requests ADD COLUMN quality_level INTEGER",
    "ALTER TABLE portal_karum_requests ADD COLUMN quality_mode TEXT",
    # Portal roles on /portal/me (2026-09-03): the Discord identity an admin account
    # is mapped to, so a role-gated V2 surface can resolve a role server-side. NULL
    # on every row until an operator maps one; the partial unique index keeps one
    # Discord id from claiming two admin accounts. The index rides this list rather
    # than SCHEMA because on an existing admin.db the column only exists after the
    # ALTER above has run.
    "ALTER TABLE users ADD COLUMN discord_id TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id)"
    " WHERE discord_id IS NOT NULL",
    # Player-chosen purpose tags on a market listing (Fremkit wave 6). Its own
    # column beside the server-derived `tags`, which every publish rewrites from
    # the blueprint pieces and would otherwise eat. NULL on every row published
    # before this, and NULL reads as no purpose tags, so there is no backfill;
    # the tag filter already scans the derived column with LIKE and scans this
    # one the same way, so there is no index either.
    "ALTER TABLE portal_blueprint_market ADD COLUMN user_tags TEXT",
    # Slash commands (Fremkit wave 11.1). How the row was produced: 'say' is a
    # typed message, 'emote' is /me, 'roll' is a server-resolved /roll. The
    # CREATE TABLE above carries the same column so a fresh install matches, and
    # the DEFAULT is what backfills every row posted before this wave: they were
    # all typed, so 'say' is the truth and not a placeholder.
    "ALTER TABLE portal_chat_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'say'",
)


def _try_alter(conn: sqlite3.Connection, sql: str) -> None:
    try:
        conn.execute(sql)
    except sqlite3.DatabaseError:
        # OperationalError = already applied; IntegrityError = an index that cannot
        # be built yet. Neither may raise: init_db runs in the app lifespan and a
        # raise here restarts lastsietch-admin in a loop.
        pass


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    for sql in _PORTAL_ALTERS:
        _try_alter(conn, sql)
    conn.commit()
    conn.close()


def has_users() -> bool:
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    conn.close()
    return row[0] > 0
