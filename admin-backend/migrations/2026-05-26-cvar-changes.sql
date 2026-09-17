-- P8 — CVars admin sub-tab. Audit ledger for every Operator-initiated INI
-- write. Lives in the DUNE Postgres database (NOT admin-backend SQLite) so
-- the relay can INSERT transactionally with the file write.
--
-- Schema source of truth: docs/dune-research/P8-EXECUTION-BRIEF.md Appendix B.5.
--
-- OWNER dune is MANDATORY per the custom-table ownership rule —
-- Funcom's pre-update pg_dump halts the entire game update if any custom
-- table in the dune database has a non-`dune` owner.
--
-- Apply procedure (admin-backend has no migration runner; the operator applies
-- manually on the dune Postgres host before P8 deploy):
--
--   ssh dune-pg "psql -U postgres -d dune -f -" \
--     < admin-backend/migrations/2026-05-26-cvar-changes.sql
--
-- Re-run safe: CREATE SCHEMA IF NOT EXISTS + CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS. ALTER OWNER is idempotent.

CREATE SCHEMA IF NOT EXISTS holadmin AUTHORIZATION dune;

CREATE TABLE IF NOT EXISTS holadmin.cvar_changes (
  id                   BIGSERIAL PRIMARY KEY,
  ts                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  operator_discord_id  TEXT NOT NULL,
  cvar_key             TEXT NOT NULL,
  old_value            TEXT,
  new_value            TEXT NOT NULL,
  reason               TEXT,
  ini_layer_before     TEXT,
  relay_audit_json     JSONB,
  status               TEXT NOT NULL CHECK (status IN ('applied','failed','rolled_back'))
);

ALTER TABLE holadmin.cvar_changes OWNER TO dune;

CREATE INDEX IF NOT EXISTS idx_cvar_changes_ts
  ON holadmin.cvar_changes(ts DESC);

CREATE INDEX IF NOT EXISTS idx_cvar_changes_key
  ON holadmin.cvar_changes(cvar_key);
