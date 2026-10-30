#!/usr/bin/env python3
"""Bake a social hub's War-Table relief from the game's own cartography diorama mesh.

The hub backdrop (admin-backend/static/img/maps/<hub>-map.webp) is OUR orthographic top-down render of the same
mesh: camera straight down glTF -Y, image right = +X,
image down = +Z, square frustum centred on the footprint with half-size `half` (2% pad). map_model.py's hub cal
is that image's world calibration (render_cal, 15 ground-truth anchors). So mesh -> image -> world is fixed:
  u = (x - cx) / (2*half) + 0.5,  v = (z - cz) / (2*half) + 0.5        (0..1 of the backdrop)
and the raster row/col IS the backdrop pixel, so the drape aligns by construction. Height: glTF Y scaled by
the same world-per-mesh-unit factor (cal span / 2*half) -> true scale.

  bake_hub.py <hub> <diorama.glb> --client <build> --preview-dir <outside static/>
Writes admin-backend/portal-nextgen/static/terrain/<hub>/{manifest.json,<hub>.json,<hub>.bin,<hub>_normal.webp}.
"""
import argparse, hashlib, importlib.util, json, os, sys, time
import numpy as np
from PIL import Image
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
spec = importlib.util.spec_from_file_location("hb", os.path.join(REPO, "ops/hagga-terrain-bake/hagga_bake.py"))
hb = importlib.util.module_from_spec(spec); spec.loader.exec_module(hb)

# Frustum framing of the live backdrops (SPARK-RUN-2026-06-12-NIGHT.md: frustum_half, center_xz, "== original").
FRAMING = {
    "arrakeen": {"half": 362.9702590942383, "cx": -32.421, "cz": -31.748},
    "harko-village": {"half": 354.0991181945801, "cx": 0.629, "cz": -105.779},
}
# The holo drape is the LIT re-render (render_hub_lit.py, identical camera/frustum, so the same framing): flat
# sandstone/slate that reads on the table. The 2D board keeps its own backdrop. Copied next to the bake so it ships
# with the frontend release.
DRAPE_SRC = {"arrakeen": "admin-backend/static/img/maps/arrakeen-map.lit-staged.webp",
             "harko-village": "admin-backend/static/img/maps/harko-village-map.lit-staged.webp"}
DRAPE_GAIN = {"arrakeen": 1.0, "harko-village": 1.0}


def hub_cal(hub):
    sys.path.insert(0, os.path.join(REPO, "admin-backend"))
    import map_model
    return map_model.MAPS[hub]["cal"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hub", choices=sorted(FRAMING)); ap.add_argument("glb")
    ap.add_argument("--client", required=True)
    ap.add_argument("--grid", type=int, default=1024); ap.add_argument("--normal-grid", type=int, default=2048)
    ap.add_argument("--work-grid", type=int, default=2048)
    ap.add_argument("--height-scale", type=float, default=1.0)
    ap.add_argument("--drape-gain", type=float, default=None,
                    help="drape exposure for the holo; default per hub (the hub renders are dark night scenes)")
    ap.add_argument("--out", default=None); ap.add_argument("--preview-dir", required=True)
    a = ap.parse_args()
    out = a.out or os.path.join(REPO, "admin-backend/portal-nextgen/static/terrain", a.hub)
    os.makedirs(out, exist_ok=True); os.makedirs(a.preview_dir, exist_ok=True)
    f = FRAMING[a.hub]; cal = hub_cal(a.hub)
    if cal.get("flipY") or abs(cal["spanX"] - cal["spanY"]) > 1e-6:
        sys.exit("FAIL: hub cal must be square and unflipped (render_cal contract)")
    cm_per_unit = cal["spanX"] / (2 * f["half"])
    v, i = hb.read_glb(a.glb)
    gx, gy, gz = v[:, 0], v[:, 1], v[:, 2]
    W = a.work_grid
    u = ((gx - f["cx"]) / (2 * f["half"]) + 0.5) * W
    w = ((gz - f["cz"]) / (2 * f["half"]) + 0.5) * W
    B = np.full((W, W), -np.inf); T = np.stack([u, w], 1)[i]; Z = (gy * cm_per_unit)[i]; rng = np.random.default_rng(0)
    e1 = T[:, 1] - T[:, 0]; e2 = T[:, 2] - T[:, 0]; area = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2
    k = np.clip((area * 3).astype(int) + 3, 3, 60000); rep = np.repeat(np.arange(len(T)), k)
    r1 = np.sqrt(rng.random(len(rep))); r2 = rng.random(len(rep)); A, Bc, C = 1 - r1, r1 * (1 - r2), r1 * r2
    P = A[:, None] * T[rep, 0] + Bc[:, None] * T[rep, 1] + C[:, None] * T[rep, 2]; Zs = A * Z[rep, 0] + Bc * Z[rep, 1] + C * Z[rep, 2]
    ix = P[:, 0].astype(int); iy = P[:, 1].astype(int); ok = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < W)
    np.maximum.at(B, (iy[ok], ix[ok]), Zs[ok])
    cov = np.isfinite(B)
    if cov.mean() < 0.2:
        sys.exit(f"FAIL: coverage {cov.mean():.1%}; wrong mesh or framing")
    floor = float(np.percentile(B[cov], 1))
    idx = ndimage.distance_transform_edt(~cov, return_distances=False, return_indices=True)
    full = B[idx[0], idx[1]]
    full = np.where(ndimage.binary_dilation(cov, iterations=4), full, floor)   # void around the diorama = floor
    np.save(os.path.join(a.preview_dir, f"{a.hub}_cm.npy"), full.astype(np.float32))
    h = np.asarray(Image.fromarray(full.astype(np.float32)).resize((a.grid, a.grid), Image.BOX)).astype(np.float64)
    zmin, zmax = float(h.min()), float(h.max())
    u16 = np.round((h - zmin) / (zmax - zmin) * 65535).astype("<u2")
    open(f"{out}/{a.hub}.bin", "wb").write(u16.tobytes())
    n = np.asarray(Image.fromarray(full.astype(np.float32)).resize((a.normal_grid, a.normal_grid), Image.BOX)).astype(np.float64)
    gyy, gxx = np.gradient(n, cal["spanX"] / a.normal_grid); nz = 1 / np.sqrt(1 + gxx ** 2 + gyy ** 2)
    rgb = np.stack([(-gxx * nz + 1) / 2, (-gyy * nz + 1) / 2, (nz + 1) / 2], -1)
    Image.fromarray(np.round(rgb * 255).astype("uint8")).save(f"{out}/{a.hub}_normal.webp", quality=90, method=6)
    sha = hashlib.sha256(u16.tobytes()).hexdigest(); stamp = int(time.time() * 1000)
    meta = {"grid": [a.grid, a.grid], "worldOrigin": [cal["originX"], cal["originY"]],
            "worldSpan": [cal["spanX"], cal["spanY"]], "zMinCm": round(zmin, 2), "zMaxCm": round(zmax, 2),
            "floorCm": round(floor, 2), "heightScale": a.height_scale,
            "normalMap": f"{a.hub}_normal.webp", "normalGrid": a.normal_grid,
            "drape": f"{a.hub}_drape.webp",
            "drapeGain": a.drape_gain if a.drape_gain is not None else DRAPE_GAIN[a.hub],
            "source": {"client": a.client, "mesh": os.path.basename(a.glb), "framing": f, "cmPerMeshUnit": cm_per_unit,
                       "coverage": round(float(cov.mean()), 4)}}
    Image.open(os.path.join(REPO, DRAPE_SRC[a.hub])).convert("RGB").save(f"{out}/{a.hub}_drape.webp", quality=88, method=6)
    json.dump(meta, open(f"{out}/{a.hub}.json", "w"), indent=2)
    json.dump({"bakeStamp": stamp, "map": a.hub, "raster": [a.grid, a.grid], "heightScale": a.height_scale,
               "layouts": [{"id": 0, "bin": f"{a.hub}.bin", "meta": f"{a.hub}.json", "sha": sha}]},
              open(f"{out}/manifest.json", "w"), indent=1)
    print(f"{a.hub}: coverage {cov.mean():.1%}, z {zmin/100:.0f}..{zmax/100:.0f} m, {cm_per_unit:.1f} cm/unit, sha {sha[:12]}")


if __name__ == "__main__":
    main()
