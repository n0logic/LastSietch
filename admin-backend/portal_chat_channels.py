"""Chat channels and membership (Fremkit wave 11).

Who may read, post in, and moderate a chat channel. Three functions carry the
whole authz story and both chat routers import them from here, so there is one
answer to "is this player in that room" rather than two that can drift:

    channels_for(request)          every channel this session can see
    can_access(request, channel)   may this session read/post in one channel
    moderates(request, channel)    may this session delete/mute in one channel

Four properties matter more than the feature:

  * MEMBERSHIP IS COMPUTED, NEVER STORED. A guild roster and a character's house
    live in the GAME database and change without telling us. Nothing here is
    written beside a message, so leaving a guild removes the room on the next
    request rather than on a backfill nobody remembers to run.
  * A CHANNEL ID IS NOT A CAPABILITY. Every id off the wire is re-checked here
    against the session's own membership. The browser naming a channel is a
    request, not a claim.
  * NO FACTION IS NOT A GUESS. `_current_faction` answering None (unaligned, or
    a relay miss) means no faction room, never a default house. A read that
    genuinely failed is not cached either, so a relay blip cannot seal the room
    for the next ten minutes.
  * MODERATION IS NARROWER THAN THE PLAN'S TABLE. An admin moderates anywhere; a
    guild LEADER moderates their own guild channel and nothing else. Officers
    (who share `_guild_editable_role` with leaders, because recruiting is theirs
    too) do not moderate.

Import-safe on its own: routers.portal and portal_quiz pull FastAPI and httpx,
so they are imported lazily inside `_portal()` / `_quiz()` rather than at module
scope. That keeps this module loadable by a stdlib-only test suite and gives the
suite one seam to stub the guild and faction readers at.
"""
import logging
import re
import time

import map_model
from database import get_db

logger = logging.getLogger("portal")

SIETCH_CHANNEL = "sietch"
GUILD_PREFIX = "guild:"

# House rooms. The key is what `portal_quiz._current_faction` returns; anything
# else it can answer (None, "Unaligned") is deliberately absent.
FACTION_CHANNELS = {"Atreides": "faction:atreides", "Harkonnen": "faction:harkonnen"}
FACTION_LABELS = {"faction:atreides": "House Atreides",
                  "faction:harkonnen": "House Harkonnen"}

# Owner ruling 2026-09-04: the three Hagga sietches and the two Deep Desert
# modes, nothing else. Amtal is off the PUBLIC map list but a linked-only room
# for it is allowed; the instanced worlds and the social hubs get no room this
# wave. The ids are FIXED strings and the labels track map_model, so a rename of
# a sietch lands in one place and does not move anybody's channel id.
_MAP_CHANNEL_SPECS = (
    ("map:hagga:habbanya", "hagga", "habbanya"),
    ("map:hagga:kulon", "hagga", "kulon"),
    ("map:hagga:amtal", "hagga", "amtal"),
    ("map:deep-desert:pve", "deep-desert", "pve"),
    ("map:deep-desert:pvp", "deep-desert", "pvp"),
)

# How long one character's resolved house is trusted. Faction is a relay read on
# a path a player changes rarely and the channel list is fetched on every page
# load, so the cache is what keeps chat off the game box.
FACTION_TTL_S = 600.0
_faction_cache: dict = {}

# The outer shape of a channel id, applied before anything is looked up. Bounded
# so a path parameter cannot become an unbounded LIKE argument.
CHANNEL_RE = re.compile(r"[a-z0-9:_-]{1,64}")


def _map_label(map_key: str, instance_key: str) -> str:
    """'Hagga Basin (Habbanya)'. Falls back to the raw keys if map_model ever
    stops carrying the instance, so a channel is never labelled with nothing."""
    board = map_model.MAPS.get(map_key) or {}
    name = board.get("name") or map_key
    for inst in board.get("instances") or []:
        if inst.get("key") == instance_key:
            return "%s (%s)" % (name, inst.get("label") or instance_key)
    return "%s (%s)" % (name, instance_key)


MAP_CHANNELS = tuple((cid, _map_label(mk, ik)) for cid, mk, ik in _MAP_CHANNEL_SPECS)
MAP_CHANNEL_IDS = frozenset(cid for cid, _label in MAP_CHANNELS)


def _portal():
    from routers import portal
    return portal


def _quiz():
    import portal_quiz
    return portal_quiz


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

def identity(request):
    """(discord_id, account_id) off the SIGNED portal session cookie, or
    ("", 0). The routers gate on `_require_linked_session_json` first; this reads
    the same cookie again so the three public functions can keep the one-argument
    signature both routers were written against."""
    session = _portal().get_portal_session(request)
    if not session:
        return "", 0
    from portal_identity import actor_key
    return actor_key(session), int(session.get("aid") or 0)


def is_admin(discord_id: str) -> bool:
    """The portal admin role, resolved from admin.db's operator-set mapping. Any
    failure inside `_roles_for_discord` is already an empty list, which seals."""
    if not discord_id:
        return False
    return "admin" in (_portal()._roles_for_discord(discord_id) or [])


# --------------------------------------------------------------------------- #
# Faction and guild, the two computed memberships
# --------------------------------------------------------------------------- #

async def current_faction(account_id: int, ctrl):
    """The SELECTED character's house, the way /portal/character/v2 resolves it:
    ctrl-scoped progress plus the player's tags through
    `portal_quiz._current_faction`. Returns 'Atreides', 'Harkonnen' or None.

    Cached per (account_id, ctrl) for FACTION_TTL_S. A resolved answer is
    cached, INCLUDING a genuine None (unaligned is an answer). A read that
    failed outright is not: caching a relay outage would take the house room
    away for ten minutes from a player whose house never changed."""
    key = (int(account_id), ctrl)
    now = time.monotonic()
    hit = _faction_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    portal, quiz = _portal(), _quiz()
    try:
        progress = await (portal._load_progress_ctrl(account_id, ctrl) if ctrl is not None
                          else portal._load_progress(account_id))
        tags = await quiz._read_player_tags(account_id)
    except Exception:  # noqa: BLE001 - a house is not worth failing the page for
        logger.warning("chat: faction resolve failed acct=%s ctrl=%s", account_id, ctrl,
                       exc_info=True)
        return None
    if progress is None and not tags:
        return None  # nothing answered; not an answer, so not cached

    faction = quiz._current_faction(progress, tags or [])
    if faction not in FACTION_CHANNELS:
        faction = None
    _faction_cache[key] = (now + FACTION_TTL_S, faction)
    return faction


def forget_faction(account_id: int):
    """Drop every cached house for one account. The character switcher is what
    should call this; without it a switch to the other house waits out the TTL."""
    for key in [k for k in _faction_cache if k[0] == int(account_id)]:
        _faction_cache.pop(key, None)


async def guild_for(account_id: int):
    """(guild, member_row) for the account's guild, or None. The directory read
    is the same cached one the guild pages use, so this adds no game-box load."""
    portal = _portal()
    try:
        data = await portal._load_guilds()
    except Exception:  # noqa: BLE001
        logger.warning("chat: guild directory read failed acct=%s", account_id,
                       exc_info=True)
        return None
    for guild in (data or {}).get("guilds") or []:
        member = portal._guild_member_account(guild, account_id)
        if member is not None:
            return guild, member
    return None


def is_leader(guild, account_id: int) -> bool:
    """Leader ONLY. `_guild_editable_role` is non-null for Leader AND Officer
    because both may edit recruiting; moderation is the leader's alone, so the
    role is put through the name helper rather than tested for non-null."""
    portal = _portal()
    role = portal._guild_editable_role(guild, account_id)
    return role is not None and portal._guild_role_name(role) == "Leader"


# --------------------------------------------------------------------------- #
# Channel ids
# --------------------------------------------------------------------------- #

def guild_id_of(channel_id):
    """The guild id inside 'guild:<id>', or None for any other channel. Digits
    only and bounded: this value reaches an int() and a comparison, never SQL."""
    if not isinstance(channel_id, str) or not channel_id.startswith(GUILD_PREFIX):
        return None
    raw = channel_id[len(GUILD_PREFIX):]
    if not raw.isdigit() or len(raw) > 12:
        return None
    return int(raw)


def channel_kind(channel_id):
    """'sietch' | 'guild' | 'faction' | 'map', or None when the id is not one of
    ours. Shape only: it says nothing about whether the caller may be there."""
    if not isinstance(channel_id, str) or not CHANNEL_RE.fullmatch(channel_id):
        return None
    if channel_id == SIETCH_CHANNEL:
        return "sietch"
    if channel_id in FACTION_LABELS:
        return "faction"
    if channel_id in MAP_CHANNEL_IDS:
        return "map"
    if guild_id_of(channel_id) is not None:
        return "guild"
    return None


# --------------------------------------------------------------------------- #
# admin.db reads: unread counts and the caller's mute
# --------------------------------------------------------------------------- #

def _borrow(conn):
    """(handle, should_close). A helper called INSIDE somebody else's write
    transaction must never open a second connection: SQLite would queue the new
    one behind a lock its own caller is holding, which is a self-deadlock."""
    if conn is not None:
        return conn, False
    return get_db(), True


def unread_counts(account_id: int, channel_ids) -> dict:
    """{channel: count} of live messages the account has not marked read, its
    OWN messages excluded. A channel with no read row counts from the start."""
    ids = [c for c in (channel_ids or []) if isinstance(c, str)]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT channel, COUNT(*) AS c FROM portal_chat_messages m"
            " WHERE m.channel IN (%s) AND m.deleted_utc IS NULL AND m.account_id != ?"
            "   AND m.id > COALESCE((SELECT r.last_id FROM portal_chat_reads r"
            "                         WHERE r.account_id = ? AND r.channel = m.channel), 0)"
            " GROUP BY m.channel" % marks,
            (*ids, int(account_id), int(account_id)),
        ).fetchall()
    finally:
        conn.close()
    return {r["channel"]: int(r["c"]) for r in rows}


def active_mute(account_id: int, channel=None, conn=None):
    """The mute in force on this account, as a dict, or None.

    `channel` None asks "muted anywhere" (what the channels payload reports);
    a channel id asks about that room, which a channel-wide mute and an
    everywhere mute ('') both answer. A mute with until_utc in the past is over
    and a lifted one is over; neither row is deleted, because "expired" and
    "lifted by a moderator" are different facts a moderator needs to tell apart.

    An open-ended mute (until_utc NULL) sorts FIRST: it is the strictest answer
    and the one the composer must show."""
    sql = ["SELECT id, account_id, channel, by_kind, reason, until_utc, created_utc",
           "  FROM portal_chat_mutes",
           " WHERE account_id = ? AND lifted_utc IS NULL",
           "   AND (until_utc IS NULL OR until_utc > ?)"]
    args = [int(account_id), _now_utc()]
    if channel is not None:
        sql.append("   AND (channel = '' OR channel = ?)")
        args.append(str(channel))
    sql.append(" ORDER BY (until_utc IS NOT NULL), until_utc DESC, id DESC LIMIT 1")
    handle, close = _borrow(conn)
    try:
        row = handle.execute("\n".join(sql), args).fetchone()
    finally:
        if close:
            handle.close()
    if row is None:
        return None
    return {"id": int(row["id"]), "channel": row["channel"], "by_kind": row["by_kind"],
            "reason": row["reason"], "until_utc": row["until_utc"],
            "created_utc": row["created_utc"]}


def _now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# The three public answers
# --------------------------------------------------------------------------- #

async def channels_for(request):
    """Every channel this session may be in, in rail order: Sietch, the player's
    guild, their house, then the five map rooms. Each row is
    {id, kind, label, can_post, can_moderate, unread}.

    A guild the player is not in, and a house their selected character does not
    carry, are simply absent: the rail is the membership answer, so a room a
    player cannot enter is never rendered greyed out."""
    discord_id, account_id = identity(request)
    if not account_id:
        return []
    admin = is_admin(discord_id)
    ctrl = _portal()._selected_ctrl(request, account_id)

    rows = [{"id": SIETCH_CHANNEL, "kind": "sietch", "label": "Sietch",
             "can_post": True, "can_moderate": admin}]

    found = await guild_for(account_id)
    if found is not None:
        guild, _member = found
        rows.append({"id": "%s%d" % (GUILD_PREFIX, int(guild["guild_id"])),
                     "kind": "guild",
                     "label": guild.get("guild_name") or "Guild",
                     "can_post": True,
                     "can_moderate": admin or is_leader(guild, account_id)})

    faction = await current_faction(account_id, ctrl)
    if faction:
        cid = FACTION_CHANNELS[faction]
        rows.append({"id": cid, "kind": "faction", "label": FACTION_LABELS[cid],
                     "can_post": True, "can_moderate": admin})

    for cid, label in MAP_CHANNELS:
        rows.append({"id": cid, "kind": "map", "label": label,
                     "can_post": True, "can_moderate": admin})

    counts = unread_counts(account_id, [r["id"] for r in rows])
    for row in rows:
        row["unread"] = counts.get(row["id"], 0)
    return rows


async def can_access(request, channel_id) -> bool:
    """May this session read and post in `channel_id`. False for an id that is
    not one of ours, so a caller never has to sanity-check the string first.

    An admin is NOT let into a guild room here. Moderating a guild channel is
    `moderates`, and the reports queue carries the message context an admin needs
    without putting them in the room, so a private guild stays private."""
    _discord_id, account_id = identity(request)
    kind = channel_kind(channel_id)
    if not account_id or kind is None:
        return False
    if kind in ("sietch", "map"):
        return True
    if kind == "faction":
        ctrl = _portal()._selected_ctrl(request, account_id)
        faction = await current_faction(account_id, ctrl)
        return bool(faction) and FACTION_CHANNELS[faction] == channel_id
    found = await guild_for(account_id)
    return found is not None and int(found[0]["guild_id"]) == guild_id_of(channel_id)


async def moderates(request, channel_id) -> bool:
    """May this session delete a message or mute a player in `channel_id`.
    Admin anywhere; a guild LEADER in their own guild channel and nowhere else."""
    discord_id, account_id = identity(request)
    kind = channel_kind(channel_id)
    if not account_id or kind is None:
        return False
    if is_admin(discord_id):
        return True
    if kind != "guild":
        return False
    found = await guild_for(account_id)
    if found is None or int(found[0]["guild_id"]) != guild_id_of(channel_id):
        return False
    return is_leader(found[0], account_id)
