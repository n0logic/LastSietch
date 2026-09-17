"""Portal rate-limit checks (P4).

Five rolling-window tiers per architect §4 / §8:
- 5 OAuth starts / hour / IP
- 5 OAuth callbacks / hour / IP
- 3 link attempts / 24h / discord_id
- 5 attempts / 24h / account_id
- 3 fails / 24h / (discord_id, account_id) → 24h cooldown

Implementation: SQLite COUNT(*) against portal_link_attempts with a windowed
WHERE clause. Source of truth is the SQLite table — no in-memory state that
diverges across workers (currently single-worker, but defensive).

Each check returns (ok: bool, retry_after_seconds: int). On breach the caller
returns 429 with a Retry-After header + renders error_rate_limited.html.
"""
from datetime import datetime, timedelta, timezone
from typing import Tuple

from config import (
    PORTAL_RATE_ATTEMPTS_PER_ACCOUNT_PER_DAY,
    PORTAL_RATE_ATTEMPTS_PER_DISCORD_PER_DAY,
    PORTAL_RATE_CALLBACKS_PER_IP_PER_HOUR,
    PORTAL_RATE_COOLDOWN_HOURS,
    PORTAL_RATE_FAILS_TO_COOLDOWN,
    PORTAL_RATE_STARTS_PER_IP_PER_HOUR,
)
from database import get_db

# Per-discord_id select burst limit (M-4 review fix). A user with a 5-min
# link_flow cookie could otherwise POST /portal/link/select unbounded and
# generate thousands of quiz rows + relay reads before per-day caps catch up.
PORTAL_RATE_SELECTS_PER_DISCORD_PER_WINDOW = 10
PORTAL_RATE_SELECT_WINDOW_MINUTES = 5


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_minus(hours: int) -> str:
    return (_now_utc() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _iso_minus_minutes(minutes: int) -> str:
    return (_now_utc() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def check_oauth_start_ip(ip: str) -> Tuple[bool, int]:
    """Per-IP cap on /portal/login starts (counts state_token mints in last hour)."""
    if not ip:
        return True, 0
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_link_attempts
               WHERE ip_addr = ? AND state_issued_at >= ?
                 AND is_test_run = 0""",
            (ip, _iso_minus(1)),
        ).fetchone()
    finally:
        conn.close()
    if row["c"] >= PORTAL_RATE_STARTS_PER_IP_PER_HOUR:
        return False, 3600
    return True, 0


def check_oauth_callback_ip(ip: str) -> Tuple[bool, int]:
    """Per-IP cap on callback hits. Counts rows where callback_hit_at is set
    in the last hour. callback_hit_at is stamped at callback entry BEFORE
    state validation, so forged-state and replayed-state attempts all count
    (M-6 review fix — previously gated on state_consumed_at, which a
    tampered-state attacker could bypass)."""
    if not ip:
        return True, 0
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_link_attempts
               WHERE ip_addr = ? AND callback_hit_at >= ?
                 AND is_test_run = 0""",
            (ip, _iso_minus(1)),
        ).fetchone()
    finally:
        conn.close()
    if row["c"] >= PORTAL_RATE_CALLBACKS_PER_IP_PER_HOUR:
        return False, 3600
    return True, 0


def check_selects_per_discord(discord_id: str) -> Tuple[bool, int]:
    """Per-discord_id select burst cap (M-4 review fix). Counts rows in
    portal_link_attempts where pick_token IS NOT NULL in the last 5 minutes
    for this discord_id. Prevents unbounded /portal/link/select POSTs from
    minting attempt rows + churning relay snapshot reads inside the
    5-minute link_flow window."""
    if not discord_id:
        return True, 0
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_link_attempts
               WHERE discord_id = ?
                 AND pick_token IS NOT NULL
                 AND state_issued_at >= ?
                 AND is_test_run = 0""",
            (discord_id, _iso_minus_minutes(PORTAL_RATE_SELECT_WINDOW_MINUTES)),
        ).fetchone()
    finally:
        conn.close()
    if row["c"] >= PORTAL_RATE_SELECTS_PER_DISCORD_PER_WINDOW:
        return False, PORTAL_RATE_SELECT_WINDOW_MINUTES * 60
    return True, 0


def check_attempts_per_discord(discord_id: str) -> Tuple[bool, int]:
    """3 attempts / 24h per discord_id. Counts rows in last 24h where the
    discord_id was bound (i.e. callback completed)."""
    if not discord_id:
        return True, 0
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_link_attempts
               WHERE discord_id = ? AND attempt_at >= ?
                 AND is_test_run = 0""",
            (discord_id, _iso_minus(24)),
        ).fetchone()
    finally:
        conn.close()
    if row["c"] >= PORTAL_RATE_ATTEMPTS_PER_DISCORD_PER_DAY:
        return False, 24 * 3600
    return True, 0


def check_attempts_per_account(account_id: int) -> Tuple[bool, int]:
    """5 attempts / 24h per account_id. Possible impersonation signal —
    operator alert recommended (deferred to post-P4 1.1)."""
    if not account_id:
        return True, 0
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_link_attempts
               WHERE account_id = ? AND attempt_at >= ?
                 AND is_test_run = 0""",
            (account_id, _iso_minus(24)),
        ).fetchone()
    finally:
        conn.close()
    if row["c"] >= PORTAL_RATE_ATTEMPTS_PER_ACCOUNT_PER_DAY:
        return False, 24 * 3600
    return True, 0


def check_cooldown(discord_id: str, account_id: int) -> Tuple[bool, int]:
    """3 fails in 24h on the SAME (discord_id, account_id) pair → 24h cooldown.
    Returns (ok=False, retry_after_sec) once the cooldown is in force."""
    if not discord_id or not account_id:
        return True, 0
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM portal_link_attempts
               WHERE discord_id = ? AND account_id = ?
                 AND result = 'fail' AND attempt_at >= ?
                 AND is_test_run = 0""",
            (discord_id, account_id, _iso_minus(PORTAL_RATE_COOLDOWN_HOURS)),
        ).fetchone()
    finally:
        conn.close()
    if row["c"] >= PORTAL_RATE_FAILS_TO_COOLDOWN:
        return False, PORTAL_RATE_COOLDOWN_HOURS * 3600
    return True, 0
