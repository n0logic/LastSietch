"""The packages the server sent one player (Fremkit wave 12).

Two of them exist. The Welcome Package lands on a new account; the Return
Package lands after 28 days away. Both are delivered in LEGS that complete at
different times and in different places, and until this module there was nowhere
a player could look to find out which legs had landed. The whisper is fired once,
while they are offline, and then it is gone.

Four properties matter more than the feature:

  * A LEG IS READ, NEVER ASSUMED. Every leg state comes off a stamp column or a
    delivery-log row. The one exception is the pre-v1.7 offline lane, which wrote
    straight into the bag and carries no per-leg stamp at all: those legs read
    delivered at `granted_at` because that IS when they landed, and the rule is
    keyed on the pack version so it can never leak onto a v1.7 row.
  * THE EXCHANGE LEG IS NEVER "DELIVERED" ON ITS OWN. Overflow is listed on the
    CHOAM Exchange and stays `pending` forever: the player takes it at a terminal
    and nothing observes that. Calling it delivered would tell 336 live lines'
    worth of players their items had arrived when they are still sitting in a
    terminal. It reads `waiting`, with the copy that says what to press.
  * A NULL IS NOT A ZERO AND NOT A FALSE. An unstamped leg is `pending`, an
    unreadable read is None all the way up, and neither is ever coerced.
  * NOTHING IDENTIFYING LEAVES. No account id, no actor, no controller, no grant
    ids, no fls/funcom ids, and never the raw `notes` text: notes names internal
    lanes and counts ("19 via RMQ + 8 waiting in the exchange"). Notes are READ,
    to pick between two return-package outcomes, and then dropped.

The relay answers; this module shapes. `shape` is pure and is what the suite
executes against the real probe payloads.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from name_lookups import ITEMS as _ITEM_NAMES
from item_icons import icon_for as _icon_for

logger = logging.getLogger("portal")

# --- deliveries model (pure; the suite execs from here) ---------------------- #

# One read per account is trusted for five minutes. A package lands rarely (283
# welcome rows and 97 return rows in the life of the server), the card is on Home
# and therefore on every page load, and the read is four joined tables on the
# game host. The cache is what keeps Home off that box.
CACHE_TTL_S = 300.0
# Bounded on purpose: an unbounded per-account dict in a long-lived process is a
# slow leak keyed by something an attacker with many accounts could grow.
CACHE_MAX = 2000
# Home gathers its nine loaders, so the landing page waits for the slowest one on
# a cold cache. This is deliberately SHORTER than the relay's own 20 second ssh
# budget: the relay answering available:false and this loader giving up both seal
# the card in exactly the same way, so there is nothing to buy by waiting longer
# and a visibly slower Home to lose.
RELAY_TIMEOUT_S = 12.0
# How long a FAILED read is remembered. Home reads this on every poll, so an
# outage without a negative cache is one 12 second ssh attempt per request per
# player, all of them queued behind a box that is already not answering. Short
# on purpose: the card seals for at most half a minute after the relay comes
# back, which is the cheap half of the trade.
FAIL_TTL_S = 30.0

_cache: dict = {}

# The pack version decides where every welcome leg landed, so it is matched
# EXACTLY and never by "is it not the new one". v1.0.1 through v1.6 are the
# offline lane, which applied the whole pack inline at grant time; v1.7 is the
# online lane, which delivers over RMQ and defers research and scrip to a sweep.
#
# Everything else is a THIRD case and it has to stay one. `notes` is NULL on some
# rows, and the re-roll heal writes the transient marker "REGRANT-REROLL-HEAL"
# into it; both used to fall through to the offline rules and report a backpack,
# research and scrip the server may never have sent. An unrecognised version
# reads every leg "unknown", which is the only true answer available.
VERSION_OFFLINE_RE = re.compile(r"^v1\.[0-6]")
VERSION_RMQ_RE = re.compile(r"^v1\.7")

LANE_OFFLINE = "offline"
LANE_RMQ = "rmq"
LANE_UNKNOWN = "unknown"

# The delivery-log lane -> where the player will find the item.
_LANE_WHERE = {"rmq": "backpack", "exchange": "exchange", "db": "backpack"}
# The delivery-log status -> the state word the card renders.
_STATUS_STATE = {"delivered": "delivered", "pending": "pending"}

# Player-facing copy. Sentence case, no em dashes, and the wording for where
# things land is the public site's.
NOTE_EXCHANGE = "Shows as CANCELED. Press Take item at any tradepost terminal."
NOTE_NEXT_LOGOUT = "Lands the next time you log out and back in."
NOTE_TOOLS_TO_BANK = "Your backpack was full, so they wait in the bank."
NOTE_TOOLS_BACKPACK = "In your backpack."
NOTE_WHILE_OFFLINE = "Lands while you are offline."


# The fractional-seconds group of an ISO stamp, padded to microseconds below.
_FRACTION_RE = re.compile(r"\.\d+")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(stamp):
    """A game-host timestamp as an aware datetime, or None. The host emits
    'ISO with zone' but the column type is not ours to depend on, so a naive
    stamp is read as UTC rather than refused.

    psql prints as many fractional digits as the value has, so a real stamp can
    arrive as ".23799". datetime.fromisoformat before 3.11 accepts only three or
    six, and a stamp this refused would silently sort to the bottom and drop a
    skip window, so the fraction is padded to microseconds first."""
    if not stamp:
        return None
    text = str(stamp).strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(lambda m: (m.group(0) + "000000")[:7], text, count=1)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_list(value):
    """A jsonb array of template ids as a list of strings. Anything else is an
    empty list: plan/overflow/db_owed are absent on every pre-v1.7 row."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, (str, int)) and str(v)]


def _leg(key, label, state, at=None, note=None):
    return {"key": key, "label": label, "state": state, "at": at, "note": note}


def _item(template_id, quantity, where, state):
    tid = str(template_id or "")
    return {
        "name": "Solari" if tid == "SolarisCoin" else _ITEM_NAMES.lookup_or_synthesize(tid),
        "icon": _icon_for(tid),
        "quantity": _int_or_none(quantity),
        "where": where,
        "state": state,
    }


def _package_state(legs) -> str:
    """delivered when every leg has landed, pending when none has, partial in
    between. A package with no legs at all has landed nothing."""
    if not legs:
        return "pending"
    done = sum(1 for leg in legs if leg["state"] == "delivered")
    if done == len(legs):
        return "delivered"
    return "pending" if done == 0 else "partial"


def _lane_for(version) -> str:
    """Which delivery lane a welcome row was written by, from its pack version.
    Three answers, and the third is not a fallback for the second."""
    text = str(version or "").strip()
    if VERSION_RMQ_RE.match(text):
        return LANE_RMQ
    if VERSION_OFFLINE_RE.match(text):
        return LANE_OFFLINE
    return LANE_UNKNOWN


def _is_rmq(version) -> bool:
    return _lane_for(version) == LANE_RMQ


def _stamped_leg(key, label, stamp, granted_at, lane, note=None):
    """The three legs that differ only by which column stamps them: on the RMQ
    lane a stamp is the proof, on the offline lane the grant itself is (that lane
    applied them inline with no column to write), and on an unrecognised version
    there is no proof either way."""
    if lane == LANE_UNKNOWN:
        return _leg(key, label, "unknown")
    if lane == LANE_OFFLINE:
        return _leg(key, label, "delivered", granted_at)
    if stamp:
        return _leg(key, label, "delivered", stamp)
    return _leg(key, label, "pending", None, note)


def _welcome_legs(welcome, lines, overflow):
    lane = _lane_for(welcome.get("version"))
    granted_at = welcome.get("granted_at")
    legs = []

    if lane == LANE_UNKNOWN:
        legs.append(_leg("backpack", "Your backpack", "unknown"))
    elif lane == LANE_RMQ:
        rmq_at = welcome.get("rmq_delivered_at")
        legs.append(_leg("backpack", "Your backpack",
                         "delivered" if rmq_at else "pending", rmq_at or None))
    else:
        legs.append(_leg("backpack", "Your backpack", "delivered", granted_at))

    exchange_lines = [l for l in lines if l.get("lane") == "exchange"]
    if overflow or exchange_lines:
        # Never "delivered" unless a log line says so, and every line has to say
        # so: one taken item out of eight is not a finished leg.
        taken = bool(exchange_lines) and all(
            l.get("status") == "delivered" for l in exchange_lines)
        if lane == LANE_UNKNOWN:
            legs.append(_leg("exchange", "CHOAM Exchange, Completed tab", "unknown"))
        else:
            legs.append(_leg("exchange", "CHOAM Exchange, Completed tab",
                             "delivered" if taken else "waiting",
                             welcome.get("exchange_listed_at"),
                             None if taken else NOTE_EXCHANGE))

    legs.append(_stamped_leg("research", "Base Construction research",
                             welcome.get("research_applied_at"), granted_at, lane,
                             NOTE_NEXT_LOGOUT))
    legs.append(_stamped_leg("scrip", "100,000 House Scrip",
                             welcome.get("scrip_applied_at"), granted_at, lane,
                             NOTE_NEXT_LOGOUT))
    # Intel is the one leg the sweep has always applied, on both known lanes, so
    # it reads its own stamp whatever the pack version said. On an unrecognised
    # version it reads unknown with the rest: a stamp proves the sweep ran, but
    # nothing here proves this row was ever owed an intel leg at all.
    intel_at = welcome.get("intel_applied_at")
    if lane == LANE_UNKNOWN:
        legs.append(_leg("intel", "100 Intel points", "unknown"))
    else:
        legs.append(_leg("intel", "100 Intel points",
                         "delivered" if intel_at else "pending", intel_at or None,
                         None if intel_at else NOTE_NEXT_LOGOUT))
    return legs


def _welcome_items(welcome, lines):
    """The item list, which only the RMQ lane can produce: the older offline lane
    wrote no delivery-log rows, so its package carries a count and no list rather
    than a list this module invented."""
    if not _is_rmq(welcome.get("version")):
        return []
    items = []
    for line in lines:
        lane = str(line.get("lane") or "")
        where = _LANE_WHERE.get(lane, "unknown")
        state = _STATUS_STATE.get(str(line.get("status") or ""), "unknown")
        if lane == "exchange" and state == "pending":
            state = "waiting"
        items.append(_item(line.get("template_id"), line.get("quantity"), where, state))
    for tid in _text_list(welcome.get("db_owed")):
        items.append(_item(tid, None, "backpack", "pending"))
    return items


def _welcome_package(welcome):
    lines = [l for l in (welcome.get("lines") or []) if isinstance(l, dict)]
    overflow = _text_list(welcome.get("overflow"))
    legs = _welcome_legs(welcome, lines, overflow)
    return {
        "kind": "welcome",
        "label": "Welcome Package",
        "granted_at": welcome.get("granted_at"),
        "items_total": _int_or_none(welcome.get("granted_items")),
        "state": _package_state(legs),
        "legs": legs,
        "items": _welcome_items(welcome, lines),
    }


def _return_package(row):
    tier = _int_or_none(row.get("tier"))
    granted_at = row.get("granted_at")
    bank_at = row.get("bank_done_at")
    tools_at = row.get("tools_done_at")
    # The one thing `notes` is read for: which of the two tool outcomes happened.
    # The text itself never leaves this function.
    tools_to_bank = "to bank" in str(row.get("notes") or "").lower()

    legs = [_leg("bank", "Your CHOAM bank",
                 "delivered" if bank_at else "pending", bank_at or None)]
    if tools_at:
        legs.append(_leg("tools", "Base and vehicle tools", "delivered", tools_at,
                         NOTE_TOOLS_TO_BANK if tools_to_bank else NOTE_TOOLS_BACKPACK))
    else:
        legs.append(_leg("tools", "Base and vehicle tools", "pending", None,
                         NOTE_WHILE_OFFLINE))

    bank_state = "delivered" if bank_at else "pending"
    tools_state = "delivered" if tools_at else "pending"
    tools_where = "bank" if tools_to_bank else "backpack"
    items = [_item(it.get("template_id"), it.get("quantity"), "bank", bank_state)
             for it in (row.get("bank_items") or []) if isinstance(it, dict)]
    items += [_item(it.get("template_id"), it.get("quantity"), tools_where, tools_state)
              for it in (row.get("tool_items") or []) if isinstance(it, dict)]

    label = "Return Package" if tier is None else "Return Package (Tier %d)" % tier
    return {
        "kind": "return",
        "label": label,
        "granted_at": granted_at,
        "items_total": len(items) or None,
        "state": _package_state(legs),
        "legs": legs,
        "items": items,
    }


def _skip(raw_skip, now):
    """The fresh-start cooldown notice: a re-roll inside the 30-day identity
    window, and the date it runs out.

    A window that has already closed is DROPPED, not shown expired. The row is
    permanent (34 of them going back to the spring) and the player it belongs to
    is long past the cooldown; a card that keeps telling them about a wait that
    ended in July is worse than no card. A row whose window cannot be resolved at
    all, because either half is missing, is dropped for the same reason: there is
    no date to promise."""
    if not isinstance(raw_skip, dict):
        return None
    notified_at = raw_skip.get("notified_at")
    days = _int_or_none(raw_skip.get("remaining_days_at_skip"))
    when = _parse(notified_at)
    if when is None or days is None:
        return None
    eligible = when + timedelta(days=days)
    if eligible <= now:
        return None
    return {"notified_at": notified_at, "eligible_at": eligible.isoformat()}


def _sort_key(package):
    when = _parse(package.get("granted_at"))
    return when or datetime.min.replace(tzinfo=timezone.utc)


def summary(shaped) -> dict:
    """{count, pending_count, latest}: the three numbers the Home card renders,
    computed off the shaped packages so the card and the page cannot disagree."""
    packages = (shaped or {}).get("packages") or []
    latest = None
    if packages:
        head = packages[0]
        latest = {"kind": head["kind"], "label": head["label"],
                  "granted_at": head["granted_at"], "state": head["state"]}
    return {
        "count": len(packages),
        "pending_count": sum(1 for p in packages if p["state"] != "delivered"),
        "latest": latest,
    }


def unavailable() -> dict:
    """The sealed answer. Not an empty one: packages [] with available true says
    'nothing was sent to you', which is a different sentence."""
    return {"available": False, "read_at": _iso_now(), "packages": [],
            "skip": None, "summary": None}


def shape(raw, *, now=None) -> dict:
    """The relay payload as the portal contract. Pure apart from the clock, which
    is injectable: no cache, no I/O. `now` decides whether a fresh-start cooldown
    is still running. This is the function the suite runs the real probe payloads
    through."""
    if not isinstance(raw, dict) or raw.get("available") is False:
        return unavailable()
    now = now or datetime.now(timezone.utc)

    packages = []
    welcome = raw.get("welcome")
    if isinstance(welcome, dict):
        packages.append(_welcome_package(welcome))
    for row in (raw.get("returns") or []):
        if isinstance(row, dict):
            packages.append(_return_package(row))
    packages.sort(key=_sort_key, reverse=True)

    shaped = {
        "available": True,
        "read_at": _iso_now(),
        "packages": packages,
        "skip": _skip(raw.get("welcome_skip"), now),
        "summary": None,
    }
    shaped["summary"] = summary(shaped)
    return shaped


# --- the cached relay read --------------------------------------------------- #

def _remember(key, value, clock, ttl):
    """Store one account's read for `ttl` seconds. A shaped dict is an answer
    and lives for CACHE_TTL_S; None is a failure and lives for FAIL_TTL_S.

    When the bound is reached the entries closest to expiry go first, so a full
    cache still turns over instead of freezing on whoever got there first."""
    if key not in _cache and len(_cache) >= CACHE_MAX:
        doomed = sorted(_cache, key=lambda k: _cache[k][0])[:max(1, CACHE_MAX // 10)]
        for stale in doomed:
            _cache.pop(stale, None)
    _cache[key] = (clock + ttl, value)


def forget(account_id):
    """Drop one account's cached read. Nothing calls this on a schedule; it is
    here so a future writer that lands a package can invalidate its own row."""
    _cache.pop(int(account_id), None)


async def load(account_id, now=None):
    """The shaped deliveries for one account, or None when the read failed.

    None is the only failure value, and it is remembered too, for FAIL_TTL_S
    rather than the full CACHE_TTL_S. Home reads this on every poll: without a
    negative cache an outage means a fresh 12 second ssh attempt per request per
    player against a box that is already not answering, and with the full TTL it
    would seal the card for five minutes after the relay came back. Thirty
    seconds is the interval that costs neither. A successful read (including a
    genuinely empty one) is cached for CACHE_TTL_S."""
    key = int(account_id)
    clock = time.monotonic() if now is None else float(now)
    hit = _cache.get(key)
    if hit is not None and hit[0] > clock:
        return hit[1]

    from relay import call_relay
    try:
        raw = await call_relay("/dune/player/%d/deliveries" % key,
                               timeout=RELAY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - a sealed panel, never a failed page
        # No traceback, and the exception TYPE rather than its text. Home reads
        # this on every poll, so an outage would otherwise write one traceback
        # per request per player; and the relay puts the failing path, which
        # carries the account id, into its own error detail.
        logger.warning("portal: deliveries read failed (%s)", type(exc).__name__)
        _remember(key, None, clock, FAIL_TTL_S)
        return None
    if not isinstance(raw, dict) or raw.get("available") is False:
        logger.warning("portal: deliveries unavailable from the relay")
        _remember(key, None, clock, FAIL_TTL_S)
        return None

    shaped = shape(raw)
    _remember(key, shaped, clock, CACHE_TTL_S)
    return shaped
