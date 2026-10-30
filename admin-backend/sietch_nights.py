"""Seven Nights of the Sietch: night math, eligibility, caps, credit and Knock.

Everything here is admin.db only. Nothing reads or writes dune.* directly; the
three game signals arrive through existing read paths and are handed in by the
caller (the worker for login days and the mirror, the router for the Knock
position), so every function below is stdlib-only and takes its connection and
its clock explicitly.

Money model (PLAN §3, §6, §16.4):
  * portal_sn_earn is per-account evidence, one row per (account, night, source,
    ref). It accrues for unlinked accounts too.
  * credit_profile() turns evidence into ledger rows for one profile. For each
    (night, source, ref) the credit is the MAX amount across the profile's linked
    accounts, never the SUM, then clipped to what is left of that source's cap
    for the night. ux_sn_credit makes a second credit impossible.
  * A balance is SUM(delta) over every event. Earned totals (board, calendar) are
    scoped to this event.

One night is one UTC day: it starts at 00:00Z, which is 20:00 EDT, and night 7
ends at 00:00Z on 11-02, which is 19:00 EST because DST ends on 11-01. Every
boundary is computed in UTC; nothing here knows a fixed 20:00 ET.
"""
import json
import logging
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import map_instances

log =logging.getLogger("sietch_nights")

EVENT = "sn2026"
NIGHT_KEYS = ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29",
              "2026-10-30", "2026-10-31", "2026-11-01")
ACTIVITIES = ("porches", "riddles", "offerings", "stall", "seek", "haunt", "costumes")
# Earning eligibility: a login before this UTC date, or this character level.
EARLY_LOGIN_BEFORE = "2026-10-25"
ELIGIBLE_LVL = 20
# Late evidence (a login-days pull that lands after the close) still credits for
# this long after the last night ends.
CREDIT_GRACE = timedelta(hours=6)

# Every earn was doubled on 2026-10-27 (owner), retroactive to night 1.
VISIT_AWARD = 4
STREAK_AWARD = 20
KNOCK_CAP = 6
LANDSRAAD_T = 3000          # set from the 2026-10-24 dry run (median player-night ~3500)
TRAIL_ALL_AWARD = 6
SEEK_FIRST_N = 3
STORM_BONUS = 4

# Per-night caps per source, in tokens. activity and admin are bounded by their
# own spot rows and case ids instead.
LANDSRAAD_REF_AWARD = 2   # per ladder rung (t1..t4)
CAPS = {"visit": VISIT_AWARD, "knock": KNOCK_CAP, "landsraad": 2 * LANDSRAAD_REF_AWARD,
        "streak": STREAK_AWARD}
OFFERINGS_NIGHT = NIGHT_KEYS[2]

# Game units are centimetres: 15,000 is the owner-approved 150 m.
DEFAULT_RADIUS = 15000.0
SEEK_BANDS = ((30000.0, "scorching"), (150000.0, "warm"), (400000.0, "cool"))
# Extra points that also count as a spot. The game ships two "Wreck of the Tartarus"
# map markers ~2.25 km apart; players following the in-game label stand at the second.
SPOT_ALIASES = {"n1-wreck-tartarus": ((-392417.0, 308351.0),)}
# Every open Hagga sietch (map instance config); a closed one is off the event.
HAGGA_EVENT_PARTITIONS = tuple(r["partition"] for r in map_instances.open_instances("hagga")
                               if r["partition"] is not None)
HIDDEN_CAP_SHOWN = 20   # the activity cap shown for a night's hidden spots, whatever they hold
KNOCK_COOLDOWN_S = 30
KNOCKS_PER_NIGHT = 60

SPOT_KINDS = ("porch", "clue", "stall", "seek", "haunt")
SPOT_MAPS = ("hagga", "deep-desert")
_SPOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_PROFILE_RE = re.compile(r"^[a-f0-9]{32}$")
_GAME_MAP_TO_KEY = {"HaggaBasin": "hagga", "DeepDesert": "deep-desert"}


# --- night math --------------------------------------------------------------

def _utc(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def night_bounds(key):
    start = datetime.combine(date.fromisoformat(key), datetime.min.time(), timezone.utc)
    return start, start + timedelta(days=1)


def night_info(n):
    key = NIGHT_KEYS[n - 1]
    start, end = night_bounds(key)
    return {"n": n, "key": key, "starts_at": _iso(start), "ends_at": _iso(end),
            "activity": ACTIVITIES[n - 1]}


def night_number(key):
    return NIGHT_KEYS.index(key) + 1 if key in NIGHT_KEYS else 0


def current_night(now=None):
    """The night running at `now` as night_info(), or None outside the event."""
    key = _utc(now).date().isoformat()
    n = night_number(key)
    return night_info(n) if n else None


def event_start():
    return night_bounds(NIGHT_KEYS[0])[0]


def event_end():
    return night_bounds(NIGHT_KEYS[-1])[1]


def next_night_start(now=None):
    """ISO start of the next night after `now`, or None once night 7 has begun."""
    now = _utc(now)
    for key in NIGHT_KEYS:
        start, _ = night_bounds(key)
        if start > now:
            return _iso(start)
    return None


def credit_open(now=None):
    now = _utc(now)
    return event_start() <= now < event_end() + CREDIT_GRACE


def calendar_state(key, now=None):
    start, end = night_bounds(key)
    now = _utc(now)
    if now >= end:
        return "past"
    if now >= start:
        return "tonight"
    return "upcoming"


def _now_db(now=None):
    return _utc(now).strftime("%Y-%m-%d %H:%M:%S")


# --- identity ----------------------------------------------------------------

def valid_profile_id(value):
    return isinstance(value, str) and _PROFILE_RE.fullmatch(value) is not None


def _has(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _links_source(conn):
    # portal_identity_links is the live view (credentials installed). The raw
    # table is the fallback a fresh or test admin.db carries.
    if _has(conn, "portal_identity_links"):
        return ("SELECT profile_id, account_id, character_name, discord_id, linked_at "
                "FROM portal_identity_links")
    if _has(conn, "portal_profile_accounts"):
        return ("SELECT a.profile_id, a.account_id, '' AS character_name, "
                "COALESCE(p.legacy_discord_id, 'profile:' || p.id) AS discord_id, a.linked_at "
                "FROM portal_profile_accounts a JOIN portal_profiles p ON p.id = a.profile_id "
                "WHERE a.revoked_at IS NULL AND p.disabled_at IS NULL")
    return None


def profile_accounts(conn, profile_id):
    """[account_id, ...] actively linked to the profile, oldest link first."""
    src = _links_source(conn)
    if src is None or not valid_profile_id(profile_id):
        return []
    rows = conn.execute("SELECT account_id FROM (" + src + ") WHERE profile_id = ? "
                        "ORDER BY linked_at, account_id", (profile_id,)).fetchall()
    return [int(r[0]) for r in rows]


def profile_for_account(conn, account_id):
    src = _links_source(conn)
    if src is None:
        return None
    row = conn.execute("SELECT profile_id FROM (" + src + ") WHERE account_id = ? LIMIT 1",
                       (int(account_id),)).fetchone()
    return row[0] if row else None


def linked_profiles(conn):
    """{profile_id: [account_id, ...]} over every active link."""
    src = _links_source(conn)
    out = {}
    if src is None:
        return out
    for pid, aid in conn.execute("SELECT profile_id, account_id FROM (" + src + ") "
                                 "ORDER BY linked_at, account_id"):
        if valid_profile_id(pid):
            out.setdefault(pid, []).append(int(aid))
    return out


def profile_identity(conn, profile_id):
    """(identity key used by portal_prefs, primary character name) for a profile."""
    src = _links_source(conn)
    if src is None:
        return None, None
    row = conn.execute("SELECT discord_id, character_name FROM (" + src + ") WHERE profile_id = ? "
                       "ORDER BY linked_at, account_id LIMIT 1", (profile_id,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


# --- eligibility -------------------------------------------------------------

def record_login_days(conn, account_id, dates, now=None):
    """Store what one login-days pull taught us: the early-login fact, and a visit
    evidence row for every event night the account was seen on (up to tonight).
    Returns the number of new visit rows."""
    account_id = int(account_id)
    dates = sorted({d for d in dates if isinstance(d, str)})
    early = 1 if any(d < EARLY_LOGIN_BEFORE for d in dates) else 0
    conn.execute("INSERT INTO portal_sn_accounts(account_id, early_login, login_pulled_at) "
                 "VALUES (?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
                 "early_login = MAX(early_login, excluded.early_login), "
                 "login_pulled_at = excluded.login_pulled_at",
                 (account_id, early, _now_db(now)))
    today = _utc(now).date().isoformat()
    added = 0
    for d in dates:
        if d in NIGHT_KEYS and d <= today:
            added += add_earn(conn, account_id, d, "visit", "", VISIT_AWARD,
                              {"date_utc": d})
    return added


def record_level(conn, account_id, lvl):
    if not isinstance(lvl, int) or isinstance(lvl, bool) or lvl <= 0:
        return
    conn.execute("INSERT INTO portal_sn_accounts(account_id, max_lvl) VALUES (?, ?) "
                 "ON CONFLICT(account_id) DO UPDATE SET max_lvl = MAX(max_lvl, excluded.max_lvl)",
                 (int(account_id), lvl))


def account_eligible(conn, account_id):
    row = conn.execute("SELECT early_login, max_lvl FROM portal_sn_accounts WHERE account_id = ?",
                       (int(account_id),)).fetchone()
    return bool(row) and (row[0] == 1 or (row[1] or 0) >= ELIGIBLE_LVL)


def profile_eligible(conn, profile_id, accounts=None):
    """Profile-level: any linked account qualifying is enough, so a rerolled
    character inherits it."""
    accounts = profile_accounts(conn, profile_id) if accounts is None else accounts
    return any(account_eligible(conn, a) for a in accounts)


# --- evidence and credit -----------------------------------------------------

def add_earn(conn, account_id, night, source, ref, amount, evidence=None, operator=None):
    """INSERT OR IGNORE one evidence row; 1 when it is new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO portal_sn_earn(event, account_id, night, source, ref, amount, "
        "evidence, operator) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (EVENT, int(account_id), night, source, ref or "", int(amount),
         json.dumps(evidence, separators=(",", ":")) if evidence is not None else None, operator))
    return cur.rowcount


def _credited(conn, profile_id, night, source):
    row = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM portal_sn_ledger WHERE kind = 'earn' "
                       "AND profile_id = ? AND night = ? AND source = ?",
                       (profile_id, night, source)).fetchone()
    return int(row[0])


def cap_for(night, source):
    if source == "landsraad" and night == OFFERINGS_NIGHT:
        return 4 * LANDSRAAD_REF_AWARD
    return CAPS.get(source)


def credit_profile(conn, profile_id, accounts=None):
    """Credit every pending evidence row of the profile's linked accounts. MAX per
    (night, source, ref) across accounts, then clipped to the night's source cap.
    The caller owns the transaction. Returns tokens credited."""
    accounts = profile_accounts(conn, profile_id) if accounts is None else accounts
    if not accounts or not profile_eligible(conn, profile_id, accounts):
        return 0
    marks = ",".join("?" * len(accounts))
    groups = conn.execute(
        "SELECT e.night, e.source, e.ref, MAX(e.amount) AS amount, MIN(e.id) AS earn_id "
        "FROM portal_sn_earn e WHERE e.event = ? AND e.account_id IN (" + marks + ") "
        "AND NOT EXISTS (SELECT 1 FROM portal_sn_ledger l WHERE l.kind = 'earn' "
        "  AND l.profile_id = ? AND l.night = e.night AND l.source = e.source AND l.ref = e.ref) "
        "GROUP BY e.night, e.source, e.ref ORDER BY MIN(e.id)",
        (EVENT, *accounts, profile_id)).fetchall()
    total = 0
    for night, source, ref, amount, earn_id in groups:
        cap = cap_for(night, source)
        if cap is not None:
            amount = min(amount, cap - _credited(conn, profile_id, night, source))
        if amount <= 0:
            continue
        cur = conn.execute("INSERT OR IGNORE INTO portal_sn_ledger(event, profile_id, delta, kind, night, "
                           "source, ref, earn_id) VALUES (?, ?, ?, 'earn', ?, ?, ?, ?)",
                           (EVENT, profile_id, amount, night, source, ref, earn_id))
        if cur.rowcount:
            total += amount
    total += _credit_streak(conn, profile_id)
    return total


def _credit_streak(conn, profile_id):
    """E2 full moon: a credited visit on all seven nights pays once, on night 7."""
    row = conn.execute("SELECT COUNT(DISTINCT night) FROM portal_sn_ledger WHERE kind = 'earn' "
                       "AND profile_id = ? AND source = 'visit' AND event = ?",
                       (profile_id, EVENT)).fetchone()
    if row[0] < len(NIGHT_KEYS):
        return 0
    cur = conn.execute("INSERT OR IGNORE INTO portal_sn_ledger(event, profile_id, delta, kind, night, "
                       "source, ref) VALUES (?, ?, ?, 'earn', ?, 'streak', '')",
                       (EVENT, profile_id, STREAK_AWARD, NIGHT_KEYS[-1]))
    return STREAK_AWARD if cur.rowcount else 0


def credit_all(conn):
    """One credit pass over every linked profile. Returns {profile_id: tokens}."""
    out = {}
    for pid, accounts in linked_profiles(conn).items():
        got = credit_profile(conn, pid, accounts)
        if got:
            out[pid] = got
    return out


def balance(conn, profile_id):
    row = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM portal_sn_ledger WHERE profile_id = ?",
                       (profile_id,)).fetchone()
    return int(row[0])


def earned_total(conn, profile_id):
    row = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM portal_sn_ledger WHERE profile_id = ? "
                       "AND event = ? AND kind IN ('earn', 'admin_adjust') AND delta > 0",
                       (profile_id, EVENT)).fetchone()
    return int(row[0])


def admin_award(conn, profile_id, amount, reason, case_id, operator, now=None):
    """E7. One row per (profile, case_id): the same case twice is a replay, not a
    second award. Returns (ledger_id, replay)."""
    night = current_night(now)
    cur = conn.execute("INSERT OR IGNORE INTO portal_sn_ledger(event, profile_id, delta, kind, night, "
                       "source, ref, operator, reason) VALUES (?, ?, ?, 'admin_adjust', ?, 'admin', ?, ?, ?)",
                       (EVENT, profile_id, int(amount), night["key"] if night else None,
                        case_id, operator, reason))
    if cur.rowcount:
        return cur.lastrowid, False
    row = conn.execute("SELECT id FROM portal_sn_ledger WHERE kind = 'admin_adjust' AND profile_id = ? "
                       "AND ref = ?", (profile_id, case_id)).fetchone()
    return row[0], True


# --- Discord event contests (Cielago, internal key) ---------------------------

CONTEST_CASE_RE = re.compile(
    r"^contest:(?P<cid>[a-z0-9-]{1,24}):(entry:(?P<did>[1-9][0-9]{0,19})|place:[1-3])$")
CONTEST_OPERATOR = "cielago-contest"


def contest_ids(raw):
    """LASTSIETCH_SIETCH_CONTEST_IDS ('a,b') -> the set of contest ids allowed to pay."""
    return {x.strip() for x in (raw or "").split(",") if re.fullmatch(r"[a-z0-9-]{1,24}", x.strip())}


def profile_for_discord(conn, discord_id):
    """The active profile whose ACTIVE Discord provider is this id. With the
    provider table installed a revoked provider is unlinked, even though the
    profile's legacy_discord_id still matches; without it, legacy is all there is."""
    if not isinstance(discord_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", discord_id):
        return None
    if _has(conn, "portal_profile_providers"):
        row = conn.execute("SELECT p.id FROM portal_profile_providers v JOIN portal_profiles p "
                           "ON p.id = v.profile_id WHERE v.provider = 'discord' AND v.subject = ? "
                           "AND v.revoked_at IS NULL AND p.disabled_at IS NULL", (discord_id,)).fetchone()
    elif _has(conn, "portal_profiles"):
        row = conn.execute("SELECT id FROM portal_profiles WHERE legacy_discord_id = ? "
                           "AND disabled_at IS NULL", (discord_id,)).fetchone()
    else:
        row = None
    return row[0] if row and valid_profile_id(row[0]) else None


def contest_award(conn, discord_id, amount, reason, case_id, allowed_ids, now=None):
    """A contest payout by Discord id. Returns (status, ledger_id, replay) with
    status 'ok', 'unlinked' or 'case_taken'. ux_sn_admin is per (profile, ref), so
    a place case already paid to ANOTHER profile is refused here, not re-paid.
    Only contest ids the server allows (LASTSIETCH_SIETCH_CONTEST_IDS) can pay at all."""
    m = CONTEST_CASE_RE.fullmatch(case_id or "")
    if m is None:
        raise ValueError("not a contest case id")
    if m.group("cid") not in allowed_ids:
        raise ValueError("contest id is not enabled on this server")
    if m.group("did") is not None and m.group("did") != discord_id:
        raise ValueError("an entry case must name the same discord id")
    pid = profile_for_discord(conn, discord_id)
    if pid is None:
        return "unlinked", None, False
    other = conn.execute("SELECT 1 FROM portal_sn_ledger WHERE kind = 'admin_adjust' AND ref = ? "
                         "AND profile_id != ?", (case_id, pid)).fetchone()
    if other is not None:
        return "case_taken", None, False
    ledger_id, replay = admin_award(conn, pid, amount, reason, case_id, CONTEST_OPERATOR, now)
    return "ok", ledger_id, replay


# --- E4 Landsraad -----------------------------------------------------------

def landsraad_readings(payload):
    """{ladder: my_contribution} for each ladder the payload actually carries, or
    None when the read failed. An empty or unavailable read is NOT zero: taking it
    as zero would re-base every account and pay the whole term on the next good read."""
    if not isinstance(payload, dict) or payload.get("available") is False:
        return None
    ladders = payload.get("ladders")
    if not isinstance(ladders, dict) or not ladders:
        return None
    out = {}
    for key, ladder in ladders.items():
        v = ladder.get("my_contribution") if isinstance(ladder, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[str(key)] = float(v)
    return out or None


def landsraad_turnins(payload):
    """{ladder: turn-in count} from the same payload, or None when the read
    carries no counts (an older game-side script): then only points pay."""
    if landsraad_readings(payload) is None:
        return None
    out = {}
    for key, ladder in payload["ladders"].items():
        v = ladder.get("my_turnins") if isinstance(ladder, dict) else None
        if isinstance(v, int) and not isinstance(v, bool):
            out[str(key)] = v
    return out or None


def landsraad_refs(night, delta, turnins=0):
    """One rung per turn-in (owner 2026-10-27), or the points rule, whichever pays more."""
    rungs = 4 if night == OFFERINGS_NIGHT else 2
    by_points = sum((delta > 0, delta >= LANDSRAAD_T, delta >= 2 * LANDSRAAD_T, delta >= 3 * LANDSRAAD_T))
    n = min(rungs, max(by_points, int(turnins or 0)))
    return ["t%d" % i for i in range(1, n + 1)]


def landsraad_observe(conn, account_id, night, readings, now=None, turnins=None):
    """Fold one reading into the account's night. Each ladder keeps its own base
    across nights, so a turn-in across the boundary is not lost, a ladder missing
    from one read is neither re-based nor paid, and a ladder's very first reading
    is only a baseline. A bare number is one ladder. Returns the evidence refs written."""
    if isinstance(readings, (int, float)) and not isinstance(readings, bool):
        readings = {"_total": float(readings)}
    if not readings:
        return []
    account_id = int(account_id)
    seen = _now_db(now)
    gained = 0.0
    for ladder, cur in sorted(readings.items()):
        prev = conn.execute("SELECT base FROM portal_sn_landsraad_ladder WHERE account_id = ? AND ladder = ?",
                            (account_id, ladder)).fetchone()
        if prev is not None:
            step = cur - prev[0]
            # A negative step is a term rollover on that ladder: re-base and keep what was earned.
            gained += (step if step > 0 else 0.0)
        conn.execute("INSERT INTO portal_sn_landsraad_ladder(account_id, ladder, base, seen_at) "
                     "VALUES (?, ?, ?, ?) ON CONFLICT(account_id, ladder) DO UPDATE SET "
                     "base = excluded.base, seen_at = excluded.seen_at", (account_id, ladder, cur, seen))
    # Turn-in counts ride their own ladder rows ("<ladder>#n") with the same baseline rule.
    new_turnins = 0
    for ladder, cur in sorted((turnins or {}).items()):
        key = ladder + "#n"
        prev = conn.execute("SELECT base FROM portal_sn_landsraad_ladder WHERE account_id = ? AND ladder = ?",
                            (account_id, key)).fetchone()
        if prev is not None and cur > prev[0]:
            new_turnins += int(cur - prev[0])
        conn.execute("INSERT INTO portal_sn_landsraad_ladder(account_id, ladder, base, seen_at) "
                     "VALUES (?, ?, ?, ?) ON CONFLICT(account_id, ladder) DO UPDATE SET "
                     "base = excluded.base, seen_at = excluded.seen_at", (account_id, key, cur, seen))
    total = sum(readings.values())
    row = conn.execute("SELECT delta, turnins FROM portal_sn_landsraad WHERE account_id = ? AND night = ?",
                       (account_id, night)).fetchone()
    delta = (row[0] if row else 0.0) + gained
    count = (row[1] if row else 0) + new_turnins
    conn.execute("INSERT INTO portal_sn_landsraad(account_id, night, base, delta, seen_at, turnins) "
                 "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, night) DO UPDATE SET base = excluded.base, "
                 "delta = excluded.delta, seen_at = excluded.seen_at, turnins = excluded.turnins",
                 (account_id, night, total, delta, seen, count))
    refs = landsraad_refs(night, delta, count)
    for ref in refs:
        add_earn(conn, account_id, night, "landsraad", ref, LANDSRAAD_REF_AWARD,
                 {"delta": delta, "turnins": count})
    return refs


# --- spots -------------------------------------------------------------------

def validate_spot(raw):
    """(spot_dict, None) or (None, error). Coordinates are game units."""
    if not isinstance(raw, dict):
        return None, "spot must be an object"
    sid = raw.get("id")
    if not isinstance(sid, str) or not _SPOT_ID_RE.fullmatch(sid):
        return None, "id must be 1-32 chars of a-z, 0-9 and -"
    if raw.get("night") not in NIGHT_KEYS:
        return None, "night must be one of the event night keys"
    if raw.get("kind") not in SPOT_KINDS:
        return None, "kind must be one of " + ", ".join(SPOT_KINDS)
    if raw.get("map") not in SPOT_MAPS:
        return None, "map must be one of " + ", ".join(SPOT_MAPS)
    out = {"id": sid, "night": raw["night"], "kind": raw["kind"], "map": raw["map"]}
    for k in ("x", "y"):
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None, k + " must be a number"
        out[k] = float(v)
    radius = raw.get("radius", DEFAULT_RADIUS)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 1000 <= radius <= 50000:
        return None, "radius must be 1000 to 50000 game units"
    out["radius"] = float(radius)
    award = raw.get("award")
    if isinstance(award, bool) or not isinstance(award, int) or not 1 <= award <= 10:
        return None, "award must be an integer 1 to 10"
    out["award"] = award
    bonus = raw.get("first_n_bonus", 0)
    if isinstance(bonus, bool) or not isinstance(bonus, int) or not 0 <= bonus <= 10:
        return None, "first_n_bonus must be an integer 0 to 10"
    out["first_n_bonus"] = bonus
    seq = raw.get("seq")
    if out["kind"] == "clue":
        if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 9:
            return None, "a clue needs seq 1 to 9"
    else:
        seq = None
    out["seq"] = seq
    for k, limit in (("public_name", 80), ("riddle", 400)):
        v = raw.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > limit):
            return None, "%s must be text up to %d chars" % (k, limit)
        out[k] = v.strip() if isinstance(v, str) else None
    if out["kind"] == "clue" and not out["riddle"]:
        return None, "a clue needs its riddle"
    # A seek point is never public, whatever the caller sent.
    out["public"] = 0 if out["kind"] == "seek" else (0 if raw.get("public") in (0, False) else 1)
    return out, None


def upsert_spot(conn, spot):
    conn.execute(
        "INSERT INTO portal_sn_spots(id, night, kind, map, x, y, radius, seq, public_name, riddle, "
        "award, first_n_bonus, public) VALUES (:id, :night, :kind, :map, :x, :y, :radius, :seq, "
        ":public_name, :riddle, :award, :first_n_bonus, :public) ON CONFLICT(id) DO UPDATE SET "
        "night = excluded.night, kind = excluded.kind, map = excluded.map, x = excluded.x, "
        "y = excluded.y, radius = excluded.radius, seq = excluded.seq, "
        "public_name = excluded.public_name, riddle = excluded.riddle, award = excluded.award, "
        "first_n_bonus = excluded.first_n_bonus, public = excluded.public", spot)


def spots_for(conn, night):
    rows = conn.execute("SELECT * FROM portal_sn_spots WHERE night = ? ORDER BY kind, seq, id",
                        (night,)).fetchall()
    return [dict(r) for r in rows]


def _hit_refs(conn, profile_id, night):
    rows = conn.execute("SELECT ref FROM portal_sn_ledger WHERE kind = 'earn' AND profile_id = ? "
                        "AND night = ? AND source IN ('knock', 'activity')",
                        (profile_id, night)).fetchall()
    return {r[0] for r in rows}


def current_clue(spots, done):
    """The lowest-seq clue not yet found, or None when the trail is done."""
    clues = sorted((s for s in spots if s["kind"] == "clue"), key=lambda s: s["seq"])
    for s in clues:
        if s["id"] not in done:
            return {"seq": s["seq"], "of": len(clues), "riddle": s["riddle"], "_id": s["id"]}
    return None


PINNED_KINDS = ("porch", "stall")


def public_spots(spots, done, project=None):
    """Player-safe projection: name and kind only. Seek and clue rows never list.
    With `project` (map_model.project), public porch and stall rows, which are
    named in-game landmarks, also carry their map pin in view units. Never a haunt,
    seek or clue, and never world x/y."""
    out = []
    for s in spots:
        if s["public"] != 1 or s["kind"] not in ("porch", "stall", "haunt"):
            continue
        row = {"id": s["id"], "name": s["public_name"] or "", "kind": s["kind"], "done": s["id"] in done}
        if project is not None and s["kind"] in PINNED_KINDS:
            nxy = project(s["map"], s["x"], s["y"])
            if nxy:
                row.update(map=s["map"], nx=round(nxy[0], 1), ny=round(nxy[1], 1))
        out.append(row)
    return out


def spot_dist(s, x, y):
    pts = ((s["x"], s["y"]),) + SPOT_ALIASES.get(s["id"], ())
    return min(math.hypot(x - px, y - py) for px, py in pts)


def dist_text(units):
    metres = units / 100.0
    if metres >= 1000:
        return "%.1f km" % (metres / 1000.0)
    return "%d m" % int(round(metres))


def seek_band(units):
    for limit, name in SEEK_BANDS:
        if units < limit:
            return name
    return "cold"


# --- Knock -------------------------------------------------------------------

def _source_for(kind):
    return "knock" if kind == "porch" else "activity"


def _log_knock(conn, profile_id, account_id, night, result, spot_id=None, dist=None,
               hit=0, stale=0, now=None):
    conn.execute("INSERT INTO portal_sn_knocks(profile_id, account_id, night, spot_id, dist, band, "
                 "hit, stale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (profile_id, int(account_id), night, spot_id, dist, result, hit, stale, _now_db(now)))


def knock_throttled(conn, profile_id, night, now=None):
    """'cooldown' when the last counted attempt is under 30 s old or 60 have been
    made tonight. Refused attempts are logged but never counted."""
    rows = conn.execute("SELECT created_at FROM portal_sn_knocks WHERE profile_id = ? AND night = ? "
                        "AND band <> 'cooldown' ORDER BY id DESC", (profile_id, night)).fetchall()
    if len(rows) >= KNOCKS_PER_NIGHT:
        return True
    if rows:
        last = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if (_utc(now) - last).total_seconds() < KNOCK_COOLDOWN_S:
            return True
    return False


def _credit_hit(conn, profile_id, account_id, night, spot, dist, accounts):
    """Evidence + immediate credit for one hit. Returns the tokens credited."""
    source = _source_for(spot["kind"])
    before = balance(conn, profile_id)
    add_earn(conn, account_id, night, source, spot["id"], spot["award"],
             {"dist": round(dist), "map": spot["map"]})
    if spot["kind"] == "seek" and spot["first_n_bonus"]:
        finders = conn.execute("SELECT COUNT(DISTINCT profile_id) FROM portal_sn_ledger WHERE kind = 'earn' "
                               "AND night = ? AND source = 'activity' AND ref = ?",
                               (night, spot["id"])).fetchone()[0]
        if finders < SEEK_FIRST_N:
            add_earn(conn, account_id, night, "activity", spot["id"] + ":first", spot["first_n_bonus"],
                     {"finder": finders + 1})
    credit_profile(conn, profile_id, accounts)
    return balance(conn, profile_id) - before


def knock(conn, profile_id, account_id, pos, now=None, spot_id=None, storm_live=False,
          accounts=None):
    """Evaluate one Knock. `pos` is the caller's OWN position read server-side
    ({"map","part","x","y","online"} plus "stale" when the cache served an old
    value) or None when the read failed. Runs its own BEGIN IMMEDIATE so the
    rate limit, the first-3 order and the credit are serialised. Never returns a
    coordinate, and never a distance to a public=0 spot."""
    night = current_night(now)
    if night is None:
        return {"result": "closed"}
    key = night["key"]
    accounts = profile_accounts(conn, profile_id) if accounts is None else accounts
    if account_id not in accounts:
        accounts = accounts + [account_id]
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = _knock_txn(conn, profile_id, account_id, key, pos, now, spot_id, storm_live, accounts)
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise


def _int_or_none(v):
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def off_event_hagga(pos):
    """Fail closed: on Hagga, a position that is not provably on an open sietch
    (HAGGA_EVENT_PARTITIONS) gets no doors and no hint."""
    if (pos.get("map") or "HaggaBasin") != "HaggaBasin":
        return False
    return _int_or_none(pos.get("part")) not in HAGGA_EVENT_PARTITIONS


def _knock_txn(conn, profile_id, account_id, key, pos, now, spot_id, storm_live, accounts):
    if knock_throttled(conn, profile_id, key, now):
        _log_knock(conn, profile_id, account_id, key, "cooldown", now=now)
        return {"result": "cooldown"}
    if not profile_eligible(conn, profile_id, accounts):
        _log_knock(conn, profile_id, account_id, key, "ineligible", now=now)
        return {"result": "ineligible"}
    # Stale first: a failed or cache-served read is "try again", never "offline".
    if isinstance(pos, dict) and pos.get("stale"):
        _log_knock(conn, profile_id, account_id, key, "stale", stale=1, now=now)
        return {"result": "stale"}
    if not isinstance(pos, dict) or pos.get("x") is None or pos.get("y") is None:
        _log_knock(conn, profile_id, account_id, key, "offline", now=now)
        return {"result": "offline"}
    if not pos.get("online"):
        _log_knock(conn, profile_id, account_id, key, "offline", now=now)
        return {"result": "offline"}
    if off_event_hagga(pos):
        # "amtal" is the result token the portal keys its copy on ("No doors out
        # here."), not an instance key: it stays fixed whatever the config holds.
        _log_knock(conn, profile_id, account_id, key, "amtal", now=now)
        return {"result": "amtal"}

    map_key = _GAME_MAP_TO_KEY.get(pos.get("map") or "")
    x, y = float(pos["x"]), float(pos["y"])
    spots = spots_for(conn, key)
    done = _hit_refs(conn, profile_id, key)
    clue = current_clue(spots, done)
    porches_full = _credited(conn, profile_id, key, "knock") >= cap_for(key, "knock")
    open_spots = []
    for s in spots:
        if s["id"] in done or (spot_id and s["id"] != spot_id):
            continue
        if s["kind"] == "porch" and porches_full:
            continue
        if s["kind"] == "clue" and (clue is None or s["id"] != clue["_id"]):
            continue
        open_spots.append(s)
    if not open_spots:
        _log_knock(conn, profile_id, account_id, key, "done", now=now)
        return {"result": "done"}

    here = [(spot_dist(s, x, y), s) for s in open_spots if s["map"] == map_key]
    here.sort(key=lambda t: t[0])
    for dist, s in here:
        if dist <= s["radius"]:
            return _knock_hit(conn, profile_id, account_id, key, s, dist, storm_live, accounts, now)

    seek = [(d, s) for d, s in here if s["kind"] == "seek"]
    if seek:
        dist, s = seek[0]
        band = seek_band(dist)
        _log_knock(conn, profile_id, account_id, key, "band", s["id"], dist, now=now)
        return {"result": "band", "band": band}
    if clue is not None and any(s["kind"] == "clue" for s in open_spots):
        _log_knock(conn, profile_id, account_id, key, "miss", clue["_id"],
                   here[0][0] if here else None, now=now)
        return {"result": "miss"}
    shown = [(d, s) for d, s in here if s["public"] == 1]
    _log_knock(conn, profile_id, account_id, key, "miss", shown[0][1]["id"] if shown else None,
               shown[0][0] if shown else None, now=now)
    out = {"result": "miss"}
    if shown:
        out["dist_text"] = dist_text(shown[0][0])
    return out


def _knock_hit(conn, profile_id, account_id, key, spot, dist, storm_live, accounts, now):
    _credit_hit(conn, profile_id, account_id, key, spot, dist, accounts)
    refs = [spot["id"], spot["id"] + ":first"]
    if spot["kind"] == "haunt" and storm_live:
        add_earn(conn, account_id, key, "activity", spot["id"] + ":storm", STORM_BONUS, {"storm": True})
        refs.append(spot["id"] + ":storm")
        credit_profile(conn, profile_id, accounts)
    out = {"result": "treat"}
    if spot["kind"] == "clue":
        spots = spots_for(conn, key)
        done = _hit_refs(conn, profile_id, key)
        nxt = current_clue(spots, done)
        if nxt is None:
            add_earn(conn, account_id, key, "activity", "trail:all", TRAIL_ALL_AWARD, {"trail": "all"})
            refs.append("trail:all")
            credit_profile(conn, profile_id, accounts)
        else:
            out["next_clue"] = {k: v for k, v in nxt.items() if not k.startswith("_")}
    # What THIS hit paid, from its own ledger rows: a balance delta would also
    # count unrelated evidence that happened to credit in the same txn.
    marks = ",".join("?" * len(refs))
    out["award"] = int(conn.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM portal_sn_ledger WHERE profile_id = ? AND night = ? "
        "AND kind = 'earn' AND source IN ('knock', 'activity') AND ref IN (" + marks + ")",
        (profile_id, key, *refs)).fetchone()[0])
    _log_knock(conn, profile_id, account_id, key, "treat", spot["id"], dist, hit=1, now=now)
    return out


def _credit_after(conn, profile_id, accounts):
    before = balance(conn, profile_id)
    credit_profile(conn, profile_id, accounts)
    return balance(conn, profile_id) - before


# --- read models -------------------------------------------------------------

def progress(conn, profile_id, night_key, spots=None):
    spots = spots_for(conn, night_key) if spots is None else spots
    # Public spots only: summing hidden ones would tell how many there are and what they pay.
    activity_cap = sum(s["award"] for s in spots if s["kind"] != "porch" and s["public"] == 1)
    if any(s["kind"] == "clue" for s in spots):
        activity_cap += TRAIL_ALL_AWARD
    if any(s["public"] != 1 for s in spots):
        activity_cap += HIDDEN_CAP_SHOWN
    out = {}
    for source, cap in (("visit", cap_for(night_key, "visit")), ("knock", cap_for(night_key, "knock")),
                        ("landsraad", cap_for(night_key, "landsraad")), ("activity", activity_cap)):
        used = _credited(conn, profile_id, night_key, source)
        out[source] = {"used": used, "cap": max(cap, used) if source == "activity" else cap}
    return out


def calendar(conn, profile_id, now=None):
    rows = dict(conn.execute("SELECT night, SUM(delta) FROM portal_sn_ledger WHERE profile_id = ? "
                             "AND event = ? AND kind IN ('earn', 'admin_adjust') AND delta > 0 "
                             "GROUP BY night", (profile_id, EVENT)).fetchall())
    return [{"n": i + 1, "key": k, "state": calendar_state(k, now), "earned": int(rows.get(k) or 0)}
            for i, k in enumerate(NIGHT_KEYS)]


def ledger_rows(conn, profile_id, limit=50):
    rows = conn.execute("SELECT id, delta, kind, night, source, order_id, reason, created_at "
                        "FROM portal_sn_ledger WHERE profile_id = ? ORDER BY id DESC LIMIT ?",
                        (profile_id, limit)).fetchall()
    out = []
    for r in rows:
        row = {"id": r["id"], "delta": r["delta"], "kind": r["kind"],
               "created_at": r["created_at"].replace(" ", "T") + "Z"}
        if r["kind"] == "earn":
            row.update(source=r["source"], night=r["night"])
        elif r["kind"] in ("order", "refund"):
            row["order_id"] = r["order_id"]
        else:
            row["reason"] = r["reason"]
        out.append(row)
    return out


def read_prefs(conn, identity):
    if not identity or not _has(conn, "portal_prefs"):
        return {}
    row = conn.execute("SELECT prefs FROM portal_prefs WHERE discord_id = ? AND scope = ''",
                       (identity,)).fetchone()
    try:
        doc = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        doc = {}
    return doc if isinstance(doc, dict) else {}


def sietch_prefs(doc):
    return {"flair": doc.get("sietch_flair"), "mapdot": doc.get("sietch_mapdot"),
            "board_hidden": doc.get("sietch_board_hidden") is True}


def board(conn, anon_name="A wanderer", limit=10):
    rows = conn.execute("SELECT profile_id, SUM(delta) AS earned, MAX(id) AS last FROM portal_sn_ledger "
                        "WHERE event = ? AND kind IN ('earn', 'admin_adjust') AND delta > 0 "
                        "GROUP BY profile_id ORDER BY earned DESC, last ASC LIMIT ?",
                        (EVENT, limit)).fetchall()
    out = []
    for rank, r in enumerate(rows, start=1):
        identity, name = profile_identity(conn, r["profile_id"])
        prefs = sietch_prefs(read_prefs(conn, identity))
        shown = anon_name if prefs["board_hidden"] or not name else name
        out.append({"rank": rank, "name": shown, "earned": int(r["earned"]), "flair": prefs["flair"]})
    return out


# --- shop ------------------------------------------------------------------
#
# Two legs, the Karum shape. Leg 1 (place_order) is one BEGIN IMMEDIATE txn here:
# balance, limit, stock and window checked, then the order row and its debit
# written together. Leg 2 is the game writer, reached with the order's
# delivery_key; apply_outcome folds its answer back. A DEFINITE failure refunds
# (one refund row per order, ux_sn_refund); anything ambiguous goes to
# reconciling and is re-driven with the SAME key. There is never a blind refund.

LANES = ("prefs", "role", "wallet", "bank", "spec", "charxp")
GAME_LANES = ("wallet", "bank", "spec", "charxp")
WRITER_KINDS = {"bank": "sn_shop_bank", "wallet": "sn_shop_solari", "spec": "sn_shop_spec",
                "charxp": "sn_shop_charxp"}
SPEC_TRACKS = ("Combat", "Crafting", "Gathering", "Exploration", "Sabotage")
SPEC_XP_CAP = 44182
CHAR_XP_CAP = 344440
# A bank row may stack past 10 only for a template listed here (its in-game stack max).
STACK_MAX = {"MelangeSpice": 500}
WALLET_SOLARI_MAX = 100000
BANK_SOLARI_MAX = 250000
DEFINITE_FAILURES = ("rejected", "not_owner", "cap", "already_owned", "capacity", "bank_unopened")
_DELIVERY_NS = "6f1d3c52-8b0e-4b8a-9a55-5a1e7c9d2b10"
_SKU_RE = re.compile(r"^sn-[a-z0-9-]{1,48}$")
_IDEM_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_PREF_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def denied(template_id, deny):
    t = template_id.lower()
    if t in {x.lower() for x in deny.get("templates") or ()}:
        return True
    if any(t.startswith(p.lower()) for p in deny.get("deny_prefixes") or ()):
        return True
    return any(s.lower() in t for s in deny.get("deny_substrings") or ())


def _row_error(row):
    if not isinstance(row, dict) or not isinstance(row.get("sku"), str) or not _SKU_RE.fullmatch(row["sku"]):
        return "bad sku"
    if row.get("lane") not in LANES:
        return "bad lane"
    for k in ("price", "per_profile_limit"):
        v = row.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < (0 if k == "price" else 1):
            return "bad " + k
    if not isinstance(row.get("name"), str) or not row["name"]:
        return "bad name"
    for k in ("opens_at", "closes_at"):
        try:
            datetime.fromisoformat(str(row.get(k)).replace("Z", "+00:00"))
        except ValueError:
            return "bad " + k
    lane = row["lane"]
    if lane == "bank":
        items = row.get("items")
        if not isinstance(items, list) or not items:
            return "bank row needs items"
        for it in items:
            if not isinstance(it, dict):
                return "bad template_id"
            while it is not None:
                if not isinstance(it, dict) or not _TEMPLATE_RE.fullmatch(str(it.get("template_id"))):
                    return "bad template_id"
                if (it.get("qty") not in range(1, STACK_MAX.get(it.get("template_id"), 10) + 1)
                        or it.get("grade") not in range(0, 6)):
                    return "bad qty or grade"
                skip = it.get("skip_if_unlocked", [])
                if not isinstance(skip, list) or not all(_TEMPLATE_RE.fullmatch(str(u)) for u in skip):
                    return "bad skip_if_unlocked"
                it = it.get("fallback")
    # A wallet row is Solari only; a bank row may carry Solari too, credited in
    # the same writer txn as its items.
    if lane == "wallet" or (lane == "bank" and "solari" in row):
        v = row.get("solari")
        top = WALLET_SOLARI_MAX if lane == "wallet" else BANK_SOLARI_MAX
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= top:
            return "bad solari"
    if lane == "spec":
        v = row.get("spec_xp")
        if row.get("track") not in SPEC_TRACKS:
            return "bad track"
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= SPEC_XP_CAP:
            return "bad spec_xp"
    if lane == "charxp":
        v = row.get("char_xp")
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= CHAR_XP_CAP:
            return "bad char_xp"
    # prefs rows grant a pref_key; a bank row may also grant one, owned once its
    # game leg delivers.
    if (lane == "prefs" or (lane == "bank" and "pref_key" in row)) \
            and not _PREF_RE.fullmatch(str(row.get("pref_key"))):
        return "bad pref_key"
    if lane == "role" and not _PREF_RE.fullmatch(str(row.get("role_key"))):
        return "bad role_key"
    return None


def _item_templates(items):
    """Every template a bank row could mint: each item and its fallback chain."""
    out = []
    for it in items:
        while isinstance(it, dict):
            out.append(str(it.get("template_id")))
            it = it.get("fallback")
    return out


def load_catalog(catalog_path, deny_path, sku_off=()):
    """({sku: row}, [(sku, reason), ...] refused). A row whose template matches the
    deny list, or whose shape is wrong, never loads: refusing it here is what keeps
    an MTX, DLC or Halloween template off the stall even if someone adds one."""
    with open(deny_path, "r", encoding="utf-8") as fh:
        deny = json.load(fh)
    with open(catalog_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    rows, refused = {}, []
    for row in doc.get("rows") or []:
        err = _row_error(row)
        if err is None:
            bad = [t for t in _item_templates(row.get("items") or ()) if denied(t, deny)]
            if bad:
                err = "denied template " + ",".join(bad)
        if err is None and row["sku"] in rows:
            err = "duplicate sku"
        if err:
            refused.append((row.get("sku") if isinstance(row, dict) else None, err))
            continue
        row = dict(row)
        if row["sku"] in sku_off:
            row["disabled"] = True
        rows[row["sku"]] = row
    return rows, refused


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def sku_open(row, now=None):
    now = _utc(now)
    return (not row.get("disabled")) and _parse(row["opens_at"]) <= now < _parse(row["closes_at"])


def _live_count(conn, sku, profile_id=None):
    sql = "SELECT COUNT(*) FROM portal_sn_orders WHERE sku = ? AND status <> 'refunded'"
    args = [sku]
    if profile_id is not None:
        sql += " AND profile_id = ?"
        args.append(profile_id)
    return conn.execute(sql, args).fetchone()[0]


def owned_pref_keys(conn, profile_id, catalog, slot=None):
    skus = [r["sku"] for r in catalog.values() if r.get("pref_key") and slot in (None, r.get("kind"))]
    if not skus:
        return set()
    marks = ",".join("?" * len(skus))
    got = {r[0] for r in conn.execute("SELECT DISTINCT sku FROM portal_sn_orders WHERE profile_id = ? "
                                      "AND status = 'delivered' AND sku IN (" + marks + ")",
                                      (profile_id, *skus))}
    return {catalog[s]["pref_key"] for s in got}


def catalog_view(conn, profile_id, catalog, now=None):
    """Player-safe catalog. A secret row is ABSENT (not hidden, not counted) until
    it opens. Template ids, unlock ids and amounts beyond the display never leave."""
    now = _utc(now)
    out = []
    for row in catalog.values():
        # Not open yet = absent, secret or not (catalog-balance default: no teasers).
        if now < _parse(row["opens_at"]):
            continue
        used = _live_count(conn, row["sku"], profile_id)
        delivered = conn.execute("SELECT 1 FROM portal_sn_orders WHERE profile_id = ? AND sku = ? "
                                 "AND status = 'delivered' LIMIT 1", (profile_id, row["sku"])).fetchone()
        stock = row.get("stock")
        sold_out = stock is not None and _live_count(conn, row["sku"]) >= stock
        view = {"sku": row["sku"], "name": row["name"], "kind": row.get("kind"), "lane": row["lane"],
                "price": row["price"], "limit": row["per_profile_limit"],
                "limit_left": max(0, row["per_profile_limit"] - used), "owned": bool(delivered),
                "disabled": bool(row.get("disabled")) or sold_out or not sku_open(row, now),
                "opens_at": row["opens_at"], "closes_at": row["closes_at"], "secret": bool(row.get("secret")),
                "blurb": row.get("blurb") or "", "applies_to": row.get("applies_to") or []}
        if row.get("pref_key"):
            view["pref_key"] = row["pref_key"]
        if row.get("contains"):
            view["contains"] = list(row["contains"])
        out.append(view)
    return out


def delivery_key(profile_id, sku, order_id):
    import uuid
    return str(uuid.uuid5(uuid.UUID(_DELIVERY_NS), "%s:%s:%d" % (profile_id, sku, order_id)))


def order_row(conn, order_id):
    row = conn.execute("SELECT * FROM portal_sn_orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def place_order(conn, profile_id, sku, idem_key, catalog, now=None, discord_id=None,
                account_id=None, controller_id=None):
    """Leg 1. Returns (order_dict, None) or (None, refusal_token). A replay of the
    same idem_key returns the existing order unchanged, whatever it says now."""
    if not isinstance(idem_key, str) or not _IDEM_RE.fullmatch(idem_key):
        return None, "bad_idem_key"
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute("SELECT * FROM portal_sn_orders WHERE idem_key = ?", (idem_key,)).fetchone()
        if prior is not None:
            conn.rollback()
            if prior["profile_id"] != profile_id:
                return None, "bad_idem_key"
            return dict(prior), None
        err = _order_refusal(conn, profile_id, sku, catalog, now, account_id, controller_id)
        if err:
            conn.rollback()
            return None, err
        row = catalog[sku]
        status = "delivered" if row["lane"] == "prefs" else "queued"
        cur = conn.execute(
            "INSERT INTO portal_sn_orders(event, idem_key, delivery_key, profile_id, discord_id, account_id, "
            "controller_id, sku, lane, price, status, settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (EVENT, idem_key, "pending:" + idem_key, profile_id, discord_id, account_id, controller_id, sku,
             row["lane"], row["price"], status, _now_db(now) if status == "delivered" else None))
        oid = cur.lastrowid
        conn.execute("UPDATE portal_sn_orders SET delivery_key = ? WHERE id = ?",
                     (delivery_key(profile_id, sku, oid), oid))
        if row["price"] > 0:
            conn.execute("INSERT INTO portal_sn_ledger(event, profile_id, delta, kind, order_id, reason) "
                         "VALUES (?, ?, ?, 'order', ?, ?)", (EVENT, profile_id, -row["price"], oid, sku))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return order_row(conn, oid), None


def _order_refusal(conn, profile_id, sku, catalog, now, account_id, controller_id):
    row = catalog.get(sku)
    if row is None or (row.get("secret") and _utc(now) < _parse(row["opens_at"])):
        return "unknown_sku"
    if not sku_open(row, now):
        return "closed"
    if _live_count(conn, sku, profile_id) >= row["per_profile_limit"]:
        return "limit"
    if row.get("stock") is not None and _live_count(conn, sku) >= row["stock"]:
        return "sold_out"
    if row["lane"] in GAME_LANES and not (account_id and controller_id):
        return "no_character"
    if balance(conn, profile_id) < row["price"]:
        return "balance"
    return None


def refund_needs_confirm(order):
    """True when a grant for this order may already have landed, so an operator
    must check the game (or Discord) side before refunding it."""
    if order["status"] in ("delivering", "reconciling"):
        return True
    if order["lane"] == "role":
        return True   # the role sync may hold it mid-call; there is no claim step
    return order["status"] == "queued" and order["attempts"] > 0


def refund(conn, order_id, reason, operator=None, now=None):
    """Write the one refund row and mark the order refunded. Idempotent: a second
    call finds the refund already there. The caller owns the transaction."""
    order = order_row(conn, order_id)
    if order is None:
        return False
    if order["price"] > 0:
        conn.execute("INSERT OR IGNORE INTO portal_sn_ledger(event, profile_id, delta, kind, order_id, "
                     "operator, reason) VALUES (?, ?, ?, 'refund', ?, ?, ?)",
                     (order["event"], order["profile_id"], order["price"], order_id, operator, reason))
    conn.execute("UPDATE portal_sn_orders SET status = 'refunded', settled_at = ?, "
                 "error = COALESCE(error, ?) WHERE id = ?", (_now_db(now), reason, order_id))
    return True


DELIVERING_STALE_S = 900   # longer than any writer call (relay 60 s, sweep 600 s)


def _stale_delivering_cutoff(now):
    return (_utc(now) - timedelta(seconds=DELIVERING_STALE_S)).strftime("%Y-%m-%d %H:%M:%S")


def begin_delivery(conn, order_id, now=None):
    """Claim a game-lane order for ONE writer call, in its own short txn, before
    the call goes out: 'delivering' tells the refund route a grant may be landing.
    A 'delivering' order older than DELIVERING_STALE_S was orphaned (a crash
    between the claim and the answer) and may be claimed again with the same key.
    Returns the order to send, or None when it is not ours to send."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = order_row(conn, order_id)
        if order is None or order["lane"] not in GAME_LANES:
            conn.rollback()
            return None
        orphan = (order["status"] == "delivering"
                  and (order.get("attempt_at") or "") < _stale_delivering_cutoff(now))
        if order["status"] not in ("queued", "reconciling") and not orphan:
            conn.rollback()
            return None
        conn.execute("UPDATE portal_sn_orders SET status = 'delivering', attempts = attempts + 1, "
                     "attempt_at = ? WHERE id = ?", (_now_db(now), order_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return order_row(conn, order_id)


def apply_outcome(conn, order_id, outcome, now=None):
    """Leg 2's answer. `outcome` is the writer JSON, or None when the call timed
    out or 5xx'd. Returns the new status. Only a definite error code refunds;
    a writer reply we do not recognise is treated as ambiguous, never as failure."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = order_row(conn, order_id)
        status = outcome.get("status") if isinstance(outcome, dict) else None
        echo_ok = (isinstance(outcome, dict) and order is not None
                   and outcome.get("delivery_key") == order["delivery_key"]
                   and outcome.get("sku") == order["sku"]
                   and outcome.get("controller_id") == order["controller_id"])
        if order is None or order["status"] in ("delivered", "refunded"):
            conn.rollback()
            if order is not None and order["status"] == "refunded" and status in ("applied", "replay") and echo_ok:
                log.warning("ALERT sietch shop: order %s (%s, profile %s) was refunded but the writer "
                            "delivered it: the player has the item AND the Tokens back",
                            order_id, order["sku"], order["profile_id"])
            return order["status"] if order else None
        if status in ("applied", "replay") and echo_ok:
            conn.execute("UPDATE portal_sn_orders SET status = 'delivered', settled_at = ?, lane_ref = ?, "
                         "error = NULL WHERE id = ?",
                         (_now_db(now), str(outcome.get("audit_id") or ""), order_id))
            new = "delivered"
        elif status == "deferred":
            conn.execute("UPDATE portal_sn_orders SET status = 'queued', error = 'deferred' WHERE id = ?",
                         (order_id,))
            new = "queued"
        elif status == "failed" and outcome.get("error") in DEFINITE_FAILURES:
            conn.execute("UPDATE portal_sn_orders SET status = 'failed', error = ? WHERE id = ?",
                         (outcome["error"], order_id))
            refund(conn, order_id, outcome["error"], "writer", now)
            new = "refunded"
        else:
            # Timeout, 5xx, an unknown error code, or a success that did not echo
            # this order's key: the grant may have committed. Re-drive the same key.
            if status in ("applied", "replay"):
                why = "no_echo"
            elif isinstance(outcome, dict):
                why = outcome.get("error") or status or "bad_reply"
            else:
                why = "no_response"
            conn.execute("UPDATE portal_sn_orders SET status = 'reconciling', error = ? WHERE id = ?",
                         (str(why)[:64], order_id))
            new = "reconciling"
        conn.commit()
        return new
    except Exception:
        conn.rollback()
        raise


def writer_payload(order, operator="sietch-shop"):
    """The ONLY fields leg 2 sends. No template, quantity, grade or amount: the
    writer looks the SKU up in its own embedded allowlist, so a buggy or hostile
    portal can at most ask for a known SKU."""
    out = {"reward_kind": WRITER_KINDS[order["lane"]],
           "sku": order["sku"], "account_id": int(order["account_id"]),
           "controller_id": int(order["controller_id"]), "idempotency_key": order["delivery_key"],
           "profile_id": order["profile_id"], "operator": operator, "mode": "apply"}
    if isinstance(order.get("discord_id"), str) and re.fullmatch(r"[1-9][0-9]{0,19}", order["discord_id"]):
        out["discord_id"] = order["discord_id"]
    return out


def due_deliveries(conn, limit=20, now=None):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM portal_sn_orders WHERE lane IN ('bank', 'wallet', 'spec', 'charxp') "
        "AND (status IN ('queued', 'reconciling') OR (status = 'delivering' AND attempt_at < ?)) "
        "ORDER BY id LIMIT ?", (_stale_delivering_cutoff(now), limit))]


def stale_reconciling(conn, hours=24, now=None):
    cutoff = (_utc(now) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return [dict(r) for r in conn.execute(
        "SELECT id, sku, profile_id, created_at, attempts, error FROM portal_sn_orders "
        "WHERE status = 'reconciling' AND created_at < ? ORDER BY id", (cutoff,))]


def ack_role(conn, order_id, ok, error=None, now=None):
    """The Cielago role sync's answer: a 204 delivers, a definite 4xx refunds."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = order_row(conn, order_id)
        if order is None or order["lane"] != "role" or order["status"] not in ("queued", "reconciling"):
            conn.rollback()
            if order is not None and order["status"] == "refunded" and ok:
                log.warning("ALERT sietch shop: role order %s was refunded but Discord granted the role",
                            order_id)
            return order["status"] if order else None
        if ok:
            conn.execute("UPDATE portal_sn_orders SET status = 'delivered', settled_at = ? WHERE id = ?",
                         (_now_db(now), order_id))
            new = "delivered"
        else:
            conn.execute("UPDATE portal_sn_orders SET status = 'failed', error = ? WHERE id = ?",
                         (str(error or "role_refused")[:64], order_id))
            refund(conn, order_id, str(error or "role_refused")[:64], "role-sync", now)
            new = "refunded"
        conn.commit()
        return new
    except Exception:
        conn.rollback()
        raise


def orders_view(conn, profile_id, catalog, limit=50):
    rows = conn.execute("SELECT id, sku, lane, price, status, created_at, settled_at FROM portal_sn_orders "
                        "WHERE profile_id = ? ORDER BY id DESC LIMIT ?", (profile_id, limit)).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "sku": r["sku"], "name": (catalog.get(r["sku"]) or {}).get("name", r["sku"]),
                    "lane": r["lane"], "price": r["price"],
                    # reconciling is an internal state; to a player it is still on its way.
                    "status": "delivering" if r["status"] == "reconciling" else r["status"],
                    "created_at": r["created_at"].replace(" ", "T") + "Z"})
    return out
