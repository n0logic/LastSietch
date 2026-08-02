-- ls_item_transfers — durable audit + idempotency ledger for portal CHOAM BANK
-- item transfers (Tier 5). One row per attempted transfer, keyed by a UNIQUE
-- idempotency_key so a relay retry or double-submit is a no-op replay. Written
-- inside the SAME transaction as the atomic item move by scripts/dune-item-transfer-op.sh
-- (deployed to the game host:/root/).
--
-- The transfer itself is a SINGLE-ROW re-home: UPDATE dune.items SET inventory_id =
-- <recipient bank> pinned by WHERE id = <item> AND inventory_id = <sender bank>. That
-- is atomic and value-conserving by construction (the row is moved, never copied), so
-- there is no delete+insert dupe/loss window.
--
-- 🔴 THE ONLINE-SAFETY CLAIM THAT USED TO BE IN THIS HEADER IS FALSE. It read: "Both
-- banks are inventory_type=30, which is online-safe — no offline gate, no fly-out/in RAM
-- reload; the item renders on the recipient's next bank-UI open." Live-tested 2026-07-26
-- on build 24376904 and corrected 2026-07-27. Both halves are wrong:
--   * the bank loads at a ZONE TRANSITION, not on bank-UI open;
--   * a REMOVAL from an online player's bank is restored under its ORIGINAL item id as
--     soon as that session moves the item, and survives a full reload.
-- Giving to an online player is safe; TAKING from one is not. The SENDER side of this
-- transfer is therefore OFFLINE-GATED, in scripts/lib/dune-take-item.sh, the shared take
-- this writer and the Karum writer both use. The recipient side stays ungated: it is a
-- give. See.
--
-- OWNER MUST BE dune. Funcom's pre-update pg_dump aborts if any object in the dune
-- schema is owned by a role other than `dune` (
-- + our internal notes). The ALTER ... OWNER TO dune
-- below is REQUIRED. Keep it in the dune schema for the same reason.
--
-- Idempotent: safe to re-run. Guards each object with IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS dune.ls_item_transfers (
    id                      bigserial PRIMARY KEY,
    idempotency_key         uuid UNIQUE NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    sender_account_id       bigint,
    recipient_account_id    bigint,
    sender_pawn_id          bigint,
    recipient_pawn_id       bigint,
    sender_bank_inv_id      bigint,
    recipient_bank_inv_id   bigint,
    item_id                 bigint NOT NULL,
    template_id             text,
    stack_size              bigint,
    quality_level           bigint,
    detail                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    operator                text,
    requested_by_discord_id text,
    status                  text NOT NULL DEFAULT 'applied',
    fail_reason             text,
    applied_at              timestamptz
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt (see header).
ALTER TABLE dune.ls_item_transfers OWNER TO dune;

-- Recent-transfer lookups + per-sender / sender->recipient daily rate windows.
CREATE INDEX IF NOT EXISTS ls_item_transfers_created_idx
    ON dune.ls_item_transfers (created_at DESC);
CREATE INDEX IF NOT EXISTS ls_item_transfers_sender_idx
    ON dune.ls_item_transfers (sender_account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ls_item_transfers_pair_idx
    ON dune.ls_item_transfers (sender_account_id, recipient_account_id, created_at DESC);
