import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from auth import get_current_user, require_admin
from cache import ttl_cache
from config import RELAY_API_KEY, RELAY_URL
from relay import call_relay

try:
    import rules_catalog
except Exception:
    # main.py imports this router unguarded, so an ImportError here is the whole
    # app, not one panel. Without the module /api/dune/rules answers unavailable
    # and every other route is untouched.
    rules_catalog = None

router = APIRouter()

# Router-local rate limiter for the public Dune JSON. The global rate_limit.py
# (5/60s) is too strict for a widget that polls /api/dune/status every 30-60s.
_dune_buckets: dict[str, deque] = defaultdict(deque)
_dune_last_cleanup = 0.0
DUNE_RATE_LIMIT = 30
DUNE_RATE_WINDOW = 60

# Short TTL for /api/dune/positions — it is only the SSE poll-fallback path,
# so a small cache keeps the fallback near-live (~5s) per the live-map plan.
POSITIONS_CACHE_TTL = 5
_positions_cache: dict = {"data": None, "fetched_at": 0.0}
VEHICLES_CACHE_TTL = 15
_vehicles_cache: dict = {"data": None, "fetched_at": 0.0}
FIELDS_CACHE_TTL = 60
_fields_cache: dict = {"data": None, "fetched_at": 0.0}
ROSTER_CACHE_TTL = 60
_roster_cache: dict = {"data": None, "fetched_at": 0.0}

# Progression telemetry caches — snapshot updates every ~10 min upstream so the
# 60s admin-backend cache adds negligible staleness; level-ups ticker pulls at
# 30s to feel "live" without hammering the SSH/relay path.
PROGRESSION_SNAPSHOT_CACHE_TTL = 60
_progression_snapshot_cache: dict = {"data": None, "fetched_at": 0.0}
PROGRESSION_LEVELUPS_CACHE_TTL = 30
_progression_levelups_cache: dict = {"data": None, "fetched_at": 0.0}

PRESENCE_WINDOWS = {"1h", "6h", "12h", "24h", "7d", "30d"}

# /dune is public — players must never see raw farm_state pod names. The five
# main playable maps get a friendly display name (raw names verified against
# live relay data 2026-05-21); a trailing shard suffix (_1, _2…) is stripped
# before lookup; anything unmapped falls back to its raw string.
MAP_DISPLAY_NAMES = {
    "Survival_1": "Hagga Basin",
    "DeepDesert": "Deep Desert",
    "Overmap": "Overmap",
    "SH_Arrakeen": "Arrakeen",
    "SH_HarkoVillage": "Harko Village",
}

# Everything namespaced CB_*, Story_*, DLC_* is instanced content (dungeons,
# ecolabs, story missions, DLC pools) — collapsed into one summary line rather
# than listed map-by-map. The five main maps above carry no such prefix.
_INSTANCE_PREFIXES = ("CB_", "Story_", "DLC_")

# The sietch hub cities spin their pod down to zero when empty and spin it back
# up on demand when a player travels there. A down pod on these is NORMAL
# on-demand behavior, not an outage — the public surface renders them
# "Available on travel" instead of a red "Offline". The core open-world maps
# (Hagga/Deep Desert/Overmap) are always-on: down there IS a real problem.
_ON_DEMAND_MAPS = {"SH_Arrakeen", "SH_HarkoVillage"}


def _friendly_map_name(raw: str) -> str:
    if not raw:
        return "Unknown"
    base = re.sub(r"_\d+$", "", raw)
    return MAP_DISPLAY_NAMES.get(raw) or MAP_DISPLAY_NAMES.get(base) or raw


def _is_on_demand_map(raw: str) -> bool:
    if not raw:
        return False
    base = re.sub(r"_\d+$", "", raw)
    return raw in _ON_DEMAND_MAPS or base in _ON_DEMAND_MAPS


def _is_instance_map(raw: str) -> bool:
    return bool(raw) and raw.startswith(_INSTANCE_PREFIXES)


def _check_dune_rate(ip: str) -> bool:
    global _dune_last_cleanup
    now = time.monotonic()
    cutoff = now - DUNE_RATE_WINDOW

    # Prune empty/stale buckets every 5 min so the dict can't grow unbounded
    # when the client IP varies (mirrors rate_limit.py's sweep).
    if now - _dune_last_cleanup > 300:
        _dune_last_cleanup = now
        stale = [k for k, v in _dune_buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _dune_buckets[k]

    bucket = _dune_buckets[ip]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= DUNE_RATE_LIMIT:
        return False
    bucket.append(now)
    return True


def _client_ip(request: Request) -> str:
    # request.client.host is the loopback peer behind Caddy, so every public
    # caller shared ONE 30/60s bucket. portal_auth.client_ip reads the
    # forwarded hop Caddy appended, which is the real peer. Imported here rather
    # than at module scope: this router is imported unguarded by main.py and has
    # no other portal dependency.
    from portal_auth import client_ip
    return client_ip(request) or "unknown"


def _sanitize_status(raw: dict) -> dict:
    """Whitelist counts / up-down only. The public surface must carry no PII —
    never player names, account ids, or anything player-identifying.

    Relay shape: maps[] are farm_state pods (map/pods/players/ready/alive);
    label + blocked live in a separate partitions[] array, joined on `map`
    (one map can have several partitions)."""
    parts_by_map: dict[str, list] = defaultdict(list)
    for p in raw.get("partitions", []) or []:
        parts_by_map[p.get("map")].append(p)

    main_maps = []
    instances = []
    seen_bases = set()
    for m in raw.get("maps", []) or []:
        name = m.get("map")
        seen_bases.add(re.sub(r"_\d+$", "", name or ""))
        parts = parts_by_map.get(name, [])
        entry = {
            "name": _friendly_map_name(name),
            "players": m.get("players"),
            # "up" = the pod is both ready and alive.
            "up": bool(m.get("ready") and m.get("alive")),
            # "blocked" only when every partition for this map is blocked.
            "blocked": bool(parts) and all(p.get("blocked") for p in parts),
            # On-demand hubs render "Available on travel" (not "Offline") when down.
            "on_demand": _is_on_demand_map(name),
        }
        if _is_instance_map(name):
            instances.append(entry)
        else:
            main_maps.append(entry)

    # A fully spun-down hub can drop out of farm_state entirely (no stale row).
    # Inject it as "available on travel" so the grid stays consistent rather than
    # silently dropping the card.
    for raw_name in _ON_DEMAND_MAPS:
        if raw_name not in seen_bases:
            main_maps.append({
                "name": _friendly_map_name(raw_name),
                "players": None, "up": False, "blocked": False,
                "on_demand": True,
            })

    # Content-instance pods are collapsed — players think in main maps.
    instance_summary = {
        "total": len(instances),
        "online": sum(1 for e in instances if e["up"]),
        "players": sum((e["players"] or 0) for e in instances),
    }

    # bgName/bgId are k8s namespace ids — internal, intentionally dropped.
    # bg.get("error") is str(exc) from the relay — coerce to a bool flag so no
    # raw exception text (errno, host:port) reaches the public surface.
    bg = raw.get("battlegroup") or {}
    # Login-queue depth, sampled defensively by dune-status.py (the BGD field is
    # undocumented, so it may simply be absent). null means "not observed", NOT an
    # empty queue. Server identifiers are dropped for the same reason bgName/bgId
    # are -- they are k8s ids -- so the public surface carries the total plus how
    # many servers reported one.
    q = bg.get("queue") if isinstance(bg.get("queue"), dict) else {}
    q_total = q.get("total")
    srv = q.get("servers")
    return {
        "battlegroup": {
            "title": bg.get("bgTitle"),
            "region": bg.get("bgRegion"),
            "error": bool(bg.get("error")),
        },
        "queue": {
            "total": q_total if isinstance(q_total, int) and not isinstance(q_total, bool) else None,
            "servers_reporting": len(srv) if isinstance(srv, list) else 0,
            # The sampler's payload walk hit its visit cap, so a null total here
            # means "unknown", not "nobody waiting". Operators read it; the
            # player-facing renderers never show it.
            "capped": q.get("capped") is True,
        },
        "maps": main_maps,
        # Per-dimension player counts, friendly-named. `maps` above is grouped by
        # map only, so a multi-world map (DD dim0/1, Hagga dim0/1/2) reports one
        # combined figure -- correct for a map total, WRONG for a per-instance
        # card. Consumers that render one instance must use this instead.
        "map_dims": [
            {"name": _friendly_map_name(d.get("map")),
             "dim": d.get("dimension_index"),
             "players": d.get("players") or 0}
            for d in (raw.get("map_dims") or [])
        ],
        "instances": instance_summary,
        "online_players": raw.get("online_players"),
    }


# --- Public JSON (no auth) ---

@ttl_cache("dune.status", ttl=60, max_stale=300)
async def _fetch_dune_status() -> dict:
    raw = await call_relay("/dune/status")
    data = _sanitize_status(raw)
    data["available"] = True
    return data


@ttl_cache("dune.landsraad_rewards", ttl=30, max_stale=120)
async def cached_landsraad_rewards(account_id: int) -> dict:
    """A player's own unclaimed Landsraad rewards (per-house items + Solari),
    read-only via the relay. Keyed per account_id (ttl_cache key includes args).
    Used by the public portal /portal/landsraad page + the dashboard teaser."""
    return await call_relay(f"/dune/player/{account_id}/landsraad-rewards")


@ttl_cache("dune.landsraad_board", ttl=30, max_stale=120)
async def cached_landsraad_board() -> dict:
    """The live Landsraad term board (25 tiles + per-faction progress +
    great-house score + top-guild contributors + reward ladders), read-only via
    the relay. TERM-GLOBAL (not per-account) — one fetch served to everyone; the
    contributions update ~15s in-game so a 30s ttl stays fresh. Used by the public
    portal /portal/landsraad live board."""
    return await call_relay("/dune/landsraad/board")


@ttl_cache("dune.rewards_enabled", ttl=30, max_stale=120)
async def cached_rewards_enabled() -> dict:
    """Global LASTSIETCH_REWARD_ENABLED mirror, probed via a side-effect-free dry-run
    reward-op: the game-host writer returns status 'deferred' while DARK (its dark
    gate runs first) and 'dry-run' once enabled (the daily dry branch exits before
    any DB txn). NOT per-account; the flag is server-wide. Cached 30s so the
    overview never pays a live probe per load. Fails closed to disabled."""
    body = {
        "account_id": 1,               # sentinel; the dry branch never resolves it
        "reward_kind": "daily_solari",
        "amount": 1,
        "mode": "dry-run",
        # fixed uuid — a dry-run never persists it, so it is safe to reuse
        "idempotency_key": "00000000-0000-4000-8000-000000000000",
    }
    try:
        r = await call_relay("/dune/reward-op", method="POST", json_body=body, timeout=20)
    except Exception:  # noqa: BLE001
        return {"enabled": False}
    return {"enabled": bool(isinstance(r, dict) and r.get("status") == "dry-run")}


@ttl_cache("dune.reward_login_days", ttl=60, max_stale=300)
async def cached_reward_login_days(account_id: int) -> dict:
    """A player's daily-login history (UTC dates seen online, newest first, 60-day
    window), read-only via the relay from the telemetry portal_login_days table.
    Keyed per account_id. Mirrors the presence read pattern; the portal derives
    streak + calendar claim-state from it. Used by /portal/rewards/overview."""
    return await call_relay(f"/dune/rewards/login-days?account_id={account_id}")


@ttl_cache("dune.player_progress", ttl=30, max_stale=120)
async def cached_player_progress(account_id: int) -> dict:
    """A player's character stats (XP / skill points) + economy (Solari / Scrip),
    read-only via the relay. Keyed per account_id. Used by the public portal
    account dashboard card."""
    return await call_relay(f"/dune/player/{account_id}/progress")


@ttl_cache("dune.player_equipped", ttl=30, max_stale=120)
async def cached_player_equipped(account_id: int) -> dict:
    """A player's EQUIPPED gear loadout (inventory_type=1) — per-slot template_id
    + mesh VariantId + dye SwatchId + durability, read-only via the relay. Keyed
    per account_id. Used by the public portal character stage / 3D gear viewer."""
    return await call_relay(f"/dune/player/{account_id}/equipped")


@ttl_cache("dune.player_specializations", ttl=30, max_stale=120)
async def cached_player_specializations(account_id: int) -> dict:
    """A player's specialization tracks (level/xp per track) + owned-keystone
    count, read-only via the relay (reuses the v2 admin progression_state read).
    Keyed per account_id. Used by the public portal account specialization card."""
    return await call_relay(f"/dune/player/{account_id}/progression_state")


@ttl_cache("dune.player_map", ttl=8, max_stale=30)
async def cached_player_map(account_id: int) -> dict:
    """A player's OWN position + base totems + owned vehicles, read-only via the
    relay. Keyed per account_id. 8s ttl keeps the self-marker + vehicles near-live
    (the map polls /me every 10s) without hammering the SSH/DB path -- per-account
    so it only fires for players who actually have their map open. NOTE: the upstream
    ceiling is the game's save tick (dune.actors.transform is written periodically),
    so this is as live as the DB exposes; the telemetry positions stream is the
    path to true real-time if needed. Served only for the caller's own session."""
    return await call_relay(f"/dune/player/{account_id}/map")


@ttl_cache("dune.spice_active", ttl=90, max_stale=300)
async def cached_spice_active() -> dict:
    """Active Deep Desert Large spice field per dimension (liveness + clustered
    sector), read-only via the relay. Global (not per-player); 90s ttl keeps the
    public spice map "live" without hammering the SSH/DB path."""
    return await call_relay("/dune/spice/active")


@ttl_cache("dune.worms", ttl=10, max_stale=60)
async def cached_worms() -> dict:
    """Live Deep Desert sandworm positions + threat state per dimension, read-only
    via the relay. Global (not per-player); 10s ttl keeps the danger overlay live
    (worms move fast) without hammering the SSH/log path. Used by
    /portal/maps/{m}/worms (the map polls it every 10s, no page refresh)."""
    return await call_relay("/dune/worms")


@ttl_cache("dune.sandstorm", ttl=30, max_stale=90)
async def cached_sandstorm() -> dict:
    """Sandstorm forecast + the live moving-storm position per Deep Desert
    dimension, read-only via the relay. Global (not per-player). 30s ttl: the
    storm POSITION freshness is bounded by the relay-side ramcache timer
    (dune-storm-ramcache.py, ~20-30s during a sweep), so polling the relay faster
    than that gains nothing and just re-runs the relay's kubectl log-tail -- 30s
    matches the cache cadence. Used by /portal/maps/{m}/live (consolidated feed)."""
    return await call_relay("/dune/sandstorm")


@ttl_cache("dune.market_rare_recent", ttl=20, max_stale=60)
async def cached_market_rare_recent(after: str = "", limit: int = 50) -> dict:
    """Recently-listed rare-rotation items for the Cielago market announcer,
    read-only via the relay. Keyed per (after, limit); a short ttl keeps the
    announcer close to live without hammering the SSH/DB path. `after` is an
    ISO8601 timestamptz cursor, re-validated by the relay + dispatcher. Shape:
    {"rows":[...]}."""
    params = [f"limit={limit}"]
    if after:
        params.append(f"after={after}")
    return await call_relay(f"/dune/market/rare-recent?{'&'.join(params)}")


@ttl_cache("dune.guilds", ttl=60, max_stale=300)
async def cached_guilds() -> dict:
    """Full guild directory (guilds + members resolved to character names +
    Landsraad contributions overall/current-term), read-only via the relay.
    Global (not per-player); 60s ttl keeps the portal Guild directory fresh
    without hammering the SSH/DB path. Used by /portal/guilds."""
    return await call_relay("/dune/guilds")


@ttl_cache("dune.guild_invites", ttl=20, max_stale=60)
async def cached_guild_invites(account_id: int) -> dict:
    """A player's OWN pending guild invites, read-only via the relay. Keyed per
    account_id; account_id is resolved to controller_id server-side on lastsietch-dune
    (tombstone-safe). Short ttl keeps the portal invites panel near-live. The
    admin-backend only ever passes the session-bound account_id. Used by
    /portal/guilds/invites."""
    return await call_relay(f"/dune/guild-invites?account_id={account_id}")


@ttl_cache("dune.guild_census", ttl=20, max_stale=60)
async def cached_guild_census(guild_id: int) -> dict:
    """Online-state census of one guild's roster, read-only via the relay. Keyed
    per guild_id; short ttl. Caller-must-be-a-member is enforced by the portal
    route BEFORE this is called. Used by /portal/guilds/{id}/presence."""
    return await call_relay(f"/dune/guild-census?guild_id={guild_id}")


@ttl_cache("dune.status_public", ttl=60, max_stale=300)
async def cached_status() -> dict:
    """Server-wide live status (online_players + per-map players + battlegroup
    health), sanitized counts only — no PII. Global; 60s ttl. Shared by the
    public Server page; mirrors the /api/dune/status surface."""
    return await _fetch_dune_status()


@ttl_cache("dune.presence", ttl=60, max_stale=300)
async def cached_presence(window: str = "24h") -> dict:
    """Server-wide presence aggregates for a window — peak concurrent + total
    play-hours + the sampled series (counts only, no PII). Global; 60s ttl keeps
    the public Server page cheap under anonymous load. Used by /portal/server."""
    return await call_relay(f"/dune/stats/presence?window={window}")


@ttl_cache("dune.world_pulse", ttl=120, max_stale=600)
async def cached_world_pulse(window: str = "24h") -> dict:
    """Server-wide world telemetry aggregates (subfief / structure / vehicle
    counters) for a window — counts only, no positions/owners. Global; a longer
    ttl keeps the public Server page cheap (these move slowly). Used by
    /portal/server."""
    return await call_relay(f"/dune/telemetry/world?window={window}")


@ttl_cache("dune.last_funcom_push", ttl=120, max_stale=600)
async def cached_last_funcom_push() -> dict:
    """Last Funcom settingsUpdate push descriptor (relay RMQ capture). Global;
    longer ttl. The PUBLIC Server page surfaces ONLY the relative timestamp from
    this — sha256 / push-count / diff stay admin-only and are stripped at the
    aggregator. Used by /portal/server."""
    return await call_relay("/dune/rmq/last-funcom-push")


@router.get("/api/dune/status")
async def dune_status(request: Request):
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    try:
        return await _fetch_dune_status()
    except Exception:
        # Past max_stale and the relay is still down — hardcoded down state so
        # the dashboard renders rather than throws. Stale-within-window is
        # handled by ttl_cache transparently (returns dict + stale=True).
        return {
            "battlegroup": {"title": None, "region": None, "error": True},
            "maps": [],
            "instances": {"total": 0, "online": 0, "players": 0},
            "online_players": None,
            "available": False,
        }


@router.get("/api/dune/stats/presence")
async def dune_presence(request: Request, window: str = "24h"):
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    if window not in PRESENCE_WINDOWS:
        raise HTTPException(400, f"window must be one of {sorted(PRESENCE_WINDOWS)}")

    # Presence series is counts-only — no PII — so it passes through unchanged.
    try:
        return await call_relay(f"/dune/stats/presence?window={window}")
    except Exception:
        return {
            "window": window,
            "series": [],
            "peak": None,
            "play_hours": None,
            "available": False,
        }


@router.get("/api/dune/positions")
async def dune_positions(request: Request):
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    return await positions_payload()


async def positions_raw_payload() -> dict | None:
    """The relay's positions feed as the game host emits it, cached, UNPROJECTED.

    The game-host script carries `a` (account id) and `n` (character name) on
    every point as well as the coordinates. This accessor is the only place that
    holds them: positions_payload() below projects them away for the public
    surface, and routers/v2_world.py reads them for the admin Live Map, which is
    behind require_admin. Splitting the fetch from the projection is what lets
    one relay call and one cache serve both audiences, so the admin map and the
    public map can never disagree about who is online.

    Returns None when the relay is unreachable AND there is no cache to fall
    back on. Callers decide what a down feed looks like on their own surface;
    inventing a shape here would put a public envelope in front of an admin one.
    """
    now = time.monotonic()
    cached = _positions_cache["data"]
    if cached is not None and now - _positions_cache["fetched_at"] < POSITIONS_CACHE_TTL:
        return cached

    try:
        raw = await call_relay("/dune/players/positions")
    except Exception:
        # Relay unreachable — serve stale cache if we have it, else nothing.
        if cached is not None:
            return {**cached, "stale": True}
        return None

    if not isinstance(raw, dict):
        return None
    _positions_cache["data"] = raw
    _positions_cache["fetched_at"] = now
    return raw


async def positions_payload() -> dict:
    """The public positions payload WITHOUT the per-IP rate check. The admin
    Live Map used to call this directly: an admin session is already gated by
    require_admin, and routing its 20 s layer polls through the public bucket
    (30 per 60 s per IP) starved the admin's own map with 429s the moment a
    portal tab shared the address (2026-09-03). Same cache, same Amtal filter,
    same allowlist: only the bucket is skipped. The admin map now reads
    positions_raw_payload() instead, off that same cache and that same relay
    call, because it needs the identity this function exists to remove."""
    raw = await positions_raw_payload()
    if raw is None:
        return {"map": "HaggaBasin", "count": 0, "players": [], "available": False}

    # Coords + partition tag only. Defensively strip anything that is not
    # x/y/p (partition_id) from each entry so a future relay change can never
    # leak PII through here. `p` splits the two Hagga sietches (1=Habbanya,
    # 32=Kulon) and is a non-identifying world id.
    # The strip is now load-bearing rather than defensive: since 2026-09-04 the
    # game-host feed DOES carry `a` (account id) and `n` (character name), for
    # the admin Live Map. This allowlist is what keeps them off the public wire.
    # `m` (map) and `d` (dimension) added 2026-07-27 so the dashboard can draw
    # every map instead of Hagga only: the feed used to be hardcoded to Hagga and
    # 3 of 5 online players were invisible to it. Both are world ids, not
    # identifiers of a person, so the PII posture is unchanged — the defensive
    # strip below still drops anything not on this allowlist.
    # AMTAL EXCLUSION (owner, 2026-08-27): partition 33 is the full-PvP sietch —
    # public live positions there are a free hunting tool, so Amtal players are
    # never emitted. This is the choke point for every POLLED public read (the
    # /dune page poll, the V1 portal map, the nextgen map). The /dune page's
    # PRIMARY path is the SSE proxy below, which is fed by the telemetry sweep,
    # not by this payload: it applies the same policy per frame in
    # public_stream_frame() (2026-09-04, review finding H1). (Faction-scoped
    # visibility returns later as the earned Mentat Vision reward.)
    players = [
        {"x": p.get("x"), "y": p.get("y"), "p": p.get("p"),
         "m": p.get("m"), "d": p.get("d")}
        for p in (raw.get("players") or [])
        if p.get("p") != 33
    ]
    data = {
        "map": raw.get("map", "HaggaBasin"),
        "count": raw.get("count", len(players)),
        "players": players,
        "by_map": raw.get("by_map") or {},
        "available": True,
    }
    # The cache lives in positions_raw_payload now, so `stale` arrives on the raw
    # payload and is carried through rather than stamped here.
    if raw.get("stale"):
        data["stale"] = True
    return data


@router.get("/api/dune/vehicles")
async def dune_vehicles(request: Request):
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    return await vehicles_payload()


async def vehicles_payload() -> dict:
    """The public vehicles payload without the per-IP rate check; see
    positions_payload for why the admin Live Map reads it directly."""
    now = time.monotonic()
    cached = _vehicles_cache["data"]
    if cached is not None and now - _vehicles_cache["fetched_at"] < VEHICLES_CACHE_TTL:
        return cached

    try:
        raw = await call_relay("/dune/players/vehicles")
    except Exception:
        if cached is not None:
            return {**cached, "stale": True}
        return {"map": "HaggaBasin", "count": 0, "vehicles": [], "available": False}

    # Coords + partition + type (+ subtype `st`, the ornithopter tier that picks
    # a map icon, added 2026-09-04) + world ids only. Defensively strip anything
    # else so a future relay change can never leak identity through here
    # (public-safe). `m` (map) and `d` (dimension) are world ids, not people:
    # consumers MUST filter on `m` now that the feed covers every map, since
    # partition alone only separated Hagga from Deep Desert by luck.
    vehicles = [
        {"x": v.get("x"), "y": v.get("y"), "p": v.get("p"), "t": v.get("t"),
         "m": v.get("m"), "d": v.get("d"), "st": v.get("st")}
        for v in (raw.get("vehicles") or [])
    ]
    data = {
        "map": raw.get("map", "HaggaBasin"),
        "count": raw.get("count", len(vehicles)),
        "vehicles": vehicles,
        "by_map": raw.get("by_map") or {},
        "available": True,
    }
    _vehicles_cache["data"] = data
    _vehicles_cache["fetched_at"] = now
    return data


_STREAM_POINT_KEYS = ("x", "y", "p")
_STREAM_BUF_MAX = 64 * 1024


def public_stream_frame(frame: str):
    """Apply the public positions policy to ONE SSE frame from the telemetry
    stream before it is forwarded to an anonymous client.

    The stream is fed by the telemetry sweep, which selects every Hagga
    partition, so this is the public choke point for the live path (the polled
    path is positions_payload). Rules: comment / keepalive frames pass through
    untouched; a data frame is parsed, every point is rebuilt from the
    x/y/p allowlist (never copied), partition 33 (Amtal, owner ruling
    2026-08-27) is dropped and `count` is recomputed; a data frame that does
    not parse as a JSON object is DROPPED (fail closed), never forwarded raw."""
    if not frame.strip():
        return None
    lines = frame.split("\n")
    data_lines = [ln[5:].lstrip() for ln in lines if ln.startswith("data:")]
    if not data_lines:
        return frame                      # ": keepalive" and event-only frames
    try:
        payload = json.loads("\n".join(data_lines))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if "players" not in payload:
        # A status frame with no points: forward SCALARS only, never the
        # original text, so an unknown future shape cannot pass through raw.
        head = [ln for ln in lines if not ln.startswith("data:")]
        scalars = {k: v for k, v in payload.items()
                   if isinstance(v, (str, int, float, bool, type(None)))}
        return "\n".join(head + ["data: " + json.dumps(scalars)])
    players = [
        {k: pt.get(k) for k in _STREAM_POINT_KEYS}
        for pt in (payload.get("players") or [])
        if isinstance(pt, dict) and pt.get("p") != 33
    ]
    payload["players"] = players
    payload["count"] = len(players)
    head = [ln for ln in lines if not ln.startswith("data:")]
    return "\n".join(head + ["data: " + json.dumps(payload)])


@router.get("/api/dune/positions/stream")
async def dune_positions_stream(request: Request):
    """SSE pass-through of the live Hagga player-position stream.

    Proxies the relay's text/event-stream (/dune/positions/stream) frame for
    frame through public_stream_frame(), which enforces the same public policy
    as /api/dune/positions (x/y/p only, Amtal dropped), so no auth gate. The dashboard EventSource falls back
    to the 5s /api/dune/positions poll on a stream error."""
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")

    url = f"{RELAY_URL}/dune/positions/stream"
    headers = {"X-API-Key": RELAY_API_KEY}

    async def gen():
        buf = b""
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        yield b'event: error\ndata: {"available": false}\n\n'
                        return
                    async for chunk in resp.aiter_bytes():
                        if await request.is_disconnected():
                            break
                        # Reassemble whole frames: a chunk boundary can fall
                        # inside a data line, and the policy must see the
                        # complete JSON before anything reaches the client.
                        buf += chunk.replace(b"\r\n", b"\n")
                        if len(buf) > _STREAM_BUF_MAX:
                            # No separator in 64 KB is not a stream we
                            # understand; drop the connection, never the policy.
                            yield b'event: error\ndata: {"available": false}\n\n'
                            return
                        while b"\n\n" in buf:
                            frame, buf = buf.split(b"\n\n", 1)
                            out = public_stream_frame(frame.decode("utf-8", "replace"))
                            if out is not None:
                                yield out.encode("utf-8") + b"\n\n"
        except Exception:
            # Stream broke (relay down, SSH drop) — emit one error frame so the
            # client EventSource onerror fires and falls back to polling.
            yield b'event: error\ndata: {"available": false}\n\n'

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/dune/fields")
async def dune_fields(request: Request):
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")

    now = time.monotonic()
    cached = _fields_cache["data"]
    if cached is not None and now - _fields_cache["fetched_at"] < FIELDS_CACHE_TTL:
        return cached

    try:
        raw = await call_relay("/dune/fields")
    except Exception:
        # Relay unreachable — serve stale cache if we have it, else a down state.
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "partitions": []}

    # Field aggregates are pure counts — no PII — so they pass through unchanged.
    data = {
        "available": True,
        "partitions": raw.get("partitions", []) or [],
    }
    _fields_cache["data"] = data
    _fields_cache["fetched_at"] = now
    return data


@router.get("/api/dune/progression/snapshot")
async def dune_progression_snapshot(request: Request):
    """Latest per-account character progression for the dashboard widgets
    (level distribution histogram, Top-N by level).

    Carries character names — visible per the leaderboard-convention PII
    policy (same convention as the PvP leaderboards). PII-sensitive fields
    NOT in the public surface: account_id is stripped, online_status is
    coarsened to up/down boolean, ts is dropped. xp, lvl, total_sp,
    unspent_sp, keystone_sp, intel pass through."""
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")

    now = time.monotonic()
    cached = _progression_snapshot_cache["data"]
    if cached is not None and now - _progression_snapshot_cache["fetched_at"] < PROGRESSION_SNAPSHOT_CACHE_TTL:
        return cached

    try:
        raw = await call_relay("/dune/progression/snapshot")
    except Exception:
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "count": 0, "players": []}

    players = []
    for p in (raw.get("players") or []):
        players.append({
            "name": p.get("char_name"),
            "online": (p.get("online_status") == "Online"),
            "xp": p.get("xp"),
            "lvl": p.get("lvl"),
            "total_sp": p.get("total_sp"),
            "unspent_sp": p.get("unspent_sp"),
            "keystone_sp": p.get("keystone_sp"),
            "intel": p.get("intel"),
        })

    data = {"available": True, "count": len(players), "players": players}
    _progression_snapshot_cache["data"] = data
    _progression_snapshot_cache["fetched_at"] = now
    return data


@router.get("/api/dune/progression/levelups")
async def dune_progression_levelups(request: Request, limit: int = 50):
    """Recent level-up events for the dashboard ticker. Newest first.

    Carries character names — same PII policy as the snapshot route.
    account_id is stripped; xp deltas are dropped (only ts + name +
    from_lvl + to_lvl pass through)."""
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    now = time.monotonic()
    cached = _progression_levelups_cache["data"]
    if (cached is not None
            and cached.get("limit") == limit
            and now - _progression_levelups_cache["fetched_at"] < PROGRESSION_LEVELUPS_CACHE_TTL):
        return cached

    try:
        raw = await call_relay(f"/dune/progression/levelups?limit={limit}")
    except Exception:
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "count": 0, "levelups": [], "limit": limit}

    levelups = []
    for lu in (raw.get("levelups") or []):
        levelups.append({
            "ts": lu.get("ts"),
            "name": lu.get("char_name"),
            "from_lvl": lu.get("from_lvl"),
            "to_lvl": lu.get("to_lvl"),
        })

    data = {"available": True, "count": len(levelups),
            "levelups": levelups, "limit": limit}
    _progression_levelups_cache["data"] = data
    _progression_levelups_cache["fetched_at"] = now
    return data


# --- Authenticated JSON (player names = PII) ---
#
# Cache wraps below put account_id directly in the key so two admins viewing
# two different players never see each other's data. Graceful-stale window
# means a brief relay outage degrades to a cached payload with stale=True.

@ttl_cache("dune.progression_snapshot_full", ttl=30, max_stale=60)
async def _cached_progression_snapshot_full() -> dict:
    """Server-wide snapshot used by v2_player._load_identity_data to find a
    single account. NOT per-player; cache key carries no account_id."""
    return await call_relay("/dune/progression/snapshot")


@ttl_cache("dune.player_tags", ttl=30, max_stale=60)
async def _cached_player_tags(account_id: str) -> dict:
    return await call_relay(f"/dune/player/{account_id}/tags")


@ttl_cache("dune.player_containers", ttl=30, max_stale=60)
async def _cached_player_containers(account_id: str) -> dict:
    return await call_relay(f"/dune/player/{account_id}/containers")


@ttl_cache("dune.player_container_items", ttl=30, max_stale=60)
async def _cached_player_container_items(account_id: str, container_id: str, page: int) -> dict:
    """Cache key tuple is (call_name, account_id, container_id, page) — so
    moving between pages or between containers issues fresh fetches, and a
    second click on the same (acct, container, page) within 30s is a hit."""
    return await call_relay(f"/dune/player/{account_id}/_container/{container_id}/_items?page={page}")


@ttl_cache("dune.container_search", ttl=30, max_stale=60)
async def _cached_container_search(account_id: str) -> dict:
    """Whole-account cross-container item index (all storage items aggregated by
    container + template_id). Cached per account; the portal filters the rows by
    the player's search term in-process, so re-searching within 30s is a hit."""
    return await call_relay(f"/dune/player/{account_id}/container-search")


@ttl_cache("dune.player_my_orders", ttl=20, max_stale=60)
async def _cached_my_orders(account_id: str) -> dict:
    """A player's CHOAM "My Orders" view: active sell listings + Completed tab +
    recent history. Per-account; a short TTL keeps it snappy while staying close
    to live (orders change when the player or the bot trades)."""
    return await call_relay(f"/dune/player/{account_id}/my-orders")


@ttl_cache("dune.market_listings", ttl=60, max_stale=180)
async def _cached_market_listings(q: str) -> dict:
    """Active CHOAM exchange listings matching a template_id fragment (the same
    search path that backs the admin Server>Market panel). Keyed per term so each
    category/search is cached independently; global data, not per-account, so the
    portal Market page is a read-only browse over the live exchange. The term is
    validated template_id-safe by the caller and re-validated by the relay."""
    return await call_relay(f"/dune/market/listings?q={q}")


@router.get("/api/dune/player/{account_id}/tags")
async def dune_player_tags(account_id: str, request: Request):
    """Read-only player-tag list for an account. Tags include dialogue flags,
    POI discoveries, mentor classes, journey progression — all identifiable so
    this surface is auth-gated. Useful for diagnosing stuck-quest issues and
    confirming progression_preset / reset_tutorials effects."""
    get_current_user(request)
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return await _cached_player_tags(account_id)
    except Exception:
        return {"available": False, "tags": [], "count": 0}


@router.get("/api/dune/player/{account_id}/containers")
async def dune_player_containers(account_id: str, request: Request):
    """Read-only per-player container list for the v2 admin Player Tools tab.
    Filters: world POIs (owner_entity_id JOIN), holograms, and a 4-class
    storage building_type whitelist. No Hagga-only filter — admin needs DD
    partition containers too. Auth-gated (admin only); the portal variant is
    deferred to P4 where session->account match will be enforced."""
    require_admin(request)
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be a positive integer")
    try:
        return await _cached_player_containers(account_id)
    except Exception:
        return {"available": False, "containers": [], "count": 0}


async def dune_player_container_items(
    account_id: str,
    container_id: str,
    page: int,
    request: Request,
) -> dict:
    """Read-only items list for one container belonging to one account
    (LIFT-10). The helper script enforces ownership server-side via the JOIN
    chain; an `available=false, error="not_owned"` envelope here maps to a
    404 in the calling route. Admin-only — portal exposure is P4."""
    require_admin(request)
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    if not account_id.isdigit() or not container_id.isdigit():
        raise HTTPException(400, "account_id and container_id must be positive integers")
    if not isinstance(page, int) or page < 1:
        page = 1
    try:
        return await _cached_player_container_items(account_id, container_id, page)
    except Exception:
        return {
            "available": False,
            "error": "relay_unavailable",
            "account_id": account_id,
            "container_id": container_id,
            "items": [],
            "count": 0,
            "total_count": 0,
            "page": page,
            "page_size": 100,
        }


async def cached_roster() -> dict:
    """The RAW relay roster payload, ROSTER_CACHE_TTL-cached and stale-on-failure.
    The single shared read for every consumer of the roster: /api/dune/roster
    below is auth + rate limit + the response allowlist on top of it, and
    portal_quiz reads it instead of hitting the relay on every page load.

    Raw on purpose. The route's own five-field player projection is a PII
    allowlist, not a data model, and server-side callers (the portal's placement
    lookup) need fields the client is never handed. It carries character NAMES
    and whatever else upstream sends, so nothing may return it to a client
    unshaped. {available:false, maps:[]} when the relay is down with no last good
    read; the last good read comes back stamped stale:true."""
    now = time.monotonic()
    cached = _roster_cache["data"]
    if cached is not None and now - _roster_cache["fetched_at"] < ROSTER_CACHE_TTL:
        return cached

    try:
        raw = await call_relay("/dune/players/roster")
    except Exception:
        # Relay unreachable — serve stale cache if we have it, else a down state.
        if cached is not None:
            return {**cached, "stale": True}
        return {"available": False, "maps": []}
    if not isinstance(raw, dict):
        return {"available": False, "maps": []}

    _roster_cache["data"] = raw
    _roster_cache["fetched_at"] = now
    return raw


@router.get("/api/dune/roster")
async def dune_roster(request: Request):
    """Per-map online player roster. Carries character NAMES (PII) — unlike the
    public /api/dune/* endpoints above, this one requires an authenticated
    session. get_current_user raises 401 if the request is not authenticated."""
    get_current_user(request)
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")

    raw = await cached_roster()
    # Positive allowlist, byte-identical to what this route has always emitted:
    # the payload behind it is upstream JSON we do not own, so only these five
    # player fields are ever published, whatever else arrives alongside them.
    maps = []
    for m in (raw.get("maps") or []):
        players = [
            {
                "name": p.get("name"),
                "x": p.get("x"),
                "y": p.get("y"),
                "guild": p.get("guild"),
                "faction": p.get("faction"),
            }
            for p in (m.get("players") or [])
        ]
        maps.append({"map": m.get("map"), "players": players})

    data = {"available": bool(raw.get("available", True)), "maps": maps}
    if raw.get("stale"):
        data["stale"] = True
    return data


# --- Sietch Traffic + Server Rules (wave 5) ----------------------------------
#
# Both surfaces read ONE BGD payload -- relay /dune/battlegroup, itself one SSH
# to the director -- so a page showing the traffic board and the rules panel
# costs a single upstream read. cached_battlegroup_raw() is that read;
# /api/dune/traffic and /api/dune/rules shape it and never call the relay
# themselves.
#
# The payload is upstream JSON we do not own (docs/samples/dune-battlegroup.
# sample.json is the captured shape). Everything below walks it defensively:
# no nesting is asserted, the walk is bounded by TRAFFIC_VISIT_CAP, and the
# assembled dict is filtered through a positive key allowlist plus a refusal
# regex over every string value before it leaves the process.
#
# lastServerState.players[] carries playerId / flsId. It is PII and is never
# read, never counted, never emitted -- player counts come from the numeric
# scalars the director already aggregates.

TRAFFIC_VISIT_CAP = 4000
WARM_REPORT_MAX_AGE_S = 600

# Canonical board rows. `src` locates the row in the payload:
#   ("dim", <engine map>, <dimensionIndex>)   dimension maps, always on
#   ("instanced", <engine map>)               instanced maps, scale on demand
# Names and modes track admin-backend/map_model.py MAPS[..]["instances"]: Deep
# Desert labels its two instances by mode, Hagga by sietch name.
_TRAFFIC_ROWS = (
    {"key": "deep-desert:pve", "map_key": "deep-desert", "inst": "pve",
     "name": "Deep Desert", "mode": "PvE", "src": ("dim", "DeepDesert_1", 0)},
    {"key": "deep-desert:pvp", "map_key": "deep-desert", "inst": "pvp",
     "name": "Deep Desert", "mode": "PvP", "src": ("dim", "DeepDesert_1", 1)},
    {"key": "hagga:habbanya", "map_key": "hagga", "inst": "habbanya",
     "name": "Habbanya", "mode": "PvE", "src": ("dim", "Survival_1", 0)},
    {"key": "hagga:kulon", "map_key": "hagga", "inst": "kulon",
     "name": "Kulon", "mode": "PvP", "src": ("dim", "Survival_1", 1)},
    # Owner ruling 891b8aa: Amtal stays public-count-free. players / in_game /
    # queue are all player counts, so all three are withheld, not just the first
    # -- publishing "0 in game" would answer the question the ruling closes.
    {"key": "hagga:amtal", "map_key": "hagga", "inst": "amtal",
     "name": "Amtal", "mode": "Full PvP", "tracked": False,
     "src": ("dim", "Survival_1", 2)},
    {"key": "arrakeen:main", "map_key": "arrakeen", "inst": "main",
     "name": "Arrakeen", "mode": "Social hub", "src": ("instanced", "SH_Arrakeen")},
    {"key": "harko-village:main", "map_key": "harko-village", "inst": "main",
     "name": "Harko Village", "mode": "Social hub",
     "src": ("instanced", "SH_HarkoVillage")},
)

# Player-facing names for the instanced worlds, keyed on the engine key the
# battlegroup reports (stable across builds). The trailing comment is the BGD
# `partition.label`, the level asset alias, which is what ties each key to a
# place without a datamine (research 2026-09-04: awakening.wiki cross-check).
# The label itself is never sent: it is not in _TRAFFIC_KEYS and the partition
# object carries a raw server id beside it. Anything unmapped falls back to the
# family guesses in _instanced_name, then to the engine key with its namespace
# prefix stripped and underscores as spaces.
_INSTANCED_NAMES = {
    "SH_Arrakeen": "Arrakeen",                                  # Arrakeen_0
    "SH_HarkoVillage": "Harko Village",                         # HarkoVillage_0
    # The five hazard-themed Overland testing stations. The numeric suffix is
    # the real station number; they are NOT the Hagga "Imperial Testing
    # Station No. N" open-world markers, which are a disjoint set.
    "CB_Ecolab_Bronze_Green_024": "Testing Station 24",         # DarknessDungeon_0
    "CB_Ecolab_Bronze_Green_089": "Testing Station 89",         # RadiationDungeon_0
    "CB_Ecolab_Bronze_Green_136": "Testing Station 136",        # FireDungeon_0
    "CB_Ecolab_Bronze_Green_152": "Testing Station 152",        # ElectricityDungeon_0
    "CB_Ecolab_Bronze_Green_195": "Testing Station 195",        # PoisonDungeon_0
    "CB_Overland_M_01": "Wreck of the Tyche",                   # RadioactiveShipwreck_0
    "CB_Overland_S_04": "Blushing Cavern",                      # ErythriteCaveIsland_0
    "CB_Overland_S_06": "Smuggler's Run",                       # GroundVehicleTimeTrialIsland_0
    "CB_Overland_S_07": "The Ruins of Tsimpo",                  # TheRuinsOfTsimpo_0
    "CB_Overland_S_08": "Wind Pass",                            # WindPass_0
    "CB_Story_BanditFortress01": "The Broodworks",              # SandfliesFortress_0
    "CB_Story_Hephaestus": "Wreck of the Hephaestus (story)",   # WreckOfHephaestus_0
    "CB_Dungeon_Hephaestus": "Wreck of the Hephaestus (dungeon)",  # WreckOfHephaestusDungeon_0
    "CB_Dungeon_OldCarthag": "Ruins of Old Carthag",            # OldCarthagDungeon_0
    "CB_Story_Ecolab_Carthag": "Beneath Carthag",               # BeneathCarthag_0
    "CB_Story_WaterFatManor": "Water Shipper's Mansion",        # WaterFat_0
    "CB_Dungeon_ThePit": "The Old Quarry",                      # PitDungeon_0
    "Story_HeighlinerDungeon": "Fallen Light",                  # HeighlinerDungeon_0
    "Story_ArtOfKanly": "Neo-Carthag Arena",                    # ArtOfKanly_0
    "Story_ProcesVerbal": "Proces-Verbal",                      # ProcesVerbal_0
    "DLC_Story_LostHarvest_ForgottenLab": "Lost Harvest: Forgotten Testing Station",  # LostHarvest_ForgottenLab_0
    "DLC_Story_LostHarvest_EcolabA": "Lost Harvest: Secret Lab A",  # LostHarvest_EcolabA_0
    "DLC_Story_LostHarvest_EcolabB": "Lost Harvest: Secret Lab B",  # LostHarvest_EcolabB_0
    # No asset alias and no wiki page: the label is the raw key, so the game
    # never named these. Honest labels rather than a guess.
    "Story_Faction_Outpost_Atre": "Atreides faction outpost (story)",  # Story_Faction_Outpost_Atre_0
    "Story_Faction_Outpost_Hark": "Harkonnen faction outpost (story)",  # Story_Faction_Outpost_Hark_0
}
_INSTANCED_PREFIXES = ("DLC_Story_", "CB_", "Story_")

# Positive key allowlist over the ASSEMBLED payload. A key nobody named here
# never reaches the wire, whatever a future build adds upstream.
_TRAFFIC_KEYS = frozenset({
    "available", "read_at", "stale", "battlegroup", "title", "region",
    "online_players", "totals", "travel_requests", "login_requests", "rows",
    "key", "map_key", "inst", "name", "mode", "state", "players", "in_game",
    "queue", "cap", "instances", "tracked", "report_age_s", "scaling",
    "min_servers", "extra_servers", "auto_scaling", "instanced", "maps", "warm",
})
_RULES_KEYS = frozenset({
    "available", "read_at", "stale", "groups", "category", "label", "rows",
    "key", "controls", "value", "unit", "source", "varies", "row_key",
    "config", "note", "basis",
})

# Refusal regex, applied to every string value in the assembled payload: BGD
# serverIds (`sh-` + 16 hex), any dotted quad (the game host IP, RFC1918 or
# public), any long hex run or uuid (a serverId-shaped token under a different
# build's spelling). A match is nulled, not raised on -- a public read-only
# board must degrade, not 500.
_FORBIDDEN = re.compile(
    r"sh-[0-9a-f]{16}"
    r"|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
    r"|\b[0-9a-f]{16,}\b"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)

# Second, deliberately AGED cache. When the settings block vanishes from the
# payload entirely, /api/dune/rules serves this snapshot with every row marked
# `unread` and the ORIGINAL read_at, so the page can say "these are the values
# we last read, at HH:MM UTC" instead of showing a wall of nulls. It is never
# refreshed by a failed read and it deliberately outlives the ttl_cache window.
_last_good_rules: dict = None


def _dig(node, *path):
    """Nested read where every level may be missing or the wrong type. The BGD
    payload is not ours; asserted nesting is how a shaper turns an upstream
    shape change into a 500 on a public page."""
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _as_dict(node):
    """A map we are about to iterate, or an empty one. `or {}` is not enough:
    a truthy list upstream would reach .values() and throw."""
    return node if isinstance(node, dict) else {}


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value):
    return value if isinstance(value, str) else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sum_or_none(values):
    """Sum of the observed figures, or None when NOTHING was observed. A missing
    scalar is 'not observed' and must never render as a confident 0."""
    seen = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    return sum(seen) if seen else None


def _instances_of(node):
    """The instance list of an instancedMaps entry, or None when the map is
    absent from the payload (which is 'unknown', not 'zero instances')."""
    if not isinstance(node, dict):
        return None
    insts = node.get("instances")
    return [i for i in insts if isinstance(i, dict)] if isinstance(insts, list) else []


def _report_age(inst, now):
    ts = _int_or_none(_dig(inst, "lastServerState", "reportTimestamp"))
    if ts is None:
        return None
    return max(0, int(now - ts))


def _instanced_name(key: str) -> str:
    named = _INSTANCED_NAMES.get(key)
    if named:
        return named
    # Family guesses for keys a future build adds before someone names them.
    if "Ecolab" in key:
        return "Testing station"
    if key.startswith("CB_Overland_"):
        return "Overland cave"
    base = key
    stripped = True
    while stripped:
        stripped = False
        for prefix in _INSTANCED_PREFIXES:
            if base.startswith(prefix):
                base = base[len(prefix):]
                stripped = True
                break
    return base.replace("_", " ")


def _state_for(insts, always_on, min_servers, now):
    """warm  = some instance reported ready inside the last WARM_REPORT_MAX_AGE_S
    spins_up_on_travel = not warm and the map keeps no server warm (minServers 0)
    cold  = not warm on an always-on map (dimension maps, Overmap, minServers>=1)
    unknown = the map is not in the payload at all."""
    if insts is None:
        return "unknown"
    for inst in insts:
        if _dig(inst, "lastServerState", "ready") is True:
            age = _report_age(inst, now)
            if age is not None and age < WARM_REPORT_MAX_AGE_S:
                return "warm"
    if not always_on and min_servers in (0, None):
        return "spins_up_on_travel"
    return "cold"


def _traffic_row(spec, raw, now):
    kind = spec["src"][0]
    if kind == "dim":
        entry = _dig(raw, "dimensionMaps", spec["src"][1], "serversByDimension",
                     str(spec["src"][2]))
        insts = [entry] if isinstance(entry, dict) else None
        map_cfg = _dig(raw, "dimensionMaps", spec["src"][1], "cfg")
        map_queue = _int_or_none(_dig(entry, "numPlayersInQueue"))
        # Dimension maps carry no instance-scaling config at all: they are
        # always on, one server per dimension. Reporting min_servers 1 here
        # would be inventing a number the payload does not hold.
        scaling = {"min_servers": None, "extra_servers": None, "auto_scaling": None}
        always_on = True
        instances = 1 if insts else None
    else:
        node = _dig(raw, "instancedMaps", spec["src"][1])
        insts = _instances_of(node)
        map_cfg = _dig(node, "cfg")
        map_queue = _int_or_none(_dig(node, "numPlayersInQueue"))
        scaling = {
            "min_servers": _int_or_none(_dig(map_cfg, "minServers")),
            "extra_servers": _int_or_none(_dig(map_cfg, "numExtraServers")),
            "auto_scaling": _dig(map_cfg, "enableAutomaticInstanceScaling") is True,
        }
        always_on = False
        # Built for N > 1; only N = 1 instance per map has been observed at
        # idle, so the multi-instance branch is unverified against a live server.
        instances = len(insts) if insts is not None else None

    walked = insts or []
    cap = _sum_or_none([_int_or_none(_dig(i, "cfg", "playerHardCap")) for i in walked[:1]])
    if cap is None:
        cap = _int_or_none(_dig(map_cfg, "playerHardCap"))
    ages = [a for a in (_report_age(i, now) for i in walked) if a is not None]

    row = {
        "key": spec["key"],
        "map_key": spec["map_key"],
        "inst": spec["inst"],
        "name": spec["name"],
        "mode": spec["mode"],
        "state": _state_for(insts, always_on, scaling["min_servers"], now),
        "players": _sum_or_none([_int_or_none(i.get("numPlayersOnline")) for i in walked]),
        "in_game": _sum_or_none([_int_or_none(i.get("numPlayersInGame")) for i in walked]),
        "queue": map_queue,
        "cap": cap,
        "instances": instances,
        "tracked": spec.get("tracked", True),
        "report_age_s": min(ages) if ages else None,
        "scaling": scaling,
    }
    if not row["tracked"]:
        row["players"] = None
        row["in_game"] = None
        row["queue"] = None
    return row


def _instanced_rows(raw, now):
    maps = _dig(raw, "instancedMaps")
    if not isinstance(maps, dict):
        return []
    rows = []
    visited = 0
    for key in sorted(maps, key=str):
        if not isinstance(key, str):
            continue
        node = maps.get(key)
        insts = _instances_of(node) or []
        visited += 1 + len(insts)
        if visited > TRAFFIC_VISIT_CAP:
            break
        cfg = _dig(node, "cfg")
        min_servers = _int_or_none(_dig(cfg, "minServers"))
        cap = _sum_or_none([_int_or_none(_dig(i, "cfg", "playerHardCap")) for i in insts[:1]])
        rows.append({
            "key": key,
            "name": _instanced_name(key),
            "players": _sum_or_none([_int_or_none(i.get("numPlayersOnline")) for i in insts]),
            "queue": _int_or_none(_dig(node, "numPlayersInQueue")),
            "state": _state_for(_instances_of(node), False, min_servers, now),
            "instances": len(insts),
            "cap": cap if cap is not None else _int_or_none(_dig(cfg, "playerHardCap")),
        })
    return rows


def _fleet_online(raw, now):
    """Fleet-wide online count. Already published verbatim by /api/dune/status,
    so this repeats a public figure rather than adding one -- the Amtal ruling
    withholds the per-sietch count, not the server total."""
    seen = []
    for entry in _as_dict(_dig(raw, "singleServerMaps")).values():
        seen.append(_int_or_none(_dig(entry, "numPlayersOnline")))
    for entry in _as_dict(_dig(raw, "dimensionMaps")).values():
        for node in _as_dict(_dig(entry, "serversByDimension")).values():
            seen.append(_int_or_none(_dig(node, "numPlayersOnline")))
    visited = 0
    for entry in _as_dict(_dig(raw, "instancedMaps")).values():
        for node in _instances_of(entry) or []:
            visited += 1
            if visited > TRAFFIC_VISIT_CAP:
                break
            seen.append(_int_or_none(node.get("numPlayersOnline")))
    return _sum_or_none(seen)


def _scrub(node, allowed):
    """Positive key allowlist + string refusal over the assembled payload.

    Belt and braces after an explicit assembly: a key nobody named is dropped,
    and a string that looks like a serverId, a host IP or a hex token is nulled.
    Nulling rather than raising keeps a public read-only surface renderable."""
    if isinstance(node, dict):
        return {k: _scrub(v, allowed) for k, v in node.items() if k in allowed}
    if isinstance(node, list):
        return [_scrub(v, allowed) for v in node]
    if isinstance(node, str) and _FORBIDDEN.search(node):
        return None
    return node


def _shape_traffic(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    now = time.time()
    rows = [_traffic_row(spec, raw, now) for spec in _TRAFFIC_ROWS]
    inst_rows = _instanced_rows(raw, now)
    payload = {
        "available": True,
        "read_at": _now_iso(),
        # ttl_cache merges stale=True into a degraded raw read; carry it through
        # so the board can say the figures are the last ones we got.
        "stale": raw.get("stale") is True,
        "battlegroup": {
            "title": _str_or_none(raw.get("bgTitle")),
            "region": _str_or_none(raw.get("bgRegion")),
        },
        "online_players": _fleet_online(raw, now),
        "totals": {
            "travel_requests": _int_or_none(raw.get("numTravelRequestsTotal")),
            "login_requests": _int_or_none(raw.get("numLoginRequestsTotal")),
        },
        "rows": rows,
        "instanced": {
            "maps": len(inst_rows),
            "warm": sum(1 for r in inst_rows if r["state"] == "warm"),
            "players": _sum_or_none([r["players"] for r in inst_rows]),
            "queue": _sum_or_none([r["queue"] for r in inst_rows]),
            "rows": inst_rows,
        },
    }
    return _scrub(payload, _TRAFFIC_KEYS)


def _by_key(item):
    """Sort key for an upstream map whose keys are not guaranteed to be strings
    (mixed types raise on a bare sorted())."""
    return str(item[0])


def _canonical_row_key(engine_map, dim):
    for spec in _TRAFFIC_ROWS:
        if spec["src"] == ("dim", engine_map, dim):
            return spec["key"]
    return f"{engine_map}:{dim}"


def _settings_blocks(raw):
    """(row_key, serverGameplaySettings) for every instance that reported one.

    Canonical board rows first so `varies` reads in board order. Bounded by the
    same visit cap as the traffic walk."""
    blocks = []
    visited = 0
    for engine_map, entry in sorted(_as_dict(_dig(raw, "dimensionMaps")).items(), key=_by_key):
        for dim, node in sorted(_as_dict(_dig(entry, "serversByDimension")).items(), key=_by_key):
            visited += 1
            settings = _dig(node, "lastServerState", "serverGameplaySettings")
            if isinstance(settings, dict):
                index = int(dim) if str(dim).isdigit() else dim
                blocks.append((_canonical_row_key(engine_map, index), settings))
    for engine_map, entry in sorted(_as_dict(_dig(raw, "singleServerMaps")).items(), key=_by_key):
        visited += 1
        settings = _dig(entry, "lastServerState", "serverGameplaySettings")
        if isinstance(settings, dict):
            blocks.append((engine_map, settings))
    for engine_map, entry in sorted(_as_dict(_dig(raw, "instancedMaps")).items(), key=_by_key):
        for node in _instances_of(entry) or []:
            visited += 1
            if visited > TRAFFIC_VISIT_CAP:
                return blocks
            settings = _dig(node, "lastServerState", "serverGameplaySettings")
            if isinstance(settings, dict):
                blocks.append((engine_map, settings))
    return blocks


def _publishable_value(value):
    """Only scalars ship. The one string in this body is serverDisplayName,
    which is withheld; refusing every non-scalar keeps a future build's new
    string knob off the public page by default rather than by review."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return None


def _rules_groups(blocks):
    groups = []
    by_category = {}
    for category, key in rules_catalog.PUBLISHED:
        # The allowlist is the gate, and this is the second lock on it: a key
        # that lands on both lists (a future build renaming a knob into a
        # do_not_expose / not_in_this_body name) is dropped, not published.
        if not rules_catalog.is_publishable(key):
            continue
        block_name = rules_catalog.block_for(category)
        seen = []
        for row_key, settings in blocks:
            leaf = _dig(settings, block_name)
            if not isinstance(leaf, dict) or key not in leaf:
                continue
            value = _publishable_value(leaf.get(key))
            if value is None:
                continue
            # The catalog types some knobs bool while the wire carries 1/0
            # (encoding "1"/"0"); normalise so the page says on/off, never "1".
            if rules_catalog.value_type_for(key) == "bool" and value in (0, 1) and not isinstance(value, bool):
                value = bool(value)
            if (row_key, value) not in seen:
                seen.append((row_key, value))
        label, controls, unit = rules_catalog.label_for(key)
        row = {
            "key": key,
            "label": label,
            "controls": controls,
            # Never the catalog's liveValue: that is a June observation, not a
            # figure anybody read today.
            "value": None,
            "unit": unit,
            "source": "live" if seen else "unread",
        }
        if seen:
            values = {v for _, v in seen}
            if len(values) == 1:
                row["value"] = seen[0][1]
            else:
                row["varies"] = [{"row_key": rk, "value": v} for rk, v in seen]
        group = by_category.get(category)
        if group is None:
            group = {"category": category,
                     "label": rules_catalog.category_label(category), "rows": []}
            by_category[category] = group
            groups.append(group)
        group["rows"].append(row)
    return groups


def _rows_unread(groups):
    """The last-good snapshot, restamped: values kept, every row marked unread."""
    return [{
        "category": g.get("category"),
        "label": g.get("label"),
        "rows": [{**r, "source": "unread"} for r in (g.get("rows") or [])],
    } for g in groups]


def _config_block() -> dict:
    """The "from server config" rows: catalog data for values the live feed
    cannot carry (ini and client properties). source is "config" so the page
    can label them apart from the live rows. Empty when the catalog is."""
    if rules_catalog is None:
        return {"note": "", "rows": []}
    return {
        "note": getattr(rules_catalog, "CONFIG_NOTE", "") or "",
        "rows": [{**r, "source": "config"} for r in (getattr(rules_catalog, "CONFIG_ROWS", None) or [])],
    }


def _shape_rules(raw: dict) -> dict:
    global _last_good_rules
    if rules_catalog is None:
        return {"available": False, "read_at": None, "stale": True, "groups": []}
    if not isinstance(raw, dict):
        raw = {}
    blocks = _settings_blocks(raw)
    if not blocks:
        snapshot = _last_good_rules
        payload = {
            "available": True,
            "read_at": snapshot["read_at"] if snapshot else _now_iso(),
            "stale": True,
            "groups": _rows_unread(snapshot["groups"]) if snapshot else _rules_groups([]),
            "config": _config_block(),
        }
        return _scrub(payload, _RULES_KEYS)
    groups = _rules_groups(blocks)
    read_at = _now_iso()
    _last_good_rules = {"read_at": read_at, "groups": groups}
    return _scrub({
        "available": True,
        "read_at": read_at,
        "stale": raw.get("stale") is True,
        "groups": groups,
        "config": _config_block(),
    }, _RULES_KEYS)


@ttl_cache("dune.bg_raw", ttl=60, max_stale=300)
async def cached_battlegroup_raw() -> dict:
    """Full BGD battlegroup state (relay passthrough of /v0/battlegroup). The
    ONE upstream read behind both wave 5 surfaces; nothing else calls it."""
    return await call_relay("/dune/battlegroup")


@ttl_cache("dune.traffic", ttl=60, max_stale=300)
async def cached_traffic() -> dict:
    return _shape_traffic(await cached_battlegroup_raw())


@ttl_cache("dune.rules", ttl=60, max_stale=300)
async def cached_rules() -> dict:
    return _shape_rules(await cached_battlegroup_raw())


@router.get("/api/dune/traffic")
async def dune_traffic(request: Request):
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    try:
        return await cached_traffic()
    except Exception:
        # Past max_stale and the BGD read is still failing. Hardcoded down state
        # so the board renders "not observed" instead of the page throwing.
        return {
            "available": False,
            "read_at": None,
            "stale": True,
            "battlegroup": {"title": None, "region": None},
            "online_players": None,
            "totals": {"travel_requests": None, "login_requests": None},
            "rows": [],
            "instanced": {"maps": 0, "warm": 0, "players": None, "queue": None,
                          "rows": []},
        }


@router.get("/api/dune/rules")
async def dune_rules(request: Request):
    if not _check_dune_rate(_client_ip(request)):
        raise HTTPException(429, "Rate limit exceeded")
    try:
        return await cached_rules()
    except Exception:
        # The relay or BGD is down past max_stale. The last-good snapshot exists for
        # exactly this outage: serve it restamped unread with its ORIGINAL read_at so
        # the page can say "these are the values we last read, at HH:MM UTC".
        snap = _last_good_rules
        if snap and snap.get("groups"):
            return _scrub({
                "available": True,
                "read_at": snap.get("read_at"),
                "stale": True,
                "groups": _rows_unread(snap["groups"]),
                "config": _config_block(),
            }, _RULES_KEYS)
        return {"available": False, "read_at": None, "stale": True, "groups": []}
