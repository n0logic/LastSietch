# Minimal voxel-space heightfield renderer: drape a colour map over a heightfield, oblique camera.
import sys, json, numpy as np
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
bake, tex_path, out, exag = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
m = json.load(open(bake + "/hagga.json"))
h = np.frombuffer(open(bake + "/hagga.bin", "rb").read(), dtype="<u2").reshape(384, 384).astype(float)
z = (m["zMinCm"] + h / 65535 * (m["zMaxCm"] - m["zMinCm"])) / 100.0          # metres
N = 1536
zN = np.asarray(Image.fromarray(z.astype("float32")).resize((N, N), Image.BICUBIC))
tex = np.asarray(Image.open(tex_path).convert("RGB").resize((N, N), Image.LANCZOS)).astype(float)
cell = 8128.0 / N                                                              # metres per texel
W, H = 1800, 1000
img = np.zeros((H, W, 3)); img[:] = (12, 10, 9)
ybuf = np.full(W, H, dtype=float)
cam_x, cam_y, cam_h = N / 2, N * 1.32, 2600.0 / cell                           # south of the basin, looking north
horizon, scale = 140.0, 900.0
for dist in np.arange(2, N * 1.6, 0.6):
    half = dist * 0.62
    xs = cam_x + np.linspace(-half, half, W); ys = np.full(W, cam_y - dist)
    ok = (xs >= 0) & (xs < N - 1) & (ys >= 0) & (ys < N - 1)
    if not ok.any():
        continue
    xi = np.clip(xs.astype(int), 0, N - 1); yi = np.clip(ys.astype(int), 0, N - 1)
    hz = zN[yi, xi] * exag / cell
    sy = (cam_h - hz) / dist * scale + horizon
    col = tex[yi, xi]
    for x in np.nonzero(ok & (sy < ybuf))[0]:
        top = max(int(sy[x]), 0); bot = int(ybuf[x])
        if top < bot:
            img[top:bot, x] = col[x]
        ybuf[x] = min(ybuf[x], sy[x])
Image.fromarray(img.astype("uint8")).save(out)
