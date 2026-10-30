#!/usr/bin/env python3
"""Seven Nights of the Sietch earning worker (lastsietch-sietch-nights.timer, every 5 min).

One tick:
  1. gate: BOTH /etc/lastsietch/sietch-nights-live AND LASTSIETCH_SIETCH_NIGHTS_ENABLED must be on,
     or the tick logs which one is closed and exits 0 having touched nothing;
  2. E4 Landsraad: fold every fresh mirror row's contribution total into tonight
     (local mirror.sqlite, no relay hop); record levels for eligibility;
  3. E1 visit: pull login days through the relay for a bounded set of accounts
     (online now and not yet credited tonight, plus a small catch-up of linked
     accounts not pulled for a while). One pull carries 60 days, so it credits
     every event night at once;
  4. credit every linked profile (MAX across accounts, caps per night).

Reads the game only through existing Tier A verbs and writes only admin.db.

  --dry-run   run the same tick against a private COPY of admin.db (sqlite
              backup API), print what it would credit, discard the copy. Ignores
              the gate so it can be reviewed before launch.
  --now ISO   pretend the clock is ISO (dry runs only).
  --deliver   the Token Stall sweep instead (lastsietch-sietch-deliver.timer): re-send every
              queued or reconciling bank/wallet/spec/charxp order with its OWN delivery_key and
              fold the writer's answer back. Gate: LASTSIETCH_SIETCH_NIGHTS_ENABLED and
              LASTSIETCH_SIETCH_SHOP_ENABLED. The game-side gate file still decides whether
              anything is applied; a closed one answers deferred and the order waits.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import sietch_nights as sn  # noqa: E402

log = logging.getLogger("sietch-nights-tick")

FLAG_FILE = os.environ.get("LASTSIETCH_SIETCH_NIGHTS_FLAG_FILE", "/etc/lastsietch/sietch-nights-live")
ENV_FLAG = "LASTSIETCH_SIETCH_NIGHTS_ENABLED"
CATCHUP_PER_TICK = 10
CATCHUP_AFTER = timedelta(hours=3)
ONLINE_PULLS_PER_TICK = 40
PULL_FAILS_TO_STOP = 3
PULL_BUDGET_S = 150   # of TimeoutStartSec=240: the credit step must still run


def gate_state():
    import feature_flags
    return os.path.exists(FLAG_FILE), bool(feature_flags.enabled(ENV_FLAG, "0"))


def mirror_rows():
    """[(account_id, online, lvl, landsraad_readings, landsraad_turnins)] for every FRESH mirror row."""
    conn = sqlite3.connect("file:%s?mode=ro" % config.MIRROR_DB_PATH, uri=True, timeout=5)
    try:
        rows = conn.execute("SELECT account_id, online, lvl, landsraad_json, src_synced_at "
                            "FROM player_read_model").fetchall()
    finally:
        conn.close()
    now = datetime.now(timezone.utc)
    out, stale = [], 0
    for aid, online, lvl, blob, synced in rows:
        try:
            t = datetime.fromisoformat(synced)
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            stale += 1
            continue
        if (now - t).total_seconds() > config.MIRROR_MAX_STALE:
            stale += 1
            continue
        try:
            payload = json.loads(blob) if blob else None
            total = sn.landsraad_readings(payload)
            turnins = sn.landsraad_turnins(payload)
        except ValueError:
            total = turnins = None
        out.append((int(aid), bool(online), lvl, total, turnins))
    return out, stale


def _pull(client, account_id):
    # Sync on purpose: relay.call_relay's shared AsyncClient is bound to one event
    # loop, and a worker that asyncio.run()s per account would trip over it.
    resp = client.get("%s/dune/rewards/login-days" % config.RELAY_URL,
                      params={"account_id": account_id},
                      headers={"X-API-Key": config.RELAY_API_KEY})
    resp.raise_for_status()
    payload = resp.json()
    days = payload.get("login_days") if isinstance(payload, dict) else None
    if not isinstance(days, list):
        raise RuntimeError("login-days: unexpected payload")
    return [r.get("date_utc") for r in days if isinstance(r, dict)]


def pick_pulls(conn, mirror, night_key, now):
    online = [aid for aid, on, *_ in mirror if on]
    credited = {r[0] for r in conn.execute(
        "SELECT account_id FROM portal_sn_earn WHERE night = ? AND source = 'visit'", (night_key,))}
    picks = [a for a in online if a not in credited][:ONLINE_PULLS_PER_TICK]
    cutoff = (now - CATCHUP_AFTER).strftime("%Y-%m-%d %H:%M:%S")
    linked = sorted({a for accts in sn.linked_profiles(conn).values() for a in accts})
    pulled = dict(conn.execute("SELECT account_id, login_pulled_at FROM portal_sn_accounts"))
    due = sorted((a for a in linked if a not in picks and a not in credited
                  and (pulled.get(a) is None or pulled[a] < cutoff)),
                 key=lambda a: pulled.get(a) or "")
    return picks + due[:CATCHUP_PER_TICK]


def tick(conn, now, mirror, pull):
    night = sn.current_night(now)
    stats = {"night": night["key"] if night else None, "mirror_rows": len(mirror),
             "landsraad_refs": 0, "pulls": 0, "pull_errors": 0, "visit_rows": 0}
    if night:
        for aid, _on, lvl, total, turnins in mirror:
            sn.record_level(conn, aid, lvl)
            if total is not None:
                stats["landsraad_refs"] += len(sn.landsraad_observe(conn, aid, night["key"], total, now,
                                                                    turnins))
        conn.commit()
    key = night["key"] if night else sn.NIGHT_KEYS[-1]
    streak = 0
    started = time.monotonic()
    for aid in pick_pulls(conn, mirror, key, now):
        if time.monotonic() - started > PULL_BUDGET_S:
            log.warning("login-days: pull budget of %d s spent, crediting what we have", PULL_BUDGET_S)
            break
        try:
            dates = pull(aid)
        except Exception as exc:  # noqa: BLE001
            stats["pull_errors"] += 1
            streak += 1
            log.warning("login-days pull failed acct=%s: %s", aid, exc)
            if streak >= PULL_FAILS_TO_STOP:
                # A hung relay would otherwise cost 30 s per account and outrun
                # the unit timeout; the next tick tries again.
                log.warning("login-days: %d failures in a row, stopping pulls this tick", streak)
                break
            continue
        streak = 0
        stats["pulls"] += 1
        stats["visit_rows"] += sn.record_login_days(conn, aid, dates, now)
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    credited = sn.credit_all(conn)
    conn.commit()
    stats["profiles_credited"] = len(credited)
    stats["tokens_credited"] = sum(credited.values())
    return stats, credited


DELIVER_PER_TICK = 10
DELIVER_FAILS_TO_STOP = 3


def _send(client, order):
    resp = client.post("%s/dune/reward-op" % config.RELAY_URL, json=sn.writer_payload(order),
                       headers={"X-API-Key": config.RELAY_API_KEY})
    if resp.status_code >= 500:
        return None
    resp.raise_for_status()
    return resp.json()


def deliver_sweep(conn, send, now=None):
    stats = {"sent": 0, "delivered": 0, "refunded": 0, "queued": 0, "reconciling": 0, "errors": 0}
    streak = 0
    for due in sn.due_deliveries(conn, DELIVER_PER_TICK, now):
        order = sn.begin_delivery(conn, due["id"], now)
        if order is None:
            continue   # the inline deliver or an operator got it first
        try:
            outcome = send(order)
        except Exception as exc:  # noqa: BLE001
            # A timeout or a refused connection says nothing about the grant: hold.
            log.warning("deliver: order %s no answer: %s", order["id"], exc)
            outcome = None
            stats["errors"] += 1
        streak = streak + 1 if outcome is None else 0
        status = sn.apply_outcome(conn, order["id"], outcome, now)
        stats["sent"] += 1
        stats[status] = stats.get(status, 0) + 1
        if streak >= DELIVER_FAILS_TO_STOP:
            log.warning("deliver: %d orders in a row without an answer, stopping this tick", streak)
            break
    for row in sn.stale_reconciling(conn, now=now):
        log.warning("ALERT deliver: order %s (%s) reconciling since %s after %s attempts: %s",
                    row["id"], row["sku"], row["created_at"], row["attempts"], row["error"])
    return stats


def deliver_main():
    import feature_flags
    earn = bool(feature_flags.enabled(ENV_FLAG, "0"))
    shop = bool(feature_flags.enabled("LASTSIETCH_SIETCH_SHOP_ENABLED", "0"))
    log.info("deliver gate: earn=%d shop=%d", int(earn), int(shop))
    if not (earn and shop):
        log.info("deliver gate closed; nothing done")
        return 0
    import httpx
    from database import get_db
    client = httpx.Client(timeout=httpx.Timeout(50.0, connect=5.0))
    conn = get_db()
    try:
        stats = deliver_sweep(conn, lambda order: _send(client, order))
    finally:
        conn.close()
        client.close()
    log.info("deliver: %s", json.dumps(stats, sort_keys=True))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now")
    ap.add_argument("--deliver", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.deliver:
        if args.dry_run or args.now:
            ap.error("--deliver takes no other flag")
        return deliver_main()

    now = datetime.now(timezone.utc)
    if args.now:
        if not args.dry_run:
            ap.error("--now is for dry runs only")
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))

    flag_file, env_on = gate_state()
    log.info("gate: flag_file=%s env=%s", "present" if flag_file else "absent", int(env_on))
    if not args.dry_run and not (flag_file and env_on):
        log.info("gate closed; nothing done")
        return 0
    if not sn.credit_open(now):
        log.info("outside the event window (%s); nothing done", now.isoformat())
        return 0

    mirror, stale = mirror_rows()
    log.info("mirror: %d fresh rows, %d stale skipped", len(mirror), stale)

    import httpx
    client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))

    def pull(aid):
        return _pull(client, aid)

    if args.dry_run:
        live = sqlite3.connect("file:%s?mode=ro" % config.DB_PATH, uri=True, timeout=5)
        tmp = tempfile.NamedTemporaryFile(prefix="sn-dry-", suffix=".db", delete=False)
        tmp.close()
        try:
            conn = sqlite3.connect(tmp.name)
            live.backup(conn)
            live.close()
            # The live admin.db may predate the portal_sn_* tables until init_db runs.
            import database
            conn.executescript(database.SCHEMA)
            conn.row_factory = sqlite3.Row
            stats, credited = tick(conn, now, mirror, pull)
            conn.close()
        finally:
            os.unlink(tmp.name)
        log.info("DRY RUN (copy discarded): %s", json.dumps(stats, sort_keys=True))
        for pid, tokens in sorted(credited.items(), key=lambda kv: -kv[1]):
            log.info("  would credit %s.. +%d", pid[:8], tokens)
        return 0

    from database import get_db
    conn = get_db()
    try:
        stats, _credited = tick(conn, now, mirror, pull)
    finally:
        conn.close()
    log.info("tick: %s", json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
