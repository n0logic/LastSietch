#!/usr/bin/env python3
"""Regression tests for the Deliveries read (Fremkit wave 12, lane A).

The server sends a player two packages they never watch land. The Welcome
Package arrives on a new account and the Return Package after 28 days away, both
of them while the player is offline, both announced by a single whisper that is
gone by the time they log in. This is the first surface that tells them what was
sent and where it went, which makes being WRONG here worse than being absent:

  * A LEG IS READ, NEVER ASSUMED. Every leg state comes off a stamp column or a
    delivery-log row. The pre-v1.7 offline lane carries no per-leg stamp because
    it applied everything inline, so those legs read delivered at `granted_at`,
    and the rule is keyed on the pack version so it can never leak onto a v1.7
    row whose research and scrip genuinely have not landed yet.
  * THE EXCHANGE LEG IS NEVER "DELIVERED" ON ITS OWN. Overflow is listed on the
    CHOAM Exchange and its log line stays `pending` forever: the player takes the
    item at a terminal and nothing observes that. 336 live lines say pending
    today, and calling them delivered would tell every one of those players their
    items had already arrived.
  * A NULL IS NOT A ZERO. An unstamped leg is pending, an unreadable read is None
    all the way up, and a relay outage caches NOTHING: remembering an outage for
    five minutes would seal the card long after the box came back.
  * NOTHING IDENTIFYING LEAVES. No account id, no actor, no controller, no grant
    ids, no fls/funcom ids, and never the raw `notes` text, which names internal
    lanes and counts. Every key of the real shaped output is checked, recursively.

The fixtures below are the REAL payload shapes read off the live tables in the
read-only probe of 2026-09-05 (identifiers replaced with synthetic ones; a test
file is a public surface). The shaping module, both route bodies, the dispatcher
case and the game-host handler are EXECUTED here, not merely scanned.

Prod-safe: no host, no network, no game database, no admin.db.

Run:  python3 scripts/tests/test_portal_deliveries.py
"""
import ast
import asyncio
import collections
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
BACKEND = os.path.join(REPO, "admin-backend")

# The relocating-variant harness at the foot of this file re-runs the whole suite
# against a scratch COPY of the tree, so the files this suite READS come off
# SRC_ROOT while the sidecars it IMPORTS stay the repo's own.
SRC_ROOT = os.environ.get("PDELIV_ROOT") or REPO
CHILD = os.environ.get("PDELIV_CHILD") == "1"

DELIV_REL = os.path.join("admin-backend", "portal_deliveries.py")
ROUTER_REL = os.path.join("admin-backend", "routers", "portal_deliveries.py")
FEATURES_REL = os.path.join("admin-backend", "routers", "portal_features.py")
DISPATCH_REL = os.path.join("scripts", "dune-relay-dispatch.sh")
HANDLER_REL = os.path.join("scripts", "dune-deliveries-by-player.sh")

DELIV = os.path.join(SRC_ROOT, DELIV_REL)
ROUTER = os.path.join(SRC_ROOT, ROUTER_REL)
FEATURES = os.path.join(SRC_ROOT, FEATURES_REL)
DISPATCH = os.path.join(SRC_ROOT, DISPATCH_REL)
HANDLER = os.path.join(SRC_ROOT, HANDLER_REL)

# Never mutated: these belong to the Home suite and the unfurl suite.
PORTAL = os.path.join(BACKEND, "routers", "portal.py")
MAIN = os.path.join(BACKEND, "main.py")
UNFURL = os.path.join(BACKEND, "nextgen_unfurl.py")
RELAY_APP = os.path.join(REPO, "relay", "app.py")

# Anything matching this in a response key is an internal identifier and has no
# business on a page a player pastes into Discord. Same expression the Home suite
# applies to the Home body.
ID_KEY_RE = re.compile(r"account_id|controller|discord|identity|(^|_)code($|_)",
                       re.IGNORECASE)

ACCOUNT = 4242

# Written as an escape so this file can hold the pin without tripping it.
EM_DASH = "\u2014"

FEATURE_KEYS = ["amtal", "amtal_pvp", "augment", "chat", "game_login", "market_sell_backpack", "profile_login", "refinery", "reports", "shell_next", "sietch", "storage_move", "weekend_remind"]
FEATURE_PAIRS = [
    ("chat", "LASTSIETCH_CHAT_ENABLED"),
    ("refinery", "LASTSIETCH_REFINERY_ENABLED"),
    ("storage_move", "LASTSIETCH_STORAGE_MOVE_ENABLED"),
    ("market_sell_backpack", "LASTSIETCH_MARKET_SELL_BACKPACK_ENABLED"),
    ("augment", "LASTSIETCH_AUGMENT_ENABLED"),
    ("reports", "LASTSIETCH_REPORTS_ENABLED"),
    ("amtal_pvp", "LASTSIETCH_AMTAL_PVP_ENABLED"),
    ("sietch", "LASTSIETCH_SIETCH_NIGHTS_ENABLED"),
    ("shell_next", "LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED"),
    ("weekend_remind", "LASTSIETCH_WEEKEND_REMIND_ENABLED"),
]


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# The real probe payloads, 2026-09-05 (read-only)
# --------------------------------------------------------------------------- #

# The v1.7 plan as the online lane writes it: 27 template ids, of which the last
# eight overflowed the backpack and went to the CHOAM Exchange.
WELCOME_PLAN = [
    "SolarisCoin", "Combat_Choam_Light06_Helmet", "Combat_Choam_Light06_Chest",
    "Combat_Choam_Light06_Arms", "Combat_Choam_Light06_Legs",
    "Combat_Choam_Light06_Boots", "WaterPouchLarge", "SurvivalKit",
    "CuttertrayBlade", "PlasteelMicroflora", "MelangeSpice", "Stone", "IronOre",
    "CopperOre", "AluminumOre", "CarbonOre", "SteelIngot", "PlastoneCompound",
    "FuelCell",
    "OrnithopterLightLocomotion_6", "OrnithopterLightHullFront_6",
    "OrnithopterLightHullBack_6", "OrnithopterLightWings_6",
    "OrnithopterLightEngine_6", "OrnithopterLightCockpit_6", "BaseBackupTool",
    "RepairTool",
]
RMQ_IDS = WELCOME_PLAN[:19]
EXCHANGE_IDS = WELCOME_PLAN[19:]
OVERFLOW_IDS = ["OrnithopterLightLocomotion_6", "OrnithopterLightHullFront_6",
                "OrnithopterLightHullBack_6"]

# Verbatim off the host, fractional seconds and all. psql prints as many digits
# as the value has, so ".23799" is five, and datetime.fromisoformat before 3.11
# accepts only three or six: a hand-typed whole-second stamp would have hidden
# that, and a stamp _parse refuses sorts to the bottom and drops a skip window.
HOST_STAMPS = ("2026-09-05T14:27:11.073305+00:00",
               "2026-09-05T14:26:08.23799+00:00",
               "2026-09-04T21:41:01.371467+00:00")

V17_GRANTED_AT = HOST_STAMPS[1]
V17_RMQ_AT = HOST_STAMPS[0]
V17_EXCHANGE_AT = "2026-09-05T14:27:24.9+00:00"
V17_INTEL_AT = "2026-09-05T14:35:03.512+00:00"


def _lines():
    rows = []
    for i, tid in enumerate(RMQ_IDS):
        rows.append({"template_id": tid, "quantity": 1, "lane": "rmq",
                     "status": "delivered", "delivered_at": V17_RMQ_AT,
                     "created_at": "2026-09-05T14:26:%02d.23799+00:00" % (8 + i % 8)})
    for i, tid in enumerate(EXCHANGE_IDS):
        rows.append({"template_id": tid, "quantity": 1, "lane": "exchange",
                     "status": "pending", "delivered_at": None,
                     "created_at": "2026-09-05T14:27:%02d.073305+00:00" % (11 + i)})
    return rows


def welcome_v17(**over):
    """(1) The online lane: items over RMQ, overflow listed on the Exchange, and
    research + scrip deferred to a sweep that has not run for this row."""
    row = {
        "granted_at": V17_GRANTED_AT,
        "version": "v1.7-rmq",
        "granted_items": 27,
        "notes": ("v1.7-rmq: 19 via RMQ + 8 waiting in the exchange; "
                  "research/intel/scrip deferred to the offline sweep"),
        "whispered_at": "2026-09-05T14:27:30.44+00:00",
        "rmq_delivered_at": V17_RMQ_AT,
        "exchange_listed_at": V17_EXCHANGE_AT,
        "research_applied_at": None,
        "scrip_applied_at": None,
        "intel_applied_at": V17_INTEL_AT,
        "plan": list(WELCOME_PLAN),
        "overflow": list(OVERFLOW_IDS),
        "db_owed": None,
        "lines": _lines(),
    }
    row.update(over)
    return row


V16_GRANTED_AT = "2026-06-11T09:41:52.008+00:00"


def welcome_v16(**over):
    """(2) The offline lane: everything applied inline at grant time, no
    delivery_detail, no delivery-log rows, and only the intel sweep stamped."""
    row = {
        "granted_at": V16_GRANTED_AT,
        "version": "v1.6",
        "granted_items": 31,
        "notes": "v1.6: 27 main (armor + tools + resources) + 4 hotbar consumables",
        "whispered_at": "2026-06-11T09:42:00+00:00",
        "rmq_delivered_at": None,
        "exchange_listed_at": None,
        "research_applied_at": None,
        "scrip_applied_at": None,
        "intel_applied_at": "2026-06-11T09:50:00+00:00",
        "plan": None,
        "overflow": None,
        "db_owed": None,
        "lines": [],
    }
    row.update(over)
    return row


# (3) A re-roll inside the 30-day identity cooldown. The live row, verbatim: its
# window closed on 2026-08-24, so it is only shown to a clock standing before it.
SKIP = {"notified_at": "2026-07-27T00:18:47.373966+00:00",
        "remaining_days_at_skip": 28}
SKIP_ELIGIBLE_AT = "2026-08-24T00:18:47.373966+00:00"
# A clock inside the window, and one past it.
INSIDE = datetime(2026, 8, 1, tzinfo=timezone.utc)
OUTSIDE = datetime(2026, 9, 5, tzinfo=timezone.utc)

# (4) Tools fitted in the backpack.
RETURN_BACKPACK = {
    "granted_at": HOST_STAMPS[2],
    "tier": 3, "char_level": 200, "days_away": 28,
    "bank_done_at": "2026-09-04T21:41:02.118+00:00",
    "tools_done_at": "2026-09-04T21:41:03.9004+00:00",
    "whispered_at": None,
    "notes": "2 tool(s) to backpack (free was 43)",
    "bank_items": [{"template_id": "MelangeSpice", "quantity": 200, "quality": 0},
                   {"template_id": "Stone", "quantity": 500, "quality": 0}],
    "tool_items": [{"template_id": "BaseBackupTool", "quantity": 1}],
}

# (5) Backpack full, so the tools went to the bank instead.
RETURN_TO_BANK = {
    "granted_at": "2026-07-02T11:05:40.61+00:00",
    "tier": 2, "char_level": 140, "days_away": 61,
    "bank_done_at": "2026-07-02T11:05:41.223471+00:00",
    "tools_done_at": "2026-07-02T11:05:42.5+00:00",
    "whispered_at": "2026-07-04T18:00:00.101+00:00",
    "notes": "backpack full (free -4 < 3), tools to bank",
    "bank_items": [{"template_id": "MelangeSpice", "quantity": 200, "quality": 0}],
    "tool_items": [{"template_id": "BaseBackupTool", "quantity": 1}],
}

# A return whose legs have not run yet: the watch stamped the grant, the sweep
# has not landed either half.
RETURN_PENDING = {
    "granted_at": "2026-09-04T09:20:00.874113+00:00",
    "tier": 1, "char_level": 60, "days_away": 30,
    "bank_done_at": None, "tools_done_at": None, "whispered_at": None,
    "notes": None, "bank_items": [], "tool_items": [],
}

def welcome_version(version, **over):
    """A welcome row that differs from the offline lane ONLY in its version, so a
    pin about the version rule cannot pass on some other difference."""
    return welcome_v16(version=version, intel_applied_at=None, **over)


# (6) An account the server has never sent anything.
EMPTY = {"account_id": 1, "welcome": None, "welcome_skip": None, "returns": []}

# (7) The relay could not read the game host.
UNAVAILABLE = {"available": False, "error": "lastsietch-dune command failed: timed out",
               "account_id": 1, "welcome": None, "welcome_skip": None, "returns": []}


def payload(welcome=None, skip=None, returns=()):
    return {"account_id": 1, "welcome": welcome, "welcome_skip": skip,
            "returns": list(returns)}


# --------------------------------------------------------------------------- #
# The exec harness
# --------------------------------------------------------------------------- #

class _Resp:
    """Stands in for JSONResponse(payload, status_code=..., headers=...)."""

    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = dict(headers or {})


class _Request:
    pass


class _Router:
    def __init__(self):
        self.routes = []

    def _record(self, method, path):
        self.routes.append((method, path))
        return lambda fn: fn

    def get(self, path):
        return self._record("get", path)

    def post(self, path):
        return self._record("post", path)


def _module():
    """The REAL text of portal_deliveries.py, executed. Its only non-stdlib
    imports are the two sidecars every storage row already uses, so the shaping
    this exercises is the shipped shaping and the names are the real ones."""
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    ns = {"__name__": "portal_deliveries_under_test", "__file__": DELIV}
    exec(compile(_read(DELIV), DELIV, "exec"), ns)  # noqa: S102
    return ns


_MOD = None


def mod():
    global _MOD
    if _MOD is None:
        _MOD = _module()
    return _MOD


def shape(raw, **kw):
    return mod()["shape"](raw, **kw)


def _lift(path, names, extra_ns=None):
    """Exec the named module-level constants and functions out of `path` into a
    fresh namespace, DECORATORS INTACT, so the route registration is captured by
    the stub router. The function text is the real one; only its collaborators
    are ours."""
    tree = ast.parse(_read(path))
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names
                for target in node.targets for t in ast.walk(target)):
            wanted.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            wanted.append(node)
    ns = {"json": json, "os": os, "re": re, "asyncio": asyncio,
          "logger": logging.getLogger("portal-deliveries-test"),
          "Request": _Request, "JSONResponse": _Resp}
    ns.update(extra_ns or {})
    exec(compile(ast.Module(body=wanted, type_ignores=[]), path, "exec"), ns)  # noqa: S102
    missing = [n for n in names if n not in ns]
    assert not missing, "%s no longer defines %s" % (os.path.basename(path), missing)
    return ns


def _deliveries_route(loaded=None, raises=False, gate=None, early=None):
    """The shipped route body, executed against a stub loader and gate."""
    async def _load(account_id):
        assert account_id == ACCOUNT, "the route asked about the wrong account"
        if raises:
            raise RuntimeError("the loader blew up")
        return loaded

    router = _Router()
    if gate is None and early is None:
        gate = ("session", "did-9000000000000001", ACCOUNT, {"character_name": "Stilgar"})
    ns = _lift(ROUTER, ("portal_deliveries_v2",), {
        "router": router,
        "portal_deliveries": types.SimpleNamespace(load=_load),
        "_require_linked_session_json": lambda _req: (gate, early),
    })
    return ns, router


def _map_instances():
    """The real loader (stdlib only); with no config file it serves the defaults."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "map_instances_for_features", os.path.join(REPO, "admin-backend", "map_instances.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _features_route(values=None, calls=None):
    router = _Router()
    seen = calls if calls is not None else []

    def _enabled(name, default=False):
        seen.append((name, default))
        return bool((values or {}).get(name, False))

    ns = _lift(FEATURES, ("FEATURE_GATES", "PUBLIC_INSTANCE_FIELDS", "public_instances", "portal_features"), {
        "router": router,
        "feature_flags": types.SimpleNamespace(enabled=_enabled),
        "map_instances": _map_instances(),
    })
    return ns, router, seen


def _walk_keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            _walk_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_keys(v, out)


def _legs(package):
    return {leg["key"]: leg for leg in package["legs"]}


# --------------------------------------------------------------------------- #
# 1. shape(), against the real probe payloads
# --------------------------------------------------------------------------- #

def test_v17_welcome_reads_every_leg_off_its_own_column():
    out = shape(payload(welcome=welcome_v17()))
    assert out["available"] is True
    assert len(out["packages"]) == 1
    pkg = out["packages"][0]
    assert pkg["kind"] == "welcome" and pkg["label"] == "Welcome Package"
    assert pkg["granted_at"] == V17_GRANTED_AT
    assert pkg["items_total"] == 27
    legs = _legs(pkg)
    assert legs["backpack"]["state"] == "delivered"
    assert legs["backpack"]["at"] == V17_RMQ_AT, \
        "the RMQ lane's backpack leg is not stamped by rmq_delivered_at"
    assert legs["intel"]["state"] == "delivered" and legs["intel"]["at"] == V17_INTEL_AT
    assert pkg["state"] == "partial", \
        "a package with landed and unlanded legs is not partial: %s" % pkg["state"]


def test_v17_research_and_scrip_are_pending_with_the_logout_note():
    """41 live v1.7 rows carry NULL in both columns because the sweep that was
    supposed to apply them does not exist yet. Reading those as delivered would
    tell every one of those players they already have their research."""
    legs = _legs(shape(payload(welcome=welcome_v17()))["packages"][0])
    for key in ("research", "scrip"):
        assert legs[key]["state"] == "pending", \
            "%s read %s on a v1.7 row with a NULL stamp" % (key, legs[key]["state"])
        assert legs[key]["at"] is None, "a pending leg carries a landing time"
        assert legs[key]["note"] == "Lands the next time you log out and back in.", \
            "the deferred leg does not tell the player when it lands: %r" % legs[key]["note"]


def test_the_exchange_leg_is_waiting_and_says_what_to_press():
    legs = _legs(shape(payload(welcome=welcome_v17()))["packages"][0])
    assert legs["exchange"]["state"] == "waiting", \
        "the exchange leg read %s; it is never delivered while its lines are pending" \
        % legs["exchange"]["state"]
    assert legs["exchange"]["label"] == "CHOAM Exchange, Completed tab"
    assert legs["exchange"]["at"] == V17_EXCHANGE_AT
    assert legs["exchange"]["note"] == \
        "Shows as CANCELED. Press Take item at any tradepost terminal.", \
        "the exchange copy is not the public site's: %r" % legs["exchange"]["note"]


def test_the_exchange_leg_only_reads_delivered_when_every_line_says_so():
    """One taken item out of eight is not a finished leg."""
    rows = _lines()
    exchange = [r for r in rows if r["lane"] == "exchange"]
    exchange[0]["status"] = "delivered"
    legs = _legs(shape(payload(welcome=welcome_v17(lines=rows)))["packages"][0])
    assert legs["exchange"]["state"] == "waiting", \
        "one delivered line out of eight finished the whole exchange leg"

    for row in exchange:
        row["status"] = "delivered"
    legs = _legs(shape(payload(welcome=welcome_v17(lines=rows)))["packages"][0])
    assert legs["exchange"]["state"] == "delivered", \
        "every exchange line says delivered and the leg still says waiting"


def test_a_welcome_with_no_overflow_grows_no_exchange_leg():
    rows = [r for r in _lines() if r["lane"] == "rmq"]
    legs = _legs(shape(payload(
        welcome=welcome_v17(lines=rows, overflow=None)))["packages"][0])
    assert "exchange" not in legs, \
        "a package with nothing on the Exchange still shows an Exchange leg"


def test_v17_items_carry_a_name_an_icon_and_where_they_landed():
    pkg = shape(payload(welcome=welcome_v17()))["packages"][0]
    assert len(pkg["items"]) == 27, "the item list is not the delivery log"
    by_name = {it["name"]: it for it in pkg["items"]}
    assert "Solari" in by_name, \
        "SolarisCoin did not resolve to Solari: %s" % sorted(by_name)[:4]
    solari = by_name["Solari"]
    assert solari["where"] == "backpack" and solari["state"] == "delivered"
    assert solari["icon"], "an item reached the card with no icon"
    exchanged = [it for it in pkg["items"] if it["where"] == "exchange"]
    assert len(exchanged) == 8, "the exchange lane did not place eight items"
    assert all(it["state"] == "waiting" for it in exchanged), \
        "a pending exchange item reads as pending rather than waiting"


def test_a_line_the_log_cannot_explain_reads_unknown():
    """The delivery log also carries `undelivered` and `failed`, and a lane this
    module has never seen would arrive as a plain string. Neither is delivered
    and neither is pending: "unknown" is the honest word, and it is the one the
    contract reserves for exactly this."""
    rows = [{"template_id": "SurvivalKit", "quantity": 1, "lane": "rmq",
             "status": "failed", "delivered_at": None,
             "created_at": V17_GRANTED_AT},
            {"template_id": "WaterPouchLarge", "quantity": 2, "lane": "courier",
             "status": "delivered", "delivered_at": V17_RMQ_AT,
             "created_at": V17_GRANTED_AT}]
    items = shape(payload(
        welcome=welcome_v17(lines=rows, overflow=None)))["packages"][0]["items"]
    assert [it["state"] for it in items] == ["unknown", "delivered"], \
        "a failed line was reported as landed: %s" % [it["state"] for it in items]
    assert [it["where"] for it in items] == ["backpack", "unknown"], \
        "an unrecognised lane was placed somewhere specific: %s" % [it["where"] for it in items]


def test_db_owed_items_land_as_pending_in_the_backpack():
    owed = ["SurvivalKit", "WaterPouchLarge"]
    pkg = shape(payload(welcome=welcome_v17(db_owed=owed)))["packages"][0]
    tail = pkg["items"][-2:]
    assert [it["where"] for it in tail] == ["backpack", "backpack"], \
        "an owed item was placed somewhere it will not arrive: %s" % [it["where"] for it in tail]
    assert [it["state"] for it in tail] == ["pending", "pending"], \
        "an item the server still owes reads as delivered"
    assert all(it["quantity"] is None for it in tail), \
        "an owed item invented a quantity the detail does not carry"


def test_the_older_offline_lane_reads_delivered_at_granted_at():
    """v1.6 and older applied research, scrip and the items inline and wrote no
    per-leg column at all. `granted_at` IS when they landed."""
    out = shape(payload(welcome=welcome_v16()))
    pkg = out["packages"][0]
    legs = _legs(pkg)
    for key in ("backpack", "research", "scrip"):
        assert legs[key]["state"] == "delivered", \
            "%s read %s on a pre-v1.7 row" % (key, legs[key]["state"])
        assert legs[key]["at"] == V16_GRANTED_AT, \
            "%s is not stamped by granted_at on the offline lane" % key
    assert pkg["state"] == "delivered"
    assert pkg["items"] == [], \
        "the offline lane wrote no delivery-log rows, so an item list was invented"
    assert pkg["items_total"] == 31, "the offline lane's count is not granted_items"


UNKNOWN_VERSIONS = (None, "", "REGRANT-REROLL-HEAL", "v2.0", "1.6", "v1.9")


def test_the_pack_version_decides_the_lane_and_the_third_case_is_not_a_fallback():
    """Three lanes, matched exactly. v1.0.1 through v1.6 applied the whole pack
    inline; v1.7 delivers over RMQ. Everything else is a THIRD answer, because
    `notes` is NULL on some rows and the re-roll heal writes a transient marker
    into it, and both used to fall through to the offline rules and report a
    backpack, research and scrip the server may never have sent."""
    lane_for = mod()["_lane_for"]
    for version in ("v1.7", "v1.7-rmq", "v1.7.2", "v1.7x", "v1.8-rmq"):
        assert lane_for(version) == "rmq", version
    # v1.8 is the 2026-10-20 Mk3 pack: the offline lane writes 'v1.8', the
    # online lane 'v1.8-rmq'. Only the -rmq suffix moves it to the RMQ lane.
    for version in ("v1.0.1", "v1.2", "v1.6", "v1.6-hotfix", "v1.8"):
        assert lane_for(version) == "offline", version
    for version in UNKNOWN_VERSIONS:
        assert lane_for(version) == "unknown", \
            "%r was read as the %s lane" % (version, lane_for(version))


def test_real_notes_strings_land_in_their_lane():
    """The relay derives version as split_part(notes, ':', 1). Feed it the
    strings each lane actually writes: grant.sh 'v1.6:' and 'v1.8:' (offline,
    research and scrip applied inline), grant-online 'v1.7-rmq' and 'v1.8-rmq'
    (RMQ, research and scrip deferred to the sweep)."""
    lane_for = mod()["_lane_for"]
    cases = (
        ("v1.6: 27 main (armor + Replica Pulse-sword + Maula Pistol Mk5) + 4 hotbar", "offline"),
        ("v1.7-rmq: 19 via RMQ + 8 waiting in the exchange; research/intel/scrip deferred to the offline sweep", "rmq"),
        ("v1.8: 27 main (Mk3 fight gear: Slaver Scout armor + Artisan Sword) + 4 hotbar", "offline"),
        ("v1.8-rmq: 19 via RMQ + 8 waiting in the exchange; research/intel/scrip deferred to the offline sweep", "rmq"),
    )
    for notes, lane in cases:
        version = notes.split(":", 1)[0]
        assert lane_for(version) == lane, "%r read as %s, expected %s" % (notes[:12], lane_for(version), lane)


def test_an_unrecognised_pack_version_reads_unknown_and_never_delivered():
    for version in UNKNOWN_VERSIONS:
        pkg = shape(payload(welcome=welcome_version(version)))["packages"][0]
        legs = _legs(pkg)
        assert set(legs) == {"backpack", "exchange", "research", "scrip", "intel"} \
            or set(legs) == {"backpack", "research", "scrip", "intel"}, sorted(legs)
        for key, leg in legs.items():
            assert leg["state"] == "unknown", \
                "%r reported %s as %s" % (version, key, leg["state"])
            assert leg["at"] is None, "%r invented a landing time for %s" % (version, key)
            assert leg["note"] is None, "%r invented a note for %s" % (version, key)
        assert pkg["state"] == "pending", \
            "%r settled on %s for the whole package" % (version, pkg["state"])
        assert pkg["items"] == [], "%r produced an item list" % (version,)


def test_an_unrecognised_version_still_shows_its_exchange_leg_as_unknown():
    rows = [r for r in _lines() if r["lane"] == "exchange"]
    pkg = shape(payload(welcome=welcome_version(
        "REGRANT-REROLL-HEAL", lines=rows, overflow=list(OVERFLOW_IDS))))["packages"][0]
    leg = _legs(pkg)["exchange"]
    assert leg["state"] == "unknown", leg
    assert leg["at"] is None and leg["note"] is None, leg


def test_intel_reads_its_own_stamp_on_every_version():
    """The intel sweep is the one that has always run. It is stamped or it is
    not, and the pack version has no say in it."""
    legs = _legs(shape(payload(
        welcome=welcome_v16(intel_applied_at=None)))["packages"][0])
    assert legs["intel"]["state"] == "pending", \
        "an unstamped intel leg read as delivered because the row was old"
    assert legs["intel"]["at"] is None


def test_the_skip_notice_carries_when_the_next_one_may_land():
    out = shape(payload(skip=SKIP), now=INSIDE)
    assert out["skip"] == {"notified_at": SKIP["notified_at"],
                           "eligible_at": SKIP_ELIGIBLE_AT}, out["skip"]
    assert "remaining_days_at_skip" not in (out["skip"] or {}), \
        "the raw cooldown counter leaked instead of the date it resolves to"


def test_a_fresh_start_window_that_has_already_closed_is_dropped():
    """The skip row is permanent and the 34 live ones go back to the spring. A
    card that keeps telling a player about a wait that ended in July is worse
    than no card, so a closed window is dropped rather than shown expired."""
    assert shape(payload(skip=SKIP), now=OUTSIDE)["skip"] is None, \
        "a cooldown that ran out on %s is still on the card" % SKIP_ELIGIBLE_AT
    edge = _MOD_ELIGIBLE = None  # noqa: F841 - readability only
    from datetime import timedelta as _td
    just_open = mod()["_parse"](SKIP_ELIGIBLE_AT) - _td(seconds=1)
    assert shape(payload(skip=SKIP), now=just_open)["skip"] is not None, \
        "a window with a second left on it was dropped early"
    assert shape(payload(skip=SKIP),
                 now=mod()["_parse"](SKIP_ELIGIBLE_AT))["skip"] is None, \
        "the window is still open at the exact moment it runs out"


def test_a_skip_window_that_cannot_be_resolved_is_dropped():
    """Either half missing means there is no date to promise, and a notice with
    no date is a notice about nothing."""
    for broken in ({"notified_at": None, "remaining_days_at_skip": 28},
                   {"notified_at": SKIP["notified_at"], "remaining_days_at_skip": None},
                   {"notified_at": "not a stamp", "remaining_days_at_skip": 28},
                   {}):
        assert shape(payload(skip=broken), now=INSIDE)["skip"] is None, \
            "an unresolvable cooldown was shown anyway: %r" % broken


def test_a_return_package_with_tools_in_the_backpack():
    pkg = shape(payload(returns=[RETURN_BACKPACK]))["packages"][0]
    assert pkg["kind"] == "return"
    assert pkg["label"] == "Return Package (Tier 3)", pkg["label"]
    legs = _legs(pkg)
    assert legs["bank"]["state"] == "delivered"
    assert legs["bank"]["at"] == RETURN_BACKPACK["bank_done_at"]
    assert legs["tools"]["state"] == "delivered"
    assert legs["tools"]["note"] == "In your backpack.", legs["tools"]["note"]
    assert pkg["state"] == "delivered"
    where = [it["where"] for it in pkg["items"]]
    assert where == ["bank", "bank", "backpack"], where
    quantities = [it["quantity"] for it in pkg["items"]]
    assert quantities == [200, 500, 1], quantities
    assert pkg["items_total"] == 3


def test_a_return_package_whose_tools_went_to_the_bank():
    """"backpack full ... tools to bank" is the other half of the writer's own
    wording, and the item row has to follow the note or the player looks in the
    wrong container."""
    pkg = shape(payload(returns=[RETURN_TO_BANK]))["packages"][0]
    legs = _legs(pkg)
    assert legs["tools"]["state"] == "delivered"
    assert legs["tools"]["note"] == "Your backpack was full, so they wait in the bank.", \
        legs["tools"]["note"]
    assert [it["where"] for it in pkg["items"]] == ["bank", "bank"], \
        "the tools were sent to the bank and the card still says backpack"


def test_a_return_package_with_nothing_stamped_is_pending():
    pkg = shape(payload(returns=[RETURN_PENDING]))["packages"][0]
    legs = _legs(pkg)
    assert legs["bank"]["state"] == "pending" and legs["bank"]["at"] is None
    assert legs["tools"]["state"] == "pending"
    assert legs["tools"]["note"] == "Lands while you are offline."
    assert pkg["state"] == "pending"
    assert pkg["items_total"] is None, \
        "a package with no item rows read a total of 0 instead of nothing"


def test_the_raw_notes_text_never_reaches_the_output():
    """`notes` names internal lanes and counts ("19 via RMQ + 8 waiting in the
    exchange"). It is READ, to pick between two return outcomes, and dropped."""
    out = shape(payload(welcome=welcome_v17(), skip=SKIP,
                        returns=[RETURN_BACKPACK, RETURN_TO_BANK]), now=INSIDE)
    blob = json.dumps(out)
    for leak in ("via RMQ", "deferred to the offline sweep", "free was 43",
                 "backpack full (free", "27 main"):
        assert leak not in blob, "the raw notes text reached the body: %r" % leak


def test_packages_are_newest_first_across_both_kinds():
    out = shape(payload(welcome=welcome_v17(), skip=SKIP,
                        returns=[RETURN_BACKPACK, RETURN_TO_BANK]), now=INSIDE)
    stamps = [p["granted_at"] for p in out["packages"]]
    assert stamps == sorted(stamps, reverse=True), stamps
    assert stamps[0] == welcome_v17()["granted_at"], \
        "the newest package is not first: %s" % stamps
    assert [p["kind"] for p in out["packages"]] == ["welcome", "return", "return"], \
        "the two kinds are not interleaved by granted_at"


def test_the_summary_counts_what_is_still_landing():
    out = shape(payload(welcome=welcome_v17(), returns=[RETURN_BACKPACK, RETURN_PENDING]))
    summary = out["summary"]
    assert summary["count"] == 3, summary
    # the v1.7 welcome (partial) and the unstamped return (pending). The fully
    # delivered return is not counted, which is the whole point of the number.
    assert summary["pending_count"] == 2, summary
    assert summary["latest"] == {
        "kind": "welcome", "label": "Welcome Package",
        "granted_at": welcome_v17()["granted_at"], "state": "partial"}, summary["latest"]
    assert summary["latest"]["granted_at"] == max(
        p["granted_at"] for p in out["packages"]), "latest is not the newest package"


def test_an_account_sent_nothing_is_empty_but_available():
    out = shape(EMPTY)
    assert out["available"] is True, "an empty account read as unavailable"
    assert out["packages"] == [] and out["skip"] is None
    assert out["summary"] == {"count": 0, "pending_count": 0, "latest": None}, out["summary"]


def test_an_unavailable_read_seals_instead_of_reading_empty():
    out = shape(UNAVAILABLE)
    assert out["available"] is False, \
        "a failed relay read reported itself as an account with no packages"
    assert out["packages"] == [] and out["skip"] is None
    assert out["summary"] is None, \
        "an unavailable read handed the card a summary it could not have computed"
    assert "error" not in out, "the relay's error text reached the player"


def test_a_null_is_never_a_zero():
    """Every optional number in the contract has to survive as null. A 0 is a
    different sentence and it is the wrong one."""
    out = shape(payload(welcome=welcome_v16(granted_items=None),
                        returns=[RETURN_PENDING]))
    welcome = [p for p in out["packages"] if p["kind"] == "welcome"][0]
    assert welcome["items_total"] is None, \
        "an unknown item count became %r" % welcome["items_total"]
    ret = [p for p in out["packages"] if p["kind"] == "return"][0]
    assert ret["items_total"] is None
    for leg in ret["legs"]:
        if leg["state"] != "delivered":
            assert leg["at"] is None, "an unlanded leg carries a landing time"
    skipped = shape(payload(skip={"notified_at": None, "remaining_days_at_skip": None}))
    assert skipped["skip"] is None, skipped["skip"]


def test_real_host_stamps_parse_and_survive_the_shaping():
    """psql prints as many fractional digits as the value has. Five of them is a
    stamp datetime.fromisoformat refuses before 3.11, and a refused stamp sorts
    to the bottom of the package list and silently drops a skip window."""
    parse = mod()["_parse"]
    for stamp in HOST_STAMPS:
        assert parse(stamp) is not None, "the shaping cannot read %r" % stamp
    assert parse("2026-09-05T14:26:08.23799+00:00").microsecond == 237990, \
        "a five-digit fraction was misread"
    assert parse("not a stamp") is None and parse(None) is None

    out = shape(payload(welcome=welcome_v17(), returns=[RETURN_BACKPACK]))
    # A stamp the card shows is the host's own text, passed through, and every
    # one of them is still a parseable ISO string on the way out.
    ats = [leg["at"] for pkg in out["packages"] for leg in pkg["legs"] if leg["at"]]
    ats += [pkg["granted_at"] for pkg in out["packages"]]
    assert ats, "no stamp survived the shaping"
    for value in ats:
        assert isinstance(value, str) and parse(value) is not None, \
            "a stamp came out unparseable: %r" % value
    assert V17_RMQ_AT in ats and HOST_STAMPS[2] in ats, \
        "a host stamp was reformatted instead of passed through"

    # The pad is what makes the five-digit case safe on a runtime older than
    # 3.11, where fromisoformat accepts only three or six digits. CI and this
    # machine run 3.12, which accepts any width, so _parse cannot demonstrate
    # the pad here and the ORDER is pinned instead: a pad that runs after the
    # parse it exists to protect is the same as no pad, and <web-host>'s
    # interpreter is not this one.
    body = _read(DELIV)
    body = body[body.index("def _parse("):]
    body = body[:body.index("\n\ndef ")]
    assert body.index("_FRACTION_RE.sub(") < body.index("datetime.fromisoformat("), \
        "the fraction pad runs after the parse it exists to protect"


def test_no_internal_identifier_reaches_the_shaped_body():
    keys = set()
    _walk_keys(shape(payload(welcome=welcome_v17(), skip=SKIP,
                             returns=[RETURN_BACKPACK, RETURN_TO_BANK]),
                     now=INSIDE), keys)
    bad = sorted(k for k in keys if ID_KEY_RE.search(k))
    assert not bad, "internal identifiers on the deliveries body: %s" % bad
    for banned in ("actor", "granted_actor_id", "fls_id", "funcom_id",
                   "bank_grant_ids", "tools_grant_ids", "notes", "plan",
                   "whispered_at", "player_controller_id"):
        assert banned not in keys, "%s reached the response body" % banned


# --------------------------------------------------------------------------- #
# 2. The per-account cache
# --------------------------------------------------------------------------- #

def _relay_stub(answers):
    """A fake `relay` module. `answers` is popped per call, so a second relay
    round trip is visible as a second consumed answer."""
    calls = []

    async def call_relay(path, **kw):
        calls.append(path)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    sys.modules["relay"] = types.ModuleType("relay")
    sys.modules["relay"].call_relay = call_relay
    return calls


def _fresh():
    """A private copy of the module, so one test's cache is not another's."""
    ns = _module()
    ns["_cache"].clear()
    return ns


def test_a_second_read_inside_the_ttl_does_not_touch_the_relay():
    ns = _fresh()
    calls = _relay_stub([payload(welcome=welcome_v17())])
    first = asyncio.run(ns["load"](ACCOUNT, now=1000.0))
    second = asyncio.run(ns["load"](ACCOUNT, now=1000.0 + ns["CACHE_TTL_S"] - 1))
    assert first is second, "the cached read was re-shaped instead of returned"
    assert len(calls) == 1, "the cache did not hold: %d relay calls" % len(calls)


def test_the_cache_expires_after_its_ttl():
    ns = _fresh()
    calls = _relay_stub([payload(welcome=welcome_v17()), payload(welcome=welcome_v16())])
    asyncio.run(ns["load"](ACCOUNT, now=1000.0))
    later = asyncio.run(ns["load"](ACCOUNT, now=1000.0 + ns["CACHE_TTL_S"] + 1))
    assert len(calls) == 2, "the entry never expired"
    assert later["packages"][0]["items_total"] == 31, "the stale answer was served"
    assert ns["CACHE_TTL_S"] == 300.0, "the contract TTL moved: %s" % ns["CACHE_TTL_S"]


def test_the_cache_is_bounded():
    ns = _fresh()
    ns["CACHE_MAX"] = 20
    for i in range(60):
        ns["_remember"](i, {"available": True}, float(i), ns["CACHE_TTL_S"])
    assert len(ns["_cache"]) <= 20, \
        "the per-account cache grew past its bound: %d" % len(ns["_cache"])
    assert 59 in ns["_cache"], "the newest entry was the one evicted"
    assert _module()["CACHE_MAX"] == 2000, "the shipped bound is not the contract's"


def test_a_relay_failure_returns_none_and_is_remembered_briefly():
    """Home reads this on every poll. Without a negative cache one outage is a
    fresh 12 second ssh attempt per request per player against a box that is
    already not answering; with the full TTL it would seal the card for five
    minutes after the relay came back."""
    ns = _fresh()
    assert ns["FAIL_TTL_S"] == 30.0, \
        "the negative-cache window moved: %s" % ns["FAIL_TTL_S"]
    assert ns["FAIL_TTL_S"] < ns["CACHE_TTL_S"], \
        "an outage is remembered as long as an answer"
    calls = _relay_stub([RuntimeError("the relay is down"),
                         payload(welcome=welcome_v17())])
    assert asyncio.run(ns["load"](ACCOUNT, now=1000.0)) is None, \
        "a dead relay produced an answer"
    # Remembered, and still None: a sealed card, at no cost to the game host.
    assert asyncio.run(ns["load"](ACCOUNT, now=1000.0 + 29)) is None
    assert len(calls) == 1, \
        "the outage was retried %d times inside its window" % len(calls)
    # ...and released on time, not held for the full answer TTL.
    assert asyncio.run(ns["load"](ACCOUNT, now=1000.0 + 31)) is not None, \
        "the card stayed sealed after the negative-cache window closed"
    assert len(calls) == 2


def test_a_failed_read_never_writes_a_traceback():
    """One outage times 500 players times a poll a minute is a log nobody can
    read and a disk nobody expected to fill. The relay also puts the failing
    path, which carries the account id, into its own error detail, so the
    exception TYPE is logged and its text is not."""
    body = _read(DELIV)
    body = body[body.index("async def load("):]
    assert "exc_info" not in body, \
        "the deliveries loader writes a traceback on a read Home makes every poll"
    assert "type(exc).__name__" in body, \
        "the loader logs the relay error text instead of the exception type"

    ns = _fresh()
    _relay_stub([RuntimeError("the relay is down")])
    logged = []
    ns["logger"] = types.SimpleNamespace(
        warning=lambda msg, *a: logged.append(str(msg) % a if a else str(msg)))
    assert asyncio.run(ns["load"](ACCOUNT, now=1000.0)) is None
    assert logged and "RuntimeError" in logged[0], logged
    assert "the relay is down" not in " ".join(logged), \
        "the relay error text reached the log"


def test_an_unavailable_relay_answer_returns_none_and_is_remembered_briefly():
    ns = _fresh()
    calls = _relay_stub([UNAVAILABLE, UNAVAILABLE])
    assert asyncio.run(ns["load"](ACCOUNT, now=1000.0)) is None, \
        "available:false from the relay was shaped into an answer"
    assert asyncio.run(ns["load"](ACCOUNT, now=1000.0 + 29)) is None
    assert len(calls) == 1, "an unavailable read was retried inside its window"
    assert ns["_cache"][ACCOUNT][1] is None, \
        "an unavailable read was cached as an answer rather than as a failure"


def test_an_empty_account_is_cached_because_it_is_an_answer():
    ns = _fresh()
    calls = _relay_stub([EMPTY])
    out = asyncio.run(ns["load"](ACCOUNT, now=1000.0))
    assert out["available"] is True and out["packages"] == []
    asyncio.run(ns["load"](ACCOUNT, now=1001.0))
    assert len(calls) == 1, "a genuinely empty answer was not cached"


def test_the_relay_read_gives_up_before_home_does():
    """Home gathers nine loaders, so the landing page waits for the slowest one
    on a cold cache. The relay answers available:false at its own 20 second ssh
    budget and this loader returns None; both seal the card in exactly the same
    way, so waiting the full budget buys nothing and costs a visibly slower
    Home."""
    ns = _fresh()
    assert ns["RELAY_TIMEOUT_S"] == 12.0, \
        "the deliveries relay timeout moved: %s" % ns["RELAY_TIMEOUT_S"]
    assert ns["RELAY_TIMEOUT_S"] < 20, \
        "the loader waits longer than the relay's own ssh budget"

    seen = {}

    async def call_relay(path, **kw):
        seen.update(kw)
        return EMPTY

    sys.modules["relay"] = types.ModuleType("relay")
    sys.modules["relay"].call_relay = call_relay
    asyncio.run(ns["load"](ACCOUNT, now=1000.0))
    assert seen.get("timeout") == 12.0, \
        "the loader did not pass its own timeout to the relay: %s" % seen


def test_the_loader_asks_the_relay_for_its_own_route():
    ns = _fresh()
    calls = _relay_stub([EMPTY])
    asyncio.run(ns["load"](ACCOUNT, now=1000.0))
    assert calls == ["/dune/player/%d/deliveries" % ACCOUNT], calls


# --------------------------------------------------------------------------- #
# 3. GET /portal/deliveries/v2
# --------------------------------------------------------------------------- #

def test_the_deliveries_route_is_registered_at_the_contract_path():
    _ns, router = _deliveries_route(loaded=shape(EMPTY))
    assert router.routes == [("get", "/portal/deliveries/v2")], router.routes


def test_anonymous_gets_the_401_envelope_and_no_packages():
    early = _Resp({"ok": False, "error": "unauthenticated",
                   "message": "Sign in to the portal with a linked character to continue."}, status_code=401)
    ns, _router = _deliveries_route(gate=None, early=early)
    out = asyncio.run(ns["portal_deliveries_v2"](_Request()))
    assert out.status_code == 401, "anonymous got %s" % out.status_code
    assert out.payload.get("ok") is False
    for leak in ("packages", "skip", "summary", "available", "read_at"):
        assert leak not in out.payload, "the 401 carried %s" % leak


def test_the_happy_path_carries_the_whole_shape():
    shaped = shape(payload(welcome=welcome_v17(), skip=SKIP, returns=[RETURN_BACKPACK]),
                   now=INSIDE)
    ns, _router = _deliveries_route(loaded=shaped)
    out = asyncio.run(ns["portal_deliveries_v2"](_Request()))
    assert out.status_code == 200
    assert out.payload["ok"] is True
    assert set(out.payload) == {"ok", "available", "read_at", "packages", "skip",
                                "summary"}, sorted(out.payload)
    assert out.payload["available"] is True
    assert len(out.payload["packages"]) == 2
    keys = set()
    _walk_keys(out.payload, keys)
    bad = sorted(k for k in keys if ID_KEY_RE.search(k))
    assert not bad, "internal identifiers on the route body: %s" % bad


def test_an_unavailable_loader_answers_200_with_the_sealed_envelope():
    """A 502 here would be a broken Mailbox page. The panel seals in place."""
    ns, _router = _deliveries_route(loaded=None)
    out = asyncio.run(ns["portal_deliveries_v2"](_Request()))
    assert out.status_code == 200, "a dead read answered %s" % out.status_code
    assert out.payload == {"ok": True, "available": False, "packages": [],
                           "skip": None, "summary": None}, out.payload


def test_the_deliveries_response_is_private_no_store():
    ns, _router = _deliveries_route(loaded=shape(EMPTY))
    out = asyncio.run(ns["portal_deliveries_v2"](_Request()))
    cc = out.headers.get("Cache-Control", "")
    assert "private" in cc and "no-store" in cc, \
        "per-player packages are cacheable: Cache-Control=%r" % cc


def test_the_route_reads_the_account_off_the_session_and_nowhere_else():
    src = _read(ROUTER)
    body = src[src.index("async def portal_deliveries_v2"):]
    assert "_require_linked_session_json(request)" in body
    assert "request.query_params" not in body and "account_id=" not in body, \
        "the deliveries route takes an account from the caller"
    assert "gate" in body, "the route does not read the session gate tuple"


# --------------------------------------------------------------------------- #
# 4. GET /portal/features
# --------------------------------------------------------------------------- #

def test_the_features_route_is_registered_and_public():
    ns, router, _calls = _features_route()
    assert router.routes == [("get", "/portal/features")], router.routes
    src = _read(FEATURES)
    assert "_require_linked_session_json" not in src, \
        "the features read grew a session gate; the Help page is public"
    assert "Request" not in src, "the features route takes a request it does not need"


def test_the_features_key_set_is_fixed():
    ns, _router, _calls = _features_route()
    assert [k for k, _f in ns["FEATURE_GATES"]] == [p[0] for p in FEATURE_PAIRS], \
        "the public feature key set drifted: %s" % [k for k, _f in ns["FEATURE_GATES"]]
    assert list(ns["FEATURE_GATES"]) == [tuple(p) for p in FEATURE_PAIRS], \
        "a public feature key is wired to the wrong gate: %s" % list(ns["FEATURE_GATES"])
    assert isinstance(ns["FEATURE_GATES"], tuple), "the gate list is mutable"


def test_the_features_read_answers_one_boolean_per_gate():
    calls = []
    ns, _router, seen = _features_route(
        values={"LASTSIETCH_CHAT_ENABLED": True, "LASTSIETCH_REFINERY_ENABLED": True}, calls=calls)
    out = asyncio.run(ns["portal_features"]())
    assert out.status_code == 200
    assert set(out.payload) == {"ok", "features", "instances"}, sorted(out.payload)
    assert out.payload["ok"] is True
    features = out.payload["features"]
    assert sorted(features) == FEATURE_KEYS, sorted(features)
    assert all(isinstance(v, bool) for v in features.values()), features
    assert features["chat"] is True and features["refinery"] is True
    assert features["augment"] is False and features["storage_move"] is False
    assert features['profile_login'] is False and features['game_login'] is False
    assert [name for name, _d in seen] == [f for _k, f in FEATURE_PAIRS], seen
    assert all(default == "0" for _n, default in seen), \
        "a gate was read with a default other than OFF: %s" % seen


def test_features_cannot_advertise_unsupported_move_when_all_flags_are_on():
    ns, _router, _seen = _features_route(values={flag: True for _key, flag in FEATURE_PAIRS})
    out = asyncio.run(ns["portal_features"]())
    assert out.payload["features"]["storage_move"] is False
    assert all(out.payload["features"][key] is True for key, _flag in FEATURE_PAIRS
               if key != "storage_move")
    assert out.payload["features"]["profile_login"] is False
    assert out.payload["features"]["game_login"] is False


def test_nothing_but_the_five_booleans_is_exposed():
    ns, _router, _seen = _features_route(values={"LASTSIETCH_CHAT_ENABLED": True})
    out = asyncio.run(ns["portal_features"]())
    keys = set()
    _walk_keys(out.payload["features"], keys)
    for leak in ("env", "override", "default", "effective", "name", "env_set"):
        assert leak not in keys, "the features read exposes the switchboard: %s" % leak
    # The map instance list beside it carries its public fields and nothing else
    # (no pod, ticket label or aliases).
    for row in out.payload["instances"]:
        assert set(row) == {"key", "name", "map", "inst", "partition", "dim", "mode", "pvp", "tracked"}, row


def test_the_features_response_is_publicly_cacheable():
    ns, _router, _seen = _features_route()
    out = asyncio.run(ns["portal_features"]())
    cc = out.headers.get("Cache-Control", "")
    assert "public" in cc and "max-age=60" in cc, \
        "the public features read is not cacheable: Cache-Control=%r" % cc


# --------------------------------------------------------------------------- #
# 5. The game-host handler and the dispatcher case, EXECUTED
# --------------------------------------------------------------------------- #

def _run(argv, env=None, cwd=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, **(env or {})), cwd=cwd)


def test_the_handler_refuses_a_non_numeric_account_id_before_any_sql():
    for junk in ("", "abc", "1; DROP TABLE", "-1", "1 2"):
        out = _run(["bash", HANDLER, junk])
        assert out.returncode == 2, \
            "the handler accepted %r (exit %s)" % (junk, out.returncode)
        assert "invalid account_id" in out.stderr, out.stderr[:200]
        assert "dq.sh" not in out.stderr, \
            "the handler reached psql before validating %r" % junk


def _handler_with_stub(tmp, stub_out, rc=0):
    """The REAL handler text with only the dq.sh PATH redirected at a stub, so
    the jq validity gate below it is the shipped one."""
    stub = os.path.join(tmp, "dq-stub.sh")
    with open(stub, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\ncat >/dev/null\n"
                 'printf "%s\\n" "$LASTSIETCH_STUB_OUT"\nexit %d\n' % ("%s", rc))
    os.chmod(stub, 0o755)
    text = _read(HANDLER)
    assert text.count("/root/dq.sh") == 1, "the handler no longer calls /root/dq.sh once"
    path = os.path.join(tmp, "handler.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace("/root/dq.sh", stub))
    os.chmod(path, 0o755)
    return _run(["bash", path, "4242"], env={"LASTSIETCH_STUB_OUT": stub_out})


def test_the_handler_refuses_output_that_is_not_json():
    tmp = tempfile.mkdtemp(prefix="deliveries-handler-")
    try:
        out = _handler_with_stub(tmp, 'ERROR:  relation "dune.ls_welcome_pack_grants" does not exist')
        assert out.returncode == 3, \
            "a psql error was passed off as an answer (exit %s)" % out.returncode
        assert "not valid JSON" in out.stderr, out.stderr[:200]
        assert out.stdout.strip() == "", "junk reached stdout"

        out = _handler_with_stub(tmp, "")
        assert out.returncode == 3, "empty output was passed off as an answer"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_handler_passes_valid_json_through_unchanged():
    tmp = tempfile.mkdtemp(prefix="deliveries-handler-")
    try:
        body = ('{"account_id":4242,"welcome":null,"welcome_skip":null,"returns":[]}')
        out = _handler_with_stub(tmp, body)
        assert out.returncode == 0, out.stderr[:300]
        assert json.loads(out.stdout) == json.loads(body), out.stdout[:200]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_handler_sql_is_scoped_and_capped():
    src = _read(HANDLER)
    assert "source = 'welcome_pack'" in src, \
        "the delivery-log read is not scoped to the welcome pack"
    assert "LIMIT 60" in src, "the delivery-log read lost its 60-line cap"
    assert "LIMIT 5" in src, "the return-package read lost its five-row cap"
    assert src.count("account_id = :account_id") >= 4, \
        "a subquery reads rows that are not this account's"
    for banned in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"):
        assert banned not in src, "the read handler carries a %s" % banned
    assert "granted_actor_id" not in src and "fls_id" not in src \
        and "funcom_id" not in src and "player_controller_id" not in src, \
        "the handler selects an identifier the portal must never see"


def _dispatch(command):
    return _run(["bash", DISPATCH], env={"SSH_ORIGINAL_COMMAND": command,
                                        "LASTSIETCH_RELAY_ALLOWED_IPS": "127.0.0.1"})


def test_the_dispatcher_case_takes_one_numeric_argument():
    out = _dispatch("deliveries-by-player 4242")
    # /root/dune-deliveries-by-player.sh does not exist off the game host, so a
    # valid call proves itself by failing AT THE EXEC of the deployed path.
    assert "dune-deliveries-by-player.sh" in out.stderr, \
        "the case did not exec the deployed handler: %r" % out.stderr[:200]
    assert "rejected" not in out.stderr, \
        "a valid account id was rejected: %r" % out.stderr[:200]


def test_the_dispatcher_case_rejects_junk_and_extras():
    for junk in ("deliveries-by-player", "deliveries-by-player abc",
                 "deliveries-by-player -1", "deliveries-by-player 4242;id"):
        out = _dispatch(junk)
        assert out.returncode == 2, "%r was accepted (exit %s)" % (junk, out.returncode)
        assert "invalid account_id" in out.stderr, out.stderr[:200]
    out = _dispatch("deliveries-by-player 4242 999")
    assert out.returncode == 1, "an extra argument was accepted"
    assert "unexpected args" in out.stderr, out.stderr[:200]
    for junk in ("deliveries-by-player abc", "deliveries-by-player 4242 999"):
        assert "dune-deliveries-by-player.sh" not in _dispatch(junk).stderr, \
            "%r reached the exec anyway" % junk


def test_the_dispatcher_case_execs_the_deployed_path():
    src = _read(DISPATCH)
    assert src.count("  deliveries-by-player)") == 1, \
        "the deliveries case is missing or duplicated"
    assert 'exec /root/dune-deliveries-by-player.sh "$dl_aid"' in src, \
        "the case does not exec /root/dune-deliveries-by-player.sh"


def test_the_relay_route_degrades_to_available_false():
    src = _read(RELAY_APP)
    assert '@app.get("/dune/player/{account_id}/deliveries", dependencies=[Depends(verify_key)])' in src, \
        "the relay route is missing or unguarded by verify_key"
    body = src[src.index("def dune_player_deliveries("):]
    body = body[:body.index("\n\nclass ") if "\n\nclass " in body else len(body)]
    assert 'deliveries-by-player {account_id}' in body, \
        "the relay route does not call the dispatcher verb"
    assert "timeout=20" in body, "the relay read lost its 20 second timeout"
    assert '"available": False' in body and '"welcome": None' in body \
        and '"welcome_skip": None' in body and '"returns": []' in body, \
        "the relay failure shape is not the contract's"


# --------------------------------------------------------------------------- #
# 6. Home, the includes and the unfurl copy
# --------------------------------------------------------------------------- #

def test_home_carries_deliveries_as_its_ninth_sub_object():
    src = _read(PORTAL)
    assert '"orders", "caps", "deliveries")' in src, \
        "_HOME_PARTS did not gain deliveries as its ninth entry"
    assert '"deliveries": deliveries,' in src, "the Home envelope carries no deliveries"
    body = src[src.index("async def _home_deliveries"):]
    body = body[:body.index("\n\n\n")]
    assert "portal_deliveries.load(" in body
    assert "call_relay" not in body, "the Home builder reaches the relay directly"


def test_both_routers_are_included_under_their_own_guards():
    src = _read(MAIN)
    for module in ("portal_deliveries", "portal_features"):
        assert "from routers import %s" % module in src, "main.py does not import %s" % module
        assert "app.include_router(%s.router)" % module in src, \
            "main.py does not register %s" % module
    # Two guards, not one: an operator reading the log has to see which half is
    # missing, and the features read has nothing to do with the deliveries read.
    assert src.count("routers.portal_deliveries absent") == 1
    assert src.count("routers.portal_features absent") == 1


def test_the_help_unfurl_copy_obeys_the_house_rules():
    src = _read(UNFURL)
    assert '"help": ("Help and what\'s new",' in src, \
        "the /help unfurl entry is missing or renamed"
    ns = {}
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SERVERS":
            ns["SERVERS"] = ast.literal_eval(node.value)
    assert ns.get("SERVERS"), "nextgen_unfurl lost SERVERS"
    m = re.search(r'"help": \("([^"]+)",\s*\n?\s*f"([^"]+)"\)', src)
    assert m, "the help entry is not a (title, f-string) pair"
    title, desc = m.group(1), m.group(2).replace("{SERVERS}", ns["SERVERS"])
    assert desc.endswith(".") and desc.count(".") == 1, "help: one sentence"
    blob = (title + " " + desc).lower()
    assert "@" not in blob and "http" not in blob, "help: no handles or links"
    assert EM_DASH not in blob, "help: house style, no em dashes"


# --------------------------------------------------------------------------- #
# 7. The deploy lane
# --------------------------------------------------------------------------- #

























# --------------------------------------------------------------------------- #
# 8. Relocating-variant proof. Every mutation below MOVES or swaps a guarded
# expression inside a scratch COPY of the tree: nothing is deleted, the
# identifier census is asserted unchanged, and the pin above still has to go RED.
# --------------------------------------------------------------------------- #

MUTATIONS = (
    ("a package with one landed leg reads as delivered", DELIV_REL,
     """    if done == len(legs):
        return "delivered"
    return "pending" if done == 0 else "partial\"""",
     """    if done == 0:
        return "delivered"
    return "pending" if done == len(legs) else "partial\""""),

    ("the exchange leg reads delivered while its lines are pending", DELIV_REL,
     '                             "delivered" if taken else "waiting",',
     '                             "waiting" if taken else "delivered",'),

    ("an owed item is placed where it already is", DELIV_REL,
     '        items.append(_item(tid, None, "backpack", "pending"))',
     '        items.append(_item(tid, None, "pending", "backpack"))'),

    ("the delivery lanes are wired to the wrong containers", DELIV_REL,
     '_LANE_WHERE = {"rmq": "backpack", "exchange": "exchange", "db": "backpack"}',
     '_LANE_WHERE = {"rmq": "exchange", "exchange": "backpack", "db": "backpack"}'),

    ("the summary counts what has already landed", DELIV_REL,
     '"pending_count": sum(1 for p in packages if p["state"] != "delivered"),',
     '"pending_count": sum(1 for p in packages if p["state"] == "delivered"),'),

    ("the offline and RMQ version patterns swap", DELIV_REL,
     'VERSION_OFFLINE_RE = re.compile(r"^v1\\.([0-6]|8(?!-rmq))")\nVERSION_RMQ_RE = re.compile(r"^v1\\.(7|8-rmq)")',
     'VERSION_OFFLINE_RE = re.compile(r"^v1\\.(7|8-rmq)")\nVERSION_RMQ_RE = re.compile(r"^v1\\.([0-6]|8(?!-rmq))")'),

    ("v1.8-rmq moves to the offline pattern", DELIV_REL,
     'VERSION_OFFLINE_RE = re.compile(r"^v1\\.([0-6]|8(?!-rmq))")\nVERSION_RMQ_RE = re.compile(r"^v1\\.(7|8-rmq)")',
     'VERSION_OFFLINE_RE = re.compile(r"^v1\\.([0-6]|8(?!-rmq)|8-rmq)")\nVERSION_RMQ_RE = re.compile(r"^v1\\.(7)")'),

    ("an unrecognised pack version falls back to the offline lane", DELIV_REL,
     '    if VERSION_OFFLINE_RE.match(text):\n        return LANE_OFFLINE\n    return LANE_UNKNOWN',
     '    if VERSION_OFFLINE_RE.match(text):\n        return LANE_UNKNOWN\n    return LANE_OFFLINE'),

    ("a closed fresh-start window is shown and an open one is dropped", DELIV_REL,
     "    if eligible <= now:\n        return None",
     "    if eligible >= now:\n        return None"),

    ("the loader waits longer than the relay's own ssh budget", DELIV_REL,
     "RELAY_TIMEOUT_S = 12.0",
     "RELAY_TIMEOUT_S = 25.0"),

    ("a five-digit fractional stamp is refused", DELIV_REL,
     '    text = _FRACTION_RE.sub(lambda m: (m.group(0) + "000000")[:7], text, count=1)\n',
     ""),

    ("a relay outage is remembered for the full answer TTL", DELIV_REL,
     "FAIL_TTL_S = 30.0",
     "FAIL_TTL_S = 300.0"),

    ("an outage is not remembered at all, so every request retries it", DELIV_REL,
     '        logger.warning("portal: deliveries unavailable from the relay")\n'
     "        _remember(key, None, clock, FAIL_TTL_S)\n"
     "        return None",
     '        logger.warning("portal: deliveries unavailable from the relay")\n'
     "        return None"),

    ("a failed read writes a traceback on a per-poll path", DELIV_REL,
     '        logger.warning("portal: deliveries read failed (%s)", type(exc).__name__)',
     '        logger.warning("portal: deliveries read failed (%s)", type(exc).__name__,\n'
     "                       exc_info=exc)"),


    ("a sealed read is announced as an available empty one", ROUTER_REL,
     '        payload = {"ok": True, "available": False, "packages": [],',
     '        payload = {"ok": False, "available": True, "packages": [],'),

    ("two public feature keys are wired to each other's gates", FEATURES_REL,
     '''    ("chat", "LASTSIETCH_CHAT_ENABLED"),
    ("refinery", "LASTSIETCH_REFINERY_ENABLED"),''',
     '''    ("chat", "LASTSIETCH_REFINERY_ENABLED"),
    ("refinery", "LASTSIETCH_CHAT_ENABLED"),'''),

    ("the dispatcher execs before it rejects the extra argument", DISPATCH_REL,
     '''    [ -z "${dl_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }
    exec /root/dune-deliveries-by-player.sh "$dl_aid"''',
     '''    exec /root/dune-deliveries-by-player.sh "$dl_aid"
    [ -z "${dl_extra:-}" ] || { echo "rejected: unexpected args" >&2; exit 1; }'''),

    ("the handler's numeric guard is inverted", HANDLER_REL,
     'if [[ ! "$account_id" =~ ^[0-9]+$ ]]; then',
     'if [[ "$account_id" =~ ^[0-9]+$ ]]; then'),

)

# Where a relocation's second half lands. A mutation whose `replace` drops the
# block re-inserts it verbatim at this anchor, so the statement MOVES and the
# file's identifier census is unchanged (asserted per mutation below).
RELOCATIONS = {
    "an outage is not remembered at all, so every request retries it": (
        "    shaped = shape(raw)\n",
        "    _remember(key, None, clock, FAIL_TTL_S)\n    shaped = shape(raw)\n"),
    # A deletion always changes the census, so the fraction fix is MOVED to
    # after the parse it was protecting, where it can no longer do its job.
    "a five-digit fractional stamp is refused": (
        '    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)\n',
        '    text = _FRACTION_RE.sub(lambda m: (m.group(0) + "000000")[:7], text, count=1)\n    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)\n'),
}

_SCRATCH_TREE = (DELIV_REL, ROUTER_REL, FEATURES_REL, DISPATCH_REL, HANDLER_REL)


def _scratch_copy(tmp):
    for rel in _SCRATCH_TREE:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)
    # The lane greps `git log` for drift guards only below its gate, but the
    # suite reads it as text; nothing else in the scratch needs a repo.


def _run_child(root):
    env = dict(os.environ, PDELIV_ROOT=root, PDELIV_CHILD="1")
    return subprocess.run([sys.executable, os.path.abspath(__file__)],
                          capture_output=True, text=True, env=env, timeout=900)


def _census(src):
    """Every identifier in the file, counted. A relocation must not change it;
    a deletion always does, which is what keeps these mutations honest."""
    return collections.Counter(re.findall(r"[A-Za-z_]\w*", src))


def test_every_pin_is_red_on_a_relocated_variant():
    if CHILD:
        return
    tmp = tempfile.mkdtemp(prefix="deliveries-broken-",
                           dir=os.environ.get("PDELIV_SCRATCH") or None)
    try:
        _scratch_copy(tmp)
        clean = _run_child(tmp)
        assert clean.returncode == 0, (
            "the unmutated scratch copy already fails: "
            + " | ".join(l for l in clean.stdout.splitlines() if l.startswith("FAIL")))

        for label, rel, find, replace in MUTATIONS:
            path = os.path.join(tmp, rel)
            original = _read(path)
            assert original.count(find) == 1, \
                "[%s] the mutation target is not unique in %s (%d matches)" \
                % (label, rel, original.count(find))
            moved = original.replace(find, replace, 1)
            if label in RELOCATIONS:
                anchor, landing = RELOCATIONS[label]
                assert moved.count(anchor) == 1, "[%s] the landing anchor is not unique" % label
                moved = moved.replace(anchor, landing, 1)
            # No count may DROP. An addition ("somebody put exc_info back") is
            # as honest a mutation as a move; a deletion is the dishonest one,
            # because a pin can go red for the symbol being gone rather than for
            # the behaviour being wrong.
            before, after = _census(original), _census(moved)
            lost = sorted(t for t, n in before.items() if after[t] < n)
            assert not lost, \
                "[%s] the mutation deleted %s instead of moving it" % (label, lost)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(moved)
            try:
                broken = _run_child(tmp)
                assert broken.returncode != 0, \
                    "[%s] the suite stayed GREEN on a relocated variant" % label
                print("  relocated-variant RED: %s" % label)
            finally:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(original)

        again = _run_child(tmp)
        assert again.returncode == 0, "the scratch copy was not restored between mutations"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    for fn in _all_tests():
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
    print("\n%d/%d passed" % (len(_all_tests()) - failures, len(_all_tests())))
    raise SystemExit(1 if failures else 0)
