#!/usr/bin/env python3
"""Live "active Large spice field" producer for the public spice map.

Read-only. Emits JSON on stdout describing, per Deep Desert dimension, whether a
Large spice field is currently active (from dune.spicefield_types.
current_globally_active) and which 9x9 sector it is in.

The SECTOR is authoritative from the RAM bloom discriminator (dune-spice-ramcache
-> ram_sector, the surfaced field where m_BloomVariationIndex != -1), surfaced
here with its bloom/active_seq so the consumer can pin it with proof. Harvester
clustering is retained ONLY as a last-ditch hint for when no RAM scan is available
(the world position is not persisted in the DB; the field actor is RAM-only). We
sample dune.actors (players + vehicles only, base structures excluded), reject
parked-at-base outliers, and convert the cluster centroid to a sector with the
authoritative map calibration from LogDuneWorldPartitioner.

Calibration (authoritative, 2026-06-03):
  Deep Desert world bounds Min=(-1270000,-1270000) Max=(1168400,1168400).
  Player survey grid is 9x9: rows A..I run SOUTH->NORTH (A=south, I=north;
  +y=south), columns 1..9 run WEST->EAST (1=west=min x). Sector size 270933.33 UU.

Deployed to lastsietch-dune:/root/dune-spice-active.py, invoked read-only via the
relay dispatcher action `spice-active`. No DB writes, no game impact.
"""
import json
import subprocess
import sys
from statistics import median

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DBPOD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"

# Authoritative live-position cache written by dune-spice-ramcache.py (the
# event-driven RAM reader). Per dim: {field_id, sector, x, y, scanned_utc, ...}.
# We surface it raw; the consumer pins it only when ram_field_id == the live
# field_id (i.e. the scan still describes the currently-active field).
RAMCACHE = "/root/dune-spice-ramcache.json"

MAP_MIN = -1270000.0
MAP_MAX = 1168400.0
SECTOR = (MAP_MAX - MAP_MIN) / 9.0          # 270933.33 UU per sector
OUTLIER_RADIUS = 450000.0                   # ~1.7 sectors from the cluster median

# dimension_index -> (label, Large spicefield_type_id for DeepDesert)
DIMS = {0: ("PvE", 6), 1: ("PvP", 12)}


_DB_CREDS: tuple[str, str] | None = None


def _db_creds() -> tuple[str, str]:
    """(password, port) read once from the db pod env. The DB service port differs
    by box (15433 on the old box, 15432 on the EPYC) -> read it from the pod rather
    than hardcode, so the same script works before AND after the hardware cutover.
    Falls back to 15433 (the historical default) if the env var is absent."""
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
    """Run read-only SQL in the live dune DB pod, tuples-only unaligned."""
    pgpass, pgport = _db_creds()
    out = subprocess.run(
        ["kubectl", "exec", "-n", NS, DBPOD, "--",
         "env", f"PGPASSWORD={pgpass}", "psql", "-h", "localhost", "-p", pgport,
         "-U", "postgres", "-d", "dune", "-tAc", sql],
        capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "psql failed").strip()[:300])
    return out.stdout.strip()


def load_ramcache() -> dict:
    """Best-effort read of the RAM-reader cache. {} if absent/unreadable so the
    producer degrades to the survey/cluster path."""
    try:
        with open(RAMCACHE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def sector_for(x: float, y: float) -> str:
    col = int((x - MAP_MIN) / SECTOR) + 1
    col = max(1, min(9, col))
    row_idx = int((MAP_MAX - y) / SECTOR)    # 0=A (south/+y) .. 8=I (north/-y)
    row_idx = max(0, min(8, row_idx))
    return f"{chr(ord('A') + row_idx)}{col}"


def cluster(players: list[tuple[float, float]],
            vehicles: list[tuple[float, float]]) -> dict:
    """Locate the harvest knot from DISTINCT harvesters. `players` is one point
    per account (deduped upstream); `vehicles` is one point per rig. Median-anchor
    outlier rejection drops parked-at-base rigs and lone wanderers.

    A real Large harvest has rigs on site (you haul spice by ornithopter/buggy),
    so we only assert a sector when the surviving cluster carries genuine
    harvesting signal: a vehicle present, or >=3 distinct entities. Two players
    standing somewhere with no rig is NOT trusted (that produced a bogus southern
    read while the real Large sat unmanned in the north)."""
    pts = players + vehicles
    if not pts:
        return {"sector": None, "confidence": "none",
                "distinct_players": 0, "vehicles": 0, "centroid": None}
    mx, my = median(p[0] for p in pts), median(p[1] for p in pts)
    near = lambda p: ((p[0] - mx) ** 2 + (p[1] - my) ** 2) ** 0.5 <= OUTLIER_RADIUS
    kp = [p for p in players if near(p)]
    kv = [v for v in vehicles if near(v)]
    kept = kp + kv or pts
    cx = sum(p[0] for p in kept) / len(kept)
    cy = sum(p[1] for p in kept) / len(kept)

    distinct = len(kp) + len(kv)
    has_rig = len(kv) >= 1
    if distinct >= 3 and has_rig:
        conf = "high"
    elif (distinct >= 2 and has_rig) or distinct >= 4:
        conf = "medium"
    else:
        conf = "low"   # e.g. lone players, no rig: not trusted as the Large
    sector = sector_for(cx, cy)
    trusted = conf in ("high", "medium")
    # Legal-band guard: the baked spice heatmaps (RE 2026-06-07, validated against
    # ground-truth sites F1/I3/H5) put the Large only in rows D-I; A/B/C are the
    # Shield Wall + southern buffer. A cluster computing to A/B/C is players staging
    # near the wall, never the Large, so never assert it.
    if sector and sector[0] in ("A", "B", "C"):
        trusted = False
    return {"sector": sector if trusted else None,
            "confidence": conf, "distinct_players": len(kp), "vehicles": len(kv),
            "centroid": [round(cx, 1), round(cy, 1)]}


def main() -> int:
    # Authoritative liveness: is a Large active per dimension?
    live_sql = (
        "SELECT json_agg(json_build_object("
        "'dim', dimension_index, 'active', current_globally_active)) "
        "FROM dune.spicefield_types "
        "WHERE map_name='DeepDesert' AND field_type='Large';")
    live = {int(r["dim"]): int(r["active"]) for r in (json.loads(_psql(live_sql)) or [])}

    # Authoritative field identity per dim. The Large is the highest-value Spice
    # field row (Large=2,500,000 remaining vs Medium=150,000 / Small=5,000). Its
    # field_id is a stable 64-bit id that CHANGES when the field rotates (each
    # Coriolis roll), so the portal uses it to detect rotation and key its survey
    # cache. RE 2026-06-07: the world position is NOT persisted anywhere -- field_id
    # is a non-invertible identity hash -- so the exact sector still comes from a
    # survey, but field_id tells us authoritatively WHEN the survey went stale.
    fid_sql = (
        "SELECT json_agg(json_build_object('dim', dimension_index, "
        "'fid', field_id::text, 'val', value_remaining)) "
        "FROM dune.resourcefield_state "
        "WHERE map='DeepDesert' AND field_kind_id=1;")
    large_fid: dict[int, tuple[str, int]] = {}
    for r in (json.loads(_psql(fid_sql)) or []):
        d, v = int(r["dim"]), r["val"]
        if v is None:
            continue
        fid = str(r["fid"])
        prev = large_fid.get(d)
        # highest value per dim = Large; on a tie (coexisting Larges all read
        # 2,500,000) pick the smaller field_id deterministically -- this MUST match
        # dune-spice-ramcache.live_field_ids() so the cache's field_id and this
        # live field_id agree and the RAM pin stays 'live' instead of flickering.
        if prev is None or int(v) > prev[1] or (int(v) == prev[1] and fid < prev[0]):
            large_fid[d] = (fid, int(v))

    # Live harvester positions: players + vehicles only (no base structures),
    # tagged by kind + owner so a single player (PlayerState + Controller +
    # Character rows all at one spot) counts ONCE, and rigs stay distinct.
    pos_sql = (
        "SELECT json_agg(json_build_object('dim', dimension_index, "
        "'kind', CASE WHEN class LIKE '%DunePlayer%' THEN 'player' ELSE 'vehicle' END, "
        "'owner', owner_account_id, "
        "'x', split_part(split_part(transform::text,'(\"(',2),',',1)::float8, "
        "'y', split_part(split_part(transform::text,'(\"(',2),',',2)::float8)) "
        "FROM dune.actors "
        "WHERE map='DeepDesert' AND transform IS NOT NULL "
        "AND (class LIKE '%DunePlayer%' OR class LIKE '%/Vehicles/%');")
    rows = json.loads(_psql(pos_sql)) or []

    # Per dim: dedupe players by owner_account_id (one point each); rigs kept all.
    players: dict[int, dict] = {0: {}, 1: {}}
    vehicles: dict[int, list] = {0: [], 1: []}
    for r in rows:
        d = int(r["dim"])
        if d not in players or r["x"] is None or r["y"] is None:
            continue
        pt = (float(r["x"]), float(r["y"]))
        if r["kind"] == "player":
            players[d][r.get("owner")] = pt   # last write per owner; pos is stable
        else:
            vehicles[d].append(pt)

    ramcache = load_ramcache()
    # Per-cycle accumulated candidate sectors (the full 2-5 set built up over the
    # cycle as the blow rotates; the per-dim cache entry only carries the ~3
    # resident at the last scan). Falls back to the instantaneous list on a legacy
    # cache that predates the accumulator.
    cand_acc = (ramcache.get("meta") or {}).get("candidates_acc") or {}
    # Parallel exact-coord accumulator (Part B): {dim: {sector: [x, y]}}. Empty on a
    # legacy cache that predates coord capture -> the portal falls back to sector
    # center. Mediums are read per-dim straight off the cache entry (full set).
    cand_acc_xy = (ramcache.get("meta") or {}).get("candidates_acc_xy") or {}

    dims_out = {}
    for d, (label, _type) in DIMS.items():
        # current_globally_active is a COUNT: normally 1, but a Spice Harvest event
        # raises max_globally_active so 2-3 Larges erupt at once. Keep the bool for
        # back-compat and surface the integer count alongside.
        active_count = int(live.get(d, 0))
        active = bool(active_count)
        c = cluster(list(players[d].values()), vehicles[d])
        # Authoritative live position from the RAM reader (when its scan still
        # matches the active field_id). Surfaced raw; build_grid pins it.
        rc = ramcache.get(str(d)) or {}
        # The RAM bloom discriminator is the source of truth for the sector. The
        # harvester cluster (c) is now only a last-ditch hint for when no RAM scan
        # is available; build_grid ignores it for pinning when ram_sector is fresh.
        # A cache entry with active=False means the scan ran and found NO surfaced
        # Large (between rotations) -> suppress its (None) sector so we don't pin
        # the cluster guess over a known-awaiting state.
        ram_active = rc.get("active", True)        # legacy entries lack the flag -> assume active
        live_large_fid = large_fid.get(d, (None,))[0] if active else None
        # When the RAM scan still describes the currently-active field (the same
        # field_id condition build_grid pins on) AND found a surfaced Large, RAM is
        # the ground truth for the active sector. The harvester cluster must NOT
        # compete as the active Large then -- the players may be working a *Medium*
        # field (live 2026-06-10: cluster said G7 'high' while RAM ground truth was
        # F1, a Medium worksite). Demote it to a separate 'worksite' + confidence low.
        ram_fresh = bool(active and ram_active and rc.get("sector")
                         and rc.get("field_id") and rc.get("field_id") == live_large_fid)
        if not active:
            heur_sector, heur_conf, worksite = None, "none", None
        elif ram_fresh:
            heur_sector, heur_conf = None, "low"
            worksite = {
                "sector": c["sector"], "confidence": c["confidence"],
                "centroid": c["centroid"],
                "distinct_players": c["distinct_players"], "vehicles": c["vehicles"],
            } if c["sector"] else None
        else:
            heur_sector, heur_conf, worksite = c["sector"], c["confidence"], None
        dims_out[str(d)] = {
            "label": label,
            "large_active": active,
            "large_active_count": active_count,
            # Every surfaced Large this scan (Spice Harvest = 2-3 at once). The cache
            # writer carries each as {sector,x,y,bloom,active_seq}; empty on a legacy
            # cache, so the consumer falls back to the single ram_sector pin.
            "ram_active_fields": rc.get("actives") or [],
            "large_field_id": live_large_fid,
            "sector": heur_sector,
            "confidence": heur_conf,
            "worksite": worksite,
            "distinct_players": c["distinct_players"],
            "vehicles": c["vehicles"],
            "centroid": c["centroid"],
            "ram_sector": rc.get("sector") if ram_active else None,
            "ram_field_id": rc.get("field_id"),
            "ram_bloom": rc.get("bloom"),
            "ram_active_seq": rc.get("active_seq"),
            "ram_active": ram_active,
            "ram_x": rc.get("x"),
            "ram_y": rc.get("y"),
            # Full per-cycle Large candidate SITES (RAM-authoritative; the diamonds
            # the map draws). Accumulated over the cycle as the blow rotates, so it
            # reflects the true count (2-5) rather than the ~3 resident at one scan.
            # Falls back to the last scan's instantaneous list on a legacy cache.
            "ram_candidates": sorted((cand_acc.get(str(d)) or {}).keys()) or (rc.get("candidates") or []),
            # Exact per-candidate coords (Part B): one {sector,x,y} per site,
            # accumulated over the cycle (so a rotated-away site keeps its coord).
            # Falls back to the last scan's instantaneous list on a legacy cache.
            "ram_candidates_xy": (
                [{"sector": s, "x": xy[0], "y": xy[1]}
                 for s, xy in sorted((cand_acc_xy.get(str(d)) or {}).items())]
                or (rc.get("candidates_xy") or [])),
            # Full Medium-field layer (Part A): the complete per-cycle set with exact
            # coords (mediums do not rotate, so one read is the whole layer).
            "ram_mediums": rc.get("mediums") or [],
            "ram_scanned_utc": rc.get("scanned_utc"),
            "ram_vptr_source": rc.get("vptr_source"),
        }

    print(json.dumps({"dimensions": dims_out}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # degrade to a parseable error, never a stack trace
        print(json.dumps({"error": str(exc)[:300], "dimensions": {}}))
        sys.exit(1)
