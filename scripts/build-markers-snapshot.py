#!/usr/bin/env python3
"""Build the compact portal map-marker snapshots, enriching named POIs with names.

Input  : a marker export (scripts/dune-markers-export.py output) carrying the raw
         DisplayName payload per marker, AND the existing curated snapshots.
Output : the same curated snapshots with a friendly name `n` attached to every
         POI that has a resolvable DisplayName (Testing Stations, Caves, Sietches,
         Fortresses, Trading Posts, Enemy camps/outposts, Exploration POIs, House
         representatives, Trainers). Resource/scrap nodes stay {t,x,y} so the
         ~17k-marker Hagga payload stays lean.

This ENRICHES the existing snapshot marker set in place (matched by type + rounded
world coord); it does not re-derive which markers belong on the map, so the curated
set and its counts are unchanged -- only names are added.

Name parse is build-time only: the runtime just reads `n` from the snapshot. v1 is
number-only ("Imperial Testing Station 2"); the hazard variant "(Fire/Radiation)"
is a later the web host follow-up (the export already captured the tags column).

  python3 build-markers-snapshot.py \
      --export ../our internal design notes \
      --data   ../admin-backend/data
"""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# snapshot file -> map_name_id (matches admin-backend/map_model.MAPS)
SNAPSHOTS = {"dd-markers-snapshot.json": 7, "hagga-markers-snapshot.json": 11}

# short alias -> snapshot filename (for --maps scoping)
_MAP_ALIASES = {"dd": "dd-markers-snapshot.json", "deep-desert": "dd-markers-snapshot.json",
                "hagga": "hagga-markers-snapshot.json"}

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|_")
# the second argument of LOCTABLE("namespace", "key")
_LOCTABLE_KEY = re.compile(r'LOCTABLE\(\s*"[^"]*"\s*,\s*"([^"]+)"\s*\)')


def _name_token(key: str) -> str | None:
    """Reduce a LOCTABLE key to the bare name token, dropping namespace + region.

    Handles the forms seen in dune.markers payloads (2026-06-11 export):
      NPCS_AND_WORLD/WORLD_MAP_LOCATION_Survival_<Region>_<Name>   (most POIs)
      NPCS_AND_WORLD/WORLD_MAP_MARKER_LOCATION_DEEP_DESERT_..._<NAME>_<NN>
      NPCS_AND_WORLD/WORLD_MAP_MARKER_<Name>                        (Sietch_Tarl)
      NPCS_AND_WORLD/NPC_NAMED_<NAME>_DISPLAYNAME                   (Trainers)
      UI/EnemyCampDisplayName/<Name>
      UI/UI/Map_Legend_Entry_HouseRepresentative_<House>
    Returns None when nothing usable is left (caller falls back to the type label)."""
    seg = key.split("/")[-1]
    # House representative legend entry: ..._HouseRepresentative_<House>
    m = re.search(r"HouseRepresentative_(.+)$", seg)
    if m:
        return "House " + m.group(1)
    # named NPC trainer: NPC_NAMED_<NAME>_DISPLAYNAME
    if seg.upper().startswith("NPC_NAMED_"):
        return re.sub(r"_DISPLAYNAME$", "", seg[len("NPC_NAMED_"):], flags=re.I).title()
    # primary form: name is the token after Survival_<Region>_ (camelCase name)
    m = re.search(r"WORLD_MAP_LOCATION_Survival_[A-Za-z0-9]+_(.+)$", seg)
    if m:
        return m.group(1)
    # upper/mixed marker-location form ending in <Word>_<digits> (e.g. ECOLAB_013,
    # ShipWreck_03) -> keep just that trailing name, dropping the region prefix.
    m = re.search(r"([A-Za-z]+)_(\d+)$", seg)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    # plain marker form: drop a known namespace prefix, keep the rest.
    for p in ("WORLD_MAP_MARKER_LOCATION_", "WORLD_MAP_MARKER_", "WORLD_MAP_LOCATION_",
              "Map_Legend_Entry_"):
        if seg.upper().startswith(p.upper()):
            return seg[len(p):] or None
    # UI EnemyCamp / fallthrough: last segment as-is
    return seg or None


def friendly_name(dn: str | None) -> str | None:
    """Raw DisplayName payload string -> a clean human name, or None."""
    if not dn:
        return None
    m = _LOCTABLE_KEY.search(dn)
    key = m.group(1) if m else dn
    tok = _name_token(key)
    if not tok:
        return None
    # camelCase / snake / digit-boundary split, collapse whitespace, tidy casing.
    name = _CAMEL.sub(" ", tok).strip()
    name = re.sub(r"\s+", " ", name)
    # UPPER_SNAKE leftovers (e.g. "ECOLAB 013") -> Title Case; keep mixed case as-is.
    if name and name.upper() == name:
        name = " ".join(w.capitalize() if not w.isdigit() else w for w in name.split())
    # Strip leading region/namespace phrases that slip through the generic DD
    # marker form ("Deep Desert Shield Wall Ecolab 013" -> "Ecolab 013").
    name = re.sub(r"^(Survival|Deep Desert(?: Shield Wall)?)\s+", "", name, flags=re.I).strip()
    return name or None


def build_name_index(export_rows: list) -> dict:
    """{(map_id, marker_type, round_x, round_y): friendly_name}."""
    idx = {}
    for r in export_rows:
        n = friendly_name(r.get("dn"))
        if not n:
            continue
        try:
            key = (int(r["map"]), r["t"], int(round(float(r["x"]))), int(round(float(r["y"]))))
        except (KeyError, TypeError, ValueError):
            continue
        idx[key] = n
    return idx


def enrich_snapshot(path: Path, map_id: int, name_idx: dict) -> tuple[int, int]:
    snap = json.loads(path.read_text(encoding="utf-8"))
    markers = snap.get("markers") or []
    named = 0
    for mk in markers:
        t, x, y = mk.get("t"), mk.get("x"), mk.get("y")
        if t is None or x is None or y is None:
            continue
        n = name_idx.get((map_id, t, int(round(float(x))), int(round(float(y)))))
        if n:
            mk["n"] = n
            named += 1
        elif "n" in mk:
            del mk["n"]            # idempotent: drop a stale name if no longer resolved
    path.write_text(json.dumps(snap, separators=(",", ":")), encoding="utf-8")
    return len(markers), named


def rebuild_snapshot(path: Path, map_id: int, export_rows: list, name_idx: dict) -> tuple[int, int]:
    """Rebuild a snapshot's ENTIRE marker set from the live export (not an enrich):
    the snapshot becomes exactly the current dune.markers layer for this map, so
    markers that moved or vanished in a Coriolis re-roll are dropped and freshly
    spawned ones are added. Resource/scrap nodes stay {t,x,y}; named POIs (Caves,
    Testing Stations, ...) carry the friendly name `n`. Runtime-excluded types
    (SurveyPoint/NoIcon) stay in the file -- map_model.build_data drops them at
    render -- so the snapshot remains a faithful copy of the raw layer."""
    markers = []
    named = 0
    for r in export_rows:
        try:
            if int(r["map"]) != map_id:
                continue
            t = r["t"]
            x = int(round(float(r["x"])))
            y = int(round(float(r["y"])))
        except (KeyError, TypeError, ValueError):
            continue
        if t is None:
            continue
        mk = {"t": t, "x": x, "y": y}
        n = name_idx.get((map_id, t, x, y))
        if n:
            mk["n"] = n
            named += 1
        markers.append(mk)
    snap = {
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "markers": markers,
    }
    path.write_text(json.dumps(snap, separators=(",", ":")), encoding="utf-8")
    return len(markers), named


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--export", default=str(here.parent / "our internal design notes"))
    ap.add_argument("--data", default=str(here.parent / "admin-backend/data"))
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the FULL marker set from the export (drops stale/moved "
                         "markers, adds new ones) instead of only enriching names")
    ap.add_argument("--maps", default="",
                    help="comma list to scope (dd / hagga / a snapshot filename); default = all")
    args = ap.parse_args()

    export_rows = json.loads(Path(args.export).read_text(encoding="utf-8"))
    name_idx = build_name_index(export_rows)
    print(f"resolved {len(name_idx)} named POIs from {len(export_rows)} export rows")

    targets = SNAPSHOTS
    if args.maps.strip():
        want = {_MAP_ALIASES.get(tok.strip(), tok.strip()) for tok in args.maps.split(",")}
        targets = {f: m for f, m in SNAPSHOTS.items() if f in want}
        if not targets:
            print(f"  no matching snapshots for --maps {args.maps!r}")
            return 2

    data_dir = Path(args.data)
    for fname, map_id in targets.items():
        path = data_dir / fname
        if args.rebuild:
            total, named = rebuild_snapshot(path, map_id, export_rows, name_idx)
            print(f"  {fname}: REBUILT {total} markers ({named} named)")
            continue
        if not path.exists():
            print(f"  skip {fname} (missing)")
            continue
        total, named = enrich_snapshot(path, map_id, name_idx)
        print(f"  {fname}: {named}/{total} markers named")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
