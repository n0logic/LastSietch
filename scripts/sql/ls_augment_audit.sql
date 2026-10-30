-- ls_augment_audit, durable audit trail for the augment writer
-- (scripts/dune-augment.py, deployed to the game host:/root/). One row per
-- attempted augment operation, written INSIDE the same transaction as the
-- stats write, so an audit row and its effect can never disagree.
--
-- WHY THIS EXISTS. The writer edits dune.items.stats->'FAugmentedItemStats' and,
-- on a swap, DELETES a standalone augment item the player owned. Once it became
-- player-facing with a TRUE RANDOM reroll (a reroll can legitimately come out
-- worse), "I rerolled and it got worse" and "my augment vanished" became reports
-- we must be able to answer. Without this there is nothing to check: the old
-- values are overwritten in place and the consumed row is gone.
--
-- op is one of: install | reroll | swap
--   install : augments added to an item that had none
--   reroll  : same augment ids, roll VALUES redrawn (roll_mode=random) or forced
--             to 1.0 (roll_mode=perfect, the prize/operator path)
--   swap    : at least one augment id replaced; the incoming augment is consumed
--             from the player's own inventory in the same transaction
--
-- before/after hold the FULL FAugmentedItemStats block either side of the write,
-- so a support question is answered by reading one row rather than reconstructing
-- state. consumed_item_ids records exactly which rows the swap destroyed.
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the
-- dune schema is owned by a role other than `dune` (a Funcom migration
-- can reset custom table ownership). The ALTER ... OWNER TO dune below is REQUIRED. Keep it in
-- the dune schema for the same reason.
--
-- Idempotent: safe to re-run. Guards each object with IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS dune.ls_augment_audit (
    id                 bigserial PRIMARY KEY,
    created_at         timestamptz NOT NULL DEFAULT now(),
    owner_ctrl         bigint NOT NULL,
    item_id            bigint NOT NULL,
    template_id        text NOT NULL,
    op                 text NOT NULL,
    roll_mode          text,
    augments_before    jsonb NOT NULL DEFAULT '[]'::jsonb,
    augments_after     jsonb NOT NULL DEFAULT '[]'::jsonb,
    grades_after       jsonb NOT NULL DEFAULT '[]'::jsonb,
    rerolled           jsonb NOT NULL DEFAULT '[]'::jsonb,
    consumed_item_ids  jsonb NOT NULL DEFAULT '[]'::jsonb,
    operator           text
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt (see header).
ALTER TABLE dune.ls_augment_audit OWNER TO dune;

-- "what happened to this item" and "what has this player been doing" are the two
-- questions support actually asks.
CREATE INDEX IF NOT EXISTS ls_augment_audit_item_idx
    ON dune.ls_augment_audit (item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ls_augment_audit_owner_idx
    ON dune.ls_augment_audit (owner_ctrl, created_at DESC);
