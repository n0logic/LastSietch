-- ls_gift_failures, durable record of REFUSED / FAILED portal Solari gifts.
--
-- Sibling of dune.ls_guild_gifts, which records only gifts that COMMITTED. A
-- gift that trips a cap or the balance pre-check does so with RAISE EXCEPTION
-- inside the same transaction that inserted its own ls_guild_gifts audit row,
-- so the attempt rolls back with the money and leaves zero trace. This table is
-- written by scripts/dune-gift-op.sh in a SECOND, independent transaction after
-- the first one has already rolled back, which is why the record survives.
--
-- WHY NOT just populate ls_guild_gifts.fail_reason on the same row:
-- ls_guild_gifts.idempotency_key is UNIQUE and is the idempotency gate. A
-- 'failed' row under that key makes the gate's ON CONFLICT DO NOTHING report
-- is_new = false on the next attempt with the same key, so a legitimate retry
-- would return "already applied, idempotent replay" having moved no money at
-- all. A false success is worse than the missing record it fixes. Failures
-- therefore go to their own table, keyed loosely, and ls_guild_gifts stays a
-- ledger of what actually happened to balances.
--
-- idempotency_key here is NOT unique: one key may legitimately fail several
-- times (transient DB error, then a cap). Every attempt is a row.
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the
-- dune schema is owned by a role other than `dune` (a Funcom migration
-- can reset custom table ownership). The ALTER ... OWNER TO dune below is
-- REQUIRED, not optional. Keep it in the dune schema for the same reason.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS dune.ls_gift_failures (
    id                      bigserial PRIMARY KEY,
    created_at              timestamptz NOT NULL DEFAULT now(),
    idempotency_key         uuid,
    sender_account_id       bigint,
    recipient_account_id    bigint,
    amount                  bigint,
    operator                text,
    requested_by_discord_id text,
    fail_reason             text NOT NULL
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt (see header).
ALTER TABLE dune.ls_gift_failures OWNER TO dune;

-- "Who is probing the caps" is the question this table exists to answer, so the
-- indexes are the two ways an operator asks it: recently, and by sender.
CREATE INDEX IF NOT EXISTS ls_gift_failures_created_idx
    ON dune.ls_gift_failures (created_at DESC);
CREATE INDEX IF NOT EXISTS ls_gift_failures_sender_idx
    ON dune.ls_gift_failures (sender_account_id, created_at DESC);
