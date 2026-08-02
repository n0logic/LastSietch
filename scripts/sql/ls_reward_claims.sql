-- ls_reward_claims — durable audit + idempotency ledger for the portal login
-- REWARDS module (login-rewards V2). One row per attempted claim, keyed by a
-- UNIQUE idempotency_key so a relay retry or double-submit is a no-op replay.
-- Written by scripts/dune-reward-op.sh (deployed to the game host:/root/).
--
-- reward_kind is one of: daily_solari | weekly_item | monthly_augment.
--   daily_solari  : +credit via dune.adjust_player_virtual_currency_balance,
--                   audit row + credit in ONE atomic transaction.
--   weekly_item   : the mint is delegated to dune-grant.sh G29 bank_items_batch
--                   (online-safe, inv_type 30); this row is reserved as
--                   status='pending', then flipped to 'applied' once the mint
--                   returns. status therefore is one of pending|applied|failed.
--   monthly_augment : Phase 2 (gated on the bank-render proof-of-life); no
--                   writer path yet.
--
-- Per-account cap: dune-reward-op.sh counts prior status='applied' rows for the
-- account + reward_kind in the current CALENDAR period (daily_solari = this UTC
-- calendar day, weekly_item = this ISO week; date_trunc, not a rolling window)
-- and refuses when the cap is hit. Combined with the idempotency_key UNIQUE
-- guard (backend keys it uuid5(account, kind, period)) this makes double-claim
-- and replay both no-ops.
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the
-- dune schema is owned by a role other than `dune` (see
-- our internal notes + our internal notes
-- funcom_migration). The ALTER ... OWNER TO dune below is REQUIRED. Keep it in
-- the dune schema for the same reason.
--
-- Idempotent: safe to re-run. Guards each object with IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS dune.ls_reward_claims (
    id               bigserial PRIMARY KEY,
    idempotency_key  uuid UNIQUE NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    account_id       bigint NOT NULL,
    reward_kind      text NOT NULL,
    detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
    operator         text,
    status           text,
    applied_at       timestamptz
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt (see header).
ALTER TABLE dune.ls_reward_claims OWNER TO dune;

-- Per-account claim history + the daily/weekly cap window scans.
CREATE INDEX IF NOT EXISTS ls_reward_claims_account_idx
    ON dune.ls_reward_claims (account_id, created_at DESC);
