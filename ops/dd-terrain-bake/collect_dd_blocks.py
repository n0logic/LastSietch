#!/usr/bin/env python3
"""Collect every Deep Desert terrain block (rock) per Coriolis layout and map each block asset to its meshes.

  collect_dd_blocks.py --json-root <jsonlist root> --tb-json <tb asset json root> --file-list <list.txt> --out <dir>

Sources (verified client 25690505):
  per layout  Maps/Arrakis/DeepDesert_1/Layouts/DA_DeepDesert_1_Layout_NN.m_Clusters ->
              {ClusterAnchor (cells), WLC_* asset, ClusterVariation}. WLC.m_Variations[var].TerrainBlocks are authored
              in cluster space (world frame with the cluster at anchor 0,0): world = Location + anchor * 101600.
  static      BP_TerrainBlockActor_C in the DeepDesert_1 sublevels (ShieldWall_01 490, GapFix 4, Travel 3), the
              same 497 in every layout.
Mesh map: same rule as ops/hagga-terrain-bake/collect_terrain_blocks.py (asset -> content folder -> collision
meshes SM_Collision_CB_*_Art_N, else the LOD1 proxy; never _UIMap).

Writes terrain-blocks-layoutNN.json, tb-assets.json, tb-lod1-map.json, tb-collision-map.json, the two GLB export
manifests and summary.json.
"""
import argparse, collections, glob, json, os, re, sys

CELL = 101600.0


def asset_name(ref):
    s = (ref or {}).get("ObjectName", "")
    return s.split("'")[1] if "'" in s else ""


def static_blocks(arrakis):
    out = []
    for f in sorted(glob.glob(arrakis + "/DeepDesert_1/SubLevels/*.json")):
        level = os.path.basename(f)[:-5]
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
            asset = asset_name(p.get("m_TerrainBlockAsset"))
            root = (p.get("RootComponent") or {}).get("ObjectName", "").split("'")
            cp = {}
            if len(root) > 1:
                cp = (comps.get((e["Name"], root[1].split(".")[-1])) or {}).get("Properties") or {}
            if not asset or not cp.get("RelativeLocation"):
                sys.exit(f"FAIL: {level}/{e['Name']} has no asset or transform")
            out.append({"src": level, "actor": e["Name"], "asset": asset, "t": cp["RelativeLocation"],
                        "r": cp.get("RelativeRotation"), "s": cp.get("RelativeScale3D")})
    return out


def cluster_blocks(da, wlc):
    out = []
    for c in da["m_Clusters"]:
        v = c["Value"]
        name = v["ClusterDataAsset"]["ObjectPath"].split("/")[-1].split(".")[0]
        anchor = v["ClusterAnchor"]
        if v["ClusterVariation"] not in wlc.get(name, {}):
            sys.exit(f"FAIL: {name} has no variation {v['ClusterVariation']}")
        for b in wlc[name][v["ClusterVariation"]].get("TerrainBlocks", []):
            if not b["bIsEnabled"]:
                continue
            L = b["Location"]
            out.append({"src": f"{name}:{v['ClusterVariation']}@{anchor['X']},{anchor['Y']}",
                        "actor": b["TerrainBlockActorName"], "asset": asset_name(b["TerrainBlockDataAsset"]),
                        "t": {"X": L["X"] + anchor["X"] * CELL, "Y": L["Y"] + anchor["Y"] * CELL, "Z": L["Z"]},
                        "r": b["Rotation"], "s": b.get("Scale") or b.get("Scale3D")})
    return out


def mesh_maps(assets, tb_json, file_list):
    by_dir = collections.defaultdict(list)
    for line in open(file_list):
        path = line.strip()
        if path.endswith(".uasset"):
            by_dir[path.rsplit("/", 1)[0].replace("/LOD", "").replace("/Meshes", "")].append(path)
    lod1, coll = {}, {}
    for a in assets:
        j = glob.glob(f"{tb_json}/**/{a}.json", recursive=True)
        if not j:
            sys.exit(f"FAIL: no data-asset json for {a} under {tb_json}")
        refs = re.findall(r"/Game/(Dune/Maps/[A-Za-z0-9_/]+)/CB_[A-Za-z0-9_]+", open(j[0]).read())
        if not refs:
            continue
        names = {f.rsplit("/", 1)[-1][:-7]: f for f in by_dir.get("DuneSandbox/Content/" + refs[0], [])}
        l1 = [f for n, f in names.items() if re.match(r"SM_(PROXY_)?CB_.+_(Art|Final|Content)_LOD1$", n)]
        if l1:
            lod1[a] = sorted(l1, key=lambda f: ("_Art_" not in f, f))[0]
        cl = sorted(f for n, f in names.items() if re.match(r"SM_Collision_CB_.+_Art_\d+$", n))
        if cl:
            coll[a] = cl
    return lod1, coll


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json-root", required=True, help="jsonlist root holding DuneSandbox/Content/Dune/Maps/Arrakis")
    ap.add_argument("--tb-json", required=True, help="jsonlist root of TerrainBlocks/Collection/<asset>.uasset")
    ap.add_argument("--file-list", required=True, help="DuneExtractor `list` output for the same client build")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    arrakis = a.json_root + "/DuneSandbox/Content/Dune/Maps/Arrakis"
    static = static_blocks(arrakis)
    wlc = {}
    for f in glob.glob(arrakis + "/SeaOfSand/WLC_*.json"):
        p = json.load(open(f))[0]["Properties"]
        wlc[os.path.basename(f)[:-5]] = {v["Key"]: v["Value"] for v in p.get("m_Variations", [])}
    summary, assets = {}, set()
    for f in sorted(glob.glob(arrakis + "/DeepDesert_1/Layouts/DA_DeepDesert_1_Layout_*.json")):
        lid = os.path.basename(f)[:-5].split("_")[-1]
        blocks = static + cluster_blocks(json.load(open(f))[0]["Properties"], wlc)
        json.dump(blocks, open(f"{a.out}/terrain-blocks-layout{lid}.json", "w"), indent=0)
        assets |= {b["asset"] for b in blocks}
        summary[lid] = {"total": len(blocks), "static": len(static), "cluster": len(blocks) - len(static)}
    assets = sorted(assets)
    lod1, coll = mesh_maps(assets, a.tb_json, a.file_list)
    json.dump(assets, open(a.out + "/tb-assets.json", "w"), indent=0)
    json.dump(lod1, open(a.out + "/tb-lod1-map.json", "w"), indent=1)
    json.dump(coll, open(a.out + "/tb-collision-map.json", "w"), indent=1)
    json.dump(sorted(set(lod1.values())), open(a.out + "/tb-lod1-manifest.json", "w"), indent=1)
    json.dump(sorted({p for v in coll.values() for p in v}), open(a.out + "/tb-collision-manifest.json", "w"), indent=1)
    json.dump(summary, open(a.out + "/summary.json", "w"), indent=1)
    print(collections.Counter(b["src"] for b in static))
    print(json.dumps(summary))
    print(f"{len(assets)} assets; LOD1 {len(lod1)}, collision {len(coll)}; "
          f"neither: {[x for x in assets if x not in lod1 and x not in coll]}")


if __name__ == "__main__":
    main()
