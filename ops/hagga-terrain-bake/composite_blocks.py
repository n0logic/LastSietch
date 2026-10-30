#!/usr/bin/env python3
"""Hagga relief from the game's two terrain layers, and the War-Table outputs.

  layer 1  landscape: the 64 SOS-MinorErg tiles' heightmaps, stitched to 1 m (stitch_landscape.py -> .npy, cm)
  layer 2  terrain blocks: every BP_TerrainBlockActor_C from the regional sublevels (collect_terrain_blocks.py),
           each rasterized at its sublevel transform. Collision meshes where the block has them (denser, the
           walkable shape), else its in-world LOD1 proxy. The landscape is cut flat under blocks, so max() of the
           two layers is the surface.

Outputs in --out (same contract terrain.js reads, plus a normal map):
  hagga.bin    Uint16 LE, --grid x --grid, row 0 = min world Y, u16 = (z - zMin)/(zMax - zMin)*65535
  hagga.json   grid, worldOrigin, worldSpan, zMinCm, zMaxCm, floorCm, heightScale, normalMap, source
  hagga_normal.webp --normal-grid RGB tangent-space normal map of the full-detail composite (lossy q90)
  manifest.json
and --preview-dir/hagga_composite_cm.npy (full working grid, cm) for previews and checks.
"""
import argparse, glob, hashlib, json, math, os, sys, time, importlib.util
import numpy as np
from PIL import Image
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
spec = importlib.util.spec_from_file_location("hb", os.path.join(HERE, "hagga_bake.py"))
hb = importlib.util.module_from_spec(spec); spec.loader.exec_module(hb)
ORIGIN, SPAN = -457200.0, 812800.0


def rot(r):
    p, y, ro = (math.radians((r or {}).get(k, 0.0)) for k in ("Pitch", "Yaw", "Roll"))
    cp, sp, cy, sy, cr, sr = math.cos(p), math.sin(p), math.cos(y), math.sin(y), math.cos(ro), math.sin(ro)
    # UE FRotationMatrix, rows = rotated X, Y, Z axes; world = local @ M
    return np.array([[cp * cy, cp * sy, sp],
                     [sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp],
                     [-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp]])


def splat(B, w, i, G, rng):
    T = ((w[:, :2] - ORIGIN) / SPAN * G)[i]; Z = w[i, 2]
    e1 = T[:, 1] - T[:, 0]; e2 = T[:, 2] - T[:, 0]
    area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2
    k = np.clip((area * 3).astype(int) + 3, 3, 40000); rep = np.repeat(np.arange(len(T)), k)
    r1 = np.sqrt(rng.random(len(rep))); r2 = rng.random(len(rep)); A, Bc, C = 1 - r1, r1 * (1 - r2), r1 * r2
    P = A[:, None] * T[rep, 0] + Bc[:, None] * T[rep, 1] + C[:, None] * T[rep, 2]
    Zs = A * Z[rep, 0] + Bc * Z[rep, 1] + C * Z[rep, 2]
    ix = P[:, 0].astype(int); iy = P[:, 1].astype(int); ok = (ix >= 0) & (ix < G) & (iy >= 0) & (iy < G)
    np.maximum.at(B, (iy[ok], ix[ok]), Zs[ok])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--landscape", required=True, help="stitch_landscape.py output (.npy, cm)")
    ap.add_argument("--blocks", required=True, help="collect_terrain_blocks.py output dir")
    ap.add_argument("--lod1-meshes", required=True); ap.add_argument("--collision-meshes", default="")
    ap.add_argument("--client", required=True)
    ap.add_argument("--work-grid", type=int, default=2048)
    ap.add_argument("--grid", type=int, default=1024, help="shipped height raster")
    ap.add_argument("--normal-grid", type=int, default=2048)
    ap.add_argument("--height-scale", type=float, default=1.0)
    ap.add_argument("--clip-top-m", type=float, default=450.0,
                    help="outer Shield Wall vistas reach ~1 km; clamp so basin relief keeps the Uint16 range")
    ap.add_argument("--out", default=os.path.join(REPO, "admin-backend/portal-nextgen/static/terrain/hagga"))
    ap.add_argument("--preview-dir", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); os.makedirs(a.preview_dir, exist_ok=True)
    G = a.work_grid
    L = np.asarray(Image.fromarray(np.load(a.landscape).astype(np.float32)).resize((G, G), Image.BILINEAR)).astype(np.float64)
    blocks = json.load(open(a.blocks + "/terrain-blocks.json"))
    lod1 = json.load(open(a.blocks + "/tb-lod1-map.json"))
    coll = json.load(open(a.blocks + "/tb-collision-map.json")) if a.collision_meshes else {}
    index = lambda d: {os.path.basename(p)[:-4]: p for p in glob.glob(d + "/**/*.glb", recursive=True)} if d else {}
    g_lod1, g_coll = index(a.lod1_meshes), index(a.collision_meshes)
    B = np.full((G, G), -np.inf); rng = np.random.default_rng(0); cache = {}
    used = {"collision": 0, "lod1": 0, "none": 0}
    for o in blocks:
        t = o["t"]
        if not (ORIGIN - 50000 <= t["X"] < ORIGIN + SPAN + 50000 and ORIGIN - 50000 <= t["Y"] < ORIGIN + SPAN + 50000):
            continue
        names = [os.path.basename(p)[:-7] for p in coll.get(o["asset"], [])]
        if names and all(n in g_coll for n in names):
            src, kind = [g_coll[n] for n in names], "collision"
        elif o["asset"] in lod1 and os.path.basename(lod1[o["asset"]])[:-7] in g_lod1:
            src, kind = [g_lod1[os.path.basename(lod1[o["asset"]])[:-7]]], "lod1"
        else:
            used["none"] += 1; continue
        s = o["s"] or {"X": 1, "Y": 1, "Z": 1}; M = rot(o["r"]); off = np.array([t["X"], t["Y"], t["Z"]])
        for g in src:
            if g not in cache:
                v, i = hb.read_glb(g); cache[g] = (np.stack([v[:, 0], v[:, 2], v[:, 1]], 1) * 100, i)
            v, i = cache[g]
            splat(B, (v * np.array([s["X"], s["Y"], s["Z"]])) @ M + off, i, G, rng)
        used[kind] += 1
    comp = np.maximum(L, np.where(np.isfinite(B), B, -np.inf))
    np.save(os.path.join(a.preview_dir, "hagga_composite_cm.npy"), comp.astype(np.float32))
    comp = np.minimum(comp, a.clip_top_m * 100)
    # shipped height raster
    h = np.asarray(Image.fromarray(comp.astype(np.float32)).resize((a.grid, a.grid), Image.BOX)).astype(np.float64)
    zmin, zmax = float(h.min()), float(h.max())
    u16 = np.round((h - zmin) / (zmax - zmin) * 65535).astype("<u2")
    open(a.out + "/hagga.bin", "wb").write(u16.tobytes())
    # normal map of the full-detail surface (tangent space, +Z up, Y = world +Y rows)
    n = np.asarray(Image.fromarray(comp.astype(np.float32)).resize((a.normal_grid, a.normal_grid), Image.BOX)).astype(np.float64)
    cell = SPAN / a.normal_grid
    gy, gx = np.gradient(n, cell)
    nz = 1.0 / np.sqrt(1 + gx * gx + gy * gy)
    rgb = np.stack([(-gx * nz + 1) / 2, (-gy * nz + 1) / 2, (nz + 1) / 2], -1)
    Image.fromarray(np.round(rgb * 255).astype("uint8")).save(a.out + "/hagga_normal.webp", quality=90, method=6)
    stamp = int(time.time() * 1000); sha = hashlib.sha256(u16.tobytes()).hexdigest()
    meta = {"grid": [a.grid, a.grid], "worldOrigin": [ORIGIN, ORIGIN], "worldSpan": [SPAN, SPAN],
            "zMinCm": round(zmin, 2), "zMaxCm": round(zmax, 2), "floorCm": round(zmin, 2),
            "heightScale": a.height_scale, "normalMap": "hagga_normal.webp", "normalGrid": a.normal_grid,
            "source": {"client": a.client, "layers": ["landscape SOS-MinorErg x0..7 y0..7",
                                                      "terrain blocks from SOC_1 regional sublevels"],
                       "blocks": used, "workGrid": G, "clipTopM": a.clip_top_m}}
    json.dump(meta, open(a.out + "/hagga.json", "w"), indent=2)
    json.dump({"bakeStamp": stamp, "map": "hagga", "raster": [a.grid, a.grid], "heightScale": a.height_scale,
               "layouts": [{"id": 0, "bin": "hagga.bin", "meta": "hagga.json", "sha": sha}]},
              open(a.out + "/manifest.json", "w"), indent=1)
    print(f"blocks {used}; composite z {comp.min()/100:.0f}..{comp.max()/100:.0f} m; raster {a.grid}^2 sha {sha[:12]}; "
          f"bin {os.path.getsize(a.out + '/hagga.bin')/1e6:.1f} MB, normal {os.path.getsize(a.out + '/hagga_normal.webp')/1e6:.1f} MB")


if __name__ == "__main__":
    main()
