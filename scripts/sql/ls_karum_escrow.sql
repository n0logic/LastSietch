-- Karum escrow ledger. THE AUTHORITATIVE MARKER that distinguishes live player escrow in
-- exchange inventory 610 from the ~368k market-bot orphan rows that share the exact same
-- shape: an item row in 610 with no dune_exchange_orders row. See the Karum build contract
-- section 4 (docs/dune-research/v2-portal/KARUM-BUILD-CONTRACT-2026-07-27.md).
--
-- 🔴 NOBODY PURGES INVENTORY 610 WITHOUT EXCLUDING ROWS PRESENT HERE WITH state = 'held'.
--    The obvious cleanup query for the bot's litter is the same query that selects live
--    player goods. `acquisition_time <> 0` is explicitly NOT a safe discriminator: the
--    claim lane writes 0 deliberately and DB-granted player items stamp 0 too, so it is a
--    negative-space test dressed up as a marker. A marker must be something we write on
--    purpose that nothing else writes. This table is that thing.
--
-- Why the 368k exist at all: they are the bot's own litter. dune.get_exchange_inventory_id(2)
-- = 610 = the bot's bot_inv_id, its listing insert omits acquisition_time, and it deletes
-- order rows on cull while leaving the item rows. Every DELETE path in the bot derives its
-- item ids FROM ORDER ROWS, never from an inventory scan, so Karum escrow (which has no
-- order row until settlement) is structurally invisible to all of them. That is why escrow
-- here is durable, and it is also why the litter accumulated.
--
-- Lives in dune.* alongside ls_item_delivery_log and ls_reward_claims per the
-- custom-table-ownership rule. correlation_id is the idempotency key: the game DB owns
-- idempotency, admin.db only mirrors after the writer confirms applied or replay.
--
-- correlation_id is `text`, not `uuid`, deliberately: it joins against
-- dune.ls_item_delivery_log.correlation_id (text, UNIQUE) which the delivery leg writes
-- under the same id. Two types that must join is a papercut nobody needs.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS dune.ls_karum_escrow (
  id                bigserial PRIMARY KEY,
  correlation_id    text        NOT NULL UNIQUE,
  listing_id        bigint      NOT NULL,      -- portal_karum_listings.listing_id
  item_id           bigint      NOT NULL,      -- dune.items.id parked in inv 610
  inventory_id      bigint      NOT NULL,      -- 610, recorded not assumed
  seller_account_id bigint      NOT NULL,
  seller_ctrl       bigint      NOT NULL,
  template_id       text        NOT NULL,
  stack_size        integer     NOT NULL,
  quality_level     integer     NOT NULL DEFAULT 0,
  -- Exchange category, SNAPSHOTTED when the item is taken. See the note below: this is a
  -- correctness requirement, not a cache.
  category_mask     bigint,
  category_depth    bigint,
  state             text        NOT NULL DEFAULT 'held',
  buyer_ctrl        bigint,                    -- set when delivered
  order_id          bigint,                    -- dune_exchange_orders.id on delivery
  held_at           timestamptz NOT NULL DEFAULT now(),
  closed_at         timestamptz,
  operator          text,

  CONSTRAINT ls_karum_escrow_state_chk
    CHECK (state IN ('held', 'delivered', 'returned', 'reconciled_missing'))
);

-- Added 2026-07-27, after the first live cancel stranded an item.
--
-- 🔴 WHY THE CATEGORY IS SNAPSHOTTED AND NOT LOOKED UP AT DELIVERY TIME.
-- Every leg that hands the item over (buy -> buyer, cancel -> seller, admin ->
-- force-deliver/force-return) builds a dune_exchange_orders row, and category_mask/depth are
-- NOT NULL there. The original builder copied them from a live order for the same template
-- at DELIVERY time. But dune.dune_exchange_orders is TRANSIENT -- it holds currently-live
-- orders, not history, and rows disappear when an order fills or is culled. So a template
-- that was categorisable when the seller listed can be uncategorisable by the time they
-- cancel, and then the item has NO route out at all: not to a buyer, not back to the seller,
-- and not via the operator page, because force-return hits the identical lookup.
--
-- Capturing the mask at LIST time removes the transience: whatever every hand-over leg
-- needs was already resolved and stored while the item was being taken. The listing leg
-- refuses outright (`no_category`) when it cannot resolve one, so an item that could not be
-- handed back is never escrowed in the first place.
--
-- Nullable, because rows written before these columns existed have no snapshot; the builder
-- falls back to the live lookup for those. New rows always carry one.
ALTER TABLE dune.ls_karum_escrow ADD COLUMN IF NOT EXISTS category_mask  bigint;
ALTER TABLE dune.ls_karum_escrow ADD COLUMN IF NOT EXISTS category_depth bigint;

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt. A Funcom migration
-- that reassigns schema objects leaves correctly-owned tables alone; a table owned by
-- postgres aborts the whole update. Gate before any battlegroup restart following a
-- Funcom update: ssh <game-host> /root/dune-owner-check.sh
ALTER TABLE dune.ls_karum_escrow OWNER TO dune;

-- The audit's hot path: everything Karum currently believes it is holding.
CREATE INDEX IF NOT EXISTS idx_hke_held
  ON dune.ls_karum_escrow (item_id) WHERE state = 'held';
CREATE INDEX IF NOT EXISTS idx_hke_listing
  ON dune.ls_karum_escrow (listing_id);
CREATE INDEX IF NOT EXISTS idx_hke_seller
  ON dune.ls_karum_escrow (seller_account_id, held_at);
