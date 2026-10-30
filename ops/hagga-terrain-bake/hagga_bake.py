#!/usr/bin/env python3
"""Bake the Hagga Basin War-Table heightfield from the game's own UI-map meshes.

Source: Cartography/Survival_1/BP_WorldMap_Meshes_Survival_5, extracted by DuneExtractor into --src
(meshes/**.glb + hagga-placements.json from hagga_placements.py). See README.md for the full run.
Terrain = ground tiles (SM_LandscapeStreamingProxy_MinorErg_*), vistas (SOS) and rock formations
(SM_*CB*_UIMap). Wreck pieces are props, not terrain, and are excluded (same rule as the DD bake).

Output (same contract as the DD holo rasters, read by portal-nextgen/src/lib/map/holo/terrain.js):
  <out>/manifest.json   layouts: [{id: 0, bin: hagga.bin, meta: hagga.json, sha}]
  <out>/hagga.bin       Uint16 LE, grid W x H, row 0 = min world Y, u16 = (z - zMin)/(zMax - zMin)*65535
  <out>/hagga.json      grid, worldOrigin, worldSpan, zMinCm, zMaxCm, floorCm, heightScale
  <preview-dir>/preview_*.png   hillshade + overlay on the live backdrop (never under static/)
Calibration = live HAGGA_CAL in admin-backend/map_model.py: origin -457200 cm, span 812800 cm, flipY False.
"""
import argparse, glob, hashlib, json, os, struct, sys, time
import numpy as np
from PIL import Image
from scipy import ndimage

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ORIGIN, SPAN = -457200.0, 812800.0


def read_glb(path):
    f = open(path, "rb").read(); off, ch = 12, []
    while off < len(f):
        cl, ct = struct.unpack("<II", f[off:off + 8]); ch.append(f[off + 8:off + 8 + cl]); off += 8 + cl
    gl = json.loads(ch[0]); b = ch[1]

    def acc(i, dt, nc):
        a = gl["accessors"][i]; bv = gl["bufferViews"][a["bufferView"]]
        o = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        isz = np.dtype(dt).itemsize; st = bv.get("byteStride", isz * nc)
        if st == isz * nc:
            r = np.frombuffer(b, dtype=dt, count=a["count"] * nc, offset=o)
        else:
            need = st * (a["count"] - 1) + isz * nc
            raw = np.frombuffer(b, dtype=np.uint8, count=min(need, len(b) - o), offset=o)
            if len(raw) < need:   # interleaved view whose last element ends the buffer
                raw = np.concatenate([raw, np.zeros(need - len(raw), np.uint8)])
            r = np.lib.stride_tricks.as_strided(raw, shape=(a["count"], isz * nc), strides=(st, 1)).copy().view(dt)
        return r.reshape(-1, nc) if nc > 1 else r.reshape(-1)

    V, I, base = [], [], 0
    for m in gl["meshes"]:
        for p in m["primitives"]:
            v = acc(p["attributes"]["POSITION"], "<f4", 3).astype("f8")
            ia = gl["accessors"][p["indices"]]
            dt = {5125: "<u4", 5123: "<u2", 5121: "u1"}[ia["componentType"]]
            I.append(acc(p["indices"], dt, 1).astype(int).reshape(-1, 3) + base); V.append(v); base += len(v)
    return np.concatenate(V), np.concatenate(I)


def quat(q):
    x, y, z, w = q["X"], q["Y"], q["Z"], q["W"]
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def is_terrain(mesh):
    return "LandscapeStreamingProxy" in mesh or "SOS" in mesh or "SoS" in mesh or "_CB_" in mesh


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="extraction dir with meshes/ and hagga-placements.json")
    ap.add_argument("--client", required=True, help="client build id the extraction came from")
    ap.add_argument("--out", default=os.path.join(REPO, "admin-backend/portal-nextgen/static/terrain/hagga"))
    ap.add_argument("--preview-dir", required=True, help="where previews go (keep it outside static/)")
    ap.add_argument("--tier", choices=["off", "const", "field"], default="off",
                    help="north raised-tier treatment: off = game geometry as is; const = lower each raised "
                         "tile by a constant; field = lower by a smooth offset field (no straight steps)")
    ap.add_argument("--soften-cells", type=int, default=0,
                    help="soften the straight north cliff: blend cells within this many of a >= --soften-step-m step toward a wider blur")
    ap.add_argument("--soften-step-m", type=float, default=30.0)
    ap.add_argument("--tier-min-m", type=float, default=50.0, help="a ground tile is in the raised tier when its p5 height is at least this")
    ap.add_argument("--grid", type=int, default=384)
    ap.add_argument("--super", type=int, default=4, help="supersample factor for rasterization")
    ap.add_argument("--height-scale", type=float, default=1.0)
    ap.add_argument("--smooth-sigma", type=float, default=1.5)
    ap.add_argument("--clip-top-pct", type=float, default=99.8)
    ap.add_argument("--seam-match", action="store_true", help="opt-in: shift north tiles to agree at seams (leaves vertical seams; off by default)")
    ap.add_argument("--feather-cells", type=int, default=0, help="opt-in seam feather (draws box outlines at 3; off by default)")
    ap.add_argument("--seam-min-m", type=float, default=40.0, help="only correct tiles whose seam step is at least this")
    ap.add_argument("--backdrop", default=os.path.join(REPO, "website/assets/HaggaBasin.webp"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True); os.makedirs(a.preview_dir, exist_ok=True)
    if os.path.abspath(a.preview_dir).startswith(os.path.join(REPO, "admin-backend")):
        sys.exit("FAIL: --preview-dir must not be inside the shipped tree")
    G = a.grid; N = G * a.super
    glbs = {os.path.basename(p)[:-4]: p for p in glob.glob(a.src + "/meshes/**/*.glb", recursive=True)}
    place = [p for p in json.load(open(a.src + "/hagga-placements.json")) if is_terrain(p["mesh"])]
    missing = sorted({p["mesh"] for p in place if p["mesh"] not in glbs})
    if missing:
        sys.exit(f"FAIL: {len(missing)} placed terrain meshes have no GLB, e.g. {missing[:3]}")
    # --- seam matching: per ground tile vertical offset so shared edges agree (BFS from the south row)
    TILE = 101600.0
    tiles = {}
    for p in place:
        if "LandscapeStreamingProxy" not in p["mesh"]:
            continue
        v, _ = read_glb(glbs[p["mesh"]])
        w = np.stack([v[:, 0], v[:, 2], v[:, 1]], 1) * 100 + np.array([p["t"]["X"], p["t"]["Y"], p["t"].get("Z", 0.0)])
        tiles[(int((p["t"]["X"] - ORIGIN) // TILE), int((p["t"]["Y"] - ORIGIN) // TILE))] = w
    band = TILE * 0.04

    def edge(w, side):
        x0 = w[:, 0].min(); y0 = w[:, 1].min()
        sel = {"W": w[:, 0] < x0 + band, "E": w[:, 0] > x0 + TILE - band,
               "S": w[:, 1] < y0 + band, "N": w[:, 1] > y0 + TILE - band}[side]
        return np.median(w[sel, 2]) if sel.any() else None

    nb = {"E": (1, 0, "W"), "W": (-1, 0, "E"), "N": (0, 1, "S"), "S": (0, -1, "N")}
    gmax = max(k[1] for k in tiles)
    offset = {}; queue = [k for k in tiles if k[1] == gmax]
    for k in queue:
        offset[k] = 0.0
    while queue:
        k = queue.pop(0)
        for side, (dx, dy, opp) in nb.items():
            n = (k[0] + dx, k[1] + dy)
            if n not in tiles or n in offset:
                continue
            diffs = []
            for s2, (ddx, ddy, o2) in nb.items():
                f = (n[0] + ddx, n[1] + ddy)
                if f in offset:
                    a_ = edge(tiles[n], s2); b_ = edge(tiles[f], o2)
                    if a_ is not None and b_ is not None:
                        diffs.append((b_ + offset[f]) - a_)
            offset[n] = float(np.median(diffs)) if diffs else 0.0
            queue.append(n)
    offset = {k: (o if abs(o) >= a.seam_min_m * 100 else 0.0) for k, o in offset.items()}
    # never lower a tile's own ground below the basin floor: clamp by the tile's 5th-percentile height
    base = {k: float(np.percentile(w[:, 2], 5)) for k, w in tiles.items()}
    offset = {k: (max(o, -max(base[k], 0.0)) if o < 0 else o) for k, o in offset.items()}
    if not a.seam_match:
        offset = {k: 0.0 for k in tiles}
    gy = max(k[1] for k in tiles) + 1; gx = max(k[0] for k in tiles) + 1
    om = np.zeros((gy, gx))
    for (cx, cy), o in offset.items():
        om[cy, cx] = o / 100
    print("seam offsets (m), rows = world Y ascending:"); print(np.round(om).astype(int))

    def tile_offset(x, y):
        return offset.get((int((x - ORIGIN) // TILE), int((y - ORIGIN) // TILE)), 0.0)

    hm = np.full((N, N), -np.inf); rng = np.random.default_rng(0); cache = {}
    for p in place:
        if p["mesh"] not in cache:
            v, i = read_glb(glbs[p["mesh"]]); cache[p["mesh"]] = (np.stack([v[:, 0], v[:, 2], v[:, 1]], 1) * 100, i)
        v, i = cache[p["mesh"]]; s = p["s"] or {"X": 1, "Y": 1, "Z": 1}; cs = p["comp_scale"]
        w = (v * np.array([s["X"] * cs["X"], s["Y"] * cs["Y"], s["Z"] * cs["Z"]])) @ quat(p["q"]).T \
            + np.array([p["t"]["X"], p["t"]["Y"], p["t"].get("Z", 0.0)])
        if "LandscapeStreamingProxy" in p["mesh"]:
            w[:, 2] += tile_offset(p["t"]["X"] + 1, p["t"]["Y"] + 1)
        else:
            w[:, 2] += tile_offset(p["t"]["X"], p["t"]["Y"])
        T = ((w[:, :2] - ORIGIN) / SPAN * N)[i]; Z = w[i, 2]
        if T[..., 0].max() < 0 or T[..., 0].min() > N or T[..., 1].max() < 0 or T[..., 1].min() > N:
            continue
        e1 = T[:, 1] - T[:, 0]; e2 = T[:, 2] - T[:, 0]; area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2
        k = np.clip((area * 2).astype(int) + 3, 3, 20000); rep = np.repeat(np.arange(len(T)), k)
        r1 = np.sqrt(rng.random(len(rep))); r2 = rng.random(len(rep)); A, B, C = 1 - r1, r1 * (1 - r2), r1 * r2
        P = A[:, None] * T[rep, 0] + B[:, None] * T[rep, 1] + C[:, None] * T[rep, 2]
        Zs = A * Z[rep, 0] + B * Z[rep, 1] + C * Z[rep, 2]
        ix = P[:, 0].astype(int); iy = P[:, 1].astype(int); ok = (ix >= 0) & (ix < N) & (iy >= 0) & (iy < N)
        np.maximum.at(hm, (iy[ok], ix[ok]), Zs[ok])
    covered = np.isfinite(hm); cov_hi = covered.mean()
    # fill holes from the nearest covered cell, then box-average down to the grid
    idx = ndimage.distance_transform_edt(~covered, return_distances=False, return_indices=True)
    hm = hm[idx[0], idx[1]]
    h = hm.reshape(G, a.super, G, a.super).mean(axis=(1, 3))
    # north raised tier: the game's LOD1 ground tiles along the north edge carry a 50-150 m base offset
    # with tile-aligned edges. Lower each raised tile by (its p5 - the basin floor p5); each tile keeps its
    # own mesh relief, so the northern cliffs still stand (same idea as the DD --keep-elevated default).
    cells = G / round(SPAN / TILE)
    p5 = {k: float(np.percentile(w[:, 2], 5)) for k, w in tiles.items()}
    floor = float(np.median([v for v in p5.values() if v < a.tier_min_m * 100]))
    raised = {k: v - floor for k, v in p5.items() if v >= a.tier_min_m * 100}
    if a.tier != "off" and raised:
        off = np.zeros((G, G))
        for (cx, cy), o in raised.items():
            off[int(cy * cells):int((cy + 1) * cells), int(cx * cells):int((cx + 1) * cells)] = o
        if a.tier == "field":
            off = ndimage.gaussian_filter(off, cells / 2, mode="nearest")
        h = h - off
    print(f"tier {a.tier}: {len(raised)} raised tiles (p5 >= {a.tier_min_m:.0f} m), floor {floor/100:.1f} m, "
          f"offsets m {sorted(round(v/100) for v in raised.values())}")
    if a.smooth_sigma > 0:
        h = ndimage.gaussian_filter(h, a.smooth_sigma)
    # seam feather: the game's LOD1 ground tiles disagree at seams in the corrected (north) tiles.
    # Blend a narrow band along those tiles' boundaries toward a wider blur; nothing else changes.
    fixed = [k for k, o in offset.items() if o != 0.0]
    if fixed and a.feather_cells > 0:
        band = np.zeros((G, G), bool); r = a.feather_cells
        for (cx, cy) in fixed:
            x0, x1 = int(cx * cells), int((cx + 1) * cells); y0, y1 = int(cy * cells), int((cy + 1) * cells)
            for xb in (x0, x1):
                band[max(0, y0 - r):min(G, y1 + r), max(0, xb - r):min(G, xb + r)] = True
            for yb in (y0, y1):
                band[max(0, yb - r):min(G, yb + r), max(0, x0 - r):min(G, x1 + r)] = True
        wide = ndimage.gaussian_filter(h, a.feather_cells * 1.5)
        wgt = ndimage.gaussian_filter(band.astype(float), 1.0)
        h = h * (1 - wgt) + wide * wgt
        print(f"seam feather: {band.mean():.1%} of cells in band, {len(fixed)} corrected tiles")
    # the north raised ground is real (the game's 2D map shows it); only its LOD1 edge is ruler-straight.
    # Soften just the cells along steep steps; the plateau height and everything else stay as the game has it.
    if a.soften_cells > 0:
        st = np.zeros_like(h, bool)
        st[1:, :] |= np.abs(np.diff(h, axis=0)) >= a.soften_step_m * 100
        st[:, 1:] |= np.abs(np.diff(h, axis=1)) >= a.soften_step_m * 100
        band = ndimage.binary_dilation(st, iterations=a.soften_cells)
        wgt = ndimage.gaussian_filter(band.astype(float), a.soften_cells / 2)
        h = h * (1 - wgt) + ndimage.gaussian_filter(h, a.soften_cells) * wgt
        print(f"soften: {st.sum()} step cells, band {band.mean():.1%} of the raster")
    zmin = float(np.percentile(h, 0.1)); zmax = float(np.percentile(h, a.clip_top_pct))
    h = np.clip(h, zmin, zmax)
    u16 = np.round((h - zmin) / (zmax - zmin) * 65535).astype("<u2")
    stamp = int(time.time() * 1000)
    open(a.out + "/hagga.bin", "wb").write(u16.tobytes())
    meta = {"grid": [G, G], "worldOrigin": [ORIGIN, ORIGIN], "worldSpan": [SPAN, SPAN],
            "zMinCm": round(zmin, 2), "zMaxCm": round(zmax, 2), "floorCm": round(zmin, 2),
            "heightScale": a.height_scale,
            "source": {"client": a.client, "asset": "Cartography/Survival_1/BP_WorldMap_Meshes_Survival_5",
                       "terrainInstances": len(place), "coverageBeforeFill": round(cov_hi, 4)},
            "smoothSigma": a.smooth_sigma, "clipTopPct": a.clip_top_pct, "seamMatched": a.seam_match, "seamFeatherCells": a.feather_cells,
            "tier": a.tier, "tierMinM": a.tier_min_m, "raisedTiles": len(raised),
            "softenCells": a.soften_cells, "softenStepM": a.soften_step_m}
    json.dump(meta, open(a.out + "/hagga.json", "w"), indent=2)
    sha = hashlib.sha256(u16.tobytes()).hexdigest()
    json.dump({"bakeStamp": stamp, "map": "hagga", "raster": [G, G], "heightScale": a.height_scale,
               "layouts": [{"id": 0, "bin": "hagga.bin", "meta": "hagga.json", "sha": sha}]},
              open(a.out + "/manifest.json", "w"), indent=1)
    # previews: hillshade and contour overlay on the live backdrop
    gy, gx = np.gradient(h); shade = np.clip(0.55 + (-gx - gy) / (np.percentile(np.abs(gx) + np.abs(gy), 99) + 1e-9) * 0.45, 0, 1)
    Image.fromarray((shade * 255).astype("uint8")).resize((1024, 1024)).save(a.preview_dir + "/preview_hillshade.png")
    if os.path.exists(a.backdrop):
        Image.MAX_IMAGE_PIXELS = None
        bd = np.asarray(Image.open(a.backdrop).convert("RGB").resize((G, G))).astype("f8")
        g = (h - zmin) / (zmax - zmin); edges = np.zeros_like(g, bool)
        for t in np.linspace(0.05, 0.95, 10):
            m = g > t; edges |= (m ^ np.roll(m, 1, 0)) | (m ^ np.roll(m, 1, 1))
        bd[edges] = [0, 240, 255]
        Image.fromarray(bd.astype("uint8")).resize((1024, 1024), Image.NEAREST).save(a.preview_dir + "/preview_contours.png")
    print(f"baked {G}x{G}: {len(place)} terrain instances, coverage before fill {cov_hi:.1%}, "
          f"z {zmin:.0f}..{zmax:.0f} cm, heightScale {a.height_scale}, sha {sha[:12]}, stamp {stamp}")


if __name__ == "__main__":
    main()
