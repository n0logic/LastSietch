"""P8 — CVars admin sub-tab.

Reference: docs/dune-research/P8-EXECUTION-BRIEF.md (Appendix B is canonical;
the original §3/§4/§9 are obsolete).

Architecture: admin-backend → relay host → dispatch shell on lastsietch-dune
→ kubectl exec into game pod. No direct Postgres client in admin-backend;
no Python helper module to import. All game-side reads and writes go through
the three relay endpoints:

    GET  /server/cvars/read           — merged 5+1-layer INI walk + raw_sections
    POST /server/cvars/write          — UserOverrides.ini write + relay-side INSERT
    GET  /server/cvars/diff?since=…   — settingsUpdate capture diff
    GET  /server/cvars/history        — paginated holadmin.cvar_changes rows

The relay owns the `holadmin.cvar_changes` INSERT (it has pg access; admin-
backend does not — see Appendix B.5 + dune_grant.py precedent). Admin-backend
just persists its own SQLite audit_log row referencing the relay's change_id.

Auth: `require_admin` on reads, `require_admin + require_csrf` on POST.
The brief refers to this gate as `require_operator`; that helper does not
exist (team-lead confirmed). The admin role IS the Operator role for v1.

CSRF header is `X-CSRF-Token` (matches existing `require_csrf` validator and
the JS in `templates/v2/base.html`). The brief mentioned `X-Admin-CSRF-Token`
in error.

Success banner copy: "Queued — applies at next maintenance restart".
NEVER "Saved". The change is filesystem-only on UserOverrides.ini — it does
not take effect until the next announced maintenance restart. Never restart
a live community server outside an announced window.
"""
import json
import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

try:
    import tomllib  # stdlib 3.11+
except ModuleNotFoundError:
    tomllib = None  # type: ignore

from auth import audit_log, get_current_user, require_admin, require_csrf
from relay import call_relay

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Sidecars.
_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
)
_CATALOG_PATH = os.path.join(_DATA_DIR, "cvars-schema.json")
_CANONICAL_PATH = os.path.join(_DATA_DIR, "cvars-canonical.toml")
# Intel sidecar — Last Sietch-curated annotations (per_map / high_impact / no_op /
# intel_id / notes) keyed by "<section>::<key>" because dotted TOML keys can't
# represent the Unreal `/Script/...` paths cleanly. Overlaid onto catalog
# fields + live settings at render time. See cvars-intel.toml for format.
_INTEL_PATH = os.path.join(_DATA_DIR, "cvars-intel.toml")

_catalog_cache: dict = {"data": None, "mtime": 0.0}
_canonical_cache: dict = {"data": None, "mtime": 0.0}
_intel_cache: dict = {"data": None, "mtime": 0.0}

VALID_SUBTABS = ("live", "catalog", "history", "diff")
HISTORY_PAGE_SIZE = 50

QUEUED_BANNER = "Queued — applies at next maintenance restart"
CONFIRM_TOKEN_EXPECTED = "CONFIRM"

# Input bounds — match holadmin.cvar_changes column widths (TEXT, unconstrained
# in pg, but we still cap server-side for log readability + audit JSON size).
MAX_SECTION_LEN = 200
MAX_KEY_LEN = 200
MAX_VALUE_LEN = 4096
MAX_REASON_LEN = 500

# Conservative INI key / section shapes (Unreal-style).
_INI_KEY_RE = re.compile(r"^[A-Za-z0-9_.+\-]+$")
_INI_SECTION_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")


# --- Auth wrappers ---

def _admin_or_redirect(request: Request):
    try:
        user = get_current_user(request)
    except HTTPException:
        return None, RedirectResponse(url="/admin/login", status_code=302)
    if user["role"] != "admin":
        return None, RedirectResponse(url="/admin/", status_code=302)
    return user, None


# --- Sidecar loaders ---

def _load_json_sidecar(path: str, cache: dict) -> dict | None:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if cache["data"] is not None and mtime == cache["mtime"]:
        return cache["data"]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning("cvars sidecar %s unreadable", path)
        return None
    cache["data"] = data
    cache["mtime"] = mtime
    return data


def _load_toml_sidecar(path: str, cache: dict) -> dict | None:
    if tomllib is None:
        logger.warning("tomllib stdlib unavailable (requires Python 3.11+)")
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if cache["data"] is not None and mtime == cache["mtime"]:
        return cache["data"]
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning("cvars sidecar %s unreadable as TOML", path)
        return None
    cache["data"] = data
    cache["mtime"] = mtime
    return data


def _load_catalog() -> dict | None:
    return _load_json_sidecar(_CATALOG_PATH, _catalog_cache)


def _load_canonical() -> dict | None:
    return _load_toml_sidecar(_CANONICAL_PATH, _canonical_cache)


def _load_intel() -> dict:
    """Intel sidecar — empty dict if absent. Top-level keys are
    `<section>::<key>` strings; values are per-cvar annotation dicts."""
    return _load_toml_sidecar(_INTEL_PATH, _intel_cache) or {}


def _intel_for(section: str | None, key: str) -> dict:
    intel = _load_intel()
    if not intel:
        return {}
    return intel.get(f"{section or ''}::{key}", {}) or {}


def _enrich_catalog(catalog: dict | None) -> dict:
    """Overlay intel annotations onto catalog fields. Adds `per_map`,
    `high_impact`, `no_op`, `intel_id`, `notes` keys to each field where the
    sidecar has an entry. Returns a NEW dict (does not mutate the cached
    file)."""
    if not catalog:
        return catalog or {}
    intel = _load_intel()
    if not intel:
        return catalog
    out_categories = []
    for cat in catalog.get("categories", []):
        out_fields = []
        for f in cat.get("fields", []):
            annot = intel.get(f"{f.get('section') or ''}::{f.get('key')}", {})
            if annot:
                merged = dict(f)
                for k in ("per_map", "high_impact", "no_op", "intel_id", "notes"):
                    if k in annot:
                        merged[k] = annot[k]
                out_fields.append(merged)
            else:
                out_fields.append(f)
        out_categories.append({**cat, "fields": out_fields})
    return {**catalog, "categories": out_categories}


def _enrich_live(live: dict | None) -> dict:
    """Overlay intel annotations onto live settings. Adds the same intel keys
    to each `live.settings[]` row. Returns a NEW dict (no mutation)."""
    if not live:
        return live or {}
    intel = _load_intel()
    if not intel:
        return live
    out_settings = []
    for s in live.get("settings", []):
        annot = intel.get(f"{s.get('section') or ''}::{s.get('key')}", {})
        if annot:
            merged = dict(s)
            for k in ("per_map", "high_impact", "no_op", "intel_id", "notes"):
                if k in annot:
                    merged[k] = annot[k]
            out_settings.append(merged)
        else:
            out_settings.append(s)
    return {**live, "settings": out_settings}


# --- Relay shims (each tolerates offline by returning None) ---

# The live-values read is the slow part of the CVars page (~3s: SSH -> kubectl
# exec -> merge 6 INI layers). Cache it briefly so repeat opens are instant. A
# short TTL (not hours) keeps it fresh enough that out-of-band changes (CLI
# edits, queued overrides applied at a maintenance restart) surface quickly; a
# successful panel write busts the cache immediately via _invalidate_live_read().
LIVE_READ_TTL = 90.0
_live_read_cache: dict = {"data": None, "fetched_at": 0.0}


def _invalidate_live_read():
    """Drop the cached live-values read so the next page load re-fetches.
    Called after a successful CVar write (UserOverrides.ini changed)."""
    _live_read_cache["data"] = None
    _live_read_cache["fetched_at"] = 0.0


async def _relay_read(force: bool = False) -> dict | None:
    now = time.monotonic()
    cached = _live_read_cache["data"]
    if not force and cached is not None and now - _live_read_cache["fetched_at"] < LIVE_READ_TTL:
        return cached
    try:
        data = await call_relay("/server/cvars/read", timeout=30)
        _live_read_cache["data"] = data
        _live_read_cache["fetched_at"] = now
        return data
    except HTTPException as exc:
        logger.warning("relay /server/cvars/read failed: %s", exc.detail)
        return cached  # stale-on-error: better than nothing
    except Exception:
        logger.warning("relay /server/cvars/read unexpected", exc_info=True)
        return cached


async def _relay_diff(since: str | None = None) -> dict | None:
    path = "/server/cvars/diff"
    if since:
        path = f"{path}?since={since}"
    try:
        return await call_relay(path, timeout=30)
    except HTTPException as exc:
        logger.warning("relay /server/cvars/diff failed: %s", exc.detail)
        return None
    except Exception:
        logger.warning("relay /server/cvars/diff unexpected", exc_info=True)
        return None


async def _relay_history(limit: int, offset: int) -> dict | None:
    try:
        return await call_relay(
            f"/server/cvars/history?limit={limit}&offset={offset}", timeout=30,
        )
    except HTTPException as exc:
        logger.warning("relay /server/cvars/history failed: %s", exc.detail)
        return None
    except Exception:
        logger.warning("relay /server/cvars/history unexpected", exc_info=True)
        return None


# --- Compute canonical drift client-side from the live read + canonical.toml ---

def _compute_canonical_drift(live: dict | None, canonical: dict | None) -> dict:
    """Compare per-key effective values against the canonical TOML file. The
    canonical file is a small flat map of `<section>.<key> = "value"` entries
    (or grouped under section tables); we accept either shape. Drift = key
    present in both with mismatched value. Missing-on-one-side is not drift.

    Returns: {drifts: [{key, section, expected, actual, source}]}
    """
    if not live or not canonical:
        return {"drifts": [], "total": 0}

    # Flatten the canonical TOML into (section, key) -> value. The seed file
    # uses [section."key"] = value shape; we also accept a flat top-level map
    # for forward compat.
    flat: dict[tuple[str, str], str] = {}
    for top_key, top_val in canonical.items():
        if isinstance(top_val, dict):
            section = top_key
            for inner_key, inner_val in top_val.items():
                if isinstance(inner_val, (str, int, float, bool)):
                    flat[(section, inner_key)] = str(inner_val)
        elif isinstance(top_val, (str, int, float, bool)):
            # Flat top-level "section.key" = value form (rare; allowed).
            if "." in top_key:
                section, key = top_key.rsplit(".", 1)
                flat[(section, key)] = str(top_val)

    drifts = []
    for s in (live.get("settings") or []):
        section = s.get("section") or ""
        key = s.get("key") or ""
        actual = s.get("current_value")
        expected = flat.get((section, key))
        if expected is None:
            continue
        if str(actual) != str(expected):
            drifts.append({
                "section": section,
                "key": key,
                "expected": expected,
                "actual": actual,
                "source": s.get("source"),
            })
    return {"drifts": drifts, "total": len(drifts)}


# --- Page route ---

@router.get("/v2/server/cvars")
async def cvars_page(request: Request, sub: str = "live"):
    """CVars tab shell. First-paint includes the active sub-tab inline so the
    page works without JS / before HTMX swaps the body."""
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    sub = sub if sub in VALID_SUBTABS else "live"

    ctx: dict = {
        "user": user,
        "current_tab": "server",
        "current_sub_tab": "cvars",
        # VC2 P1 — Server hub shell hint. Mirrors current_sub_tab; both keys
        # are passed so the existing server_subnav.html (which reads
        # current_sub_tab) and any forward-looking server.html shell template
        # (which would read current_server_sub) light up the CVars tab.
        "current_server_sub": "cvars",
        "active_subtab": sub,
        "queued_banner": QUEUED_BANNER,
    }

    if sub == "live":
        live = await _relay_read()
        ctx["live"] = _enrich_live(live)
        ctx["live_available"] = live is not None
    elif sub == "catalog":
        catalog = _load_catalog()
        live = await _relay_read()
        ctx["catalog"] = _enrich_catalog(catalog)
        ctx["catalog_available"] = catalog is not None
        ctx["live"] = _enrich_live(live)
        ctx["live_available"] = live is not None
    elif sub == "history":
        data = await _relay_history(HISTORY_PAGE_SIZE, 0)
        ctx["history"] = (data or {}).get("rows") or []
        ctx["history_total"] = (data or {}).get("total") or 0
        ctx["history_page"] = 1
        ctx["history_page_size"] = HISTORY_PAGE_SIZE
        ctx["history_available"] = data is not None
    elif sub == "diff":
        live = await _relay_read()
        canonical = _load_canonical()
        settings_diff = await _relay_diff()
        ctx["canonical_diff"] = _compute_canonical_drift(live, canonical)
        ctx["canonical_diff_available"] = (live is not None) and (canonical is not None)
        ctx["settings_diff"] = settings_diff or {}
        ctx["settings_diff_available"] = settings_diff is not None
        ctx["canonical"] = canonical or {}

    return templates.TemplateResponse(request, "v2/cvars.html", ctx)


# --- Fragment routes (HTMX swap targets) ---

@router.get("/v2/server/cvars/live")
async def cvars_live_fragment(request: Request):
    require_admin(request)
    live = await _relay_read()
    return templates.TemplateResponse(
        request,
        "v2/_fragments/cvars_live.html",
        {
            "live": _enrich_live(live),
            "live_available": live is not None,
        },
    )


@router.get("/v2/server/cvars/catalog")
async def cvars_catalog_fragment(request: Request):
    require_admin(request)
    catalog = _load_catalog()
    live = await _relay_read()
    return templates.TemplateResponse(
        request,
        "v2/_fragments/cvars_catalog.html",
        {
            "catalog": _enrich_catalog(catalog),
            "catalog_available": catalog is not None,
            "live": _enrich_live(live),
            "live_available": live is not None,
        },
    )


@router.get("/v2/server/cvars/history")
async def cvars_history_fragment(request: Request, page: int = 1):
    require_admin(request)
    page = max(page, 1)
    offset = (page - 1) * HISTORY_PAGE_SIZE
    data = await _relay_history(HISTORY_PAGE_SIZE, offset)
    rows = (data or {}).get("rows") or []
    total = (data or {}).get("total") or 0
    return templates.TemplateResponse(
        request,
        "v2/_fragments/cvars_history.html",
        {
            "history": rows,
            "history_total": total,
            "history_page": page,
            "history_page_size": HISTORY_PAGE_SIZE,
            "history_available": data is not None,
        },
    )


@router.get("/v2/server/cvars/diff")
async def cvars_diff_fragment(request: Request):
    require_admin(request)
    live = await _relay_read()
    canonical = _load_canonical()
    settings_diff = await _relay_diff()
    return templates.TemplateResponse(
        request,
        "v2/_fragments/cvars_diff.html",
        {
            "canonical_diff": _compute_canonical_drift(live, canonical),
            "canonical_diff_available": (live is not None) and (canonical is not None),
            "settings_diff": settings_diff or {},
            "settings_diff_available": settings_diff is not None,
            "canonical": canonical or {},
        },
    )


# --- Write route ---

class CvarWriteRequest(BaseModel):
    section: str
    key: str
    value: str                          # "" = clear (icehunter remove convention)
    reason: str | None = None
    confirm_token: str | None = None    # operator must echo "CONFIRM"


@router.post("/v2/server/cvars/write")
async def cvars_write(request: Request, body: CvarWriteRequest):
    """Operator-gated CVar write. Calls the relay's POST /server/cvars/write
    which owns the atomic transaction (file backup → INI patch → INSERT into
    holadmin.cvar_changes → return audit envelope). Admin-backend records its
    own audit_log row referencing the returned change_id.

    Banner copy on success: "Queued — applies at next maintenance restart".
    NEVER "Saved" — UserOverrides.ini changes are filesystem-only and only
    take effect on the next announced maintenance restart of the game pod.
    """
    user = require_admin(request)
    require_csrf(request, user)

    ip = request.client.host if request.client else "unknown"
    target = f"{body.section}:{body.key}"
    operator_handle = user["username"]  # stored in operator_discord_id TEXT col

    # --- Validation (failures are audited too) ---
    try:
        if not body.section or not isinstance(body.section, str):
            raise HTTPException(400, "section is required")
        if len(body.section) > MAX_SECTION_LEN or not _INI_SECTION_RE.match(body.section):
            raise HTTPException(400, "section has invalid shape or is too long")
        if not body.key or not isinstance(body.key, str):
            raise HTTPException(400, "key is required")
        if len(body.key) > MAX_KEY_LEN or not _INI_KEY_RE.match(body.key):
            raise HTTPException(400, "key has invalid shape or is too long")
        if not isinstance(body.value, str):
            raise HTTPException(400, "value must be a string (empty-string = clear)")
        if len(body.value) > MAX_VALUE_LEN:
            raise HTTPException(400, "value too long")
        if body.reason is not None and len(body.reason) > MAX_REASON_LEN:
            raise HTTPException(400, "reason too long")
        if body.confirm_token != CONFIRM_TOKEN_EXPECTED:
            raise HTTPException(400, "confirm_token mismatch — write rejected")
    except HTTPException as exc:
        audit_log(
            user["id"], user["username"], "cvar_write",
            target, ip,
            details=json.dumps({
                "section": body.section,
                "key": body.key,
                "value": body.value,
                "reason": body.reason,
                "result": f"validation_error: {exc.detail}",
            }),
            success=False,
        )
        raise

    # Best-effort pre-write read so we can attribute `ini_layer_before` to
    # the actual winning layer (defaultGame / userOverrides / etc.) rather
    # than the helper's fallback heuristic. A failure here does NOT block
    # the write — the relay falls back to its own attribution.
    ini_layer_before: str | None = None
    pre_read = await _relay_read()
    if pre_read:
        for s in (pre_read.get("settings") or []):
            if s.get("section") == body.section and s.get("key") == body.key:
                ini_layer_before = s.get("source")
                break

    relay_body = {
        "section": body.section,
        "key": body.key,
        "value": body.value,
        "operator": operator_handle,
        "operator_user_id": user["id"],
        "ini_layer_before": ini_layer_before,
        "reason": body.reason,
    }

    try:
        result = await call_relay("/server/cvars/write", "POST", relay_body, timeout=60)
    except HTTPException as exc:
        audit_log(
            user["id"], user["username"], "cvar_write",
            target, ip,
            details=json.dumps({
                "section": body.section,
                "key": body.key,
                "value": body.value,
                "reason": body.reason,
                "result": f"relay_error: {exc.detail}",
            }),
            success=False,
        )
        return {
            "success": False,
            "status": "failed",
            "message": str(exc.detail),
            "banner": None,
            "change_id": None,
        }
    except Exception as exc:
        audit_log(
            user["id"], user["username"], "cvar_write",
            target, ip,
            details=json.dumps({
                "section": body.section,
                "key": body.key,
                "value": body.value,
                "reason": body.reason,
                "result": f"error: {exc}",
            }),
            success=False,
        )
        return {
            "success": False,
            "status": "failed",
            "message": str(exc),
            "banner": None,
            "change_id": None,
        }

    status = (result or {}).get("status", "ok")
    success = status == "ok"
    if success:
        # UserOverrides.ini changed; drop the cached live read so the next page
        # load reflects the new value immediately instead of waiting out the TTL.
        _invalidate_live_read()
    change_id = (result or {}).get("change_id")
    old_value = (result or {}).get("old_value")
    source_before = (result or {}).get("source_before")

    audit_db_failed = bool((result or {}).get("audit_db_failed"))
    row_status = (result or {}).get("row_status")

    audit_log(
        user["id"], user["username"], "cvar_write",
        target, ip,
        details=json.dumps({
            "section": body.section,
            "key": body.key,
            "value": body.value,
            "old_value": old_value,
            "source_before": source_before,
            "reason": body.reason,
            "change_id": change_id,
            "row_status": row_status,
            "audit_db_failed": audit_db_failed,
            "bak_path": (result or {}).get("bak_path"),
            "sha256_before": (result or {}).get("sha256_before"),
            "sha256_after": (result or {}).get("sha256_after"),
            "result": status,
        }),
        # If the file write succeeded but the DB INSERT raced, the operator
        # still sees the UX banner — the work IS applied — but the audit
        # row carries success=False so the discrepancy is grep-able later.
        success=success and not audit_db_failed,
    )

    return {
        "success": success,
        "status": status,
        "banner": QUEUED_BANNER if success else None,
        "message": (result or {}).get("error", "") if not success else "",
        "change_id": change_id,
        "old_value": old_value,
        "new_value": (result or {}).get("new_value"),
        "source_before": source_before,
        "audit_db_failed": audit_db_failed,
        "row_status": row_status,
    }
