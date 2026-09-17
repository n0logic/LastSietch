"""Account preferences (Fremkit wave 10b).

A preference is a small choice a player made in the portal that should follow
them to the next browser: the Storage grid/list toggle, the map layers they hide,
the map viewer they picked. It lives on the ACCOUNT, not on the device, which is
the whole reason this module exists.

Three properties matter more than the feature:

  * THE KEY SET IS A WHITELIST, NOT A SCHEMA. `VALIDATORS` is the only set of
    keys the server will store and the only set of value shapes it accepts. An
    unknown key refuses the WHOLE request and writes nothing, so a client bug
    cannot quietly turn this table into free storage.
  * A SCOPE IS A DOCUMENT. '' is identity-wide; 'char:<controller_id>' is one
    character, so a map default saved on one character does not follow another.
    A write shallow-merges into one scope and a null value deletes its key.
  * PREFERENCES ARE NOT A SOURCE OF TRUTH. Nothing here gates anything, so there
    are no audit rows, and a row whose JSON no longer parses reads as an empty
    document rather than failing the page.

All state is admin.db (portal_prefs); zero game-DB touch. Session gate, refusal
envelope and CSRF check are reused from routers.portal (loaded first by main.py)
so the auth story matches the rest of the portal exactly.

The prefs body is never logged. It is not sensitive, but it is the player's
choice and it has no business in an operator's log lines.

Noted follow-ups (backend review 2026-09-04, none blocking):
  * A map key or layer id may still hold unicode look-alikes of the ASCII the
    regexes allow; they store and read back consistently, so this costs a
    confusing key, not a wrong one.
  * `__proto__` and friends are ordinary strings to Python and are stored as
    map keys like any other; the browser side is what would have to care.
  * A locked admin.db raises out of apply_patch as a 500 rather than a refusal
    token, unlike portal_codes, which converts it. A lost preference write is a
    retry, so this is not urgent.
"""
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_db
from routers.portal import (
    _require_linked_session_json,
    _v2_body_and_csrf,
    _v2_err,
    _v2_ok,
)

logger = logging.getLogger("portal")
router = APIRouter()


# --- prefs model (stdlib only below; the test suite execs this section) ------

# '' identity-wide, or one character. Written as an optional group rather than
# an empty alternative so the empty scope is legible; applied with fullmatch.
# [0-9] and not \d: \d matches every unicode decimal digit, so 'char:\u0663'
# would validate here and then miss every row (review 2026-09-04).
SCOPE_RE = re.compile(r"(?:char:[0-9]{1,12})?")

# Caps. The byte cap is measured on the MERGED document, so a patch that fits
# on its own but would push the scope over still refuses, and the scope cap
# counts rows for one identity: 32 characters is far past anyone's roster.
MAX_DOC_BYTES = 8192
MAX_SCOPES = 32
# How much of a refused key is echoed back. The key came off the request body,
# which nothing upstream caps, and the refusal exists to tell a developer WHICH
# key was wrong, not to mirror an arbitrary payload back at whoever sent it.
ECHO_MAX = 64
# map_hidden shape caps. A map key is a board key; a layer id is a layer's own
# id, which carries dots and colons in the shipped boards.
MAP_KEY_RE = re.compile(r"[a-z0-9_-]{1,32}")
LAYER_ID_RE = re.compile(r"[a-zA-Z0-9_.:-]{1,64}")
VIEWER_RE = re.compile(r"[a-z0-9_-]{1,16}")
MAX_MAPS = 24
MAX_LAYERS = 200


def _valid_storage_view(value):
    return value in ("grid", "list")


def _valid_map_hidden(value):
    """{mapKey: {layerId: true}}. `is not True` rather than a truth test on
    purpose: 1, "true" and {} are all truthy and none of them is the contract."""
    if not isinstance(value, dict) or len(value) > MAX_MAPS:
        return False
    for map_key, layers in value.items():
        if not isinstance(map_key, str) or not MAP_KEY_RE.fullmatch(map_key):
            return False
        if not isinstance(layers, dict) or len(layers) > MAX_LAYERS:
            return False
        for layer_id, hidden in layers.items():
            if not isinstance(layer_id, str) or not LAYER_ID_RE.fullmatch(layer_id):
                return False
            if hidden is not True:
                return False
    return True


def _valid_map_viewer(value):
    """{mapKey: viewerId}. Capped at the same fan-out as map_hidden: a player has
    one viewer per board either way, and an uncapped map is a map the byte cap
    alone has to argue with."""
    if not isinstance(value, dict) or len(value) > MAX_MAPS:
        return False
    for map_key, viewer in value.items():
        if not isinstance(map_key, str) or not MAP_KEY_RE.fullmatch(map_key):
            return False
        if not isinstance(viewer, str) or not VIEWER_RE.fullmatch(viewer):
            return False
    return True


def _valid_reach_paths(value):
    return (isinstance(value, list) and len(value) <= 12
            and all(isinstance(path, str) and len(path) <= 256
                    and re.fullmatch(r"/[a-zA-Z0-9/_?=&%.-]*", path)
                    and not path.startswith("//") for path in value))


def _valid_market_searches(value):
    if not isinstance(value, list) or len(value) > 8:
        return False
    for row in value:
        if not isinstance(row, dict) or set(row) - {"q", "side", "sort", "grade"}:
            return False
        if not isinstance(row.get("q"), str) or len(row["q"]) > 64:
            return False
        if row.get("side") not in ("sell", "wanted") or row.get("sort") not in ("new", "cheap", "dear"):
            return False
        grade = row.get("grade")
        if grade is not None and (type(grade) is not int or grade not in range(6)):
            return False
    return True


def _valid_activity_seen(value):
    if not isinstance(value, str) or len(value) > 32:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.tzinfo is not None and 2020 <= stamp.year <= 2100
    except ValueError:
        return False


# The ONLY keys the server stores, and the only value shapes it accepts. A key
# the portal wants to save has to be added HERE first; there is no fallthrough.
VALIDATORS = {
    "storage_view": _valid_storage_view,
    "map_hidden": _valid_map_hidden,
    "map_viewer": _valid_map_viewer,
    "reach_pins": _valid_reach_paths,
    "reach_recent": _valid_reach_paths,
    "market_searches": _valid_market_searches,
    "activity_seen": _valid_activity_seen,
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def valid_scope(scope) -> bool:
    return isinstance(scope, str) and bool(SCOPE_RE.fullmatch(scope))


def check_patch(patch):
    """(None, None) when every key is whitelisted and every value fits its
    shape, else (token, key): 'unknown' for a key the server does not store,
    'bad_value' for a whitelisted key whose value is not the agreed shape. The
    whole patch is checked before anything is written, because a partially
    applied refusal is worse than a refused one."""
    for key, value in patch.items():
        if key not in VALIDATORS:
            return "unknown", key
        if value is None:          # a null deletes the key; nothing to validate
            continue
        if not VALIDATORS[key](value):
            return "bad_value", key
    return None, None


def load_scopes(identity):
    """({scope: doc}, newest updated_utc or None) for one identity. A row whose
    JSON no longer parses is skipped: a preference that cannot be read is a
    default, never a 500."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT scope, prefs, updated_utc FROM portal_prefs WHERE discord_id = ?",
            (str(identity),),
        ).fetchall()
    finally:
        conn.close()
    docs = {}
    newest = None
    for row in rows:
        try:
            doc = json.loads(row["prefs"] or "{}")
        except ValueError:
            logger.debug("portal prefs: unreadable document in scope %r", row["scope"])
            continue
        if not isinstance(doc, dict):
            continue
        docs[row["scope"] or ""] = doc
        stamp = row["updated_utc"] or ""
        if stamp and (newest is None or stamp > newest):
            newest = stamp
    return docs, newest


def apply_patch(identity, scope, patch):
    """Shallow-merge `patch` into one scope, a null value deleting its key.
    Returns (doc, updated_utc, None) or (None, None, 'too_large'|'too_many_scopes').

    The read, the caps and the write share one transaction: two tabs saving at
    once would otherwise both read the same document and write half of it back,
    and the scope cap would be counted against a row list that has already moved."""
    stamp = _now()
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT prefs FROM portal_prefs WHERE discord_id = ? AND scope = ?",
            (str(identity), scope),
        ).fetchone()
        try:
            doc = json.loads(row["prefs"] or "{}") if row else {}
        except ValueError:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        for key, value in patch.items():
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
        # An emptied scope is a DELETED row, not a stored {}. An empty document
        # is what a missing row already means, and keeping the row would spend
        # one of the 32 slots forever on nothing every time clearScope() ran.
        # The DELETE is a no-op when there was no row, so there is no branch.
        if not doc:
            conn.execute(
                "DELETE FROM portal_prefs WHERE discord_id = ? AND scope = ?",
                (str(identity), scope),
            )
            conn.commit()
            return {}, stamp, None
        encoded = json.dumps(doc, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_DOC_BYTES:
            conn.rollback()
            return None, None, "too_large"
        if row is None:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM portal_prefs WHERE discord_id = ?",
                (str(identity),),
            ).fetchone()["c"]
            if int(count) >= MAX_SCOPES:
                conn.rollback()
                return None, None, "too_many_scopes"
        conn.execute(
            """INSERT INTO portal_prefs (discord_id, scope, prefs, updated_utc)
                    VALUES (?, ?, ?, ?)
               ON CONFLICT(discord_id, scope) DO UPDATE
                    SET prefs = excluded.prefs, updated_utc = excluded.updated_utc""",
            (str(identity), scope, encoded, stamp),
        )
        conn.commit()
    finally:
        conn.close()
    return doc, stamp, None


# --- routes ------------------------------------------------------------------


def _v2_err_field(error, message, status=400, **extra):
    """The shared refusal envelope plus the field it is about. `_v2_err` carries
    only the token and the message, and a client that sent three keys has to
    learn WHICH one was refused. Rebuilt rather than patched after the fact: a
    rendered JSONResponse already carries its own content-length."""
    payload = {"ok": False, "error": error, "message": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


@router.get("/portal/settings/prefs")
async def portal_prefs_get(request: Request):
    """Every preference scope this identity owns. `identity` is the '' scope and
    `scopes` is every other one, so the client does not have to know which key
    the empty scope hides behind. Auth: linked session."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, _, _ = gate

    docs, newest = load_scopes(discord_id)
    identity_doc = docs.pop("", {})
    return _v2_ok({"identity": identity_doc, "scopes": docs, "updated_utc": newest})


@router.put("/portal/settings/prefs")
async def portal_prefs_set(request: Request):
    """Shallow-merge one scope: `{scope, prefs}` where a null value deletes its
    key. An unknown key refuses the whole request and writes nothing, so a
    client that sent one good key and one bad one has stored neither.
    Auth: linked session + CSRF."""
    gate, early = _require_linked_session_json(request)
    if early is not None:
        return early
    _, discord_id, _, _ = gate

    body, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return _v2_err("csrf", "Invalid CSRF token", status=403)

    scope = body.get("scope")
    if scope is None:
        scope = ""
    if not valid_scope(scope):
        return _v2_err_field("bad_request", "That preference scope is not valid.",
                             field="scope")

    patch = body.get("prefs")
    if not isinstance(patch, dict):
        return _v2_err_field("bad_request", "Preferences must be an object.",
                             field="prefs")

    token, key = check_patch(patch)
    if token == "unknown":
        return _v2_err_field("bad_request", "That preference is not one this server "
                                            "stores.", field="prefs",
                             unknown=str(key)[:ECHO_MAX])
    if token == "bad_value":
        return _v2_err_field("bad_request", "That preference value is not valid.",
                             field="prefs", key=str(key)[:ECHO_MAX])

    doc, stamp, error = apply_patch(discord_id, scope, patch)
    if error == "too_large":
        return _v2_err("too_large",
                       "You have saved as many preferences as this scope holds.",
                       status=413)
    if error == "too_many_scopes":
        return _v2_err("too_many_scopes",
                       "You have preferences saved on too many characters.",
                       status=409)
    return _v2_ok({"scope": scope, "prefs": doc, "updated_utc": stamp})
