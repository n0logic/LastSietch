"""Market price-alert watchlist: background watcher + CRUD helpers.

A portal player sets "notify me when <item> is listed at/below <max_price>".
This module's background loop diffs the local market mirror (mirror.sqlite,
already refreshed every ~30s by mirror.sync_loop) and fires one alert when the
cheapest current listing for a watched item crosses the threshold. It is fully
READ-ONLY on the exchange: no buy, no sell, no game-DB writes. All state lives
in admin.db (portal_market_watch + portal_market_alert).

Fire/re-arm: a watch is `armed` while waiting. When the cheapest listing for its
template drops to <= max_price it fires once and disarms; it re-arms only after
the cheapest price rises back above the threshold (or all listings disappear).
That turns a continuously-cheap item into a single alert, not one per tick.

All listings count toward the cheapest price (NPC market-bot and player sellers
alike) — chosen 2026-06-05.

Delivery is two-channel: the portal bell (seen_in_portal) and a Cielago Discord
DM (dm_sent, pulled via routers/portal_alerts.py /_internal/market-alerts).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import config
import mirror
from database import get_db

log = logging.getLogger("lastsietch-admin.market_watch")

# Cap how many distinct templates go into one IN(...) clause (SQLite param limit
# is ~999; stay well under and chunk).
_IN_CHUNK = 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------- price lookup ---

def _mirror_market_fresh(conn: sqlite3.Connection) -> bool:
    """True when the local market mirror has rows and they are not stale."""
    row = conn.execute("SELECT src_synced_at FROM market_listing LIMIT 1").fetchone()
    return bool(row) and not mirror._is_stale(row["src_synced_at"])


def cheapest_prices(template_ids: list[str]) -> dict | None:
    """Cheapest current listing price per template over the local market mirror,
    counting every listing (NPC + player). Returns {template_id: min_price} for
    templates that have at least one listing, or None when the mirror market data
    is missing/stale (caller then skips the tick rather than firing on old data).
    Matched case-insensitively to align with market_item_detail()."""
    tpls = [t for t in {(t or "").strip() for t in template_ids} if t]
    if not tpls:
        return {}
    try:
        conn = mirror._connect()
    except Exception as exc:  # noqa: BLE001
        log.warning("market_watch: mirror connect failed: %s", exc)
        return None
    try:
        if not _mirror_market_fresh(conn):
            return None
        out: dict = {}
        for i in range(0, len(tpls), _IN_CHUNK):
            chunk = tpls[i:i + _IN_CHUNK]
            qmarks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT template_id, MIN(item_price) AS lo FROM market_listing "
                f"WHERE template_id IN ({qmarks}) COLLATE NOCASE "
                f"AND item_price IS NOT NULL GROUP BY template_id COLLATE NOCASE",
                chunk,
            ).fetchall()
            for r in rows:
                out[str(r["template_id"])] = r["lo"]
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("market_watch: cheapest_prices failed: %s", exc)
        return None
    finally:
        conn.close()


def _cheapest_for_one(template_id: str) -> int | None:
    """Convenience single-template lookup for the route layer (watchlist page +
    add-form 'current cheapest' hint). None on no-listing or stale mirror."""
    prices = cheapest_prices([template_id])
    if not prices:
        return None
    # cheapest_prices matches case-insensitively; resolve back leniently.
    for k, v in prices.items():
        if k.lower() == (template_id or "").strip().lower():
            return v
    return None


# ----------------------------------------------------------- watcher tick ---

def run_once() -> dict:
    """Evaluate every watch against the current mirror prices: fire newly-crossed
    armed watches, re-arm watches whose price rose back above threshold. Returns
    {checked, fired, rearmed, skipped} (skipped=True when the mirror was stale)."""
    conn = get_db()
    try:
        watches = conn.execute(
            "SELECT id, discord_id, account_id, template_id, name_cached, "
            "max_price, armed FROM portal_market_watch"
        ).fetchall()
        if not watches:
            return {"checked": 0, "fired": 0, "rearmed": 0, "skipped": False}

        prices = cheapest_prices([w["template_id"] for w in watches])
        if prices is None:
            log.debug("market_watch: mirror market stale/unavailable, skipping tick")
            return {"checked": len(watches), "fired": 0, "rearmed": 0, "skipped": True}

        # Case-insensitive price map so a watch's stored template casing still hits.
        pmap = {k.lower(): v for k, v in prices.items()}
        now = _now_iso()
        fired = rearmed = 0

        for w in watches:
            cheapest = pmap.get((w["template_id"] or "").lower())
            conn.execute(
                "UPDATE portal_market_watch SET last_checked_at=? WHERE id=?",
                (now, w["id"]))
            if w["armed"]:
                if cheapest is not None and cheapest <= w["max_price"]:
                    conn.execute(
                        "INSERT INTO portal_market_alert(watch_id, discord_id, "
                        "account_id, template_id, name_cached, threshold_price, "
                        "match_price, created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (w["id"], w["discord_id"], w["account_id"], w["template_id"],
                         w["name_cached"], w["max_price"], cheapest, now))
                    conn.execute(
                        "UPDATE portal_market_watch SET armed=0, last_match_price=?, "
                        "last_triggered_at=? WHERE id=?", (cheapest, now, w["id"]))
                    fired += 1
            else:
                # Disarmed: re-arm once the deal is gone (price rose or no listing).
                if cheapest is None or cheapest > w["max_price"]:
                    conn.execute(
                        "UPDATE portal_market_watch SET armed=1 WHERE id=?", (w["id"],))
                    rearmed += 1

        conn.commit()
        if fired or rearmed:
            log.info("market_watch: tick checked=%d fired=%d rearmed=%d",
                     len(watches), fired, rearmed)
        return {"checked": len(watches), "fired": fired, "rearmed": rearmed,
                "skipped": False}
    finally:
        conn.close()


async def sync_loop(app):
    """Background loop: evaluate watches every MARKET_WATCH_INTERVAL seconds.
    Gated by MARKET_WATCH_ENABLED so it can be deployed dark and flipped on after
    the mirror + UI are verified (same rollout shape as the mirror read path)."""
    if not config.MARKET_WATCH_ENABLED:
        log.info("market_watch: disabled (LASTSIETCH_MARKET_WATCH_ENABLED=0); loop not started")
        return
    log.info("market_watch: loop started (interval=%ss)", config.MARKET_WATCH_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("market_watch: tick failed: %s", exc)
        await asyncio.sleep(config.MARKET_WATCH_INTERVAL)


# --------------------------------------------------------- CRUD for routes ---

def list_watches(account_id: int) -> list[dict]:
    """The account's watches, newest first, each decorated with the current
    cheapest mirror price + a derived status (triggered / waiting / no-listings)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, template_id, name_cached, max_price, armed, "
            "last_match_price, last_triggered_at, created_at "
            "FROM portal_market_watch WHERE account_id=? ORDER BY created_at DESC",
            (int(account_id),)).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    prices = cheapest_prices([r["template_id"] for r in rows]) or {}
    pmap = {k.lower(): v for k, v in prices.items()}
    out = []
    for r in rows:
        cheapest = pmap.get((r["template_id"] or "").lower())
        if cheapest is None:
            status = "none"            # no current listings
        elif cheapest <= r["max_price"]:
            status = "met"             # at/below the player's target right now
        else:
            status = "waiting"
        out.append({
            "id": r["id"],
            "template_id": r["template_id"],
            "name": r["name_cached"] or r["template_id"],
            "max_price": r["max_price"],
            "max_price_display": f"{r['max_price']:,}",
            "cheapest": cheapest,
            "cheapest_display": (f"{cheapest:,}" if cheapest is not None else "—"),
            "status": status,
            "armed": bool(r["armed"]),
            "last_triggered_at": r["last_triggered_at"],
        })
    return out


def count_watches(account_id: int) -> int:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM portal_market_watch WHERE account_id=?",
            (int(account_id),)).fetchone()["n"]
    finally:
        conn.close()


def add_watch(account_id: int, discord_id: str, template_id: str,
              name_cached: str, max_price: int) -> dict:
    """Create or update (UPSERT) a watch. Returns {ok, error?, at_cap?}. A repeat
    template for the same account updates the threshold + re-arms it."""
    if count_watches(account_id) >= config.MARKET_WATCH_MAX_PER_ACCOUNT:
        # Allow updating an existing template even at cap; only block brand-new ones.
        conn = get_db()
        try:
            exists = conn.execute(
                "SELECT 1 FROM portal_market_watch WHERE account_id=? AND template_id=?",
                (int(account_id), template_id)).fetchone()
        finally:
            conn.close()
        if not exists:
            return {"ok": False, "at_cap": True,
                    "error": f"You can watch up to {config.MARKET_WATCH_MAX_PER_ACCOUNT} items."}
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO portal_market_watch(discord_id, account_id, template_id, "
            "name_cached, max_price, armed) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(account_id, template_id) DO UPDATE SET "
            "max_price=excluded.max_price, name_cached=excluded.name_cached, armed=1, "
            "last_match_price=NULL, last_triggered_at=NULL",
            (discord_id, int(account_id), template_id, name_cached, int(max_price)))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def remove_watch(account_id: int, watch_id: int) -> bool:
    """Delete one of the account's watches. Ownership enforced via account_id."""
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM portal_market_watch WHERE id=? AND account_id=?",
            (int(watch_id), int(account_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def unseen_alert_count(account_id: int) -> int:
    """Count of fired alerts the player has not yet seen in the portal (bell)."""
    try:
        conn = get_db()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM portal_market_alert "
                "WHERE account_id=? AND seen_in_portal=0",
                (int(account_id),)).fetchone()["n"]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("market_watch: unseen_alert_count(%s) failed: %s", account_id, exc)
        return 0


def list_alerts(account_id: int, limit: int = 50) -> list[dict]:
    """Recent fired alerts for the account, newest first (for the watchlist page)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, template_id, name_cached, threshold_price, match_price, "
            "created_at, seen_in_portal FROM portal_market_alert "
            "WHERE account_id=? ORDER BY created_at DESC LIMIT ?",
            (int(account_id), int(limit))).fetchall()
    finally:
        conn.close()
    return [{
        "id": r["id"],
        "template_id": r["template_id"],
        "name": r["name_cached"] or r["template_id"],
        "threshold_price": r["threshold_price"],
        "threshold_display": f"{r['threshold_price']:,}",
        "match_price": r["match_price"],
        "match_display": f"{r['match_price']:,}",
        "created_at": r["created_at"],
        "seen": bool(r["seen_in_portal"]),
    } for r in rows]


# A portal feed renders at most a page of alerts, so a request naming more ids
# than this is not a real clear; the cap keeps the IN list bounded.
MARK_SEEN_MAX_IDS = 200


def mark_alerts_seen(account_id: int, ids) -> int:
    """Mark the NAMED alerts seen for this account. Returns rows updated.

    ids is required and never implicit: the portal feed shows a limited, newest-
    first slice, and an unbounded clear marks alerts seen that were never on the
    page the player looked at -- the row they never saw is exactly the one the
    badge exists for. An empty (or all-junk) id list is a no-op, not a clear-all.
    The account_id predicate stays regardless, so a foreign id can never be
    touched."""
    wanted = []
    for raw in (ids or [])[:MARK_SEEN_MAX_IDS * 2]:   # junk-heavy lists cannot spin past the cap
        if isinstance(raw, bool):
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            wanted.append(val)
        if len(wanted) >= MARK_SEEN_MAX_IDS:
            break
    if not wanted:
        return 0

    conn = get_db()
    try:
        placeholders = ",".join("?" * len(wanted))
        cur = conn.execute(
            "UPDATE portal_market_alert SET seen_in_portal=1 "
            f"WHERE account_id=? AND seen_in_portal=0 AND id IN ({placeholders})",
            (int(account_id), *wanted))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ----------------------------------------- Cielago DM delivery (internal) ---

def pending_dms(limit: int = 50) -> list[dict]:
    """Undelivered DM alerts for Cielago to push, oldest first."""
    conn = get_db()
    try:
        import portal_identity
        if portal_identity.credentials_installed(conn):
            rows = conn.execute(
                "SELECT a.id, v.subject AS discord_id, a.template_id, a.name_cached, "
                "a.threshold_price, a.match_price, a.created_at FROM portal_market_alert a "
                "JOIN portal_profiles p ON COALESCE(p.legacy_discord_id,'profile:'||p.id)=a.discord_id "
                "JOIN portal_profile_providers v ON v.profile_id=p.id AND v.provider='discord' "
                "WHERE a.dm_sent=0 AND p.disabled_at IS NULL AND v.revoked_at IS NULL "
                "ORDER BY a.id ASC LIMIT ?", (int(limit),)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, discord_id, template_id, name_cached, threshold_price, "
                "match_price, created_at FROM portal_market_alert "
                "WHERE dm_sent=0 ORDER BY id ASC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    return [{
        "id": r["id"],
        "discord_id": r["discord_id"],
        "template_id": r["template_id"],
        "name": r["name_cached"] or r["template_id"],
        "threshold_price": r["threshold_price"],
        "match_price": r["match_price"],
        "created_at": r["created_at"],
    } for r in rows]


def ack_dms(ids: list[int], note: str | None = None) -> int:
    """Mark the given alert ids as DM-delivered (or permanently given up). Returns
    rows updated. note carries an optional delivery outcome (e.g. 'dms_closed')."""
    clean = [int(i) for i in (ids or []) if str(i).strip().lstrip("-").isdigit()]
    if not clean:
        return 0
    conn = get_db()
    try:
        total = 0
        for i in range(0, len(clean), _IN_CHUNK):
            chunk = clean[i:i + _IN_CHUNK]
            qmarks = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"UPDATE portal_market_alert SET dm_sent=1, dm_note=? "
                f"WHERE id IN ({qmarks}) AND dm_sent=0", [note, *chunk])
            total += cur.rowcount
        conn.commit()
        return total
    finally:
        conn.close()
