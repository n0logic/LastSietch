"""Login-rewards V2 logic (Phase 1): streak math, weekly rotation, reward pool
metadata, and the <web-host> read-side claim mirror.

Split from the router so the pure functions (streak, ramp, rotation, period keys,
deterministic idempotency) are unit-testable with ZERO web/game-DB dependencies.
The game host (dune.ls_reward_claims) is the source of truth for idempotency and
the grant itself; the local admin.db ls_reward_claims table this module writes is
only a render cache for streak / calendar claim-state.

Keying: PER-ACCOUNT (session aid), never per-character. Daily bucket = UTC
calendar day (date_utc). Weekly bucket = ISO week (Monday-aligned). Streak = the
run of consecutive date_utc ending today.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

# --- tunables (admin-editable defaults; the game-host writer holds the hard caps) --

# Aggressive daily Solari ramp 10k -> 25k over days 1-7 (owner-locked 2026-07-15);
# resets to day 1 on a missed day. Index 0 = day 1. The ramp REPEATS in 7-day cycles
# (owner-chosen 2026-07-16): reaching day 7 grants the weekly weapon, then the next
# login is cycle-day 1 again (10k) and the ramp climbs anew. The total streak keeps
# counting for the flame; the ramp position is the cycle day. See cycle_day().
DAILY_SOLARI_RAMP = [10000, 12000, 15000, 18000, 20000, 22000, 25000]
DAILY_CYCLE_LEN = len(DAILY_SOLARI_RAMP)  # 7-day repeating ramp cycle

# Milestone days get a brighter bracket in the UI (design section 7.3).
DAILY_MILESTONE_DAYS = (3, 7)

# Weekly (day-7 milestone) = one rotating T6 grade-3 weapon, minted to the CHOAM
# bank via G29 (owner-locked 2026-07-15). quality_level 3 == "grade 3". The pool is
# admin-editable; rotation picks pool[iso_week_index % len(pool)]. Order is the
# owner-approved 12-week starter schedule; all are tier-6 named weapons verified in
# admin-backend/data/dune-give-item-catalog.json. T6_Augment_* are EXCLUDED (those
# feed the Phase-2 monthly augment pool, not the weekly weapon).
WEEKLY_QUALITY_LEVEL = 3
WEEKLY_ROTATION_POOL = [
    "UniqueScattergun5",                 # Perforator (shotgun)
    "LongRifle_Unique_LargeMag_06",      # Regis Tripleshot Repeating Rifle
    "LMG_Unique_Power_06",               # Plasma Cannon
    "Kindjal_Unique_Blood_06",           # Feyd's Drinker
    "UniqueSda6",                        # Way of the Misr (pistol)
    "RocketLauncher_Unique_Homing_06",   # The Ancient Way
    "SMG_Unique_LargeMag_06",            # A Dart for Every Man
    "HeavyPistol_Unique_Headshot_06",    # Seethe
    "UniqueSword_05",                    # Replica Pulse-sword
    "UniqueAr4",                         # Salusan Vengeance (battle rifle)
    "UniqueFlameThrower_02",             # Vaporizer
    "Shotgun_Unique_Explosive_06",       # Regis Burst Drillshot
]

# The weekly weapon unlocks at the day-7 streak milestone.
WEEKLY_STREAK_REQUIREMENT = 7

# Daily streak milestones (drive the "next_milestone" gauge). The ramp tops at 7.
STREAK_MILESTONES = (3, 7)

# Catalog / gear-stats rarity strings -> the frontend's 5-tier scale
# (common|uncommon|rare|epic|legendary). gear-stats uses Common/Unique/Memento;
# the give-item catalog uses common/rare/Unique. Unique == a named premium item
# -> epic ring; Memento == the rarest -> legendary.
_RARITY_5SCALE = {
    "common": "common",
    "uncommon": "uncommon",
    "rare": "rare",
    "unique": "epic",
    "epic": "epic",
    "memento": "legendary",
    "legendary": "legendary",
    "exotic": "legendary",
}

# Catalog `cat` (weapon subtype / armor slot / tool) -> the frontend item type,
# used only when gear-stats has no explicit type. Anything unmatched is a weapon
# (the weekly rotation pool is all weapons).
_ARMOR_CATS = {"head", "chest", "hands", "feet", "legs", "back", "heavyarmor",
               "lightarmor", "garment", "stillsuit"}
_TOOL_CATS = {"tool", "cutteray", "scanner", "powerpack", "utility"}

# Fixed namespace for deterministic idempotency keys. A claim's key is derived from
# (account_id, reward_kind, period_key), so a retry / double-click / concurrent
# submit all produce the SAME uuid -> the game-host UNIQUE(idempotency_key) collapses
# them to a single grant (replay), never a double grant. This is the real double-claim
# guard; the local composite UNIQUE index is just a cheap pre-check.
_REWARD_NS = uuid.UUID("a1f0c3d2-7b64-4e59-9c2a-1d5e6f7a8b90")

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CATALOG_PATH = os.path.join(_DATA_DIR, "dune-give-item-catalog.json")
_NON_TRADEABLE_PATH = os.path.join(_DATA_DIR, "dune-item-non-tradeable.json")


# --- time / period keys ------------------------------------------------------ #

def utc_today(now: Optional[datetime] = None) -> date:
    """Today's UTC calendar date. The daily boundary is UTC midnight (matches the
    login_days recorder's date(ts,'unixepoch'))."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date()


def date_key(d: date) -> str:
    """UTC calendar-day bucket, matching portal_login_days.date_utc."""
    return d.strftime("%Y-%m-%d")


def week_start(d: date) -> date:
    """Monday of d's ISO week."""
    return d - timedelta(days=d.weekday())


def iso_week_key(d: date) -> str:
    """Weekly claim bucket, 'yyyy-Www' (ISO year + week)."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_index(d: date) -> int:
    """Monday-aligned monotonic week counter (advances by exactly 1 each ISO week).
    Used to index the weekly rotation pool so the year boundary never double-picks
    or stalls a pool position."""
    return week_start(d).toordinal() // 7


def next_utc_midnight(now: Optional[datetime] = None) -> datetime:
    """The next daily reset instant (UTC). The frontend counts down to this."""
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    tomorrow = now.date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)


def next_week_start(now: Optional[datetime] = None) -> datetime:
    """The next weekly-rotation instant: 00:00 UTC on the coming Monday (the ISO week
    boundary weekly_template_for rotates on). On a Monday this is the FOLLOWING Monday,
    a full week out (the current week's weapon is still active today)."""
    now = now or datetime.now(timezone.utc)
    d = now.astimezone(timezone.utc).date()
    ahead = (7 - d.weekday()) % 7 or 7  # Monday=0; always jump to the next Monday
    nxt = d + timedelta(days=ahead)
    return datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc)


# --- daily ramp + streak ------------------------------------------------------ #

def cycle_day(day_number: int) -> int:
    """Map a 1-based total streak day to its position in the repeating 7-day cycle
    (1..DAILY_CYCLE_LEN). Day 8 -> 1, day 14 -> 7, etc. Days < 1 clamp to cycle-day 1."""
    if day_number < 1:
        return 1
    return ((day_number - 1) % DAILY_CYCLE_LEN) + 1


def daily_amount(day_number: int) -> int:
    """Solari for a given streak day (1-based). The ramp repeats every DAILY_CYCLE_LEN
    days, so the amount is the cycle-day rung (day 8 == day 1 == 10k, not a clamp)."""
    return DAILY_SOLARI_RAMP[cycle_day(day_number) - 1]


def _longest_run(days: set) -> int:
    """Longest run of consecutive calendar dates in the set."""
    if not days:
        return 0
    parsed = sorted(datetime.strptime(s, "%Y-%m-%d").date() for s in days)
    best = run = 1
    for prev, cur in zip(parsed, parsed[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        best = max(best, run)
    return best


def run_ending(login_dates: Iterable[str], d: date) -> int:
    """Length of the consecutive login run ENDING at d (inclusive); 0 if d has no
    login. This is the ramp day-number that date would have earned — used to label
    each calendar cell."""
    s = {x for x in login_dates if x}
    if date_key(d) not in s:
        return 0
    n = 0
    one = timedelta(days=1)
    cur = d
    while date_key(cur) in s:
        n += 1
        cur -= one
    return n


def compute_streak(login_dates: Iterable[str], today: date) -> dict:
    """Streak state from a set of 'YYYY-MM-DD' login dates.

    today_day_number = (run ending yesterday) + 1 — what a claim TODAY counts as,
    robust to the 300s recorder lag not yet having logged today. current is the
    displayed streak (includes today only once today's login row lands). Reset to
    day 1 happens implicitly: a gap breaks the run, so the next day is day 1."""
    s = {d for d in login_dates if d}
    one = timedelta(days=1)

    prev = 0
    d = today - one
    while date_key(d) in s:
        prev += 1
        d -= one

    logged_today = date_key(today) in s
    today_day_number = prev + 1
    current = today_day_number if logged_today else prev
    best = _longest_run(s)
    return {
        "current": current,
        "best": max(best, current),
        "logged_today": logged_today,
        "today_day_number": today_day_number,
        # ramp/cycle positions (repeating 7-day cycle): today's claim rung, and the
        # current streak's position for the milestone gauge.
        "today_cycle_day": cycle_day(today_day_number),
        "cycle_day": cycle_day(current) if current else 0,
    }


def _cycle_window(today: date, cycle_day_today: int):
    """(cycle_day p, date) for each day of the current cycle so far: p=1 is the
    cycle's first day, p=cycle_day_today is today. `today - (C - p)` days back."""
    C = cycle_day_today
    return [(p, today - timedelta(days=C - p)) for p in range(1, C + 1)]


def claim_pool(login_dates, daily_claimed, today: date):
    """Accumulate model: the unclaimed logged days in the CURRENT 7-day cycle that
    the next claim will grant, and their summed Solari. Bounded to one ramp cycle
    (<= sum(DAILY_SOLARI_RAMP) == 122k) so a single claim never exceeds the writer
    ceiling; unclaimed days from an earlier cycle are not carried (claim within the
    week). Returns (total_solari, [(date_key, amount), ...] oldest-first). Days must
    be BOTH logged and unclaimed to count."""
    s = {d for d in login_dates if d}
    claimed = set(daily_claimed or ())
    C = compute_streak(login_dates, today)["today_cycle_day"]
    total, entries = 0, []
    for p, day in _cycle_window(today, C):
        k = date_key(day)
        if k in s and k not in claimed:
            amt = DAILY_SOLARI_RAMP[p - 1]
            total += amt
            entries.append((k, amt))
    return total, entries


def cycle_cells(login_dates, daily_claimed, today: date):
    """The current 7-day ramp cycle as 7 grid cells (cycle_day 1..7). state in
    {claimed, claimable, pending, upcoming}: a past/today cycle day you logged in and
    have not claimed is 'claimable' (it is in the accumulate pool); today before your
    login row lands is 'pending'; days past today's position are 'upcoming'."""
    s = {d for d in login_dates if d}
    claimed = set(daily_claimed or ())
    st = compute_streak(login_dates, today)
    C = st["today_cycle_day"]
    logged_today = st["logged_today"]
    cells = []
    for p in range(1, DAILY_CYCLE_LEN + 1):
        day = today - timedelta(days=C - p)
        k = date_key(day)
        if k in claimed:
            state = "claimed"
        elif p > C:
            state = "upcoming"
        elif p == C and not logged_today:
            state = "pending"
        elif k in s:
            state = "claimable"
        else:
            state = "upcoming"
        cells.append({
            "cycle_day": p,
            "amount": DAILY_SOLARI_RAMP[p - 1],
            "state": state,
            "is_today": p == C,
            "milestone": p in DAILY_MILESTONE_DAYS,
        })
    return cells


# --- weekly rotation ---------------------------------------------------------- #

def weekly_template_for(d: date) -> str:
    """The rotating weekly weapon template_id for d's ISO week."""
    return WEEKLY_ROTATION_POOL[week_index(d) % len(WEEKLY_ROTATION_POOL)]


# --- monthly reward (Phase 2) -------------------------------------------------

# Monthly Reward = one pre-augmented weapon granted per (account, 28-day
# period -- see monthly_period_start/end below, NOT a calendar month), minted
# straight to the CHOAM bank (owner-locked 2026-07-15 section 10.3: augments
# only, delivered pre-augmented rather than applied live, since
# dune-augment.py's apply path is offline-only + requires the item in the
# CHARACTER inventory, which conflicts with online bank delivery -- see
# LOGIN-REWARDS-BUILD-CONTRACT section 6). "Monthly Reward" is the player-facing
# name (owner-locked 2026-08-01, matching "Daily Rewards"/"Weekly Reward" --
# "draw" implied a lottery, and with a real earning requirement it is not one).
# The wire value stays `monthly_augment` everywhere (relay, writer, ledger,
# tests); only copy/comments changed.
#
# quality_level 5 == grade 5 (the augment TIER, not the roll value -- rolls are
# randomised by the writer, see dune-reward-op.sh do_monthly_augment; a
# guaranteed grade-5 perfect roll every month would remove rolls as something
# to chase, per the owner). This matches the live proof-of-life mint's grade
# (item 1623019481, 2026-08-01: LMG_Unique_Power_06 quality 5, 3 grade-5
# augments).
#
# Every (template_id, augment) pair below is compatibility-checked against
# scripts/data/augment-compatibility.json's documented matchRule (compatible when
# any ITEM tag startswith any AUGMENT tag) in scripts/tests/test_rewards.py; a pool
# entry that fails that check cannot ship.
#
# `rolls` is the augment's real StatRolls array LENGTH (a count, not a value --
# the writer randomises the values, this only fixes how many slots exist). It is
# NOT derivable from the catalogue's text effects (roll count varies per augment
# and is unrelated to the number of displayed effect lines, e.g.
# T6_Augment_Lmg1 has 6 rolls but 1 effect line) -- only augments with a roll
# count VERIFIED against a real live item are used here, because a wrong count
# renders an empty augment slot in game. The verified counts (77 augments,
# measured live 2026-08-01, range 1..7) live in scripts/data/augment-roll-counts.json;
# `rolls` below is a literal copy for readability, kept honest by
# test_monthly_pool_roll_counts_match_verified_file, which fails if a literal
# ever drifts from that file OR names an augment the file has no count for.
# Melee weapons ARE included now that melee-capable augments have verified
# counts (9 of the 77); they were excluded before that data existed.
MONTHLY_AUGMENT_QUALITY_LEVEL = 5

# Unlock gate (owner-locked 2026-08-01): the account must have logged in on this
# many DISTINCT UTC calendar days within the current month before the monthly
# reward is claimable. Calendar-month boundary, not a rolling 30-day window (see
# monthly_login_days_count / monthly_unlocked below).
MONTHLY_LOGIN_DAYS_REQUIRED = 15

MONTHLY_POOL = [
    {
        # Reproduces the live proof-of-life mint exactly (item 1623019481,
        # 2026-08-01): LMG_Unique_Power_06 quality 5, these 3 augments.
        "template_id": "LMG_Unique_Power_06",              # Plasma Cannon
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Lmg1", "grade": 5, "rolls": 6,
             "label": "VULCAN GAU-92 Expander"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "UniqueScattergun5",                # Perforator
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            # rolls=4 per the verified file (2026-08-01 measurement); an
            # earlier informal count of 5 for this augment was wrong and would
            # have shipped an empty slot. This is exactly the drift the
            # roll-counts file + its ship-gate test exist to catch.
            {"name": "T6_Augment_Ch5_Scattergun1", "grade": 5, "rolls": 4,
             "label": "Scattergun Rampage-Enhancement"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "LongRifle_Unique_LargeMag_06",     # Regis Tripleshot Repeating Rifle
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_Spitdart1", "grade": 5, "rolls": 3,
             "label": "JABAL Spitdart Ranger"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "SMG_Unique_LargeMag_06",           # A Dart for Every Man
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_SMG1", "grade": 5, "rolls": 5,
             "label": "Disruptor M11 Personnel-Buster"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "RocketLauncher_Unique_Homing_06",  # The Ancient Way
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_RocketLauncher1", "grade": 5, "rolls": 3,
             "label": "Missile Launcher Fragmenter"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "HeavyPistol_Unique_Headshot_06",   # Seethe
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_Heavypistol1", "grade": 5, "rolls": 4,
             "label": "Rafiq Snubnose Marksman"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "UniqueSda6",                       # Way of the Misr (pistol)
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_Maulapistol1", "grade": 5, "rolls": 5,
             "label": "Maula Pistol Antipersonnel Rounds"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "UniqueAr4",                        # Salusan Vengeance (battle rifle)
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_BR1", "grade": 5, "rolls": 3,
             "label": "Karpov 38 Sniper Barrel"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "Shotgun_Unique_Explosive_06",      # Regis Burst Drillshot
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_Shotgun1", "grade": 5, "rolls": 6,
             "label": "Drillshot FK7 Spray-and-Pray"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        "template_id": "UniqueFlameThrower_02",            # Vaporizer
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Flamethrower1", "grade": 5, "rolls": 3,
             "label": "Flamethrower Amplifier"},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1,
             "label": "Heavy Caliber Upgrade"},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1,
             "label": "Precision Barrel Adjuster"},
        ],
    },
    {
        # Melee (knife). All melee-tagged augments are generic to any melee
        # weapon (no per-weapon-family split like the ranged augments above),
        # so this picks 3 varied, all verified.
        "template_id": "Kindjal_Unique_Blood_06",          # Feyd's Drinker
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_Melee3", "grade": 5, "rolls": 3,
             "label": "Blade Blood Grooves"},
            {"name": "T6_Augment_Melee8", "grade": 5, "rolls": 3,
             "label": "Blade Optimizer"},
            {"name": "T6_Augment_Melee1", "grade": 5, "rolls": 1,
             "label": "Blade Sharpener"},
        ],
    },
    {
        "template_id": "UniqueSword_05",                   # Replica Pulse-sword
        "quality_level": MONTHLY_AUGMENT_QUALITY_LEVEL,
        "augments": [
            {"name": "T6_Augment_Ch5_Melee1", "grade": 5, "rolls": 2,
             "label": "Heavy Metal Blade Coating"},
            {"name": "T6_Augment_Melee9", "grade": 5, "rolls": 2,
             "label": "Edge Optimizer"},
            {"name": "T6_Augment_Melee2", "grade": 5, "rolls": 1,
             "label": "Blade Grip Adjuster"},
        ],
    },
]


# Fixed 28-day period (owner-locked 2026-08-01), NOT a calendar month. A
# calendar month makes the requirement UNFAIR across months: 15 of 28 days is
# 53.6% of February but only 48.4% of March -- February would silently be the
# hardest month of the year to qualify in. A fixed 28-day period makes every
# period identical, forever. (This superseded an earlier calendar-month design
# in the same session; nothing had shipped live, so there was nothing to migrate.)
#
# The anchor is a MONDAY (2026-07-13, the Monday on/before the 2026-07-17
# rewards reveal) and the length is exactly 4 ISO weeks. Both are load-bearing:
# Monday alignment means every period boundary is also a Monday, so the
# rewards page's 4x7 calendar grid rows are REAL weeks, and they stay lined up
# with the weekly weapon's own Monday 00:00 UTC rotation, forever. A
# non-Monday anchor would desync those two clocks permanently.
MONTHLY_PERIOD_ANCHOR = date(2026, 7, 13)
MONTHLY_PERIOD_LENGTH_DAYS = 28

# Claim-grace (owner-approved 2026-08-10): hitting the 15-day threshold and then
# letting the period roll over used to void the earned reward with no recourse. For
# the first MONTHLY_CLAIM_GRACE_DAYS days of a period, an account that met the
# threshold in the PRIOR period but never claimed it can still collect it -- the
# grant is recorded against the PRIOR period key, so (cap being period-scoped,
# BUG-022) it neither consumes nor is blocked by the current period's own claim.
# 7 days == the first Monday-aligned week of the 28-day period.
MONTHLY_CLAIM_GRACE_DAYS = 7


def monthly_period_start(d: date) -> date:
    """Start date (always a Monday) of the fixed 28-day period containing d."""
    offset = (d - MONTHLY_PERIOD_ANCHOR).days
    index = offset // MONTHLY_PERIOD_LENGTH_DAYS   # floor division: correct for d before the anchor too
    return MONTHLY_PERIOD_ANCHOR + timedelta(days=index * MONTHLY_PERIOD_LENGTH_DAYS)


def monthly_period_end(d: date) -> date:
    """Last day (inclusive) of the fixed 28-day period containing d."""
    return monthly_period_start(d) + timedelta(days=MONTHLY_PERIOD_LENGTH_DAYS - 1)


def monthly_period_key(d: date) -> str:
    """Claim bucket = the period's own START DATE, 'yyyy-mm-dd'. Unambiguous,
    lexically sortable, human-readable, and states its own window -- not an
    opaque index."""
    return monthly_period_start(d).strftime("%Y-%m-%d")


def next_monthly_period_start(now: Optional[datetime] = None) -> datetime:
    """The next period's reset instant: 00:00 UTC on the first day after the
    current period ends (always a Monday)."""
    now = now or datetime.now(timezone.utc)
    d = now.astimezone(timezone.utc).date()
    nxt = monthly_period_start(d) + timedelta(days=MONTHLY_PERIOD_LENGTH_DAYS)
    return datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc)


def monthly_prior_period_key(d: date) -> str:
    """Period key of the period immediately before the one containing d. (The very
    first period's `prior` predates the rewards system, so nothing was ever earned
    in it -- the grace check below naturally finds it unclaimed-and-unearned.)"""
    prev = monthly_period_start(d) - timedelta(days=1)
    return monthly_period_key(prev)


def monthly_in_grace_window(d: date) -> bool:
    """True while d falls in the first MONTHLY_CLAIM_GRACE_DAYS days of its period
    (day index 0..GRACE-1), i.e. the window during which the PRIOR period's
    still-unclaimed reward remains collectible."""
    return (d - monthly_period_start(d)).days < MONTHLY_CLAIM_GRACE_DAYS


def monthly_grace_deadline(d: date) -> datetime:
    """00:00 UTC at which the current period's claim-grace closes -- the start of
    the period plus MONTHLY_CLAIM_GRACE_DAYS days (always a Monday-aligned instant
    GRACE days into the period)."""
    end = monthly_period_start(d) + timedelta(days=MONTHLY_CLAIM_GRACE_DAYS)
    return datetime(end.year, end.month, end.day, tzinfo=timezone.utc)


def monthly_login_days_count(login_dates: Iterable[str], period_key: str) -> int:
    """Count of DISTINCT UTC login dates within the fixed 28-day period whose
    key is `period_key` (the period's own start date, 'yyyy-mm-dd'). A 28-day
    window does not align with any string prefix (it can straddle a calendar
    month boundary), so dates are parsed and range-compared, not prefix-matched.
    NOT a streak: unlike the daily ramp (run_ending / compute_streak), a missed
    day never resets this count -- the monthly reward rewards total engagement
    across the period, not a consecutive run. This is the progress metric the
    UI shows as 'N of MONTHLY_LOGIN_DAYS_REQUIRED'."""
    start = datetime.strptime(period_key, "%Y-%m-%d").date()
    end = start + timedelta(days=MONTHLY_PERIOD_LENGTH_DAYS - 1)
    days = set()
    for raw in login_dates:
        if not raw:
            continue
        try:
            d = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except ValueError:
            continue
        if start <= d <= end:
            days.add(d)
    return len(days)


def monthly_unlocked(login_dates: Iterable[str], period_key: str) -> bool:
    """True once the account has logged in on >= MONTHLY_LOGIN_DAYS_REQUIRED
    distinct UTC days within the given 28-day period."""
    return monthly_login_days_count(login_dates, period_key) >= MONTHLY_LOGIN_DAYS_REQUIRED


def monthly_claim_state(login_dates: Iterable[str], claimed_keys, today: date) -> dict:
    """Single source of truth for the monthly reward's claim state, INCLUDING the
    claim-grace window, so the overview card and the claim endpoint can never
    disagree on what is claimable. `claimed_keys` is the set of already-claimed
    monthly period keys for this account (claimed_period_keys(acct,
    'monthly_augment')).

    `claim_key` is the period the NEXT claim would be recorded under, or None when
    nothing is claimable: the CURRENT period when it is itself unlocked+unclaimed,
    otherwise the PRIOR period while grace is open (met last period, never claimed).
    Current takes precedence, though the two are mutually exclusive in practice --
    the current period cannot be unlocked (needs 15 days) inside the 7-day grace
    window. `grace_deadline` is a datetime only while grace is actually active."""
    login_dates = list(login_dates or ())
    claimed_keys = set(claimed_keys or ())
    cur_key = monthly_period_key(today)
    prior_key = monthly_prior_period_key(today)

    cur_progress = monthly_login_days_count(login_dates, cur_key)
    cur_unlocked = cur_progress >= MONTHLY_LOGIN_DAYS_REQUIRED
    cur_claimed = cur_key in claimed_keys

    prior_progress = monthly_login_days_count(login_dates, prior_key)
    prior_unlocked = prior_progress >= MONTHLY_LOGIN_DAYS_REQUIRED
    prior_claimed = prior_key in claimed_keys

    in_grace = monthly_in_grace_window(today)
    grace_active = in_grace and prior_unlocked and not prior_claimed

    if cur_unlocked and not cur_claimed:
        claim_key = cur_key
    elif grace_active:
        claim_key = prior_key
    else:
        claim_key = None

    return {
        "current_key": cur_key,
        "prior_key": prior_key,
        "current_progress": cur_progress,
        "current_unlocked": cur_unlocked,
        "current_claimed": cur_claimed,
        "current_period_end": monthly_period_end(today).strftime("%Y-%m-%d"),
        "prior_progress": prior_progress,
        "prior_unlocked": prior_unlocked,
        "prior_claimed": prior_claimed,
        "prior_period_end": (monthly_period_start(today) - timedelta(days=1))
                            .strftime("%Y-%m-%d"),
        "in_grace": in_grace,
        "grace_active": grace_active,
        "claim_key": claim_key,
        "claimable": claim_key is not None,
        "grace_deadline": monthly_grace_deadline(today) if grace_active else None,
    }


def period_cells(login_dates, daily_claimed, today: date) -> list:
    """The CURRENT 28-day period (monthly_period_start..end -- the single
    source of truth, never recomputed here) as 28 REAL dated grid cells,
    Monday-aligned by construction: index 0 is the period's own Monday start,
    index // 7 is the week row, index % 7 is the weekday column. This is the
    date-ABSOLUTE calendar (owner-locked 2026-08-01, replacing the old
    streak-relative grid that had "no real dates" and fabricated 3 identical
    upcoming rows client-side): the monthly reward's 15-of-28 requirement is
    now directly countable on it, not just carried as a separate number.
    Distinct from cycle_cells (the 7-cell RAMP-relative gauge for the daily
    Solari claim button) -- both stay in service; the frontend migration off
    the old grid is separate work.

    Per cell: date, logged, claimed, is_today, is_future, and -- ONLY for a
    PAST OR TODAY day the player actually logged in -- amount + ramp_day (the
    CYCLE-WRAPPED 1..DAILY_CYCLE_LEN ramp rung that date earned: run_ending
    gives the raw consecutive-day count, cycle_day wraps it same as
    daily_amount does internally). A future day's amount depends on a streak
    that has not happened yet, so it is never guessed: amount/ramp_day stay
    None for it. `milestone` is `ramp_day in DAILY_MILESTONE_DAYS` -- ramp_day
    is already cycle-wrapped, so e.g. a raw 10-day streak (not itself a
    milestone number) still flags correctly at its wrapped rung 3. NEVER flag
    by calendar column: DAILY_MILESTONE_DAYS are ramp positions, not weekdays.

    No streak semantics leak in here: a missed day just yields logged=False
    for that one cell and never affects any OTHER cell. The period's total
    logged-day count (sum of `logged` across all 28 cells) always equals
    monthly_login_days_count() for the same inputs -- both are a plain
    distinct-day count over the identical [start, end] window, never a
    consecutive run."""
    s = {d for d in login_dates if d}
    claimed = set(daily_claimed or ())
    start = monthly_period_start(today)
    cells = []
    for idx in range(MONTHLY_PERIOD_LENGTH_DAYS):
        day = start + timedelta(days=idx)
        k = date_key(day)
        logged = k in s
        is_future = day > today
        amount = None
        ramp_day = None
        milestone = False
        if not is_future and logged:
            raw = run_ending(login_dates, day)
            ramp_day = cycle_day(raw)
            amount = daily_amount(raw)
            milestone = ramp_day in DAILY_MILESTONE_DAYS
        cells.append({
            "date": k,
            "logged": logged,
            "claimed": k in claimed,
            "is_today": day == today,
            "is_future": is_future,
            "amount": amount,
            "ramp_day": ramp_day,
            "milestone": milestone,
        })
    return cells


def monthly_period_index(d: date) -> int:
    """Monotonic 28-day period counter, advancing by exactly 1 per period.
    The monthly analogue of week_index(), and the index into MONTHLY_POOL."""
    return (d - MONTHLY_PERIOD_ANCHOR).days // MONTHLY_PERIOD_LENGTH_DAYS


def monthly_pool_entry_for(account_id: int, period_key: str) -> dict:
    """The monthly reward for the period named by `period_key`, PINNED TO THAT
    PERIOD (owner decision 2026-08-24, superseding the 2026-08-10 "mirror the
    current weekly weapon" rule -- that rule is dead, do not restore it).

    Two things were wrong with mirroring the weekly:

    1. It read utc_today() and IGNORED the period_key it was handed, so the
       weapon changed under players mid-period. Worse than cosmetic: during a
       grace claim monthly_claim_state passes the PRIOR period's key, so a late
       claimant was minted whatever weapon happened to be showing on the day
       they clicked rather than the one they earned.
    2. Its stated benefit ("weekly and monthly always agree") is unobtainable
       under any pinned scheme -- a 28-day period spans four weekly rotations,
       so the two necessarily disagree for at least three of every four weeks.

    Indexed by PERIOD NUMBER, not by the weekly template. Deriving the monthly
    from weekly_template_for(period_start) looks equivalent and is not: the
    period is exactly 4 weeks and the weekly pool has 12 entries, so
    gcd(4, 12) = 4 and the period start only ever lands on 12/4 = 3 distinct
    pool positions. That would have made 9 of the 12 weapons permanently
    unreachable as a monthly reward. Indexing by period number instead walks
    the whole pool, one step per period.

    Same for every account: account_id is retained for signature and
    idempotency-caller stability but does not vary the weapon. Raises on a
    malformed period_key rather than silently substituting a default -- every
    caller sources it from monthly_period_key(), so a bad value is an upstream
    bug and minting the wrong weapon is worse than a loud failure."""
    idx = monthly_period_index(date.fromisoformat(period_key))
    return MONTHLY_POOL[idx % len(MONTHLY_POOL)]


def monthly_reward_desc(account_id: int, period_key: str) -> dict:
    """Frontend RewardDesc for the monthly reward, extended with the augment
    list (name/grade/label) so the pre-claim preview and the post-claim
    confirmation show the exact item that gets minted. Roll VALUES are not
    included here -- they are randomised at grant time by the writer, not
    known ahead of the claim."""
    entry = monthly_pool_entry_for(account_id, period_key)
    desc = reward_desc(entry["template_id"], entry["quality_level"])
    desc["augments"] = [
        {"name": a["name"], "grade": a["grade"], "label": a.get("label") or a["name"]}
        for a in entry["augments"]
    ]
    return desc


# --- idempotency -------------------------------------------------------------- #

def deterministic_idem(account_id: int, reward_kind: str, period_key: str) -> str:
    """Stable uuid for (account, kind, period). Same inputs -> same uuid -> the
    game-host ledger collapses retries/concurrent claims to one grant."""
    return str(uuid.uuid5(_REWARD_NS, f"{int(account_id)}:{reward_kind}:{period_key}"))


def classify_claim_failure(result: dict):
    """Map a non-success reward-op writer result to (error_code, message, http_status),
    or None if it is not specifically classifiable (caller falls back to 'unavailable').

    Precedence matters: a full CHOAM bank ('bank capacity exceeded') MUST be caught
    before the generic 'cap'/'already' double-claim match, because that message
    contains the substring 'cap'. A full bank is a distinct, RETRYABLE case (free up
    space and claim again); it is never recorded as a local claim, so the reward
    stays claimable. 'no bank inventory' means the player has never opened their
    CHOAM bank in-game (it is pawn-keyed), also retryable."""
    err = (result.get("error") or "").lower()
    msg = (result.get("message") or "").lower()
    if err == "bank_full" or "bank capacity exceeded" in msg:
        return ("bank_full",
                result.get("message")
                or "Your CHOAM bank is full. Free up some space, then claim again.", 409)
    if err == "bank_unopened" or "no bank inventory" in msg:
        return ("bank_unopened",
                result.get("message")
                or "Open your CHOAM bank in-game once, then claim.", 409)
    if "cap" in msg or "already" in msg:
        return ("already_claimed",
                "You have already claimed this reward. Come back later.", 409)
    if "rate" in msg:
        return ("rate_limited", "Slow down a moment and try again.", 429)
    return None


# --- reward pool metadata (names / icons) ------------------------------------- #

_pool_cache: dict = {"data": None, "mtime": 0.0}


def _load_catalog() -> dict:
    """template_id -> catalog entry ({id,name,cat,tier,rarity,...}), reloaded on
    file mtime change. EXCLUDES the non-tradeable set (design section 5)."""
    try:
        mtime = os.path.getmtime(_CATALOG_PATH)
    except OSError:
        return {}
    if _pool_cache["data"] is not None and mtime == _pool_cache["mtime"]:
        return _pool_cache["data"]

    try:
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _pool_cache["data"] or {}

    non_tradeable = set()
    try:
        with open(_NON_TRADEABLE_PATH, encoding="utf-8") as fh:
            non_tradeable = {str(t).lower() for t in json.load(fh)}
    except (OSError, json.JSONDecodeError):
        non_tradeable = set()

    by_id = {}
    for it in raw.get("items", []):
        tid = it.get("id")
        if not tid or tid.lower() in non_tradeable:
            continue
        by_id[tid] = it
    _pool_cache["data"] = by_id
    _pool_cache["mtime"] = mtime
    return by_id


_gear_cache: dict = {"data": None, "mtime": 0.0}
_GEAR_STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "static", "data", "gear-stats.json")


def _load_gear_stats() -> dict:
    """template_id -> {type, rarity, description, ...} from gear-stats.json (the
    richer type/rarity source). Reloaded on mtime; empty dict if unreadable."""
    try:
        mtime = os.path.getmtime(_GEAR_STATS_PATH)
    except OSError:
        return {}
    if _gear_cache["data"] is not None and mtime == _gear_cache["mtime"]:
        return _gear_cache["data"]
    try:
        with open(_GEAR_STATS_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _gear_cache["data"] or {}
    _gear_cache["data"] = raw.get("gear", {}) if isinstance(raw, dict) else {}
    _gear_cache["mtime"] = mtime
    return _gear_cache["data"]


def solari_icon() -> str:
    """The Solari-coin icon basename for coin reward cells."""
    try:
        from item_icons import icon_for as _icon_for
        return _icon_for("SolarisCoin")
    except Exception:  # noqa: BLE001
        return "T_UI_IconResourceSolarisCoin_D"


def _icon_basename(template_id: str) -> Optional[str]:
    try:
        from item_icons import icon_for as _icon_for
        return _icon_for(template_id)
    except Exception:  # noqa: BLE001
        return None


def reward_desc(template_id: str, grade: Optional[int] = None) -> dict:
    """Frontend RewardDesc for an item reward:
    {template_id, name, icon (basename), grade, rarity, type}. name from the give-item
    catalog, type/rarity from gear-stats (fallback to catalog cat/rarity)."""
    catalog = _load_catalog().get(template_id) or {}
    gear = _load_gear_stats().get(template_id) or {}

    rarity_raw = str(gear.get("rarity") or catalog.get("rarity") or "common").lower()
    rarity = _RARITY_5SCALE.get(rarity_raw, "rare")

    typ = str(gear.get("type") or "").lower()
    if typ not in ("weapon", "armor", "tool"):
        cat = str(catalog.get("cat") or "").lower()
        typ = "armor" if cat in _ARMOR_CATS else ("tool" if cat in _TOOL_CATS else "weapon")

    return {
        "template_id": template_id,
        "name": catalog.get("name") or template_id,
        "icon": _icon_basename(template_id),
        "grade": grade,
        "rarity": rarity,
        "type": typ,
    }


def next_milestone(current: int) -> int:
    """The next streak milestone strictly above `current` (drives the gauge). Caps
    at the top daily milestone."""
    for m in STREAK_MILESTONES:
        if current < m:
            return m
    return STREAK_MILESTONES[-1]


def item_meta(template_id: str) -> dict:
    """Display metadata for a reward item: name (catalog, falling back to the
    template_id), icon url, tier, rarity. Icon lookup is lazy so the pure logic
    stays import-light."""
    entry = _load_catalog().get(template_id) or {}
    try:
        from item_icons import icon_for as _icon_for
        icon = _icon_for(template_id)
    except Exception:  # noqa: BLE001
        icon = None
    return {
        "template_id": template_id,
        "name": entry.get("name") or template_id,
        "icon": icon,
        "tier": entry.get("tier"),
        "rarity": entry.get("rarity"),
    }


# --- local claim mirror (admin.db read-side) ---------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_claim(account_id: int, reward_kind: str, period_key: str,
                 idempotency_key: str, status: str, *,
                 amount: Optional[int] = None, template_id: Optional[str] = None,
                 quality_level: Optional[int] = None,
                 detail: Optional[dict] = None) -> bool:
    """Upsert the local read-side row after the writer confirms a real grant
    (status applied/replay). INSERT OR IGNORE keyed on the deterministic
    idempotency_key AND the (account,kind,period) UNIQUE index, so a replay never
    duplicates. Returns True if a new row landed. NEVER call for a 'deferred'
    (DARK) result — nothing was granted."""
    from database import get_db
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO ls_reward_claims
                   (idempotency_key, account_id, reward_kind, period_key, amount,
                    template_id, quality_level, status, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (idempotency_key, int(account_id), reward_kind, period_key,
             amount, template_id, quality_level, status,
             json.dumps(detail, separators=(",", ":")) if detail else None,
             _now_iso()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_claimed(account_id: int, reward_kind: str, period_key: str) -> bool:
    """True if a local claim row exists for (account, kind, period)."""
    from database import get_db
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT 1 FROM ls_reward_claims
                WHERE account_id = ? AND reward_kind = ? AND period_key = ?
                LIMIT 1""",
            (int(account_id), reward_kind, period_key),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def claimed_period_keys(account_id: int, reward_kind: str) -> set:
    """All period_keys the account has locally-recorded claims for, one kind."""
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT period_key FROM ls_reward_claims
                WHERE account_id = ? AND reward_kind = ?""",
            (int(account_id), reward_kind),
        ).fetchall()
    finally:
        conn.close()
    return {r["period_key"] for r in rows}
