#!/usr/bin/env python3
"""Overland vehicles: the BP_MeshFollowingSpline actors of Overmap_VFX (3 distance ornithopters, 1 tiny sandbike).
Writes overmap-vehicles.json (each actor's root transform, its spline in actor space with the game's Hermite tangents,
LoopTime) and packs the two meshes (sandbike with its albedo embedded) to meshopt GLBs.
Usage: overmap_vehicles.py --extract <json root> --meshes <vehicle mesh root> --gltfpack <bin> --out <static/world-data>
"""
import argparse, io, json, os, struct, subprocess, tempfile
from PIL import Image

LEVEL = "DuneSandbox/Content/Dune/Systems/Overmap/Overmap_VFX.json"
DEFAULT_LOOP = 10.0   # BP_MeshFollowingSpline default LoopTime


def v3(p):
    return [round(p.get("X", 0.0), 3), round(p.get("Y", 0.0), 3), round(p.get("Z", 0.0), 3)]


def actors(level):
    objs = {o.get("Name"): o for o in level}
    by_path = {}
    for o in level:
        by_path.setdefault((o.get("Type"), o.get("Outer", {}).get("ObjectName") if isinstance(o.get("Outer"), dict) else o.get("Outer")), []).append(o)
    out = []
    for o in level:
        if o.get("Type") != "BP_MeshFollowingSpline_C":
            continue
        name = o["Name"]
        mesh = o["Properties"]["Mesh"]["ObjectName"]
        root = spline = None
        for c in level:
            outer = c.get("Outer")
            if not (isinstance(outer, dict) and outer.get("ObjectName", "").endswith(name + "'")):
                continue
            if c.get("Name") == "DefaultSceneRoot":
                root = c["Properties"]
            elif c.get("Type") == "SplineComponent":
                spline = c["Properties"]
        pts = [{"p": v3(q["OutVal"]), "arrive": v3(q["ArriveTangent"]), "leave": v3(q["LeaveTangent"])}
               for q in spline["SplineCurves"]["Position"]["Points"]]
        out.append({
            "actor": name,
            "kind": "sandbike" if "Sandbike" in mesh else "ornithopter",
            "rootCm": v3(root["RelativeLocation"]),
            "yawDeg": root.get("RelativeRotation", {}).get("Yaw", 0.0),
            "splineOffsetCm": v3(spline.get("RelativeLocation", {})),
            "loopSec": o["Properties"].get("LoopTime", DEFAULT_LOOP),
            "points": pts,
        })
    return out


def pack(gltfpack, src_glb, dst, png=None):
    raw = open(src_glb, "rb").read()
    n = struct.unpack("<I", raw[12:16])[0]
    g = json.loads(raw[20:20 + n])
    binc = bytearray(raw[20 + n + 8:])
    if png:
        buf = io.BytesIO()
        Image.open(png).convert("RGB").save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        while len(binc) % 4:
            binc.append(0)
        g["bufferViews"].append({"buffer": 0, "byteOffset": len(binc), "byteLength": len(data)})
        binc += data
        while len(binc) % 4:
            binc.append(0)
        g["images"] = [{"bufferView": len(g["bufferViews"]) - 1, "mimeType": "image/png"}]
        g["samplers"] = [{"magFilter": 9729, "minFilter": 9987}]
        g["textures"] = [{"source": 0, "sampler": 0}]
        g["materials"][0]["pbrMetallicRoughness"] = {"baseColorTexture": {"index": 0}, "metallicFactor": 0.0, "roughnessFactor": 0.8}
        g["buffers"][0]["byteLength"] = len(binc)
    js = json.dumps(g).encode()
    while len(js) % 4:
        js += b" "
    glb = struct.pack("<III", 0x46546C67, 2, 28 + len(js) + len(binc)) + struct.pack("<II", len(js), 0x4E4F534A) + js \
        + struct.pack("<II", len(binc), 0x004E4942) + bytes(binc)
    with tempfile.TemporaryDirectory() as t:
        tmp = os.path.join(t, "in.glb")
        open(tmp, "wb").write(glb)
        subprocess.run([gltfpack, "-i", tmp, "-o", dst, "-c", "-kn"], check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", required=True)
    ap.add_argument("--meshes", required=True)
    ap.add_argument("--gltfpack", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    level = json.load(open(os.path.join(a.extract, LEVEL)))
    doc = {"client": "25690505", "units": "overmap cm; points are in actor space (rootCm + yaw rotation + splineOffsetCm), UE cubic Hermite per segment",
           "actors": actors(level)}
    json.dump(doc, open(os.path.join(a.out, "overmap-vehicles.json"), "w"), separators=(",", ":"))
    c = os.path.join(a.meshes, "DuneSandbox/Content/Dune")
    pack(a.gltfpack, os.path.join(c, "Environment/Sky/DistantVillageShips/Meshes/SM_VFX_Env_Atre_Ships_DistanceOrnithopter.glb"),
         os.path.join(a.out, "overmap-ornithopter.glb"))
    sb = os.path.join(c, "Systems/Overmap/VFX/MeshFollowingSpline")
    pack(a.gltfpack, os.path.join(sb, "SM_OverlandMap_TinySandbike.glb"), os.path.join(a.out, "overmap-sandbike.glb"),
         os.path.join(sb, "T_OverlandMap_Sandbike_D.png"))


if __name__ == "__main__":
    main()
