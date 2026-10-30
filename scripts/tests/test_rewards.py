#!/usr/bin/env python3
"""Unit tests for the login-rewards V2 backend (Phase 1): the pure reward logic
(admin-backend/rewards.py, streak math, daily Solari ramp, weekly rotation index,
deterministic idempotency, period keys) + the local admin.db claim mirror, plus
source-scan guards over the transport/endpoint layers (relay/app.py,
admin-backend/routers/dune.py + portal.py) so a regression that (a) trusts a client
account_id, (b) fakes a success while DARK, (c) drops CSRF, or (d) records a claim
on a deferred result gets caught.

Prod-safe: runs entirely against a throwaway admin.db in a temp dir (LASTSIETCH_DB_PATH),
NO game DB, NO network.

Run:  python3 scripts/tests/test_rewards.py     (also pytest-compatible)
"""
import atexit
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ADMIN = os.path.join(REPO, "admin-backend")
sys.path.insert(0, ADMIN)

# admin.db in a temp dir + the env vars config.py requires at import.
_TMP = tempfile.mkdtemp()
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ.setdefault("LASTSIETCH_DB_PATH", os.path.join(_TMP, "admin.db"))
os.environ.setdefault("LASTSIETCH_RELAY_API_KEY", "test")
os.environ.setdefault("LASTSIETCH_SESSION_SECRET", "test")

import database  # noqa: E402
import rewards  # noqa: E402

database.init_db()

RELAY = os.path.join(REPO, "relay", "app.py")
PORTAL = os.path.join(ADMIN, "routers", "portal.py")
DUNE = os.path.join(ADMIN, "routers", "dune.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Daily Solari ramp (owner-locked 10k -> 25k over days 1-7, reset on a miss)
# --------------------------------------------------------------------------- #

def test_daily_ramp():
    assert rewards.DAILY_SOLARI_RAMP == [10000, 12000, 15000, 18000, 20000, 22000, 25000]
    assert rewards.DAILY_CYCLE_LEN == 7
    assert [rewards.daily_amount(n) for n in range(1, 8)] == rewards.DAILY_SOLARI_RAMP
    # day 0/negative -> cycle day 1
    assert rewards.daily_amount(0) == 10000
    assert rewards.daily_amount(-5) == 10000


def test_cycle_repeats_every_7_days():
    # the ramp REPEATS in 7-day cycles (no clamp): day 8 == day 1, day 14 == day 7
    assert rewards.cycle_day(8) == 1 and rewards.daily_amount(8) == 10000
    assert rewards.cycle_day(9) == 2 and rewards.daily_amount(9) == 12000
    assert rewards.cycle_day(14) == 7 and rewards.daily_amount(14) == 25000
    assert rewards.cycle_day(15) == 1 and rewards.daily_amount(15) == 10000
    assert rewards.cycle_day(0) == 1  # clamp below 1
    # compute_streak surfaces the cycle position for the milestone gauge
    from datetime import date, timedelta
    today = date(2026, 7, 16)
    run = {(today - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(0, 12)}
    st = rewards.compute_streak(sorted(run), today)
    assert st["current"] == 12 and st["cycle_day"] == 5 and st["today_cycle_day"] == 5


# --------------------------------------------------------------------------- #
# Streak math
# --------------------------------------------------------------------------- #

def test_streak_consecutive_including_today():
    today = date(2026, 7, 15)
    s = rewards.compute_streak(["2026-07-13", "2026-07-14", "2026-07-15"], today)
    assert s["current"] == 3
    assert s["today_day_number"] == 3
    assert s["logged_today"] is True
    assert s["best"] == 3


def test_streak_not_logged_today_uses_yesterday_run():
    today = date(2026, 7, 15)
    # run ends yesterday; today_day_number is what a claim TODAY would count as.
    s = rewards.compute_streak(["2026-07-13", "2026-07-14"], today)
    assert s["logged_today"] is False
    assert s["current"] == 2
    assert s["today_day_number"] == 3


def test_streak_gap_resets_to_day1():
    today = date(2026, 7, 15)
    s = rewards.compute_streak(["2026-07-10", "2026-07-15"], today)
    assert s["current"] == 1
    assert s["today_day_number"] == 1
    assert s["best"] == 1


def test_streak_best_is_longest_historical_run():
    today = date(2026, 7, 15)
    # a 4-day run in the past, a 1-day today
    s = rewards.compute_streak(
        ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-15"], today)
    assert s["current"] == 1
    assert s["best"] == 4


def test_streak_empty_history():
    today = date(2026, 7, 15)
    s = rewards.compute_streak([], today)
    assert s == {"current": 0, "best": 0, "logged_today": False, "today_day_number": 1,
                 "today_cycle_day": 1, "cycle_day": 0}


def test_run_ending():
    days = ["2026-07-13", "2026-07-14", "2026-07-15"]
    assert rewards.run_ending(days, date(2026, 7, 15)) == 3
    assert rewards.run_ending(days, date(2026, 7, 14)) == 2
    assert rewards.run_ending(days, date(2026, 7, 12)) == 0   # no login that day


# --------------------------------------------------------------------------- #
# Weekly rotation index + period keys
# --------------------------------------------------------------------------- #

def test_weekly_rotation_index_and_pool():
    assert len(rewards.WEEKLY_ROTATION_POOL) == 12
    # No augments in the WEEKLY weapon pool (those feed the Monthly Reward, Phase 2).
    assert not any(t.startswith("T6_Augment_") for t in rewards.WEEKLY_ROTATION_POOL)
    d = date(2026, 7, 15)
    wi = rewards.week_index(d)
    assert rewards.weekly_template_for(d) == rewards.WEEKLY_ROTATION_POOL[wi % 12]


def test_weekly_index_monotonic_across_year_boundary():
    # advances by exactly 1 each ISO week, including over the year boundary
    mondays = [date(2026, 12, 21), date(2026, 12, 28), date(2027, 1, 4), date(2027, 1, 11)]
    idx = [rewards.week_index(m) for m in mondays]
    assert idx == [idx[0] + i for i in range(4)]
    # every day within one ISO week maps to the same rotation pick
    picks = {rewards.weekly_template_for(date(2026, 7, 13) + _delta(i)) for i in range(7)}
    assert len(picks) == 1


def _delta(n):
    from datetime import timedelta
    return timedelta(days=n)


def test_period_keys():
    assert rewards.date_key(date(2026, 7, 15)) == "2026-07-15"
    assert rewards.iso_week_key(date(2026, 7, 15)) == "2026-W29"
    nm = rewards.next_utc_midnight(datetime(2026, 7, 15, 18, 30, tzinfo=timezone.utc))
    assert nm == datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Monthly Reward (Phase 2): period key, unlock gate, determinism, idempotency, pool
# --------------------------------------------------------------------------- #

def test_monthly_period_anchor_is_a_monday():
    # load-bearing: every period boundary being a Monday is what keeps the
    # rewards grid's 4 rows real weeks AND lined up with the weekly weapon's
    # own Monday 00:00 UTC rotation.
    assert rewards.MONTHLY_PERIOD_ANCHOR == date(2026, 7, 13)
    assert rewards.MONTHLY_PERIOD_ANCHOR.weekday() == 0   # Monday
    assert rewards.MONTHLY_PERIOD_LENGTH_DAYS == 28


def test_monthly_period_boundaries_match_owner_example():
    # owner-supplied example (2026-08-01 is day 20 of period 1):
    #   period 1: 2026-07-13 .. 2026-08-09
    #   period 2: 2026-08-10 .. 2026-09-06
    #   period 3: 2026-09-07 .. 2026-10-04
    assert rewards.monthly_period_start(date(2026, 8, 1)) == date(2026, 7, 13)
    assert rewards.monthly_period_end(date(2026, 8, 1)) == date(2026, 8, 9)
    assert (date(2026, 8, 1) - rewards.monthly_period_start(date(2026, 8, 1))).days + 1 == 20

    assert rewards.monthly_period_start(date(2026, 8, 10)) == date(2026, 8, 10)
    assert rewards.monthly_period_end(date(2026, 8, 10)) == date(2026, 9, 6)

    assert rewards.monthly_period_start(date(2026, 9, 7)) == date(2026, 9, 7)
    assert rewards.monthly_period_end(date(2026, 9, 7)) == date(2026, 10, 4)


def test_monthly_period_is_always_exactly_28_days():
    for probe in (date(2026, 7, 13), date(2026, 8, 1), date(2027, 2, 15), date(2030, 1, 1)):
        start = rewards.monthly_period_start(probe)
        end = rewards.monthly_period_end(probe)
        assert (end - start).days == rewards.MONTHLY_PERIOD_LENGTH_DAYS - 1
        assert start.weekday() == 0 and end.weekday() == 6   # Monday .. Sunday


def test_monthly_period_key_equals_start_date():
    for probe in (date(2026, 7, 13), date(2026, 8, 1), date(2027, 3, 3)):
        assert rewards.monthly_period_key(probe) == rewards.monthly_period_start(probe).strftime("%Y-%m-%d")


def test_monthly_period_day1_and_day28_same_period():
    start = rewards.monthly_period_start(date(2026, 8, 1))   # 2026-07-13
    day1 = start
    day28 = start + timedelta(days=27)
    assert rewards.monthly_period_key(day1) == rewards.monthly_period_key(day28) == "2026-07-13"


def test_monthly_period_rollover_day28_to_next_day1():
    # the day AFTER day 28 must fall in the NEXT period, not the same one.
    last_day_p1 = date(2026, 8, 9)
    first_day_p2 = date(2026, 8, 10)
    assert rewards.monthly_period_key(last_day_p1) == "2026-07-13"
    assert rewards.monthly_period_key(first_day_p2) == "2026-08-10"


def test_next_monthly_period_start_matches_rollover():
    nxt = rewards.next_monthly_period_start(datetime(2026, 8, 1, 18, 30, tzinfo=timezone.utc))
    assert nxt == datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
    # on the last day of a period, the next reset is still the following Monday
    nxt2 = rewards.next_monthly_period_start(datetime(2026, 8, 9, 23, 59, tzinfo=timezone.utc))
    assert nxt2 == datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


def test_monthly_login_days_count_and_unlocked_threshold():
    # owner-locked 2026-08-01: 14 distinct days -> NOT unlocked, 15 -> unlocked.
    assert rewards.MONTHLY_LOGIN_DAYS_REQUIRED == 15
    period_key = rewards.monthly_period_key(date(2026, 8, 1))   # "2026-07-13"
    days_14 = [(date(2026, 7, 13) + timedelta(days=n)).isoformat() for n in range(14)]
    days_15 = [(date(2026, 7, 13) + timedelta(days=n)).isoformat() for n in range(15)]
    assert rewards.monthly_login_days_count(days_14, period_key) == 14
    assert rewards.monthly_unlocked(days_14, period_key) is False
    assert rewards.monthly_login_days_count(days_15, period_key) == 15
    assert rewards.monthly_unlocked(days_15, period_key) is True


def test_monthly_login_days_count_spans_a_calendar_month_boundary():
    # period 1 (2026-07-13 .. 2026-08-09) straddles July/August -- prove the
    # count is a real date-range comparison, not a string-prefix match (which
    # would silently miss the August days, since it no longer aligns with any
    # single 'yyyy-mm' prefix).
    period_key = rewards.monthly_period_key(date(2026, 8, 1))
    mixed = (["2026-07-13", "2026-07-31"]      # inside, July side
             + ["2026-08-01", "2026-08-09"]    # inside, August side
             + ["2026-08-10", "2026-07-12"])   # outside the period on both edges
    assert rewards.monthly_login_days_count(mixed, period_key) == 4


def test_monthly_login_days_count_ignores_other_periods():
    period1 = rewards.monthly_period_key(date(2026, 8, 1))     # 2026-07-13
    period2 = rewards.monthly_period_key(date(2026, 8, 10))    # 2026-08-10
    days = [(date(2026, 7, 13) + timedelta(days=n)).isoformat() for n in range(15)]  # all in period 1
    assert rewards.monthly_login_days_count(days, period1) == 15
    assert rewards.monthly_login_days_count(days, period2) == 0


def test_monthly_login_days_count_dedupes():
    # duplicate rows for the same date (should not occur given the recorder's
    # PRIMARY KEY, but the count must not be foolable by a dirty read either).
    period_key = rewards.monthly_period_key(date(2026, 8, 1))
    assert rewards.monthly_login_days_count(
        ["2026-07-13", "2026-07-13", "2026-07-14"], period_key) == 2


def test_monthly_login_days_not_a_streak():
    # a gap must NOT reset progress -- unlike the daily ramp, the monthly
    # reward counts total distinct days in the period, not a consecutive run.
    period_key = rewards.monthly_period_key(date(2026, 8, 1))   # 2026-07-13 .. 2026-08-09
    gappy = ["2026-07-13", "2026-07-20", "2026-08-01", "2026-08-09"]   # 4 days, big gaps
    assert rewards.monthly_login_days_count(gappy, period_key) == 4


def test_monthly_reward_deterministic_same_account_same_period():
    # the preview shown before claiming and the item actually minted on claim
    # must always be the same PICK -- a re-read (page reload, retry) can never
    # show a different item for the same account + period. (Roll VALUES are a
    # separate concern, randomised by the writer at grant time -- see
    # test_monthly_writer_rolls_random_values_fixed_count.)
    key = rewards.monthly_period_key(date(2026, 8, 1))
    assert (rewards.monthly_pool_entry_for(4242, key)
            == rewards.monthly_pool_entry_for(4242, key))


def test_monthly_reward_identical_for_every_account():
    # NOT account-sensitive. Everyone earning the same period earns the same
    # weapon -- so a player can be told what this period's reward IS, and two
    # players comparing cards never see different items. This assertion is the
    # exact inverse of the one it replaced (which required the pick to VARY by
    # account, the pre-2026-08-10 design); if a per-account scheme is ever
    # wanted again, this test is the thing that has to be consciously deleted.
    key = rewards.monthly_period_key(date(2026, 8, 1))
    picks = {rewards.monthly_pool_entry_for(a, key)["template_id"]
             for a in range(1, 500)}
    assert len(picks) == 1, picks


def test_monthly_reward_pinned_to_its_earning_period():
    # 🔴 THE POINT OF THE PIN (owner decision 2026-08-24). The weapon is a
    # property of the PERIOD, so every day inside one period resolves to the
    # same pick. Before the pin the picker read utc_today() and ignored the
    # period_key it was handed, so the reward changed under players every
    # Monday -- and a grace claimant (whose period_key is the PRIOR period)
    # was minted whatever weapon happened to be showing the day they clicked
    # rather than the one they actually earned.
    start = date(2026, 8, 10)
    keys = {rewards.monthly_period_key(start + timedelta(days=n)) for n in range(28)}
    assert len(keys) == 1, "fixture bug: those 28 days are not one period"
    picks = {rewards.monthly_pool_entry_for(4242, k)["template_id"] for k in keys}
    assert len(picks) == 1, picks
    # and a prior-period key resolves to the PRIOR period's weapon, not today's
    prior = rewards.monthly_period_key(start - timedelta(days=1))
    assert (rewards.monthly_pool_entry_for(4242, prior)["template_id"]
            != rewards.monthly_pool_entry_for(4242, list(keys)[0])["template_id"])


def test_monthly_reward_reaches_every_weapon_in_the_pool():
    # 🔴 REGRESSION GUARD FOR A REJECTED IMPLEMENTATION, not a style preference.
    # "Pin to the earning period" reads as though it means
    # weekly_template_for(period_start). It must NOT be implemented that way:
    # a period is exactly 4 weeks and WEEKLY_ROTATION_POOL has 12 entries, so
    # gcd(4, 12) = 4 and the period start lands on only 12/4 = 3 distinct pool
    # positions -- permanently stranding 9 of the 12 weapons. Indexing by
    # PERIOD NUMBER walks the whole pool instead. This asserts the whole pool
    # is reachable, which the rejected version fails 3 != 12.
    n = len(rewards.MONTHLY_POOL)
    picks = {rewards.monthly_pool_entry_for(4242, rewards.monthly_period_key(
        date(2026, 7, 13) + timedelta(days=28 * i)))["template_id"]
        for i in range(n)}
    assert len(picks) == n, f"only {len(picks)} of {n} weapons reachable: {picks}"


def test_monthly_reward_rejects_a_malformed_period_key():
    # a bad period_key is an upstream bug (every caller sources it from
    # monthly_period_key). Fail loudly rather than silently minting pool[0] --
    # a wrong weapon that looks deliberate is worse than a 500.
    for bad in ("not-a-date", "", "2026-13-01", "08-10-2026"):
        try:
            rewards.monthly_pool_entry_for(4242, bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"accepted malformed period_key {bad!r}")


def test_monthly_idempotency_key_stable_and_scoped():
    key = rewards.monthly_period_key(date(2026, 8, 1))                 # "2026-07-13"
    key2 = rewards.monthly_period_key(date(2026, 8, 10))               # "2026-08-10", next period
    a = rewards.deterministic_idem(4242, "monthly_augment", key)
    assert a == rewards.deterministic_idem(4242, "monthly_augment", key)   # stable
    assert a != rewards.deterministic_idem(4242, "monthly_augment", key2)  # per period
    assert a != rewards.deterministic_idem(4243, "monthly_augment", key)   # per account
    assert a != rewards.deterministic_idem(4242, "weekly_item", key)       # per kind


# --------------------------------------------------------------------------- #
# period_cells: the real dated 28-cell period calendar (alongside cycle_cells)
# --------------------------------------------------------------------------- #

def test_period_cells_count_and_start_is_a_monday():
    today = date(2026, 8, 1)
    cells = rewards.period_cells([], set(), today)
    assert len(cells) == 28
    assert cells[0]["date"] == rewards.monthly_period_key(today) == "2026-07-13"
    assert datetime.strptime(cells[0]["date"], "%Y-%m-%d").date().weekday() == 0
    assert cells[-1]["date"] == rewards.monthly_period_end(today).strftime("%Y-%m-%d")


def test_period_cells_gap_resets_ramp_not_continues():
    # owner's explicit regression case: a post-gap day must pay the RESET
    # ramp rung (10000), not a continued rung as if the gap never happened.
    today = date(2026, 7, 20)
    login_dates = ["2026-07-13", "2026-07-14", "2026-07-15",   # 3-day run
                   "2026-07-18", "2026-07-19", "2026-07-20"]   # gap on 16/17, new run
    cells = rewards.period_cells(login_dates, set(), today)
    by_date = {c["date"]: c for c in cells}
    assert by_date["2026-07-15"]["amount"] == 15000 and by_date["2026-07-15"]["ramp_day"] == 3
    assert by_date["2026-07-18"]["amount"] == 10000 and by_date["2026-07-18"]["ramp_day"] == 1
    assert by_date["2026-07-19"]["amount"] == 12000
    assert by_date["2026-07-20"]["amount"] == 15000 and by_date["2026-07-20"]["is_today"] is True
    # the two gap days themselves: not logged, no amount
    assert by_date["2026-07-16"]["logged"] is False
    assert by_date["2026-07-16"]["amount"] is None


def test_period_cells_future_days_never_carry_an_amount():
    today = date(2026, 7, 20)
    login_dates = ["2026-07-13"]
    cells = rewards.period_cells(login_dates, set(), today)
    future = [c for c in cells if c["is_future"]]
    assert future   # sanity: period 1 runs past 07-20, so future cells exist
    assert all(c["amount"] is None and c["ramp_day"] is None for c in future)
    assert all(c["date"] > rewards.date_key(today) for c in future)


def test_period_cells_milestone_follows_ramp_day_not_column():
    # log in 10 CONSECUTIVE days from the period start: raw streak lengths
    # 1..10. Ramp day 3 (07-15) and day 7 (07-19) are milestones; day 10
    # (07-22) wraps to cycle position 3 -- ALSO a milestone, even though its
    # RAW length (10) is not itself in DAILY_MILESTONE_DAYS. This is the
    # exact case a naive raw-day check would get wrong.
    today = date(2026, 7, 22)
    login_dates = [(date(2026, 7, 13) + timedelta(days=n)).isoformat() for n in range(10)]
    cells = rewards.period_cells(login_dates, set(), today)
    by_date = {c["date"]: c for c in cells}
    assert by_date["2026-07-15"]["milestone"] is True    # ramp day 3
    assert by_date["2026-07-19"]["milestone"] is True    # ramp day 7
    assert by_date["2026-07-22"]["milestone"] is True    # raw 10 -> wrapped ramp day 3
    assert by_date["2026-07-13"]["milestone"] is False   # ramp day 1
    # 2026-07-27 sits in the same weekday COLUMN as 07-13/07-20 but was never
    # logged in -- if milestone were ever keyed off column instead of ramp
    # day, this would wrongly flag.
    assert by_date["2026-07-27"]["milestone"] is False


def test_period_cells_logged_count_matches_progress():
    today = date(2026, 8, 1)
    login_dates = [(date(2026, 7, 13) + timedelta(days=n)).isoformat()
                   for n in (0, 2, 5, 10, 15, 19)]   # 6 non-consecutive days
    period_key = rewards.monthly_period_key(today)
    cells = rewards.period_cells(login_dates, set(), today)
    logged_count = sum(1 for c in cells if c["logged"])
    assert logged_count == rewards.monthly_login_days_count(login_dates, period_key) == 6


def test_period_cells_claimed_flag_independent_of_logged():
    today = date(2026, 7, 20)
    login_dates = ["2026-07-13", "2026-07-14"]
    cells = rewards.period_cells(login_dates, {"2026-07-13"}, today)
    by_date = {c["date"]: c for c in cells}
    assert by_date["2026-07-13"]["claimed"] is True
    assert by_date["2026-07-14"]["claimed"] is False   # logged but not claimed


def test_portal_overview_daily_exposes_calendar_alongside_cycle():
    src = _read(PORTAL)
    ov = src.split("async def portal_rewards_overview", 1)[1].split("async def ", 1)[0]
    assert '"cycle": grid_cells' in ov          # old key untouched
    assert '"calendar": calendar_cells' in ov   # new key, additive
    assert "rewards.period_cells(login_days, daily_claimed, today)" in ov


def test_monthly_progress_count_equals_the_calendar_grid():
    # THE INVARIANT the gauge/grid agreement actually rests on: the number the
    # card shows and the number a player can count off the 28-cell grid must be
    # the same, or the UI contradicts itself.
    #
    # This replaced a source-scan for the literal string
    #   monthly_progress = sum(1 for c in calendar_cells if c["logged"])
    # which went stale the moment that line was refactored to read
    # mstate["current_progress"]. Both period_cells() and
    # monthly_login_days_count() DOCUMENT that they agree; two docstrings
    # asserting the same equivalence is not a proof of it, so this asserts the
    # counts themselves. Behaviour cannot be refactored out from under it.
    import random
    rnd = random.Random(20260824)
    today = date(2026, 8, 24)
    start = rewards.monthly_period_start(today)
    key = rewards.monthly_period_key(today)
    span = rewards.MONTHLY_PERIOD_LENGTH_DAYS

    scenarios = [
        [],                                                                # never logged in
        [rewards.date_key(start + timedelta(days=i)) for i in range(span)],  # every day
        [rewards.date_key(start - timedelta(days=1))],                     # only OUTSIDE (prior period)
        [rewards.date_key(start + timedelta(days=span))],                  # only OUTSIDE (next period)
        [rewards.date_key(start)] * 5,                                     # duplicates of one day
        [rewards.date_key(start), "", None],                               # falsy entries
    ]
    for _ in range(40):                                                    # seeded, deterministic
        scenarios.append([rewards.date_key(start + timedelta(days=rnd.randrange(-7, span + 7)))
                          for _ in range(rnd.randrange(0, 40))])

    for logins in scenarios:
        cells = rewards.period_cells(logins, [], today)
        assert len(cells) == span, f"grid is not {span} cells"
        grid_count = sum(1 for c in cells if c["logged"])
        gauge_count = rewards.monthly_login_days_count(logins, key)
        assert grid_count == gauge_count, (
            f"grid shows {grid_count} but gauge says {gauge_count} for {logins!r}")


def test_portal_overview_progress_and_grid_share_one_source():
    # Pairs with test_monthly_progress_count_equals_the_calendar_grid. That one
    # proves the two rewards.py counters agree; this proves the overview endpoint
    # actually USES those two and not some third count of its own. Neither is
    # sufficient alone: agreeing counters do not help if the endpoint ignores
    # them, and using them does not help if they disagree.
    src = _read(PORTAL)
    ov = src.split("async def portal_rewards_overview", 1)[1].split("async def ", 1)[0]
    assert "calendar_cells = rewards.period_cells(login_days, daily_claimed, today)" in ov
    assert 'monthly_progress = mstate["current_progress"]' in ov
    assert '"calendar": calendar_cells' in ov


def test_monthly_pool_every_entry_resolves_reward_desc():
    for entry in rewards.MONTHLY_POOL:
        r = rewards.reward_desc(entry["template_id"], entry["quality_level"])
        assert r["name"] and r["name"] != entry["template_id"], \
            f"{entry['template_id']} has no catalog name"
        assert r["type"] == "weapon"
        assert r["rarity"] in ("common", "uncommon", "rare", "epic", "legendary")


def test_monthly_pool_augment_shape():
    # every augment carries a positive roll count and a grade in 1..5, in the
    # T<digit>_Augment_* family the writer re-validates against, and the pool
    # entry never exceeds the game's weapon augment slot cap (3).
    import re
    for entry in rewards.MONTHLY_POOL:
        assert 1 <= len(entry["augments"]) <= 3
        for a in entry["augments"]:
            assert re.match(r"^T\d+_Augment_", a["name"])
            assert 1 <= a["grade"] <= 5
            assert 1 <= a["rolls"] <= 10


def test_monthly_pool_augments_are_catalogue_compatible():
    """Every (template_id, augment) pair in MONTHLY_POOL must pass
    scripts/data/augment-compatibility.json's matchRule (compatible when any
    ITEM tag startswith any AUGMENT tag). A pool entry that fails this can
    never ship -- this IS the ship gate, not documentation of one."""
    import json as _json
    catalog_path = os.path.join(REPO, "scripts", "data", "augment-compatibility.json")
    with open(catalog_path, encoding="utf-8") as fh:
        catalog = _json.load(fh)
    augments_catalog = catalog["augments"]
    aliases = catalog["itemAliases"]

    def compatible(template_id, augment_id):
        it_tags = aliases.get(template_id) or []
        aug_tags = (augments_catalog.get(augment_id) or {}).get("tags") or []
        assert it_tags, f"{template_id} has no itemAliases entry -- cannot verify"
        assert aug_tags, f"{augment_id} has no augments entry -- cannot verify"
        return any(i.startswith(a) for i in it_tags for a in aug_tags)

    for entry in rewards.MONTHLY_POOL:
        for a in entry["augments"]:
            assert compatible(entry["template_id"], a["name"]), (
                f"{a['name']} is not compatible with {entry['template_id']} "
                "per the catalogue's matchRule")


def test_monthly_pool_roll_counts_match_verified_file():
    """Roll counts in MONTHLY_POOL are literals for readability, but they must
    never drift from scripts/data/augment-roll-counts.json (measured live
    2026-08-01 against real standalone augment rows) -- the COUNT is
    load-bearing (a wrong count renders an empty augment slot in game), so
    this is the ship gate that keeps a literal from silently going stale. An
    augment absent from the file has no verified count and must be impossible
    to ship, caught by the same assertion (dict lookup would KeyError, but we
    check explicitly for a clear failure message)."""
    counts_path = os.path.join(REPO, "scripts", "data", "augment-roll-counts.json")
    with open(counts_path, encoding="utf-8") as fh:
        verified = json.load(fh)["roll_counts"]
    for entry in rewards.MONTHLY_POOL:
        for a in entry["augments"]:
            assert a["name"] in verified, (
                f"{a['name']} has no verified roll count on file -- "
                "cannot ship an unmeasured augment")
            assert a["rolls"] == verified[a["name"]], (
                f"{a['name']}: pool says {a['rolls']} rolls, verified file "
                f"says {verified[a['name']]} -- literal has drifted, fix the pool")


def test_monthly_pool_includes_melee_variety():
    # owner's ask: the pool must not be weapon-class-monotonous. At least one
    # entry uses a melee-tagged augment (Melee*/Ch5_Melee* family).
    melee_entries = [e for e in rewards.MONTHLY_POOL
                     if any(a["name"].startswith(("T6_Augment_Melee", "T6_Augment_Ch5_Melee"))
                            for a in e["augments"])]
    assert len(melee_entries) >= 1


def test_monthly_pool_is_not_thin():
    # six weapons with no melee was the thin first-month pool the owner asked
    # to widen; this pins that the widening actually happened and stays put.
    assert len(rewards.MONTHLY_POOL) >= 10


def test_portal_claim_accepts_monthly_augment():
    src = _read(PORTAL)
    assert '"daily_solari", "weekly_item", "monthly_augment"' in src


def test_portal_claim_monthly_derives_entry_server_side():
    claim = _read(PORTAL).split("async def portal_rewards_claim", 1)[1].split("\nasync def ", 1)[0]
    # the monthly entry (template/quality/augments) is derived from
    # rewards.monthly_pool_entry_for(account, period_key), never from the client.
    assert "rewards.monthly_pool_entry_for(active_account_id, period_key)" in claim
    assert 'body.get("template_id")' not in claim
    assert 'body.get("augments")' not in claim


def test_portal_claim_monthly_reeligibility_is_server_side():
    claim = _read(PORTAL).split("async def portal_rewards_claim", 1)[1].split("\nasync def ", 1)[0]
    # the 15-day gate is re-derived from the account's OWN login history on
    # every claim -- a client can never assert the gate is already met.
    #
    # Previously asserted rewards.monthly_unlocked(...) / monthly_login_days_count(...)
    # by name. Those call sites were folded into monthly_claim_state(), which
    # derives the gate from login_days internally (cur_progress ->
    # cur_unlocked), so the PROPERTY still holds and only the names moved. The
    # teeth are the two negative assertions below, which are what actually stop
    # a client asserting its own eligibility -- those are unchanged.
    assert "rewards.monthly_claim_state(login_days, monthly_claimed, today)" in claim
    assert "login_days" in claim
    # the gate inputs come from the session/DB, never the request body
    assert 'body.get("login_days")' not in claim
    assert 'body.get("progress")' not in claim
    assert 'body.get("period_key")' not in claim
    # the refusal reports the player's actual progress, not a static string
    assert "progress" in claim


def test_portal_overview_monthly_exposes_requirement_and_progress():
    src = _read(PORTAL)
    ov = src.split("async def portal_rewards_overview", 1)[1].split("async def ", 1)[0]
    assert '"requirement": rewards.MONTHLY_LOGIN_DAYS_REQUIRED' in ov
    assert '"progress": monthly_progress' in ov


def _run_reward_op_dry(payload):
    """Invoke dune-reward-op.sh --dry-run (SQL built, never executed, no DB/SSH
    touched -- returns before resolve_db_pod()). Mirrors the existing
    dune-gift-op.sh subprocess-probe pattern (test_guild_op.py)."""
    reward_op = os.path.join(REPO, "scripts", "dune-reward-op.sh")
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    env = dict(os.environ)
    env["LASTSIETCH_REWARD_ENABLED"] = "1"   # so dry-run reaches the writer, not the dark gate
    proc = subprocess.run(
        ["bash", reward_op, "--op-b64", b64, "--dry-run"],
        capture_output=True, text=True, env=env, timeout=30)
    return json.loads(proc.stdout)


def test_monthly_writer_rolls_random_values_fixed_count():
    """Owner-locked 2026-08-01: rolls are no longer a guaranteed perfect 1.0.
    The writer must draw a FRESH random value per roll slot, but the roll
    COUNT must stay exactly what the caller (rewards.py's pool entry)
    specifies -- a wrong count renders an empty augment slot in game."""
    payload = {
        "account_id": 2036, "reward_kind": "monthly_augment",
        "idempotency_key": "11111111-1111-4111-8111-111111111111",
        "template_id": "LMG_Unique_Power_06", "quality_level": 5,
        "augments": [
            {"name": "T6_Augment_Lmg1", "grade": 5, "rolls": 6},
            {"name": "T6_Augment_Damage1", "grade": 5, "rolls": 1},
            {"name": "T6_Augment_Acuracy1", "grade": 5, "rolls": 1},
        ],
        "period_key": "2026-08-10", "operator": "test", "mode": "dry-run",
    }
    out1 = _run_reward_op_dry(payload)
    out2 = _run_reward_op_dry(payload)
    assert out1["status"] == "dry-run", out1
    assert out2["status"] == "dry-run", out2
    rd1 = out1["stats"]["FAugmentedItemStats"][1]["AppliedAugmentRollData"]
    rd2 = out2["stats"]["FAugmentedItemStats"][1]["AppliedAugmentRollData"]
    # roll COUNT stays fixed to the requested augments (6, 1, 1)
    assert [len(r["StatRolls"]) for r in rd1] == [6, 1, 1]
    assert [len(r["StatRolls"]) for r in rd2] == [6, 1, 1]
    # roll VALUES are no longer a guaranteed perfect 1.0, and vary run to run
    all_values = [v for r in rd1 for v in r["StatRolls"]]
    assert not all(v == 1.0 for v in all_values), "rolls must not be perfect anymore"
    assert all(0.0 <= v <= 1.0 for v in all_values)
    assert rd1 != rd2, "roll values must be freshly randomised, not deterministic"


# --------------------------------------------------------------------------- #
# Deterministic idempotency (the real double-claim guard)
# --------------------------------------------------------------------------- #

def test_deterministic_idem_stable_and_scoped():
    a = rewards.deterministic_idem(1001, "daily_solari", "2026-07-15")
    assert a == rewards.deterministic_idem(1001, "daily_solari", "2026-07-15")   # stable
    assert a != rewards.deterministic_idem(1001, "daily_solari", "2026-07-16")   # per period
    assert a != rewards.deterministic_idem(1002, "daily_solari", "2026-07-15")   # per account
    assert a != rewards.deterministic_idem(1001, "weekly_item", "2026-07-15")    # per kind
    # canonical uuid shape (the relay + writer both require a UUID)
    import re
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", a)


# --------------------------------------------------------------------------- #
# Local claim mirror (admin.db read-side)
# --------------------------------------------------------------------------- #

def test_local_mirror_record_and_query():
    acct = 5001
    key = rewards.date_key(date(2026, 7, 15))
    idem = rewards.deterministic_idem(acct, "daily_solari", key)
    assert rewards.is_claimed(acct, "daily_solari", key) is False
    assert rewards.record_claim(acct, "daily_solari", key, idem, "applied", amount=15000) is True
    assert rewards.is_claimed(acct, "daily_solari", key) is True
    assert rewards.claimed_period_keys(acct, "daily_solari") == {key}


def test_local_mirror_replay_is_idempotent():
    acct = 5002
    key = rewards.date_key(date(2026, 7, 15))
    idem = rewards.deterministic_idem(acct, "daily_solari", key)
    assert rewards.record_claim(acct, "daily_solari", key, idem, "applied", amount=15000) is True
    # same idem (a game-host replay) -> no duplicate row
    assert rewards.record_claim(acct, "daily_solari", key, idem, "replay", amount=15000) is False


def test_local_mirror_composite_unique_blocks_second_claim_same_period():
    acct = 5003
    key = rewards.date_key(date(2026, 7, 15))
    idem1 = rewards.deterministic_idem(acct, "daily_solari", key)
    idem2 = rewards.deterministic_idem(acct, "daily_solari", key + "-x")  # different uuid
    assert rewards.record_claim(acct, "daily_solari", key, idem1, "applied", amount=15000) is True
    # a different idempotency key but the SAME (account, kind, period) is refused
    assert rewards.record_claim(acct, "daily_solari", key, idem2, "applied", amount=15000) is False
    assert rewards.claimed_period_keys(acct, "daily_solari") == {key}


def test_local_mirror_per_account_keying():
    key = rewards.date_key(date(2026, 7, 15))
    for acct in (6001, 6002):
        idem = rewards.deterministic_idem(acct, "weekly_item", rewards.iso_week_key(date(2026, 7, 15)))
        wk = rewards.iso_week_key(date(2026, 7, 15))
        rewards.record_claim(acct, "weekly_item", wk, idem, "applied", template_id="UniqueScattergun5",
                             quality_level=3)
    assert rewards.is_claimed(6001, "weekly_item", rewards.iso_week_key(date(2026, 7, 15)))
    assert rewards.is_claimed(6002, "weekly_item", rewards.iso_week_key(date(2026, 7, 15)))
    # one account's claim never satisfies another's
    assert not rewards.is_claimed(6003, "weekly_item", rewards.iso_week_key(date(2026, 7, 15)))


# --------------------------------------------------------------------------- #
# Reward-pool metadata (catalog names, non-tradeable exclusion)
# --------------------------------------------------------------------------- #

def test_item_meta_from_catalog():
    m = rewards.item_meta("UniqueScattergun5")
    assert m["name"] == "Perforator"
    assert m["tier"] == 6
    # unknown template falls back to the raw id, never throws
    u = rewards.item_meta("NotARealTemplate_ZZZ")
    assert u["name"] == "NotARealTemplate_ZZZ"


def test_reward_desc_shape_for_pool_item():
    r = rewards.reward_desc("UniqueScattergun5", grade=3)
    assert r["template_id"] == "UniqueScattergun5"
    assert r["name"] == "Perforator"
    assert r["grade"] == 3
    assert r["type"] == "weapon"                    # gear-stats type
    assert r["rarity"] == "epic"                    # gear-stats Unique -> epic
    assert r["icon"] and isinstance(r["icon"], str)  # basename, not a URL
    assert r["rarity"] in ("common", "uncommon", "rare", "epic", "legendary")


def test_every_pool_template_resolves_reward_desc():
    for tid in rewards.WEEKLY_ROTATION_POOL:
        r = rewards.reward_desc(tid, grade=3)
        assert r["name"] and r["name"] != tid, f"{tid} has no catalog name"
        assert r["type"] in ("weapon", "armor", "tool")
        assert r["rarity"] in ("common", "uncommon", "rare", "epic", "legendary")


def test_next_milestone():
    assert rewards.next_milestone(0) == 3
    assert rewards.next_milestone(2) == 3
    assert rewards.next_milestone(3) == 7
    assert rewards.next_milestone(6) == 7
    assert rewards.next_milestone(7) == 7
    assert rewards.next_milestone(99) == 7


def test_solari_icon_basename():
    assert rewards.solari_icon() == "T_UI_IconResourceSolarisCoin_D"


def test_non_tradeables_excluded_from_pool_catalog():
    import json
    nt = json.load(open(os.path.join(ADMIN, "data", "dune-item-non-tradeable.json")))
    catalog = rewards._load_catalog()
    for t in list(nt)[:200]:
        assert t not in catalog, f"non-tradeable {t} leaked into the reward catalog"


# --------------------------------------------------------------------------- #
# Transport / endpoint source-scan guards
# --------------------------------------------------------------------------- #

def test_relay_reward_op_wiring():
    src = _read(RELAY)
    assert '@app.post("/dune/reward-op"' in src
    assert '_dune_ssh_stdin("reward-op"' in src
    # DARK-safe: the writer surfaces status:deferred; the relay forwards verbatim.
    assert '@app.get("/dune/rewards/login-days"' in src
    assert '_dune_ssh_json(f"login-days {account_id}"' in src
    # template_id charset matches the writer (alnum + underscore, no hyphen)
    assert r'[A-Za-z0-9_]{1,64}' in src


def test_dune_router_login_days_proxy():
    src = _read(DUNE)
    assert "cached_reward_login_days" in src
    assert "/dune/rewards/login-days?account_id=" in src


def test_portal_claim_uses_session_account_never_client():
    src = _read(PORTAL)
    # account is the session aid, forwarded to the writer; the body is never a source
    # of account_id in the claim path.
    #
    # Scoped to the CLAIM HANDLER, not the whole file. A file-wide scan for
    # body.get("account_id") is a false positive: /portal/select-account (the
    # multi-account switcher) legitimately reads a client-supplied account_id and
    # THEN proves Discord ownership of it before trusting it. Scanning the whole
    # file made this test fail on an untouched tree, which is worse than useless
    # because a permanently-red test hides the regression it exists to catch.
    # Same top-level-async-def isolation the sibling tests use.
    claim = src.split("async def portal_rewards_claim", 1)[1].split("\nasync def ", 1)[0]
    assert '"account_id": active_account_id' in claim
    assert 'reward_body.get("account_id")' not in claim
    assert 'body.get("account_id")' not in claim


def test_portal_claim_csrf_and_deferred_honesty():
    src = _read(PORTAL)
    # the claim endpoint gates on CSRF via the shared v2 helper
    assert "_v2_body_and_csrf(request)" in src
    # DARK: a deferred result returns before any local claim is recorded, and never
    # as a success. record_claim must live in the applied/replay branch only.
    # isolate the handler; split on a TOP-LEVEL async def so the nested async _grant
    # helper does not truncate the body.
    claim = src.split("async def portal_rewards_claim", 1)[1].split("\nasync def ", 1)[0]
    deferred_at = claim.find('== "deferred"')
    record_at = claim.find("rewards.record_claim(")
    assert deferred_at != -1 and record_at != -1
    assert deferred_at < record_at, "the deferred short-circuit must precede record_claim"
    # the deferred branch does not claim success
    assert '"status": "deferred"' in claim


def test_portal_overview_shape_keys():
    src = _read(PORTAL)
    ov = src.split("async def portal_rewards_overview", 1)[1].split("async def ", 1)[0]
    # matches the FROZEN canonical contract keys exactly
    for key in ('"enabled"', '"server_now_utc"', '"next_claim_utc"', '"streak"',
                '"daily"', '"weekly"', '"monthly"', '"csrf_token"',
                '"milestone_next"', '"logged_today"', '"week_key"', '"template_id"',
                '"ramp"', '"milestones"', '"cycle"', '"claimable_total"',
                '"cycle_len"', '"weeks"'):
        assert key in ov, f"overview missing {key}"
    # Phase 2: the Monthly Reward is a real payload now, not a "coming soon" teaser.
    for key in ('"period_start"', '"period_end"', '"augments"', '"resets_utc"',
                '"resets_label"', '"unlocked"', '"claimable"', '"requirement"', '"progress"'):
        assert key in ov, f"monthly overview missing {key}"
    assert '"status": "coming_soon"' not in ov


def test_cycle_grid_and_accumulate_pool():
    from datetime import date, timedelta
    today = date(2026, 7, 16)
    k = lambda d: d.strftime("%Y-%m-%d")  # noqa: E731
    login = [k(today), k(today - timedelta(days=1))]   # 2-day streak, nothing claimed

    # accumulate: both unclaimed logged days -> 10k + 12k = 22k (the Claim button)
    total, entries = rewards.claim_pool(login, set(), today)
    assert total == 22000
    assert [e[0] for e in entries] == [k(today - timedelta(days=1)), k(today)]
    assert [e[1] for e in entries] == [10000, 12000]

    # grid: Day 1 + Day 2 claimable (in pool), Day 3-7 upcoming; today == Day 2
    cells = rewards.cycle_cells(login, set(), today)
    assert [c["state"] for c in cells] == \
        ["claimable", "claimable", "upcoming", "upcoming", "upcoming", "upcoming", "upcoming"]
    assert cells[6]["cycle_day"] == 7 and cells[6]["amount"] == 25000
    assert cells[1]["is_today"] is True   # Day 2

    # claiming Day 2 (recording both days) leaves an empty pool next call
    claimed = {k(today - timedelta(days=1)), k(today)}
    assert rewards.claim_pool(login, claimed, today) == (0, [])
    assert [c["state"] for c in rewards.cycle_cells(login, claimed, today)][:2] == \
        ["claimed", "claimed"]


def test_portal_claim_success_shape():
    src = _read(PORTAL)
    claim = src.split("async def portal_rewards_claim", 1)[1].split("\nasync def ", 1)[0]
    # success surfaces reward_kind verbatim; daily reports the ACCUMULATED total + day
    # count, weekly the template + grade. Each rewarded period is granted+recorded per
    # rewarded day (idempotent per day) so a re-run can never double-grant.
    assert '"reward_kind": reward_kind' in claim
    assert 'out["amount"] = granted_amount' in claim
    assert 'out["days"] = granted_n' in claim
    assert "rewards.record_claim(" in claim
    # inline eligibility/refusal codes handled directly in the claim handler
    for code in ('"not_logged_in"', '"already_claimed"', '"locked"', '"csrf"',
                 '"unavailable"'):
        assert code in claim, f"claim missing error code {code}"
    # failure classification (bank_full/bank_unopened/already_claimed/rate_limited) is
    # delegated to the pure, tested rewards.classify_claim_failure helper
    assert "classify_claim_failure" in claim
    # body carries reward_kind (not a client uuid) and account is server-side
    assert 'body.get("reward_kind"' in claim
    assert 'body.get("uuid"' not in claim


def test_enabled_probe_is_dry_run():
    src = _read(DUNE)
    assert "cached_rewards_enabled" in src
    assert '"mode": "dry-run"' in src            # side-effect-free probe
    assert 'r.get("status") == "dry-run"' in src  # enabled iff writer accepts a dry-run


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def test_classify_bank_full_from_error_code():
    code, msg, status = rewards.classify_claim_failure(
        {"success": False, "error": "bank_full", "message": "Your CHOAM bank is full."})
    assert code == "bank_full" and status == 409 and "full" in msg.lower()


def test_classify_bank_full_from_message_beats_already_claimed():
    # The writer's capacity message contains the substring "cap" -> it MUST NOT be
    # misread as already_claimed. This is the regression this whole safeguard guards.
    res = {"success": False, "status": "failed",
           "message": "weekly_item mint failed: BANK_BATCH_FAIL: bank capacity exceeded "
                      "(used 500 + batch 1 > cap 500)"}
    code, _, status = rewards.classify_claim_failure(res)
    assert code == "bank_full", f"expected bank_full, got {code}"
    assert status == 409


def test_classify_bank_unopened():
    code, _, status = rewards.classify_claim_failure(
        {"success": False, "message": "BANK_BATCH_FAIL: no bank inventory for account=2036"})
    assert code == "bank_unopened" and status == 409


def test_classify_already_claimed_and_rate_and_none():
    c1, _, s1 = rewards.classify_claim_failure({"message": "reward cap: already has 1 applied"})
    assert c1 == "already_claimed" and s1 == 409
    c2, _, s2 = rewards.classify_claim_failure({"message": "rate limited, slow down"})
    assert c2 == "rate_limited" and s2 == 429
    assert rewards.classify_claim_failure({"message": "some unexpected failure"}) is None


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
