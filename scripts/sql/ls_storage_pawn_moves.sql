SET search_path TO dune, public;

CREATE TABLE IF NOT EXISTS dune.ls_storage_pawn_moves (
    idempotency_key       uuid        PRIMARY KEY,
    owner_ctrl            bigint      NOT NULL,
    item_id               bigint      NOT NULL,
    expected_template     text,
    template_id           text        NOT NULL,
    requested_src_kind    text        NOT NULL CHECK (requested_src_kind IN ('bank', 'backpack')),
    requested_dst_kind    text        NOT NULL CHECK (requested_dst_kind IN ('bank', 'backpack')),
    src_inventory_id      bigint      NOT NULL,
    dst_inventory_id      bigint      NOT NULL,
    src_position_index    bigint,
    dst_position_index    bigint      NOT NULL,
    stack_size            bigint      NOT NULL,
    volume_unverified     boolean     NOT NULL DEFAULT false,
    status                text        NOT NULL DEFAULT 'applied' CHECK (status IN ('applied')),
    created_at            timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE dune.ls_storage_pawn_moves OWNER TO dune;

CREATE INDEX IF NOT EXISTS ls_storage_pawn_moves_owner_created_idx
    ON dune.ls_storage_pawn_moves (owner_ctrl, created_at DESC);

COMMENT ON TABLE dune.ls_storage_pawn_moves IS
    'Idempotency and audit ledger for offline CHOAM Bank and Backpack item moves.';
