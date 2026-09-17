"""Deep Desert spice-field tracker for the public portal.

Loads data/dune-spice-fields.json (the current Coriolis cycle's surveyed field
sites, keyed by 9x9 grid sector like 'F1') and builds the grid model the
template renders. Deep Desert regenerates every Coriolis storm, so this sidecar
is a per-cycle survey, updated by hand from in-game screenshots.

Grid convention: rows A-I (A=south -> I=north) x columns 1-9 (west -> east); a
sector is letter+number, e.g. 'F1' = row F, column 1. We track the SET of
large/medium/small surveyed sites for the cycle, and (when available) overlay the
LIVE active Large field per dimension, located by clustering harvester positions
against the authoritative DD map bounds. See
docs/dune-research/SPICE-LIVE-ACTIVE-FIELD-RESEARCH-2026-06-03.md.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SIDECAR = Path(__file__).parent / "data" / "dune-spice-fields.json"
ROWS = "ABCDEFGHI"
COLS = [str(n) for n in range(1, 10)]
_SIZE_RANK = {"large": 3, "medium": 2, "small": 1}


def load() -> dict:
    """Read the spice-field sidecar. Returns {} on any failure so the page
    degrades to an empty grid rather than 500ing."""
    try:
        return json.loads(_SIDECAR.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("spice_fields: sidecar missing at %s", _SIDECAR)
    except Exception as exc:
        logger.warning("spice_fields: load failed: %s", exc)
    return {}


def build_grid(data: dict, active: dict | None = None) -> dict:
    """Turn the sidecar into a render-ready grid: 9 rows of 9 cells, each cell
    carrying its sector code and (if surveyed) the field size at that site.
    Largest field wins when sizes overlap on one sector.

    `active` is the live producer payload {"dimensions": {dim: {label,
    large_active, ram_sector, ram_field_id, ram_bloom, ram_active_seq, ...}}};
    when present, the cell whose sector matches a dimension's active Large is
    flagged so the template can highlight it, and a per-dimension `active` summary
    (carrying the bloom discriminator on a live pin) drives the live banner."""
    fields = data.get("fields") or {}
    by_sector: dict[str, str] = {}
    for size in ("small", "medium", "large"):
        for f in (fields.get(size) or []):
            sec = (f.get("sector") or "").strip().upper()
            if not sec:
                continue
            cur = by_sector.get(sec)
            if cur is None or _SIZE_RANK[size] >= _SIZE_RANK[cur]:
                by_sector[sec] = size

    # Live active-Large overlay, in priority order:
    #   1. RAM reader (dune-spice-ramcache.py): the exact actor transform read from
    #      the live game process, paired with the field_id it described. When that
    #      field_id still equals the live one, this is the AUTHORITATIVE auto-pin
    #      (status "live", confidence "exact") -- no survey needed.
    #   2. Survey cache (data["active"][dim] = {sector, field_id}): a field_id MATCH
    #      means the hand-surveyed sector is still current -> pin it.
    #   3. Otherwise: rotated (survey stale) or awaiting -> show the legal band, not
    #      a guess. (The clustered sector stays an untrusted hint; field_id is a
    #      non-invertible hash so position never comes from the DB alone.)
    survey = data.get("active") or {}
    active_by_sector: dict[str, list] = {}
    active_summary = []
    for dim, info in (active or {}).get("dimensions", {}).items():
        if not info.get("large_active"):
            continue
        live_fid = info.get("large_field_id")
        ram_fid = info.get("ram_field_id")
        ram_sec = (info.get("ram_sector") or "").strip().upper()
        # Per-dim live count (Spice Harvest raises it to 2-3); default to 1 on a
        # legacy producer payload that predates the count.
        dim_count = info.get("large_active_count") or 1
        rec = survey.get(str(dim)) or {}
        rec_fid = rec.get("field_id")
        rec_sec = (rec.get("sector") or "").strip().upper()
        # Multi-active fast path: when the reader surfaced more than one Large this
        # cycle and the scan still describes the live field (same field_id freshness
        # intent as the single-field RAM pin below), pin EVERY surfaced field as its
        # own live/exact entry. Each carries its own sector + bloom discriminator.
        ram_actives = info.get("ram_active_fields") or []
        ram_fresh = bool(ram_fid and live_fid and str(ram_fid) == str(live_fid)
                         and info.get("ram_scanned_utc"))
        if ram_actives and ram_fresh:
            for f in ram_actives:
                fsec = (f.get("sector") or "").strip().upper()
                if not fsec:
                    continue
                entry = {"label": info.get("label"), "sector": fsec,
                         "status": "live", "field_id": live_fid, "confidence": "exact",
                         "scanned_utc": info.get("ram_scanned_utc"),
                         "bloom": f.get("bloom"), "active_seq": f.get("active_seq"),
                         "dim_active_count": dim_count}
                active_summary.append(entry)
                active_by_sector.setdefault(fsec, []).append(entry)
            continue
        if ram_sec and ram_fid and live_fid and str(ram_fid) == str(live_fid):
            sector, status, conf = ram_sec, "live", "exact"   # RAM-read auto-pin
        elif rec_sec and rec_fid and live_fid and str(rec_fid) == str(live_fid):
            sector, status, conf = rec_sec, "surveyed", "surveyed"
        elif rec_sec and not live_fid:
            sector, status, conf = rec_sec, "surveyed", "surveyed"   # legacy, no live id
        elif live_fid and rec_fid and str(rec_fid) != str(live_fid):
            sector, status, conf = None, "rotated", info.get("confidence")
        else:
            sector, status, conf = None, "awaiting", info.get("confidence")
        entry = {"label": info.get("label"), "sector": sector or None,
                 "status": status, "field_id": live_fid, "confidence": conf,
                 "scanned_utc": info.get("ram_scanned_utc") if status == "live" else None,
                 # bloom discriminator proof (only meaningful on a live RAM pin):
                 # m_BloomVariationIndex >= 0 = surfaced/active, the basis for "exact".
                 "bloom": info.get("ram_bloom") if status == "live" else None,
                 "active_seq": info.get("ram_active_seq") if status == "live" else None,
                 "dim_active_count": dim_count}
        active_summary.append(entry)
        if sector:
            active_by_sector.setdefault(sector, []).append(entry)
    active_summary.sort(key=lambda e: (e.get("label") or "", e.get("sector") or ""))

    # Render top->bottom as I..A to match the in-game survey map (I at the top,
    # row A = the Shield Wall along the bottom edge).
    rows = []
    for r in reversed(ROWS):
        cells = []
        for c in COLS:
            sec = f"{r}{c}"
            cells.append({"sector": sec, "size": by_sector.get(sec),
                          "active": active_by_sector.get(sec)})
        rows.append({"label": r, "cells": cells})

    counts = {s: len(fields.get(s) or []) for s in ("large", "medium", "small")}
    return {
        "rows": rows,
        "cols": COLS,
        # Rows where a Large can legally spawn (baked-heatmap RE, validated vs
        # F1/I3/H5). The template shades these as the candidate band when the live
        # field is active but awaiting survey.
        "legal_rows": list("DEFGHI"),
        "counts": counts,
        "total_sites": sum(counts.values()),
        "cycle_label": data.get("cycle_label"),
        "cycle_ends": data.get("cycle_ends"),
        "updated_utc": data.get("updated_utc"),
        "note": data.get("note"),
        "active": active_summary,
    }
