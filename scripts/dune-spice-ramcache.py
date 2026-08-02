#!/usr/bin/env python3
"""Event-driven active-Large-spice-field cache builder (lastsietch-dune, root, read-only).

Runs on a short systemd timer (~90s). Each tick is cheap: it reads the live Large
spice field_id per Deep Desert dimension from the DB (dune.resourcefield_state).
The expensive RAM scan (dune-spice-ramread.py, a full heap walk of the two DD game
processes, minutes long) is triggered ONLY when a dimension's field_id changed vs
the cache (= a Coriolis/worm rotation), plus a periodic safety re-scan.

It writes /root/dune-spice-ramcache.json: the authoritative live sector per dim,
paired with the field_id it corresponds to and the bloom discriminator
(m_BloomVariationIndex/m_ActiveSequence) that PROVES it is the surfaced field.
The DB field_id is only the cheap rotation trigger; the RAM bloom discriminator is
the source of truth for WHICH resident Large is active (dormant craters tie on
value). dune-spice-active.py reads that cache and, when the cached field_id still
matches the live one, emits the exact sector (replacing the survey/cluster guess)
so /portal/spice auto-pins the active Large.

READ-ONLY end to end: a read-only DB query (kubectl exec psql) + a read-only
/proc/<pid>/mem scan. It never writes the DB and never touches the game pods.
"""
import json
import os
import re
import subprocess
import sys
import fcntl
from datetime import datetime, timezone, timedelta

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DBPOD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"

CACHE = "/root/dune-spice-ramcache.json"
LOCK = "/run/lastsietch-spice-ramcache.lock"
RAMREAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dune-spice-ramread.py")
SAFETY_SECS = 21600         # force a re-scan at least every 6h even with no rotation
                            # (rotations trigger their own scan; this is just a backstop)
INACTIVE_RETRY_SECS = 300   # if a dim is cached active=false, re-scan within ~5 min.
                            # A scan that lands while the world is still booting (active
                            # flag not yet set, e.g. right after a build roll) caches
                            # active=false; the field_id + 6h-safety triggers would leave
                            # it wrong for hours. This retries the not-yet-active field.
SCAN_TIMEOUT = 900          # the full heap walk of both DD procs can take minutes
# Candidate accumulation: only ~3 Large fields are instantiated at once (1 active
# + 2 dormant), and they rotate/respawn across the cycle's candidate SITES, so no
# single scan ever sees them all. We accumulate the distinct sectors observed over
# the cycle (the count genuinely varies, 2-5). A sector ages out PRUNE_SECS after
# it was last seen (> one 14-day Coriolis cycle = a clean backstop); a fully-
# disjoint observation (all-new sectors) is a Coriolis regeneration -> hard reset.
PRUNE_SECS = 15 * 86400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_secs(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


# Coriolis cycle boundary (same 05:00 UTC / 14-day cadence the portal uses). The
# fully-disjoint reset in accumulate() misses a re-roll when the new seed shares a
# sector with the old (e.g. sector I5 common to seed 2 and seed 6 on 2026-07-14),
# leaving previous-cycle ghosts that only age out after PRUNE_SECS (> a full cycle).
# Evicting entries last confirmed BEFORE the current cycle start is immune to that.
_CORIOLIS_ANCHOR = os.environ.get("LASTSIETCH_CORIOLIS_ANCHOR", "2026-06-16T05:00:00+00:00")
_CORIOLIS_PERIOD_SECS = float(os.environ.get("LASTSIETCH_CORIOLIS_PERIOD_DAYS", "14")) * 86400


def _cycle_start_iso() -> str | None:
    """UTC ISO of the current Coriolis cycle start, or None on error (then skip the
    boundary evict and fall back to the disjoint-reset + PRUNE_SECS backstop)."""
    try:
        anchor = datetime.fromisoformat(_CORIOLIS_ANCHOR)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        if _CORIOLIS_PERIOD_SECS <= 0:
            return None
        now = datetime.now(timezone.utc)
        idx = int((now - anchor).total_seconds() // _CORIOLIS_PERIOD_SECS)
        return (anchor + timedelta(seconds=idx * _CORIOLIS_PERIOD_SECS)).isoformat(timespec="seconds")
    except Exception:
        return None


def _before_cycle_start(ts: str, cycle_start: str | None) -> bool:
    """True if ISO ts is strictly before the cycle start (parsed, offset-safe)."""
    if not cycle_start or not ts:
        return False
    try:
        return datetime.fromisoformat(ts) < datetime.fromisoformat(cycle_start)
    except ValueError:
        return False


_DB_CREDS: tuple[str, str] | None = None


def _db_creds() -> tuple[str, str]:
    """(password, port) read once from the db pod env. The DB service port differs
    by box (15433 on the old box, 15432 on the EPYC) -> read it from the pod rather
    than hardcode, so the same script works before AND after the hardware cutover.
    The env var name (..._DB_DBDEPL_SVC_SERVICE_PORT) is stable across boxes; only
    its value changes. Falls back to 15433 (the historical default) if absent."""
    global _DB_CREDS
    if _DB_CREDS is None:
        env = subprocess.run(
            ["kubectl", "exec", "-n", NS, DBPOD, "--", "printenv"],
            capture_output=True, text=True, timeout=20).stdout
        pw, port = "", "15433"
        for line in env.splitlines():
            k, _, v = line.partition("=")
            if k == "POSTGRES_PASSWORD":
                pw = v.strip()
            elif k.endswith("_DB_DBDEPL_SVC_SERVICE_PORT") and v.strip().isdigit():
                port = v.strip()
        _DB_CREDS = (pw, port)
    return _DB_CREDS


def _psql(sql: str) -> str:
    """Read-only SQL in the live dune DB pod (tuples-only, unaligned)."""
    pgpass, pgport = _db_creds()
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, DBPOD, "--",
         "env", f"PGPASSWORD={pgpass}", "psql", "-h", "localhost", "-p", pgport,
         "-U", "postgres", "-d", "dune", "-tAc", sql],
        capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "psql failed").strip()[:300])
    return out.stdout.strip()


def live_field_ids() -> dict[int, str]:
    """{dimension_index: field_id} for the Large per DD dimension (highest
    value_remaining Spice field; Large=2,500,000 vs Medium=150,000 / Small=5,000).

    This is only the cheap ROTATION TRIGGER: when this field_id changes vs the
    cache we kick the (expensive) RAM scan, which then authoritatively picks the
    surfaced Large via the bloom discriminator. The Larges all tie at 2,500,000
    when several coexist (the dormant-craters case), and json_agg order is not
    guaranteed, so break ties on the field_id itself -> a STABLE pick across ticks
    (otherwise the trigger flickers 'rotated' between equal-value Larges and the
    live pin drops to 'awaiting' for no reason)."""
    sql = ("SELECT json_agg(json_build_object('dim', dimension_index, "
           "'fid', field_id::text, 'val', value_remaining)) "
           "FROM dune.resourcefield_state WHERE map='DeepDesert' AND field_kind_id=1;")
    best: dict[int, tuple[int, str]] = {}   # dim -> (value, field_id)
    for r in (json.loads(_psql(sql)) or []):
        v = r.get("val")
        if v is None:
            continue
        d, fid = int(r["dim"]), str(r["fid"])
        cand = (int(v), fid)
        cur = best.get(d)
        # higher value wins; on a tie the lexicographically-smaller field_id wins
        # (deterministic), so the trigger key is stable while several Larges coexist.
        if cur is None or cand[0] > cur[0] or (cand[0] == cur[0] and fid < cur[1]):
            best[d] = cand
    return {d: fid for d, (_v, fid) in best.items()}


def load_cache() -> dict:
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_cache(cache: dict) -> None:
    tmp = f"{CACHE}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CACHE)


def _update_candidate_accumulator(cache: dict, observed: dict[int, set[str]],
                                  observed_xy: dict[int, dict[str, tuple]] | None,
                                  now_iso: str) -> None:
    """Accumulate the distinct Large candidate SECTORS observed this cycle, per dim.

    A single scan only ever sees the ~3 resident Larges (1 active + 2 dormant), but
    the cycle's full candidate set (2-5 sites) is the union of what's resident over
    the whole cycle as the blow rotates and depleted fields respawn elsewhere. We
    track {sector: last_seen_iso} per dim so:
      - a fresh observation refreshes/extends the set (grows, never shrinks
        mid-cycle -> the map stops flapping);
      - an observation fully DISJOINT from the current fresh set means the world
        regenerated at the Coriolis reset -> hard-reset that dim to the new set;
      - any sector not re-seen within PRUNE_SECS ages out (backstop for the case a
        reset's new set partially overlaps the old one).
    """
    acc = cache.setdefault("meta", {}).setdefault("candidates_acc", {})
    # Parallel coord accumulator (Part B): {dim: {sector: [x, y]}}, kept in lockstep
    # with the sector accumulator so a site that rotated away keeps its exact coord.
    acc_xy = cache["meta"].setdefault("candidates_acc_xy", {})
    observed_xy = observed_xy or {}
    for d, secs in observed.items():
        if not secs:
            continue                      # no resident Large this scan -> nothing to add
        key = str(d)
        prev = acc.get(key) or {}
        prev_xy = acc_xy.get(key) or {}
        # Disjoint from everything we currently hold => Coriolis regeneration.
        if prev and secs.isdisjoint(prev.keys()):
            prev = {}
            prev_xy = {}
        xy = observed_xy.get(d) or {}
        for s in secs:
            prev[s] = now_iso
            if s in xy:
                prev_xy[s] = [xy[s][0], xy[s][1]]
        # Age out sectors not seen within the prune window (coords follow sectors),
        # AND evict previous-cycle ghosts last confirmed before the current Coriolis
        # cycle start (the disjoint-reset misses these when the new seed overlaps).
        cycle_start = _cycle_start_iso()
        prev = {s: ts for s, ts in prev.items()
                if _age_secs(ts) <= PRUNE_SECS and not _before_cycle_start(ts, cycle_start)}
        prev_xy = {s: c for s, c in prev_xy.items() if s in prev}
        acc[key] = prev
        acc_xy[key] = prev_xy


def run_ramread() -> list[dict]:
    """Run the (root) RAM reader in v2 system-array mode; return its per-process
    list, or [] on failure. `--system` reads the USpiceHarvestingSystem candidate
    array directly (active blow via surfaced/+0x4D0 picker, auto-discovered
    candidates) instead of the v1 full-heap vptr scan. Same JSON contract."""
    out = subprocess.run([sys.executable, RAMREAD, "--system"], capture_output=True,
                         text=True, timeout=SCAN_TIMEOUT)
    try:
        return json.loads(out.stdout).get("processes", [])
    except ValueError:
        return []


def main() -> int:
    # Single-flight: a slow scan must not overlap the next timer tick.
    lock_fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0    # a previous (scanning) run still holds the lock; skip this tick

    cache = load_cache()
    try:
        live = live_field_ids()
    except Exception as exc:
        cache.setdefault("meta", {})["last_error"] = str(exc)[:200]
        cache["meta"]["last_poll_utc"] = _now()
        write_cache(cache)
        return 1

    reasons = {}
    for d in (0, 1):
        key = str(d)
        live_fid = live.get(d)
        if live_fid is None:                      # no active Large this dim -> drop stale entry
            if key in cache:
                cache.pop(key)
                reasons[d] = "no-active-large"
            continue
        ent = cache.get(key) or {}
        if ent.get("field_id") != live_fid:
            reasons[d] = "rotated" if ent else "first-seen"
        elif ent.get("active") is False and _age_secs(ent.get("scanned_utc")) > INACTIVE_RETRY_SECS:
            reasons[d] = "inactive-retry"
        elif _age_secs(ent.get("scanned_utc")) > SAFETY_SECS:
            reasons[d] = "safety"

    if reasons:
        procs = run_ramread()
        scanned = _now()
        by_dim = {p.get("dim"): p for p in procs if p.get("dim") is not None}
        observed: dict[int, set[str]] = {}
        observed_xy: dict[int, dict[str, tuple[float, float]]] = {}
        for d in (0, 1):
            if d not in reasons:
                continue
            live_fid = live.get(d)
            p = by_dim.get(d)
            large = (p or {}).get("large")
            # Auto-discovered Large candidate SITES resident THIS SCAN (active blow
            # + dormant craters; the value=2.5M fields in the system array). Only
            # ~3 exist at once, so this snapshot feeds the per-cycle accumulator
            # below, which unions them over the cycle into the full candidate set.
            large_cands = sorted({
                c.get("sector") for c in (p or {}).get("candidates", [])
                if c.get("value") == 2_500_000 and c.get("sector")})
            # Same sites WITH exact coords (Part B), one entry per sector.
            large_cands_xy: list[dict] = []
            _seen_sec: set[str] = set()
            for c in (p or {}).get("candidates", []):
                sec = c.get("sector")
                if (c.get("value") == 2_500_000 and sec and sec not in _seen_sec
                        and c.get("x") is not None and c.get("y") is not None):
                    _seen_sec.add(sec)
                    large_cands_xy.append({"sector": sec, "x": c["x"], "y": c["y"]})
            # Full Medium-field layer this scan (exact coords, already deduped by the
            # reader). Mediums are the complete per-cycle set, so no accumulation.
            mediums = (p or {}).get("mediums") or []
            # Every surfaced (active) Large this scan, not just the first. A Spice
            # Harvest event raises max_globally_active so 2-3 Larges erupt at once;
            # `large` keeps the first for back-compat while this carries the full set.
            actives = [
                {"sector": a.get("sector"), "x": a.get("x"), "y": a.get("y"),
                 "bloom": a.get("bloom"), "active_seq": a.get("active_seq")}
                for a in ((p or {}).get("actives") or []) if a.get("sector")]
            # Sectors observed this scan = resident craters + the active sector.
            obs = set(large_cands)
            obs_xy = {c["sector"]: (c["x"], c["y"]) for c in large_cands_xy}
            if large and large.get("sector"):
                obs.add(large["sector"])
                if large.get("x") is not None and large.get("y") is not None:
                    obs_xy.setdefault(large["sector"], (large["x"], large["y"]))
            observed[d] = obs
            observed_xy[d] = obs_xy
            if live_fid and large and large.get("sector"):
                # The reader's `large` is ALREADY the surfaced field (bloom != -1);
                # store its discriminator (bloom/active_seq) so the consumer can
                # prove activeness and so a future dormant-coexisting case is
                # self-evident in the cache rather than inferred.
                cache[str(d)] = {
                    "field_id": live_fid,
                    "sector": large["sector"],
                    "x": large.get("x"), "y": large.get("y"),
                    "value": large.get("value"),
                    "bloom": large.get("bloom"),
                    "active_seq": large.get("active_seq"),
                    "active": True,
                    "actives": actives,
                    "candidates": large_cands,
                    "candidates_xy": large_cands_xy,
                    "mediums": mediums,
                    "vptr_source": (p or {}).get("vptr_source"),
                    "scanned_utc": scanned,
                    "reason": reasons[d],
                }
            elif p is not None:
                # The scan ran and found NO surfaced Large for this dim (all
                # resident Larges read bloom == -1, i.e. between rotations / the
                # bubble is awaiting). Record that explicitly so the consumer shows
                # 'awaiting' instead of pinning the previous cycle's stale sector.
                cache[str(d)] = {
                    "field_id": live_fid,
                    "sector": None,
                    "active": False,
                    "actives": actives,
                    "candidates": large_cands,
                    "candidates_xy": large_cands_xy,
                    "mediums": mediums,
                    "large_resident": p.get("large_resident"),
                    "vptr_source": p.get("vptr_source"),
                    "scanned_utc": scanned,
                    "reason": reasons[d],
                }
        _update_candidate_accumulator(cache, observed, observed_xy, scanned)
        cache.setdefault("meta", {})["last_scan_utc"] = scanned
        cache["meta"]["last_scan_reasons"] = {str(k): v for k, v in reasons.items()}
        cache["meta"].pop("last_error", None)

    cache.setdefault("meta", {})["last_poll_utc"] = _now()
    cache["meta"]["live_field_ids"] = {str(d): f for d, f in live.items()}
    write_cache(cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
