-- Karum payment gate + durable audit row.
--
-- 🔴 THIS TABLE IS WHAT MAKES THE PAYMENT LEG IDEMPOTENT. The Funcom currency proc does
--    NOT provide idempotency and never did. Without this table, the retry that the
--    contract's own compensation policy prescribes as the correct response to a failed
--    delivery would debit the buyer and credit the seller a SECOND time.
--
-- The gate-then-mutate pattern is copied from dune.ls_guild_gifts, which is the real
-- mechanism behind dune-gift-op.sh's idempotency: the INSERT ... ON CONFLICT DO NOTHING
-- happens in the SAME transaction as the balance adjustment, and the adjustment only runs
-- when the insert was new (dune-gift-op.sh:158-172 opens the gate, :190-193 returns before
-- touching a single balance when it was not new). The proc contributes atomicity, not
-- idempotency. It is easy to assume otherwise, and an earlier draft of the Karum contract
-- did exactly that.
--
-- 🔴 Karum's BUY deliberately splits payment (txn A) from delivery (txn B), which is the
--    whole reason the paid_undelivered state exists and the whole reason retrying a
--    delivery is safe. That split means a retried karum-buy re-enters txn A. The gate is
--    what makes that free.
--
-- A REFUND IS A PAYMENT TOO. A compensating refund is NOT an UPDATE of the original row:
-- it is a NEW row with its own correlation_id, so the refund is itself idempotent and a
-- retried refund cannot double-credit. The original row is stamped status = 'reversed'
-- with reversal_corr_id in the same transaction.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS dune.ls_karum_payments (
  id                    bigserial   PRIMARY KEY,
  correlation_id        uuid        NOT NULL UNIQUE,   -- the buy uuid, all four layers
  listing_id            bigint      NOT NULL,
  buyer_account_id      bigint      NOT NULL,
  seller_account_id     bigint      NOT NULL,
  amount                bigint      NOT NULL,
  currency_id           bigint      NOT NULL,          -- dune.get_solaris_id()
  buyer_balance_before  bigint,
  seller_balance_before bigint,
  status                text        NOT NULL DEFAULT 'applied',
  detail                jsonb,
  operator              text,
  applied_at            timestamptz NOT NULL DEFAULT now(),
  reversed_at           timestamptz,                   -- set by a compensating refund
  reversal_corr_id      uuid,                          -- the refund's own correlation_id

  CONSTRAINT ls_karum_payments_status_chk
    CHECK (status IN ('applied', 'reversed')),
  CONSTRAINT ls_karum_payments_amount_chk
    CHECK (amount > 0)
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt (see
-- ls_karum_escrow.sql for the full reason).
ALTER TABLE dune.ls_karum_payments OWNER TO dune;

CREATE INDEX IF NOT EXISTS idx_hkp_listing
  ON dune.ls_karum_payments (listing_id);
CREATE INDEX IF NOT EXISTS idx_hkp_buyer
  ON dune.ls_karum_payments (buyer_account_id, applied_at);
CREATE INDEX IF NOT EXISTS idx_hkp_seller
  ON dune.ls_karum_payments (seller_account_id, applied_at);
