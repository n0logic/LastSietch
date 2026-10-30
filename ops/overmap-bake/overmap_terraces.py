#!/usr/bin/env python3
"""Terraced overland relief: the game's visual SM_OvermapOutputMesh_* tiles (the stepped sand table the overland
screen draws, about 63.5 cm per terrace) decimated ONE TILE AT A TIME with the gltfpack CLI, then merged and
meshopt-compressed into overmap-terrace.glb. overmap-relief.glb (collision tiles) stays as the fallback.

Memory rule (two agents were OOM-killed on 2026-10-06 rasterising all 6.6M triangles at once in float64): only one
source tile is ever in memory, it is freed before the next, the simplification runs in the gltfpack process, and the
merge only holds the already-decimated tiles. Run it under a cgroup cap (README). See README.md.
"""
import argparse
import gc
import hashlib
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

import overmap_bake as ob

SIMPLIFY_RATIO = 0.06     # gltfpack -si per tile
SIMPLIFY_ERROR = 0.0005   # gltfpack -se per tile: 0.05% of the tile extent, ~25-40 cm here (< the 63 cm step);
                          # 0.0008 (1.6 MB) visibly erases the low steps, 0.0003 is 3.7 MB
POSITION_BITS = 16        # final -vp: 16-bit positions over ~3.1 km = ~5 cm (a terrace step is 63 cm)
TERRACE_STEP_CM = 63.45


def tile_to_out(path, placement):
    """One visual tile -> (float32 positions in output metres, uint32 triangles). Frees the source arrays."""
    p, tri = ob.read_mesh(path)
    w = (p * placement["scale"]) @ placement["rot"].T + placement["loc"]
    if np.prod(np.sign(placement["scale"])) < 0:
        tri = tri[:, [0, 2, 1]]
    out = np.stack([w[:, 0], w[:, 2], w[:, 1]], 1).astype("<f4") / np.float32(100.0)
    del p, w
    return out, tri.astype("<u4")


def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        ob.fail(f"{os.path.basename(cmd[0])} failed: {res.stderr.strip()}")
    return res.stdout + res.stderr


def read_plain(path):
    """Positions + triangles of an unquantized (-noq) gltfpack output, all primitives with their node transforms."""
    gl, binc = ob.glb_buffer_check(path)
    pts, tris, base = [], [], 0
    for node in gl.get("nodes", []):
        if "mesh" not in node:
            continue
        m = np.eye(4)
        if "matrix" in node:
            m = np.array(node["matrix"], float).reshape(4, 4).T
        elif "translation" in node or "scale" in node:
            m[:3, :3] = np.diag(node.get("scale", [1, 1, 1]))
            m[:3, 3] = node.get("translation", [0, 0, 0])
        for prim in gl["meshes"][node["mesh"]]["primitives"]:
            p = ob.accessor(gl, binc, prim["attributes"]["POSITION"]).astype(np.float64)
            t = ob.accessor(gl, binc, prim["indices"]).astype(np.uint32).reshape(-1, 3)
            pts.append((p @ m[:3, :3].T + m[:3, 3]).astype("<f4"))
            tris.append(t + base)
            base += len(p)
    return np.concatenate(pts), np.concatenate(tris)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", required=True, help="DuneExtractor dump root with the overmap level JSON")
    ap.add_argument("--visual-meshes", required=True, help="folder holding SM_OvermapOutputMesh_*.glb")
    ap.add_argument("--gltfpack", required=True)
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", default=ob.DEFAULT_OUT)
    ap.add_argument("--ratio", type=float, default=SIMPLIFY_RATIO)
    ap.add_argument("--error", type=float, default=SIMPLIFY_ERROR)
    ap.add_argument("--keep-dir", help="also keep the merged unquantized terrace GLB here (previews)")
    args = ap.parse_args()

    placed = [p for p in ob.placed_meshes(args.extract) if "OvermapOutputMesh_" in p["mesh"]]
    if len(placed) != 22:
        ob.fail(f"expected 22 placed visual tiles, got {len(placed)}")
    stats, parts_p, parts_t, base = [], [], [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for t in sorted(placed, key=lambda x: x["mesh"]):
            src = os.path.join(args.visual_meshes, t["mesh"] + ".glb")
            if not os.path.isfile(src):
                ob.fail(f"placed visual mesh missing: {t['mesh']}")
            pos, tri = tile_to_out(src, t)
            raw = os.path.join(tmp, "tile.glb")
            ob.write_glb(raw, pos, tri, {"tile": t["mesh"]})
            n_src = len(tri)
            del pos, tri
            gc.collect()
            dec = os.path.join(tmp, "tile-dec.glb")
            run([args.gltfpack, "-i", raw, "-o", dec, "-noq", "-slb", "-si", str(args.ratio), "-se", str(args.error)])
            p, tr = read_plain(dec)
            os.remove(raw)
            os.remove(dec)
            parts_p.append(p)
            parts_t.append(tr + base)
            base += len(p)
            stats.append({"mesh": t["mesh"], "srcTris": int(n_src), "tris": int(len(tr))})
            print(f"  {t['mesh']}: {n_src} -> {len(tr)} tris", flush=True)
            gc.collect()
        pos = np.concatenate(parts_p)
        tri = np.concatenate(parts_t)
        del parts_p, parts_t
        raw = os.path.join(tmp, "terrace-raw.glb")
        extras = {"units": "overmap metres (UE cm / 100)", "axes": "x = X east, y = Z up, z = Y south",
                  "client": args.client, "source": "SM_OvermapOutputMesh_* at their Overmap_Geometry placements"}
        ob.write_glb(raw, pos, tri, extras)
        if args.keep_dir:
            os.makedirs(args.keep_dir, exist_ok=True)
            ob.write_glb(os.path.join(args.keep_dir, "overmap-terrace-plain.glb"), pos, tri, extras)
        out = os.path.join(args.out, "overmap-terrace.glb")
        run([args.gltfpack, "-i", raw, "-o", out, "-c", "-vp", str(POSITION_BITS)])
    gl, _ = ob.glb_buffer_check(out)
    size = os.path.getsize(out)
    src = sum(s["srcTris"] for s in stats)
    print(f"overmap-terrace.glb {size} bytes, {src} -> {len(tri)} tris, {gl.get('extensionsUsed')}")
    with open(out, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    meta = {"file": "overmap-terrace.glb", "bytes": size, "sha256": sha, "tiles": len(stats),
            "srcTris": src, "tris": int(len(tri)), "verts": int(len(pos)),
            "gltfpackPerTile": ["-noq", "-slb", "-si", str(args.ratio), "-se", str(args.error)],
            "gltfpackFinal": ["-c", "-vp", str(POSITION_BITS)], "terraceStepCm": TERRACE_STEP_CM,
            "perTile": stats}
    ob.write_json(os.path.join(args.out, "overmap-terrace.json"), meta)
    ob.register(args.out, ["overmap-terrace.glb", "overmap-terrace.json"], "terrace",
                {"files": ["overmap-terrace.glb", "overmap-terrace.json"], "script": "overmap_terraces.py",
                 "tiles": len(stats), "srcTris": src, "tris": int(len(tri)),
                 "gltfpackPerTile": meta["gltfpackPerTile"], "gltfpackFinal": meta["gltfpackFinal"],
                 "fallback": "overmap-relief.glb"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
