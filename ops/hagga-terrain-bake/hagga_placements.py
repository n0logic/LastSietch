#!/usr/bin/env python3
# Distill BP_WorldMap_Meshes_Survival_5.json (DuneExtractor jsonlist output) into the Hagga mesh list and
# per-instance placements the bake reads. Replaces the ad hoc 2026-10-05 snippet.
#   hagga_placements.py <BP_WorldMap_Meshes_Survival_5.json> <out_dir>
# Writes <out_dir>/mesh-manifest.json (asset paths to export as GLB) and <out_dir>/hagga-placements.json.
import collections, json, os, sys

src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
paths, place, fam = set(), [], collections.Counter()
for e in json.load(open(src)):
    if e.get("Type") != "InstancedStaticMeshComponent":
        continue
    props = e.get("Properties") or {}
    op = (props.get("StaticMesh") or {}).get("ObjectPath", "").rsplit(".", 1)[0]
    if not op:
        continue
    paths.add("DuneSandbox/Content/" + op[len("/Game/"):] + ".uasset")
    cs = props.get("RelativeScale3D") or {"X": 1, "Y": 1, "Z": 1}
    for inst in e.get("PerInstanceSMData") or []:
        t = inst["TransformData"]
        place.append({"mesh": op.split("/")[-1], "path": op, "t": t["Translation"],
                      "q": {k: t["Rotation"][k] for k in "XYZW"}, "s": t.get("Scale3D"), "comp_scale": cs})
    fam[op.split("/")[-1].split("_x")[0][:40]] += len(e.get("PerInstanceSMData") or [])
json.dump(sorted(paths), open(out + "/mesh-manifest.json", "w"), indent=1)
json.dump(place, open(out + "/hagga-placements.json", "w"))
print(len(paths), "mesh assets,", len(place), "instances; top families:", fam.most_common(4))
