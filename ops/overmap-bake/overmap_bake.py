#!/usr/bin/env python3
"""Sand Table overland bake: the game's overmap relief, contours, art underlay and places for the browser.

Reads a DuneExtractor dump of the overmap (Overmap.umap + Overmap_Geometry.umap + the 3 Overmap/Sublevels,
their collision/isoline GLBs and T_UI_GlobalMap_D) and writes static/world-data/. See README.md.
"""
import argparse
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image, ImageDraw

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_OUT = os.path.join(REPO, "admin-backend", "portal-nextgen", "static", "world-data")
OVERMAP_DIR = "json/DuneSandbox/Content/Dune/Systems/Overmap"
MESH_DIR = "meshes/DuneSandbox/Content/Dune/Systems/Overmap/Meshes/OutputMeshes"
ART = "png/DuneSandbox/Content/Dune/GUI/Textures/Menus/Gameplay/Map/T_UI_GlobalMap_D.png"
LEVELS = ["Overmap.json", "Overmap_Geometry.json", "Sublevels/Overmap_ArrakeenSpacePort.json",
          "Sublevels/Overmap_OldQuarry.json", "Sublevels/Overmap_WindPass.json"]
SUBLEVELS = LEVELS[2:]

# BP_MapLayer_Global: 1 layer unit = 250 overmap cm; the 4096x2048 art spans a 2000x1000-unit plane centred at
# layer (3.016, -113.10). px = (X/250 + 996.984) * 2.048, py = (Y/250 + 613.10) * 2.048 on the 4096 art.
CM_PER_UNIT = 250.0
ART_OFFSET_UNITS = (996.984, 613.10)
ART_PLANE_UNITS = (2000.0, 1000.0)
ART_OUT_SIZE = (2048, 1024)

PLACES_COUNT = 76
ISOLINE_WIDTH_CM = 20.0          # ribbon width of SM_OvermapOutputIsolineMesh_*: rungs are 20 cm
CONTOUR_TOLERANCE_CM = 120.0     # Douglas-Peucker; about 0.5 px on the 4096 art
GLTFPACK_ARGS = ["-c"]

# Place -> MAPS key join (hand table, plan section 4.1). Everything not listed carries no key and no live data.
STONES = [
    {"key": "hagga", "name": "Hagga Basin", "poi": "Hagga Basin", "travelPrefix": "Survival1_to_Overland_",
     "travelLabelPrefix": "BP_TravelDestination_HaggaBasin_",
     "facets": [{"key": "habbanya", "name": "Habbanya", "rule": "PvE"},
                {"key": "amtal", "name": "Amtal", "rule": "Full PvP, safe at Riftwatch and Helius Gate"}]},
    {"key": "deep-desert", "name": "Deep Desert", "poi": "Deep Desert", "travelPrefix": "DeepDesert1_to_Overland_",
     "travelLabelPrefix": "BP_TravelDestination_DeepDesert_",
     "facets": [{"key": "pve", "name": "PvE", "rule": "PvE"}, {"key": "pvp", "name": "PvP", "rule": "PvP"}]},
    {"key": "arrakeen", "name": "Arrakeen", "poi": "Arrakeen", "travelPrefix": "Arrakeen_to_Overland",
     "travelLabelPrefix": "BP_TravelDestination_Arrakeen", "facets": []},
    {"key": "harko-village", "name": "Harko Village", "poi": "Harko", "travelPrefix": "Harko_to_Overland",
     "travelLabelPrefix": "BP_TravelDestination_HarkoVillage", "facets": []},
]

CATEGORY_KIND = {
    "Poi.Location.Regions": "region", "Poi.Location.Cities": "city", "Poi.Dungeons": "dungeon",
    "Poi.StoryRelatedMaps": "story", "Poi.PointsOfInterest": "poi",
}

# The 23 overland locations (owner decision 2026-10-06): one per distinct POI name across Overmap.umap and the 3
# sublevels. Arsunt Garrison has two POI actors 352 cm apart (Atreides and Harkonnen faction outposts) and is one place.
LOCATION_KIND = {
    "Poi.Location.Regions": "region", "Poi.Location.Cities": "settlement", "Poi.StoryRelatedMaps": "story",
    "Poi.PointsOfInterest": "landmark", "Poi.Dungeons": "dungeon",
}
LOCATION_KIND_COUNTS = {"region": 2, "settlement": 2, "story": 6, "landmark": 7, "dungeon": 6}
# The 3 Overmap/Sublevels are world-composition tiles of Overmap.umap. The raw DuneWorldComposition_1 export lists 7
# tiles (Audio, Geometry, Light, VFX, ArrakeenSpacePort, OldQuarry, WindPass), every FWorldTileInfo.Position is
# (0,0,0), and there are no LevelStreaming objects. LevelInstance_0 ("Overmap", yaw -90) is an editor instance
# embedded at cook time with no WorldAsset. So the sublevel transform is identity (verified against the owner's
# in-game screenshots 2026-10-06, see README).
SUBLEVEL_TRANSFORM = {"source": "DuneWorldComposition_1 FWorldTileInfo.Position", "translationCm": [0, 0, 0],
                      "yawDeg": 0}


def fail(msg):
    raise SystemExit(f"overmap_bake: {msg}")


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ---------------------------------------------------------------- GLB io

def read_glb(path):
    with open(path, "rb") as fh:
        data = fh.read()
    magic, version, length = struct.unpack("<III", data[:12])
    if magic != 0x46546C67 or version != 2 or length != len(data):
        fail(f"{path}: bad GLB header (length {length} vs file {len(data)})")
    off, chunks = 12, []
    while off < len(data):
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        chunks.append((ctype, data[off + 8:off + 8 + clen]))
        off += 8 + clen
    gl = json.loads(chunks[0][1])
    binc = chunks[1][1] if len(chunks) > 1 else b""
    return gl, binc


def glb_buffer_check(path):
    """GLB integrity: the embedded buffer must exactly fill the BIN chunk (a truncated copy fails this)."""
    gl, binc = read_glb(path)
    embedded = [b for b in gl.get("buffers", []) if "uri" not in b
                and not b.get("extensions", {}).get("EXT_meshopt_compression", {}).get("fallback")]
    total = sum(b["byteLength"] for b in embedded)
    if total != len(binc):
        fail(f"{path}: buffer byteLength {total} != BIN chunk {len(binc)}")
    return gl, binc


def accessor(gl, binc, idx):
    a = gl["accessors"][idx]
    bv = gl["bufferViews"][a["bufferView"]]
    ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
    dt = np.dtype({5126: "<f4", 5125: "<u4", 5123: "<u2", 5121: "u1"}[a["componentType"]])
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride", dt.itemsize * ncomp)
    arr = np.ndarray((a["count"], ncomp), dt, binc, off, (stride, dt.itemsize))
    return arr.copy() if ncomp > 1 else arr[:, 0].copy()


def read_mesh(path):
    """All triangle primitives of a GLB as (positions in UE local cm, triangles). glTF (x, y, z) m -> UE (x, z, y) cm."""
    gl, binc = glb_buffer_check(path)
    pts, tris, base = [], [], 0
    for mesh in gl["meshes"]:
        for prim in mesh["primitives"]:
            if prim.get("mode", 4) != 4:
                fail(f"{path}: non-triangle primitive")
            p = accessor(gl, binc, prim["attributes"]["POSITION"]).astype(np.float64)
            t = accessor(gl, binc, prim["indices"]).astype(np.int64).reshape(-1, 3)
            pts.append(np.stack([p[:, 0], p[:, 2], p[:, 1]], 1) * 100.0)
            tris.append(t + base)
            base += len(p)
    return np.concatenate(pts), np.concatenate(tris)


def write_glb(path, positions, triangles, extras):
    pos = positions.astype("<f4")
    idx = triangles.astype("<u4").ravel()
    pos_bytes, idx_bytes = pos.tobytes(), idx.tobytes()
    binc = pos_bytes + idx_bytes
    binc += b"\0" * (-len(binc) % 4)
    gl = {
        "asset": {"version": "2.0", "generator": "LastSietch ops/overmap-bake", "extras": extras},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"name": "overmap-relief", "mesh": 0}],
        "meshes": [{"name": "overmap-relief", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(binc)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes), "target": 34962},
                        {"buffer": 0, "byteOffset": len(pos_bytes), "byteLength": len(idx_bytes), "target": 34963}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3",
                       "min": pos.min(0).tolist(), "max": pos.max(0).tolist()},
                      {"bufferView": 1, "componentType": 5125, "count": len(idx), "type": "SCALAR"}],
    }
    js = json.dumps(gl, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(binc)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        fh.write(struct.pack("<II", len(binc), 0x004E4942) + binc)


# ---------------------------------------------------------------- level placements

def rot_matrix(r):
    """UE FRotator (degrees) -> 3x3 rotation matrix acting on column vectors."""
    p, y, ro = (math.radians((r or {}).get(k, 0.0)) for k in ("Pitch", "Yaw", "Roll"))
    cp, sp, cy, sy, cr, sr = math.cos(p), math.sin(p), math.cos(y), math.sin(y), math.cos(ro), math.sin(ro)
    return np.array([[cp * cy, cy * sp * sr - sy * cr, -(cy * sp * cr + sy * sr)],
                     [cp * sy, sy * sp * sr + cy * cr, cy * sr - sy * sp * cr],
                     [sp, -cp * sr, cp * cr]])


def asset_name(ref):
    name = (ref or {}).get("ObjectName", "")
    return name.split("'")[1] if "'" in name else name


def load_level(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def by_object_name(level):
    """Index exports by the path that ObjectName references use, e.g. 'Overmap:PersistentLevel.X.Root'."""
    out = {}
    for e in level:
        outer = (e.get("Outer") or {}).get("ObjectName", "")
        if "'" in outer:
            out[outer.split("'")[1] + "." + e["Name"]] = e
    return out


def component_transform(comp, index, depth=0):
    """World transform of a scene component as (scale, rotation, location), following AttachParent."""
    p = comp.get("Properties") or {}
    own = asset_name(comp.get("Outer")) + "." + comp.get("Name", "")
    s = p.get("RelativeScale3D") or {}
    scale = np.array([s.get("X", 1.0), s.get("Y", 1.0), s.get("Z", 1.0)])
    rot = rot_matrix(p.get("RelativeRotation"))
    loc = np.array([(p.get("RelativeLocation") or {}).get(k, 0.0) for k in ("X", "Y", "Z")])
    parent = p.get("AttachParent")
    if parent and asset_name(parent) != own:
        if depth > 8:
            fail(f"attach chain too deep at {own}")
        key = asset_name(parent)
        if key not in index:
            fail(f"attach parent {key} not in level")
        ps, pr, pl = component_transform(index[key], index, depth + 1)
        loc = pl + pr @ (ps * loc)
        rot = pr @ rot
        scale = ps * scale
    return scale, rot, loc


def placed_meshes(extract):
    """Every StaticMeshComponent in the overmap levels, with its world transform."""
    out = []
    for rel in LEVELS:
        level = load_level(os.path.join(extract, OVERMAP_DIR, rel))
        index = by_object_name(level)
        for e in level:
            if e.get("Type") != "StaticMeshComponent":
                continue
            p = e.get("Properties") or {}
            mesh = asset_name(p.get("StaticMesh"))
            if not mesh:
                continue
            scale, rot, loc = component_transform(e, index)
            out.append({"level": rel, "mesh": mesh, "scale": scale, "rot": rot, "loc": loc})
    return out


# ---------------------------------------------------------------- relief

def assemble_relief(extract, placed):
    tiles = [p for p in placed if "OvermapOutputCollisionMesh" in p["mesh"]]
    if not tiles:
        fail("no placed collision tiles")
    allp, allt, base, per_level, info = [], [], 0, {}, []
    for t in tiles:
        path = os.path.join(extract, MESH_DIR, t["mesh"] + ".glb")
        if not os.path.isfile(path):
            fail(f"placed mesh missing from extraction: {t['mesh']}")
        p, tri = read_mesh(path)
        w = (p * t["scale"]) @ t["rot"].T + t["loc"]
        if np.prod(np.sign(t["scale"])) < 0:
            tri = tri[:, [0, 2, 1]]
        # weld exact duplicates inside the tile (split normals/uvs in the source)
        key, inv = np.unique(np.round(w, 2), axis=0, return_inverse=True)
        tri = inv.ravel()[tri]
        tri = tri[(tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])]
        allp.append(key)
        allt.append(tri + base)
        base += len(key)
        per_level.setdefault(t["level"], []).append(t["mesh"])
        info.append({"mesh": t["mesh"], "level": t["level"], "verts": len(key), "tris": len(tri),
                     "x": [round(key[:, 0].min()), round(key[:, 0].max())],
                     "y": [round(key[:, 1].min()), round(key[:, 1].max())]})
    return np.concatenate(allp), np.concatenate(allt), per_level, info


def unplaced_tiles(extract, placed):
    names = {p["mesh"] for p in placed}
    folder = os.path.join(extract, MESH_DIR)
    return sorted(f[:-4] for f in os.listdir(folder)
                  if f.startswith("SM_OvermapOutputCollisionMesh") and f[:-4] not in names)


def pack_relief(raw_glb, out_glb, gltfpack):
    cmd = [gltfpack, "-i", raw_glb, "-o", out_glb] + GLTFPACK_ARGS
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        fail(f"gltfpack failed: {res.stderr.strip()}")
    ver = subprocess.run([gltfpack, "-h"], capture_output=True, text=True)
    return (ver.stdout + ver.stderr).strip().splitlines()[0]


# ---------------------------------------------------------------- contours

def ribbon_centerlines(points, tris):
    """SM_OvermapOutputIsolineMesh_* are 20 cm ribbons: quads of two triangles whose short edge is a rung across
    the ribbon. The centreline joins rung midpoints. Returns polylines in overmap cm (x, y)."""
    xy = points[:, :2]
    partner = {}
    rung_of_tri = []
    for t in tris:
        e = [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])]
        lens = [np.hypot(*(xy[a] - xy[b])) for a, b in e]
        k = int(np.argmin(lens))
        a, b = e[k]
        if lens[k] > ISOLINE_WIDTH_CM * 1.5:
            continue
        partner[a], partner[b] = b, a
        rung_of_tri.append((a, b, t[(k + 2) % 3]))

    def node(a, b):
        m = (xy[a] + xy[b]) / 2
        return (round(m[0], 0), round(m[1], 0))

    adj = {}
    for a, b, c in rung_of_tri:
        if c not in partner:
            continue
        n1, n2 = node(a, b), node(c, partner[c])
        if n1 == n2:
            continue
        adj.setdefault(n1, set()).add(n2)
        adj.setdefault(n2, set()).add(n1)

    seen, lines = set(), []

    def walk(start, nxt):
        line = [start, nxt]
        seen.add(frozenset((start, nxt)))
        prev, cur = start, nxt
        while len(adj[cur]) == 2:
            n = next(x for x in adj[cur] if x != prev)
            edge = frozenset((cur, n))
            if edge in seen:
                break
            seen.add(edge)
            line.append(n)
            prev, cur = cur, n
        return line

    for n in adj:  # open chains start at ends and junctions
        if len(adj[n]) != 2:
            for m in adj[n]:
                if frozenset((n, m)) not in seen:
                    lines.append(walk(n, m))
    for n in adj:  # closed loops
        for m in adj[n]:
            if frozenset((n, m)) not in seen:
                lines.append(walk(n, m))
    return [np.array(l, dtype=np.float64) for l in lines]


def douglas_peucker(pts, tol):
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        seg = b - a
        L = np.hypot(*seg)
        rel = pts[i + 1:j] - a
        d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / L if L > 0 else np.hypot(rel[:, 0], rel[:, 1])
        k = int(np.argmax(d))
        if d[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return pts[keep]


def build_contours(extract, placed):
    isos = [p for p in placed if "OvermapOutputIsolineMesh" in p["mesh"]]
    lines, raw_points = [], 0
    for t in isos:
        path = os.path.join(extract, MESH_DIR, t["mesh"] + ".glb")
        if not os.path.isfile(path):
            fail(f"placed mesh missing from extraction: {t['mesh']}")
        p, tri = read_mesh(path)
        w = (p * t["scale"]) @ t["rot"].T + t["loc"]
        for line in ribbon_centerlines(w, tri):
            raw_points += len(line)
            s = douglas_peucker(line, CONTOUR_TOLERANCE_CM)
            if len(s) >= 2 and np.hypot(*(s.max(0) - s.min(0))) > CONTOUR_TOLERANCE_CM * 2:
                lines.append(np.round(s).astype(int))
    return lines, raw_points, len(isos)


# ---------------------------------------------------------------- places

def places(extract):
    level = load_level(os.path.join(extract, OVERMAP_DIR, "Overmap.json"))
    return distil_places(level, "Overmap.json")


def distil_places(level, level_name):
    index = by_object_name(level)
    out = []
    for e in level:
        t = e.get("Type")
        p = e.get("Properties") or {}
        if t not in ("BP_OvermapPoi_C", "BP_IngameText_C", "BP_TravelDestination_C"):
            continue
        root = index.get(asset_name(p.get("RootComponent")))
        if root is None:
            fail(f"{level_name}: {e['Name']} has no root component")
        _, _, loc = component_transform(root, index)
        rec = {"kind": None, "label": e.get("ActorLabel"), "id": None, "name": None, "category": None,
               "description": None, "travelId": None,
               "position": {"x": round(float(loc[0]), 1), "y": round(float(loc[1]), 1), "z": round(float(loc[2]), 1)}}
        if t == "BP_OvermapPoi_C":
            poi = index.get(asset_name(p.get("m_PoiComponent")))
            pp = (poi or {}).get("Properties") or {}
            rec["kind"] = "poi"
            rec["id"] = pp.get("m_PoiName")
            rec["name"] = (pp.get("m_LocationName") or {}).get("LocalizedString")
            rec["description"] = (pp.get("m_LocationDescription") or {}).get("LocalizedString")
            tags = pp.get("m_PoiCategoryTags") or []
            rec["category"] = tags[0] if tags else None
        elif t == "BP_IngameText_C":
            rec["kind"] = "text"
            rec["name"] = (p.get("Text") or {}).get("LocalizedString")
        else:
            rec["kind"] = "dest"
            rec["travelId"] = p.get("TravelDestinationNameOverride") or \
                (root.get("Properties") or {}).get("m_TravelDestinationName")
        out.append(rec)
    order = {"poi": 0, "dest": 1, "text": 2}
    out.sort(key=lambda r: (order[r["kind"]], r["label"] or "", r["id"] or "", r["travelId"] or "",
                            r["position"]["x"], r["position"]["y"]))
    return out


def join_keys(recs):
    for r in recs:
        r["key"] = None
        for s in STONES:
            if r["kind"] == "poi" and r["id"] == s["poi"]:
                r["key"] = s["key"]
            if r["kind"] == "dest" and (
                    (r["travelId"] or "").startswith(s["travelPrefix"]) or
                    (r["label"] or "").startswith(s["travelLabelPrefix"])):
                r["key"] = s["key"]
    stones = []
    for s in STONES:
        poi = [r for r in recs if r["kind"] == "poi" and r["key"] == s["key"]]
        if len(poi) != 1:
            fail(f"stone {s['key']}: expected 1 poi, got {len(poi)}")
        stones.append({"key": s["key"], "name": s["name"], "position": poi[0]["position"],
                       "category": poi[0]["category"], "facets": s["facets"],
                       "travel": [{"label": r["label"], "travelId": r["travelId"]} for r in recs
                                  if r["kind"] == "dest" and r["key"] == s["key"]]})
    return stones


def slug(name):
    out = "".join(c.lower() if c.isalnum() else "-" for c in name.replace("’", "").replace("'", ""))
    return "-".join(p for p in out.split("-") if p)


def build_locations(recs, sub_recs):
    """Merge every POI (Overmap.umap + sublevels, sublevels at their identity world-composition offset) by name."""
    by_name = {}
    for r in recs + sub_recs:
        if r["kind"] != "poi":
            continue
        by_name.setdefault(r["name"], []).append(r)
    out = []
    for name, group in by_name.items():
        cats = {g["category"] for g in group}
        if len(cats) != 1 or group[0]["category"] not in LOCATION_KIND:
            fail(f"location {name}: unexpected categories {cats}")
        pos = {k: round(sum(g["position"][k] for g in group) / len(group), 1) for k in ("x", "y", "z")}
        out.append({
            "id": slug(name), "name": name, "kind": LOCATION_KIND[group[0]["category"]],
            "category": group[0]["category"], "description": group[0]["description"],
            "level": group[0].get("level", "Overmap"), "poiIds": sorted({g["id"] for g in group}),
            "poiCount": len(group), "position": pos,
            "key": next((g["key"] for g in group if g.get("key")), None),
        })
    order = list(LOCATION_KIND_COUNTS)
    out.sort(key=lambda l: (order.index(l["kind"]), l["name"]))
    counts = {k: sum(1 for l in out if l["kind"] == k) for k in order}
    if counts != LOCATION_KIND_COUNTS:
        fail(f"locations by kind {counts}, expected {LOCATION_KIND_COUNTS}")
    if len({l["id"] for l in out}) != len(out):
        fail("duplicate location ids")
    return out


# ---------------------------------------------------------------- art

def art_transform():
    w, h = ART_OUT_SIZE
    x0 = -ART_OFFSET_UNITS[0] * CM_PER_UNIT
    y0 = -ART_OFFSET_UNITS[1] * CM_PER_UNIT
    return {
        "image": "overmap-art-2048.webp", "size": [w, h],
        "source": "T_UI_GlobalMap_D 4096x2048 (Content/Dune/GUI/Textures/Menus/Gameplay/Map)",
        "cmPerLayerUnit": CM_PER_UNIT, "layerOffset": list(ART_OFFSET_UNITS), "layerPlane": list(ART_PLANE_UNITS),
        "formula4096": "px = (X/250 + 996.984) * 2.048, py = (Y/250 + 613.10) * 2.048",
        "pxPerCm": w / (ART_PLANE_UNITS[0] * CM_PER_UNIT),
        "worldRectCm": {"x0": x0, "y0": y0, "x1": x0 + ART_PLANE_UNITS[0] * CM_PER_UNIT,
                        "y1": y0 + ART_PLANE_UNITS[1] * CM_PER_UNIT},
        "axes": "overmap cm, X east (image right), Y south (image down); no rotation or flip",
    }


def write_art(extract, out_path):
    im = Image.open(os.path.join(extract, ART))
    if im.size != (4096, 2048):
        fail(f"T_UI_GlobalMap_D is {im.size}, expected 4096x2048")
    im = im.convert("RGB").resize(ART_OUT_SIZE, Image.LANCZOS)
    im.save(out_path, "WEBP", quality=82, method=6)
    return im


# ---------------------------------------------------------------- previews

def to_px(xy, size):
    w, h = size
    s = w / (ART_PLANE_UNITS[0] * CM_PER_UNIT)
    return np.stack([(xy[:, 0] / CM_PER_UNIT + ART_OFFSET_UNITS[0]) * CM_PER_UNIT * s,
                     (xy[:, 1] / CM_PER_UNIT + ART_OFFSET_UNITS[1]) * CM_PER_UNIT * s], 1)


def rasterize_height(points, tris, size):
    w, h = size
    z = np.full((h, w), np.nan)
    px = to_px(points[:, :2], size)
    for t in tris:
        a, b, c = px[t]
        x0, x1 = int(max(0, math.floor(min(a[0], b[0], c[0])))), int(min(w - 1, math.ceil(max(a[0], b[0], c[0]))))
        y0, y1 = int(max(0, math.floor(min(a[1], b[1], c[1])))), int(min(h - 1, math.ceil(max(a[1], b[1], c[1]))))
        if x1 < x0 or y1 < y0:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-9:
            continue
        l1 = ((b[1] - c[1]) * (gx - c[0]) + (c[0] - b[0]) * (gy - c[1])) / d
        l2 = ((c[1] - a[1]) * (gx - c[0]) + (a[0] - c[0]) * (gy - c[1])) / d
        l3 = 1 - l1 - l2
        m = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if not m.any():
            continue
        zz = l1 * points[t[0], 2] + l2 * points[t[1], 2] + l3 * points[t[2], 2]
        sub = z[y0:y1 + 1, x0:x1 + 1]
        sub[m] = np.fmax(sub[m], zz[m])
    return z


def hillshade(z, cm_per_px, exaggeration=3.0):
    zz = np.nan_to_num(z, nan=np.nanmin(z))
    gy, gx = np.gradient(zz * exaggeration, cm_per_px)
    az, alt = math.radians(315), math.radians(40)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    return np.clip(np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect), 0, 1)


def write_previews(preview_dir, extract, relief_pts, relief_tris, contours, recs, sub_recs, orphans):
    os.makedirs(preview_dir, exist_ok=True)
    size = (2048, 1024)
    cm_per_px = ART_PLANE_UNITS[0] * CM_PER_UNIT / size[0]
    z = rasterize_height(relief_pts, relief_tris, size)
    hs = hillshade(z, cm_per_px)
    covered = ~np.isnan(z)
    base = np.where(covered, 60 + 180 * hs, 25).astype(np.uint8)
    im = Image.fromarray(np.stack([base, (base * 0.92).astype(np.uint8), (base * 0.8).astype(np.uint8)], -1))
    d = ImageDraw.Draw(im)
    for line in contours:
        d.line([tuple(p) for p in to_px(line.astype(float), size)], fill=(200, 140, 40), width=1)
    colours = {"region": (255, 70, 70), "city": (70, 200, 255), "dungeon": (255, 210, 0),
               "story": (200, 120, 255), "poi": (120, 255, 120)}
    for r in recs + sub_recs:
        x, y = to_px(np.array([[r["position"]["x"], r["position"]["y"]]]), size)[0]
        if r["kind"] == "poi":
            col = (255, 255, 255) if r in sub_recs else colours.get(CATEGORY_KIND.get(r["category"]), (255, 255, 255))
            d.ellipse([x - 7, y - 7, x + 7, y + 7], outline=col, width=3)
            d.text((x + 10, y - 6), (r["name"] or r["id"] or "") + (" [sublevel]" if r in sub_recs else ""), fill=col)
        elif r["kind"] == "dest":
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(240, 240, 240))
    im.save(os.path.join(preview_dir, "overmap-relief-hillshade.png"))

    art = Image.open(os.path.join(extract, ART)).convert("RGB").resize(size, Image.LANCZOS)
    a = np.asarray(art).astype(np.float64)
    rock = covered & (np.nan_to_num(z, nan=0) > 400)
    edge = rock ^ np.roll(rock, 1, 0) | rock ^ np.roll(rock, 1, 1)
    over = a * 0.75 + 0.25 * np.stack([hs * 255] * 3, -1) * covered[..., None]
    over[edge] = (0, 255, 255)
    ov = Image.fromarray(over.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(ov)
    for line in contours:
        d.line([tuple(p) for p in to_px(line.astype(float), size)], fill=(255, 60, 60), width=1)
    for name in orphans["tiles"]:
        bx = orphans["bounds"][name]
        p = to_px(np.array([[bx[0], bx[2]], [bx[1], bx[3]]], float), size)
        d.rectangle([tuple(p[0]), tuple(p[1])], outline=(255, 0, 255), width=1)
        d.text((p[0][0] + 2, p[0][1] + 2), name[-3:], fill=(255, 0, 255))
    for r in recs + sub_recs:
        if r["kind"] != "poi":
            continue
        x, y = to_px(np.array([[r["position"]["x"], r["position"]["y"]]]), size)[0]
        d.ellipse([x - 6, y - 6, x + 6, y + 6], outline=(255, 255, 255), width=2)
    ov.save(os.path.join(preview_dir, "overmap-relief-vs-art.png"))


# ---------------------------------------------------------------- main

def write_json(path, obj, compact=False):
    with open(path, "w", encoding="utf-8") as fh:
        if compact:
            json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)
        else:
            json.dump(obj, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def register(out, names, section, record):
    """Add files written by a later bake (miniatures, fx, terraces) to world-manifest.json with bytes + sha256."""
    path = os.path.join(out, "world-manifest.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    for n in names:
        p = os.path.join(out, n)
        manifest["files"][n] = {"bytes": os.path.getsize(p), "sha256": sha256(p)}
    manifest.setdefault("extraBakes", {})[section] = record
    write_json(path, manifest)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", required=True, help="DuneExtractor dump root (has json/, meshes/, png/)")
    ap.add_argument("--client", required=True, help="client build id the dump came from, e.g. 25690505")
    ap.add_argument("--gltfpack", required=True, help="path to the pinned gltfpack binary/wrapper")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--preview-dir", help="write previews here (never under static/)")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    if args.preview_dir and os.path.abspath(args.preview_dir).startswith(os.path.join(REPO, "admin-backend")):
        fail("previews must not land in the served tree")
    os.makedirs(out, exist_ok=True)

    placed = placed_meshes(args.extract)
    pts, tris, per_level, tile_info = assemble_relief(args.extract, placed)
    orphan_names = unplaced_tiles(args.extract, placed)
    orphan_bounds = {}
    for n in orphan_names:
        p, _ = read_mesh(os.path.join(args.extract, MESH_DIR, n + ".glb"))
        orphan_bounds[n] = [float(p[:, 0].min()), float(p[:, 0].max()), float(p[:, 1].min()), float(p[:, 1].max())]
    extent = {"x": [round(float(pts[:, 0].min())), round(float(pts[:, 0].max()))],
              "y": [round(float(pts[:, 1].min())), round(float(pts[:, 1].max()))],
              "z": [round(float(pts[:, 2].min())), round(float(pts[:, 2].max()))]}
    print(f"relief: {len(tile_info)} tiles, {len(pts)} verts, {len(tris)} tris, extent {extent}")
    for lvl, names in per_level.items():
        print(f"  {lvl}: {len(names)} collision tiles")
    for lvl in SUBLEVELS:
        meshes = sorted({p["mesh"] for p in placed if p["level"] == lvl})
        print(f"  {lvl}: places {meshes}")
    print(f"  unplaced collision tiles in the extraction: {len(orphan_names)}")

    # glTF out: x = X/100, y = Z/100, z = Y/100 (overmap metres, Y-up; X east, +z south)
    gpos = np.stack([pts[:, 0], pts[:, 2], pts[:, 1]], 1) / 100.0
    extras = {"units": "overmap metres (UE cm / 100)", "axes": "x = X east, y = Z up, z = Y south",
              "client": args.client}
    relief_path = os.path.join(out, "overmap-relief.glb")
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "relief-raw.glb")
        write_glb(raw, gpos, tris, extras)
        glb_buffer_check(raw)
        gltfpack_version = pack_relief(raw, relief_path, args.gltfpack)
    gl, _ = glb_buffer_check(relief_path)
    print(f"relief glb: {os.path.getsize(relief_path)} bytes, extensions {gl.get('extensionsUsed')}")

    contours, raw_points, iso_tiles = build_contours(args.extract, placed)
    contour_doc = {"units": "overmap cm", "axes": "x east, y south", "toleranceCm": CONTOUR_TOLERANCE_CM,
                   "source": "SM_OvermapOutputIsolineMesh_* ribbon centrelines (Overmap_Geometry placements)",
                   "lines": [l.ravel().tolist() for l in contours]}
    contours_path = os.path.join(out, "overmap-contours.json")
    write_json(contours_path, contour_doc, compact=True)
    kept = sum(len(l) for l in contours)
    print(f"contours: {iso_tiles} isoline meshes, {len(contours)} lines, {raw_points} -> {kept} points")

    recs = places(args.extract)
    if len(recs) != PLACES_COUNT:
        fail(f"Overmap.json gives {len(recs)} places, expected {PLACES_COUNT}")
    stones = join_keys(recs)
    sub_recs = []
    for lvl in SUBLEVELS:
        for r in distil_places(load_level(os.path.join(args.extract, OVERMAP_DIR, lvl)), lvl):
            r["key"] = None
            r["level"] = lvl.split("/")[-1][:-5]
            sub_recs.append(r)
    locations = build_locations(recs, sub_recs)
    places_doc = {"units": "overmap cm", "axes": "x east, y south, z up", "client": args.client,
                  "categories": CATEGORY_KIND, "stones": stones, "places": recs, "sublevelPlaces": sub_recs,
                  "sublevelTransform": SUBLEVEL_TRANSFORM, "locations": locations}
    places_path = os.path.join(out, "overmap-places.json")
    write_json(places_path, places_doc)

    art_path = os.path.join(out, "overmap-art-2048.webp")
    write_art(args.extract, art_path)
    cal = art_transform()
    cal_path = os.path.join(out, "overmap-art.json")
    write_json(cal_path, cal)

    files = {}
    for p in (relief_path, contours_path, places_path, art_path, cal_path):
        files[os.path.basename(p)] = {"bytes": os.path.getsize(p), "sha256": sha256(p)}
    manifest = {
        "bakeStamp": int(time.time() * 1000), "client": args.client, "files": files,
        "calibration": {"cmPerLayerUnit": CM_PER_UNIT, "layerOffset": list(ART_OFFSET_UNITS),
                        "formula4096": cal["formula4096"]},
        "relief": {"tiles": len(tile_info), "tilesByLevel": {k: len(v) for k, v in per_level.items()},
                   "verts": int(len(pts)), "tris": int(len(tris)), "extentCm": extent,
                   "gltfpack": gltfpack_version, "gltfpackArgs": GLTFPACK_ARGS,
                   "units": extras["units"], "axes": extras["axes"],
                   "unplacedTilesSkipped": orphan_names,
                   "artOnlyIsland": {"xCm": [-26000, 33000], "yCm": [71000, 95000],
                                     "note": "the oblong island at the bottom of T_UI_GlobalMap_D has no geometry "
                                             "in this build (no placed collision or visual tile; only the unplaced "
                                             "visual tiles 029-031 cover it). It shows as art + contours only. No "
                                             "location sits on it: the sublevels are at identity (owner-confirmed "
                                             "2026-10-06: The Old Quarry is the southernmost location)."}},
        "contours": {"isolineMeshes": iso_tiles, "lines": len(contours), "points": kept,
                     "toleranceCm": CONTOUR_TOLERANCE_CM},
        "places": {"count": len(recs), "stones": [s["key"] for s in stones], "sublevelPlaces": len(sub_recs),
                   "locations": len(locations), "locationsByKind": LOCATION_KIND_COUNTS},
    }
    # keep what the later bakes registered (their files are unchanged by this one)
    old_path = os.path.join(out, "world-manifest.json")
    if os.path.isfile(old_path):
        with open(old_path, encoding="utf-8") as fh:
            old = json.load(fh)
        for name, rec in old.get("extraBakes", {}).items():
            manifest.setdefault("extraBakes", {})[name] = rec
            for f in rec.get("files", []):
                if f in old["files"] and os.path.isfile(os.path.join(out, f)):
                    manifest["files"][f] = old["files"][f]
    write_json(old_path, manifest)
    for k, v in files.items():
        print(f"  {k}: {v['bytes']} bytes sha256 {v['sha256']}")

    if args.preview_dir:
        write_previews(args.preview_dir, args.extract, pts, tris, contours, recs, sub_recs,
                       {"tiles": orphan_names, "bounds": orphan_bounds})
        print(f"previews -> {args.preview_dir}")


if __name__ == "__main__":
    sys.exit(main())
