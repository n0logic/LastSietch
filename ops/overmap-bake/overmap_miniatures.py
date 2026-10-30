#!/usr/bin/env python3
"""Location miniatures for the Sand Table: every overland location's own in-game miniature (the SM_OvermapPoi_*
meshes its POI actor and the StaticMeshActors around it place), the travel-volume disc and ring, the in-game label
and the travel-pointer splines. Writes overmap-miniatures.glb + overmap-miniatures.json next to the other world
artifacts. Reads the locations from overmap-places.json, so run overmap_bake.py first. See README.md.
"""
import argparse
import io
import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

import overmap_bake as ob

MESH_ROOT_HINT = "~/dd-re/overmap/meshes (DuneExtractor mesh dump of Systems/Overmap/Meshes/POI and their materials)"
ASSIGN_RADIUS_CM = 3000.0        # a miniature component belongs to the nearest location within this
TEX_SIZE = 256
GLTFPACK_ARGS = ["-c", "-kn", "-km"]

# Game material -> web material. Base colour textures are the ones the game material samples (world-aligned in the
# game; here on UV0, which these meshes unwrap 0..1). Tints are the material's own parameters, linear RGB.
MATERIALS = {
    "M_OverlandMap_POI": {
        "texture": "T_MLS_Metal_Atre_Bronze_02_D", "factor": [1.0, 1.0, 1.0, 1.0], "metallic": 0.6, "roughness": 0.55,
        "game": {"ColorBalance Color": [0.05, 0.08, 0.09], "ColorTweaks": [0.05, 0.1, 0.1], "RoughnessMin": -0.5,
                 "Roughness Max": 0.8, "textures": ["T_MLS_Metal_Atre_Bronze_02_D", "T_MLS_Metal_Atre_Bronze_07_D",
                                                    "T_MLS_Metal_Ecolabs_Rusted_01_N"]}},
    "MI_OverlandMap_POI_02": {
        "texture": "T_MLS_Metal_Choam_Alluminium_01_D", "factor": [0.292969, 0.3125, 0.298249, 1.0], "metallic": 0.7,
        "roughness": 0.5,
        "game": {"parent": "MI_ModularBuildinings_WindS_Metal_Master", "FlatColorTint": [0.292969, 0.3125, 0.298249],
                 "ConvexColorTint": [0.039918, 0.038602, 0.072917], "ConcaveColorTint": [0.51987, 0.688085, 0.835069]}},
    "MI_Overland_Erythrite_Crystals": {
        "texture": "T_MLS_Stone_Neut_Crystal_01_D", "factor": [1.0, 0.75, 0.8, 1.0], "metallic": 0.0, "roughness": 0.3,
        "emissive": [0.71875, 0.071126, 0.157476],
        "game": {"Crystal - Emissive Color": [0.71875, 0.071126, 0.157476], "Crystal - Emissive Power": 2.0,
                 "Crystal - Glowing Speed": 0.4}},
}
# The material slot of SM_Env_Mineral_* is overridden in the level to MI_Overland_Erythrite_Crystals.
MESH_MATERIAL_OVERRIDE = {"SM_Env_Mineral_Neut_Ore_Erythrite_D_02": "MI_Overland_Erythrite_Crystals"}

LABEL_STYLE = {
    "gold3d": {"use": "regions and settlements: 3D gold letters", "transform": "upper",
               "game": "BP_IngameText FontSize E_InGameTextSize::NewEnumerator1, ETextTransformPolicy::ToUpper"},
    "caption": {"use": "small caption under the disc", "transform": "none", "tint": [0.697917, 0.525801, 0.38905],
                "game": "BP_IngameText default (BP_EditorWidgetComponent TintColorAndOpacity DAC0A8)"},
}
# MPC_POI_TravelVolumes + M_POI_TravelVolume_Circular (translucent, on an engine Plane of 100 cm)
DISC = {"fill": [0.046875, 0.019775, 0.029992, 1.0], "border": [0.276042, 0.212316, 0.092014, 0.4],
        "thickness": 0.01, "size": 0.5, "falloff": 3.102082, "gradientStrength": 1.0,
        "source": "MPC_POI_TravelVolumes FillColor/BorderColor, M_POI_TravelVolume_Circular scalars (linear RGB)"}


def is_miniature_mesh(name):
    if name.startswith("SM_OvermapPoi_"):
        return not name.startswith("SM_OvermapPoi_Gate")
    return name.startswith("SM_Env_Mineral_")


def to_out(v_cm):
    """UE cm (X, Y, Z) -> output metres (x = X, y = Z, z = Y), the frame of overmap-relief.glb."""
    return np.array([v_cm[0], v_cm[2], v_cm[1]]) / 100.0


def instance_matrix(comp, anchor_cm):
    """glTF column-major matrix placing an extractor-frame mesh relative to the location anchor."""
    p = np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]], float)
    lin = p @ comp["rot"] @ np.diag(comp["scale"]) @ p
    t = to_out(comp["loc"]) - to_out(anchor_cm)
    m = np.eye(4)
    m[:3, :3] = lin
    m[:3, 3] = t
    return m


def index_files(roots, ext):
    out = {}
    for root in roots:
        for d, _, files in os.walk(os.path.expanduser(root)):
            for f in files:
                if f.endswith(ext):
                    out.setdefault(f[:-len(ext)], os.path.join(d, f))
    return out


def level_components(extract):
    out = []
    for rel in ["Overmap.json"] + ob.SUBLEVELS:
        level = ob.load_level(os.path.join(extract, ob.OVERMAP_DIR, rel))
        index = ob.by_object_name(level)
        for e in level:
            if e.get("Type") != "StaticMeshComponent":
                continue
            p = e.get("Properties") or {}
            mesh = ob.asset_name(p.get("StaticMesh"))
            if not mesh or not is_miniature_mesh(mesh):
                continue
            scale, rot, loc = ob.component_transform(e, index)
            mats = [ob.asset_name(m) for m in (p.get("OverrideMaterials") or []) if m]
            out.append({"level": rel, "mesh": mesh, "scale": scale, "rot": rot, "loc": loc,
                        "actor": ob.asset_name(e.get("Outer")).split(".")[-1], "overrideMaterials": mats})
    return out


def disc_planes(extract):
    out = []
    for rel in ["Overmap.json"] + ob.SUBLEVELS:
        level = ob.load_level(os.path.join(extract, ob.OVERMAP_DIR, rel))
        index = ob.by_object_name(level)
        for e in level:
            if e.get("Type") != "StaticMeshComponent":
                continue
            p = e.get("Properties") or {}
            mats = [ob.asset_name(m) for m in (p.get("OverrideMaterials") or []) if m]
            if ob.asset_name(p.get("StaticMesh")) != "Plane" or mats != ["M_POI_TravelVolume_Circular"]:
                continue
            scale, _, loc = ob.component_transform(e, index)
            out.append({"loc": loc, "radiusCm": round(abs(float(scale[0])) * 50.0, 1)})
    return out


def text_labels(extract):
    out = []
    for rel in ["Overmap.json"] + ob.SUBLEVELS:
        level = ob.load_level(os.path.join(extract, ob.OVERMAP_DIR, rel))
        index = ob.by_object_name(level)
        for e in level:
            if e.get("Type") != "BP_IngameText_C":
                continue
            p = e.get("Properties") or {}
            root = index.get(ob.asset_name(p.get("RootComponent")))
            _, _, loc = ob.component_transform(root, index)
            big = p.get("FontSize") == "E_InGameTextSize::NewEnumerator1"
            out.append({"text": (p.get("Text") or {}).get("LocalizedString"), "loc": loc,
                        "style": "gold3d" if big else "caption", "actor": e.get("ActorLabel")})
    return out


def hermite(p0, m0, p1, m1, n=24):
    t = np.linspace(0, 1, n)[:, None]
    h00, h10, h01, h11 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t, -2 * t**3 + 3 * t**2, t**3 - t**2
    return h00 * p0 + h10 * m0 + h01 * p1 + h11 * m1


def travel_lines(extract, locations):
    level = ob.load_level(os.path.join(extract, ob.OVERMAP_DIR, "Overmap.json"))
    index = ob.by_object_name(level)
    lines = []
    for e in level:
        if e.get("Type") != "SplineComponent":
            continue
        scale, rot, loc = ob.component_transform(e, index)
        pts = (e.get("Properties") or {}).get("SplineCurves", {}).get("Position", {}).get("Points", [])
        world = []
        for a, b in zip(pts, pts[1:]):
            v = lambda d: np.array([d["X"], d["Y"], d["Z"]])
            p0, p1 = v(a["OutVal"]), v(b["OutVal"])
            m0, m1 = v(a["LeaveTangent"]), v(b["ArriveTangent"])
            seg = hermite(p0, m0, p1, m1)
            world.extend(((seg * scale) @ rot.T + loc).tolist())
        if len(world) < 2:
            continue
        ends = [nearest_location(locations, np.array(world[0])), nearest_location(locations, np.array(world[-1]))]
        lines.append({"actor": ob.asset_name(e.get("Outer")).split(".")[-1],
                      "from": ends[0][0], "fromDistCm": ends[0][1], "to": ends[1][0], "toDistCm": ends[1][1],
                      "game": "BP_Pointers (NS_FlyingLights along a SplineComponent)",
                      "pointsCm": [[round(c) for c in p] for p in world]})
    lines.sort(key=lambda l: l["actor"])
    return lines


def nearest_location(locations, xyz):
    best = min(locations, key=lambda l: math.hypot(l["position"]["x"] - xyz[0], l["position"]["y"] - xyz[1]))
    return best["id"], round(math.hypot(best["position"]["x"] - xyz[0], best["position"]["y"] - xyz[1]))


def assign(locations, comps):
    per = {l["id"]: [] for l in locations}
    for c in comps:
        d = [(math.hypot(l["position"]["x"] - c["loc"][0], l["position"]["y"] - c["loc"][1]), l["id"])
             for l in locations]
        dist, lid = min(d)
        if dist > ASSIGN_RADIUS_CM:
            ob.fail(f"miniature component {c['actor']}/{c['mesh']} is {dist:.0f} cm from the nearest location")
        per[lid].append(c)
    for lid, cs in per.items():
        if not cs:
            ob.fail(f"location {lid} has no miniature of its own")
    return per


# ---------------------------------------------------------------- glb

def load_mini_mesh(path):
    """Primitives of an extractor GLB as (positions, normals, uv0, indices, material name), extractor frame."""
    gl, binc = ob.glb_buffer_check(path)
    out = []
    for mesh in gl["meshes"]:
        for prim in mesh["primitives"]:
            a = prim["attributes"]
            mat = gl["materials"][prim["material"]]["name"] if "material" in prim else None
            out.append((ob.accessor(gl, binc, a["POSITION"]).astype("<f4"),
                        ob.accessor(gl, binc, a["NORMAL"]).astype("<f4"),
                        ob.accessor(gl, binc, a["TEXCOORD_0"]).astype("<f4"),
                        ob.accessor(gl, binc, prim["indices"]).astype("<u4"), mat))
    return out


def texture_jpeg(path):
    im = Image.open(path).convert("RGB").resize((TEX_SIZE, TEX_SIZE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue()


class GlbWriter:
    def __init__(self):
        self.bin = bytearray()
        self.gl = {"asset": {"version": "2.0", "generator": "LastSietch ops/overmap-bake"}, "scene": 0,
                   "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "materials": [], "textures": [],
                   "images": [], "samplers": [{"wrapS": 10497, "wrapT": 10497}], "buffers": [],
                   "bufferViews": [], "accessors": []}

    def view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        bv = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            bv["target"] = target
        self.bin.extend(data)
        self.gl["bufferViews"].append(bv)
        return len(self.gl["bufferViews"]) - 1

    def accessor(self, arr, ctype, typ, target, minmax=False):
        acc = {"bufferView": self.view(arr.tobytes(), target), "componentType": ctype,
               "count": len(arr), "type": typ}
        if minmax:
            acc["min"] = arr.min(0).tolist()
            acc["max"] = arr.max(0).tolist()
        self.gl["accessors"].append(acc)
        return len(self.gl["accessors"]) - 1

    def material(self, name, spec, jpeg):
        img = {"bufferView": self.view(jpeg), "mimeType": "image/jpeg", "name": spec["texture"]}
        self.gl["images"].append(img)
        self.gl["textures"].append({"source": len(self.gl["images"]) - 1, "sampler": 0})
        mat = {"name": name, "pbrMetallicRoughness": {
            "baseColorTexture": {"index": len(self.gl["textures"]) - 1}, "baseColorFactor": spec["factor"],
            "metallicFactor": spec["metallic"], "roughnessFactor": spec["roughness"]},
            "extras": {"game": spec["game"]}}
        if spec.get("emissive"):
            mat["emissiveFactor"] = spec["emissive"]
        self.gl["materials"].append(mat)
        return len(self.gl["materials"]) - 1

    def mesh(self, name, prims, mat_index):
        out = []
        for pos, nrm, uv, idx, mat in prims:
            out.append({"attributes": {"POSITION": self.accessor(pos, 5126, "VEC3", 34962, True),
                                       "NORMAL": self.accessor(nrm, 5126, "VEC3", 34962),
                                       "TEXCOORD_0": self.accessor(uv, 5126, "VEC2", 34962)},
                        "indices": self.accessor(idx, 5125, "SCALAR", 34963), "material": mat_index[mat],
                        "mode": 4})
        self.gl["meshes"].append({"name": name, "primitives": out})
        return len(self.gl["meshes"]) - 1

    def node(self, node, root=False):
        self.gl["nodes"].append(node)
        i = len(self.gl["nodes"]) - 1
        if root:
            self.gl["scenes"][0]["nodes"].append(i)
        return i

    def write(self, path):
        while len(self.bin) % 4:
            self.bin.append(0)
        self.gl["buffers"] = [{"byteLength": len(self.bin)}]
        js = json.dumps(self.gl, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        import struct
        with open(path, "wb") as fh:
            fh.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(self.bin)))
            fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
            fh.write(struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin))


def build_glb(path, locations, per, mesh_files, tex_files):
    w = GlbWriter()
    mat_index = {}
    for name, spec in MATERIALS.items():
        if spec["texture"] not in tex_files:
            ob.fail(f"texture {spec['texture']} missing from the texture roots")
        mat_index[name] = w.material(name, spec, texture_jpeg(tex_files[spec["texture"]]))
    mesh_index, tris = {}, {}
    for lid in per:
        for c in per[lid]:
            if c["mesh"] in mesh_index:
                continue
            if c["mesh"] not in mesh_files:
                ob.fail(f"miniature mesh {c['mesh']} missing from the mesh roots")
            prims = load_mini_mesh(mesh_files[c["mesh"]])
            if c["mesh"] in MESH_MATERIAL_OVERRIDE:
                prims = [p[:4] + (MESH_MATERIAL_OVERRIDE[c["mesh"]],) for p in prims]
            for p in prims:
                if p[4] not in mat_index:
                    ob.fail(f"{c['mesh']}: material {p[4]} has no web mapping")
            mesh_index[c["mesh"]] = w.mesh(c["mesh"], prims, mat_index)
            tris[c["mesh"]] = int(sum(len(p[3]) for p in prims) // 3)
    for loc in locations:
        anchor = np.array([loc["position"][k] for k in ("x", "y", "z")])
        children = []
        for k, c in enumerate(per[loc["id"]]):
            m = instance_matrix(c, anchor)
            children.append(w.node({"name": f"{loc['id']}_{k}_{c['mesh']}", "mesh": mesh_index[c["mesh"]],
                                    "matrix": m.T.ravel().round(6).tolist()}))
        w.node({"name": "loc_" + loc["id"], "translation": to_out(anchor).round(3).tolist(), "children": children},
               root=True)
    w.write(path)
    return tris


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", required=True, help="DuneExtractor dump root with the overmap level JSON")
    ap.add_argument("--meshes", required=True, help=MESH_ROOT_HINT)
    ap.add_argument("--textures", nargs="+", required=True, help="roots holding the decoded T_MLS_* pngs")
    ap.add_argument("--gltfpack", required=True)
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", default=ob.DEFAULT_OUT)
    args = ap.parse_args()

    places = json.load(open(os.path.join(args.out, "overmap-places.json"), encoding="utf-8"))
    locations = places["locations"]
    comps = level_components(args.extract)
    per = assign(locations, comps)
    mesh_files = index_files([args.meshes], ".glb")
    tex_files = index_files(args.textures + [args.meshes], ".png")

    glb_path = os.path.join(args.out, "overmap-miniatures.glb")
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "miniatures-raw.glb")
        tris = build_glb(raw, locations, per, mesh_files, tex_files)
        ob.glb_buffer_check(raw)
        print(f"raw miniatures glb {os.path.getsize(raw)} bytes, {len(tris)} meshes, {sum(tris.values())} tris")
        res = subprocess.run([args.gltfpack, "-i", raw, "-o", glb_path] + GLTFPACK_ARGS, capture_output=True,
                             text=True)
        if res.returncode != 0:
            ob.fail(f"gltfpack failed: {res.stderr.strip()}")
    ob.glb_buffer_check(glb_path)
    print(f"overmap-miniatures.glb {os.path.getsize(glb_path)} bytes")

    users = {}
    for lid, cs in per.items():
        for m in {c["mesh"] for c in cs}:
            users.setdefault(m, []).append(lid)
    discs = disc_planes(args.extract)
    labels = text_labels(args.extract)
    out_locs = {}
    for loc in locations:
        anchor = np.array([loc["position"][k] for k in ("x", "y", "z")])
        cs = per[loc["id"]]
        meshes = {}
        for c in cs:
            meshes[c["mesh"]] = meshes.get(c["mesh"], 0) + 1
        near = [d for d in discs if math.hypot(*(d["loc"][:2] - anchor[:2])) < max(ASSIGN_RADIUS_CM, d["radiusCm"])]
        disc = min(near, key=lambda d: math.hypot(*(d["loc"][:2] - anchor[:2]))) if near else None
        reach = max(6000.0, disc["radiusCm"] if disc else 0.0)
        lab = [l for l in labels if math.hypot(*(l["loc"][:2] - anchor[:2])) < reach]
        lab.sort(key=lambda l: (l["text"].lower().strip() != loc["name"].lower().strip(),
                                math.hypot(*(l["loc"][:2] - anchor[:2]))))
        big = loc["kind"] in ("region", "settlement")
        out_locs[loc["id"]] = {
            "name": loc["name"], "kind": loc["kind"], "node": "loc_" + loc["id"],
            "anchorCm": loc["position"],
            "meshes": [{"mesh": m, "count": n, "tris": tris[m],
                        "sharedWith": sorted(x for x in users[m] if x != loc["id"])} for m, n in sorted(meshes.items())],
            "actors": sorted({c["actor"] for c in cs}),
            "disc": None if disc is None else {"centerCm": [round(float(v)) for v in disc["loc"]],
                                               "radiusCm": disc["radiusCm"]},
            "label": {"text": lab[0]["text"] if lab else loc["name"],
                      "positionCm": [round(float(v)) for v in lab[0]["loc"]] if lab else None,
                      "style": "gold3d" if big else "caption", "gameStyle": lab[0]["style"] if lab else None},
        }
    doc = {
        "client": args.client, "glb": "overmap-miniatures.glb",
        "units": "glb: overmap metres, x = X east, y = Z up, z = Y south (same frame as overmap-relief.glb); "
                 "json: overmap cm",
        "nodes": "one root node 'loc_<id>' per location at its anchor; children are the game's mesh "
                 "placements relative to it",
        "gltfpack": GLTFPACK_ARGS, "disc": DISC, "labelStyles": LABEL_STYLE,
        "materials": {k: {"texture": v["texture"], "game": v["game"]} for k, v in MATERIALS.items()},
        "locations": out_locs, "travelLines": travel_lines(args.extract, locations),
        "unusedPoiMeshes": sorted(k for k in mesh_files if k.startswith("SM_OvermapPoi_")
                                  and k not in users and not k.startswith("SM_OvermapPoi_Gate")),
    }
    ob.write_json(os.path.join(args.out, "overmap-miniatures.json"), doc)
    ob.register(args.out, ["overmap-miniatures.glb", "overmap-miniatures.json"], "miniatures",
                {"files": ["overmap-miniatures.glb", "overmap-miniatures.json"], "script": "overmap_miniatures.py",
                 "locations": len(out_locs), "meshes": len(tris), "tris": sum(tris.values()),
                 "gltfpack": GLTFPACK_ARGS})
    for lid, l in out_locs.items():
        print(f"  {lid:26s} {[m['mesh'] for m in l['meshes']]} disc={l['disc'] and l['disc']['radiusCm']} "
              f"label={l['label']['text']!r}/{l['label']['gameStyle']}")
    for t in doc["travelLines"]:
        print(f"  line {t['actor']}: {t['from']} ({t['fromDistCm']}) -> {t['to']} ({t['toDistCm']})")
    print("  unused POI meshes:", doc["unusedPoiMeshes"])


if __name__ == "__main__":
    sys.exit(main())
