#!/usr/bin/env python3
"""Dune server-stats collector, emits a structured JSON stats blob.

This is the DATA half of the daily/weekly server digest. It runs on lastsietch-dune
(where the data physically lives) and emits pure structured data on stdout.
All presentation, branding, and Discord posting live in the Cielago bot
(Last Sietch), which fetches this blob via the lastsietch-relay /dune/stats/digest
endpoint and renders the embed.

History: this replaces the data layer of /opt/lastsietch-stats/digest.py, which both
collected stats AND posted a House-0f-Fedaykin-branded embed to the Last Sietch Discord.
The posting half is retired; Cielago now owns scheduling, branding, and posting.

Deploy home: <game box>:/opt/lastsietch-stats/dune-stats-collect.py, the lastsietch-stats
subsystem dir, alongside sampler.py and the retired digest.py. This is the copy
the relay forced-command dispatcher actually executes (dune-relay-dispatch.sh
`stats-collect` action -> /dune/stats/digest). DO NOT deploy to /root: nothing
runs a /root copy, and a stale /root copy was the dual-copy trap that hid the
section_economy add during the 2026-06-10 cutover. Always deploy + verify
through the executed /opt/lastsietch-stats path, never a bare file run.

Usage:
  dune-stats-collect.py --period daily
  dune-stats-collect.py --period weekly

Data sources (all read-only):
  - /var/lib/lastsietch-telemetry/telemetry.db  (presence, combat, flight, connections)
  - postgres dune.*                       (accounts, building, spice, partitions)
  - postgres lsadmin.*                   (bans, player_actions, moderation)
  - /var/lib/lastsietch-pod-watcher/state.json   (pod restart state)
  - /var/lib/lastsietch-stats/improvements.jsonl (server-improvement log)
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DB_POD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"
DB_PATH = "/var/lib/lastsietch-telemetry/telemetry.db"
POD_STATE = "/var/lib/lastsietch-pod-watcher/state.json"
IMPROVEMENTS_PATH = "/var/lib/lastsietch-stats/improvements.jsonl"
STATUS_SCRIPT = "/root/dune-status.py"
SAMPLE_MINUTES = 5  # telemetry presence sweep interval; one row ~= 5 min online

# Hagga = Survival_1, Deep Desert = DeepDesert_1 (both maps host two partitions).
HAGGA_MAP = "Survival_1"
DEEP_DESERT_MAP = "DeepDesert_1"


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} [stats-collect] {msg}",
          file=sys.stderr, flush=True)


def kubectl(args, input_data=None):
    return subprocess.run(
        ["sudo", "kubectl", *args],
        capture_output=True, text=True, input=input_data, timeout=90,
    )


def psql(sql):
    r = kubectl(
        ["exec", "-i", "-n", NS, DB_POD, "--", "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_", "-h", "localhost", "-p", "15432", "-U", "postgres", "-d", "dune",
         "-t", "-A", "-F|", "-v", "ON_ERROR_STOP=1"],
        input_data=sql,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return [ln.split("|") for ln in r.stdout.splitlines() if ln.strip()]


def display_name(funcom_id):
    return (funcom_id or "").split("#")[0].strip() or "(unknown)"


def hours(sample_count):
    return round(sample_count * SAMPLE_MINUTES / 60.0, 1)


# ---- shared helpers -------------------------------------------------------

def resolve_names(account_ids):
    """Map telemetry account_id values -> display names.

    Priority: (1) latest non-null roster_snapshot.character_name; (2) postgres
    dune.accounts.funcom_id; (3) f"acct {id}" fallback. Keyed by str().
    """
    ids = sorted({str(a) for a in account_ids if str(a).strip()})
    names = {}
    if not ids:
        return names

    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT account_id, character_name FROM roster_snapshot "
                "WHERE character_name IS NOT NULL ORDER BY ts"
            ).fetchall()
        finally:
            con.close()
        for aid, cname in rows:
            if str(aid) in set(ids) and cname:
                names[str(aid)] = cname  # later row wins (ordered by ts)
    except Exception as e:
        log(f"resolve_names roster lookup failed: {e}")

    missing = [i for i in ids if i not in names and i.isdigit()]
    if missing:
        try:
            rows = psql(f"SELECT id, funcom_id FROM dune.accounts WHERE id IN ({','.join(missing)});")
            for r in rows:
                names[str(r[0])] = display_name(r[1])
        except Exception as e:
            log(f"resolve_names psql lookup failed: {e}")

    for i in ids:
        names.setdefault(i, f"acct {i}")
    return names


_ACT_RE = re.compile(r"!!act#(\d+)")


def _act_int(val):
    m = _ACT_RE.search(val or "")
    return int(m.group(1)) if m else None


def _resolve_player_controllers(controller_ids):
    """player_controller_id -> character_name via dune.player_state."""
    ids = sorted({str(c) for c in controller_ids if c is not None})
    if not ids:
        return {}
    rows = psql(
        f"SELECT player_controller_id, character_name FROM dune.player_state "
        f"WHERE player_controller_id IN ({','.join(ids)});"
    )
    return {str(r[0]): (r[1] or "").strip() for r in rows}


# ---- sections (return structured data, NOT formatted strings) -------------

def section_new_players(interval_sql):
    rows = psql(
        f"SELECT a.funcom_id FROM dune.ls_welcome_pack_grants g "
        f"JOIN dune.accounts a ON a.id = g.account_id "
        f"WHERE g.granted_at >= now() - interval '{interval_sql}' "
        f"AND g.notes NOT LIKE 'operator-skip%' "
        f"ORDER BY g.granted_at;"
    )
    names = [display_name(r[0]) for r in rows]
    total = None
    try:
        total = int(psql("SELECT count(*) FROM dune.accounts;")[0][0])
    except Exception:
        pass
    return {"names": names, "count": len(names), "total_all_time": total}


def section_server_pulse(con, window_start, period):
    rows = con.execute(
        "SELECT ts, COUNT(*) AS concurrent FROM presence WHERE ts >= ? GROUP BY ts",
        (window_start,),
    ).fetchall()
    if not rows:
        return None
    peak = max(c for _, c in rows)
    play_hours = round(sum(c for _, c in rows) * SAMPLE_MINUTES / 60.0, 1)

    by_hour = {}
    by_date = set()
    for ts, c in rows:
        tm = time.gmtime(ts)
        by_hour[tm.tm_hour] = by_hour.get(tm.tm_hour, 0) + c
        by_date.add((tm.tm_year, tm.tm_yday))
    busiest_hour = max(by_hour, key=by_hour.get)

    return {
        "peak": peak,
        "play_hours": play_hours,
        "busiest_hour_utc": busiest_hour,
        "active_days": min(len(by_date), 7) if period == "weekly" else None,
    }


def section_most_active(con, window_start):
    rows = con.execute(
        "SELECT account_id, COUNT(*) FROM presence WHERE ts >= ? "
        "GROUP BY account_id ORDER BY 2 DESC LIMIT 5",
        (window_start,),
    ).fetchall()
    if not rows:
        return []
    names = resolve_names([aid for aid, _ in rows])
    return [{"name": names.get(str(aid), f"acct {aid}"), "hours": hours(cnt)}
            for aid, cnt in rows]


def section_pilot(con, iso_week):
    rows = con.execute(
        "SELECT account_id, vehicle_class, meters FROM flight_distance_weekly "
        "WHERE iso_week = ? AND meters > 0 ORDER BY meters DESC LIMIT 5",
        (iso_week,),
    ).fetchall()
    if not rows:
        return []
    names = resolve_names([aid for aid, _, _ in rows])
    return [{"name": names.get(str(aid), f"acct {aid}"),
             "km": round(meters / 1000.0, 1),
             "vehicle_raw": vc or ""}
            for aid, vc, meters in rows]


def section_deaths(con, window_start):
    rows = con.execute(
        "SELECT victim_account_id, victim_name, COUNT(*) AS deaths FROM combat_events "
        "WHERE event_type = 0 AND victim_account_id IS NOT NULL AND occurred_epoch >= ? "
        "GROUP BY victim_account_id ORDER BY deaths DESC LIMIT 3",
        (window_start,),
    ).fetchall()
    if not rows:
        return []
    need = [str(aid) for aid, vname, _ in rows if not vname]
    names = resolve_names(need) if need else {}
    return [{"name": (vname or names.get(str(aid), f"acct {aid}")), "count": deaths}
            for aid, vname, deaths in rows]


def section_origins(con, window_start):
    rows = con.execute(
        "SELECT country, COUNT(DISTINCT ip) FROM connections "
        "WHERE conn_epoch >= ? AND country IS NOT NULL "
        "GROUP BY country ORDER BY 2 DESC",
        (window_start,),
    ).fetchall()
    return [{"country": country, "count": cnt} for country, cnt in rows]


def section_raids(con, window_start):
    rows = con.execute(
        "SELECT occurred_epoch, actor_id, raw FROM combat_events "
        "WHERE event_type IN (10,23) AND occurred_epoch >= ? ORDER BY occurred_epoch",
        (window_start,),
    ).fetchall()

    raid_structures = []   # (epoch, actor_id, causer_int, thing, shielded)
    raid_vehicles = {}     # m_VehicleId -> (epoch, actor_id, causer_int, model, shielded)
    self_demos = 0
    storm_orni = 0
    storm_buggy = 0
    storm_seen = set()

    for epoch, actor_id, raw in rows:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        causer_type = d.get("m_CauserType")
        causer_int = _act_int(d.get("m_CauserId"))
        vid = d.get("m_VehicleId")
        model = d.get("m_VehicleModelName")
        shielded = bool(d.get("m_bWasShielded"))

        if causer_type == "Player" and causer_int is not None and causer_int != actor_id:
            cls = "RAID"
        elif causer_type in ("Sandstorm", "Environment"):
            cls = "STORM_LOSS"
        else:
            cls = "OWNER_CLEANUP"

        if cls == "RAID":
            if vid:
                if vid not in raid_vehicles:
                    raid_vehicles[vid] = (epoch, actor_id, causer_int, model, shielded)
            else:
                thing = d.get("m_BuildableName") or "structure"
                raid_structures.append((epoch, actor_id, causer_int, thing, shielded))
        elif cls == "STORM_LOSS":
            if vid and vid not in storm_seen:
                storm_seen.add(vid)
                if "Orni" in (model or ""):
                    storm_orni += 1
                else:
                    storm_buggy += 1
        else:
            self_demos += 1

    raids = list(raid_structures)
    for epoch, actor_id, causer_int, model, shielded in raid_vehicles.values():
        # vehicle losses carry the raw model in the 'thing' slot; Cielago prettifies.
        raids.append((epoch, actor_id, causer_int, model or "vehicle", shielded))
    raids.sort(key=lambda r: r[0])

    out = {"events": [], "self_demos": self_demos,
           "storm_orni": storm_orni, "storm_buggy": storm_buggy}
    if not raids:
        return out

    controllers = set()
    for _, actor_id, causer_int, _, _ in raids:
        controllers.add(actor_id)
        if causer_int is not None:
            controllers.add(causer_int)
    cnames = _resolve_player_controllers(controllers)

    for epoch, actor_id, causer_int, thing, shielded in raids:
        out["events"].append({
            "epoch": epoch,
            "owner": cnames.get(str(actor_id)) or f"acct {actor_id}",
            "raider": cnames.get(str(causer_int)) or "an unknown raider",
            "thing": thing,
            "shielded": shielded,
        })
    return out


def section_server_health():
    try:
        with open(POD_STATE) as f:
            state = json.load(f)
    except Exception:
        return None
    troubled = []
    earliest = None
    for pod, info in state.items():
        short = pod.split("-sg-")[-1] if "-sg-" in pod else pod
        rc = int(info.get("restartCount", 0))
        phase = info.get("phase", "")
        if rc > 0:
            troubled.append(f"{short} (x{rc})")
        elif phase and phase != "Running":
            troubled.append(f"{short} ({phase})")
        st = info.get("startTime")
        if st:
            try:
                t = datetime.strptime(st, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if earliest is None or t < earliest:
                    earliest = t
            except ValueError:
                pass
    return {
        "pods_total": len(state),
        "troubled": troubled,
        "up_since_utc": earliest.isoformat() if earliest else None,
    }


def section_improvements(window_start):
    """Server improvements logged via add-improvement.py within the window."""
    try:
        with open(IMPROVEMENTS_PATH, encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
    except FileNotFoundError:
        return []
    recent = [e for e in entries if e.get("ts", 0) > window_start]
    recent.sort(key=lambda e: e["ts"])
    return [e["text"] for e in recent[-12:]]


# ---- new-capability sections ----------------------------------------------

def section_spice():
    """Spice-field spawning status (W6 capability)."""
    rows = psql(
        "SELECT map_name, field_type, current_globally_active, is_spawning_active "
        "FROM dune.spicefield_types ORDER BY map_name, field_type;"
    )
    fields = []
    active_total = spawning = 0
    for map_name, field_type, active_now, spawning_active in rows:
        on = spawning_active in ("t", "true", True)
        active_now = int(active_now or 0)
        fields.append({"map": map_name, "field_type": field_type,
                       "active_now": active_now, "spawning": on})
        active_total += active_now
        if on:
            spawning += 1
    return {"fields": fields, "active_now_total": active_total,
            "spawning_count": spawning, "field_count": len(fields)}


def section_economy(interval_sql):
    """Server-wide currency totals + market net injected/sunk this period.

    totals = sum of player_virtual_currency_balances per currency (0 = Solari,
    1 = Scrip), the same join used by dune-player-vitals.py get_economy() but
    aggregated across every controller instead of one account.

    market = dune.ls_market_log realised flow over the window: Solari paid into
    sell proceeds (completion_type 4) vs spent on purchases (completion_type 5),
    and the net Solari injected/sunk by bot trades (bot_trade IS TRUE).
    """
    out = {"totals": {"solari": None, "scrip": None}, "market": {}}
    try:
        rows = psql(
            "SELECT currency_id, sum(balance) "
            "FROM dune.player_virtual_currency_balances "
            "GROUP BY currency_id;"
        )
        for cid, total in rows:
            key = {0: "solari", 1: "scrip"}.get(int(cid))
            if key:
                out["totals"][key] = int(total or 0)
    except Exception as e:
        log(f"economy totals failed: {e}")
    try:
        rows = psql(
            "SELECT "
            "  COALESCE(sum(CASE WHEN completion_type=4 "
            "    THEN item_price*stack_size ELSE 0 END),0), "
            "  COALESCE(sum(CASE WHEN completion_type=5 "
            "    THEN item_price*stack_size ELSE 0 END),0), "
            "  COALESCE(sum(CASE WHEN bot_trade IS TRUE "
            "    THEN item_price*stack_size ELSE 0 END),0), "
            "  count(*) "
            "FROM dune.ls_market_log "
            f"WHERE logged_at > now() - interval '{interval_sql}';"
        )
        if rows:
            sold, bought, bot_flow, trades = rows[0]
            out["market"] = {
                "sold_value": int(sold or 0),
                "bought_value": int(bought or 0),
                "bot_trade_value": int(bot_flow or 0),
                "net_injected": int(sold or 0) - int(bought or 0),
                "trades": int(trades or 0),
            }
    except Exception as e:
        log(f"economy market_log failed: {e}")
    return out


def section_moderation(interval_sql):
    """Kicks/bans/unbans this period + currently-active bans."""
    out = {"kicks": 0, "bans": 0, "unbans": 0, "active_bans": 0}
    try:
        rows = psql(
            f"SELECT action_type, count(*) FROM lsadmin.player_actions "
            f"WHERE created_at >= now() - interval '{interval_sql}' "
            f"GROUP BY action_type;"
        )
        for action_type, cnt in rows:
            key = (action_type or "").strip().lower()
            if key in out:
                out[key] = int(cnt)
    except Exception as e:
        log(f"moderation player_actions failed: {e}")
    try:
        out["active_bans"] = int(psql("SELECT count(*) FROM lsadmin.bans WHERE active;")[0][0])
    except Exception as e:
        log(f"moderation active_bans failed: {e}")
    return out


def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def section_almanac(con, window_start, iso_week):
    """Weekly 'Desert Almanac' flavor facts (the cog renders these weekly only).

    flight + world-snapshot deltas come from telemetry.db; worm breaches and
    sandstorms come from the new world_events stream and stay None until that
    table accrues data, so the cog hides those lines on a fresh deploy.
    """
    out = {"flight_km_total": None, "structures_delta": None, "vehicles_now": None,
           "worm_breaches": None, "sandstorms": None}

    try:
        row = con.execute(
            "SELECT SUM(meters) FROM flight_distance_weekly WHERE iso_week = ?",
            (iso_week,),
        ).fetchone()
        if row and row[0]:
            out["flight_km_total"] = round(row[0] / 1000.0, 1)
    except Exception as e:
        log(f"almanac flight failed: {e}")

    try:
        rows = con.execute(
            "SELECT value FROM world_snapshots WHERE metric='structures' "
            "AND ts >= ? ORDER BY ts",
            (window_start,),
        ).fetchall()
        if len(rows) >= 2:
            out["structures_delta"] = int(rows[-1][0]) - int(rows[0][0])
    except Exception as e:
        log(f"almanac structures failed: {e}")

    try:
        row = con.execute(
            "SELECT value FROM world_snapshots WHERE metric='vehicles' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            out["vehicles_now"] = int(row[0])
    except Exception as e:
        log(f"almanac vehicles failed: {e}")

    # world_events only exists once the new telemetry stream has run. Keep both
    # counters None until the table holds data so the cog can hide the lines.
    if _table_exists(con, "world_events"):
        try:
            total = con.execute("SELECT count(*) FROM world_events").fetchone()[0]
            if total:
                out["worm_breaches"] = int(con.execute(
                    "SELECT count(*) FROM world_events "
                    "WHERE event_type='worm_breach' AND ts >= ?",
                    (window_start,),
                ).fetchone()[0])
                storms = {"HaggaBasin": 0, "DeepDesert": 0}
                for mp, cnt in con.execute(
                    "SELECT map, count(*) FROM world_events "
                    "WHERE event_type='sandstorm' AND ts >= ? GROUP BY map",
                    (window_start,),
                ).fetchall():
                    if mp in storms:
                        storms[mp] = int(cnt)
                out["sandstorms"] = storms
        except Exception as e:
            log(f"almanac world_events failed: {e}")

    return out


def section_worlds():
    """Live per-world player counts + total online (from dune-status.py)."""
    try:
        r = subprocess.run([STATUS_SCRIPT], capture_output=True, text=True, timeout=60)
        status = json.loads(r.stdout)
    except Exception as e:
        log(f"worlds status failed: {e}")
        return None
    hagga = sum(m.get("players", 0) for m in status.get("maps", []) if m.get("map") == HAGGA_MAP)
    deep = sum(m.get("players", 0) for m in status.get("maps", []) if m.get("map") == DEEP_DESERT_MAP)
    return {
        "hagga_players": hagga,
        "deep_desert_players": deep,
        "online_total": int(status.get("online_players", 0) or 0),
    }


# ---- testing stations -----------------------------------------------------

# dungeon_id -> the name players actually use.
# DA_Dgn_Pit = "The Old Quarry": CONFIRMED by the owner 2026-07-28 (it was inferred from
# being the only non-numbered station in a list of six with one non-numbered entry).
STATION_NAMES = {
    "DA_Dgn_024_Darkness":  "Testing Station 24",
    "DA_Dgn_089_Radiation": "Testing Station 89",
    "DA_Dgn_136_Fire":      "Testing Station 136",
    "DA_Dgn_152_Electric":  "Testing Station 152",
    "DA_Dgn_195_Poison":    "Testing Station 195",
    "DA_Dgn_Pit":           "The Old Quarry",
}

# 🔴 Seeded test data, NOT play: rows with a duration of exactly 600.000s. Audited
# 2026-07-28 as completion_id 329-334 (one per station, solo, difficulty 100); ticket #132
# (2026-10-29) found 327 from the same seeded player at difficulty 40, which the old id
# list missed. No real clear lands on exactly 10:00.000, so the rule is narrower than a
# difficulty cut-off and does not need a new id every time one turns up. The number
# dropped is reported in the payload so the exclusion is never invisible.
SEED_DURATION_MS = 600000
NOT_SEED = f"duration_ms <> {SEED_DURATION_MS}"

WATERMARK_PATH = "/var/lib/lastsietch-stats/dungeon-watermark.json"


def _read_watermark(period):
    try:
        with open(WATERMARK_PATH) as fh:
            return int(json.load(fh).get(period) or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _write_watermark(period, value):
    """Daily and weekly keep SEPARATE cursors -- one shared cursor would mean whichever
    digest ran first consumed the window for the other."""
    try:
        with open(WATERMARK_PATH) as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[period] = int(value)
    try:
        os.makedirs(os.path.dirname(WATERMARK_PATH), exist_ok=True)
        tmp = WATERMARK_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, WATERMARK_PATH)
    except OSError as e:
        log(f"watermark write failed: {e}")


def section_testing_stations(period):
    """Highest difficulty COMPLETED per repeatable testing station.

    dune.dungeon_completion has NO timestamp column, so "this period" cannot be a time
    window. completion_id is monotonic and is used as the cursor instead, with a
    watermark persisted per period.

    ⚠️ Deliberately NOT a per-player leaderboard. Funcom's proc writes one
    dungeon_completion_players row per member, but party rows are later lost (character
    deletes cascade) and some point at controllers with no player_state, so the one row
    left on a group clear cannot be trusted (ticket #132). A runner is named only for a
    verified solo: players_num = 1 with exactly one player row whose name resolves.
    The record row is picked the way dune.get_best_dungeon_completion picks it.
    """
    since = _read_watermark(period)
    # Only the repeatable stations: event dungeons (e.g. DA_Dng_HalloweenEvent_2026) also
    # write dungeon_completion and would otherwise show up under their raw asset id
    stations_in = ",".join(f"'{k}'" for k in STATION_NAMES)

    rows = psql(
        "SELECT dungeon_id, max(difficulty), max(completion_id), count(*) "
        f"FROM dune.dungeon_completion WHERE {NOT_SEED} AND dungeon_id IN ({stations_in}) "
        "GROUP BY 1 ORDER BY 2 DESC;"
    )
    period_rows = psql(
        "SELECT dungeon_id, max(difficulty) "
        f"FROM dune.dungeon_completion WHERE {NOT_SEED} AND dungeon_id IN ({stations_in}) "
        f"AND completion_id > {int(since)} GROUP BY 1;"
    )
    best_this_period = {r[0]: int(r[1] or 0) for r in period_rows if len(r) >= 2}

    stations, high_water = [], since
    for row in rows:
        if len(row) < 4:
            continue
        dungeon_id, top, max_id, runs = row[0], int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)
        high_water = max(high_water, max_id)
        detail = psql(
            "SELECT dc.players_num, "
            "(SELECT count(*) FROM dune.dungeon_completion_players p "
            "WHERE p.completion_id = dc.completion_id), "
            "(SELECT coalesce(min(ps.character_name), '') FROM dune.dungeon_completion_players p "
            "JOIN dune.player_state ps ON ps.player_controller_id = p.player_id "
            "WHERE p.completion_id = dc.completion_id) "
            "FROM dune.dungeon_completion dc "
            f"WHERE dc.dungeon_id = '{dungeon_id}' AND dc.difficulty = {top} AND dc.{NOT_SEED} "
            "ORDER BY dc.players_num, dc.duration_ms, dc.completion_id LIMIT 1;"
        )
        party, runner = 0, ""
        if detail and len(detail[0]) >= 3:
            party = int(detail[0][0] or 0)
            if party == 1 and int(detail[0][1] or 0) == 1:
                runner = (detail[0][2] or "").strip()
        stations.append({
            "dungeon_id": dungeon_id,
            "name": STATION_NAMES.get(dungeon_id, dungeon_id),
            "top_difficulty": top,
            "top_this_period": best_this_period.get(dungeon_id, 0),
            "runs": runs,
            "record_runner": runner,
            "record_party_size": party,
        })

    if high_water > since:
        _write_watermark(period, high_water)

    seeded = psql(
        f"SELECT count(*) FROM dune.dungeon_completion WHERE duration_ms = {SEED_DURATION_MS};")
    return {
        "stations": stations,
        "station_count": len(stations),
        "cursor_from": since,
        "cursor_to": high_water,
        "first_run": since == 0,     # no baseline yet, so "this period" is meaningless
        "excluded_seed_rows": int(seeded[0][0]) if seeded and seeded[0] else 0,
    }


# ---- main -----------------------------------------------------------------

def collect(period):
    now = time.time()
    if period == "daily":
        window_seconds = 86400
        interval_sql = "24 hours"
    else:
        window_seconds = 604800
        interval_sql = "7 days"
    window_start = int(now - window_seconds)
    iso_week = "{}-W{:02d}".format(*datetime.now(timezone.utc).isocalendar()[:2])

    con = (sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
           if os.path.exists(DB_PATH) else None)

    def safe(fn, *a, default=None):
        try:
            return fn(*a)
        except Exception as e:
            log(f"section error in {fn.__name__}: {e}")
            return default

    out = {
        "period": period,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "window_seconds": window_seconds,
        "iso_week": iso_week,
        "new_players": safe(section_new_players, interval_sql, default={}),
        "server_pulse": safe(section_server_pulse, con, window_start, period) if con else None,
        "most_active": safe(section_most_active, con, window_start, default=[]) if con else [],
        "deaths": safe(section_deaths, con, window_start, default=[]) if con else [],
        "raids": safe(section_raids, con, window_start, default={}) if con else {},
        "server_health": safe(section_server_health, default=None),
        "improvements": safe(section_improvements, window_start, default=[]),
        "spice": safe(section_spice, default=None),
        "economy": safe(section_economy, interval_sql, default={}),
        "moderation": safe(section_moderation, interval_sql, default={}),
        "worlds": safe(section_worlds, default=None),
        "testing_stations": safe(section_testing_stations, period, default=None),
        "almanac": safe(section_almanac, con, window_start, iso_week, default=None) if con else None,
    }

    if period == "weekly":
        out["pilot"] = safe(section_pilot, con, iso_week, default=[]) if con else []
        out["origins"] = safe(section_origins, con, window_start, default=[]) if con else []
    else:
        out["pilot"] = None
        out["origins"] = None

    if con:
        con.close()
    return out


# Report collection is a separate, read-only path. It never advances the legacy
# testing-station watermark; published editions supply the comparison baseline.
REPORT_SCOPE_PATH = '/etc/lastsietch/report-scope.json'


def report_query(sql):
    rows = psql("BEGIN READ ONLY; SET LOCAL statement_timeout='8s'; SET LOCAL lock_timeout='500ms'; SET LOCAL TIME ZONE 'UTC';\n" + sql + '\nROLLBACK;')
    return [row for row in rows if row not in (['BEGIN'], ['SET'], ['ROLLBACK'])]


def report_read(con, sql, args=()):
    deadline = time.monotonic() + 3
    con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.set_progress_handler(None, 0)


def report_scope():
    import stat
    try:
        descriptor = os.open(REPORT_SCOPE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return set(), {}
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022 or info.st_size > 65536):
            raise ValueError('unsafe_report_scope')
        value = json.loads(source.read(65537))
    if not isinstance(value, dict) or set(value) - {'excluded_account_ids', 'public_aliases'}:
        raise ValueError('invalid_report_scope')
    excluded = value.get('excluded_account_ids', [])
    aliases = value.get('public_aliases', {})
    if (not isinstance(excluded, list) or any(type(i) is not int or i <= 0 for i in excluded)
            or not isinstance(aliases, dict)
            or any(not str(k).isdigit() or not isinstance(v, str) or not v.strip() or len(v) > 80
                   for k, v in aliases.items())):
        raise ValueError('invalid_report_scope')
    return {str(i) for i in excluded}, aliases


def report_names(con, account_ids, aliases):
    ids = sorted({str(a) for a in account_ids if str(a).isdigit()})
    names = {key: aliases[key] for key in ids if key in aliases}
    if con is not None and ids:
        marks = ','.join('?' for _ in ids)
        try:
            rows = report_read(con, 'SELECT account_id,character_name FROM roster_snapshot WHERE account_id IN ('
                               + marks + ') AND character_name IS NOT NULL ORDER BY ts', ids)
            for aid, name in rows:
                if name and str(aid) not in aliases:
                    names[str(aid)] = name
        except sqlite3.Error:
            pass
    return {key: names.get(key, 'A fellow Sleeper') for key in ids}


def report_activity(con, start, end, excluded, aliases, period):
    from collections import defaultdict
    from zoneinfo import ZoneInfo
    if con is None:
        raise ValueError('activity_unavailable')
    rows = report_read(con, 'SELECT DISTINCT ts,account_id FROM presence WHERE ts>=? AND ts<? ORDER BY ts LIMIT 500001',
                       (start, end))
    if not rows or len(rows) > 500000:
        raise ValueError('activity_coverage_unverified')
    by_tick, by_account = defaultdict(set), defaultdict(int)
    zone = ZoneInfo('America/New_York')
    bins = {}
    cursor = datetime.fromtimestamp(start, zone)
    stop = datetime.fromtimestamp(end, zone)
    while cursor < stop:
        next_at = min(cursor + timedelta(days=1 if period == 'weekly' else 0,
                                                               hours=0 if period == 'weekly' else 4), stop)
        bins[int(cursor.timestamp())] = {'start': int(cursor.timestamp()), 'end': int(next_at.timestamp()),
                                        'label': cursor.strftime('%a %m/%d') if period == 'weekly' else cursor.strftime('%-I %p'),
                                        'hours': None}
        cursor = next_at
    for stamp, aid in rows:
        if str(aid) in excluded:
            continue
        by_tick[stamp].add(str(aid)); by_account[str(aid)] += 1
        for point in bins.values():
            if point['start'] <= stamp < point['end']:
                point['hours'] = (point['hours'] or 0) + SAMPLE_MINUTES / 60.0
                break
    leaders = sorted(by_account.items(), key=lambda item: (-item[1], item[0]))[:5]
    names = report_names(con, [aid for aid, _ in leaders], aliases)
    return {'active_accounts': len(by_account), 'peak': max((len(v) for v in by_tick.values()), default=0),
            'player_hours': round(sum(by_account.values()) * SAMPLE_MINUTES / 60.0, 1), 'coverage': None,
            'leaders': [{'name': names.get(aid, 'A fellow Sleeper'), 'hours': hours(count)} for aid, count in leaders],
            'series': [{'start': point['start'], 'label': point['label'],
                        'hours': round(point['hours'], 1) if point['hours'] is not None else None}
                       for point in bins.values()]}


def report_arrivals(start, end, excluded, aliases):
    rows = report_query('SELECT g.account_id,coalesce(max(ps.character_name),\'\') '
        'FROM dune.ls_welcome_pack_grants g LEFT JOIN dune.player_state ps ON ps.account_id=g.account_id '
        f"WHERE g.granted_at>=to_timestamp({start}) AND g.granted_at<to_timestamp({end}) "
        "AND g.notes NOT LIKE 'operator-skip%' GROUP BY g.account_id ORDER BY g.account_id;")
    chosen = [(str(row[0]), row[1]) for row in rows if str(row[0]) not in excluded]
    return {'count': len(chosen), 'names': [aliases.get(aid, name or 'A fellow Sleeper') for aid, name in chosen[:5]]}


def report_economy(start, end):
    rows = report_query('SELECT count(*) FILTER(WHERE completion_type=4), '
        'coalesce(sum(item_price*stack_size) FILTER(WHERE completion_type=4),0), '
        'count(*) FILTER(WHERE completion_type=5) FROM dune.ls_market_log '
        f'WHERE logged_at>=to_timestamp({start}) AND logged_at<to_timestamp({end}) '
        'AND completion_type IN (4,5);')
    if len(rows) != 1 or len(rows[0]) != 3:
        raise ValueError('market_result_unverified')
    sales, value, purchases = rows[0]
    return {'sales_count': int(sales), 'sale_value': int(value), 'purchase_count': int(purchases)}


def report_stations(excluded, aliases):
    stations = ','.join("'" + key + "'" for key in STATION_NAMES)
    # Record row picked like dune.get_best_dungeon_completion; credited only for a verified solo
    rows = report_query('WITH records AS (SELECT DISTINCT ON (dungeon_id) '
        'dungeon_id,difficulty,completion_id,players_num FROM dune.dungeon_completion '
        f'WHERE {NOT_SEED} AND dungeon_id IN ({stations}) '
        'ORDER BY dungeon_id,difficulty DESC,players_num,duration_ms,completion_id) '
        'SELECT r.dungeon_id,r.difficulty,r.players_num,coalesce(p.character_name,\'\'),p.account_id,'
        '(SELECT count(*) FROM dune.dungeon_completion_players c WHERE c.completion_id=r.completion_id) '
        'FROM records r LEFT JOIN LATERAL (SELECT ps.character_name,ps.account_id '
        'FROM dune.dungeon_completion_players dcp JOIN dune.player_state ps '
        'ON ps.player_controller_id=dcp.player_id WHERE dcp.completion_id=r.completion_id '
        'ORDER BY ps.account_id,ps.player_controller_id LIMIT 1) p ON true ORDER BY r.difficulty DESC;')

    def credit(row):
        solo = int(row[2] or 0) == 1 and int(row[5] or 0) == 1
        if not solo or str(row[4]) in excluded:
            return ''
        return aliases.get(str(row[4]), row[3])

    return [{'id': row[0], 'name': STATION_NAMES[row[0]], 'tier': int(row[1]),
             'party_size': int(row[2] or 0), 'runner': credit(row)} for row in rows]


def report_losses(con, start, end, excluded):
    if con is None:
        raise ValueError('losses_unavailable')
    rows = report_read(con, 'SELECT raw,victim_account_id FROM combat_events WHERE event_type=0 AND victim_account_id IS NOT NULL '
                       'AND occurred_epoch>=? AND occurred_epoch<? LIMIT 500001', (start, end))
    if len(rows) > 500000:
        raise ValueError('losses_window_too_large')
    rows = [row for row in rows if str(row[1]) not in excluded]
    worm = 0
    for row in rows:
        try:
            value = json.loads(row[0])
            worm += value.get('m_KillerType') == 'ShaiHulud'
        except (ValueError, TypeError, AttributeError):
            pass
    return {'total': len(rows), 'worm': worm}


def report_spice():
    rows = report_query('SELECT current_globally_active,is_spawning_active FROM dune.spicefield_types;')
    if not rows:
        raise ValueError('spice_unavailable')
    return {'active': sum(int(row[0] or 0) for row in rows),
            'enabled': sum(row[1] in ('t', 'true') for row in rows), 'types': len(rows)}


def report_changes(start, end):
    try:
        with open(IMPROVEMENTS_PATH, encoding='utf-8') as source:
            rows = [json.loads(line) for line in source if line.strip()]
    except FileNotFoundError:
        return []
    return [{'text': row['text']} for row in rows if start <= row.get('ts', 0) < end][-6:]


def collect_report(period, start, end):
    if (period not in ('daily', 'weekly') or type(start) is not int or type(end) is not int
            or start < 1 or end > time.time() + 1 or end <= start
            or not (23*3600 <= end-start <= 25*3600 if period == 'daily' else 167*3600 <= end-start <= 169*3600)
            or time.time() - end > 31*86400):
        raise ValueError('invalid_report_window')
    excluded, aliases = report_scope()
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) if os.path.exists(DB_PATH) else None
    def part(fn, *args):
        try:
            return {'status': 'ok', 'data': fn(*args)}
        except Exception:
            return {'status': 'unavailable', 'data': None}
    try:
        result = {'schema': 2, 'period': period, 'window_start': start, 'window_end': end,
                  'activity': part(report_activity, con, start, end, excluded, aliases, period),
                  'arrivals': part(report_arrivals, start, end, excluded, aliases),
                  'economy': part(report_economy, start, end), 'stations': part(report_stations, excluded, aliases),
                  'losses': part(report_losses, con, start, end, excluded), 'spice': part(report_spice),
                  'changes': part(report_changes, start, end)}
        result['captured_at'] = int(time.time())
        return result
    finally:
        if con is not None:
            con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["daily", "weekly"])
    ap.add_argument('--report-job-b64')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--window-start', type=int)
    ap.add_argument('--window-end', type=int)
    args = ap.parse_args()
    if args.report_job_b64 is not None:
        if args.period is not None or args.report or args.window_start is not None or args.window_end is not None:
            ap.error('report payload cannot be combined with other arguments')
        if len(args.report_job_b64) > 1024:
            ap.error('report payload too large')
        job = json.loads(base64.b64decode(args.report_job_b64, validate=True))
        if not isinstance(job, dict) or set(job) != {'period', 'window_start', 'window_end'}:
            ap.error('invalid report payload')
        result = collect_report(job['period'], job['window_start'], job['window_end'])
    elif args.period is None:
        ap.error('--period is required')
    elif args.report:
        result = collect_report(args.period, args.window_start, args.window_end)
    elif args.window_start is not None or args.window_end is not None:
        ap.error('explicit windows require --report')
    else:
        result = collect(args.period)
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
