#!/usr/bin/env python3
"""Unit tests for the login-rewards V2 backend (Phase 1): the pure reward logic
(admin-backend/rewards.py — streak math, daily Solari ramp, weekly rotation index,
deterministic idempotency, period keys) + the local admin.db claim mirror, plus
source-scan guards over the transport/endpoint layers (relay/app.py,
admin-backend/routers/dune.py + portal.py) so a regression that (a) trusts a client
account_id, (b) fakes a success while DARK, (c) drops CSRF, or (d) records a claim
on a deferred result gets caught.

Prod-safe: runs entirely against a throwaway admin.db in a temp dir (LASTSIETCH_DB_PATH),
NO game DB, NO network.

Run:  python3 scripts/tests/test_rewards.py     (also pytest-compatible)
"""
import os
import sys
import tempfile
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ADMIN = os.path.join(REPO, "admin-backend")
sys.path.insert(0, ADMIN)

# admin.db in a temp dir + the env vars config.py requires at import.
os.environ.setdefault("LASTSIETCH_DB_PATH", os.path.join(tempfile.mkdtemp(), "admin.db"))
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
    # No augments in the WEEKLY weapon pool (those feed the Phase-2 monthly draw).
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
    assert '"account_id": active_account_id' in src
    assert 'reward_body.get("account_id")' not in src
    assert 'body.get("account_id")' not in src


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
    assert '"status": "coming_soon"' in ov   # monthly Phase-2 teaser


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
