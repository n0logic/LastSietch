#!/usr/bin/env python3
"""High-resolution top-down overland map (overmap-hires.webp, 8192x4096 over the full art frame, plus a 4096
sibling) for the Sand Table. Base colour is T_UI_GlobalMap_D upsampled; relief detail (sunlit / shaded terrace
risers, pale terrace rims, soft long cast shadows) comes from the 22 placed visual SM_OvermapOutputMesh_* tiles at
full resolution. Where no tile covers the frame (the oblong south island, open sand) the upsampled art shows
through, feathered at the coverage edge. Deterministic: game data + fixed shading, no generative step.

Memory rule (README): ONE source tile in memory at a time, float32 rasters, shading and compositing in tiles with
a halo. Peak RSS is about 2.5 GB (the 2x supersampled height field is 537 MB). Run it under the cgroup cap.

Stages (cached in --work so a killed run resumes): height (2x supersampled max-z raster) -> relief (per 1x pixel:
lit fraction, rim, coverage, height) -> shadow -> compose.

--albedo-only: the scene lights the relief itself (time-of-day presets move the sun), so the baked texture must carry
no light. It keeps the art colour and the pale terrace rims (paint) and drops the Lambert term, the cast shadows and the
riser crease darkening. The shadow stage is skipped.
"""
import argparse
import gc
import json
import math
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

import overmap_bake as ob

Image.MAX_IMAGE_PIXELS = None

OUT_SIZE = (8192, 4096)
LOW_SIZE = (4096, 2048)
SS = 2                        # supersample factor of the height raster
TILE = 1024                   # 1x output tile edge
SUN_AZIMUTH_DEG = 315.0       # light from the NW (X east, Y south), shadows fall SE
SUN_ALTITUDE_DEG = 22.0       # low sun: a 63 cm step casts ~1.6 m, the 50 m cap ~125 m
LIT_MAX = 1.6                 # sunlit risers: cap the relative brightening so cliff faces do not blow out
AMBIENT = 0.52                # fraction of flat-ground light left in full shadow
SHADOW_SOFT = 0.10            # horizon-tangent band of the penumbra
RIM_STEP_CM = 25.0            # a pixel is a terrace rim if it stands this far above a 3x3 neighbour (step 63.45)
RIM_COLOUR = (0.96, 0.89, 0.76)
RIM_STRENGTH = 0.55
RIM_DENSE_FADE = 1.4          # where rims crowd (steep slopes) they fade so the slope does not wash out pale
CREASE_STRENGTH = 0.06        # slight darkening at the foot of each riser
ART_TINT_BLUR_PX = 6.0        # under geometry the art is only a colour tint: its painted rocks do not always sit
ART_DETAIL_KEEP = 0.12        # where the placed geometry has relief (e.g. Wind Pass), so their detail is dropped
RELIEF_WINDOW_PX = 15         # local height range over this window decides "has relief"
RELIEF_RANGE_CM = (120.0, 500.0)  # range -> 0..1 art-detail suppression (flat sand keeps the art's own detail)
FEATHER_PX = 12.0
WEBP_QUALITY = 84
WEBP_QUALITY_LOW = 82


def frame():
    art = ob.art_transform()
    r = art["worldRectCm"]
    return r["x0"], r["y0"], r["x1"], r["y1"]


# ---------------------------------------------------------------- height

def raster_tile(z_out, pts, tris, x0, y0, cmpp):
    """Max-z rasterisation of one tile's triangles into z_out (float32, -inf = empty). Pixel centres are sampled;
    triangles are bucketed by power-of-two bounding boxes so every chunk is a dense vectorised barycentric test."""
    h_px, w_px = z_out.shape
    flat = z_out.reshape(-1)
    u = (pts[:, 0] - x0) / cmpp - 0.5
    v = (pts[:, 1] - y0) / cmpp - 0.5
    z = pts[:, 2]
    ua, ub, uc = u[tris[:, 0]], u[tris[:, 1]], u[tris[:, 2]]
    va, vb, vc = v[tris[:, 0]], v[tris[:, 1]], v[tris[:, 2]]
    xmin = np.maximum(np.ceil(np.minimum(np.minimum(ua, ub), uc)), 0).astype(np.int64)
    xmax = np.minimum(np.floor(np.maximum(np.maximum(ua, ub), uc)), w_px - 1).astype(np.int64)
    ymin = np.maximum(np.ceil(np.minimum(np.minimum(va, vb), vc)), 0).astype(np.int64)
    ymax = np.minimum(np.floor(np.maximum(np.maximum(va, vb), vc)), h_px - 1).astype(np.int64)
    d = (vb - vc) * (ua - uc) + (uc - ub) * (va - vc)
    bw = xmax - xmin + 1
    bh = ymax - ymin + 1
    keep = (bw > 0) & (bh > 0) & (np.abs(d) > 1e-9)
    bucket_w = 1 << np.ceil(np.log2(np.maximum(bw, 1))).astype(np.int64)
    bucket_h = 1 << np.ceil(np.log2(np.maximum(bh, 1))).astype(np.int64)
    key = np.where(keep, bucket_w * 65536 + bucket_h, -1)
    samples = 0
    for k in np.unique(key):
        if k < 0:
            continue
        idx = np.nonzero(key == k)[0]
        kw, kh = int(k // 65536), int(k % 65536)
        oy, ox = np.divmod(np.arange(kw * kh), kw)
        chunk = max(1, 4_000_000 // (kw * kh))
        for s in range(0, len(idx), chunk):
            sel = idx[s:s + chunk]
            px = xmin[sel, None] + ox
            py = ymin[sel, None] + oy
            inb = (px <= xmax[sel, None]) & (py <= ymax[sel, None])
            dd = d[sel, None]
            l1 = ((vb - vc)[sel, None] * (px - uc[sel, None]) + (uc - ub)[sel, None] * (py - vc[sel, None])) / dd
            l2 = ((vc - va)[sel, None] * (px - uc[sel, None]) + (ua - uc)[sel, None] * (py - vc[sel, None])) / dd
            l3 = 1.0 - l1 - l2
            m = inb & (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
            zz = l1 * z[tris[sel, 0], None] + l2 * z[tris[sel, 1], None] + l3 * z[tris[sel, 2], None]
            np.maximum.at(flat, (py * w_px + px)[m], zz[m].astype(np.float32))
            samples += int(m.sum())
            del px, py, inb, l1, l2, l3, m, zz
    return samples


def stage_height(args, work):
    path = os.path.join(work, "height-ss.npy")
    meta_path = os.path.join(work, "height-ss.json")
    if os.path.isfile(path) and os.path.isfile(meta_path):
        print("height: cached", flush=True)
        with open(meta_path) as fh:
            return json.load(fh)
    x0, y0, x1, y1 = frame()
    w, h = OUT_SIZE[0] * SS, OUT_SIZE[1] * SS
    cmpp = (x1 - x0) / w
    zbuf = np.full((h, w), -np.inf, np.float32)
    placed = [p for p in ob.placed_meshes(args.extract) if "OvermapOutputMesh_" in p["mesh"]]
    if len(placed) != 22:
        ob.fail(f"expected 22 placed visual tiles, got {len(placed)}")
    tiles = []
    for t in sorted(placed, key=lambda x: x["mesh"]):
        src = os.path.join(args.visual_meshes, t["mesh"] + ".glb")
        if not os.path.isfile(src):
            ob.fail(f"placed visual mesh missing: {t['mesh']}")
        p, tri = ob.read_mesh(src)
        wpts = (p * t["scale"]) @ t["rot"].T + t["loc"]
        del p
        n = raster_tile(zbuf, wpts, tri, x0, y0, cmpp)
        tiles.append({"mesh": t["mesh"], "tris": int(len(tri)), "samples": n,
                      "xCm": [round(float(wpts[:, 0].min())), round(float(wpts[:, 0].max()))],
                      "yCm": [round(float(wpts[:, 1].min())), round(float(wpts[:, 1].max()))],
                      "zCm": [round(float(wpts[:, 2].min())), round(float(wpts[:, 2].max()))]})
        print(f"  {t['mesh']}: {len(tri)} tris, {n} samples", flush=True)
        del wpts, tri
        gc.collect()
    zbuf[np.isneginf(zbuf)] = np.nan
    covered = int(np.isfinite(zbuf).sum())
    np.save(path, zbuf)
    del zbuf
    meta = {"ssSize": [w, h], "cmPerPxSS": cmpp, "coveredSSPx": covered, "tiles": tiles}
    ob.write_json(meta_path, meta)
    return meta


# ---------------------------------------------------------------- relief (lit fraction, rim, coverage, height)

def sun_vector():
    az, alt = math.radians(SUN_AZIMUTH_DEG), math.radians(SUN_ALTITUDE_DEG)
    # azimuth clockwise from north; north = -Y, east = +X
    return np.array([math.sin(az) * math.cos(alt), -math.cos(az) * math.cos(alt), math.sin(alt)])


def fill_nearest(a):
    """NaN -> value of the nearest finite pixel (so gradients do not see a cliff at the coverage edge)."""
    bad = ~np.isfinite(a)
    if not bad.any():
        return a
    if bad.all():
        return np.zeros_like(a)
    idx = ndimage.distance_transform_edt(bad, return_distances=False, return_indices=True)
    return a[tuple(idx)]


def stage_relief(work, cmpp_ss):
    names = ["lit", "rim", "crease", "cover", "height"]
    paths = {n: os.path.join(work, f"relief-{n}.npy") for n in names}
    if all(os.path.isfile(p) for p in paths.values()):
        print("relief: cached", flush=True)
        return
    hs = np.load(os.path.join(work, "height-ss.npy"), mmap_mode="r")
    w, h = OUT_SIZE
    out = {n: np.zeros((h, w), np.float32) for n in names}
    out["lit"][:] = 1.0
    sun = sun_vector()
    halo = 8 * SS
    t = TILE * SS
    for ty in range(0, h * SS, t):
        for tx in range(0, w * SS, t):
            ya, yb = max(0, ty - halo), min(h * SS, ty + t + halo)
            xa, xb = max(0, tx - halo), min(w * SS, tx + t + halo)
            raw = np.array(hs[ya:yb, xa:xb], np.float32)
            if not np.isfinite(raw).any():
                continue
            z = fill_nearest(raw)
            gy, gx = np.gradient(z, cmpp_ss)
            norm = np.sqrt(gx * gx + gy * gy + 1.0)
            lam = (-gx * sun[0] - gy * sun[1] + sun[2]) / norm
            lit = np.clip(lam, 0.0, None) / sun[2]
            # rims/creases only where the whole 5x5 is real geometry: the nearest-fill makes fake steps at the
            # coverage edge
            inside = ndimage.minimum_filter(np.isfinite(raw), 5)
            rim = ((z - ndimage.minimum_filter(z, 3)) > RIM_STEP_CM) & inside
            crease = ((ndimage.maximum_filter(z, 3) - z) > RIM_STEP_CM) & inside
            cy, cx = ty - ya, tx - xa
            sl = (slice(cy, cy + t), slice(cx, cx + t))
            oy, ox = ty // SS, tx // SS
            parts = {"lit": lit, "rim": rim.astype(np.float32), "crease": crease.astype(np.float32),
                     "cover": np.isfinite(raw).astype(np.float32), "height": z}
            for n, a in parts.items():
                a = a[sl]
                out[n][oy:oy + t // SS, ox:ox + t // SS] = a.reshape(t // SS, SS, t // SS, SS).mean((1, 3))
            del raw, z, gx, gy, norm, lam, lit, rim, crease, inside, parts
        print(f"  relief row {ty // t + 1}/{h * SS // t}", flush=True)
    del hs
    for n in names:
        np.save(paths[n], out[n])


# ---------------------------------------------------------------- cast shadows

def stage_shadow(work, cmpp):
    path = os.path.join(work, "shadow.npy")
    if os.path.isfile(path):
        print("shadow: cached", flush=True)
        return
    z = np.load(os.path.join(work, "relief-height.npy"))
    cover = np.load(os.path.join(work, "relief-cover.npy"))
    h, w = z.shape
    tan_alt = math.tan(math.radians(SUN_ALTITUDE_DEG))
    reach = (float(z[cover > 0].max()) - float(z[cover > 0].min())) / tan_alt
    sun = sun_vector()
    ux, uy = sun[0], sun[1]
    scale = 1.0 / max(abs(ux), abs(uy))
    ux, uy = ux * scale, uy * scale
    step_cm = math.hypot(ux, uy) * cmpp
    kmax = int(math.ceil(reach / step_cm)) + 1
    halo = kmax + 4
    shadow = np.zeros((h, w), np.float32)
    for ty in range(0, h, TILE):
        for tx in range(0, w, TILE):
            if not cover[max(0, ty - halo):ty + TILE + halo, max(0, tx - halo):tx + TILE + halo].any():
                continue
            ya, yb = max(0, ty - halo), min(h, ty + TILE + halo)
            xa, xb = max(0, tx - halo), min(w, tx + TILE + halo)
            zt = z[ya:yb, xa:xb]
            pad = np.pad(zt, kmax + 1, mode="edge")
            best = np.full(zt.shape, -np.inf, np.float32)
            for k in range(1, kmax + 1):
                dx, dy = int(round(k * ux)), int(round(k * uy))
                sh = pad[kmax + 1 + dy:kmax + 1 + dy + zt.shape[0], kmax + 1 + dx:kmax + 1 + dx + zt.shape[1]]
                np.maximum(best, (sh - zt) / np.float32(k * step_cm), out=best)
            s = np.clip((best - tan_alt) / (SHADOW_SOFT * tan_alt) + 0.5, 0.0, 1.0)
            s = ndimage.gaussian_filter(s, 1.2)
            cy, cx = ty - ya, tx - xa
            shadow[ty:ty + TILE, tx:tx + TILE] = s[cy:cy + TILE, cx:cx + TILE]
        print(f"  shadow row {ty // TILE + 1}/{h // TILE} (k={kmax})", flush=True)
    np.save(path, shadow)


# ---------------------------------------------------------------- compose

def stage_compose(args, work):
    albedo = args.albedo_only
    w, h = OUT_SIZE
    art = Image.open(os.path.join(args.extract, ob.ART)).convert("RGB")
    if art.size != (4096, 2048):
        ob.fail(f"T_UI_GlobalMap_D is {art.size}, expected 4096x2048")
    art_up = np.asarray(art.resize(OUT_SIZE, Image.LANCZOS), np.float32) / 255.0
    del art
    lit = np.load(os.path.join(work, "relief-lit.npy"), mmap_mode="r")
    rim = np.load(os.path.join(work, "relief-rim.npy"), mmap_mode="r")
    crease = np.load(os.path.join(work, "relief-crease.npy"), mmap_mode="r")
    cover = np.load(os.path.join(work, "relief-cover.npy"), mmap_mode="r")
    shadow = None if albedo else np.load(os.path.join(work, "shadow.npy"), mmap_mode="r")
    height = np.load(os.path.join(work, "relief-height.npy"), mmap_mode="r")
    out = np.empty((h, w, 3), np.uint8)
    rim_col = np.array(RIM_COLOUR, np.float32)
    halo = 64
    for ty in range(0, h, TILE):
        for tx in range(0, w, TILE):
            ya, yb = max(0, ty - halo), min(h, ty + TILE + halo)
            xa, xb = max(0, tx - halo), min(w, tx + TILE + halo)
            a = art_up[ya:yb, xa:xb]
            c = np.array(cover[ya:yb, xa:xb])
            if not c.any():
                res = a
            else:
                blur = ndimage.gaussian_filter(a, (ART_TINT_BLUR_PX, ART_TINT_BLUR_PX, 0))
                z = np.array(height[ya:yb, xa:xb])
                rng = ndimage.maximum_filter(z, RELIEF_WINDOW_PX) - ndimage.minimum_filter(z, RELIEF_WINDOW_PX)
                rel = np.clip((rng - RELIEF_RANGE_CM[0]) / (RELIEF_RANGE_CM[1] - RELIEF_RANGE_CM[0]), 0.0, 1.0)
                rel = ndimage.gaussian_filter(rel * c, FEATHER_PX)
                keep = (1.0 - rel * (1.0 - ART_DETAIL_KEEP))[..., None]
                base = blur + keep * (a - blur)
                if albedo:
                    illum = np.ones(base.shape[:2], np.float32)
                    col = base
                else:
                    illum = AMBIENT + (1.0 - AMBIENT) * np.minimum(lit[ya:yb, xa:xb], LIT_MAX) * (1.0 - np.array(shadow[ya:yb, xa:xb]))
                    col = base * illum[..., None]
                    col *= (1.0 - CREASE_STRENGTH * np.array(crease[ya:yb, xa:xb]))[..., None]
                rt = np.array(rim[ya:yb, xa:xb])
                dense = ndimage.gaussian_filter(rt, 2.0)
                r = (RIM_STRENGTH * rt * np.clip(1.0 - RIM_DENSE_FADE * dense, 0.35, 1.0))[..., None]
                col = col * (1.0 - r) + (rim_col * (0.55 + 0.45 * illum[..., None])) * r
                wgt = ndimage.gaussian_filter(c, FEATHER_PX)
                wgt = (np.clip((wgt - 0.5) * 2.0, 0.0, 1.0) * c)[..., None]
                res = a * (1.0 - wgt) + col * wgt
            cy, cx = ty - ya, tx - xa
            out[ty:ty + TILE, tx:tx + TILE] = np.clip(res[cy:cy + TILE, cx:cx + TILE] * 255.0 + 0.5, 0, 255)
        print(f"  compose row {ty // TILE + 1}/{h // TILE}", flush=True)
    del art_up
    im = Image.fromarray(out)
    im.save(os.path.join(work, "overmap-hires.png"))
    return im


def rect_both(r):
    """overmap-art.json's x0/y0/x1/y1 plus the xMin/xMax/yMin/yMax names agreed with the Sand Table (same values)."""
    return {"x0": r["x0"], "y0": r["y0"], "x1": r["x1"], "y1": r["y1"],
            "xMin": r["x0"], "xMax": r["x1"], "yMin": r["y0"], "yMax": r["y1"]}


def hires_json(name, size, cmpp, extra):
    art = ob.art_transform()
    doc = {"image": name, "size": list(size),
           "source": "T_UI_GlobalMap_D upsampled + relief shading from the 22 placed SM_OvermapOutputMesh_* tiles",
           "cmPerLayerUnit": art["cmPerLayerUnit"], "layerOffset": art["layerOffset"], "layerPlane": art["layerPlane"],
           "pxPerCm": size[0] / (art["layerPlane"][0] * art["cmPerLayerUnit"]), "cmPerPx": cmpp,
           "worldRectCm": rect_both(art["worldRectCm"]), "axes": art["axes"]}
    doc.update(extra)
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", required=True, help="DuneExtractor dump root (has json/ and png/)")
    ap.add_argument("--visual-meshes", required=True, help="folder holding SM_OvermapOutputMesh_*.glb")
    ap.add_argument("--work", required=True, help="scratch dir for the cached rasters (never under the repo)")
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", default=ob.DEFAULT_OUT)
    ap.add_argument("--albedo-only", action="store_true", help="no baked light: art colour + pale rims only (the scene lights it)")
    ap.add_argument("--stop-after", choices=["height", "relief", "shadow", "compose"])
    args = ap.parse_args()
    work = os.path.abspath(args.work)
    if work.startswith(ob.REPO):
        ob.fail("--work must be outside the repo")
    os.makedirs(work, exist_ok=True)

    meta = stage_height(args, work)
    if args.stop_after == "height":
        return 0
    stage_relief(work, meta["cmPerPxSS"])
    if args.stop_after == "relief":
        return 0
    x0, _, x1, _ = frame()
    cmpp = (x1 - x0) / OUT_SIZE[0]
    if not args.albedo_only:
        stage_shadow(work, cmpp)
    if args.stop_after == "shadow":
        return 0
    im = stage_compose(args, work)
    if args.stop_after == "compose":
        return 0

    big = os.path.join(args.out, "overmap-hires.webp")
    low = os.path.join(args.out, "overmap-hires-4096.webp")
    im.save(big, "WEBP", quality=WEBP_QUALITY, method=6)
    im.resize(LOW_SIZE, Image.LANCZOS).save(low, "WEBP", quality=WEBP_QUALITY_LOW, method=6)
    del im
    shading = {"mode": "albedo-only", "supersample": SS, "rimStepCm": RIM_STEP_CM} if args.albedo_only else {"sunAzimuthDeg": SUN_AZIMUTH_DEG, "sunAltitudeDeg": SUN_ALTITUDE_DEG, "ambient": AMBIENT, "litMax": LIT_MAX,
               "rimStepCm": RIM_STEP_CM, "supersample": SS, "verticalExaggeration": 1.0}
    cover = {"geometryTiles": len(meta["tiles"]), "srcTris": sum(t["tris"] for t in meta["tiles"]),
             "coveredFraction": round(meta["coveredSSPx"] / (meta["ssSize"][0] * meta["ssSize"][1]), 5),
             "artOnly": "outside the 22 visual tiles (open sand, the oblong south island) the upsampled art shows "
                        "through, feathered at the coverage edge"}
    doc = hires_json("overmap-hires.webp", OUT_SIZE, cmpp,
                     {"image4096": "overmap-hires-4096.webp",
                      "lowTier": {"image": "overmap-hires-4096.webp", "size": list(LOW_SIZE),
                                  "cmPerPx": cmpp * OUT_SIZE[0] / LOW_SIZE[0]},
                      "client": args.client, "shading": shading, "coverage": cover})
    ob.write_json(os.path.join(args.out, "overmap-hires.json"), doc)
    files = ["overmap-hires.webp", "overmap-hires-4096.webp", "overmap-hires.json"]
    ob.register(args.out, files, "hires",
                {"files": files, "script": "overmap_hires.py", "size": list(OUT_SIZE), "cmPerPx": cmpp,
                 "lowTier": "overmap-hires-4096.webp", "fallback": "overmap-art-2048.webp", **shading})
    for n in files:
        p = os.path.join(args.out, n)
        print(f"{n} {os.path.getsize(p)} bytes {ob.sha256(p)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
