"""
SQLite store for the Last Sietch Dune telemetry logger.

This is the logger's OWN database. The logger writes here and ONLY here -
it never writes to the game DB (see gamedb.py). The store is opened in WAL
mode so the read API can read concurrently while the logger writes.

All timestamps for the sampler-derived tables (presence, world_snapshots,
connections, geoip_cache) are Unix epoch integers, to stay diff-compatible
with the old lastsietch-stats stats.db. Game-event / position tables carry the
Postgres timestamp as ISO-8601 text plus a derived epoch integer for range
queries.
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- ---- Phase 1 ----

CREATE TABLE IF NOT EXISTS presence (
    ts          INTEGER NOT NULL,
    account_id  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_presence_ts ON presence(ts);

CREATE TABLE IF NOT EXISTS world_snapshots (
    ts      INTEGER NOT NULL,
    metric  TEXT    NOT NULL,
    value   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_world_ts ON world_snapshots(ts);

CREATE TABLE IF NOT EXISTS connections (
    conn_epoch    INTEGER NOT NULL,
    ip            TEXT    NOT NULL,
    country       TEXT,
    country_code  TEXT
);
CREATE INDEX IF NOT EXISTS ix_conn_epoch ON connections(conn_epoch);
CREATE UNIQUE INDEX IF NOT EXISTS ux_conn ON connections(conn_epoch, ip);

CREATE TABLE IF NOT EXISTS geoip_cache (
    ip            TEXT PRIMARY KEY,
    country       TEXT,
    country_code  TEXT,
    resolved_ts   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS roster_snapshot (
    ts              INTEGER NOT NULL,
    account_id      TEXT,
    character_name  TEXT,
    map             TEXT,
    guild           TEXT,
    faction         TEXT
);
CREATE INDEX IF NOT EXISTS ix_roster_ts ON roster_snapshot(ts);

-- ---- Phase 2 ----

CREATE TABLE IF NOT EXISTS combat_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key          TEXT    NOT NULL UNIQUE,
    occurred_at        TEXT    NOT NULL,
    occurred_epoch     INTEGER NOT NULL,
    map                TEXT,
    partition_id       INTEGER,
    event_type         INTEGER NOT NULL,
    actor_id           INTEGER,
    victim_account_id  TEXT,
    victim_name        TEXT,
    killer_type        TEXT,
    killer_account_id  TEXT,
    killer_name        TEXT,
    damage_type        TEXT,
    causer_row_index   INTEGER,
    x                  REAL,
    y                  REAL,
    z                  REAL,
    raw                TEXT,
    harvested_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_combat_epoch  ON combat_events(occurred_epoch);
CREATE INDEX IF NOT EXISTS ix_combat_type   ON combat_events(event_type);
CREATE INDEX IF NOT EXISTS ix_combat_killer ON combat_events(killer_account_id);

-- ---- Phase 3 ----

CREATE TABLE IF NOT EXISTS vehicle_positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    vehicle_id        INTEGER NOT NULL,
    vehicle_class     TEXT    NOT NULL,
    pilot_account_id  TEXT,
    pilot_name        TEXT,
    x                 REAL,
    y                 REAL,
    z                 REAL
);
CREATE INDEX IF NOT EXISTS ix_vpos_vehicle_ts ON vehicle_positions(vehicle_id, ts);
CREATE INDEX IF NOT EXISTS ix_vpos_ts         ON vehicle_positions(ts);

CREATE TABLE IF NOT EXISTS flight_distance_weekly (
    iso_week        TEXT    NOT NULL,
    account_id      TEXT    NOT NULL,
    vehicle_class   TEXT    NOT NULL,
    meters          REAL    NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (iso_week, account_id, vehicle_class)
);

-- ---- Stream A (live map) ----

CREATE TABLE IF NOT EXISTS player_positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    map          TEXT    NOT NULL,
    partition_id INTEGER,
    x            REAL,
    y            REAL
);
CREATE INDEX IF NOT EXISTS ix_ppos_ts ON player_positions(ts);

-- ---- Item C (grant ledger) ----

CREATE TABLE IF NOT EXISTS grant_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pg_grant_id    INTEGER NOT NULL UNIQUE,
    granted_at     TEXT    NOT NULL,
    granted_epoch  INTEGER NOT NULL,
    account_id     TEXT,
    grant_type     TEXT    NOT NULL,
    detail         TEXT,
    operator       TEXT,
    status         TEXT,
    harvested_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_grant_events_epoch   ON grant_events(granted_epoch);
CREATE INDEX IF NOT EXISTS ix_grant_events_account ON grant_events(account_id);
CREATE INDEX IF NOT EXISTS ix_grant_events_type    ON grant_events(grant_type);

-- ---- Stream D (progression snapshots) ----

CREATE TABLE IF NOT EXISTS player_progression (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    account_id      TEXT    NOT NULL,
    char_name       TEXT,
    online_status   TEXT,
    xp              INTEGER NOT NULL,
    lvl             INTEGER NOT NULL,
    total_sp        INTEGER NOT NULL,
    unspent_sp      INTEGER NOT NULL,
    keystone_sp     INTEGER NOT NULL,
    intel           INTEGER NOT NULL,
    sample_hash     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pprog_account_ts ON player_progression(account_id, ts);
CREATE INDEX IF NOT EXISTS ix_pprog_ts         ON player_progression(ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pprog_account_hash
    ON player_progression(account_id, sample_hash);

CREATE TABLE IF NOT EXISTS player_progression_levelups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    account_id    TEXT    NOT NULL,
    char_name     TEXT,
    from_lvl      INTEGER NOT NULL,
    to_lvl        INTEGER NOT NULL,
    from_xp       INTEGER NOT NULL,
    to_xp         INTEGER NOT NULL,
    crossed_lvl   INTEGER NOT NULL,
    detected_at   INTEGER NOT NULL,
    UNIQUE(account_id, crossed_lvl, to_xp)
);
CREATE INDEX IF NOT EXISTS ix_plvl_ts      ON player_progression_levelups(ts);
CREATE INDEX IF NOT EXISTS ix_plvl_account ON player_progression_levelups(account_id, ts);

-- ---- Transfers / removals (account_removal_log + character_transfer_imports) ----

CREATE TABLE IF NOT EXISTS transfer_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    account_id      TEXT,
    fls_id          TEXT,
    transfer_state  TEXT,
    raw_json        TEXT    NOT NULL,
    dedup_key       TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_transfer_events_ts  ON transfer_events(ts);
CREATE INDEX IF NOT EXISTS ix_transfer_events_fls ON transfer_events(fls_id);
CREATE INDEX IF NOT EXISTS ix_transfer_events_aid ON transfer_events(account_id);

-- ---- Read-model mirror (portal/admin local read layer; see
--      docs/dune-research/PHASE-1-MIRROR-CONTRACT-2026-06-05.md) ----
-- One row per account. Section blobs hold the EXACT relay payloads they replace
-- so the web host read path is a drop-in. Denormalized scalars (online/lvl/
-- intel/char_name/current_map) come from the global snapshot + roster calls.
-- Full-refresh each cycle (upsert by account_id) -> deletes handled for free.

CREATE TABLE IF NOT EXISTS player_read_model (
    account_id            INTEGER PRIMARY KEY,
    char_name             TEXT,
    online                INTEGER NOT NULL DEFAULT 0,
    lvl                   INTEGER,
    intel                 INTEGER,
    current_map           TEXT,
    progress_json         TEXT,
    specializations_json  TEXT,
    landsraad_json        TEXT,
    tags_json             TEXT,
    src_synced_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_meta (
    section       TEXT PRIMARY KEY,
    last_run_at   TEXT,
    last_pull_at  TEXT,
    row_count     INTEGER,
    ok            INTEGER NOT NULL DEFAULT 1,
    note          TEXT
);

-- ---- Phase 2 mirror: storage + market (see
--      docs/dune-research/PHASE-2-MIRROR-CONTRACT-2026-06-05.md) ----
-- Per-account storage snapshot blob: one document feeding the container list,
-- the per-container item drilldown (paginated locally), and the cross-container
-- "find an item" search. Full-refresh by account_id each cycle.
CREATE TABLE IF NOT EXISTS player_storage (
    account_id     INTEGER PRIMARY KEY,
    storage_json   TEXT,
    src_synced_at  TEXT NOT NULL
);

-- Full active CHOAM exchange listing set (global). The mirror runs the substring
-- search LOCALLY over this table. Anonymous rows -> full-refresh (delete-all +
-- bulk insert) each cycle; no upsert key.
CREATE TABLE IF NOT EXISTS market_listing (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id    TEXT NOT NULL,
    item_price     INTEGER,
    quality_level  INTEGER,
    is_npc_order   INTEGER,
    stack          INTEGER,
    owner_id       INTEGER,
    order_id       INTEGER,
    revision       INTEGER,
    src_synced_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_market_listing_tpl ON market_listing(template_id);

-- ---- World events (sandstorm spawns + sandworm breaches from pod logs) ----
-- Low-frequency map events harvested from a bounded `kubectl logs --tail` of the
-- game pods. dedup_key = (event_type, dimension, log ts [, worm id]) so the
-- overlapping tails of consecutive stream runs are idempotent.

CREATE TABLE IF NOT EXISTS world_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    map         TEXT    NOT NULL,
    dimension   TEXT,
    event_type  TEXT    NOT NULL,
    dedup_key   TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_world_events_ts   ON world_events(ts);
CREATE INDEX IF NOT EXISTS ix_world_events_type ON world_events(event_type);

-- ---- Login rewards (login-rewards V2) ----
-- One row per online ACCOUNT per UTC day (login_days stream). The composite PK
-- (account_id, date_utc) gives per-account streak scans for free and makes each
-- same-day sweep an idempotent INSERT OR IGNORE, so first_seen_ts records the
-- first sighting of the day. Boundary = UTC midnight (date(ts,'unixepoch')).

CREATE TABLE IF NOT EXISTS portal_login_days (
    account_id     TEXT    NOT NULL,   -- TEXT to match presence + the other per-account tables
    date_utc       TEXT    NOT NULL,
    first_seen_ts  INTEGER NOT NULL,
    PRIMARY KEY (account_id, date_utc)
);

-- ---- service state ----

CREATE TABLE IF NOT EXISTS cursors (
    name        TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
"""


def open_db(path):
    """Open (creating if needed) the telemetry SQLite store with WAL enabled."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL is required: the old stats.db is NOT WAL, the new DB must be so the
    # read API can read concurrently with the logger writing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _add_column_if_missing(conn, table, column, ddl):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_schema(conn):
    conn.executescript(SCHEMA)
    # Lightweight migrations for stores created before a column was added.
    _add_column_if_missing(conn, "player_positions", "partition_id", "partition_id INTEGER")
    # order_id/revision carry the listing identity into a portal BUY (revision-drift guard).
    _add_column_if_missing(conn, "market_listing", "order_id", "order_id INTEGER")
    _add_column_if_missing(conn, "market_listing", "revision", "revision INTEGER")
    conn.commit()


def get_cursor(conn, name):
    row = conn.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
    return row["value"] if row else None


def set_cursor(conn, name, value, updated_at):
    conn.execute(
        "INSERT INTO cursors(name, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (name, str(value), updated_at))


def insert_many(conn, table, columns, rows):
    """Bulk insert. Returns the number of rows the DB reports as changed."""
    if not rows:
        return 0
    placeholders = ",".join("?" * len(columns))
    cols = ",".join(columns)
    cur = conn.executemany(
        f"INSERT INTO {table}({cols}) VALUES({placeholders})", rows)
    return cur.rowcount


def insert_many_ignore(conn, table, columns, rows):
    """Bulk INSERT OR IGNORE - used for dedup on a UNIQUE index."""
    if not rows:
        return 0
    placeholders = ",".join("?" * len(columns))
    cols = ",".join(columns)
    inserted = 0
    for row in rows:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {table}({cols}) VALUES({placeholders})", row)
        inserted += cur.rowcount
    return inserted


_READ_MODEL_COLUMNS = [
    "account_id", "char_name", "online", "lvl", "intel", "current_map",
    "progress_json", "specializations_json", "landsraad_json", "tags_json",
    "src_synced_at",
]


def upsert_read_models(conn, rows):
    """Upsert player_read_model rows keyed by account_id, then prune accounts no
    longer present (full-refresh semantics: handles deletes). `rows` is a list of
    dicts with the _READ_MODEL_COLUMNS keys. Returns (upserted, pruned)."""
    if not rows:
        return 0, 0
    cols = ",".join(_READ_MODEL_COLUMNS)
    placeholders = ",".join("?" * len(_READ_MODEL_COLUMNS))
    updates = ",".join(
        f"{c}=excluded.{c}" for c in _READ_MODEL_COLUMNS if c != "account_id")
    sql = (f"INSERT INTO player_read_model({cols}) VALUES({placeholders}) "
           f"ON CONFLICT(account_id) DO UPDATE SET {updates}")
    seen = []
    for r in rows:
        conn.execute(sql, [r.get(c) for c in _READ_MODEL_COLUMNS])
        seen.append(r["account_id"])
    qmarks = ",".join("?" * len(seen))
    pruned = conn.execute(
        f"DELETE FROM player_read_model WHERE account_id NOT IN ({qmarks})",
        seen).rowcount
    return len(seen), pruned


def set_sync_meta(conn, section, *, last_run_at=None, last_pull_at=None,
                  row_count=None, ok=1, note=None):
    """Upsert a sync_meta row, leaving any field passed as None unchanged on update."""
    conn.execute(
        "INSERT INTO sync_meta(section, last_run_at, last_pull_at, row_count, ok, note) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET "
        "last_run_at=coalesce(excluded.last_run_at, sync_meta.last_run_at), "
        "last_pull_at=coalesce(excluded.last_pull_at, sync_meta.last_pull_at), "
        "row_count=coalesce(excluded.row_count, sync_meta.row_count), "
        "ok=excluded.ok, note=excluded.note",
        (section, last_run_at, last_pull_at, row_count, ok, note))


def upsert_storage(conn, rows, prune_keep=None):
    """Upsert player_storage rows keyed by account_id. Prune accounts no longer
    present: by default against the upserted set, but pass `prune_keep` (the full
    current account-id list) so an account that FAILED to build this cycle keeps
    its last-good row instead of being deleted. `rows` = dicts with account_id,
    storage_json, src_synced_at. Returns (upserted, pruned)."""
    sql = ("INSERT INTO player_storage(account_id, storage_json, src_synced_at) "
           "VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
           "storage_json=excluded.storage_json, src_synced_at=excluded.src_synced_at")
    seen = []
    for r in rows:
        conn.execute(sql, (r["account_id"], r.get("storage_json"), r["src_synced_at"]))
        seen.append(r["account_id"])
    keep = prune_keep if prune_keep is not None else seen
    if not keep:
        return len(seen), 0
    qmarks = ",".join("?" * len(keep))
    pruned = conn.execute(
        f"DELETE FROM player_storage WHERE account_id NOT IN ({qmarks})", list(keep)).rowcount
    return len(seen), pruned


def replace_market_listings(conn, listings, src_synced_at):
    """Full-refresh the global market_listing table: delete all, bulk insert the
    current active listing set. `listings` = list of dicts with template_id,
    item_price, quality_level, is_npc_order, stack, owner_id. Returns inserted count."""
    conn.execute("DELETE FROM market_listing")
    if not listings:
        return 0
    rows = [(
        l.get("template_id"), l.get("item_price"), l.get("quality_level"),
        1 if l.get("is_npc_order") else 0, l.get("stack"), l.get("owner_id"),
        l.get("order_id"), l.get("revision"), src_synced_at,
    ) for l in listings]
    conn.executemany(
        "INSERT INTO market_listing(template_id, item_price, quality_level, "
        "is_npc_order, stack, owner_id, order_id, revision, src_synced_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)
