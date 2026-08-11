-- Add the idempotency key to dune.ls_augment_audit (created by
-- ls_augment_audit.sql). Split into its own migration so the base table can
-- ship ahead of the player-facing write path, which is what happened.
--
-- WHY THIS IS LOAD-BEARING, not bookkeeping. The player-facing swap path
-- permanently DESTROYS a standalone augment item the player owned, inside the
-- same transaction as the stats write. Augments are rare: 297 standalone ones
-- exist across the entire live server. If a response is dropped in flight and
-- the client retries the same intent, without this key the second attempt is
-- indistinguishable from a new one and consumes a SECOND augment for a single
-- action the player took once. There is no compensating path afterwards: the
-- row is gone and the "before" state is overwritten.
--
-- The key is claimed inside the writer's own transaction, so the guarantee is
-- atomic with the consume + DELETE rather than advisory. A duplicate is
-- detected and REPLAYED (the prior outcome is reported back) rather than
-- re-executed. The partial unique index is what makes a concurrent duplicate
-- fail at the database instead of racing.
--
-- NULLable on purpose: the operator/prize paths (dune-grant.sh monthly reward,
-- a manual admin install) legitimately carry no client key, and a partial index
-- lets any number of those coexist while still enforcing uniqueness for every
-- real one.
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the
-- dune schema is owned by another role (see the custom-table ownership requirement).
-- The base table is already dune-owned; ADD COLUMN does not change that, and the
-- index inherits the table's owner. No ALTER needed here, but do not add objects
-- to this file without checking.
--
-- Idempotent: safe to re-run.

ALTER TABLE dune.ls_augment_audit
    ADD COLUMN IF NOT EXISTS idempotency_key text;

CREATE UNIQUE INDEX IF NOT EXISTS ls_augment_audit_idem_uidx
    ON dune.ls_augment_audit (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
