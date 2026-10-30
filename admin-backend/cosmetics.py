"""Cosmetic titles beside player names (COSMETICS-MAP-ICONS-PLAN-2026-10-29, P0).

P0 stores nothing of its own. It reads what Seven Nights already keeps:

  * the equipped title is the profile's `sietch_flair` pref;
  * it counts only while a DELIVERED Stall order of a flair row carries that
    pref_key (sietch_nights.owned_pref_keys), so a stale or hand-set pref never
    renders as a title.

A title decorates a name the surface already shows. Callers pass account ids
they already hold; nothing here turns an id into a name, and nothing here is
reached by the anonymous map crowd.

The catalog is read straight off the data files rather than through
routers.portal_sietch, so chat and the standings board do not import a router.
LASTSIETCH_SIETCH_SKU_OFF is deliberately ignored: a disabled row stops sales, never
ownership.
"""
import logging
import os
import sqlite3

import feature_flags
import sietch_nights as sn

logger = logging.getLogger("portal")

FLAG = "LASTSIETCH_COSMETIC_TITLES_ENABLED"
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CATALOG_PATH = os.path.join(_DATA, "sietch_nights_catalog.json")
DENY_PATH = os.path.join(_DATA, "sietch_nights_deny.json")
_CATALOG_CACHE = {}


def enabled():
    return bool(feature_flags.enabled(FLAG, "0"))


def catalog():
    """{sku: row}, re-read when either file changes. A failed load keeps the
    last good copy; with none, titles simply go quiet."""
    try:
        key = (os.path.getmtime(CATALOG_PATH), os.path.getmtime(DENY_PATH))
    except OSError:
        return {}
    if _CATALOG_CACHE.get("key") != key:
        try:
            rows, _refused = sn.load_catalog(CATALOG_PATH, DENY_PATH)
        except (OSError, ValueError) as exc:
            logger.error("cosmetics: catalog load failed: %s", exc)
            return _CATALOG_CACHE.get("rows") or {}
        _CATALOG_CACHE.update(key=key, rows=rows)
    return _CATALOG_CACHE["rows"]


def _named(rows, kind):
    return {r["pref_key"]: r["name"] for r in rows.values()
            if r.get("kind") == kind and r.get("pref_key")}


def profile_for_session(conn, session, account_id):
    pid = (session or {}).get("pid")
    if sn.valid_profile_id(pid):
        return pid
    pid = sn.profile_for_account(conn, account_id) if account_id is not None else None
    return pid if sn.valid_profile_id(pid) else None


def title_for_profile(conn, profile_id, rows):
    names = _named(rows, "flair")
    identity, _name = sn.profile_identity(conn, profile_id)
    key = sn.read_prefs(conn, identity).get("sietch_flair")
    if not isinstance(key, str) or key not in names:
        return None
    if key not in sn.owned_pref_keys(conn, profile_id, rows, "flair"):
        return None
    return {"key": key, "name": names[key]}


def titles_for_accounts(conn, account_ids):
    """{account_id: {key, name}} for the ids whose profile wears an owned title.
    {} while the gate is off, and {} on any database error: a title must never
    take a chat page or the standings board down with it."""
    if not enabled():
        return {}
    rows = catalog()
    if not rows:
        return {}
    out, by_profile = {}, {}
    try:
        for raw in set(account_ids or ()):
            try:
                aid = int(raw)
            except (TypeError, ValueError):
                continue
            pid = sn.profile_for_account(conn, aid)
            if not sn.valid_profile_id(pid):
                continue
            if pid not in by_profile:
                by_profile[pid] = title_for_profile(conn, pid, rows)
            if by_profile[pid]:
                out[aid] = by_profile[pid]
    except sqlite3.Error as exc:
        logger.warning("cosmetics: title lookup failed: %s", exc)
        return {}
    return out


def account_titles(account_ids):
    """titles_for_accounts on its own connection, opened only while the gate is on."""
    if not enabled():
        return {}
    from database import get_db
    conn = get_db()
    try:
        return titles_for_accounts(conn, account_ids)
    finally:
        conn.close()


def owned_view(conn, session, account_id, identity):
    """The Settings picker: owned titles and map dots, and which of each is worn.
    A worn key the profile no longer owns reads as None."""
    rows = catalog()
    pid = profile_for_session(conn, session, account_id)
    if pid is None or not rows:
        return {"titles": [], "mapdots": [], "equipped": {"flair": None, "mapdot": None}}
    prefs = sn.read_prefs(conn, identity)
    out = {"equipped": {}}
    for kind, field, pref in (("flair", "titles", "sietch_flair"), ("mapdot", "mapdots", "sietch_mapdot")):
        names = _named(rows, kind)
        owned = sn.owned_pref_keys(conn, pid, rows, kind)
        out[field] = [{"key": k, "name": names[k]} for k in sorted(owned) if k in names]
        worn = prefs.get(pref)
        out["equipped"][kind] = worn if isinstance(worn, str) and worn in owned else None
    return out
