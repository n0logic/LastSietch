-- ls_guild_ops — durable audit + idempotency ledger for portal guild WRITE ops.
--
-- Mirrors dune.ls_progression_grants (the grant-tool audit table): one row per
-- attempted guild operation, keyed by a UNIQUE idempotency_key so a relay retry
-- or double-submit is a no-op replay. Written inside the SAME transaction as the
-- guild mutation by scripts/dune-guild-op.sh (deployed to lastsietch-dune:/root/).
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the
-- dune schema is owned by a role other than `dune` (see
-- our internal notes + our internal notes
-- funcom_migration). The ALTER ... OWNER TO dune below is REQUIRED, not optional:
-- creating this table as `postgres` and skipping the ALTER will break the next
-- Funcom migration dump. Keep it in the dune schema (not a custom schema) for
-- the same reason.
--
-- Idempotent: safe to re-run. Guards each object with IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS dune.ls_guild_ops (
    id                      bigserial PRIMARY KEY,
    idempotency_key         uuid UNIQUE NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    op                      text NOT NULL,
    guild_id                bigint,
    actor_account_id        bigint,
    target_account_id       bigint,
    detail                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    operator                text,
    requested_by_discord_id text,
    status                  text NOT NULL DEFAULT 'applied',
    fail_reason             text,
    applied_at              timestamptz
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt (see header).
ALTER TABLE dune.ls_guild_ops OWNER TO dune;

-- Recent-ops lookups for the admin/audit panel.
CREATE INDEX IF NOT EXISTS ls_guild_ops_created_idx
    ON dune.ls_guild_ops (created_at DESC);
CREATE INDEX IF NOT EXISTS ls_guild_ops_guild_idx
    ON dune.ls_guild_ops (guild_id, created_at DESC);
