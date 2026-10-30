#!/usr/bin/env python3
"""Contact sheet of the 23 location miniatures: a small numpy software renderer (orthographic 3/4 view, z-buffer,
per-pixel base-colour texture on UV0, Lambert + ambient) over the travel disc and ring, labelled in the label style
overmap-miniatures.json gives. Renders the same placements overmap_miniatures.py bakes. Preview only, never shipped.
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import overmap_bake as ob
import overmap_miniatures as om

CELL = 420
COLS = 6
AZIMUTH_DEG, ELEVATION_DEG = 45.0, 35.0
SAND = np.array([196, 140, 92], float)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def lin_to_srgb(c):
    c = np.clip(np.asarray(c, float), 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055) * 255


def view_basis():
    az, el = math.radians(AZIMUTH_DEG), math.radians(ELEVATION_DEG)
    fwd = np.array([math.cos(el) * math.sin(az), -math.sin(el), math.cos(el) * math.cos(az)])  # y up frame
    right = np.cross(fwd, [0, 1, 0])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    return right, up, fwd


def raster(img, zbuf, sx, sy, depth, shade, uv, tex):
    """One triangle, per-pixel barycentric depth test and texture lookup."""
    h, w = zbuf.shape
    x0, x1 = max(0, int(math.floor(sx.min()))), min(w - 1, int(math.ceil(sx.max())))
    y0, y1 = max(0, int(math.floor(sy.min()))), min(h - 1, int(math.ceil(sy.max())))
    if x1 < x0 or y1 < y0:
        return
    d = (sy[1] - sy[2]) * (sx[0] - sx[2]) + (sx[2] - sx[1]) * (sy[0] - sy[2])
    if abs(d) < 1e-9:
        return
    gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
    l1 = ((sy[1] - sy[2]) * (gx - sx[2]) + (sx[2] - sx[1]) * (gy - sy[2])) / d
    l2 = ((sy[2] - sy[0]) * (gx - sx[2]) + (sx[0] - sx[2]) * (gy - sy[2])) / d
    l3 = 1 - l1 - l2
    m = (l1 >= -1e-4) & (l2 >= -1e-4) & (l3 >= -1e-4)
    if not m.any():
        return
    z = l1 * depth[0] + l2 * depth[1] + l3 * depth[2]
    sub = zbuf[y0:y1 + 1, x0:x1 + 1]
    m &= z < sub
    if not m.any():
        return
    sub[m] = z[m]
    if tex is None:
        col = np.broadcast_to(shade, (int(m.sum()), 3))
    else:
        u = (l1 * uv[0, 0] + l2 * uv[1, 0] + l3 * uv[2, 0])[m]
        v = (l1 * uv[0, 1] + l2 * uv[1, 1] + l3 * uv[2, 1])[m]
        th, tw = tex.shape[:2]
        col = tex[(np.mod(v, 1) * th).astype(int) % th, (np.mod(u, 1) * tw).astype(int) % tw] * shade
    img[y0:y1 + 1, x0:x1 + 1][m] = col


def render_location(loc, comps, mesh_cache, tex_cache, info):
    right, up, fwd = view_basis()
    light = -np.array([0.5, -0.8, 0.3])
    light /= np.linalg.norm(light)
    anchor = np.array([loc["position"][k] for k in ("x", "y", "z")])
    parts = []
    for c in comps:
        m = om.instance_matrix(c, anchor)
        for pos, nrm, uv, idx, mat in mesh_cache[c["mesh"]]:
            mat = om.MESH_MATERIAL_OVERRIDE.get(c["mesh"], mat)
            p = pos.astype(np.float64) @ m[:3, :3].T + m[:3, 3]
            n = nrm.astype(np.float64) @ np.linalg.inv(m[:3, :3])
            n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
            parts.append((p, n, uv, idx.reshape(-1, 3), mat))
    allp = np.concatenate([p for p, *_ in parts])
    disc = info["disc"]
    centre = om.to_out(np.array(disc["centerCm"], float)) - om.to_out(anchor)
    radius = disc["radiusCm"] / 100.0
    ext = max(np.abs(allp[:, [0, 2]] - centre[[0, 2]]).max(), radius) * 1.15
    ring = np.array([[centre[0] + ext * math.cos(a), allp[:, 1].min(), centre[2] + ext * math.sin(a)]
                     for a in np.linspace(0, 2 * math.pi, 16)])
    pts = np.concatenate([allp, ring])
    sxa, sya = pts @ right, pts @ up
    cx, cy = (sxa.min() + sxa.max()) / 2, (sya.min() + sya.max()) / 2
    scale = (CELL - 70) / max(sxa.max() - sxa.min(), sya.max() - sya.min())

    def proj(p):
        return CELL / 2 + (p @ right - cx) * scale, CELL / 2 - 20 - (p @ up - cy) * scale, p @ fwd

    img = np.tile(SAND, (CELL, CELL, 1))
    zbuf = np.full((CELL, CELL), np.inf)
    # travel disc: translucent mauve fill, gold border ring (MPC_POI_TravelVolumes), at the miniature's base
    base_y = allp[:, 1].min()
    fill = lin_to_srgb(om.DISC["fill"][:3])
    gold = lin_to_srgb(om.DISC["border"][:3]) * 1.6
    seg = 72
    for k in range(seg):
        a0, a1 = 2 * math.pi * k / seg, 2 * math.pi * (k + 1) / seg
        for r0, r1, col in ((0.0, radius * 0.96, SAND * 0.45 + fill * 0.55 + 25), (radius * 0.96, radius, gold)):
            quad = np.array([[centre[0] + r0 * math.cos(a0), base_y, centre[2] + r0 * math.sin(a0)],
                             [centre[0] + r1 * math.cos(a0), base_y, centre[2] + r1 * math.sin(a0)],
                             [centre[0] + r1 * math.cos(a1), base_y, centre[2] + r1 * math.sin(a1)],
                             [centre[0] + r0 * math.cos(a1), base_y, centre[2] + r0 * math.sin(a1)]])
            sx, sy, z = proj(quad)
            for t in ((0, 1, 2), (0, 2, 3)):
                raster(img, zbuf, sx[list(t)], sy[list(t)], z[list(t)] + 1e6, np.clip(col, 0, 255), None, None)
    zbuf[:] = np.where(np.isfinite(zbuf), np.inf, np.inf)
    for p, n, uv, tris, mat in parts:
        sx, sy, z = proj(p)
        tex = tex_cache[mat]
        factor = np.array(om.MATERIALS[mat]["factor"][:3])
        emissive = lin_to_srgb(om.MATERIALS[mat].get("emissive", [0, 0, 0])) / 255.0
        lam = np.clip(n @ light, 0, 1)
        for t in tris:
            fn = n[t].mean(0)
            shade = (0.35 + 0.85 * max(0.0, float(fn @ light))) * np.clip(lin_to_srgb(factor) / 255.0, 0.35, 1.0)
            shade = shade + emissive * 0.5
            raster(img, zbuf, sx[t], sy[t], z[t], shade, uv[t], tex)
        del lam
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def label_cell(im, loc, info):
    d = ImageDraw.Draw(im)
    style = info["label"]["style"]
    text = info["label"]["text"]
    if style == "gold3d":
        f = ImageFont.truetype(FONT, 30)
        text = text.upper()
        w = d.textlength(text, font=f)
        x = (CELL - w) / 2
        d.text((x + 2, CELL - 54), text, font=f, fill=(60, 40, 15))
        d.text((x, CELL - 56), text, font=f, fill=(232, 190, 96))
    else:
        f = ImageFont.truetype(FONT, 20)
        w = d.textlength(text, font=f)
        d.text(((CELL - w) / 2, CELL - 48), text, font=f, fill=tuple(int(v) for v in lin_to_srgb(
            om.LABEL_STYLE["caption"]["tint"]) * 0.0 + np.array([250, 236, 214])), stroke_width=2,
            stroke_fill=(70, 45, 25))
    small = ImageFont.truetype(FONT, 13)
    meshes = ", ".join(m["mesh"].replace("SM_OvermapPoi_", "").replace("SM_Env_Mineral_Neut_Ore_", "")
                       for m in info["meshes"])
    d.text((8, 6), f"{loc['kind']}  |  {meshes}", font=small, fill=(40, 25, 15))
    d.text((8, 22), f"disc r {info['disc']['radiusCm'] / 100:.1f} m", font=small, fill=(40, 25, 15))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", required=True)
    ap.add_argument("--meshes", required=True)
    ap.add_argument("--textures", nargs="+", required=True)
    ap.add_argument("--world", default=ob.DEFAULT_OUT, help="static/world-data with overmap-places/-miniatures.json")
    ap.add_argument("--out", required=True, help="png path (outside the repo)")
    args = ap.parse_args()
    if os.path.abspath(args.out).startswith(os.path.abspath(os.path.join(ob.REPO, "admin-backend"))):
        ob.fail("the contact sheet must not land in the served tree")

    locations = json.load(open(os.path.join(args.world, "overmap-places.json"), encoding="utf-8"))["locations"]
    minis = json.load(open(os.path.join(args.world, "overmap-miniatures.json"), encoding="utf-8"))["locations"]
    per = om.assign(locations, om.level_components(args.extract))
    mesh_files = om.index_files([args.meshes], ".glb")
    tex_files = om.index_files(args.textures + [args.meshes], ".png")
    tex_cache = {k: np.asarray(Image.open(tex_files[v["texture"]]).convert("RGB").resize((256, 256)), float)
                 for k, v in om.MATERIALS.items()}
    mesh_cache = {}
    rows = math.ceil(len(locations) / COLS)
    sheet = Image.new("RGB", (CELL * COLS, CELL * rows + 40), (30, 22, 16))
    ImageDraw.Draw(sheet).text((10, CELL * rows + 10), "Sand Table: the game's own overland miniatures "
                               "(SM_OvermapPoi_*), 3/4 view, base-colour textures on UV0, travel disc + ring "
                               "from MPC_POI_TravelVolumes. Client 25690505.",
                               font=ImageFont.truetype(FONT, 16), fill=(230, 200, 150))
    for i, loc in enumerate(locations):
        for c in per[loc["id"]]:
            if c["mesh"] not in mesh_cache:
                mesh_cache[c["mesh"]] = om.load_mini_mesh(mesh_files[c["mesh"]])
        im = render_location(loc, per[loc["id"]], mesh_cache, tex_cache, minis[loc["id"]])
        label_cell(im, loc, minis[loc["id"]])
        sheet.paste(im, ((i % COLS) * CELL, (i // COLS) * CELL))
        print(f"  {i + 1:2d}/{len(locations)} {loc['id']}", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    sheet.save(args.out)
    print(f"contact sheet -> {args.out}")


if __name__ == "__main__":
    sys.exit(main())
