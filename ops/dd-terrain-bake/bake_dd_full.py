#!/usr/bin/env python3
"""Full-detail Deep Desert War-Table relief, one bake per Coriolis layout (portal 2.26.0).

Two layers, the same model as Hagga (ops/hagga-terrain-bake/composite_blocks.py):
  ground  the SoS landscape tiles at 1 m (dd_ground.py), box-averaged to --ground-px-m, with the authored
          +150 m Shield Wall tier (rows 21-23) kept
  rocks   every terrain block of the layout: its WLC cluster blocks plus the 497 static ShieldWall / GapFix /
          Travel blocks (collect_dd_blocks.py), collision meshes first, else the LOD1 proxy, max()-splatted
composite = max(ground, rocks). The ground is smoothed but not cut flat under the rock, so max() is the surface.
The Outer Shield Wall vistas (row 23) reach ~1.76 km and are clamped at --clip-top-m.

Shipped layout N = DA_DeepDesert_1_Layout_0N (the Cartography UI blueprint N matches DA 0N, Jaccard 0.98+).

Outputs in --out (contract read by portal-nextgen/src/lib/map/holo/terrain.js):
  layout_N.bin         Uint16 LE, --grid^2, row 0 = min world Y, u16 = (z - zMin)/(zMax - zMin)*65535
  layout_N.json        grid, worldOrigin, worldSpan, zMinCm, zMaxCm, floorCm, heightScale, normalMap, normalGrid, source
  layout_N_normal.webp --normal-grid RGB tangent-space normal map (R=-dh/dx, G=-dh/dy, B=nz; rows = world +Y), q90
  layout_N_drape.webp  --drape-grid RGB unshaded map art (dd_drape.py: textured LOD1 rock over Hagga-palette sand
                       toned by the landscape paint layers), row 0 = min world Y, q88. The client lights it.
  manifest.json        rebuilt from every layout_N.bin present; fingerprints.json is never touched
Previews (hillshades, optional composite .npy) go to --preview-dir, outside static/.
"""
import argparse, glob, hashlib, importlib.util, json, math, os, sys, time
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import dd_ground  # noqa: E402
import dd_drape  # noqa: E402

spec = importlib.util.spec_from_file_location("hb", os.path.join(REPO, "ops/hagga-terrain-bake/hagga_bake.py"))
hb = importlib.util.module_from_spec(spec); spec.loader.exec_module(hb)

# Frozen DD calibration: admin-backend/map_model.py deep-desert cal (markers align to it).
ORIGIN, SPAN = -1270000.0, 2438400.0
RE = os.path.expanduser("~/dd-re")
EXTRACTED = os.path.expanduser(os.environ.get("DUNE_EXTRACT_DIR", "~/dune-extract") + "")
STONE_RGB = (150, 128, 104)   # ShieldWall-region ground tint (stone shelf, not sand)


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


class Rocks:
    """Rasterizes terrain blocks onto the DD frame. Meshes: collision set if every piece exported, else LOD1."""

    def __init__(self, blocks_dir, mesh_dirs, G):
        self.G = G
        self.lod1 = json.load(open(blocks_dir + "/tb-lod1-map.json"))
        self.coll = json.load(open(blocks_dir + "/tb-collision-map.json"))
        self.glb = {}
        for d in mesh_dirs:
            for p in glob.glob(d + "/**/*.glb", recursive=True):
                self.glb.setdefault(os.path.basename(p)[:-4], p)
        self.cache = {}

    def meshes(self, asset):
        names = [os.path.basename(p)[:-7] for p in self.coll.get(asset, [])]
        if names and all(n in self.glb for n in names):
            return [self.glb[n] for n in names], "collision"
        l1 = os.path.basename(self.lod1.get(asset, ""))[:-7]
        if l1 in self.glb:
            return [self.glb[l1]], "lod1"
        return [], "none"

    def raster(self, blocks):
        G = self.G
        B = np.full((G, G), -np.inf); rng = np.random.default_rng(0)
        used = {"collision": 0, "lod1": 0, "none": 0, "outside": 0}; none_assets = set()
        for o in blocks:
            t = o["t"]
            # vista blocks more than 1 km outside the frame cannot reach it
            if not (ORIGIN - 100000 <= t["X"] < ORIGIN + SPAN + 100000 and ORIGIN - 100000 <= t["Y"] < ORIGIN + SPAN + 100000):
                used["outside"] += 1; continue
            src, kind = self.meshes(o["asset"])
            used[kind] += 1
            if not src:
                none_assets.add(o["asset"]); continue
            s = o["s"] or {"X": 1, "Y": 1, "Z": 1}; M = rot(o["r"]); off = np.array([t["X"], t["Y"], t["Z"]])
            for p in src:
                if p not in self.cache:
                    v, i = hb.read_glb(p); self.cache[p] = (np.stack([v[:, 0], v[:, 2], v[:, 1]], 1) * 100, i)
                v, i = self.cache[p]
                splat(B, (v * np.array([s["X"], s["Y"], s["Z"]])) @ M + off, i, G, rng)
        return B.astype(np.float32), used, sorted(none_assets)


def static_rocks(rocks, static, cache_dir):
    """The 497 static blocks are identical in every layout: rasterize once per (blocks, grid, meshes)."""
    key = hashlib.sha256(json.dumps([static, rocks.G, sorted(rocks.glb.items())], sort_keys=True).encode()).hexdigest()[:16]
    p = os.path.join(cache_dir, f"static_rocks_{rocks.G}_{key}.npz")
    if os.path.exists(p):
        z = np.load(p, allow_pickle=False)
        return z["B"], json.loads(str(z["used"])), json.loads(str(z["none"]))
    B, used, none = rocks.raster(static)
    np.savez(p, B=B, used=json.dumps(used), none=json.dumps(none))
    return B, used, none


class Drape:
    """Per-run state for the map-art drape: palette, textures, LOD1 lookup, static-block colour cache."""

    def __init__(self, a, rocks):
        self.a = a
        self.palette = dd_drape.hagga_sand_palette(a.hagga_art)
        dirs = [d for d in a.mesh_dirs.split(",") if d]
        self.textures = dd_drape.Textures(dirs)
        self.lod1 = json.load(open(a.blocks + "/tb-lod1-map.json"))
        self.glb = rocks.glb

    def lod1_glb(self, asset):
        return self.glb.get(os.path.basename(self.lod1.get(asset, ""))[:-7])

    def static_colour(self, static):
        G = self.a.drape_grid
        key = hashlib.sha256(json.dumps([static, G, sorted(self.glb.items()), sorted(self.textures.png.items())],
                                        sort_keys=True).encode()).hexdigest()[:16]
        p = os.path.join(self.a.cache, f"static_colour_{G}_{key}.npz")
        if os.path.exists(p):
            z = np.load(p, allow_pickle=False)
            return z["Z"], z["C"], json.loads(str(z["used"]))
        Z, C, used = dd_drape.rock_colour(static, self.lod1_glb, self.textures, G, ORIGIN, SPAN)
        np.savez(p, Z=Z, C=C, used=json.dumps(used))
        return Z, C, used

    def bake(self, lid, ground_src, arrakis, static, cluster, ground, R):
        a, G = self.a, self.a.drape_grid
        SZ, SC, s_used = self.static_colour(static)
        CZ, CC, c_used = dd_drape.rock_colour(cluster, self.lod1_glb, self.textures, G, ORIGIN, SPAN)
        top = CZ > SZ
        Z = np.where(top, CZ, SZ); C = np.where(top[..., None], CC, SC)
        rock = np.isfinite(R)
        vis = np.asarray(Image.fromarray((rock & (R >= ground)).astype(np.float32)).resize((G, G), Image.BOX)) > 0.5
        W = dd_ground.assemble_weights(ground_src, arrakis, lid, a.ground_px_m)
        W = np.stack([np.asarray(Image.fromarray(w).resize((G, G), Image.BILINEAR)) for w in W])
        reg = np.asarray(Image.fromarray(dd_ground.region_map(arrakis).astype(np.float32)).resize((G, G), Image.BILINEAR))
        sand = dd_drape.sand_albedo(W, self.palette, reg, STONE_RGB)
        img = dd_drape.compose(sand, C, Z, vis)
        Image.fromarray(img).save(f"{a.out}/layout_{lid}_drape.webp", quality=a.drape_quality, method=6)
        # phone / weak-tier sibling (owner 2026-10-06): terrain.js picks meta.drapeSmall on small tiers
        Image.fromarray(img).resize((2048, 2048), Image.LANCZOS).save(f"{a.out}/layout_{lid}_drape-2048.webp", quality=86, method=6)
        used = {k: s_used[k] + c_used[k] for k in s_used}
        return used, float(vis.mean()), float((np.isfinite(Z) & vis).sum() / max(vis.sum(), 1))


def hillshade(H, px_m, az=315, alt=45):
    gy, gx = np.gradient(H / 100.0, px_m)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    a, z = np.radians(360 - az + 90), np.radians(alt)
    s = np.sin(z) * np.sin(slope) + np.cos(z) * np.cos(slope) * np.cos(a - aspect)
    return (np.clip(s, 0, 1) * 255).astype(np.uint8)


def resize(a, n):
    return np.asarray(Image.fromarray(a.astype(np.float32)).resize((n, n), Image.BOX)).astype(np.float64)


def bake_layout(lid, a, ground_src, rocks, static, arrakis, drape):
    t0 = time.time()
    ground, tiles = dd_ground.assemble(ground_src, arrakis, lid, a.ground_px_m)
    if ground.shape != (rocks.G, rocks.G):
        sys.exit(f"FAIL: ground {ground.shape} vs rock grid {rocks.G}")
    blocks = json.load(open(f"{a.blocks}/terrain-blocks-layout{lid:02d}.json"))
    n_static = sum(1 for b in blocks if not b["src"].startswith("WLC_"))
    if n_static != len(static):
        sys.exit(f"FAIL: layout {lid} has {n_static} static blocks, expected {len(static)}")
    SB, s_used, s_none = static_rocks(rocks, static, a.cache)
    CB, c_used, c_none = rocks.raster([b for b in blocks if b["src"].startswith("WLC_")])
    R = np.maximum(SB, CB)
    rock = np.isfinite(R)
    comp = np.maximum(ground.astype(np.float64), np.where(rock, R, -np.inf))
    raw_max = float(comp.max())
    if a.save_npy:
        np.save(os.path.join(a.preview_dir, f"layout_{lid}_composite_cm.npy"), comp.astype(np.float32))
    comp = np.minimum(comp, a.clip_top_m * 100)

    h = resize(comp, a.grid)
    zmin, zmax = float(h.min()), float(h.max())
    u16 = np.round((h - zmin) / (zmax - zmin) * 65535).astype("<u2")
    open(f"{a.out}/layout_{lid}.bin", "wb").write(u16.tobytes())

    n = resize(comp, a.normal_grid)
    gy, gx = np.gradient(n, SPAN / a.normal_grid)
    nz = 1.0 / np.sqrt(1 + gx * gx + gy * gy)
    rgb = np.stack([(-gx * nz + 1) / 2, (-gy * nz + 1) / 2, (nz + 1) / 2], -1)
    Image.fromarray(np.round(rgb * 255).astype("uint8")).save(f"{a.out}/layout_{lid}_normal.webp", quality=90, method=6)

    d_used, d_vis, d_coloured = drape.bake(lid, ground_src, arrakis, static,
                                           [b for b in blocks if b["src"].startswith("WLC_")], ground.astype(np.float64), R)

    blocks_stat = {"static": len(static), "cluster": len(blocks) - len(static),
                   "collision": s_used["collision"] + c_used["collision"], "lod1": s_used["lod1"] + c_used["lod1"],
                   "noArt": s_used["none"] + c_used["none"], "outsideFrame": s_used["outside"] + c_used["outside"]}
    meta = {"grid": [a.grid, a.grid], "worldOrigin": [ORIGIN, ORIGIN], "worldSpan": [SPAN, SPAN],
            "zMinCm": round(zmin, 2), "zMaxCm": round(zmax, 2), "floorCm": round(zmin, 2),
            "heightScale": a.height_scale, "normalMap": f"layout_{lid}_normal.webp", "normalGrid": a.normal_grid,
            "drape": f"layout_{lid}_drape.webp", "drapeSmall": f"layout_{lid}_drape-2048.webp", "drapeGain": a.drape_gain,
            "source": {"client": a.client, "daLayout": f"DA_DeepDesert_1_Layout_{lid:02d}",
                       "layers": ["SoS landscape tiles (DA override else DunesMid02 / DunesLow04 preset), +150 m ShieldWall tier",
                                  "terrain blocks: WLC cluster variations + DeepDesert_1 static sublevels"],
                       "uniqueTiles": len(set(tiles.values())), "blocks": blocks_stat,
                       "noArtAssets": sorted(set(s_none) | set(c_none)), "groundPxM": a.ground_px_m,
                       "workGrid": rocks.G, "clipTopM": a.clip_top_m,
                       "drape": {"grid": a.drape_grid, "rockLod1": d_used, "rockVisibleFrac": round(d_vis, 4),
                                 "rockColouredFrac": round(d_coloured, 4),
                                 "sandPalette": [[round(float(x)) for x in c] for c in drape.palette]}}}
    json.dump(meta, open(f"{a.out}/layout_{lid}.json", "w"), indent=2)

    hs = hillshade(comp, SPAN / rocks.G / 100)
    Image.fromarray(hs).resize((2032, 2032), Image.LANCZOS).save(f"{a.preview_dir}/layout_{lid}_hillshade.png", optimize=True)
    Image.fromarray(hillshade((u16 / 65535.0) * (zmax - zmin) + zmin, SPAN / a.grid / 100)).save(
        f"{a.preview_dir}/layout_{lid}_{a.grid}_hillshade.png", optimize=True)
    stats = {"id": lid, "groundM": [round(float(ground.min()) / 100, 1), round(float(ground.max()) / 100, 1)],
             "compositeRawMaxM": round(raw_max / 100, 1), "zM": [round(zmin / 100, 1), round(zmax / 100, 1)],
             "rockFrac": round(float(rock.mean()), 4),
             "clippedFrac": round(float((comp >= a.clip_top_m * 100).mean()), 5),
             "basinMaxM": round(float(comp[:20 * rocks.G // 24].max()) / 100, 1),   # rows 0-19, north of the wall
             "blocks": blocks_stat, "sha": hashlib.sha256(u16.tobytes()).hexdigest(),
             "binBytes": os.path.getsize(f"{a.out}/layout_{lid}.bin"),
             "normalBytes": os.path.getsize(f"{a.out}/layout_{lid}_normal.webp"),
             "drapeBytes": os.path.getsize(f"{a.out}/layout_{lid}_drape.webp"), "drapeRock": d_used,
             "rockColouredFrac": round(d_coloured, 4), "seconds": round(time.time() - t0, 1)}
    print(json.dumps(stats), flush=True)
    return stats


def write_manifest(a):
    entries, grids = [], set()
    for lid in range(12):
        b = f"{a.out}/layout_{lid}.bin"
        if not os.path.exists(b):
            continue
        meta = json.load(open(f"{a.out}/layout_{lid}.json"))
        grids.add(tuple(meta["grid"]))
        entries.append({"id": lid, "bin": f"layout_{lid}.bin", "meta": f"layout_{lid}.json",
                        "sha": hashlib.sha256(open(b, "rb").read()).hexdigest()})
    if len(grids) != 1:
        print(f"WARN: manifest mixes raster grids {sorted(grids)}; bake every layout before shipping")
    man = {"bakeStamp": a.stamp or int(time.time() * 1000), "raster": list(max(grids)), "heightScale": a.height_scale,
           "fingerprints": "fingerprints.json", "source": {"client": a.client, "clipTopM": a.clip_top_m},
           "layouts": entries}
    json.dump(man, open(f"{a.out}/manifest.json", "w"), indent=1)
    print(f"manifest: {len(entries)} layouts, bakeStamp {man['bakeStamp']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layouts", default="0-11", help="e.g. 0, 0,6 or 0-11 (shipped ids; DA layout 0N)")
    ap.add_argument("--client", required=True, help="client build the inputs were extracted from")
    ap.add_argument("--json-root", default=RE + "/rocks/json", help="jsonlist root with Maps/Arrakis/DeepDesert_1 + SeaOfSand")
    ap.add_argument("--tile-json", default=RE + "/ground/tilejson", help="jsonlist of the SoS tile .umaps")
    ap.add_argument("--hm", default=RE + "/ground/hm", help="DuneExtractor landscape PNG root")
    ap.add_argument("--raw", default=RE + "/ground/raw," + RE + "/drape/raw-sos",
                    help="DuneExtractor raw roots, comma-separated (tile .ubulk: base-preset heightmaps, all weightmaps)")
    ap.add_argument("--blocks", default=RE + "/rocks/blocks", help="collect_dd_blocks.py output")
    ap.add_argument("--mesh-dirs", default=",".join([RE + "/rocks/meshes-collision", RE + "/rocks/meshes-lod1",
                                                     EXTRACTED + "/cb-placements/meshes-collision",
                                                     EXTRACTED + "/cb-placements/meshes", RE + "/drape/lod1-extra"]))
    ap.add_argument("--cache", default=RE + "/bake-cache", help="decoded 1 m tiles + static rock raster")
    ap.add_argument("--ground-px-m", type=int, default=4, help="work grid metres per pixel (divides 1016)")
    ap.add_argument("--grid", type=int, default=1024, help="shipped height raster")
    ap.add_argument("--normal-grid", type=int, default=2048)
    ap.add_argument("--height-scale", type=float, default=1.0)
    ap.add_argument("--clip-top-m", type=float, default=600.0,
                    help="basin rock tops ~200 m, the Shield Wall face (rows 21-22) ~600 m, the row-23 vistas up to "
                         "1.76 km; 600 keeps the wall face and flattens only the vista strip (u16 step ~1 cm)")
    ap.add_argument("--drape-grid", type=int, default=4096, help="map-art drape size (~6 m/px)")
    ap.add_argument("--drape-quality", type=int, default=88)
    ap.add_argument("--drape-gain", type=float, default=1.0, help="meta.drapeGain (shader exposure)")
    ap.add_argument("--hagga-art", default=os.path.join(REPO, "website/assets/HaggaBasin.webp"),
                    help="in-game Hagga map art; the sand palette is sampled from its southern dunes")
    ap.add_argument("--stamp", type=int, default=0, help="manifest bakeStamp (ms); 0 = now")
    ap.add_argument("--out", default=os.path.join(REPO, "admin-backend/portal-nextgen/static/terrain"))
    ap.add_argument("--preview-dir", default=RE + "/bake-previews")
    ap.add_argument("--save-npy", action="store_true", help="also save the unclipped work-grid composite (150 MB each)")
    a = ap.parse_args()
    ids = []
    for part in a.layouts.split(","):
        lo, _, hi = part.partition("-")
        ids += list(range(int(lo), int(hi or lo) + 1))
    if any(i < 0 or i > 11 for i in ids):
        sys.exit("FAIL: shipped layouts are 0..11")
    for d in (a.out, a.preview_dir, a.cache):
        os.makedirs(d, exist_ok=True)
    arrakis = a.json_root + "/DuneSandbox/Content/Dune/Maps/Arrakis"
    G = 24 * (dd_ground.TILE_M // a.ground_px_m)
    ground_src = dd_ground.TileSource(a.tile_json, a.hm, [r for r in a.raw.split(",") if r], os.path.join(a.cache, "tiles"))
    rocks = Rocks(a.blocks, [d for d in a.mesh_dirs.split(",") if d], G)
    drape = Drape(a, rocks)
    static = [b for b in json.load(open(f"{a.blocks}/terrain-blocks-layout00.json")) if not b["src"].startswith("WLC_")]
    for lid in ids:
        bake_layout(lid, a, ground_src, rocks, static, arrakis, drape)
    write_manifest(a)


if __name__ == "__main__":
    main()
