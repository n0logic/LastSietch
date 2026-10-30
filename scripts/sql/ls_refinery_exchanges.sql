SET search_path TO dune, public;

CREATE TABLE IF NOT EXISTS dune.ls_refinery_exchanges (
    idempotency_key   uuid        PRIMARY KEY,
    account_id        bigint      NOT NULL,
    owner_ctrl        bigint      NOT NULL,
    tier              int         NOT NULL CHECK (tier BETWEEN 1 AND 6),
    output_template   text        NOT NULL,
    batches           bigint      NOT NULL CHECK (batches > 0),
    dust_units        bigint      NOT NULL CHECK (dust_units > 0),
    input_template    text        NOT NULL,
    input_units       bigint      NOT NULL CHECK (input_units > 0),
    spice_units       bigint      NOT NULL CHECK (spice_units > 0),
    consumed_json     jsonb       NOT NULL,
    granted_json      jsonb       NOT NULL,
    rate_version      text        NOT NULL,
    volume_unverified boolean     NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- 🔴 EVERY column added after this file first shipped needs BOTH a line in the CREATE
-- above (so a fresh box gets it) AND an additive ALTER below (so an EXISTING box gets it).
-- CREATE TABLE IF NOT EXISTS is a no-op once the table is there, so a column added only
-- above is silently absent on every live box, and the writer's INSERT names its columns
-- explicitly: the first trade after that deploy fails as write_failed with nothing in the
-- logs pointing at a missing column. Additive, IF NOT EXISTS, never a rewrite.
ALTER TABLE dune.ls_refinery_exchanges
    ADD COLUMN IF NOT EXISTS volume_unverified boolean NOT NULL DEFAULT false;

ALTER TABLE dune.ls_refinery_exchanges OWNER TO dune;

-- idempotency_key is the PRIMARY KEY, which is already a unique index; a second one over
-- the same column would only cost every insert an extra write for no added guarantee.

-- The game-side weekly cap backstop: SUM(dust_units) per account per output template
-- over a rolling window. The portal's SQLite counter is the primary cap; this index is
-- what keeps the second line cheap inside the write transaction.
CREATE INDEX IF NOT EXISTS ls_refinery_exchanges_account_output_created_idx
    ON dune.ls_refinery_exchanges (account_id, output_template, created_at DESC);

COMMENT ON TABLE dune.ls_refinery_exchanges IS
    'Idempotency, audit and weekly-cap ledger for offline Ingot Refinery exchanges.';
