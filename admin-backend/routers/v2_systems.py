"""Admin wave 1, Set B: Systems & Health, Economy reads, Social reads.

Reference: docs/dune-research/admin-panel/ADMIN-WAVE1-PLAN-2026-09-03.md section 3, 6.

EVERY route here is READ-ONLY. Nothing writes, nothing restarts, nothing touches
dune.* directly; the only game-side reads go through the relay, exactly as
v2_monitor.py does them. Safe to open under a change freeze.

Per-source isolation, same envelope as the Monitor aggregator: a card is one
source, one route, and a dead source degrades that card to
{available: false, error: ...} instead of blanking the page. The JS fetches each
card's own URL, so the isolation is structural rather than something the
aggregator has to remember to do.

🔴 market_history.db is opened ONLY as `file:<path>?mode=ro` with uri=True.
lastsietch-admin owns that file; a plain read-write open on that path creates
-wal/-shm sidecars (and an empty database if the path is wrong), which is how a
"read-only page" ends up writing to the box. The ro URI cannot.

The flag board reads the `config` MODULE, i.e. the environment the running
process actually resolved, never the service environment file. A file says what someone
intended; the module says what is live. Names carrying a secret are dropped by
name before any value is read.
"""
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
import feature_flags
import rewards
from auth import (audit_log, get_current_user, require_admin, require_csrf,
                  require_owner)
from database import get_db
from portal_gift_limits import (GIFT_INBOUND_ALERT_30D, GIFT_MAX_AMOUNT,
                                GIFT_MAX_PER_DAY, GIFT_MAX_PER_PAIR_PER_DAY)
from relay import call_relay

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Page size for every table on these three pages.
PAGE_SIZE = 50
_MAX_PAGE = 200

_PER_SOURCE_TIMEOUT = 8.0

# Sources that cost a subprocess, a relay round trip or a large file stat get a
# short TTL so a 10s poll with several tabs open does not multiply the cost.
_TTL = 10.0
_CACHE: dict = {}

BACKEND_DIR = Path(__file__).resolve().parent.parent
NEXTGEN_DIR = BACKEND_DIR / "portal-nextgen"

# Units the operator actually cares about. lastsietch-telemetry and lastsietch-market-bot run
# on the GAME host, so on <web-host> they resolve to LoadState=not-found: that is
# "not this host", not "dead", and the card must say so rather than showing a
# false red. LoadState is the only thing that separates the two.
SERVICE_UNITS = (
    ("lastsietch-admin.service", "Admin panel + portal"),
    ("lastsietch-relay.service", "Dune relay API"),
    ("cielago.service", "Cielago Discord bot"),
    ("lastsietch-telemetry.service", "Telemetry logger"),
    ("lastsietch-market-bot.service", "Exchange market bot"),
)

# The two nightly reporters. Their bodies go to Discord and the journal, which
# the service user cannot read; systemctl show gives the run state either way.
NIGHTLY_UNITS = (
    ("lastsietch-nightly-check.service", "Nightly portal deploy check"),
    ("lastsietch-drift-check.service", "Nightly drift sentinel"),
)

# Tier table plus the timestamp column each one buckets on.
MARKET_HISTORY_TIERS = (("mph_raw", "ts"), ("mph_hourly", "hour_ts"),
                        ("mph_daily", "day_ts"))

# 🔴 A name carrying any of these NEVER reaches the flag board, whatever its
# value looks like. Name-based because a value test cannot be trusted: an empty
# secret is still a secret name, and a rotated one changes shape. URL is in the
# list because a relay/DSN URL can carry credentials inline.
SECRET_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD",
                     "KEY", "DSN", "URL", "CREDENTIAL",
                     # review 9/3: names a future credential is likely to carry
                     "WEBHOOK", "SALT", "HASH", "PEM", "CERT", "PRIVATE", "SIG", "AUTH")

_UPPER_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_secret_name(name: str) -> bool:
    up = name.upper()
    return any(part in up for part in SECRET_NAME_PARTS)


def _admin_or_redirect(request: Request):
    """HTML page guard. Same shape as routers/v2.py and v2_monitor.py, repeated
    rather than imported to avoid a router-to-router import cycle."""
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


def _fail(exc) -> dict:
    # asyncio.TimeoutError stringifies to nothing, which renders as a bare
    # "TimeoutError:" on the card and tells an operator less than the number.
    if isinstance(exc, asyncio.TimeoutError):
        return {"available": False,
                "error": f"no answer within {int(_PER_SOURCE_TIMEOUT)}s"}
    return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _cached(key: str, fn):
    """One TTL bucket per source key. Failures are cached too: a dead source
    should not be re-probed on every poll from every open tab. Single-flight per
    key: the handlers run in the threadpool (they are plain def, so the event
    loop never blocks on systemctl or sqlite), and without the lock every open
    tab polling at once would stack the same subprocess N times (review 9/3)."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        hit = _CACHE.get(key)
        if hit and (now - hit[0]) < _TTL:
            return hit[1]
        try:
            value = fn()
        except Exception as exc:
            value = _fail(exc)
        _CACHE[key] = (now, value)
        return value


async def _gather(*coros):
    """Per-source timeout inside a return_exceptions gather, so one slow relay
    call cannot hold the whole card past its budget."""
    async def wrap(coro):
        try:
            return await asyncio.wait_for(coro, timeout=_PER_SOURCE_TIMEOUT)
        except Exception as exc:
            return exc
    return await asyncio.gather(*(wrap(c) for c in coros), return_exceptions=True)


def _envelope(result) -> dict:
    if isinstance(result, BaseException):
        return _fail(result)
    if isinstance(result, dict):
        return result if "available" in result else {"available": True, **result}
    return {"available": True, "data": result}


def _page(request: Request):
    """limit/offset clamped at the boundary. Tables paginate at PAGE_SIZE."""
    try:
        limit = int(request.query_params.get("limit", PAGE_SIZE))
    except ValueError:
        limit = PAGE_SIZE
    try:
        offset = int(request.query_params.get("offset", 0))
    except ValueError:
        offset = 0
    return max(1, min(limit, _MAX_PAGE)), max(0, offset)


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _scalar(conn, sql, params=(), default=0):
    row = conn.execute(sql, params).fetchone()
    return (row[0] if row and row[0] is not None else default)


def _file_stats(path: str) -> dict:
    """Size of a SQLite file plus its -wal and -shm sidecars. A WAL that never
    checkpoints is the shape of a stuck writer, so it gets its own number."""
    out = {"path": path, "exists": os.path.exists(path), "bytes": 0,
           "wal_bytes": 0, "shm_bytes": 0, "mtime": 0}
    if out["exists"]:
        st = os.stat(path)
        out["bytes"] = st.st_size
        out["mtime"] = int(st.st_mtime)
    for suffix, key in (("-wal", "wal_bytes"), ("-shm", "shm_bytes")):
        side = path + suffix
        if os.path.exists(side):
            out[key] = os.stat(side).st_size
    return out


def _systemctl_show(unit: str) -> dict:
    """LoadState is included on purpose: without it a unit that lives on another
    host reads as `inactive`, which is a lie the card would render as a fault."""
    out = {"unit": unit, "load_state": "unknown", "active_state": "unknown",
           "sub_state": "", "since": "", "since_epoch": 0}
    try:
        proc = subprocess.run(
            ["systemctl", "show", "-p",
             "LoadState,ActiveState,SubState,ActiveEnterTimestamp", unit],
            capture_output=True, text=True, timeout=5)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "LoadState":
            out["load_state"] = value
        elif key == "ActiveState":
            out["active_state"] = value
        elif key == "SubState":
            out["sub_state"] = value
        elif key == "ActiveEnterTimestamp":
            out["since"] = value
            out["since_epoch"] = _parse_systemd_stamp(value)
    return out


def _parse_systemd_stamp(value: str) -> int:
    """systemd prints 'Wed 2026-09-02 21:19:14 EDT'. Local zone, no offset, so
    parse the naive part and let the OS resolve it."""
    if not value:
        return 0
    parts = value.split()
    if len(parts) < 3:
        return 0
    try:
        naive = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int(naive.timestamp())


def _read_text(path: Path, limit: int = 200000) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(limit)


# --------------------------------------------------------------------------- #
# HTML pages
# --------------------------------------------------------------------------- #

@router.get("/v2/systems")
def v2_systems_page(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    # The toggles are owner-only and the server enforces that on every POST.
    # This only decides whether a control a non-owner cannot use is rendered at
    # all: an admin should not be shown a button that will 403.
    return templates.TemplateResponse(
        request, "v2/systems.html",
        {"user": user, "current_tab": "systems", "page_size": PAGE_SIZE,
         "is_owner": int(user.get("id") or 0) == 1})


@router.get("/v2/economy")
def v2_economy_page(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request, "v2/economy.html",
        {"user": user, "current_tab": "economy", "page_size": PAGE_SIZE})


@router.get("/v2/social")
def v2_social_page(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request, "v2/social.html",
        {"user": user, "current_tab": "social", "page_size": PAGE_SIZE})


# --------------------------------------------------------------------------- #
# Systems & Health
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Feature flags: two families, two sources, two labels (admin wave 2)
# --------------------------------------------------------------------------- #

# Family 1a, the <web-host> process, supported request-time gates. portal.py and
# solido.py resolve through feature_flags.enabled() on EVERY call, so an override
# lands on the next request with no restart. (env name, coded default, what a
# player loses while it is off).
RUNTIME_GATES = (
    ("LASTSIETCH_STORAGE_PAWN_MOVE_ENABLED", "0",
     "moving items into and out of the bank and the backpack"),
    ("LASTSIETCH_AUGMENT_ENABLED", "0",
     "augment reroll and swap"),
    ("LASTSIETCH_AUGMENT_TARGETED_UPGRADE_ENABLED", "0",
     "the transplant swap that keeps the consumed augment's rolls"),
    ("LASTSIETCH_ITEM_TRANSFER_ENABLED", "0",
     "player to player item transfer"),
    ("LASTSIETCH_KARUM_ENABLED", "0",
     "the Karum: listing, buying and every open order"),
    ("LASTSIETCH_KARUM_WTB_ENABLED", "0",
     "Karum wanted orders"),
    ("LASTSIETCH_BLUEPRINT_PUBLISH_ENABLED", "1",
     "publishing a blueprint to the Solido market"),
    ("LASTSIETCH_BLUEPRINT_IMPORT_ENABLED", "1",
     "importing a blueprint from the Solido market"),
    ("LASTSIETCH_MARKET_SELL_BACKPACK_ENABLED", "0",
     "listing on the Exchange straight from the backpack (the game-host file flag is the authority; this only opens the door in the portal)"),
    ("LASTSIETCH_REFINERY_ENABLED", "0",
     "the Ingot Refinery: trading refined ingots and Spice Melange for spiced dust (the game-host file flag is the authority; this only opens the door in the portal)"),
    ("LASTSIETCH_CHAT_ENABLED", "0",
     "portal chat: reading and posting in every channel, and the moderation queue. Retention keeps running while this is off, so turning it off stops new messages and never stops the 30-day sweep"),
)

# Family 1b, the same process, IMPORT time. config.py resolves these once at
# import, so an override could not reach them without a restart. RULING (plan
# section 8.8): they stay read-only on the board this wave. A control that
# quietly did nothing would be worse than no control.
RESTART_GATES = (
    ("REPAIR_ALL_ENABLED", "LASTSIETCH_REPAIR_ALL_ENABLED",
     "the once-a-day Repair and Refurbish Everything tier"),
    ("REPAIR_VEHICLE_ENABLED", "LASTSIETCH_REPAIR_VEHICLE_ENABLED",
     "the per-vehicle Refurbish Vehicle tier"),
    ("REPAIR_BOX_ENABLED", "LASTSIETCH_REPAIR_BOX_ENABLED",
     "the per-container Repair box tier"),
    ("EXPORT_ENABLED", "LASTSIETCH_EXPORT_ENABLED",
     "the player self-service Download my data export"),
    ("MULTIACCOUNT_ENABLED", "LASTSIETCH_MULTIACCOUNT_ENABLED",
     "linking and switching between several game accounts"),
    ("MARKET_WATCH_ENABLED", "LASTSIETCH_MARKET_WATCH_ENABLED",
     "the market price-alert watcher"),
)

# Family 2, the game host. Files under /etc/lastsietch on <game-host>, read by the WRITERS
# themselves, which is why they are the switch that really darks a feature. The
# portal-side gate above only decides whether the door is offered.
HOST_FLAGS = (
    ("karum-enabled", "the Karum writer: every listing, fill and payout"),
    ("karum-wtb-enabled", "the Karum wanted-order writer"),
    ("augment-enabled", "the augment reroll and swap writer"),
    ("reward-enabled", "the login-rewards writer"),
    ("servercmd-enabled",
     "THIS PANEL'S OWN live actions on the game host, not a player feature. "
     "Teleport, give, XP, water, kick, ban and broadcast all stop. SSH to "
     "<game-host> stays the way back in."),
    ("market-sell-container-enabled", "selling to the exchange from a container"),
    ("storage-pawn-move-enabled", "the bank and backpack move writer"),
    ("market-sell-backpack-enabled", "selling to the exchange from the backpack"),
    ("item-transfer-enabled",
     "the player to player CHOAM bank item transfer writer. Both layers must be "
     "on for a transfer to move anything: this file AND the process gate "
     "LASTSIETCH_ITEM_TRANSFER_ENABLED above. While either is off the writer answers "
     "deferred and nothing leaves a bank."),
    ("refinery-enabled", "the Ingot Refinery writer: every ingot and melange exchange"),
)

HOST_FLAG_NAMES = tuple(name for name, _ in HOST_FLAGS)
RUNTIME_GATE_NAMES = tuple(name for name, _, _ in RUNTIME_GATES)

# Typed token the client must echo, same shape as v2_actions' CONFIRM_* trio.
CONFIRM_FLAG = "FLAG"

# Per-admin debounce on a live flip, mirroring v2_actions.APPLY_DEBOUNCE_S. A
# stuck finger on a kill switch is a feature going on and off for every player
# on the server.
APPLY_DEBOUNCE_S = 10
_last_apply: dict = {}  # (user_id, family) -> monotonic timestamp


class FlagSetRequest(BaseModel):
    name: str
    state: str                     # on | off
    confirm: str | None = None     # must equal CONFIRM_FLAG
    acknowledge_dark: bool = False  # required when a flag goes OFF


def _flag_impact(name: str) -> str:
    for gate, _default, impact in RUNTIME_GATES:
        if gate == name:
            return impact
    for gate, impact in HOST_FLAGS:
        if gate == name:
            return impact
    return ""


def _check_flag_request(body: FlagSetRequest, allowed, user: dict, family: str) -> str:
    """Shared apply gate: allowlisted name, valid state, typed token, the second
    explicit acknowledgement when a flag goes dark, and the debounce. Returns the
    normalised state. Raises HTTPException on any failure."""
    if body.name not in allowed:
        raise HTTPException(400, "flag not in the allowlist")
    # The board never emits a name carrying a secret, and neither does this. A
    # toggle route that could be pointed at an arbitrary settable is a way to
    # discover one by its effect.
    if is_secret_name(body.name):
        raise HTTPException(400, "flag not in the allowlist")
    state = (body.state or "").strip().lower()
    if state not in ("on", "off"):
        raise HTTPException(400, "state must be on or off")
    if body.confirm != CONFIRM_FLAG:
        raise HTTPException(400, f'a flag change requires confirm="{CONFIRM_FLAG}"')
    if state == "off" and not body.acknowledge_dark:
        raise HTTPException(
            400,
            "turning a flag off takes acknowledge_dark: players lose "
            + (_flag_impact(body.name) or "this feature"))
    key = (user["id"], family)
    last = _last_apply.get(key)
    now = time.monotonic()
    if last is not None and now - last < APPLY_DEBOUNCE_S:
        wait = int(APPLY_DEBOUNCE_S - (now - last)) + 1
        raise HTTPException(429, f"slow down: wait {wait}s before another flag change")
    return state


def _runtime_gate_rows() -> list:
    """One row per request-time gate, computed by the SAME helper the gate calls.
    A board that resolved the layers itself could disagree with the gate it
    claims to describe, which is the only way this card can lie."""
    specs = [(name, default) for name, default, _ in RUNTIME_GATES
             if not is_secret_name(name)]
    impacts = {name: impact for name, _default, impact in RUNTIME_GATES}
    rows = feature_flags.snapshot(specs)
    for row in rows:
        row["impact"] = impacts.get(row["name"], "")
        row["restart_required"] = False
    return rows


def _restart_gate_rows() -> list:
    """The six import-time constants: their live value out of the config module,
    flagged restart-required so the client renders no control."""
    rows = []
    for const, env_name, impact in RESTART_GATES:
        if is_secret_name(const) or is_secret_name(env_name):
            continue
        rows.append({
            "name": env_name,
            "constant": const,
            "effective": bool(getattr(config, const, False)),
            "env": feature_flags.env_value(env_name),
            "env_set": env_name in os.environ,
            "override": None,
            "impact": impact,
            "restart_required": True,
        })
    return rows


def _flag_rows() -> list:
    """Every settable from the LIVE config module, secrets dropped by name.

    Booleans are the feature gates the owner asked to see; ints and short
    strings are the tuning next to them and are worth the same board. A value is
    only read after its name has passed the secret filter."""
    rows = []
    for name in sorted(dir(config)):
        if not _UPPER_NAME.match(name) or is_secret_name(name):
            continue
        value = getattr(config, name)
        if isinstance(value, bool):
            kind = "gate"
        elif isinstance(value, int):
            kind = "number"
        elif isinstance(value, str):
            kind = "text"
        else:
            continue
        rows.append({
            "name": name,
            "env": "LASTSIETCH_" + name if ("LASTSIETCH_" + name) in os.environ else None,
            "kind": kind,
            "value": value,
        })
    return rows


@router.get("/api/dune/v2/systems/flags")
def systems_flags(request: Request):
    require_admin(request)
    rows = _flag_rows()
    gates = [r for r in rows if r["kind"] == "gate"]
    runtime = _runtime_gate_rows()
    restart = _restart_gate_rows()
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "family": "portal process (<web-host>)",
        "source": "config module in the running process, not a file on disk",
        "override_file": feature_flags.override_path(),
        "gates_on": sum(1 for r in gates if r["value"]),
        "gates_total": len(gates),
        "runtime_gates": runtime,
        "restart_gates": restart,
        "confirm": CONFIRM_FLAG,
        "flags": rows,
    })


@router.post("/api/dune/v2/systems/flags/_set")
def systems_flags_set(request: Request, body: FlagSetRequest):
    """WRITE, owner only. Set or clear ONE request-time override.

    No restart, no sudoers, no os.environ mutation: this writes the override
    file that feature_flags.enabled() re-reads on every call. The response is a
    fresh snapshot taken AFTER the write, never an echo of the request."""
    user = require_owner(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "feature_flag_set", str(body.name), ip,
            details=json.dumps({
                "family": "process",
                "requested": (body.state or "")[:16],
                "impact": _flag_impact(body.name),
                **extra,
            }),
            success=success,
        )

    try:
        state = _check_flag_request(body, RUNTIME_GATE_NAMES, user, "process")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    before = _runtime_gate_rows()
    old = next((r for r in before if r["name"] == body.name), {})
    try:
        feature_flags.set_override(body.name, state == "on")
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"write_error: {type(exc).__name__}: {exc}"})
        raise HTTPException(500, f"could not write the override: {exc}")

    _last_apply[(user["id"], "process")] = time.monotonic()
    after = _runtime_gate_rows()
    new = next((r for r in after if r["name"] == body.name), {})
    _audit(True, {"result": "ok",
                  "old": {"override": old.get("override"), "effective": old.get("effective")},
                  "new": {"override": new.get("override"), "effective": new.get("effective")}})
    return JSONResponse({
        "available": True,
        "success": True,
        "generated_at": int(time.time()),
        "family": "portal process (<web-host>)",
        "name": body.name,
        "runtime_gates": after,
        "restart_gates": _restart_gate_rows(),
    })


@router.get("/api/dune/v2/systems/host-flags")
async def systems_host_flags(request: Request):
    """The game-host kill-switch files on <game-host>, through the relay. Read-only
    and live: every value is a fresh `test -f` on the box."""
    require_admin(request)
    results = await _gather(call_relay("/dune/flags", timeout=_PER_SOURCE_TIMEOUT))
    payload = _envelope(results[0])
    return JSONResponse(_host_flag_envelope(payload))


@router.post("/api/dune/v2/systems/host-flags/_set")
async def systems_host_flags_set(request: Request, body: FlagSetRequest):
    """WRITE, owner only. Create or remove ONE kill-switch file on <game-host>.

    Nothing here restarts a pod, the BattleGroup Director or k3s. The response
    is a fresh flag-list round trip taken AFTER the write, so what the board
    renders is the box's own state and never an echo of the request."""
    user = require_owner(request)
    require_csrf(request, user)
    ip = request.client.host if request.client else "unknown"

    def _audit(success: bool, extra: dict) -> None:
        audit_log(
            user["id"], user["username"], "feature_flag_set", str(body.name), ip,
            details=json.dumps({
                "family": "game host",
                "requested": (body.state or "")[:16],
                "impact": _flag_impact(body.name),
                **extra,
            }),
            success=success,
        )

    try:
        state = _check_flag_request(body, HOST_FLAG_NAMES, user, "host")
    except HTTPException as exc:
        _audit(False, {"result": f"validation_error: {exc.detail}"})
        raise

    before = await _host_flag_states()
    try:
        res = await call_relay(f"/dune/flags/{body.name}", method="POST",
                               json_body={"state": state},
                               timeout=_PER_SOURCE_TIMEOUT * 2)
    except HTTPException as exc:
        _audit(False, {"result": f"relay_error: {exc.detail}"})
        raise HTTPException(502, f"the game host refused: {str(exc.detail)[:300]}")
    except Exception as exc:  # noqa: BLE001
        _audit(False, {"result": f"error: {type(exc).__name__}: {exc}"})
        raise HTTPException(502, f"the game host was unreachable: {str(exc)[:300]}")

    _last_apply[(user["id"], "host")] = time.monotonic()
    # The re-read is a SECOND round trip, not the writer's own answer. The
    # writer already re-verifies with a fresh test -f; this proves the same
    # thing through the read path the board actually renders.
    fresh = await _gather(call_relay("/dune/flags", timeout=_PER_SOURCE_TIMEOUT))
    payload = _envelope(fresh[0])
    after = _host_flag_envelope(payload)
    _audit(bool(res.get("verified")),
           {"result": res.get("ok", res),
            "old": before.get(body.name),
            "new": after.get("states", {}).get(body.name)})
    # `after` carries the READ (available / error); the write outcome gets its
    # own keys so a failed toggle against a healthy read is still legible.
    out = dict(after)
    out["writer"] = res
    out["success"] = bool(res.get("verified"))
    if not out["success"]:
        out["write_error"] = (res.get("error")
                              or "the game host did not confirm the change")
    return JSONResponse(out)


def _host_flag_envelope(payload: dict) -> dict:
    """Shape the relay's flag-list into the board's envelope. A flag the box did
    not report is rendered `unknown`, never assumed off: "we could not look" is
    not "it is off", and that distinction is the whole point of a kill switch."""
    reported = {}
    if isinstance(payload, dict):
        for row in (payload.get("flags") or []):
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                reported[row["name"]] = row.get("state")
    rows = []
    for name, impact in HOST_FLAGS:
        state = reported.get(name)
        rows.append({
            "name": name,
            "state": state if state in ("on", "off") else "unknown",
            "impact": impact,
            "warning": name == "servercmd-enabled",
        })
    return {
        "available": payload.get("available", True) is not False,
        "error": payload.get("error"),
        "generated_at": int(time.time()),
        "family": "game host files (<game-host>)",
        "source": "a fresh test -f on <game-host>, through the relay",
        "dir": payload.get("dir"),
        "confirm": CONFIRM_FLAG,
        "flags": rows,
        "states": {r["name"]: r["state"] for r in rows},
    }


async def _host_flag_states() -> dict:
    results = await _gather(call_relay("/dune/flags", timeout=_PER_SOURCE_TIMEOUT))
    return _host_flag_envelope(_envelope(results[0])).get("states", {})


def _version_sources() -> dict:
    """V2 app version + both service-worker cache keys.

    The BUILT copy is the one the browser is served, so it is read first and the
    `from` field says which file answered. Reading only the source tree would
    report what the next deploy will ship, not what is live."""
    out = {"available": True}

    version_file = NEXTGEN_DIR / "build" / "_app" / "version.json"
    try:
        text = _read_text(version_file, 4096)
        match = re.search(r'"version"\s*:\s*"([^"]+)"', text)
        out["v2_app_version"] = match.group(1) if match else None
        out["v2_app_version_from"] = str(version_file)
        out["v2_app_version_mtime"] = int(version_file.stat().st_mtime)
    except Exception as exc:
        out["v2_app_version"] = None
        out["v2_app_version_error"] = f"{type(exc).__name__}: {exc}"

    for candidate in (NEXTGEN_DIR / "build" / "sw.js", NEXTGEN_DIR / "static" / "sw.js"):
        try:
            match = re.search(r"CACHE\s*=\s*'([^']+)'", _read_text(candidate, 8192))
        except Exception:
            continue
        if match:
            out["v2_sw_key"] = match.group(1)
            out["v2_sw_key_from"] = str(candidate)
            break
    out.setdefault("v2_sw_key", None)

    portal_router = BACKEND_DIR / "routers" / "portal.py"
    try:
        match = re.search(r"CACHE\s*=\s*'(ls-portal-v\d+)'", _read_text(portal_router))
        out["classic_sw_key"] = match.group(1) if match else None
        out["classic_sw_key_from"] = str(portal_router)
    except Exception as exc:
        out["classic_sw_key"] = None
        out["classic_sw_key_error"] = f"{type(exc).__name__}: {exc}"

    return out


@router.get("/api/dune/v2/systems/versions")
def systems_versions(request: Request):
    require_admin(request)
    return JSONResponse(_cached("versions", _version_sources))


@router.get("/api/dune/v2/systems/rmq")
async def systems_rmq(request: Request):
    """The five RMQ capture reads the Monitor aggregator already makes, on their
    own card. Same relay paths, same isolation, no new transport."""
    require_admin(request)
    results = await _gather(
        call_relay("/dune/rmq/partition-counts", timeout=_PER_SOURCE_TIMEOUT),
        call_relay("/dune/rmq/travel-queue", timeout=_PER_SOURCE_TIMEOUT),
        call_relay("/dune/rmq/last-funcom-push", timeout=_PER_SOURCE_TIMEOUT),
        call_relay("/dune/rmq/bgd-rpc-recent", timeout=_PER_SOURCE_TIMEOUT),
        call_relay("/dune/rmq/completions-recent?limit=10", timeout=_PER_SOURCE_TIMEOUT),
    )
    keys = ("partition_counts", "travel_queue", "last_funcom_push",
            "bgd_rpc", "completions")
    out = {"generated_at": int(time.time())}
    for key, result in zip(keys, results):
        out[key] = _envelope(result)
    # The fan-out itself completed, so the CARD is available. Each capture
    # carries its own state; collapsing five sources into one boolean is exactly
    # the loss of resolution the isolation exists to prevent.
    out["available"] = True
    out["reading"] = sum(1 for k in keys if out[k].get("available") is not False)
    out["sources"] = len(keys)
    return JSONResponse(out)


def _telemetry_local_db() -> dict:
    """The telemetry store lives on the GAME host. If a path is configured for
    this host we stat it; otherwise the card says where it actually is instead
    of rendering an empty gauge as a fault."""
    path = os.environ.get("LASTSIETCH_TELEMETRY_DB_PATH", "")
    if not path:
        return {"available": False,
                "error": "telemetry store is on the game host; reachable only through the relay"}
    return {"available": True, **_file_stats(path)}


@router.get("/api/dune/v2/systems/telemetry")
async def systems_telemetry(request: Request):
    require_admin(request)
    results = await _gather(
        call_relay("/dune/telemetry/events?limit=1", timeout=_PER_SOURCE_TIMEOUT),
        call_relay("/dune/telemetry/transfers?limit=1", timeout=_PER_SOURCE_TIMEOUT),
        call_relay("/dune/telemetry/world?window=24h", timeout=_PER_SOURCE_TIMEOUT),
    )
    events, transfers, world = (_envelope(r) for r in results)

    def newest(payload, keys):
        for key in keys:
            items = payload.get(key)
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    for field in ("ts", "timestamp", "created_at", "at"):
                        if isinstance(first.get(field), (int, float)):
                            return int(first[field])
        return 0

    now = int(time.time())
    last_event = newest(events, ("events", "items", "rows"))
    last_transfer = newest(transfers, ("transfers", "items", "rows"))
    return JSONResponse({
        # Same rule as the RMQ card: the route ran, so the card renders. Each
        # relay read carries its own availability below.
        "available": True,
        "generated_at": now,
        "last_event_ts": last_event,
        "last_event_age_s": (now - last_event) if last_event else None,
        "last_transfer_ts": last_transfer,
        "last_transfer_age_s": (now - last_transfer) if last_transfer else None,
        "events": events,
        "transfers": transfers,
        "world": world,
        "db": _cached("telemetry_db", _telemetry_local_db),
    })


def _nightly_snapshot() -> dict:
    """Run state of the two nightly reporters.

    Their report BODIES go to Discord and the journal; lastsietch-admin is not in
    systemd-journal, so the text is genuinely unreadable from here and the card
    says that rather than implying the jobs are silent. The run state is real."""
    units = []
    for unit, label in NIGHTLY_UNITS:
        row = _systemctl_show(unit)
        row["label"] = label
        row["found"] = row["load_state"] not in ("not-found", "unknown")
        units.append(row)
    return {
        # The systemd read itself succeeded, so the card is available even when
        # a unit is not installed here. "not on this host" is a state to render,
        # not a source failure that should blank the card.
        "available": True,
        "generated_at": int(time.time()),
        "units": units,
        "report_body": {
            "available": False,
            "error": "nightly report bodies go to Discord and the journal; "
                     "the service user cannot read the journal",
        },
        "databases": [_file_stats(config.DB_PATH),
                      _file_stats(config.MARKET_HISTORY_DB_PATH)],
    }


@router.get("/api/dune/v2/systems/snapshot")
def systems_snapshot(request: Request):
    require_admin(request)
    return JSONResponse(_cached("snapshot", _nightly_snapshot))


def _services() -> dict:
    units = []
    for unit, label in SERVICE_UNITS:
        row = _systemctl_show(unit)
        row["label"] = label
        row["found"] = row["load_state"] not in ("not-found", "unknown")
        units.append(row)
    return {"available": True, "generated_at": int(time.time()), "units": units}


@router.get("/api/dune/v2/systems/services")
def systems_services(request: Request):
    require_admin(request)
    return JSONResponse(_cached("services", _services))


def _db_stats() -> dict:
    admin = _file_stats(config.DB_PATH)
    history = _file_stats(config.MARKET_HISTORY_DB_PATH)
    admin["label"] = "admin.db"
    history["label"] = "market_history.db"
    return {"available": True, "generated_at": int(time.time()),
            "databases": [admin, history]}


@router.get("/api/dune/v2/systems/db")
def systems_db(request: Request):
    require_admin(request)
    return JSONResponse(_cached("db", _db_stats))


def _reward_stats() -> dict:
    """Claims by day over 14 d, the claim-run distribution, and the monthly
    grace window.

    The run length here is counted from CLAIMED daily periods, not from login
    days: this database holds claims, not logins. Calling it a login streak
    would be a true number answering a question nobody asked."""
    today = rewards.utc_today()
    conn = get_db()
    try:
        by_day = _rows(conn, """
            SELECT substr(created_at, 1, 10) AS day,
                   reward_kind,
                   COUNT(*) AS claims,
                   COUNT(DISTINCT account_id) AS accounts,
                   COALESCE(SUM(amount), 0) AS solari
              FROM ls_reward_claims
             WHERE created_at >= ?
             GROUP BY day, reward_kind
             ORDER BY day""", (str(today - timedelta(days=13)),))

        totals = _rows(conn, """
            SELECT reward_kind, COUNT(*) AS claims,
                   COUNT(DISTINCT account_id) AS accounts
              FROM ls_reward_claims
             GROUP BY reward_kind ORDER BY reward_kind""")

        daily_periods = _rows(conn, """
            SELECT account_id, period_key
              FROM ls_reward_claims
             WHERE reward_kind = 'daily_solari' AND period_key >= ?
             ORDER BY account_id, period_key""",
                              (str(today - timedelta(days=30)),))

        period_key = rewards.monthly_period_key(today)
        prior_key = rewards.monthly_prior_period_key(today)
        monthly_now = _scalar(conn, """
            SELECT COUNT(*) FROM ls_reward_claims
             WHERE reward_kind = 'monthly_augment' AND period_key = ?""",
                              (period_key,))
        monthly_prior = _scalar(conn, """
            SELECT COUNT(*) FROM ls_reward_claims
             WHERE reward_kind = 'monthly_augment' AND period_key = ?""",
                                (prior_key,))
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()

    by_account = {}
    for row in daily_periods:
        by_account.setdefault(row["account_id"], set()).add(row["period_key"])
    buckets = {"1": 0, "2": 0, "3-6": 0, "7-13": 0, "14+": 0}
    for periods in by_account.values():
        run = rewards.run_ending(periods, today)
        if run == 0:
            run = rewards.run_ending(periods, today - timedelta(days=1))
        if run <= 0:
            continue
        if run == 1:
            buckets["1"] += 1
        elif run == 2:
            buckets["2"] += 1
        elif run < 7:
            buckets["3-6"] += 1
        elif run < 14:
            buckets["7-13"] += 1
        else:
            buckets["14+"] += 1

    return {
        "available": True,
        "generated_at": int(time.time()),
        "by_day": by_day,
        "totals": totals,
        "claim_runs": [{"bucket": k, "accounts": v} for k, v in buckets.items()],
        "claim_run_note": "runs of claimed daily periods, not login days",
        "monthly": {
            "period_key": period_key,
            "prior_period_key": prior_key,
            "in_grace_window": rewards.monthly_in_grace_window(today),
            "grace_days": rewards.MONTHLY_CLAIM_GRACE_DAYS,
            "grace_deadline": rewards.monthly_grace_deadline(today).isoformat(),
            "claims_this_period": monthly_now,
            "claims_prior_period": monthly_prior,
        },
    }


@router.get("/api/dune/v2/systems/rewards")
def systems_rewards(request: Request):
    require_admin(request)
    return JSONResponse(_cached("rewards", _reward_stats))


# --------------------------------------------------------------------------- #
# Economy
# --------------------------------------------------------------------------- #

def _history_ro():
    """🔴 The ONLY way this process opens market_history.db. mode=ro cannot
    create the file or its sidecars, which a plain connect() would."""
    return sqlite3.connect("file:" + config.MARKET_HISTORY_DB_PATH + "?mode=ro", uri=True)


def _history_stats() -> dict:
    now = int(time.time())
    conn = _history_ro()
    conn.row_factory = sqlite3.Row
    try:
        tiers = []
        for table, ts_column in MARKET_HISTORY_TIERS:
            row = conn.execute(
                f"SELECT COUNT(*) AS rows, MAX({ts_column}) AS newest, "
                f"MIN({ts_column}) AS oldest FROM {table}").fetchone()
            newest = row["newest"] or 0
            tiers.append({
                "tier": table,
                "rows": row["rows"] or 0,
                "newest_ts": newest,
                "newest_age_s": (now - newest) if newest else None,
                "oldest_ts": row["oldest"] or 0,
            })
        templates_count = _scalar(conn, "SELECT COUNT(*) FROM templates")
        row = conn.execute(
            "SELECT v FROM mph_meta WHERE k = 'last_rollup_hour'").fetchone()
        last_rollup = int(row["v"]) if row and str(row["v"]).isdigit() else None
    finally:
        conn.close()

    current_hour = now - (now % 3600)
    lag_hours = None
    if last_rollup:
        lag_hours = round((current_hour - last_rollup) / 3600.0, 2)
    return {
        "available": True,
        "generated_at": now,
        "tiers": tiers,
        "templates": templates_count,
        "last_rollup_hour": last_rollup,
        "last_rollup_lag_hours": lag_hours,
        "file": _file_stats(config.MARKET_HISTORY_DB_PATH),
    }


@router.get("/api/dune/v2/economy/history")
def economy_history(request: Request):
    require_admin(request)
    return JSONResponse(_cached("economy_history", _history_stats))


def _watch_stats() -> dict:
    conn = get_db()
    try:
        watches = _scalar(conn, "SELECT COUNT(*) FROM portal_market_watch")
        armed = _scalar(conn,
                        "SELECT COUNT(*) FROM portal_market_watch WHERE armed = 1")
        accounts = _scalar(
            conn, "SELECT COUNT(DISTINCT account_id) FROM portal_market_watch")
        last_checked = conn.execute(
            "SELECT MAX(last_checked_at) FROM portal_market_watch").fetchone()[0]
        alerts_24h = _scalar(conn, """
            SELECT COUNT(*) FROM portal_market_alert
             WHERE created_at >= datetime('now', '-1 day')""")
        alerts_total = _scalar(conn, "SELECT COUNT(*) FROM portal_market_alert")
        dm_pending = _scalar(
            conn, "SELECT COUNT(*) FROM portal_market_alert WHERE dm_sent = 0")
        unseen = _scalar(
            conn, "SELECT COUNT(*) FROM portal_market_alert WHERE seen_in_portal = 0")
        recent = _rows(conn, """
            SELECT id, template_id, name_cached, threshold_price, match_price,
                   created_at, seen_in_portal, dm_sent
              FROM portal_market_alert
             ORDER BY id DESC LIMIT ?""", (PAGE_SIZE,))
        top = _rows(conn, """
            SELECT template_id, name_cached, COUNT(*) AS watchers,
                   MIN(max_price) AS lowest_threshold
              FROM portal_market_watch
             GROUP BY template_id ORDER BY watchers DESC, template_id LIMIT 10""")
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()
    return {
        "available": True,
        "generated_at": int(time.time()),
        "loop_enabled": config.MARKET_WATCH_ENABLED,
        "loop_interval_s": config.MARKET_WATCH_INTERVAL,
        "max_per_account": config.MARKET_WATCH_MAX_PER_ACCOUNT,
        "watches": watches,
        "watches_armed": armed,
        "accounts": accounts,
        "last_checked_at": last_checked,
        "alerts_24h": alerts_24h,
        "alerts_total": alerts_total,
        "alerts_dm_pending": dm_pending,
        "alerts_unseen": unseen,
        "top_templates": top,
        "recent_alerts": recent,
    }


@router.get("/api/dune/v2/economy/watch")
def economy_watch(request: Request):
    require_admin(request)
    return JSONResponse(_cached("economy_watch", _watch_stats))


@router.get("/api/dune/v2/economy/karum-wtb")
def economy_karum_wtb(request: Request):
    """portal_karum_requests: buy orders. Written since the WTB board shipped and
    never listed anywhere in the admin panel until now.

    quality_level NULL is LEGACY ANY-GRADE and 0 is Base, a real request. They
    are rendered as different things because conflating them is exactly the bug
    the writer guard exists to refuse."""
    require_admin(request)
    limit, offset = _page(request)
    status = (request.query_params.get("status") or "").strip()
    conn = get_db()
    try:
        where, params = "", []
        if status:
            where = "WHERE status = ?"
            params.append(status)
        rows = _rows(conn, f"""
            SELECT request_id, requester_name, template_id, display_name, category,
                   stack_size, price, quality_level, quality_mode, status,
                   filler_name, created_at, updated_at, filled_at, closed_at
              FROM portal_karum_requests
              {where}
             ORDER BY request_id DESC LIMIT ? OFFSET ?""",
                     tuple(params) + (limit + 1, offset))
        by_status = _rows(conn, """
            SELECT status, COUNT(*) AS n, COALESCE(SUM(price), 0) AS solari
              FROM portal_karum_requests GROUP BY status ORDER BY status""")
        total = _scalar(conn, "SELECT COUNT(*) FROM portal_karum_requests")
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()
    has_more = len(rows) > limit
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "total": total,
        "by_status": by_status,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "requests": rows[:limit],
    })


@router.get("/api/dune/v2/economy/blueprints")
def economy_blueprints(request: Request):
    require_admin(request)
    limit, offset = _page(request)
    conn = get_db()
    try:
        rows = _rows(conn, """
            SELECT publish_id, author_name, title, faction, piece_count,
                   has_paid_pieces, download_count, blob_bytes, status,
                   created_at, updated_at
              FROM portal_blueprint_market
             ORDER BY publish_id DESC LIMIT ? OFFSET ?""",
                     (limit + 1, offset))
        by_status = _rows(conn, """
            SELECT status, COUNT(*) AS n, COALESCE(SUM(download_count), 0) AS downloads,
                   COALESCE(SUM(blob_bytes), 0) AS bytes
              FROM portal_blueprint_market GROUP BY status ORDER BY status""")
        publishes_24h = _scalar(conn, """
            SELECT COUNT(*) FROM portal_blueprint_publish_log
             WHERE published_at >= datetime('now', '-1 day')""")
        publishers_24h = _scalar(conn, """
            SELECT COUNT(DISTINCT account_id) FROM portal_blueprint_publish_log
             WHERE published_at >= datetime('now', '-1 day')""")
        at_cap = _rows(conn, """
            SELECT account_id, COUNT(*) AS publishes
              FROM portal_blueprint_publish_log
             WHERE published_at >= datetime('now', '-1 day')
             GROUP BY account_id HAVING publishes >= ?
             ORDER BY publishes DESC LIMIT 10""",
                       (config.BLUEPRINT_PUBLISH_DAILY_CAP,))
        missing_blob = _scalar(conn, """
            SELECT COUNT(*) FROM portal_blueprint_market
             WHERE status = 'published' AND (blob_path = '' OR blob_bytes = 0)""")
        total = _scalar(conn, "SELECT COUNT(*) FROM portal_blueprint_market")
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()
    has_more = len(rows) > limit
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "total": total,
        "by_status": by_status,
        "publishes_24h": publishes_24h,
        "publishers_24h": publishers_24h,
        "daily_cap": config.BLUEPRINT_PUBLISH_DAILY_CAP,
        "accounts_at_cap": at_cap,
        "published_without_blob": missing_blob,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "publishes": rows[:limit],
    })


# --------------------------------------------------------------------------- #
# Social (read-only)
# --------------------------------------------------------------------------- #

@router.get("/api/dune/v2/social/guilds")
def social_guilds(request: Request):
    """Join requests, recruiting posts and per-guild inbox config. Character
    names only; account ids stay server-side, same rule as the portal."""
    require_admin(request)
    limit, offset = _page(request)
    conn = get_db()
    try:
        requests_rows = _rows(conn, """
            SELECT id, requester_char_name, guild_id, note, status,
                   created_at, expires_at
              FROM portal_guild_join_requests
             ORDER BY id DESC LIMIT ? OFFSET ?""", (limit + 1, offset))
        requests_by_status = _rows(conn, """
            SELECT status, COUNT(*) AS n FROM portal_guild_join_requests
             GROUP BY status ORDER BY status""")
        recruiting = _rows(conn, """
            SELECT guild_id, recruiting, playstyle, timezone, language,
                   new_player_friendly, set_by_char_name, updated_at,
                   LENGTH(COALESCE(blurb, '')) AS blurb_len
              FROM portal_guild_recruiting
             ORDER BY recruiting DESC, updated_at DESC LIMIT ?""", (PAGE_SIZE,))
        recruiting_open = _scalar(
            conn, "SELECT COUNT(*) FROM portal_guild_recruiting WHERE recruiting = 1")
        inbox = _rows(conn, """
            SELECT guild_id, view_min_role, manage_min_role, set_by_char_name,
                   updated_at FROM portal_guild_inbox_config
             ORDER BY updated_at DESC LIMIT ?""", (PAGE_SIZE,))
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "limit": limit,
        "offset": offset,
        "has_more": len(requests_rows) > limit,
        "join_requests": requests_rows[:limit],
        "join_requests_by_status": requests_by_status,
        "recruiting_open": recruiting_open,
        "recruiting": recruiting,
        "inbox_configs": inbox,
    })


@router.get("/api/dune/v2/social/messages")
def social_messages(request: Request):
    """Mailbox volume and the block list. Subjects and bodies are NOT read: this
    is a health surface, not a way to read players' mail."""
    require_admin(request)
    conn = get_db()
    try:
        volume = _rows(conn, """
            SELECT substr(created_at, 1, 10) AS day, kind, COUNT(*) AS n
              FROM portal_messages
             WHERE created_at >= datetime('now', '-14 day') AND deleted_at IS NULL
             GROUP BY day, kind ORDER BY day""")
        by_state = _rows(conn, """
            SELECT recipient_kind, state, COUNT(*) AS n
              FROM portal_messages WHERE deleted_at IS NULL
             GROUP BY recipient_kind, state ORDER BY recipient_kind, state""")
        total = _scalar(conn,
                        "SELECT COUNT(*) FROM portal_messages WHERE deleted_at IS NULL")
        deleted = _scalar(conn,
                          "SELECT COUNT(*) FROM portal_messages WHERE deleted_at IS NOT NULL")
        unread = _scalar(conn, """
            SELECT COUNT(*) FROM portal_messages
             WHERE state = 'unread' AND deleted_at IS NULL""")
        blocks = _scalar(conn, "SELECT COUNT(*) FROM portal_message_block")
        blockers = _scalar(
            conn, "SELECT COUNT(DISTINCT account_id) FROM portal_message_block")
        blocks_7d = _scalar(conn, """
            SELECT COUNT(*) FROM portal_message_block
             WHERE created_at >= datetime('now', '-7 day')""")
        top_senders = _rows(conn, """
            SELECT sender_char_name, COUNT(*) AS sent
              FROM portal_messages
             WHERE kind = 'user' AND sender_char_name IS NOT NULL
               AND created_at >= datetime('now', '-7 day')
             GROUP BY sender_char_name ORDER BY sent DESC LIMIT 10""")
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "total": total,
        "deleted": deleted,
        "unread": unread,
        "volume_14d": volume,
        "by_state": by_state,
        "blocks": blocks,
        "blockers": blockers,
        "blocks_7d": blocks_7d,
        "top_senders_7d": top_senders,
    })


@router.get("/api/dune/v2/social/gifts")
def social_gifts(request: Request):
    """Solari gift volume plus the two caps and the fan-in alert threshold, so a
    refusal count can be read against the limit that produced it."""
    require_admin(request)
    conn = get_db()
    try:
        volume = _rows(conn, """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS gifts,
                   COALESCE(SUM(CASE WHEN status IN ('applied','replay')
                                     THEN amount ELSE 0 END), 0) AS solari
              FROM portal_gift_events
             WHERE created_at >= datetime('now', '-14 day')
             GROUP BY day ORDER BY day""")
        by_status = _rows(conn, """
            SELECT status, COUNT(*) AS n FROM portal_gift_events
             GROUP BY status ORDER BY status""")
        refusals = _rows(conn, """
            SELECT COALESCE(fail_reason, 'unspecified') AS reason, COUNT(*) AS n
              FROM portal_gift_events
             WHERE status IN ('refused','failed')
               AND created_at >= datetime('now', '-30 day')
             GROUP BY reason ORDER BY n DESC LIMIT ?""", (PAGE_SIZE,))
        senders_at_cap = _rows(conn, """
            SELECT sender_identity, COUNT(*) AS gifts
              FROM portal_gift_events
             WHERE status = 'applied' AND created_at >= datetime('now', '-1 day')
             GROUP BY sender_identity HAVING gifts >= ?
             ORDER BY gifts DESC LIMIT 10""", (GIFT_MAX_PER_DAY,))
        inbound = _rows(conn, """
            SELECT recipient_identity, COALESCE(SUM(amount), 0) AS received
              FROM portal_gift_events
             WHERE status = 'applied' AND intra_identity = 0
               AND created_at >= datetime('now', '-30 day')
             GROUP BY recipient_identity HAVING received >= ?
             ORDER BY received DESC LIMIT 10""", (GIFT_INBOUND_ALERT_30D,))
        total = _scalar(conn, "SELECT COUNT(*) FROM portal_gift_events")
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "total": total,
        "volume_14d": volume,
        "by_status": by_status,
        "refusals_30d": refusals,
        "caps": {
            "per_identity_per_day": GIFT_MAX_PER_DAY,
            "per_pair_per_day": GIFT_MAX_PER_PAIR_PER_DAY,
            "max_amount": GIFT_MAX_AMOUNT,
            "inbound_alert_30d": GIFT_INBOUND_ALERT_30D,
        },
        "senders_at_cap_24h": senders_at_cap,
        "inbound_over_threshold_30d": inbound,
    })


# Heuristics only, and deliberately dumb ones: a hit is "worth a look", never a
# verdict, and nothing here hides or removes anything. A control character or an
# unbroken 40-char run in a blurb is what a rendering exploit looks like; a URL
# is what unsolicited advertising looks like.
_PROFILE_FLAGS = (
    ("control_chars", r"[\x00-\x08\x0b\x0c\x0e-\x1f]"),
    ("markup", r"[<>]"),
    ("link", r"(?i)https?://|discord\.gg/|www\."),
    ("long_run", r"\S{40,}"),
)


@router.get("/api/dune/v2/social/profiles")
def social_profiles(request: Request):
    require_admin(request)
    limit, offset = _page(request)
    conn = get_db()
    try:
        total = _scalar(conn, "SELECT COUNT(*) FROM portal_player_profile")
        opted_out = _scalar(
            conn, "SELECT COUNT(*) FROM portal_player_profile WHERE listed = 0")
        with_blurb = _scalar(conn, """
            SELECT COUNT(*) FROM portal_player_profile
             WHERE blurb IS NOT NULL AND TRIM(blurb) <> ''""")
        rows = _rows(conn, """
            SELECT char_name, listed, blurb, updated_at
              FROM portal_player_profile
             WHERE blurb IS NOT NULL AND TRIM(blurb) <> ''
             ORDER BY updated_at DESC
             LIMIT 500""")
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()

    flagged = []
    for row in rows:
        hits = [name for name, pattern in _PROFILE_FLAGS
                if re.search(pattern, row["blurb"] or "")]
        if hits:
            flagged.append({"char_name": row["char_name"], "listed": row["listed"],
                            "updated_at": row["updated_at"], "flags": hits,
                            "blurb": (row["blurb"] or "")[:280]})
    window = flagged[offset:offset + limit + 1]
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "total": total,
        "opted_out": opted_out,
        "with_blurb": with_blurb,
        "flagged_total": len(flagged),
        "heuristics": [name for name, _ in _PROFILE_FLAGS],
        "limit": limit,
        "offset": offset,
        "has_more": len(window) > limit,
        "flagged": window[:limit],
    })


@router.get("/api/dune/v2/social/lfg")
def social_lfg(request: Request):
    require_admin(request)
    limit, offset = _page(request)
    conn = get_db()
    try:
        rows = _rows(conn, """
            SELECT char_name, playstyle, timezone, role, note, updated_at, expires_at,
                   CASE WHEN expires_at IS NULL OR expires_at > datetime('now')
                        THEN 1 ELSE 0 END AS active
              FROM portal_lfg_seekers
             ORDER BY updated_at DESC LIMIT ? OFFSET ?""", (limit + 1, offset))
        total = _scalar(conn, "SELECT COUNT(*) FROM portal_lfg_seekers")
        active = _scalar(conn, """
            SELECT COUNT(*) FROM portal_lfg_seekers
             WHERE expires_at IS NULL OR expires_at > datetime('now')""")
        by_playstyle = _rows(conn, """
            SELECT COALESCE(NULLIF(TRIM(playstyle), ''), 'unset') AS playstyle,
                   COUNT(*) AS n FROM portal_lfg_seekers
             GROUP BY playstyle ORDER BY n DESC LIMIT 10""")
    except sqlite3.Error as exc:
        # the card must paint its own failure and leave the board reading (QA 9/3)
        return JSONResponse(_fail(exc))
    finally:
        conn.close()
    return JSONResponse({
        "available": True,
        "generated_at": int(time.time()),
        "total": total,
        "active": active,
        "by_playstyle": by_playstyle,
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) > limit,
        "seekers": rows[:limit],
    })
