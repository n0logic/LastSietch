-- VC3 v1.1 — Add parameters column to holadmin.grant_presets
-- Date: 2026-05-27
--
-- Presets with runtime parameters (e.g. level_200_kit with faction choice)
-- need a schema slot. Stored as JSONB array of param descriptors:
--   [{ "name": "faction", "label": "Faction", "type": "choice",
--      "choices": ["atreides", "harkonnen"] }]
-- Default is empty array — most presets are parameter-free.

ALTER TABLE holadmin.grant_presets
  ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '[]'::jsonb;
