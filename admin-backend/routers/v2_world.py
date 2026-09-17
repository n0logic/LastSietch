"""World & Maps > Live Map: one full-viewport operator map per instance.

Every LAYER is read-only, composed from a source that already exists and is
already cached somewhere else in this app -- the live position/vehicle feeds,
the cached claim directory, the portal map-data route, the spice/worm/storm
overlays, and two admin.db tables. Nothing here calls the relay a second time on
its own account and no layer writes.

The one write on this page is the Deep Desert layout pin (wave 2 lever A): two
owner-only POSTs that replace or unlink data/dd_layout_override.json, the file
portal_map_data reads per request to pick the island backdrop. It touches no
database and no game host.

PII: this page puts player names, claim owners and waypoint authors on a map, so
every route is require_admin and none of it may ever be folded into
/portal/maps/{key}/data (that one is built for the public site to consume).

LAYER ISOLATION IS THE POINT. Eight sources feed one page; on a live box some of
them are always briefly unreachable. Each layer is built inside its own guard and
degrades to {"available": false, "error": ...}, so a cold relay costs one legend
row rather than the whole map. The page itself must never 500.

AMTAL: /api/dune/positions filters partition 33 at the single public choke point
(owner ruling 2026-08-27). That ruling is about the PUBLIC feed -- the portal must
never be a hunting tool -- and it was never about the operators. The players layer
therefore reads routers.dune.positions_raw_payload(), the unprojected side of the
same cache and the same relay call, and draws Amtal like every other instance.
The public projection keeps its filter untouched; this page is require_admin.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import map_model
from auth import audit_log, get_current_user, require_admin, require_csrf, require_owner
from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Rescue rows carried on the map. The window is the read horizon; `open` is the
# durable 1-hour self-rescue cooldown from portal.py, which is the only "still in
# effect" state this table has -- there is no open-request row anywhere in the
# schema, so nothing here may claim one.
_RESCUE_WINDOW_HOURS = 24
_RESCUE_OPEN_SECONDS = 3600

# Per-layer ceiling. Every source behind this page has its own relay timeout, up
# to 70s on the claim directory; without a ceiling here a cold relay would hold
# the page's poll open for minutes instead of showing seven layers and one that
# did not answer.
_LAYER_TIMEOUT = 8.0

_PLAYER_HREF = "/admin/v2/players/{account_id}"

# Hagga is the one map whose registry backdrop is a portal-side path
# (/assets/HaggaBasin.webp). The edge does serve it, but it is the full 8192
# original at 3.6 MB on a page you pan and zoom; Players > Claims already ships
# a 2048 downscale under the admin's own static tree, so reuse that. Every other
# map's registry backdrop is already an /admin/static path.
_ADMIN_BACKDROPS = {
    "hagga": {"type": "image", "src": "/admin/static/img/maps/hagga-map.webp?v=1",
              "w": 2048, "h": 2048},
}


def _backdrop_for(map_key: str, meta: dict) -> dict | None:
    return _ADMIN_BACKDROPS.get(map_key) or meta.get("backdrop")


# --- Deep Desert layout pin (wave 2 lever A) ---------------------------------
#
# The pin is the owner saying "the matcher is wrong, this cycle is layout N".
# portal_map_data reads this file per request and prefers it over the matcher, so
# a bad write shows up on every player's next map load and a TRUNCATED write is
# worse than a wrong one: same-directory temp file, fsync, os.replace, and never
# open(target, "w").

CONFIRM_PIN = "PIN"
CONFIRM_CLEAR = "CLEAR"

LAYOUT_MIN, LAYOUT_MAX = 0, 11
MAX_NOTE = 200

# Per-admin debounce on the pin, mirroring v2_actions._last_apply.
PIN_DEBOUNCE_S = 10
_last_apply: dict = {}

# admin-backend/data/dd_layout_override.json: the same path dd_layout_match
# reads and ops/dd-layout-override.sh writes. Derived from this file's location
# rather than imported, so the router does not depend on a private name.
_DATA_DIR = os.environ.get("LASTSIETCH_PORTAL_RUNTIME_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
_OVERRIDE_PATH = os.path.join(_DATA_DIR, "dd_layout_override.json")
_OVERRIDE_TMP = os.path.join(_DATA_DIR, ".dd_layout_override.json.tmp")
_OVERRIDE_MODE = 0o640


def _cycle_key() -> str:
    import spice_candidates_acc
    return str(spice_candidates_acc.cycle_key())


def _override_payload(layout_id: int, note: str) -> dict:
    """The exact bytes that land on disk.

    cycle_key and set_utc are stamped HERE, from the live cycle and the server
    clock. Neither is a parameter, so a client cannot pin an override into a
    cycle it does not belong to, which would outlive the Coriolis reset that is
    supposed to expire it."""
    return {
        "id": int(layout_id),
        "cycle_key": _cycle_key(),
        "note": (note or "")[:MAX_NOTE],
        "set_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _read_override() -> dict | None:
    """The override file as it is on disk right now, or None when absent.

    `active` is whether the current cycle is the one it was pinned for: a file
    from an earlier cycle is inert (dd_layout_match ignores it) but still has to
    be visible, or the card shows nothing while a stale file sits there."""
    try:
        with open(_OVERRIDE_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: override read failed: %s", exc)
        return {"unreadable": True, "error": str(exc)[:200]}
    ck = str(raw.get("cycle_key"))
    try:
        current = _cycle_key()
    except Exception:  # noqa: BLE001
        current = None
    return {
        "id": raw.get("id"),
        "note": (raw.get("note") or "")[:MAX_NOTE],
        "set_utc": raw.get("set_utc"),
        "cycle_key": ck,
        "active": current is not None and ck == current,
    }


def _write_override(payload: dict) -> None:
    """Replace the override atomically, in the SAME directory.

    A partial file here is read live by portal_map_data, so the write must never
    be observable half-done: temp file beside the target (same filesystem, so
    os.replace is atomic), fsync before the rename, 0640 for the lastsietch-admin
    service user."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    fd = os.open(_OVERRIDE_TMP, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OVERRIDE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload))
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(_OVERRIDE_TMP)
        except OSError:
            pass
        raise
    os.replace(_OVERRIDE_TMP, _OVERRIDE_PATH)
    os.chmod(_OVERRIDE_PATH, _OVERRIDE_MODE)


def _clear_override() -> bool:
    """Unlink. Never a null-id tombstone: dd_layout_match reads int(raw["id"]),
    so a tombstone would be a parse failure logged on every player map load
    rather than an absent override."""
    try:
        os.unlink(_OVERRIDE_PATH)
        return True
    except FileNotFoundError:
        return False


def _pin_debounce(user: dict, verb: str) -> None:
    key = (user["id"], verb)
    last = _last_apply.get(key)
    now = time.monotonic()
    if last is not None and now - last < PIN_DEBOUNCE_S:
        wait = int(PIN_DEBOUNCE_S - (now - last)) + 1
        raise HTTPException(429, f"slow down: wait {wait}s before another layout {verb}")


class DDLayoutPinRequest(BaseModel):
    id: int
    note: str | None = None
    confirm: str


class DDLayoutClearRequest(BaseModel):
    confirm: str


def _admin_or_redirect(request: Request):
    """Page routes: 401 -> login, 403 -> /admin/. Same shape as v2.py."""
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _proj(map_key: str, x, y):
    """World (x, y) -> [nx, ny] in the map's 0..1000 frame, or None.

    Guards the inputs rather than trusting them: a None or a NaN through
    map_model.project comes back as a NaN pair, and a NaN dot renders nowhere
    while still counting -- an empty map that says it has ten players on it."""
    if not (_num(x) and _num(y)):
        return None
    xy = map_model.project(map_key, x, y)
    if not xy or not (_num(xy[0]) and _num(xy[1])):
        return None
    return [round(float(xy[0]), 1), round(float(xy[1]), 1)]


def _instance(m: dict, inst_key: str | None) -> dict:
    for inst in m["instances"]:
        if inst["key"] == inst_key:
            return inst
    return m["instances"][0]


def _routes_here(row: dict, engine_map: str, inst: dict) -> bool:
    """Does this live actor belong on the selected instance?

    Same discriminator ladder the Overview panels use: engine map first, then
    partition where the map has one (Hagga's three sietches share a dimension
    space in the feed's eyes), then dimension, then the map alone for the
    single-instance hubs."""
    if (row.get("m") or "HaggaBasin") != engine_map:
        return False
    if inst.get("part") is not None:
        return row.get("p") == inst["part"]
    if inst.get("dim") is not None:
        return (row.get("d") or 0) == inst["dim"]
    return True


def _fail(exc) -> dict:
    text = str(exc)[:300]
    # an upstream HTTPException reads "404: {"detail": ...}"; the legend is not the
    # place for a JSON blob (QA 9/3), the status code is the whole story
    m = re.match(r"^(\d{3}):\s*(.*)$", text, re.S)
    if m and m.group(2).lstrip().startswith("{"):
        text = f"upstream answered {m.group(1)}"
    return {"available": False, "error": text}


def _not_applicable(why: str) -> dict:
    """A layer this map structurally does not have, which is NOT a failure.

    The flag is what the client keys on. Reading the prose instead would make
    the legend's behaviour depend on the wording of an error message, and the
    two would drift the first time someone reworded one."""
    return {"available": False, "applicable": False, "error": why}


async def _guarded(name, coro):
    """Build one layer. Never raises, never outlives the timeout.

    The timeout SHIELDS the underlying task instead of cancelling it: these
    builders sit on process-wide TTL caches, so a slow fetch is left to finish
    and warm the cache for the next poll rather than being thrown away and
    retried by the next caller. This response falls back to an envelope."""
    task = asyncio.ensure_future(coro)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    try:
        return name, await asyncio.wait_for(asyncio.shield(task), _LAYER_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("v2_world: layer %s timed out", name)
        return name, {"available": False,
                      "error": f"source did not answer within {_LAYER_TIMEOUT:.0f}s"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: layer %s failed: %s", name, exc)
        return name, _fail(exc)


def _guarded_sync(name, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: layer %s failed: %s", name, exc)
        return _fail(exc)


# --- layers ------------------------------------------------------------------

async def _players_layer(request: Request, map_key: str, m: dict, inst: dict) -> dict:
    """Who is standing where, on EVERY instance including Amtal.

    positions_raw_payload, not the public projection and not the dune_positions
    ROUTE. The route spends the public per-IP bucket an admin session must not
    share (2026-09-03); the projection strips the account id and the character
    name and drops partition 33, which left this map drawing anonymous dots and
    an empty Amtal. Same cache, same relay call, one layer earlier."""
    from routers.dune import positions_raw_payload
    payload = await positions_raw_payload()
    if not isinstance(payload, dict):
        return {"available": False, "error": "positions feed did not answer"}
    points = []
    for p in (payload.get("players") or []):
        if not _routes_here(p, m["engine_map"], inst):
            continue
        nxy = _proj(map_key, p.get("x"), p.get("y"))
        if not nxy:
            continue
        account_id = p.get("a")
        points.append({
            "nx": nxy[0], "ny": nxy[1],
            "account_id": account_id,
            "name": p.get("n"),
            "href": _PLAYER_HREF.format(account_id=account_id) if account_id else None,
        })
    return {"available": bool(payload.get("available", True)),
            "count": len(points), "points": points,
            "stale": bool(payload.get("stale"))}


async def _vehicles_layer(request: Request, map_key: str, m: dict, inst: dict) -> dict:
    from routers.dune import vehicles_payload
    payload = await vehicles_payload()
    if not isinstance(payload, dict):
        return {"available": False, "error": "vehicle feed returned no object"}
    points = []
    for v in (payload.get("vehicles") or []):
        if not _routes_here(v, m["engine_map"], inst):
            continue
        nxy = _proj(map_key, v.get("x"), v.get("y"))
        if nxy:
            points.append({"nx": nxy[0], "ny": nxy[1], "t": v.get("t") or "vehicle",
                           "st": v.get("st")})
    return {"available": bool(payload.get("available", True)),
            "count": len(points), "points": points,
            "stale": bool(payload.get("stale"))}


async def _bases_layer(map_key: str, m: dict, inst: dict) -> dict:
    """Claims on this instance, by owner, each deep-linked to the drilldown.

    Stored backups are dropped: they keep a world transform but nothing stands
    there, and their dimension_index is garbage (values in the hundreds of
    millions are live in the data), so they would also defeat the dim filter."""
    from routers.v2_bases import _cached_bases
    payload = await _cached_bases()
    if not isinstance(payload, dict):
        return {"available": False, "error": "claim directory returned no object"}
    if payload.get("available") is False:
        return {"available": False, "error": payload.get("error") or "claim directory unavailable"}
    points = []
    for b in (payload.get("bases") or []):
        if b.get("map") != m["engine_map"]:
            continue
        if b.get("ownership") == "stored_backup":
            continue
        if inst.get("dim") is not None and b.get("dimension_index") != inst["dim"]:
            continue
        nxy = _proj(map_key, b.get("x"), b.get("y"))
        if not nxy:
            continue
        owner = b.get("owner") or {}
        account_id = owner.get("account_id")
        points.append({
            "nx": nxy[0], "ny": nxy[1],
            "totem_id": b.get("totem_id"),
            "label": b.get("label"),
            "owner_name": owner.get("name"),
            "account_id": account_id,
            "href": _PLAYER_HREF.format(account_id=account_id) if account_id else None,
            "ownership": b.get("ownership"),
            "owner_activity": b.get("owner_activity"),
            "size": b.get("size"),
        })
    return {"available": True, "count": len(points), "points": points,
            "stale": bool(payload.get("stale"))}


async def _storms_layer(map_key: str, m: dict, inst: dict) -> dict:
    if not m.get("has_storms"):
        return _not_applicable("this map has no storm system")
    from routers.portal import _sandstorm_overlay
    payload = await _sandstorm_overlay(map_key)
    if not isinstance(payload, dict):
        return {"available": False, "error": "storm overlay returned no object"}
    dims = payload.get("dimensions") or {}
    info = dims.get(str(inst.get("dim"))) or {}
    return {"available": bool(payload.get("available")) and bool(info),
            "storm": info, "generated_utc": payload.get("generated_utc")}


async def _worms_layer(map_key: str, m: dict, inst: dict) -> dict:
    if not m.get("has_spice"):
        return _not_applicable("worm tracking is Deep Desert only")
    from routers.portal import _worms_overlay
    payload = await _worms_overlay(map_key)
    if not isinstance(payload, dict):
        return {"available": False, "error": "worm overlay returned no object"}
    dims = payload.get("dimensions") or {}
    info = dims.get(str(inst.get("dim"))) or {}
    worms = info.get("worms") or []
    return {"available": True, "count": len(worms), "worms": worms,
            "generated_utc": payload.get("generated_utc")}


def _spice_from_map_data(data: dict) -> dict:
    """Split the /data payload's spice into sized layers.

    `medium` carries default_hidden so the legend can open with it off without
    the client knowing anything about spice: the flag travels with the layer,
    which is what stops a second copy of the Wave 1.1 ruling from drifting."""
    large = []
    for sector, xy in (data.get("spice_candidate_coords") or {}).items():
        if isinstance(xy, (list, tuple)) and len(xy) >= 2 and _num(xy[0]) and _num(xy[1]):
            large.append({"nx": round(float(xy[0]), 1), "ny": round(float(xy[1]), 1),
                          "sector": str(sector).upper()})
    known = {p["sector"] for p in large}
    for sector in (data.get("spice_candidates") or []):
        sec = str(sector).upper()
        if sec not in known:
            large.append({"nx": None, "ny": None, "sector": sec})
    medium = []
    for row in (data.get("spice_mediums") or []):
        if not (isinstance(row, (list, tuple)) and len(row) >= 3):
            continue
        if not (_num(row[0]) and _num(row[1])):
            continue
        medium.append({"nx": round(float(row[0]), 1), "ny": round(float(row[1]), 1),
                       "sector": str(row[2]).upper(),
                       "active": bool(row[3]) if len(row) > 3 else False})
    return {
        "large": {"count": len(large), "sites": large, "default_hidden": False},
        "medium": {"count": len(medium), "sites": medium, "default_hidden": True},
    }


async def _spice_layer(map_key: str, m: dict, inst: dict) -> dict:
    if not m.get("has_spice"):
        return _not_applicable("this map has no spice system")
    data = await _map_data(map_key)
    sized = _spice_from_map_data(data)
    from routers.portal import _spice_overlay
    active = await _spice_overlay(map_key)
    info = ((active or {}).get("dimensions") or {}).get(str(inst.get("dim"))) or {}
    blows = []
    for f in (info.get("ram_active_fields") or []):
        if isinstance(f, dict) and _num(f.get("nx")) and _num(f.get("ny")):
            blows.append({"nx": f["nx"], "ny": f["ny"], "sector": f.get("sector")})
    if not blows and _num(info.get("ram_nx")) and _num(info.get("ram_ny")):
        blows.append({"nx": info["ram_nx"], "ny": info["ram_ny"],
                      "sector": info.get("ram_sector")})
    return {"available": True, "active": {"count": len(blows), "sites": blows},
            "coriolis": data.get("coriolis"), **sized}


def _waypoint_rows(map_key: str | None) -> list:
    """Every player's waypoints, with the linked character name.

    The portal's own endpoint is deliberately scoped to the session's account, so
    there is no helper to borrow for the all-players read -- this is the table
    query the admin surface needs and it exists nowhere else. Read-only."""
    sql = ("SELECT w.id, w.account_id, w.map_key, w.nx, w.ny, w.note, w.created_at, "
           "       (SELECT l.character_name FROM ls_account_links l "
           "         WHERE l.account_id = w.account_id AND l.revoked_at IS NULL "
           "         ORDER BY l.id DESC LIMIT 1) AS character_name "
           "  FROM portal_map_waypoints w")
    args: tuple = ()
    if map_key:
        sql += " WHERE w.map_key = ?"
        args = (map_key,)
    sql += " ORDER BY w.id DESC LIMIT 500"
    conn = get_db()
    try:
        import portal_identity
        rows = conn.execute(sql.replace('ls_account_links', portal_identity.link_table(conn)), args).fetchall()
    finally:
        conn.close()
    return [{
        "id": r["id"], "map": r["map_key"],
        "nx": r["nx"], "ny": r["ny"],
        "note": r["note"] or "",
        "created_at": r["created_at"],
        "account_id": r["account_id"],
        "character_name": r["character_name"],
        "href": _PLAYER_HREF.format(account_id=r["account_id"]),
    } for r in rows]


def _waypoints_layer(map_key: str) -> dict:
    rows = [r for r in _waypoint_rows(map_key)
            if _num(r["nx"]) and _num(r["ny"])]
    return {"available": True, "count": len(rows), "points": rows}


def _rescue_rows(map_key: str | None) -> list:
    """Self-rescues in the read window, positioned where the player was stuck.

    portal_rescue_log records COMPLETED rescues; the schema has no open-request
    row, so `open` here means only "still inside the durable 1-hour cooldown",
    which is the one piece of live state the table does carry. The pin links to
    the player drilldown, where the teleport lever already lives. This router
    never fires it."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=_RESCUE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        import portal_identity
        rows = conn.execute(
            ("SELECT r.id, r.account_id, r.used_at, r.from_x, r.from_y, r.from_map, "
            "       r.to_base_x, r.to_base_y, "
            "       (SELECT l.character_name FROM ls_account_links l "
            "         WHERE l.account_id = r.account_id AND l.revoked_at IS NULL "
            "         ORDER BY l.id DESC LIMIT 1) AS character_name "
            "  FROM portal_rescue_log r WHERE r.used_at >= ? "
            " ORDER BY r.used_at DESC LIMIT 200").replace('ls_account_links', portal_identity.link_table(conn)),
            (since,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        key = map_model.game_map_to_key(r["from_map"])
        if map_key and key != map_key:
            continue
        try:
            used = datetime.strptime(r["used_at"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            age_s = int((now - used).total_seconds())
        except (TypeError, ValueError):
            age_s = None
        out.append({
            "id": r["id"], "map": key, "from_map": r["from_map"],
            "used_at": r["used_at"], "age_s": age_s,
            "open": age_s is not None and age_s < _RESCUE_OPEN_SECONDS,
            "from_x": r["from_x"], "from_y": r["from_y"],
            "account_id": r["account_id"],
            "character_name": r["character_name"],
            "href": _PLAYER_HREF.format(account_id=r["account_id"]),
        })
    return out


def _rescue_layer(map_key: str) -> dict:
    points = []
    for r in _rescue_rows(map_key):
        nxy = _proj(map_key, r["from_x"], r["from_y"])
        if not nxy:
            continue
        points.append({**r, "nx": nxy[0], "ny": nxy[1]})
    return {"available": True, "count": len(points),
            "open_count": sum(1 for p in points if p["open"]),
            "window_hours": _RESCUE_WINDOW_HOURS, "points": points}


# --- Deep Desert layout ------------------------------------------------------

async def _map_data(map_key: str) -> dict:
    """The cached /portal/maps/{key}/data payload as a dict.

    The layout match, the per-cycle spice candidates and the medium-field set are
    all assembled inside that route and nowhere else, so this reads the route's
    own answer rather than rebuilding the pipeline beside it."""
    from routers.portal import portal_map_data
    resp = await portal_map_data(map_key)
    body = getattr(resp, "body", None)
    if body is None:
        return resp if isinstance(resp, dict) else {}
    return json.loads(body)


async def _dd_layout() -> dict:
    """Which Coriolis layout Deep Desert is on, told in three separate parts.

    `effective` is what players get (portal_map_data has already preferred the
    override over the matcher). `matcher` is what the fingerprint scorer says on
    this same payload, and it has to be recomputed here rather than read out of
    the payload, because once a pin exists portal_map_data has replaced the
    matcher's answer with it. Without both halves the card could not say
    "the matcher says 7 at 0.31, you pinned 3", which is the whole point of
    showing an operator a pin.

    match() is a pure scorer apart from its own last-known sidecar, and
    portal_map_data ran it on this identical payload one call earlier, so the
    recomputation writes the same entry it just wrote."""
    data = await _map_data("deep-desert")
    effective = data.get("layout") or {}
    try:
        import dd_layout_match
        matcher = dd_layout_match.match(
            data, data.get("spice_candidate_coords"), data.get("spice_mediums"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: matcher recompute failed: %s", exc)
        matcher = None
    try:
        import spice_candidates_acc
        cycle_key = spice_candidates_acc.cycle_key()
        cycle = spice_candidates_acc.cycle_window()
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: cycle read failed: %s", exc)
        cycle_key, cycle = None, None
    backdrop = data.get("backdrop") or {}
    return {
        "available": bool(effective),
        # flat id/source/confidence stay for the existing legend readers
        "id": effective.get("id"),
        "source": effective.get("source"),
        "confidence": effective.get("confidence"),
        "matcher": {"id": (matcher or {}).get("id"),
                    "confidence": (matcher or {}).get("confidence"),
                    "source": (matcher or {}).get("source")} if matcher else None,
        "override": _read_override(),
        "effective": {"id": effective.get("id"),
                      "confidence": effective.get("confidence"),
                      "source": effective.get("source")} if effective else None,
        "cycle_key": cycle_key,
        "cycle": cycle,
        "backdrop": backdrop.get("src") if backdrop.get("type") == "image" else None,
        "backdrop_template": ((map_model.MAPS.get("deep-desert") or {})
                              .get("backdrop_layouts") or {}).get("template"),
        "layout_min": LAYOUT_MIN,
        "layout_max": LAYOUT_MAX,
        "candidates": len(data.get("spice_candidate_coords") or {}),
    }


# --- routes ------------------------------------------------------------------

@router.get("/v2/world/map")
async def v2_world_map(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    maps = []
    for key in ("hagga", "deep-desert", "arrakeen", "harko-village"):
        meta = map_model.map_meta(key)
        if meta:
            maps.append({**meta, "backdrop": _backdrop_for(key, meta)})
    # Whether to DRAW the pin controls is decided by the same require_owner the
    # POSTs enforce, not by a second copy of the owner rule in a template. A
    # non-owner admin simply never sees the control; the server refuses it
    # either way.
    try:
        require_owner(request)
        is_owner = True
    except HTTPException:
        is_owner = False
    return templates.TemplateResponse(
        request,
        "v2/world_map.html",
        {
            "user": user,
            "current_tab": "world",
            "current_sub_tab": "map",
            "title": "World & Maps > Live Map",
            "maps": maps,
            "view": int(map_model.VIEW),
            "is_owner": is_owner,
            "layout_ids": list(range(LAYOUT_MIN, LAYOUT_MAX + 1)),
            "layout_thumb": ((map_model.MAPS.get("deep-desert") or {})
                             .get("backdrop_layouts") or {}).get("template", ""),
        },
    )


@router.get("/api/dune/v2/world/layers")
async def v2_world_layers(request: Request, map: str = "hagga", instance: str | None = None):
    """Every layer for one map instance, each isolated behind its own guard.

    A layer that cannot be built comes back {"available": false, "error": ...}.
    Nothing in here raises past this point except an unknown map, which is a
    client error and should say so."""
    require_admin(request)
    map_key = map
    m = map_model.MAPS.get(map_key)
    if not m:
        raise HTTPException(404, "Unknown map")
    inst = _instance(m, instance)

    # gather is safe here ONLY because every member is _guarded and therefore
    # cannot raise. A bare gather would lose every other layer to whichever one
    # failed first, which is the exact failure the envelope exists to prevent.
    # Concurrency matters too: run sequentially, a cold relay would hold the
    # request open for the SUM of the per-layer timeouts.
    results = await asyncio.gather(
        _guarded("players", _players_layer(request, map_key, m, inst)),
        _guarded("vehicles", _vehicles_layer(request, map_key, m, inst)),
        _guarded("bases", _bases_layer(map_key, m, inst)),
        _guarded("storms", _storms_layer(map_key, m, inst)),
        _guarded("worms", _worms_layer(map_key, m, inst)),
        _guarded("spice", _spice_layer(map_key, m, inst)),
    )
    layers: dict = dict(results)
    layers["waypoints"] = _guarded_sync("waypoints", _waypoints_layer, map_key)
    layers["rescue"] = _guarded_sync("rescue", _rescue_layer, map_key)

    meta = map_model.map_meta(map_key) or {}
    return {
        "map": map_key,
        "name": m["name"],
        "engine_map": m["engine_map"],
        "instance": inst,
        "view": int(map_model.VIEW),
        "cal": m["cal"],
        "grid": m["grid"],
        "backdrop": _backdrop_for(map_key, meta),
        "layers": layers,
    }


@router.get("/api/dune/v2/world/dd-layout")
async def v2_world_dd_layout(request: Request):
    """Which Coriolis layout Deep Desert is on, and how we know."""
    require_admin(request)
    try:
        return await _dd_layout()
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: dd-layout failed: %s", exc)
        return _fail(exc)


@router.post("/api/dune/v2/world/dd-layout/_pin")
async def v2_world_dd_layout_pin(request: Request, body: DDLayoutPinRequest):
    """Pin the Deep Desert layout for the CURRENT Coriolis cycle.

    Owner-only: this changes what every player's map draws, without a restart
    and without a deploy. Audited on validation failure, on write failure and on
    success, so the log answers "who pinned 3, when, and why" six weeks later.

    The response re-reads the file that was just written rather than echoing the
    request: only the file proves what landed."""
    user = require_admin(request)
    require_owner(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dd_layout_pin", str(body.id), ip,
            details=json.dumps({
                "layout_id": body.id,
                "note": (body.note or "")[:MAX_NOTE],
                "confirm": "[redacted]",
                **extra,
            }),
            success=success,
        )

    try:
        if body.confirm != CONFIRM_PIN:
            raise HTTPException(400, f'confirm must be "{CONFIRM_PIN}"')
        if isinstance(body.id, bool) or not isinstance(body.id, int):
            raise HTTPException(400, "id must be an integer")
        if not LAYOUT_MIN <= body.id <= LAYOUT_MAX:
            raise HTTPException(400, f"id must be {LAYOUT_MIN}..{LAYOUT_MAX}")
        if body.note is not None:
            if not isinstance(body.note, str):
                raise HTTPException(400, "note must be a string")
            if len(body.note) > MAX_NOTE:
                raise HTTPException(400, f"note exceeds {MAX_NOTE} chars")
        _pin_debounce(user, "pin")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    previous = _read_override()
    try:
        payload = _override_payload(body.id, body.note or "")
        _write_override(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: layout pin write failed: %s", exc)
        _audit(False, {"result": f"write_error: {exc}", "previous": previous})
        raise HTTPException(500, f"the override could not be written: {str(exc)[:200]}")

    _last_apply[(user["id"], "pin")] = time.monotonic()
    saved = _read_override()
    _audit(True, {"result": "pinned", "previous": previous, "written": saved})
    return {
        "success": True,
        "override": saved,
        "expires": "at the next Coriolis reset",
    }


@router.post("/api/dune/v2/world/dd-layout/_clear")
async def v2_world_dd_layout_clear(request: Request, body: DDLayoutClearRequest):
    """Drop the pin and hand Deep Desert back to the fingerprint matcher.

    Clearing is an unlink, never a null-id tombstone. Same three audit call
    sites, and the response re-reads the file to prove it is gone."""
    user = require_admin(request)
    require_owner(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"
    previous = _read_override()

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "dd_layout_clear",
            str((previous or {}).get("id", "none")), ip,
            details=json.dumps({
                "confirm": "[redacted]",
                "previous": previous,
                **extra,
            }),
            success=success,
        )

    try:
        if body.confirm != CONFIRM_CLEAR:
            raise HTTPException(400, f'confirm must be "{CONFIRM_CLEAR}"')
        _pin_debounce(user, "clear")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    try:
        removed = _clear_override()
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: layout clear failed: %s", exc)
        _audit(False, {"result": f"unlink_error: {exc}"})
        raise HTTPException(500, f"the override could not be cleared: {str(exc)[:200]}")

    _last_apply[(user["id"], "clear")] = time.monotonic()
    still_there = _read_override()
    _audit(True, {"result": "cleared" if removed else "no_override_present",
                  "override_after": still_there})
    return {"success": True, "removed": removed, "override": still_there}


@router.get("/api/dune/v2/world/waypoints")
async def v2_world_waypoints(request: Request, map: str | None = None):
    """Every player's map pins, newest first. Read-only."""
    require_admin(request)
    if map and map not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    try:
        rows = _waypoint_rows(map)
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: waypoints failed: %s", exc)
        return _fail(exc)
    return {"available": True, "count": len(rows), "waypoints": rows}


@router.get("/api/dune/v2/world/rescue")
async def v2_world_rescue(request: Request, map: str | None = None):
    """Self-rescues in the read window. The pin links to the drilldown that owns
    the teleport lever; this route never dispatches one."""
    require_admin(request)
    if map and map not in map_model.MAPS:
        raise HTTPException(404, "Unknown map")
    try:
        rows = _rescue_rows(map)
    except Exception as exc:  # noqa: BLE001
        logger.warning("v2_world: rescue failed: %s", exc)
        return _fail(exc)
    return {"available": True, "count": len(rows),
            "open_count": sum(1 for r in rows if r["open"]),
            "window_hours": _RESCUE_WINDOW_HOURS, "rescues": rows}
