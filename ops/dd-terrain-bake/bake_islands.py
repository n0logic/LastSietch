#!/usr/bin/env python3
"""Bake the Deep Desert rock islands per Coriolis layout and render 2D map backdrops.

Companion to bake_terrain.py (heightfield for the holo tier; see README). This pass does
not reconstruct height. It rasterises the FOOTPRINT of every SeaOfSand landmark tile
(rock outcrops, shield wall, wrecks) top-down, rock where the tile geometry stands above
the sand floor, and paints a stylised backdrop the 2D map can use directly.

Inputs (read-only, client pak extraction; re-extract after a game update):
  --layouts  .../Cartography/DeepDesert_1/Layouts/{N}/BP_WorldMap_Meshes_DeepDesert_Layout_N.json
  --tiles    .../extracted/dd-terrain  (sos-tiles/** SM_SoS_*.glb; proxies are ignored = sand)
Outputs (--out):
  dd-layout-{N}-rock.png     rock mask, 1536x1536, 255 = rock
  dd-layout-{N}.webp         backdrop, 1536x1536, our palette
  islands-manifest.json      stamp, threshold, per-layout rock fraction, band rows

Frozen calibration (must match admin-backend/map_model.py or markers drift):
  world origin -1270000,-1270000 cm; span 2438400 cm; 24x24 cells of 101600 cm.
Raster: 1536 px = 64 px per cell (16 m per px). Row 0 = min world Y unless --flip-y.
"""
import argparse, glob, hashlib, json, math, os, struct, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageChops

ORIGIN = -1270000.0
SPAN = 2438400.0
CELL = 101600.0
N = 1536
PX_PER_CM = N / SPAN


def read_glb(path):
    f = open(path, 'rb').read()
    assert f[:4] == b'glTF', path
    off, chunks = 12, []
    while off < len(f):
        cl, ct = struct.unpack('<II', f[off:off + 8]); chunks.append((ct, f[off + 8:off + 8 + cl])); off += 8 + cl
    gl = json.loads(chunks[0][1]); bin_ = chunks[1][1]
    def acc(i):
        a = gl["accessors"][i]; bv = gl["bufferViews"][a["bufferView"]]
        o = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        ct = {5126: np.float32, 5123: np.uint16, 5125: np.uint32, 5121: np.uint8}[a["componentType"]]
        n = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[a["type"]]
        arr = np.frombuffer(bin_, dtype=ct, count=a["count"] * n, offset=o)
        return arr.reshape(-1, n) if n > 1 else arr
    tris = []
    for m in gl["meshes"]:
        for p in m["primitives"]:
            pos = acc(p["attributes"]["POSITION"]).astype(np.float64)
            idx = acc(p["indices"]).astype(np.int64) if "indices" in p else np.arange(len(pos))
            tris.append(pos[idx.reshape(-1, 3)])
    return np.concatenate(tris) if tris else np.zeros((0, 3, 3))


def quat_yaw_deg(r):
    # UE quaternion (X,Y,Z,W); yaw about Z.
    x, y, z, w = r.get("X", 0.0), r.get("Y", 0.0), r.get("Z", 0.0), r.get("W", 1.0)
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))


def load_layout(path):
    d = json.load(open(path))
    out = []
    for e in d:
        if e.get("Type") != "InstancedStaticMeshComponent":
            continue
        P = e.get("Properties", {})
        sm = P.get("StaticMesh") or {}
        op = sm.get("ObjectPath", "") if isinstance(sm, dict) else ""
        is_sos = "/SeaOfSand/" in op
        is_proxy = "LandscapeStreamingProxy" in op
        if not (is_sos or is_proxy):
            continue                      # landmark props (wreck debris etc) are not terrain
        key = op.split("/")[-1].split(".")[0]
        fam = (key.split("_")[2] if key.startswith("SM_SoS_") else "?") if is_sos else "Proxy"
        for inst in P.get("PerInstanceSMData") or e.get("PerInstanceSMData") or []:
            t = inst["TransformData"]; T = t["Translation"]; S = t.get("Scale3D", {})
            out.append((key, fam, float(T["X"]), float(T["Y"]), float(T.get("Z", 0.0)),
                        quat_yaw_deg(t.get("Rotation", {})), float(S.get("X", 1.0)), float(S.get("Y", 1.0))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layouts", default=os.path.expanduser(os.environ.get("DUNE_EXTRACT_DIR", "~/dune-extract") + "/blueprints/DuneSandbox/Content/Dune/Systems/Cartography/DeepDesert_1/Layouts"))
    ap.add_argument("--tiles", default=os.path.expanduser(os.environ.get("DUNE_EXTRACT_DIR", "~/dune-extract") + "/dd-terrain"))
    ap.add_argument("--out", default=os.path.expanduser("~/git/LastSietch/admin-backend/static/img/maps/dd-islands"))
    ap.add_argument("--only", default="", help="comma list of layout ids")
    ap.add_argument("--rock-m", type=float, default=6.0, help="triangle mean height above tile floor that counts as rock")
    ap.add_argument("--flip-y", action="store_true", help="row 0 = max world Y")
    ap.add_argument("--swap-xz", action="store_true", help="use glTF Z as UE X (tile axis convention)")
    ap.add_argument("--stamp", default="")
    ap.add_argument("--holo", action="store_true", help="also fold the islands into the holo heightfield (static/terrain)")
    ap.add_argument("--no-2d", action="store_true", help="skip the 2D mask/backdrop outputs (holo only)")
    ap.add_argument("--holo-base", default=os.path.expanduser("~/git/LastSietch/ops/dd-terrain-bake/base-july"), help="frozen July dune field (bake_terrain output)")
    ap.add_argument("--holo-out", default=os.path.expanduser("~/git/LastSietch/admin-backend/portal-nextgen/static/terrain"))
    ap.add_argument("--preview-dir", default=os.path.expanduser("~/git/LastSietch/ops/dd-terrain-bake/previews"), help="hillshade eyeballs (gitignored, not shipped)")
    ap.add_argument("--rock-min-m", type=float, default=10.0, help="every rock pixel stands at least this far above the local dune base")
    ap.add_argument("--rock-cap-m", type=float, default=60.0, help="clip rock relief (owner: no cartoon cliffs)")
    ap.add_argument("--plateau-m", type=float, default=25.0, help="shield-wall plateau floor above the dune base")
    a = ap.parse_args()
    if a.holo:
        sys.exit("FAIL: --holo is retired; the holo relief is bake_dd_full.py (2.26.0) and --holo would overwrite it "
                 "with the 384 islands fold")
    os.makedirs(a.out, exist_ok=True)
    stamp = a.stamp or time.strftime("%Y%m%d%H%M")

    glbs = {}
    for p in glob.glob(os.path.join(a.tiles, "sos-tiles", "**", "SM_SoS_*.glb"), recursive=True):
        glbs[os.path.basename(p)[:-4]] = p
    ids = [int(x) for x in a.only.split(",")] if a.only else list(range(12))
    manifest = {"stamp": stamp, "rockMeters": a.rock_m, "raster": N, "flipY": a.flip_y, "swapXZ": a.swap_xz, "layouts": {}}
    cache = {}
    missing = set()
    holo_entries = []
    G = 64                                   # samples per tile edge (16 m)
    gy, gx = np.mgrid[0:G, 0:G]
    gpts = np.stack([(gx + 0.5) * (CELL / 100.0 / G), (gy + 0.5) * (CELL / 100.0 / G)], axis=-1).reshape(-1, 2)  # metres, (x, y)

    def tile_surface(key):
        """Continuous height surface (G x G, metres) from the LOD mesh vertices by k-NN IDW,
        sand (0) where no vertex is within reach. Mirrors bake_terrain's reconstruction."""
        tris = read_glb(glbs[key])
        if not len(tris):
            return np.zeros((G, G))
        v = np.unique(tris.reshape(-1, 3), axis=0)
        vx = (v[:, 2] if a.swap_xz else v[:, 0]); vy = (v[:, 0] if a.swap_xz else v[:, 2]); vh = v[:, 1]
        pts = np.stack([vx, vy], axis=-1)
        d2 = ((gpts[:, None, :] - pts[None, :, :]) ** 2).sum(-1)          # (G*G, V)
        k = min(8, len(pts))
        nn = np.argpartition(d2, k - 1, axis=1)[:, :k]
        nd = np.take_along_axis(d2, nn, axis=1)
        w = 1.0 / (nd + 4.0)
        h = (w * vh[nn]).sum(1) / w.sum(1)
        h[np.sqrt(nd.min(1)) > 90.0] = 0.0                                # beyond reach = open sand
        return h.reshape(G, G)

    for lid in ids:
        lp = os.path.join(a.layouts, str(lid), f"BP_WorldMap_Meshes_DeepDesert_Layout_{lid}.json")
        inst = load_layout(lp)
        rock = np.zeros((N, N), dtype=bool)
        hgt = np.zeros((N, N), dtype=np.float32)          # rock surface height above the sand floor, metres
        fam_count = {}
        elevated = set()
        for key, fam, tx, ty, tz, yaw, sx, sy in inst:
            fam_count[fam] = fam_count.get(fam, 0) + 1
            col = int(round((tx - ORIGIN) / CELL)); row = int(round((ty - ORIGIN) / CELL))
            if tz >= 10000.0:                                               # the 150 m shield-wall plateau
                elevated.add((col, row)); m = np.ones((G, G), dtype=bool)
                hm = np.full((G, G), a.plateau_m, dtype=np.float32)
                if fam != "Proxy" and key in glbs:                          # SW/WLC/Eco tiles keep their own relief
                    if key not in cache:
                        cache[key] = tile_surface(key)
                    hm = np.maximum(hm, cache[key].astype(np.float32))
            elif fam == "Proxy":
                continue                                                    # ground-level dunes = sand
            else:
                if key not in glbs:
                    missing.add(key); continue
                if key not in cache:
                    cache[key] = tile_surface(key)
                surf = cache[key] + tz / 100.0
                m = surf > a.rock_m
                hm = np.where(m, surf, 0.0).astype(np.float32)
            q = int(round(yaw / 90.0)) % 4
            if q:
                m = np.rot90(m, k=-q); hm = np.rot90(hm, k=-q)
            if not (0 <= col < 24 and 0 <= row < 24):
                continue
            y0, x0 = row * G, col * G
            if a.flip_y:
                y0 = N - y0 - G; m = m[::-1]; hm = hm[::-1]
            rock[y0:y0 + G, x0:x0 + G] |= m
            hgt[y0:y0 + G, x0:x0 + G] = np.maximum(hgt[y0:y0 + G, x0:x0 + G], hm)
        mask = Image.fromarray((rock * 255).astype(np.uint8), "L")
        # tidy: close pinholes, soften the plateau's cell edges into a ragged shore
        m = mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(7))   # merge neighbouring outcrops
        m = m.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))      # drop specks
        if elevated:
            plat = np.zeros((N, N), dtype=bool)
            for (c, r) in elevated:
                plat[r * G:(r + 1) * G, c * G:(c + 1) * G] = True
            # domain-warp the cell-aligned plateau into a rugged shore: soften the mask into a
            # distance-like field, add two octaves of noise, re-threshold at the midpoint
            pm = np.asarray(Image.fromarray((plat * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(16)), dtype=np.float32)
            rng = np.random.default_rng(1000 + lid)                       # seeded: re-bakes reproduce the shore
            def noise(blur):
                g = Image.fromarray(np.clip(rng.normal(128, 80, (N, N)), 0, 255).astype(np.uint8), "L")
                return np.asarray(g.filter(ImageFilter.GaussianBlur(blur)), dtype=np.float32) - 128
            n1 = noise(9)
            n2 = noise(3)
            field = pm + n1 * 14.0 + n2 * 1.6
            shore = Image.fromarray(((field > 128) * 255).astype(np.uint8), "L")
            shore = shore.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
            m = ImageChops.lighter(ImageChops.subtract(m, Image.fromarray((plat * 255).astype(np.uint8), "L")), shore)
        arr = np.asarray(m) > 127
        band = sorted({r for (_c, r) in elevated})
        if a.no_2d:
            mp = os.path.join(a.out, f"dd-layout-{lid}-rock.png")
            if os.path.exists(mp):                                          # shipped 2D mask = geometry authority
                arr = np.asarray(Image.open(mp).convert("L")) > 127
        if a.holo:
            holo_entries.append(bake_holo(lid, arr, hgt, a, stamp))
        if a.no_2d:
            print(f"layout {lid}: holo only, rock {arr.mean()*100:.1f}%")
            continue
        m.save(os.path.join(a.out, f"dd-layout-{lid}-rock.png"), optimize=True)
        render(m, lid, a.out, stamp, heightfield(lid))
        manifest["layouts"][str(lid)] = {"rockFraction": round(float(arr.mean()), 4), "bandCellRows": band,
                                         "families": fam_count, "instances": len(inst)}
        print(f"layout {lid}: {len(inst)} SoS instances, rock {arr.mean()*100:.1f}%, band cell rows {band}, fams {fam_count}")
    if missing:
        print("WARNING missing GLBs:", sorted(missing)[:10], "…", len(missing))
    manifest["missingTiles"] = sorted(missing)
    if not a.no_2d:
        json.dump(manifest, open(os.path.join(a.out, "islands-manifest.json"), "w"), indent=1)
    if a.holo:
        write_holo_manifest(a, stamp, holo_entries)


def bake_holo(lid, rock, hgt, a, stamp):
    """Fold the islands into the holo heightfield: July dune base + rock relief, same
    384x384 Uint16 contract terrain.js already reads. Rock pixels rise at least rock_min_m
    above the local dune base (so a one-cell island still reads), the shield wall gets the
    plateau floor, everything is clipped at rock_cap_m. Block-MAX downsampling keeps a
    two-pixel outcrop from averaging away into sand."""
    meta = json.load(open(os.path.join(a.holo_base, f"layout_{lid}.json")))
    assert "islands" not in meta, "holo base already has islands folded in; point --holo-base at the frozen July field"
    W, H = meta["grid"]
    u = np.fromfile(os.path.join(a.holo_base, f"layout_{lid}.bin"), dtype="<u2").reshape(H, W).astype(np.float64)
    base = meta["zMinCm"] + u / 65535.0 * (meta["zMaxCm"] - meta["zMinCm"])          # cm
    f = N // W
    h = np.where(rock, np.clip(np.maximum(hgt, a.rock_min_m), a.rock_min_m, a.rock_cap_m), 0.0)
    blk = h.reshape(H, f, W, f).max(axis=(1, 3))                                      # metres, 0 = sand
    z = base + blk * 100.0
    zmin, zmax = float(z.min()), float(z.max())
    q = np.round((z - zmin) / (zmax - zmin) * 65535.0).astype("<u2")
    os.makedirs(a.holo_out, exist_ok=True)
    bin_path = os.path.join(a.holo_out, f"layout_{lid}.bin")
    q.tofile(bin_path)
    meta.update({"zMinCm": round(zmin, 2), "zMaxCm": round(zmax, 2), "floorCm": round(zmin, 2),
                 "islands": {"stamp": stamp, "rockMinM": a.rock_min_m, "rockCapM": a.rock_cap_m,
                             "plateauM": a.plateau_m, "rockCells": int((blk > 0).sum()),
                             "base": "ops/dd-terrain-bake/base-july"}})
    json.dump(meta, open(os.path.join(a.holo_out, f"layout_{lid}.json"), "w"), indent=2)
    # eyeball preview: hillshade of the folded field, lit from the upper left
    gy, gx = np.gradient(z / 100.0)
    shade = np.clip(140 + (gx - gy) * 2.2, 20, 235).astype(np.uint8)
    os.makedirs(a.preview_dir, exist_ok=True)
    Image.fromarray(shade, "L").resize((W * 2, H * 2), Image.BILINEAR).save(
        os.path.join(a.preview_dir, f"holo_layout_{lid}_islands.png"), optimize=True)
    print(f"  holo {lid}: z {zmin:.0f}..{zmax:.0f} cm, rock cells {(blk > 0).sum()}/{W*H}, "
          f"rock relief {blk[blk > 0].min() if (blk > 0).any() else 0:.0f}-{blk.max():.0f} m")
    return {"id": lid, "bin": f"layout_{lid}.bin", "meta": f"layout_{lid}.json",
            "sha": hashlib.sha256(open(bin_path, "rb").read()).hexdigest()}


def write_holo_manifest(a, stamp, entries):
    """Re-stamp manifest.json so terrain.js's ?v= cache key changes; keep the July fields."""
    mp = os.path.join(a.holo_out, "manifest.json")
    man = json.load(open(mp))
    by_id = {e["id"]: e for e in man.get("layouts", [])}
    for e in entries:
        by_id[e["id"]] = {**by_id.get(e["id"], {}), **e}
    man["layouts"] = [by_id[k] for k in sorted(by_id)]
    man["bakeStamp"] = int(time.time() * 1000)
    man["islands"] = {"stamp": stamp, "rockMinM": a.rock_min_m, "rockCapM": a.rock_cap_m, "plateauM": a.plateau_m,
                      "base": "ops/dd-terrain-bake/base-july", "layouts": sorted(e["id"] for e in entries)}
    json.dump(man, open(mp, "w"), indent=2)
    print(f"manifest: bakeStamp {man['bakeStamp']}, {len(entries)} layouts folded")


def heightfield(lid):
    """Baked 384x384 Uint16 relief from bake_terrain (holo tier), upsampled, or None."""
    d = os.path.expanduser("~/git/LastSietch/admin-backend/portal-nextgen/static/terrain")
    try:
        meta = json.load(open(os.path.join(d, f"layout_{lid}.json")))
        w, h = meta["grid"]
        z = np.fromfile(os.path.join(d, f"layout_{lid}.bin"), dtype="<u2").reshape(h, w).astype(np.float32)
        return Image.fromarray(z / 257.0).resize((N, N), Image.BILINEAR)
    except Exception:
        return None


def render(mask, lid, out, stamp, hf=None):
    """Backdrop in our palette: sand ground, umber rock with a lit edge, no grid (the engine draws it)."""
    sand = Image.new("RGB", (N, N), (214, 176, 118))
    # gentle sand mottle so the ground is not flat
    noise = Image.effect_noise((N, N), 18).filter(ImageFilter.GaussianBlur(3))
    sand = Image.composite(Image.new("RGB", (N, N), (200, 160, 104)), sand, noise.point(lambda v: 90 if v > 128 else 0))
    ripples = Image.effect_noise((N, N), 40).filter(ImageFilter.GaussianBlur(1)).point(lambda v: 30 if v > 150 else 0)
    sand = ImageChops.subtract(sand, Image.merge("RGB", (ripples, ripples, ripples)).point(lambda v: v // 3))
    rock = Image.new("RGB", (N, N), (122, 76, 48))
    rock_dark = Image.new("RGB", (N, N), (92, 56, 34))
    inner = mask.filter(ImageFilter.MinFilter(7)).filter(ImageFilter.GaussianBlur(2))
    rockface = Image.composite(rock_dark, rock, inner)              # darker interior, lighter rim
    edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(5))).filter(ImageFilter.GaussianBlur(1))
    rockface = Image.composite(Image.new("RGB", (N, N), (160, 108, 70)), rockface, edge)
    shadow = ImageChops.offset(mask, 4, 5).filter(ImageFilter.GaussianBlur(6))
    shadow = ImageChops.subtract(shadow, mask)
    ground = Image.composite(Image.new("RGB", (N, N), (168, 132, 84)), sand, shadow.point(lambda v: v // 2))
    if hf is not None:
        # hillshade from the baked relief, lit from the upper left, only inside rock
        z = np.asarray(hf.convert("F"), dtype=np.float32)
        gy, gx = np.gradient(z)
        shade = np.clip(128 + (gx - gy) * 6.0, 40, 220).astype(np.uint8)
        sh = Image.fromarray(shade, "L").filter(ImageFilter.GaussianBlur(1.2))
        shaded = ImageChops.multiply(rockface, Image.merge("RGB", (sh, sh, sh)).point(lambda v: min(255, v + 64)))
        rockface = Image.composite(shaded, rockface, mask.filter(ImageFilter.MinFilter(3)))
        rockface = Image.blend(rockface, ImageChops.screen(rockface, Image.merge("RGB", (sh, sh, sh)).point(lambda v: max(0, v - 150))), 0.35)
    img = Image.composite(rockface, ground, mask.filter(ImageFilter.GaussianBlur(0.8)))
    img.save(os.path.join(out, f"dd-layout-{lid}.webp"), quality=84, method=6)


if __name__ == "__main__":
    main()
