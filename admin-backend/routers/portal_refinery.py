"""The Ingot Refinery: the portal half of the offline ingot-to-dust service (wave 10).

A linked player, logged out of the game, hands in base refined ingots plus Spice
Melange from the CHOAM bank, the backpack or the toolbar, and receives the
matching Spice-infused metal dust in the bank. Two routes: a read that prices
the trade, and a write that makes it.

Four properties matter more than the feature:

  * THE RATE LIVES ON THE GAME HOST, ONCE. scripts/refinery-rates.json ships
    beside the writer and is read fresh per call. This module carries NO copy of
    it: the catalog route reads the table through the relay and caches it for 60
    seconds per account, and every exchange echoes the rate it was shown. A
    mismatch is refused `rate_changed` rather than filled at whichever number
    happened to be newer. Two copies of a rate table is two rates.
  * THE CAP IS KEYED ON THE DISCORD IDENTITY. 250 dusts of each tier per rolling
    seven days, six independent counters, summed over units and not counted over
    rows. See portal_refinery_events for both reasons; the important one here is
    that the cap is reserved BEFORE the relay call and settled after, so two
    concurrent submits cannot both pass a cap neither has breached yet.
  * OFFLINE ONLY, AND THE EDGE IS NOT THE GATE. Taking an item out of an
    inventory the engine holds in RAM is a duplication path, so the writer
    hard-gates on online_status plus the reconnect grace under a row lock. This
    module refuses only on `_resolve_online(...) is True`, exactly as the
    storage and repair paths do. An UNDETERMINED status is deliberately NOT
    refused here; tightening the edge would only hide the authoritative gate.
  * TWO KILL SWITCHES, BOTH DARK BY DEFAULT. LASTSIETCH_REFINERY_ENABLED gates whether
    the portal offers the door at all; /etc/lastsietch/refinery-enabled on the game
    host is what the writer itself reads and is the real switch. While either is
    off the exchange answers {ok:false, error:"refinery_disabled"} at HTTP 200,
    the storage family's soft shape, so the page renders "not open yet" and
    never a toast and never a success.

owner_ctrl and account_id are resolved SERVER-SIDE from the session and are
never read from the browser. Nothing here reads dune.*; every game fact arrives
through the relay.
"""
import json
import logging
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

import portal_refinery_events
from portal_auth import SESSION_COOKIE, client_ip, csrf_for_session
from routers.portal import (
    _GUILD_OP_UUID_RE,
    _require_linked_session_json,
    _resolve_buyer_ctrl_and_bank,
    _resolve_online,
    _selected_ctrl,
    _storage_rate_ok,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()

# The outer wall, not the product ceiling: see portal_refinery_events. The real
# per-request limit is the rate table's max_batches_per_request, read off the
# catalog per request by _max_batches() so a retune lands without a deploy.
SAFETY_MAX_BATCHES = portal_refinery_events.SAFETY_MAX_BATCHES_PER_REQUEST
WINDOW_DAYS = portal_refinery_events.WINDOW_DAYS
WEEKLY_DUST_CAP = portal_refinery_events.WEEKLY_DUST_CAP

# Template ids are CamelCase in dune.items and compared case-sensitively all the
# way down. This gate only keeps junk off the wire; the writer matches the real
# thing against its own rate table under a lock.
_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
# One idempotency key per intended exchange, reused across retries so a lost
# response replays instead of refining twice. Validated with the SAME regex the
# guild, pawn-move and transfer paths use (routers.portal._GUILD_OP_UUID_RE), so
# there is one definition of "a UUID" on this side rather than a second one that
# can drift from the relay's.

_EXPECTED_TEMPLATE_FIELDS = ("expected_input_template", "expected_spice_template")
_EXPECTED_COUNT_FIELDS = ("expected_input_per_batch", "expected_spice_per_batch",
                          "expected_output_per_batch")
# The page echoes the catalog's rate_version too. The five fields above pin what
# a batch COSTS; this pins everything else the rate table can move (max_stack,
# the weekly cap, the window), which a page sitting open would otherwise never
# notice had changed under it.
# Bound and charset are the WRITER's, which validates ^[A-Za-z0-9._-]{1,32}$.
# Three layers disagreeing about the same field is how a value gets accepted at
# the edge, forwarded, and then refused at the far end with a message nobody
# expected. fullmatch, not match: `$` also matches before a trailing newline.
_RATE_VERSION_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")


def _refinery_enabled() -> bool:
    """Portal-side kill switch for the Ingot Refinery. Default OFF.

    Mirrors /etc/lastsietch/refinery-enabled on the game host, which is what the writer
    itself gates on: this copy only decides whether the portal offers the door,
    and is NOT the security boundary. Both must be on for a trade to land.

    Resolved by feature_flags, re-read on every call and never memoised: the
    data/feature_flags.json override wins, then the process environment
    (os.environ.get("LASTSIETCH_REFINERY_ENABLED", "0") == "1"), then the coded default
    OFF. The owner-only Systems toggle writes that override, so a flip lands on
    the next request without restarting lastsietch-admin."""
    import feature_flags
    return feature_flags.enabled("LASTSIETCH_REFINERY_ENABLED", "0")


# --------------------------------------------------------------------------- #
# Player-facing copy. The writer's own messages are operator-facing and name
# inventory and account ids, so none of them is ever surfaced: every refusal
# below is this module's own sentence, chosen by the writer's stable token.
# --------------------------------------------------------------------------- #

_TEXT = {
    "unauthenticated": "Sign in to the portal to use the Ingot Refinery.",
    "csrf": "Invalid CSRF token",
    "bad_request": "That refining request was malformed.",
    "unknown_recipe": "We do not refine that ingot. Refresh and try again.",
    "rate_changed": "The refining rate changed while this page was open. Refresh "
                    "to see the new rate. Nothing was taken.",
    "refinery_disabled": "The Ingot Refinery is not open yet. Nothing was taken "
                         "and nothing was made.",
    "recipe_disabled": "That tier is not open yet. The other tiers still refine.",
    "player_online": "Log out of the game first, then refine. The refinery only "
                     "works on storage nobody is holding open. If you already "
                     "logged out, give the server a minute to catch up, then "
                     "retry.",
    "rate_limited": "One batch at a time, please. Wait a moment and retry.",
    "no_bank": "We could not find this character's CHOAM bank. Open the bank "
               "in-game once, then retry.",
    "no_pawn_storage": "We could not read this character's backpack and toolbar. "
                       "Refresh and try again.",
    "insufficient_input": "Not enough {material} for that. You have {held} across "
                          "your bank, backpack and toolbar; this needs {need}.",
    "insufficient_spice": "Not enough Spice Melange for that. You have {held} "
                          "across your bank, backpack and toolbar; this needs "
                          "{need}.",
    "bank_full": "Your CHOAM bank has no room for the dust. Free a slot and try "
                 "again. Nothing was taken.",
    "bank_volume": "Your CHOAM bank does not have room for that much dust. "
                   "Refine a smaller batch. Nothing was taken.",
    "idempotency_conflict": "That refining request was already used for a "
                            "different order.",
    "unresolved": "We could not verify your in-game character right now. Please "
                  "try again in a moment.",
    "unavailable": "The refinery is unavailable right now. Please try again "
                   "shortly.",
    "write_failed": "The refining could not be completed. Nothing was taken. "
                    "Please try again.",
}

# The weekly-cap sentence promises no reset time on purpose: a rolling window
# has none, and a countdown would have to be seconds until the oldest counted
# row ages out rather than a clock hour.
_WEEKLY_CAP_TEXT = ("You have refined all the {dust} you can this week. Your "
                    "allowance for it frees up as your last batches age past "
                    "seven days. The other tiers are unaffected.")

# The writer's fourteen tokens, mapped onto the portal's own. The writer names a
# refusal for the operator (`no_space`); the portal names it for the player
# ("free a slot"). Every key here is a token that can actually arrive: the
# writer emits the first fourteen and the relay emits writer_no_output.
#
# The seven superseded spellings from plan section 5 are deliberately absent,
# and the grep that proves it is the point. Carrying a dead alias is not free:
# one of them had a live token as a PREFIX of itself, which is the pair the
# architect banned for misrouting, and the rest would have quietly documented a
# contract the writer does not have.
#
# An unmapped token falls through to write_failed, whose copy says nothing was
# taken. That is safe by construction only because every refusal above is
# all-or-nothing in the writer's transaction; if that ever stops being true,
# this fallthrough becomes a lie.
_TOKEN_MAP = {
    "refinery_disabled": "refinery_disabled",
    "recipe_disabled": "recipe_disabled",
    "unknown_recipe": "unknown_recipe",
    "rate_changed": "rate_changed",
    "bad_request": "bad_request",
    "player_online": "player_online",
    "no_bank": "no_bank",
    "no_pawn_storage": "no_pawn_storage",
    "insufficient_input": "insufficient_input",
    "insufficient_spice": "insufficient_spice",
    "no_space": "bank_full",
    "weekly_cap": "weekly_cap",
    "idempotency_conflict": "idempotency_conflict",
    # The writer catching its own conservation invariant failing and rolling
    # back is a bug on its side, not a thing to explain to a player. Mapped
    # explicitly rather than left to the fallthrough, so the intent is on the
    # record: nothing was taken is the true sentence here.
    "conservation_failed": "write_failed",
    # The RELAY's token, not the writer's: the forced command produced no
    # parseable JSON.
    "writer_no_output": "write_failed",
    # Live: the writer's fifteenth token. It fires only when the bank has a
    # positive max_item_volume AND the dust's unit volume is known AND the mint
    # would exceed the cap, so it is always a genuine will-not-fit and never a
    # could-not-tell. That is what makes "refine a smaller batch" honest advice
    # rather than a guess. A bank with no volume cap is not gated at all, and an
    # unknown unit volume allows the trade and flags it (see volume_unverified).
    "bank_volume": "bank_volume",
}

# This is a V2 JSON route: a malformed or unknown request is a 400 carrying the
# refusal envelope, never a 200. Only the three SOFT states answer 200, and each
# of them is a state the page renders rather than an error it toasts. 409 is
# reserved for "the world changed under you", which is the class the page offers
# a refresh for.
# R2-M1. WRITER tokens that PROVE nothing was taken, so the reservation can be
# released and the player's weekly allowance handed straight back.
#
# Everything absent from this set is INDETERMINATE and leaves the reservation
# pending to age out at PENDING_TTL_SECONDS: write_failed and writer_no_output
# mean the writer's answer could not be read, and psql exiting 0 with unparseable
# stdout can happen after the COMMIT. conservation_failed IS here despite mapping
# to the same player-facing copy, because it is a RAISE inside the transaction
# and therefore rolls back: the writer proved the trade did not land.
#
# rate_limited and the other edge-only refusals never reach here, since nothing
# was reserved before them; they are listed for the page's NOTHING_TAKEN set,
# not for this one.
_NOTHING_TAKEN_TOKENS = frozenset({
    "recipe_disabled", "unknown_recipe", "rate_changed", "bad_request",
    "player_online", "weekly_cap", "no_bank", "no_pawn_storage",
    "insufficient_input", "insufficient_spice", "no_space", "bank_volume",
    "idempotency_conflict", "conservation_failed",
})

_STATUS = {
    "refinery_disabled": 200,
    "recipe_disabled": 200,
    "write_failed": 200,
    "bad_request": 400,
    "unknown_recipe": 400,
    "unauthenticated": 401,
    "csrf": 403,
    "player_online": 409,
    "insufficient_input": 409,
    "insufficient_spice": 409,
    "no_bank": 409,
    "no_pawn_storage": 409,
    "bank_full": 409,
    "bank_volume": 409,
    "rate_changed": 409,
    "idempotency_conflict": 409,
    "rate_limited": 429,
    "weekly_cap": 429,
    "unresolved": 502,
    "unavailable": 503,
}


def _err(token: str, message: Optional[str] = None):
    return _v2_err(token, message or _TEXT.get(token, _TEXT["write_failed"]),
                   status=_STATUS.get(token, 400))


# --------------------------------------------------------------------------- #
# Names and icons. The catalog file's `id` is the canonical case for every
# template id in this feature (three of the six inputs are traps if spelled from
# the display name), and it is also where the player-facing names come from.
# --------------------------------------------------------------------------- #

_ITEM_CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "dune-give-item-catalog.json")
_ITEM_CATALOG_CACHE = {"mtime": None, "by_id": {}}


def _item_catalog() -> dict:
    """template_id -> {name, max_stack}, refreshed when the file changes. A
    missing or corrupt catalog degrades to empty rather than 500ing: the page
    then shows template ids as names, which is ugly and still true."""
    try:
        mtime = os.path.getmtime(_ITEM_CATALOG_PATH)
        if _ITEM_CATALOG_CACHE["mtime"] == mtime:
            return _ITEM_CATALOG_CACHE["by_id"]
        with open(_ITEM_CATALOG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("refinery: item catalogue unavailable: %s", exc)
        return _ITEM_CATALOG_CACHE["by_id"]
    by_id = {}
    for row in raw.get("items") or []:
        template_id = row.get("id")
        if not isinstance(template_id, str) or not _TEMPLATE_RE.fullmatch(template_id):
            continue
        try:
            max_stack = max(1, int(row.get("pak_max_stack") or 1))
        except (TypeError, ValueError):
            max_stack = 1
        by_id[template_id] = {"name": str(row.get("name") or template_id)[:120],
                              "max_stack": max_stack}
    _ITEM_CATALOG_CACHE.update({"mtime": mtime, "by_id": by_id})
    return by_id


def _display_name(template_id: Optional[str]) -> str:
    if not isinstance(template_id, str) or not template_id:
        return "that material"
    entry = _item_catalog().get(template_id)
    return entry["name"] if entry else template_id


def _icon(template_id: Optional[str]) -> str:
    if not isinstance(template_id, str) or not template_id:
        return ""
    try:
        import item_icons
        return item_icons.icon_for(template_id) or ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# The catalog read, cached 60 s per (account, character).
# --------------------------------------------------------------------------- #

_CATALOG_TTL_SECONDS = 60.0
# One entry per (account, controller) actively looking at the page. Bounded so a
# long-lived process cannot accumulate a row per player who ever loaded it.
_CATALOG_CACHE_MAX = 512
_catalog_cache: dict = {}


def _catalog_key(account_id: int, owner_ctrl: int) -> tuple:
    return (int(account_id), int(owner_ctrl))


def _catalog_cached(key: tuple):
    hit = _catalog_cache.get(key)
    if hit is None:
        return None
    if (time.monotonic() - hit[0]) >= _CATALOG_TTL_SECONDS:
        _catalog_cache.pop(key, None)
        return None
    return hit[1]


def _catalog_store(key: tuple, payload: dict) -> None:
    if len(_catalog_cache) >= _CATALOG_CACHE_MAX:
        cutoff = time.monotonic() - _CATALOG_TTL_SECONDS
        for stale in [k for k, v in _catalog_cache.items() if v[0] <= cutoff]:
            _catalog_cache.pop(stale, None)
        if len(_catalog_cache) >= _CATALOG_CACHE_MAX:
            _catalog_cache.clear()
    _catalog_cache[key] = (time.monotonic(), payload)


def _catalog_drop(key: tuple) -> None:
    _catalog_cache.pop(key, None)


async def _relay_catalog(account_id: int, owner_ctrl: int, use_cache: bool = True):
    """The rate table plus this character's holdings, read through the relay.

    Only a successful read of an OPEN refinery is cached. Two things are
    deliberately not:
      a refusal, which is a real answer but is not a rate table;
      a dark read (ok true, enabled false), because caching one would leave the
      page sealed for up to a minute after the owner flips the host flag on, and
      a kill switch that takes a minute to come back reads as a broken deploy.
    Caching a dark read also costs nothing to skip: it carries no holdings, so
    there is no expensive work being repeated."""
    key = _catalog_key(account_id, owner_ctrl)
    if use_cache:
        hit = _catalog_cached(key)
        if hit is not None:
            return hit
    try:
        from relay import call_relay
        payload = await call_relay(
            "/dune/refinery/catalog", method="POST",
            json_body={"op": "catalog", "owner_ctrl": int(owner_ctrl),
                       "account_id": int(account_id)},
            timeout=30)
    except HTTPException as exc:
        logger.warning("refinery: catalog relay error acct=%s: %s",
                       account_id, exc.detail)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("refinery: catalog read failed acct=%s: %s", account_id, exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") and payload.get("enabled") is True:
        _catalog_store(key, payload)
    return payload


_HELD_UNKNOWN = {"bank": None, "backpack": None, "toolbar": None, "total": None}


def _held(raw, unread: bool = False) -> dict:
    """One holdings block, normalised to the four keys the page reads. The
    toolbar never appears in the container browser, so these three numbers are
    summed SERVER-SIDE and handed over as one shape: a client that fanned out
    per source would be back at the 55-request hang of 2026-08-02.

    NULL IS NOT ZERO. While the refinery is dark the game host opens no DB
    session at all, so a holding is UNKNOWN rather than empty, and the writer
    says so with holdings_read:false. Coercing that to 0 would publish "you hold
    no Copper Ingot" about a bank nobody looked in, which is the same confident
    wrong number home.svelte.js keeps null all the way to the verdict rather
    than letting `?? 0` turn "we could not read this" into "you have none". The
    sibling readers _bank and _sources already preserve unknowns; this one used
    to be the odd one out."""
    if unread or not isinstance(raw, dict):
        return dict(_HELD_UNKNOWN)

    def _n(key):
        try:
            return max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    bank, backpack, toolbar = _n("bank"), _n("backpack"), _n("toolbar")
    total = raw.get("total")
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = bank + backpack + toolbar
    return {"bank": bank, "backpack": backpack, "toolbar": toolbar,
            "total": max(0, total)}


def _held_output_units(recipe: dict, unread: bool = False):
    """How much of this dust the player already holds, as ONE number.

    The writer sends held_output as a {bank, backpack, toolbar, total} object
    like the other two, plus a flat held_output_units. The page binds a scalar,
    so the flat field wins and the object's total is the fallback. Reading the
    object straight through would have rendered a dict where a count belongs.

    None when the holdings were never read, for the same reason as _held."""
    if unread:
        return None
    flat = recipe.get("held_output_units")
    if not isinstance(flat, bool) and isinstance(flat, int):
        return max(0, flat)
    raw = recipe.get("held_output")
    if isinstance(raw, dict):
        return _held(raw)["total"]
    if raw is None:
        return None
    return _pos(raw, 0)


def _max_batches(payload: Optional[dict]) -> int:
    """The per-request batch ceiling THE RATE TABLE published, clamped to the
    safety bound. The rate table is the single authority for what the service
    will trade, this included, so the owner retunes it in one file and both the
    stepper and the refusal follow. A host that publishes no ceiling leaves only
    the safety bound standing, which is the honest outcome: we do not invent a
    product limit the owner did not set."""
    raw = payload if isinstance(payload, dict) else {}
    value = raw.get("max_batches_per_request")
    if value is None:
        caps = raw.get("caps")
        if isinstance(caps, dict):
            value = caps.get("max_batches_per_request")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return SAFETY_MAX_BATCHES
    return min(value, SAFETY_MAX_BATCHES)


def _pos(raw, default=0) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _affordable(recipe: dict, held_input: dict, held_spice: dict,
                dust_left: int, output_room: Optional[int], max_batches: int):
    """How many batches the player could ask for right now. ADVISORY: the writer
    recomputes every one of these under its own locks, and its answer wins.

    None, never 0, when the holdings behind it were not read: "you can afford
    none" is a claim about the player's own stock and must not be manufactured
    from an absence."""
    if held_input["total"] is None or held_spice["total"] is None:
        return None
    per_input = _pos(recipe.get("input_per_batch"), 0)
    per_spice = _pos(recipe.get("spice_per_batch"), 0)
    per_output = _pos(recipe.get("output_per_batch"), 1)
    # The rate table's ceiling, passed in rather than read from a constant: the
    # stepper must never offer a batch count the exchange would refuse.
    limits = [max_batches]
    if per_input > 0:
        limits.append(held_input["total"] // per_input)
    if per_spice > 0:
        limits.append(held_spice["total"] // per_spice)
    limits.append(max(0, dust_left) // per_output)
    if output_room is not None:
        limits.append(max(0, output_room) // per_output)
    return max(0, min(limits))


def _shape_recipes(payload: Optional[dict], actor_identity: str) -> list:
    """The six recipe rows, the host's rate table merged with this identity's six
    weekly counters. The counters are ours; everything else is the host's."""
    raw = (payload or {}).get("recipes")
    if not isinstance(raw, list):
        return []
    # The writer's own verdict on whether it read anything. Explicit False is
    # the dark case; a payload that omits the flag entirely is an older shape,
    # so fall back to whether the fields themselves arrived rather than nulling
    # holdings a writer did in fact send.
    unread = (payload or {}).get("holdings_read") is False
    max_batches = _max_batches(payload)
    rows = [r for r in raw if isinstance(r, dict)
            and isinstance(r.get("output_template"), str)
            and _TEMPLATE_RE.fullmatch(r["output_template"])]
    counters = portal_refinery_events.caps(
        actor_identity, [r["output_template"] for r in rows])

    out = []
    for r in rows:
        output_template = r["output_template"]
        input_template = r.get("input_template")
        spice_template = r.get("spice_template")
        counter = counters.get(output_template) or {
            "weekly_dust_cap": WEEKLY_DUST_CAP,
            "weekly_dust_used": 0,
            "weekly_dust_left": WEEKLY_DUST_CAP,
        }
        held_input = _held(r.get("held_input"), unread)
        held_spice = _held(r.get("held_spice"), unread)
        output_room = r.get("output_room_units")
        output_room = None if output_room is None else _pos(output_room, 0)
        out.append({
            "tier": _pos(r.get("tier"), 0),
            "output_template": output_template,
            "output_name": _display_name(output_template),
            "output_icon": _icon(output_template),
            "input_template": input_template,
            "input_name": _display_name(input_template),
            "input_icon": _icon(input_template),
            "spice_template": spice_template,
            "spice_name": _display_name(spice_template),
            "spice_icon": _icon(spice_template),
            "input_per_batch": _pos(r.get("input_per_batch"), 0),
            "spice_per_batch": _pos(r.get("spice_per_batch"), 0),
            "output_per_batch": _pos(r.get("output_per_batch"), 1),
            "max_stack": _pos(r.get("max_stack"), 1),
            "held_input": held_input,
            "held_spice": held_spice,
            "held_output": _held_output_units(r, unread),
            "max_batches_affordable": _affordable(
                r, held_input, held_spice, counter["weekly_dust_left"],
                output_room, max_batches),
            # PRIMARY counters: our own identity-keyed SQLite sums. The writer
            # sends its account-keyed backstop as weekly_dust_used_account and
            # weekly_dust_left_account; those are deliberately NOT copied here.
            # For a player with linked alts the account number is the looser of
            # the two, and showing it would promise an allowance the cap that
            # actually decides the trade will refuse.
            "weekly_dust_cap": counter["weekly_dust_cap"],
            "weekly_dust_used": counter["weekly_dust_used"],
            "weekly_dust_left": counter["weekly_dust_left"],
            # `is True`, never `is not False`: this is the per-tier kill
            # switch, so an absent or null value must read as CLOSED. Failing
            # open here would trade a tier the owner had turned off.
            "enabled": r.get("enabled") is True,
        })
    return out


def _consumed(raw) -> dict:
    """What the trade took, keyed by TEMPLATE ID: {units, from {bank, backpack,
    toolbar}}. Shaped here rather than passed through, so the page always gets
    the four keys it binds to even if the writer answers with a thinner row.

    The per-source breakdown is the same reason build_withdraw_sql returns
    dst_inv and dst_slot: a player who was told the drain order is bank, then
    backpack, then toolbar is owed the proof of which one it actually came out
    of."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for template, entry in raw.items():
        if not isinstance(template, str) or not _TEMPLATE_RE.fullmatch(template):
            continue
        entry = entry if isinstance(entry, dict) else {}
        sources = _held(entry.get("from"))
        units = entry.get("units")
        if isinstance(units, bool) or not isinstance(units, int):
            units = sources["total"]
        out[template] = {
            "units": max(0, units),
            "from": {"bank": sources["bank"], "backpack": sources["backpack"],
                     "toolbar": sources["toolbar"]},
        }
    return out


def _granted(raw, output_template: str) -> dict:
    """What the trade made: {template, units, stacks [{item_id, slot,
    stack_size, merged}]}. `merged` is the fact the page needs and the id alone
    cannot carry: whether the dust landed on an existing stack or opened a new
    slot."""
    raw = raw if isinstance(raw, dict) else {}
    stacks = []
    for one in raw.get("stacks") or []:
        if not isinstance(one, dict):
            continue
        stacks.append({"item_id": one.get("item_id"),
                       "slot": one.get("slot"),
                       "stack_size": one.get("stack_size"),
                       "merged": bool(one.get("merged"))})
    template = raw.get("template")
    units = raw.get("units")
    return {
        "template": template if isinstance(template, str) else output_template,
        "units": None if isinstance(units, bool) or not isinstance(units, int) else units,
        "stacks": stacks,
    }


def _bank(payload: Optional[dict]) -> dict:
    raw = (payload or {}).get("bank")
    raw = raw if isinstance(raw, dict) else {}
    return {"inv_id": raw.get("inv_id"), "mic": raw.get("mic"),
            "used_slots": raw.get("used_slots"), "free_slots": raw.get("free_slots")}


def _sources(payload: Optional[dict]) -> dict:
    raw = (payload or {}).get("sources")
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for kind in ("bank", "backpack", "toolbar"):
        one = raw.get(kind)
        one = one if isinstance(one, dict) else {}
        out[kind] = {"inv_id": one.get("inv_id"),
                     "count": one.get("count"),
                     # reachable is the exchange's own rule restated: exactly one
                     # resolved inventory of that type. A toolbar resolving to
                     # zero is simply not a source; a bank or backpack that is
                     # not exactly one is what the writer refuses as no_bank or
                     # no_pawn_storage.
                     "reachable": bool(one.get("reachable"))}
    return out


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.get("/portal/refinery/v2/catalog")
async def portal_refinery_v2_catalog(request: Request):
    """What the Ingot Refinery will trade, for the selected character, right now.

    Read-only: linked session, no CSRF. The rate table and the holdings come from
    the game host through the relay (cached 60 s per character); the six weekly
    counters come from admin.db and are merged in here.

    `enabled` is the AND of both kill switches: this process's gate and the game
    host's own. The page seals on `enabled`, never on `ok` and never on an error
    string: a dark host answers ok:true with enabled:false, holdings_read:false
    and the full recipe list, because reading the rate table opens no DB session
    and a read that genuinely succeeded must not report itself as a failure.
    While dark, holdings, sources and bank are absent and holdings_read says so,
    which is what stops the page rendering "you hold 0" as a fact."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return _err("unauthenticated")
    _, discord_id, active_account_id, row = gate

    owner_ctrl, _bank_solari = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    online = await _resolve_online(active_account_id)

    payload = None
    if owner_ctrl is not None:
        payload = await _relay_catalog(active_account_id, owner_ctrl)

    payload_dict = payload if isinstance(payload, dict) else {}
    session_token = request.cookies.get(SESSION_COOKIE, "")
    return _v2_ok({
        # 🔴 NOT the same field as the writer's `enabled`, despite the name.
        # The writer's is the host flag file alone; this one is the AND of that
        # and the portal's own gate, which is the right thing to publish. The
        # writer's guarantee "enabled false with holdings_read true cannot
        # happen" is about ITS field, not this one. This composite CAN be false
        # with holdings_read true, and that state is correct and must stay: the
        # host read real numbers and the portal is simply not offering the door.
        # Do not "restore" the writer's invariant here by nulling holdings on
        # the seal, or every player's stock reads as unknown the moment the
        # owner flips the portal gate off.
        "enabled": bool(_refinery_enabled() and payload_dict.get("enabled") is True),
        # INDEPENDENT of `enabled`, and it means one thing only: the holdings in
        # this payload are REAL. The writer sets it when the switch is on AND the
        # DB read succeeded AND the pawn resolved to exactly the set an exchange
        # needs (one bank, one backpack, at most one toolbar). All three states
        # below are reachable and each is a different sentence to the player:
        #   enabled + read      the numbers are real, render them
        #   enabled + NOT read  the refinery is open, this CHARACTER did not
        #                       resolve; see pawn_refusal for which way
        #   dark    + NOT read  no session was opened at all
        # It used to be true that not-read implied dark. It no longer is, and
        # anything that treats a missing number as 0 is now wrong for a live
        # service rather than merely wrong while it is closed.
        "holdings_read": payload_dict.get("holdings_read") is True,
        # Which refusal an exchange on THIS character would hit ("no_bank",
        # "no_pawn_storage") or None when the pawn is fine. Passed through
        # unchanged so the page can say "open the bank in-game once, then retry"
        # instead of the generic could-not-read line. Constrained to a string or
        # None only so a malformed value cannot reach the page as a dict.
        "pawn_refusal": (payload_dict.get("pawn_refusal")
                         if isinstance(payload_dict.get("pawn_refusal"), str)
                         else None),
        # Exactly the storage overview's two fields, with exactly its semantics,
        # so the shared storage components bind without a new prop:
        # online True (locked) / False (writes allowed) / None (undetermined,
        # which the writer treats as LOCKED).
        "online": online,
        "offline_ok": online is False,
        "character_name": row["character_name"],
        "bank": _bank(payload),
        "sources": _sources(payload),
        "recipes": _shape_recipes(payload, discord_id),
        # The rate table's ceiling, not our safety bound: this is what the
        # stepper clamps to, and it must be the number the owner set.
        "caps": {"max_batches_per_request": _max_batches(payload_dict),
                 "window_days": WINDOW_DAYS},
        "rate_version": (payload or {}).get("rate_version"),
        "csrf_token": csrf_for_session(session_token) if session_token else "",
    })


@router.post("/portal/refinery/v2/exchange")
async def portal_refinery_v2_exchange(request: Request):
    """Refine `batches` lots of one tier: ingots plus melange in, dust out.

    Keyed on the OUTPUT template and echoing the whole rate the page was shown,
    so a rate retuned between the read and the write is refused rather than
    filled at the wrong number. owner_ctrl and account_id are resolved
    SERVER-SIDE; no inventory id and no source selection is accepted from the
    browser. Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return _err("unauthenticated")
    _, discord_id, active_account_id, _row = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _err("csrf")
    # Soft dark shape at HTTP 200, the storage family form: the page renders
    # "not open yet", never a toast and never a success.
    if not _refinery_enabled():
        return _err("refinery_disabled")

    output_template = str(body.get("output_template", "") or "").strip()
    if not _TEMPLATE_RE.fullmatch(output_template):
        return _err("unknown_recipe")

    raw_batches = body.get("batches")
    if isinstance(raw_batches, bool):
        batches = None
    elif isinstance(raw_batches, int):
        batches = raw_batches
    else:
        raw = str(raw_batches or "").strip()
        batches = int(raw) if raw.isdigit() else None
    # Safety bound only here; the rate table's real ceiling is enforced below,
    # once the catalog that publishes it has been read.
    if batches is None or not 1 <= batches <= SAFETY_MAX_BATCHES:
        return _err("bad_request")

    idem = str(body.get("uuid", "") or "").strip().lower()
    if not _GUILD_OP_UUID_RE.match(idem):
        return _err("bad_request")

    expected = {}
    for field in _EXPECTED_TEMPLATE_FIELDS:
        value = str(body.get(field, "") or "").strip()
        if not _TEMPLATE_RE.fullmatch(value):
            return _err("bad_request")
        expected[field] = value
    for field in _EXPECTED_COUNT_FIELDS:
        value = body.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return _err("bad_request")
        expected[field] = value
    # PRESENT-WITH-NULL IS NOT ABSENT. The page always sends the key, but the
    # value is null when the host published no rate_version, which is a real
    # configuration and not a malformed request. Judgement is therefore deferred
    # to the comparison below, which can tell a host that has no version from a
    # field that was eaten in transit. A present but MALFORMED value is refused
    # here and now, because only a hand-built body can produce one.
    raw_rate_version = body.get("expected_rate_version")
    if raw_rate_version is None:
        rate_version = None
    else:
        rate_version = str(raw_rate_version).strip()
        if not _RATE_VERSION_RE.fullmatch(rate_version):
            return _err("bad_request")

    owner_ctrl, _bank_solari = await _resolve_buyer_ctrl_and_bank(
        active_account_id, _selected_ctrl(request, active_account_id))
    if owner_ctrl is None:
        return _err("unresolved")

    # The rate the player was shown against the rate the host is quoting now. The
    # cached read is the SAME one the page rendered from, which is the point: a
    # rate retuned in the last minute has to be seen and confirmed, not filled
    # silently at the new number. The writer re-proves the rate under its locks.
    catalog = await _relay_catalog(active_account_id, owner_ctrl)
    if not isinstance(catalog, dict):
        # No answer from the host at all. That is not "we do not refine that",
        # and saying so would send the player looking for a mistake they did not
        # make.
        return _err("unavailable")
    if not catalog.get("ok"):
        # ok:false on a CATALOG read means the read itself failed. A dark host
        # answers ok:true with enabled:false, so this branch is never the kill
        # switch and must not be reported as one.
        return _err(_TOKEN_MAP.get(str(catalog.get("error") or ""), "unavailable"))
    if catalog.get("enabled") is not True:
        # The game host's own gate. The portal's was checked above; either one
        # being off is the same soft 200 to the player.
        return _err("refinery_disabled")
    recipe = None
    for r in _shape_recipes(catalog, discord_id):
        if r["output_template"] == output_template:
            recipe = r
            break
    if recipe is None:
        return _err("unknown_recipe")
    if not recipe["enabled"]:
        return _err("recipe_disabled")
    # R2-L2: the rate table owns the per-request ceiling. Checked against the
    # catalog the page was shown, so a retune that lowers it refuses the next
    # oversized request rather than waiting for a deploy.
    if batches > _max_batches(catalog):
        return _err("bad_request")
    if (recipe["input_template"] != expected["expected_input_template"]
            or recipe["spice_template"] != expected["expected_spice_template"]
            or recipe["input_per_batch"] != expected["expected_input_per_batch"]
            or recipe["spice_per_batch"] != expected["expected_spice_per_batch"]
            or recipe["output_per_batch"] != expected["expected_output_per_batch"]):
        return _err("rate_changed")
    # The whole-table version, which catches the retunes the five fields above
    # cannot see: max_stack, the weekly cap and the window all move the writer's
    # behaviour while every expected_* still matches.
    host_rate_version = catalog.get("rate_version")
    host_has_version = isinstance(host_rate_version, str) and bool(host_rate_version)
    if rate_version is None:
        if host_has_version:
            # The page was shown a version and did not send it back. Something
            # dropped it between there and here, and a trade that proceeds
            # unguarded is precisely what this field exists to prevent. Loud,
            # not silent.
            return _err("bad_request")
        # A host that publishes no rate_version cannot be traded against at all,
        # because the writer now REQUIRES the field on this path. That is a host
        # misconfiguration, so it answers unavailable rather than blaming the
        # player for a malformed request they did not make.
        logger.warning("refinery: host published no rate_version, exchange "
                       "refused acct=%s", active_account_id)
        return _err("unavailable")
    if host_has_version and host_rate_version != rate_version:
        return _err("rate_changed")
    expected["expected_rate_version"] = rate_version

    # OFFLINE GATE, edge half. A take from an inventory the engine holds in RAM
    # is resurrected under its original item id, which is a duplication path, so
    # a definitely-online player is refused here with no relay and no DB touch.
    # UNDETERMINED is deliberately not refused: the writer's in-transaction gate
    # on online_status plus the reconnect grace is the authoritative one.
    if await _resolve_online(active_account_id) is True:
        return _err("player_online")

    if not _storage_rate_ok(active_account_id):
        return _err("rate_limited")

    dust_units = batches * recipe["output_per_batch"]
    input_units = batches * recipe["input_per_batch"]
    spice_units = batches * recipe["spice_per_batch"]

    # The weekly cap, reserved BEFORE the relay in the same write that summed it.
    # Keyed on the Discord identity, so linked alts share one allowance.
    reserved, cap_reason, retry_after, used = portal_refinery_events.check_and_reserve(
        idempotency_key=idem,
        actor_identity=discord_id,
        account_id=active_account_id,
        owner_ctrl=owner_ctrl,
        tier=recipe["tier"],
        output_template=output_template,
        input_template=recipe["input_template"],
        batches=batches,
        dust_units=dust_units,
        input_units=input_units,
        spice_units=spice_units)
    if not reserved:
        resp = _v2_err("weekly_cap",
                       _WEEKLY_CAP_TEXT.format(dust=recipe["output_name"]),
                       status=429)
        resp.headers["Retry-After"] = str(retry_after)
        logger.info("refinery: weekly cap refusal acct=%s tier=%s reason=%s",
                    active_account_id, recipe["tier"], cap_reason)
        return resp

    exchange_body = {
        "op": "exchange",
        "owner_ctrl": owner_ctrl,              # from the session, NEVER the client
        "account_id": active_account_id,       # likewise
        "output_template": output_template,
        "batches": batches,
        "uuid": idem,
        "dry_run": False,
        **expected,
    }

    ip = client_ip(request)
    result = None
    ok = False
    err_token = None
    try:
        from relay import call_relay
        result = await call_relay("/dune/refinery/exchange", method="POST",
                                  json_body=exchange_body, timeout=45)
        ok = isinstance(result, dict) and bool(result.get("ok"))
        if not ok and isinstance(result, dict):
            err_token = result.get("error")
    except HTTPException as exc:
        logger.warning("refinery: exchange relay error acct=%s: %s",
                       active_account_id, exc.detail)
        err_token = "unavailable"
    except Exception as exc:  # noqa: BLE001
        logger.warning("refinery: exchange failed acct=%s: %s", active_account_id, exc)
        err_token = "unavailable"
    finally:
        # success= is the REAL outcome. Omitting it defaults it to True and files
        # every refusal as a successful trade, which is how the transfer lane's
        # audit trail lied for a fortnight.
        from auth import audit_log
        audit_log(
            None, f"portal:{discord_id}", "portal_refinery_exchange",
            str(owner_ctrl), ip,
            details=json.dumps({
                "account_id": active_account_id,
                "owner_ctrl": owner_ctrl,
                "tier": recipe["tier"],
                "output_template": output_template,
                "batches": batches,
                "dust_units": dust_units,
                "result": "ok" if ok else (err_token or "error"),
                "replay": bool((result or {}).get("replay"))
                          if isinstance(result, dict) else False,
                "rate_version": expected.get("expected_rate_version"),
                # Recorded on every row, not just the ones where it is True, so
                # the rate of drift is countable rather than merely visible.
                "volume_unverified": (result or {}).get("volume_unverified")
                                     if isinstance(result, dict) else None,
                "idempotency_key": idem,
            }),
            success=ok,
        )

    if not isinstance(result, dict):
        # No usable answer. The reservation stays `pending` and ages out: settling
        # it `failed` here would free an allowance for a trade that may well have
        # applied, and a lost response is not evidence that nothing happened.
        return _err("unavailable")

    if ok:
        replay = bool(result.get("replay"))
        if result.get("volume_unverified") is True:
            # Not a player-facing state: the dust IS in the bank. A sustained run
            # of these means template_volume.json on the game host has drifted
            # behind a content update and wants refreshing.
            logger.warning("refinery: volume unverified acct=%s tier=%s "
                           "template=%s batches=%s", active_account_id,
                           recipe["tier"], output_template, batches)
        portal_refinery_events.settle(
            idem, "replay" if replay else "applied",
            input_units=result.get("input_units"),
            spice_units=result.get("spice_units"),
            input_template=recipe["input_template"],
            owner_ctrl=owner_ctrl)
        _catalog_drop(_catalog_key(active_account_id, owner_ctrl))
        _drop_storage_caches(active_account_id)
        return _v2_ok({
            "output_template": output_template,
            "batches": batches,
            "consumed": _consumed(result.get("consumed")),
            "granted": _granted(result.get("granted"), output_template),
            "holdings_after": result.get("holdings_after") or {},
            "weekly_dust_used": used,
            "weekly_dust_cap": WEEKLY_DUST_CAP,
            "replay": replay,
            "message": f"Refined {dust_units} {recipe['output_name']} into your "
                       "CHOAM bank. It will not appear in-game until you next log in.",
        })

    token = _TOKEN_MAP.get(err_token or "", "write_failed")
    if token == "weekly_cap":
        # The writer's game-side backstop refused. Same refusal to the player as
        # our own cap, so it gets the same sentence and the same status: two caps
        # stopping one trade must not read as two different failures.
        #
        # Its numbers (weekly_dust_used_account, requested, weekly_dust_cap) are
        # ACCOUNT-keyed and are logged here, not shown. Per lane C ruling 6 the
        # number a player sees is always our identity-keyed primary, which for a
        # player with linked alts is the stricter and therefore the true one.
        logger.info("refinery: writer weekly cap refusal acct=%s tier=%s "
                    "account_used=%s requested=%s account_cap=%s",
                    active_account_id, recipe["tier"],
                    result.get("weekly_dust_used_account"),
                    result.get("requested"), result.get("weekly_dust_cap"))
        portal_refinery_events.settle(idem, "refused", err_token or "weekly_cap")
        resp = _v2_err("weekly_cap",
                       _WEEKLY_CAP_TEXT.format(dust=recipe["output_name"]),
                       status=429)
        resp.headers["Retry-After"] = str(WINDOW_DAYS * 24 * 3600)
        return resp
    # R2-M1. The settle decision keys on the WRITER'S RAW TOKEN, never on the
    # mapped one: conservation_failed and writer_no_output both map to
    # write_failed and need OPPOSITE treatment, so the mapped token cannot
    # decide this.
    #
    # Releasing a reservation says "this dust was never minted, give the
    # allowance back". Only a token that PROVES nothing was taken may do that.
    # write_failed is the indeterminate class: the writer emits it when psql
    # exited 0 but stdout would not parse, which can happen AFTER the COMMIT, so
    # the dust may well exist. Handing the allowance back there is the same
    # mistake as settling a lost relay answer, and it is refused for the same
    # reason at :795. Those age out of the window instead.
    if err_token == "refinery_disabled":
        portal_refinery_events.settle(idem, "deferred", err_token)
    elif err_token in _NOTHING_TAKEN_TOKENS:
        portal_refinery_events.settle(idem, "refused", err_token)
    else:
        logger.warning("refinery: indeterminate outcome, reservation left "
                       "pending to age out acct=%s token=%s idem=%s",
                       active_account_id, err_token, idem)
    if token in ("insufficient_input", "insufficient_spice"):
        return _err(token, _shortfall_copy(token, recipe, result))
    return _err(token)


def _shortfall_copy(token: str, recipe: dict, result: dict) -> str:
    """The "not enough" sentence, with the writer's own numbers when it sent
    them. Never the writer's message, which names inventory ids, and never a raw
    template id in place of a material name."""
    material = (recipe["input_name"] if token == "insufficient_input"
                else recipe["spice_name"])
    held = result.get("held")
    need = result.get("need")
    if isinstance(held, int) and isinstance(need, int) and not isinstance(held, bool):
        return _TEXT[token].format(material=material, held=held, need=need)
    return (f"Not enough {material} for that. Check your bank, backpack and "
            "toolbar, then refine a smaller batch.")


def _drop_storage_caches(account_id: int) -> None:
    """The same drops the pawn-move route makes: the trade changed the bank and
    the backpack, and a stale grid is a player staring at ingots that are gone."""
    try:
        import mirror as _mirror
        _mirror.invalidate_storage(account_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from cache import invalidate
        invalidate("dune.player_containers", str(account_id))
        invalidate("dune.container_search", str(account_id))
    except Exception:  # noqa: BLE001
        pass
