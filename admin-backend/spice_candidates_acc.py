"""Per-Coriolis-cycle accumulation of Large spice candidate sectors.

The live RAM reader on the game box only instantiates ~3 candidate Large fields
at once (1 active + 2 dormant) and rotates them across the cycle's sites, so any
single read of /dune/spice/active sees a small subset of `ram_candidates`. The
public portal already polls the live spice feed (~90s TTL), so here we union each
poll's candidate sectors into admin.db keyed by Coriolis cycle. The map then
plots EVERY candidate site observed this cycle, not just the current few.

Freeze-safe by design: this accumulates only data the relay already returns; it
makes no change to (and no extra call against) the game box. When the box is
unfrozen, dune-spice-active.py can be upgraded to emit the on-box `candidates_acc`
(and exact per-candidate coords) directly, at which point this becomes a
belt-and-suspenders fallback.

cycle_key: the Deep Desert regenerates every Coriolis storm (14 days). We bucket
sightings by the integer cycle index relative to a known reset anchor, so a new
cycle starts a fresh accumulation automatically and stale sites age out.
"""
import datetime
import logging
import os

from database import get_db

logger = logging.getLogger(__name__)

# A known Coriolis cycle boundary + the cycle period. The engine logs a fixed
# 05:00 UTC / 14-day cadence ("LogCoriolis: Next Coriolis Cycle start date UTC:
# 2026.06.30-05.00.00"), so 2026-06-16T05:00Z is the cycle-boundary anchor: the
# boundary the next-reset countdown tracks, NOT the 12:00Z manual-reset wall-clock
# used before. Overridable via env for when the cadence shifts.
_ANCHOR_ISO = os.environ.get("LASTSIETCH_CORIOLIS_ANCHOR", "2026-06-16T05:00:00+00:00")
_PERIOD_DAYS = float(os.environ.get("LASTSIETCH_CORIOLIS_PERIOD_DAYS", "14"))

# Defense in depth vs the reader emitting a previous-cycle ghost: drop a cycle
# sector not re-observed within this window. The reader re-emits every held site
# each ~90s poll, so a genuinely-held site is never this stale; only a site the
# reader has dropped (a ghost) freezes and ages past the cutoff. Generous enough
# to survive a maintenance window (the map repopulates when the reader resumes).
_STALE_HOURS = float(os.environ.get("LASTSIETCH_SPICE_CANDIDATE_STALE_HOURS", "12"))


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stale_cutoff_iso() -> str:
    return (_now() - datetime.timedelta(hours=_STALE_HOURS)).isoformat()


def cycle_key(now: datetime.datetime | None = None) -> str:
    """Integer Coriolis cycle index (as a string) for bucketing sightings."""
    now = now or _now()
    try:
        anchor = datetime.datetime.fromisoformat(_ANCHOR_ISO)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=datetime.timezone.utc)
        period = _PERIOD_DAYS * 86_400
        if period <= 0:
            return "0"
        idx = int((now - anchor).total_seconds() // period)
        return str(idx)
    except Exception as exc:
        logger.warning("spice_candidates_acc: cycle_key failed: %s", exc)
        return "0"


def cycle_window(now: datetime.datetime | None = None) -> dict | None:
    """Current + next Coriolis cycle boundaries as UTC ISO 'Z' strings.

    Deterministic from the anchor: the engine logs a fixed 05:00 UTC / 14-day
    cadence, so the next-reset countdown the portal renders needs no game-box
    read. Returns {"cycle_start_utc", "next_cycle_utc"} or None on error."""
    now = now or _now()
    try:
        anchor = datetime.datetime.fromisoformat(_ANCHOR_ISO)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=datetime.timezone.utc)
        period = datetime.timedelta(days=_PERIOD_DAYS)
        if period.total_seconds() <= 0:
            return None
        idx = int((now - anchor).total_seconds() // period.total_seconds())
        start = anchor + idx * period
        nxt = start + period

        def _z(d: datetime.datetime) -> str:
            return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {"cycle_start_utc": _z(start), "next_cycle_utc": _z(nxt)}
    except Exception as exc:
        logger.warning("spice_candidates_acc: cycle_window failed: %s", exc)
        return None


def record(active: dict | None) -> None:
    """Union this poll's per-dim `ram_candidates` (+ the live `ram_sector`) into
    the current cycle's accumulation. Never raises; a logging failure must not
    break the map feed."""
    if not isinstance(active, dict):
        return
    dims = active.get("dimensions") or {}
    if not dims:
        return
    ck = cycle_key()
    now_iso = _now().isoformat()
    # (dim, sector, x, y) -- x/y are None until the reader emits ram_candidates_xy.
    rows: list[tuple[str, str, float | None, float | None]] = []
    for dim, info in dims.items():
        if not isinstance(info, dict):
            continue
        # Exact coords per sector (Part B), when the reader provides them.
        xy: dict[str, tuple[float, float]] = {}
        for c in (info.get("ram_candidates_xy") or []):
            sec = (c.get("sector") or "").strip().upper()
            if sec and c.get("x") is not None and c.get("y") is not None:
                xy[sec] = (float(c["x"]), float(c["y"]))
        secs = set(info.get("ram_candidates") or [])
        secs.update(xy)
        live = (info.get("ram_sector") or "").strip().upper()
        if live and info.get("large_active"):
            secs.add(live)
            if info.get("ram_x") is not None and info.get("ram_y") is not None:
                xy.setdefault(live, (float(info["ram_x"]), float(info["ram_y"])))
        for sec in secs:
            sec = (sec or "").strip().upper()
            if sec:
                wx, wy = xy.get(sec, (None, None))
                rows.append((str(dim), sec, wx, wy))
    if not rows:
        return
    try:
        conn = get_db()
        try:
            # COALESCE keeps a previously-stored coord if a later poll lacks it (the
            # site rotated away) and fills it in once the reader first emits it.
            conn.executemany(
                """INSERT INTO spice_candidate_acc (cycle_key, dim, sector, first_utc, last_utc, x, y)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cycle_key, dim, sector)
                   DO UPDATE SET last_utc = excluded.last_utc,
                                 x = COALESCE(excluded.x, spice_candidate_acc.x),
                                 y = COALESCE(excluded.y, spice_candidate_acc.y)""",
                [(ck, dim, sec, now_iso, now_iso, wx, wy) for (dim, sec, wx, wy) in rows],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("spice_candidates_acc: record failed: %s", exc)


def by_dim() -> dict[str, list[str]]:
    """{dim: [sectors]} accumulated for the current cycle (sorted)."""
    ck = cycle_key()
    out: dict[str, list[str]] = {}
    try:
        conn = get_db()
        try:
            cur = conn.execute(
                "SELECT dim, sector FROM spice_candidate_acc "
                "WHERE cycle_key = ? AND last_utc >= ? ORDER BY dim, sector",
                (ck, _stale_cutoff_iso()),
            )
            for dim, sec in cur.fetchall():
                out.setdefault(str(dim), []).append(sec)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("spice_candidates_acc: by_dim failed: %s", exc)
    return out


def coords_by_sector() -> dict[str, tuple[float, float]]:
    """{sector: (x, y)} exact world coords accumulated this cycle (Part B), for the
    sectors the reader has reported coords for. Empty until Phase 2 feeds x/y; the
    map then plots candidates at their true position instead of the sector center.
    On a coord conflict across dims (mediums/larges coincide spatially) the last
    write wins -- candidate sites are dim-agnostic in position."""
    ck = cycle_key()
    out: dict[str, tuple[float, float]] = {}
    try:
        conn = get_db()
        try:
            cur = conn.execute(
                "SELECT sector, x, y FROM spice_candidate_acc "
                "WHERE cycle_key = ? AND last_utc >= ? AND x IS NOT NULL AND y IS NOT NULL",
                (ck, _stale_cutoff_iso()),
            )
            for sec, x, y in cur.fetchall():
                if sec:
                    out[sec.strip().upper()] = (float(x), float(y))
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("spice_candidates_acc: coords_by_sector failed: %s", exc)
    return out


def union(extra: set[str] | None = None) -> list[str]:
    """Flat sorted union of all candidate sectors for the current cycle, optionally
    merged with `extra` (the instantaneous set) so the result is never smaller than
    what is live right now."""
    secs: set[str] = set(extra or set())
    for sectors in by_dim().values():
        secs.update(sectors)
    return sorted(s for s in secs if s)
