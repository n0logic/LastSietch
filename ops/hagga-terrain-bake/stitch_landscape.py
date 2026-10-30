# Stitch the 64 decoded Hagga landscape tiles into one heightfield (cm), 1 sample per metre.
import json, sys, numpy as np
from PIL import Image
O, out = sys.argv[1], sys.argv[2]
Q, T = 127, 8 * 127
H = np.full((8 * T + 1, 8 * T + 1), np.nan, dtype=np.float32)
for ty in range(8):
    for tx in range(8):
        t = f"SOS-MinorErg_x{tx}_y{ty}"
        d = json.load(open(f"{O}/json/DuneSandbox/Content/Dune/Maps/Arrakis/SOC_1/Tiles/{t}.json"))
        rgba = np.asarray(Image.open(f"{O}/hm/{t}/Heightmap_0.png").convert("RGBA")).astype(np.int32)
        raw = (rgba[..., 0] << 8) | rgba[..., 1]
        px = next(e for e in d if e.get("Type") == "LandscapeStreamingProxy")["Properties"].get("LandscapeSectionOffset", {"X": 0, "Y": 0})
        root = next(e for e in d if e.get("Type") == "SceneComponent" and e.get("Name") == "RootComponent0")["Properties"]
        sz, oz = root.get("RelativeScale3D", {"Z": 100.0})["Z"], root.get("RelativeLocation", {"Z": 0.0})["Z"]
        for c in (e for e in d if e.get("Type") == "LandscapeComponent"):
            p = c["Properties"]
            gx, gy = int(p.get("SectionBaseX", 0)), int(p.get("SectionBaseY", 0))      # landscape quads from the landscape origin
            b = p["HeightmapScaleBias"]; u = int(round(b.get("Z", 0) * 1024)); v = int(round(b.get("W", 0) * 1024))
            H[gy:gy + Q + 1, gx:gx + Q + 1] = (raw[v:v + Q + 1, u:u + Q + 1] - 32768) * sz / 128.0 + oz
print("nan", int(np.isnan(H).sum()), "range m", np.nanmin(H) / 100, np.nanmax(H) / 100)
np.save(out, H)
