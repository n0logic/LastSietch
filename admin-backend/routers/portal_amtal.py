# Public Amtal surface for the player portal: the PvP kill board, kill streaks and
# titles. Read-only and unauthenticated on purpose: names, counts and spice only,
# staff marked, never an account id.

from fastapi import APIRouter

import feature_flags
from cache import ttl_cache
from relay import call_relay
from routers.dune import STANDINGS_EXCLUDE

router = APIRouter()

# The board and kill streaks read kills the game already records.
_PVP_FLAG = "LASTSIETCH_AMTAL_PVP_ENABLED"
_PVP_TOP = 10
_PVP_COLLECTED = 10


def _staff(name):
    return (name or "").strip().lower() in STANDINGS_EXCLUDE


# PvP titles (ops/amtal-pvp-flag/TITLES-DRAFT.md, trimmed for the small population on
# 2026-10-20). Staff appear on the board but never hold a title (owner 2026-10-20).
SARDAUKAR, KANLY, HEADTAKER, BLOODED = "Sardaukar of the Week", "Kanly", "Headtaker", "Blooded"
SARDAUKAR_MIN_KILLS = 2
KANLY_MIN_STREAK = 3
BLOODED_MIN_KILLS = 10


def pvp_titles(raw):
    """{account_id: [title, ...]} plus the weekly holders, from the telemetry payload.
    Weekly titles come from the last COMPLETED ISO week; nobody holds one in a week
    with no qualifying player. Ties: fewer deaths, then whoever reached it first."""
    held, weekly = {}, {SARDAUKAR: None, KANLY: None}
    eligible = [e for e in raw.get("last_week") or [] if not _staff(e.get("name"))]
    late = float("inf")
    best = sorted((e for e in eligible if (e.get("kills") or 0) >= SARDAUKAR_MIN_KILLS),
                  key=lambda e: (-e["kills"], e.get("deaths") or 0, e.get("reached_at") or late))
    if best:
        weekly[SARDAUKAR] = best[0]
    best = sorted((e for e in eligible if (e.get("best_streak") or 0) >= KANLY_MIN_STREAK),
                  key=lambda e: (-e["best_streak"], e.get("deaths") or 0, e.get("reached_at") or late))
    if best:
        weekly[KANLY] = best[0]
    for title, e in weekly.items():
        if e:
            held.setdefault(e["account_id"], []).append(title)
    for c in raw.get("collected") or []:
        acct = c.get("collector_account_id")
        if acct and not _staff(c.get("collector_name")) and HEADTAKER not in held.get(acct, []):
            held.setdefault(acct, []).append(HEADTAKER)
    for e in raw.get("season") or []:
        if (e.get("kills") or 0) >= BLOODED_MIN_KILLS and not _staff(e.get("name")):
            held.setdefault(e["account_id"], []).append(BLOODED)
    return held, {t: (e["name"] if e else None) for t, e in weekly.items()}


def public_pvp(raw):
    """The public cut of the telemetry /amtal/pvp payload: names (crew marked staff,
    owner 2026-10-20), counts, spice and titles only. Account ids never leave the server."""
    held, weekly = pvp_titles(raw)

    def row(e, keys):
        out = {k: e.get(k) for k in keys}
        out["staff"] = _staff(e.get("name"))
        out["titles"] = list(held.get(e.get("account_id"), []))
        return out

    board_keys = ("name", "kills", "deaths", "kd", "best_streak")
    return {
        "week_label": raw.get("week_label"),
        "season_start": raw.get("season_start"),
        "streak_step": raw.get("streak_step"),
        "streak_spice": raw.get("streak_spice"),
        "titles": {"last_week_label": raw.get("last_week_label"), "weekly": weekly,
                   "rules": {"sardaukar_min_kills": SARDAUKAR_MIN_KILLS, "kanly_min_streak": KANLY_MIN_STREAK,
                             "blooded_min_kills": BLOODED_MIN_KILLS}},
        "season": [row(e, board_keys) for e in (raw.get("season") or [])[:_PVP_TOP]],
        "week": [row(e, board_keys) for e in (raw.get("week") or [])[:_PVP_TOP]],
        "streaks": [row(e, ("name", "kills", "bounty", "since")) for e in raw.get("streaks") or []],
        "collected": [{"victim": c.get("victim_name"), "victim_staff": _staff(c.get("victim_name")),
                       "collector": c.get("collector_name"), "collector_staff": _staff(c.get("collector_name")),
                       "streak": c.get("streak"), "bounty": c.get("bounty"), "at": c.get("occurred_at")}
                      for c in (raw.get("collected") or [])[:_PVP_COLLECTED]],
    }


@ttl_cache("amtal.pvp", ttl=120, max_stale=900)
async def _pvp_payload() -> dict:
    raw = await call_relay("/dune/telemetry/amtal-pvp")
    if not isinstance(raw, dict) or raw.get("available") is False or "season" not in raw:
        # raise, never return: ttl_cache then serves the last good board (max_stale)
        raise RuntimeError("amtal pvp read unavailable")
    return public_pvp(raw)


@router.get("/portal/amtal/pvp")
async def portal_amtal_pvp():
    if not feature_flags.enabled(_PVP_FLAG):
        return {"enabled": False}
    try:
        return {"enabled": True, "available": True, **(await _pvp_payload())}
    except Exception:
        return {"enabled": True, "available": False}
