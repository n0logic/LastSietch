"""Deep Desert map-art drape: unshaded albedo for the War-Table, synthesized from game data per layout.

  rock  each placed block's LOD1 proxy (the in-world far LOD) rasterized top-down with its baked diffuse texture
        (MI_PROXY_*.json DiffuseTexture -> T_PROXY_CB_*_Art_LOD1_D.png next to the GLB), barycentric UV per splat
        sample, topmost surface wins (the same max-z rule as the height splat). Proxies whose material is
        M_SimpleVertexColor take COLOR_0; the three such proxies in client 25690505 export all-zero colours and are
        left uncoloured. Visible rock with no colour takes the nearest coloured rock (<= 40 px), else the median rock
        colour. Collision-only geometry is coloured by the block's LOD1 proxy, which shares the block transform.
  sand  a palette sampled from the in-game Hagga map art (open southern dunes), modulated by the SoS landscape
        paint layers (Flat / Wind / Occluded / ColorMask_RGB); the ShieldWall-region ground gets a stone tint.
The shader adds lambert from the normal map, so nothing here is shaded.
"""
import glob, json, os, struct
import numpy as np
from PIL import Image
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = None


def read_glb(path):
    """[(pos (n,3) f8, idx (m,3) int, uv0 (n,2) | None, col0 (n,3) 0..1 | None, material name)] per primitive."""
    f = open(path, "rb").read(); off, ch = 12, []
    while off < len(f):
        cl, _ct = struct.unpack("<II", f[off:off + 8]); ch.append(f[off + 8:off + 8 + cl]); off += 8 + cl
    gl = json.loads(ch[0]); b = ch[1]
    CT = {5126: ("<f4", 1.0), 5125: ("<u4", None), 5123: ("<u2", 65535.0), 5121: ("u1", 255.0)}
    NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

    def acc(i):
        a = gl["accessors"][i]; bv = gl["bufferViews"][a["bufferView"]]
        dt, norm = CT[a["componentType"]]; nc = NC[a["type"]]
        o = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        isz = np.dtype(dt).itemsize; st = bv.get("byteStride", isz * nc)
        if st == isz * nc:
            r = np.frombuffer(b, dtype=dt, count=a["count"] * nc, offset=o)
        else:
            need = st * (a["count"] - 1) + isz * nc
            raw = np.frombuffer(b, dtype=np.uint8, count=min(need, len(b) - o), offset=o)
            if len(raw) < need:
                raw = np.concatenate([raw, np.zeros(need - len(raw), np.uint8)])
            r = np.lib.stride_tricks.as_strided(raw, shape=(a["count"], isz * nc), strides=(st, 1)).copy().view(dt)
        r = r.reshape(-1, nc) if nc > 1 else r.reshape(-1)
        if a.get("normalized") and norm:
            r = r.astype("f8") / norm
        return r

    mats = [m.get("name", "") for m in gl.get("materials", [])]
    out = []
    for m in gl["meshes"]:
        for p in m["primitives"]:
            at = p["attributes"]
            pos = acc(at["POSITION"]).astype("f8")
            idx = acc(p["indices"]).astype(int).reshape(-1, 3)
            uv = acc(at["TEXCOORD_0"]).astype("f8") if "TEXCOORD_0" in at else None
            col = acc(at["COLOR_0"]).astype("f8")[:, :3] if "COLOR_0" in at else None
            if col is not None and col.max() > 1.5:
                col = col / 255.0
            if col is not None and col.max() < 0.02:
                col = None                     # exported all-zero (no paint): treat as uncoloured
            out.append((pos, idx, uv, col, mats[p["material"]] if "material" in p else ""))
    return out


class Textures:
    """Diffuse texture per material instance, from the MI json + PNGs the extractor writes next to each GLB."""

    def __init__(self, mesh_dirs):
        self.png, self.mi = {}, {}
        for d in mesh_dirs:
            for p in glob.glob(d + "/**/*.png", recursive=True):
                self.png.setdefault(os.path.basename(p)[:-4], p)
            for p in glob.glob(d + "/**/MI_*.json", recursive=True):
                self.mi.setdefault(os.path.basename(p)[:-5], p)
        self.cache = {}

    def diffuse(self, material):
        """uint8 (h, w, 3) or None (vertex colour / no texture)."""
        if material in self.cache:
            return self.cache[material]
        img = None
        if material in self.mi:
            t = json.load(open(self.mi[material])).get("Textures", {})
            ref = t.get("DiffuseTexture") or t.get("PM_Diffuse") or ""
            name = ref.split(".")[-1]
            if name and name != "T_BaseDiffuse_D" and name in self.png:
                img = np.asarray(Image.open(self.png[name]).convert("RGB"))
        self.cache[material] = img
        return img


def rot(r):
    import math
    p, y, ro = (math.radians((r or {}).get(k, 0.0)) for k in ("Pitch", "Yaw", "Roll"))
    cp, sp, cy, sy, cr, sr = math.cos(p), math.sin(p), math.cos(y), math.sin(y), math.cos(ro), math.sin(ro)
    return np.array([[cp * cy, cp * sy, sp],
                     [sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp],
                     [-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp]])


def splat_colour(Zb, Cb, w, idx, uv, col, tex, G, origin, span, rng):
    """Top-down splat of one primitive into the z-buffer Zb (cm) and colour buffer Cb (uint8 RGB)."""
    T = ((w[:, :2] - origin) / span * G)[idx]; Z = w[idx, 2]
    e1 = T[:, 1] - T[:, 0]; e2 = T[:, 2] - T[:, 0]
    area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2
    k = np.clip((area * 3).astype(int) + 3, 3, 40000); rep = np.repeat(np.arange(len(T)), k)
    r1 = np.sqrt(rng.random(len(rep))); r2 = rng.random(len(rep)); A, B, C = 1 - r1, r1 * (1 - r2), r1 * r2
    P = A[:, None] * T[rep, 0] + B[:, None] * T[rep, 1] + C[:, None] * T[rep, 2]
    Zs = A * Z[rep, 0] + B * Z[rep, 1] + C * Z[rep, 2]
    ix = P[:, 0].astype(int); iy = P[:, 1].astype(int)
    ok = (ix >= 0) & (ix < G) & (iy >= 0) & (iy < G)
    if not ok.any():
        return
    rep, A, B, C, Zs, ix, iy = rep[ok], A[ok], B[ok], C[ok], Zs[ok], ix[ok], iy[ok]
    tri = idx[rep]
    if tex is not None and uv is not None:
        U = A[:, None] * uv[tri[:, 0]] + B[:, None] * uv[tri[:, 1]] + C[:, None] * uv[tri[:, 2]]
        h, wd = tex.shape[:2]
        tx = (np.mod(U[:, 0], 1.0) * wd).astype(int).clip(0, wd - 1)
        ty = (np.mod(U[:, 1], 1.0) * h).astype(int).clip(0, h - 1)
        rgb = tex[ty, tx]
    elif col is not None:
        V = A[:, None] * col[tri[:, 0]] + B[:, None] * col[tri[:, 1]] + C[:, None] * col[tri[:, 2]]
        rgb = np.clip(np.round(V * 255), 0, 255).astype(np.uint8)
    else:
        return
    # topmost sample per pixel, then only where it beats what is already there
    key = iy * G + ix
    order = np.lexsort((Zs, key))
    key, Zs, rgb = key[order], Zs[order], rgb[order]
    last = np.r_[key[1:] != key[:-1], True]
    key, Zs, rgb = key[last], Zs[last], rgb[last]
    zf = Zb.reshape(-1); cf = Cb.reshape(-1, 3)
    win = Zs > zf[key]
    zf[key[win]] = Zs[win]; cf[key[win]] = rgb[win]


def rock_colour(blocks, lod1_glb, textures, G, origin, span):
    """(z-buffer cm, RGB) of every block's LOD1 proxy. lod1_glb: asset -> glb path or None."""
    Zb = np.full((G, G), -np.inf); Cb = np.zeros((G, G, 3), np.uint8)
    rng = np.random.default_rng(0); cache = {}; used = {"textured": 0, "vertexColour": 0, "none": 0}
    for o in blocks:
        t = o["t"]
        if not (origin - 100000 <= t["X"] < origin + span + 100000 and origin - 100000 <= t["Y"] < origin + span + 100000):
            continue
        p = lod1_glb(o["asset"])
        if not p:
            used["none"] += 1; continue
        if p not in cache:
            cache[p] = [(np.stack([v[:, 0], v[:, 2], v[:, 1]], 1) * 100, i, uv, col, textures.diffuse(m))
                        for v, i, uv, col, m in read_glb(p)]
        s = o["s"] or {"X": 1, "Y": 1, "Z": 1}; M = rot(o["r"]); off = np.array([t["X"], t["Y"], t["Z"]])
        kinds = set()
        for v, i, uv, col, tex in cache[p]:
            w = (v * np.array([s["X"], s["Y"], s["Z"]])) @ M + off
            splat_colour(Zb, Cb, w, i, uv, col, tex, G, origin, span, rng)
            kinds.add("textured" if tex is not None else "vertexColour" if col is not None else "none")
        used["textured" if "textured" in kinds else "vertexColour" if "vertexColour" in kinds else "none"] += 1
    return Zb, Cb, used


def hagga_sand_palette(path, box=(0.42, 0.79, 0.96, 0.94)):
    """(shadow, mid, light) RGB of open sand in the in-game Hagga map art: the southern dunes (box in 0..1 image
    coords), excluding rock shadows, the red border and anything far from the median tone."""
    im = Image.open(path).convert("RGB")
    W, H = im.size
    a = np.asarray(im.crop((int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H))).resize((1024, 256)))
    px = a.reshape(-1, 3).astype(np.float64)
    lum = px.mean(1)
    sat = px.max(1) - px.min(1)
    med = np.median(px, 0)
    keep = (lum > np.percentile(lum, 15)) & (np.abs(px - med).max(1) < 40) & (sat < np.percentile(sat, 90))
    px, lum = px[keep], lum[keep]
    return (px[lum <= np.percentile(lum, 25)].mean(0), np.median(px, 0), px[lum >= np.percentile(lum, 75)].mean(0))


def sand_albedo(weights, palette, region_px, stone):
    """weights: (L, G, G) 0..1 in dd_ground.LAYERS order; region_px: (G, G) 0/1 ShieldWall region (feathered ok)."""
    flat, wind, occl, cr, cg, cb = weights
    dark, mid, light = (np.asarray(c, np.float64) for c in palette)
    # light on open flats and wind-swept faces, darker in sheltered troughs; ColorMask adds a faint hue drift
    t = np.clip(0.55 + 0.35 * (flat - 0.4) + 0.15 * (wind - 0.6) - 0.45 * (occl - 0.35), 0, 1)
    rgb = np.where(t[..., None] < 0.5,
                   dark + (mid - dark) * (t[..., None] / 0.5),
                   mid + (light - mid) * ((t[..., None] - 0.5) / 0.5))
    hue = np.stack([cr - 0.4, cg - 0.18, cb - 0.09], -1)
    rgb = rgb * (1 + 0.05 * np.clip(hue, -1, 1))
    rgb = rgb * (1 - 0.6 * region_px[..., None]) + np.asarray(stone, np.float64) * (0.6 * region_px[..., None])
    return np.clip(rgb, 0, 255)


def compose(sand, rock_rgb, rock_z, visible):
    """Rock colour where rock is the visible surface (visible: bool (G, G)); pixels there with no LOD1 colour take
    the nearest coloured rock pixel, else the median rock colour. 1 px feather at the rock edge."""
    have = np.isfinite(rock_z)
    fallback = np.median(rock_rgb[have], 0) if have.any() else np.array([120.0, 90.0, 70.0])
    rock = rock_rgb.astype(np.float64)
    need = visible & ~have
    if need.any() and have.any():
        idx = ndimage.distance_transform_edt(~have, return_distances=False, return_indices=True)
        filled = rock[idx[0], idx[1]]
        dist = ndimage.distance_transform_edt(~have)
        rock = np.where((need & (dist <= 40))[..., None], filled, rock)
        need = need & (dist > 40)
    rock[need] = fallback
    a = ndimage.gaussian_filter(visible.astype(np.float64), 0.7)[..., None]
    return np.clip(np.round(sand * (1 - a) + rock * a), 0, 255).astype(np.uint8)
