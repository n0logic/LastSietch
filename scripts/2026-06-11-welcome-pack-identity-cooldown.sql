-- Welcome-pack re-roll hole fix: dedup the Sietch Welcome Package on stable
-- identity (fls_id) with a configurable cooldown instead of on account_id.
--
-- WHY: dune.ls_welcome_pack_grants dedups on account_id. A character
-- delete/recreate mints a brand-new account_id for the same Funcom identity, so
-- each re-roll passed the "account_id NOT IN grants" filter and farmed a fresh
-- full pack (one account claimed a double-digit number of packs;
-- 54 orphaned grant rows server-wide). Stable identity = dune.accounts."user"
-- (the FLS id, a quoted reserved word) / dune.accounts.funcom_id (Display#tag).
--
-- POLICY (the operator-approved): one pack per stable identity per cooldown window,
-- default 30 days, configurable. The watcher + grant.sh read
-- WELCOME_PACK_COOLDOWN_DAYS (single source of truth in the watcher env).
--
-- OWNERSHIP: dune.ls_welcome_pack_grants is already OWNER dune; ALTER preserves
-- it. The new dune.ls_welcome_pack_skips is explicitly OWNER dune so Funcom's
-- pre-update pg_dump (run as the dune role) can dump it. A ls_* table owned by
-- postgres halts the entire game update (2026-05-21 burn,
--).
--
-- APPLY ONCE, MANUALLY, BY AN OPERATOR (gated stage post-backup). Not
-- auto-applied by any service or watcher. Apply with the read/write dq.sh
-- harness:
--   sudo kubectl exec -i -n <ns> <db-pod> -- \
--     env PGPASSWORD=<pw> psql -h localhost -p 15432 -U postgres -d dune \
--     -v ON_ERROR_STOP=1 -f 2026-06-11-welcome-pack-identity-cooldown.sql

BEGIN;

-- 1. Identity columns on the grant ledger.
ALTER TABLE dune.ls_welcome_pack_grants
  ADD COLUMN IF NOT EXISTS fls_id    text,
  ADD COLUMN IF NOT EXISTS funcom_id text;

-- 2a. Backfill identity for grants whose account still exists.
UPDATE dune.ls_welcome_pack_grants g
   SET fls_id    = a."user",
       funcom_id = a.funcom_id
  FROM dune.accounts a
 WHERE a.id = g.account_id
   AND g.fls_id IS NULL;

-- 2b. Backfill orphans (account deleted via re-roll) from the removal log.
--     account_removal_log only carries fls_id, so funcom_id stays NULL here.
UPDATE dune.ls_welcome_pack_grants g
   SET fls_id = rl.fls_id
  FROM (
    SELECT DISTINCT ON (account_id) account_id, fls_id
      FROM dune.account_removal_log
     ORDER BY account_id, event_time DESC
  ) rl
 WHERE g.fls_id IS NULL
   AND g.account_id = rl.account_id;

-- 3. Non-unique index: cooldown policy allows repeats over time, so this is a
--    lookup index, NOT a unique constraint.
CREATE INDEX IF NOT EXISTS ls_welcome_pack_grants_fls_idx
  ON dune.ls_welcome_pack_grants (fls_id, granted_at DESC);

-- 4. Cooldown-skip suppression ledger. A re-roll account has no grant row of its
--    own, so without this marker the watcher would re-send the eligibility
--    whisper every 60s sweep. One row per skipped account_id; the whisper fires
--    once.
CREATE TABLE IF NOT EXISTS dune.ls_welcome_pack_skips (
  account_id             bigint      PRIMARY KEY,
  fls_id                 text,
  remaining_days_at_skip int,
  notified_at            timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE dune.ls_welcome_pack_skips OWNER TO dune;

CREATE INDEX IF NOT EXISTS ls_welcome_pack_skips_fls_idx
  ON dune.ls_welcome_pack_skips (fls_id);

COMMIT;

-- ---------------------------------------------------------------------------
-- ONE-SHOT AUDIT (read-only; run after applying). Identities that historically
-- received more than one pack — the backfill makes the re-roll farming visible:
--
--   SELECT fls_id,
--          COUNT(*)        AS packs,
--          MIN(granted_at) AS first_pack,
--          MAX(granted_at) AS last_pack
--     FROM dune.ls_welcome_pack_grants
--    WHERE fls_id IS NOT NULL
--    GROUP BY fls_id
--   HAVING COUNT(*) > 1
--    ORDER BY packs DESC;
-- ---------------------------------------------------------------------------
