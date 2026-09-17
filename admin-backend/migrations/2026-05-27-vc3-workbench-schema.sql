-- VC3 — Player Workbench schema migration
-- Date: 2026-05-27
-- Author: VC3 ship
-- Reversible: ALTER ADD COLUMN IF NOT EXISTS is idempotent; DROP TABLE for grant_presets is safe pre-prod.
--
-- New columns on dune.ls_progression_grants:
--   batch_id              (UUID, nullable)        — groups grants fired together (preset apply, multi-recipient batch)
--   reverted_by_grant_id  (BIGINT, nullable)      — when a grant is reversed, the new grant's id goes here
--   preset_name           (TEXT, nullable)        — the preset (if any) that produced this grant
--
-- New table holadmin.grant_presets (OWNER dune mandatory — Funcom pg_dump pre-update halts otherwise).
--
-- Welcome-pack consolidation:
--   - INSERT into ls_progression_grants from ls_welcome_pack_grants (one-shot, non-destructive — original table stays)
--   - Each consolidated row gets preset_name='welcome_pack', grant_type='welcome_pack', operator='system-migration', status='applied'
--   - Original detail captured in detail jsonb (granted_items, intel_applied_at, original notes, migrated_from)
--   - DROP of dune.ls_welcome_pack_grants deferred 2 weeks (separate migration after verification)

-- ----------------------------------------------------------------------------
-- 1. ALTER existing audit ledger
-- ----------------------------------------------------------------------------

ALTER TABLE dune.ls_progression_grants
  ADD COLUMN IF NOT EXISTS batch_id              UUID,
  ADD COLUMN IF NOT EXISTS reverted_by_grant_id  BIGINT,
  ADD COLUMN IF NOT EXISTS preset_name           TEXT;

CREATE INDEX IF NOT EXISTS idx_hpg_batch_id
  ON dune.ls_progression_grants (batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_hpg_preset_name
  ON dune.ls_progression_grants (preset_name) WHERE preset_name IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 2. CREATE holadmin.grant_presets (OWNER dune — mandatory per Funcom pg_dump constraint)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS holadmin.grant_presets (
  id             BIGSERIAL PRIMARY KEY,
  name           TEXT UNIQUE NOT NULL,
  display        TEXT NOT NULL,
  description    TEXT,
  ops_json       JSONB NOT NULL,
  reversible     BOOLEAN NOT NULL DEFAULT false,
  operator_role  TEXT NOT NULL DEFAULT 'admin',
  created_by     TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE holadmin.grant_presets OWNER TO dune;

CREATE INDEX IF NOT EXISTS idx_grant_presets_name
  ON holadmin.grant_presets (name);

-- ----------------------------------------------------------------------------
-- 3. Welcome-pack consolidation (idempotent — gated on NOT EXISTS sentinel)
-- ----------------------------------------------------------------------------

DO $$
DECLARE
  src_count  INTEGER;
  dst_count  INTEGER;
BEGIN
  SELECT count(*) INTO src_count FROM dune.ls_welcome_pack_grants;
  SELECT count(*) INTO dst_count FROM dune.ls_progression_grants
    WHERE preset_name = 'welcome_pack'
      AND detail ->> 'migrated_from' = 'ls_welcome_pack_grants';

  IF dst_count > 0 THEN
    RAISE NOTICE 'welcome-pack consolidation already applied (% migrated rows). Skipping.', dst_count;
  ELSIF src_count = 0 THEN
    RAISE NOTICE 'ls_welcome_pack_grants is empty. Nothing to consolidate.';
  ELSE
    INSERT INTO dune.ls_progression_grants
      (idempotency_key, granted_at, account_id, grant_type, detail, operator, status, preset_name, applied_at)
    SELECT
      gen_random_uuid(),
      wpg.granted_at,
      wpg.account_id,
      'welcome_pack',
      jsonb_build_object(
        'granted_items',     wpg.granted_items,
        'intel_applied_at',  wpg.intel_applied_at,
        'notes',             wpg.notes,
        'migrated_from',     'ls_welcome_pack_grants',
        'bash_v1_5',         true
      ),
      'system-migration',
      'applied',
      'welcome_pack',
      COALESCE(wpg.intel_applied_at, wpg.granted_at)
    FROM dune.ls_welcome_pack_grants wpg;
    RAISE NOTICE 'welcome-pack consolidation: % rows inserted.', src_count;
  END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 4. Post-migration sanity (RAISEs only — no failure)
-- ----------------------------------------------------------------------------

DO $$
DECLARE
  hpg_owner   TEXT;
  gp_owner    TEXT;
  src_count   INTEGER;
  dst_count   INTEGER;
BEGIN
  SELECT tableowner INTO hpg_owner FROM pg_tables
    WHERE schemaname = 'dune' AND tablename = 'ls_progression_grants';
  SELECT tableowner INTO gp_owner  FROM pg_tables
    WHERE schemaname = 'holadmin' AND tablename = 'grant_presets';
  SELECT count(*) INTO src_count FROM dune.ls_welcome_pack_grants;
  SELECT count(*) INTO dst_count FROM dune.ls_progression_grants
    WHERE preset_name = 'welcome_pack'
      AND detail ->> 'migrated_from' = 'ls_welcome_pack_grants';

  RAISE NOTICE 'VC3 migration sanity:';
  RAISE NOTICE '  dune.ls_progression_grants OWNER = %', hpg_owner;
  RAISE NOTICE '  holadmin.grant_presets OWNER      = %', gp_owner;
  RAISE NOTICE '  welcome_pack source rows           = %', src_count;
  RAISE NOTICE '  welcome_pack consolidated rows     = %', dst_count;

  IF gp_owner IS NULL OR gp_owner <> 'dune' THEN
    RAISE EXCEPTION 'holadmin.grant_presets MUST be OWNER dune (got %). Funcom pg_dump pre-update would halt.', gp_owner;
  END IF;
  IF src_count <> dst_count AND dst_count > 0 THEN
    RAISE WARNING 'welcome-pack row count mismatch: source=% consolidated=%', src_count, dst_count;
  END IF;
END $$;
