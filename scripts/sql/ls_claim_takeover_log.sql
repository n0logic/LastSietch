-- Audit trail for admin adoption of ORPHANED land claims.
--
-- An orphan is a base still standing whose ownership record is gone, because
-- dune.permission_actor_rank.player_id FKs to actors ON DELETE CASCADE: deleting
-- the owner's character silently erases who owned the base while the base stays
-- up. Nobody can be contacted about them. There are 9 as of 2026-07-27.
--
-- Why an audit table when the takeover itself is reversible: the game DB keeps
-- no history of who held a claim. Once dune.permission_actor_takeover runs, the
-- ONLY record that this totem was ever an orphan, and that a human chose to
-- adopt it, is this row. Without it a later reader sees a normal owned base and
-- has no way to tell an adopted orphan from one the player built. That matters
-- if a former owner ever comes back asking.
--
-- Rows are written BEFORE the proc call and marked applied after, so a crash
-- between the two leaves evidence rather than a silent gap. Reversals are a new
-- row with action='revert', never an UPDATE of the original: an audit log that
-- can be edited is not an audit log.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS dune.ls_claim_takeover_log (
  id                 bigserial PRIMARY KEY,
  totem_id           bigint      NOT NULL,
  action             text        NOT NULL,
  account_id         bigint      NOT NULL,
  player_id          bigint      NOT NULL,   -- player_controller_id, what rank rows key on
  character_name     text,
  -- What the claim looked like at adoption time. The base can be dismantled or
  -- decay afterwards, so these are the only numbers that describe what was
  -- actually taken on.
  map                text,
  dimension_index    integer,
  world_x            bigint,
  world_y            bigint,
  pieces             integer,
  placeables         integer,
  prior_ownership    text,                   -- expected 'orphaned'; recorded, not assumed
  operator           text,
  requested_at       timestamptz NOT NULL DEFAULT now(),
  applied_at         timestamptz,
  outcome            text,
  detail             text,

  CONSTRAINT ls_cto_action_chk CHECK (action IN ('takeover', 'revert'))
);

-- Ownership: REQUIRED so Funcom's pre-update pg_dump does not halt. A Funcom
-- migration that reassigns schema objects leaves correctly-owned tables alone; a
-- table owned by postgres aborts the whole update. Gate before any battlegroup
-- restart following a Funcom update: ssh <game-host> /root/dune-owner-check.sh
ALTER TABLE dune.ls_claim_takeover_log OWNER TO dune;
ALTER SEQUENCE dune.ls_claim_takeover_log_id_seq OWNER TO dune;

CREATE INDEX IF NOT EXISTS idx_hcto_totem ON dune.ls_claim_takeover_log (totem_id, requested_at);
CREATE INDEX IF NOT EXISTS idx_hcto_account ON dune.ls_claim_takeover_log (account_id, requested_at);

-- Added 2026-07-27 with the owned-claim override. Taking a claim from a player
-- who still exists is only defensible if it is undoable, and undoing it needs
-- the previous owner recorded at the moment of the takeover: the game DB keeps
-- no ownership history, so once the rank row is replaced nothing anywhere
-- remembers who held it. These two columns ARE the undo.
ALTER TABLE dune.ls_claim_takeover_log
  ADD COLUMN IF NOT EXISTS prior_owner_player_id bigint,
  ADD COLUMN IF NOT EXISTS prior_owner_name      text;
