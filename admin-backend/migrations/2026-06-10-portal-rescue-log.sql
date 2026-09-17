-- 2026-06-10 portal_rescue_log — durable 1/hour cooldown table for the portal
-- self-rescue ("I'm stuck") teleport (Feature B-2b).
--
-- NOTE ON APPLICATION: unlike the other files in this dir (which are Postgres
-- holadmin.* schema applied manually with psql), this table lives in the
-- admin-backend SQLite admin.db — the same DB as users / sessions / audit_log /
-- portal_link_attempts. Per migrations/README.md, *new SQLite tables go in
-- database.py::init_db()*, which runs idempotently on every boot. This table is
-- therefore ALREADY created on boot by the CREATE TABLE IF NOT EXISTS block added
-- to database.py's SCHEMA. This file is the human-readable record of that change;
-- it does NOT need to be run with psql (the SCHEMA block is the source of truth).
--
-- Re-run-safe SQLite reference DDL (matches database.py SCHEMA exactly):

CREATE TABLE IF NOT EXISTS portal_rescue_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,                  -- ls_account_links.account_id
    used_at     TEXT    NOT NULL,                  -- UTC 'YYYY-MM-DD HH:MM:SS'
    from_x      INTEGER,                           -- player position before teleport
    from_y      INTEGER,
    from_map    TEXT,                              -- map the player was on
    to_base_x   INTEGER,                           -- chosen destination base totem
    to_base_y   INTEGER,
    CHECK (account_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_prl_account_used
    ON portal_rescue_log(account_id, used_at);
