-- ls_guild_gifts — durable audit + idempotency ledger for portal Solari GIFTS.
--
-- Mirrors dune.ls_guild_ops (the guild-op audit table): one row per attempted
-- gift, keyed by a UNIQUE idempotency_key so a relay retry or double-submit is a
-- no-op replay. Written inside the SAME transaction as the two balance adjusts by
-- scripts/dune-gift-op.sh (deployed to the game host:/root/).
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the
-- dune schema is owned by a role other than `dune` (see
-- our internal notes + our internal notes
-- funcom_migration). The ALTER ... OWNER TO dune below is REQUIRED, not optional.
-- Keep it in the dune schema (not a custom schema) for the same reason.
--
-- Idempotent: safe to re-run. Guards each object with IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS dune.ls_guild_gifts (
    id                      bigserial PRIMARY KEY,
    idempotency_key         uuid UNIQUE NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    sender_account_id       bigint,
    recipient_account_id    bigint,
    sender_controller_id    bigint,
    recipient_controller_id bigint,
    amount                  bigint NOT NULL,
    currency_id             smallint NOT NULL DEFAULT 0,   -- 0 = BANK Solari (get_solaris_id)
    detail                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    operator                text,
    requested_by_discord_id text,
    status                  text NOT NULL DEFAULT 'applied',
    fail_reason             text,
    applied_at              timestamptz,
    CHECK (amount > 0)
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt (see header).
ALTER TABLE dune.ls_guild_gifts OWNER TO dune;

-- Recent-gift lookups + per-sender / sender->recipient daily rate windows.
CREATE INDEX IF NOT EXISTS ls_guild_gifts_created_idx
    ON dune.ls_guild_gifts (created_at DESC);
CREATE INDEX IF NOT EXISTS ls_guild_gifts_sender_idx
    ON dune.ls_guild_gifts (sender_account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ls_guild_gifts_pair_idx
    ON dune.ls_guild_gifts (sender_account_id, recipient_account_id, created_at DESC);
