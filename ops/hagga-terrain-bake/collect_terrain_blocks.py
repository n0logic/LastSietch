#!/usr/bin/env python3
# Collect every hand-placed terrain block (BP_TerrainBlockActor_C) from Hagga's regional sublevels and map each
# terrain-block asset to the meshes the composite can rasterize.
#   collect_terrain_blocks.py <sublevel_json_dir> <tb_asset_json_dir> <client_file_list.txt> <out_dir>
# <sublevel_json_dir>: DuneExtractor jsonlist output of SOC_1/SubLevels/Survival_1_<Region>.umap (see README).
# <tb_asset_json_dir>: jsonlist output of Systems/DynamicContent/TerrainBlocks/Collection/<asset>.uasset. Each data
#   asset names its content folder; meshes are resolved by folder because asset, folder and mesh names disagree
#   (e.g. CB_RedDesert_LandMark_Mirzabah -> .../CB_RedDesert_Landmark_Mirzabah/SM_CB_RedDesert_Landmark_Mizabah_*).
# Writes terrain-blocks.json, tb-lod1-map.json, tb-collision-map.json and the two GLB export manifests.
import collections, glob, json, os, re, sys

src, tb_src, file_list, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
os.makedirs(out, exist_ok=True)

blocks = []
for f in sorted(glob.glob(src + "/**/Survival_1_*.json", recursive=True)):
    level = os.path.basename(f)[:-5]
    if "MTXEvent" in level or "MTX_" in level:
        continue                                     # event-only content, not the standing world
    d = json.load(open(f))
    comps = {}
    for e in d:
        o = e.get("Outer")
        if isinstance(o, dict):
            comps[(o.get("ObjectName", "").split(".")[-1].rstrip("'"), e.get("Name"))] = e
    for e in d:
        if e.get("Type") != "BP_TerrainBlockActor_C":
            continue
        p = e.get("Properties") or {}
        asset = (p.get("m_TerrainBlockAsset") or {}).get("ObjectName", "").split("'")
        root = (p.get("RootComponent") or {}).get("ObjectName", "").split("'")
        if len(asset) < 2 or len(root) < 2:
            continue
        cp = (comps.get((e["Name"], root[1].split(".")[-1])) or {}).get("Properties") or {}
        if not cp.get("RelativeLocation"):
            sys.exit(f"FAIL: {level}/{e['Name']} has no resolvable transform")
        blocks.append({"level": level, "actor": e["Name"], "asset": asset[1], "t": cp["RelativeLocation"],
                       "r": cp.get("RelativeRotation"), "s": cp.get("RelativeScale3D")})

by_dir = collections.defaultdict(list)
for line in open(file_list):
    path = line.strip()
    if path.endswith(".uasset"):
        by_dir[path.rsplit("/", 1)[0].replace("/LOD", "").replace("/Meshes", "")].append(path)

assets = sorted({b["asset"] for b in blocks})
lod1_map, coll_map, folders = {}, {}, {}
for a in assets:
    j = glob.glob(f"{tb_src}/**/{a}.json", recursive=True)
    if not j:
        continue
    refs = re.findall(r"/Game/(Dune/Maps/[A-Za-z0-9_/]+)/CB_[A-Za-z0-9_]+", open(j[0]).read())
    if not refs:
        continue
    folder = "DuneSandbox/Content/" + refs[0]; folders[a] = folder
    files = by_dir.get(folder, [])
    names = {f.rsplit("/", 1)[-1][:-7]: f for f in files}
    l1 = [f for n, f in names.items() if re.match(r"SM_(PROXY_)?CB_.+_(Art|Final|Content)_LOD1$", n)]
    if l1:
        lod1_map[a] = sorted(l1, key=lambda f: ("_Art_" not in f, f))[0]
    cl = sorted(f for n, f in names.items() if re.match(r"SM_Collision_CB_.+_Art_\d+$", n))
    if cl:
        coll_map[a] = cl
json.dump(blocks, open(out + "/terrain-blocks.json", "w"), indent=1)
json.dump(lod1_map, open(out + "/tb-lod1-map.json", "w"), indent=1)
json.dump(coll_map, open(out + "/tb-collision-map.json", "w"), indent=1)
json.dump(sorted(set(lod1_map.values())), open(out + "/tb-lod1-manifest.json", "w"), indent=1)
json.dump(sorted({p for v in coll_map.values() for p in v}), open(out + "/tb-collision-manifest.json", "w"), indent=1)
print(f"{len(blocks)} blocks from {len({b['level'] for b in blocks})} sublevels, {len(assets)} assets; "
      f"LOD1 proxy for {len(lod1_map)}, collision for {len(coll_map)}; "
      f"neither: {[a for a in assets if a not in lod1_map and a not in coll_map]}")
print(collections.Counter(b["level"] for b in blocks).most_common())
