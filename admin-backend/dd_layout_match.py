"""Runtime Deep Desert layout matcher (M4).

The War-Table's real relief is one of 12 fixed Coriolis layouts; the offline
bake (dev-A) emits a fingerprint per layout in `fingerprints.json` (spice +
shipwreck feature sectors and cluster centroids in normalized 0..1000 coords).
This module scores the LIVE Deep Desert feature snapshot (already assembled in
the /portal/maps/deep-desert/data route) against those 12 signatures and returns
the best-match layout id + a confidence margin, so the client can fetch that one
baked heightfield.

Design mirrors spice_candidates_acc.py: keyed by the Coriolis `cycle_key`, and it
never raises into the map feed (any failure -> None, client stays on the seeded
procedural relief). The last CONFIDENT match per cycle is persisted to a small
JSON sidecar so an ambiguous or sparse later read falls back to it rather than
flipping the map. It NEVER emits a confident-but-wrong id: below the margin
threshold the source is 'last-known' (if we have one this cycle) or 'fallback'.

Frozen inputs (dev-A contract):
  fingerprints.json = [{id, features:[{cls, sector, nx, ny}],
                        centroids:{spice:[[nx,ny]...], shipwreck:[[nx,ny]...]}}]
Payload output (contract 5.4): {id:int, confidence:float, source:str}.
"""
import json
import logging
import math
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
# dev-A bakes into static/terrain/; the SvelteKit static adapter copies it into
# build/terrain/, and ONLY build/ is rsynced to <web-host>. Read from build/ so
# the deployed backend finds the artifacts; fall back to static/ for local/dev.
_TERRAIN_DIR = _HERE / "portal-nextgen" / "build" / "terrain"
if not (_TERRAIN_DIR / "fingerprints.json").exists():
    _TERRAIN_DIR = _HERE / "portal-nextgen" / "static" / "terrain"
_FINGERPRINTS = _TERRAIN_DIR / "fingerprints.json"
# Last-known match per Coriolis cycle (our own sidecar; mirrors the acc pattern).
_RUNTIME = Path(os.environ.get("LASTSIETCH_PORTAL_RUNTIME_DIR", str(_HERE / "data")))
_LASTKNOWN = _RUNTIME / "dd_layout_lastknown.json"

VIEW = 1000.0

# Scoring weights + guards. Tunable; conservative defaults that refuse to commit
# on a thin or ambiguous survey.
_JACCARD_W = 0.6            # occupied-sector set overlap
_CENTROID_W = 0.4          # nearest-centroid proximity
_CENT_SCALE = 300.0        # board-units at which centroid similarity -> 0
_MIN_FEATURES = 3          # fewer live features than this = too sparse to trust
_MARGIN_MIN = 0.06         # best-over-runner-up margin needed to commit


def _sector_from_norm(nx: float, ny: float) -> str:
    """9x9 DD sector for a normalized (nx, ny), matching map_model.sector_for.

    Derivation: sector_for uses col=(x-originX)/(span/9)+1 and
    row=(maxY-y)/(span/9); in normalized space (spanX==spanY for DD) these reduce
    to the linear maps below, so a projected marker lands in the same sector as
    the offline fingerprint generator produced."""
    col = int(nx * 9.0 / VIEW) + 1
    col = 1 if col < 1 else 9 if col > 9 else col
    row = int(9.0 - ny * 9.0 / VIEW)
    row = 0 if row < 0 else 8 if row > 8 else row
    return f"{chr(ord('A') + row)}{col}"


def live_features(data: dict, spice_coords: dict | None,
                  spice_mediums: list | None) -> list[dict]:
    """Build the live [{cls, sector, nx, ny}] feature set from the /data payload.

    Spice comes from the per-cycle candidate coords + the medium-field coords
    (both already projected to 0..1000). Shipwrecks come from the static DD
    marker layer (the 'Shipwreck' salvage type). Everything the assembler used as
    a distinctive fingerprint class; the fixed Eco/shield-wall meshes are excluded
    upstream (they never entered the marker layer as 'Shipwreck')."""
    feats: list[dict] = []
    # Spice: the per-cycle candidate LARGE sites only (sector -> [nx, ny]). The
    # signatures' "spice" class is the SM_SoS_Spice tile set = the Large sites
    # (4-6 per layout). Medium fields (~85/cycle) are NOT in the signatures;
    # folding them in swamped the Jaccard and centroid terms so no layout could
    # ever clear the margin (verified 2026-09-02 on the live payload: best 0.0099).
    for sec, xy in (spice_coords or {}).items():
        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
            feats.append({"cls": "spice", "sector": (sec or "").upper(),
                          "nx": float(xy[0]), "ny": float(xy[1])})
    # Shipwrecks: deliberately NOT taken from the static 'Shipwreck' salvage
    # markers. Those are the fixed row-A wrecks along the shield wall (identical
    # every cycle), while the signatures' shipwreck class is the per-layout
    # SM_SoS_Shipwreck tiles (F8/H8/I1 etc). Comparing the two scores 0 for every
    # layout and only drags the margin down. The signature centroids stay in the
    # file for a future live source (RAM/log) that can see the tiles.
    del spice_mediums, data
    return feats


def _load_fingerprints() -> list[dict]:
    try:
        return json.loads(_FINGERPRINTS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []                      # pre-bake: no signatures -> no layout field
    except Exception as exc:
        logger.warning("dd_layout_match: fingerprints load failed: %s", exc)
        return []


def _occupied(feats: list[dict]) -> set[tuple[str, str]]:
    return {(f.get("cls", ""), (f.get("sector") or "").upper())
            for f in feats if f.get("sector")}


def _nearest_centroid_sim(feats: list[dict], centroids: dict) -> float:
    """Mean similarity of each live feature to the nearest same-class centroid.

    similarity = max(0, 1 - dist/_CENT_SCALE) in board units; 0..1. Classes with
    no signature centroids contribute 0 (a feature that shouldn't be there)."""
    if not feats:
        return 0.0
    total = 0.0
    for f in feats:
        pts = centroids.get(f.get("cls", "")) or []
        if not pts:
            continue
        best = min(math.hypot(f["nx"] - p[0], f["ny"] - p[1])
                   for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2)
        total += max(0.0, 1.0 - best / _CENT_SCALE)
    return total / len(feats)


def _score(feats: list[dict], sig: dict) -> float:
    live_occ = _occupied(feats)
    sig_occ = _occupied(sig.get("features") or [])
    if live_occ or sig_occ:
        inter = len(live_occ & sig_occ)
        union = len(live_occ | sig_occ)
        jac = inter / union if union else 0.0
    else:
        jac = 0.0
    cent = _nearest_centroid_sim(feats, sig.get("centroids") or {})
    return _JACCARD_W * jac + _CENTROID_W * cent


def _read_lastknown() -> dict:
    try:
        return json.loads(_LASTKNOWN.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("dd_layout_match: lastknown read failed: %s", exc)
        return {}


def _write_lastknown(cycle_key: str, entry: dict) -> None:
    try:
        store = _read_lastknown()
        store[cycle_key] = entry
        _LASTKNOWN.parent.mkdir(parents=True, exist_ok=True)
        _LASTKNOWN.write_text(json.dumps(store), encoding="utf-8")
    except Exception as exc:
        logger.warning("dd_layout_match: lastknown write failed: %s", exc)


def match(data: dict, spice_coords: dict | None = None,
          spice_mediums: list | None = None) -> dict | None:
    """Identify the active DD layout from the live /data snapshot.

    Returns {id:int, confidence:float, source:'fingerprint'|'last-known'|'fallback'}
    or None when no signatures exist yet (pre-bake -> client stays seeded).
    Never raises: any internal error yields None."""
    try:
        sigs = _load_fingerprints()
        if not sigs:
            return None

        try:
            import spice_candidates_acc
            ck = spice_candidates_acc.cycle_key()
        except Exception:
            ck = "0"

        feats = live_features(data, spice_coords, spice_mediums)

        # Score every signature; sort by descending score.
        scored = sorted(
            ((_score(feats, s), s.get("id")) for s in sigs if s.get("id") is not None),
            key=lambda t: -t[0],
        )
        best_score, best_id = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_score - runner

        sparse = len(feats) < _MIN_FEATURES
        confident = (not sparse) and margin >= _MARGIN_MIN and best_id is not None

        if confident:
            entry = {"id": int(best_id), "margin": round(margin, 4)}
            _write_lastknown(ck, entry)
            return {"id": int(best_id), "confidence": round(margin, 4),
                    "source": "fingerprint"}

        # Ambiguous or sparse: prefer this cycle's last confident match.
        lk = _read_lastknown().get(ck)
        if lk and lk.get("id") is not None:
            return {"id": int(lk["id"]),
                    "confidence": round(float(lk.get("margin", 0.0)), 4),
                    "source": "last-known"}

        # Nothing trustworthy yet: signal fallback so the client stays seeded.
        return {"id": int(best_id) if best_id is not None else -1,
                "confidence": round(margin, 4), "source": "fallback"}
    except Exception as exc:
        logger.warning("dd_layout_match: match failed: %s", exc)
        return None


# --- Owner override + island backdrop (2026-09-02) ---------------------------------

_OVERRIDE = _RUNTIME / "dd_layout_override.json"


def override_for_cycle() -> dict | None:
    """Owner-set layout for the CURRENT Coriolis cycle only: {id, cycle_key, note}.
    A file from an earlier cycle is ignored, so a stale override can never pin the
    wrong map after a reset. Written by ops/dd-layout-override.sh."""
    try:
        if not _OVERRIDE.exists():
            return None
        raw = json.loads(_OVERRIDE.read_text(encoding="utf-8"))
        import spice_candidates_acc
        if str(raw.get("cycle_key")) != str(spice_candidates_acc.cycle_key()):
            return None
        lid = int(raw.get("id"))
        if not 0 <= lid <= 11:
            return None
        return {"id": lid, "confidence": 1.0, "source": "override"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("dd_layout_match: override read failed: %s", exc)
        return None


def backdrop_for(map_key: str, layout: dict | None) -> dict | None:
    """Image backdrop for a known layout, from the map's `backdrop_layouts` template.
    None when the map has no baked islands or the layout is unknown/fallback."""
    try:
        if not layout or layout.get("source") == "fallback":
            return None
        import map_model
        tpl = ((map_model.MAPS.get(map_key) or {}).get("backdrop_layouts") or {}).get("template")
        if not tpl:
            return None
        return {"type": "image", "src": tpl.format(id=int(layout["id"])), "layout": int(layout["id"])}
    except Exception as exc:  # noqa: BLE001
        logger.warning("dd_layout_match: backdrop_for failed: %s", exc)
        return None
