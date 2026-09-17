"""Local read-model mirror for the portal/admin fast read path.

A WAL-mode SQLite file on <web-host> (config.MIRROR_DB_PATH), kept in sync by a
background loop that pulls /dune/read-models via the relay and upserts here. The
portal/admin read path then reads this LOCAL store instead of crossing the
internet to the game DB on each page load.

The sync loop always runs (so the mirror stays warm); only the get_* readers are
gated by config.PORTAL_MIRROR_READS. A row older than config.MIRROR_MAX_STALE is
treated as a miss so a stalled collector/sync degrades to the live relay path,
never to ancient data. See docs/dune-research/PHASE-1-MIRROR-CONTRACT-2026-06-05.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone

import config
import relay

log = logging.getLogger("lastsietch-admin.mirror")

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS player_storage (
    account_id     INTEGER PRIMARY KEY,
    storage_json   TEXT,
    src_synced_at  TEXT NOT NULL
);
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
CREATE TABLE IF NOT EXISTS bot_price (
    template_id    TEXT PRIMARY KEY,
    buyable        INTEGER NOT NULL DEFAULT 0,
    caps_json      TEXT,
    src_synced_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_limit_snapshot (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json   TEXT NOT NULL,
    src_synced_at  TEXT NOT NULL
);
"""

# bot-prices staleness ceiling: the producer (lastsietch-market-bot) rewrites its
# export only once per buy tick (300s), the same as MIRROR_MAX_STALE, so the
# generic window would flap stale at the tail of every tick. Bot prices move
# at most once per list tick (30 min); a 20-minute ceiling is still "live".
_BOT_PRICES_MAX_STALE = 1200
_BOT_LIMITS_MAX_STALE = 1200

# Page size for the per-container item drilldown, matching the live
# dune-container-items.py PAGE_SIZE so local pagination reproduces live pages.
_STORAGE_PAGE_SIZE = 100

_SECTION_COL = {
    "progress": "progress_json",
    "specializations": "specializations_json",
    "landsraad": "landsraad_json",
    "tags": "tags_json",
}

_UPSERT = (
    "INSERT INTO player_read_model(account_id, char_name, online, lvl, intel, "
    "current_map, progress_json, specializations_json, landsraad_json, tags_json, "
    "src_synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(account_id) DO UPDATE SET "
    "char_name=excluded.char_name, online=excluded.online, lvl=excluded.lvl, "
    "intel=excluded.intel, current_map=excluded.current_map, "
    "progress_json=excluded.progress_json, "
    "specializations_json=excluded.specializations_json, "
    "landsraad_json=excluded.landsraad_json, tags_json=excluded.tags_json, "
    "src_synced_at=excluded.src_synced_at"
)


def _connect():
    conn = sqlite3.connect(config.MIRROR_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init():
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        # Idempotent column adds for DBs created before order_id/revision (the
        # BUY path carries them so the writer can verify the listing revision).
        for col in ("order_id", "revision"):
            try:
                conn.execute(f"ALTER TABLE market_listing ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass  # already present
        conn.commit()
    finally:
        conn.close()


def _dump(v):
    return json.dumps(v) if v is not None else None


def apply_snapshot(payload):
    """Upsert a /dune/read-models payload into the local mirror; prune accounts
    no longer present; stamp sync_meta. Returns the number of players applied."""
    players = payload.get("players") or []
    now_pull = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        seen = []
        for p in players:
            aid = p["account_id"]
            seen.append(aid)
            conn.execute(_UPSERT, (
                aid, p.get("char_name"), 1 if p.get("online") else 0,
                p.get("lvl"), p.get("intel"), p.get("current_map"),
                _dump(p.get("progress")), _dump(p.get("specializations")),
                _dump(p.get("landsraad")), _dump(p.get("tags")),
                p.get("src_synced_at") or now_pull,
            ))
        if seen:
            qmarks = ",".join("?" * len(seen))
            conn.execute(
                f"DELETE FROM player_read_model WHERE account_id NOT IN ({qmarks})",
                seen)
        sync = payload.get("sync") or {}
        conn.execute(
            "INSERT INTO sync_meta(section, last_run_at, last_pull_at, row_count, ok, note) "
            "VALUES('read_models',?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET "
            "last_run_at=excluded.last_run_at, last_pull_at=excluded.last_pull_at, "
            "row_count=excluded.row_count, ok=excluded.ok, note=excluded.note",
            (sync.get("last_run_at"), now_pull, len(players),
             sync.get("ok", 1), sync.get("note")))
        conn.commit()
        return len(players)
    finally:
        conn.close()


def apply_storage_snapshot(payload):
    """Upsert a /dune/storage-models payload into player_storage; prune accounts
    no longer present; stamp sync_meta('storage'). Returns players applied."""
    players = payload.get("players") or []
    now_pull = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        seen = []
        for p in players:
            aid = p["account_id"]
            seen.append(aid)
            conn.execute(
                "INSERT INTO player_storage(account_id, storage_json, src_synced_at) "
                "VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "storage_json=excluded.storage_json, src_synced_at=excluded.src_synced_at",
                (aid, _dump(p.get("storage")), p.get("src_synced_at") or now_pull))
        if seen:
            qmarks = ",".join("?" * len(seen))
            conn.execute(
                f"DELETE FROM player_storage WHERE account_id NOT IN ({qmarks})", seen)
        sync = payload.get("sync") or {}
        conn.execute(
            "INSERT INTO sync_meta(section, last_run_at, last_pull_at, row_count, ok, note) "
            "VALUES('storage',?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET "
            "last_run_at=excluded.last_run_at, last_pull_at=excluded.last_pull_at, "
            "row_count=excluded.row_count, ok=excluded.ok, note=excluded.note",
            (sync.get("last_run_at"), now_pull, len(players),
             sync.get("ok", 1), sync.get("note")))
        conn.commit()
        return len(players)
    finally:
        conn.close()


def apply_market_snapshot(payload):
    """Full-refresh market_listing from a /dune/market-listings-all payload (delete
    all + bulk insert); stamp sync_meta('market'). Returns listings applied."""
    listings = payload.get("listings") or []
    now_pull = datetime.now(timezone.utc).isoformat()
    src = payload.get("src_synced_at") or now_pull
    conn = _connect()
    try:
        # V2 Exchange price-history: downsample THIS snapshot to one point per
        # template (throttled + pruned inside market_history, which lives in
        # admin.db, not this mirror DB) BEFORE we refresh the live listing table.
        # Best-effort and fully isolated: a failure here never breaks the market
        # mirror refresh.
        try:
            import market_history
            market_history.capture(market_history.aggregate_listings(listings))
        except Exception as exc:  # noqa: BLE001
            log.warning("mirror: market price-history capture failed: %s", exc)
        conn.execute("DELETE FROM market_listing")
        if listings:
            conn.executemany(
                "INSERT INTO market_listing(template_id, item_price, quality_level, "
                "is_npc_order, stack, owner_id, order_id, revision, src_synced_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(l.get("template_id"), l.get("item_price"), l.get("quality_level"),
                  1 if l.get("is_npc_order") else 0, l.get("stack"), l.get("owner_id"),
                  l.get("order_id"), l.get("revision"), src) for l in listings])
        sync = payload.get("sync") or {}
        conn.execute(
            "INSERT INTO sync_meta(section, last_run_at, last_pull_at, row_count, ok, note) "
            "VALUES('market',?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET "
            "last_run_at=excluded.last_run_at, last_pull_at=excluded.last_pull_at, "
            "row_count=excluded.row_count, ok=excluded.ok, note=excluded.note",
            (sync.get("last_run_at"), now_pull, len(listings),
             sync.get("ok", 1), sync.get("note")))
        conn.commit()
        return len(listings)
    finally:
        conn.close()


def apply_bot_prices_snapshot(payload):
    """Full-refresh bot_price from a /dune/market/bot-prices payload (delete all
    + bulk insert); stamp sync_meta('bot_prices'). An empty/missing items list is
    treated as a failed pull (last-good rows stay; the producer always exports
    the full catalog). Returns rows applied."""
    items = payload.get("items") or []
    now_pull = datetime.now(timezone.utc).isoformat()
    src = payload.get("src_synced_at") or now_pull
    conn = _connect()
    try:
        if items:
            conn.execute("DELETE FROM bot_price")
            conn.executemany(
                "INSERT INTO bot_price(template_id, buyable, caps_json, "
                "src_synced_at) VALUES(?,?,?,?)",
                [(i.get("template_id"), 1 if i.get("buyable") else 0,
                  _dump(i.get("caps")), src) for i in items if i.get("template_id")])
        conn.execute(
            "INSERT INTO sync_meta(section, last_run_at, last_pull_at, row_count, ok, note) "
            "VALUES('bot_prices',?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET "
            "last_run_at=excluded.last_run_at, last_pull_at=excluded.last_pull_at, "
            "row_count=excluded.row_count, ok=excluded.ok, note=excluded.note",
            (src, now_pull, len(items), 1 if items else 0,
             None if items else "empty payload"))
        conn.commit()
        return len(items)
    finally:
        conn.close()


def apply_bot_limits_snapshot(payload):
    """Store the /dune/market/bot-limits payload (single-row JSON blob). An empty
    scopes list is treated as a failed pull (last-good row stays). Stamps
    sync_meta('bot_limits'). Returns scope count."""
    scopes = (payload or {}).get("scopes") or []
    now_pull = datetime.now(timezone.utc).isoformat()
    src = (payload or {}).get("updated_at") or now_pull
    conn = _connect()
    try:
        if scopes:
            conn.execute("DELETE FROM bot_limit_snapshot")
            conn.execute(
                "INSERT INTO bot_limit_snapshot(id, payload_json, src_synced_at) "
                "VALUES(1,?,?)", (_dump(payload), src))
        conn.execute(
            "INSERT INTO sync_meta(section, last_run_at, last_pull_at, row_count, ok, note) "
            "VALUES('bot_limits',?,?,?,?,?) ON CONFLICT(section) DO UPDATE SET "
            "last_run_at=excluded.last_run_at, last_pull_at=excluded.last_pull_at, "
            "row_count=excluded.row_count, ok=excluded.ok, note=excluded.note",
            (src, now_pull, len(scopes), 1 if scopes else 0,
             None if scopes else "empty payload"))
        conn.commit()
        return len(scopes)
    finally:
        conn.close()


def bot_limits_snapshot():
    """The bot's per-category weekly buy/sell budget usage payload
    ({updated_at, window_days, scopes:[...]}), or None on flag-off / miss / stale."""
    if not config.PORTAL_MIRROR_READS:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT payload_json, src_synced_at FROM bot_limit_snapshot "
                "WHERE id=1").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: bot_limits_snapshot failed: %s", exc)
        return None
    if not row or _is_stale(row["src_synced_at"], _BOT_LIMITS_MAX_STALE):
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:  # noqa: BLE001
        return None


def bot_prices_all():
    """The NPC buyer's full price-cap table: {template_id_lower: {"buyable":
    bool, "caps": {grade: cap} | None}}, or None on flag-off / miss / stale.
    Caps are the per-unit price at/below which the market-maker bot still buys
    a player listing of that quality grade."""
    if not config.PORTAL_MIRROR_READS:
        return None
    try:
        conn = _connect()
        try:
            fresh = conn.execute(
                "SELECT src_synced_at FROM bot_price LIMIT 1").fetchone()
            if not fresh or _is_stale(fresh["src_synced_at"],
                                      _BOT_PRICES_MAX_STALE):
                return None
            rows = conn.execute(
                "SELECT template_id, buyable, caps_json FROM bot_price").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: bot_prices_all failed: %s", exc)
        return None
    out = {}
    for r in rows:
        caps = None
        if r["caps_json"]:
            try:
                caps = json.loads(r["caps_json"])
            except Exception:  # noqa: BLE001
                caps = None
        out[r["template_id"].lower()] = {
            "template_id": r["template_id"],
            "buyable": bool(r["buyable"]),
            "caps": caps,
        }
    return out


def bot_price_for(template_id):
    """The NPC buyer's price caps for one item (case-insensitive), or None on
    flag-off / miss / stale / unknown item."""
    if not config.PORTAL_MIRROR_READS or not template_id:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT template_id, buyable, caps_json, src_synced_at "
                "FROM bot_price WHERE template_id = ? COLLATE NOCASE",
                (template_id,)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: bot_price_for(%s) failed: %s", template_id, exc)
        return None
    if not row or _is_stale(row["src_synced_at"], _BOT_PRICES_MAX_STALE):
        return None
    caps = None
    if row["caps_json"]:
        try:
            caps = json.loads(row["caps_json"])
        except Exception:  # noqa: BLE001
            caps = None
    return {"template_id": row["template_id"],
            "buyable": bool(row["buyable"]), "caps": caps}


def _is_stale(src_synced_at, max_stale=None):
    if not src_synced_at:
        return True
    try:
        t = datetime.fromisoformat(src_synced_at)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        limit = max_stale if max_stale is not None else config.MIRROR_MAX_STALE
        return (datetime.now(timezone.utc) - t).total_seconds() > limit
    except Exception:  # noqa: BLE001
        return True


def get_section(account_id, section):
    """Parsed section payload from the local mirror, or None on flag-off / miss /
    stale / parse error (caller then falls back to the live relay)."""
    if not config.PORTAL_MIRROR_READS:
        return None
    col = _SECTION_COL.get(section)
    if not col:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                f"SELECT {col} AS blob, src_synced_at FROM player_read_model "
                "WHERE account_id=?", (int(account_id),)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: get_section(%s,%s) failed: %s", account_id, section, exc)
        return None
    if not row or not row["blob"] or _is_stale(row["src_synced_at"]):
        return None
    try:
        return json.loads(row["blob"])
    except Exception:  # noqa: BLE001
        return None


def get_scalars(account_id):
    """Denormalized snapshot scalars (char_name/online/lvl/intel/current_map) from
    the mirror, or None on flag-off / miss / stale."""
    if not config.PORTAL_MIRROR_READS:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT char_name, online, lvl, intel, current_map, src_synced_at "
                "FROM player_read_model WHERE account_id=?",
                (int(account_id),)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: get_scalars(%s) failed: %s", account_id, exc)
        return None
    if not row or _is_stale(row["src_synced_at"]):
        return None
    return {
        "char_name": row["char_name"],
        "online": bool(row["online"]),
        "lvl": row["lvl"],
        "intel": row["intel"],
        "current_map": row["current_map"],
    }


def _get_storage_blob(account_id):
    """Parsed storage blob for an account from the mirror, or None on flag-off /
    miss / stale / parse error (caller falls back to the live relay)."""
    if not config.PORTAL_MIRROR_READS:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT storage_json, src_synced_at FROM player_storage "
                "WHERE account_id=?", (int(account_id),)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: storage blob(%s) failed: %s", account_id, exc)
        return None
    if not row or not row["storage_json"] or _is_stale(row["src_synced_at"]):
        return None
    try:
        return json.loads(row["storage_json"])
    except Exception:  # noqa: BLE001
        return None


def invalidate_storage(account_id):
    """Drop one account's storage blob after a write that re-homed its items.

    The blob is a whole-account snapshot refreshed only by `sync_loop`, and every
    portal storage read consults it BEFORE the relay cache. So a committed write is
    invisible until the next sync (up to MIRROR_MAX_STALE): the moved stack shows in
    neither the source nor the destination. Deleting the row makes the next read fall
    through to the live relay (game-DB truth); `sync_loop` repopulates it.

    Best-effort: a failure here only costs freshness, never correctness, so it must
    never fail the write that already committed."""
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM player_storage WHERE account_id=?",
                         (int(account_id),))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: invalidate_storage(%s) failed: %s", account_id, exc)


def get_storage_containers(account_id):
    """Reproduce the /dune/player/{aid}/containers payload from the mirror, or
    None on miss/stale/flag-off."""
    blob = _get_storage_blob(account_id)
    if blob is None:
        return None
    containers = blob.get("containers") or []
    return {"available": True, "account_id": str(account_id),
            "count": len(containers), "containers": containers}


def get_storage_search(account_id):
    """Reproduce the /dune/player/{aid}/container-search payload, or None."""
    blob = _get_storage_blob(account_id)
    if blob is None:
        return None
    rows = blob.get("search_rows") or []
    return {"available": True, "account_id": str(account_id),
            "count": len(rows), "rows": rows}


def get_storage_items(account_id, container_id, page):
    """Reproduce the /dune/player/{aid}/_container/{cid}/_items?page=N payload from
    the mirror, paginating locally. error='not_owned' when the container is not in
    the account's snapshot (so the route still 404s). None on miss/stale/flag-off."""
    blob = _get_storage_blob(account_id)
    if blob is None:
        return None
    cid = str(container_id)
    owned = any(str(c.get("id")) == cid for c in (blob.get("containers") or []))
    page = max(int(page or 1), 1)
    if not owned:
        return {"available": False, "error": "not_owned",
                "account_id": str(account_id), "container_id": cid,
                "items": [], "count": 0, "total_count": 0,
                "page": page, "page_size": _STORAGE_PAGE_SIZE}
    all_items = (blob.get("items_by_container") or {}).get(cid) or []
    total = len(all_items)
    offset = (page - 1) * _STORAGE_PAGE_SIZE
    items = all_items[offset:offset + _STORAGE_PAGE_SIZE]
    return {"available": True, "account_id": str(account_id), "container_id": cid,
            "items": items, "count": len(items), "total_count": total,
            "page": page, "page_size": _STORAGE_PAGE_SIZE}


def search_market(needle):
    """Reproduce the /dune/market/listings?q= payload by searching the local
    market_listing table, or None on flag-off / miss / stale. `needle` must be a
    template_id fragment (the caller already charset-guards it)."""
    if not config.PORTAL_MIRROR_READS or not needle:
        return None
    try:
        conn = _connect()
        try:
            fresh = conn.execute(
                "SELECT src_synced_at FROM market_listing LIMIT 1").fetchone()
            if not fresh or _is_stale(fresh["src_synced_at"]):
                return None
            like = "%" + needle + "%"
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM market_listing WHERE template_id LIKE ?",
                (like,)).fetchone()["n"]
            rows = conn.execute(
                "SELECT template_id, item_price, quality_level, is_npc_order, "
                "stack, owner_id, order_id, revision FROM market_listing "
                "WHERE template_id LIKE ? "
                "ORDER BY is_npc_order, item_price LIMIT 200", (like,)).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: search_market(%s) failed: %s", needle, exc)
        return None
    listings = [{
        "template_id": r["template_id"],
        "item_price": r["item_price"],
        "quality_level": r["quality_level"],
        "is_npc_order": bool(r["is_npc_order"]),
        "stack": r["stack"],
        "owner_id": r["owner_id"],
        "order_id": r["order_id"],
        "revision": r["revision"],
    } for r in rows]
    return {"status": "ok", "query": needle, "total_matches": total,
            "shown": len(listings), "limit": 200, "listings": listings}


def _market_is_fresh(conn):
    row = conn.execute("SELECT src_synced_at FROM market_listing LIMIT 1").fetchone()
    return bool(row) and not _is_stale(row["src_synced_at"])


def market_summary():
    """Grouped-by-item aggregates over the whole local exchange, for browse +
    friendly-name search (the portal resolves names + filters/sorts). Returns a
    list of {template_id, listing_count, total_qty, min_price, max_price,
    has_npc, has_player}, or None on flag-off / miss / stale."""
    if not config.PORTAL_MIRROR_READS:
        return None
    try:
        conn = _connect()
        try:
            if not _market_is_fresh(conn):
                return None
            rows = conn.execute(
                "SELECT template_id, COUNT(*) AS listing_count, "
                "SUM(stack) AS total_qty, MIN(item_price) AS min_price, "
                "MAX(item_price) AS max_price, "
                "MAX(is_npc_order) AS has_npc, "
                "MAX(CASE WHEN is_npc_order=0 THEN 1 ELSE 0 END) AS has_player, "
                "MAX(COALESCE(quality_level, 0)) AS max_quality "
                "FROM market_listing GROUP BY template_id").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: market_summary failed: %s", exc)
        return None
    return [{
        "template_id": r["template_id"],
        "listing_count": r["listing_count"],
        "total_qty": r["total_qty"] or 0,
        "min_price": r["min_price"],
        "max_price": r["max_price"],
        "has_npc": bool(r["has_npc"]),
        "has_player": bool(r["has_player"]),
        "max_quality": r["max_quality"] or 0,
    } for r in rows]


def market_player_floors():
    """Cheapest PLAYER (non-NPC) ask per template AND GRADE over the local exchange,
    for the V2 Flip Board: the market-maker bot buys PLAYER listings, so an NPC sell
    order is not a flip source and must be excluded from the ask floor.

    🔴 Grouping is (template, grade), NOT template alone. The bot's buy price is
    per-grade, so a template-wide floor lets a grade-4 listing be compared against the
    grade-0 cap — which is exactly how the Flip Board came to advertise flips that could
    never settle (ticket #130). Grade is part of the identity of a price here.

    Returns {template_id_lower: {grade_int: min_player_price}}, or None on
    flag-off / miss / stale."""
    if not config.PORTAL_MIRROR_READS:
        return None
    try:
        conn = _connect()
        try:
            if not _market_is_fresh(conn):
                return None
            rows = conn.execute(
                "SELECT template_id, COALESCE(quality_level, 0) AS grade, "
                "       MIN(item_price) AS lo "
                "FROM market_listing "
                "WHERE is_npc_order=0 AND item_price IS NOT NULL "
                "GROUP BY template_id, COALESCE(quality_level, 0)").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: market_player_floors failed: %s", exc)
        return None
    out: dict = {}
    for r in rows:
        try:
            grade = int(r["grade"] or 0)
        except (TypeError, ValueError):
            continue
        out.setdefault(r["template_id"].lower(), {})[grade] = r["lo"]
    return out


def market_categorised_templates():
    """Templates that currently have at least one live CHOAM exchange order, lowercased.

    Used by the Karum to refuse a listing it could not later hand back. Every Karum leg
    that gives the item to somebody (buy, cancel, admin force-return) has to build an
    exchange order, and category_mask/depth on that order can only come from a real order
    for the same template -- so a template with no order anywhere cannot be delivered OR
    returned, and escrowing one strands it (2026-07-27, listing 6).

    ⚠️ This set is NARROWER than the writer's guard in two ways, deliberately:
      * it cannot see mask <> 0, because the market mirror carries no category_mask column
        (the payload comes from the telemetry mirror, not the game DB), so the ~5 bank
        templates whose only orders have mask 0 look fine here;
      * it is a 30s-refreshed snapshot of LIVE orders, so it lags reality slightly.
    Both are fine because this is the UI filter, not the gate: dune-karum-op.sh resolves
    the category inside the take transaction and refuses there. Returns None when the
    mirror is off/stale/empty, which callers MUST treat as fail-open for exactly that
    reason -- an empty set would make every item unlistable, a self-inflicted outage,
    while the writer would still refuse the genuinely undeliverable ones.
    """
    if not config.PORTAL_MIRROR_READS:
        return None
    try:
        conn = _connect()
        try:
            if not _market_is_fresh(conn):
                return None
            rows = conn.execute(
                "SELECT DISTINCT template_id FROM market_listing "
                "WHERE template_id IS NOT NULL").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: market_categorised_templates failed: %s", exc)
        return None
    out = {r["template_id"].lower() for r in rows if r["template_id"]}
    return out or None


def market_item_detail(template_id):
    """The full price ladder for one item from the local exchange: every listing
    sorted cheapest first (NPC vs player, qty, quality). Returns
    {template_id, listings:[...], count} or None on flag-off / miss / stale.
    `template_id` is matched exactly (case-insensitive)."""
    if not config.PORTAL_MIRROR_READS or not template_id:
        return None
    try:
        conn = _connect()
        try:
            if not _market_is_fresh(conn):
                return None
            rows = conn.execute(
                "SELECT item_price, quality_level, is_npc_order, stack, owner_id, "
                "order_id, revision "
                "FROM market_listing WHERE template_id = ? COLLATE NOCASE "
                "ORDER BY item_price, is_npc_order", (template_id,)).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("mirror: market_item_detail(%s) failed: %s", template_id, exc)
        return None
    listings = [{
        "item_price": r["item_price"],
        "quality_level": r["quality_level"],
        "is_npc_order": bool(r["is_npc_order"]),
        "stack": r["stack"],
        "owner_id": r["owner_id"],
        "order_id": r["order_id"],
        "revision": r["revision"],
    } for r in rows]
    return {"template_id": template_id, "listings": listings, "count": len(listings)}


async def sync_loop(app):
    """Background loop: pull /dune/read-models via the relay and upsert into the
    local mirror every config.MIRROR_PULL_INTERVAL seconds. Best-effort; errors are
    logged and retried next tick. Runs even when PORTAL_MIRROR_READS is off so the
    mirror is warm before reads are flipped on."""
    init()
    log.info("mirror: sync loop started (db=%s interval=%ss reads=%s)",
             config.MIRROR_DB_PATH, config.MIRROR_PULL_INTERVAL,
             config.PORTAL_MIRROR_READS)
    # Bot prices change at most once per bot list tick (30 min); pulling them
    # every loop would just add SSH churn on the game box. Every ~5 minutes is
    # plenty (the producer rewrites the file every 300s buy tick).
    bot_prices_every = max(1, 300 // max(config.MIRROR_PULL_INTERVAL, 1))
    loop_n = 0
    while True:
        # Each section is pulled independently so one slow/failed endpoint never
        # blocks the others. The collector refreshes each at its own cadence on
        # lastsietch-dune; pulling every MIRROR_PULL_INTERVAL just re-reads local data.
        try:
            n = apply_snapshot(await relay.call_relay("/dune/read-models", timeout=30))
            log.debug("mirror: pulled %d read-models", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("mirror: read-models sync failed: %s", exc)
        if loop_n % bot_prices_every == 0:
            try:
                n = apply_bot_prices_snapshot(
                    await relay.call_relay("/dune/market/bot-prices", timeout=30))
                log.debug("mirror: pulled %d bot prices", n)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("mirror: bot-prices sync failed: %s", exc)
            try:
                n = apply_bot_limits_snapshot(
                    await relay.call_relay("/dune/market/bot-limits", timeout=30))
                log.debug("mirror: pulled %d bot-limit scopes", n)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("mirror: bot-limits sync failed: %s", exc)
        loop_n += 1
        try:
            n = apply_storage_snapshot(await relay.call_relay("/dune/storage-models", timeout=45))
            log.debug("mirror: pulled %d storage snapshots", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("mirror: storage sync failed: %s", exc)
        try:
            n = apply_market_snapshot(await relay.call_relay("/dune/market-listings-all", timeout=45))
            log.debug("mirror: pulled %d market listings", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("mirror: market sync failed: %s", exc)
        await asyncio.sleep(config.MIRROR_PULL_INTERVAL)
